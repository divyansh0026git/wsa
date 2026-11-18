"""Gemini model catalog, mode/thinking metadata, and model config normalization."""
from __future__ import annotations

import json
import os
import time

from .config import CONFIG, _CONFIG_LOCK
from .parser import extract_thinking_text

MODE_CATEGORY_NAMES = {
    0: "MODE_CATEGORY_UNSPECIFIED",
    1: "MODE_CATEGORY_FAST",
    2: "MODE_CATEGORY_THINKING",
    3: "MODE_CATEGORY_PRO",
    4: "MODE_CATEGORY_AUTO",
    5: "MODE_CATEGORY_FAST_DYNAMIC_THINKING",
    6: "MODE_CATEGORY_FLASH_LITE",
    7: "MODE_CATEGORY_FLASH_PLUS",
    8: "MODE_CATEGORY_ULTRA",
}


THINKING_LEVEL_NAMES = {
    0: "THINKING_LEVEL_UNSPECIFIED",
    1: "THINKING_LEVEL_STANDARD",
    2: "THINKING_LEVEL_EXTENDED",
    3: "THINKING_LEVEL_DEEP_THINK",
    4: "THINKING_LEVEL_MEDIUM",
}


WEB_PROVIDER_SPECS = {
    "gemini": {
        "mode": -1,
        "mode_name": "GEMINI_WEB",
        "site": "gemini.google.com",
        "url": "https://gemini.google.com/app",
        "label": "Gemini",
        "owned_by": "google",
    },
    "claude": {
        "mode": -2,
        "mode_name": "CLAUDE_WEB",
        "site": "claude.ai",
        "url": "https://claude.ai/new",
        "label": "Claude",
        "owned_by": "anthropic",
    },
    "chatgpt": {
        "mode": -3,
        "mode_name": "CHATGPT_WEB",
        "site": "chatgpt.com",
        "url": "https://chatgpt.com/",
        "label": "ChatGPT",
        "owned_by": "openai",
    },
    "deepseek": {
        "mode": -4,
        "mode_name": "DEEPSEEK_WEB",
        "site": "chat.deepseek.com",
        "url": "https://chat.deepseek.com/",
        "label": "DeepSeek",
        "owned_by": "deepseek",
    },
    "venice": {
        "mode": -5,
        "mode_name": "VENICE_WEB",
        "site": "venice.ai",
        "url": "https://venice.ai/chat/agent",
        "label": "Venice",
        "owned_by": "venice",
    },
}

WEB_BRIDGE_MODES = tuple(spec["mode"] for spec in WEB_PROVIDER_SPECS.values())

LAST_UPSTREAM_MODEL = {
    "provider": None,
    "site": None,
    "badge": None,
    "hex": None,
    "mode": None,
    "mode_name": None,
    "slot80": None,
    "thinking_level": None,
    "is_tier4_high": False,
    "is_downgraded_flash_lite": False,
    "thinking_chars": 0,
    "updated_at": None,
    "account_file": None,
    "relay_id": None,
    "relay_label": None,
}


