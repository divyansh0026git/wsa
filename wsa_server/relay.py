"""Browser DOM extension relay queues, account switching, session continuity, and telemetry."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import threading
import time
import uuid
from typing import Optional

from . import config as _cfg
from .accounts import _sync_config_cookie_from_file, get_account_pool
from .config import CONFIG, _CONFIG_LOCK, _resolve_cookie_file, _resolve_local_path, log
from .models import (
    LAST_UPSTREAM_MODEL,
    MODE_CATEGORY_NAMES,
    MODELS,
    THINKING_LEVEL_NAMES,
    WEB_BRIDGE_MODES,
    WEB_PROVIDER_SPECS,
)
from .multimodal import (
    _RELAY_BLEED_TAIL_RE,
    _get_server_namespace,
    _sanitize_assistant_relay_prose,
    detect_image_mime,
    fetch_image_bytes,
    get_prompt_max_bytes,
    messages_to_delta_prompt,
)
from .parser import clean_gemini_text

_PERSIST_RELAY_SESSIONS = False
_PENDING_SESSION_META: dict = {}
_TOOL_FENCE_STRIP_RE = re.compile(
    r"(?:`{3,}|~{3,})[ \t]*(?:tool[_-]?calls?|function[_-]?calls?)\b[\s\S]*?(?:`{3,}|~{3,})",
    re.IGNORECASE,
)

_KNOWN_BROWSER_NAMES = {"chrome", "brave", "edge"}
KNOWN_WEB_PROVIDERS = tuple(WEB_PROVIDER_SPECS.keys())

_PROVIDER_LIMIT_ERR_RE = re.compile(
    r"(?:\[LIMIT_REACHED\]|"
    r"reached\s+(?:your|the|our)\s+(?:\w+\s+){0,3}(?:limit|cap)|"
    r"(?:\d+\s*[- ]?\s*hour|usage|message|rate|daily(?:\s+(?:chat|message|free))?|hourly|weekly|monthly|session|reasoning|thinking|deepthink|search|credit|free\s+plan)\s*(?:cap|limit)\b|"
    r"(?:limit|quota|cap)\s+(?:reached|exceeded|exhausted|will\s+reset|resets)|"
    r"(?:out\s+of|insufficient|no)\s+(?:venice\s+)?credits|"
    r"upgrade\s+to\s+pro\s+to\s+(?:continue|chat|send)|"
    r"approaching\s+(?:your\s+)?(?:\w+\s+){0,2}limit|"
    r"too\s+many\s+(?:concurrent\s+)?requests|"
    r"resource[_\s-]*exhausted|"
    r"quota\s+exceeded|"
    r"usage\s+cap|"
    r"(?:server|service|system|model)\s+is\s+(?:currently\s+)?busy|"
    r"high\s+(?:demand|traffic)|"
    r"responses\s+will\s+use\s+another\s+model|"
    r"resets?\s+(?:at|after|in)\b|"
    r"at\s+(?:high\s+)?capacity|"
    r"http\s*(?:error\s*)?429\b)",
    re.IGNORECASE,
)


_PROMPT_TOO_LONG_ERR_RE = re.compile(
    r"(?:\[PROMPT_TOO_LONG\]|"
    r"the\s+message\s+you\s+submitted\s+was\s+too\s+long|"
    r"message\s+(?:you\s+submitted\s+)?(?:is|was)\s+too\s+long|"
    r"please\s+edit\s+it\s+and\s+resubmit|"
    r"prompt\s+(?:is\s+)?too\s+long|"
    r"exceeds?\s+(?:the\s+)?max(?:imum)?\s+(?:message\s+)?length|"
    r"character\s+limit\s+exceeded|"
    r"maximum\s+context\s+(?:length|limit|window)\s+exceeded|"
    r"context\s+(?:window|limit)\s+exceeded|"
    r"context_length_exceeded)",
    re.IGNORECASE,
)


class PromptTooLongError(RuntimeError):
    """Raised when an upstream web composer rejects a prompt as too long to submit."""

    def __init__(self, message: str, provider: str = ""):
        super().__init__(message)
        self.provider = _detect_provider_from_hint(provider) or str(provider or "").strip().lower()


def is_prompt_too_long_error(message: Optional[str]) -> bool:
    """Return True when an error string describes web composer message length rejection."""
    return bool(_PROMPT_TOO_LONG_ERR_RE.search(str(message or "")))


class ProviderLimitError(RuntimeError):
    """Raised when an upstream web provider hits a rate/usage/message limit."""

    def __init__(self, message: str, provider: str = ""):
        super().__init__(message)
        self.provider = _detect_provider_from_hint(provider) or str(provider or "").strip().lower()


_SOFT_LIMIT_WARNING_RE = re.compile(
    r"\b(?:over\s+|nearly\s+|almost\s+|approaching\s+|used\s+)?(?:[1-9]?\d)\s*%\s+of\s+(?:your\s+)?(?:\S+\s+){0,3}(?:limit|limi|quota|usage)\b",
    re.IGNORECASE,
)

_HARD_LIMIT_BLOCK_RE = re.compile(
    r"(?:\b(?:reached|exceeded|hit|exhausted|will\s+reset|resets?\s+(?:at|in|after)|0\s+messages|out\s+of\s+(?:free\s+)?messages|upgrade\s+to\s+pro\s+to\s+(?:continue|chat|send)|cannot\s+send|try\s+again\s+(?:later|at|in)|until\s+\d)\b)",
    re.IGNORECASE,
)


def is_provider_limit_error(message: Optional[str]) -> bool:
    """Return True when an error string describes a web provider rate or usage limit."""
    msg = str(message or "")
    if is_prompt_too_long_error(msg):
        return False
    if "[SAFEGUARD_PAUSED]" in msg:
        return False
    # Non-blocking informational usage warnings (e.g. "Over 90% of your 5-hour limit")
    # must not be misclassified as an exhausted/blocked account unless a hard-block signal is present.
    if _SOFT_LIMIT_WARNING_RE.search(msg) and not _HARD_LIMIT_BLOCK_RE.search(msg):
        return False
    return bool(_PROVIDER_LIMIT_ERR_RE.search(msg))


def _detect_provider_from_hint(provider: Optional[str] = None) -> str:
    p = str(provider or "").strip().lower()
    if not p:
        return ""
    if "claude" in p:
        return "claude"
    if "venice" in p or "vernice" in p or "v4-1-flash" in p or "v4.1-flash" in p:
        return "venice"
    if "chatgpt" in p or "openai" in p or re.match(r"^(?:gpt|o[134])\b", p):
        return "chatgpt"
    if "deepseek" in p or p in ("ds", "r1"):
        return "deepseek"
    if "gemini" in p:
        return "gemini"
    return ""


def _normalize_provider(provider: Optional[str] = None) -> str:
    return _detect_provider_from_hint(provider) or "gemini"


def _is_provider_thread_url(url: str, provider: Optional[str] = None) -> bool:
    """Return True when `url` points to a resumable chat thread for `provider`."""
    u = str(url or "").strip().lower()
    if not u:
        return False
    prov = _normalize_provider(provider)
    if prov == "gemini":
        return "/app/" in u
    if prov == "claude":
        return "/chat/" in u
    if prov == "chatgpt":
        return "/c/" in u
    if prov == "deepseek":
        return "/chat/" in u or "/s/" in u
    if prov == "venice":
        return bool(
            re.search(r"/chat/(?:agent|classic)/[a-z0-9_-]+", u)
            or re.search(r"/chat/(?!agent(?:[/?#]|$)|classic(?:[/?#]|$))[a-z0-9_-]+", u)
        )
    return False


def _session_relay_key(session_id: str, provider: Optional[str] = None) -> str:
    sid = (session_id or "").strip()
    if not sid:
        return ""
    prov = _normalize_provider(provider)
    return sid if prov == "gemini" else f"{sid}::{prov}"


def _relay_provider_email(entry: Optional[dict], provider: str = "gemini") -> str:
    """Return normalized active account email on a relay entry for `provider`."""
    if not isinstance(entry, dict):
        return ""
    prov = _normalize_provider(provider)
    p_info = (entry.get("providers") or {}).get(prov)
    if isinstance(p_info, dict):
        p_email = str(p_info.get("account_email") or p_info.get("email") or "").strip()
        if p_email:
            return p_email
    active_prov = _normalize_provider(entry.get("active_provider") or "") if entry.get("active_provider") else ""
    if active_prov in ("", prov):
        return str(entry.get("account_email") or "").strip()
    return ""


def _relay_provider_auth_user(entry: Optional[dict], provider: str = "gemini") -> str:
    """Return normalized Gemini `/u/<n>` auth_user slot on a relay entry, or ''."""
    if not isinstance(entry, dict):
        return ""
    prov = _normalize_provider(provider)
    if prov != "gemini":
        return ""
    p_info = (entry.get("providers") or {}).get("gemini")
    if isinstance(p_info, dict) and p_info.get("auth_user") not in (None, ""):
        return str(p_info.get("auth_user")).strip()
    val = entry.get("auth_user")
    return str(val).strip() if val not in (None, "") else ""


def _record_invalidated_chat_url_locked(url: str, *, account_email: str = "", explicit: bool = False) -> None:
    """Remember a thread URL evicted by account/relay switching so stale client hints are rejected."""
    norm = str(url or "").strip().rstrip("/")
    if not norm:
        return
    inv = _dom_relay.setdefault("invalidated_chat_urls", {})
    inv[norm] = {
        "account_email": "*" if explicit else str(account_email or "").strip().lower(),
        "explicit": bool(explicit),
        "ts": time.time(),
    }
    if len(inv) > 200:
        oldest = sorted(inv.items(), key=lambda kv: float((kv[1] or {}).get("ts") or 0.0))[: len(inv) - 200]
        for ok, _ in oldest:
            inv.pop(ok, None)


def _is_chat_url_stale_for_relay_locked(url: str, *, provider: str = "gemini", relay_id: str = "") -> bool:
    """Unlocked core check (requires `_dom_relay_lock` held): True if *url* is invalidated for relay."""
    norm = str(url or "").strip().rstrip("/")
    if not norm:
        return False
    prov = _normalize_provider(provider)
    inv = (_dom_relay.get("invalidated_chat_urls") or {}).get(norm)
    if not isinstance(inv, dict):
        return False
    if inv.get("explicit") or inv.get("account_email") == "*":
        return True
    old_email = str(inv.get("account_email") or "").strip().lower()
    if not old_email:
        return True
    target_id = str(relay_id or _dom_relay.get("active_relay_id") or "")
    entry = (_dom_relay.get("relays") or {}).get(target_id) or {}
    cur_email = _relay_provider_email(entry, prov).strip().lower()
    return cur_email != old_email


def is_chat_url_stale_for_relay(url: str, *, provider: str = "gemini", relay_id: str = "") -> bool:
    """Return True if *url* belonged to an account/session that was invalidated for the active relay."""
    with _dom_relay_lock:
        return _is_chat_url_stale_for_relay_locked(url, provider=provider, relay_id=relay_id)


def _relay_has_provider_tab(entry: Optional[dict], prov: str) -> bool:
    """Return True when a browser relay reports an open live tab for `prov`."""
    if not isinstance(entry, dict) or not prov:
        return False
    p_map = entry.get("providers")
    if isinstance(p_map, dict):
        p_rec = p_map.get(prov)
        if isinstance(p_rec, dict) and p_rec.get("has_tab"):
            return True
    active_p = _detect_provider_from_hint(entry.get("active_provider") or entry.get("active_site") or "")
    return bool(active_p and active_p == prov)


def _find_provider_fallback_relay_locked(relays: dict, current_id: str, prov: str, now: float) -> str:
    """Pick an online relay advertising `prov` when the active relay lacks that provider tab."""
    if not isinstance(relays, dict) or not prov:
        return ""
    cur_entry = relays.get(current_id) or {}
    cur_online = bool(cur_entry) and (now - float(cur_entry.get("last_seen") or 0.0)) <= 30.0
    if cur_online and _relay_has_provider_tab(cur_entry, prov):
        return ""
    candidates = []
    for cid, entry in relays.items():
        if not cid or cid == current_id or not isinstance(entry, dict):
            continue
        last_seen = float(entry.get("last_seen") or 0.0)
        if (now - last_seen) > 30.0:
            continue
        if _relay_has_provider_tab(entry, prov):
            candidates.append((last_seen, cid))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][1]


def _clear_relay_sessions_on_email_change_locked(cid: str, prev_entry: Optional[dict], new_entry: Optional[dict]) -> int:
    """Invalidate same-chat affinity for a provider on `cid` when its signed-in email or Gemini slot changes."""
    if not cid or not isinstance(prev_entry, dict) or not isinstance(new_entry, dict):
        return 0
    changed_providers = []
    for pk in KNOWN_WEB_PROVIDERS:
        old_email = _relay_provider_email(prev_entry, pk).lower()
        new_email = _relay_provider_email(new_entry, pk).lower()
        email_changed = bool(old_email and new_email and old_email != new_email)
        auth_changed = False
        if pk == "gemini":
            old_auth = _relay_provider_auth_user(prev_entry, "gemini")
            new_auth = _relay_provider_auth_user(new_entry, "gemini")
            auth_changed = bool(old_auth and new_auth and old_auth != new_auth)
        if email_changed or auth_changed:
            changed_providers.append((pk, old_email or "*", new_email or "*"))
    if not changed_providers:
        return 0
    sessions = _dom_relay.get("sessions") or {}
    changed_map = {pk: (old_e, new_e) for pk, old_e, new_e in changed_providers}
    cleared = 0
    for s_key, sinfo in list(sessions.items()):
        if not isinstance(sinfo, dict):
            continue
        owner_id = str(sinfo.get("relay_id") or "")
        s_prov = _normalize_provider(sinfo.get("provider") or "gemini")
        if s_prov in changed_map and owner_id in ("", cid):
            old_e, _ = changed_map[s_prov]
            old_url = str(sinfo.get("chat_url") or "").strip()
            if old_url:
                _record_invalidated_chat_url_locked(old_url, account_email=old_e, explicit=(old_e == "*"))
            sessions.pop(s_key, None)
            _PENDING_SESSION_META.pop(s_key, None)
            cleared += 1
    if cleared:
        _persist_relay_sessions_locked()
        for pk, old_e, new_e in changed_providers:
            log(f"Cleared {cleared} stale {pk} relay chat(s) after account switch on {cid}: {old_e} -> {new_e}")
    return cleared


def _merge_relay_providers(prev_providers: Optional[dict], client_info: dict) -> dict:
    """Merge per-provider tab/account/model state across supported web providers for a browser relay."""
    base = prev_providers if isinstance(prev_providers, dict) else {}
    inc_map = client_info.get("providers") if isinstance(client_info.get("providers"), dict) else {}
    raw_site = str(client_info.get("active_site") or client_info.get("site") or "").strip().lower()
    raw_prov = (
        _detect_provider_from_hint(client_info.get("provider") or client_info.get("active_provider"))
        or _detect_provider_from_hint(raw_site)
    )
    fallback_email = str(client_info.get("email") or client_info.get("account_email") or "").strip()
    fallback_badge = str(client_info.get("model_badge") or "").strip()

    merged = {}
    for pk in KNOWN_WEB_PROVIDERS:
        prev_p = base.get(pk) if isinstance(base.get(pk), dict) else {}
        inc_p = inc_map.get(pk) if isinstance(inc_map.get(pk), dict) else {}

        raw_has = inc_p.get("has_tab")
        if raw_has is None and f"has_{pk}" in client_info and client_info.get(f"has_{pk}") not in (None, ""):
            raw_has = str(client_info.get(f"has_{pk}")).strip().lower() in ("1", "true", "yes")
        if raw_has is None:
            has_tab = bool(prev_p.get("has_tab")) or (raw_prov == pk)
        else:
            has_tab = bool(raw_has)

        inc_auth = str(inc_p.get("auth_user") or (client_info.get("auth_user") if pk == "gemini" else "") or "").strip()
        prev_auth = str(prev_p.get("auth_user") or "").strip()
        auth_changed = bool(pk == "gemini" and prev_auth and inc_auth and prev_auth != inc_auth)

        p_email = str(
            inc_p.get("account_email")
            or inc_p.get("email")
            or client_info.get(f"{pk}_email")
            or (fallback_email if raw_prov == pk or (not raw_prov and pk == "gemini") else "")
            or ("" if auth_changed else prev_p.get("account_email"))
            or ""
        ).strip()
        p_badge = str(
            inc_p.get("model_badge")
            or client_info.get(f"{pk}_model")
            or (fallback_badge if raw_prov == pk or (not raw_prov and pk == "gemini") else "")
            or prev_p.get("model_badge")
            or ""
        ).strip()
        p_url = str(inc_p.get("url") or ("" if auth_changed else prev_p.get("url")) or "").strip()
        p_auth = str(inc_auth or prev_auth or "0").strip()

        merged[pk] = {
            "has_tab": has_tab,
            "account_email": p_email,
            "email": p_email,
            "model_badge": p_badge,
            "auth_user": p_auth,
            "url": p_url,
        }
    return merged


def _sanitize_relay_tag(val: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (val or "").strip().lower()).strip("-")
    return cleaned or "browser"


def _relay_cookie_path(label: str) -> str:
    acc_dir = os.path.join(_cfg.SCRIPT_DIR, "accounts")
    os.makedirs(acc_dir, exist_ok=True)
    return os.path.join(acc_dir, f"relay-{_sanitize_relay_tag(label)}.json")


def _resolve_relay_id(target, relays: dict, now: Optional[float] = None) -> Optional[str]:
    """Match a target name/index/email/browser against registered DOM relays."""
    if not relays or target is None:
        return None
    ts = now if now is not None else time.time()
    ordered = sorted(
        relays.items(),
        key=lambda kv: (
            0 if (ts - float(kv[1].get("last_seen") or 0.0)) <= 12.0 else 1,
            -float(kv[1].get("last_seen") or 0.0),
            kv[0],
        ),
    )
    t_str = str(target).strip()
    if not t_str:
        return None
    if t_str.startswith("relay:") and t_str[6:].isdigit():
        idx = int(t_str[6:])
        return ordered[idx][0] if 0 <= idx < len(ordered) else None
    t_low = t_str.lower()
    if t_low in relays:
        return t_low
    for cid, entry in ordered:
        if cid.lower() == t_low:
            return cid
    if t_low in _KNOWN_BROWSER_NAMES:
        for cid, entry in ordered:
            if str(entry.get("browser") or "").strip().lower() == t_low:
                return cid
        for cid, entry in ordered:
            if str(entry.get("label") or "").strip().lower() == t_low:
                return cid
        return None
    for field in ("label", "account_email", "browser"):
        for cid, entry in ordered:
            val = str(entry.get(field) or "").strip().lower()
            if val and val == t_low:
                return cid
    for cid, entry in ordered:
        combo = f"{entry.get('browser', '')}:u{entry.get('auth_user', '0')}".lower()
        lbl = str(entry.get("label") or "").lower()
        email = str(entry.get("account_email") or "").lower()
        if t_low == combo or (t_low and (t_low in lbl or (email and t_low in email))):
            return cid
    return None


_dom_relay_lock = threading.Lock()


_relay_req_ctx = threading.local()


_dom_relay = {
    "pending": {},
    "cancelled_req_ids": {},
    "browser_cmds": {},
    "last_poll_ts": 0.0,
    "sessions": {},
    "relays": {},
    "active_relay_id": None,
    "pinned_relay_label": None,
    "explicit_relay_lock": False,
    "recent_requests": {},
    "invalidated_chat_urls": {},
}


_ps_relay_state = {
    "session_url": "",
    "cookies": "",
    "auth_user": "0",
    "bl": "",
    "at": "",
    "updated_at": 0.0,
}


def list_relays_status(max_age: float = 12.0) -> dict:
    """Return all registered browser relays and single active relay lock state."""
    now = time.time()
    with _CONFIG_LOCK:
        single_mode = bool(CONFIG.get("single_account_mode", True))
    with _dom_relay_lock:
        relays = dict(_dom_relay.get("relays") or {})
        active_id = _dom_relay.get("active_relay_id")
        pinned_label = _dom_relay.get("pinned_relay_label")
        explicit = bool(_dom_relay.get("explicit_relay_lock"))
        items = []
        for idx, (cid, entry) in enumerate(sorted(relays.items(), key=lambda kv: (-float(kv[1].get("last_seen") or 0.0), kv[0]))):
            age = round(max(0.0, now - float(entry.get("last_seen") or 0.0)), 1)
            online = age <= max_age
            cfile = entry.get("cookie_file")
            providers = dict(entry.get("providers") or {})
            items.append({
                "index": idx,
                "client_id": cid,
                "browser": entry.get("browser") or "chrome",
                "label": entry.get("label") or entry.get("browser") or cid,
                "auth_user": str(entry.get("auth_user") or "0"),
                "account_email": entry.get("account_email") or "",
                "model_badge": entry.get("model_badge") or "",
                "active_site": entry.get("active_site") or "",
                "active_provider": entry.get("active_provider") or "",
                "providers": providers,
                "cookie_file": os.path.basename(cfile) if cfile else None,
                "last_seen_age_sec": age,
                "online": online,
                "active": bool(active_id and cid == active_id),
            })
        active_entry = relays.get(active_id or "") or {}
    return {
        "active_relay_id": active_id,
        "active_relay_label": active_entry.get("label") or pinned_label,
        "active_browser": active_entry.get("browser"),
        "active_email": active_entry.get("account_email") or "",
        "active_site": active_entry.get("active_site") or "",
        "active_provider": active_entry.get("active_provider") or "",
        "active_model_badge": active_entry.get("model_badge") or "",
        "active_providers": dict(active_entry.get("providers") or {}),
        "explicit_lock": explicit,
        "single_account_mode": single_mode,
        "relays": items,
    }


def select_active_relay(
    target=None,
    client_id: Optional[str] = None,
    browser: Optional[str] = None,
    label: Optional[str] = None,
    explicit: bool = True,
    session_id: str = "",
    providers: Optional[dict] = None,
    active_site: str = "",
    account_email: str = "",
    model_badge: str = "",
) -> dict:
    """Bind a single browser relay (and its paired cookie file) to WSA."""
    now = time.time()
    sync_path = None
    with _dom_relay_lock:
        relays = _dom_relay.setdefault("relays", {})
        chosen_id = None
        if client_id and client_id in relays:
            chosen_id = client_id
        elif client_id:
            chosen_id = client_id
            b_tag = _sanitize_relay_tag(browser or "chrome")
            l_tag = str(label or b_tag).strip()
            relays[chosen_id] = {
                "client_id": chosen_id,
                "browser": b_tag,
                "label": l_tag,
                "auth_user": "0",
                "account_email": account_email,
                "model_badge": model_badge,
                "active_site": active_site,
                "active_provider": _normalize_provider(active_site) if active_site else "",
                "providers": _merge_relay_providers({}, {
                    "providers": providers or {},
                    "active_site": active_site,
                    "email": account_email,
                    "model_badge": model_badge,
                }),
                "last_seen": now,
                "cookie_file": _relay_cookie_path(l_tag) if os.path.exists(_relay_cookie_path(l_tag)) else None,
            }
        elif target is not None:
            chosen_id = _resolve_relay_id(target, relays, now=now)
        elif label or browser:
            chosen_id = _resolve_relay_id(label or browser, relays, now=now)

        if not chosen_id or chosen_id not in relays:
            return {
                "ok": False,
                "error": f"Browser relay not found: {target or client_id or label or browser}",
                "relays": list(relays.keys()),
            }

        entry = relays[chosen_id]
        prev_snapshot = {
            "account_email": entry.get("account_email") or "",
            "active_provider": entry.get("active_provider") or "",
            "providers": {
                k: dict(v) for k, v in (entry.get("providers") or {}).items() if isinstance(v, dict)
            },
        }
        if browser:
            entry["browser"] = _sanitize_relay_tag(browser)
        if label:
            entry["label"] = str(label).strip() or entry.get("label") or entry["browser"]
        if account_email:
            entry["account_email"] = account_email
        if model_badge:
            entry["model_badge"] = model_badge
        if active_site:
            entry["active_site"] = active_site
            entry["active_provider"] = _normalize_provider(active_site)
        if providers or active_site or account_email or model_badge:
            entry["providers"] = _merge_relay_providers(entry.get("providers"), {
                "providers": providers or {},
                "active_site": active_site or entry.get("active_site") or "",
                "email": account_email or entry.get("account_email") or "",
                "model_badge": model_badge or entry.get("model_badge") or "",
            })

        prev_active_id = _dom_relay.get("active_relay_id") or ""
        _dom_relay["active_relay_id"] = chosen_id
        _dom_relay["pinned_relay_label"] = entry.get("label") or entry.get("browser")
        _dom_relay["explicit_relay_lock"] = bool(explicit)
        _dom_relay["last_poll_ts"] = float(entry.get("last_seen") or now)

        # An account change affects its provider's sessions; a browser switch
        # only invalidates the session that requested the switch. Other projects
        # retain their own relay affinity and are checked again when resumed.
        sessions = _dom_relay.get("sessions") or {}
        cleared_sessions = _clear_relay_sessions_on_email_change_locked(chosen_id, prev_snapshot, entry)
        for s_key, sinfo in list(sessions.items()):
            owner_id = sinfo.get("relay_id") or ""
            changed_owner = bool(owner_id and owner_id != chosen_id)
            same_sid = bool(
                session_id
                and (
                    s_key == session_id
                    or s_key.startswith(f"{session_id}::")
                    or s_key.startswith(f"{session_id}:role:")
                )
            )
            changed_active = bool(same_sid and prev_active_id and prev_active_id != chosen_id)
            if same_sid and (changed_owner or changed_active):
                old_u = str(sinfo.get("chat_url") or "").strip() if isinstance(sinfo, dict) else ""
                if old_u:
                    _record_invalidated_chat_url_locked(old_u, explicit=True)
                sessions.pop(s_key, None)
                _PENDING_SESSION_META.pop(s_key, None)
                cleared_sessions += 1
        if cleared_sessions:
            _persist_relay_sessions_locked()

        candidate_cfile = entry.get("cookie_file") or _relay_cookie_path(entry.get("label") or entry.get("browser") or chosen_id)
        if candidate_cfile and os.path.exists(candidate_cfile):
            sync_path = candidate_cfile
            entry["cookie_file"] = candidate_cfile

    if sync_path:
        _sync_config_cookie_from_file(sync_path)

    log(
        f"Bound single active relay -> {entry.get('label') or chosen_id} "
        f"(browser={entry.get('browser')}, id={chosen_id}, cleared_chats={cleared_sessions})"
    )
    return {
        "ok": True,
        "active_relay_id": chosen_id,
        "active_relay_label": entry.get("label") or entry.get("browser"),
        "browser": entry.get("browser"),
        "account_email": entry.get("account_email") or "",
        "model_badge": entry.get("model_badge") or "",
        "active_site": entry.get("active_site") or "",
        "active_provider": entry.get("active_provider") or "",
        "providers": dict(entry.get("providers") or {}),
        "auth_user": str(entry.get("auth_user") or "0"),
        "active_account": os.path.basename(sync_path) if sync_path else (entry.get("label") or chosen_id),
        "cookie_file": sync_path,
        "cleared_chats": cleared_sessions,
    }


def switch_account(target, session_id: str = "") -> dict:
    """Switch active single account (connected browser relay OR cookie_file pool entry)."""
    with _dom_relay_lock:
        relays = dict(_dom_relay.get("relays") or {})
        matched_relay = _resolve_relay_id(target, relays)
    if matched_relay:
        res = select_active_relay(target=matched_relay, explicit=True, session_id=session_id)
        if res.get("ok"):
            res["pool"] = get_account_pool()
            return res

    pool = get_account_pool()
    chosen = None
    if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
        idx = int(target)
        if 0 <= idx < len(pool):
            chosen = pool[idx]
    elif isinstance(target, str) and target.strip():
        resolved = _resolve_local_path(target.strip())
        if resolved and os.path.exists(resolved):
            chosen = os.path.abspath(resolved)
        else:
            for p in pool:
                if os.path.basename(p) == target.strip() or target.strip().lower() in os.path.basename(p).lower():
                    chosen = p
                    break
    if not chosen or not os.path.exists(chosen):
        return {"ok": False, "error": f"Account or browser relay not found: {target}", "pool": pool}

    _sync_config_cookie_from_file(chosen)
    # If the chosen credential file belongs to a registered relay, bind that relay too.
    try:
        with open(chosen, "r", encoding="utf-8") as f:
            cdata = json.load(f)
        if isinstance(cdata, dict) and (cdata.get("client_id") or cdata.get("relay_label")):
            with _dom_relay_lock:
                relays = dict(_dom_relay.get("relays") or {})
                rel_match = _resolve_relay_id(cdata.get("client_id") or cdata.get("relay_label"), relays)
            if rel_match:
                select_active_relay(target=rel_match, explicit=True)
    except Exception:
        pass
    log(f"Switched active Pro account -> {os.path.basename(chosen)}")
    return {"ok": True, "active_account": chosen, "pool": get_account_pool()}


def rotate_account(reason: str = "") -> Optional[str]:
    """Rotate cleanly to the next online browser relay or credential file in the pool."""
    now = time.time()
    with _dom_relay_lock:
        relays = dict(_dom_relay.get("relays") or {})
        online_ids = [
            cid for cid, entry in sorted(relays.items())
            if (now - float(entry.get("last_seen") or 0.0)) <= 12.0
        ]
        cur_relay = _dom_relay.get("active_relay_id")
    if len(online_ids) > 1:
        try:
            idx = online_ids.index(cur_relay)
        except ValueError:
            idx = -1
        nxt_id = online_ids[(idx + 1) % len(online_ids)]
        rres = select_active_relay(target=nxt_id, explicit=True)
        if rres.get("ok"):
            log(f"Rotated single active relay ({reason or 'manual'}): {rres.get('active_relay_label')}")
            return rres.get("cookie_file") or rres.get("active_relay_label")

    pool = get_account_pool()
    if len(pool) <= 1:
        return None
    with _CONFIG_LOCK:
        current = _resolve_local_path(CONFIG.get("cookie_file"))
    try:
        idx = pool.index(os.path.abspath(current)) if current else -1
    except ValueError:
        idx = -1
    next_acct = pool[(idx + 1) % len(pool)]
    res = switch_account(next_acct)
    if res.get("ok"):
        log(f"Rotated Pro account ({reason or 'manual'}): {os.path.basename(next_acct)}")
        return next_acct
    return None


def clear_session_relay_chats(
    session_id: str = "",
    providers: Optional[list] = None,
    include_roles: bool = True,
    client_urls: Optional[list] = None,
) -> dict:
    """Clear stored relay chat bindings for `session_id` (and mark old URLs stale for account switches)."""
    sid = str(session_id or "").strip()
    prov_filter = {
        _normalize_provider(p)
        for p in (providers or [])
        if str(p or "").strip()
    }
    cleared = 0
    with _dom_relay_lock:
        sessions = _dom_relay.get("sessions") or {}
        for s_key, sinfo in list(sessions.items()):
            if not isinstance(sinfo, dict):
                continue
            if sid:
                matches_sid = (
                    s_key == sid
                    or s_key.startswith(f"{sid}::")
                    or (include_roles and s_key.startswith(f"{sid}:role:"))
                )
                if not matches_sid:
                    continue
            s_prov = _normalize_provider(sinfo.get("provider") or "gemini")
            if prov_filter and s_prov not in prov_filter:
                continue
            old_url = str(sinfo.get("chat_url") or "").strip()
            if old_url:
                _record_invalidated_chat_url_locked(old_url, explicit=True)
            sessions.pop(s_key, None)
            _PENDING_SESSION_META.pop(s_key, None)
            cleared += 1
        for raw_u in (client_urls or []):
            u = str(raw_u or "").strip()
            if u:
                _record_invalidated_chat_url_locked(u, explicit=True)
        if cleared:
            _persist_relay_sessions_locked()
    if cleared or client_urls:
        log(
            f"Cleared {cleared} relay session chat(s) after account switch"
            + (f" (session={sid})" if sid else "")
        )
    return {"ok": True, "cleared": cleared, "session_id": sid}


def seed_session_hint_url(
    session_id: str,
    hint_chat_url: str,
    provider: str = "gemini",
    relay_id: str = "",
) -> bool:
    """Validate and bind a client-supplied thread URL only if it matches the active account."""
    sid = str(session_id or "").strip()
    prov = _normalize_provider(provider)
    url = str(hint_chat_url or "").strip()
    if not sid or not _is_provider_thread_url(url, prov):
        return False
    if is_chat_url_stale_for_relay(url, provider=prov, relay_id=relay_id):
        return False
    s_key = _session_relay_key(sid, prov)
    with _dom_relay_lock:
        sessions = _dom_relay.setdefault("sessions", {})
        target_id = str(relay_id or _dom_relay.get("active_relay_id") or "")
        entry = (_dom_relay.get("relays") or {}).get(target_id) or {}
        cur_email = _relay_provider_email(entry, prov).strip().lower()
        cur_auth = _relay_provider_auth_user(entry, prov)
        cur_s = sessions.get(s_key)
        if not cur_s:
            sessions[s_key] = {
                "last_sig": "",
                "chat_url": url,
                "relay_id": target_id,
                "account_email": cur_email,
                "auth_user": cur_auth,
                "provider": prov,
                "turns": 1,
                "updated_at": time.time(),
            }
            return True
        sess_email = str(cur_s.get("account_email") or "").strip().lower()
        if sess_email and cur_email and sess_email != cur_email:
            return False
        if prov == "gemini":
            sess_auth = str(cur_s.get("auth_user") or "").strip()
            if sess_auth and cur_auth and sess_auth != cur_auth:
                return False
        if not cur_s.get("chat_url"):
            cur_s["chat_url"] = url
            if cur_email and not cur_s.get("account_email"):
                cur_s["account_email"] = cur_email
            if cur_auth and not cur_s.get("auth_user"):
                cur_s["auth_user"] = cur_auth
        return True


def _assistant_turn_sig(content: str, tool_calls: Optional[list] = None) -> str:
    """Stable short fingerprint for an assistant turn (DOM-bleed, tool-fence, and compaction resilient)."""
    text = _RELAY_BLEED_TAIL_RE.sub("", str(content or ""))
    names = [
        (tc.get("function", {}).get("name", "") if isinstance(tc, dict) else "")
        for tc in (tool_calls or [])
    ]
    if names:
        text = _TOOL_FENCE_STRIP_RE.sub("", text)
        text = _sanitize_assistant_relay_prose(text, has_tool_calls=True)
    norm_text = " ".join(text.split())[:200]
    raw = json.dumps([norm_text, names], ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _tools_sig(tools: Optional[list]) -> str:
    """Short deterministic signature of allowed tool names and schema keys."""
    if not tools:
        return ""
    sigs = []
    for item in tools:
        if not isinstance(item, dict):
            continue
        fn = item.get("function", item) if item.get("type") == "function" else item
        name = str(fn.get("name") or "").strip()
        params = sorted((fn.get("parameters") or {}).get("properties", {}).keys())
        if name:
            sigs.append([name, params])
    if not sigs:
        return ""
    sigs.sort(key=lambda x: x[0])
    return hashlib.sha1(json.dumps(sigs, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def _sys_role_meta(messages: Optional[list]) -> tuple:
    """Identify explicit WSA role transitions (`explorer`/`architect`/`coder`/`verifier`/`main`) cleanly."""
    for msg in (messages or []):
        if msg.get("role") == "system" and msg.get("content"):
            txt = str(msg["content"])
            lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
            head_lines = []
            for ln in lines[:8]:
                if ln.startswith(("Working Directory", "### Scoped Context", "## Workspace Context")):
                    break
                head_lines.append(ln)
            head = "\n".join(head_lines).strip()[:600]
            first = lines[0] if lines else ""
            m = re.match(
                r"^(?:(?:You are|Role:)\s*WSA\s+|Workflow Phase\s*[—:-]\s*)(Explorer|Architect|Coder|Verifier)\b",
                first,
                re.IGNORECASE,
            )
            if m:
                return m.group(1).lower(), head
            if (
                "workflow judgment" in first.lower()
                or '"mode":"orchestrate"|"direct"' in txt.lower()
            ):
                return "workflow_router", "\n".join(lines)[:600]
            if (
                "engineering workflow preferences for this repository:" in txt.lower()
                or "my engineering workflow preferences for working together on this codebase:" in txt.lower()
                or "my engineering preferences for this project:" in txt.lower()
                or "personal local workspace cli (`wsa`)" in first.lower()
            ):
                return "main", (
                    "Workflow Phase — Main Coding: returning to full coding & verification scope "
                    "(including local reads, surgical edits, shell/python checks, and browser inspection) "
                    "for the next request in this project."
                )
            if (
                "you are collaborating with the user inside their personal" in first.lower()
                or "dynamic runtime adaptation" in txt.lower()
                or first.startswith("You operate at your full")
            ):
                return "main", "\n".join(lines[:3])[:320]
    return "", ""


def _persist_relay_sessions_locked(path: Optional[str] = None) -> None:
    """Persist relay session affinity safely while caller holds `_dom_relay_lock`."""
    if not _PERSIST_RELAY_SESSIONS and path is None:
        return
    target = path or _cfg.RELAY_SESSIONS_FILE
    try:
        sessions = dict(_dom_relay.get("sessions") or {})
        tmp = f"{target}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"sessions": sessions, "saved_at": int(time.time())}, fh, indent=2)
        os.replace(tmp, target)
    except Exception:
        pass


def load_relay_sessions(path: Optional[str] = None, enable_persist: bool = True) -> int:
    """Load persisted project chat thread mappings into `_dom_relay['sessions']`."""
    global _PERSIST_RELAY_SESSIONS
    if enable_persist:
        _PERSIST_RELAY_SESSIONS = True
    target = path or _cfg.RELAY_SESSIONS_FILE
    if not os.path.exists(target) and path is None:
        legacy_target = os.path.join(_cfg.SCRIPT_DIR, ".gwa_relay_sessions.json")
        if os.path.exists(legacy_target):
            target = legacy_target
    if not os.path.exists(target):
        return 0
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        loaded = data.get("sessions") if isinstance(data, dict) else None
        if not isinstance(loaded, dict):
            return 0
        with _dom_relay_lock:
            sessions = _dom_relay.setdefault("sessions", {})
            count = 0
            for sid, info in loaded.items():
                if isinstance(sid, str) and sid.strip() and isinstance(info, dict):
                    sessions[sid.strip()] = dict(info)
                    count += 1
            if len(sessions) > 50:
                oldest = sorted(sessions.items(), key=lambda kv: kv[1].get("updated_at", 0.0))[: len(sessions) - 50]
                for old_key, _ in oldest:
                    sessions.pop(old_key, None)
        return count
    except Exception:
        return 0


def record_session_relay_turn(
    session_id: str,
    assistant_content: str,
    tool_calls: Optional[list] = None,
    chat_url: str = "",
    relay_id: str = "",
    provider: str = "gemini",
) -> None:
    """Record the latest answered assistant signature and owning relay for same-chat continuation."""
    s_key = _session_relay_key(session_id, provider)
    if not s_key:
        return
    sig = _assistant_turn_sig(assistant_content, tool_calls)
    now = time.time()
    with _dom_relay_lock:
        sessions = _dom_relay.setdefault("sessions", {})
        prev = sessions.get(s_key) or {}
        pending_meta = _PENDING_SESSION_META.pop(s_key, None)
        if isinstance(pending_meta, tuple) and len(pending_meta) >= 3:
            pending_t_sig, pending_s_sig, started_new_chat = pending_meta[0], pending_meta[1], bool(pending_meta[2])
        elif isinstance(pending_meta, tuple) and len(pending_meta) == 2:
            pending_t_sig, pending_s_sig, started_new_chat = pending_meta[0], pending_meta[1], False
        else:
            pending_t_sig, pending_s_sig, started_new_chat = "", "", False
        prev_url = prev.get("chat_url", "")
        relays = _dom_relay.get("relays") or {}
        owner = relay_id or prev.get("relay_id") or (_dom_relay.get("active_relay_id") or "")
        owner_entry = relays.get(owner) or {}
        prov_norm = _normalize_provider(provider)
        acct_email = _relay_provider_email(owner_entry, prov_norm)
        acct_auth = _relay_provider_auth_user(owner_entry, prov_norm)
        if started_new_chat:
            turns = 1
            if chat_url and chat_url != prev_url and _is_provider_thread_url(chat_url, prov_norm):
                effective_url = chat_url
            else:
                effective_url = ""
        elif prev_url and chat_url and chat_url != prev_url and _is_provider_thread_url(chat_url, prov_norm):
            turns = 1
            effective_url = chat_url
        else:
            turns = int(prev.get("turns") or 0) + 1
            effective_url = chat_url or prev_url
        if effective_url and _is_chat_url_stale_for_relay_locked(effective_url, provider=prov_norm, relay_id=owner):
            effective_url = ""
        sessions[s_key] = {
            "last_sig": sig,
            "chat_url": effective_url,
            "relay_id": owner,
            "account_email": acct_email,
            "auth_user": acct_auth,
            "provider": prov_norm,
            "turns": turns,
            "tools_sig": pending_t_sig or prev.get("tools_sig", ""),
            "sys_sig": pending_s_sig or prev.get("sys_sig", ""),
            "updated_at": now,
        }
        if len(sessions) > 50:
            oldest = sorted(sessions.items(), key=lambda kv: kv[1].get("updated_at", 0.0))[: len(sessions) - 50]
            for old_key, _ in oldest:
                sessions.pop(old_key, None)
        _persist_relay_sessions_locked()


def prepare_session_relay_prompt(
    session_id: str,
    messages: list,
    tools: list,
    full_prompt: str,
    full_images: list,
    provider: str = "gemini",
    force_new_chat: Optional[bool] = None,
    relay_id: str = "",
) -> tuple:
    """Decide whether `session_id` can continue cleanly in the same browser chat thread.

    Returns (prompt_to_send, fallback_prompt, images_to_use, new_chat, hint_chat_url).
    hint_chat_url is the server-known thread URL when new_chat=False, or "" when new_chat=True.
    The extension uses this to navigate back to the right conversation on the next turn.
    """
    prov = _normalize_provider(provider)
    sid = _session_relay_key(session_id, prov)
    cur_tools_sig = _tools_sig(tools)
    cur_sys_sig, role_directive = _sys_role_meta(messages)

    def _stash_pending(is_new_chat: bool = False) -> None:
        if sid:
            with _dom_relay_lock:
                _PENDING_SESSION_META[sid] = (cur_tools_sig, cur_sys_sig, bool(is_new_chat))

    if force_new_chat is True:
        _stash_pending(True)
        return full_prompt, full_prompt, full_images, True, ""
    if not sid:
        with _CONFIG_LOCK:
            default_new = bool(CONFIG.get("dom_relay_new_chat", True))
        return full_prompt, full_prompt, full_images, default_new, ""
    with _CONFIG_LOCK:
        rollover_turns = int(CONFIG.get("dom_relay_rollover_turns", 120) or 0)
    with _dom_relay_lock:
        sess = dict((_dom_relay.get("sessions") or {}).get(sid) or {})
        active_relay_id = str(relay_id or _dom_relay.get("active_relay_id") or "")
        active_relay_entry = dict((_dom_relay.get("relays") or {}).get(active_relay_id) or {})
    last_sig = sess.get("last_sig")
    saved_chat_url = str(sess.get("chat_url") or "").strip()
    if not last_sig and not saved_chat_url:
        _stash_pending(True)
        return full_prompt, full_prompt, full_images, True, ""
    turns_in_thread = int(sess.get("turns") or 0)
    if rollover_turns > 0 and turns_in_thread >= rollover_turns:
        log(f"DOM relay session [{sid}]: rolling over to fresh chat after {turns_in_thread} turns (>= {rollover_turns})")
        _stash_pending(True)
        return full_prompt, full_prompt, full_images, True, ""
    if sess.get("relay_id") and active_relay_id and sess.get("relay_id") != active_relay_id:
        log(f"DOM relay session [{sid}]: starting new_chat (relay switched {sess.get('relay_id')} -> {active_relay_id})")
        _stash_pending(True)
        return full_prompt, full_prompt, full_images, True, ""
    sess_email = str(sess.get("account_email") or "").strip().lower()
    cur_email = _relay_provider_email(active_relay_entry, prov).strip().lower()
    if sess_email and cur_email and sess_email != cur_email:
        log(f"DOM relay session [{sid}]: starting new_chat (account switched {sess_email} -> {cur_email})")
        _stash_pending(True)
        return full_prompt, full_prompt, full_images, True, ""
    if prov == "gemini":
        sess_auth = str(sess.get("auth_user") or "").strip()
        cur_auth = _relay_provider_auth_user(active_relay_entry, "gemini")
        if sess_auth and cur_auth and sess_auth != cur_auth:
            log(f"DOM relay session [{sid}]: starting new_chat (gemini auth_user switched {sess_auth} -> {cur_auth})")
            _stash_pending(True)
            return full_prompt, full_prompt, full_images, True, ""
    if saved_chat_url and is_chat_url_stale_for_relay(saved_chat_url, provider=prov, relay_id=active_relay_id):
        log(f"DOM relay session [{sid}]: starting new_chat (stale invalidated chat_url rejected)")
        _stash_pending(True)
        return full_prompt, full_prompt, full_images, True, ""

    prev_t_sig = str(sess.get("tools_sig") or "")
    prev_s_sig = str(sess.get("sys_sig") or "")
    tools_changed = bool(tools) and bool(prev_t_sig) and (cur_tools_sig != prev_t_sig)
    role_changed = bool(cur_sys_sig) and (
        cur_sys_sig == "workflow_router"
        or (bool(prev_s_sig) and cur_sys_sig != prev_s_sig)
        or (not prev_s_sig and cur_sys_sig != "main")
    )

    non_sys = [m for m in (messages or []) if m.get("role") != "system"]
    last_asst_idx = -1
    matched_idx = -1
    for i in range(len(non_sys) - 1, -1, -1):
        if non_sys[i].get("role") == "assistant":
            if last_asst_idx < 0:
                last_asst_idx = i
            if last_sig and _assistant_turn_sig(
                non_sys[i].get("content") or "",
                non_sys[i].get("tool_calls"),
            ) == last_sig:
                matched_idx = i
                break
    anchor_idx = matched_idx if matched_idx >= 0 else last_asst_idx
    if anchor_idx < 0 or anchor_idx >= len(non_sys) - 1:
        # If no assistant anchor was found in `non_sys` (e.g. session resume or single-chat stage transition), resume pinned thread.
        has_thread_url = _is_provider_thread_url(saved_chat_url, prov)
        if has_thread_url and non_sys and non_sys[-1].get("role") == "user":
            tail_delta, tail_images = messages_to_delta_prompt(
                [non_sys[-1]],
                messages,
                tools,
                provider=prov,
                include_tools_spec=tools_changed,
                role_directive=(role_directive if (role_changed or tools_changed) else ""),
            )
            if tail_delta.strip():
                log(f"DOM relay session [{sid}]: continuing pinned project chat {saved_chat_url}")
                _stash_pending(False)
                return tail_delta, full_prompt, tail_images, False, saved_chat_url
        log(f"DOM relay session [{sid}]: starting new_chat (missing prior assistant turn in messages, last_asst_idx={last_asst_idx})")
        _stash_pending(True)
        return full_prompt, full_prompt, full_images, True, ""
    if matched_idx < 0 and not saved_chat_url:
        log(f"DOM relay session [{sid}]: starting new_chat (assistant signature mismatch and no saved chat_url)")
        _stash_pending(True)
        return full_prompt, full_prompt, full_images, True, ""
    delta_msgs = non_sys[anchor_idx + 1 :]
    if anchor_idx == last_asst_idx and any(m.get("role") == "assistant" for m in delta_msgs):
        log(f"DOM relay session [{sid}]: starting new_chat (unexpected assistant message in delta)")
        _stash_pending(True)
        return full_prompt, full_prompt, full_images, True, ""
    prov_limit = get_prompt_max_bytes(prov)
    delta_prompt, delta_images = messages_to_delta_prompt(
        delta_msgs,
        messages,
        tools,
        provider=prov,
        include_tools_spec=tools_changed,
        role_directive=(role_directive if (role_changed or tools_changed) else ""),
        max_bytes=prov_limit,
    )
    if not delta_prompt.strip() or len(delta_prompt) > prov_limit:
        log(f"DOM relay session [{sid}]: starting new_chat (delta_len={len(delta_prompt)}/{prov_limit})")
        _stash_pending(True)
        return full_prompt, full_prompt, full_images, True, ""
    _stash_pending(False)
    return delta_prompt, full_prompt, delta_images, False, saved_chat_url


def is_dom_relay_active(max_age: float = 12.0) -> bool:
    """Return True if the active browser relay has polled /pending-prompt within max_age seconds."""
    now = time.time()
    with _dom_relay_lock:
        active_id = _dom_relay.get("active_relay_id")
        relays = _dom_relay.get("relays") or {}
        if active_id and active_id in relays:
            last_ts = float(relays[active_id].get("last_seen") or 0.0)
            return bool(last_ts and (now - last_ts) <= max_age)
        last_ts = _dom_relay.get("last_poll_ts", 0.0)
    return bool(last_ts and (now - last_ts) <= max_age)


def register_or_touch_relay(client_info: Optional[dict]) -> dict:
    """Register or refresh a browser extension relay and enforce single-active-relay arbitration."""
    if not isinstance(client_info, dict) or not str(client_info.get("client_id") or "").strip():
        with _dom_relay_lock:
            cur_id = _dom_relay.get("active_relay_id")
            cur_entry = (_dom_relay.get("relays") or {}).get(cur_id or "") or {}
        return {
            "client_id": "",
            "is_active": True,
            "active_relay_id": cur_id or "",
            "active_relay_label": cur_entry.get("label") or "",
        }

    now = time.time()
    cid = str(client_info["client_id"]).strip()
    browser = _sanitize_relay_tag(client_info.get("browser") or "chrome")
    label = str(client_info.get("label") or browser).strip() or browser
    auth_user = str(client_info.get("auth_user") if client_info.get("auth_user") is not None else "0")
    email = str(client_info.get("email") or client_info.get("account_email") or "").strip()
    badge = str(client_info.get("model_badge") or "").strip()
    active_site = str(client_info.get("active_site") or client_info.get("site") or "").strip()
    active_prov = (
        _detect_provider_from_hint(client_info.get("provider") or client_info.get("active_provider"))
        or _detect_provider_from_hint(active_site)
    )

    with _CONFIG_LOCK:
        cfg_pref = str(CONFIG.get("active_relay") or "").strip().lower()

    sync_path = None
    with _dom_relay_lock:
        relays = _dom_relay.setdefault("relays", {})
        prev = relays.get(cid) or {}
        cfile = prev.get("cookie_file")
        default_cfile = _relay_cookie_path(label)
        if not cfile and os.path.exists(default_cfile):
            cfile = default_cfile
        merged_providers = _merge_relay_providers(prev.get("providers"), client_info)
        eff_site = active_site or prev.get("active_site") or WEB_PROVIDER_SPECS.get(active_prov, {}).get("site", "")
        eff_prov = active_prov or prev.get("active_provider") or (_normalize_provider(eff_site) if eff_site else "")
        entry = {
            "client_id": cid,
            "browser": browser or prev.get("browser") or "chrome",
            "label": label or prev.get("label") or browser,
            "auth_user": auth_user,
            "account_email": email or prev.get("account_email") or "",
            "model_badge": badge or prev.get("model_badge") or "",
            "active_site": eff_site,
            "active_provider": eff_prov,
            "providers": merged_providers,
            "cookie_file": cfile,
            "last_seen": now,
        }
        relays[cid] = entry
        _clear_relay_sessions_on_email_change_locked(cid, prev, entry)

        pref = (_dom_relay.get("pinned_relay_label") or cfg_pref or "").strip().lower()
        explicit = bool(_dom_relay.get("explicit_relay_lock"))
        cur_id = _dom_relay.get("active_relay_id")

        matches_pref = bool(
            pref and pref in (cid.lower(), entry["label"].lower(), entry["browser"].lower(), entry["account_email"].lower())
        )
        if not cur_id or cur_id not in relays:
            if not pref or matches_pref:
                _dom_relay["active_relay_id"] = cid
                if entry.get("cookie_file") and os.path.exists(entry["cookie_file"]):
                    sync_path = entry["cookie_file"]
        elif cur_id != cid and not explicit:
            cur_age = now - float(relays[cur_id].get("last_seen") or 0.0)
            pending_for_cur = any(
                (item.get("target_relay_id") in (None, "", cur_id))
                for item in (_dom_relay.get("pending") or {}).values()
            )
            if matches_pref and not pending_for_cur:
                _dom_relay["active_relay_id"] = cid
                if entry.get("cookie_file") and os.path.exists(entry["cookie_file"]):
                    sync_path = entry["cookie_file"]
            elif cur_age > 15.0 and not pending_for_cur and not pref:
                _dom_relay["active_relay_id"] = cid
                if entry.get("cookie_file") and os.path.exists(entry["cookie_file"]):
                    sync_path = entry["cookie_file"]

        # Prune relays absent > 10 minutes that aren't active.
        for old_id, old_entry in list(relays.items()):
            if old_id != _dom_relay.get("active_relay_id") and (now - float(old_entry.get("last_seen") or 0.0)) > 600.0:
                relays.pop(old_id, None)

        active_id = _dom_relay.get("active_relay_id") or ""
        active_entry = relays.get(active_id) or {}
        is_active = bool(active_id == cid)

    if sync_path and is_active:
        _sync_config_cookie_from_file(sync_path)

    return {
        "client_id": cid,
        "is_active": is_active,
        "active_relay_id": active_id,
        "active_relay_label": active_entry.get("label") or active_entry.get("browser") or "",
    }


def is_argon_badge(badge: str) -> bool:
    """True only when the live browser tab's model picker really reports Argon (or Carbon)."""
    b = (badge or "").lower()
    return ("argon" in b or "carbon" in b) and not re.search(r"\b(medium|low|standard)\b", b)


