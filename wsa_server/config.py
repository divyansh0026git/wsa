"""Configuration, path resolution, logging, and runtime lock for the WSA proxy server."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Optional

__version__ = "1.2.0"
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "127.0.0.1",
    "retry_attempts": 2,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "relay_send_timeout_sec": 70,
    "gemini_bl": "boq_gemini-web-uiserver_20261111.09_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini",
    "log_requests": True,
    "cookie_file": None,
    "cookie_files": [],
    "proxy": None,
    "api_keys": [],
    "temporary_chats": False,
    "single_account_mode": True,
    "allow_auto_rotate": False,
    "active_relay": None,
    "dom_relay_rollover_turns": 120,
}


RELAY_SESSIONS_FILE = os.path.join(SCRIPT_DIR, ".wsa_relay_sessions.json")


CONFIG = dict(DEFAULT_CONFIG)


_CONFIG_LOCK = threading.Lock()


def _resolve_local_path(path_str: Optional[str]) -> Optional[str]:
    """Resolve relative config paths from the server installation directory."""
    if not path_str:
        return None
    if os.path.isabs(path_str):
        return path_str
    candidate = os.path.join(SCRIPT_DIR, path_str.lstrip("./\\"))
    if os.path.exists(candidate):
        return candidate
    return path_str


def _resolve_cookie_file(default_write: bool = False) -> Optional[str]:
    """Resolve active auth cookie JSON, preferring `wsa-auth.json` with legacy fallback."""
    configured = CONFIG.get("cookie_file")
    if configured:
        return _resolve_local_path(configured)
    for name in ("wsa-auth.json", "gemini-auth.json"):
        candidate = _resolve_local_path(f"./{name}")
        if candidate and os.path.exists(candidate):
            return candidate
    if default_write:
        return os.path.join(SCRIPT_DIR, "wsa-auth.json")
    return None


def log(msg: str):
    if CONFIG["log_requests"]:
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()


def _xsrf_timestamp_ms(token: Optional[str]) -> int:
    """Extract epoch-ms suffix from a Google SNlM0e XSRF token ('...:1794...')."""
    if not token or not isinstance(token, str) or ":" not in token:
        return 0
    suffix = token.rsplit(":", 1)[-1]
    return int(suffix) if suffix.isdigit() else 0


def load_config(path: str):
    resolved = _resolve_local_path(path)
    if resolved and os.path.exists(resolved):
        with open(resolved, encoding="utf-8-sig") as f:
            config_data = json.load(f)
        with _CONFIG_LOCK:
            CONFIG.update(config_data)
            if CONFIG.get("cookie_file"):
                CONFIG["cookie_file"] = _resolve_local_path(CONFIG["cookie_file"])
                cfile = CONFIG["cookie_file"]
                if cfile and os.path.exists(cfile):
                    try:
                        with open(cfile, "r", encoding="utf-8") as cf:
                            cdata = json.load(cf)
                        if isinstance(cdata, dict):
                            cookie_xsrf = cdata.get("xsrf_token")
                            if cookie_xsrf and _xsrf_timestamp_ms(cookie_xsrf) >= _xsrf_timestamp_ms(CONFIG.get("xsrf_token")):
                                CONFIG["xsrf_token"] = cookie_xsrf
                    except Exception:
                        pass
        log(f"Config loaded: {resolved}")
