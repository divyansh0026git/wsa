"""
wsa — Web Server Agent (agentic coding TUI bridged to claude.ai, chat.deepseek.com, gemini.google.com, chatgpt.com & venice.ai).

Usage:
    wsa                             # interactive REPL (auto-starts backend)
    wsa --project /path             # specify project root
    wsa --model claude              # bridge to open claude.ai tab
    wsa --model deepseek            # bridge to open chat.deepseek.com tab
    wsa --model gemini              # bridge to open gemini.google.com tab
    wsa --model chatgpt             # bridge to open chatgpt.com tab
    wsa --model venice              # bridge to open venice.ai tab (DeepSeek V4.1 Flash Uncensored)
    wsa --account chrome            # bind Chrome browser session
    wsa "one-shot query"            # single-shot mode
    wsa --no-server                 # don't auto-start the backend
"""
from __future__ import annotations
import argparse
import atexit
import subprocess
import sys
import time
from pathlib import Path

# ─── Setup path so imports work when run as script ────────────────────────────
_here = Path(__file__).resolve().parent.parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from wsa_agent.bootstrap import ensure_wsa_dependencies
ensure_wsa_dependencies()

from wsa_agent.session import SessionState, ALL_MODELS
from wsa_agent.client import WSAClient
from wsa_agent.agent import run_turn
from wsa_agent.commands import handle_command
from wsa_agent.tools import set_project_root
from wsa_agent import renderer as R
from wsa_agent import tui as TUI
from rich.markup import escape


# ─── Embedded server manager ──────────────────────────────────────────────────

