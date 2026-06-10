#!/usr/bin/env python3
"""Read-only audit for duplicate uploads in Google Photos.

Run this INSIDE the deployed image on the prod host — it needs the real
Google token and the album_id recorded in data/state.json. It only ever
touches media items THIS app created (scope: photoslibrary.readonly.
appcreateddata), so it never sees Gmail, Drive, or your personal photos.

What it does
------------
1. Lists every media item the app created and groups them by filename.
   Our filenames are deterministic (``YYYY-MM-DD_slug_NN.jpg``), so the same
   logical photo always lands on the same filename — a filename mapping to
   more than one distinct mediaItem id therefore means the bytes were
   uploaded more than once (a re-upload that Google did NOT byte-dedupe).
2. For each such group it downloads the originals and compares content
   hashes, so a true re-upload (identical content) is told apart from a mere
   filename collision (two genuinely different photos that happen to share a
   date+slug+index). Only identical-content groups are reported as dupes.

Default mode is READ-ONLY: it prints a report and exits. Pass ``--prune`` to
remove the redundant copies of content-identical dupes from the album,
keeping the earliest by creationTime. Pruned items stay in the main library
— the Photos API cannot delete library items; do that in the UI.

    docker compose -f /srv/tapestry/docker-compose.yml run --rm \
        tapestry python3 uploader/audit_dups.py            # read-only
    docker compose -f /srv/tapestry/docker-compose.yml run --rm \
        tapestry python3 uploader/audit_dups.py --prune    # remove extras
"""
import sys
import json
import hashlib
import argparse
import collections
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

import config
from tapestry_upload import get_access_token

CFG = config.load()


def list_app_media(token: str) -> list[dict]:
    """Every media item the app created, paginated."""
    headers = {"Authorization": f"Bearer {token}"}
    items: list[dict] = []
    page = None
    while True:
        params = {"pageSize": 100}
        if page:
            params["pageToken"] = page
        r = requests.get("https://photoslibrary.googleapis.com/v1/mediaItems",
                         headers=headers, params=params)
        if not r.ok:
            print(f"  mediaItems list failed: {r.status_code} {r.text[:200]}")
            break
        j = r.json()
        items.extend(j.get("mediaItems", []))
        page = j.get("nextPageToken")
        if not page:
            break
    return items


def content_hash(item: dict) -> str | None:
    """md5 of the original bytes (baseUrl + '=d' returns full resolution)."""
    base = item.get("baseUrl")
    if not base:
        return None
    r = requests.get(base + "=d")
    if not r.ok:
        return None
    return hashlib.md5(r.content).hexdigest()


def remove_from_album(token: str, album_id: str, ids: list[str]) -> tuple[bool, str | None]:
    url = f"https://photoslibrary.googleapis.com/v1/albums/{album_id}:batchRemoveMediaItems"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json={"mediaItemIds": ids})
    return (r.ok, None if r.ok else f"{r.status_code} {r.text[:200]}")


def creation_time(item: dict) -> str:
    return (item.get("mediaMetadata") or {}).get("creationTime", "")


def main():
    ap = argparse.ArgumentParser(description="Audit (and optionally prune) duplicate uploads.")
    ap.add_argument("--prune", action="store_true",
                    help="Remove redundant copies of content-identical dupes from the album.")
    args = ap.parse_args()

    print("🔑 Getting access token...")
    token = get_access_token()

    state = json.loads(CFG.state_file.read_text()) if CFG.state_file.exists() else {}
    album_id = state.get("google_photos_album_id")
    ledger = state.get("uploaded_hashes") or {}
    print(f"   album_id in state.json: {'yes' if album_id else 'NO (every run would mint a new album!)'}")
    print(f"   dedup ledger entries:   {len(ledger)}")

    print("📃 Listing app-created media items...")
    items = list_app_media(token)
    print(f"   total app-created media items: {len(items)}")

    by_name: dict[str, list[dict]] = collections.defaultdict(list)
    for it in items:
        by_name[it.get("filename", "?")].append(it)

    candidates = {n: its for n, its in by_name.items() if len({i["id"] for i in its}) > 1}
    print(f"   distinct filenames:            {len(by_name)}")
    print(f"   filenames with >1 media id:    {len(candidates)}")

    if not candidates:
        print("\n✅ No duplicate filenames among app-created items — no re-uploads detected.")
        return

    print("\n🔬 Verifying candidates by content hash (downloading originals)...")
    true_dupes: dict[str, list[dict]] = {}      # filename -> items sorted oldest-first (identical content)
    collisions: list[str] = []                  # same filename, different content (NOT dupes)
    for name, its in sorted(candidates.items()):
        by_hash: dict[str, list[dict]] = collections.defaultdict(list)
        for it in its:
            h = content_hash(it)
            by_hash[h or f"?{it['id']}"].append(it)
        dup_groups = {h: g for h, g in by_hash.items() if len(g) > 1 and not h.startswith("?")}
        if dup_groups:
            # Flatten all identical-content items for this filename, oldest first.
            flat = [it for g in dup_groups.values() for it in g]
            flat.sort(key=creation_time)
            true_dupes[name] = flat
        else:
            collisions.append(name)

    print(f"\n── Results ───────────────────────────────────────────")
    print(f"   TRUE duplicates (identical content, >1 copy): {len(true_dupes)} filename(s)")
    print(f"   filename collisions (different content, NOT dupes): {len(collisions)}")

    extra_total = 0
    for name, flat in true_dupes.items():
        keep = flat[0]
        extras = flat[1:]
        extra_total += len(extras)
        print(f"\n   • {name}  ({len(flat)} copies, {len(extras)} redundant)")
        print(f"       keep  {keep['id'][:24]}…  {creation_time(keep)}  {keep.get('productUrl','')}")
        for e in extras:
            print(f"       extra {e['id'][:24]}…  {creation_time(e)}  {e.get('productUrl','')}")
    if collisions:
        print(f"\n   (filename collisions left untouched: {', '.join(collisions[:10])}"
              f"{' …' if len(collisions) > 10 else ''})")

    if not true_dupes:
        print("\n✅ No content-identical duplicates. The repeated filenames are distinct photos.")
        return

    print(f"\n   → {extra_total} redundant cop{'y' if extra_total == 1 else 'ies'} across "
          f"{len(true_dupes)} filename(s).")

    if not args.prune:
        print("\nRead-only audit complete. Re-run with --prune to remove the redundant")
        print("copies from the album (the earliest copy of each is kept).")
        return

    if not album_id:
        print("\n⚠️  --prune requested but no album_id in state.json; cannot remove from album.")
        return

    print(f"\n🧹 Pruning {extra_total} redundant cop{'y' if extra_total == 1 else 'ies'} from the album...")
    removed = 0
    failed = 0
    for name, flat in true_dupes.items():
        extra_ids = [e["id"] for e in flat[1:]]
        ok, reason = remove_from_album(token, album_id, extra_ids)
        if ok:
            removed += len(extra_ids)
            print(f"   removed {len(extra_ids)} from album: {name}")
        else:
            failed += len(extra_ids)
            print(f"   ⚠️  failed to remove {name}: {reason}")
    print(f"\n✅ Removed {removed} from the album (still in main library — delete those in the UI). "
          f"Failed {failed}.")


if __name__ == "__main__":
    main()