def argon_strict_relay_required() -> bool:
    """Authenticated Pro accounts serve 4 Argon only through the live tab (processSession).

    Unless the user explicitly opts into `argon_allow_pro_fallback`, mode 7/8 requests must go
    through the browser DOM relay and must never be silently substituted by 3.1 Pro / Flash-Lite.
    """
    with _CONFIG_LOCK:
        allow_fallback = bool(CONFIG.get("argon_allow_pro_fallback"))
        cfile = _resolve_local_path(CONFIG.get("cookie_file"))
    return (not allow_fallback) and bool(cfile and os.path.exists(cfile))


_CLAUDE_THINK_NOISE_LINE_RE = re.compile(
    r"^(?:running|done|loading(?:\.{3}|…)?|\d+(?:\.\d+)?s|"
    r"(?:thought|reasoned|worked)\s+(?:for|about)\s+(?:a\s+(?:couple|few)\s+of\s+seconds|\d+(?:\.\d+)?\s*(?:s|sec|secs|seconds?|m|min)?)(?:\s*·\s*\d+\s*steps?)?(?:\s+running)?|"
    r"thought\s+process|thinking(?:\.{3}|…|\s+process)?|extended\s+thinking|deep\s*think(?:ing)?|"
    r"show\s+thinking|hide\s+thinking|already\s+read\s+\d+\s+web\s+pages?|"
    r"you\s+stopped\s+(?:claude|chatgpt|deepseek|venice)['’]?s\s+response|"
    r"show\s+message\s+actions|(?:claude|chatgpt|deepseek|venice)\s+responded\b.*)$",
    re.IGNORECASE,
)


