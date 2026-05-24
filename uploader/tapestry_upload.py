#!/usr/bin/env python3
"""Tapestry → Google Photos uploader.

Builds a chronological queue of newly-scraped photos across all configured
children, dedupes by content hash and against the album, skips
family-authored observations, and uploads each photo with a caption built
from the diary notes/comments.

After a successful run, deletes local photos that are confirmed in the
album OR were intentionally skipped (family-authored, library-dups).
"""
import sys
import os
# Allow `import config` from the repo root
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import json
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

import config

CFG = config.load()

PHOTO_DIRS = {c.folder: CFG.photo_root / c.folder for c in CFG.children}
DIARY_DIRS = {c.folder: CFG.diary_root / c.folder for c in CFG.children}
SCRAPE_SUMMARY = CFG.scrape_summary
DOBS = {c.folder: c.dob for c in CFG.children}
DISPLAY_NAMES = {c.folder: c.display for c in CFG.children}

TOKEN_FILE = CFG.google_token
ALBUM_TITLE = CFG.album_title
FAMILY_AUTHORS = CFG.family_authors

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_access_token() -> str:
    """Read the OpenClaw-managed authorized-user JSON, refresh in-memory if
    needed, return a valid access_token. Does NOT write back to the token
    file — OpenClaw owns its lifecycle and runs a weekly cron to re-issue
    when the refresh_token is revoked (GCP project is in Testing mode).
    """
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception as e:
            raise RuntimeError(
                "Google token refresh failed (refresh_token may be revoked). "
                "Wait for the weekly renewal cron (Mon 10 AM London) or "
                "run uploader/reauth.py manually. "
                f"Underlying error: {e}"
            ) from e
    return creds.token


def age_str(dob: date, photo_date: date) -> str:
    years = photo_date.year - dob.year - (
        (photo_date.month, photo_date.day) < (dob.month, dob.day)
    )
    months = (photo_date.month - dob.month) % 12
    if years == 0:
        return f"{months}m"
    elif months == 0:
        return f"{years}y"
    else:
        return f"{years}y {months}m"


def slug_from_photo(filename: str) -> str:
    """Extract slug from photo filename: YYYY-MM-DD_SLUG_01.jpg → SLUG (lowercased, no hyphens)"""
    stem = Path(filename).stem  # e.g. 2025-07-01_Science-fun-with-Miss-Giovana-AlkaS_01
    parts = stem.split("_")
    if len(parts) >= 2:
        slug = "_".join(parts[1:-1]) if len(parts) > 2 else parts[1]
        return slug.lower().replace("-", "").replace("_", "")
    return stem.lower().replace("-", "").replace("_", "")


def slug_from_diary(filename: str) -> str:
    """Extract slug from diary filename: DD-Mon-YYYY_SLUG.md or 01-----202_SLUG.md"""
    stem = Path(filename).stem
    # Match DD-Mon-YYYY_ or DD-----202_ prefix
    m = re.match(r'^\d{2}[-\w]+_(.+)$', stem)
    if m:
        return m.group(1).lower().replace("-", "").replace("_", "")
    return stem.lower().replace("-", "").replace("_", "")


def date_from_diary(diary_file: Path) -> date | None:
    """Extract date from diary file content (for files without date in filename)."""
    try:
        text = diary_file.read_text(errors="ignore")
        m = re.search(r'\*\*Date:\*\*\s*(\d{2}\s+\w+\s+\d{4})', text)
        if m:
            return datetime.strptime(m.group(1), "%d %b %Y").date()
    except Exception:
        pass
    return None


def date_from_photo(filename: str) -> date | None:
    m = re.match(r'^(\d{4}-\d{2}-\d{2})_', filename)
    if m:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    return None


def load_recent_photo_set() -> set:
    """Build the set of photo filenames the most recent scrape just downloaded.

    Reads scrape_summary.json (one row per (obs, child) with photos_downloaded count)
    and resolves which actual files were newly written by checking the photo
    directories for files whose date prefix matches the observation's createdAt
    and whose slug matches. This is approximate — better is to filter by mtime
    of the file vs. the time of the last scrape run.
    """
    if not SCRAPE_SUMMARY.exists():
        return set()
    rows = json.loads(SCRAPE_SUMMARY.read_text())
    # Use mtime: take all photos newer than (oldest written diary in this run) - 60s.
    # Simpler & more robust: just collect every file under PHOTO_DIRS whose mtime
    # is within the last <RECENT_WINDOW_HOURS> hours, since the scraper just ran.
    return None  # caller should fall back to mtime filter