MODELS = {
    "gemini": {
        "provider": "gemini",
        "mode": -1,
        "think": 0,
        "desc": "Gemini Web Bridge (uses the model & thinking mode selected in gemini.google.com)",
    },
    "claude": {
        "provider": "claude",
        "mode": -2,
        "think": 0,
        "desc": "Claude Web Bridge (uses the model & thinking/effort selected in claude.ai)",
    },
    "chatgpt": {
        "provider": "chatgpt",
        "mode": -3,
        "think": 0,
        "desc": "ChatGPT Web Bridge (uses the model & Think toggle selected in chatgpt.com)",
    },
    "deepseek": {
        "provider": "deepseek",
        "mode": -4,
        "think": 0,
        "desc": "DeepSeek Web Bridge (uses the model & DeepThink toggle selected in chat.deepseek.com)",
    },
    "venice": {
        "provider": "venice",
        "mode": -5,
        "think": 0,
        "desc": "Venice Web Bridge (DeepSeek V4.1 Flash Uncensored on venice.ai)",
    },
    "gemini-4-argon": {
        "mode": 8, "think": 0,
        "hex": "95a4c0abcba24364",
        "slot80": 2,
        "desc": "Gemini 4 Argon High - maximum reasoning (requires Pro/Advanced cookie)",
    },
    "gemini-4-argon@think=0": {
        "mode": 8, "think": 0,
        "hex": "95a4c0abcba24364",
        "slot80": 2,
        "desc": "Gemini 4 Argon High - deepest thinking (requires Pro/Advanced cookie)",
    },
    "gemini-4-argon@think=1": {
        "mode": 8, "think": 0,
        "hex": "95a4c0abcba24364",
        "slot80": 2,
        "desc": "Gemini 4 Argon High - high thinking (requires Pro/Advanced cookie)",
    },
    "gemini-4-argon@think=2": {
        "mode": 8, "think": 0,
        "hex": "95a4c0abcba24364",
        "slot80": 4,
        "desc": "Gemini 4 Argon Medium - medium thinking (requires Pro/Advanced cookie)",
    },
    "gemini-4-carbon": {
        "mode": 8, "think": 0,
        "hex": "cf41b0e0dd7d53e5",
        "slot80": 2,
        "desc": "Gemini 4 Carbon High - alias for Tier-4 Ultra/Pro reasoning",
    },
    "gemini-3.7-flash": {
        "mode": 1, "think": 4,
        "desc": "Latest all-around model (Gemini 3.7 Flash)",
    },
    "gemini-3.6-flash": {
        "mode": 1, "think": 4,
        "desc": "All-around model (Gemini 3.6 Flash)",
    },
    "gemini-3.5-flash": {
        "mode": 1, "think": 4,
        "desc": "Alias for gemini-3.6-flash (backend upgraded)",
    },
    "gemini-3.5-flash-thinking": {
        "mode": 2, "think": 0,
        "desc": "Deep thinking mode, longest output (~20k chars)",
    },
    "gemini-3.1-pro": {
        "mode": 3, "think": 1,
        "hex": "pro",
        "field25": "pro",
        "slot80": 2,
        "inner2026": True,
        "desc": "Pro model (Gemini 3.1 Pro High on StreamGenerate, requires Pro cookie)",
    },
    "gemini-dom-relay": {
        "mode": -1, "think": 0,
        "desc": "Routes prompt through live Chrome Gemini tab (real 4 Argon High via processSession)",
    },
    "gemini-auto": {
        "mode": 4, "think": 4,
        "desc": "Auto model selection",
    },
    "gemini-3.5-flash-thinking-lite": {
        "mode": 5, "think": 0,
        "desc": "Dynamic thinking with adaptive depth",
    },
    "gemini-flash-lite": {
        "mode": 6, "think": 4,
        "desc": "Lightweight fast model",
    },
}