def _clean_claude_thinking_text(text: str) -> str:
    """Strip Claude Extended Thinking status chrome (timers, running tokens, glued/duplicate headlines)."""
    raw_s = str(text or "").replace("\r\n", "\n")
    # Split glued `<text>[22s]running<CapitalizedStep>` and deduplicate doubled spans
    raw_s = re.sub(
        r"([A-Za-z0-9.?!\]\)])(?:\d{1,3}(?:\.\d+)?s)?running(?=[A-Z][a-z]{2,}|$)",
        r"\1\n",
        raw_s,
    )
    raw_s = re.sub(r"(\S.{11,}?)\1+", r"\1", raw_s)
    out = []
    seen = set()
    for raw in raw_s.splitlines():
        is_list_item = bool(re.match(r"^\s*(?:[-*]|\d+\.)\s+", raw))
        s = re.sub(r"[ \t]+", " ", raw).rstrip() if is_list_item else re.sub(r"\s+", " ", raw).strip()
        k = s.strip()
        if not k or _CLAUDE_THINK_NOISE_LINE_RE.match(k):
            continue
        s = re.sub(
            r"(?:\s*(?:\d{1,3}(?:\.\d+)?s\s*running|\brunning|\d{1,3}(?:\.\d+)?s))+$",
            "",
            s,
            flags=re.IGNORECASE,
        ).rstrip()
        k2 = s.strip()
        if not k2 or _CLAUDE_THINK_NOISE_LINE_RE.match(k2):
            continue
        dedup_key = k2.casefold()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        out.append(s if is_list_item else k2)
    return "\n".join(out)


