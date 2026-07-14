# Seattle Used Car Tracker

A local dashboard of used-car listings across the Seattle metro area
(50 miles of downtown), aggregated from Craigslist, Autotrader, Cars.com
and CarMax — with filtering, saved favorites, price-drop tracking and
market trend charts. Sibling project of
[skagit-housing](https://github.com/connorfitz10/skagit-housing).

Data comes from each site's publicly visible search results (unofficial
feeds — the same JSON that powers their own search pages). This is for
personal use; be a polite guest (the fetch runs once a day, not
continuously) and expect that any endpoint could change without notice.

## Setup

```
pip install -r requirements.txt
```

## Daily use

1. Fetch/refresh the data (run once a day):

   ```
   python fetch_listings.py
   ```

   You can also fetch a subset: `python fetch_listings.py craigslist,carmax`

2. View the dashboard:

   ```
   python -m http.server 8743
   ```

   then open http://localhost:8743 in a browser.

## What each source contributes

| Source     | Coverage | Notes |
| ---------- | -------- | ----- |
| Craigslist | full sweep daily (~14k) | The private-party backbone. Each query returns at most ~360 results, so the fetch recursively splits price bands until everything fits. Mileage, photos and posting dates included. |
| CarMax     | full local sweep daily (~850) | Clean fixed-price benchmark, full listing detail. |
| Autotrader | daily sample (~3.5k) | Serves only the first ~430 results of any query and the metro has ~48k cars, so each price band contributes its cheapest/priciest ~430, alternating sort direction by day. Includes KBB fair-purchase-price, which powers the "Deals" tab. |
| Cars.com   | newest ~500 daily | New-to-market dealer inventory; overlaps Autotrader heavily, so it isn't swept exhaustively. |
| Edmunds    | nearest ~600 daily | Distance-sorted stable subset (same cars re-seen daily). Includes Edmunds' own market-value estimate and deal rating. Their bot protection answers only the first request per session, so the fetch uses one session per page. |

| Dealer sites | full sweep daily (~500) | Direct from big local dealerships' own websites: Honda/Toyota of Seattle + Klein Honda (Team Velocity platform), Jerry Smith Chevrolet (Burlington) + Dewey Griffin Subaru (Bellingham) (Dealer eProcess platform, parsed from schema.org JSON-LD with recursive price-band splitting). Add stores to `DEALER_SITES` in fetch_listings.py. Foothills Toyota was evaluated but publishes no prices; Skagit Ford and Blade Chevrolet (Dealer Inspire platform) block automated access. |

(Amazon Autos was evaluated and skipped: its storefront is fully
JS-rendered with no reachable inventory feed, and the pilot's used
inventory is tiny. Costco Auto Program was evaluated and skipped: it is
a dealer referral program with no browsable inventory or prices.)

Because Autotrader and Cars.com are samples, market *totals* are
indicative rather than a census; Craigslist and CarMax numbers are
complete. Listings sharing a VIN across sources get a "N sources" badge.

## How price-drop tracking works

Sources only report each listing's *current* price. This project builds
the history itself: every run stores a snapshot in `data/listings.db`
(SQLite).

- **First time a listing appears** → its price is recorded as `original_price`.
- **Price differs from last run** → a row is added to `price_history`.
- **Listing vanishes from its source** → marked inactive after a per-source
  grace period (2 days for the fully-swept sources, longer for sampled
  ones so a listing outside today's sample window isn't declared sold).

The dashboard's "Price drops" tab and ▼ badges compare current price
against original price. Day one has no drops by definition — the longer
the fetch runs daily, the richer the history gets.

## Files

| File                 | Purpose                                          |
| -------------------- | ------------------------------------------------ |
| `fetch_listings.py`  | Pulls all four sources, updates SQLite, exports JSON |
| `index.html`         | The dashboard (static, no build step)            |
| `daily_update.sh`    | Fetch + git push; run daily by launchd/cron      |
| `data/listings.db`   | SQLite: `listings`, `price_history`, `aggregates` |
| `data/listings.json` | Export read by the dashboard                     |

## How it's published

The live site is **https://connorfitz10.github.io/seattle-cars/**
(GitHub Pages, serving this repo's `main` branch via
`.github/workflows/pages.yml`).

The daily fetch likely cannot run in GitHub Actions (these sites tend to
block cloud-runner IPs — Redfin did for skagit-housing; test with the
manual `fetch.yml` workflow). Instead, schedule `daily_update.sh` on a
local machine. On macOS:

```
crontab -e
# add:
30 7 * * * /path/to/seattle-cars/daily_update.sh >> /tmp/seattle-cars.log 2>&1
```

(cron on modern macOS needs Full Disk Access for the calling shell, or
use `launchctl` with a LaunchAgent; on Windows use Task Scheduler like
skagit-housing does.)
