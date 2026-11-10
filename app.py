"""Local Jira → Trello Roadmap Sync helper."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

import auth
import jira_client
import mapping
import trello_client
from config import (
    PORT,
    ignore_issue,
    jira_site,
    load_defaults,
    load_ignored,
    reset_user_boards,
    save_user_defaults,
    unignore_issue,
)

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    def _run() -> None:
        try:
            auth.try_refresh_from_profile()
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()
    yield


app = FastAPI(title="Jira → Trello Roadmap Sync", version="1.1.3", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class LoginRequest(BaseModel):
    target: str = "both"


class DefaultsRequest(BaseModel):
    trello: dict[str, Any] = Field(default_factory=dict)
    jira: dict[str, Any] = Field(default_factory=dict)
    ui: dict[str, Any] = Field(default_factory=dict)


class DraftRequest(BaseModel):
    key: str
    projectFilter: str = "COL"


class CreateRequest(BaseModel):
    key: str
    name: str
    desc: str = ""
    idList: str
    idLabels: list[str] = Field(default_factory=list)
    idMembers: list[str] = Field(default_factory=list)
    comment: str = ""
    customFields: list[dict[str, Any]] = Field(default_factory=list)
    due: str | None = None
    cardId: str | None = None


class UpdateRequest(CreateRequest):
    cardId: str


class IgnoreRequest(BaseModel):
    key: str
    summary: str = ""
    reason: str = ""


class LinkRequest(BaseModel):
    key: str
    cardId: str


def _require_jira() -> None:
    if not auth.probe_jira().get("ok"):
        raise HTTPException(401, "Sign in to Jira first.")


def _require_trello() -> None:
    if not auth.probe_trello().get("ok"):
        raise HTTPException(401, "Sign in to Trello first.")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/auth/status")
def auth_status() -> dict[str, Any]:
    return auth.auth_status()


@app.post("/api/auth/import-chrome")
def import_chrome() -> dict[str, Any]:
    return auth.import_chrome()


@app.post("/api/auth/login")
def start_login(payload: LoginRequest) -> dict[str, Any]:
    return auth.start_login(payload.target)


@app.get("/api/auth/login-status")
def login_status() -> dict[str, Any]:
    return auth.login_status()


@app.post("/api/auth/logout")
def logout() -> dict[str, Any]:
    auth.clear_session()
    return auth.auth_status()


@app.get("/api/defaults")
def get_defaults() -> dict[str, Any]:
    return load_defaults()


@app.put("/api/defaults")
def put_defaults(payload: DefaultsRequest) -> dict[str, Any]:
    overlay: dict[str, Any] = {}
    if payload.jira:
        overlay["jira"] = payload.jira
    if payload.trello:
        overlay["trello"] = payload.trello
    if payload.ui:
        overlay["ui"] = payload.ui
    return save_user_defaults(overlay)


@app.post("/api/defaults/reset")
def reset_defaults() -> dict[str, Any]:
    return reset_user_boards()


def _read_app_version() -> str:
    try:
        text = (APP_DIR / "VERSION").read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return "0.0"
    return (text[0].strip() if text else "") or "0.0"


@app.get("/api/version")
def api_version() -> dict[str, Any]:
    return {"version": _read_app_version()}


def _list_jira_boards() -> dict[str, Any]:
    _require_jira()
    boards = jira_client.list_boards()
    defaults = load_defaults()["jira"]
    return {"boards": boards, "selectedId": defaults.get("boardId")}


@app.get("/api/jira/boards")
async def jira_boards() -> dict[str, Any]:
    try:
        return await run_in_threadpool(_list_jira_boards)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


def _list_jira_filters(board_id: str) -> dict[str, Any]:
    _require_jira()
    filters = jira_client.list_quick_filters(board_id)
    defaults = load_defaults()["jira"]
    return {"filters": filters, "selectedId": defaults.get("quickFilterId")}


@app.get("/api/jira/boards/{board_id}/filters")
async def jira_filters(board_id: str) -> dict[str, Any]:
    try:
        return await run_in_threadpool(_list_jira_filters, board_id)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


def _list_trello_boards() -> dict[str, Any]:
    _require_trello()
    boards = trello_client.list_boards()
    defaults = load_defaults()["trello"]
    return {"boards": boards, "selectedId": defaults.get("boardId")}


@app.get("/api/trello/boards")
async def trello_boards() -> dict[str, Any]:
    try:
        return await run_in_threadpool(_list_trello_boards)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


_update_check_lock = threading.Lock()


def _apply_github_update() -> None:
    time.sleep(0.8)
    try:
        env = dict(os.environ)
        env.pop("JIRA_TRELLO_SKIP_UPDATE", None)
        subprocess.run(
            [sys.executable, str(APP_DIR / "update_from_github.py")],
            cwd=str(APP_DIR),
            env=env,
            timeout=180,
            check=False,
        )
        time.sleep(0.4)
        os.execv(sys.executable, [sys.executable, str(APP_DIR / "app.py")])
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        if _update_check_lock.locked():
            _update_check_lock.release()


@app.post("/api/update/check")
def api_update_check() -> dict[str, Any]:
    if not _update_check_lock.acquire(blocking=False):
        return {
            "ok": True,
            "updating": True,
            "version": _read_app_version(),
            "message": "An update check is already running.",
        }
    try:
        from update_from_github import fetch_public_version, is_newer, load_config, working_tree_dirty

        config = load_config()
        repo = str(config.get("repo") or "").strip()
        branch = str(config.get("branch") or "main").strip() or "main"
        if not repo:
            _update_check_lock.release()
            raise HTTPException(400, "GitHub updates are not configured.")
        remote_version = fetch_public_version(repo, branch)
        current = _read_app_version()
        if not remote_version:
            _update_check_lock.release()
            return {
                "ok": True,
                "updating": False,
                "reachable": False,
                "version": current,
                "message": (
                    f"GitHub was unreachable, so this copy was not checked. "
                    f"You are still on Version {current}."
                ),
            }
        if not is_newer(remote_version, current):
            _update_check_lock.release()
            return {
                "ok": True,
                "updating": False,
                "version": current,
                "message": f"Version {current} is already current.",
            }
        if working_tree_dirty():
            _update_check_lock.release()
            return {
                "ok": True,
                "updating": False,
                "localWork": True,
                "version": current,
                "message": (
                    f"This copy has local work, so it stays on Version {current}. "
                    f"GitHub {remote_version} was not installed."
                ),
            }
        threading.Thread(target=_apply_github_update, daemon=True).start()
        return {
            "ok": True,
            "updating": True,
            "version": remote_version,
            "message": f"Updating to Version {remote_version}. The page will reload automatically.",
        }
    except HTTPException:
        raise
    except Exception as exc:
        if _update_check_lock.locked():
            _update_check_lock.release()
        raise HTTPException(500, f"Could not check for updates: {exc}") from exc


def _compare(project_filter: str, roadmap_only: bool) -> dict[str, Any]:
    _require_jira()
    _require_trello()
    defaults = load_defaults()
    ignored = load_ignored()
    jira_board = jira_client.fetch_board(project_filter)
    trello_board = trello_client.fetch_board()
    lists_by_id = {item["id"]: item["name"] for item in trello_board.get("lists") or []}
    index = mapping.index_trello_cards(trello_board.get("cards") or [])
    delivered_id = mapping.delivered_field_id(
        trello_board.get("customFields") or [],
        defaults.get("trello", {}).get("deliveredFieldName") or "Delivered",
    )
    items = []
    seen_keys: set[str] = set()
    for issue in jira_board.get("issues") or []:
        if roadmap_only and str(issue.get("roadmap") or "").strip().lower() != "yes":
            continue
        key = str(issue.get("key") or "").upper()
        seen_keys.add(key)
        row = mapping.compare_item(
            issue,
            index.get(issue["key"], []),
            lists_by_id,
            delivered_id,
        )
        stored = ignored.get(key)
        row["ignored"] = bool(stored)
        if stored:
            row["ignoreReason"] = stored.get("reason") or ""
            row["ignoredAt"] = stored.get("ignoredAt")
        items.append(row)
    for key, stored in ignored.items():
        if key in seen_keys:
            continue
        items.append(
            {
                "key": key,
                "summary": stored.get("summary") or "",
                "url": f"{defaults['jira']['site'].rstrip('/')}/browse/{key}",
                "gap": "ignored",
                "ignored": True,
                "ignoreReason": stored.get("reason") or "",
                "ignoredAt": stored.get("ignoredAt"),
                "trello": None,
            }
        )
    gap_rank = {
        "missing": 0,
        "not-delivered": 1,
        "quarter-mismatch": 2,
        "name-mismatch": 3,
        "matched": 4,
        "ignored": 5,
    }
    items.sort(
        key=lambda row: (
            gap_rank.get(row.get("gap") or "", 9),
            row.get("swimlane") or "",
            row.get("key") or "",
        )
    )
    active = [row for row in items if not row.get("ignored")]
    counts = {
        "total": len(active),
        "missing": sum(1 for row in active if row.get("gap") == "missing"),
        "notDelivered": sum(1 for row in active if row.get("gap") == "not-delivered"),
        "matched": sum(1 for row in active if row.get("gap") == "matched"),
        "mismatch": sum(
            1
            for row in active
            if row.get("gap") in {"quarter-mismatch", "name-mismatch", "not-delivered"}
        ),
        "ignored": sum(1 for row in items if row.get("ignored")),
    }
    return {
        "jira": {
            "id": jira_board.get("id"),
            "name": jira_board.get("name"),
            "url": jira_board.get("url") or defaults["jira"].get("boardUrl"),
        },
        "trello": {
            "id": trello_board.get("id"),
            "name": trello_board.get("name"),
            "url": trello_board.get("url") or defaults["trello"].get("boardUrl"),
            "lists": trello_board.get("lists") or [],
            "labels": trello_board.get("labels") or [],
            "customFields": trello_board.get("customFields") or [],
            "members": trello_board.get("members") or [],
        },
        "links": {
            "jira": defaults["jira"].get("boardUrl"),
            "trello": defaults["trello"].get("boardUrl"),
            "roadmap": defaults["trello"].get("publicRoadmapUrl"),
        },
        "counts": counts,
        "items": items,
        "defaults": defaults,
        "roadmapOnly": roadmap_only,
    }


@app.get("/api/compare")
async def compare(project: str = "COL", roadmapOnly: bool = True) -> dict[str, Any]:
    try:
        return await run_in_threadpool(_compare, project, roadmapOnly)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


def _draft(key: str, project_filter: str) -> dict[str, Any]:
    _require_jira()
    _require_trello()
    board = jira_client.fetch_board(project_filter)
    issue_row = next((item for item in board["issues"] if item.get("key") == key), None)
    detail = jira_client.fetch_issue(key)
    if issue_row:
        detail["swimlane"] = issue_row.get("swimlane")
        detail["due"] = detail.get("due") or issue_row.get("due")
        detail["status"] = detail.get("status") or issue_row.get("status")
        detail["assignee"] = detail.get("assignee") or issue_row.get("assignee")
        detail["jiraDone"] = issue_row.get("jiraDone")
        detail["isResolved"] = issue_row.get("isResolved")
    trello_board = trello_client.fetch_board()
    matches = mapping.index_trello_cards(trello_board.get("cards") or []).get(key, [])
    if matches:
        draft = mapping.draft_from_card(detail, trello_board, matches[0])
    else:
        draft = mapping.draft_from_issue(detail, trello_board)
    return {
        "issue": detail,
        "draft": draft,
        "trello": {
            "lists": trello_board.get("lists") or [],
            "labels": trello_board.get("labels") or [],
            "customFields": trello_board.get("customFields") or [],
            "members": trello_board.get("members") or [],
        },
    }


@app.post("/api/draft")
async def draft(payload: DraftRequest) -> dict[str, Any]:
    try:
        return await run_in_threadpool(_draft, payload.key.strip().upper(), payload.projectFilter)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


def _custom_items(fields: list[dict[str, Any]], include_unchecked: bool) -> list[dict[str, Any]]:
    items = []
    for field in fields:
        if not field.get("id"):
            continue
        field_type = field.get("type") or ""
        if field_type == "list":
            if not field.get("idValue"):
                continue
        elif field_type == "text":
            if not (field.get("value") or "").strip():
                continue
        elif field_type == "checkbox":
            checked = str(field.get("value") or "").lower() in {"true", "yes", "1", "on"}
            if not checked and not include_unchecked:
                continue
        elif not field.get("idValue") and not field.get("value"):
            continue
        items.append(
            {
                "idCustomField": field.get("id"),
                "name": field.get("name"),
                "type": field_type,
                "idValue": field.get("idValue") or "",
                "value": field.get("value") or "",
            }
        )
    return items


def _card_payload(payload: CreateRequest, include_unchecked: bool) -> dict[str, Any]:
    return {
        "idList": payload.idList,
        "name": payload.name,
        "desc": payload.desc,
        "idLabels": payload.idLabels,
        "idMembers": payload.idMembers,
        "comment": payload.comment,
        "customFieldItems": _custom_items(payload.customFields, include_unchecked),
        "due": payload.due,
    }


def _create(payload: CreateRequest) -> dict[str, Any]:
    _require_trello()
    card = trello_client.create_card(_card_payload(payload, include_unchecked=False))
    return {"ok": True, "card": card, "key": payload.key, "mode": "create"}


def _update(payload: UpdateRequest) -> dict[str, Any]:
    _require_trello()
    card = trello_client.update_card(payload.cardId, _card_payload(payload, include_unchecked=True))
    return {"ok": True, "card": card, "key": payload.key, "mode": "update"}


@app.get("/api/trello/mentions")
async def mentions(q: str = "") -> dict[str, Any]:
    _require_trello()
    query = (q or "").strip()
    if not query:
        return {"mentions": []}
    try:
        found = await run_in_threadpool(trello_client.search_mentions, query)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"mentions": found}


@app.post("/api/cards/create")
async def create_card(payload: CreateRequest) -> dict[str, Any]:
    try:
        return await run_in_threadpool(_create, payload)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/cards/update")
async def update_card(payload: UpdateRequest) -> dict[str, Any]:
    try:
        return await run_in_threadpool(_update, payload)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


def _trello_picker() -> dict[str, Any]:
    _require_trello()
    board = trello_client.fetch_board()
    lists_by_id = {item.get("id"): item.get("name") or "" for item in board.get("lists") or []}
    members_by_id = {item.get("id"): item for item in board.get("members") or [] if item.get("id")}
    cards = []
    for card in board.get("cards") or []:
        keys = mapping.issue_keys_in_text(f"{card.get('name') or ''}\n{card.get('desc') or ''}")
        members = [
            members_by_id[member_id]
            for member_id in (card.get("idMembers") or [])
            if member_id in members_by_id
        ]
        search = " ".join(
            [
                card.get("name") or "",
                lists_by_id.get(card.get("idList") or "", ""),
                " ".join(keys),
                " ".join(lab.get("name") or "" for lab in (card.get("labels") or [])),
                " ".join(
                    (item.get("fullName") or "") + " " + (item.get("username") or "")
                    for item in members
                ),
                card.get("desc") or "",
            ]
        ).lower()
        cards.append(
            {
                "id": card.get("id"),
                "name": card.get("name") or "",
                "url": card.get("url") or card.get("shortUrl"),
                "idList": card.get("idList"),
                "listName": lists_by_id.get(card.get("idList") or "", ""),
                "labels": card.get("labels") or [],
                "idMembers": card.get("idMembers") or [],
                "members": members,
                "keys": keys,
                "search": search,
            }
        )
    return {
        "board": {
            "id": board.get("id"),
            "name": board.get("name"),
            "url": board.get("url"),
        },
        "lists": board.get("lists") or [],
        "labels": [lab for lab in (board.get("labels") or []) if lab.get("name")],
        "members": board.get("members") or [],
        "cards": cards,
    }


def _link_card(key: str, card_id: str) -> dict[str, Any]:
    _require_jira()
    _require_trello()
    key = key.strip().upper()
    if not key or not card_id:
        raise RuntimeError("Jira key and Trello card are required.")
    detail = jira_client.fetch_issue(key)
    jira_url = detail.get("url") or f"{jira_site()}/browse/{key}"
    card = trello_client.link_jira_to_card(card_id, jira_url, key)
    return {
        "ok": True,
        "key": key,
        "jiraUrl": jira_url,
        "summary": detail.get("summary") or "",
        "card": card,
    }


@app.get("/api/trello/cards")
async def trello_cards() -> dict[str, Any]:
    try:
        return await run_in_threadpool(_trello_picker)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/cards/link")
async def link_card(payload: LinkRequest) -> dict[str, Any]:
    try:
        return await run_in_threadpool(_link_card, payload.key.strip().upper(), payload.cardId.strip())
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/ignore")
def hide_issue(payload: IgnoreRequest) -> dict[str, Any]:
    key = payload.key.strip().upper()
    if not key:
        raise HTTPException(400, "Issue key is required.")
    items = ignore_issue(key, payload.summary, payload.reason)
    return {"ok": True, "key": key, "ignored": list(items.values())}


@app.delete("/api/ignore/{key}")
def show_issue(key: str) -> dict[str, Any]:
    items = unignore_issue(key)
    return {"ok": True, "key": key.strip().upper(), "ignored": list(items.values())}


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=PORT, reload=False)
