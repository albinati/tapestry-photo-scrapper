# tapestry-photo-scrapper

A small pipeline that mirrors a [Tapestry Journal](https://tapestryjournal.com) (a UK-popular early-years/nursery learning journal platform) account into a Google Photos album, with diary-derived captions per photo and per-child markdown reports.

It does three things:

1. **Scrape** new diary observations and photos from Tapestry's parent-side API (idempotent, watermarked).
2. **Build** per-child markdown reports summarizing the new entries (titles, dates, authors, comments).
3. **Upload** the photos to a Google Photos album, with a caption built from the diary notes + teacher comments and a per-photo "Xy Ym old" age string.

It also handles a handful of real-world edge cases I hit while building this for my own family:

- The same photo tagged under multiple children gets uploaded **once** (content-hash dedup).
- Observations authored by family members (whose photos are already in other Google Photos albums) are skipped.
- Two teachers writing separate diaries for the same school week with the same title produce identical filenames but different content; the uploader keeps one and **patches the description** to include both diaries' text.
- Google Photos' library-dedup quirk (where re-uploading bytes that already exist in the user's library returns OK but doesn't add to the album) is handled with an explicit `albums:batchAddMediaItems` follow-up.
- Album order is **chronological across all configured children**, not grouped by child.
- After a successful run, local photos are auto-deleted (they're in the album now). Diary markdown stays for the report.

## Status

I built this to handle my own kids' nursery records. It's working code, not a polished product — expect to read the source if something breaks. PRs welcome but not soliciting them.

## Setup

Requirements: Python 3.11+ (uses `tomllib` from stdlib), and a Google Cloud project with the Photos Library API enabled.

```bash
git clone https://github.com/albinati/tapestry-photo-scrapper.git
cd tapestry-photo-scrapper
pip install requests beautifulsoup4 lxml google-auth-oauthlib

cp .env.example .env                 # fill in Tapestry email + password
cp config.example.toml config.toml   # fill in school slug, children, family authors
```

Place your Google OAuth client (from a Desktop app type in Google Cloud Console) at `~/.config/google/credentials.json`. Then run a one-time auth:

```bash
python3 uploader/reauth.py
```

It opens a browser, you consent to the requested scopes, the script captures the redirect on a local loopback port and writes `~/.config/google/token.json`.

The required scopes are:

- `https://www.googleapis.com/auth/photoslibrary.appendonly` — upload + add to app-created albums.
- `https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata` — patch descriptions, remove items from album.
- `https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata` — list app-created albums and items in them.

> **Note**: the broader `photoslibrary.readonly` scope is deprecated as of 2025 and now returns 403 even on tokens that have it. Use `readonly.appcreateddata` instead.

## Running

End-to-end:

```bash
bash run_all.sh
```

That's `scraper.py` → `build_summary.py` → `uploader/tapestry_upload.py`. Idempotent — re-running with no new observations is a no-op.

Or step-by-step:

```bash
python3 scraper.py            # writes tapestry-{photos,diary}/<child>/...
python3 build_summary.py      # writes REPORT_<child>.md per kid
python3 uploader/tapestry_upload.py  # uploads + auto-deletes local photos
```

### State / watermark

The scraper records the highest processed observation id in `state.json`:

```json
{
  "last_observation_id": 24528,
  "last_scrape_at": "2026-05-08T22:48:00Z",
  "last_upload_at": "2026-05-08T23:30:00Z"
}
```

Subsequent runs walk Tapestry's `/api/4/observations/list` newest-first and stop the moment they hit an id ≤ the watermark. Bump the watermark down (or delete `state.json`) to re-fetch older entries; the scraper will skip files that already exist on disk.

### Cleanup behaviour

After a successful upload, the script deletes any local photo whose filename is in the album (uploaded), or whose diary author is in the configured family list (intentionally skipped — already in your other albums), or that the API rejected as a library-dup. Set `UPLOAD_KEEP_LOCAL=1` to disable.

Diary markdown files are always kept (they're tiny and the reports use them).

## How it talks to Tapestry

Tapestry's parent-facing API isn't documented but is straightforward. After a session-cookie login, the relevant endpoints are:

- `GET /api/4/observations/list?perPage=50&cursor=...` — paginated list, newest first.
- `GET /api/4/observations/get/{id}` — single observation with full notes, comments, and media URLs.

Login is a CSRF-protected `POST /login` with `email`, `password`, and the `_token` value scraped from the school landing page.

## Configuration

`config.toml` (see `config.example.toml` for the full schema):

```toml
[tapestry]
school_slug = "your-school-slug"

[paths]
data_root = "/home/me/tapestry"

[[children]]
api_name = "First Last"
folder   = "FirstName"
display  = "First"
dob      = "2020-01-15"

[google_photos]
album_title    = "Tapestry"
family_authors = ["Parent One", "Parent Two"]
```

`.env` (gitignored):

```bash
TAPESTRY_EMAIL=...
TAPESTRY_PASSWORD=...
```

## File layout

```
tapestry-photo-scrapper/
├── scraper.py                    # Tapestry scraper
├── build_summary.py              # Markdown reports per child
├── run_all.sh                    # Orchestrator
├── config.py                     # Config loader
├── config.example.toml           # Sample config
├── .env.example                  # Sample secrets
├── uploader/
│   └── tapestry_upload.py        # Google Photos uploader
│   └── reauth.py                 # OAuth bootstrap
└── (gitignored at runtime)
    ├── .env
    ├── config.toml
    ├── state.json
    ├── scrape_summary.json
    ├── tapestry-photos/<child>/  # downloaded JPGs
    ├── tapestry-diary/<child>/   # diary markdown
    └── REPORT_<child>.md
```

## License

MIT.
