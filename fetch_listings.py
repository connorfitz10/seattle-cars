"""Fetch used-car listings for the Seattle metro area (50 mi of 98101) from
Craigslist, Autotrader, Cars.com and CarMax, and maintain a local
price-history database.

Run daily (manually or via scheduler):
    python fetch_listings.py

Outputs:
    data/listings.db      - SQLite: listings + price_history + aggregates
    data/listings.json    - export consumed by the dashboard (index.html)

Source notes (all unofficial feeds; be a polite guest, expect breakage):
  craigslist  sapi.craigslist.org JSON search API. Each query returns at
              most ~360 newest results, so the sweep recursively splits
              price bands until every band fits. Full sweep daily,
              by-owner and by-dealer separately.
  autotrader  Listing JSON embedded in each search page (__NEXT_DATA__).
              Pagination caps at ~1000 per query and the metro has ~48k
              cars, so fixed price bands rotate: one third of the bands
              are swept per day (full market refresh every 3 days).
  carscom     Per-card JSON in data-vehicle-details attributes. Only the
              newest ~500 listings are fetched daily (the rest of its
              inventory overlaps Autotrader heavily).
  carmax      Clean JSON search API. Full local-store sweep daily.
"""

import ast
import html as html_mod
import json
import re
import sqlite3
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from curl_cffi import requests

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "listings.db"
JSON_PATH = DATA_DIR / "listings.json"

ZIP, RADIUS = "98101", 50

# --- craigslist ---
CL_REGIONS = [                    # (areaId, warm-up URL)
    (2, "https://seattle.craigslist.org/search/cta"),      # covers Skagit (skc)
    (217, "https://bellingham.craigslist.org/search/cta"), # Whatcom County
]
CL_SAPI = "https://sapi.craigslist.org/web/v8/postings/search/full"
CL_BAND_CAP = 350                 # split a price band when it holds more
CL_MAX_REQUESTS = 600
CL_DELAY_S = 0.35

# --- autotrader ---
AT_URL = "https://www.autotrader.com/cars-for-sale/all-cars/seattle-wa"
AT_BANDS = [(0, 4999), (5000, 9999), (10000, 14999), (15000, 19999),
            (20000, 24999), (25000, 29999), (30000, 39999), (40000, 59999),
            (60000, 2_000_000)]
AT_PAGE = 100                     # numRecords per request
AT_MAX_PAGES = 4                  # each query serves only its first ~430 results
AT_DELAY_S = 0.5

# --- cars.com ---
CARSCOM_URL = "https://www.cars.com/shopping/results/"
CARSCOM_PAGES = 15                # newest ~500 used listings per day
CARSCOM_NEW_PAGES = 8             # newest ~280 new listings per day
CARSCOM_DELAY_S = 0.5

# --- carmax ---
CARMAX_API = "https://www.carmax.com/cars/api/search/run"
CARMAX_DELAY_S = 0.3

# --- edmunds ---
EDMUNDS_API = ("https://www.edmunds.com/gateway/api/purchasefunnel/v1/srp/"
               "inventory")
EDMUNDS_PAGES = 30                # nearest ~600 used listings per day
EDMUNDS_NEW_PAGES = 10            # nearest ~200 new listings per day
EDMUNDS_DELAY_S = 2.5

# --- carvana ---
CARVANA_URL = "https://www.carvana.com/cars"
CARVANA_PAGES = 20                # ~440 cars/day of the national inventory
CARVANA_IMG = "https://cdnblob.fastly.carvana.io"
CARVANA_DELAY_S = 0.8

# --- local dealership sites, keyed by inventory platform ---
# teamvelocity: server-rendered grid at /inventory/used?page=N
# dep (Dealer eProcess): 12 vehicles per page as schema.org JSON-LD, no
#   working pagination, but price-range filters (&s:pr=1&pr=lo:hi) allow
#   the same recursive band-splitting used for craigslist.
DEALER_SITES = [
    # Honda of Seattle and Toyota of Seattle are sister stores sharing one
    # used-inventory pool — one fetch covers both.
    ("Honda/Toyota of Seattle", "teamvelocity",
     "https://www.hondaofseattle.com", "Seattle"),
    ("Klein Honda", "teamvelocity",
     "https://www.kleinhonda.com", "Everett"),
    ("Jerry Smith Chevrolet", "dep",
     "https://www.jerrysmithchevrolet.com/search/used-burlington-wa/?tp=used",
     "Burlington"),
    ("Dewey Griffin Subaru", "dep",
     "https://www.deweygriffinsubaru.com/search/used-bellingham-wa/?tp=used",
     "Bellingham"),
    # Foothills shows no prices on its site's search grid, but real prices
    # ride along in the same schema.org JSON-LD the parser already reads.
    ("Foothills Toyota", "dep",
     "https://www.foothillstoyota.com/search/used-foothills-toyota-burlington-wa/?cy=98233&lc=97&tp=used",
     "Burlington"),
    ("Dwayne Lane's Auto Family", "dep",
     "https://www.dwaynelane.com/search/used/?tp=used",
     "Everett/Arlington"),
    # separate store site; its inventory is NOT in the dwaynelane.com sweep
    ("Dwayne Lane's Skagit Subaru", "dep",
     "https://www.dwaynelaneskagitsubaru.com/search/used/?tp=used",
     "Burlington"),
    ("Honda of Burlington", "teamvelocity",
     "https://hondaburlington.com", "Burlington"),
    ("Lynnwood Honda", "teamvelocity",
     "https://www.lynnwoodhonda.com", "Lynnwood"),
    ("Carter Volkswagen", "dep",
     "https://www.cartervw.com/search/used/?tp=used", "Seattle"),
    ("Carter Acura of Lynnwood", "dep",
     "https://www.carteracura.com/search/used/?tp=used", "Lynnwood"),
    ("Doug's Family Dealerships", "dep",
     "https://www.dougs.com/search/used/?tp=used", "Lynnwood"),
    ("Rodland Toyota", "dep",
     "https://www.rodlandtoyota.com/search/used/?tp=used", "Everett"),
    ("Volkswagen of Bellingham", "dealeron",
     "https://www.volkswagenofbellingham.com", "Bellingham"),
    ("Magic Toyota", "dealeron",
     "https://www.magictoyota.com", "Edmonds"),
    ("Toyota of Lake City", "dealeron",
     "https://www.toyotaoflakecity.com", "Seattle"),
    ("Younker Nissan", "dep",
     "https://www.younkernissan.com/search/used/?tp=used", "Renton"),
    ("Bellevue Nissan", "dep",
     "https://www.bellevuenissan.com/search/used/?tp=used", "Bellevue"),
]
DEALER_MAX_PAGES = 12
DEALER_DELAY_S = 0.8
DEP_PAGE_SIZE = 12