def _compute_thinking_delta(prev_sent: str, incoming: str, provider: str = "") -> tuple:
    """Return `(delta_to_emit, updated_sent)` without duplicating already-streamed thought lines."""
    p_low = str(provider or "").lower()
    inc = _clean_claude_thinking_text(incoming) if p_low in ("claude", "chatgpt", "deepseek", "venice") else str(incoming or "")
    prev = str(prev_sent or "")
    if not inc or inc == prev:
        return "", prev
    if not prev:
        return inc, inc
    if inc.startswith(prev):
        return inc[len(prev):], inc
    if prev.startswith(inc):
        return "", prev
    seen = {ln.strip() for ln in prev.splitlines() if ln.strip()}
    new_lines = [ln for ln in inc.splitlines() if ln.strip() and ln.strip() not in seen]
    if not new_lines:
        return "", prev
    prefix = "\n" if (prev and not prev.endswith("\n")) else ""
    delta = prefix + "\n".join(new_lines)
    return delta, prev + delta


def _clean_streaming_gemini_text(text: str) -> str:
    """Clean streamed markdown while holding unclosed internal sandbox code_reference/stdout blocks."""
    cleaned = clean_gemini_text(text or "", strip=False)
    m = re.search(
        r"```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+(?:\n|$)",
        cleaned,
    )
    if m:
        cleaned = cleaned[: m.start()]
    return cleaned


