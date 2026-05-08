"""Single config loader for the Tapestry pipeline.

Reads:
- `.env` next to this file (KEY=VALUE) for secrets — Tapestry login.
- `config.toml` next to this file for everything else — children, school slug,
  paths, family-author skip list, Google Photos album title.

Both can be overridden with env vars (see `_env_or` below).
"""
from __future__ import annotations

import os
import pathlib
import tomllib
from datetime import date
from typing import NamedTuple

ROOT = pathlib.Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
CONFIG_FILE = ROOT / "config.toml"


def _load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _env_or(env: dict[str, str], key: str, default: str | None = None) -> str | None:
    return os.environ.get(key) or env.get(key) or default


class Child(NamedTuple):
    api_name: str       # exact fullName from Tapestry API (e.g., "First Last")
    folder: str         # local folder name under tapestry-{photos,diary}/
    display: str        # short display name used in Google Photos captions
    dob: date           # date of birth (for "Xy Ym old" caption)


class Config:
    def __init__(self):
        if not CONFIG_FILE.exists():
            raise FileNotFoundError(
                f"Missing {CONFIG_FILE}. Copy config.example.toml to config.toml "
                "and fill it in."
            )
        env = _load_env()
        with open(CONFIG_FILE, "rb") as f:
            cfg = tomllib.load(f)

        # Tapestry
        tap = cfg.get("tapestry", {})
        self.school_slug: str = tap["school_slug"]
        self.tapestry_email: str = _env_or(env, "TAPESTRY_EMAIL", env.get("user")) or ""
        self.tapestry_password: str = _env_or(env, "TAPESTRY_PASSWORD", env.get("pass")) or ""
        if not self.tapestry_email or not self.tapestry_password:
            raise RuntimeError(
                "Set TAPESTRY_EMAIL and TAPESTRY_PASSWORD in .env (or as env vars)."
            )

        # Paths
        paths = cfg.get("paths", {})
        self.data_root: pathlib.Path = pathlib.Path(
            os.path.expanduser(paths.get("data_root", str(ROOT)))
        ).resolve()
        self.photo_root = self.data_root / "tapestry-photos"
        self.diary_root = self.data_root / "tapestry-diary"
        self.state_file = self.data_root / "state.json"
        self.scrape_summary = self.data_root / "scrape_summary.json"

        # Children
        self.children: list[Child] = []
        for c in cfg.get("children", []):
            self.children.append(
                Child(
                    api_name=c["api_name"],
                    folder=c["folder"],
                    display=c.get("display", c["folder"]),
                    dob=date.fromisoformat(c["dob"]),
                )
            )
        if not self.children:
            raise RuntimeError("config.toml must define at least one [[children]].")

        # API-name → folder map (some kids have multiple aliases)
        self.child_dirs: dict[str, str] = {}
        for c in self.children:
            self.child_dirs[c.api_name] = c.folder
        for extra in cfg.get("name_aliases", []):
            self.child_dirs[extra["api_name"]] = extra["folder"]

        # Quick lookups by folder
        self.children_by_folder: dict[str, Child] = {c.folder: c for c in self.children}

        # Google Photos
        gp = cfg.get("google_photos", {})
        self.album_title: str = gp.get("album_title", "Tapestry")
        # Token must be in google-auth "authorized user" format — that
        # format embeds client_id/client_secret, so a separate
        # credentials.json is no longer required.
        self.google_token: pathlib.Path = pathlib.Path(
            os.path.expanduser(gp.get("token_path", "~/.config/google/token.json"))
        )
        # Authors whose observations should NOT be uploaded — typically
        # family members whose photos are already in other albums.
        self.family_authors: set[str] = set(gp.get("family_authors", []))

        # Scraper safety floor
        scr = cfg.get("scraper", {})
        self.cutoff_date: str | None = scr.get("cutoff_date")  # ISO yyyy-mm-dd; optional


_singleton: Config | None = None


def load() -> Config:
    global _singleton
    if _singleton is None:
        _singleton = Config()
    return _singleton