# How many days a listing may go unseen before it is marked inactive, and
# the minimum fetch size for the sweep to be trusted for deactivation
# (a partial API failure must not mass-deactivate the database).
SOURCE_POLICY = {
    "craigslist": {"grace_days": 2, "min_fetch": 2000},
    "autotrader": {"grace_days": 10, "min_fetch": 1000},
    "carscom":    {"grace_days": 14, "min_fetch": 100},
    "carmax":     {"grace_days": 2, "min_fetch": 200},
    "edmunds":    {"grace_days": 10, "min_fetch": 200},
    "dealer":     {"grace_days": 2, "min_fetch": 50},
    "carvana":    {"grace_days": 10, "min_fetch": 100},
}

# Make aliases for parsing craigslist titles (alias -> canonical).
MAKES = {}
for m in ["Acura", "Alfa Romeo", "Aston Martin", "Audi", "BMW", "Buick",
          "Cadillac", "Chevrolet", "Chrysler", "Datsun", "Dodge", "Ferrari",
          "Fiat", "Ford", "Genesis", "GMC", "Honda", "Hummer", "Hyundai",
          "Infiniti", "Isuzu", "Jaguar", "Jeep", "Kia", "Lamborghini",
          "Land Rover", "Lexus", "Lincoln", "Lucid", "Maserati", "Mazda",
          "McLaren", "Mercedes-Benz", "Mercury", "Mini", "Mitsubishi",
          "Nissan", "Oldsmobile", "Plymouth", "Polestar", "Pontiac",
          "Porsche", "Ram", "Rivian", "Saab", "Saturn", "Scion", "Smart",
          "Subaru", "Suzuki", "Tesla", "Toyota", "Volkswagen", "Volvo"]:
    MAKES[m.lower()] = m
MAKES.update({"chevy": "Chevrolet", "vw": "Volkswagen", "mercedes": "Mercedes-Benz",
              "benz": "Mercedes-Benz", "landrover": "Land Rover", "infinity": "Infiniti",
              "mini cooper": "Mini", "range rover": "Land Rover"})
MAKE_RE = re.compile(
    r"\b(" + "|".join(sorted((re.escape(a) for a in MAKES), key=len, reverse=True)) + r")\b",
    re.I)
YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")


def parse_title(title):
    """Best-effort (year, make, model) from a free-text craigslist title."""
    year = make = model = None
    if not title:
        return year, make, model
    ym = YEAR_RE.search(title)
    if ym:
        year = int(ym.group(1))
    mm = MAKE_RE.search(title)
    if mm:
        make = MAKES[mm.group(1).lower()]
        rest = title[mm.end():].strip(" -:,.")
        toks = re.split(r"[\s,/|(]+", rest)
        words = []
        for t in toks[:2]:
            if not t or YEAR_RE.match(t) or not re.search(r"[a-z0-9]", t, re.I):
                break
            words.append(t)
        if words:
            model = " ".join(words)[:24].strip(" -.!*")
    return year, make, model


def to_int(v):
    if v is None:
        return None
    try:
        return int(float(str(v).replace(",", "").replace("$", "").strip() or 0)) or None
    except ValueError:
        return None


# --------------------------------------------------------------------------
# craigslist
# --------------------------------------------------------------------------

def cl_query(session, area_id, min_price, max_price, purveyor):
    params = {
        "batch": f"{area_id}-0-360-1-0",
        "cc": "US", "lang": "en", "searchPath": "cta",
        "min_price": str(min_price), "max_price": str(max_price),
        "purveyor": purveyor,
    }
    r = session.get(CL_SAPI, params=params, timeout=30,
                    headers={"Referer": "https://seattle.craigslist.org/"})
    r.raise_for_status()
    body = r.json()
    data = body.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"bad response (errors={body.get('errors')})")
    return data


def cl_parse_items(data, purveyor, out):
    dec = data.get("decode")
    if not isinstance(dec, dict):     # empty bands ship "decode": 0
        return
    min_id = dec.get("minPostingId", 0)
    min_ts = dec.get("minPostedDate", 0)
    descs = None
    try:
        descs = ast.literal_eval(dec.get("locationDescriptions") or "") or None
    except (ValueError, SyntaxError):
        pass
    for it in data.get("items", []):
        if not isinstance(it, list) or len(it) < 4:
            continue
        tags = {s[0]: s[1:] for s in it if isinstance(s, list) and len(s) > 1
                and isinstance(s[0], int)}
        title = it[-1] if isinstance(it[-1], str) else None
        pid = min_id + it[0] if isinstance(it[0], int) else None
        if pid is None:
            continue
        posted = (datetime.fromtimestamp(min_ts + it[1]).date().isoformat()
                  if isinstance(it[1], int) else None)
        price = it[3] if isinstance(it[3], int) and it[3] > 0 else None
        lat = lng = city = None
        geo = next((x for x in it if isinstance(x, str) and "~" in x), None)
        if geo:
            parts = geo.split("~")
            try:
                lat, lng = float(parts[1]), float(parts[2])
                if descs:
                    di = int(parts[0].split(":")[-1])
                    if 0 < di < len(descs):
                        city = str(descs[di])
            except (ValueError, IndexError):
                pass
        slug = tags.get(6, [None])[0]
        token = tags.get(13, [None])[0]
        url = (f"https://www.craigslist.org/view/d/{slug}/{token}"
               if slug and token else None)
        image = None
        imgs = tags.get(4)
        if imgs and isinstance(imgs[0], str) and ":" in imgs[0]:
            image = ("https://images.craigslist.org/"
                     + imgs[0].split(":", 1)[1] + "_300x300.jpg")
        year, make, model = parse_title(title)
        key = f"craigslist:{pid}"
        out[key] = {
            "key": key, "source": "craigslist", "source_id": str(pid),
            "vin": None, "url": url, "title": title,
            "year": year, "make": make, "model": model, "trim": None,
            "price": price, "mileage": to_int(tags.get(9, [None])[0]),
            "city": city, "lat": lat, "lng": lng,
            "seller_type": purveyor, "seller_name": None,
            "kbb_fair_price": None, "image_url": image,
            "dom": None, "posted": posted, "condition": "used",
        }