def _optimize_image_for_vlm(
    data: bytes,
    mime: str,
    *,
    max_edge: int = 1568,
    max_bytes: int = 650_000,
) -> tuple[bytes, str]:
    """Resize oversized screenshots to <=1568px and compress to WebP/JPEG when Pillow is available.

    Gracefully returns `(data, mime)` unchanged if Pillow is unavailable, the image is
    already compact and within bounds, or the image is an animated GIF.
    """
    if not data:
        return data, mime
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return data, mime
    try:
        with Image.open(io.BytesIO(data)) as im:
            if getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1:
                return data, mime
            w, h = im.size
            needs_resize = max(w, h) > max_edge
            needs_compress = len(data) > max_bytes
            if not needs_resize and not needs_compress:
                return data, mime
            if needs_resize:
                scale = max_edge / max(w, h)
                new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
                work = im.resize(new_size, resample=Image.Resampling.LANCZOS)
            else:
                work = im.copy()
        candidates: list[tuple[bytes, str]] = []
        # Try WebP first (best text/UI edge sharpness per byte and preserves alpha).
        try:
            buf = io.BytesIO()
            webp_img = work if work.mode in ("RGB", "RGBA") else work.convert("RGBA")
            webp_img.save(buf, format="WEBP", quality=86, method=4)
            candidates.append((buf.getvalue(), "image/webp"))
        except Exception:
            pass
        # Also try JPEG (universally accepted across all web AI uploaders).
        try:
            buf = io.BytesIO()
            if work.mode in ("RGBA", "LA") or (work.mode == "P" and "transparency" in work.info):
                bg = Image.new("RGB", work.size, (255, 255, 255))
                rgba = work.convert("RGBA")
                bg.paste(rgba, mask=rgba.split()[-1])
                rgb_img = bg
            else:
                rgb_img = work.convert("RGB")
            rgb_img.save(buf, format="JPEG", quality=86, optimize=True)
            candidates.append((buf.getvalue(), "image/jpeg"))
        except Exception:
            pass
        # Also try optimized PNG (often smallest for crisp flat UI / sparse synthetic graphics).
        try:
            buf = io.BytesIO()
            work.save(buf, format="PNG", optimize=True)
            candidates.append((buf.getvalue(), "image/png"))
        except Exception:
            pass
        if not candidates:
            return data, mime
        best_bytes, best_mime = min(candidates, key=lambda pair: len(pair[0]))
        if needs_resize or len(best_bytes) < len(data):
            return best_bytes, best_mime
        return data, mime
    except Exception:
        return data, mime


def _serialize_relay_images(images: Optional[list]) -> list:
    """Convert `(bytes, mime)` or URL image items into JSON-safe base64 payloads for DOM relay."""
    if not images:
        return []
    srv = _get_server_namespace()
    fetch_fn = getattr(srv, "fetch_image_bytes", fetch_image_bytes)
    detect_fn = getattr(srv, "detect_image_mime", detect_image_mime)
    ext_map = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/bmp": "bmp",
    }
    serialized = []
    for idx, item in enumerate(images[:4]):
        try:
            if isinstance(item, tuple) and len(item) == 2:
                src, mime = item
            else:
                src, mime = item, "image/png"
            if isinstance(src, str):
                data = fetch_fn(src)
            elif isinstance(src, (bytes, bytearray)):
                data = bytes(src)
            else:
                continue
            if not data or len(data) > 24 * 1024 * 1024:
                continue
            mime = detect_fn(data, mime or "image/png")
            data, mime = _optimize_image_for_vlm(data, mime)
            if not data or len(data) > 8 * 1024 * 1024:
                continue
            ext = ext_map.get(mime, "png")
            serialized.append({
                "filename": f"screenshot_{idx + 1}.{ext}",
                "mime_type": mime,
                "data": base64.b64encode(data).decode("ascii"),
            })
        except Exception as e:
            log(f"DOM relay image encode skipped: {e}")
    return serialized


