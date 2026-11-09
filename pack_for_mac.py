#!/usr/bin/env python3
"""Build a coworker zip on the Desktop. No secrets, venv, git, or login files."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ZIP_NAME = "Jira-Trello-Roadmap.zip"
FOLDER = "Jira Trello Roadmap"

FILES = (
    "START HERE.txt",
    "README.md",
    "start.command",
    "requirements.txt",
    "VERSION",
    "github-update.txt",
    "update_from_github.py",
    "app.py",
    "auth.py",
    "config.py",
    "jira_client.py",
    "trello_client.py",
    "mapping.py",
    "card_defaults.json",
    "static/index.html",
    "static/favicon.svg",
    "static/favicon.png",
)

BLOCKED_NAMES = {
    ".env",
    ".session.json",
    ".user-defaults.json",
    ".ignored-issues.json",
}


def version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except OSError:
        return "0.0"


def main() -> None:
    desktop = Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    dest = desktop / ZIP_NAME
    missing = [name for name in FILES if not (ROOT / name).is_file()]
    if missing:
        raise SystemExit("Missing files: " + ", ".join(missing))
    if dest.exists():
        dest.unlink()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name in FILES:
            path = ROOT / name
            if path.name in BLOCKED_NAMES:
                continue
            if name == "start.command":
                info = zipfile.ZipInfo(f"{FOLDER}/{name}")
                info.date_time = path_date(path)
                info.external_attr = 0o755 << 16
                zipped.writestr(info, path.read_bytes())
                continue
            zipped.write(path, arcname=f"{FOLDER}/{name}")
    print(f"Wrote {dest} (Version {version()})")


def path_date(path: Path) -> tuple[int, int, int, int, int, int]:
    stamp = path.stat().st_mtime
    import time

    parts = time.localtime(stamp)
    return parts.tm_year, parts.tm_mon, parts.tm_mday, parts.tm_hour, parts.tm_min, parts.tm_sec


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