RECENT_WINDOW_HOURS = float(os.environ.get("UPLOAD_WINDOW_HOURS", "6"))


def is_recent(p: Path) -> bool:
    return (time.time() - p.stat().st_mtime) < RECENT_WINDOW_HOURS * 3600


ONLY_RECENT = os.environ.get("UPLOAD_ONLY_RECENT", "1") != "0"


def build_matches(child: str):
    photo_dir = PHOTO_DIRS[child]
    diary_dir = DIARY_DIRS[child]
    if not photo_dir.exists() or not diary_dir.exists():
        return [], []

    # Build diary slug → file map
    # For children with only undated files (01-----202_), include those
    diary_map = {}
    undated_map = {}
    for f in diary_dir.iterdir():
        if f.suffix != ".md" or "Zone" in f.name:
            continue
        if re.match(r'^\d{2}-----', f.name):
            slug = slug_from_diary(f.name)
            undated_map[slug] = f
        else:
            slug = slug_from_diary(f.name)
            diary_map[slug] = f

    # If no dated files exist, use undated
    if not diary_map:
        diary_map = undated_map

    # Build a list of (slug, file) sorted by slug length descending for prefix matching
    diary_slugs = sorted(diary_map.items(), key=lambda x: len(x[0]), reverse=True)

    def find_diary(photo_slug: str):
        # Exact match first
        if photo_slug in diary_map:
            return diary_map[photo_slug]
        # Prefix match: photo slug is truncated version of diary slug
        for d_slug, d_file in diary_slugs:
            if d_slug.startswith(photo_slug) or photo_slug.startswith(d_slug):
                return d_file
        return None

    matches = []
    unmatched_photos = []

    for photo in sorted(photo_dir.iterdir()):
        if photo.suffix.lower() not in (".jpg", ".jpeg", ".png") or "Zone" in photo.name:
            continue
        if ONLY_RECENT and not is_recent(photo):
            continue
        photo_slug = slug_from_photo(photo.name)
        photo_date = date_from_photo(photo.name)
        diary_file = find_diary(photo_slug)
        if diary_file:
            # Get date from diary content if not in photo filename
            effective_date = photo_date or date_from_diary(diary_file)
            matches.append((photo, diary_file, effective_date))
        else:
            unmatched_photos.append((photo, photo_date))

    return matches, unmatched_photos


def read_diary(diary_file: Path) -> tuple[str, str]:
    """Return (notes, comments) extracted from a diary markdown file.

    New-format files have a metadata header, then `---`, then the notes,
    then optionally `---` and `## Comments (N)` followed by comment blocks.
    Old-format files (legacy HTML-scraped) get a best-effort fallback.
    """
    text = diary_file.read_text(errors="ignore")

    # New format: header --- notes [--- ## Comments ...]
    parts = re.split(r"\n---\n", text, maxsplit=2)
    if len(parts) >= 2:
        # Drop the metadata header (parts[0]); join the rest back
        body = "\n---\n".join(parts[1:]).strip()
    else:
        # Fallback: try to find content after a "Notes" line (legacy)
        m = re.search(r"Notes\s*\n(.+)", text, re.DOTALL)
        body = m.group(1).strip() if m else text.strip()

    # Comments section: split out "## Comments (N)" or just "Comments\n"
    notes_part = body
    comments_part = ""
    cm = re.search(r"\n##\s*Comments\b.*?\n", body)
    if cm:
        notes_part = body[:cm.start()].strip()
        comments_part = body[cm.end():].strip()
    else:
        # Legacy text dumps from old scraper
        cm2 = re.search(r"^\s*Comments\s*\n", body, re.MULTILINE)
        if cm2:
            notes_part = body[:cm2.start()].strip()
            comments_part = body[cm2.end():].strip()

    # Strip trailing `---` separator that may leak into notes
    notes_part = re.sub(r"\n*-{3,}\s*$", "", notes_part).strip()
    # Strip legacy boilerplate from comments section
    comments_part = re.sub(r"Add a comment.*?Powered by\s*", "", comments_part,
                           flags=re.DOTALL).strip()

    return notes_part[:1200], comments_part[:600]


