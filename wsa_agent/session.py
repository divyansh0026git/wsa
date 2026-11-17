"""
Session state management: web bridge selection (claude | deepseek | gemini | chatgpt | venice),
multiagent failover queue state, project root, conversation history, file tracking,
and session persistence helpers.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set

from .context import FileContextLedger

try:
    from wsa_server.multimodal import is_claude_workflow_refusal as _is_workflow_refusal
except Exception:  # pragma: no cover - fallback when server package unavailable
    def _is_workflow_refusal(_text: Optional[str]) -> bool:
        return False


WEB_MODEL_IDS = ("claude", "deepseek", "gemini", "chatgpt", "venice")
DEFAULT_MULTIAGENT_QUEUE = ("claude", "deepseek", "gemini", "chatgpt", "venice")

PROVIDER_SITES = {
    "claude": "claude.ai",
    "deepseek": "chat.deepseek.com",
    "gemini": "gemini.google.com",
    "chatgpt": "chatgpt.com",
    "venice": "venice.ai",
}

MODEL_CATALOG = [
    {
        "id": "claude",
        "name": "Claude Web",
        "site": "claude.ai",
        "tier": "Web Bridge",
        "transport": "claude.ai",
        "thinking": "Web-controlled",
        "requires": "Open claude.ai tab",
        "features": "Bridges to open claude.ai tab (select Claude model & extended thinking on web)",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek Web",
        "site": "chat.deepseek.com",
        "tier": "Web Bridge",
        "transport": "chat.deepseek.com",
        "thinking": "Web-controlled (DeepThink)",
        "requires": "Open chat.deepseek.com tab",
        "features": "Bridges to open chat.deepseek.com tab (select DeepSeek-V3 or DeepThink R1 on web)",
    },
    {
        "id": "gemini",
        "name": "Gemini Web",
        "site": "gemini.google.com",
        "tier": "Web Bridge",
        "transport": "gemini.google.com",
        "thinking": "Web-controlled",
        "requires": "Open gemini.google.com tab",
        "features": "Bridges to open gemini.google.com tab (select any Gemini model on web)",
    },
    {
        "id": "chatgpt",
        "name": "ChatGPT Web",
        "site": "chatgpt.com",
        "tier": "Web Bridge",
        "transport": "chatgpt.com",
        "thinking": "Web-controlled (Think)",
        "requires": "Open chatgpt.com tab",
        "features": "Bridges to open chatgpt.com tab (select ChatGPT model & Think pill on web)",
    },
    {
        "id": "venice",
        "name": "Venice Web",
        "site": "venice.ai",
        "tier": "Web Bridge",
        "transport": "venice.ai",
        "thinking": "Web-controlled (Reasoning)",
        "requires": "Open venice.ai tab",
        "features": "Bridges to open venice.ai tab (DeepSeek V4.1 Flash Uncensored · 524K ctx)",
    },
]

ALL_MODELS = [
    (entry["id"], f"{entry['name']} ({entry['site']}) — {entry['features']}")
    for entry in MODEL_CATALOG
]

_MODEL_ALIAS_IDS = {
    "claude": "claude",
    "claude-web": "claude",
    "claude.ai": "claude",
    "anthropic": "claude",
    "deepseek": "deepseek",
    "deepseek-web": "deepseek",
    "chat.deepseek.com": "deepseek",
    "deepseek-r1": "deepseek",
    "deepseek-v3": "deepseek",
    "deepthink": "deepseek",
    "r1": "deepseek",
    "gemini": "gemini",
    "gemini-web": "gemini",
    "gemini.google.com": "gemini",
    "google": "gemini",
    "chatgpt": "chatgpt",
    "chatgpt-web": "chatgpt",
    "chatgpt.com": "chatgpt",
    "chat.openai.com": "chatgpt",
    "openai": "chatgpt",
    "gpt": "chatgpt",
    "venice": "venice",
    "venice-web": "venice",
    "veniceai": "venice",
    "venice.ai": "venice",
    "vernice": "venice",
    "vernice-web": "venice",
    "verniceai": "venice",
    "vernice.ai": "venice",
    "deepseek-v4.1-flash": "venice",
    "deepseek-v4-1-flash-uncensored": "venice",
    "deepseek-v4.1-flash-uncensored": "venice",
}


def normalize_web_model(model: str) -> Optional[str]:
    """Normalize a user-supplied model choice to 'claude', 'deepseek', 'gemini', 'chatgpt', or 'venice' (else None)."""
    raw = (model or "").strip().lower()
    if raw in _MODEL_ALIAS_IDS:
        return _MODEL_ALIAS_IDS[raw]
    if raw.startswith("claude") and " " not in raw:
        return "claude"
    if (raw.startswith(("venice", "vernice")) or "v4.1-flash" in raw or "v4-1-flash" in raw) and " " not in raw:
        return "venice"
    if raw.startswith("deepseek") and " " not in raw:
        return "deepseek"
    if (raw.startswith("chatgpt") or raw.startswith("gpt-")) and " " not in raw:
        return "chatgpt"
    if raw in ("gemini", "gemini-web"):
        return "gemini"
    return None


def infer_provider_from_site(site_or_url: str) -> str:
    """Infer canonical provider ID from a domain name or full tab/chat URL."""
    low = str(site_or_url or "").lower()
    if "claude.ai" in low:
        return "claude"
    if "venice.ai" in low or "vernice.ai" in low:
        return "venice"
    if "chat.deepseek.com" in low or "deepseek.com" in low:
        return "deepseek"
    if "chatgpt.com" in low or "chat.openai.com" in low:
        return "chatgpt"
    if "gemini.google" in low:
        return "gemini"
    return ""


def is_provider_thread_url(url: str) -> bool:
    """Return True if URL points to a saved thread/conversation on any supported web provider."""
    s = str(url or "").strip()
    if not s:
        return False
    low = s.lower()
    if "venice.ai" in low or "vernice.ai" in low:
        return bool(
            re.search(r"/chat/(?:agent|classic)/[A-Za-z0-9_-]+|/chat/(?!agent(?:[/?#]|$)|classic(?:[/?#]|$))[A-Za-z0-9_-]+", s, re.I)
        )
    return (
        "/app/" in s
        or "/chat/" in s
        or "/a/chat/s/" in s
        or "/c/" in s
    )


def _infer_browser_title(state) -> str:
    """Return capitalized browser name ('Brave', 'Chrome', 'Edge') when a DOM relay is active."""
    if state is None:
        return ""
    browser = str(getattr(state, "active_browser", "") or "").strip().lower()
    acct = str(getattr(state, "active_account_label", "") or "").strip().lower()
    hint = browser or acct
    if "brave" in hint:
        return "Brave"
    if "edge" in hint:
        return "Edge"
    if "chrome" in hint:
        return "Chrome"
    if "arc" in hint:
        return "Arc"
    if browser:
        return browser.capitalize()
    return ""


def get_model_info(model_id: str, state=None) -> Dict[str, str]:
    """Return dynamic metadata for the active web bridge based on synced browser state."""
    canonical = normalize_web_model(model_id) or "gemini"
    base_entry: Optional[Dict[str, str]] = None
    for entry in MODEL_CATALOG:
        if entry["id"] == canonical:
            base_entry = dict(entry)
            break
    if base_entry is None:
        base_entry = dict(MODEL_CATALOG[2] if len(MODEL_CATALOG) > 2 else MODEL_CATALOG[0])

    browser_title = _infer_browser_title(state)
    providers = getattr(state, "providers_info", None) or {}
    p_info = providers.get(canonical) if isinstance(providers, dict) else None
    p_info = p_info if isinstance(p_info, dict) else {}

    p_email = str(p_info.get("email") or "").strip()
    p_badge = str(p_info.get("model_badge") or "").strip()
    p_has_tab = bool(p_info.get("has_tab"))

    acct_email = p_email or (
        str(getattr(state, "active_account_email", "") or "").strip()
        if state is not None and (getattr(state, "model", "") == canonical or not providers)
        else ""
    )
    web_badge = p_badge or (
        str(getattr(state, "active_web_model", "") or "").strip()
        if state is not None and getattr(state, "model", "") == canonical and (not providers or canonical not in providers)
        else ""
    )

    if browser_title:
        base_entry["transport"] = f"{browser_title} Web · {base_entry['site']}"
        if p_has_tab and acct_email:
            base_entry["requires"] = f"{browser_title} · {acct_email}"
        elif p_has_tab:
            base_entry["requires"] = f"{browser_title} · {base_entry['site']} open"
        elif providers:
            base_entry["requires"] = f"{browser_title} · open {base_entry['site']}"
        else:
            base_entry["requires"] = f"{browser_title} Web"
    if web_badge:
        base_entry["thinking"] = f"Web: {web_badge}"
    return base_entry


# ─── Project autodetection ────────────────────────────────────────────────────
PROJECT_MARKERS = {
    "package.json":  "Node.js / JavaScript",
    "pyproject.toml": "Python (pyproject)",
    "setup.py":      "Python",
    "Cargo.toml":    "Rust",
    "go.mod":        "Go",
    "pom.xml":       "Java (Maven)",
    "build.gradle":  "Java/Kotlin (Gradle)",
    "composer.json": "PHP",
    "Gemfile":       "Ruby",
    "mix.exs":       "Elixir",
    ".git":          "Git repo",
}


def detect_project_type(root: Path) -> str:
    for marker, label in PROJECT_MARKERS.items():
        if (root / marker).exists():
            return label
    return "Unknown"


@dataclass
class SessionState:
    project_root: Path = field(default_factory=Path.cwd)
    selected_model: str = "gemini"   # "claude" | "gemini"
    messages: List[Dict] = field(default_factory=list)
    context_summary: str = ""
    free_mode: bool = False
    tool_history: List[Dict] = field(default_factory=list)
    turn_count: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    created_at: float = field(default_factory=time.time)
    api_base: str = "http://127.0.0.1:8081/v1"
    api_key: str = "sk-wsa"
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    permission_mode: str = "smart"     # smart, strict, or explicit auto bypass
    auto_approve: bool = False
    approval_callback: Optional[Callable[[dict], bool]] = field(default=None, repr=False, compare=False)
    # Claude Code-style file tracking
    written_files: Set[str] = field(default_factory=set)
    verified_files: Set[str] = field(default_factory=set)
    active_account_label: str = ""
    active_account_email: str = ""
    active_browser: str = ""
    active_site: str = ""
    active_web_model: str = ""
    providers_info: Dict[str, dict] = field(default_factory=dict)
    chat_urls: Dict[str, str] = field(default_factory=dict)
    # Phase 3: structured context transfer, plan state, and scoped subagent role threads
    file_ledger: FileContextLedger = field(default_factory=FileContextLedger)
    decisions: List[str] = field(default_factory=list)
    plan_items: List[Dict] = field(default_factory=list)
    turn_digest: List[Dict] = field(default_factory=list)  # compact structured history for cross-turn context
    team_mode: str = "auto"           # auto, on, or off
    thread_mode: str = "single"       # single (one continuous project chat) or roles (split per role)
    dual_lane: bool = True            # allow dual-relay lane split when 2 healthy relays are connected
    role_threads: Dict[str, str] = field(default_factory=dict)
    agent_mode: str = "singleagent"   # "singleagent" (default) | "multiagent" (ordered failover queue)
    multiagent_queue: List[str] = field(default_factory=list)
    multiagent_index: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.project_root, Path):
            self.project_root = Path(str(self.project_root or ".")).resolve()

    def append_tool_history(
        self,
        entry: Dict,
        *,
        max_items: int = 120,
        max_arg_chars: int = 300,
    ) -> None:
        """Record a compact tool-history item and bound in-memory length."""
        clean_entry = dict(entry)
        raw_args = clean_entry.get("args")
        if isinstance(raw_args, dict):
            compact_args: Dict = {}
            for k, v in list(raw_args.items())[:12]:
                if isinstance(v, str) and len(v) > max_arg_chars:
                    compact_args[k] = f"{v[:max_arg_chars]}… ({len(v)} chars)"
                elif isinstance(v, list) and len(v) > 8:
                    compact_args[k] = v[:8] + [f"… (+{len(v) - 8} more)"]
                else:
                    compact_args[k] = v
            clean_entry["args"] = compact_args
        self.tool_history.append(clean_entry)
        if len(self.tool_history) > max_items:
            del self.tool_history[: len(self.tool_history) - max_items]

    # ── Computed properties ───────────────────────────────────────────────────
    @property
    def model(self) -> str:
        return self.selected_model if self.selected_model in WEB_MODEL_IDS else "gemini"

    @property
    def active_site_name(self) -> str:
        return PROVIDER_SITES.get(self.model, "gemini.google.com")

    @property
    def active_chat_url(self) -> str:
        return str(self.chat_urls.get(self.model, "") or "")

    @active_chat_url.setter
    def active_chat_url(self, value: str) -> None:
        val = str(value or "").strip()
        if val:
            prov = infer_provider_from_site(val) or self.model
            self.chat_urls[prov] = val
        else:
            self.chat_urls.pop(self.model, None)

    @property
    def project_name(self) -> str:
        return self.project_root.name

    @property
    def project_type(self) -> str:
        return detect_project_type(self.project_root)

    @property
    def session_duration(self) -> str:
        secs = int(time.time() - self.created_at)
        if secs < 60:
            return f"{secs}s"
        return f"{secs//60}m{secs%60}s"

    # ── Helpers ───────────────────────────────────────────────────────────────
    def set_model(self, model: str) -> bool:
        """Select web bridge ('claude', 'deepseek', 'gemini', 'chatgpt', or 'venice'). Returns True if valid."""
        canonical = normalize_web_model(model)
        if canonical is None:
            return False
        prev_model = self.selected_model
        self.selected_model = canonical
        self.active_site = PROVIDER_SITES.get(canonical, "gemini.google.com")
        p_info = self.providers_info.get(canonical) if isinstance(self.providers_info, dict) else None
        if isinstance(p_info, dict):
            self.active_web_model = str(p_info.get("model_badge") or "").strip()
            if p_info.get("email"):
                self.active_account_email = str(p_info["email"]).strip()
        elif self.providers_info or (prev_model and prev_model != canonical):
            self.active_web_model = ""
        return True

    def configure_singleagent(self, model: Optional[str] = None) -> str:
        """Switch to single-agent mode (optional target web provider) and clear multiagent queue."""
        self.agent_mode = "singleagent"
        self.multiagent_index = 0
        if model:
            self.set_model(model)
        return self.model

    def configure_multiagent(self, queue: Optional[List[str]] = None) -> List[str]:
        """Configure ordered multiagent failover queue and select its first provider."""
        ordered: List[str] = []
        for item in (queue if queue is not None else list(DEFAULT_MULTIAGENT_QUEUE)):
            canonical = normalize_web_model(item)
            if canonical and canonical not in ordered:
                ordered.append(canonical)
        if not ordered:
            ordered = list(DEFAULT_MULTIAGENT_QUEUE)
        self.multiagent_queue = ordered
        self.multiagent_index = 0
        self.agent_mode = "multiagent"
        self.set_model(ordered[0])
        return list(ordered)

    def advance_multiagent_queue(self, failed_model: Optional[str] = None) -> Optional[str]:
        """Advance to the next provider in the multiagent failover queue, or return None if exhausted."""
        import sys
        import threading

        lock = sys.modules[__name__].__dict__.setdefault("_MULTIAGENT_LOCK", threading.RLock())
        with lock:
            if self.agent_mode != "multiagent" or not self.multiagent_queue:
                return None
            cur = normalize_web_model(failed_model or self.model) or self.model
            if cur in self.multiagent_queue:
                cur_idx = self.multiagent_queue.index(cur)
                active_idx = int(self.multiagent_index)
                active_norm = normalize_web_model(self.model) or self.model
                if (
                    active_idx > cur_idx
                    and active_idx < len(self.multiagent_queue)
                    and active_norm == self.multiagent_queue[active_idx]
                ):
                    return self.model
                next_idx = cur_idx + 1
            else:
                next_idx = int(self.multiagent_index) + 1
            if next_idx >= len(self.multiagent_queue):
                return None
            self.multiagent_index = next_idx
            next_model = self.multiagent_queue[next_idx]
            self.set_model(next_model)
            return next_model

    def missing_queued_sites(
        self,
        providers_map: Optional[dict] = None,
        queue: Optional[List[str]] = None,
    ) -> List[str]:
        """Return queued provider IDs that do not currently report an open synced tab."""
        pmap = providers_map if isinstance(providers_map, dict) else (self.providers_info or {})
        target_queue = queue if queue is not None else (self.multiagent_queue or ([self.model] if self.model else []))
        missing: List[str] = []
        for pid in target_queue:
            info = pmap.get(pid) if isinstance(pmap, dict) else None
            if not (isinstance(info, dict) and info.get("has_tab")):
                missing.append(pid)
        return missing

    def clear_provider_chats(
        self,
        providers: Optional[Sequence[str]] = None,
        *,
        clear_roles: bool = True,
    ) -> int:
        """Invalidate saved browser thread URLs for *providers* (or all providers when None)."""
        removed = 0
        if not providers:
            removed = len(self.chat_urls) + (len(self.role_threads) if clear_roles else 0)
            self.chat_urls.clear()
            if clear_roles:
                self.role_threads.clear()
            return removed
        targets = {
            normalize_web_model(str(p)) or str(p).strip().lower()
            for p in providers
            if str(p or "").strip()
        }
        for pk in targets:
            if self.chat_urls.pop(pk, None):
                removed += 1
            for k in [key for key in list(self.chat_urls.keys()) if key.startswith(f"{pk}:role:")]:
                if self.chat_urls.pop(k, None):
                    removed += 1
        if clear_roles and self.role_threads:
            for role_key, rurl in list(self.role_threads.items()):
                r_prov = infer_provider_from_site(str(rurl or "")) if is_provider_thread_url(str(rurl or "")) else ""
                if r_prov in targets or not r_prov:
                    self.role_threads.pop(role_key, None)
                    removed += 1
        return removed

    def sync_account_from_health(self, health: Optional[dict]) -> None:
        """Update dynamic bound account/browser/site/model metadata from /v1/health response."""
        if not isinstance(health, dict) or not health:
            return
        prev_label = str(self.active_account_label or "").strip()
        prev_browser = str(self.active_browser or "").strip()
        prev_email = str(self.active_account_email or "").strip()
        prev_providers = {
            k: dict(v)
            for k, v in (self.providers_info or {}).items()
            if isinstance(v, dict)
        }

        active_relay = str(health.get("active_relay") or "").strip()
        active_account = str(health.get("active_account") or "").strip()
        active_browser = str(health.get("active_browser") or "").strip()
        active_email = str(health.get("active_email") or "").strip()
        active_relay_id = str(health.get("active_relay_id") or "").strip()
        active_site = str(health.get("active_site") or "").strip()
        active_badge = str(health.get("active_model_badge") or "").strip()
        active_prov = str(health.get("active_provider") or "").strip().lower()
        providers = health.get("active_providers")

        for rel in (health.get("relays") or []):
            if not isinstance(rel, dict):
                continue
            if rel.get("active") or (active_relay_id and rel.get("client_id") == active_relay_id):
                active_relay = str(rel.get("label") or active_relay).strip()
                active_browser = str(rel.get("browser") or active_browser).strip()
                active_email = str(rel.get("account_email") or active_email).strip()
                active_site = str(rel.get("active_site") or active_site).strip()
                active_badge = str(rel.get("model_badge") or active_badge).strip()
                active_prov = str(rel.get("active_provider") or active_prov).strip().lower()
                if isinstance(rel.get("providers"), dict):
                    providers = rel["providers"]
                break

        if not active_prov and active_site:
            active_prov = infer_provider_from_site(active_site)

        new_label = active_relay or active_account
        # Relay is considered "switched" only when the browser itself changed OR
        # the account email changed — not when the relay label drifts (e.g. label
        # normalisation differences between turns).  A pure label change while
        # the email stays the same does NOT warrant clearing saved chat threads.
        relay_switched = bool(
            (prev_browser and active_browser and prev_browser.lower() != active_browser.lower())
            or (prev_email and active_email and prev_email.lower() != active_email.lower()
                and prev_label and new_label and prev_label.lower() != new_label.lower())
        )
        changed_providers: List[str] = []
        if not relay_switched and isinstance(providers, dict):
            for pk, new_p in providers.items():
                if not isinstance(new_p, dict):
                    continue
                old_p = prev_providers.get(pk) or {}
                old_e = str(old_p.get("email") or old_p.get("account_email") or "").strip().lower()
                new_e = str(new_p.get("email") or new_p.get("account_email") or "").strip().lower()
                email_diff = bool(old_e and new_e and old_e != new_e)
                auth_diff = False
                if str(pk).strip().lower() == "gemini":
                    old_a = str(old_p.get("auth_user") or "").strip()
                    new_a = str(new_p.get("auth_user") or "").strip()
                    auth_diff = bool(old_a and new_a and old_a != new_a)
                if email_diff or auth_diff:
                    changed_providers.append(str(pk).strip().lower())

        if new_label:
            self.active_account_label = new_label
        if active_browser:
            self.active_browser = active_browser
        if isinstance(providers, dict):
            self.providers_info = providers
        cur_p = self.providers_info.get(self.model) if isinstance(self.providers_info, dict) else None
        if isinstance(cur_p, dict):
            if cur_p.get("email"):
                active_email = str(cur_p["email"])
            cur_badge = str(cur_p.get("model_badge") or "").strip()
            self.active_web_model = cur_badge or (active_badge if active_prov == self.model else "")
        elif active_badge and (not active_prov or active_prov == self.model):
            self.active_web_model = active_badge
        elif active_prov and active_prov != self.model:
            self.active_web_model = ""
        if active_email:
            if prev_email and active_email.lower() != prev_email.lower():
                target_p = active_prov or self.model
                if target_p and target_p not in changed_providers:
                    changed_providers.append(target_p)
            self.active_account_email = active_email
        if active_site:
            self.active_site = active_site

        if relay_switched:
            self.clear_provider_chats(clear_roles=True)
        elif changed_providers:
            self.clear_provider_chats(changed_providers, clear_roles=True)

    def sync_account_from_res(self, res: Optional[dict]) -> None:
        """Update dynamic bound account/browser metadata from switch/bind/upstream dict."""
        if not isinstance(res, dict) or not res:
            return
        lbl = str(res.get("active_relay_label") or res.get("active_relay") or res.get("relay_label") or res.get("active_account") or res.get("account_file") or "").strip()
        browser = str(res.get("browser") or "").strip()
        email = str(res.get("account_email") or "").strip()
        chat_url = str(res.get("chat_url") or "").strip()
        site = str(res.get("active_site") or res.get("site") or "").strip()
        badge = str(res.get("badge") or res.get("model_badge") or "").strip()
        res_prov = str(res.get("provider") or res.get("active_provider") or "").strip().lower()
        if not res_prov and site:
            res_prov = infer_provider_from_site(site)
        if not res_prov and chat_url:
            res_prov = infer_provider_from_site(chat_url)
        providers = res.get("providers")
        prev_lbl = self.active_account_label
        prev_browser = self.active_browser
        prev_email = self.active_account_email
        prev_providers = {
            k: dict(v)
            for k, v in (self.providers_info or {}).items()
            if isinstance(v, dict)
        }
        relay_switched = bool(
            (prev_browser and browser and prev_browser.lower() != browser.lower())
            or (prev_lbl and lbl and prev_lbl.lower() != lbl.lower())
        )
        changed_providers: List[str] = []
        if not relay_switched and isinstance(providers, dict):
            for pk, new_p in providers.items():
                if not isinstance(new_p, dict):
                    continue
                old_p = prev_providers.get(pk) or {}
                old_e = str(old_p.get("email") or old_p.get("account_email") or "").strip().lower()
                new_e = str(new_p.get("email") or new_p.get("account_email") or "").strip().lower()
                email_diff = bool(old_e and new_e and old_e != new_e)
                auth_diff = False
                if str(pk).strip().lower() == "gemini":
                    old_a = str(old_p.get("auth_user") or "").strip()
                    new_a = str(new_p.get("auth_user") or "").strip()
                    auth_diff = bool(old_a and new_a and old_a != new_a)
                if email_diff or auth_diff:
                    changed_providers.append(str(pk).strip().lower())
        if prev_email and email and prev_email.lower() != email.lower():
            t_prov = res_prov or self.model
            if t_prov and t_prov not in changed_providers:
                changed_providers.append(t_prov)

        if lbl:
            self.active_account_label = lbl
        if browser:
            self.active_browser = browser
        if isinstance(providers, dict):
            self.providers_info = providers
        if email:
            self.active_account_email = email
        elif lbl and lbl != prev_lbl:
            self.active_account_email = ""
        if site:
            self.active_site = site
        cur_p = self.providers_info.get(self.model) if isinstance(self.providers_info, dict) else None
        if isinstance(cur_p, dict):
            self.active_web_model = str(cur_p.get("model_badge") or "").strip() or (
                badge if res_prov == self.model else ""
            )
        elif badge and (not res_prov or res_prov == self.model):
            self.active_web_model = badge
        elif res_prov and res_prov != self.model:
            self.active_web_model = ""

        if relay_switched or int(res.get("cleared_chats") or 0) > 0:
            self.clear_provider_chats(clear_roles=True)
        elif changed_providers:
            self.clear_provider_chats(changed_providers, clear_roles=True)

        # Only latch onto a browser thread URL on active turns or non-empty sessions;
        # binding `/account` on a brand-new empty session must not hijack an unrelated open tab.
        is_turn_res = bool(res.get("id") or res.get("usage") or ("thinking_chars" in res))
        has_turn_context = bool(
            self.messages
            or self.turn_count > 0
            or is_turn_res
        )
        if (
            has_turn_context
            and (is_turn_res or (not relay_switched and not changed_providers))
            and not int(res.get("cleared_chats") or 0)
            and chat_url
            and is_provider_thread_url(chat_url)
        ):
            self.active_chat_url = chat_url
            target_p = res_prov or self.model
            if target_p and isinstance(self.chat_urls, dict):
                self.chat_urls[target_p] = chat_url

    @property
    def account_display(self) -> str:
        """Concise human-readable active single account label for TUI banner & toolbar."""
        lbl = (self.active_account_label or "").strip()
        email = (self.active_account_email or "").strip()
        if lbl and email:
            return f"{lbl} · {email}"
        return lbl or email or ""

    def _reset_workspace_thread_state(self, *, reset_tokens: bool = True) -> None:
        """Clear conversation, file ledger, plans, and web-thread state cleanly."""
        self.messages.clear()
        self.context_summary = ""
        self.tool_history.clear()
        self.turn_count = 0
        if reset_tokens:
            self.total_tokens_in = 0
            self.total_tokens_out = 0
        self.written_files.clear()
        self.verified_files.clear()
        self.chat_urls.clear()
        self.file_ledger.clear()
        self.decisions.clear()
        self.plan_items.clear()
        self.turn_digest.clear()
        self.role_threads.clear()
        self.multiagent_index = 0
        if self.agent_mode == "multiagent" and self.multiagent_queue:
            self.set_model(self.multiagent_queue[0])
        self.session_id = uuid.uuid4().hex[:12]
        self.__dict__.pop("_workspace_snapshot", None)
        self.__dict__.pop("_test_check_done", None)
        self.__dict__.pop("_tests_exist", None)

    def set_project(self, path: str) -> bool:
        p = Path(path).expanduser().resolve()
        if p.is_dir():
            self.project_root = p
            self._reset_workspace_thread_state(reset_tokens=True)
            return True
        return False

    def clear_history(self) -> None:
        self._reset_workspace_thread_state(reset_tokens=True)

    def role_session_id(self, role: str = "main") -> str:
        """Return persistent web-thread session key for a subagent role (A1 policy)."""
        r = str(role or "main").strip().lower()
        if r in ("", "main", "coordinator"):
            return self.session_id
        return f"{self.session_id}:role:{r}"

    def get_role_chat_url(self, role: str, model: Optional[str] = None) -> str:
        """Return persisted thread URL for *role* scoped to *model*, ignoring non-URL IDs."""
        r = str(role or "main").strip().lower()
        if r in ("", "main", "coordinator"):
            return self.active_chat_url
        prov = normalize_web_model(model or self.model) or self.model
        scoped = str((self.chat_urls or {}).get(f"{prov}:role:{r}") or "").strip()
        if scoped and is_provider_thread_url(scoped) and (infer_provider_from_site(scoped) or prov) == prov:
            return scoped
        legacy = str((self.role_threads or {}).get(r) or "").strip()
        if legacy and is_provider_thread_url(legacy) and (infer_provider_from_site(legacy) or prov) == prov:
            return legacy
        return ""

    def set_role_chat_url(self, role: str, url: str, model: Optional[str] = None) -> None:
        """Persist thread URL for *role* scoped cleanly to its web provider."""
        r = str(role or "main").strip().lower()
        if r in ("", "main", "coordinator"):
            self.active_chat_url = url
            return
        val = str(url or "").strip()
        prov = normalize_web_model(model or self.model) or self.model
        if val and is_provider_thread_url(val):
            prov = infer_provider_from_site(val) or prov
            self.chat_urls[f"{prov}:role:{r}"] = val
            self.role_threads[r] = val
        elif not val:
            self.clear_role_chat_url(r, model=prov)

    def clear_role_chat_url(self, role: str, model: Optional[str] = None) -> None:
        """Clear saved role thread URL for *model* (and matching legacy slot)."""
        r = str(role or "main").strip().lower()
        if r in ("", "main", "coordinator"):
            return
        prov = normalize_web_model(model or self.model) or self.model
        self.chat_urls.pop(f"{prov}:role:{r}", None)
        legacy = str((self.role_threads or {}).get(r) or "").strip()
        if legacy and ((not is_provider_thread_url(legacy)) or (infer_provider_from_site(legacy) or prov) == prov):
            self.role_threads.pop(r, None)

    def resolve_stage_session_id(self, role: str = "main", *, lane_suffix: str = "") -> str:
        """Resolve chat session key per `thread_mode` ('single' unified chat vs 'roles' split chats)."""
        r = str(role or "main").strip().lower()
        if lane_suffix:
            return f"{self.role_session_id(r)}:{lane_suffix}"
        if str(getattr(self, "thread_mode", "single") or "single").strip().lower() == "roles":
            return self.role_session_id(r)
        return self.session_id

    def compact_history(self, keep: int = 14) -> int:
        """Summarize evicted messages without splitting assistant/tool pairs."""
        if len(self.messages) <= keep:
            return 0
        split_idx = len(self.messages) - keep
        while split_idx < len(self.messages) and self.messages[split_idx].get("role") == "tool":
            split_idx += 1
        if split_idx >= len(self.messages):
            # The trailing portion is one uninterrupted tool-call round (e.g. a large
            # batched tool call). Advancing past it would evict every message -
            # including the in-flight round - and leave self.messages empty. Fall
            # back to the start of that round so it survives compaction intact.
            run_start = len(self.messages) - 1
            while run_start > 0 and self.messages[run_start].get("role") == "tool":
                run_start -= 1
            split_idx = run_start
        if split_idx <= 0:
            return 0
        old_msgs = self.messages[:split_idx]
        removed = len(old_msgs)
        tool_rounds = sum(1 for m in old_msgs if m.get("role") == "assistant" and m.get("tool_calls"))
        prior_goals = []
        prior_outcomes = []
        for line in self.context_summary.splitlines():
            for prefix in (
                "Earlier User Goals: ",
                "Completed Earlier Topics (Already Answered — Reference Only): ",
            ):
                if line.startswith(prefix):
                    prior_goals.extend(line.replace(prefix, "", 1).split(" | "))
                    break
            else:
                if line.startswith("Key Prior Findings: "):
                    prior_outcomes.extend(line.replace("Key Prior Findings: ", "", 1).split(" | "))

        def _text_of(val) -> str:
            if isinstance(val, list):
                bits = []
                for part in val:
                    if isinstance(part, dict):
                        if part.get("type") == "text" and part.get("text"):
                            bits.append(str(part["text"]))
                        elif part.get("type") in ("image_url", "input_image", "image"):
                            bits.append("[Screenshot attached]")
                return " ".join(bits)
            return str(val or "")

        goals = prior_goals + [
            _text_of(m.get("content", "")).strip().splitlines()[0][:160]
            for m in old_msgs
            if m.get("role") == "user"
            and _text_of(m.get("content", "")).strip()
            and not _text_of(m.get("content", "")).startswith(("[Continue]:", "[System Verification Gate]", "[Visual Attachment]"))
        ]
        prior_outcomes = [o for o in prior_outcomes if not _is_workflow_refusal(o)]
        outcomes = prior_outcomes + [
            " ".join(_text_of(m.get("content", "")).split())[:160]
            for m in old_msgs
            if m.get("role") == "assistant"
            and not m.get("tool_calls")
            and _text_of(m.get("content", "")).strip()
            and not _is_workflow_refusal(_text_of(m.get("content", "")))
        ][-4:]
        summary_lines = [
            f"[Context Transfer Digest: {removed} earlier messages ({tool_rounds} tool rounds) summarized]"
        ]
        if goals:
            summary_lines.append(
                "Completed Earlier Topics (Already Answered — Reference Only): "
                + " | ".join(list(dict.fromkeys(goals))[-8:])
            )
        if outcomes:
            summary_lines.append("Key Prior Findings: " + " | ".join(list(dict.fromkeys(outcomes))[-4:]))
        if self.written_files:
            summary_lines.append("Modified Files: " + ", ".join(sorted(self.written_files)[:16]))
        if self.verified_files:
            summary_lines.append("Verified/Read Files: " + ", ".join(sorted(self.verified_files)[:16]))
        if self.tool_history:
            recent_ops = []
            for entry in self.tool_history[-8:]:
                t_name = entry.get("tool", "")
                t_args = entry.get("args") or {}
                target = t_args.get("path") or t_args.get("pattern") or t_args.get("command", "")[:32]
                label = f"{t_name}({target})" if target else t_name
                if label and label not in recent_ops:
                    recent_ops.append(label)
            if recent_ops:
                summary_lines.append("Recent Tool Milestones: " + ", ".join(recent_ops[-6:]))
        if self.decisions:
            summary_lines.append("Pinned Decisions: " + " | ".join(self.decisions[-8:]))
        if self.plan_items:
            p_tags = [
                f"{str(it.get('status') or 'pending').upper()}:{str(it.get('title') or it.get('id') or '')[:48]}"
                for it in self.plan_items[:10]
            ]
            summary_lines.append("Plan State: " + " ; ".join(p_tags))
        ws_digest = self.file_ledger.render_working_set_digest(max_chars=1800) if self.file_ledger else ""
        if ws_digest:
            summary_lines.append("Working-Set File Map:\n" + ws_digest)
        self.context_summary = "\n".join(summary_lines)
        # Append a typed digest entry so subagents & next turns have structured history
        if goals or outcomes or self.written_files:
            digest_entry = {
                "turns_evicted": removed,
                "goals": list(dict.fromkeys(goals))[-4:],
                "outcomes": list(dict.fromkeys(outcomes))[-2:],
                "written": sorted(self.written_files)[:8],
                "decisions": list(self.decisions[-4:]),
            }
            self.turn_digest = list(self.turn_digest or [])[-19:] + [digest_entry]
        self.messages = self.messages[split_idx:]
        return removed

    # ── Session persistence ───────────────────────────────────────────────────

    @property
    def _session_hash(self) -> str:
        return hashlib.md5(str(self.project_root).encode()).hexdigest()[:8]

    @property
    def _save_path(self) -> Path:
        save_dir = Path.home() / ".wsa-sessions"
        save_dir.mkdir(exist_ok=True)
        return save_dir / f"{self.project_name}-{self._session_hash}.json"

    def save(self) -> str:
        """Save session to disk. Returns save file path."""
        data = {
            "project_root": str(self.project_root),
            "selected_model": self.model,
            "messages": self.messages,
            "context_summary": self.context_summary,
            "free_mode": self.free_mode,
            "tool_history": self.tool_history[-50:],
            "turn_count": self.turn_count,
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "api_base": self.api_base,
            "api_key": self.api_key,
            "session_id": self.session_id,
            "permission_mode": self.permission_mode,
            "auto_approve": self.auto_approve,
            "written_files": sorted(self.written_files),
            "verified_files": sorted(self.verified_files),
            "active_account_label": self.active_account_label,
            "active_account_email": self.active_account_email,
            "active_browser": self.active_browser,
            "active_site": self.active_site,
            "active_web_model": self.active_web_model,
            "chat_urls": self.chat_urls,
            "active_chat_url": self.active_chat_url,
            "file_ledger": self.file_ledger.to_dict(),
            "decisions": self.decisions[-16:],
            "plan_items": self.plan_items[:24],
            "team_mode": self.team_mode,
            "thread_mode": self.thread_mode,
            "dual_lane": bool(self.dual_lane),
            "agent_mode": self.agent_mode,
            "multiagent_queue": list(self.multiagent_queue),
            "multiagent_index": int(self.multiagent_index),
            "role_threads": dict(self.role_threads),
            "turn_digest": [dict(d) for d in self.turn_digest[-20:]],
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        payload = json.dumps(data, indent=2)
        target = self._save_path
        tmp_path = target.parent / f".{target.name}.{secrets.token_hex(6)}.tmp"
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, target)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        return str(target)

    @classmethod
    def find_project_session_path(cls, project_root: Path) -> Optional[Path]:
        """Return the saved session JSON path for `project_root` if one exists and is non-empty."""
        root = Path(project_root).resolve()
        proj_hash = hashlib.md5(str(root).encode()).hexdigest()[:8]
        for folder in (".wsa-sessions", ".gwa-sessions"):
            candidate = Path.home() / folder / f"{root.name}-{proj_hash}.json"
            if candidate.exists():
                return candidate
        return None

    def auto_checkpoint(self) -> Optional[str]:
        """Silently persist a crash-safe session checkpoint after turns or compaction."""
        if self.turn_count <= 0 and not self.messages:
            return None
        try:
            return self.save()
        except Exception:
            return None

    def load(self, save_path: Path, *, sync_project_root: bool = True) -> None:
        """Load session from a JSON file, keeping project_root aligned when valid."""
        data = json.loads(save_path.read_text(encoding="utf-8"))
        saved_root_raw = str(data.get("project_root") or "").strip()
        if sync_project_root and saved_root_raw:
            try:
                saved_root = Path(saved_root_raw).expanduser().resolve()
                if saved_root.is_dir():
                    self.project_root = saved_root
            except OSError:
                pass
        self.__dict__.pop("_workspace_snapshot", None)
        self.__dict__.pop("_test_check_done", None)
        self.__dict__.pop("_tests_exist", None)
        self.messages = data.get("messages", [])
        self.context_summary = data.get("context_summary", "")
        self.session_id = str(data.get("session_id") or "").strip() or uuid.uuid4().hex[:12]
        self.free_mode = bool(data.get("free_mode", False))
        self.tool_history = data.get("tool_history", [])
        self.turn_count = data.get("turn_count", 0)
        self.total_tokens_in = data.get("total_tokens_in", 0)
        self.total_tokens_out = data.get("total_tokens_out", 0)
        # Bypass is an explicit choice for the current run, never inherited
        # from an old session whose historical default may have been auto.
        permission_mode = data.get("permission_mode", "smart")
        self.permission_mode = permission_mode if permission_mode in ("smart", "strict") else "smart"
        self.auto_approve = False
        self.written_files = set(data.get("written_files", []))
        self.verified_files = set(data.get("verified_files", []))
        self.active_account_label = str(data.get("active_account_label") or "")
        self.active_account_email = str(data.get("active_account_email") or "")
        self.active_browser = str(data.get("active_browser") or "")
        self.active_site = str(data.get("active_site") or "")
        self.active_web_model = str(data.get("active_web_model") or "")
        raw_urls = data.get("chat_urls")
        if isinstance(raw_urls, dict):
            self.chat_urls = {str(k): str(v) for k, v in raw_urls.items() if v}
        elif data.get("active_chat_url"):
            legacy_url = str(data["active_chat_url"])
            prov = infer_provider_from_site(legacy_url) or "gemini"
            self.chat_urls[prov] = legacy_url
        self.file_ledger = FileContextLedger.from_dict(data.get("file_ledger"))
        self.decisions = [str(d) for d in (data.get("decisions") or []) if d][-16:]
        self.plan_items = [dict(it) for it in (data.get("plan_items") or []) if isinstance(it, dict)][:24]
        tm = str(data.get("team_mode") or "auto").lower()
        self.team_mode = tm if tm in ("auto", "on", "off") else "auto"
        t_chat = str(data.get("thread_mode") or "single").lower()
        self.thread_mode = t_chat if t_chat in ("single", "roles") else "single"
        self.dual_lane = bool(data.get("dual_lane", True))
        am = str(data.get("agent_mode") or "singleagent").lower()
        self.agent_mode = am if am in ("singleagent", "multiagent") else "singleagent"
        raw_q = data.get("multiagent_queue") or []
        norm_q: List[str] = []
        for q_item in raw_q if isinstance(raw_q, list) else []:
            c_item = normalize_web_model(str(q_item))
            if c_item and c_item not in norm_q:
                norm_q.append(c_item)
        self.multiagent_queue = norm_q
        self.multiagent_index = max(0, int(data.get("multiagent_index") or 0))
        if self.multiagent_queue and self.multiagent_index >= len(self.multiagent_queue):
            self.multiagent_index = 0
        raw_roles = data.get("role_threads")
        self.role_threads = {str(k): str(v) for k, v in raw_roles.items() if v} if isinstance(raw_roles, dict) else {}
        self.turn_digest = [dict(d) for d in (data.get("turn_digest") or []) if isinstance(d, dict)][-20:]
        raw_model = str(data.get("selected_model") or data.get("custom_model") or "").strip()
        if raw_model:
            if not self.set_model(raw_model):
                inferred = infer_provider_from_site(raw_model)
                self.set_model(inferred or "gemini")

    @staticmethod
    def list_saved_sessions() -> List[Dict]:
        """Return list of saved sessions as dicts with metadata."""
        files: List[Path] = []
        for folder in (".wsa-sessions", ".gwa-sessions"):
            save_dir = Path.home() / folder
            if save_dir.exists():
                files.extend(save_dir.glob("*.json"))
        if not files:
            return []
        sessions = []
        for f in sorted(files, key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                sessions.append({
                    "file": f,
                    "name": f.stem,
                    "project": data.get("project_root", "?"),
                    "turns": data.get("turn_count", 0),
                    "saved_at": data.get("saved_at", "?"),
                    "messages": len(data.get("messages", [])),
                })
            except Exception:
                pass
        return sessions

    def to_dict(self) -> dict:
        return {
            "project_root": str(self.project_root),
            "model": self.model,
            "api_base": self.api_base,
            "api_key": self.api_key,
        }