def cl_sweep(session, area_id, purveyor, lo, hi, out, budget, depth=0):
    if budget[0] <= 0:
        return
    budget[0] -= 1
    data = None
    for attempt in (1, 2):
        try:
            data = cl_query(session, area_id, lo, hi, purveyor)
            break
        except Exception as e:
            if attempt == 2:
                print(f"  craigslist band {lo}-{hi} failed: {e}")
                return
            time.sleep(3)
    time.sleep(CL_DELAY_S)
    total = data.get("totalResultCount", 0)
    cl_parse_items(data, purveyor, out)
    if total > CL_BAND_CAP and lo < hi and depth < 24:
        mid = (lo + hi) // 2
        cl_sweep(session, area_id, purveyor, lo, mid, out, budget, depth + 1)
        cl_sweep(session, area_id, purveyor, mid + 1, hi, out, budget, depth + 1)


def fetch_craigslist():
    out, budget, expected = {}, [CL_MAX_REQUESTS], 0
    for area_id, warm in CL_REGIONS:
        session = requests.Session(impersonate="chrome")
        session.get(warm, timeout=30)
        for purveyor in ("owner", "dealer"):
            before = len(out)
            try:
                expected += cl_query(session, area_id, 0, 2_000_000,
                                     purveyor).get("totalResultCount", 0)
            except Exception:
                pass
            cl_sweep(session, area_id, purveyor, 0, 2_000_000, out, budget)
            print(f"  craigslist area {area_id} by-{purveyor}: "
                  f"{len(out) - before} listings")
    print(f"  craigslist requests used: {CL_MAX_REQUESTS - budget[0]}")
    # A sweep well short of the site's own count means bands failed
    # (rate limiting); deactivating on such a partial view would wrongly
    # mark the unfetched bands' listings as gone.
    complete = expected == 0 or len(out) >= 0.85 * expected
    if not complete:
        print(f"  craigslist sweep partial ({len(out)}/{expected}); "
              "deactivation will be skipped")
    return list(out.values()), complete


# --------------------------------------------------------------------------
# autotrader
# --------------------------------------------------------------------------

AT_NEXT_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def at_query(session, lo, hi, first, sort):
    r = session.get(AT_URL, params={
        "searchRadius": str(RADIUS), "zip": ZIP,
        "minPrice": str(lo), "maxPrice": str(hi),
        "numRecords": str(AT_PAGE), "firstRecord": str(first),
        "sortBy": sort,
    }, timeout=40)
    r.raise_for_status()
    m = AT_NEXT_RE.search(r.text)
    if not m:
        raise RuntimeError("__NEXT_DATA__ not found (page layout changed?)")
    state = json.loads(m.group(1))["props"]["pageProps"]["__eggsState"]
    return state.get("inventory", {}) or {}


def at_parse(inv, out):
    added = 0
    for lid, h in inv.items():
        if not isinstance(h, dict) or not h.get("id"):
            continue
        key = f"autotrader:{h['id']}"
        if key in out:
            continue
        pricing = h.get("pricingDetail") or {}
        price = to_int(pricing.get("salePrice") or pricing.get("displayPrice"))
        mileage = to_int((h.get("mileage") or {}).get("value"))
        images = (h.get("images") or {}).get("sources") or []
        image = images[0].get("src") if images and isinstance(images[0], dict) else None
        if image and image.startswith("//"):
            image = "https:" + image
        owner = h.get("ownerName")
        out[key] = {
            "key": key, "source": "autotrader", "source_id": str(h["id"]),
            "vin": h.get("vin"),
            "url": f"https://www.autotrader.com/cars-for-sale/vehicle/{h['id']}",
            "title": h.get("title"),
            "year": to_int(h.get("year")),
            "make": (h.get("make") or {}).get("name") if isinstance(h.get("make"), dict) else h.get("make"),
            "model": (h.get("model") or {}).get("name") if isinstance(h.get("model"), dict) else h.get("model"),
            "trim": (h.get("trim") or {}).get("name") if isinstance(h.get("trim"), dict) else h.get("trim"),
            "price": price, "mileage": mileage,
            "city": None, "lat": None, "lng": None,
            "seller_type": "owner" if owner == "Private Seller Exchange" else "dealer",
            "seller_name": owner,
            "kbb_fair_price": to_int(pricing.get("kbbFppAmount")),
            "image_url": image,
            "dom": to_int(h.get("daysOnSite")), "posted": None,
            "condition": "new" if str(h.get("title") or "").startswith("New")
                         else "used",
        }
        added += 1
    return added


def at_sweep(session, lo, hi, sort, out):
    for page in range(AT_MAX_PAGES):
        inv = None
        for attempt in (1, 2, 3):
            try:
                inv = at_query(session, lo, hi, page * AT_PAGE, sort)
                break
            except Exception as e:
                if attempt == 3:
                    print(f"  autotrader band {lo}-{hi} page {page}: {e}")
                    return session
                # rate-limited mid-band: back off and continue on a
                # fresh session (limits appear to be per-session)
                time.sleep(attempt * 8)
                session = requests.Session(impersonate="chrome")
        time.sleep(AT_DELAY_S)
        added = at_parse(inv, out)
        if added < AT_PAGE * 0.5:          # short page: end of this band
            return session
    return session