def generate_description(child: str, photo_date: date, notes: str, comments: str, title: str) -> str:
    dob = DOBS[child]
    name = DISPLAY_NAMES[child]
    age = age_str(dob, photo_date) if photo_date else "?"
    date_str = photo_date.strftime("%-d %B %Y") if photo_date else "?"

    desc = f"{name} · {age} old · {date_str}\n"
    desc += f"📚 {title}\n\n"

    # Notes — collapse whitespace, strip boilerplate, cap length
    clean_notes = re.sub(r"\s+", " ", notes).strip()
    clean_notes = re.split(r"Add a comment|Powered by|This is a group observation",
                           clean_notes)[0].strip()
    if len(clean_notes) > 700:
        clean_notes = clean_notes[:697] + "..."
    desc += clean_notes

    if comments:
        clean_c = re.sub(r"\s+", " ", comments).strip()
        if len(clean_c) > 250:
            clean_c = clean_c[:247] + "..."
        desc += f"\n\n💬 {clean_c}"

    # Google Photos description max is 1000 chars
    return desc[:1000]


def _read_state() -> dict:
    if CFG.state_file.exists():
        try:
            return json.loads(CFG.state_file.read_text())
        except Exception:
            return {}
    return {}


def _write_state(state: dict) -> None:
    CFG.state_file.write_text(json.dumps(state, indent=2))


def get_or_create_album(token: str, title: str) -> str:
    """Return the persistent album_id for `title`, creating it once if needed.

    Google Photos API can no longer enumerate albums for unverified apps
    (GET /v1/albums → 403, even with photoslibrary.readonly), so we don't
    look it up by title. Instead the id is recorded in state.json on
    first creation and reused thereafter.
    """
    state = _read_state()
    album_id = state.get("google_photos_album_id")
    if album_id:
        print(f"  Reusing album_id from state.json: {album_id[:20]}...")
        return album_id

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post("https://photoslibrary.googleapis.com/v1/albums",
                      headers=headers, json={"album": {"title": title}})
    r.raise_for_status()
    album_id = r.json()["id"]
    print(f"  Created new album '{title}': {album_id[:20]}...")
    state["google_photos_album_id"] = album_id
    state["google_photos_album_created_at"] = datetime.now(timezone.utc).isoformat()
    _write_state(state)
    return album_id


def list_album_filenames(token: str, album_id: str) -> set:
    """Return set of filenames already in the album. Used to dedupe uploads.

    Best-effort. Post-March-2025, `mediaItems:search` 403s for unverified
    apps even with `photoslibrary.readonly`. When that happens we return
    an empty set; the in-run dedupe (content-hash and same-filename
    tracking as we upload) plus the scrape-side watermark in state.json
    keep things idempotent enough — re-runs with no new observations
    are no-ops because the scraper itself returns nothing.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    seen = set()
    page_token = None
    while True:
        body = {"albumId": album_id, "pageSize": 100}
        if page_token:
            body["pageToken"] = page_token
        r = requests.post("https://photoslibrary.googleapis.com/v1/mediaItems:search",
                          headers=headers, json=body)
        if not r.ok:
            print(f"  mediaItems:search failed: {r.status_code} {r.text[:200]}")
            return seen
        j = r.json()
        for item in j.get("mediaItems", []):
            fn = item.get("filename")
            if fn:
                seen.add(fn)
        page_token = j.get("nextPageToken")
        if not page_token:
            return seen


def upload_photo(token: str, photo_path: Path, description: str) -> str | None:
    """Upload bytes, get upload token."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-type": "application/octet-stream",
        "X-Goog-Upload-File-Name": photo_path.name,
        "X-Goog-Upload-Protocol": "raw",
    }
    with open(photo_path, "rb") as f:
        data = f.read()
    r = requests.post("https://photoslibrary.googleapis.com/v1/uploads",
        headers=headers, data=data)
    if not r.ok:
        print(f"    Upload bytes failed: {r.status_code} {r.text[:200]}")
        return None
    return r.text.strip()


