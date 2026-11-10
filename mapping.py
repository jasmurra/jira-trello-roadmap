"""Turn a Jira issue into a Trello draft and match existing cards."""

from __future__ import annotations

import re
from typing import Any

from config import load_defaults

ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
BROWSE_RE = re.compile(
    r"cisco-gde\.atlassian\.net/browse/([A-Z][A-Z0-9]+-\d+)",
    re.I,
)
QUARTER_RE = re.compile(r"\b(Q[1-4]FY\d{2})\b", re.I)


def issue_keys_in_text(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in BROWSE_RE.findall(text or ""):
        key = match.upper()
        if key not in seen:
            seen.add(key)
            found.append(key)
    for match in ISSUE_KEY_RE.findall(text or ""):
        key = match.upper()
        if key not in seen:
            seen.add(key)
            found.append(key)
    return found


def jira_smart_link(jira_url: str) -> str:
    url = (jira_url or "").strip()
    return f'[{url}]({url} "smartCard-inline")'


def append_jira_link(desc: str, jira_url: str, key: str) -> str:
    """Add the Jira URL at the bottom of a Trello description if it is missing."""
    desc = desc or ""
    url = (jira_url or "").strip()
    key = (key or "").strip().upper()
    if not url:
        return desc
    if key and key in issue_keys_in_text(desc):
        return desc
    if url.rstrip("/") in desc.replace("\\", ""):
        return desc
    link = jira_smart_link(url)
    body = desc.rstrip()
    if body:
        return body + "\n\n" + link + "\n"
    return link + "\n"


def quarter_from_name(name: str | None) -> str | None:
    if not name:
        return None
    match = QUARTER_RE.search(name)
    return match.group(1).upper() if match else None


def parse_jira_description(text: str) -> tuple[str, str]:
    body = (text or "").strip()
    products = ""
    split = re.split(r"trello\s+description\s*:\s*", body, maxsplit=1, flags=re.I)
    if len(split) == 2:
        body = split[1].strip()
    match = re.search(r"^products\s*:\s*(.+)$", body, re.I | re.M)
    if match:
        products = match.group(1).strip()
        body = (body[: match.start()] + body[match.end() :]).strip()
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body, products


def build_trello_description(blurb: str, products: str, jira_url: str) -> str:
    blurb = (blurb or "").strip()
    products = (products or "").strip()
    lines: list[str] = []
    if blurb:
        if "\n" not in blurb:
            lines.append(f"### {blurb}")
        else:
            lines.append(blurb)
        lines.append("")
    if products:
        lines.append(f"Products:\n{products}")
        lines.append("")
    lines.append(f"[{jira_url}]({jira_url} \"smartCard-inline\")")
    return "\n".join(lines).strip() + "\n"


def pick_list(quarter: str | None, swimlane: str | None, lists: list[dict[str, Any]]) -> dict[str, Any] | None:
    names = {item["name"]: item for item in lists if item.get("name")}
    q = quarter or quarter_from_name(swimlane)
    if q:
        for name, item in names.items():
            if quarter_from_name(name) == q:
                return item
    if swimlane and "NO DUE DATE" in swimlane.upper():
        return names.get("Inbox") or names.get("Future Consideration")
    if swimlane and "FUTURE" in swimlane.upper():
        return names.get("Future Consideration")
    return names.get("Inbox") or (lists[0] if lists else None)


def _label_ids_by_name(labels: list[dict[str, Any]], names: list[str]) -> list[str]:
    by_name = {(item.get("name") or "").strip().lower(): item["id"] for item in labels if item.get("id")}
    out: list[str] = []
    for name in names:
        found = by_name.get(name.strip().lower())
        if found and found not in out:
            out.append(found)
    return out


def custom_field_selections(
    fields: list[dict[str, Any]],
    defaults: dict[str, str],
) -> list[dict[str, Any]]:
    wanted = {key.strip().lower(): value for key, value in defaults.items()}
    items: list[dict[str, Any]] = []
    for field in fields:
        name = field.get("name") or ""
        wanted_value = wanted.get(name.strip().lower())
        selected = None
        if wanted_value:
            for option in field.get("options") or []:
                if (option.get("text") or "").strip().lower() == wanted_value.strip().lower():
                    selected = option.get("id")
                    break
        checked = False
        if field.get("type") == "checkbox":
            checked = str(wanted_value or "").lower() in {"true", "yes", "1"}
        items.append(
            {
                "id": field.get("id"),
                "name": name,
                "type": field.get("type"),
                "options": field.get("options") or [],
                "idValue": selected,
                "value": ("true" if checked else "false") if field.get("type") == "checkbox" else (wanted_value if field.get("type") == "text" else ""),
                "checked": checked,
            }
        )
    return items


def products_from_trello_desc(desc: str) -> str:
    match = re.search(
        r"^Products:\s*\n(.+?)(?:\n\n|\n\[|$)",
        desc or "",
        re.I | re.M | re.S,
    )
    return match.group(1).strip() if match else ""


def custom_fields_from_card(
    fields: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {item.get("idCustomField"): item for item in items or []}
    out: list[dict[str, Any]] = []
    for field in fields:
        item = by_id.get(field.get("id")) or {}
        value = item.get("value") or {}
        text = ""
        checked = False
        if isinstance(value, dict):
            text = value.get("text") or ""
            checked = str(value.get("checked") or "").lower() in {"true", "yes", "1"}
        elif value:
            text = str(value)
        out.append(
            {
                "id": field.get("id"),
                "name": field.get("name"),
                "type": field.get("type"),
                "options": field.get("options") or [],
                "idValue": item.get("idValue"),
                "value": ("true" if checked else "false") if field.get("type") == "checkbox" else text,
                "checked": checked,
            }
        )
    return out


def draft_from_issue(
    issue: dict[str, Any],
    board: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    defaults = load_defaults()
    trello_defaults = defaults["trello"]
    extra = extra or {}
    blurb, products_from_desc = parse_jira_description(issue.get("description") or "")
    products = (extra.get("products") or products_from_desc or issue.get("products") or "").strip()
    if not blurb:
        blurb = issue.get("summary") or ""
    jira_url = issue.get("url") or ""
    description = build_trello_description(blurb, products, jira_url)
    quarter = quarter_from_name(issue.get("swimlane")) or extra.get("quarter")
    chosen_list = pick_list(quarter, issue.get("swimlane"), board.get("lists") or [])
    label_ids = _label_ids_by_name(
        board.get("labels") or [],
        trello_defaults.get("defaultLabelNames") or [],
    )
    return {
        "key": issue.get("key"),
        "jiraUrl": jira_url,
        "name": issue.get("summary") or "",
        "blurb": blurb,
        "products": products,
        "desc": description,
        "idList": (chosen_list or {}).get("id"),
        "listName": (chosen_list or {}).get("name"),
        "quarter": quarter,
        "idLabels": label_ids,
        "idMembers": [],
        "customFields": custom_field_selections(
            board.get("customFields") or [],
            trello_defaults.get("defaultCustomFields") or {},
        ),
        "comment": trello_defaults.get("commentTemplate") or "",
        "mention": True,
        "mode": "create",
        "cardId": None,
        "cardUrl": None,
    }


def draft_from_card(
    issue: dict[str, Any],
    board: dict[str, Any],
    card: dict[str, Any],
) -> dict[str, Any]:
    defaults = load_defaults()["trello"]
    delivered_id = delivered_field_id(
        board.get("customFields") or [],
        defaults.get("deliveredFieldName") or "Delivered",
    )
    delivered = card_is_delivered(card, delivered_id)
    jira_done = bool(issue.get("jiraDone") or issue.get("isResolved"))
    comment = ""
    if jira_done and not delivered:
        comment = defaults.get("deliveredCommentTemplate") or defaults.get("commentTemplate") or ""
    members_by_id = {item.get("id"): item for item in (board.get("members") or []) if item.get("id")}
    members = []
    for member_id in card.get("idMembers") or []:
        found = members_by_id.get(member_id)
        if found:
            members.append(found)
        else:
            members.append({"id": member_id, "username": "", "fullName": member_id, "initials": "", "avatarUrl": ""})
    desc = card.get("desc") or ""
    return {
        "key": issue.get("key"),
        "jiraUrl": issue.get("url") or "",
        "name": card.get("name") or issue.get("summary") or "",
        "products": products_from_trello_desc(desc),
        "desc": desc,
        "idList": card.get("idList"),
        "listName": next((item.get("name") for item in (board.get("lists") or []) if item.get("id") == card.get("idList")), ""),
        "quarter": quarter_from_name(issue.get("swimlane")),
        "idLabels": [lab.get("id") for lab in (card.get("labels") or []) if lab.get("id")],
        "idMembers": [item.get("id") for item in members if item.get("id")],
        "members": members,
        "customFields": custom_fields_from_card(
            board.get("customFields") or [],
            card.get("customFieldItems") or [],
        ),
        "comment": comment,
        "mention": bool(comment),
        "mode": "update",
        "cardId": card.get("id"),
        "cardUrl": card.get("url") or card.get("shortUrl"),
        "delivered": delivered,
        "jiraDone": jira_done,
    }


def index_trello_cards(cards: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
        blob = f"{card.get('name') or ''}\n{card.get('desc') or ''}"
        for key in issue_keys_in_text(blob):
            index.setdefault(key, []).append(card)
    return index


def delivered_field_id(custom_fields: list[dict[str, Any]], name: str = "Delivered") -> str | None:
    wanted = (name or "Delivered").strip().lower()
    for field in custom_fields:
        if (field.get("name") or "").strip().lower() == wanted:
            return field.get("id")
    return None


def card_is_delivered(card: dict[str, Any] | None, field_id: str | None) -> bool:
    if not card or not field_id:
        return False
    for item in card.get("customFieldItems") or []:
        if item.get("idCustomField") != field_id:
            continue
        value = item.get("value") or {}
        if isinstance(value, dict):
            checked = value.get("checked")
            return str(checked).lower() in {"true", "yes", "1"}
        return str(value).lower() in {"true", "yes", "1"}
    return False


def compare_item(
    issue: dict[str, Any],
    matches: list[dict[str, Any]],
    lists_by_id: dict[str, str],
    delivered_id: str | None = None,
) -> dict[str, Any]:
    quarter = quarter_from_name(issue.get("swimlane"))
    jira_done = bool(issue.get("jiraDone") or issue.get("isResolved"))
    if not matches:
        return {
            **issue,
            "quarter": quarter,
            "trello": None,
            "trelloDelivered": False,
            "gap": "missing",
        }
    card = matches[0]
    list_name = lists_by_id.get(card.get("idList") or "", "")
    card_quarter = quarter_from_name(list_name)
    name_match = (card.get("name") or "").strip().lower() == (issue.get("summary") or "").strip().lower()
    delivered = card_is_delivered(card, delivered_id)
    if jira_done and not delivered:
        gap = "not-delivered"
    elif quarter and card_quarter and quarter != card_quarter:
        gap = "quarter-mismatch"
    elif not name_match:
        gap = "name-mismatch"
    else:
        gap = "matched"
    return {
        **issue,
        "quarter": quarter,
        "trello": {
            "id": card.get("id"),
            "name": card.get("name"),
            "url": card.get("url") or card.get("shortUrl"),
            "listName": list_name,
            "listQuarter": card_quarter,
            "labels": [lab.get("name") for lab in (card.get("labels") or []) if lab.get("name")],
            "nameMatch": name_match,
            "delivered": delivered,
        },
        "trelloDelivered": delivered,
        "gap": gap,
        "extraMatches": max(0, len(matches) - 1),
    }