def fetch_autotrader():
    """Autotrader serves only the first ~430 results of any query, and the
    metro has ~48k cars, so this is a daily *sample*, not a full sweep:
    each price band contributes its ~430 cheapest or priciest cars, with
    the sort direction alternating by day so both ends of every band are
    covered over time. Craigslist + CarMax remain the exhaustive sources."""
    sort = ("derivedpriceASC", "derivedpriceDESC")[date.today().toordinal() % 2]
    out = {}
    for lo, hi in AT_BANDS:
        session = requests.Session(impersonate="chrome")   # per-session limits
        before = len(out)
        at_sweep(session, lo, hi, sort, out)
        print(f"  autotrader ${lo}-${hi} ({sort}): {len(out) - before} listings")
    return list(out.values())


# --------------------------------------------------------------------------
# cars.com
# --------------------------------------------------------------------------

CARSCOM_CARD_RE = re.compile(r'data-vehicle-details="(.*?)"', re.S)


def fetch_carscom():
    session = requests.Session(impersonate="chrome")
    out = {}
    sweeps = [("used", CARSCOM_PAGES), ("new", CARSCOM_NEW_PAGES)]
    for stock_type, pages in sweeps:
        for page in range(1, pages + 1):
            try:
                r = session.get(CARSCOM_URL, params={
                    "stock_type": stock_type, "zip": ZIP,
                    "maximum_distance": str(RADIUS),
                    "page_size": "100", "page": str(page),
                    "sort": "listed_at_desc",
                }, timeout=40)
                r.raise_for_status()
            except Exception as e:
                print(f"  cars.com {stock_type} page {page}: {e}")
                break
            time.sleep(CARSCOM_DELAY_S)
            cards = CARSCOM_CARD_RE.findall(r.text)
            if not cards:
                break
            carscom_parse_cards(cards, out)
    return list(out.values())


def carscom_parse_cards(cards, out):
    for raw in cards:
        try:
            d = json.loads(html_mod.unescape(raw))
        except json.JSONDecodeError:
            continue
        lid = d.get("listingId")
        if not lid:
            continue
        key = f"carscom:{lid}"
        out[key] = {
            "key": key, "source": "carscom", "source_id": lid,
            "vin": d.get("vin"),
            "url": f"https://www.cars.com/vehicledetail/{lid}/",
            "title": " ".join(str(x) for x in
                              [d.get("year"), d.get("make"), d.get("model"),
                               d.get("trim")] if x),
            "year": to_int(d.get("year")), "make": d.get("make"),
            "model": d.get("model"), "trim": d.get("trim"),
            "price": to_int(d.get("price")), "mileage": to_int(d.get("mileage")),
            "city": None, "lat": None, "lng": None,
            "seller_type": "dealer", "seller_name": None,
            "kbb_fair_price": None, "image_url": None,
            "dom": None, "posted": None,
            "condition": "new" if str(d.get("stockType", "")).lower() == "new"
                         else "used",
        }


# --------------------------------------------------------------------------
# carmax
# --------------------------------------------------------------------------

def fetch_carmax():
    session = requests.Session(impersonate="chrome")
    out, skip, total = {}, 0, 1
    while skip < total and skip < 5000:
        try:
            r = session.get(CARMAX_API, params={
                "uri": "/cars", "skip": str(skip), "take": "100",
                "zipCode": ZIP, "radiusMiles": str(RADIUS), "shipping": "0",
            }, timeout=40, headers={"Referer": "https://www.carmax.com/cars"})
            r.raise_for_status()
            d = r.json()
        except Exception as e:
            print(f"  carmax skip={skip}: {e}")
            break
        time.sleep(CARMAX_DELAY_S)
        total = d.get("totalCount") or 0
        items = d.get("items", [])
        if not items:
            break
        for h in items:
            stock = h.get("stockNumber")
            if not stock:
                continue
            key = f"carmax:{stock}"
            out[key] = {
                "key": key, "source": "carmax", "source_id": str(stock),
                "vin": h.get("vin"),
                "url": f"https://www.carmax.com/car/{stock}",
                "title": " ".join(str(x) for x in
                                  [h.get("year"), h.get("make"), h.get("model"),
                                   h.get("trim")] if x),
                "year": to_int(h.get("year")), "make": h.get("make"),
                "model": h.get("model"), "trim": h.get("trim"),
                "price": to_int(h.get("basePrice")), "mileage": to_int(h.get("mileage")),
                "city": h.get("storeCity"), "lat": None, "lng": None,
                "seller_type": "dealer",
                "seller_name": f"CarMax {h.get('storeName')}" if h.get("storeName") else "CarMax",
                "kbb_fair_price": None,
                "image_url": h.get("heroImageUrl")
                             or f"https://img2.carmax.com/assets/{stock}/hero.jpg?width=320",
                "dom": None, "posted": None, "condition": "used",
            }
        skip += 100
    return list(out.values())


# --------------------------------------------------------------------------
# edmunds
# --------------------------------------------------------------------------

def fetch_edmunds():
    """Edmunds' inventory API answers the first request of a fresh session,
    then its bot protection (Akamai) serves empty decoys — so every page
    gets its own session. Results are distance-sorted, giving a stable
    'nearest ~600' subset that is re-seen daily (good drop tracking).
    Includes Edmunds' own market-value estimate and deal rating."""
    today_ms = time.time() * 1000
    out = {}
    sweeps = [("used,cpo", EDMUNDS_PAGES), ("new", EDMUNDS_NEW_PAGES)]
    for inventory_type, pages in sweeps:
        for page in range(1, pages + 1):
            results = None
            # rate-limit flags clear after ~45s and are fingerprint-
            # sensitive, so retries escalate the backoff and rotate the
            # impersonated browser
            for attempt, (fingerprint, backoff) in enumerate(
                    [("chrome", 3), ("safari", 45), ("safari_ios", 60)], 1):
                session = requests.Session(impersonate=fingerprint)
                try:
                    r = session.get(EDMUNDS_API, params={
                        # NB: this endpoint silently ignores lowercase param
                        # names on paged requests (serving NEW cars) — the
                        # camelCase spellings are the ones that stick.
                        "zip": ZIP, "radius": str(RADIUS),
                        "inventoryType": inventory_type,
                        "pageNum": str(page), "pagesize": "20",
                    }, timeout=30, headers={
                        "Referer": "https://www.edmunds.com/inventory/srp.html"})
                    r.raise_for_status()
                    results = r.json().get("inventories", {}).get("results") or []
                    if results:
                        break
                except Exception as e:
                    if attempt == 3:
                        print(f"  edmunds {inventory_type} page {page}: {e}")
                time.sleep(backoff)
            if not results:
                print(f"  edmunds {inventory_type} stopped at page {page} "
                      "(empty response twice)")
                break
            edmunds_parse_results(results, today_ms, out)
            time.sleep(EDMUNDS_DELAY_S)
    return list(out.values())