def dom_relay_iter(
    prompt: str,
    timeout: float = 300.0,
    pickup_timeout: float = 45.0,
    step: float = 1.0,
    session_id: str = "",
    fallback_prompt: str = "",
    new_chat: Optional[bool] = None,
    client_request_id: str = "",
    images: Optional[list] = None,
    provider: str = "gemini",
):
    """Queue a prompt for the bound live browser AI tab ('gemini' or 'claude').

    Yields ("keepalive", "") every `step` seconds while waiting so SSE clients do not time out,
    streaming delta events and finally yielding ("result", {...}). Raises RuntimeError on pickup
    timeout, total timeout, extension error, or offline explicit relay. Accepts whichever model
    variant the user has selected directly inside the live browser tab.
    """
    req_id = str(uuid.uuid4())[:8]
    ctx_meta = getattr(_relay_req_ctx, "relay_meta", None)
    requested_relay_id = ""
    if isinstance(ctx_meta, dict):
        if not session_id:
            session_id = str(ctx_meta.get("session_id") or "").strip()
        if not fallback_prompt:
            fallback_prompt = str(ctx_meta.get("fallback_prompt") or "")
        if not client_request_id:
            client_request_id = str(ctx_meta.get("client_request_id") or "").strip()
        if ctx_meta.get("relay_prompt"):
            prompt = str(ctx_meta["relay_prompt"])
        if ctx_meta.get("provider"):
            provider = str(ctx_meta.get("provider"))
        if ctx_meta.get("target_relay_id"):
            requested_relay_id = str(ctx_meta.get("target_relay_id") or "").strip()
        if new_chat is None and "new_chat" in ctx_meta and ctx_meta.get("new_chat") is not None:
            new_chat = bool(ctx_meta["new_chat"])
        if images is None and ctx_meta.get("relay_images"):
            images = ctx_meta.get("relay_images")
        ctx_meta["relay_used"] = True
    # hint_chat_url: server-side known thread URL for same-chat continuation.
    # Passed to the extension so it can navigate back to the right conversation
    # even if the tab has drifted to another URL between turns.
    _hint_chat_url = str((ctx_meta or {}).get("hint_chat_url") or "").strip() if ctx_meta else ""
    prov = _normalize_provider(provider)
    spec = WEB_PROVIDER_SPECS.get(prov, WEB_PROVIDER_SPECS["gemini"])
    site_name = spec["site"]
    site_url = spec["url"]
    prov_label = spec["label"]
    if new_chat is None:
        with _CONFIG_LOCK:
            new_chat = bool(CONFIG.get("dom_relay_new_chat", True))

    dedup_key = f"{session_id}:{prov}:{client_request_id}" if client_request_id else ""

    # Check whether this exact client_request_id is already in-flight or recently completed
    existing_wait_entry = None
    existing_wait_event = None
    cached_result = None
    if dedup_key:
        now_ts = time.time()
        with _dom_relay_lock:
            recent = _dom_relay.setdefault("recent_requests", {})
            for rk in [
                k for k, v in recent.items()
                if v.get("event") is None and (now_ts - float(v.get("ts") or 0.0)) > 300.0
            ]:
                recent.pop(rk, None)
            active_dedup_key = dedup_key
            entry = recent.get(active_dedup_key)
            if entry is None and prov == "gemini" and client_request_id:
                legacy_key = f"{session_id}:{client_request_id}"
                entry = recent.get(legacy_key)
                if entry is not None:
                    active_dedup_key = legacy_key
            if entry:
                if entry.get("result") is not None:
                    cached_result = dict(entry["result"])
                elif entry.get("error"):
                    recent.pop(active_dedup_key, None)
                else:
                    existing_wait_entry = entry
                    existing_wait_event = entry.get("event")

    if cached_result is not None:
        if isinstance(ctx_meta, dict):
            if cached_result.get("chat_url"):
                ctx_meta["chat_url"] = cached_result["chat_url"]
            if cached_result.get("relay_id"):
                ctx_meta["relay_id"] = cached_result["relay_id"]
        yield ("result", cached_result)
        return

    if existing_wait_entry is not None:
        ev_existing = existing_wait_event
        wait_start = time.time()
        while ev_existing is not None and not ev_existing.wait(timeout=step):
            with _dom_relay_lock:
                primary_alive = any(
                    it.get("event") is ev_existing and not it.get("cancelled")
                    for it in (_dom_relay.get("pending") or {}).values()
                )
            if not primary_alive and (time.time() - wait_start) >= timeout:
                raise RuntimeError(f"DOM relay timeout ({int(timeout)}s) waiting on deduplicated request")
            yield ("keepalive", "")
        with _dom_relay_lock:
            final_res = dict(existing_wait_entry["result"]) if existing_wait_entry.get("result") is not None else None
            final_err = existing_wait_entry.get("error")
        if final_err:
            if is_prompt_too_long_error(final_err):
                raise PromptTooLongError(final_err, provider=prov)
            if is_provider_limit_error(final_err):
                raise ProviderLimitError(final_err, provider=prov)
            raise RuntimeError(final_err)
        if final_res is not None:
            if isinstance(ctx_meta, dict):
                if final_res.get("chat_url"):
                    ctx_meta["chat_url"] = final_res["chat_url"]
                if final_res.get("relay_id"):
                    ctx_meta["relay_id"] = final_res["relay_id"]
            yield ("result", final_res)
            return

    ev = threading.Event()
    wake_ev = threading.Event()
    # Wait briefly if the explicitly bound relay hasn't heartbeated in >60s before declaring it offline.
    for _wait_try in range(6):
        now = time.time()
        offline_err = None
        with _dom_relay_lock:
            relays = _dom_relay.get("relays") or {}
            active_id = _dom_relay.get("active_relay_id") or ""
            target_relay_id = requested_relay_id or active_id
            explicit_lock = bool(requested_relay_id or _dom_relay.get("explicit_relay_lock"))
            target_entry = relays.get(target_relay_id) or {}
            target_label = target_entry.get("label") or target_entry.get("browser") or target_relay_id or "browser"
            if requested_relay_id and not target_entry:
                offline_err = f"Bound {prov_label} relay '{requested_relay_id}' is not connected."
            elif explicit_lock and target_relay_id and target_entry:
                age = now - float(target_entry.get("last_seen") or 0.0)
                if age > 60.0:
                    offline_err = (
                        f"Bound {prov_label} relay '{target_label}' is offline (last seen {int(age)}s ago). "
                        f"Open {site_url} in {target_entry.get('browser', 'that browser')} "
                        "or switch browser in WSA with `/account <chrome|brave>`."
                    )
        if not offline_err:
            break
        if _wait_try < 5:
            time.sleep(0.5)
        else:
            raise RuntimeError(offline_err)

    relay_images_payload = _serialize_relay_images(images)
    if images and len(relay_images_payload) != min(len(images), 4):
        raise RuntimeError("[IMAGE_ATTACH_FAILED] Could not prepare all requested attachments for the browser.")

    dedup_entry = None
    with _dom_relay_lock:
        now_route = time.time()
        relays = _dom_relay.get("relays") or {}
        active_id = _dom_relay.get("active_relay_id") or ""
        target_relay_id = requested_relay_id or active_id
        if requested_relay_id and requested_relay_id not in relays:
            raise RuntimeError(f"Bound {prov_label} relay '{requested_relay_id}' is not connected.")
        routed_to_provider_relay = False
        if not requested_relay_id and not _dom_relay.get("explicit_relay_lock"):
            fb_relay_id = _find_provider_fallback_relay_locked(relays, target_relay_id, prov, now_route)
            if fb_relay_id:
                target_relay_id = fb_relay_id
                routed_to_provider_relay = True
        target_entry = relays.get(target_relay_id) or {}
        target_label = target_entry.get("label") or target_entry.get("browser") or target_relay_id or "browser"

        _dom_relay["pending"][req_id] = {
            "prompt": prompt,
            "fallback_prompt": fallback_prompt or prompt,
            "images": relay_images_payload,
            "session_id": session_id,
            "client_request_id": client_request_id,
            "provider": prov,
            "target_relay_id": target_relay_id,
            "explicit_relay_target": bool((requested_relay_id and target_relay_id == requested_relay_id) or routed_to_provider_relay),
            "dispatched_to": "",
            "response": None,
            "thinking": "",
            "model": "",
            "chat_url": "",
            "hint_chat_url": _hint_chat_url,
            "event": ev,
            "wake_event": wake_ev,
            "error": None,
            "cancelled": False,
            "dispatched": False,
            "dispatched_at": 0.0,
            "queued_mono": time.monotonic(),
            "dispatched_mono": None,
            "submitted_mono": None,
            "first_token_mono": None,
            "status_seq": 0,
            "status_stage": "queued",
            "images_attached": None,
            "browser_upload_ms": None,
            "browser_submit_ms": None,
            "has_images": bool(relay_images_payload),
            "idle_polls_since_dispatch": 0,
            "new_chat": bool(new_chat),
            "progress_seq": 0,
            "progress_response": "",
            "progress_thinking": "",
            "progress_model": "",
            "progress_chat_url": "",
            "last_progress_ts": 0.0,
            "last_busy_ts": 0.0,
            "last_generating_ts": 0.0,
            "is_generating": False,
        }
        if isinstance(ctx_meta, dict):
            ctx_meta["req_id"] = req_id
        if dedup_key:
            recent = _dom_relay.setdefault("recent_requests", {})
            dedup_entry = {"event": ev, "result": None, "error": None, "ts": time.time()}
            recent[dedup_key] = dedup_entry
            if len(recent) > 32:
                oldest = sorted(recent.items(), key=lambda kv: float(kv[1].get("ts") or 0.0))[: len(recent) - 32]
                for rk, _ in oldest:
                    recent.pop(rk, None)

    def _finish_dedup_err(msg: str, *, limit_hit: bool = False, too_long: bool = False) -> RuntimeError:
        if dedup_entry is not None:
            with _dom_relay_lock:
                dedup_entry["error"] = msg
                dedup_entry["event"] = None
                ev.set()
        wake_ev.set()
        if too_long or is_prompt_too_long_error(msg):
            return PromptTooLongError(msg, provider=prov)
        if limit_hit or is_provider_limit_error(msg):
            return ProviderLimitError(msg, provider=prov)
        return RuntimeError(msg)

    mode_tag = "new-chat" if new_chat else f"same-chat:{session_id or 'default'}"
    log(f"DOM relay [{req_id}] ({prov}, {mode_tag}, target={target_label}): queued prompt ({len(prompt)} chars)...")
    start = time.time()
    last_keepalive_ts = start
    sent_seq = 0
    sent_status_seq = 0
    emitted_generating = False
    streamed_thinking = ""
    streamed_response = ""
    completed_cleanly = False
    emit_relay_status = bool(relay_images_payload or client_request_id)
    if emit_relay_status:
        yield ("relay_status", {
            "req_id": req_id,
            "client_request_id": client_request_id,
            "provider": prov,
            "stage": "queued",
            "has_images": bool(relay_images_payload),
            "images_attached": None,
        })
    try:
        while not ev.is_set():
            wait_slice = min(float(step), 0.25)
            wake_ev.wait(timeout=wait_slice)
            wake_ev.clear()

            prog_events = []
            with _dom_relay_lock:
                cur_item = _dom_relay["pending"].get(req_id) or {}
                dispatched = bool(cur_item.get("dispatched"))
                cancelled_flag = bool(cur_item.get("cancelled"))
                cur_seq = int(cur_item.get("progress_seq") or 0)
                cur_status_seq = int(cur_item.get("status_seq") or 0)
                cur_status_stage = str(cur_item.get("status_stage") or "queued")
                cur_images_attached = cur_item.get("images_attached")
                cur_upload_ms = cur_item.get("browser_upload_ms")
                cur_q_mono = cur_item.get("queued_mono")
                cur_d_mono = cur_item.get("dispatched_mono")
                cur_s_mono = cur_item.get("submitted_mono")
                prog_resp = cur_item.get("progress_response") or ""
                prog_think = cur_item.get("progress_thinking") or ""
                prog_model = " ".join((cur_item.get("progress_model") or "").split())
                last_prog_ts = float(cur_item.get("last_progress_ts") or 0.0)
                last_gen_ts = float(cur_item.get("last_generating_ts") or 0.0)
                last_busy_ts = float(cur_item.get("last_busy_ts") or 0.0)
                is_generating_now = bool(cur_item.get("is_generating"))
                has_live_turn = bool(
                    cur_seq > 0
                    or cur_s_mono is not None
                    or last_gen_ts > 0.0
                    or streamed_thinking
                    or streamed_response
                )
                live_heartbeat_ts = max(last_gen_ts, last_busy_ts if has_live_turn else 0.0)
                last_act = max(start, last_prog_ts, live_heartbeat_ts)
                dispatched_at = float(cur_item.get("dispatched_at") or start)
                dispatched_id = cur_item.get("dispatched_to") or target_relay_id or ""
                d_entry_live = (_dom_relay.get("relays") or {}).get(dispatched_id) or {}
                relay_last_seen = float(d_entry_live.get("last_seen") or 0.0)
                relay_lbl = d_entry_live.get("label") or d_entry_live.get("browser") or dispatched_id or target_label

            if cancelled_flag:
                raise _finish_dedup_err("DOM relay request cancelled by user")

            if cur_status_seq > sent_status_seq:
                sent_status_seq = cur_status_seq
                if emit_relay_status:
                    st_payload = {
                        "req_id": req_id,
                        "client_request_id": client_request_id,
                        "provider": prov,
                        "stage": cur_status_stage,
                        "has_images": bool(relay_images_payload),
                        "images_attached": cur_images_attached,
                    }
                    if cur_q_mono is not None and cur_d_mono is not None:
                        st_payload["relay_pickup_ms"] = max(0, int(round((cur_d_mono - cur_q_mono) * 1000.0)))
                    if cur_d_mono is not None and cur_s_mono is not None:
                        st_payload["relay_attach_ms"] = max(0, int(round((cur_s_mono - cur_d_mono) * 1000.0)))
                    if cur_upload_ms is not None:
                        st_payload["upload_ms"] = int(cur_upload_ms)
                    prog_events.append(("relay_status", st_payload))

            if cur_seq > sent_seq:
                sent_seq = cur_seq
                if prog_model:
                    with _CONFIG_LOCK:
                        LAST_UPSTREAM_MODEL["badge"] = prog_model
                        LAST_UPSTREAM_MODEL["provider"] = prov
                        LAST_UPSTREAM_MODEL["site"] = site_name
                if prog_think:
                    t_delta, streamed_thinking = _compute_thinking_delta(streamed_thinking, prog_think, provider=prov)
                    if t_delta:
                        prog_events.append(("thinking_delta", t_delta))
                if prog_resp or streamed_response:
                    prog_clean = _clean_streaming_gemini_text(prog_resp) if prog_resp else ""
                    if prog_clean != streamed_response:
                        if prog_clean and prog_clean.startswith(streamed_response):
                            c_delta = prog_clean[len(streamed_response):]
                            if c_delta:
                                prog_events.append(("content_delta", c_delta))
                            streamed_response = prog_clean
                        elif not prog_clean and streamed_response:
                            prog_events.append(("reset_content", ""))
                            streamed_response = ""
                        elif prog_clean and (
                            not streamed_response.startswith(prog_clean)
                            or len(prog_clean) >= int(len(streamed_response) * 0.85)
                        ):
                            prog_events.append(("reset_content", ""))
                            prog_events.append(("content_delta", prog_clean))
                            streamed_response = prog_clean
                if not emitted_generating and any(ek in ("thinking_delta", "content_delta") for ek, _ in prog_events):
                    emitted_generating = True
                    with _dom_relay_lock:
                        live_it = _dom_relay["pending"].get(req_id)
                        if live_it is not None and live_it.get("first_token_mono") is None:
                            live_it["first_token_mono"] = time.monotonic()
                            live_it["status_stage"] = "generating"
                    if emit_relay_status:
                        prog_events.insert(0, ("relay_status", {
                            "req_id": req_id,
                            "client_request_id": client_request_id,
                            "provider": prov,
                            "stage": "generating",
                            "has_images": bool(relay_images_payload),
                            "images_attached": cur_images_attached,
                        }))
            for ek, ep in prog_events:
                yield (ek, ep)

            if ev.is_set():
                break

            now = time.time()
            elapsed = now - start
            idle_elapsed = now - last_act
            max_total_cap = float(CONFIG.get("dom_relay_max_total_sec") or 0.0)
            if not dispatched and elapsed >= pickup_timeout:
                with _dom_relay_lock:
                    _dom_relay["pending"].pop(req_id, None)
                    t_entry = (_dom_relay.get("relays") or {}).get(target_relay_id or "") or {}
                    was_busy = (now - float(t_entry.get("last_busy_ts") or 0.0)) <= 10.0
                busy_note = " (extension reported busy with another active response)" if was_busy else ""
                raise _finish_dedup_err(
                    f"{prov_label} Web bridge uses the browser DOM relay ({target_label}), but no {prov_label} tab "
                    f"picked up the prompt in {int(pickup_timeout)}s{busy_note}. Open {site_url} in Chrome/Brave with the "
                    "'WSA — Web Browser Bridge' extension enabled and click Sync, then retry."
                )
            # Allow multiple alarm wake cycles when background scheduling is
            # delayed. A single missed 30-second wake does not mean tab closure.
            dead_cutoff = max(120.0, float(pickup_timeout))
            if (
                dispatched
                and relay_last_seen > 0.0
                and (now - relay_last_seen) > dead_cutoff
                and idle_elapsed > dead_cutoff
                and (now - dispatched_at) > dead_cutoff
            ):
                with _dom_relay_lock:
                    _dom_relay["pending"].pop(req_id, None)
                    _dom_relay.setdefault("cancelled_req_ids", {})[req_id] = now + 120.0
                raise _finish_dedup_err(
                    f"DOM relay disconnected: {relay_lbl} stopped heartbeating for >{int(dead_cutoff)}s "
                    f"waiting on {prov_label}."
                )
            # Claude send-stall: the tab accepted the prompt but the web composer
            # never submitted it (zero thinking/content progress) because the
            # account is capped and the send button is disabled. The user-visible
            # symptom is a message that simply cannot be sent. After
            # `relay_send_timeout_sec` (default 70s) of total silence treat it as a
            # provider limit so multiagent failover advances to the next agent.
            if (
                prov == "claude"
                and dispatched
                and cur_seq == 0
                and cur_s_mono is None
                and not is_generating_now
                and last_gen_ts <= 0.0
                and not streamed_thinking
                and not streamed_response
            ):
                _send_timeout = float(CONFIG.get("relay_send_timeout_sec") or 70.0)
                if (now - dispatched_at) >= _send_timeout:
                    with _dom_relay_lock:
                        _dom_relay["pending"].pop(req_id, None)
                    raise _finish_dedup_err(
                        f"Claude message could not be sent within {int(_send_timeout)}s "
                        "(composer did not submit; account limit likely reached)",
                        limit_hit=True,
                    )
            if idle_elapsed >= timeout or (max_total_cap > 0.0 and elapsed >= max_total_cap):
                with _dom_relay_lock:
                    _dom_relay["pending"].pop(req_id, None)
                raise _finish_dedup_err(
                    f"DOM relay timeout ({int(timeout)}s) waiting for the {prov_label} tab to finish answering"
                )
            if (now - last_keepalive_ts) >= step:
                last_keepalive_ts = now
                yield ("keepalive", "")
        with _dom_relay_lock:
            item = _dom_relay["pending"].pop(req_id, {})
            dispatched_id = item.get("dispatched_to") or target_relay_id
            d_entry = (_dom_relay.get("relays") or {}).get(dispatched_id or "")
            if not isinstance(d_entry, dict):
                d_entry = target_entry
            d_label = d_entry.get("label") or d_entry.get("browser") or dispatched_id or "browser-dom-relay"
            d_cfile = d_entry.get("cookie_file")
        if item.get("cancelled"):
            raise _finish_dedup_err("DOM relay request cancelled by user")
        if item.get("error"):
            raw_err = str(item["error"])
            raise _finish_dedup_err(
                f"DOM relay error: {raw_err}",
                limit_hit=is_provider_limit_error(raw_err),
                too_long=is_prompt_too_long_error(raw_err),
            )
        completed_cleanly = True
    finally:
        if not completed_cleanly:
            _fail_skey = _session_relay_key(session_id, prov)
            with _dom_relay_lock:
                _dom_relay["pending"].pop(req_id, None)
                if _fail_skey:
                    _PENDING_SESSION_META.pop(_fail_skey, None)
                _dom_relay.setdefault("cancelled_req_ids", {})[req_id] = time.time() + 120.0
                if dedup_entry is not None and dedup_entry.get("result") is None and not dedup_entry.get("error"):
                    dedup_entry["error"] = "DOM relay request cancelled"
                    dedup_entry["event"] = None
                ev.set()
    resp_text = item.get("response") or ""
    thinking_text = item.get("thinking") or ""
    if prov in ("claude", "chatgpt", "deepseek", "venice") and thinking_text:
        thinking_text = _clean_claude_thinking_text(thinking_text)
    model_badge = " ".join((item.get("model") or "").split()) or (f"{prov_label} Web")
    chat_url = (item.get("chat_url") or "").strip()

    with _dom_relay_lock:
        live_entry = (_dom_relay.get("relays") or {}).get(dispatched_id or "")
        if isinstance(live_entry, dict):
            if model_badge:
                live_entry["model_badge"] = model_badge
                live_entry["active_site"] = site_name
                live_entry["active_provider"] = prov
                prov_map = live_entry.setdefault("providers", {})
                p_rec = prov_map.setdefault(prov, {})
                if isinstance(p_rec, dict):
                    p_rec["has_tab"] = True
                    p_rec["model_badge"] = model_badge
                    if chat_url:
                        p_rec["url"] = chat_url

    argon_hit = prov == "gemini" and is_argon_badge(model_badge)
    with _CONFIG_LOCK:
        LAST_UPSTREAM_MODEL.update({
            "provider": prov,
            "site": site_name,
            "badge": model_badge,
            "hex": MODELS["gemini-4-argon"]["hex"] if argon_hit else None,
            "mode": 8 if argon_hit else spec["mode"],
            "mode_name": MODE_CATEGORY_NAMES[8] if argon_hit else spec["mode_name"],
            "slot80": 2 if argon_hit else None,
            "thinking_level": THINKING_LEVEL_NAMES[2] if argon_hit else "WEB_UI",
            "is_tier4_high": bool(argon_hit),
            "is_downgraded_flash_lite": bool(
                prov == "gemini" and any(k in model_badge.lower() for k in ("flash-lite", "flash lite", "auto", "gemini auto"))
            ),
            "thinking_chars": len(thinking_text),
            "updated_at": int(time.time()),
            "account_file": os.path.basename(d_cfile) if d_cfile else d_label,
            "relay_id": dispatched_id or None,
            "relay_label": d_label,
        })
    completed_mono = time.monotonic()
    q_mono = item.get("queued_mono")
    d_mono = item.get("dispatched_mono")
    s_mono = item.get("submitted_mono")
    ft_mono = item.get("first_token_mono")
    timings: dict = {
        "provider": prov,
        "has_images": bool(item.get("has_images")),
        "images_attached": item.get("images_attached"),
        "submit_confirmed": s_mono is not None,
    }
    if q_mono is not None:
        timings["round_trip_ms"] = max(0, int(round((completed_mono - q_mono) * 1000.0)))
    if q_mono is not None and d_mono is not None:
        timings["relay_pickup_ms"] = max(0, int(round((d_mono - q_mono) * 1000.0)))
    if d_mono is not None and s_mono is not None:
        timings["relay_attach_ms"] = max(0, int(round((s_mono - d_mono) * 1000.0)))
    if s_mono is not None:
        timings["response_ms"] = max(0, int(round((completed_mono - s_mono) * 1000.0)))
        if ft_mono is not None and ft_mono >= s_mono:
            timings["ttft_ms"] = max(0, int(round((ft_mono - s_mono) * 1000.0)))
    if item.get("browser_upload_ms") is not None:
        timings["upload_ms"] = int(item["browser_upload_ms"])
    if item.get("browser_submit_ms") is not None:
        timings["submit_ms"] = int(item["browser_submit_ms"])

    log(f"DOM relay [{req_id}] ({prov}, {mode_tag}, relay={d_label}): answered by '{model_badge}' in {time.time() - start:.1f}s ({len(resp_text)} chars)")
    if isinstance(ctx_meta, dict):
        if chat_url:
            ctx_meta["chat_url"] = chat_url
        if dispatched_id:
            ctx_meta["relay_id"] = dispatched_id
        ctx_meta["relay_timings"] = timings
    res_obj = {
        "response": resp_text,
        "thinking": thinking_text,
        "model": model_badge,
        "provider": prov,
        "chat_url": chat_url,
        "relay_id": dispatched_id,
        "timings": timings,
    }
    if dedup_entry is not None:
        with _dom_relay_lock:
            dedup_entry["result"] = dict(res_obj)
            dedup_entry["event"] = None
            dedup_entry["ts"] = time.time()
    yield ("result", res_obj)


