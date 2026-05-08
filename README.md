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

Requirements: Python 3.11+ (for stdlib `tomllib`), or Docker. A Google Cloud project with the Photos Library API enabled.

### 1. Google OAuth (one-time)

You need a Google OAuth client and a token granting the Photos Library scopes.

1. **Create a Google Cloud project** at https://console.cloud.google.com (or use an existing one). Enable the **Photos Library API** under *APIs & Services → Library*.

2. **Create an OAuth client** under *APIs & Services → Credentials*. Either application type works:
   - **Desktop app** — simplest. Loopback redirect (`http://localhost`) on any port is allowed automatically.
   - **Web application** — requires you to register specific redirect URIs (e.g. `http://localhost:8080/`). Pick this if you want strict-mode browsers / Google's stricter handling, or if you're sharing the client with another service.

   Either way, download the client JSON.

3. **Required scopes** for this pipeline:
   - `https://www.googleapis.com/auth/photoslibrary.appendonly` — upload media and add to app-created albums.
   - `https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata` — patch descriptions, remove items from album.
   - `https://www.googleapis.com/auth/photoslibrary.readonly` — read media items in known album ids (used for dedup against album contents).

   > **API quirk**: post-March 2025, even tokens holding `photoslibrary.readonly` get a `403 PERMISSION_DENIED` on `GET /v1/albums` for unverified apps. So this pipeline does **not** enumerate albums by title — it creates the album once, persists the `album_id` in `state.json`, and reuses it on every subsequent run.

4. **Get a token**. The bundled `uploader/reauth.py` does the OAuth dance — it starts a local HTTP server, opens a browser, captures the redirect, and writes the token in google-auth `authorized_user` format (the format `Credentials.to_authorized_user_info()` produces — embeds `client_id`/`client_secret`/`refresh_token`/`scopes`):

   ```bash
   pip install -r requirements.txt
   # Place the OAuth client JSON at ~/.config/google/credentials.json (the
   # script flattens `installed:`/`web:` wrappers automatically)
   python3 uploader/reauth.py
   ```

   The token is written to `~/.config/google/token.json`.

5. **Token refresh strategy** — pick one:
   - **Trusted Tester / unverified app in Testing mode**: refresh tokens revoke after **~7 days**. Either publish the OAuth consent screen (review can take days) or set up a weekly cron that re-runs `reauth.py` and notifies you to click an auth link. This pipeline assumes the token is kept fresh by *something* and never writes back to it.
   - **Verified or Internal app**: refresh tokens are long-lived; one-time setup.

### 2. Configure

```bash
cp .env.example .env                  # TAPESTRY_EMAIL / TAPESTRY_PASSWORD
cp config.example.toml config.toml    # school_slug, children, family authors,
                                      # google_photos.token_path
```

### 3. Run

#### Option A — Docker (recommended for servers)

A multi-arch image is published to `ghcr.io/albinati/tapestry-photo-scrapper:main` on every push.

```bash
# On the host, e.g. /srv/tapestry/
mkdir -p /srv/tapestry/{data,secrets}
cp .env config.toml docker-compose.yml /srv/tapestry/

# Either copy the token file directly:
cp ~/.config/google/token.json /srv/tapestry/secrets/token.json
# OR symlink it from wherever your refresh mechanism owns it:
# ln -s /path/to/managed-token.json /srv/tapestry/secrets/token.json

# Make data and secrets readable by uid 1001 (the container's user):
chown -R 1001:1001 /srv/tapestry/data /srv/tapestry/secrets

cd /srv/tapestry && docker compose pull
```

Trigger a run on demand:

```bash
docker compose -f /srv/tapestry/docker-compose.yml run --rm tapestry
```

That spins up an ephemeral container, runs the pipeline once, and removes itself — **zero idle footprint**, no `docker ps` clutter. Hardening included in the compose file: read-only root filesystem, all Linux capabilities dropped, `no-new-privileges`, journald logging, 256 MB memory ceiling.

Wire the trigger up to whatever fits: a daily cron, an inbox watcher (Tapestry sends a daily summary mail with `<author> added observation <title>` lines you can pattern-match on), a webhook, etc.

Run a one-off shell in the same environment:

```bash
docker compose run --rm tapestry bash
```

#### Option B — Bare-metal

```bash
git clone https://github.com/albinati/tapestry-photo-scrapper.git
cd tapestry-photo-scrapper
pip install -r requirements.txt
# (config.toml + .env + Google token already set up above)
bash run_all.sh
```

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
