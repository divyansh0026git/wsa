"""
Claude Code-style inline interactive TUI selectors for WSA (Web Server Agent).
Supports arrow-key navigation (↑↓ / j k), Enter to confirm, Esc to cancel,
and graceful numbered fallback in non-TTY terminals.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from rich.markup import escape
from rich.rule import Rule

from . import renderer as R
from .session import MODEL_CATALOG
from .theme import ThemePalette, get_theme, list_themes

try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import StyleAndTextTuples
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.shortcuts import input_dialog
    from prompt_toolkit.styles import Style as PTStyle
    HAS_PT_UI = True
except ImportError:
    HAS_PT_UI = False


def _pt_dialog_style(theme: Optional[ThemePalette] = None):
    if not HAS_PT_UI:
        return None
    t = theme or get_theme()
    return PTStyle.from_dict({
        "dialog":             "bg:#141415",
        "dialog.body":        f"bg:#19191b fg:{t.text}",
        "dialog.shadow":      "bg:#0f0f10",
        "dialog frame.label": f"bg:#19191b fg:{t.claude} bold",
        "dialog frame":       f"fg:{t.border}",
        "button":             f"bg:#232325 fg:{t.secondary_text}",
        "button.focused":     f"bg:{t.claude} fg:#141415 bold",
    })


def _is_interactive_tty() -> bool:
    try:
        return bool(sys.stdin and sys.stdout and os.isatty(sys.stdin.fileno()) and os.isatty(sys.stdout.fileno()))
    except Exception:
        return False


def _inline_select_menu(
    title: str,
    subtitle: str,
    options: List[Dict[str, str]],
    current_value: str = "",
) -> Optional[str]:
    """
    Claude Code-style rule-framed inline selector (↑↓ / j k · Enter · Esc).
    Each option dict supports keys: value, title, meta, detail, extra.
    """
    if not options:
        return None

    t = get_theme()
    start_idx = next((i for i, o in enumerate(options) if o["value"] == current_value), 0)

    if HAS_PT_UI and _is_interactive_tty():
        try:
            state = {"idx": start_idx, "result": None}
            kb = KeyBindings()

            @kb.add("up")
            @kb.add("k")
            def _up(event):
                state["idx"] = (state["idx"] - 1) % len(options)
                event.app.invalidate()

            @kb.add("down")
            @kb.add("j")
            def _down(event):
                state["idx"] = (state["idx"] + 1) % len(options)
                event.app.invalidate()

            @kb.add("home")
            def _home(event):
                state["idx"] = 0
                event.app.invalidate()

            @kb.add("end")
            def _end(event):
                state["idx"] = len(options) - 1
                event.app.invalidate()

            @kb.add("enter")
            def _accept(event):
                state["result"] = options[state["idx"]]["value"]
                event.app.exit(result=state["result"])

            @kb.add("escape")
            @kb.add("q")
            @kb.add("c-c")
            def _cancel(event):
                state["result"] = None
                event.app.exit(result=None)

            def _render_fragments() -> StyleAndTextTuples:
                cols = R.ui_frame_width()
                rule_line = "─" * cols
                max_title = max(14, min(26, max((len(o.get("title", o["value"])) for o in options), default=18) + 2))
                compact = cols < 66
                chev = R.ui_glyph("chevron")
                ok_mark = R.ui_glyph("ok")
                frags: StyleAndTextTuples = [
                    (f"fg:{t.border}", f"  {rule_line}\n"),
                    (f"fg:{t.text} bold", f"  {title}"),
                    (f"fg:{t.subtle}", f"  ·  {subtitle}\n\n" if subtitle else "\n\n"),
                ]

                for i, opt in enumerate(options):
                    focused = i == state["idx"]
                    active = opt["value"] == current_value
                    pointer = f"{chev} " if focused else "  "
                    num = f"{i + 1:2d}  "
                    label = opt.get("title", opt["value"])
                    meta = opt.get("meta", "")
                    suffix = f"  {ok_mark}" if active else ""

                    if focused:
                        frags.append((f"fg:{t.claude} bold", f"  {pointer}"))
                        frags.append((f"fg:{t.subtle}", num))
                        frags.append((f"fg:{t.text} bold", label if compact else f"{label:<{max_title}}"))
                        if meta and not compact:
                            frags.append((f"fg:{t.secondary_text}", f"  {meta}"))
                        if suffix:
                            frags.append((f"fg:{t.success} bold", suffix))
                        if compact and meta:
                            frags.append(("", f"\n        "))
                            frags.append((f"fg:{t.subtle}", meta))
                    else:
                        frags.append(("", f"  {pointer}"))
                        frags.append((f"fg:{t.subtle}", num))
                        frags.append((f"fg:{t.secondary_text}", label if compact else f"{label:<{max_title}}"))
                        if meta and not compact:
                            frags.append((f"fg:{t.subtle}", f"  {meta}"))
                        if suffix:
                            frags.append((f"fg:{t.success}", suffix))
                    frags.append(("", "\n"))

                cur = options[state["idx"]]
                frags.append(("", "\n"))
                if cur.get("detail"):
                    frags.append((f"fg:{t.secondary_text}", f"    {cur['detail']}\n"))
                if cur.get("extra"):
                    frags.append((f"fg:{t.subtle}", f"    {cur['extra']}\n"))
                frags.append((f"fg:{t.border}", f"  {rule_line}\n"))
                frags.append((f"fg:{t.subtle}", "  ↑/↓ move  ·  Enter select  ·  Esc cancel"))
                return frags

            control = FormattedTextControl(text=_render_fragments, focusable=True, show_cursor=False)
            app = Application(
                layout=Layout(HSplit([Window(content=control, always_hide_cursor=True)])),
                key_bindings=kb,
                full_screen=False,
                mouse_support=False,
            )
            return app.run()
        except Exception:
            pass

    return _fallback_numbered_select(title, options, current_value)


def _fallback_numbered_select(
    title: str,
    options: List[Dict[str, str]],
    current_value: str = "",
) -> Optional[str]:
    """Clean fallback selector when interactive key capture is unavailable."""
    R.sync_theme()
    max_title = max(14, min(26, max((len(o.get("title", o["value"])) for o in options), default=18) + 2))
    R.console.print()
    R.console.print(Rule(title=f"[bold {R.Theme.PRIMARY}]{escape(title)}[/]", align="left", style=R.Theme.BORDER))
    for i, opt in enumerate(options, 1):
        is_curr = opt["value"] == current_value
        marker = f"[{R.Theme.ACCENT}]{R.ui_glyph('chevron')}[/]" if is_curr else " "
        meta = f"  [{R.Theme.DIM}]{escape(opt['meta'])}[/]" if opt.get("meta") else ""
        check = f"  [{R.Theme.OK}]{R.ui_glyph('ok')}[/]" if is_curr else ""
        R.console.print(
            f"  {marker} [{R.Theme.DIM}]{i:2d}[/]  [bold {R.Theme.PRIMARY}]{escape(opt['title']):<{max_title}}[/]{meta}{check}"
        )
        if opt.get("extra"):
            R.console.print(f"       [{R.Theme.SECONDARY}]{escape(opt['extra'])}[/]")
    R.console.print(Rule(style=R.Theme.BORDER))
    R.console.print(f"  [{R.Theme.DIM}]Select number (Enter keeps current)[/] ", end="")
    try:
        choice = input().strip()
        if not choice:
            return None
        idx = int(choice) - 1
        if 0 <= idx < len(options):
            return options[idx]["value"]
    except (ValueError, EOFError, KeyboardInterrupt):
        pass
    return None


# ─── Model Picker (Web Bridges: claude | deepseek | gemini | chatgpt | venice) ────────

def _model_display_items(state=None) -> List[Dict[str, str]]:
    """Build structured Web Bridge interactive options ('claude' | 'deepseek' | 'gemini' | 'chatgpt' | 'venice')."""
    from .session import get_model_info
    items: List[Dict[str, str]] = []
    for raw_m in MODEL_CATALOG:
        m = get_model_info(raw_m["id"], state=state)
        items.append({
            "value": m["id"],
            "title": m["name"],
            "meta": m["transport"],
            "detail": f"/model {m['id']}  ·  {m['thinking']}  ·  {m['requires']}",
            "extra": m["features"],
        })
    return items


def pick_model(current: str = "", state=None) -> Optional[str]:
    """Show interactive Web Bridge selector (claude | deepseek | gemini | chatgpt | venice)."""
    options = _model_display_items(state=state)
    acct_lbl = (getattr(state, "active_account_label", "") or "").strip() if state is not None else ""
    acct_email = (getattr(state, "active_account_email", "") or "").strip() if state is not None else ""
    sub = "claude · deepseek · gemini · chatgpt · venice"
    if acct_lbl and acct_email:
        sub += f" · {acct_lbl} · {acct_email}"
    elif acct_lbl:
        sub += f" · {acct_lbl}"
    return _inline_select_menu(
        title="Web Bridge",
        subtitle=sub,
        options=options,
        current_value=current,
    )


def pick_multiagent_strategy(state=None) -> Optional[str]:
    """Choose Default (claude -> deepseek -> gemini -> chatgpt) or Custom slot-by-slot queue."""
    from .session import DEFAULT_MULTIAGENT_QUEUE
    default_chain = " → ".join(DEFAULT_MULTIAGENT_QUEUE)
    cur_mode = (getattr(state, "agent_mode", "singleagent") or "singleagent").strip().lower()
    acct_lbl = (getattr(state, "active_account_label", "") or "").strip() if state is not None else ""
    sub = "Select exact models, Think/DeepThink & effort directly on each website"
    if acct_lbl:
        sub = f"{acct_lbl} · {sub}"
    options = [
        {
            "value": "default",
            "title": "Default Multiagent",
            "meta": default_chain,
            "detail": f"Queue order: {default_chain}",
            "extra": "Auto-transfers full context to the next website when any limit is reached",
        },
        {
            "value": "custom",
            "title": "Custom Multiagent",
            "meta": "build queue slot-by-slot",
            "detail": "Pick Slot 1, Slot 2, ... from claude / deepseek / gemini / chatgpt / venice",
            "extra": "Shows remaining available models and the allotted queue at each step",
        },
    ]
    return _inline_select_menu(
        title="Multiagent Failover Queue",
        subtitle=sub,
        options=options,
        current_value="default" if cur_mode == "multiagent" else "",
    )


def pick_multiagent_custom_queue(state=None) -> Optional[List[str]]:
    """Interactive slot-by-slot queue builder displaying remaining models and allotted queue."""
    from .session import get_model_info

    allotted: List[str] = []
    remaining = [m["id"] for m in MODEL_CATALOG]

    while remaining:
        slot_num = len(allotted) + 1
        allotted_txt = " → ".join(allotted) if allotted else "(none yet)"
        avail_txt = ", ".join(remaining)

        R.console.print()
        R.console.print(
            f"  [bold {R.Theme.PRIMARY}]Slot {slot_num}[/]  "
            f"[{R.Theme.DIM}]Allotted queue:[/] [bold {R.Theme.ACCENT}]{escape(allotted_txt)}[/]  "
            f"[{R.Theme.DIM}]Available:[/] [{R.Theme.SECONDARY}]{escape(avail_txt)}[/]"
        )

        options: List[Dict[str, str]] = []
        for mid in remaining:
            info = get_model_info(mid, state=state)
            options.append({
                "value": mid,
                "title": info["name"],
                "meta": info["site"],
                "detail": f"Allot as Slot {slot_num} ({mid})  ·  {info['transport']}",
                "extra": info["features"],
            })
        if len(allotted) >= 2:
            options.append({
                "value": "__done__",
                "title": f"Confirm allotted queue ({len(allotted)} sites)",
                "meta": allotted_txt,
                "detail": "Lock queue order and check open synced browser tabs",
                "extra": f"Order: {allotted_txt}",
            })

        picked = _inline_select_menu(
            title=f"Choose Slot {slot_num} Model",
            subtitle=f"Allotted: {allotted_txt}  ·  Available: {avail_txt}",
            options=options,
            current_value="",
        )
        if picked is None:
            return None
        if picked == "__done__":
            return allotted
        if picked in remaining:
            allotted.append(picked)
            remaining.remove(picked)

    return allotted if allotted else None


# ─── Theme Picker (/theme) ────────────────────────────────────────────────────

def pick_theme(current: str = "") -> Optional[str]:
    """Show interactive /theme palette selector."""
    curr = current or get_theme().id
    themes = list_themes()
    options = [
        {
            "value": pal.id,
            "title": pal.name,
            "meta": f"accent {pal.claude}",
            "detail": pal.description,
            "extra": f"/theme {pal.id}",
        }
        for pal in themes
    ]
    return _inline_select_menu(
        title="Theme",
        subtitle=f"{len(themes)} palettes",
        options=options,
        current_value=curr,
    )


# ─── Project Directory Picker ─────────────────────────────────────────────────

def pick_project(current: Path) -> Optional[Path]:
    """Prompt for a project directory path."""
    if HAS_PT_UI and _is_interactive_tty():
        try:
            from prompt_toolkit.formatted_text import HTML
            result = input_dialog(
                title="  Switch Project  ",
                text=HTML(
                    "  Enter the project directory path:\n"
                    f"  Current: <ansigray>{escape(str(current))}</ansigray>"
                ),
                default=str(current),
                style=_pt_dialog_style(),
            ).run()
            if result and result.strip():
                p = Path(result.strip()).expanduser().resolve()
                if p.is_dir():
                    return p
                R.print_error(f"Directory not found: {result}")
            return None
        except Exception:
            pass

    R.console.print(f"  [dim]Current: {escape(str(current))}[/]")
    R.console.print("  New project path: ", end="")
    try:
        raw = input().strip()
        if raw:
            p = Path(raw).expanduser().resolve()
            if p.is_dir():
                return p
            R.print_error(f"Directory not found: {raw}")
    except (EOFError, KeyboardInterrupt):
        pass
    return None


def confirm(title: str, text: str, default: bool = False) -> bool:
    """Clean inline Yes/No prompt with explicit option legend."""
    suffix = "[Y/n]" if default else "[y/N]"
    yes_lbl = "Yes (default)" if default else "Yes"
    no_lbl = "No" if default else "No (default)"
    R.console.print(
        f"  [{R.Theme.DIM}]{R.ui_glyph('bullet')}[/] {escape(text)} [{R.Theme.DIM}]{escape(suffix)}[/] "
        f"[{R.Theme.DIM}](y={yes_lbl}, n={no_lbl})[/] ",
        end="",
    )
    try:
        ans = input().strip().lower().strip("[](). ")
        if not ans:
            return default
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return default
