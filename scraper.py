#!/usr/bin/env python3
"""Tapestry scraper. Logs in via /login, paginates /api/4/observations/list,
fetches /api/4/observations/get/{id} for full details (notes, comments, media),
saves diary markdown and photos for each child configured in config.toml.

Idempotent — files that already exist are skipped. Watermark in state.json
(`last_observation_id`) determines where the scraper resumes.
"""
import json
import pathlib
import re
import sys
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

import config

CFG = config.load()

BASE = "https://tapestryjournal.com"
SCHOOL_URL = f"{BASE}/s/{CFG.school_slug}"
LOGIN_URL = f"{BASE}/login"
LIST_URL = f"{BASE}/api/4/observations/list"
GET_URL = f"{BASE}/api/4/observations/get/{{id}}"


def load_state() -> dict:
    if CFG.state_file.exists():
        return json.loads(CFG.state_file.read_text())
    return {"last_observation_id": 0}


def save_state(state: dict):
    CFG.state_file.write_text(json.dumps(state, indent=2))


def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    })
    r = s.get(SCHOOL_URL, allow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    token = soup.find("input", {"name": "_token"})
    if not token:
        raise RuntimeError("Could not find CSRF _token on login page")
    csrf = token["value"]
    r2 = s.post(LOGIN_URL, data={
        "_token": csrf,
        "email": CFG.tapestry_email,
        "password": CFG.tapestry_password,
        "login_redirect_url": "",
        "login_redirect_school": "",
        "oauth": "",
        "oauth_login_url": "",
    }, allow_redirects=True)
    r2.raise_for_status()
    if "/login" in r2.url and "logout" not in r2.text.lower():
        raise RuntimeError(f"Login failed; landed at {r2.url}")
    print(f"[login] ok, landed at {r2.url}", file=sys.stderr)
    return s


def slugify(s: str, maxlen: int = 35) -> str:
    """Strip non-alnum/non-space, collapse spaces to hyphens, truncate, then
    strip trailing hyphens. Preserves the legacy filename pattern."""
    s = re.sub(r"[^A-Za-z0-9 ]", "", s)
    s = s.replace(" ", "-")
    return s[:maxlen].strip("-")


def parse_dt(iso_str: str) -> datetime:
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))


def fmt_diary_date(dt: datetime) -> str:
    return dt.strftime("%d-%b-%Y")  # e.g., 31-Jan-2026


def fmt_iso_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def fmt_human(dt: datetime) -> str:
    return dt.strftime("%d %b %Y %I:%M %p")


def list_observations(s: requests.Session, last_id: int = 0):
    """Yield observations newer than `last_id`, walking the API newest-first.

    Stops when an observation's id is <= last_id (already processed) or when
    createdAt drops below the optional `cutoff_date` safety floor.
    """
    cutoff = None
    if CFG.cutoff_date:
        cutoff = datetime.fromisoformat(CFG.cutoff_date).replace(tzinfo=timezone.utc)
    cursor = None
    while True:
        params = {"perPage": 50}
        if cursor:
            params["cursor"] = cursor
        r = s.get(LIST_URL, params=params)
        r.raise_for_status()
        data = r.json()
        for obs in data["observations"]:
            if obs["id"] <= last_id:
                return
            if cutoff and parse_dt(obs["createdAt"]) < cutoff:
                return
            yield obs
        cursor = data.get("nextCursor")
        if not cursor:
            return


def get_observation(s: requests.Session, obs_id: int) -> dict:
    r = s.get(GET_URL.format(id=obs_id))
    r.raise_for_status()
    return r.json()


def download(s: requests.Session, url: str, dest: pathlib.Path) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        return False
    r = s.get(url, stream=True)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return True