def edmunds_parse_results(results, today_ms, out):
    for h in results:
        vin = h.get("vin")
        if not vin:
            continue
        vi = h.get("vehicleInfo") or {}
        si = vi.get("styleInfo") or {}
        prices = h.get("prices") or {}
        pv = (h.get("thirdPartyInfo") or {}).get("priceValidation") or {}
        dealer = h.get("dealerInfo") or {}
        addr = dealer.get("address") or {}
        listed = h.get("listedSince")
        dom = (int((today_ms - listed) / 86_400_000)
               if isinstance(listed, (int, float)) and listed > 0 else None)
        make, model, year = si.get("make"), si.get("model"), si.get("year")
        slug = "/".join(str(x).lower().replace(" ", "-")
                        for x in (make, model, year) if x)
        key = f"edmunds:{vin}"
        out[key] = {
            "key": key, "source": "edmunds", "source_id": vin, "vin": vin,
            "url": f"https://www.edmunds.com/{slug}/vin/{vin}/" if slug else None,
            "title": None,
            "year": to_int(year), "make": make, "model": model,
            "trim": si.get("trim"),
            "price": to_int(prices.get("displayPrice")
                            or prices.get("advertisedPrice")),
            "mileage": to_int(vi.get("mileage")),
            "city": addr.get("city"), "lat": None, "lng": None,
            "seller_type": "owner" if h.get("isPrivateParty") else "dealer",
            "seller_name": (dealer.get("displayInfo") or {}).get(
                "parentDealershipName"),
            "kbb_fair_price": to_int(pv.get("listPriceEstimate")),
            "image_url": None,
            "dom": dom, "posted": None,
            "condition": "new" if str(h.get("type", "")).upper() == "NEW"
                         else "used",
        }


# --------------------------------------------------------------------------
# carvana
# --------------------------------------------------------------------------

def fetch_carvana():
    """Carvana's search pages are Next.js server-rendered; the vehicle
    records ride in escaped 'flight data' chunks. National fixed-price
    inventory (delivers locally) — a rotating ~440-car sample serves as a
    price benchmark like CarMax, not a census."""
    session = requests.Session(impersonate="chrome")
    out = {}
    for page in range(1, CARVANA_PAGES + 1):
        try:
            r = session.get(CARVANA_URL, params={"page": str(page)}, timeout=40)
            r.raise_for_status()
        except Exception as e:
            print(f"  carvana page {page}: {e}")
            break
        time.sleep(CARVANA_DELAY_S)
        text = r.text.replace('\\"', '"')
        chunks = text.split('"vehicleId":')
        added = 0
        for chunk in chunks[1:]:
            cid = re.match(r"([0-9]+)", chunk)
            vin = re.search(r'"vin":"([A-HJ-NPR-Z0-9]{17})"', chunk)
            if not (cid and vin):
                continue
            key = f"carvana:{vin.group(1)}"
            if key in out:
                continue
            g = lambda pat: (re.search(pat, chunk) or [None, None])[1]
            img = g(r'"(?:heroImageUrl|imageUrl)":"([^"]+)"')
            if img and img.startswith("/"):
                img = CARVANA_IMG + img
            out[key] = {
                "key": key, "source": "carvana", "source_id": cid.group(1),
                "vin": vin.group(1),
                "url": f"https://www.carvana.com/vehicle/{cid.group(1)}",
                "title": None,
                "year": to_int(g(r'"year":([0-9]{4})')),
                "make": g(r'"make":"([^"]+)"'),
                "model": g(r'"model":"([^"]+)"'),
                "trim": g(r'"trim":"([^"]+)"'),
                "price": to_int(g(r'"incentivizedPrice":([0-9]{4,6})')
                                or g(r'"price":([0-9]{4,6})')),
                "mileage": to_int(g(r'"mileage":([0-9]+)')),
                "city": None, "lat": None, "lng": None,
                "seller_type": "dealer", "seller_name": "Carvana",
                "kbb_fair_price": None, "image_url": img,
                "dom": None, "posted": None, "condition": "used",
            }
            added += 1
        if added == 0:
            break
    return list(out.values())


# --------------------------------------------------------------------------
# local dealership sites (Team Velocity platform)
# --------------------------------------------------------------------------

