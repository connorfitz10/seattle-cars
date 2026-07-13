#!/bin/zsh
# Fetch fresh listings and push, which republishes the GitHub Pages site.
# Scheduled daily via launchd (see README) or run by hand.
set -e
cd "$(dirname "$0")"
python3 fetch_listings.py
git add data/listings.db data/listings.json
if git diff --cached --quiet; then
  echo "No changes to commit."
else
  git commit -m "Daily data update $(date +%F)"
  git push
fi
