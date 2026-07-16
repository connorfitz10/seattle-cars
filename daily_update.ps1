# Fetch fresh listings and push, which republishes the GitHub Pages site.
# Scheduled daily by Windows Task Scheduler (see README) — same pattern as
# skagit-housing's "Skagit Housing Daily Fetch" task.
Set-Location $PSScriptRoot
git pull --rebase
python fetch_listings.py
git add data/listings.db data/listings.json
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "Daily data update $(Get-Date -Format yyyy-MM-dd)"
    git push
} else {
    Write-Output "No changes to commit."
}