def create_media_item(token: str, upload_token: str, description: str, album_id: str) -> tuple[bool, str | None]:
    """Create a media item in the user's library AND add it to the album.

    Quirk: if the upload bytes match content already present in the user's
    library (e.g., the same photo was uploaded earlier by another app),
    batchCreate returns the existing mediaItem id but does NOT add it to
    the album, even though we passed albumId. We therefore call
    batchAddMediaItems explicitly. That second call may 400 with
    INVALID_ARGUMENT on items the app didn't originally create — those
    items live in the user's main library and can only be added to the
    album by the user via the UI.

    Returns (added_to_album, reason_if_not).
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "albumId": album_id,
        "newMediaItems": [{
            "description": description[:1000],
            "simpleMediaItem": {"uploadToken": upload_token}
        }]
    }
    r = requests.post("https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate",
        headers=headers, json=body)
    if not r.ok:
        return (False, f"batchCreate {r.status_code}: {r.text[:200]}")
    results = r.json().get("newMediaItemResults", [])
    if not results:
        return (False, "no result")
    res0 = results[0]
    status = res0.get("status", {})
    msg = status.get("message", "OK")
    if msg not in ("OK", "Success", ""):
        return (False, f"status={msg}")
    item = res0.get("mediaItem")
    if not item:
        return (False, "no mediaItem")
    # Explicit add to album to bypass the library-dup quirk
    add_url = f"https://photoslibrary.googleapis.com/v1/albums/{album_id}:batchAddMediaItems"
    r2 = requests.post(add_url, headers=headers, json={"mediaItemIds": [item["id"]]})
    if r2.ok:
        return (True, None)
    # 400 INVALID_ARGUMENT typically means the item already existed in the
    # user's library (created by another app) and we can't add it via API.
    return (False, f"library-dup (cannot add via API): {r2.status_code}")


# ── Main ──────────────────────────────────────────────────────────────────────

def file_md5(p: Path) -> str:
    import hashlib
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def title_for_diary(diary_file: Path) -> str:
    first = diary_file.read_text(errors="ignore").splitlines()[:1]
    if first and first[0].startswith("# "):
        return first[0][2:].strip()
    title_raw = re.sub(r"^\d{2}-\w+-\d{4}_", "", diary_file.stem)
    return title_raw.replace("-", " ").replace("_", " ").strip()


def author_of_diary(diary_file: Path) -> str | None:
    """Extract `**Authored:** <Name> on ...` from a new-format diary file."""
    text = diary_file.read_text(errors="ignore")
    m = re.search(r"\*\*Authored:\*\*\s*(.+?)\s+on\s", text)
    return m.group(1).strip() if m else None


def build_chronological_queue(already: set):
    """Return one chronologically-sorted list of upload jobs across all
    configured children, deduplicated against the album by filename and
    against itself by content hash (so a photo present in more than one
    child's folder uploads only once). Each job:
    (sort_key, name, photo, child, diary_file_or_none, photo_date).
    """
    seen_hashes = set()           # content-hashes we'll upload exactly once
    jobs = []                     # collected jobs to sort
    skipped_in_album = 0
    skipped_self_dup = 0
    skipped_family = 0

    for child in [c.folder for c in CFG.children]:
        matches, unmatched = build_matches(child)
        for p, diary, photo_date in matches:
            if p.name in already:
                skipped_in_album += 1
                continue
            if author_of_diary(diary) in FAMILY_AUTHORS:
                skipped_family += 1
                continue
            h = file_md5(p)
            if h in seen_hashes:
                skipped_self_dup += 1
                continue
            seen_hashes.add(h)
            jobs.append((photo_date, p.name, p, child, diary, photo_date))
        for p, photo_date in unmatched:
            if p.name in already:
                skipped_in_album += 1
                continue
            h = file_md5(p)
            if h in seen_hashes:
                skipped_self_dup += 1
                continue
            seen_hashes.add(h)
            jobs.append((photo_date, p.name, p, child, None, photo_date))

    # Chronological by photo_date, then by filename for stable ordering within day.
    jobs.sort(key=lambda j: (j[0] or date.min, j[1]))
    return jobs, skipped_in_album, skipped_self_dup, skipped_family


def main():
    print("🔑 Getting access token...")
    token = get_access_token()

    print("📁 Getting/creating Tapestry album...")
    album_id = get_or_create_album(token, ALBUM_TITLE)

    print("🔎 Listing existing photos in album to skip duplicates...")
    already = list_album_filenames(token, album_id)
    print(f"   Album already contains {len(already)} unique filenames")

    print("🧮 Building chronological queue (cross-kid content-dedup, family-author skip)...")
    jobs, skipped_album, skipped_self, skipped_family = build_chronological_queue(already)
    print(f"   Queue: {len(jobs)} | skipped (in album): {skipped_album} | "
          f"skipped (cross-kid dup content): {skipped_self} | "
          f"skipped (family-authored): {skipped_family}")

    total_uploaded = 0
    total_failed = 0

    for i, (_, _, photo, child, diary, photo_date) in enumerate(jobs, 1):
        if diary is not None:
            notes, comments = read_diary(diary)
            title = title_for_diary(diary)
        else:
            notes, comments = "", ""
            title_raw = re.sub(r"^\d{4}-\d{2}-\d{2}_", "", photo.stem)
            title_raw = re.sub(r"_\d+$", "", title_raw)
            title = title_raw.replace("-", " ").replace("_", " ")

        description = generate_description(child, photo_date, notes, comments, title)
        # Belt-and-braces: a final filename check against the running already-set
        if photo.name in already:
            continue

        print(f"  [{i}/{len(jobs)}] {child[:5]:<5} {photo_date} ⬆ {photo.name[:55]}")
        upload_token = upload_photo(token, photo, description)
        if not upload_token:
            total_failed += 1
            continue
        ok, reason = create_media_item(token, upload_token, description, album_id)
        if ok:
            total_uploaded += 1
            already.add(photo.name)
        else:
            total_failed += 1
            print(f"     ⚠️  not added to album: {reason}")
        time.sleep(0.3)

    print(f"\n✅ Done! Uploaded {total_uploaded}. Failed {total_failed}. "
          f"Skipped {skipped_album} already-in-album, {skipped_self} cross-kid content dups, "
          f"{skipped_family} family-authored.")

    # Post-upload cleanup: delete local photos that are either already
    # in the album OR were intentionally skipped (family-authored, since
    # those photos are already in the user's other Google Photos albums).
    # Local photos that are library-dups (couldn't be added to album) are
    # also deleted — they live in your main library, just not in this album.
    # Diary markdown files stay (small, useful for reports).
    if os.environ.get("UPLOAD_KEEP_LOCAL", "0") != "1":
        print("\n🧹 Cleaning up local photos that are processed...")
        final_set = list_album_filenames(token, album_id)
        # Build a "is family-authored" predicate per photo by looking up its diary
        family_photo_names = set()
        for child in [c.folder for c in CFG.children]:
            matches, _ = build_matches(child)
            for p, diary, _ in matches:
                if diary and author_of_diary(diary) in FAMILY_AUTHORS:
                    family_photo_names.add(p.name)

        deleted_in_album = 0
        deleted_family = 0
        kept = 0
        kept_names = []
        for child_dir in PHOTO_DIRS.values():
            if not child_dir.exists():
                continue
            for f in list(child_dir.iterdir()):
                if f.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                    continue
                if f.name in final_set:
                    f.unlink()
                    deleted_in_album += 1
                elif f.name in family_photo_names:
                    f.unlink()
                    deleted_family += 1
                else:
                    kept += 1
                    if len(kept_names) < 10:
                        kept_names.append(f.name)
        print(f"   Deleted {deleted_in_album} (in album), "
              f"{deleted_family} (family-authored). Kept {kept}.")
        if kept_names:
            print("   Kept (library-dups or unknown — likely already in your main library):")
            for n in kept_names:
                print(f"     {n}")
        # Stamp upload completion in state.json
        state_path = CFG.state_file
        try:
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
        except Exception:
            state = {}
        from datetime import datetime, timezone
        state["last_upload_at"] = datetime.now(timezone.utc).isoformat()
        state["last_upload_uploaded"] = total_uploaded
        state["last_upload_failed"] = total_failed
        state_path.write_text(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
