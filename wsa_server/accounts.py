"""Cookie store, multi-account discovery, SAPISIDHASH signing, and header cookie rotation."""
from __future__ import annotations

import glob
import hashlib
import json
import os
import time

from . import config as _cfg
from .config import CONFIG, _CONFIG_LOCK, _resolve_cookie_file, _resolve_local_path, log

def get_account_pool() -> list:
    """Return ordered list of discovered/configured Pro account auth files."""
    with _CONFIG_LOCK:
        active = CONFIG.get("cookie_file")
        configured_list = list(CONFIG.get("cookie_files") or [])
    if not active and not configured_list:
        return []
    candidates = []
    if active:
        resolved = _resolve_local_path(active)
        if resolved:
            candidates.append(resolved)
    for item in configured_list:
        resolved = _resolve_local_path(item)
        if resolved and resolved not in candidates and os.path.exists(resolved):
            candidates.append(resolved)
    for pattern in (
        os.path.join(_cfg.SCRIPT_DIR, "wsa-auth*.json"),
        os.path.join(_cfg.SCRIPT_DIR, "gemini-auth*.json"),
        os.path.join(_cfg.SCRIPT_DIR, "accounts", "*.json"),
    ):
        for p in sorted(glob.glob(pattern)):
            abs_p = os.path.abspath(p)
            if abs_p not in candidates and os.path.exists(abs_p):
                candidates.append(abs_p)
    return candidates


def _is_auto_rotate_allowed() -> bool:
    """Single-account mode strictly forbids silent mid-run account switching."""
    with _CONFIG_LOCK:
        single_only = bool(CONFIG.get("single_account_mode", True))
        allow_rotate = bool(CONFIG.get("allow_auto_rotate", False))
    return allow_rotate and (not single_only)


def _sync_config_cookie_from_file(cookie_path: str) -> None:
    """Update live CONFIG credentials from a single chosen account JSON file."""
    with _CONFIG_LOCK:
        CONFIG["cookie_file"] = cookie_path
        try:
            with open(cookie_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                if data.get("xsrf_token"):
                    CONFIG["xsrf_token"] = data["xsrf_token"]
                if data.get("gemini_bl") and str(data["gemini_bl"]).startswith("boq_gemini-web-uiserver_"):
                    CONFIG["gemini_bl"] = data["gemini_bl"]
        except Exception:
            pass


_ROTATING_COOKIE_KEYS = {
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "__Secure-1PSIDRTS", "__Secure-3PSIDRTS",
    "SIDCC", "__Secure-1PSIDCC", "__Secure-3PSIDCC", "NID",
}


def update_rotating_cookies_from_headers(resp_headers) -> None:
    """Persist approved Google session-cookie rotations from an upstream response."""
    if not resp_headers:
        return
    if hasattr(resp_headers, "get_all"):
        raw_headers = resp_headers.get_all("Set-Cookie") or []
    elif hasattr(resp_headers, "get_list"):
        raw_headers = resp_headers.get_list("set-cookie") or []
    else:
        raw_headers = []
    if not isinstance(raw_headers, (list, tuple)):
        return
    updates = {}
    for header in raw_headers:
        first_pair = header.split(";", 1)[0].strip()
        if "=" in first_pair:
            key, value = first_pair.split("=", 1)
            if key in _ROTATING_COOKIE_KEYS and value:
                updates[key] = value
    if not updates:
        return
    cookie_file = _resolve_cookie_file()
    if not cookie_file or not os.path.exists(cookie_file):
        return
    with _CONFIG_LOCK:
        try:
            with open(cookie_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            cookie_str = data.get("cookie", "")
            parts, seen = [], set()
            for item in cookie_str.split("; "):
                if "=" not in item:
                    continue
                key, _ = item.split("=", 1)
                value = updates.get(key, item)
                parts.append(f"{key}={value}" if key in updates else value)
                seen.add(key)
            for key, value in updates.items():
                if key not in seen:
                    parts.append(f"{key}={value}")
            data["cookie"] = "; ".join(parts)
            with open(cookie_file, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
        except (OSError, ValueError, TypeError) as exc:
            log(f"Cookie rotation persistence failed: {exc}")


def load_cookie() -> tuple:
    """Load cookie from file. Returns (cookie_str, sapisid)."""
    cookie_file = _resolve_cookie_file()
    if not cookie_file:
        return "", None
    if not os.path.exists(cookie_file):
        return "", None
    try:
        with open(cookie_file, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content.startswith("{"):
            data = json.loads(content)
            cookie_str = data.get("cookie", "")
            sapisid = data.get("sapisid", "")
        else:
            cookie_str = content
            pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
            sapisid = pairs.get("SAPISID", "")
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        return "", None


def make_sapisidhash(sapisid: str) -> str:
    """Generate all three SAPISID hash variants required for Advanced/Argon routing."""
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h} SAPISID1PHASH {ts}_{h} SAPISID3PHASH {ts}_{h}"


def account_prefix() -> str:
    auth_user = CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


def apply_chat_persistence_flags(inner: list) -> None:
    if CONFIG.get("temporary_chats", False):
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]