DEALER_CARD_RE = re.compile(r'data-itemid="([^"]+)"')
DEALER_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def dealer_parse_card(itemid, segment, dealer_name, base_url, city, out,
                      condition="used"):
    parts = itemid.rsplit("-", 1)
    if len(parts) != 2 or not DEALER_VIN_RE.match(parts[1]):
        return
    ymt, vin = parts
    tokens = ymt.split("-")
    # hyphenated makes split apart ("Mercedes-Benz-C 300-…"); re-join via
    # the known-makes table
    make, rest = tokens[0], tokens[1:]
    if len(tokens) >= 2 and f"{tokens[0]} {tokens[1]}".lower() in MAKES:
        make, rest = f"{tokens[0]} {tokens[1]}", tokens[2:]
    make = MAKES.get(make.lower(), make)
    model = rest[0] if rest else None
    trim = rest[1] if len(rest) > 1 else None

    href = re.search(r'href="(https?://[^"]*?/viewdetails/[^"]+?)[?"]', segment)
    url = href.group(1) if href else f"{base_url}/inventory/used"
    year = None
    ym = re.search(r"/viewdetails/[^/]+/[^/]+/((?:19|20)\d{2})-", url)
    if ym:
        year = int(ym.group(1))
    # cards carry both a "Selling Price" and a doc-fee-inclusive
    # "No-Haggle, Upfront Price"; parse the selling price (the basis the
    # aggregators list) so the tracked price is stable. (Sic: the site
    # spells it "Sellng".)
    price = (re.search(r"Sell\w*g\s+Price[^$]{0,80}\$\s*([\d,]{4,})", segment)
             or re.search(r"\$\s*([\d,]{4,})", segment))
    mileage = (re.search(r'details-item-label">\s*Mileage\s*</div>\s*'
                         r'<div class="details-item-value">\s*([\d,]+)', segment)
               or re.search(r'"mileageFromOdometer"[^}]*?"value":\s*"?([\d,]+)',
                            segment))
    if mileage is None and condition != "new":
        # loose fallback; unsafe on new-car cards, whose only "N Miles"
        # text is the powertrain warranty
        mileage = re.search(r"([\d,]{4,})\s*Miles\b", segment)
    image = re.search(r'(https://content\.homenetiol\.com/[^"\s]+?\.(?:jpg|png|webp))',
                      segment)
    key = f"dealer:{vin}"
    out[key] = {
        "key": key, "source": "dealer", "source_id": vin, "vin": vin,
        "url": url,
        "title": " ".join(str(x) for x in (year, make, model, trim) if x),
        "year": year, "make": make, "model": model, "trim": trim,
        "price": to_int(price.group(1)) if price else None,
        "mileage": to_int(mileage.group(1)) if mileage else None,
        "city": city, "lat": None, "lng": None,
        "seller_type": "dealer", "seller_name": dealer_name,
        "kbb_fair_price": None,
        "image_url": image.group(1) if image else None,
        "dom": None, "posted": None, "condition": condition,
    }


def tv_fetch_dealer(session, dealer_name, base_url, city, out,
                    condition="used"):
    for page in range(1, DEALER_MAX_PAGES + 1):
        try:
            r = session.get(f"{base_url}/inventory/{condition}",
                            params={"page": str(page)}, timeout=40)
            r.raise_for_status()
        except Exception as e:
            print(f"  {dealer_name} page {page}: {e}")
            break
        time.sleep(DEALER_DELAY_S)
        marks = list(DEALER_CARD_RE.finditer(r.text))
        added = 0
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(r.text)
            seg = r.text[m.end():end]
            k = len(out)
            dealer_parse_card(m.group(1), seg, dealer_name, base_url,
                              city, out, condition)
            added += len(out) - k
        if added == 0:      # past the last page (or repeated content)
            break


DEP_LDJSON_RE = re.compile(
    r'<script type="application/ld\+json">\s*(.*?)\s*</script>', re.S)
DEP_TOTAL_RE = re.compile(r"([0-9,]+)\s*(?:vehicles found|Results Found)", re.I)


def dep_parse_page(html, dealer_name, base_url, city, out, condition="used"):
    origin = re.match(r"https?://[^/]+", base_url).group(0)
    prefix = "New" if condition == "new" else "Used"
    for block in DEP_LDJSON_RE.findall(html):
        try:
            d = json.loads(block)
        except json.JSONDecodeError:
            continue
        items = d.get("@graph", [d]) if isinstance(d, dict) else d
        for it in items:
            if not isinstance(it, dict):
                continue
            vin = it.get("vehicleIdentificationNumber") or it.get("vin")
            name = it.get("name") or ""
            # the prefix check also skips the featured-vehicles widget,
            # which lists cars of the other condition without offers
            if not vin or not name.startswith(prefix):
                continue
            offer = it.get("offers")
            if isinstance(offer, list):
                offer = offer[0] if offer else {}
            offer = offer or {}
            mileage = it.get("mileageFromOdometer")
            if isinstance(mileage, dict):
                mileage = mileage.get("value")
            brand = it.get("brand") or it.get("manufacturer") or {}
            make = brand.get("name") if isinstance(brand, dict) else brand
            images = it.get("image")
            image = images[0] if isinstance(images, list) and images else images
            path = offer.get("url") or ""
            key = f"dealer:{vin}"
            out[key] = {
                "key": key, "source": "dealer", "source_id": vin, "vin": vin,
                "url": origin + path if path.startswith("/") else (path or base_url),
                "title": name,
                "year": to_int(it.get("vehicleModelDate") or it.get("releaseDate")),
                "make": MAKES.get(str(make).lower(), make) if make else None,
                "model": it.get("model"),
                "trim": it.get("vehicleConfiguration"),
                "price": to_int(offer.get("price")),
                "mileage": to_int(mileage),
                "city": city, "lat": None, "lng": None,
                "seller_type": "dealer", "seller_name": dealer_name,
                "kbb_fair_price": None,
                "image_url": image if isinstance(image, str) else None,
                "dom": None, "posted": None, "condition": condition,
            }


def dep_fetch_dealer(session, dealer_name, srp_url, city, out,
                     condition="used", lo=1, hi=500_000, depth=0):
    try:
        r = session.get(f"{srp_url}&s:pr=1&pr={lo}:{hi}", timeout=40)
        r.raise_for_status()
    except Exception as e:
        print(f"  {dealer_name} band {lo}-{hi}: {e}")
        return
    time.sleep(DEALER_DELAY_S)
    m = DEP_TOTAL_RE.search(r.text)
    total = to_int(m.group(1)) if m else 0
    dep_parse_page(r.text, dealer_name, srp_url, city, out, condition)
    if total and total > DEP_PAGE_SIZE and lo < hi and depth < 12:
        mid = (lo + hi) // 2
        dep_fetch_dealer(session, dealer_name, srp_url, city, out,
                         condition, lo, mid, depth + 1)
        dep_fetch_dealer(session, dealer_name, srp_url, city, out,
                         condition, mid + 1, hi, depth + 1)


DEALERON_ID_RE = re.compile(r'"?[Dd]ealerId"?[\'":=\s]+(\d+)')
DEALERON_PAGE_RE = re.compile(r'"?[Pp]ageId"?[\'":=\s]+(\d+)')