def extract_upstream_model_info(raw_or_inner) -> dict:
    """Extract upstream model badge, hex ID, executed MODE_CATEGORY, and THINKING_LEVEL from wrb.fr frames."""
    badge = None
    model_hex = None
    executed_mode = None
    executed_slot = None
    thinking_chars = 0

    def _inspect_inner(inner_list):
        nonlocal badge, model_hex, executed_mode, executed_slot, thinking_chars
        if not isinstance(inner_list, list):
            return
        if len(inner_list) > 42 and isinstance(inner_list[42], str) and inner_list[42]:
            badge = inner_list[42]
        if len(inner_list) > 39 and isinstance(inner_list[39], str) and inner_list[39]:
            model_hex = inner_list[39]
        if len(inner_list) > 58 and isinstance(inner_list[58], int):
            executed_mode = inner_list[58]
        if len(inner_list) > 46 and isinstance(inner_list[46], int):
            executed_slot = inner_list[46]
        if len(inner_list) > 4 and isinstance(inner_list[4], list) and inner_list[4]:
            t = extract_thinking_text(inner_list[4][0])
            if t and len(t) > thinking_chars:
                thinking_chars = len(t)

    if isinstance(raw_or_inner, dict):
        badge = raw_or_inner.get("badge")
        model_hex = raw_or_inner.get("hex")
        executed_mode = raw_or_inner.get("mode")
        executed_slot = raw_or_inner.get("slot80")
        thinking_chars = raw_or_inner.get("thinking_chars", 0)
    elif isinstance(raw_or_inner, list):
        _inspect_inner(raw_or_inner)
    elif isinstance(raw_or_inner, str):
        for line in raw_or_inner.split("\n"):
            if '"wrb.fr"' not in line or len(line) < 80:
                continue
            try:
                arr = json.loads(line)
                inner_str = arr[0][2] if arr and isinstance(arr[0], list) and len(arr[0]) > 2 else None
                if not inner_str:
                    continue
                _inspect_inner(json.loads(inner_str))
            except Exception:
                pass

    is_downgraded = bool(
        executed_mode == 6
        or (badge and any(k in badge.lower() for k in ("flash-lite", "flash lite", "auto", "gemini auto")))
    )
    is_tier4 = bool(
        not is_downgraded
        and (
            executed_mode == 8
            or (badge and ("4 Argon" in badge or "4 Carbon" in badge or badge.startswith("4 ")))
        )
    )
    return {
        "badge": badge,
        "hex": model_hex,
        "mode": executed_mode,
        "mode_name": MODE_CATEGORY_NAMES.get(executed_mode, str(executed_mode) if executed_mode is not None else None),
        "slot80": executed_slot,
        "thinking_level": THINKING_LEVEL_NAMES.get(executed_slot, str(executed_slot) if executed_slot is not None else None),
        "is_tier4_high": is_tier4,
        "is_downgraded_flash_lite": is_downgraded,
        "thinking_chars": thinking_chars,
    }


def update_last_upstream_model(raw_or_inner, thinking_chars: int = None) -> dict:
    """Update thread-safe LAST_UPSTREAM_MODEL telemetry if upstream frame has model metadata."""
    info = extract_upstream_model_info(raw_or_inner)
    if thinking_chars is not None and thinking_chars > info.get("thinking_chars", 0):
        info["thinking_chars"] = thinking_chars
    if info.get("badge") is None and info.get("mode") is None and not info.get("thinking_chars"):
        with _CONFIG_LOCK:
            return dict(LAST_UPSTREAM_MODEL)
    with _CONFIG_LOCK:
        for k, v in info.items():
            if v is not None or k in ("is_tier4_high", "is_downgraded_flash_lite"):
                LAST_UPSTREAM_MODEL[k] = v
        LAST_UPSTREAM_MODEL["updated_at"] = int(time.time())
        cookie_file = CONFIG.get("cookie_file")
        LAST_UPSTREAM_MODEL["account_file"] = os.path.basename(cookie_file) if cookie_file else "anonymous"
        return dict(LAST_UPSTREAM_MODEL)


def _normalize_model_cfg(model_id=None, think_mode: int = 4, model_cfg: dict = None) -> dict:
    """Normalize model_id / model_cfg and populate default Argon hex/slot80 for modes 7 and 8."""
    if model_cfg is not None:
        cfg = dict(model_cfg)
    elif isinstance(model_id, dict):
        cfg = dict(model_id)
    elif model_id is not None:
        cfg = {"mode": model_id, "think": think_mode}
    else:
        cfg = dict(MODELS[CONFIG["default_model"]])
    if cfg.get("mode") in (7, 8) and not cfg.get("hex"):
        cfg["hex"] = MODELS["gemini-4-argon"]["hex"]
        if cfg.get("slot80") is None:
            cfg["slot80"] = 2 if cfg.get("think", 0) in (0, 1, 2) else 4
    return cfg