class _ServerManager:
    """
    Manages the WSA bridge server backend as a child process.

    - Checks if the server is already running (port probe).
    - If not, spawns `python -m wsa_server` using the same venv Python.
    - Waits up to _STARTUP_TIMEOUT seconds for it to accept connections.
    - Registers atexit to terminate the child on clean exit.
    - On KeyboardInterrupt / crash, also terminates cleanly.
    """

    _STARTUP_TIMEOUT = 15   # seconds to wait for server to come up
    _CHECK_INTERVAL  = 0.3  # polling interval while waiting

    def __init__(self, api_base: str):
        self._proc: subprocess.Popen | None = None
        self._we_started = False
        self._log_fh = None
        # Extract host/port from api_base (e.g. http://127.0.0.1:8081/v1)
        import re
        m = re.search(r':(\d+)', api_base)
        self._port = int(m.group(1)) if m else 8081
        self._host = "127.0.0.1"

    def _is_server_up(self) -> bool:
        """Fast TCP probe — returns True if something is listening on the port."""
        import socket
        try:
            with socket.create_connection((self._host, self._port), timeout=1.0):
                return True
        except (ConnectionRefusedError, OSError):
            return False

    def _check_server_authenticated(self) -> tuple[bool, int | None]:
        """Return authentication state and PID reported by the local health endpoint."""
        import json as _json
        import urllib.request
        try:
            request = urllib.request.Request(f"http://{self._host}:{self._port}/v1/health")
            with urllib.request.urlopen(request, timeout=2.0) as response:
                data = _json.loads(response.read().decode("utf-8", errors="replace"))
            if data.get("product") != "wsa" or data.get("status") != "ok":
                return False, None
            pid = data.get("pid")
            return bool(data.get("authenticated")), pid if type(pid) is int and pid > 0 else None
        except Exception:
            return False, None

    def ensure_running(self) -> bool:
        """
        Ensure the server is running. Returns True if the server is reachable
        (whether we started it or it was already running).
        """
        # Locate the Python interpreter (same venv as us)
        python = sys.executable
        # Project root is two levels up from this file: wsa_agent/__main__.py → project root (wsa)
        project_root = Path(__file__).resolve().parent.parent
        if self._is_server_up():
            _, old_pid = self._check_server_authenticated()
            # Cookie authentication is independent of the DOM relay. Never kill
            # a pre-existing process based on a failed HTTP/authentication probe.
            if old_pid is not None:
                return True
            R.print_error(f"Port {self._port} is occupied by an unverifiable backend; it was left running.")
            return False

        # Build launch command: python -m wsa_server (loads config.json automatically)
        cmd = [python, "-m", "wsa_server"]

        R.console.print()
        R.console.print(
            f"  [{R.Theme.ACCENT}]·[/] Starting local backend "
            f"[{R.Theme.DIM}]port {self._port}[/]"
        )

        try:
            # Suppress the subprocess stdout/stderr into a log file so it
            # doesn't bleed into the TUI. We write to a temp log file so the
            # user can inspect it if needed.
            log_path = Path.home() / ".wsa-server.log"
            log_fh = open(log_path, "w", encoding="utf-8")
            self._log_fh = log_fh

            self._proc = subprocess.Popen(
                cmd,
                cwd=str(project_root),
                stdout=log_fh,
                stderr=log_fh,
                # Detach from our console group on Windows so Ctrl+C in wsa
                # doesn't also kill the server mid-request.
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
            self._we_started = True
            atexit.register(self._shutdown)

        except Exception as e:
            if self._log_fh:
                try:
                    self._log_fh.close()
                except Exception:
                    pass
                self._log_fh = None
            R.print_error(f"Failed to start backend: {e}")
            R.print_info("Run [bold].\\wsa.ps1[/] or [bold].\\start.ps1[/] manually in another terminal.")
            return False

        # ── Wait for the server to accept connections ──────────────────────────
        deadline = time.time() + self._STARTUP_TIMEOUT
        dots = 0
        while time.time() < deadline:
            if self._is_server_up():
                # Double-check process didn't die immediately
                if self._proc.poll() is not None:
                    R.print_error(
                        f"Backend exited early (code {self._proc.returncode}). "
                        f"Check [dim]~/.wsa-server.log[/] for details."
                    )
                    return False
                elapsed = time.time() - (deadline - self._STARTUP_TIMEOUT)
                R.print_success(
                    f"Backend ready [{R.Theme.DIM}]· {elapsed:.1f}s · ~/.wsa-server.log[/]"
                )
                return True

            # Check if process died
            if self._proc.poll() is not None:
                R.print_error(
                    f"Backend failed to start (exit {self._proc.returncode}). "
                    f"Check [dim]~/.wsa-server.log[/]"
                )
                return False

            time.sleep(self._CHECK_INTERVAL)
            dots += 1
            if dots % 10 == 0:
                remaining = int(deadline - time.time())
                R.console.print(
                    f"  [dim]  still waiting… ({remaining}s left)[/]",
                    end="\r",
                )

        R.print_error(
            f"Backend didn't respond within {self._STARTUP_TIMEOUT}s. "
            "Proceeding anyway — queries may fail until it's ready."
        )
        return False

    def _shutdown(self):
        """Terminate the child process on exit (atexit handler)."""
        if self._proc and self._proc.poll() is None:
            try:
                if sys.platform == "win32":
                    # Send Ctrl+Break to the new process group
                    import signal
                    self._proc.send_signal(signal.CTRL_BREAK_EVENT)
                    self._proc.wait(timeout=3)
                else:
                    self._proc.terminate()
                    self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None

    def stop(self):
        """Explicit stop — called on clean /exit."""
        self._shutdown()


# Module-level singleton — set in main()
_server_mgr: _ServerManager | None = None

# ─── prompt_toolkit ──────────────────────────────────────────────────────────
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.document import Document
    HAS_PROMPT_TOOLKIT = True
except ImportError:
    HAS_PROMPT_TOOLKIT = False


# ─── Context-aware completer ──────────────────────────────────────────────────

SLASH_COMMANDS = [
    "/help", "/shortcuts", "/model", "/models", "/multiagent", "/singleagent", "/theme", "/project",
    "/status", "/tokens", "/init", "/files", "/read", "/run", "/search",
    "/team", "/plan", "/verify", "/skills",
    "/diff", "/undo", "/rules", "/tools", "/perm", "/account", "/free", "/save", "/load", "/new", "/clear", "/compact", "/exit", "/quit",
]

MODEL_NAMES = [name for name, _ in ALL_MODELS]


if HAS_PROMPT_TOOLKIT:
    def _complete_file_path(prefix: str):
        """Yield Completion objects for files matching prefix under cwd."""
        try:
            from wsa_agent.tools import get_project_root
            root = get_project_root()
            prefix_path = Path(prefix) if prefix else Path("")
            # Resolve search base
            if prefix and not prefix.endswith("/") and not prefix.endswith("\\"):
                parent = prefix_path.parent
                stem   = prefix_path.name
            else:
                parent = prefix_path
                stem   = ""
            search_dir = (root / parent) if not parent.is_absolute() else parent
            if not search_dir.is_dir():
                return
            for entry in sorted(search_dir.iterdir())[:60]:
                rel = str(entry.relative_to(root)).replace("\\", "/")
                name = entry.name
                if stem and not name.lower().startswith(stem.lower()):
                    continue
                suffix = "/" if entry.is_dir() else ""
                display_meta = "dir" if entry.is_dir() else entry.suffix or "file"
                yield Completion(
                    rel + suffix,
                    start_position=-len(prefix),
                    display=name + suffix,
                    display_meta=display_meta,
                )
        except (PermissionError, OSError):
            pass

    class _ContextCompleter(Completer):
        """
        Context-aware Tab completion:
          /model   <Tab>  → claude | deepseek | gemini | chatgpt | venice
          /multiagent     → default | custom | queue list
          /account <Tab>  → chrome | brave | list | rotate
          /read    <Tab>  → file paths (prefix)
          <Tab> alone     → slash commands
        """
        def get_completions(self, document: Document, complete_event):
            text = document.text_before_cursor
            stripped = text.lstrip()

            # Context: after /model or /singleagent
            if (
                stripped.startswith("/model ")
                or stripped.startswith("/m ")
                or stripped.startswith("/singleagent ")
            ):
                word = stripped.split(None, 1)[-1] if " " in stripped else ""
                for name in MODEL_NAMES:
                    if name.startswith(word.lower()) or word.lower() in name.lower():
                        desc = next((d for n, d in ALL_MODELS if n == name), "")
                        clean_desc = desc.split("—", 1)[-1].strip() if "—" in desc else desc
                        yield Completion(name, start_position=-len(word),
                                         display=name, display_meta=clean_desc[:54])
                return

            # Context: after /multiagent
            if stripped.startswith("/multiagent ") or stripped.startswith("/multi "):
                word = stripped.split(None, 1)[-1] if " " in stripped else ""
                for opt, desc in (
                    ("default", "Preset queue: claude -> deepseek -> gemini -> chatgpt -> venice"),
                    ("custom", "Interactive slot-by-slot queue builder"),
                ):
                    if opt.startswith(word.lower()):
                        yield Completion(opt, start_position=-len(word), display=opt, display_meta=desc)
                return

            # Context: after /theme
            if stripped.startswith("/theme "):
                from wsa_agent.theme import list_themes
                word = stripped.split(None, 1)[-1] if " " in stripped else ""
                for pal in list_themes():
                    if pal.id.startswith(word.lower()):
                        yield Completion(pal.id, start_position=-len(word),
                                         display=pal.id, display_meta=pal.name)
                return

            # Context: after /account
            if stripped.startswith("/account ") or stripped.startswith("/acc "):
                word = stripped.split(None, 1)[-1] if " " in stripped else ""
                for opt, desc in (
                    ("chrome", "Bind Chrome browser bridge"),
                    ("brave", "Bind Brave browser bridge"),
                    ("list", "Inspect synced browsers, sites, accounts & models"),
                    ("rotate", "Rotate to next browser or account"),
                    ("auto", "Clear explicit browser lock"),
                ):
                    if opt.startswith(word.lower()):
                        yield Completion(opt, start_position=-len(word), display=opt, display_meta=desc)
                return

            # Context: after /read or /r — file path completion
            if stripped.startswith("/read ") or stripped.startswith("/r "):
                prefix = stripped.split(None, 1)[-1] if " " in stripped else ""
                yield from _complete_file_path(prefix)
                return

            # Context: after /search — recent search terms (no-op, just slash cmds)
            # Context: slash commands
            if stripped.startswith("/") and " " not in stripped:
                word = stripped
                for cmd in SLASH_COMMANDS:
                    if cmd.startswith(word):
                        yield Completion(cmd, start_position=-len(word), display=cmd)
                return


def _hex_ansi(hex_color: str) -> str:
    h = (hex_color or "#94a3b8").lstrip("#")
    if len(h) == 6:
        try:
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            return f"\033[38;2;{r};{g};{b}m"
        except ValueError:
            pass
    return "\033[37m"


def _cycle_permission_mode(state: SessionState) -> str:
    order = ("auto", "smart", "strict")
    curr = getattr(state, "permission_mode", "auto")
    nxt = order[(order.index(curr) + 1) % len(order)] if curr in order else "auto"
    state.permission_mode = nxt
    state.auto_approve = (nxt == "auto")
    return nxt


def _make_prompt_session(state: SessionState | None = None) -> "PromptSession | None":
    if not HAS_PROMPT_TOOLKIT:
        return None
    try:
        from prompt_toolkit.application.current import get_app
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style as PTStyle
        from wsa_agent.theme import get_theme

        t = get_theme()
        hist_path = Path.home() / ".wsa_history"
        try:
            history = FileHistory(str(hist_path))
        except Exception:
            history = InMemoryHistory()

        pt_style = PTStyle.from_dict({
            "": f"{t.text} bg:default",
            "bottom-toolbar": "noreverse bg:default",
            "bottom-toolbar.text": "noreverse bg:default",
            "auto-suggest": t.subtle,
            "completion-menu": f"bg:#19191b {t.secondary_text}",
            "completion-menu.completion.current": f"bg:{t.claude} #141415 bold",
            "scrollbar.background": "bg:#141415",
            "scrollbar.button": f"bg:{t.border}",
        })

        kb = KeyBindings()
        if state is not None:
            @kb.add("s-tab")
            def _on_shift_tab(event):
                _cycle_permission_mode(state)
                event.app.invalidate()

            @kb.add("?", filter=Condition(lambda: get_app().current_buffer.text == ""))
            def _on_question_mark(event):
                event.app.exit(result="/shortcuts")

            @kb.add("escape", "m")
            def _on_alt_m(event):
                event.app.exit(result="/model")

        session = PromptSession(
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=_ContextCompleter(),
            complete_while_typing=False,
            enable_history_search=True,
            key_bindings=kb,
            style=pt_style,
        )
        return session
    except Exception:
        return None


def _get_input(session, prompt_text: str, bottom_toolbar=None) -> str:
    """Get user input via prompt_toolkit or fallback to input()."""
    if session and HAS_PROMPT_TOOLKIT:
        from prompt_toolkit.formatted_text import ANSI
        try:
            kwargs = {}
            if bottom_toolbar is not None:
                if callable(bottom_toolbar):
                    kwargs["bottom_toolbar"] = lambda: ANSI(str(bottom_toolbar()))
                elif isinstance(bottom_toolbar, str):
                    kwargs["bottom_toolbar"] = ANSI(bottom_toolbar)
                else:
                    kwargs["bottom_toolbar"] = bottom_toolbar
            return R.sanitize_surrogates(session.prompt(ANSI(prompt_text), **kwargs))
        except KeyboardInterrupt:
            raise
        except EOFError:
            raise
        except Exception:
            pass
    import re as _re
    plain = _re.sub(r'\033\[[0-9;]*m', '', prompt_text)
    try:
        import readline
    except ImportError:
        pass
    return R.sanitize_surrogates(input(plain))


# ─── Prompt builder ───────────────────────────────────────────────────────────

def _prompt_rule_width() -> int:
    try:
        return R.ui_frame_width()
    except Exception:
        return 76


def _format_toolbar_account(state: SessionState) -> str:
    """Return a clean dynamic account string for the bottom toolbar."""
    lbl = (getattr(state, "active_account_label", "") or "").strip()
    email = (getattr(state, "active_account_email", "") or "").strip()
    if lbl and email:
        return f"{lbl} · {email}"
    return lbl or email or ""


def _build_bottom_toolbar(state: SessionState) -> str:
    """Live permission corner with complete, width-aware model/account rows."""
    from wsa_agent.theme import get_theme
    t = get_theme()
    rst = "\033[0m"
    hair = _hex_ansi("#52525b" if t.border == "#3f3f46" else t.border)
    dim = _hex_ansi(t.subtle)
    accent = _hex_ansi(t.claude)
    ok_col = _hex_ansi(t.success)

    mode = getattr(state, "permission_mode", "auto")
    if mode == "auto":
        mode_col = _hex_ansi(t.auto_accept)
        mode_txt = f"{R.ui_glyph('mode_bypass')} bypass permissions on"
    elif mode == "smart":
        mode_col = _hex_ansi(t.plan_mode)
        mode_txt = f"{R.ui_glyph('mode_single')} accept edits on"
    else:
        mode_col = _hex_ansi(t.permission)
        mode_txt = f"{R.ui_glyph('mode_single')} confirm all actions"

    model_label = R.friendly_model_name(state.model)
    web_badge = (getattr(state, "active_web_model", "") or "").strip()
    if web_badge and web_badge != model_label:
        model_label = f"{model_label} · {web_badge}"
    if getattr(state, "agent_mode", "singleagent") == "multiagent":
        q = list(getattr(state, "multiagent_queue", None) or [])
        if q:
            q_short = "→".join(x[:3] if len(x) > 6 else x for x in q)
            model_label = f"{model_label} · multi[{q_short}]"
    # Keep permissions in their own corner; never trade account/model data for
    # fitting a single status line. Rich's cell sizing handles wide characters.
    from rich.cells import cell_len
    from rich.text import Text
    width = max(1, (getattr(R.console, "width", 80) or 80) - 4)

    def rows(label, value, color):
        text = f"{label}{value}".replace("\r", " ").replace("\n", " ")
        return [f"  {color}{part.plain}{rst}" for part in Text(text).wrap(R.console, width)]

    shortcut = " (Shift+Tab)"
    if cell_len(mode_txt + shortcut) > width:
        shortcut = ""
    permission = mode_txt + shortcut
    if mode == "auto" and width >= 64:
        permission += " · FULL PERMISSIONS"
    project = str(getattr(state, "project_name", "") or "workspace")
    turns = getattr(state, "turn_count", 0) or 0
    context = f"{project} · {turns} {'turn' if turns == 1 else 'turns'} · /help"
    if cell_len(permission + "   " + context) <= width:
        permission += " " * (width - cell_len(permission + context)) + context
    lines = [f"  {hair}{'─' * width}{rst}"]
    lines.extend(rows("", permission, mode_col))
    lines.extend(rows("Model: ", model_label, accent))
    account = _format_toolbar_account(state)
    browser = str(getattr(state, "active_browser", "") or "")
    if browser and browser not in account:
        account = f"{browser} · {account}" if account else browser
    lines.extend(rows("Account: ", account or "not connected", ok_col if account else dim))
    if context not in permission:
        lines.extend(rows("", context, dim))
    return "\n".join(lines)



def _build_prompt(state: SessionState) -> str:
    from wsa_agent.theme import get_theme
    t = get_theme()
    rst, bold, dim = "\033[0m", "\033[1m", "\033[2m"
    hair = _hex_ansi("#52525b" if t.border == "#3f3f46" else t.border)
    accent = _hex_ansi(t.claude)
    rule = "─" * _prompt_rule_width()
    chev = R.ui_glyph("chevron")
    hint = f" {dim}(Enter to continue refinement pass){rst}" if getattr(state, "pending_refinement_pass", False) else ""
    return f"\n{hair}  {rule}{rst}\n  {accent}{bold}{chev}{rst}{hint} "


# ─── Startup banner ───────────────────────────────────────────────────────────

def _print_welcome(state: SessionState, server_ok: bool) -> None:
    """Print the compact Claude Code-style startup card."""
    R.print_welcome(state, server_ok)


# ─── Session auto-save / resume ───────────────────────────────────────────────

def _autosave(state: SessionState) -> None:
    if state.turn_count == 0:
        return
    try:
        state.save()
        R.print_info(f"Session auto-saved  ({state.turn_count} turns · {state.session_duration})")
    except Exception:
        pass


def _try_resume(state: SessionState) -> bool:
    """Offer to resume the last session for this project with a quiet inline prompt."""
    try:
        sessions = state.list_saved_sessions()
        project_str = str(state.project_root)
        try:
            target_root = Path(state.project_root).resolve()
        except Exception:
            target_root = None
        matches = [
            s for s in sessions
            if s.get("project") == project_str
            or str(s.get("project") or "").lower() == project_str.lower()
            or (target_root and s.get("project") and Path(s["project"]).resolve() == target_root)
        ]
        if not matches:
            return False
        latest = matches[0]
        t_n = latest["turns"]
        turn_word = "turn" if t_n == 1 else "turns"
        R.console.print(
            f"  [dim]{R.ui_glyph('bullet')} Saved session ({t_n} {turn_word}, {latest['saved_at']}).[/]"
        )
        R.console.print(
            f"    [bold {R.Theme.ACCENT}]\\[y][/] Resume saved session   "
            f"[bold {R.Theme.ACCENT}]\\[n / Enter][/] Start a fresh session"
        )
        R.console.print(f"  [dim]Resume? {escape('[y/N]')}:[/] ", end="")
        ans = input().strip().lower()
        if ans in ("y", "yes"):
            state.load(latest["file"])
            R.print_success(
                f"Resumed {escape(latest['name'])} ({len(state.messages)} messages, {state.turn_count} turns)"
            )
            return True
    except (EOFError, KeyboardInterrupt):
        R.console.print()
    except Exception:
        pass
    return False


# ─── One-shot mode ────────────────────────────────────────────────────────────

def _ensure_win32_processed_input() -> None:
    """Restore Windows ENABLE_PROCESSED_INPUT (0x0001) on stdin so Ctrl+C triggers SIGINT."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        if not handle or handle == -1:
            return
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            if not (mode.value & 0x0001):
                kernel32.SetConsoleMode(handle, mode.value | 0x0001)
    except Exception:
        pass


def run_oneshot(query: str, state: SessionState, client: WSAClient):
    _ensure_win32_processed_input()
    R.print_user_message(query)
    try:
        result = run_turn(query, state, client)
        if not result.get("ok", False):
            sys.exit(130 if result.get("aborted_reason") == "interrupted" else 1)
    except KeyboardInterrupt:
        R.console.print()
        R.print_warn("Interrupted by user.")
        _autosave(state)
        sys.exit(130)


# ─── Interactive REPL ─────────────────────────────────────────────────────────

def run_repl(state: SessionState, client: WSAClient, *, permission_override: str | None = None):
    session = _make_prompt_session(state)
    health = client.get_health()
    server_ok = bool(health) if health else client.check_connection()
    if health:
        state.sync_account_from_health(health)
    state._ui_connection = (("Server ready · browser relay active" if health.get("dom_relay_active")
                             else "Server ready · no active browser relay") if health
                            else ("Server reachable · relay status unknown" if server_ok else "Server offline"))

    # ── Welcome card ──────────────────────────────────────────────────────────
    _print_welcome(state, server_ok)

    # ── Offer to resume ───────────────────────────────────────────────────────
    _try_resume(state)
    if permission_override is not None:
        state.permission_mode = permission_override
        state.auto_approve = permission_override == "auto"

    last_interrupt_ts = 0.0
    while True:
        prompt = _build_prompt(state)
        try:
            raw = _get_input(
                session,
                prompt,
                bottom_toolbar=lambda: _build_bottom_toolbar(state),
            )
            _ensure_win32_processed_input()
        except KeyboardInterrupt:
            _ensure_win32_processed_input()
            now = time.time()
            R.console.print()
            if (now - last_interrupt_ts) <= 2.5:
                R.print_info("Exiting on double Ctrl+C. Goodbye.")
                _autosave(state)
                break
            last_interrupt_ts = now
            R.print_info(
                "Interrupted — press [bold]Ctrl+C[/] again within 2.5s or type [bold]/exit[/] to quit."
            )
            continue
        except EOFError:
            R.console.print()
            R.print_info("Goodbye.")
            _autosave(state)
            break

        text = raw.strip()
        if not text:
            if getattr(state, "pending_refinement_pass", False):
                state.pending_refinement_pass = False
                text = "continue"
                R.print_info("Auto-continuing exact refinement pass…")
            else:
                continue
        else:
            state.pending_refinement_pass = False
        last_interrupt_ts = 0.0
        if R.console.is_terminal:
            try:
                sys.stdout.write("\r\x1b[J")
                sys.stdout.flush()
            except Exception:
                pass
            R.console.print()
        if text == "?":
            text = "/shortcuts"

        # ── Slash commands ────────────────────────────────────────────────────
        if text.startswith("/"):
            handled, should_continue = handle_command(text, state, client=client)
            if handled:
                if not should_continue:
                    R.console.print()
                    R.print_info("Goodbye.")
                    _autosave(state)
                    break
                if text.lower().startswith("/theme"):
                    session = _make_prompt_session(state)
                elif text.lower().startswith("/account"):
                    h = client.get_health()
                    if h:
                        state.sync_account_from_health(h)
                set_project_root(state.project_root)
                continue
            # Fuzzy-match unknown command
            cmd_word = text.split()[0].lower()
            close = [c for c in SLASH_COMMANDS if c.startswith(cmd_word[:3])]
            if close:
                R.print_warn(
                    f"Unknown: [bold]{escape(cmd_word)}[/]  "
                    f"Did you mean: {', '.join(f'[bold]{c}[/]' for c in close[:3])}?"
                )
            else:
                R.print_warn(f"Unknown: [bold]{escape(cmd_word)}[/]  — type [bold]/help[/]")
            continue

        # ── Agent turn ────────────────────────────────────────────────────────
        _ensure_win32_processed_input()
        try:
            run_turn(text, state, client)
        except KeyboardInterrupt:
            _ensure_win32_processed_input()
            last_interrupt_ts = time.time()
            R.console.print()
            R.print_warn("Turn interrupted — ready for next prompt (press Ctrl+C again to exit).")
            _autosave(state)


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="wsa",
        description="WSA (Web Server Agent) — agentic coding CLI bridged to claude.ai, chat.deepseek.com, gemini.google.com, chatgpt.com & venice.ai",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  wsa                              # launch REPL
  wsa --model claude               # bridge to open claude.ai tab
  wsa --model deepseek             # bridge to open chat.deepseek.com tab
  wsa --model gemini               # bridge to open gemini.google.com tab
  wsa --model chatgpt              # bridge to open chatgpt.com tab
  wsa --model venice               # bridge to open venice.ai tab
  wsa --account chrome             # bind Chrome browser session
  wsa --account brave              # bind Brave browser session
  wsa --project C:\\myapp           # specify project directory
  wsa "explain this codebase"      # one-shot query
        """,
    )
    parser.add_argument("query",         nargs="*",              help="One-shot query (omit for REPL)")
    parser.add_argument("--project","-p",default=None,          help="Project directory (default: cwd)")
    parser.add_argument("--model",  "-m",default="gemini",      help="Web bridge: claude | deepseek | gemini | chatgpt | venice (select exact model & thinking in browser)")
    parser.add_argument("--account","-a",default=None,          help="Bind browser relay (e.g. chrome, brave)")
    parser.add_argument("--continue", "-c", dest="continue_session", action="store_true", help="Resume last saved workspace session and web chat thread")
    parser.add_argument("--api-base",    default="http://127.0.0.1:8081/v1", help="API base URL")
    parser.add_argument("--api-key",     default="sk-wsa",                  help="API key")
    parser.add_argument("--models",      action="store_true",   help="List available web bridges and exit")
    parser.add_argument("--version","-v",action="store_true",   help="Show version")
    parser.add_argument("--no-server",   action="store_true",   help="Don't auto-start the backend server")
    permission_options = parser.add_mutually_exclusive_group()
    permission_options.add_argument("--permissions", choices=("smart", "strict", "auto"), default=None,
                        help="Approval policy (default: smart). auto explicitly bypasses approvals for this run")
    permission_options.add_argument("--full-permissions", dest="permissions", action="store_const", const="auto",
                        help="Bypass WSA approval prompts for this run (same as --permissions auto; no OS elevation)")
    args = parser.parse_args()

    if args.version:
        from wsa_agent import __version__
        print(f"wsa {__version__}")
        return

    if args.models:
        R.print_models_table(ALL_MODELS)
        return

    # ── Build state ───────────────────────────────────────────────────────────
    project_root = Path(args.project).resolve() if args.project else Path.cwd()
    if not project_root.is_dir():
        R.print_error(f"Project directory not found: {args.project}")
        sys.exit(1)

    state = SessionState(
        project_root=project_root,
        api_base=args.api_base,
        api_key=args.api_key,
    )

    if args.model:
        if not state.set_model(args.model):
            R.print_warn(f"Unknown model bridge '{args.model}', using 'gemini'. Valid: claude, deepseek, gemini, chatgpt, venice.")

    set_project_root(state.project_root)

    if getattr(args, "continue_session", False):
        saved_p = SessionState.find_project_session_path(state.project_root)
        if saved_p:
            try:
                state.load(saved_p)
                cur_url = state.active_chat_url or ""
                if "/a/chat/s/" in cur_url:
                    tid = cur_url.rsplit("/a/chat/s/", 1)[-1][:12]
                elif "/chat/agent/" in cur_url:
                    tid = cur_url.rsplit("/chat/agent/", 1)[-1].split("?", 1)[0][:12]
                elif "/chat/classic/" in cur_url:
                    tid = cur_url.rsplit("/chat/classic/", 1)[-1].split("?", 1)[0][:12]
                elif "/chat/" in cur_url:
                    tid = cur_url.rsplit("/chat/", 1)[-1][:12]
                elif "/app/" in cur_url:
                    tid = cur_url.rsplit("/app/", 1)[-1][:12]
                elif "/c/" in cur_url:
                    tid = cur_url.rsplit("/c/", 1)[-1][:12]
                else:
                    tid = "new"
                R.print_success(f"Resumed workspace session ({state.turn_count} turns, thread={tid})")
            except Exception as e:
                R.print_warn(f"Could not resume saved session: {e}")

    if args.permissions:
        state.permission_mode = args.permissions
        state.auto_approve = args.permissions == "auto"
    if state.permission_mode == "auto":
        R.print_warn("Approvals bypassed for this run. Executable tools have your account's files and network access.")

    # ── Auto-start backend server if needed ───────────────────────────────────
    global _server_mgr
    if not args.no_server:
        _server_mgr = _ServerManager(api_base=state.api_base)
        if not _server_mgr.ensure_running():
            sys.exit(1)

    # ── Build client ──────────────────────────────────────────────────────────
    client = WSAClient(
        api_base=state.api_base,
        api_key=state.api_key,
        timeout=300,
    )

    if args.account:
        if args.account.lower() in ("brave", "chrome", "edge", "auto"):
            res = client.bind_relay(args.account, session_id=state.session_id)
            if not res.get("ok"):
                res = client.switch_account(args.account, session_id=state.session_id)
        else:
            res = client.switch_account(args.account, session_id=state.session_id)
            if not res.get("ok") and not args.account.isdigit():
                res = client.bind_relay(args.account, session_id=state.session_id)
        if res.get("ok"):
            state.sync_account_from_res(res)
            R.print_success(f"Bound browser -> {escape(state.account_display or state.active_account_label or args.account)}")
        else:
            R.print_warn(
                f"Browser '{args.account}' not bound yet ({res.get('error', 'not registered')}); "
                "open claude.ai, chat.deepseek.com, gemini.google.com, chatgpt.com, or venice.ai with the WSA extension synced."
            )

    # ── Run ───────────────────────────────────────────────────────────────────
    try:
        if args.query:
            run_oneshot(" ".join(args.query), state, client)
        else:
            run_repl(state, client, permission_override=args.permissions)
    finally:
        try:
            from .tools import cleanup_background_processes
            cleanup_background_processes()
        except Exception:
            pass
        # On any exit path, cleanly stop the server if we started it
        if _server_mgr and _server_mgr._we_started:
            _server_mgr.stop()


if __name__ == "__main__":
    main()
