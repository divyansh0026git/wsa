"""
Claude Code-inspired color themes and persistence for WSA (Web Server Agent).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List


@dataclass(frozen=True)
class ThemePalette:
    id: str
    name: str
    description: str
    claude: str             # Primary brand accent (Claude terracotta)
    claude_shimmer: str     # Lighter spinner / highlight tint
    text: str               # Crisp foreground text
    secondary_text: str     # Supporting prose & args
    subtle: str             # Muted metadata & timestamps
    inactive: str           # Quiet separators & disabled hints
    border: str             # Hairline rules & panel borders
    welcome_border: str     # Welcome card frame color
    permission: str         # Strict / confirmation badge
    auto_accept: str        # Bypass permissions mode indicator
    plan_mode: str          # Smart / accept-edits / plan indicator
    suggestion: str         # Inline completions & hints
    success: str            # Pass / checkmark
    error: str              # Failure / error text
    warning: str            # Warning indicator
    diff_added: str         # Added diff line text
    diff_removed: str       # Removed diff line text
    code_theme: str = "github-dark"


THEMES: Dict[str, ThemePalette] = {
    "dark": ThemePalette(
        id="dark",
        name="Claude Dark",
        description="Graphite dark surfaces with warm #d77757 terracotta accent",
        claude="#d77757",
        claude_shimmer="#eb9f7f",
        text="#ededed",
        secondary_text="#a1a1a6",
        subtle="#76767c",
        inactive="#52525b",
        border="#3f3f46",
        welcome_border="#d77757",
        permission="#b1b9f9",
        auto_accept="#af87ff",
        plan_mode="#48968c",
        suggestion="#b1b9f9",
        success="#4cc38a",
        error="#e5484d",
        warning="#d99b38",
        diff_added="#4cc38a",
        diff_removed="#e5484d",
        code_theme="github-dark",
    ),
    "dark-daltonized": ThemePalette(
        id="dark-daltonized",
        name="Claude Dark (Colorblind-safe)",
        description="High-contrast dark theme with deuteranopia/protanopia-safe diff colors",
        claude="#d77757",
        claude_shimmer="#eb9f7f",
        text="#f8fafc",
        secondary_text="#cbd5e1",
        subtle="#787878",
        inactive="#52525b",
        border="#475569",
        welcome_border="#d77757",
        permission="#60a5fa",
        auto_accept="#a78bfa",
        plan_mode="#38bdf8",
        suggestion="#93c5fd",
        success="#38bdf8",
        error="#fb923c",
        warning="#facc15",
        diff_added="#38bdf8",
        diff_removed="#fb923c",
        code_theme="one-dark",
    ),
    "github-dark": ThemePalette(
        id="github-dark",
        name="GitHub Dark",
        description="GitHub primer dark canvas borders (#30363d) with #58a6ff accent",
        claude="#58a6ff",
        claude_shimmer="#79c0ff",
        text="#e6edf3",
        secondary_text="#8b949e",
        subtle="#6e7681",
        inactive="#484f58",
        border="#30363d",
        welcome_border="#58a6ff",
        permission="#d29922",
        auto_accept="#bc8cff",
        plan_mode="#3fb950",
        suggestion="#79c0ff",
        success="#3fb950",
        error="#f85149",
        warning="#d29922",
        diff_added="#3fb950",
        diff_removed="#f85149",
        code_theme="github-dark",
    ),
    "dracula": ThemePalette(
        id="dracula",
        name="Dracula Dark",
        description="Dracula dark palette with warm #ffb86c orange and #8be9fd cyan",
        claude="#ffb86c",
        claude_shimmer="#ffd29d",
        text="#f8f8f2",
        secondary_text="#bd93f9",
        subtle="#6272a4",
        inactive="#44475a",
        border="#44475a",
        welcome_border="#ffb86c",
        permission="#8be9fd",
        auto_accept="#bd93f9",
        plan_mode="#50fa7b",
        suggestion="#8be9fd",
        success="#50fa7b",
        error="#ff5555",
        warning="#f1fa8c",
        diff_added="#50fa7b",
        diff_removed="#ff5555",
        code_theme="dracula",
    ),
}

THEME_CONFIG_PATH = Path.home() / ".wsa-theme.json"
_LEGACY_THEME_CONFIG_PATH = Path.home() / ".gwa-theme.json"
_ACTIVE_THEME_ID: str = "dark"


def _load_saved_theme_id() -> str:
    for cfg in (THEME_CONFIG_PATH, _LEGACY_THEME_CONFIG_PATH):
        try:
            if cfg.exists():
                data = json.loads(cfg.read_text(encoding="utf-8"))
                tid = str(data.get("theme") or "").strip().lower()
                if tid in THEMES:
                    return tid
        except Exception:
            pass
    return "dark"


_ACTIVE_THEME_ID = _load_saved_theme_id()


def get_theme() -> ThemePalette:
    return THEMES.get(_ACTIVE_THEME_ID, THEMES["dark"])


def list_themes() -> List[ThemePalette]:
    return list(THEMES.values())


def set_theme(theme_id: str, persist: bool = True) -> ThemePalette:
    global _ACTIVE_THEME_ID
    tid = (theme_id or "").strip().lower()
    if tid not in THEMES:
        raise ValueError(f"Unknown theme '{theme_id}'. Valid: {', '.join(THEMES)}")
    _ACTIVE_THEME_ID = tid
    if persist:
        try:
            THEME_CONFIG_PATH.write_text(json.dumps({"theme": tid}, indent=2), encoding="utf-8")
        except Exception:
            pass
    return THEMES[tid]

