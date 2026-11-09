"""Board IDs, field names, and user-overridable card defaults."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
DEFAULTS_PATH = APP_DIR / "card_defaults.json"
USER_DEFAULTS_PATH = APP_DIR / ".user-defaults.json"
SESSION_PATH = APP_DIR / ".session.json"
IGNORED_PATH = APP_DIR / ".ignored-issues.json"
PROFILE_DIR = APP_DIR / ".browser-profile"

PORT = 8785


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def load_defaults() -> dict[str, Any]:
    bundled = _load_json(DEFAULTS_PATH)
    user = _load_json(USER_DEFAULTS_PATH)
    merged = _deep_merge(bundled, user)
    jira = merged.setdefault("jira", {})
    trello = merged.setdefault("trello", {})
    jira.setdefault("site", "https://cisco-gde.atlassian.net")
    trello.setdefault("boardShortLink", "F0baTV9g")
    return merged


def save_user_defaults(overlay: dict[str, Any]) -> dict[str, Any]:
    current = _load_json(USER_DEFAULTS_PATH)
    merged = _deep_merge(current, overlay)
    USER_DEFAULTS_PATH.write_text(
        json.dumps(merged, indent=2) + "\n",
        encoding="utf-8",
    )
    return load_defaults()


def reset_user_boards() -> dict[str, Any]:
    current = _load_json(USER_DEFAULTS_PATH)
    current.pop("jira", None)
    current.pop("trello", None)
    USER_DEFAULTS_PATH.write_text(
        json.dumps(current, indent=2) + "\n",
        encoding="utf-8",
    )
    return load_defaults()


def jira_site() -> str:
    return str(load_defaults()["jira"]["site"]).rstrip("/")


def load_ignored() -> dict[str, dict[str, Any]]:
    data = _load_json(IGNORED_PATH)
    items = data.get("issues") if isinstance(data.get("issues"), dict) else data
    if not isinstance(items, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, rec in items.items():
        if isinstance(rec, dict):
            out[str(key).upper()] = rec
        else:
            out[str(key).upper()] = {"key": str(key).upper()}
    return out


def save_ignored(items: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    IGNORED_PATH.write_text(
        json.dumps({"issues": items}, indent=2) + "\n",
        encoding="utf-8",
    )
    return items


def ignore_issue(key: str, summary: str = "", reason: str = "") -> dict[str, dict[str, Any]]:
    items = load_ignored()
    items[key.strip().upper()] = {
        "key": key.strip().upper(),
        "summary": summary,
        "reason": reason,
        "ignoredAt": datetime.now(timezone.utc).isoformat(),
    }
    return save_ignored(items)


def unignore_issue(key: str) -> dict[str, dict[str, Any]]:
    items = load_ignored()
    items.pop(key.strip().upper(), None)
    return save_ignored(items)
