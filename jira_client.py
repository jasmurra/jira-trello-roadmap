"""Jira Cloud helpers for the Collab & IOT board."""

from __future__ import annotations

import html
import re
from typing import Any

from auth import http
from config import jira_site, load_defaults


def _raise_jira(resp: Any, path: str) -> Any:
    if resp.status_code in (401, 403):
        raise RuntimeError("Jira session expired. Sign in again.")
    if resp.status_code >= 400:
        raise RuntimeError(f"Jira HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    site = jira_site()
    resp = http("jira", "GET", f"{site}{path}", params=params, timeout=45)
    return _raise_jira(resp, path)


def _post(path: str, payload: dict[str, Any]) -> Any:
    site = jira_site()
    resp = http("jira", "POST", f"{site}{path}", json=payload, timeout=60)
    return _raise_jira(resp, path)


_roadmap_field_id: str | None = None


def adf_to_text(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(item) for item in node)
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type")
    if node_type == "hardBreak":
        return "\n"
    text = node.get("text") or ""
    inner = "".join(adf_to_text(item) for item in (node.get("content") or []))
    if node_type in {"paragraph", "heading", "blockquote"}:
        return f"{text}{inner}\n"
    if node_type in {"bulletList", "orderedList"}:
        return inner + ("\n" if not inner.endswith("\n") else "")
    if node_type == "listItem":
        return f"- {inner.lstrip()}"
    return f"{text}{inner}"


def _people_name(people: dict[str, Any], account_id: str | None) -> str | None:
    if not account_id:
        return None
    rec = people.get(account_id) or {}
    if isinstance(rec, dict):
        return rec.get("displayName") or rec.get("name")
    return None


def _parse_roadmap_value(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, dict):
        raw = raw.get("value") or raw.get("text") or raw.get("html") or raw.get("name") or ""
    return _strip_html(str(raw)).strip()


def _roadmap_field_name() -> str:
    return (load_defaults()["jira"].get("roadmapFieldName") or "Display on Roadmap").strip()


def resolve_roadmap_field_id() -> str:
    global _roadmap_field_id
    if _roadmap_field_id:
        return _roadmap_field_id
    defaults = load_defaults()["jira"]
    configured = str(defaults.get("roadmapFieldId") or "customfield_10210")
    wanted = _roadmap_field_name().lower()
    try:
        fields = _get("/rest/api/3/field")
    except Exception:
        _roadmap_field_id = configured
        return configured
    if isinstance(fields, list):
        for field in fields:
            if str(field.get("name") or "").strip().lower() == wanted and field.get("id"):
                _roadmap_field_id = str(field["id"])
                return _roadmap_field_id
    _roadmap_field_id = configured
    return configured


def _entry_matches_roadmap(entry: dict[str, Any], field_id: str) -> bool:
    if (entry.get("id") or "") == field_id:
        return True
    label = str(entry.get("label") or entry.get("name") or "").strip().lower()
    return bool(label) and label == _roadmap_field_name().lower()


def _roadmap_value(issue: dict[str, Any], field_id: str) -> str:
    for entry in issue.get("fieldEntries") or []:
        if isinstance(entry, dict) and _entry_matches_roadmap(entry, field_id):
            return _parse_roadmap_value(entry)
    for extra in issue.get("cardExtraFields") or []:
        if isinstance(extra, dict) and _entry_matches_roadmap(extra, field_id):
            return _parse_roadmap_value(extra)
    return ""


def fetch_roadmap_values(keys: list[str], field_id: str | None = None) -> dict[str, str]:
    """Read Display on Roadmap from the issue, not the board card layout.

    NetSec (and other boards) often omit that extra field on cards, which made
    every item look like Roadmap = No even when Jira itself is Yes.
    """
    field_id = field_id or resolve_roadmap_field_id()
    wanted: list[str] = []
    seen: set[str] = set()
    for key in keys:
        item = str(key or "").strip().upper()
        if item and item not in seen:
            seen.add(item)
            wanted.append(item)
    out: dict[str, str] = {}
    for start in range(0, len(wanted), 50):
        chunk = wanted[start : start + 50]
        data = _post(
            "/rest/api/3/issue/bulkfetch",
            {"issueIdsOrKeys": chunk, "fields": [field_id]},
        )
        for issue in data.get("issues") or []:
            key = issue.get("key")
            if not key:
                continue
            out[str(key)] = _parse_roadmap_value((issue.get("fields") or {}).get(field_id))
    return out


def list_boards() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    start = 0
    while start < 500:
        data = _get(
            "/rest/agile/1.0/board",
            params={"startAt": start, "maxResults": 50},
        )
        values = data.get("values") or []
        for board in values:
            loc = board.get("location") or {}
            out.append(
                {
                    "id": board.get("id"),
                    "name": board.get("name") or "",
                    "type": board.get("type") or "",
                    "projectKey": loc.get("projectKey") or "",
                    "projectName": loc.get("projectName") or "",
                }
            )
        if data.get("isLast") or not values:
            break
        start += len(values)
    out.sort(key=lambda item: (item.get("name") or "").lower())
    return out


def list_quick_filters(board_id: int | str | None = None) -> list[dict[str, Any]]:
    defaults = load_defaults()["jira"]
    wanted = board_id or defaults.get("boardId")
    data = _get(
        f"/rest/boards/latest/board/{wanted}",
        params={
            "hideCardExtraFields": "true",
            "moduleKey": "agile-web-board-service",
            "onlyUseEpicsFromIssues": "true",
            "operation": "fetchBoardData",
            "skipCardCovers": "true",
            "skipEtag": "true",
        },
    )
    filters = []
    for item in data.get("filters") or []:
        if not item.get("id"):
            continue
        filters.append({"id": item.get("id"), "name": item.get("name") or str(item.get("id"))})
    return filters


def _quick_filter_id(project_filter: str | None, defaults: dict[str, Any]) -> str | int | None:
    raw = (project_filter if project_filter is not None else defaults.get("quickFilterId")) or ""
    text = str(raw).strip()
    if not text or text.upper() in {"ALL", "NONE", "0"}:
        return None
    if text.upper() == "COL":
        return defaults.get("quickFilterId") or 7144
    if text.upper() == "IOT":
        return 7142
    return text


def fetch_board(project_filter: str = "COL") -> dict[str, Any]:
    defaults = load_defaults()["jira"]
    board_id = defaults["boardId"]
    params: dict[str, Any] = {
        "hideCardExtraFields": "true",
        "moduleKey": "agile-web-board-service",
        "onlyUseEpicsFromIssues": "true",
        "operation": "fetchBoardData",
        "skipCardCovers": "false",
        "skipEtag": "true",
    }
    quick_filter = _quick_filter_id(project_filter, defaults)
    if quick_filter:
        params["activeQuickFilters"] = quick_filter
    data = _get(f"/rest/boards/latest/board/{board_id}", params)
    people = data.get("people") or {}
    status_names: dict[str, str] = {}
    for column in data.get("columns") or []:
        for status in column.get("statuses") or []:
            if isinstance(status, dict) and status.get("id") is not None:
                status_names[str(status["id"])] = status.get("name") or str(status["id"])
        if column.get("name") and column.get("statusIds"):
            for sid in column["statusIds"]:
                status_names.setdefault(str(sid), column["name"])

    swimlane_by_issue: dict[int, dict[str, Any]] = {}
    swimlanes = []
    for lane in (data.get("swimlaneInfo") or {}).get("swimlanes") or []:
        swimlanes.append({"id": lane.get("id"), "name": lane.get("name")})
        for issue_id in lane.get("issueIds") or []:
            swimlane_by_issue[int(issue_id)] = lane

    issues: list[dict[str, Any]] = []
    field_id = resolve_roadmap_field_id()
    for column in data.get("columns") or []:
        for issue in column.get("issues") or []:
            if not isinstance(issue, dict) or not issue.get("key"):
                continue
            lane = swimlane_by_issue.get(int(issue["id"])) if issue.get("id") is not None else None
            status_name = status_names.get(str(issue.get("statusId") or ""), column.get("name"))
            column_name = column.get("name") or ""
            cancelled = "will not" in str(status_name or "").lower()
            jira_done = column_name.strip().lower() == "done" and not cancelled
            issues.append(
                {
                    "id": issue.get("id"),
                    "key": issue.get("key"),
                    "summary": issue.get("summary") or "",
                    "due": issue.get("dueDate") or issue.get("due"),
                    "statusId": str(issue.get("statusId") or ""),
                    "status": status_name,
                    "column": column_name,
                    "jiraDone": jira_done,
                    "isResolved": bool(issue.get("isResolved")),
                    "assigneeAccountId": issue.get("assigneeAccountId"),
                    "assignee": _people_name(people, issue.get("assigneeAccountId")),
                    "labels": issue.get("labels") or [],
                    "roadmap": _roadmap_value(issue, field_id),
                    "swimlane": (lane or {}).get("name"),
                    "projectId": issue.get("projectId"),
                    "url": f"{jira_site()}/browse/{issue.get('key')}",
                }
            )
    if issues:
        from_issue = fetch_roadmap_values([item["key"] for item in issues], field_id)
        for item in issues:
            if item.get("key") in from_issue:
                item["roadmap"] = from_issue[item["key"]]
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "filters": data.get("filters") or [],
        "swimlanes": swimlanes,
        "issues": issues,
        "url": defaults.get("boardUrl")
        or (
            f"{jira_site()}/jira/software/c/projects/{defaults.get('projectKey') or 'COL'}/boards/{board_id}"
            + (
                f"?quickFilter={defaults.get('quickFilterId')}"
                if defaults.get("quickFilterId")
                else ""
            )
        ),
    }


