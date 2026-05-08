#!/usr/bin/env python3
"""Read scrape_summary.json + diary markdown files and produce per-child reports."""
import json
import re
from collections import defaultdict

import config

CFG = config.load()


def diary_text(child_folder: str, name: str) -> str:
    p = CFG.diary_root / child_folder / name
    if not p.exists():
        return ""
    raw = p.read_text(encoding="utf-8")
    parts = raw.split("\n---\n", 1)
    return parts[1].strip() if len(parts) == 2 else raw


def first_paragraph(text: str, n_chars: int = 350) -> str:
    text = text.split("\n---\n")[0]
    text = text.split("## Comments")[0].strip()
    if not text:
        return ""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    out, total = [], 0
    for p in paras:
        out.append(p)
        total += len(p)
        if total >= n_chars:
            break
    return "\n\n".join(out)


def comments_section(child_folder: str, name: str) -> str:
    p = CFG.diary_root / child_folder / name
    if not p.exists():
        return ""
    raw = p.read_text(encoding="utf-8")
    if "## Comments" not in raw:
        return ""
    return raw.split("## Comments", 1)[1].strip()


def main():
    if not CFG.scrape_summary.exists():
        print(f"No scrape summary at {CFG.scrape_summary} — run scraper.py first.")
        return
    data = json.loads(CFG.scrape_summary.read_text())
    by_child = defaultdict(list)
    for r in data:
        by_child[r["child"]].append(r)

    # Build api_name → (folder, display) from config
    folders = {c.api_name: (c.folder, c.display) for c in CFG.children}
    # also resolve aliases that map onto a configured child
    for api_name, folder in CFG.child_dirs.items():
        if api_name not in folders and folder in CFG.children_by_folder:
            folders[api_name] = (folder, CFG.children_by_folder[folder].display)

    for child_full, rows in by_child.items():
        if child_full not in folders:
            continue
        folder, display = folders[child_full]
        rows.sort(key=lambda x: x["createdAt"])
        out = CFG.data_root / f"REPORT_{folder}.md"
        lines = [f"# Tapestry Report — {display}", ""]
        cover_start = rows[0]["createdAt"][:10]
        cover_end = rows[-1]["createdAt"][:10]
        total_photos = sum(r["photos_downloaded"] + r["photos_skipped"] for r in rows)
        new_photos = sum(r["photos_downloaded"] for r in rows)
        lines.append(f"**Coverage:** {cover_start} → {cover_end}")
        lines.append(f"**Observations:** {len(rows)}")
        lines.append(f"**Photos this run:** {new_photos} new ({total_photos} total in observations)")
        lines.append("")
        lines.append("## Highlights")
        lines.append("")
        for r in rows:
            d = r["createdAt"][:10]
            lines.append(f"- **{d}** — {r['title']} ({r['photos_downloaded'] + r['photos_skipped']} photos)")
        lines.append("")
        lines.append("## Detailed entries")
        lines.append("")
        for r in rows:
            d = r["createdAt"][:10]
            lines.append(f"### {d} — {r['title']}")
            lines.append("")
            lines.append(f"_Photos: {r['photos_downloaded'] + r['photos_skipped']} · "
                         f"file: `{r['diary']}`_")
            lines.append("")
            text = diary_text(folder, r["diary"])
            excerpt = first_paragraph(text, n_chars=600)
            if excerpt:
                lines.append(excerpt)
                lines.append("")
            comments = comments_section(folder, r["diary"])
            if comments:
                lines.append("**Comments:**")
                lines.append("")
                lines.append(comments)
                lines.append("")
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"Wrote {out}")


if __name__ == "__main__":
    main()