def dealeron_fetch_dealer(session, dealer_name, base_url, city, out,
                          condition="used"):
    """DealerOn sites: each SRP page embeds a dealerId/pageId pair whose
    'cosmos' JSON API returns the full vehicle cards."""
    path = f"/{condition}-inventory/index.htm"
    try:
        r = session.get(base_url + path, timeout=40)
        r.raise_for_status()
    except Exception as e:
        print(f"  {dealer_name} {condition} srp: {e}")
        return
    did, pid = DEALERON_ID_RE.search(r.text), DEALERON_PAGE_RE.search(r.text)
    if not (did and pid):
        print(f"  {dealer_name} {condition}: dealerId/pageId not found")
        return
    api = (f"{base_url}/api/vhcliaa/vehicle-pages/cosmos/srp/vehicles/"
           f"{did.group(1)}/{pid.group(1)}")
    pt, total_pages = 1, 1
    while pt <= total_pages and pt <= 30:
        try:
            rr = session.get(f"{api}?pt={pt}", timeout=40,
                             headers={"Referer": base_url + path})
            rr.raise_for_status()
            d = rr.json()
        except Exception as e:
            print(f"  {dealer_name} {condition} pt={pt}: {e}")
            return
        time.sleep(DEALER_DELAY_S)
        total_pages = ((d.get("Paging") or {}).get("PaginationDataModel")
                       or {}).get("TotalPages", 1)
        for card in d.get("DisplayCards", []):
            v = card.get("VehicleCard") or {}
            vin = v.get("VehicleVin")
            if not vin:
                continue
            mileage = to_int(re.sub(r"[^0-9]", "",
                                    str(v.get("VehicleMileage")
                                        or v.get("Mileage") or "")) or None)
            image = None
            im = re.search(r'https://[^"\s\\]+?\.(?:jpg|jpeg|png|webp)[^"\s\\]*',
                           json.dumps(v.get("VehicleImageModel") or {}))
            if im:
                image = im.group(0)
            url_path = v.get("VehicleDetailUrl") or ""
            key = f"dealer:{vin}"
            out[key] = {
                "key": key, "source": "dealer", "source_id": vin, "vin": vin,
                "url": base_url + url_path if url_path.startswith("/")
                       else (url_path or base_url),
                "title": v.get("VehicleName"),
                "year": to_int(v.get("VehicleYear")),
                "make": v.get("VehicleMake"), "model": v.get("VehicleModel"),
                "trim": v.get("VehicleTrim"),
                "price": to_int(v.get("VehicleInternetPrice"))
                         or to_int(v.get("VehicleMsrp")),
                "mileage": mileage,
                "city": city, "lat": None, "lng": None,
                "seller_type": "dealer", "seller_name": dealer_name,
                "kbb_fair_price": None, "image_url": image,
                "dom": None, "posted": None, "condition": condition,
            }
        pt += 1


def fetch_dealers():
    out = {}
    for dealer_name, platform, url, city in DEALER_SITES:
        session = requests.Session(impersonate="chrome")
        before = len(out)
        for condition in ("used", "new"):
            if platform == "teamvelocity":
                tv_fetch_dealer(session, dealer_name, url, city, out,
                                condition)
            elif platform == "dep":
                dep_fetch_dealer(session, dealer_name,
                                 url.replace("tp=used", f"tp={condition}"),
                                 city, out, condition)
            elif platform == "dealeron":
                dealeron_fetch_dealer(session, dealer_name, url, city, out,
                                      condition)
        print(f"  {dealer_name}: {len(out) - before} vehicles")
    return list(out.values())


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------