def fetch_issue(key: str) -> dict[str, Any]:
    defaults = load_defaults()["jira"]
    roadmap_field = resolve_roadmap_field_id()
    architecture_field = str(defaults.get("architectureFieldId") or "customfield_10074")
    fields = ",".join(
        [
            "summary",
            "description",
            "duedate",
            "status",
            "assignee",
            "labels",
            "issuetype",
            roadmap_field,
            architecture_field,
            defaults.get("productTagsFieldId") or "customfield_13157",
            defaults.get("demoNameFieldId") or "customfield_10060",
        ]
    )
    data = _get(
        f"/rest/api/3/issue/{key}",
        params={"fields": fields, "expand": "renderedFields"},
    )
    fields_data = data.get("fields") or {}
    rendered = data.get("renderedFields") or {}
    description = adf_to_text(fields_data.get("description")).strip()
    if not description and rendered.get("description"):
        description = _strip_html(str(rendered.get("description")))
    product_tags = fields_data.get(defaults.get("productTagsFieldId") or "customfield_13157") or []
    if isinstance(product_tags, list):
        products = ", ".join(str(tag) for tag in product_tags if tag)
    else:
        products = str(product_tags or "")
    roadmap_value = _parse_roadmap_value(fields_data.get(roadmap_field))
    architecture = _parse_roadmap_value(fields_data.get(architecture_field))
    assignee = fields_data.get("assignee") or {}
    status = fields_data.get("status") or {}
    return {
        "key": data.get("key") or key,
        "summary": fields_data.get("summary") or "",
        "description": description,
        "due": fields_data.get("duedate"),
        "status": (status.get("name") if isinstance(status, dict) else None),
        "assignee": assignee.get("displayName") if isinstance(assignee, dict) else None,
        "labels": fields_data.get("labels") or [],
        "products": products,
        "roadmap": roadmap_value,
        "architecture": architecture,
        "demoName": fields_data.get(defaults.get("demoNameFieldId") or "customfield_10060") or "",
        "url": f"{jira_site()}/browse/{data.get('key') or key}",
        "issueType": ((fields_data.get("issuetype") or {}).get("name") if isinstance(fields_data.get("issuetype"), dict) else None),
    }


def fetch_issues(keys: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key in keys:
        try:
            out[key] = fetch_issue(key)
        except Exception:
            continue
    return out


def _strip_html(raw: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).replace("\xa0", " ").strip()