def render_markdown(obs: dict, child_full_name: str) -> str:
    created = parse_dt(obs["createdAt"])
    lines = [f"# {obs['title']}\n"]
    lines.append(f"**Date:** {fmt_diary_date(created)}")
    lines.append(f"**Authored:** {obs['author']['fullName']} on {fmt_human(created)}")
    if obs.get("approval"):
        appr = obs["approval"]
        appr_dt = parse_dt(appr["date"])
        lines.append(f"**Approved by:** {appr['name']} on {fmt_human(appr_dt)}")
    lines.append(f"**Child:** {child_full_name}")
    lines.append(f"**Observation ID:** {obs['id']}")
    lines.append(f"**Photos:** {obs.get('mediaCount', 0)}")
    lines.append("")
    lines.append("---")
    lines.append("")
    notes = obs.get("notes") or ""
    notes_clean = BeautifulSoup(notes, "lxml").get_text("\n").strip() if notes else ""
    if notes_clean:
        lines.append(notes_clean)
        lines.append("")
    add_info = obs.get("additionalInformation")
    if add_info:
        add_clean = BeautifulSoup(add_info, "lxml").get_text("\n").strip()
        if add_clean:
            lines.append("**Additional information:**")
            lines.append(add_clean)
            lines.append("")
    comments = obs.get("comments") or []
    if comments:
        lines.append("---")
        lines.append("")
        lines.append(f"## Comments ({len(comments)})")
        lines.append("")
        for c in comments:
            cdt = parse_dt(c["createdAt"])
            user = c["user"]["fullName"]
            content = (c.get("content") or "").strip()
            lines.append(f"**{user}** — {fmt_human(cdt)}")
            lines.append("")
            lines.append(content)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_for_child(s: requests.Session, obs: dict, child: dict):
    full = child["fullName"]
    folder = CFG.child_dirs.get(full)
    if not folder:
        return None

    created = parse_dt(obs["createdAt"])
    title_slug = slugify(obs["title"])
    diary_dir = CFG.diary_root / folder
    photo_dir = CFG.photo_root / folder
    diary_dir.mkdir(parents=True, exist_ok=True)
    photo_dir.mkdir(parents=True, exist_ok=True)

    diary_name = f"{fmt_diary_date(created)}_{title_slug}.md"
    diary_path = diary_dir / diary_name

    actions = {"diary_written": False, "photos_downloaded": 0, "photos_skipped": 0}

    if not diary_path.exists():
        diary_path.write_text(render_markdown(obs, full), encoding="utf-8")
        actions["diary_written"] = True

    # Family-authored observations (Karol / Luis) are vacation/home photos
    # the parents already have on their phones and in their main Google
    # Photos library. The uploader skips them anyway; skipping here avoids
    # downloading dozens of MB we'd just delete after the upload run.
    author_name = (obs.get("author") or {}).get("fullName")
    if author_name in CFG.family_authors:
        actions["family_authored"] = True
        return diary_name, actions

    media = obs.get("media") or []
    photo_date = fmt_iso_date(created)
    img_idx = 0
    for m in media:
        if m.get("type") != "image":
            continue
        img_idx += 1
        url = m.get("url") or m.get("embedUrl")
        if not url:
            continue
        ext = pathlib.Path(m.get("name", "")).suffix or ".jpg"
        if ext.lower() not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            ext = ".jpg"
        photo_name = f"{photo_date}_{title_slug}_{img_idx:02d}{ext}"
        photo_path = photo_dir / photo_name
        if download(s, url, photo_path):
            actions["photos_downloaded"] += 1
        else:
            actions["photos_skipped"] += 1
    return diary_name, actions


def main():
    s = get_session()
    state = load_state()
    last_id = int(state.get("last_observation_id", 0))
    print(f"[state] resuming after observation id {last_id}", file=sys.stderr)
    summary = []
    max_id_seen = last_id
    for obs in list_observations(s, last_id=last_id):
        if obs["id"] > max_id_seen:
            max_id_seen = obs["id"]
        full = get_observation(s, obs["id"])
        for child in full["children"]:
            res = save_for_child(s, full, child)
            if res:
                name, act = res
                summary.append({
                    "id": full["id"],
                    "child": child["fullName"],
                    "title": full["title"],
                    "createdAt": full["createdAt"],
                    "diary": name,
                    **act,
                })
                fam = " [family-authored: photos skipped]" if act.get("family_authored") else ""
                print(f"[obs {full['id']}] {child['fullName']} | {full['title']} | "
                      f"diary={'NEW' if act['diary_written'] else 'exists'} "
                      f"photos+={act['photos_downloaded']} (skip {act['photos_skipped']}){fam}",
                      file=sys.stderr)
    CFG.scrape_summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    if max_id_seen > last_id:
        state["last_observation_id"] = max_id_seen
        state["last_scrape_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        print(f"[state] watermark advanced to observation id {max_id_seen}", file=sys.stderr)
    print(f"\nDone. {len(summary)} (obs,child) saved. Summary: {CFG.scrape_summary}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
