#!/usr/bin/env bash
# End-to-end Tapestry refresh: scrape new diary entries + photos, regenerate
# the per-child reports, dedupe-upload to the Google Photos "Tapestry" album.
#
# Idempotent — safe to re-run. Reads creds from .env (Tapestry login) and
# ~/.config/google/{credentials,token}.json (Google Photos OAuth).
#
# If the Google token is missing the photoslibrary.appendonly +
# readonly.appcreateddata scopes, run uploader/reauth.py first.

set -euo pipefail
cd "$(dirname "$0")"

echo "── 1/3 Scraping Tapestry ──"
python3 scraper.py

echo
echo "── 2/3 Building per-child reports ──"
python3 build_summary.py

echo
echo "── 3/3 Uploading new photos to Google Photos 'Tapestry' album ──"
python3 uploader/tapestry_upload.py

echo
echo "Done. Reports written as REPORT_<child>.md per configured child."
echo "Album: https://photos.google.com/ → 'Tapestry' (or whatever album_title in config.toml)."
