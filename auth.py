"""Local session for Jira Cloud and Trello: Chrome import, browser sign-in, saved cookies."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import requests

from config import PROFILE_DIR, SESSION_PATH, jira_site, load_defaults

_lock = threading.Lock()
_login_state: dict[str, Any] = {
    "running": False,
    "target": None,
    "message": "",
    "done": False,
    "error": None,
}

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_session() -> dict[str, Any]:
    if not SESSION_PATH.is_file():
        return {}
    try:
        data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_session(data: dict[str, Any]) -> None:
    SESSION_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def clear_session(target: str | None = None) -> None:
    if target in (None, "both"):
        if SESSION_PATH.is_file():
            SESSION_PATH.unlink()
        return
    data = load_session()
    data.pop(target, None)
    save_session(data)


def _host_matches_cookie(host: str, domain: str) -> bool:
    domain = (domain or "").lstrip(".").lower()
    host = (host or "").lstrip(".").lower()
    if not domain or not host:
        return False
    return host == domain or host.endswith("." + domain)


def _cookie_allowed_for_host(name: str, domain: str, host: str | None) -> bool:
    if not host:
        return True
    if _host_matches_cookie(host, domain):
        return True
    # Atlassian often stores the long-lived session on id.atlassian.com only.
    # Jira Cloud REST on the tenant host still accepts that token.
    if (
        name == "cloud.session.token"
        and host.endswith("atlassian.net")
        and "atlassian.com" in domain
    ):
        return True
    return False


def cookie_header(cookies: list[dict[str, Any]], host: str | None = None) -> str:
    """Build a Cookie header. If host is set, only include cookies for that host.

    Mixing Jira/Atlassian cookies into Trello (or collapsing same-named cookies
    from other domains) makes Trello return HTTP 400 'invalid token'.
    """
    chosen: dict[str, tuple[int, str]] = {}
    for cookie in cookies:
        name = cookie.get("name") or ""
        value = cookie.get("value")
        if not name or value is None:
            continue
        domain = (cookie.get("domain") or "").lstrip(".")
        if host and not _cookie_allowed_for_host(name, domain, host):
            continue
        score = len(domain)
        # Prefer a tenant-scoped session cookie over the id.atlassian.com copy.
        if name == "cloud.session.token" and "atlassian.net" in domain:
            score += 1000
        prev = chosen.get(name)
        if prev is None or score >= prev[0]:
            chosen[name] = (score, str(value))
    return "; ".join(f"{name}={item[1]}" for name, item in chosen.items())


def cookies_for(target: str) -> list[dict[str, Any]]:
    block = load_session().get(target) or {}
    cookies = block.get("cookies") or []
    return cookies if isinstance(cookies, list) else []


def dsc_token() -> str | None:
    for cookie in cookies_for("trello"):
        if cookie.get("name") == "dsc" and cookie.get("value"):
            return str(cookie["value"])
    return None


def requests_session(target: str) -> requests.Session:
    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": BROWSER_UA,
            "Accept": "application/json, text/plain, */*",
        }
    )
    if target == "jira":
        site = jira_site()
        sess.headers["X-Atlassian-Token"] = "no-check"
        sess.headers["Origin"] = site
        sess.headers["Referer"] = site + "/"
        host = urlparse(site).hostname or "cisco-gde.atlassian.net"
    else:
        sess.headers["Origin"] = "https://trello.com"
        sess.headers["Referer"] = "https://trello.com/"
        host = "trello.com"
    header = cookie_header(cookies_for(target), host)
    if header:
        sess.headers["Cookie"] = header
    return sess


def _browser_probe(context: Any, name: str) -> dict[str, Any]:
    try:
        if name == "jira":
            resp = context.request.get(f"{jira_site()}/rest/api/3/myself", timeout=20_000)
        else:
            resp = context.request.get(
                "https://trello.com/1/members/me?fields=fullName,username",
                timeout=20_000,
            )
        if resp.status != 200:
            return {"ok": False, "error": f"HTTP {resp.status}"}
        data = resp.json()
        if not isinstance(data, dict):
            return {"ok": False, "error": "non-JSON body"}
        if name == "jira":
            if not data.get("accountId") and not data.get("displayName"):
                return {"ok": False, "error": "Jira session is not valid"}
            return {
                "ok": True,
                "displayName": data.get("displayName"),
                "email": data.get("emailAddress"),
                "accountId": data.get("accountId"),
            }
        if not data.get("id"):
            return {"ok": False, "error": "Trello session is not valid"}
        return {
            "ok": True,
            "displayName": data.get("fullName"),
            "username": data.get("username"),
            "id": data.get("id"),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def probe_jira() -> dict[str, Any]:
    site = jira_site()
    try:
        resp = requests_session("jira").get(f"{site}/rest/api/3/myself", timeout=20)
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}
    if resp.status_code != 200:
        return {"ok": False, "error": f"Jira HTTP {resp.status_code}"}
    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "error": "Jira returned a non-JSON body"}
    return {
        "ok": True,
        "displayName": data.get("displayName"),
        "email": data.get("emailAddress"),
        "accountId": data.get("accountId"),
    }


def probe_trello() -> dict[str, Any]:
    try:
        resp = requests_session("trello").get(
            "https://trello.com/1/members/me",
            params={"fields": "fullName,username"},
            timeout=20,
        )
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}
    if resp.status_code != 200:
        return {"ok": False, "error": f"Trello HTTP {resp.status_code}"}
    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "error": "Trello returned a non-JSON body"}
    if not isinstance(data, dict) or not data.get("id"):
        return {"ok": False, "error": "Trello session is not valid"}
    return {
        "ok": True,
        "displayName": data.get("fullName"),
        "username": data.get("username"),
        "id": data.get("id"),
    }


def auth_status() -> dict[str, Any]:
    data = load_session()
    jira = probe_jira() if cookies_for("jira") else {"ok": False, "error": "Not signed in"}
    trello = probe_trello() if cookies_for("trello") else {"ok": False, "error": "Not signed in"}
    if jira.get("ok"):
        block = data.setdefault("jira", {})
        block["user"] = {k: jira[k] for k in ("displayName", "email", "accountId") if k in jira}
        block["checkedAt"] = _now()
    if trello.get("ok"):
        block = data.setdefault("trello", {})
        block["user"] = {k: trello[k] for k in ("displayName", "username", "id") if k in trello}
        block["checkedAt"] = _now()
    if data:
        save_session(data)
    return {
        "jira": jira,
        "trello": trello,
        "login": dict(_login_state),
    }


def _chrome_cookie_files() -> list[Path]:
    base = Path.home() / "Library/Application Support/Google/Chrome"
    if not base.is_dir():
        return []
    paths: list[Path] = []
    for pattern in (
        "Default/Network/Cookies",
        "Default/Cookies",
        "Profile */Network/Cookies",
        "Profile */Cookies",
    ):
        paths.extend(base.glob(pattern))
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in sorted(
        (p for p in paths if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        text = str(path)
        if "Snapshots" in text or "Backup" in text:
            continue
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


@contextmanager
def _chrome_cookie_snapshot(cookie_file: Path) -> Iterator[Path]:
    source = Path(cookie_file)
    tmpdir = Path(tempfile.mkdtemp(prefix="chrome-cookies-"))
    try:
        target = tmpdir / "Cookies"
        shutil.copyfile(source, target)
        for suffix in ("-wal", "-shm"):
            sidecar = source.with_name(source.name + suffix)
            if sidecar.is_file():
                try:
                    shutil.copyfile(sidecar, target.with_name(target.name + suffix))
                except OSError:
                    pass
        yield target
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _cookies_from_jar(jar: Any, hosts: tuple[str, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cookie in jar:
        domain = (cookie.domain or "").lstrip(".")
        if not any(host in domain or domain.endswith(host) for host in hosts):
            continue
        if not cookie.name or cookie.value is None:
            continue
        out.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": domain,
                "path": cookie.path or "/",
            }
        )
    return out


def _import_domain(browser_cookie3: Any, domain: str, hosts: tuple[str, ...]) -> list[dict[str, Any]]:
    best: list[dict[str, Any]] = []
    files = _chrome_cookie_files()
    if not files:
        jar = browser_cookie3.chrome(domain_name=domain)
        return _cookies_from_jar(jar, hosts)
    for cookie_file in files:
        try:
            with _chrome_cookie_snapshot(cookie_file) as snapshot:
                jar = browser_cookie3.chrome(
                    cookie_file=str(snapshot),
                    domain_name=domain,
                )
        except Exception:
            continue
        cookies = _cookies_from_jar(jar, hosts)
        if len(cookies) > len(best):
            best = cookies
    return best


def import_chrome() -> dict[str, Any]:
    try:
        import browser_cookie3
    except ImportError:
        return {"ok": False, "error": "browser-cookie3 is not installed."}

    jira_hosts = ("atlassian.net", "atlassian.com", "id.atlassian.com")
    trello_hosts = ("trello.com",)
    try:
        jira_cookies = _import_domain(browser_cookie3, "atlassian.net", jira_hosts)
        trello_cookies = _import_domain(browser_cookie3, "trello.com", trello_hosts)
    except Exception as exc:
        return {
            "ok": False,
            "error": (
                "Could not read Chrome cookies. Grant Full Disk Access to Terminal "
                f"(or Cursor) and stay signed in to Jira/Trello in Chrome. ({exc})"
            ),
        }

    data = load_session()
    notes: list[str] = []
    if jira_cookies:
        data["jira"] = {
            "cookies": jira_cookies,
            "source": "chrome",
            "savedAt": _now(),
        }
        notes.append(f"Jira: {len(jira_cookies)} cookie(s)")
    else:
        notes.append("Jira: no Chrome cookies found")
    if trello_cookies:
        data["trello"] = {
            "cookies": trello_cookies,
            "source": "chrome",
            "savedAt": _now(),
        }
        notes.append(f"Trello: {len(trello_cookies)} cookie(s)")
    else:
        notes.append("Trello: no Chrome cookies found")
    save_session(data)
    status = auth_status()
    status["importNotes"] = notes
    return status


def _cookie_rec(cookie: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": cookie.get("name"),
        "value": cookie.get("value"),
        "domain": (cookie.get("domain") or "").lstrip("."),
        "path": cookie.get("path") or "/",
    }


def _playwright_cookies(context: Any) -> dict[str, list[dict[str, Any]]]:
    jira: list[dict[str, Any]] = []
    trello: list[dict[str, Any]] = []
    try:
        raw = context.cookies()
    except Exception:
        raw = []
    for cookie in raw:
        rec = _cookie_rec(cookie)
        domain = rec["domain"]
        if "trello.com" in domain:
            trello.append(rec)
        if "atlassian.net" in domain or "atlassian.com" in domain:
            jira.append(rec)
    return {"jira": jira, "trello": trello}


def _merge_cookies(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for cookie in existing + incoming:
        name = cookie.get("name") or ""
        if not name or cookie.get("value") is None:
            continue
        domain = (cookie.get("domain") or "").lstrip(".")
        merged[(name, domain)] = {
            "name": name,
            "value": cookie.get("value"),
            "domain": domain,
            "path": cookie.get("path") or "/",
        }
    return list(merged.values())


def _has_cloud_session(cookies: list[dict[str, Any]], domain_part: str) -> bool:
    for cookie in cookies:
        if cookie.get("name") != "cloud.session.token":
            continue
        domain = (cookie.get("domain") or "").lstrip(".")
        if domain_part in domain:
            return True
    return False


def _store_from_context(context: Any, targets: tuple[str, ...]) -> None:
    bags = _playwright_cookies(context)
    data = load_session()
    for target in targets:
        cookies = bags.get(target) or []
        if not cookies:
            continue
        previous = (data.get(target) or {}).get("cookies") or []
        data[target] = {
            "cookies": _merge_cookies(previous if isinstance(previous, list) else [], cookies),
            "source": "browser",
            "savedAt": _now(),
        }
    save_session(data)


def _launch_persistent(playwright: Any):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "user_data_dir": str(PROFILE_DIR),
        "headless": False,
        "viewport": {"width": 1280, "height": 900},
        "chromium_sandbox": True,
        "ignore_default_args": ["--enable-automation", "--enable-automation"],
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    try:
        return playwright.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except Exception:
        return playwright.chromium.launch_persistent_context(**kwargs)


def _login_urls(target: str) -> list[tuple[str, str]]:
    defaults = load_defaults()
    board_id = defaults["jira"]["boardId"]
    project = defaults["jira"].get("projectKey") or "COL"
    jira_url = f"{jira_site()}/jira/software/c/projects/{project}/boards/{board_id}"
    trello_url = defaults["trello"].get("boardUrl") or "https://trello.com/b/F0baTV9g/roadmap-planning"
    if target == "jira":
        return [("jira", jira_url)]
    if target == "trello":
        return [("trello", trello_url)]
    return [("jira", jira_url), ("trello", trello_url)]


def _set_login(message: str, **extra: Any) -> None:
    with _lock:
        _login_state["message"] = message
        _login_state.update(extra)


def login_status() -> dict[str, Any]:
    with _lock:
        return dict(_login_state)


def _run_login(target: str) -> None:
    _set_login("Opening a Chrome window for Cisco SSO…", running=True, done=False, error=None, target=target)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _set_login(
            "Playwright is not installed. Restart from start.command.",
            running=False,
            done=True,
            error="Playwright missing",
        )
        return

    steps = _login_urls(target)
    try:
        with sync_playwright() as playwright:
            context = _launch_persistent(playwright)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                for name, url in steps:
                    _set_login(f"Sign in to {name.title()} in the browser window (Duo is fine)…")
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                    except Exception:
                        pass
                    deadline = time.time() + 480
                    while time.time() < deadline:
                        _store_from_context(context, (name,))
                        probe = _browser_probe(context, name)
                        if not probe.get("ok"):
                            probe = probe_jira() if name == "jira" else probe_trello()
                        if probe.get("ok"):
                            _store_from_context(context, (name,))
                            if name == "jira":
                                extra = time.time() + 20
                                while time.time() < extra:
                                    jira_cookies = _playwright_cookies(context).get("jira") or []
                                    if _has_cloud_session(jira_cookies, "atlassian.net"):
                                        break
                                    time.sleep(1)
                                    _store_from_context(context, (name,))
                                _store_from_context(context, (name,))
                            who = probe.get("displayName") or probe.get("username") or ""
                            _set_login(f"{name.title()} session saved{(' as ' + who) if who else ''}.")
                            break
                        time.sleep(1.5)
                    else:
                        _set_login(
                            f"Timed out waiting for {name.title()} login.",
                            running=False,
                            done=True,
                            error=f"{name} login timed out",
                        )
                        return
                _store_from_context(context, tuple(name for name, _ in steps))
                _set_login("", running=False, done=True, error=None)
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except Exception as exc:
        _set_login(
            f"Could not open the sign-in browser: {exc}",
            running=False,
            done=True,
            error=str(exc),
        )


def try_refresh_from_profile() -> dict[str, Any]:
    """Reuse the saved Chrome profile to refresh cookies without a Sign In click.

    Jira's long-lived token often lives on id.atlassian.com. Opening the persistent
    profile lets remember.me / cloud.session.token mint a tenant cookie again.
    """
    with _lock:
        if _login_state.get("running"):
            return {"ok": False, "error": "login running"}
    if not PROFILE_DIR.is_dir():
        return {"ok": False, "error": "no profile"}
    jira_ok = bool(probe_jira().get("ok")) if cookies_for("jira") else False
    trello_ok = bool(probe_trello().get("ok")) if cookies_for("trello") else False
    if jira_ok and trello_ok:
        return {"ok": True, "refreshed": False}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "Playwright missing"}
    steps = []
    if not jira_ok:
        steps.extend(_login_urls("jira"))
    if not trello_ok:
        steps.extend(_login_urls("trello"))
    try:
        with sync_playwright() as playwright:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            kwargs: dict[str, Any] = {
                "user_data_dir": str(PROFILE_DIR),
                "headless": True,
                "viewport": {"width": 1280, "height": 900},
                "chromium_sandbox": True,
                "ignore_default_args": ["--enable-automation", "--enable-automation"],
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            try:
                context = playwright.chromium.launch_persistent_context(channel="chrome", **kwargs)
            except Exception:
                context = playwright.chromium.launch_persistent_context(**kwargs)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                for name, url in steps:
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    except Exception:
                        pass
                    time.sleep(2)
                    _store_from_context(context, (name,))
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "refreshed": True,
        "jira": probe_jira() if cookies_for("jira") else {"ok": False},
        "trello": probe_trello() if cookies_for("trello") else {"ok": False},
    }


def start_login(target: str) -> dict[str, Any]:
    target = (target or "both").strip().lower()
    if target not in {"jira", "trello", "both"}:
        target = "both"
    with _lock:
        if _login_state.get("running"):
            return dict(_login_state)
        _login_state.update(
            {
                "running": True,
                "target": target,
                "message": "Starting sign-in…",
                "done": False,
                "error": None,
            }
        )
    thread = threading.Thread(target=_run_login, args=(target,), daemon=True)
    thread.start()
    return login_status()