def dom_relay_put_prompt(
    prompt: str,
    timeout: float = 300.0,
    pickup_timeout: float = 45.0,
    provider: str = "gemini",
) -> dict:
    """Blocking wrapper around dom_relay_iter; returns {'response', 'thinking', 'model', 'provider'}."""
    for kind, payload in dom_relay_iter(prompt, timeout=timeout, pickup_timeout=pickup_timeout, provider=provider):
        if kind == "result":
            return payload
    raise RuntimeError("DOM relay ended without a result")


def _prune_cancelled_req_ids_locked(now: Optional[float] = None) -> dict:
    """Drop expired cancelled relay request IDs while holding `_dom_relay_lock`."""
    cur = time.time() if now is None else float(now)
    cancelled = _dom_relay.setdefault("cancelled_req_ids", {})
    stale = [rid for rid, exp in list(cancelled.items()) if float(exp or 0.0) <= cur]
    for rid in stale:
        cancelled.pop(rid, None)
    return cancelled


def is_dom_relay_cancelled(req_id: str) -> bool:
    """Return True if `req_id` was explicitly cancelled."""
    rid = str(req_id or "").strip()
    if not rid:
        return False
    now = time.time()
    with _dom_relay_lock:
        cancelled = _prune_cancelled_req_ids_locked(now)
        if rid in cancelled:
            return True
        item = _dom_relay["pending"].get(rid)
        return bool(item and item.get("cancelled"))


def is_dom_relay_req_active(req_id: str) -> bool:
    """Return True if `req_id` is still active (pending/in-flight and uncancelled)."""
    rid = str(req_id or "").strip()
    if not rid:
        return False
    now = time.time()
    with _dom_relay_lock:
        cancelled = _prune_cancelled_req_ids_locked(now)
        if rid in cancelled:
            return False
        item = (_dom_relay.get("pending") or {}).get(rid)
        if not isinstance(item, dict):
            return False
        ev = item.get("event")
        if ev is not None and ev.is_set():
            return False
        return not bool(item.get("cancelled"))


def list_cancelled_req_ids() -> list[str]:
    """Return active (non-expired) cancelled DOM relay request IDs."""
    now = time.time()
    with _dom_relay_lock:
        cancelled = _prune_cancelled_req_ids_locked(now)
        return list(cancelled.keys())


def dom_relay_cancel_request(
    client_request_id: str = "",
    session_id: str = "",
    req_id: str = "",
    reason: str = "Cancelled by user",
) -> list[str]:
    """Cancel matching pending/in-flight DOM relay request(s) and wake waiters immediately."""
    target_rid = str(req_id or "").strip()
    target_crid = str(client_request_id or "").strip()
    target_sid = str(session_id or "").strip()
    if not (target_rid or target_crid or target_sid):
        return []
    now = time.time()
    cancelled_ids: list[str] = []
    with _dom_relay_lock:
        cancelled = _prune_cancelled_req_ids_locked(now)
        recent = _dom_relay.get("recent_requests") or {}
        for rid, item in list(_dom_relay["pending"].items()):
            matched = False
            if target_rid and rid == target_rid:
                matched = True
            elif target_crid and str(item.get("client_request_id") or "").strip() == target_crid:
                if not target_sid or str(item.get("session_id") or "").strip() == target_sid:
                    matched = True
            elif target_sid and not target_crid and not target_rid:
                if str(item.get("session_id") or "").strip() == target_sid:
                    matched = True
            if not matched:
                continue
            item["cancelled"] = True
            if item.get("error") is None:
                item["error"] = reason
            cancelled[rid] = now + 120.0
            cancelled_ids.append(rid)
            ev = item.get("event")
            if ev is not None:
                ev.set()
            crid = str(item.get("client_request_id") or "").strip()
            sid = str(item.get("session_id") or "").strip()
            prov = _normalize_provider(item.get("provider"))
            if crid:
                for dkey in (f"{sid}:{prov}:{crid}", f"{sid}:{crid}"):
                    dentry = recent.get(dkey)
                    if isinstance(dentry, dict) and dentry.get("result") is None:
                        dentry["error"] = reason
                        d_ev = dentry.get("event")
                        dentry["event"] = None
                        if d_ev is not None:
                            d_ev.set()
        if target_crid and not cancelled_ids:
            for dkey, dentry in list(recent.items()):
                if not isinstance(dentry, dict):
                    continue
                if dkey.endswith(f":{target_crid}") and (not target_sid or dkey.startswith(f"{target_sid}:")):
                    if dentry.get("result") is None:
                        dentry["error"] = reason
                        d_ev = dentry.get("event")
                        dentry["event"] = None
                        if d_ev is not None:
                            d_ev.set()
        if target_rid and target_rid not in cancelled_ids:
            cancelled[target_rid] = now + 120.0
            cancelled_ids.append(target_rid)
    return cancelled_ids


def dom_relay_get_pending(client_id: Optional[str] = None) -> Optional[dict]:
    """Return the oldest undispatched prompt permitted for `client_id` (single-account or explicit dual-lane)."""
    now = time.time()
    with _dom_relay_lock:
        active_id = _dom_relay.get("active_relay_id")
        is_primary = (not client_id) or (not active_id) or (client_id == active_id)
        if is_primary:
            _dom_relay["last_poll_ts"] = now

        # An overlapping idle poll cannot acquire a second job while this
        # worker still owns one, including during the orphan recovery grace.
        if client_id and any(
            item.get("dispatched_to") == client_id and item.get("dispatched")
            and item.get("response") is None and item.get("error") is None
            and not item.get("cancelled")
            and not (item.get("event") and item["event"].is_set())
            for item in _dom_relay["pending"].values()
        ):
            return None

        for req_id, item in _dom_relay["pending"].items():
            if item["response"] is not None or item["error"] is not None or item.get("dispatched") or item.get("cancelled"):
                continue
            target = item.get("target_relay_id") or ""
            is_explicit_lane = bool(item.get("explicit_relay_target"))
            if not is_primary:
                # Secondary browser relay ONLY receives items explicitly routed to it (dual-lane mode).
                if not (is_explicit_lane and client_id and target == client_id):
                    continue
            elif client_id and target and target != client_id:
                continue
            item["dispatched"] = True
            item["dispatched_to"] = client_id or target or ""
            item["dispatched_at"] = now
            if item.get("dispatched_mono") is None:
                item["dispatched_mono"] = time.monotonic()
            if item.get("status_stage") in (None, "", "queued"):
                item["status_stage"] = "dispatched"
                item["status_seq"] = int(item.get("status_seq") or 0) + 1
            item["idle_polls_since_dispatch"] = 0
            wev = item.get("wake_event")
            if wev is not None:
                wev.set()
            sess_id = item.get("session_id") or ""
            prov = _normalize_provider(item.get("provider"))
            s_key = _session_relay_key(sess_id, prov)
            sess_url = ""
            sess_email = ""
            if s_key and not item.get("new_chat"):
                _srec = (_dom_relay.get("sessions") or {}).get(s_key) or {}
                sess_url = _srec.get("chat_url") or ""
                sess_email = _srec.get("account_email") or ""
            return {
                "id": req_id,
                "provider": prov,
                "prompt": item["prompt"],
                "fallback_prompt": item.get("fallback_prompt") or item["prompt"],
                "images": list(item.get("images") or []),
                "session_id": sess_id,
                "chat_url": sess_url,
                "account_email": sess_email,
                "new_chat": bool(item.get("new_chat")),
                "client_id": item["dispatched_to"],
                "target_relay_id": item.get("target_relay_id") or "",
            }
    return None


def browser_cmd_get_pending(client_id: Optional[str] = None) -> Optional[dict]:
    """Pop the oldest undispatched live browser inspection/action command for the active relay."""
    now = time.time()
    with _dom_relay_lock:
        active_id = _dom_relay.get("active_relay_id")
        if client_id and active_id and client_id != active_id:
            return None
        cmds = _dom_relay.setdefault("browser_cmds", {})
        for cid, entry in list(cmds.items()):
            if (now - float(entry.get("created_at") or now)) > 60.0:
                ev = entry.get("event")
                cmds.pop(cid, None)
                if ev is not None:
                    entry["result"] = {"ok": False, "error": "Browser command expired"}
                    ev.set()
                continue
            if entry.get("dispatched") or entry.get("result") is not None:
                continue
            entry["dispatched"] = True
            return {
                "cmd_id": cid,
                "action": entry.get("action") or "list_tabs",
                "params": dict(entry.get("params") or {}),
            }
    return None


def browser_cmd_post_result(cmd_id: str, result: Optional[dict] = None) -> bool:
    """Deliver browser tab inspection/action results from the extension back to waiting WSA tool."""
    with _dom_relay_lock:
        cmds = _dom_relay.setdefault("browser_cmds", {})
        entry = cmds.get(cmd_id)
        if not entry:
            return False
        entry["result"] = result if isinstance(result, dict) else {"ok": False, "error": "Invalid browser result"}
        ev = entry.get("event")
    if ev is not None:
        ev.set()
    return True


def execute_browser_command(action: str, params: Optional[dict] = None, timeout: float = 25.0) -> dict:
    """Dispatch an inspection/screenshot/interaction command through the bound browser bridge."""
    if not is_dom_relay_active(max_age=20.0):
        return {
            "ok": False,
            "error": (
                "No active WSA Web Browser Bridge extension is connected (last poll > 20s ago). "
                "Open Brave/Chrome with Claude, DeepSeek, Gemini, ChatGPT, or Venice open, ensure WSA — Web Browser Bridge is enabled, and click Sync."
            ),
        }
    cmd_id = str(uuid.uuid4())[:10]
    ev = threading.Event()
    with _dom_relay_lock:
        cmds = _dom_relay.setdefault("browser_cmds", {})
        cmds[cmd_id] = {
            "cmd_id": cmd_id,
            "action": str(action or "list_tabs"),
            "params": dict(params or {}),
            "dispatched": False,
            "result": None,
            "event": ev,
            "created_at": time.time(),
        }
    done = ev.wait(timeout=max(2.0, float(timeout or 25.0)))
    with _dom_relay_lock:
        entry = (_dom_relay.get("browser_cmds") or {}).pop(cmd_id, None)
    if not done or not entry or entry.get("result") is None:
        return {
            "ok": False,
            "error": (
                f"Browser bridge timed out after {int(timeout)}s waiting for action '{action}'. "
                "Reload the unpacked WSA — Web Browser Bridge extension if you recently updated it."
            ),
        }
    return entry["result"]


