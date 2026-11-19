"""
Slash command handlers for WSA (Web Server Agent).
Each handler returns (handled: bool, should_continue: bool).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Optional, Tuple

from .session import (
    SessionState,
    ALL_MODELS,
    DEFAULT_MULTIAGENT_QUEUE,
    PROVIDER_SITES,
    get_model_info,
    normalize_web_model,
)
from .theme import get_theme, list_themes
from .tools import (
    set_project_root,
    execute_tool,
    get_file_backups,
    get_backup_order,
    undo_last_edit,
    clear_session_memory,
    reset_project_session_state,
    _strip_line_number_prefixes,
)
from . import renderer as R
from . import tui as TUI
from rich.markup import escape
from rich.rule import Rule
from rich.table import Table


HandlerResult = Tuple[bool, bool]  # (handled, should_continue)


_LAST_CLIENT: Optional[Any] = None


def handle_command(cmd_line: str, state: SessionState, client: Optional[Any] = None) -> HandlerResult:
    """
    Parse and dispatch a slash command.
    Returns (handled, should_continue).
    """
    global _LAST_CLIENT
    if client is not None:
        _LAST_CLIENT = client

    parts = cmd_line.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/plan", "/verify", "/multiagent", "/multi", "/singleagent", "/single"):
        eff_client = client if client is not None else _LAST_CLIENT
        if cmd == "/plan":
            return _cmd_plan(arg, state, client=eff_client)
        if cmd == "/verify":
            return _cmd_verify(arg, state, client=eff_client)
        if cmd in ("/multiagent", "/multi"):
            return _cmd_multiagent(arg, state, client=eff_client)
        return _cmd_singleagent(arg, state, client=eff_client)

    dispatch = {
        "/help":      _cmd_help,
        "/h":         _cmd_help,
        "/shortcuts": _cmd_shortcuts,
        "/?":         _cmd_shortcuts,
        "/theme":     _cmd_theme,
        "/themes":    _cmd_theme,
        "/models":    _cmd_models,
        "/model":     _cmd_model,
        "/project":   _cmd_project,
        "/cd":        _cmd_project,
        "/p":         _cmd_project,
        "/status":    _cmd_status,
        "/s":         _cmd_status,
        "/init":      _cmd_init,
        "/files":     _cmd_files,
        "/f":         _cmd_files,
        "/read":      _cmd_read,
        "/r":         _cmd_read,
        "/run":       _cmd_run,
        "/search":    _cmd_search,
        "/new":       _cmd_new,
        "/clear":     _cmd_clear,
        "/compact":   _cmd_compact,
        "/diff":      _cmd_diff,
        "/undo":      _cmd_undo,
        "/u":         _cmd_undo,
        "/save":      _cmd_save,
        "/load":      _cmd_load,
        "/tokens":    _cmd_tokens,
        "/rules":     _cmd_rules,
        "/tools":     _cmd_tools,
        "/skills":    _cmd_skills,
        "/skill":     _cmd_skills,
        "/perm":      _cmd_perm,
        "/permissions": _cmd_perm,
        "/account":   _cmd_account,
        "/accounts":  _cmd_account,
        "/acc":       _cmd_account,
        "/free":      _cmd_free,
        "/team":      _cmd_team,
        "/concept":   _cmd_concept,
        "/concepts":  _cmd_concept,
        "/exit":      _cmd_exit,
        "/quit":      _cmd_exit,
        "/q":         _cmd_exit,
        "/bye":       _cmd_exit,
    }

    if cmd not in dispatch:
        return False, True

    return dispatch[cmd](arg, state)


def _cmd_help(arg: str, state: SessionState) -> HandlerResult:
    R.print_help()
    return True, True


def _cmd_shortcuts(arg: str, state: SessionState) -> HandlerResult:
    R.print_shortcuts()
    return True, True


def _cmd_theme(arg: str, state: SessionState) -> HandlerResult:
    """View or switch the Claude Code color theme (/theme [id])."""
    target = arg.strip().lower()
    if target == "list":
        curr = get_theme().id
        R.console.print()
        for pal in list_themes():
            mark = f"[{R.Theme.ACCENT}]✓[/]" if pal.id == curr else " "
            R.console.print(
                f"  {mark} [bold {R.Theme.PRIMARY}]{escape(pal.id):<16}[/] "
                f"[{R.Theme.ACCENT}]{escape(pal.claude)}[/]  [{R.Theme.SECONDARY}]{escape(pal.description)}[/]"
            )
        R.console.print()
        return True, True

    chosen = target
    if not chosen:
        chosen = TUI.pick_theme(get_theme().id) or ""
        if not chosen:
            R.print_info(f"Theme unchanged: {escape(get_theme().name)}")
            return True, True

    try:
        pal = R.apply_theme(chosen, persist=True)
        R.print_success(f"Theme -> [{pal.claude}]{escape(pal.name)}[/] [{R.Theme.DIM}]({escape(pal.id)})[/]")
    except ValueError as e:
        R.print_error(str(e))
        R.print_info("Available themes: " + ", ".join(p.id for p in list_themes()))
    return True, True


def _cmd_free(arg: str, state: SessionState) -> HandlerResult:
    """Toggle Pure Web Mode without local system/tool overhead."""
    value = arg.strip().lower()
    if value in ("on", "1", "true"):
        state.free_mode = True
    elif value in ("off", "0", "false"):
        state.free_mode = False
    else:
        state.free_mode = not state.free_mode
    mode = (
        "ON (Pure Web mode — no local system prompt or tool schemas)"
        if state.free_mode
        else "OFF (Agentic Coding mode — tools active)"
    )
    R.print_success(f"Free Web Mode -> [bold]{mode}[/]")
    return True, True


def _cmd_models(arg: str, state: SessionState) -> HandlerResult:
    R.print_models_table(ALL_MODELS, current_model=state.model, state=state)
    return True, True


def _cmd_model(arg: str, state: SessionState) -> HandlerResult:
    chosen = arg.strip()
    if not chosen:
        chosen = TUI.pick_model(current=state.model, state=state) or ""
        if not chosen:
            R.print_info(f"Bridge unchanged: [{R.Theme.ACCENT}]{escape(R.friendly_model_name(state.model))}[/]")
            return True, True
    if not state.set_model(chosen):
        R.print_error(
            f"Unknown model bridge '{arg}'. Choose: claude (claude.ai), deepseek (chat.deepseek.com), "
            "gemini (gemini.google.com), chatgpt (chatgpt.com), or venice (venice.ai)."
        )
        R.print_info("Select exact model & thinking mode directly inside your open browser tab.")
        return True, True
    if getattr(state, "agent_mode", "singleagent") == "multiagent" and state.model in (getattr(state, "multiagent_queue", None) or []):
        state.multiagent_index = state.multiagent_queue.index(state.model)
    info = get_model_info(state.model, state=state)
    web_badge = str(getattr(state, "active_web_model", "") or "").strip()
    detail = f"{state.model} · {info['transport']}" + (f" · web: {web_badge}" if web_badge else "")
    R.print_success(
        f"Bridge -> [bold {R.Theme.ACCENT}]{escape(info['name'])}[/] "
        f"[{R.Theme.DIM}]({escape(detail)})[/]"
    )
    return True, True


def _cmd_project(arg: str, state: SessionState) -> HandlerResult:
    if not arg:
        new_path = TUI.pick_project(state.project_root)
        if new_path and state.set_project(str(new_path)):
            reset_project_session_state()
            set_project_root(state.project_root)
            state._workspace_snapshot = None
            state._test_check_done = False
            state._tests_exist = None
            clear_session_memory()
            R.print_success(f"Project -> [bold]{escape(str(state.project_root))}[/] ({state.project_type})")
            R.print_info("Conversation history cleared for new project.")
        else:
            R.print_info(f"Project unchanged: [bold]{escape(str(state.project_root))}[/]")
        return True, True
    if state.set_project(arg):
        reset_project_session_state()
        set_project_root(state.project_root)
        state._workspace_snapshot = None
        state._test_check_done = False
        state._tests_exist = None
        clear_session_memory()
        R.print_success(f"Project -> [bold]{escape(str(state.project_root))}[/] ({state.project_type})")
        R.print_info("Conversation history cleared for new project.")
    else:
        R.print_error(f"Directory not found: {arg}")
    return True, True


def _cmd_status(arg: str, state: SessionState) -> HandlerResult:
    try:
        from .client import WSAClient
        client = WSAClient(state.api_base, state.api_key)
        health = client.get_health()
        state._ui_connection = ("Server ready · browser relay active" if health.get("dom_relay_active")
                                else "Server ready · no active browser relay") if health else "Server unreachable"
        # Health is global, while this session can be pinned to another browser.
        # Update account metadata only when it refers to the same browser.
        browser = str(health.get("active_browser") or "").lower()
        bound = str(getattr(state, "active_browser", "") or "").lower()
        if health and (not bound or (browser and browser == bound)):
            state.sync_account_from_health(health)
        if isinstance(health.get("last_upstream_model"), dict):
            u = health["last_upstream_model"]
            if u.get("badge") or u.get("mode") is not None:
                state.last_upstream_model = u
    except Exception:
        pass
    R.print_status(state)
    return True, True


def _cmd_account(arg: str, state: SessionState) -> HandlerResult:
    """View, bind, switch, or rotate Chrome/Brave browser bridge and account."""
    R.sync_theme()
    from .client import WSAClient
    client = WSAClient(state.api_base, state.api_key)
    sub = arg.strip()
    sid = getattr(state, "session_id", "")

    if sub.lower() == "rotate":
        res = client.rotate_account()
        if res.get("ok"):
            bound = res.get("active_relay_label") or res.get("active_relay") or res.get("active_account")
            old_urls = list((state.chat_urls or {}).values()) + list((state.role_threads or {}).values())
            state.sync_account_from_res(res)
            state.clear_provider_chats(clear_roles=True)
            if sid and callable(getattr(client, "clear_session_chats", None)):
                client.clear_session_chats(sid, client_urls=old_urls)
            R.print_success(
                f"Rotated active account -> [bold {R.Theme.ACCENT}]{escape(str(bound))}[/] "
                f"[{R.Theme.DIM}](context preserved: {state.turn_count} turns)[/]"
            )
        else:
            R.print_warn(
                f"Could not rotate browser/account (pool size: {len(res.get('pool') or [])}, "
                f"relays: {len(res.get('relays') or [])}). Open Brave or Chrome with WSA extension synced."
            )
        return True, True

    if sub and sub.lower() != "list":
        target = sub.split(maxsplit=1)[1].strip() if sub.lower().startswith("switch ") else sub
        if target.lower() in ("brave", "chrome", "edge", "auto"):
            res = client.bind_relay(target, session_id=sid)
            if not res.get("ok"):
                res = client.switch_account(target, session_id=sid)
        else:
            res = client.switch_account(target, session_id=sid)
            if not res.get("ok") and not target.isdigit():
                res = client.bind_relay(target, session_id=sid)
        if res.get("ok"):
            bound = res.get("active_relay_label") or res.get("active_relay") or res.get("active_account")
            old_urls = list((state.chat_urls or {}).values()) + list((state.role_threads or {}).values())
            state.sync_account_from_res(res)
            if int(res.get("cleared_chats") or 0) > 0:
                state.clear_provider_chats(clear_roles=True)
            if sid and callable(getattr(client, "clear_session_chats", None)):
                client.clear_session_chats(sid, client_urls=old_urls)
            extra = ""
            if res.get("account_email"):
                extra = f" · {res['account_email']}"
            site = res.get("active_site") or ""
            badge = res.get("model_badge") or ""
            web_bits = " · ".join(x for x in (site, badge) if x)
            suffix = f" ({web_bits})" if web_bits else ""
            R.print_success(
                f"Bound browser -> [bold {R.Theme.ACCENT}]{escape(str(bound))}[/] "
                f"[{R.Theme.DIM}]({escape(res.get('browser') or 'http')}{escape(extra)}{escape(suffix)} · context preserved)[/]"
            )
        else:
            R.print_error(str(res.get("error") or "Switch failed"))
        return True, True

    info = client.get_accounts()
    health = client.get_health()
    relays = info.get("relays") or health.get("relays") or []
    accounts = info.get("accounts") or []
    state.sync_account_from_health(health)
    active_relay = info.get("active_relay") or health.get("active_relay")
    if active_relay and not state.active_account_label:
        state.active_account_label = str(active_relay)

    R.console.print()
    R.console.print(
        Rule(
            title=f"[bold {R.Theme.PRIMARY}]Browser Bridges (Chrome & Brave)[/] [{R.Theme.DIM}]· synced web sessions[/]",
            align="left",
            style=R.Theme.BORDER,
        )
    )
    if not relays:
        R.console.print(f"  [{R.Theme.DIM}]No browser extensions connected — click Sync in Brave or Chrome extension popup[/]")
    else:
        for r in relays:
            is_act = bool(r.get("active"))
            marker = f"[bold {R.Theme.ACCENT}]✓[/]" if is_act else " "
            dot = f"[{R.Theme.OK}]● online[/]" if r.get("online") else f"[{R.Theme.DIM}]○ offline ({r.get('last_seen_age_sec')}s)[/]"
            email = r.get("account_email") or f"/u/{r.get('auth_user', '0')}"
            badge = r.get("model_badge") or "auto-detecting"
            active_site = r.get("active_site") or ""
            provs = r.get("providers") or {}
            site_summaries = []
            for p_name, site_dom in (
                ("claude", "claude.ai"),
                ("deepseek", "chat.deepseek.com"),
                ("gemini", "gemini.google.com"),
                ("chatgpt", "chatgpt.com"),
                ("venice", "venice.ai"),
            ):
                p_data = provs.get(p_name) if isinstance(provs, dict) else None
                if isinstance(p_data, dict) and p_data.get("has_tab"):
                    p_bits = [site_dom]
                    if p_data.get("model_badge"):
                        p_bits.append(str(p_data["model_badge"]))
                    if p_data.get("email") or p_data.get("account_email"):
                        p_bits.append(str(p_data.get("email") or p_data.get("account_email")))
                    site_summaries.append(" · ".join(p_bits))
            detail_str = " | ".join(site_summaries) if site_summaries else (f"{active_site} · {email} · {badge}".strip(" ·"))
            R.console.print(
                f"  {marker} [{R.Theme.PRIMARY if not is_act else R.Theme.ACCENT}]{escape(str(r.get('label'))):<12}[/] "
                f"[{R.Theme.SECONDARY}]{escape(str(r.get('browser'))):<8}[/] "
                f"[{R.Theme.DIM}]{escape(detail_str)}[/]  {dot}"
            )

    if accounts:
        R.console.print()
        R.console.print(f"  [bold {R.Theme.PRIMARY}]Saved Cookie Profiles[/]")
        for item in accounts:
            marker = f"[bold {R.Theme.OK}]✓[/]" if item.get("active") else " "
            R.console.print(
                f"  {marker} [{R.Theme.DIM}]{item.get('index')}.[/] [{R.Theme.PRIMARY}]{escape(str(item.get('name')))}[/]  "
                f"[{R.Theme.DIM}]{escape(str(item.get('path')))}[/]"
            )

    u = health.get("last_upstream_model") or getattr(state, "last_upstream_model", None) or {}
    if isinstance(u, dict) and (u.get("badge") or u.get("mode") is not None):
        R.console.print()
        via = u.get("relay_label") or u.get("account_file") or "-"
        site_tag = f" ({u['site']})" if u.get("site") else ""
        R.console.print(
            f"  [{R.Theme.DIM}]Last turn[/]  [bold {R.Theme.PRIMARY}]{escape(str(u.get('badge')))}{escape(site_tag)}[/] "
            f"[{R.Theme.DIM}]via {escape(str(via))} · {u.get('thinking_chars', 0)} thinking chars[/]"
        )
    R.console.print(
        f"  [{R.Theme.DIM}]Bind[/]  /account chrome  ·  /account brave  ·  /account list  ·  /account rotate"
    )
    R.console.print(Rule(style=R.Theme.BORDER))
    R.console.print()
    return True, True


def _cmd_init(arg: str, state: SessionState) -> HandlerResult:
    """Scan the project structure and load it into context."""
    R.print_info("Scanning project structure...")
    result = execute_tool("list_dir", {"path": ".", "recursive": True})
    lines = result.splitlines()
    R.print_success(f"Found {len(lines)} files in {state.project_name}")
    state.messages.append({
        "role": "user",
        "content": (
            f"[Context] I'm working in the project '{state.project_name}'. "
            f"Here's the full file tree:\n\n```\n{result}\n```\n\n"
            "Please acknowledge this structure so I know you've seen it."
        ),
    })
    R.print_info("Project context loaded into conversation. The agent now knows the file structure.")
    return True, True


def _cmd_files(arg: str, state: SessionState) -> HandlerResult:
    path = arg or "."
    result = execute_tool("list_dir", {"path": path, "recursive": False})
    R.console.print()
    R.console.print(f"  [bold {R.Theme.PRIMARY}]{escape(str(state.project_root / path))}[/]")
    R.console.print()
    for line in result.splitlines():
        R.console.print(f"  [{R.Theme.SECONDARY}]{escape(line)}[/]")
    R.console.print()
    return True, True


def _cmd_read(arg: str, state: SessionState) -> HandlerResult:
    if not arg:
        R.print_error("Usage: /read <file-path>")
        return True, True
    from .tools import _resolve
    try:
        normalized = str(_resolve(arg).relative_to(state.project_root))
    except ValueError:
        normalized = str(_resolve(arg))
    result = execute_tool("read_file", {"path": normalized})
    if result.startswith("[Error]"):
        R.print_error(result)
        return True, True
    ext_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".jsx": "jsx", ".tsx": "tsx", ".go": "go", ".rs": "rust",
        ".java": "java", ".cs": "csharp", ".cpp": "cpp", ".c": "c",
        ".html": "html", ".css": "css", ".json": "json",
        ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
        ".sh": "bash", ".ps1": "powershell", ".md": "markdown",
        ".sql": "sql", ".xml": "xml",
    }
    suffix = Path(arg).suffix.lower()
    lang = ext_map.get(suffix, "text")
    from rich.syntax import Syntax
    from rich.panel import Panel
    clean_result = _strip_line_number_prefixes(result)
    R.console.print()
    R.console.print(Panel(
        Syntax(clean_result, lang, theme=R.Theme.CODE_THEME, line_numbers=True, word_wrap=True),
        title=f"[bold]{escape(arg)}[/]",
        border_style=R.Theme.BORDER,
    ))
    return True, True


def _cmd_run(arg: str, state: SessionState) -> HandlerResult:
    if not arg:
        R.print_error("Usage: /run <command>")
        return True, True
    R.print_info(f"Running: [bold]{escape(arg)}[/]")
    result = execute_tool("run_command", {"command": arg})
    exit_code_match = re.search(r"(?im)^exit code:\s*(-?\d+)\s*$", result)
    is_err = (
        result.startswith(("[Error]", "[Permission", "[Syntax Error", "[Self-Preservation", "[Safety Error]", "[Scope Error]", "[Recursive Agent Guard]"))
        or (exit_code_match is not None and exit_code_match.group(1) != "0")
    )
    R.print_tool_result(result, "run_command", is_error=is_err)
    return True, True


def _cmd_search(arg: str, state: SessionState) -> HandlerResult:
    if not arg:
        R.print_error("Usage: /search <pattern> [glob]")
        return True, True
    parts = arg.split(maxsplit=1)
    pattern = parts[0]
    glob = parts[1] if len(parts) > 1 else None
    tool_args = {"pattern": pattern}
    if glob:
        tool_args["file_glob"] = glob
    result = execute_tool("search_files", tool_args)
    R.console.print()
    for line in result.splitlines()[:100]:
        R.console.print(f"  [dim]{escape(line)}[/]")
    R.console.print()
    return True, True


def _cmd_new(arg: str, state: SessionState) -> HandlerResult:
    if arg.strip().lower() == "keep":
        state.messages.clear()
        state.chat_urls.clear()
        state.role_threads.clear()
        state.session_id = uuid.uuid4().hex[:12]
        state._force_new_chat_next = True
        R.print_success("Started fresh browser chat thread (retained working-set summary)")
        return True, True
    state.clear_history()
    state._force_new_chat_next = True
    clear_session_memory()
    state._workspace_snapshot = None
    R.print_success("Started fresh chat thread and cleared conversation history")
    return True, True


def _cmd_clear(arg: str, state: SessionState) -> HandlerResult:
    state.clear_history()
    clear_session_memory()
    state._workspace_snapshot = None
    R.print_success("Conversation history and session memory cleared")
    return True, True


def _cmd_compact(arg: str, state: SessionState) -> HandlerResult:
    removed = state.compact_history()
    if removed:
        R.print_success(f"Compacted conversation · removed {removed} older messages (kept {len(state.messages)})")
    else:
        R.print_info("Context is already compact (<= 20 messages)")
    return True, True


def _cmd_diff(arg: str, state: SessionState) -> HandlerResult:
    """Show files modified in this session via git diff."""
    result = execute_tool("run_command", {"command": "git diff --stat HEAD 2>&1; git status --short 2>&1"})
    if "[Error]" in result or "not a git" in result.lower():
        R.print_info("Not a git repo or no changes tracked.")
    else:
        R.console.print()
        for line in result.splitlines():
            R.console.print(f"  [dim]{escape(line)}[/]")
        R.console.print()
    return True, True


def _cmd_undo(arg: str, state: SessionState) -> HandlerResult:
    """Restore the last written file from the LIFO undo stack."""
    try:
        changed, msg, remaining = undo_last_edit()
    except Exception as e:
        R.print_error(f"Undo failed: {e}")
        return True, True

    if not changed:
        R.print_info(msg)
        return True, True

    if msg.startswith("File already deleted:"):
        R.print_info(msg)
    else:
        R.print_success(msg)
    state._workspace_snapshot = None
    R.print_info(f"Remaining undo stack: {remaining} edit(s)")
    return True, True


def _cmd_save(arg: str, state: SessionState) -> HandlerResult:
    """Save session to ~/.wsa-sessions/."""
    if state.turn_count == 0 and not state.messages:
        R.print_info("Nothing to save — no turns taken yet.")
        return True, True
    try:
        path = state.save()
        R.print_success(f"Session saved -> [dim]{escape(path)}[/]")
        R.print_info(f"{state.turn_count} turns, {len(state.messages)} messages")
    except Exception as e:
        R.print_error(f"Save failed: {e}")
    return True, True


def _cmd_load(arg: str, state: SessionState) -> HandlerResult:
    """List or restore a saved session."""
    sessions = state.list_saved_sessions()
    if not sessions:
        R.print_info("No saved sessions found in ~/.wsa-sessions/")
        return True, True
    if not arg:
        from rich.table import Table
        t = Table(
            title="[bold bright_cyan]Saved Sessions[/]",
            border_style="bright_black",
            show_header=True,
            header_style="bold white",
            expand=False,
            padding=(0, 1),
        )
        t.add_column("#", style="dim")
        t.add_column("Name", style="bold yellow")
        t.add_column("Project", style="dim")
        t.add_column("Turns", style="bright_blue")
        t.add_column("Saved", style="dim")
        for i, s in enumerate(sessions, 1):
            t.add_row(str(i), s["name"], s["project"], str(s["turns"]), s["saved_at"])
        R.console.print(t)
        R.print_info("Use: /load <name-or-number>")
        return True, True
    target = None
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(sessions):
            target = sessions[idx]
    else:
        matches = [s for s in sessions if arg.lower() in s["name"].lower()]
        if matches:
            target = matches[0]
    if not target:
        R.print_error(f"No session matching: '{arg}'")
        return True, True
    try:
        state.load(target["file"])
        set_project_root(state.project_root)
        state._workspace_snapshot = None
        R.print_success(
            f"Session loaded: [bold]{escape(target['name'])}[/] "
            f"({state.turn_count} turns, {len(state.messages)} messages · project: {escape(str(state.project_root))})"
        )
    except Exception as e:
        R.print_error(f"Load failed: {e}")
    return True, True


def _cmd_tokens(arg: str, state: SessionState) -> HandlerResult:
    """Show token usage for the current session."""
    total = state.total_tokens_in + state.total_tokens_out
    R.console.print()
    R.console.print(f"  [dim]Tokens in :[/]  [bright_blue]{state.total_tokens_in:,}[/]")
    R.console.print(f"  [dim]Tokens out:[/]  [bright_blue]{state.total_tokens_out:,}[/]")
    R.console.print(f"  [dim]Total     :[/]  [bold bright_blue]{total:,}[/]")
    R.console.print(f"  [dim]Turns     :[/]  [bright_blue]{state.turn_count}[/]")
    if state.turn_count > 0 and total > 0:
        per_turn = total // state.turn_count
        R.console.print(f"  [dim]~per turn :[/]  [bright_blue]{per_turn:,}[/]")
    R.console.print()
    return True, True


def _cmd_exit(arg: str, state: SessionState) -> HandlerResult:
    return True, False


def _cmd_rules(arg: str, state: SessionState) -> HandlerResult:
    """View, add, or clear dynamic engineering rules."""
    from .tools import get_dynamic_rules, _set_dynamic_rule, _dynamic_rules, save_dynamic_rules
    rules = get_dynamic_rules()
    if not arg:
        if not rules:
            R.print_info("No dynamic rules set. Use: /rules add <key> <rule text>  or  /rules clear")
            return True, True
        R.console.print()
        R.console.print("  [bold bright_magenta]Active Dynamic Rules[/]")
        for key, value in rules.items():
            R.console.print(f"    [bold yellow]{escape(key)}[/]: {escape(value)}")
        R.console.print()
        return True, True
    parts = arg.split(maxsplit=2)
    sub = parts[0].lower()
    if sub == "clear":
        _dynamic_rules.clear()
        save_dynamic_rules()
        R.print_success("Cleared all dynamic rules.")
    elif sub == "add" and len(parts) >= 3:
        R.print_success(escape(_set_dynamic_rule(parts[1], parts[2])))
    elif sub in ("remove", "del", "rm") and len(parts) >= 2:
        R.print_success(escape(_set_dynamic_rule(parts[1], "")))
    else:
        R.print_info("Usage: /rules | /rules add <key> <rule> | /rules remove <key> | /rules clear")
    return True, True


def _cmd_tools(arg: str, state: SessionState) -> HandlerResult:
    """Showcase major slash workflows and categorized built-in/custom runtime tools."""
    from .tools import TOOL_DEFINITIONS, load_custom_tools, remove_custom_tool

    custom = load_custom_tools()
    raw_arg = (arg or "").strip()
    if raw_arg.startswith("remove ") or raw_arg.startswith("rm "):
        tname = raw_arg.split(maxsplit=1)[1].strip()
        if remove_custom_tool(tname):
            R.print_success(f"Removed custom tool: {tname}")
        else:
            R.print_error(f"Custom tool not found: {tname}")
        return True, True

    filt = raw_arg.lower()
    R.sync_theme()
    t = R.Theme

    major_workflows = (
        ("/models  (/model)", "Switch active web bridge: claude, deepseek, gemini, chatgpt, venice"),
        ("/account", "Detect/bind Chrome or Brave bridge & inspect live synced site tabs"),
        ("/singleagent", "Default mode — pin turns to one selected site tab"),
        ("/multiagent", "Ordered failover queue (default/custom), sync-gated across open tabs"),
        ("/tools [filter]", "Browse slash workflows and workspace, shell & browser tools"),
        ("/team /plan /verify", "Subagent delegation, deep planning & verification runs"),
        ("/rules /theme /perm", "Dynamic workspace rules, color palettes & approval level"),
        ("/free /diff /undo", "Toggle raw chat mode, review session diffs & rollback edits"),
    )

    tool_categories = (
        (
            "Workspace & Edits",
            {
                "read_file", "get_file_info", "get_file_outline", "inspect_image",
                "write_file", "patch_file", "delete_file", "glob_files",
                "list_dir", "search_files", "rollback",
            },
        ),
        (
            "Shell, Services & Tests",
            {
                "run_command", "run_python", "run_tests", "start_background",
                "install_dependencies", "install_runtime", "detect_toolchain",
                "list_background", "stop_background", "restart_service", "http_request",
            },
        ),
        (
            "Browser Bridge",
            {"browser_tabs", "browser_inspect", "browser_interact"},
        ),
        (
            "Agents, Memory & Customization",
            {
                "spawn_subagent", "remember", "recall", "set_dynamic_rule",
                "list_dynamic_rules", "remove_dynamic_rule",
                "create_custom_tool", "list_custom_tools", "remove_custom_tool",
            },
        ),
    )

    R.console.print()
    R.console.print(
        Rule(
            title=f"[bold {t.PRIMARY}]Workflows & Available Tools[/] [{t.DIM}]({len(TOOL_DEFINITIONS)} total, {len(custom)} custom)[/]",
            align="left",
            style=t.BORDER,
        )
    )

    shown_workflows = [
        (cmd, desc)
        for cmd, desc in major_workflows
        if not filt or filt in cmd.lower() or filt in desc.lower() or "workflow" in filt
    ]
    if shown_workflows:
        R.console.print(f"  [bold {t.PRIMARY}]Major Workflows[/]")
        wf_tbl = Table(box=None, show_header=False, padding=(0, 3, 0, 4), expand=False)
        wf_tbl.add_column("Command", style=f"bold {t.ACCENT}", no_wrap=True)
        wf_tbl.add_column("Use Case", style=t.SECONDARY)
        for cmd, desc in shown_workflows:
            wf_tbl.add_row(escape(cmd), escape(desc))
        R.console.print(wf_tbl)
        R.console.print()

    grouped: dict[str, list[tuple[str, str, bool]]] = {title: [] for title, _ in tool_categories}
    grouped["Custom & Extensions"] = []
    for tool in TOOL_DEFINITIONS:
        fn = tool.get("function", {})
        name = str(fn.get("name", "") or "").strip()
        if not name:
            continue
        raw_desc = (fn.get("description", "").splitlines()[0] if fn.get("description") else "").strip()
        if len(raw_desc) > 60:
            cut = raw_desc[:59]
            if " " in cut:
                cut = cut.rsplit(" ", 1)[0]
            desc = cut.rstrip(" ,;:-(") + "…"
        else:
            desc = raw_desc
        is_custom = name in custom
        cat_match = next((title for title, names in tool_categories if name in names), "Custom & Extensions")
        if filt and filt not in name.lower() and filt not in raw_desc.lower() and filt not in cat_match.lower():
            continue
        grouped[cat_match].append((name, desc, is_custom))

    total_shown_tools = 0
    for cat_title, rows in grouped.items():
        if not rows:
            continue
        total_shown_tools += len(rows)
        R.console.print(f"  [bold {t.PRIMARY}]{escape(cat_title)}[/]")
        tbl = Table(box=None, show_header=False, padding=(0, 2, 0, 2), expand=False)
        tbl.add_column("Tool", style=f"bold {t.ACCENT}", no_wrap=True, width=20)
        tbl.add_column("Origin", no_wrap=True, width=6)
        tbl.add_column("Summary", style=t.SECONDARY)
        for name, desc, is_custom in rows:
            badge = f"[bold {t.WARN}]Custom[/]" if is_custom else f"[{t.DIM}]Core[/]"
            tbl.add_row(escape(name), badge, escape(desc))
        R.console.print(tbl)
        R.console.print()

    if filt and not shown_workflows and total_shown_tools == 0:
        R.print_info(f"No workflows or tools matched '{filt}'. Run /tools without filters to see all.")
    R.console.print(Rule(style=t.BORDER))
    R.console.print()
    return True, True


def _cmd_perm(arg: str, state: SessionState) -> HandlerResult:
    """View or change low-level permission mode (smart / strict / auto)."""
    mode = arg.strip().lower()
    if not mode:
        curr = "auto" if getattr(state, "auto_approve", False) else getattr(state, "permission_mode", "smart")
        R.print_info(f"Current permission mode: [bold]{curr}[/]  (Usage: /perm smart | strict | auto)")
        return True, True
    if mode in ("smart", "strict", "auto"):
        state.permission_mode = mode
        state.auto_approve = (mode == "auto")
        R.print_success(f"Permission mode -> [bold]{mode}[/]")
        if mode == "auto":
            R.print_warn("Approvals bypassed for this run. Executable tools have your account's files and network access.")
    else:
        R.print_error("Valid permission modes: smart, strict, auto")
    return True, True


def _cmd_team(arg: str, state: SessionState) -> HandlerResult:
    """View or configure adaptive scoped subagent orchestration (`/team auto|on|off|dual|single|single-chat|role-chats`)."""
    sub = arg.strip().lower()
    if not sub or sub == "status":
        mode = str(getattr(state, "team_mode", "auto") or "auto")
        t_mode = str(getattr(state, "thread_mode", "single") or "single")
        dual = bool(getattr(state, "dual_lane", False))
        roles = sorted((getattr(state, "role_threads", None) or {}).keys())
        plans = len(getattr(state, "plan_items", None) or [])
        R.print_info(
            f"Team orchestration: [bold]{escape(mode)}[/] | "
            f"Chat flow: [bold]{'single-chat' if t_mode == 'single' else 'role-chats'}[/] | "
            f"Relay lane: [bold]{'dual-lane' if dual else 'single-relay'}[/] | "
            f"Saved plan tasks: {plans} | Role threads: {', '.join(roles) or 'unified/on-demand'}"
        )
        R.print_info("Usage: /team auto | on | off | single-chat | role-chats | dual | single")
        return True, True

    if sub in ("auto", "on", "off"):
        state.team_mode = sub
        if hasattr(state, "auto_checkpoint"):
            state.auto_checkpoint()
        R.print_success(f"Team orchestration mode -> [bold]{sub}[/]")
    elif sub in ("single-chat", "single_chat", "one-chat", "unified"):
        state.thread_mode = "single"
        if hasattr(state, "auto_checkpoint"):
            state.auto_checkpoint()
        R.print_success("Single-chat workflow enabled (all stages reuse one continuous project web chat).")
    elif sub in ("role-chats", "role_chats", "split-chats", "roles"):
        state.thread_mode = "roles"
        if hasattr(state, "auto_checkpoint"):
            state.auto_checkpoint()
        R.print_success("Split role-chats enabled (each subagent role keeps a separate browser chat thread).")
    elif sub in ("dual", "parallel"):
        state.dual_lane = True
        if hasattr(state, "auto_checkpoint"):
            state.auto_checkpoint()
        R.print_success("Dual-lane parallel relay mode enabled (used when 2+ live relays & disjoint tasks exist).")
    elif sub in ("single", "sequential", "seq"):
        state.dual_lane = False
        if hasattr(state, "auto_checkpoint"):
            state.auto_checkpoint()
        R.print_success("Single active relay sequential execution mode enabled.")
    else:
        R.print_error("Usage: /team [auto|on|off|single-chat|role-chats|dual|single|status]")
    return True, True


def _cmd_concept(arg: str, state: SessionState) -> HandlerResult:
    """Configure or inspect pre-planning UI visual concept mockups (`/concept [auto|on|off|show|clear]`)."""
    raw = str(arg or "").strip()
    low = raw.lower()
    if not raw or low in ("status", "show"):
        mode = getattr(state, "visual_concept_mode", "auto")
        concepts = list(getattr(state, "approved_design_concepts", None) or [])
        R.print_info(f"Visual concept gate: mode={mode} | generator=ChatGPT | locked concepts={len(concepts)}")
        for idx, c in enumerate(concepts, 1):
            if not isinstance(c, dict):
                continue
            imgs = ", ".join(c.get("image_paths") or []) or "(web preview only)"
            R.print_info(f"  {idx}. {c.get('title', 'UI View')} [{c.get('provider', '?')}] -> {imgs}")
        return True, True
    if low in ("auto", "on", "off"):
        state.visual_concept_mode = low
        if hasattr(state, "auto_checkpoint"):
            state.auto_checkpoint()
        R.print_success(f"Visual concept gate mode -> {low}")
        return True, True
    if low == "clear":
        state.approved_design_concepts = []
        synced = getattr(state, "_concept_synced_keys", None)
        if isinstance(synced, set):
            synced.clear()
        if hasattr(state, "auto_checkpoint"):
            state.auto_checkpoint()
        R.print_success("Cleared locked UI visual concepts from session.")
        return True, True
    R.print_error("Usage: /concept [auto | on | off | show | clear]")
    return True, True


def _cmd_plan(arg: str, state: SessionState, client: Optional[Any] = None) -> HandlerResult:
    """Inspect, clear, or generate a dependency-ordered execution plan (`/plan <task>`)."""
    clean = arg.strip()
    if not clean:
        items = getattr(state, "plan_items", None) or []
        if items:
            R.print_plan_table(items)
        else:
            R.print_info("No active plan yet. Usage: /plan <task description> | /plan clear")
        return True, True
    if clean.lower() == "clear":
        state.plan_items = []
        if hasattr(state, "auto_checkpoint"):
            state.auto_checkpoint()
        R.print_success("Cleared active execution plan.")
        return True, True
    if client is None:
        R.print_error("No active server client available for /plan generation.")
        return True, True
    from .subagents import run_plan_command
    run_plan_command(clean, state, client)
    return True, True


def _cmd_verify(arg: str, state: SessionState, client: Optional[Any] = None) -> HandlerResult:
    """Run independent verifier QA checks (`/verify [scope]`)."""
    if client is None:
        R.print_error("No active server client available for /verify.")
        return True, True
    from .subagents import run_verify_command
    run_verify_command(arg.strip(), state, client)
    return True, True


def _extract_live_providers(health: Any) -> Tuple[bool, bool, dict]:
    """Return (server_ok, relay_online, providers_map) from a /v1/health response."""
    if not isinstance(health, dict) or not health or health.get("ok") is False:
        return False, False, {}
    relays = health.get("relays") or []
    active_rel = next(
        (r for r in relays if isinstance(r, dict) and r.get("active")),
        None,
    )
    if active_rel is not None:
        rel_online = bool(active_rel.get("online", health.get("relay_connected", True)))
        pmap = (
            dict(active_rel.get("providers"))
            if rel_online and isinstance(active_rel.get("providers"), dict)
            else (dict(health.get("active_providers") or {}) if rel_online else {})
        )
        # In auto multi-browser mode (no explicit browser lock), include live
        # provider tabs open in other online browser relays (e.g. Chrome + Brave).
        any_fallback_online = False
        if not bool(health.get("explicit_relay_lock")):
            for rel in relays:
                if not isinstance(rel, dict) or rel is active_rel or not bool(rel.get("online", True)):
                    continue
                rel_p = rel.get("providers")
                if not isinstance(rel_p, dict):
                    continue
                for pk, pinfo in rel_p.items():
                    if not isinstance(pinfo, dict) or not pinfo.get("has_tab"):
                        continue
                    cur_info = pmap.get(pk)
                    if isinstance(cur_info, dict) and cur_info.get("has_tab"):
                        continue
                    pmap[pk] = dict(pinfo)
                    any_fallback_online = True
        eff_online = rel_online or any_fallback_online
        return True, eff_online, (pmap if eff_online else {})

    pmap = health.get("active_providers") if isinstance(health.get("active_providers"), dict) else {}
    if "relay_connected" in health:
        rel_online = bool(health.get("relay_connected"))
    else:
        rel_online = bool(pmap)
    return True, rel_online, (pmap if rel_online else {})


def _render_multiagent_queue_gate(
    queue: list[str],
    server_ok: bool,
    relay_online: bool,
    pmap: dict,
) -> None:
    """Render each queued website's open-tab & sync readiness cleanly."""
    R.console.print()
    R.console.print(
        Rule(
            title=f"[bold {R.Theme.PRIMARY}]Multiagent Sync Gate[/] [{Theme_dim()}]({len(queue)} queued websites)[/]",
            align="left",
            style=R.Theme.BORDER,
        )
    )
    if not server_ok:
        R.print_warn("Backend status: offline")
    elif not relay_online:
        R.print_warn("Browser extension status: waiting for Sync")

    for idx, pid in enumerate(queue, 1):
        site = PROVIDER_SITES.get(pid, pid)
        pinfo = pmap.get(pid) if isinstance(pmap, dict) else None
        ready = isinstance(pinfo, dict) and bool(pinfo.get("has_tab"))
        if ready:
            bits = []
            if pinfo.get("model_badge"):
                bits.append(str(pinfo["model_badge"]))
            email_val = pinfo.get("email") or pinfo.get("account_email")
            if email_val:
                bits.append(str(email_val))
            extra = f"  [{R.Theme.DIM}]{escape(' · '.join(bits))}[/]" if bits else ""
            R.console.print(
                f"  [{R.Theme.OK}]✓[/] [bold {R.Theme.PRIMARY}]Slot {idx}: {escape(pid):<10}[/] "
                f"[{R.Theme.SECONDARY}]{escape(site)}[/]{extra}"
            )
        else:
            R.console.print(
                f"  [{R.Theme.WARN}]○[/] [bold {R.Theme.PRIMARY}]Slot {idx}: {escape(pid):<10}[/] "
                f"[{R.Theme.DIM}]{escape(site)} — open tab not synced yet[/]"
            )
    R.console.print()