def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS listings (
            key            TEXT PRIMARY KEY,
            source         TEXT,
            source_id      TEXT,
            vin            TEXT,
            url            TEXT,
            title          TEXT,
            year           INTEGER,
            make           TEXT,
            model          TEXT,
            trim           TEXT,
            price          INTEGER,
            original_price INTEGER,
            mileage        INTEGER,
            city           TEXT,
            lat            REAL,
            lng            REAL,
            seller_type    TEXT,
            seller_name    TEXT,
            kbb_fair_price INTEGER,
            image_url      TEXT,
            dom            INTEGER,
            posted         TEXT,
            first_seen     TEXT,
            last_seen      TEXT,
            active         INTEGER DEFAULT 1,
            condition      TEXT DEFAULT 'used'
        );
        CREATE TABLE IF NOT EXISTS price_history (
            key   TEXT,
            date  TEXT,
            price INTEGER,
            PRIMARY KEY (key, date)
        );
        CREATE TABLE IF NOT EXISTS aggregates (
            date          TEXT,
            source        TEXT,
            active        INTEGER,
            median_price  INTEGER,
            new_today     INTEGER,
            drops_today   INTEGER,
            PRIMARY KEY (date, source)
        );
        CREATE INDEX IF NOT EXISTS idx_listings_vin ON listings(vin);
        CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(source, active);
    """)
    # condition column added after initial schema; migrate older databases.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)")}
    if "condition" not in cols:
        conn.execute("ALTER TABLE listings ADD COLUMN condition TEXT DEFAULT 'used'")
        # the autotrader sweep always covered new cars too; backfill them
        conn.execute("UPDATE listings SET condition='new' "
                     "WHERE source='autotrader' AND title LIKE 'New %'")


UPDATE_COLS = ["vin", "url", "title", "year", "make", "model", "trim", "price",
               "mileage", "city", "lat", "lng", "seller_type", "seller_name",
               "kbb_fair_price", "image_url", "dom", "posted", "condition"]


def upsert(conn, rows, today):
    new_count = drop_count = 0
    for r in rows:
        existing = conn.execute(
            "SELECT price, original_price FROM listings WHERE key = ?",
            (r["key"],)).fetchone()
        if existing is None:
            cols = ["key", "source", "source_id"] + UPDATE_COLS + \
                   ["original_price", "first_seen", "last_seen", "active"]
            vals = [r["key"], r["source"], r["source_id"]] + \
                   [r.get(c) for c in UPDATE_COLS] + [r.get("price"), today, today, 1]
            conn.execute(
                f"INSERT INTO listings ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})", vals)
            new_count += 1
            if r.get("price"):
                conn.execute("INSERT OR REPLACE INTO price_history VALUES (?,?,?)",
                             (r["key"], today, r["price"]))
        else:
            old_price = existing[0]
            # Listing first appeared priceless ("contact for price"); the
            # price showing up later becomes its original price.
            if r.get("price") and existing[1] is None:
                conn.execute("UPDATE listings SET original_price=? WHERE key=?",
                             (r["price"], r["key"]))
                conn.execute("INSERT OR REPLACE INTO price_history VALUES (?,?,?)",
                             (r["key"], today, r["price"]))
            if r.get("price") and old_price and r["price"] != old_price:
                conn.execute("INSERT OR REPLACE INTO price_history VALUES (?,?,?)",
                             (r["key"], today, r["price"]))
                if r["price"] < old_price:
                    drop_count += 1
            sets = ", ".join(f"{c}=?" for c in UPDATE_COLS)
            conn.execute(
                f"UPDATE listings SET {sets}, last_seen=?, active=1 WHERE key=?",
                [r.get(c) for c in UPDATE_COLS] + [today, r["key"]])
    return new_count, drop_count


def deactivate(conn, source, fetched_count, today):
    policy = SOURCE_POLICY[source]
    if fetched_count < policy["min_fetch"]:
        print(f"  {source}: fetch too small ({fetched_count}), skipping deactivation")
        return 0
    cutoff = (date.fromisoformat(today)
              - timedelta(days=policy["grace_days"])).isoformat()
    cur = conn.execute(
        "UPDATE listings SET active = 0 WHERE source = ? AND active = 1 "
        "AND last_seen < ?", (source, cutoff))
    return cur.rowcount


def record_aggregates(conn, today, new_by_source, drops_by_source):
    # trend series stay used-cars-only, both for continuity with data
    # recorded before new-car tracking and because that's the market
    # being tracked; new-car counts are visible in the dashboard filters.
    rows = conn.execute(
        "SELECT source, price FROM listings WHERE active = 1 "
        "AND (condition IS NULL OR condition != 'new')").fetchall()
    by_source = {}
    for source, price in rows:
        by_source.setdefault(source, []).append(price)
    by_source["all"] = [p for v in by_source.values() for p in v]
    for source, prices in by_source.items():
        sane = sorted(p for p in prices if p and p >= 500)
        med = int(statistics.median(sane)) if sane else None
        conn.execute(
            "INSERT OR REPLACE INTO aggregates VALUES (?,?,?,?,?,?)",
            (today, source, len(prices), med,
             new_by_source.get(source, 0), drops_by_source.get(source, 0)))


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

def export_json(conn, today):
    conn.row_factory = sqlite3.Row
    listings = []
    vin_counts = {}
    for row in conn.execute("SELECT * FROM listings WHERE active = 1"):
        if row["vin"]:
            vin_counts[row["vin"]] = vin_counts.get(row["vin"], 0) + 1
    for row in conn.execute("SELECT * FROM listings WHERE active = 1"):
        d = dict(row)
        history = [{"date": h["date"], "price": h["price"]} for h in conn.execute(
            "SELECT date, price FROM price_history WHERE key = ? ORDER BY date",
            (d["key"],))]
        if len(history) > 1:
            d["price_history"] = history
        if d["original_price"] and d["price"] and d["price"] < d["original_price"]:
            d["price_drop"] = d["original_price"] - d["price"]
            d["price_drop_pct"] = round(100 * d["price_drop"] / d["original_price"], 1)
        else:
            d["price_drop"] = 0
            d["price_drop_pct"] = 0
        if d["price"] and d["kbb_fair_price"]:
            d["vs_kbb_pct"] = round(100 * d["price"] / d["kbb_fair_price"])
        else:
            d["vs_kbb_pct"] = None
        d["vin_sources"] = vin_counts.get(d["vin"], 1) if d["vin"] else 1
        for drop in ("source_id", "lat", "lng", "last_seen", "active"):
            d.pop(drop, None)
        if d["source"] != "craigslist" and d.get("year"):
            d.pop("title", None)          # reconstructable from year/make/model
        if d.get("condition") != "new":
            d.pop("condition", None)      # 'used' is the dashboard default
        listings.append({k: v for k, v in d.items()
                         if v is not None and v != 0 or k == "price_drop"})

    trends = [dict(r) for r in conn.execute(
        "SELECT * FROM aggregates ORDER BY date")]
    out = {"updated": today,
           "region": "Seattle metro (50 mi) + Skagit/Whatcom dealers",
           "listings": listings, "trends": trends}
    JSON_PATH.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    return len(listings)


# --------------------------------------------------------------------------

FETCHERS = {
    "craigslist": fetch_craigslist,
    "autotrader": fetch_autotrader,
    "carscom": fetch_carscom,
    "carmax": fetch_carmax,
    "edmunds": fetch_edmunds,
    "dealer": fetch_dealers,
    "carvana": fetch_carvana,
}


def main():
    today = date.today().isoformat()
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    only = sys.argv[1].split(",") if len(sys.argv) > 1 else list(FETCHERS)
    new_by_source, drops_by_source = {}, {}
    for source in only:
        print(f"Fetching {source} ({today})...")
        try:
            result = FETCHERS[source]()
        except Exception as e:
            print(f"  {source} FAILED: {e}")
            continue
        rows, complete = result if isinstance(result, tuple) else (result, True)
        new_count, drop_count = upsert(conn, rows, today)
        gone = deactivate(conn, source, len(rows) if complete else 0, today)
        conn.commit()
        new_by_source[source] = new_count
        drops_by_source[source] = drop_count
        print(f"  {source}: {len(rows)} fetched, {new_count} new, "
              f"{drop_count} price drops, {gone} left the market")

    record_aggregates(conn, today, new_by_source, drops_by_source)
    conn.commit()
    exported = export_json(conn, today)
    conn.close()
    print(f"Done. Exported {exported} active listings to {JSON_PATH}")


if __name__ == "__main__":
    main()