def dom_relay_poll_for_client(
    client_info: Optional[dict],
    busy: bool = False,
    busy_req_id: str = "",
    generating: bool = False,
) -> tuple:
    """Register/refresh client relay state and return (pending_item_or_None, status_dict)."""
    reg = register_or_touch_relay(client_info)
    if busy:
        brid = str(busy_req_id or "").strip()
        busy_valid = is_dom_relay_req_active(brid) if brid else True
        reg["busy_req_valid"] = busy_valid
        cid_reg = reg.get("client_id") or ""
        if busy_valid and cid_reg:
            now_busy = time.time()
            with _dom_relay_lock:
                owned = (_dom_relay.get("pending") or {}).get(brid)
                if owned and owned.get("dispatched_to") == cid_reg:
                    owned["idle_polls_since_dispatch"] = 0
                    owned["idle_since_ts"] = None
                    owned["last_busy_ts"] = now_busy
                    if generating:
                        was_gen = bool(owned.get("is_generating"))
                        owned["is_generating"] = True
                        owned["last_generating_ts"] = now_busy
                        if not was_gen:
                            wev = owned.get("wake_event")
                            if wev is not None:
                                wev.set()
                    else:
                        owned["is_generating"] = False
                if cid_reg in (_dom_relay.get("relays") or {}):
                    _dom_relay["relays"][cid_reg]["last_busy_ts"] = now_busy
        elif not busy_valid:
            log(f"DOM relay: clearing stale busy job '{brid}' reported by {cid_reg or 'browser'}")
        return None, reg
    reg["busy_req_valid"] = True
    cid = reg.get("client_id") or None
    if cid:
        now = time.time()
        with _dom_relay_lock:
            for rid, pitem in list(_dom_relay["pending"].items()):
                if (
                    not pitem.get("dispatched")
                    or pitem.get("cancelled")
                    or pitem.get("response") is not None
                    or pitem.get("error") is not None
                ):
                    continue
                ev = pitem.get("event")
                if ev and ev.is_set():
                    continue
                if (pitem.get("dispatched_to") or "") != cid:
                    continue
                idle_count = int(pitem.get("idle_polls_since_dispatch") or 0) + 1
                pitem["idle_polls_since_dispatch"] = idle_count
                dispatched_at = float(pitem.get("dispatched_at") or now)
                last_prog = float(pitem.get("last_progress_ts") or 0.0)
                age_dispatch = now - dispatched_at
                if pitem.get("idle_since_ts") is None:
                    pitem["idle_since_ts"] = now
                # Tab timers and MV3 worker wakeups can be delayed. Require
                # sustained idle evidence across several 30-second alarm cycles,
                # not two overlapping requests or a sub-second startup window.
                last_alive = max(dispatched_at, last_prog, float(pitem.get("last_busy_ts") or 0))
                if (idle_count >= 2 and now - pitem["idle_since_ts"] >= 120.0
                        and now - last_alive >= 120.0):
                    prov_tag = _normalize_provider(pitem.get("provider"))
                    prov_name = (WEB_PROVIDER_SPECS.get(prov_tag) or {}).get("label") or prov_tag
                    pitem["error"] = (
                        f"{prov_name} browser relay ({cid}) returned idle before completing "
                        "the in-flight request (extension reloaded or tab disconnected)."
                    )
                    _dom_relay.setdefault("cancelled_req_ids", {})[rid] = now + 120.0
                    if ev:
                        ev.set()
                    wev = pitem.get("wake_event")
                    if wev:
                        wev.set()
                    log(
                        f"DOM relay [{rid}]: aborted orphaned dispatched job on idle poll from "
                        f"'{cid}' (idle_polls={idle_count}, age={age_dispatch:.2f}s)"
                    )
    item = dom_relay_get_pending(client_id=cid)
    bcmd = browser_cmd_get_pending(client_id=cid) if reg.get("is_active") else None
    if bcmd:
        if item is None:
            item = {"browser_cmd": bcmd}
        else:
            item["browser_cmd"] = bcmd
    return item, reg


def dom_relay_post_progress(
    req_id: str,
    response: str = "",
    thinking: str = "",
    model: str = "",
    chat_url: str = "",
    client_id: str = "",
    seq: int = 0,
    stage: str = "",
    images_attached: Optional[bool] = None,
    upload_ms: Optional[int] = None,
    submit_ms: Optional[int] = None,
) -> bool:
    """Publish incremental response/thinking snapshots and optional relay stage updates."""
    now = time.time()
    mono_now = time.monotonic()
    stage_norm = str(stage or "").strip().lower()
    stage_ranks = {"queued": 0, "dispatched": 1, "submitted": 2, "generating": 3}
    has_content_update = bool(response) or bool(thinking) or int(seq or 0) > 0
    with _dom_relay_lock:
        if req_id in _dom_relay.get("cancelled_req_ids", {}):
            return False
        item = _dom_relay["pending"].get(req_id)
        if not item or item["event"].is_set() or item.get("cancelled"):
            return False
        owner = item.get("dispatched_to") or ""
        if client_id and owner and client_id != owner:
            return False
        cur_seq = int(item.get("progress_seq") or 0)
        if seq and seq <= cur_seq and not stage_norm:
            return True
        responder_id = client_id or owner
        if responder_id and responder_id in (_dom_relay.get("relays") or {}):
            _dom_relay["relays"][responder_id]["last_seen"] = now
        if not responder_id or responder_id == _dom_relay.get("active_relay_id"):
            _dom_relay["last_poll_ts"] = now
        item["last_progress_ts"] = now
        item["idle_polls_since_dispatch"] = 0
        item["idle_since_ts"] = None
        if stage_norm in stage_ranks:
            cur_stage = str(item.get("status_stage") or "queued")
            if stage_ranks[stage_norm] >= stage_ranks.get(cur_stage, 0):
                item["status_stage"] = stage_norm
                if stage_norm == "submitted" and item.get("submitted_mono") is None:
                    item["submitted_mono"] = mono_now
                if isinstance(images_attached, bool):
                    item["images_attached"] = images_attached
                if isinstance(upload_ms, (int, float)) and upload_ms >= 0:
                    item["browser_upload_ms"] = int(round(upload_ms))
                if isinstance(submit_ms, (int, float)) and submit_ms >= 0:
                    item["browser_submit_ms"] = int(round(submit_ms))
                item["status_seq"] = int(item.get("status_seq") or 0) + 1
        if has_content_update and (not seq or seq > cur_seq):
            item["progress_seq"] = int(seq) if seq > 0 else (cur_seq + 1)
            if response is not None:
                resp_s = str(response)
                if resp_s or seq > 0:
                    item["progress_response"] = resp_s
            if thinking is not None:
                think_s = str(thinking)
                if think_s or seq > 0:
                    item["progress_thinking"] = think_s
        if model:
            item["progress_model"] = str(model)
        if chat_url:
            item["progress_chat_url"] = str(chat_url)
        wake = item.get("wake_event")
    if wake is not None:
        wake.set()
    return True


def dom_relay_post_response(
    req_id: str,
    response: str = "",
    error: str = "",
    thinking: str = "",
    model: str = "",
    chat_url: str = "",
    client_id: str = "",
) -> bool:
    """Complete a pending DOM relay request from the bound browser extension."""
    now = time.time()
    fingerprint = hashlib.sha256(json.dumps(
        [response, error, thinking, model, chat_url], ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    with _dom_relay_lock:
        receipts = _dom_relay.setdefault("completed_responses", {})
        for rid, receipt in list(receipts.items()):
            if now - receipt["at"] > 900.0:
                del receipts[rid]
        receipt = receipts.get(req_id)
        if receipt:
            return (receipt["fingerprint"] == fingerprint
                    and (not client_id or not receipt["owner"] or client_id == receipt["owner"]))
        item = _dom_relay["pending"].get(req_id)
        if not item:
            return False
        owner = item.get("dispatched_to") or ""
        if client_id and owner and client_id != owner:
            log(f"DOM relay [{req_id}]: ignoring response from unbound relay '{client_id}' (owner='{owner}')")
            return False
        if item.get("cancelled") or req_id in _dom_relay.get("cancelled_req_ids", {}):
            return False
        if item["event"].is_set():
            # A lost acknowledgement may cause delivery to repeat. Never
            # replace a settled result with a late, different payload.
            return ((item.get("error") or "") == error
                    and (item.get("response") or "") == response
                    and (item.get("thinking") or "") == thinking
                    and (item.get("model") or "") == model
                    and (item.get("chat_url") or "") == chat_url)
        responder_id = client_id or owner
        if responder_id and responder_id in (_dom_relay.get("relays") or {}):
            _dom_relay["relays"][responder_id]["last_seen"] = now
        if not responder_id or responder_id == _dom_relay.get("active_relay_id"):
            _dom_relay["last_poll_ts"] = now
        if error:
            item["error"] = error
        else:
            item["response"] = response
            if thinking:
                item["thinking"] = thinking
            if model:
                item["model"] = model
            if chat_url:
                item["chat_url"] = chat_url
        # Keep only bounded hashes, not another copy of response contents. A
        # dropped HTTP acknowledgement can then be retried after the waiter has
        # already removed its completed pending entry.
        receipts[req_id] = {"fingerprint": fingerprint, "owner": responder_id, "at": now}
        while len(receipts) > 256:
            del receipts[next(iter(receipts))]
        item["event"].set()
        wake = item.get("wake_event")
    if wake is not None:
        wake.set()
    log(f"DOM relay [{req_id}]: received response ({len(response)} chars, thinking={len(thinking)} chars)")
    return True


def receive_cookie_sync(data: dict) -> dict:
    """Persist live browser cookies, XSRF token, and multi-site relay metadata isolated per browser."""
    if not isinstance(data, dict):
        return {"ok": False, "error": "invalid payload"}
    cookie_str = (data.get("cookie") or "").strip()
    sapisid = (data.get("sapisid") or "").strip()
    xsrf_token = (data.get("xsrf_token") or "").strip()
    gemini_bl = (data.get("gemini_bl") or "").strip()

    cid = str(data.get("client_id") or "").strip()
    browser = str(data.get("browser") or "").strip()
    label = str(data.get("label") or browser or "").strip()
    auth_user = str(data.get("auth_user") if data.get("auth_user") is not None else "0")
    account_email = str(data.get("account_email") or data.get("email") or "").strip()
    model_badge = str(data.get("model_badge") or "").strip()
    active_site = str(data.get("active_site") or data.get("site") or "").strip()
    provider = str(data.get("provider") or "").strip().lower()
    providers = data.get("providers") if isinstance(data.get("providers"), dict) else None
    has_provider_sync = bool(data.get("sync_only") or provider or active_site or providers)

    if not cookie_str and not has_provider_sync:
        return {"ok": False, "error": "missing cookie string"}
    if cookie_str and not sapisid:
        pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
        sapisid = pairs.get("SAPISID", "") or pairs.get("__Secure-3PAPISID", "")

    is_named_relay = bool(cid or label)
    is_active_sender = True
    relay_target_path = None
    reg = {}
    if is_named_relay:
        relay_info = {
            "client_id": cid or _sanitize_relay_tag(label),
            "browser": browser or label or "chrome",
            "label": label or browser or cid,
            "auth_user": auth_user,
            "email": account_email,
            "model_badge": model_badge,
            "active_site": active_site,
            "provider": provider,
        }
        if providers is not None:
            relay_info["providers"] = providers
        reg = register_or_touch_relay(relay_info)
        is_active_sender = bool(reg.get("is_active", True))
        if cookie_str:
            relay_target_path = _relay_cookie_path(label or browser or cid)
            effective_cid = cid or _sanitize_relay_tag(label)
            with _dom_relay_lock:
                if effective_cid in (_dom_relay.get("relays") or {}):
                    _dom_relay["relays"][effective_cid]["cookie_file"] = relay_target_path

    if not cookie_str:
        return {
            "ok": True,
            "synced_cookies": False,
            "client_id": reg.get("client_id") or cid,
            "active_relay_id": reg.get("active_relay_id") or "",
            "is_active_relay": is_active_sender,
            "browser": browser or label or "chrome",
            "active_site": active_site,
            "account_email": account_email,
            "model_badge": model_badge,
        }

    with _CONFIG_LOCK:
        if relay_target_path:
            target_path = relay_target_path
        else:
            target_path = _resolve_cookie_file(default_write=True) or os.path.join(_cfg.SCRIPT_DIR, "wsa-auth.json")
        os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
        existing = {}
        if os.path.exists(target_path):
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    existing = loaded
            except Exception:
                pass
        existing["cookie"] = cookie_str
        if sapisid:
            existing["sapisid"] = sapisid
        if xsrf_token:
            existing["xsrf_token"] = xsrf_token
            if is_active_sender:
                CONFIG["xsrf_token"] = xsrf_token
        if gemini_bl and gemini_bl.startswith("boq_gemini-web-uiserver_"):
            existing["gemini_bl"] = gemini_bl
            if is_active_sender:
                CONFIG["gemini_bl"] = gemini_bl
        if cid:
            existing["client_id"] = cid
        if browser:
            existing["browser"] = _sanitize_relay_tag(browser)
        if label:
            existing["relay_label"] = label
        if account_email:
            existing["account_email"] = account_email
        existing["synced_at"] = int(time.time())
        try:
            with open(target_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
            if is_active_sender:
                CONFIG["cookie_file"] = target_path
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "synced_cookies": True,
        "cookie_file": os.path.basename(target_path),
        "client_id": reg.get("client_id") or cid,
        "is_active_relay": is_active_sender,
        "has_xsrf": bool(xsrf_token or existing.get("xsrf_token")),
        "gemini_bl": CONFIG.get("gemini_bl"),
    }


def receive_ps_relay(data: dict) -> dict:
    """Receive processSession URL + cookies from browser extension."""
    if not isinstance(data, dict):
        return {"ok": False}
    cid = str(data.get("client_id") or "").strip()
    allow_ps_state = True
    if cid:
        with _dom_relay_lock:
            active_id = _dom_relay.get("active_relay_id")
            if active_id and cid != active_id:
                allow_ps_state = False
    if allow_ps_state:
        with _dom_relay_lock:
            if data.get("session_url"):
                _ps_relay_state["session_url"] = data["session_url"]
            if data.get("cookies"):
                _ps_relay_state["cookies"] = data["cookies"]
            if data.get("auth_user") is not None:
                _ps_relay_state["auth_user"] = str(data["auth_user"])
            if data.get("bl"):
                _ps_relay_state["bl"] = data["bl"]
            if data.get("at"):
                _ps_relay_state["at"] = data["at"]
            _ps_relay_state["updated_at"] = time.time()
    if data.get("cookies"):
        receive_cookie_sync({
            "cookie": data["cookies"],
            "xsrf_token": data.get("at") or "",
            "gemini_bl": data.get("bl") or "",
            "client_id": cid,
            "browser": data.get("browser") or "",
            "label": data.get("label") or "",
            "auth_user": data.get("auth_user"),
            "account_email": data.get("account_email") or "",
        })
    return {"ok": True, "active_relay": allow_ps_state}


def get_ps_status() -> dict:
    """Return status of processSession and DOM relay."""
    with _dom_relay_lock:
        age = int(time.time() - _ps_relay_state["updated_at"]) if _ps_relay_state["updated_at"] else -1
        has_url = bool(_ps_relay_state["session_url"])
    return {
        "active": is_dom_relay_active() or (has_url and 0 <= age < 600),
        "dom_relay_active": is_dom_relay_active(),
        "age_seconds": age,
        "session_url": _ps_relay_state["session_url"][:80] if has_url else "",
    }