def Theme_dim() -> str:
    return R.Theme.DIM


def _cmd_singleagent(arg: str, state: SessionState, client: Optional[Any] = None) -> HandlerResult:
    """Switch back to default single-website bridge mode (`/singleagent [model]`)."""
    raw = arg.strip()
    target: Optional[str] = None
    if raw:
        norm = normalize_web_model(raw)
        if norm not in PROVIDER_SITES:
            R.print_error(
                f"Unknown model bridge '{raw}'. Choose: claude, deepseek, gemini, chatgpt, or venice."
            )
            return True, True
        target = norm
    active = state.configure_singleagent(model=target)
    eff_client = client if client is not None else _LAST_CLIENT
    if eff_client is not None:
        try:
            health = eff_client.get_health()
            if isinstance(health, dict) and health.get("ok"):
                state.sync_account_from_health(health)
        except Exception:
            pass
    info = get_model_info(active, state=state)
    R.print_success(
        f"Agent mode -> [bold {R.Theme.ACCENT}]singleagent[/] "
        f"[{R.Theme.DIM}](active: {escape(active)} · {escape(info['site'])})[/]"
    )
    return True, True


def _cmd_multiagent(arg: str, state: SessionState, client: Optional[Any] = None) -> HandlerResult:
    """Configure /multiagent failover queue and require synced open tabs before activating."""
    raw = arg.strip()
    queue: Optional[list[str]] = None

    if raw:
        low = raw.lower()
        if low in ("status", "show"):
            cur_mode = getattr(state, "agent_mode", "singleagent")
            cur_q = list(getattr(state, "multiagent_queue", None) or [])
            if cur_mode == "multiagent" and cur_q:
                R.print_info(f"Mode: multiagent | Active: {state.model} | Queue: {' → '.join(cur_q)}")
            else:
                R.print_info(f"Mode: {cur_mode} (use /multiagent to configure a failover queue)")
            return True, True
        if low in ("off", "stop", "single", "singleagent"):
            return _cmd_singleagent("", state, client=client)
        if low == "default":
            queue = list(DEFAULT_MULTIAGENT_QUEUE)
        elif low == "custom":
            queue = TUI.pick_multiagent_custom_queue(state=state)
            if not queue:
                R.print_info("Custom multiagent setup cancelled; keeping current mode.")
                return True, True
        else:
            tokens = [tok for tok in re.split(r"[\s,>→]+", low) if tok and tok != "-"]
            parsed: list[str] = []
            unknown: list[str] = []
            for tok in tokens:
                norm = normalize_web_model(tok)
                if norm not in PROVIDER_SITES:
                    unknown.append(tok)
                elif norm not in parsed:
                    parsed.append(norm)
            if unknown or not parsed:
                bad = ", ".join(unknown) if unknown else raw
                R.print_error(
                    f"Unknown website bridge(s): '{bad}'. Choose from: claude, deepseek, gemini, chatgpt, venice."
                )
                return True, True
            queue = parsed
    else:
        strategy = TUI.pick_multiagent_strategy(state=state)
        if not strategy:
            R.print_info("Multiagent setup cancelled; keeping current mode.")
            return True, True
        if strategy == "default":
            queue = list(DEFAULT_MULTIAGENT_QUEUE)
        else:
            queue = TUI.pick_multiagent_custom_queue(state=state)
            if not queue:
                R.print_info("Custom multiagent setup cancelled; keeping current mode.")
                return True, True

    R.print_info(
        "Allotted queue: " + " → ".join(f"[{idx}] {pid}" for idx, pid in enumerate(queue, 1))
    )
    R.print_info(
        "Open each website in Chrome/Brave (background open tab is fine), select your preferred model, "
        "Think / DeepThink & effort on each site, then click Sync in the WSA extension."
    )

    eff_client = client if client is not None else _LAST_CLIENT
    if eff_client is None:
        from .client import WSAClient
        eff_client = WSAClient(api_base=state.api_base, api_key=state.api_key)

    health: dict = {}
    while True:
        try:
            health = eff_client.get_health()
        except Exception:
            health = {}
        server_ok, relay_online, pmap = _extract_live_providers(health)
        missing = state.missing_queued_sites(providers_map=pmap, queue=queue)
        _render_multiagent_queue_gate(queue, server_ok=server_ok, relay_online=relay_online, pmap=pmap)
        if not missing:
            break
        if not server_ok:
            R.print_warn("WSA backend server is unreachable. Start the server before verifying browser tabs.")
        elif not relay_online:
            R.print_warn("WSA browser extension is not synced yet. Click Sync in the Chrome/Brave popup.")
        else:
            miss_desc = ", ".join(f"{m} ({PROVIDER_SITES.get(m, m)})" for m in missing)
            R.print_warn(f"Missing synced open tab(s): {miss_desc}")
        R.print_info("Background tabs are allowed (no need to keep tabs in the foreground).")
        try:
            R.console.print(
                f"  [bold {R.Theme.ACCENT}]\\[Enter][/] Re-check tabs after clicking Sync   "
                f"[bold {R.Theme.ACCENT}]\\[c][/] Cancel and keep current mode"
            )
            ans = input("  > ").strip().lower().strip("[](). ")
        except (EOFError, KeyboardInterrupt):
            R.console.print()
            R.print_info("Multiagent activation cancelled; keeping current mode.")
            return True, True
        if ans in ("c", "cancel", "q", "quit", "n", "no"):
            R.print_info("Multiagent activation cancelled; keeping current mode.")
            return True, True

    active_q = state.configure_multiagent(queue)
    state.sync_account_from_health(health)
    if hasattr(state, "auto_checkpoint"):
        state.auto_checkpoint()
    R.print_success(
        f"Multiagent active -> [bold {R.Theme.ACCENT}]{' → '.join(active_q)}[/] "
        f"[{R.Theme.DIM}](starting on {escape(state.model)}; auto-failover with full context on limit)[/]"
    )
    return True, True


