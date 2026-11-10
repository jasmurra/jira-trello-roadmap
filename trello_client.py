"""Trello REST helpers using the browser session (cookie + dsc)."""

from __future__ import annotations

from typing import Any

from auth import dsc_token, http
from config import load_defaults

TRELLO = "https://trello.com"


def _params(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    params = dict(extra or {})
    dsc = dsc_token()
    if dsc:
        params.setdefault("dsc", dsc)
    return params


def _raise_for_status(resp: Any, action: str) -> None:
    if resp.status_code in (401, 403):
        raise RuntimeError("Trello session expired. Sign in again.")
    if resp.status_code >= 400:
        raise RuntimeError(f"Trello {action} HTTP {resp.status_code}: {resp.text[:400]}")


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    resp = http("trello", "GET", f"{TRELLO}{path}", params=_params(params), timeout=45)
    _raise_for_status(resp, path)
    return resp.json()


def _json_body(payload: dict[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    dsc = dsc_token()
    if dsc:
        body["dsc"] = dsc
    return body


def _post(path: str, payload: dict[str, Any]) -> Any:
    resp = http(
        "trello",
        "POST",
        f"{TRELLO}{path}",
        json=_json_body(payload),
        timeout=45,
        headers={"Content-Type": "application/json"},
    )
    _raise_for_status(resp, path)
    if not resp.text:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


def _delete(path: str) -> Any:
    resp = http("trello", "DELETE", f"{TRELLO}{path}", params=_params(), timeout=45)
    _raise_for_status(resp, path)
    if not resp.text:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


def _put_custom_field(card_id: str, field: dict[str, Any]) -> None:
    field_id = field.get("idCustomField")
    if not field_id:
        return
    field_type = field.get("type") or ""
    if field_type == "list" or field.get("idValue"):
        if not field.get("idValue"):
            return
        body: dict[str, Any] = {"idValue": field["idValue"]}
    elif field_type == "checkbox" or str(field.get("value") or "").lower() in {"true", "false"}:
        checked = str(field.get("value") or "").lower() in {"true", "yes", "1", "on"}
        body = {"value": {"checked": "true" if checked else "false"}}
    elif field.get("value"):
        body = {"value": {"text": str(field["value"])}}
    else:
        return
    _put(f"/1/card/{card_id}/customField/{field_id}/item", body)


def _put(path: str, payload: dict[str, Any]) -> Any:
    resp = http(
        "trello",
        "PUT",
        f"{TRELLO}{path}",
        json=_json_body(payload),
        timeout=45,
        headers={"Content-Type": "application/json"},
    )
    _raise_for_status(resp, path)
    if not resp.text:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


def list_boards() -> list[dict[str, Any]]:
    data = _get(
        "/1/members/me/boards",
        params={"fields": "name,url,shortUrl,shortLink,closed,idOrganization", "filter": "open"},
    )
    out = []
    if isinstance(data, list):
        for board in data:
            if board.get("closed"):
                continue
            out.append(
                {
                    "id": board.get("id"),
                    "name": board.get("name") or "",
                    "shortLink": board.get("shortLink") or "",
                    "url": board.get("url") or board.get("shortUrl") or "",
                    "organizationId": board.get("idOrganization") or "",
                }
            )
    out.sort(key=lambda item: (item.get("name") or "").lower())
    return out


def fetch_board() -> dict[str, Any]:
    defaults = load_defaults()["trello"]
    short = defaults["boardShortLink"]
    data = _get(
        f"/1/board/{short}",
        params={
            "fields": "name,url,shortUrl,shortLink,idOrganization",
            "lists": "open",
            "list_fields": "name,closed,pos",
            "labels": "all",
            "customFields": "true",
        },
    )
    board_id = data.get("id") or defaults["boardId"]
    cards_raw = _get(
        f"/1/boards/{board_id}/cards",
        params={
            "filter": "visible",
            "fields": "name,desc,idList,url,shortUrl,labels,due,closed,idMembers",
            "customFieldItems": "true",
        },
    )
    if not isinstance(cards_raw, list):
        cards_raw = []
    lists = [
        {"id": item.get("id"), "name": item.get("name"), "pos": item.get("pos")}
        for item in data.get("lists") or []
        if not item.get("closed")
    ]
    labels = [
        {
            "id": item.get("id"),
            "name": item.get("name") or "",
            "color": item.get("color"),
        }
        for item in data.get("labels") or []
    ]
    custom_fields = []
    for field in data.get("customFields") or []:
        options = []
        for option in field.get("options") or []:
            value = option.get("value") or {}
            options.append(
                {
                    "id": option.get("id"),
                    "text": (value.get("text") if isinstance(value, dict) else str(value or "")),
                }
            )
        custom_fields.append(
            {
                "id": field.get("id"),
                "name": field.get("name"),
                "type": field.get("type"),
                "options": options,
            }
        )
    members_raw = _get(
        f"/1/boards/{board_id}/members",
        params={"fields": "id,fullName,username,initials,avatarUrl,nonPublic"},
    )
    members = []
    if isinstance(members_raw, list):
        for member in members_raw:
            members.append(_public_member(member))

    cards = []
    for card in cards_raw:
        if card.get("closed"):
            continue
        cards.append(
            {
                "id": card.get("id"),
                "name": card.get("name") or "",
                "desc": card.get("desc") or "",
                "idList": card.get("idList"),
                "url": card.get("url") or card.get("shortUrl"),
                "shortUrl": card.get("shortUrl") or card.get("url"),
                "due": card.get("due"),
                "idMembers": card.get("idMembers") or [],
                "labels": [
                    {"id": lab.get("id"), "name": lab.get("name") or "", "color": lab.get("color")}
                    for lab in (card.get("labels") or [])
                ],
                "customFieldItems": card.get("customFieldItems") or [],
            }
        )
    return {
        "id": board_id,
        "name": data.get("name") or defaults["boardName"],
        "url": data.get("url") or defaults.get("boardUrl"),
        "shortLink": data.get("shortLink") or short,
        "lists": lists,
        "labels": labels,
        "customFields": custom_fields,
        "members": members,
        "organizationId": data.get("idOrganization") or defaults.get("organizationId"),
        "cards": cards,
    }


def _public_member(member: dict[str, Any]) -> dict[str, Any]:
    private = member.get("nonPublic") or {}
    avatar = private.get("avatarUrl") or member.get("avatarUrl") or ""
    if avatar and not avatar.endswith(".png") and "/170.png" not in avatar:
        avatar = f"{avatar}/30.png" if avatar else ""
    return {
        "id": member.get("id"),
        "username": member.get("username") or "",
        "fullName": private.get("fullName") or member.get("fullName") or member.get("username") or "",
        "initials": member.get("initials") or "",
        "avatarUrl": avatar,
    }


def search_mentions(query: str, card_id: str | None = None) -> list[dict[str, Any]]:
    defaults = load_defaults()["trello"]
    board_id = defaults["boardId"]
    org_id = defaults.get("organizationId") or "69334d96dcfe15da6727c7da"
    payload: dict[str, Any] = {
        "orgId": org_id,
        "query": (query or "").strip(),
        "totalCount": 20,
    }
    if card_id:
        payload["cardId"] = card_id
    try:
        data = _post(f"/1/boards/{board_id}/mentions", payload)
    except RuntimeError:
        if card_id:
            raise
        cards = _get(
            f"/1/boards/{board_id}/cards",
            params={"filter": "visible", "fields": "id"},
        )
        sample = cards[0]["id"] if isinstance(cards, list) and cards else None
        if not sample:
            raise
        payload["cardId"] = sample
        data = _post(f"/1/boards/{board_id}/mentions", payload)
    raw = data.get("mentions") if isinstance(data, dict) else data
    out = []
    for member in raw or []:
        rec = _public_member(member)
        if rec.get("id"):
            out.append(rec)
    return out


def create_card(payload: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "idList": payload["idList"],
        "name": payload["name"],
        "closed": False,
        "dueComplete": False,
    }
    if payload.get("desc"):
        body["desc"] = payload["desc"]
    member_ids = [str(item) for item in (payload.get("idMembers") or []) if item]
    if payload.get("idLabels"):
        body["idLabels"] = payload["idLabels"]
    if member_ids:
        body["idMembers"] = ",".join(member_ids)
    if payload.get("due"):
        body["due"] = payload["due"]
    card = _post("/1/cards", body)
    card_id = card.get("id")
    if not card_id:
        raise RuntimeError("Trello created a card but returned no id.")

    warnings: list[str] = []
    _apply_custom_fields(card_id, payload.get("customFieldItems") or [], warnings)
    _sync_members(card_id, member_ids, card.get("idMembers") or [], warnings)
    _add_comment(card_id, payload.get("comment"), warnings)

    return {
        "id": card_id,
        "name": card.get("name") or payload["name"],
        "url": card.get("url") or card.get("shortUrl"),
        "shortUrl": card.get("shortUrl") or card.get("url"),
        "idList": card.get("idList") or payload["idList"],
        "warnings": warnings,
    }


def get_card(card_id: str) -> dict[str, Any]:
    return _get(
        f"/1/cards/{card_id}",
        params={"fields": "name,desc,url,shortUrl,idList,idMembers,labels,closed"},
    )


def link_jira_to_card(card_id: str, jira_url: str, key: str) -> dict[str, Any]:
    from mapping import append_jira_link, issue_keys_in_text

    card = get_card(card_id)
    if card.get("closed"):
        raise RuntimeError("That Trello card is archived.")
    desc = card.get("desc") or ""
    already = bool(key and key.upper() in issue_keys_in_text(desc))
    new_desc = append_jira_link(desc, jira_url, key)
    if new_desc != desc:
        updated = _put(f"/1/cards/{card_id}", {"desc": new_desc})
        card = {**card, **updated, "desc": new_desc}
    return {
        "id": card.get("id") or card_id,
        "name": card.get("name") or "",
        "url": card.get("url") or card.get("shortUrl"),
        "shortUrl": card.get("shortUrl") or card.get("url"),
        "alreadyLinked": already,
        "warnings": [],
    }


def update_card(card_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if payload.get("name"):
        body["name"] = payload["name"]
    if "desc" in payload:
        body["desc"] = payload.get("desc") or ""
    if payload.get("idList"):
        body["idList"] = payload["idList"]
    if payload.get("idLabels") is not None:
        body["idLabels"] = ",".join(str(item) for item in payload.get("idLabels") or [])
    card = _put(f"/1/cards/{card_id}", body) if body else {"id": card_id}
    current = _get(
        f"/1/cards/{card_id}",
        params={"fields": "name,url,shortUrl,idList,idMembers"},
    )
    warnings: list[str] = []
    _apply_custom_fields(card_id, payload.get("customFieldItems") or [], warnings)
    wanted = [str(item) for item in (payload.get("idMembers") or []) if item]
    _sync_members(card_id, wanted, current.get("idMembers") or [], warnings)
    _add_comment(card_id, payload.get("comment"), warnings)
    return {
        "id": card_id,
        "name": current.get("name") or card.get("name") or payload.get("name"),
        "url": current.get("url") or current.get("shortUrl") or card.get("url"),
        "shortUrl": current.get("shortUrl") or current.get("url") or card.get("shortUrl"),
        "idList": current.get("idList") or payload.get("idList"),
        "warnings": warnings,
    }


def _apply_custom_fields(card_id: str, fields: list[dict[str, Any]], warnings: list[str]) -> None:
    for field in fields:
        try:
            _put_custom_field(card_id, field)
        except Exception as exc:
            warnings.append(f"{field.get('name') or field.get('idCustomField')}: {exc}")


def _sync_members(
    card_id: str,
    wanted: list[str],
    existing: list[str],
    warnings: list[str],
) -> None:
    have = {str(item) for item in existing or []}
    need = {str(item) for item in wanted or [] if item}
    for member_id in need - have:
        try:
            _post(f"/1/cards/{card_id}/idMembers", {"value": member_id})
        except Exception as exc:
            warnings.append(f"member {member_id}: {exc}")
    for member_id in have - need:
        try:
            _delete(f"/1/cards/{card_id}/idMembers/{member_id}")
        except Exception as exc:
            warnings.append(f"remove member {member_id}: {exc}")


def _add_comment(card_id: str, comment: str | None, warnings: list[str]) -> None:
    text = (comment or "").strip()
    if not text:
        return
    try:
        _post(f"/1/cards/{card_id}/actions/comments", {"text": text})
    except Exception as exc:
        warnings.append(f"comment: {exc}")