def _cmd_skills(arg: str, state: SessionState) -> HandlerResult:
    """List bundled WSA skills, read a specific skill, or run SkillSpector on a path."""
    from .task_skills import (
        CATEGORY_LABELS,
        SKILL_SPECS,
        read_skill_content,
        scan_skill_path,
    )

    clean = str(arg or "").strip()
    if not clean or clean.lower() in {"list", "catalog"}:
        R.print_skills_table(SKILL_SPECS, CATEGORY_LABELS, loaded_skills=getattr(state, "loaded_skills", ()))
        return True, True

    if clean.lower().startswith("scan "):
        raw_target = clean[5:].strip().strip('"').strip("'")
        target = Path(raw_target)
        if not target.is_absolute():
            target = (Path(state.project_root) / target).resolve()
        res = scan_skill_path(target)
        verdict = res["verdict"]
        score = res["score"]
        msg = f"SkillSpector verdict: {verdict} (score: {score}/100, files: {res.get('files_scanned', 1)}) — {res['source']}"
        if verdict == "SAFE":
            R.print_success(msg)
        elif verdict == "CAUTION":
            R.print_warn(msg)
        else:
            R.print_error(msg)
        for finding in res.get("findings") or []:
            R.print_warn(
                f"[{finding['severity']}] {finding['source']}:L{finding['line']} ({finding['rule']}): {finding['detail']}"
            )
        return True, True

    text, loaded = read_skill_content(clean, mode="brief", max_chars=8000)
    if loaded:
        R.print_markdown(text)
    else:
        R.print_warn(text)
    return True, True
