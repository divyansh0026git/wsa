"""
Filesystem + shell tools for the agent.
Claude Code-style: read-before-write, verify-after-edit, byte limits,
glob search, file info, multi-hunk patch, session memory.
"""
from __future__ import annotations
import ast
import atexit
import base64
import os
import re
import shlex
import shutil
import socket
import struct
import sys
import json
import subprocess
import fnmatch
import ipaddress
import tempfile
import threading
import time
import traceback
import difflib
import errno
import signal
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from contextvars import ContextVar
from typing import Any, Optional, List, Dict, Set, Tuple

_REAL_POPEN_CLS = subprocess.Popen

from .context import FileContextLedger, extract_file_outline, format_file_outline

_SELF_PKG_DIR: Path = Path(__file__).resolve().parent
_SELF_REPO_ROOT: Path = _SELF_PKG_DIR.parent
_SELF_SERVER_DIR: Path = (_SELF_REPO_ROOT / "wsa_server").resolve()

_project_root: Path = Path.cwd()
_bridge_api_base: str = "http://127.0.0.1:8081"
_bridge_api_key: str = ""


def set_bridge_endpoint(api_base: str = "", api_key: str = "") -> None:
    """Configure the local proxy endpoint used by live browser inspection tools."""
    global _bridge_api_base, _bridge_api_key
    base = str(api_base or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    if base:
        _bridge_api_base = base
    if api_key is not None:
        _bridge_api_key = str(api_key or "")

# ─── File backups (for /undo) & multi-file edit checkpoints (for rollback) ───
_file_backups: Dict[str, Optional[str]] = {}   # path -> previous content (most recent per path)
_backup_order: List[Tuple[str, Optional[str]]] = []  # insertion order for undo stack
_checkpoints: List[Dict[str, Any]] = []
_checkpoint_seq: int = 0
_active_checkpoint: ContextVar = ContextVar("wsa_edit_checkpoint", default=None)
_filesystem_lock = threading.RLock()

# ─── Managed background processes started by WSA in this session ──────────────
_bg_lock = threading.Lock()
_bg_processes: Dict[str, Dict[str, Any]] = {}
_bg_seq: int = 0
_bg_log_session_dir: Optional[Path] = None

# ─── Pending multimodal visual attachments (for inspect_image) ───────────────
_pending_images_lock = threading.Lock()
_pending_images: List[Dict[str, str]] = []


def consume_pending_images() -> List[Dict[str, str]]:
    """Drain and return any pending image attachments queued by inspect_image."""
    with _pending_images_lock:
        items = list(_pending_images)
        _pending_images.clear()
    return items

# ─── Session memory & active FileContextLedger ────────────────────────────────
_session_memory: Dict[str, str] = {}
_default_file_ledger = FileContextLedger()
_active_file_ledger: ContextVar = ContextVar("wsa_file_ledger", default=_default_file_ledger)
_strict_read_before_edit: ContextVar = ContextVar("wsa_strict_edit", default=False)


def set_active_file_ledger(ledger: Optional[FileContextLedger]) -> FileContextLedger:
    """Bind the session FileContextLedger used by read/outline/write/patch tools."""
    prev = _active_file_ledger.get()
    _active_file_ledger.set(ledger if ledger is not None else _default_file_ledger)
    return prev


def set_strict_read_before_edit(enabled: bool) -> bool:
    """Toggle strict read-before-edit enforcement for agentic turns."""
    prev = _strict_read_before_edit.get()
    _strict_read_before_edit.set(bool(enabled))
    return prev


def _rel_posix(p: Path) -> str:
    with _filesystem_lock:
        root = _project_root
    try:
        return str(p.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")

# ─── Read-before-edit tracking (safety guard) ─────────────────────────────────
_read_files_set: Set[str] = set()    # Tracks files read this session

# ─── Limits ───────────────────────────────────────────────────────────────────
MAX_READ_LINES = 800
MAX_READ_BYTES = 48_000
MAX_SEARCH_FILE_BYTES = 1_000_000
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", ".env",
    "node_modules", "bower_components", "__pycache__", "__pypackages__",
    ".mypy_cache", ".ruff_cache", "dist", "build", ".eggs",
    ".idea", ".vs", ".vscode", ".next", ".nuxt", ".svelte-kit",
    ".vercel", ".output", ".turbo", ".parcel-cache", ".sass-cache",
    "coverage", "htmlcov", ".nyc_output", ".pytest_cache", ".hypothesis",
    ".tox", ".nox", ".cache", "target", "out", "bin", "obj",
    "Pods", "DerivedData", ".gradle", ".dart_tool", "tmp", ".tmp",
    # Browser profiles / Chromium user-data / local runtime & auth folders
    "chrome-profiles", "brave-profiles", "edge-profiles", "firefox-profiles",
    "browser-profiles", "playwright-data", ".playwright",
    "accounts", ".wsa-sessions", ".gwa-sessions",
    "Cache", "Code Cache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
    "GPUCache", "GrShaderCache", "ShaderCache", "Service Worker",
    "IndexedDB", "Local Storage", "Session Storage", "WebStorage",
    "blob_storage", "component_crx_cache", "Crashpad", "BrowserMetrics",
    "Affiliation Database", "AutofillAiModelCache", "AutofillStrikeDatabase",
    "BudgetDatabase", "Crowd Deny", "ClientCertificates", "Safe Browsing",
    "CertificateRevocation", "ActorSafetyLists", "CaptchaProviders", "WidevineCdm",
}

IGNORE_DIR_PATTERNS = {
    "*.egg-info",
    "*.dist-info",
    "*-profiles",
    ".venv*",
    "venv*",
    "OptimizationGuide*",
}

IGNORE_FILES_PATTERNS = {
    "*.pyc", "*.pyo", "*.pyd", "*.so", "*.dll", "*.exe", "*.dylib",
    "*.bin", "*.dat", "*.pma", "*.dmp",
    "*.sqlite", "*.sqlite3", "*.db", "*.db-journal", "*.db-wal", "*.db-shm",
    "*-journal", "*-wal", "*-shm",
    "*.lock", "*.log", "*.min.js", "*.min.css", "*.map", "*.tsbuildinfo", "*.wasm",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock", "poetry.lock", "uv.lock",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.ico",
    "*.woff", "*.woff2", "*.ttf", "*.eot", "*.otf",
    "*.mp4", "*.mp3", "*.wav",
    "*.zip", "*.tar", "*.gz", "*.7z", "*.rar",
    "*.pdf", "*.doc", "*.docx",
    "*.pem", "*.key", "*.p12", "*.pfx",
    "wsa-auth.json", ".wsa_relay_sessions.json",
    ".wsa_memory.json", ".gwa_memory.json",
    ".wsa_rules.json", ".gwa_rules.json",
    ".wsa_custom_tools.json", ".gwa_custom_tools.json",
    ".env", ".env.*", ".DS_Store", "Thumbs.db",
}

_GITIGNORE_CACHE: Dict[str, Tuple[int, Tuple[Tuple[bool, bool, bool, str], ...]]] = {}


def _effective_ignore_dirs(root: Optional[Path] = None) -> Set[str]:
    """Return ignored directory names for workspace traversal."""
    return set(IGNORE_DIRS)


def _detect_python_executable(root: Optional[Path] = None) -> str:
    """Prefer the active project's own virtualenv interpreter over WSA's host venv."""
    base = root or _project_root
    candidates = (
        base / ".venv" / "Scripts" / "python.exe",
        base / ".venv" / "bin" / "python",
        base / "venv" / "Scripts" / "python.exe",
        base / "venv" / "bin" / "python",
        base / ".env" / "Scripts" / "python.exe",
        base / ".env" / "bin" / "python",
    )
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return sys.executable


_NUMBERED_LINE_PREFIX_RE = re.compile(r"^\s*(\d+)\s*\|")


def _describe_omitted_lines(lines: List[str], omit_start_idx: int, omit_end_idx: int) -> str:
    """Format exact omitted 1-based line numbers (or file line numbers for `read_file` chunks)."""
    out_start = omit_start_idx + 1
    out_end = omit_end_idx
    if 0 <= omit_start_idx < omit_end_idx <= len(lines):
        m_first = _NUMBERED_LINE_PREFIX_RE.match(lines[omit_start_idx])
        m_last = _NUMBERED_LINE_PREFIX_RE.match(lines[omit_end_idx - 1])
        if m_first and m_last:
            f_start = int(m_first.group(1))
            f_end = int(m_last.group(1))
            if f_start <= f_end:
                return f"file line {f_start}" if f_start == f_end else f"file lines {f_start}-{f_end}"
    return f"line {out_start}" if out_start == out_end else f"lines {out_start}-{out_end}"


def truncate_text_with_line_range(
    text: str,
    max_chars: int = 24_000,
    label: str = "output",
    notice_verb: str = "truncated for WSA web budget",
    extra_hint: str = "",
) -> str:
    """Bound long text keeping head + tail and reporting exact omitted 1-based line numbers."""
    if not text or len(text) <= max_chars:
        return text
    max_chars = max(512, int(max_chars))
    lines = text.splitlines(keepends=True)
    total_lines = len(lines)
    notice_reserve = 210 + len(label) + len(notice_verb) + len(extra_hint)
    usable = max(256, max_chars - notice_reserve)
    head_budget = max(128, int(usable * 0.52))
    tail_budget = max(128, usable - head_budget)

    head_chunks: List[str] = []
    head_chars = 0
    head_end = 0
    for idx, ln in enumerate(lines):
        if idx > 0 and head_chars + len(ln) > head_budget:
            break
        if len(ln) > head_budget and idx == 0:
            break
        head_chunks.append(ln)
        head_chars += len(ln)
        head_end = idx + 1

    # Single giant line fallback when line 0 alone exceeds budget
    tag = f"{label} {notice_verb}".strip()
    if head_end == 0:
        head_len = min(head_budget, len(text) // 2)
        tail_len = min(tail_budget, max(64, len(text) - head_len - 1))
        omitted_chars = max(0, len(text) - head_len - tail_len)
        hint = f" {extra_hint}" if extra_hint else ""
        notice = (
            f"\n\n... [{tag}: line 1 of {total_lines} mid-line trimmed "
            f"({omitted_chars:,} chars){hint}; total {len(text):,} chars] ...\n\n"
        )
        return text[:head_len].rstrip() + notice + text[-tail_len:].lstrip()

    tail_chunks_rev: List[str] = []
    tail_chars = 0
    tail_start = total_lines
    for idx in range(total_lines - 1, head_end - 1, -1):
        ln = lines[idx]
        if tail_chars + len(ln) > tail_budget:
            break
        tail_chunks_rev.append(ln)
        tail_chars += len(ln)
        tail_start = idx

    if tail_start <= head_end:
        return text

    omitted_slice = lines[head_end:tail_start]
    omitted_chars = sum(len(l) for l in omitted_slice)
    omitted_count = tail_start - head_end
    range_desc = _describe_omitted_lines(lines, head_end, tail_start)
    hint = f" {extra_hint}" if extra_hint else ""
    notice = (
        f"\n\n... [{tag}: {range_desc} omitted "
        f"({omitted_count:,} lines, {omitted_chars:,} chars){hint}; total {len(text):,} chars] ...\n\n"
    )
    tail_chunks_rev.reverse()
    return "".join(head_chunks).rstrip() + notice + "".join(tail_chunks_rev).lstrip()


def _truncate_tool_output(text: str, max_chars: int = 24_000, label: str = "output") -> str:
    """Bound huge tool outputs (keeping head + tail) so prompts fit comfortably in the WSA web bridge."""
    return truncate_text_with_line_range(text, max_chars=max_chars, label=label)


def _state_dotfile(wsa_name: str, legacy_name: str, write: bool = False) -> Path:
    """Resolve per-project WSA state dotfiles, falling back to legacy .gwa_* on reads."""
    with _filesystem_lock:
        root = _project_root
    primary = root / wsa_name
    if write or primary.exists():
        return primary
    legacy = root / legacy_name
    return legacy if legacy.exists() else primary


def reset_project_session_state() -> None:
    """Reset per-project file tracking, checkpoints, background processes & backups on project switch."""
    global _checkpoint_seq
    cleanup_background_processes()
    with _filesystem_lock:
        _read_files_set.clear()
        _file_backups.clear()
        _backup_order.clear()
        _checkpoints.clear()
        _GITIGNORE_CACHE.clear()
        _checkpoint_seq = 0
        _active_checkpoint.set(None)
        _default_file_ledger.clear()


def set_project_root(root: str | Path):
    global _project_root
    with _filesystem_lock:
        prev_root = _project_root
        _project_root = Path(root).resolve()
        if _project_root != prev_root:
            reset_project_session_state()
        _session_memory.clear()
        memory_path = _state_dotfile(".wsa_memory.json", ".gwa_memory.json")
        if memory_path.exists():
            try:
                data = json.loads(memory_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _session_memory.update({str(k): str(v) for k, v in data.items()})
            except (OSError, ValueError, TypeError) as exc:
                sys.stderr.write(f"[tools] Could not load persistent memory: {exc}\n")
        load_dynamic_rules()
        load_custom_tools()


def get_project_root() -> Path:
    with _filesystem_lock:
        return _project_root


def _resolve(path: str) -> Path:
    """Resolve literal filesystem input; never apply Markdown/display transforms."""
    if not isinstance(path, (str, Path)) or not str(path) or "\x00" in str(path):
        raise ValueError("Path must be a non-empty string without NUL characters")
    with _filesystem_lock:
        root = _project_root
    p = (root / path).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise PermissionError(f"Path '{path}' escapes project root")
    if sys.platform == "win32" and any(":" in part for part in p.relative_to(root).parts):
        raise PermissionError(f"Alternate data streams are not workspace files: '{path}'")
    return p


def _load_gitignore_rules(root: Optional[Path] = None) -> Tuple[Tuple[bool, bool, bool, str], ...]:
    """Parse simple .gitignore directory/file rules for `root` with mtime caching."""
    with _filesystem_lock:
        base = (root or _project_root).resolve()
        gi_path = base / ".gitignore"
        if not gi_path.is_file():
            return ()
        try:
            mtime_ns = gi_path.stat().st_mtime_ns
        except OSError:
            return ()
        key = str(base)
        cached = _GITIGNORE_CACHE.get(key)
        if cached and cached[0] == mtime_ns:
            return cached[1]
        rules: List[Tuple[bool, bool, bool, str]] = []
        try:
            for raw_line in gi_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                negated = line.startswith("!")
                if negated:
                    line = line[1:].strip()
                dir_only = line.endswith("/")
                if dir_only:
                    line = line.rstrip("/")
                anchored = line.startswith("/") or ("/" in line)
                pat = line.lstrip("/")
                if pat:
                    rules.append((negated, dir_only, anchored, pat))
        except OSError:
            return ()
        compiled = tuple(rules)
        if len(_GITIGNORE_CACHE) >= 64:
            _GITIGNORE_CACHE.pop(next(iter(_GITIGNORE_CACHE)), None)
        _GITIGNORE_CACHE[key] = (mtime_ns, compiled)
        return compiled


def _is_ignored(name: str, is_dir: bool) -> bool:
    if is_dir:
        if name in _effective_ignore_dirs():
            return True
        for pat in IGNORE_DIR_PATTERNS:
            if fnmatch.fnmatch(name, pat):
                return True
        return False
    for pat in IGNORE_FILES_PATTERNS:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def _is_path_ignored(rel_posix: str, is_dir: bool, root: Optional[Path] = None, *, skip_media_ignore: bool = False) -> bool:
    """Check built-in noise ignores plus simple project .gitignore rules."""
    clean = str(rel_posix or "").replace("\\", "/")
    while clean.startswith("./"):
        clean = clean[2:]
    clean = clean.strip("/")
    if not clean:
        return False
    name = clean.rsplit("/", 1)[-1]
    if is_dir:
        if _is_ignored(name, True):
            return True
    else:
        if skip_media_ignore and Path(name).suffix.lower() in _IMAGE_EXT_MIME:
            pass
        elif _is_ignored(name, False):
            return True

    rules = _load_gitignore_rules(root)
    if not rules:
        return False
    ignored = False
    for negated, dir_only, anchored, pat in rules:
        if dir_only and not is_dir:
            continue
        if anchored:
            matched = fnmatch.fnmatch(clean, pat) or (is_dir and clean.startswith(pat + "/"))
        else:
            matched = fnmatch.fnmatch(name, pat)
        if matched:
            ignored = not negated
    return ignored


def _iter_workspace_files(base: Path, *, skip_media_ignore: bool = False):
    """Yield `(Path, rel_posix)` for workspace files, pruning ignored subtrees cleanly."""
    root = _project_root.resolve()
    gi_base = base.resolve()
    for cur_root, dirs, files in os.walk(gi_base):
        cur_p = Path(cur_root)
        try:
            rel_dir = str(cur_p.relative_to(root)).replace("\\", "/")
        except ValueError:
            rel_dir = ""
        rel_dir = "" if rel_dir == "." else rel_dir
        kept_dirs = []
        for d in sorted(dirs):
            d_rel = f"{rel_dir}/{d}" if rel_dir else d
            if not _is_path_ignored(d_rel, True, root) and not _is_directory_link(cur_p / d):
                kept_dirs.append(d)
        dirs[:] = kept_dirs
        for fname in sorted(files):
            f_rel = f"{rel_dir}/{fname}" if rel_dir else fname
            if not _is_path_ignored(f_rel, False, root, skip_media_ignore=skip_media_ignore):
                try:
                    yield _resolve(f_rel), f_rel
                except (OSError, ValueError):
                    continue


# ─── Tool definitions ─────────────────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file's content (up to 2,000 lines / 120KB per call). "
                "Optional start_line/end_line for ranged reads."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":       {"type": "string", "description": "Path relative to project root"},
                    "start_line": {"type": "integer", "description": "1-based start line (optional)"},
                    "end_line":   {"type": "integer", "description": "1-based end line inclusive (optional)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_info",
            "description": (
                "Get file metadata: size, line count, and last modified."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to project root"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_outline",
            "description": (
                "Extract structural AST/symbol outline (classes, functions, exports, IDs, selectors) with line ranges BEFORE reading slices."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to project root"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_image",
            "description": (
                "Load an image/screenshot for visual inspection in the next turn and report dimensions, format, and byte size."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Image/screenshot path relative to project root or absolute path"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create a new file or replace a small single-purpose file (<60 lines). "
                "For modifying existing files, ALWAYS use patch_file instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "Path relative to project root"},
                    "content": {"type": "string", "description": "Complete file content (no placeholders)"},
                    "force_overwrite": {"type": "boolean", "description": "True ONLY for intentional full rewrites"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_file",
            "description": (
                "Surgically replace exact lines/hunks (old_string/new_string or atomic hunks=[{old, new}]) in an existing file and return a unified diff."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":       {"type": "string", "description": "Path relative to project root"},
                    "old_string": {"type": "string", "description": "Exact target text to replace"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                    "start_line": {"type": "integer", "description": "Optional 1-based start line window"},
                    "end_line":   {"type": "integer", "description": "Optional 1-based end line inclusive"},
                    "hunks": {
                        "type": "array",
                        "description": "List of {old, new, start_line?, end_line?} objects for atomic multi-hunk edits",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old":        {"type": "string"},
                                "new":        {"type": "string"},
                                "start_line": {"type": "integer"},
                                "end_line":   {"type": "integer"},
                            },
                            "required": ["old", "new"],
                        },
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": (
                "Find workspace files matching a glob (e.g. '**/*.py', 'src/**/*.ts', '*.json')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern":     {"type": "string", "description": "Glob pattern relative to project root"},
                    "max_results": {"type": "integer", "description": "Max files to return (default: 150, max: 500)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List directory contents cleanly (depth-bounded tree when recursive=true).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":      {"type": "string", "description": "Directory relative to project root (default: '.')"},
                    "recursive": {"type": "boolean", "description": "List subdirectories recursively (default: false)"},
                    "max_depth": {"type": "integer", "description": "Max recursion depth when recursive=true (default: 4)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Regex search across project files (optional path/file_glob), returning file:line matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern":        {"type": "string", "description": "Regex pattern to search for"},
                    "path":           {"type": "string", "description": "Subdirectory or file to scope search (default: '.')"},
                    "file_glob":      {"type": "string", "description": "Filter files by glob (e.g. '*.py', '*.ts')"},
                    "case_sensitive": {"type": "boolean", "description": "Case-sensitive search (default: true)"},
                    "max_results":    {"type": "integer", "description": "Max matched lines to return (default: 80)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_tabs",
            "description": (
                "List open browser tabs via the active browser bridge (IDs, titles, URLs)."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_inspect",
            "description": (
                "Inspect a live browser tab or URL: captures screenshot and audits console errors, overflow, and controls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url":                {"type": "string",  "description": "Optional URL to open or reuse in a dedicated preview tab"},
                    "tab_id":             {"type": "integer", "description": "Optional open non-protected app tab ID from browser_tabs"},
                    "reload":             {"type": "boolean", "description": "Reload page before auditing (default: false)"},
                    "wait_ms":            {"type": "integer", "description": "Settle wait in ms before capture (default: 600)"},
                    "capture_screenshot": {"type": "boolean", "description": "Attach live PNG/JPEG screenshot to next turn (default: true)"},
                    "restore_focus":      {"type": "boolean", "description": "Restore previous active tab after screenshot (default: true)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_interact",
            "description": (
                "Interact with live browser tab (click/fill/select/press_enter/hover/scroll/wait), then re-audit and attach screenshot."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action":             {"type": "string",  "description": "One of: click, fill, select, press_enter, hover, scroll, wait"},
                    "selector":           {"type": "string",  "description": "CSS selector for target element"},
                    "text":               {"type": "string",  "description": "Visible button/link text fallback if selector is omitted"},
                    "value":              {"type": "string",  "description": "Input/select value when action is fill or select"},
                    "scroll_y":           {"type": "string",  "description": "Scroll target: pixel delta, 'top', or 'bottom'"},
                    "url":                {"type": "string",  "description": "Optional URL if opening/switching target tab first"},
                    "tab_id":             {"type": "integer", "description": "Optional target tab ID from browser_tabs"},
                    "wait_ms":            {"type": "integer", "description": "Wait in ms after interaction before screenshot (default: 500)"},
                    "capture_screenshot": {"type": "boolean", "description": "Attach updated screenshot to next turn (default: true)"},
                    "restore_focus":      {"type": "boolean", "description": "Restore previous active tab after capture (default: true)"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "visual_inspect",
            "description": (
                "Headless 1440x900 desktop capture (optional viewports/actions/diff) with Set-of-Mark [1][2] badges, console/layout/blank-canvas audit & next-turn image attach."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target":        {"type": "string",  "description": "Local HTML/SVG path, workspace dir (serving index.html), or http(s) URL (default: 'index.html')"},
                    "viewports":     {"type": "array",   "description": "Optional viewport list (defaults to full desktop laptop 1440x900). Pass extra viewports only when specifically checking mobile/tablet, e.g. [{\"name\":\"desktop\",\"width\":1440,\"height\":900}, {\"name\":\"mobile\",\"width\":390,\"height\":844,\"mobile\":true}]"},
                    "actions":       {"type": "array",   "description": "Optional interactions before interactive tile: click, hover, scroll, drag (for 3D/canvas), type, wait"},
                    "annotate":      {"type": "boolean", "description": "Overlay Set-of-Mark [1][2][3] element badges on primary tile (default: true)"},
                    "full_page":     {"type": "boolean", "description": "Capture full scroll height on primary tile (default: false)"},
                    "include_diff":  {"type": "boolean", "description": "Include pixel-diff heatmap tile vs previous capture (default: true)"},
                    "color_scheme":  {"type": "string",  "description": "Optional CSS prefers-color-scheme emulation: 'dark' or 'light'"},
                    "disable_webgl": {"type": "boolean", "description": "Disable WebGL to test 2D/poster fallback rendering (default: false)"},
                    "wait_ms":       {"type": "integer", "description": "Settle delay in ms after page load (default: 500)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command or direct argv array in project root; use run_python for multi-line Python."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Native shell command to execute (omit when passing argv)"},
                    "argv":    {"type": "array", "items": {"type": "string"}, "description": "Direct [exe, ...args] list bypassing shell quoting"},
                    "cwd":     {"type": "string", "description": "Optional working directory relative to project root"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default: 120, max: 600)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file from the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to project root"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Store a persistent session note (architecture, findings, or plan)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key":   {"type": "string", "description": "Note identifier (e.g. 'architecture', 'bug-root-cause')"},
                    "value": {"type": "string", "description": "Note content"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute a Python code snippet cleanly via temp file using the project interpreter (supports args and cwd)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code":    {"type": "string", "description": "Python source code to execute"},
                    "args":    {"type": "array", "items": {"type": "string"}, "description": "Optional CLI arguments passed as sys.argv[1:]"},
                    "cwd":     {"type": "string", "description": "Optional working directory relative to project root"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default: 120, max: 600)"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Run tests (pytest/unittest/vitest/jest) and return structured pass/fail/skip counts and failing test names."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":      {"type": "string",  "description": "Optional test file, dir, or pytest node ID"},
                    "k":         {"type": "string",  "description": "Optional test name filter expression (-k)"},
                    "args":      {"type": "string",  "description": "Optional extra runner CLI arguments"},
                    "framework": {"type": "string",  "description": "Optional runner: auto, pytest, unittest, vitest, jest"},
                    "timeout":   {"type": "integer", "description": "Timeout in seconds (default: 180, max: 600)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rollback",
            "description": (
                "Restore files from a pre-edit checkpoint (`rollback`), `list` recorded checkpoints, or `create` a manual snapshot."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action":        {"type": "string",  "description": "One of: rollback (default), list, create"},
                    "steps":         {"type": "integer", "description": "Number of recent checkpoints to roll back (default: 1)"},
                    "checkpoint_id": {"type": "string",  "description": "Specific checkpoint ID (e.g. 'cp-2') to roll back through"},
                    "paths":         {"type": "array",   "items": {"type": "string"}, "description": "Workspace paths to snapshot when action='create'"},
                    "label":         {"type": "string",  "description": "Optional checkpoint label when action='create'"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_background",
            "description": (
                "Start a local server or worker in the background and return its PID and stdout/stderr log paths."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string",  "description": "Native shell command to run in the background"},
                    "name":    {"type": "string",  "description": "Short unique process name (optional)"},
                    "wait_ms": {"type": "integer", "description": "Initial startup wait in ms before reporting status (default: 500)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_background",
            "description": "List WSA-managed background processes with live status, PID, log paths, and recent log tail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":       {"type": "string",  "description": "Optional process name filter"},
                    "tail_lines": {"type": "integer", "description": "Recent log lines to include per stream (default: 15)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_background",
            "description": "Stop a tracked background process previously started by WSA by name or tracked PID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name or tracked PID returned by start_background"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart_service",
            "description": (
                "Stop ONLY the listener(s) on a local TCP port (refusing WSA's own port) and optionally start a replacement."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "port":    {"type": "integer", "description": "Target local TCP port (e.g. 5000, 8000)"},
                    "command": {"type": "string",  "description": "Optional native shell command to launch after freeing the port"},
                    "name":    {"type": "string",  "description": "Optional background process name (default: service-<port>)"},
                    "wait_ms": {"type": "integer", "description": "Max ms to wait for new listener readiness (default: 2000)"},
                },
                "required": ["port"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": (
                "Send an HTTP request strictly to localhost (127.0.0.1/[::1]) or the configured WSA endpoint."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url":       {"type": "string",  "description": "Target localhost HTTP(S) URL"},
                    "method":    {"type": "string",  "description": "HTTP method: GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS"},
                    "headers":   {"type": "object",  "description": "Optional HTTP headers map"},
                    "json_body": {"type": "object",  "description": "Optional JSON request body object"},
                    "body":      {"type": "string",  "description": "Optional raw string request body"},
                    "timeout":   {"type": "integer", "description": "Timeout in seconds (default: 15, max: 120)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_custom_tool",
            "description": (
                "Register a reusable custom Python workspace helper defining `def run(**kwargs) -> str`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "snake_case tool name"},
                    "description": {"type": "string", "description": "What the custom tool does"},
                    "python_code": {"type": "string", "description": "Python code defining def run(**kwargs) -> str"},
                    "parameters_json": {"type": "string", "description": "Optional JSON Schema for parameters"},
                },
                "required": ["name", "description", "python_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_custom_tools",
            "description": "List custom runtime tools created in this project.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_custom_tool",
            "description": "Remove a custom runtime tool by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Custom tool name to remove"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_dynamic_rule",
            "description": (
                "Add, update, or remove (empty rule string) a persistent project system-prompt rule."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Rule identifier"},
                    "rule": {"type": "string", "description": "Rule text, or empty string to remove it"},
                },
                "required": ["key", "rule"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dynamic_rules",
            "description": "List the dynamic rules active for this project.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_dynamic_rule",
            "description": "Remove one dynamic rule by key.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Rule identifier to remove"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Retrieve session memory notes. Omit key to get all notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Note key to retrieve (optional — omit for all notes)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_subagent",
            "description": (
                "Delegate a scoped sub-task to a worker (explorer/architect/coder/security/verifier); pass owned_files & acceptance for coder."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": "Worker specialization: explorer, architect, coder, or verifier",
                    },
                    "task": {
                        "type": "string",
                        "description": "Concrete task instructions and context for the subagent",
                    },
                    "owned_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of workspace-relative paths owned by this subagent",
                    },
                    "acceptance": {
                        "type": "string",
                        "description": "Optional verifiable pass/fail condition for completion",
                    },
                },
                "required": ["role", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": (
                "Move/rename a file inside the workspace through the guarded write path "
                "(checkpointed, path-guarded) instead of shell mv."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":     {"type": "string", "description": "Source path relative to project root"},
                    "new_path": {"type": "string", "description": "Destination path relative to project root"},
                    "overwrite": {"type": "boolean", "description": "Replace an existing destination (default: false)"},
                },
                "required": ["path", "new_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "copy_file",
            "description": (
                "Copy a file inside the workspace through the guarded write path "
                "(checkpointed, path-guarded) instead of shell cp."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":     {"type": "string", "description": "Source path relative to project root"},
                    "new_path": {"type": "string", "description": "Destination path relative to project root"},
                    "overwrite": {"type": "boolean", "description": "Replace an existing destination (default: false)"},
                },
                "required": ["path", "new_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git",
            "description": (
                "Run an allowlisted git subcommand (status/diff/log/show/blame/add/commit/stash/restore/switch/tag) via argv list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action":  {"type": "string", "description": "Subcommand, e.g. status, diff, log, add, commit"},
                    "args":    {"type": "array", "items": {"type": "string"}, "description": "Arguments as separate argv items, e.g. [\"-m\", \"msg\"]"},
                    "timeout": {"type": "integer", "description": "Seconds (default: 60, max: 300)"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch a public http(s) URL (SSRF-guarded, output truncated)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url":       {"type": "string", "description": "Public http:// or https:// URL"},
                    "method":    {"type": "string", "description": "HTTP method (default: GET)"},
                    "headers":   {"type": "object", "description": "Optional request headers object"},
                    "body":      {"type": "string", "description": "Optional raw request body"},
                    "timeout":   {"type": "integer", "description": "Seconds (default: 20, max: 120)"},
                    "max_chars": {"type": "integer", "description": "Response text cap (default: 24000)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo",
            "description": (
                "Manage task checklist: action='read'|'write'|'clear'; items=[{text, status}] (pending|in_progress|completed)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "read, write, or clear (default: read)"},
                    "items": {
                        "type": "array",
                        "description": "Full checklist for action='write'",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text":   {"type": "string"},
                                "status": {"type": "string"},
                            },
                            "required": ["text"],
                        },
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_for",
            "description": (
                "Block until a port, localhost http URL, file path, or log regex ('path::regex') is ready; replaces polling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind":     {"type": "string", "description": "port, http, file, or log"},
                    "target":   {"type": "string", "description": "host:port, http://127.0.0.1:PORT/path, path, or 'path::regex'"},
                    "timeout":  {"type": "integer", "description": "Max seconds (default: 30, max: 600)"},
                    "interval": {"type": "number", "description": "Poll interval seconds (default: 0.5)"},
                },
                "required": ["kind", "target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": (
                "Atomically edit multiple distinct files via old_string/new_string or hunks, rolling back all files if any edit fails."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "description": "Ordered per-file edits; each path must be distinct.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path":       {"type": "string", "description": "File path relative to project root"},
                                "old_string": {"type": "string", "description": "Exact target text (single-hunk mode)"},
                                "new_string": {"type": "string", "description": "Replacement text (single-hunk mode)"},
                                "hunks":      {"type": "array", "items": {"type": "object"}, "description": "Atomic multi-hunk list of {old, new, start_line?, end_line?}"},
                                "start_line": {"type": "integer", "description": "Optional 1-based window start"},
                                "end_line":   {"type": "integer", "description": "Optional 1-based window end"},
                            },
                            "required": ["path"],
                        },
                    },
                },
                "required": ["edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill",
            "description": (
                "Read bundled WSA skills before planning/coding; pass comma-separated slugs (e.g. 'impeccable,unslop') or 'catalog'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name":    {"type": "string", "description": "Skill slug(s) comma-separated, or 'catalog'"},
                    "mode":    {"type": "string", "description": "'brief' (default compressed rules) or 'full'"},
                    "section": {"type": "string", "description": "Optional heading filter for a specific section"},
                },
                "required": ["name"],
            },
        },
    },
]

_BUILTIN_TOOL_NAMES = frozenset(item["function"]["name"] for item in TOOL_DEFINITIONS)


# ── Dynamic rules and custom tools ───────────────────────────────────────────
_dynamic_rules: Dict[str, str] = {}
_custom_tools: Dict[str, Dict] = {}

LOW_LEVEL_PROTECTED_FILES = {
    "config.json",
    "wsa-auth.json",
    ".env",
    "pyproject.toml",
    "wsa.bat",
    "wsa.ps1",
    "start.ps1",
    "start.bat",
    "scripts/apply_cookies.py",
    "scripts/capture_session.py",
}


def _is_protected_repo_path(rel_or_path: str) -> bool:
    try:
        raw_path = _rel_posix(_resolve(rel_or_path))
    except (OSError, ValueError):
        return True  # Invalid/outside paths cannot bypass confirmation checks.
    if sys.platform == "win32":
        raw_path = raw_path.casefold()
    return (
        raw_path in LOW_LEVEL_PROTECTED_FILES
        or any(part in {".git", ".hg", ".svn", ".ssh", ".aws", ".codex", ".agents"}
               for part in raw_path.split("/"))
        or Path(raw_path).name.startswith((".env.", ".wsa_", ".gwa_"))
        or Path(raw_path).name == ".env"
        or raw_path.startswith("wsa_agent/")
        or raw_path.startswith("wsa_server/")
    )


def _check_command_low_level(cmd: str) -> Tuple[bool, str]:
    low_level_pats = [
        (r"\bpip\s+(?:install|uninstall)\b", "Package environment modification"),
        (r"\bgit\s+(?:push|reset|checkout|clean|rebase)\b", "Git repository history/state operation"),
        (r"\b(?:reg|sc|netsh|icacls|takeown)\b", "Windows system/registry command"),
    ]
    for pat, desc in low_level_pats:
        if re.search(pat, cmd or "", re.IGNORECASE):
            return True, f"{desc}: `{cmd[:80]}`"
    return False, ""


def is_low_level_change(tool_name: str, args: dict) -> Tuple[bool, str]:
    """Return (True, reason) when a call edits core agent/server files or system state."""
    if tool_name in ("write_file", "patch_file", "delete_file", "move_file", "copy_file"):
        raw_paths = [str(args.get("path", ""))]
        if tool_name in ("move_file", "copy_file"):
            raw_paths.append(str(args.get("new_path", "")))
        for raw_path in raw_paths:
            if _is_protected_repo_path(raw_path):
                return True, f"Core self-modification of '{raw_path}'"
    elif tool_name == "apply_patch":
        for edit in args.get("edits") or []:
            if isinstance(edit, dict) and _is_protected_repo_path(str(edit.get("path", ""))):
                return True, f"Core self-modification of '{edit.get('path')}' via apply_patch"
    elif tool_name == "git":
        sub = str(args.get("action") or "").strip().lower()
        if sub in {"add", "commit", "stash", "restore", "checkout", "switch", "tag"}:
            return True, f"Git repository history/state operation: git {sub}"
    elif tool_name in ("run_command", "start_background", "restart_service"):
        command = " ".join(args["argv"]) if isinstance(args.get("argv"), list) else str(args.get("command", ""))
        return _check_command_low_level(command)
    elif tool_name == "rollback" and str(args.get("action") or "rollback").lower() == "rollback":
        target_paths = _preview_rollback_paths(
            steps=args.get("steps", 1),
            checkpoint_id=args.get("checkpoint_id"),
        )
        protected = [p for p in target_paths if _is_protected_repo_path(p)]
        if protected:
            return True, f"Core self-modification via rollback of {', '.join(protected[:3])}"
    return False, ""


def _validate_python_syntax(path_str: str, source_text: str) -> Optional[str]:
    """Parse .py source before it is written. None means the source is valid."""
    if not str(path_str).lower().endswith(".py"):
        return None
    try:
        ast.parse(source_text, filename=str(path_str))
        return None
    except SyntaxError as se:
        lines = source_text.splitlines()
        bad_line = ""
        if se.lineno and 1 <= se.lineno <= len(lines):
            bad_line = lines[se.lineno - 1]
        return (
            f"[Syntax Error Guard] Refusing to write invalid Python syntax to '{path_str}' "
            f"(line {se.lineno}, col {se.offset}): {se.msg}\n"
            f"  Offending line: {bad_line!r}\n"
            "Fix the syntax/indentation in your patch_file or write_file call and retry."
        )


def _validate_json_syntax(path_str: str, source_text: str) -> Optional[str]:
    """Validate .json syntax before overwriting config/manifest files."""
    if not str(path_str).lower().endswith(".json"):
        return None
    try:
        json.loads(source_text)
        return None
    except json.JSONDecodeError as je:
        return (
            f"[Syntax Error Guard] Refusing to write invalid JSON to '{path_str}' "
            f"(line {je.lineno}, col {je.colno}): {je.msg}"
        )


_SCRIPT_CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".bat", ".ps1", ".css", ".html", ".htm", ".yml", ".yaml",
}
_MD_URL_IN_CODE_RE = re.compile(r"\[https?://[^\]\s]+\]\(https?://[^)\s]+\)", re.IGNORECASE)
_MD_FENCE_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def _validate_no_markdown_urls_in_code(path_str: str, source_text: str) -> Optional[str]:
    """Reject Markdown-formatted '[http...](http...)' URLs inside code files or fenced code blocks."""
    low = str(path_str or "").lower()
    if low.endswith(".md") or low.endswith(".markdown"):
        for block in _MD_FENCE_BLOCK_RE.findall(source_text or ""):
            m = _MD_URL_IN_CODE_RE.search(block)
            if m:
                return (
                    f"[Syntax Error Guard] Corrupted Markdown link inside fenced code block of '{path_str}': "
                    f"{m.group(0)!r}. Use plain raw URLs inside code/shell blocks."
                )
        return None
    if Path(low).suffix in _SCRIPT_CODE_EXTS:
        m = _MD_URL_IN_CODE_RE.search(source_text or "")
        if m:
            return (
                f"[Syntax Error Guard] Corrupted Markdown link inside code file '{path_str}': "
                f"{m.group(0)!r}. Use raw URL strings (never '[url](url)') in code/scripts."
            )
    return None


def _validate_code_content(path_str: str, source_text: str) -> Optional[str]:
    """Run all pre-write static syntax and URL integrity guards on candidate file content."""
    return (
        _validate_python_syntax(path_str, source_text)
        or _validate_json_syntax(path_str, source_text)
        or _validate_no_markdown_urls_in_code(path_str, source_text)
    )


def _is_self_code_file(p: Path) -> Optional[str]:
    """Return verification mode ('py' or 'js') if `p` belongs to WSA's own runtime codebase."""
    try:
        resolved = p.resolve()
    except Exception:
        return None
    suffix = resolved.suffix.lower()
    if suffix == ".py":
        if _SELF_PKG_DIR in resolved.parents or _SELF_SERVER_DIR in resolved.parents:
            return "py"
    elif suffix == ".js":
        ext_dir = (_SELF_REPO_ROOT / "wsa-browser-bridge").resolve()
        if ext_dir in resolved.parents:
            return "js"
    return None


def _rollback_file(p: Path, existed: bool, prev_text: Optional[str], prev_raw: Optional[bytes] = None) -> Optional[str]:
    """Restore `p` back to its pre-edit content after a failed self-modification check."""
    try:
        if existed:
            raw = prev_raw if prev_raw is not None else (prev_text or "").encode("utf-8")
            _write_bytes_atomic(p, raw)
            if p.read_bytes() != raw:
                raise OSError("Restored bytes did not match the backup")
        elif not existed and p.exists():
            _retry_filesystem(p.unlink)
    except Exception as exc:
        return f"[Error] Self-preservation rollback FAILED for '{p}': {exc}. The invalid edit may remain on disk."
    return None


def _verify_self_edit_on_disk(p: Path, existed: bool, prev_text: Optional[str], prev_raw: Optional[bytes] = None) -> Optional[str]:
    """Run an isolated compile/import gate after editing WSA's own source, rolling back on failure."""
    kind = _is_self_code_file(p)
    if not kind:
        return None
    if kind == "py":
        resolved = p.resolve()
        mod_name = ""
        if _SELF_PKG_DIR in resolved.parents or _SELF_SERVER_DIR in resolved.parents:
            rel = resolved.relative_to(_SELF_REPO_ROOT).with_suffix("")
            parts = list(rel.parts)
            if parts and parts[-1] == "__init__":
                parts = parts[:-1]
            mod_name = ".".join(parts)
        script = (
            "import importlib, py_compile, sys\n"
            "path = sys.argv[1]\n"
            "mod = sys.argv[2]\n"
            "root = sys.argv[3]\n"
            "py_compile.compile(path, doraise=True)\n"
            "if mod:\n"
            "    if root not in sys.path: sys.path.insert(0, root)\n"
            "    importlib.import_module(mod)\n"
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-c", script, str(resolved), mod_name, str(_SELF_REPO_ROOT)],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(_SELF_REPO_ROOT),
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "import check failed").strip()
                failure = _rollback_file(p, existed, prev_text, prev_raw)
                if failure:
                    return failure
                return (
                    f"[Self-Preservation Rollback] Automatically reverted '{p.name}' because self-edit "
                    f"verification failed (WSA protected from crashing):\n{detail[:1200]}"
                )
        except Exception as exc:
            failure = _rollback_file(p, existed, prev_text, prev_raw)
            if failure:
                return failure
            return f"[Self-Preservation Rollback] Reverted '{p.name}' after verification error: {exc}"
    elif kind == "js" and shutil.which("node"):
        try:
            proc = subprocess.run(
                ["node", "--check", str(p.resolve())],
                capture_output=True,
                text=True,
                timeout=6,
                cwd=str(_SELF_REPO_ROOT),
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "node --check failed").strip()
                failure = _rollback_file(p, existed, prev_text, prev_raw)
                if failure:
                    return failure
                return (
                    f"[Self-Preservation Rollback] Automatically reverted '{p.name}' due to JS syntax error:\n"
                    f"{detail[:1200]}"
                )
        except Exception as exc:
            failure = _rollback_file(p, existed, prev_text, prev_raw)
            return failure or f"[Self-Preservation Rollback] Reverted '{p.name}' after verification error: {exc}"
    return None


def _rules_file_path(write: bool = False) -> Path:
    return _state_dotfile(".wsa_rules.json", ".gwa_rules.json", write=write)


def _custom_tools_file_path(write: bool = False) -> Path:
    return _state_dotfile(".wsa_custom_tools.json", ".gwa_custom_tools.json", write=write)


def load_dynamic_rules() -> Dict[str, str]:
    global _dynamic_rules
    _dynamic_rules = {}
    fp = _rules_file_path()
    if fp.exists():
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _dynamic_rules = {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass
    return _dynamic_rules


def save_dynamic_rules() -> None:
    try:
        _rules_file_path(write=True).write_text(json.dumps(_dynamic_rules, indent=2), encoding="utf-8")
    except Exception:
        pass


def _sync_custom_tool_definitions() -> None:
    existing_names = {t.get("function", {}).get("name") for t in TOOL_DEFINITIONS}
    for name, meta in _custom_tools.items():
        if name not in existing_names:
            TOOL_DEFINITIONS.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"[Custom Tool] {meta.get('description', '')}",
                    "parameters": meta.get("parameters", {"type": "object", "properties": {}}),
                },
            })


def load_custom_tools() -> Dict[str, Dict]:
    global _custom_tools
    _custom_tools = {}
    TOOL_DEFINITIONS[:] = [
        t for t in TOOL_DEFINITIONS
        if not str(t.get("function", {}).get("description", "")).startswith("[Custom Tool]")
    ]
    fp = _custom_tools_file_path()
    if fp.exists():
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Old files may contain names that collided with built-ins.
                # Never advertise or remove an existing built-in as a custom tool.
                _custom_tools = {name: meta for name, meta in data.items() if name not in _BUILTIN_TOOL_NAMES}
                _sync_custom_tool_definitions()
        except Exception:
            pass
    return _custom_tools


def save_custom_tools() -> None:
    try:
        _custom_tools_file_path(write=True).write_text(json.dumps(_custom_tools, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_dynamic_rules() -> Dict[str, str]:
    return load_dynamic_rules()


# ─── Canonical Schema Validation, Normalization & Execution Classification ───

_PATCH_MULTI_KEYS = ("hunks", "edits", "changes", "patches")
_PATCH_OLD_KEYS = ("old", "old_string", "old_text", "search", "find", "before", "target", "original")
_PATCH_NEW_KEYS = ("new", "new_string", "new_text", "replace", "replacement", "after", "updated")
_PATCH_SINGLE_OLD_KEYS = ("old_string", "old", "old_text", "search", "find", "before", "target", "original")
_PATCH_SINGLE_NEW_KEYS = ("new_string", "new", "new_text", "replace", "replacement", "after", "updated")

_NEG_PROBE_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:git\s+check-ignore\b|rg\b|grep\b|egrep\b|fgrep\b|findstr\b|Select-String\b)",
    re.IGNORECASE,
)
_DIFF_EXIT_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:git\s+diff\b[^|;&]*(?:--quiet|--exit-code)\b|diff\b)",
    re.IGNORECASE,
)
_CMD_NOT_FOUND_RE = re.compile(
    r"CommandNotFoundException|is not recognized as an internal or external command|"
    r"The term ['\"].+?['\"] is not recognized|Executable not found for argv",
    re.IGNORECASE,
)


def _is_valid_json_type(val: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(val, str)
    if expected == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if expected == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if expected == "boolean":
        return isinstance(val, bool)
    if expected == "array":
        return isinstance(val, list)
    if expected == "object":
        return isinstance(val, dict)
    return True


def split_tool_control_args(args: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Separate scheduler control metadata (e.g. `depends_on`) from executor kwargs."""
    cleaned = dict(args or {})
    raw_deps = cleaned.pop("depends_on", cleaned.pop("_depends_on", None))
    deps: List[str] = []
    if isinstance(raw_deps, str) and raw_deps.strip():
        deps = [raw_deps.strip()]
    elif isinstance(raw_deps, list):
        deps = [str(x).strip() for x in raw_deps if str(x).strip()]
    return cleaned, deps


def normalize_patch_args(raw_args: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """Normalize single/multi-hunk `patch_file` variants and reject missing/ambiguous fields."""
    if not isinstance(raw_args, dict):
        return {}, "[Validation Error] patch_file arguments must be an object."
    allowed_top = (
        {"path", "start_line", "end_line"}
        | set(_PATCH_MULTI_KEYS)
        | set(_PATCH_SINGLE_OLD_KEYS)
        | set(_PATCH_SINGLE_NEW_KEYS)
    )
    extra_top = sorted(k for k in raw_args if k not in allowed_top)
    if extra_top:
        return {}, f"[Validation Error] Unknown parameter(s) for patch_file: {', '.join(extra_top)}."

    path_val = raw_args.get("path")
    if not isinstance(path_val, str) or not path_val.strip():
        return {}, "[Validation Error] patch_file requires a non-empty string 'path'."

    present_multi = [k for k in _PATCH_MULTI_KEYS if k in raw_args and raw_args.get(k) is not None]
    if len(present_multi) > 1:
        return {}, f"[Validation Error] patch_file received conflicting multi-hunk keys: {', '.join(present_multi)}."

    has_single_old = any(raw_args.get(k) is not None for k in _PATCH_SINGLE_OLD_KEYS)
    has_single_new = any(raw_args.get(k) is not None for k in _PATCH_SINGLE_NEW_KEYS)

    if present_multi:
        mkey = present_multi[0]
        raw_hunks = raw_args.get(mkey)
        if has_single_old or has_single_new:
            return {}, "[Validation Error] patch_file cannot combine 'hunks' with top-level 'old_string'/'new_string'."
        if not isinstance(raw_hunks, list) or not raw_hunks:
            return {}, f"[Validation Error] patch_file '{mkey}' must be a non-empty array of {{old, new}} objects."
        allowed_hunk_keys = set(_PATCH_OLD_KEYS) | set(_PATCH_NEW_KEYS) | {"start_line", "end_line"}
        norm_hunks: List[Dict[str, Any]] = []
        for i, h in enumerate(raw_hunks, 1):
            if not isinstance(h, dict):
                return {}, f"[Validation Error] patch_file hunk {i} must be an object with 'old' and 'new'."
            extra_hk = sorted(k for k in h if k not in allowed_hunk_keys)
            if extra_hk:
                return {}, f"[Validation Error] patch_file hunk {i} has unknown key(s): {', '.join(extra_hk)}."
            old_k = next((k for k in _PATCH_OLD_KEYS if k in h and h[k] is not None), None)
            new_k = next((k for k in _PATCH_NEW_KEYS if k in h and h[k] is not None), None)
            if old_k is None or not isinstance(h[old_k], str) or not h[old_k]:
                return {}, f"[Validation Error] patch_file hunk {i} must provide a non-empty string 'old' (or 'old_string')."
            if new_k is None:
                return {}, (
                    f"[Validation Error] patch_file hunk {i} is missing 'new' replacement text "
                    '(pass "" explicitly only when deleting the hunk).'
                )
            if not isinstance(h[new_k], str):
                return {}, f"[Validation Error] patch_file hunk {i} 'new' must be a string."
            item: Dict[str, Any] = {"old": h[old_k], "new": h[new_k]}
            for bound_k in ("start_line", "end_line"):
                if bound_k in h and h[bound_k] is not None:
                    bv = h[bound_k]
                    if not isinstance(bv, int) or isinstance(bv, bool) or bv < 1:
                        return {}, f"[Validation Error] patch_file hunk {i} '{bound_k}' must be an integer >= 1."
                    item[bound_k] = bv
            norm_hunks.append(item)
        return {"path": path_val, "hunks": norm_hunks}, None

    old_k = next((k for k in _PATCH_SINGLE_OLD_KEYS if k in raw_args and raw_args[k] is not None), None)
    new_k = next((k for k in _PATCH_SINGLE_NEW_KEYS if k in raw_args and raw_args[k] is not None), None)
    if old_k is None or not isinstance(raw_args[old_k], str) or not raw_args[old_k]:
        return {}, "[Validation Error] patch_file requires non-empty 'old_string' (or non-empty 'hunks' array)."
    if new_k is None:
        return {}, "[Validation Error] patch_file requires 'new_string' (pass \"\" explicitly only when deleting text)."
    if not isinstance(raw_args[new_k], str):
        return {}, "[Validation Error] patch_file 'new_string' must be a string."
    norm_single: Dict[str, Any] = {
        "path": path_val,
        "old_string": raw_args[old_k],
        "new_string": raw_args[new_k],
    }
    for bound_k in ("start_line", "end_line"):
        if bound_k in raw_args and raw_args[bound_k] is not None:
            bv = raw_args[bound_k]
            if not isinstance(bv, int) or isinstance(bv, bool) or bv < 1:
                return {}, f"[Validation Error] patch_file '{bound_k}' must be an integer >= 1."
            norm_single[bound_k] = bv
    return norm_single, None


def _lookup_tool_schema(name: str) -> Optional[Dict[str, Any]]:
    for item in TOOL_DEFINITIONS:
        if isinstance(item, dict):
            fn = item.get("function", item)
            if str(fn.get("name") or "").strip() == name:
                return fn
    if name in _custom_tools:
        meta = _custom_tools[name]
        return {"name": name, "parameters": meta.get("parameters") or {"type": "object", "properties": {}, "required": []}}
    return None


def validate_and_normalize_tool_call(
    name: str,
    raw_args: Any,
    *,
    scoped_tool_names: Optional[Set[str]] = None,
    json_repair_fn: Any = None,
) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """Decode, schema-validate, and canonicalize tool arguments BEFORE dispatch."""
    clean_name = str(name or "").strip()
    if scoped_tool_names is not None and clean_name not in scoped_tool_names:
        return (
            False,
            raw_args if isinstance(raw_args, dict) else {},
            f"[Scope Error] Role cannot execute tool '{clean_name}'. Allowed tools: {', '.join(sorted(scoped_tool_names))}",
        )

    schema = _lookup_tool_schema(clean_name)
    if schema is None:
        return False, raw_args if isinstance(raw_args, dict) else {}, f"[Validation Error] Unknown tool: {clean_name}"

    if isinstance(raw_args, str):
        txt = raw_args.strip()
        if not txt:
            args_dict: Dict[str, Any] = {}
        else:
            try:
                args_dict = json.loads(txt)
            except json.JSONDecodeError as exc:
                repaired_ok = False
                if callable(json_repair_fn):
                    try:
                        args_dict = json.loads(json_repair_fn(txt))
                        repaired_ok = True
                    except Exception:
                        repaired_ok = False
                if not repaired_ok:
                    return (
                        False,
                        {},
                        f"[Validation Error] Malformed JSON in arguments for '{clean_name}': {exc.msg} "
                        f"(line {exc.lineno}, col {exc.colno}). Re-emit valid JSON.",
                    )
    elif raw_args is None:
        args_dict = {}
    else:
        args_dict = raw_args

    if not isinstance(args_dict, dict):
        return (
            False,
            {},
            f"[Validation Error] Arguments for '{clean_name}' must be a JSON object, got {type(args_dict).__name__}.",
        )

    for dep_key in ("depends_on", "_depends_on"):
        if dep_key in args_dict:
            deps = args_dict[dep_key]
            if not (isinstance(deps, str) and deps.strip()) and not (
                isinstance(deps, list) and all(isinstance(d, str) and d.strip() for d in deps)
            ):
                return False, args_dict, f"[Validation Error] '{dep_key}' must be a non-empty call ID or array of non-empty call IDs."
    if "depends_on" in args_dict and "_depends_on" in args_dict:
        return False, args_dict, "[Validation Error] Specify only one dependency field."
    args_clean, _ = split_tool_control_args(args_dict)

    if clean_name == "patch_file":
        norm_patch, patch_err = normalize_patch_args(args_clean)
        if patch_err:
            return False, args_clean, patch_err
        return True, norm_patch, None

    if clean_name == "run_command":
        allowed_rc = {"command", "argv", "cwd", "timeout"}
        extra_rc = sorted(k for k in args_clean if k not in allowed_rc)
        if extra_rc:
            return False, args_clean, f"[Validation Error] Unknown parameter(s) for run_command: {', '.join(extra_rc)}."
        cmd_val = args_clean.get("command")
        argv_val = args_clean.get("argv")
        has_cmd = cmd_val is not None and (not isinstance(cmd_val, str) or bool(cmd_val.strip()))
        has_argv = argv_val is not None and (not isinstance(argv_val, list) or len(argv_val) > 0)
        if cmd_val is not None and not isinstance(cmd_val, str):
            return False, args_clean, "[Validation Error] run_command 'command' must be a string."
        if argv_val is not None:
            if not isinstance(argv_val, list) or not argv_val or any(not isinstance(x, str) or not x for x in argv_val):
                return False, args_clean, "[Validation Error] run_command 'argv' must be a non-empty list of strings."
        if has_cmd and has_argv:
            return False, args_clean, "[Validation Error] Specify either 'command' or 'argv' for run_command, not both."
        if not has_cmd and not has_argv:
            return False, args_clean, "[Validation Error] run_command requires a non-empty 'command' or 'argv'."
        if "cwd" in args_clean and args_clean["cwd"] is not None and not isinstance(args_clean["cwd"], str):
            return False, args_clean, "[Validation Error] run_command 'cwd' must be a string."
        if "timeout" in args_clean and args_clean["timeout"] is not None and not _is_valid_json_type(args_clean["timeout"], "integer"):
            return False, args_clean, "[Validation Error] run_command 'timeout' must be an integer."
        return True, args_clean, None

    params = schema.get("parameters") or {}
    props = params.get("properties") or {}
    required = list(params.get("required") or [])

    if "properties" in params:
        extra = sorted(k for k in args_clean if k not in props)
        if extra:
            return (
                False,
                args_clean,
                f"[Validation Error] Unexpected parameter(s) for '{clean_name}': {', '.join(extra)}. "
                f"Allowed: {', '.join(props.keys())}.",
            )
    for req_k in required:
        if req_k not in args_clean or args_clean[req_k] is None:
            return False, args_clean, f"[Validation Error] Missing required parameter '{req_k}' for '{clean_name}'."
    for key, val in args_clean.items():
        if val is None or key not in props:
            continue
        spec = props[key]
        if isinstance(spec, dict):
            jtype = spec.get("type", "")
            if jtype and not _is_valid_json_type(val, jtype):
                return (
                    False,
                    args_clean,
                    f"[Validation Error] Parameter '{key}' for '{clean_name}' must be {jtype}, got {type(val).__name__}.",
                )
            if jtype == "array" and isinstance(spec.get("items"), dict) and spec["items"].get("type"):
                item_t = spec["items"]["type"]
                if any(not _is_valid_json_type(el, item_t) for el in val):
                    return (
                        False,
                        args_clean,
                        f"[Validation Error] All items in '{key}' for '{clean_name}' must be {item_t}.",
                    )
    return True, args_clean, None


def classify_exec_result(
    result_code: int,
    stdout: str,
    stderr: str,
    *,
    timed_out: bool = False,
    command: str = "",
    argv: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Classify subprocess outcomes cleanly (distinguishing negative probes from crashes)."""
    if timed_out:
        return {
            "execution_status": "timed_out",
            "result_class": "timeout",
            "exit_code": int(result_code),
            "timed_out": True,
            "ok": False,
            "is_error": True,
        }
    rc = int(result_code)
    cmd_text = " ".join(str(x) for x in argv) if argv else str(command or "")
    combined = f"{stdout or ''}\n{stderr or ''}"
    err_low = str(stderr or "").lower()

    if rc == 0:
        return {
            "execution_status": "completed",
            "result_class": "ok",
            "exit_code": 0,
            "timed_out": False,
            "ok": True,
            "is_error": False,
        }

    if _CMD_NOT_FOUND_RE.search(combined):
        return {
            "execution_status": "completed",
            "result_class": "command_not_found",
            "exit_code": rc,
            "timed_out": False,
            "ok": False,
            "is_error": True,
        }

    if rc == 1 and not any(tok in err_low for tok in ("fatal:", "parsererror", "syntaxerror", "traceback (most recent call last):")):
        if _NEG_PROBE_RE.search(cmd_text):
            return {
                "execution_status": "completed",
                "result_class": "no_match",
                "exit_code": rc,
                "timed_out": False,
                "ok": False,
                "is_error": False,
            }
        if _DIFF_EXIT_RE.search(cmd_text):
            return {
                "execution_status": "completed",
                "result_class": "differences_found",
                "exit_code": rc,
                "timed_out": False,
                "ok": False,
                "is_error": False,
            }

    if rc == 5 and "pytest" in cmd_text.lower():
        return {
            "execution_status": "completed",
            "result_class": "no_tests",
            "exit_code": rc,
            "timed_out": False,
            "ok": False,
            "is_error": False,
        }

    if any(tok in combined for tok in ("SyntaxError:", "IndentationError:", "ParserError:")):
        rclass = "syntax_error"
    elif any(tok in combined for tok in ("PermissionError:", "UnauthorizedAccess", "Access is denied")):
        rclass = "permission_error"
    else:
        rclass = "non_zero_exit"

    return {
        "execution_status": "completed",
        "result_class": rclass,
        "exit_code": rc,
        "timed_out": False,
        "ok": False,
        "is_error": True,
    }


# ─── Executors ────────────────────────────────────────────────────────────────

def execute_tool(name: str, args: dict) -> str:
    # File mutations and their checkpoint records must be committed together.
    # Other tools (especially subprocesses) never hold this filesystem lock.
    if name in {"read_file", "get_file_info", "get_file_outline", "inspect_image",
                "write_file", "patch_file", "delete_file", "rollback", "glob_files",
                "list_dir", "search_files"}:
        with _filesystem_lock:
            return _execute_tool_unlocked(name, args)
    return _execute_tool_unlocked(name, args)


def _execute_tool_unlocked(name: str, args: dict) -> str:
    try:
        args, _ = split_tool_control_args(args if isinstance(args, dict) else {})
        if name == "read_file":    return _read_file(**args)
        if name == "get_file_info": return _get_file_info(**args)
        if name == "get_file_outline": return _get_file_outline(**args)
        if name == "inspect_image": return _inspect_image(**args)
        if name == "write_file":   return _write_file(**args)
        if name == "patch_file":
            norm_args, patch_err = normalize_patch_args(args)
            if patch_err:
                return patch_err
            return _patch_file(**norm_args)
        if name == "glob_files":   return _glob_files(**args)
        if name == "list_dir":     return _list_dir(**args)
        if name == "search_files": return _search_files(**args)
        if name == "browser_tabs":     return _browser_tabs()
        if name == "browser_inspect":  return _browser_inspect(**args)
        if name == "browser_interact": return _browser_interact(**args)
        if name == "visual_inspect":   return _visual_inspect(**args)
        if name == "run_command":      return _run_command(**args)
        if name == "delete_file":      return _delete_file(**args)
        if name == "move_file":        return _move_file(**args)
        if name == "copy_file":        return _copy_file(**args)
        if name == "git":              return _git(**args)
        if name == "web_fetch":        return _web_fetch(**args)
        if name == "todo":             return _todo(**args)
        if name == "wait_for":         return _wait_for(**args)
        if name == "apply_patch":      return _apply_patch(**args)
        if name == "read_skill":       return _read_skill(**args)
        if name == "remember":         return _remember(**args)
        if name == "recall":           return _recall(**args)
        if name == "spawn_subagent":   return _spawn_subagent(**args)
        if name == "run_python":       return _run_python(**args)
        if name == "run_tests":        return _run_tests(**args)
        if name == "rollback":         return _rollback(**args)
        if name == "start_background": return _start_background(**args)
        if name == "list_background":  return _list_background(**args)
        if name == "stop_background":  return _stop_background(**args)
        if name == "restart_service":  return _restart_service(**args)
        if name == "http_request":     return _http_request(**args)
        if name == "create_custom_tool": return _create_custom_tool(**args)
        if name == "list_custom_tools":  return _list_custom_tools()
        if name == "remove_custom_tool": return _remove_custom_tool_action(**args)
        if name == "set_dynamic_rule":   return _set_dynamic_rule(**args)
        if name == "list_dynamic_rules": return _list_dynamic_rules()
        if name == "remove_dynamic_rule": return _set_dynamic_rule(key=args.get("key", ""), rule="")
        if name in _custom_tools:        return _execute_custom_tool(name, args)
        return f"[Error] Unknown tool: {name}"
    except PermissionError as e:
        return f"[Permission denied] {e}"
    except TypeError as e:
        return f"[Error] Wrong arguments for {name}: {e}"
    except Exception as e:
        return f"[Error] {type(e).__name__}: {e}"


def _spawn_subagent(
    role: str,
    task: str,
    owned_files: Optional[List[str]] = None,
    acceptance: str = "",
) -> str:
    """Delegate a scoped sub-task to a specialized worker subagent lazily."""
    from .subagents import spawn_scoped_subagent
    return spawn_scoped_subagent(
        role=role,
        task=task,
        owned_files=owned_files,
        acceptance=acceptance,
    )


def _read_skill(
    name: str = "catalog",
    mode: str = "brief",
    section: str = "",
    max_chars: int = 8000,
) -> str:
    """Load one or more bundled WSA skills in compressed or full form."""
    from .task_skills import read_skill_content
    cap = max(1200, min(int(max_chars or 8000), 14000))
    text, _loaded = read_skill_content(name=name, mode=mode, section=section, max_chars=cap)
    return text


_IMAGE_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def detect_image_info(raw: bytes, ext: str = "") -> Optional[Dict[str, object]]:
    """Inspect common raster image headers (PNG/JPEG/GIF/WEBP/BMP) via stdlib struct."""
    if not raw:
        return None
    low_ext = (ext or "").lower()
    mime = _IMAGE_EXT_MIME.get(low_ext, "")
    fmt = ""
    width = 0
    height = 0
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        mime, fmt = "image/png", "PNG"
        width, height = struct.unpack(">II", raw[16:24])
    elif raw.startswith(b"\xff\xd8\xff"):
        mime, fmt = "image/jpeg", "JPEG"
        pos = 2
        while pos + 4 < len(raw):
            if raw[pos] != 0xFF:
                pos += 1
                continue
            marker = raw[pos + 1]
            if marker in (0xD8, 0xD9, 0x00) or 0xD0 <= marker <= 0xD7:
                pos += 2
                continue
            if pos + 4 > len(raw):
                break
            seg_len = struct.unpack(">H", raw[pos + 2 : pos + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if pos + 9 <= len(raw):
                    height, width = struct.unpack(">HH", raw[pos + 5 : pos + 9])
                break
            if seg_len < 2:
                break
            pos += 2 + seg_len
    elif raw[:6] in (b"GIF87a", b"GIF89a") and len(raw) >= 10:
        mime, fmt = "image/gif", "GIF"
        width, height = struct.unpack("<HH", raw[6:10])
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" and len(raw) >= 30:
        mime, fmt = "image/webp", "WEBP"
        chunk = raw[12:16]
        if chunk == b"VP8X" and len(raw) >= 30:
            width = int.from_bytes(raw[24:27], "little") + 1
            height = int.from_bytes(raw[27:30], "little") + 1
        elif chunk == b"VP8L" and len(raw) >= 25:
            b0, b1, b2, b3 = raw[21:25]
            bits = b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
        elif chunk == b"VP8 " and len(raw) >= 30:
            width, height = struct.unpack("<HH", raw[26:30])
            width &= 0x3FFF
            height &= 0x3FFF
    elif raw[:2] == b"BM" and len(raw) >= 26:
        mime, fmt = "image/bmp", "BMP"
        w, h = struct.unpack("<ii", raw[18:26])
        width, height = abs(w), abs(h)
    elif mime:
        fmt = low_ext.lstrip(".").upper()
    else:
        return None
    return {
        "mime": mime or "image/png",
        "format": fmt or "IMAGE",
        "width": int(width),
        "height": int(height),
        "bytes": len(raw),
    }


def _resolve_readable_file(path: str) -> Path:
    """Resolve workspace-relative or existing absolute image/file paths safely."""
    raw = str(path or "").strip().strip('"').strip("'")
    cand = Path(raw)
    if cand.is_absolute() and cand.exists():
        return cand.resolve()
    return _resolve(raw)


def _inspect_image(path: str) -> str:
    try:
        p = _resolve_readable_file(path)
    except PermissionError:
        cand = Path(str(path or "").strip().strip('"').strip("'")).expanduser()
        if cand.is_file() and cand.suffix.lower() in _IMAGE_EXT_MIME:
            p = cand.resolve()
        else:
            raise
    if not p.exists():
        return f"[Error] Image file not found: {path}"
    if not p.is_file():
        return f"[Error] Not a file: {path}"
    raw = p.read_bytes()
    info = detect_image_info(raw, p.suffix)
    if not info:
        return f"[Error] Unsupported or unrecognized image format: {path}"
    if len(raw) > 8 * 1024 * 1024:
        return f"[Error] Image '{p.name}' is too large ({len(raw):,} bytes > 8 MB limit)."
    b64 = base64.b64encode(raw).decode("ascii")
    with _pending_images_lock:
        _pending_images.append({
            "filename": p.name,
            "path": str(p),
            "mime_type": str(info["mime"]),
            "url": f"data:{info['mime']};base64,{b64}",
            "width": str(info["width"]),
            "height": str(info["height"]),
        })
    dims = f"{info['width']}x{info['height']}" if info["width"] and info["height"] else "dimensions unknown"
    return (
        f"[OK] Loaded image '{p.name}' ({info['format']}, {dims}, {len(raw):,} bytes, {info['mime']}). "
        "It is attached visually in the next message turn — analyze every visible detail thoroughly."
    )


def _strip_line_number_prefixes(text: str) -> str:
    """Remove '   1 | ' line-number prefixes without eating Python indentation."""
    lines = text.splitlines(keepends=True)
    non_empty = [l for l in lines if l.strip()]
    if not non_empty:
        return text
    # Only strip when all non-empty lines match the '   1 | ' prefix format (consume at most 1 space after '|')
    if all(re.match(r"^\s*\d+\s*\|\s?", l) for l in non_empty):
        return "".join(re.sub(r"^\s*\d+\s*\|\s?", "", l, count=1) for l in lines)
    return text


def _retry_filesystem(operation):
    """Bound sharing/access-denied retries without changing the user's ACLs."""
    for attempt in range(4):
        try:
            return operation()
        except OSError as exc:
            transient = isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EBUSY) or getattr(exc, "winerror", None) in (5, 32, 33)
            if not transient or attempt == 3:
                raise
            time.sleep(0.05 * (2 ** attempt))


def _write_bytes_atomic(p: Path, raw: bytes) -> None:
    """Stage beside the destination so a failed write never truncates its contents."""
    # tempfile.mkstemp can loop on Windows when directory access is denied:
    # it treats PermissionError as a possible name collision. Only retry real
    # collisions so an inaccessible workspace fails promptly.
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0)
    for _ in range(10):
        staged = p.parent / (".wsa-write-" + secrets.token_hex(12))
        try:
            fd = os.open(str(staged), flags, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError("Unable to allocate a unique staging file")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if p.exists():
            # Preserve ordinary permissions; never relax a read-only destination.
            staged.chmod(p.stat().st_mode & 0o777)
        _retry_filesystem(lambda: os.replace(staged, p))
    finally:
        if staged.exists():
            _retry_filesystem(staged.unlink)


def _write_text_preserving_eol(p: Path, text: str, raw_before: Optional[bytes] = None) -> None:
    """Write UTF-8 text while preserving the file's existing LF vs CRLF line endings."""
    norm = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    eol = "\r\n" if (raw_before is not None and b"\r\n" in raw_before and b"\n" in raw_before) else "\n"
    if eol == "\r\n":
        norm = norm.replace("\n", "\r\n")
    _write_bytes_atomic(p, norm.encode("utf-8"))


def _format_read_chunk(chunk: List[str], start_1based: int, max_chars: int = MAX_READ_BYTES) -> Tuple[str, int]:
    """Format line-numbered chunk within `max_chars`, capping minified long lines cleanly."""
    out: List[str] = []
    used = 0
    lines_shown = 0
    for i, raw_line in enumerate(chunk):
        ln_no = start_1based + i
        has_nl = raw_line.endswith(("\n", "\r"))
        body = raw_line.rstrip("\r\n")
        if len(body) > 1500:
            body = body[:1460] + " ... [line truncated]"
        formatted = f"{ln_no:4d} | {body}" + ("\n" if has_nl or i < len(chunk) - 1 else "")
        if lines_shown > 0 and used + len(formatted) > max_chars:
            break
        out.append(formatted)
        used += len(formatted)
        lines_shown += 1
    result = "".join(out)
    if result and not result.endswith("\n"):
        result += "\n"
    return result, lines_shown


def _read_file(path: str, start_line: int = None, end_line: int = None) -> str:
    with _filesystem_lock:
        return _read_file_unlocked(path, start_line=start_line, end_line=end_line)


def _read_file_unlocked(path: str, start_line: int = None, end_line: int = None) -> str:
    p = _resolve(path)
    if not p.exists():
        return f"[Error] File not found: {path}"
    if not p.is_file():
        return f"[Error] Not a file: {path}"

    _mark_file_read_rel(p)

    try:
        raw = p.read_bytes()
    except Exception as e:
        return f"[Error] Cannot read: {e}"

    img_info = detect_image_info(raw, p.suffix)
    if img_info:
        dims = f"{img_info['width']}x{img_info['height']}" if img_info["width"] and img_info["height"] else "unknown"
        return (
            f"[Image file: {path} | {img_info['format']} {dims} | {len(raw):,} bytes]\n"
            f"Call inspect_image('{path}') to attach this image visually into context."
        )
    if b"\x00" in raw[:1024]:
        return f"[Binary file: {path} | {len(raw):,} bytes — cannot display binary bytes as UTF-8 text.]"

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    total_lines = len(lines)
    total_bytes = len(raw)

    if total_lines == 0:
        _active_file_ledger.get().record_read(_rel_posix(p), 1, 1, text, abs_path=p)
        return f"[Empty file: {path} (0 bytes)]"

    # Range read
    if start_line is not None or end_line is not None:
        s_req = max(1, int(start_line) if start_line is not None else 1)
        if s_req > total_lines:
            return f"[Info] '{path}' has {total_lines} lines (requested start_line={s_req} is past EOF)."
        e_req = min(total_lines, int(end_line) if end_line is not None else min(total_lines, s_req + MAX_READ_LINES - 1))
        if e_req < s_req:
            e_req = s_req
        if (e_req - s_req + 1) > MAX_READ_LINES:
            e_req = s_req + MAX_READ_LINES - 1
        s = s_req - 1
        chunk = lines[s:e_req]
        numbered, shown_cnt = _format_read_chunk(chunk, s + 1)
        actual_end = s + max(1, shown_cnt)
        _active_file_ledger.get().record_read(_rel_posix(p), s + 1, actual_end, text, abs_path=p)
        more = (
            f" — use start_line={actual_end + 1}, end_line={min(total_lines, actual_end + MAX_READ_LINES)} for more"
            if actual_end < total_lines and (end_line is None or actual_end < e_req)
            else ""
        )
        return numbered + f"[Showing lines {s + 1}-{actual_end} of {total_lines} ({total_bytes:,} bytes){more}]"

    # Full read — enforce byte/line limits so prompts fit cleanly in WSA web bridge
    if total_bytes > MAX_READ_BYTES or total_lines > MAX_READ_LINES:
        shown = lines[:MAX_READ_LINES]
        numbered, shown_cnt = _format_read_chunk(shown, 1)
        actual_end = max(1, shown_cnt)
        _active_file_ledger.get().record_read(_rel_posix(p), 1, actual_end, text, abs_path=p)
        next_end = min(total_lines, actual_end + MAX_READ_LINES)
        return (
            numbered
            + f"\n[Truncated: showing lines 1-{actual_end} of {total_lines} ({total_bytes:,} bytes). "
            f"Use start_line/end_line, e.g. read_file('{path}', {actual_end + 1}, {next_end})]"
        )

    _active_file_ledger.get().record_read(_rel_posix(p), 1, total_lines, text, abs_path=p)
    numbered, _ = _format_read_chunk(lines, 1, max_chars=MAX_READ_BYTES * 2)
    return numbered


def _get_file_outline(path: str) -> str:
    p = _resolve(path)
    if not p.exists():
        return f"[Error] File not found: {path}"
    if not p.is_file():
        return f"[Error] Not a file: {path}"
    try:
        raw = p.read_bytes()
    except Exception as exc:
        return f"[Error] Cannot read {path}: {exc}"
    if b"\x00" in raw[:1024]:
        return f"[Binary file: {path} | {len(raw):,} bytes]"
    text = raw.decode("utf-8", errors="replace")
    rel = _rel_posix(p)
    _active_file_ledger.get().record_outline(rel, text, abs_path=p)
    outline = extract_file_outline(p, text=text)
    return format_file_outline(rel, outline)


def _get_file_info(path: str) -> str:
    p = _resolve(path)
    if not p.exists():
        return f"[Error] Not found: {path}"
    if not p.is_file():
        return f"[Error] Not a file: {path}"
    stat = p.stat()
    size = stat.st_size
    modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.count("\n") + 1
    except Exception:
        lines = "unknown"
        text = None
    
    readable = "yes (< 100KB)" if size < MAX_READ_BYTES else f"NO — use start_line/end_line chunks of {MAX_READ_LINES}"
    info_str = (
        f"File:     {path}\n"
        f"Size:     {size:,} bytes ({size // 1024} KB)\n"
        f"Lines:    {lines:,}\n"
        f"Modified: {modified}\n"
        f"One-shot: {readable}"
    )
    
    # Add symbol outline for code files (Python/JS/TS)
    if text and size < 500_000:  # Only for reasonably-sized files
        outline = []
        try:
            for idx, line in enumerate(text.splitlines(), 1):
                # Match function/class definitions
                if re.match(r"^\s*(?:async\s+)?def\s+\w+|^\s*class\s+\w+|^\s*(?:export\s+)?(?:async\s+)?function\s+\w+", line):
                    outline.append(f"  L{idx:4d}: {line.strip()[:75]}")
                    if len(outline) >= 60:
                        outline.append("  ... (outline truncated at 60 symbols)")
                        break
        except Exception:
            pass
        
        if outline:
            info_str += "\n\nSymbol Outline:\n" + "\n".join(outline)
    
    return info_str


_LAZY_PLACEHOLDER_RE = re.compile(
    r"^[ \t]*(?:#|//|/\*|--|<!--)\s*(?:\.\.\.|…)?\s*(?:existing code|rest of (?:file|code|implementation)|"
    r"remaining code|unchanged code|omitted for brevity|code unchanged|keep existing)[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
_CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".java", ".go", ".rs", ".c", ".cpp", ".cs"}


def _contains_lazy_placeholder(path: str, content: str) -> Optional[str]:
    """Detect explicit AI truncation stubs like '# ... existing code ...' inside code edits."""
    p_low = str(path or "").replace("\\", "/").lower()
    if any(seg in p_low for seg in ("/tests/", "/test_", "test_")) or Path(p_low).suffix not in _CODE_EXTS:
        return None
    m = _LAZY_PLACEHOLDER_RE.search(content or "")
    if not m:
        return None
    stub = m.group(0).strip()[:100]
    return (
        f"[Error] Lazy placeholder rejected in {path}: '{stub}'. "
        "Provide complete runnable code or use surgical patch_file hunks targeting only changed lines."
    )


def _unified_diff_preview(path: str, old_text: str, new_text: str, max_lines: int = 44) -> str:
    """Build a bounded unified diff for review and TUI inline diff display."""
    diff_lines = list(difflib.unified_diff(
        old_text.splitlines(keepends=False),
        new_text.splitlines(keepends=False),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
        n=2,
    ))
    if len(diff_lines) > max_lines:
        diff_lines = diff_lines[: max_lines - 2] + ["... (diff truncated)"]
    return "\n".join(diff_lines) if diff_lines else "(no changes)"


def _find_unique_line_block(text: str, target: str) -> Tuple[Optional[str], int, list]:
    """Locate target in text (exact first, then trailing-whitespace-tolerant line match).

    Returns `(matched_exact_substring_or_None, match_count, 1_based_match_lines)`.
    """
    if not target:
        return None, 0, []
    exact_cnt = text.count(target)
    if exact_cnt == 1:
        return target, 1, []
    if exact_cnt > 1:
        match_lines = []
        pos = 0
        for _ in range(exact_cnt):
            idx = text.find(target, pos)
            match_lines.append(str(text[:idx].count("\n") + 1))
            pos = idx + 1
        return None, exact_cnt, match_lines

    # Fallback: normalize trailing horizontal whitespace per line when line count matches
    target_lines = target.splitlines()
    if not target_lines:
        return None, 0, []
    src_lines = text.splitlines(keepends=True)
    src_norm = [ln.rstrip("\r\n \t") for ln in src_lines]
    tgt_norm = [ln.rstrip("\r\n \t") for ln in target_lines]
    n = len(tgt_norm)
    hits = [
        i for i in range(0, len(src_norm) - n + 1)
        if src_norm[i : i + n] == tgt_norm
    ]
    if len(hits) == 1:
        start = hits[0]
        matched = "".join(src_lines[start : start + n])
        if not target.endswith("\n") and matched.endswith("\n"):
            matched = matched.rstrip("\r\n")
        return matched, 1, []
    if len(hits) > 1:
        return None, len(hits), [str(h + 1) for h in hits]
    return None, 0, []


_MAX_READ_FILES_SET = 2000


def _mark_file_read_rel(p: Path) -> None:
    with _filesystem_lock:
        try:
            rel_path = str(p.relative_to(_project_root))
        except ValueError:
            return
        _read_files_set.add(rel_path)
        if len(_read_files_set) > _MAX_READ_FILES_SET:
            trimmed = list(_read_files_set)[len(_read_files_set) - (_MAX_READ_FILES_SET // 2):]
            _read_files_set.clear()
            _read_files_set.update(trimmed)


def _write_file(path: str, content: str, force_overwrite: bool = False) -> str:
    with _filesystem_lock:
        return _write_file_unlocked(path, content, force_overwrite=force_overwrite)


def _write_file_unlocked(path: str, content: str, force_overwrite: bool = False) -> str:
    p = _resolve(path)
    existed = p.exists()
    rel = _rel_posix(p)

    placeholder_err = _contains_lazy_placeholder(path, content)
    if placeholder_err:
        return placeholder_err

    syntax_err = _validate_code_content(path, content)
    if syntax_err:
        return syntax_err

    raw_prev = p.read_bytes() if existed else None
    prev = raw_prev.decode("utf-8", errors="replace") if raw_prev is not None else None
    if existed and not force_overwrite and prev is not None:
        safe_ok, safe_err = _active_file_ledger.get().check_edit_safety(
            rel, p, enforce_read_before_edit=_strict_read_before_edit.get()
        )
        if not safe_ok:
            return safe_err
        prev_lines = prev.count("\n") + 1
        prev_bytes = len(prev.encode("utf-8", errors="replace"))
        is_code = p.suffix.lower() in _CODE_EXTS
        if (is_code and prev_lines > 25 and prev_bytes > 1000) or (prev_lines > 60 and prev_bytes > 2500):
            return (
                f"[Error] Surgical-edit policy: '{path}' already exists ({prev_lines} lines). "
                "Use patch_file(path, old_string, new_string) or patch_file(path, hunks=[...]) "
                "to replace ONLY the exact lines needing fixes instead of regenerating the entire file "
                "(or set force_overwrite=true only for an intentional complete rewrite)."
            )

    created_dirs: List[str] = []
    root_dir = get_project_root()
    parent = p.parent
    while parent != root_dir and parent != parent.parent and not parent.exists():
        created_dirs.append(_rel_posix(parent))
        parent = parent.parent
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_text_preserving_eol(p, content, raw_prev)
    rollback_err = _verify_self_edit_on_disk(p, existed, prev, raw_prev)
    if rollback_err:
        return rollback_err
    _record_file_pre_edit(path, p, existed, prev, raw_prev, created_dirs=created_dirs)

    _mark_file_read_rel(p)
    _active_file_ledger.get().record_write(
        rel,
        content,
        summary=("created" if not existed else "overwrote"),
        created=not existed,
        abs_path=p,
    )

    action = "Updated" if existed else "Created"
    lines = content.count("\n") + 1
    header = f"[OK] {action} {path} ({lines} lines, {len(content.encode())} bytes)"
    if existed and prev is not None:
        diff_str = _unified_diff_preview(path, prev, content)
        return f"{header}\n{diff_str}\n"
    return header


def _patch_file(
    path: str,
    old_string: str = None,
    new_string: str = None,
    hunks: list = None,
    start_line: int = None,
    end_line: int = None,
) -> str:
    with _filesystem_lock:
        return _patch_file_unlocked(
            path,
            old_string=old_string,
            new_string=new_string,
            hunks=hunks,
            start_line=start_line,
            end_line=end_line,
        )


def _patch_file_unlocked(
    path: str,
    old_string: str = None,
    new_string: str = None,
    hunks: list = None,
    start_line: int = None,
    end_line: int = None,
) -> str:
    p = _resolve(path)
    if not p.exists():
        return f"[Error] File not found: {path}"
    if not p.is_file():
        return f"[Error] Not a file: {path}"
    rel = _rel_posix(p)
    safe_ok, safe_err = _active_file_ledger.get().check_edit_safety(
        rel, p, enforce_read_before_edit=_strict_read_before_edit.get()
    )
    if not safe_ok:
        return safe_err

    norm_args, norm_err = normalize_patch_args({
        "path": path,
        "old_string": old_string,
        "new_string": new_string,
        "hunks": hunks,
        "start_line": start_line,
        "end_line": end_line,
    })
    if norm_err:
        return norm_err
    hunks = norm_args.get("hunks")
    old_string = norm_args.get("old_string")
    new_string = norm_args.get("new_string")
    start_line = norm_args.get("start_line")
    end_line = norm_args.get("end_line")

    raw_bytes = p.read_bytes()
    text = raw_bytes.decode("utf-8", errors="replace").replace("\r\n", "\n")

    # ── Strip line numbers if caller copied from read_file output ──────────────
    if old_string:
        old_string = _strip_line_number_prefixes(old_string)
    if new_string:
        new_string = _strip_line_number_prefixes(new_string)

    # ── Multi-hunk mode ───────────────────────────────────────────────────────
    if hunks:
        results = []
        working = text
        line_delta = 0  # net lines added/removed by prior hunks, so later start_line/end_line stay aligned
        for i, hunk in enumerate(hunks):
            old = hunk["old"]
            new = hunk["new"]
            h_start = hunk.get("start_line")
            h_end = hunk.get("end_line")
            old = _strip_line_number_prefixes(old)
            if new:
                new = _strip_line_number_prefixes(new)
                placeholder_err = _contains_lazy_placeholder(path, new)
                if placeholder_err:
                    return f"{placeholder_err} (hunk {i+1})"
            if h_start is not None or h_end is not None:
                all_lines = working.splitlines(keepends=True)
                adj_start = (int(h_start) + line_delta) if h_start else None
                adj_end = (int(h_end) + line_delta) if h_end else None
                s_idx = max(0, (adj_start - 1) if adj_start else 0)
                e_idx = min(len(all_lines), adj_end if adj_end else len(all_lines))
                prefix = "".join(all_lines[:s_idx])
                region = "".join(all_lines[s_idx:e_idx])
                suffix = "".join(all_lines[e_idx:])
                matched, count, match_lines = _find_unique_line_block(region, old)
                if count == 0:
                    hint = _fuzzy_context(working, old)
                    return f"[Error] Hunk {i+1}: exact string not found in {path} lines {s_idx+1}-{e_idx} (adjusted by prior hunks' line delta={line_delta}).\n{hint}"
                if count > 1 or matched is None:
                    return (
                        f"[Error] Hunk {i+1} ambiguous inside lines {s_idx+1}-{e_idx}: matches {count} locations. "
                        "Narrow start_line/end_line or add context lines."
                    )
                working = prefix + region.replace(matched, new, 1) + suffix
                line_delta += new.count("\n") - matched.count("\n")
            else:
                matched, count, match_lines = _find_unique_line_block(working, old)
                if count == 0:
                    hint = _fuzzy_context(working, old)
                    return (
                        f"[Error] Hunk {i+1}: exact string not found in {path}. "
                        f"Patch aborted (no changes written).\n{hint}"
                    )
                if count > 1 or matched is None:
                    return (
                        f"[Error] Hunk {i+1} ambiguous: old matches {count} locations in {path} "
                        f"(at lines {', '.join(match_lines)}). Add 3-5 surrounding context lines."
                    )
                working = working.replace(matched, new, 1)
                line_delta += new.count("\n") - matched.count("\n")
            results.append(f"  Hunk {i+1}: OK")
        syntax_err = _validate_code_content(path, working)
        if syntax_err:
            return syntax_err
        _write_text_preserving_eol(p, working, raw_bytes)
        rollback_err = _verify_self_edit_on_disk(p, True, text, raw_bytes)
        if rollback_err:
            return rollback_err
        _record_file_pre_edit(path, p, True, text, raw_bytes)
        _mark_file_read_rel(p)
        _active_file_ledger.get().record_write(
            rel, working, summary=f"patched {len(hunks)} hunk(s)", abs_path=p
        )
        diff_str = _unified_diff_preview(path, text, working)
        return (
            f"[OK] Patched {path} — {len(hunks)} hunks applied\n"
            + "\n".join(results)
            + f"\n{diff_str}\n"
        )

    # ── Single-hunk mode ──────────────────────────────────────────────────────
    if old_string is None:
        return "[Error] patch_file requires either old_string/new_string or hunks parameter."
    if new_string is None:
        new_string = ""
    placeholder_err = _contains_lazy_placeholder(path, new_string)
    if placeholder_err:
        return placeholder_err

    # Optional start_line/end_line window restricts matching to target region when disambiguating duplicates
    if start_line is not None or end_line is not None:
        all_lines = text.splitlines(keepends=True)
        s_idx = max(0, (int(start_line) - 1) if start_line else 0)
        e_idx = min(len(all_lines), int(end_line) if end_line else len(all_lines))
        prefix = "".join(all_lines[:s_idx])
        region = "".join(all_lines[s_idx:e_idx])
        suffix = "".join(all_lines[e_idx:])
        matched, count, match_lines = _find_unique_line_block(region, old_string)
        if count == 0:
            hint = _fuzzy_context(text, old_string)
            return f"[Error] Exact string not found in {path} lines {s_idx+1}-{e_idx}.\n{hint}"
        if count > 1 or matched is None:
            return (
                f"[Error] Ambiguous patch inside lines {s_idx+1}-{e_idx}: matches {count} locations. "
                "Narrow start_line/end_line or add 3-5 context lines."
            )
        new_text = prefix + region.replace(matched, new_string, 1) + suffix
    else:
        matched, count, match_lines = _find_unique_line_block(text, old_string)
        if count == 0:
            hint = _fuzzy_context(text, old_string)
            return (
                f"[Error] Exact string not found in {path}.\n"
                f"{hint}\n"
                "Tip: Call read_file with the relevant line range to get the exact text, then retry."
            )
        if count > 1 or matched is None:
            return (
                f"[Error] Ambiguous patch: old_string matches {count} locations in {path} "
                f"(at lines {', '.join(match_lines)}). "
                "Add 3-5 surrounding context lines (or pass start_line/end_line) to uniquely target the intended location."
            )
        new_text = text.replace(matched, new_string, 1)

    if new_text == text:
        return f"[Info] No changes needed in {path}: target text already matches replacement."
    syntax_err = _validate_code_content(path, new_text)
    if syntax_err:
        return syntax_err
    _write_text_preserving_eol(p, new_text, raw_bytes)
    rollback_err = _verify_self_edit_on_disk(p, True, text, raw_bytes)
    if rollback_err:
        return rollback_err
    _record_file_pre_edit(path, p, True, text, raw_bytes)
    _mark_file_read_rel(p)
    _active_file_ledger.get().record_write(rel, new_text, summary="patched hunk", abs_path=p)

    diff_str = _unified_diff_preview(path, text, new_text)
    return (
        f"[OK] Patched {path}\n"
        f"{diff_str}\n"
    )


def _rollback_apply_patch(snapshots: List[Tuple[Path, Optional[bytes]]]) -> Tuple[int, List[str]]:
    """Restore files touched by a failed apply_patch to their exact pre-patch bytes."""
    rolled = 0
    failures: List[str] = []
    for prev_path, prev_bytes in reversed(snapshots):
        try:
            if prev_bytes is None:
                if prev_path.exists():
                    prev_path.unlink()
            else:
                _write_bytes_atomic(prev_path, prev_bytes)
            rolled += 1
        except OSError as exc:
            failures.append(f"{prev_path}: {exc}")
    return rolled, failures


def _apply_patch(edits: List[Dict[str, Any]]) -> str:
    with _filesystem_lock:
        return _apply_patch_unlocked(edits)


def _apply_patch_unlocked(edits: List[Dict[str, Any]]) -> str:
    """Apply per-file edits atomically: all succeed, or every applied file is rolled back."""
    if not isinstance(edits, list) or not edits:
        return "[Validation Error] apply_patch requires a non-empty `edits` array."
    allowed_keys = {"path", "old_string", "new_string", "hunks", "start_line", "end_line"}
    seen: Set[str] = set()
    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            return f"[Validation Error] apply_patch edit {i} must be an object."
        raw_path = edit.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return f"[Validation Error] apply_patch edit {i} requires a non-empty 'path'."
        extra = sorted(k for k in edit if k not in allowed_keys)
        if extra:
            return f"[Validation Error] apply_patch edit {i} has unknown key(s): {', '.join(extra)}."
        try:
            key = str(_resolve(raw_path))
        except (OSError, ValueError, PermissionError):
            return f"[Validation Error] apply_patch edit {i} has an invalid or out-of-workspace path: {raw_path}"
        if key in seen:
            return (
                f"[Validation Error] apply_patch edit {i} repeats '{raw_path}'. "
                "Combine same-file changes into a single 'hunks' array instead."
            )
        seen.add(key)

    snapshots: List[Tuple[Path, Optional[bytes]]] = []
    sections: List[str] = []
    outer_cp = _active_checkpoint.get()
    if outer_cp is not None:
        outer_files_snap = dict(outer_cp.get("files") or {})
        outer_raw_snap = dict(outer_cp.get("raw") or {})
        has_outer_dirs = "created_dirs" in outer_cp
        outer_dirs_snap = list(outer_cp.get("created_dirs") or [])
    else:
        outer_files_snap = {}
        outer_raw_snap = {}
        has_outer_dirs = False
        outer_dirs_snap = []
        begin_edit_checkpoint(f"apply_patch: {len(edits)} file(s)")

    def _abort_rollback() -> str:
        rolled, failures = _rollback_apply_patch(snapshots)
        if failures:
            # Preserve the original byte snapshots until the filesystem lock
            # clears, so rollback can recover a partially restored transaction.
            recovery_cp = _active_checkpoint.get()
            if recovery_cp is not None:
                for target, raw in snapshots:
                    rel = _rel_posix(target)
                    recovery_cp["files"].setdefault(rel, raw.decode("utf-8", errors="replace") if raw is not None else None)
                    recovery_cp["raw"].setdefault(rel, raw)
            cp_id = None if outer_cp is not None else commit_edit_checkpoint()
            recovery = f"checkpoint {cp_id}" if cp_id else "the enclosing batch checkpoint"
            return (f"Partial rollback: restored {rolled} file(s); recovery retained in {recovery}. "
                    "Files still requiring restoration: " + "; ".join(failures))
        if outer_cp is not None:
            outer_cp["files"] = outer_files_snap
            outer_cp["raw"] = outer_raw_snap
            if has_outer_dirs:
                outer_cp["created_dirs"] = outer_dirs_snap
            else:
                outer_cp.pop("created_dirs", None)
        else:
            _active_checkpoint.set(None)
        return f"Rolled back {rolled} previously-applied file(s). No files remain modified by this call."

    try:
        for i, edit in enumerate(edits, 1):
            target = _resolve(edit["path"])
            prev_bytes = target.read_bytes() if target.is_file() else None
            snapshots.append((target, prev_bytes))
            result = _patch_file_unlocked(
                path=edit["path"],
                old_string=edit.get("old_string"),
                new_string=edit.get("new_string"),
                hunks=edit.get("hunks"),
                start_line=edit.get("start_line"),
                end_line=edit.get("end_line"),
            )
            if not result.startswith(("[OK]", "[Info]")):
                note = _abort_rollback()
                return (
                    f"[Error] apply_patch aborted at edit {i} ({edit['path']}); {note}. "
                    f"\n{result}"
                )
            sections.append(f"── edit {i}: {edit['path']} ──\n{result}")
        cp_id = None if outer_cp is not None else commit_edit_checkpoint()
    except Exception as exc:
        note = _abort_rollback()
        return (
            f"[Error] apply_patch failed unexpectedly ({type(exc).__name__}: {exc}); "
            f"{note}"
        )

    header = f"[OK] Applied {len(edits)} edit(s) atomically"
    if cp_id:
        header += f" (checkpoint {cp_id})"
    return _truncate_tool_output(
        header + "\n\n" + "\n\n".join(sections), max_chars=24_000, label="apply_patch"
    )


def _fuzzy_context(text: str, old_string: str) -> str:
    """Find the closest matching line and return ±15 lines of context."""
    first_line = old_string.strip().splitlines()[0].strip() if old_string.strip() else ""
    all_lines = text.splitlines()
    closest = 0
    if first_line:
        for i, line in enumerate(all_lines):
            if first_line[:30] in line:
                closest = i
                break
        else:
            # Try partial match
            for i, line in enumerate(all_lines):
                if any(word in line for word in first_line.split()[:3] if len(word) > 3):
                    closest = i
                    break
    start = max(0, closest - 12)
    end = min(len(all_lines), closest + 15)
    context_lines = "\n".join(f"{start+i+1}: {l}" for i, l in enumerate(all_lines[start:end]))
    return f"Closest context (lines {start+1}–{end}):\n{context_lines}"


def _glob_to_regex(pattern: str) -> re.Pattern:
    pat = (pattern or "*").replace("\\", "/").strip()
    while pat.startswith("./"):
        pat = pat[2:]
    if "/" not in pat:
        pat = f"**/{pat}"
    out = ["^"]
    i, n = 0, len(pat)
    while i < n:
        if pat[i : i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif pat[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def _glob_files(pattern: str, max_results: int = 150) -> str:
    with _filesystem_lock:
        return _glob_files_unlocked(pattern, max_results=max_results)


def _glob_files_unlocked(pattern: str, max_results: int = 150) -> str:
    cap = max(1, min(int(max_results or 150), 500))
    pat = (pattern or "*").replace("\\", "/").strip()
    while pat.startswith("./"):
        pat = pat[2:]
    wants_media = Path(pat).suffix.lower() in _IMAGE_EXT_MIME
    results: List[str] = []
    truncated = False
    try:
        rx = _glob_to_regex(pat)
        for p, rel in _iter_workspace_files(get_project_root(), skip_media_ignore=wants_media):
            if not rx.match(rel):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            size_str = f"{size}B" if size < 1024 else f"{size // 1024}KB"
            results.append(f"{rel}  ({size_str})")
            if len(results) >= cap:
                truncated = True
                break
    except Exception as e:
        return f"[Error] glob failed: {e}"
    if truncated:
        results.append(f"... (truncated at {cap} results; narrow glob pattern if needed)")
    return _truncate_tool_output("\n".join(results), max_chars=12_000, label="glob_files") if results else f"[No files matching: {pattern}]"


def _list_dir(path: str = ".", recursive: bool = False, max_depth: Optional[int] = None, max_entries: int = 180) -> str:
    with _filesystem_lock:
        return _list_dir_unlocked(path, recursive=recursive, max_depth=max_depth, max_entries=max_entries)


def _list_dir_unlocked(path: str = ".", recursive: bool = False, max_depth: Optional[int] = None, max_entries: int = 180) -> str:
    p = _resolve(path)
    if not p.exists():
        return f"[Error] Not found: {path}"
    if not p.is_dir():
        return f"[Error] Not a directory: {path}"

    root_res = get_project_root().resolve()
    entries: List[str] = []
    used_chars = 0
    budget_chars = 8_000

    def _push(line: str) -> bool:
        nonlocal used_chars
        entries.append(line)
        used_chars += len(line) + 1
        return len(entries) < eff_cap and used_chars < budget_chars

    if recursive:
        eff_depth = 4 if max_depth is None else max(1, min(int(max_depth), 10))
        eff_cap = max(20, min(int(max_entries or 180), 400))
        per_dir_file_cap = 25
        truncated = False
        for cur_root, dirs, files in os.walk(p.resolve()):
            cur_p = Path(cur_root)
            try:
                depth = len(cur_p.relative_to(p.resolve()).parts)
            except ValueError:
                depth = 0
            try:
                rel_dir = str(cur_p.relative_to(root_res)).replace("\\", "/")
            except ValueError:
                rel_dir = ""
            rel_dir = "" if rel_dir == "." else rel_dir

            kept_dirs = []
            for d in sorted(dirs):
                d_rel = f"{rel_dir}/{d}" if rel_dir else d
                if not _is_path_ignored(d_rel, True, root_res):
                    kept_dirs.append(d)

            if depth >= eff_depth:
                if kept_dirs:
                    prefix = f"{rel_dir}/" if rel_dir else ""
                    if not _push(f"{prefix}... ({len(kept_dirs)} subdirs deeper than depth {eff_depth}; pass path='<dir>' or max_depth={eff_depth + 2})"):
                        truncated = True
                        break
                dirs[:] = []
            else:
                dirs[:] = kept_dirs

            visible_files = []
            for f in sorted(files):
                f_rel = f"{rel_dir}/{f}" if rel_dir else f
                if not _is_path_ignored(f_rel, False, root_res):
                    visible_files.append(f_rel)

            for idx, f_rel in enumerate(visible_files):
                if idx >= per_dir_file_cap:
                    rem = len(visible_files) - per_dir_file_cap
                    scope_lbl = rel_dir or "."
                    if not _push(f"{scope_lbl}/... (+{rem} more files; call list_dir('{scope_lbl}') or glob_files)"):
                        truncated = True
                    break
                if not _push(f_rel):
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            entries.append(f"... (listing capped at {len(entries)} entries / {used_chars:,} chars; use list_dir('<subdir>') or glob_files)")
    else:
        eff_cap = max(20, min(int(max_entries or 100), 200))
        try:
            rel_base = str(p.resolve().relative_to(root_res)).replace("\\", "/")
        except ValueError:
            rel_base = ""
        rel_base = "" if rel_base == "." else rel_base
        items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        shown_count = 0
        for item in items:
            is_dir = item.is_dir()
            rel_str = f"{rel_base}/{item.name}" if rel_base else item.name
            if _is_path_ignored(rel_str, is_dir, root_res):
                continue
            shown_count += 1
            if shown_count > eff_cap or used_chars >= budget_chars:
                entries.append(f"... (truncated at {eff_cap} entries; use glob_files to filter)")
                break
            if is_dir:
                line = f"{rel_str}/"
            else:
                try:
                    size = item.stat().st_size
                except OSError:
                    size = 0
                size_str = f"{size}B" if size < 1024 else f"{size // 1024}KB"
                line = f"{rel_str}  ({size_str})"
            entries.append(line)
            used_chars += len(line) + 1
    return "\n".join(entries) if entries else f"[Empty directory: {path}]"


def _search_files(
    pattern: str,
    file_glob: str = None,
    case_sensitive: bool = True,
    path: str = ".",
    max_results: int = 80,
) -> str:
    with _filesystem_lock:
        return _search_files_unlocked(
            pattern,
            file_glob=file_glob,
            case_sensitive=case_sensitive,
            path=path,
            max_results=max_results,
        )


def _search_files_unlocked(
    pattern: str,
    file_glob: str = None,
    case_sensitive: bool = True,
    path: str = ".",
    max_results: int = 80,
) -> str:
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return f"[Error] Invalid regex: {e}"
    target = _resolve(path or ".")
    if not target.exists():
        return f"[Error] Not found: {path}"
    cap = max(1, min(int(max_results or 80), 300))
    results: List[str] = []

    if target.is_file():
        candidates = [(target, _rel_posix(target))]
    else:
        candidates = _iter_workspace_files(target)

    for fpath, rel in candidates:
        fname = fpath.name
        if file_glob and not (fnmatch.fnmatch(fname, file_glob) or fnmatch.fnmatch(rel, file_glob)):
            continue
        try:
            if fpath.stat().st_size > MAX_SEARCH_FILE_BYTES:
                continue
            raw = fpath.read_bytes()
        except Exception:
            continue
        if b"\x00" in raw[:1024]:
            continue
        text = raw.decode("utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                snippet = line.rstrip()
                if len(snippet) > 220:
                    snippet = snippet[:217] + "..."
                results.append(f"{rel}:{i}: {snippet}")
                if len(results) >= cap:
                    results.append(f"[Truncated at {cap} matches — narrow path or pattern]")
                    return _truncate_tool_output("\n".join(results), max_chars=12_000, label="search_files")
    return _truncate_tool_output("\n".join(results), max_chars=12_000, label="search_files") if results else f"[No matches for: {pattern}]"


_PS_UTF8_PREFIX = (
    "[Console]::InputEncoding = [Console]::OutputEncoding = "
    "[System.Text.UTF8Encoding]::new($false); "
    "$OutputEncoding = [System.Text.UTF8Encoding]::new($false)"
)

_SELF_PRESERVATION_RULES: List[Tuple[str, re.Pattern]] = [
    (
        "taskkill-image-python",
        re.compile(
            r"\btaskkill\b[^|]*(?:/IM\s+[\"']?python[^/\s;|&]*|"
            r"/FI\s+[\"'][^\"']*IMAGENAME[^\"']*python[^\"']*[\"'])",
            re.IGNORECASE,
        ),
    ),
    (
        "stop-process-python-name",
        re.compile(
            r"\b(?:Stop-Process|kill|spps)\b[^|]*(?:-Name\b\s*[\"']?python|\b-ProcessName\b\s*[\"']?python)"
            r"|^\s*(?:Stop-Process|kill|spps)\b(?![^|]*\s-Id\b)[^|]*\bpython(?:\.exe|\*)?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "get-process-python-pipeline-kill",
        re.compile(
            r"\b(?:Get-Process|gps)\b[^|]*\bpython\b[^|]*\|\s*"
            r"(?:ForEach-Object[^|]*\b(?:Stop-Process|kill|spps|Kill)\b|(?:Stop-Process|kill|spps)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "wmic-or-cim-python-terminate",
        re.compile(
            r"(?:\bwmic\b[^|]*python[^|]*\b(?:delete|terminate)\b|"
            r"\b(?:Get-CimInstance|Get-WmiObject|gwmi|gcim)\b[^|]*Win32_Process[^|]*python[^|]*"
            r"\|\s*[^|]*\b(?:Terminate|Invoke-CimMethod|Remove-CimInstance)\b)",
            re.IGNORECASE,
        ),
    ),
]


def _wrap_powershell_utf8(command: str) -> str:
    """Configure UTF-8 and preserve native failures through PowerShell -Command.

    Capture $? before another statement overwrites it. A successful final
    command still succeeds even if an earlier native command failed.
    """
    return (f"{_PS_UTF8_PREFIX}\n$global:LASTEXITCODE = 0\n"
            "$wsaErrorCount = $Error.Count\n"
            f"{command}\n"
            "$wsaSucceeded = $?\n"
            "if ($wsaSucceeded) { exit 0 }\n"
            "if ($Error.Count -gt $wsaErrorCount -and "
            "$Error[0].FullyQualifiedErrorId -notlike 'NativeCommandError*') { exit 1 }\n"
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
            "exit 1")


def _subprocess_utf8_env() -> Dict[str, str]:
    return {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
        "WSA_ACTIVE_SESSION": "1",
        "GWA_ACTIVE_SESSION": "1",
    }


def _get_bridge_port() -> int:
    try:
        parsed = urllib.parse.urlsplit(_bridge_api_base or "http://127.0.0.1:8081")
        if parsed.port:
            return int(parsed.port)
    except Exception:
        pass
    return 8081


def _get_listening_pids_on_port(port: int) -> List[int]:
    """Return sorted unique PIDs currently LISTENING on TCP `port`."""
    try:
        target = int(port)
    except (TypeError, ValueError):
        return []
    pids: List[int] = []
    seen: Set[int] = set()
    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=6,
                check=False,
            )
            netstat_out = proc.stdout or ""
            proc6 = subprocess.run(
                ["netstat", "-ano", "-p", "tcpv6"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=6,
                check=False,
            )
            netstat_out = f"{netstat_out}\n{proc6.stdout or ''}"
            for line in netstat_out.splitlines():
                parts = line.split()
                if len(parts) < 5:
                    continue
                proto, local_addr, _, state, pid_str = parts[0], parts[1], parts[2], parts[3], parts[4]
                if not proto.upper().startswith("TCP") or state.upper() != "LISTENING":
                    continue
                if ":" not in local_addr:
                    continue
                addr_port = local_addr.rsplit(":", 1)[1]
                if addr_port == str(target) and pid_str.isdigit():
                    pid = int(pid_str)
                    if pid > 0 and pid not in seen:
                        seen.add(pid)
                        pids.append(pid)
        except (OSError, subprocess.SubprocessError):
            pass
    elif shutil.which("lsof"):
        try:
            proc = subprocess.run(
                ["lsof", "-ti", f"tcp:{target}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=6,
                check=False,
            )
            for tok in (proc.stdout or "").split():
                if tok.isdigit():
                    pid = int(tok)
                    if pid > 0 and pid not in seen:
                        seen.add(pid)
                        pids.append(pid)
        except (OSError, subprocess.SubprocessError):
            pass
    return sorted(pids)


def _protected_self_pids() -> Set[int]:
    pids = {os.getpid()}
    bridge_port = _get_bridge_port()
    for p in _get_listening_pids_on_port(bridge_port):
        pids.add(p)
    return pids


def _is_tcp_port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _check_recursive_agent_guard(command: str) -> Optional[str]:
    if re.search(
        r"\b(?:wsa\.bat|wsa\.ps1|start\.bat|start\.ps1)\b"
        r"|python(?:\.exe)?\s+-m\s+(?:wsa_agent|wsa_server)",
        command or "",
        re.IGNORECASE,
    ):
        return (
            "[Recursive Agent Guard] Blocked launching an interactive or nested WSA agent "
            "inside subprocess execution."
        )
    return None


def _check_self_preservation_guard(command: str) -> Optional[str]:
    """Block broad python termination or WSA self-PID kills, naming exact rule & match."""
    bridge_port = _get_bridge_port()
    clauses = [c.strip() for c in re.split(r"(?:;|\r?\n|&&|\|\|)+", command or "") if c.strip()]
    protected_pids = None  # resolved lazily if an explicit PID target is spotted

    for clause in clauses:
        # Inspect explicit single/list PID kills (Stop-Process -Id 123, taskkill /PID 123, Where-Object Id -eq 123)
        for pid_match in re.finditer(
            r"(?:\b(?:Stop-Process|spps|kill)\b[^|]*?-Id\s+([0-9,\s]+)"
            r"|\btaskkill\b[^|]*?/PID\s+(\d+)"
            r"|\.Id\s*-eq\s*(\d+))",
            clause,
            re.IGNORECASE,
        ):
            raw_nums = next((g for g in pid_match.groups() if g), "")
            if protected_pids is None:
                protected_pids = _protected_self_pids()
            for num_str in re.findall(r"\d+", raw_nums):
                pid_val = int(num_str)
                if pid_val in protected_pids:
                    return (
                        f"[Self-Preservation Guard] Blocked targeted kill of PID {pid_val} "
                        f"(rule: protected-self-pid; matched: {pid_match.group(0).strip()!r}) "
                        f"because it belongs to the active WSA agent/bridge (port {bridge_port})."
                    )

        # If a pipeline filters to a literal PID via Where-Object (.Id -eq <pid>), permit it when non-WSA
        has_single_pid_filter = bool(
            re.search(r"\bWhere-Object\b[^|]*\.Id\s*-eq\s*\d+\b", clause, re.IGNORECASE)
        )
        for rule_name, pattern in _SELF_PRESERVATION_RULES:
            m = pattern.search(clause)
            if not m:
                continue
            if rule_name == "get-process-python-pipeline-kill" and has_single_pid_filter:
                continue
            return (
                f"[Self-Preservation Guard] Blocked command that risks terminating all `python` processes "
                f"(rule: {rule_name}; matched: {m.group(0).strip()!r}) because it would kill the active WSA agent "
                f"and bridge server (port {bridge_port}). "
                "Alternatives: use `restart_service(port=...)`, `stop_background(name)`, or pass an explicit "
                "non-WSA PID to `Stop-Process -Id <pid>` / `taskkill /F /PID <pid>`."
            )
    return None


def _format_stream_sections(stdout: str, stderr: str) -> List[str]:
    clean_out = _ANSI_ESCAPE_RE.sub("", stdout or "").strip()
    clean_err = _ANSI_ESCAPE_RE.sub("", stderr or "").strip()
    sections: List[str] = []
    if clean_out:
        sections.append(f"STDOUT:\n{_truncate_tool_output(clean_out, max_chars=12_000, label='STDOUT')}")
    if clean_err:
        sections.append(f"STDERR:\n{_truncate_tool_output(clean_err, max_chars=6_000, label='STDERR')}")
    return sections


def _format_exec_output(
    result_code: int,
    stdout: str,
    stderr: str,
    *,
    command: str = "",
    argv: Optional[List[str]] = None,
) -> str:
    """Strip ANSI escape sequences, classify outcome, and bound output cleanly."""
    clean_out = _ANSI_ESCAPE_RE.sub("", stdout or "").strip()
    clean_err = _ANSI_ESCAPE_RE.sub("", stderr or "").strip()
    parts = _format_stream_sections(stdout, stderr)
    parts.append(f"Exit code: {result_code}")
    meta = classify_exec_result(result_code, clean_out, clean_err, command=command, argv=argv)
    rc_class = meta["result_class"]
    if rc_class == "ok":
        parts.append("Status: completed (ok)")
    elif rc_class == "no_match":
        parts.append("Status: completed (no_match — negative probe/empty match, not a fatal error)")
    elif rc_class == "differences_found":
        parts.append("Status: completed (differences_found — non-zero exit signals diff present)")
    elif rc_class == "no_tests":
        parts.append("Status: completed (no_tests — pytest collected 0 tests)")
    else:
        parts.append(f"Status: {meta['execution_status']} ({rc_class})")
    return _truncate_tool_output("\n".join(parts), max_chars=16_000, label="command output")


def _format_timeout_output(prefix_msg: str, stdout: str, stderr: str) -> str:
    """Include partial STDOUT/STDERR captured before timeout alongside the timeout error."""
    sections = _format_stream_sections(stdout, stderr)
    if not sections:
        return f"{prefix_msg}\nStatus: timed_out (timeout)\n(No stdout/stderr was captured before timeout.)"
    combined = (
        f"{prefix_msg}\nStatus: timed_out (timeout)\nPartial output captured before timeout:\n"
        + "\n".join(sections)
    )
    return _truncate_tool_output(combined, max_chars=16_000, label="partial timeout output")


def _encode_powershell_command(script: str) -> str:
    """Encode a multi-line PowerShell script cleanly as UTF-16LE Base64 for WinPS 5.1."""
    return base64.b64encode((script or "").encode("utf-16-le")).decode("ascii")


def _run_command(
    command: Optional[str] = None,
    timeout: int = 120,
    argv: Optional[List[str]] = None,
    cwd: Optional[str] = None,
) -> str:
    timeout = min(int(timeout), 600)
    run_cwd: Optional[Path] = None
    if cwd is not None and str(cwd).strip():
        resolved_cwd = _resolve(str(cwd))
        if not resolved_cwd.exists() or not resolved_cwd.is_dir():
            return f"[Error] Working directory not found or not a directory: {cwd}"
        run_cwd = resolved_cwd

    if argv is not None:
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x for x in argv):
            return "[Validation Error] run_command 'argv' must be a non-empty list of strings."
        joined = " ".join(argv)
        guard_err = _check_self_preservation_guard(joined)
        if guard_err:
            return guard_err
        rec_err = _check_recursive_agent_guard(joined)
        if rec_err:
            return rec_err
        exe = argv[0]
        if exe in ("python", "python3") and not os.path.isabs(exe):
            resolved_exe = _detect_python_executable(_project_root)
        else:
            resolved_exe = shutil.which(exe) or exe
        cmd_list = [resolved_exe, *argv[1:]]
        try:
            result_code, stdout, stderr, timed_out = (
                _run_subprocess_tree_safe(cmd_list, timeout, cwd=run_cwd)
                if run_cwd is not None
                else _run_subprocess_tree_safe(cmd_list, timeout)
            )
            if timed_out:
                return _format_timeout_output(
                    f"[Error] Timed out after {timeout}s (process tree terminated cleanly).",
                    stdout,
                    stderr,
                )
            return _format_exec_output(result_code, stdout, stderr, argv=argv)
        except FileNotFoundError:
            return (
                f"[Error] Executable not found for argv[0]={exe!r}.\n"
                "Status: completed (command_not_found — pass 'command' for a shell builtin)"
            )
        except Exception as e:
            return f"[Error] {type(e).__name__}: {e}"

    if not isinstance(command, str) or not command.strip():
        return "[Validation Error] run_command requires a non-empty 'command' or 'argv'."
    guard_err = _check_self_preservation_guard(command)
    if guard_err:
        return guard_err
    rec_err = _check_recursive_agent_guard(command)
    if rec_err:
        return rec_err
    if sys.platform != "win32":
        ps_cmd = ["/bin/sh", "-c", command]
    else:
        wrapped = _wrap_powershell_utf8(command)
        if "\n" in command or "\r" in command:
            ps_cmd = ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", _encode_powershell_command(wrapped)]
        else:
            ps_cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", wrapped]
    try:
        result_code, stdout, stderr, timed_out = (
            _run_subprocess_tree_safe(ps_cmd, timeout, cwd=run_cwd)
            if run_cwd is not None
            else _run_subprocess_tree_safe(ps_cmd, timeout)
        )
        if timed_out:
            return _format_timeout_output(
                f"[Error] Timed out after {timeout}s (process tree terminated cleanly).",
                stdout,
                stderr,
            )
        out = _format_exec_output(result_code, stdout, stderr, command=command)
        if (
            sys.platform == "win32" and result_code != 0
            and re.search(r"\bpython(?:\.exe)?\b[^\r\n]*\s-c\b", command, re.I)
            and any(tok in out for tok in ("SyntaxError", "ParserError", "UnexpectedToken"))
        ):
            out += "\nHint: For multi-line Python, call run_python(code=...) or run_command(argv=...) to avoid PowerShell quoting."
        return out
    except Exception as e:
        return f"[Error] {type(e).__name__}: {e}"


def _kill_process_tree(pid: int) -> None:
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def _drain_stream_chunks(pipe, sink: List[str], limit_chars: int = 400_000) -> None:
    """Continuously read stream lines into `sink` so timeout paths retain partial output."""
    if pipe is None:
        return
    total = 0
    try:
        while True:
            line = pipe.readline(8192)
            if not isinstance(line, str) or line == "":
                break
            if total < limit_chars:
                take = line[: max(0, limit_chars - total)]
                sink.append(take)
                total += len(take)
    except (OSError, ValueError):
        pass


def _run_subprocess_tree_safe(
    cmd_list: list,
    timeout: int,
    cwd: Optional[Path] = None,
) -> Tuple[int, str, str, bool]:
    env = _subprocess_utf8_env()
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        cmd_list,
        cwd=str(cwd or get_project_root()),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=flags,
        **({} if sys.platform == "win32" else {"start_new_session": True}),
    )
    if not isinstance(proc, _REAL_POPEN_CLS):
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return proc.returncode, stdout or "", stderr or "", False
        except KeyboardInterrupt:
            _kill_process_tree(proc.pid)
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.communicate(timeout=0.5)
            except Exception:
                pass
            raise
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc.pid)
            try:
                proc.kill()
            except OSError:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                stdout, stderr = "", ""
            return -1, stdout or "", stderr or "", True

    out_chunks: List[str] = []
    err_chunks: List[str] = []
    t_out = threading.Thread(target=_drain_stream_chunks, args=(proc.stdout, out_chunks), daemon=True)
    t_err = threading.Thread(target=_drain_stream_chunks, args=(proc.stderr, err_chunks), daemon=True)
    t_out.start()
    t_err.start()
    try:
        rc = proc.wait(timeout=timeout)
        t_out.join(timeout=2.0)
        t_err.join(timeout=2.0)
        return rc, "".join(out_chunks), "".join(err_chunks), False
    except KeyboardInterrupt:
        _kill_process_tree(proc.pid)
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=0.5)
        except Exception:
            pass
        t_out.join(timeout=0.5)
        t_err.join(timeout=0.5)
        raise
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc.pid)
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=1.5)
        except (OSError, subprocess.SubprocessError):
            pass
        t_out.join(timeout=1.5)
        t_err.join(timeout=1.5)
        return -1, "".join(out_chunks), "".join(err_chunks), True
    finally:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def _delete_file(path: str) -> str:
    with _filesystem_lock:
        return _delete_file_unlocked(path)


def _delete_file_unlocked(path: str) -> str:
    p = _resolve(path)
    if not p.exists():
        return f"[Error] Not found: {path}"
    if p.is_dir():
        return f"[Error] '{path}' is a directory. Use run_command with Remove-Item -Recurse."
    raw_prev: Optional[bytes] = None
    prev: Optional[str] = None
    try:
        raw_prev = p.read_bytes()
        prev = raw_prev.decode("utf-8", errors="replace")
    except OSError as exc:
        return f"[Error] Cannot back up '{path}' before deletion; file was not deleted: {exc}"
    rel = _rel_posix(p)
    p.unlink()
    if prev is not None:
        _record_file_pre_edit(path, p, True, prev, raw_prev)
    _active_file_ledger.get().mark_deleted(rel)
    return f"[OK] Deleted: {path}"


def _write_transfer_bytes(path: str, raw: bytes) -> None:
    """Copy exact bytes while retaining the usual recovery and ledger state."""
    dst = _resolve(path)
    existed = dst.exists()
    previous = dst.read_bytes() if existed else None
    text = raw.decode("utf-8", errors="replace")
    created_dirs: List[str] = []
    parent = dst.parent
    root = get_project_root()
    while parent != root and parent != parent.parent and not parent.exists():
        created_dirs.append(_rel_posix(parent))
        parent = parent.parent
    dst.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes_atomic(dst, raw)
    _record_file_pre_edit(path, dst, existed,
                          previous.decode("utf-8", errors="replace") if previous is not None else None,
                          previous, created_dirs=created_dirs)
    _mark_file_read_rel(dst)
    _active_file_ledger.get().record_write(_rel_posix(dst), text,
                                         summary="copied", created=not existed, abs_path=dst)


def _copy_file(path: str, new_path: str, overwrite: bool = False) -> str:
    """Copy a file through the guarded write path (checkpointed, path-guarded, no shell)."""
    with _filesystem_lock:
        src = _resolve(path)
        dst = _resolve(new_path)
        if not src.exists():
            return f"[Error] Source not found: {path}"
        if not src.is_file():
            return f"[Error] '{path}' is not a file. Use run_command for directory copies."
        if src == dst:
            return f"[Error] Source and destination are the same file: {path}"
        if dst.exists() and not overwrite:
            return f"[Error] Destination exists: {new_path}. Pass overwrite=true to replace it."
        if dst.is_dir():
            return f"[Error] Destination is a directory: {new_path}"
        try:
            content = src.read_bytes()
        except OSError as exc:
            return f"[Error] Cannot read '{path}': {exc}"
        _write_transfer_bytes(new_path, content)
        return f"[OK] Copied {path} -> {new_path} ({len(content)} bytes)"


def _move_file(path: str, new_path: str, overwrite: bool = False) -> str:
    """Move/rename a file: guarded copy to destination, then guarded delete of source."""
    with _filesystem_lock:
        src = _resolve(path)
        dst = _resolve(new_path)
        if not src.exists():
            return f"[Error] Source not found: {path}"
        if not src.is_file():
            return f"[Error] '{path}' is not a file. Use run_command for directory moves."
        if src == dst:
            return f"[Error] Source and destination are the same file: {path}"
        if dst.exists() and not overwrite:
            return f"[Error] Destination exists: {new_path}. Pass overwrite=true to replace it."
        if dst.is_dir():
            return f"[Error] Destination is a directory: {new_path}"
        try:
            content = src.read_bytes()
        except OSError as exc:
            return f"[Error] Cannot read '{path}': {exc}"
        own_checkpoint = _active_checkpoint.get() is None
        if own_checkpoint:
            begin_edit_checkpoint(f"move {path} -> {new_path}")
        try:
            _write_transfer_bytes(new_path, content)
            removed = _delete_file_unlocked(path)
            if not removed.startswith("[OK]"):
                return f"[Error] Copied {new_path} but source was not removed: {removed}. Recovery checkpoint retained."
        except OSError as exc:
            return f"[Error] Move incomplete: {exc}. Recovery checkpoint retained."
        finally:
            if own_checkpoint:
                commit_edit_checkpoint()
        return f"[OK] Moved {path} -> {new_path} ({len(content)} bytes)"


_GIT_READ_ACTIONS = frozenset({"status", "diff", "log", "show", "blame", "rev-parse", "branch", "remote", "config", "ls-files"})
_GIT_WRITE_ACTIONS = frozenset({"add", "commit", "stash", "restore", "switch", "tag"})
_GIT_BLOCKED_ACTIONS = frozenset({
    "push", "pull", "fetch", "reset", "clean", "rebase", "merge", "cherry-pick",
    "filter-branch", "gc", "reflog", "submodule", "worktree", "clone", "init", "apply", "am",
})


def _git(action: str = "status", args: Optional[List[str]] = None, timeout: int = 60) -> str:
    """Run an allowlisted git subcommand as an argv list (never a shell string)."""
    sub = str(action or "status").strip().lower()
    if sub in _GIT_BLOCKED_ACTIONS:
        return (
            f"[Error] git {sub} is blocked by policy (destructive or network history operation). "
            "Ask the user to run it manually if it is truly intended."
        )
    if sub not in _GIT_READ_ACTIONS and sub not in _GIT_WRITE_ACTIONS:
        return f"[Error] Unsupported git action '{action}'. Allowed: {', '.join(sorted(_GIT_READ_ACTIONS | _GIT_WRITE_ACTIONS))}"
    if args is not None and (not isinstance(args, list) or any(not isinstance(a, str) for a in args)):
        return "[Validation Error] git 'args' must be a list of strings."
    root = get_project_root()
    if not (root / ".git").exists():
        return f"[Error] Not a git repository: {root}"
    cmd = ["git", "-c", "core.pager=cat", "--no-pager", sub, *(args or [])]
    if sub in {"diff", "log", "show"}:
        cmd = ["git", "-c", "core.pager=cat", "--no-pager", sub,
               "--no-ext-diff", "--no-textconv", *(args or [])]
    try:
        proc = subprocess.run(
            cmd, cwd=str(root), capture_output=True, text=True,
            timeout=max(5, min(int(timeout or 60), 300)), encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"[Error] git {sub} timed out after {timeout}s"
    except FileNotFoundError:
        return "[Error] git executable not found on PATH"
    except Exception as exc:
        return f"[Error] git {sub} failed ({type(exc).__name__}): {exc}"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    output = _format_exec_output(proc.returncode, out, err, argv=cmd)
    if proc.returncode:
        output = "[Error] Git command returned a nonzero exit code.\n" + output
    return output


_WEB_FETCH_BLOCKED_HOSTS = frozenset({"localhost", "localhost.localdomain", "metadata.google.internal"})
_WEB_FETCH_MAX_BYTES = 1_048_576


def _is_blocked_fetch_host(host: str) -> Tuple[bool, str]:
    """Reject loopback/private/link-local/metadata targets to prevent SSRF."""
    h = (host or "").strip().strip("[]").lower().rstrip(".")
    if not h:
        return True, "[Error] web_fetch requires an absolute URL with a host."
    if h in _WEB_FETCH_BLOCKED_HOSTS:
        return True, f"[Error] web_fetch refuses local host '{h}'. Use http_request for localhost."
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False, ""
    if (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip == ipaddress.IPv4Address("169.254.169.254"))
    ):
        return True, f"[Error] web_fetch refuses private/reserved address {ip}. Use http_request for localhost."
    return False, ""


class _PublicOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-apply the SSRF blocklist on every redirect hop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        blocked, err = _is_blocked_fetch_host(parsed.hostname or "")
        if blocked:
            raise urllib.error.URLError(err.replace("[Error] ", "[Safety Error] ", 1))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _web_fetch(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[str] = None,
    timeout: int = 20,
    max_chars: int = 24_000,
) -> str:
    """Fetch a public http(s) URL with an SSRF guard and bounded output."""
    raw_url = str(url or "").strip()
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme.lower() not in ("http", "https"):
        return "[Error] web_fetch only supports http:// and https:// URLs."
    if parsed.username or parsed.password:
        return "[Error] web_fetch refuses URLs with embedded credentials."
    blocked, err = _is_blocked_fetch_host(parsed.hostname or "")
    if blocked:
        return err
    meth = str(method or "GET").strip().upper()
    if meth not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        return f"[Error] Unsupported HTTP method '{method}'."
    req_headers: Dict[str, str] = {"User-Agent": "WSA-Agent/1.0 (+web_fetch)"}
    if headers:
        if not isinstance(headers, dict):
            return "[Error] `headers` must be a JSON object."
        for k, v in headers.items():
            req_headers[str(k)] = str(v)
    data_bytes = str(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(raw_url, data=data_bytes, headers=req_headers, method=meth)
    opener = urllib.request.build_opener(_PublicOnlyRedirectHandler)
    try:
        with opener.open(req, timeout=max(5, min(int(timeout or 20), 120))) as resp:
            raw = resp.read(_WEB_FETCH_MAX_BYTES)
            cap = max(2_000, min(int(max_chars or 24_000), 60_000))
            ctype = str(resp.headers.get("Content-Type", "") or "")
            text = raw.decode("utf-8", errors="replace")
            head = f"HTTP {getattr(resp, 'status', resp.getcode())} {getattr(resp, 'reason', '') or ''}".rstrip()
            meta = f"{head}\ncontent-type: {ctype or 'unknown'}\nbytes: {len(raw)}"
            if "text" not in ctype.lower() and "json" not in ctype.lower() and "xml" not in ctype.lower():
                return f"{meta}\n\n[Binary or unrenderable content omitted; use http_request for local endpoints.]"
            body_text = _truncate_tool_output(text, max_chars=cap, label="web_fetch")
            return f"{meta}\n\n{body_text}"
    except urllib.error.HTTPError as he:
        try:
            raw = he.read(65_536)
        except Exception:
            raw = b""
        snippet = _truncate_tool_output(raw.decode("utf-8", errors="replace"), max_chars=4_000, label="web_fetch")
        return f"HTTP {he.code} {he.reason or ''}\n\n{snippet}".rstrip()
    except urllib.error.URLError as ue:
        reason = str(getattr(ue, "reason", ue) or ue)
        if reason.startswith("[Safety Error]"):
            return reason.replace("[Safety Error]", "[Error]", 1)
        return f"[Error] web_fetch failed: {reason}"
    except Exception as exc:
        return f"[Error] web_fetch failed ({type(exc).__name__}): {exc}"


_TODO_STATUSES = ("pending", "in_progress", "completed")


def _todo_file() -> Path:
    return _state_dotfile(".wsa_todo.json", ".gwa_todo.json", write=True)


def _todo(action: str = "read", items: Optional[List[Dict[str, Any]]] = None) -> str:
    """Persistent task checklist so long runs stay resumable and legible."""
    act = str(action or "read").strip().lower()
    path = _todo_file()
    with _filesystem_lock:
        if act == "clear":
            try:
                if path.exists():
                    path.unlink()
            except OSError as exc:
                return f"[Error] Could not clear todo list: {exc}"
            return "[OK] Todo list cleared"
        if act == "write":
            if not isinstance(items, list):
                return "[Validation Error] todo action='write' requires `items` as a list."
            cleaned: List[Dict[str, str]] = []
            for it in items:
                if not isinstance(it, dict):
                    return "[Validation Error] Each todo item must be an object with `text`."
                text = str(it.get("text", "")).strip()
                if not text:
                    return "[Validation Error] Todo item `text` must be non-empty."
                status = str(it.get("status", "pending") or "pending").strip().lower()
                if status not in _TODO_STATUSES:
                    return f"[Validation Error] Invalid status '{status}'. Use: {', '.join(_TODO_STATUSES)}"
                cleaned.append({"text": text, "status": status})
            try:
                payload = json.dumps({"items": cleaned}, ensure_ascii=False, indent=2).encode("utf-8")
                _write_bytes_atomic(path, payload)
            except OSError as exc:
                return f"[Error] Could not persist todo list: {exc}"
            done = sum(1 for c in cleaned if c["status"] == "completed")
            return f"[OK] Todo list saved ({len(cleaned)} items, {done} completed)"
        if act != "read":
            return f"[Error] Unknown todo action '{action}'. Use read, write, or clear."
        if not path.exists():
            return "[Todo list empty — use todo(action='write', items=[{text, status}])]"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return f"[Error] Could not read todo list: {exc}"
        stored = data.get("items") if isinstance(data, dict) else None
        if not isinstance(stored, list) or not stored:
            return "[Todo list empty — use todo(action='write', items=[{text, status}])]"
        marks = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
        lines = [f"{marks.get(str(s.get('status', 'pending')), '[ ]')} {s.get('text', '')}" for s in stored if isinstance(s, dict)]
        done = sum(1 for s in stored if isinstance(s, dict) and str(s.get("status")) == "completed")
        return f"Todo list ({done}/{len(stored)} completed):\n" + "\n".join(lines)


def _wait_for(kind: str, target: str, timeout: int = 30, interval: float = 0.5) -> str:
    """Block until a port/URL/file/log condition is satisfied, replacing multi-round polling."""
    k = str(kind or "").strip().lower()
    tgt = str(target or "").strip()
    if not tgt:
        return "[Validation Error] wait_for requires a non-empty target."
    budget = max(1, min(int(timeout or 30), 600))
    try:
        step = max(0.1, min(float(interval or 0.5), 10.0))
    except (TypeError, ValueError):
        step = 0.5
    deadline = time.monotonic() + budget
    last_err = ""
    while True:
        try:
            if k == "port":
                host, _, port_s = tgt.rpartition(":")
                host = host.strip() or "127.0.0.1"
                port = int(port_s)
                with socket.create_connection((host, port), timeout=2.0):
                    return f"[OK] Port {host}:{port} accepted a connection after {budget - max(0, deadline - time.monotonic()):.1f}s"
                last_err = f"port {host}:{port} refused"
            elif k == "http":
                ok, url_err = _validate_local_http_url(tgt)
                if not ok:
                    return url_err
                with urllib.request.urlopen(tgt, timeout=3) as resp:
                    return f"[OK] {tgt} responded HTTP {getattr(resp, 'status', resp.getcode())}"
            elif k == "file":
                p = _resolve(tgt)
                if p.exists():
                    return f"[OK] Path exists: {tgt}"
                last_err = f"path does not exist yet: {tgt}"
            elif k == "log":
                path_part, sep, pat = tgt.partition("::")
                if not sep or not pat.strip():
                    return "[Validation Error] log target must be 'path::regex'."
                p = _resolve(path_part.strip())
                if not p.exists():
                    last_err = f"log file does not exist yet: {path_part.strip()}"
                else:
                    rx = re.compile(pat.strip())
                    text = p.read_text(encoding="utf-8", errors="replace")
                    m = rx.search(text)
                    if m:
                        return f"[OK] Pattern matched in {path_part.strip()}: {m.group(0)[:300]}"
                    last_err = f"pattern not matched yet in {path_part.strip()}"
            else:
                return f"[Error] Unknown wait_for kind '{kind}'. Use port, http, file, or log."
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            return f"[Timeout] wait_for {k} '{tgt}' not satisfied within {budget}s (last: {last_err or 'no signal'})"
        time.sleep(step)


def _remember(key: str, value: str) -> str:
    with _filesystem_lock:
        _session_memory[key] = value
        try:
            payload = json.dumps(_session_memory, ensure_ascii=False, indent=2).encode("utf-8")
            _write_bytes_atomic(
                _state_dotfile(".wsa_memory.json", ".gwa_memory.json", write=True),
                payload,
            )
        except OSError as exc:
            return f"[Error] Could not persist note: {exc}"
        return f"[OK] Stored note: '{key}' ({len(value)} chars)"


def _recall(key: str = None) -> str:
    with _filesystem_lock:
        if key:
            val = _session_memory.get(key)
            if val is None:
                keys = list(_session_memory.keys())
                return f"[Not found: '{key}'. Available keys: {keys}]"
            return f"{key}:\n{val}"
        if not _session_memory:
            return "[Memory is empty — use remember(key, value) to store notes]"
        return "\n\n".join(f"[{k}]\n{v}" for k, v in _session_memory.items())


def clear_session_memory() -> None:
    """Clear session memory (called on /clear or project switch)."""
    with _filesystem_lock:
        _session_memory.clear()
        try:
            root = get_project_root()
            for fname in (".wsa_memory.json", ".gwa_memory.json"):
                memory_path = root / fname
                if memory_path.exists():
                    memory_path.unlink()
        except OSError as exc:
            sys.stderr.write(f"[tools] Could not clear persistent memory: {exc}\n")


_MAX_BACKUP_ENTRIES = 50
_MAX_BACKUP_BYTES = 8 * 1024 * 1024


def _record_file_pre_edit(
    backup_key: str,
    abs_path: Path,
    existed: bool,
    prev_text: Optional[str],
    raw_prev: Optional[bytes] = None,
    created_dirs: Optional[List[str]] = None,
) -> None:
    """Record pre-edit content in both LIFO `/undo` stack and active/single checkpoint."""
    global _checkpoint_seq
    with _filesystem_lock:
        prev = prev_text if existed else None
        rel = _rel_posix(abs_path)
        _file_backups[rel] = prev
        _backup_order.append((rel, prev))
        while len(_backup_order) > _MAX_BACKUP_ENTRIES:
            evicted_rel, _ = _backup_order.pop(0)
            if not any(isinstance(item, tuple) and item[0] == evicted_rel for item in _backup_order):
                _file_backups.pop(evicted_rel, None)
        total_bytes = sum(
            len(content.encode("utf-8", errors="replace"))
            for _, content in _backup_order
            if isinstance(content, str)
        )
        while len(_backup_order) > 1 and total_bytes > _MAX_BACKUP_BYTES:
            evicted_rel, evicted_prev = _backup_order.pop(0)
            if isinstance(evicted_prev, str):
                total_bytes -= len(evicted_prev.encode("utf-8", errors="replace"))
            if not any(isinstance(item, tuple) and item[0] == evicted_rel for item in _backup_order):
                _file_backups.pop(evicted_rel, None)

        active = _active_checkpoint.get()
        if active is not None:
            active["files"].setdefault(rel, prev)
            active["raw"].setdefault(rel, raw_prev if existed else None)
            for directory in created_dirs or []:
                if directory not in active.setdefault("created_dirs", []):
                    active["created_dirs"].append(directory)
        else:
            _checkpoint_seq += 1
            _checkpoints.append({
                "id": f"cp-{_checkpoint_seq}",
                "label": f"edit {rel}",
                "created_at": time.time(),
                "files": {rel: prev},
                "raw": {rel: raw_prev if existed else None},
                "created_dirs": list(created_dirs or []),
            })
            if len(_checkpoints) > 50:
                del _checkpoints[: len(_checkpoints) - 50]


def begin_edit_checkpoint(label: str = "") -> str:
    """Start capturing all subsequent file edits into one atomic multi-file checkpoint."""
    if _active_checkpoint.get() is None:
        _active_checkpoint.set({
            "label": label or "edit batch",
            "created_at": time.time(),
            "files": {},
            "raw": {},
        })
    return str(_active_checkpoint.get()["label"])


def commit_edit_checkpoint() -> Optional[str]:
    """Finalize the active batch checkpoint if at least one file was modified."""
    with _filesystem_lock:
        return _commit_edit_checkpoint_unlocked()


def _commit_edit_checkpoint_unlocked() -> Optional[str]:
    global _checkpoint_seq
    cp = _active_checkpoint.get()
    _active_checkpoint.set(None)
    if not cp or not cp.get("files"):
        return None
    _checkpoint_seq += 1
    cp_id = f"cp-{_checkpoint_seq}"
    cp["id"] = cp_id
    _checkpoints.append(cp)
    if len(_checkpoints) > 50:
        del _checkpoints[: len(_checkpoints) - 50]
    return cp_id


def get_checkpoints() -> List[Dict[str, Any]]:
    """Return metadata copies for recorded edit checkpoints."""
    with _filesystem_lock:
        return [
            {
                "id": cp["id"],
                "label": cp["label"],
                "created_at": cp["created_at"],
                "files": list(cp["files"].keys()),
                "directories": list(cp.get("directories", [])),
            }
            for cp in _checkpoints
        ]


def _select_checkpoints_for_rollback(
    steps: int = 1,
    checkpoint_id: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
    if not _checkpoints:
        return [], [], "[Info] No edit checkpoints recorded yet in this session."
    if checkpoint_id:
        needle = str(checkpoint_id).strip()
        target_idx = next((i for i, cp in enumerate(_checkpoints) if cp["id"] == needle), -1)
        if target_idx < 0:
            avail = ", ".join(cp["id"] for cp in _checkpoints[-6:])
            return [], list(_checkpoints), f"[Error] Checkpoint '{needle}' not found. Available: {avail}"
        return list(_checkpoints[target_idx:]), list(_checkpoints[:target_idx]), None
    n = max(1, min(int(steps or 1), len(_checkpoints)))
    return list(_checkpoints[-n:]), list(_checkpoints[:-n]), None


def _preview_rollback_paths(steps: int = 1, checkpoint_id: Optional[str] = None) -> List[str]:
    with _filesystem_lock:
        to_revert, _, err = _select_checkpoints_for_rollback(steps=steps, checkpoint_id=checkpoint_id)
        if err:
            return []
        seen: List[str] = []
        for cp in to_revert:
            for path in list(cp["files"]) + list(cp.get("directories", [])):
                if path not in seen:
                    seen.append(path)
        return seen


def _is_directory_link(p: Path) -> bool:
    """Detect symlinks/junctions, but allow OneDrive cloud-file reparse points."""
    st = p.lstat()
    return p.is_symlink() or bool(getattr(st, "st_reparse_tag", 0) & 0x20000000)


def _resolve_rollback_path(path: str) -> Path:
    """Recheck saved paths against link replacement before destructive recovery."""
    resolved = _resolve(path)
    literal = Path(os.path.abspath(get_project_root() / path))
    if os.path.normcase(str(literal)) != os.path.normcase(str(resolved)):
        raise PermissionError(f"Rollback path was redirected through a link: {literal} -> {resolved}")
    return resolved


def _rollback_tree(p: Path):
    """Enumerate a literal in-workspace tree without following links or junctions."""
    _resolve_rollback_path(str(p))
    if _is_directory_link(p):
        raise PermissionError(f"Rollback does not follow symlinks/junctions: {p}")
    yield p
    if p.is_dir():
        for child in sorted(p.iterdir()):
            yield from _rollback_tree(child)


def _remove_rollback_path(p: Path, failures: Dict[str, str]) -> None:
    """Delete files first and directories bottom-up, reporting every failed path."""
    try:
        _resolve_rollback_path(str(p))
        if not os.path.lexists(p):
            return
        if _is_directory_link(p):
            raise PermissionError(f"Refusing to remove a symlink/junction during rollback: {p}")
        if p.is_dir():
            for child in sorted(p.iterdir()):
                _remove_rollback_path(child, failures)
            _retry_filesystem(p.rmdir)
        else:
            _retry_filesystem(p.unlink)
        if os.path.lexists(p):
            raise OSError(f"Path still exists after removal: {p}")
        _active_file_ledger.get().mark_deleted(_rel_posix(p))
    except OSError as exc:
        failures[str(p)] = str(exc)


def _restore_checkpoint(cp: Dict[str, Any]) -> Tuple[int, Dict[str, str]]:
    failures: Dict[str, str] = {}
    restored = 0
    root_dir = get_project_root()
    files = cp["files"]
    directories = set(cp.get("directories", []))
    # Restore saved directories and files before pruning additions to a saved tree.
    for rel in sorted(directories, key=lambda value: len(Path(value).parts)):
        try:
            p = _resolve_rollback_path(rel)
            if p.exists() and not p.is_dir():
                _remove_rollback_path(p, failures)
            _retry_filesystem(lambda: p.mkdir(parents=True, exist_ok=True))
        except OSError as exc:
            failures[str(root_dir / rel)] = str(exc)
    for rel, prev in files.items():
        if prev is None:
            continue
        try:
            p = _resolve_rollback_path(rel)
            if p.is_dir():
                _remove_rollback_path(p, failures)
            _retry_filesystem(lambda: p.parent.mkdir(parents=True, exist_ok=True))
            raw = cp.get("raw", {}).get(rel)
            if raw is None:
                raw = prev.encode("utf-8")
            _write_bytes_atomic(p, raw)
            if p.read_bytes() != raw:
                raise OSError(f"Restored content verification failed: {p}")
            _mark_file_read_rel(p)
            _active_file_ledger.get().record_write(rel, prev, summary="rolled back", abs_path=p)
            restored += 1
        except OSError as exc:
            failures[str(root_dir / rel)] = str(exc)
    for rel, prev in files.items():
        if prev is None:
            try:
                _remove_rollback_path(_resolve_rollback_path(rel), failures)
            except OSError as exc:
                failures[str(root_dir / rel)] = str(exc)
    wanted = set(files) | directories
    for rel in cp.get("tree_roots", []):
        try:
            root = _resolve_rollback_path(rel)
            # Walk bottom-up; do not use workspace search ignores for recovery.
            for p in reversed(list(_rollback_tree(root))):
                if _rel_posix(p) not in wanted:
                    _remove_rollback_path(p, failures)
        except OSError as exc:
            failures[str(root_dir / rel)] = str(exc)
    for rel in sorted(cp.get("created_dirs", []), key=lambda value: len(Path(value).parts), reverse=True):
        try:
            p = _resolve_rollback_path(rel)
            if p.exists():
                # Implicit parent directories may now contain unrelated files;
                # only remove them when empty, never recursively.
                _retry_filesystem(p.rmdir)
        except OSError as exc:
            failures[str(root_dir / rel)] = str(exc)
    return restored, failures


def _rollback(
    action: str = "rollback",
    steps: int = 1,
    checkpoint_id: Optional[str] = None,
    paths: Optional[List[str]] = None,
    label: str = "",
) -> str:
    with _filesystem_lock:
        return _rollback_unlocked(
            action=action,
            steps=steps,
            checkpoint_id=checkpoint_id,
            paths=paths,
            label=label,
        )


def _rollback_unlocked(
    action: str = "rollback",
    steps: int = 1,
    checkpoint_id: Optional[str] = None,
    paths: Optional[List[str]] = None,
    label: str = "",
) -> str:
    global _checkpoint_seq
    act = str(action or "rollback").strip().lower()
    if act == "list":
        if not _checkpoints:
            return "[No checkpoints recorded in this session]"
        lines = [f"Edit Checkpoints ({len(_checkpoints)}, newest first):"]
        for cp in reversed(_checkpoints):
            cp_paths = list(cp["files"]) + list(cp.get("directories", []))
            f_list = ", ".join(cp_paths)
            lines.append(f"  {cp['id']} - {cp['label']} ({len(cp_paths)} path(s): {f_list})")
        return "\n".join(lines)

    if act == "create":
        if not paths or not isinstance(paths, list):
            return "[Error] rollback(action='create') requires paths=[...] of workspace files to snapshot."
        files_map: Dict[str, Optional[str]] = {}
        raw_map: Dict[str, Optional[bytes]] = {}
        directories: Set[str] = set()
        tree_roots: List[str] = []
        for raw_p in paths:
            p = _resolve_rollback_path(str(raw_p))
            rel = _rel_posix(p)
            if not p.exists():
                files_map[rel] = None
                raw_map[rel] = None
                continue
            if p.is_dir():
                tree_roots.append(rel)
            for target in _rollback_tree(p):
                target_rel = _rel_posix(target)
                if target.is_dir():
                    directories.add(target_rel)
                elif target.is_file():
                    raw_bytes = target.read_bytes()
                    files_map[target_rel] = raw_bytes.decode("utf-8", errors="replace")
                    raw_map[target_rel] = raw_bytes
                else:
                    raise ValueError(f"Cannot snapshot special file: {target}")
        _checkpoint_seq += 1
        cp_id = f"cp-{_checkpoint_seq}"
        _checkpoints.append({
            "id": cp_id,
            "label": label or f"manual snapshot ({len(files_map)} file(s))",
            "created_at": time.time(),
            "files": files_map,
            "raw": raw_map,
            "directories": sorted(directories),
            "tree_roots": tree_roots,
        })
        if len(_checkpoints) > 50:
            del _checkpoints[: len(_checkpoints) - 50]
        saved_paths = list(files_map) + sorted(directories)
        return f"[OK] Created checkpoint {cp_id} ({len(saved_paths)} path(s): {', '.join(saved_paths)})"

    if act != "rollback":
        return f"[Error] Unknown rollback action '{action}'. Use 'rollback', 'list', or 'create'."

    commit_edit_checkpoint()
    to_revert, _, err = _select_checkpoints_for_rollback(steps=steps, checkpoint_id=checkpoint_id)
    if err:
        return err

    restored_count = 0
    for cp in reversed(to_revert):
        count, failures = _restore_checkpoint(cp)
        restored_count += count
        if failures:
            return (
                f"[Error] Partial rollback at {cp['id']}; {restored_count} file restore(s) verified. "
                "Failed and older checkpoints retained for retry. Affected paths:\n"
                + "\n".join(f"  {path}: {error}" for path, error in failures.items())
            )
        _checkpoints.remove(cp)
    cp_ids = ", ".join(cp["id"] for cp in to_revert)
    return (
        f"[OK] Rolled back {len(to_revert)} checkpoint(s) ({cp_ids}) — "
        f"{restored_count} file restore(s) verified; created paths removed."
    )


def get_file_backups() -> Dict[str, Optional[str]]:
    """Return backup dict (for /undo)."""
    return _file_backups


def get_backup_order() -> List[Tuple[str, Optional[str]]]:
    """Return backup insertion order (for /undo stack)."""
    return _backup_order


def undo_last_edit() -> Tuple[bool, str, int]:
    """Atomically revert the last recorded file edit under `_filesystem_lock`."""
    with _filesystem_lock:
        if not _backup_order:
            return False, "Nothing to undo — no files were written this session.", 0

        entry = _backup_order.pop()
        if isinstance(entry, tuple) and len(entry) == 2:
            last_path, prev_content = entry
        else:
            last_path = str(entry)
            prev_content = _file_backups.pop(last_path, None)

        try:
            p = _resolve(last_path)
            rel = _rel_posix(p)
            if prev_content is None:
                if p.exists():
                    p.unlink()
                    msg = f"Deleted newly created file: {last_path}"
                else:
                    msg = f"File already deleted: {last_path}"
                _active_file_ledger.get().mark_deleted(rel)
            else:
                raw_existing = p.read_bytes() if p.exists() and p.is_file() else None
                p.parent.mkdir(parents=True, exist_ok=True)
                _write_text_preserving_eol(p, prev_content, raw_existing)
                _mark_file_read_rel(p)
                _active_file_ledger.get().record_write(
                    rel, prev_content, summary="undid last edit", abs_path=p
                )
                lines = prev_content.count("\n") + 1
                msg = f"Restored: {last_path} ({lines} lines)"

            found_earlier = False
            prev_earlier: Optional[str] = None
            for item in reversed(_backup_order):
                if isinstance(item, tuple) and len(item) == 2 and item[0] in (rel, last_path):
                    prev_earlier = item[1]
                    found_earlier = True
                    break
            if not found_earlier:
                _file_backups.pop(rel, None)
                _file_backups.pop(last_path, None)
            else:
                _file_backups[rel] = prev_earlier
            return True, msg, len(_backup_order)
        except Exception:
            _backup_order.append(entry)
            raise


def _bg_dir() -> Path:
    global _bg_log_session_dir
    if _bg_log_session_dir is None:
        _bg_log_session_dir = Path(tempfile.gettempdir()) / f"wsa_bg_{os.getpid()}"
    _bg_log_session_dir.mkdir(parents=True, exist_ok=True)
    return _bg_log_session_dir


class _WindowsBackgroundJob:
    """Track only processes launched by one managed Windows background job."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.kernel
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateEventW.restype = wintypes.HANDLE
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.SetEvent.argtypes = [wintypes.HANDLE]
        k.SetEvent.restype = wintypes.BOOL
        k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        k.QueryInformationJobObject.restype = wintypes.BOOL
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateJobObject.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        self.name = "Local\\wsa-bg-" + secrets.token_hex(16)
        self.job = k.CreateJobObjectW(None, None)
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        self.event = k.CreateEventW(None, True, False, self.name)
        if not self.event:
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach_and_release(self, proc) -> None:
        if not self.kernel.AssignProcessToJobObject(self.job, int(proc._handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        if not self.kernel.SetEvent(self.event):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def active_count(self) -> int:
        from ctypes import wintypes
        c = self.ctypes

        class Accounting(c.Structure):
            _fields_ = [("user", c.c_longlong), ("kernel", c.c_longlong),
                        ("period_user", c.c_longlong), ("period_kernel", c.c_longlong),
                        ("faults", wintypes.DWORD), ("total", wintypes.DWORD),
                        ("active", wintypes.DWORD), ("terminated", wintypes.DWORD)]

        info = Accounting()
        if not self.kernel.QueryInformationJobObject(self.job, 1, c.byref(info), c.sizeof(info), None):
            raise c.WinError(c.get_last_error())
        return info.active

    def terminate(self) -> None:
        if not self.kernel.TerminateJobObject(self.job, 1):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def close(self) -> None:
        for attr in ("event", "job"):
            handle = getattr(self, attr, None)
            if handle:
                self.kernel.CloseHandle(handle)
                setattr(self, attr, None)


def _refresh_bg_entry_locked(entry: Dict[str, Any]) -> Optional[int]:
    proc = entry["proc"]
    rc = proc.poll()
    job = entry.get("job")
    if rc is not None and job:
        if job.active_count():
            return None
        job.close()
        entry.pop("job", None)
    if rc is not None:
        if entry.get("exit_code") is None:
            entry["exit_code"] = rc
            entry["ended_at"] = time.time()
            entry["termination_reason"] = (
                "wsa_requested" if entry.get("stop_requested_at") is not None
                else "completed" if rc == 0 else "process_failure"
            )
        for fh_key in ("stdout_fh", "stderr_fh"):
            fh = entry.pop(fh_key, None)
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
    return rc


def _tail_log_file(path: Path, max_lines: int = 15, max_chars: int = 2400) -> str:
    if not path or not path.is_file():
        return ""
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 64_000))
            raw = stream.read(64_000).decode("utf-8", errors="replace")
    except OSError:
        return ""
    clean = _ANSI_ESCAPE_RE.sub("", raw).strip()
    if not clean:
        return ""
    lines = clean.splitlines()
    if max_lines > 0 and len(lines) > max_lines:
        lines = lines[-max_lines:]
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = "...\n" + out[-max_chars:].lstrip()
    return out


_MAX_BG_HISTORY = 40


def _start_background_proc(
    command: str,
    name: Optional[str] = None,
    wait_ms: int = 500,
    ready_port: Optional[int] = None,
) -> Tuple[bool, Dict[str, Any], Optional[bool]]:
    """Spawn a tracked background PowerShell process and wait up to `wait_ms` for early crash or port listen."""
    global _bg_seq
    cwd_root = get_project_root()
    with _bg_lock:
        _bg_seq += 1
        seq = _bg_seq
        raw_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(name or "").strip()).strip("-")
        proc_name = raw_name or f"bg-{seq}"
        existing = _bg_processes.get(proc_name)
        if existing is not None:
            if _refresh_bg_entry_locked(existing) is None:
                return False, existing, None
        # Check, spawn, and register under one lock so concurrent starts cannot
        # overwrite an entry and orphan the first worker with the same name.
        entry = _spawn_background_entry(command, proc_name, seq, cwd=cwd_root)
        _bg_processes[proc_name] = entry
        if len(_bg_processes) > _MAX_BG_HISTORY:
            finished_keys = [
                k for k, v in _bg_processes.items()
                if k != proc_name and _refresh_bg_entry_locked(v) is not None
            ]
            excess = len(_bg_processes) - _MAX_BG_HISTORY
            for k in finished_keys[:max(0, excess)]:
                _bg_processes.pop(k, None)

    wait_sec = min(max(int(wait_ms or 0), 0), 10_000) / 1000.0
    deadline = time.monotonic() + wait_sec
    port_ready: Optional[bool] = False if ready_port else None
    while time.monotonic() < deadline:
        with _bg_lock:
            rc = _refresh_bg_entry_locked(entry)
        if rc is not None:
            break
        if ready_port and _is_tcp_port_open("127.0.0.1", ready_port, timeout=0.15):
            port_ready = True
            break
        time.sleep(0.05)

    with _bg_lock:
        _refresh_bg_entry_locked(entry)
    if ready_port and not port_ready:
        port_ready = _is_tcp_port_open("127.0.0.1", ready_port, timeout=0.15)
    return True, entry, port_ready


def _spawn_background_entry(
    command: str,
    proc_name: str,
    seq: int,
    cwd: Optional[Path] = None,
) -> Dict[str, Any]:
    original_command = command
    log_dir = _bg_dir()
    stamp = f"{proc_name}_{seq}"
    stdout_path = log_dir / f"{stamp}.stdout.log"
    stderr_path = log_dir / f"{stamp}.stderr.log"
    stdout_fh = open(stdout_path, "w", encoding="utf-8", errors="replace")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    stderr_fh = None
    job = None
    proc = None
    try:
        stderr_fh = open(stderr_path, "w", encoding="utf-8", errors="replace")
        if sys.platform == "win32":
            job = _WindowsBackgroundJob()
            # The shell waits until it belongs to the job before it can launch
            # descendants, eliminating the assignment/spawn race.
            command = ("$wsaGate = [Threading.EventWaitHandle]::OpenExisting('" + job.name + "'); "
                       "try { $wsaReady = $wsaGate.WaitOne(10000) } finally { $wsaGate.Dispose() }; "
                       "if (-not $wsaReady) { exit 125 };\n" + command)
        if sys.platform == "win32":
            wrapped = _wrap_powershell_utf8(command)
            cmd_list = ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", _encode_powershell_command(wrapped)]
        else:
            cmd_list = ["/bin/sh", "-c", command]
        startup = None
        if sys.platform == "win32":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
        proc = subprocess.Popen(
            cmd_list,
            cwd=str(cwd or get_project_root()),
            stdin=subprocess.DEVNULL,
            stdout=stdout_fh,
            stderr=stderr_fh,
            env=_subprocess_utf8_env(),
            creationflags=flags,
            startupinfo=startup,
            **({} if sys.platform == "win32" else {"start_new_session": True}),
        )
        if job:
            if isinstance(proc, _REAL_POPEN_CLS):
                job.attach_and_release(proc)
            else:  # Unit-test process doubles have no Windows handle.
                job.close()
                job = None
    except Exception:
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass
        if job:
            job.close()
        stdout_fh.close()
        if stderr_fh is not None:
            stderr_fh.close()
        raise

    entry: Dict[str, Any] = {
        "name": proc_name,
        "pid": proc.pid,
        "command": original_command,
        "proc": proc,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "stdout_fh": stdout_fh,
        "stderr_fh": stderr_fh,
        "started_at": time.time(),
        "exit_code": None,
        "ended_at": None,
        "job": job,
    }
    return entry


def _format_bg_report(entry: Dict[str, Any], tail_lines: int = 15) -> List[str]:
    rc = entry.get("exit_code")
    status = "running" if rc is None else f"exited (exit code {rc})"
    if rc is not None and entry.get("termination_reason") == "wsa_requested":
        status = f"stopped by WSA (exit code {rc})"
    elapsed = round(max(0.0, (entry.get("ended_at") or time.time()) - entry["started_at"]), 2)
    lines = [
        f"Name: {entry['name']}",
        f"PID: {entry['pid']}",
        f"Status: {status} ({elapsed}s)",
        f"STDOUT log: {entry['stdout_path']}",
        f"STDERR log: {entry['stderr_path']}",
    ]
    if rc is not None:
        shell = "PowerShell" if sys.platform == "win32" else "shell"
        lines.append(f"Exit code source: tracked {shell} process (child process exit codes may differ)")
    if entry.get("stop_method"):
        lines.append(f"Shutdown: {entry['stop_method']}")
    if entry.get("termination_reason"):
        lines.append(f"Termination reason: {entry['termination_reason']}")
    for key in ("graceful_stop_error", "stop_error"):
        if entry.get(key):
            lines.append(f"{key}: {entry[key]}")
    out_tail = _tail_log_file(entry["stdout_path"], max_lines=tail_lines)
    err_tail = _tail_log_file(entry["stderr_path"], max_lines=tail_lines)
    if out_tail:
        lines.append(f"STDOUT tail:\n{out_tail}")
    if err_tail:
        lines.append(f"STDERR tail:\n{err_tail}")
    return lines


def _start_background(command: str, name: Optional[str] = None, wait_ms: int = 500) -> str:
    guard_err = _check_self_preservation_guard(command)
    if guard_err:
        return guard_err
    rec_err = _check_recursive_agent_guard(command)
    if rec_err:
        return rec_err
    started, entry, _ = _start_background_proc(command, name=name, wait_ms=wait_ms)
    if not started:
        return (
            f"[Error] Background process '{entry['name']}' is already running (PID {entry['pid']}). "
            f"Stop it first via stop_background('{entry['name']}') or pass another name."
        )
    rc = entry.get("exit_code")
    report = "\n".join(_format_bg_report(entry, tail_lines=20))
    if rc is not None and rc != 0:
        return f"[Error] Background process '{entry['name']}' exited immediately (exit code {rc}):\n{report}"
    return f"[OK] Started background process '{entry['name']}' (PID {entry['pid']}):\n{report}"


def _list_background(name: Optional[str] = None, tail_lines: int = 15) -> str:
    with _bg_lock:
        if not _bg_processes:
            return "[No background processes started in this WSA session]"
        targets = []
        for k, entry in _bg_processes.items():
            if name and k != name and str(entry["pid"]) != str(name).strip():
                continue
            _refresh_bg_entry_locked(entry)
            targets.append(dict(entry))
    if not targets:
        return f"[No tracked WSA background process matching '{name}']"
    blocks = []
    t_lines = max(0, min(int(tail_lines if tail_lines is not None else 15), 200))
    for entry in targets:
        cmd_preview = re.sub(r"\s+", " ", entry["command"]).strip()[:100]
        header = f"Command: {cmd_preview}"
        blocks.append("\n".join(_format_bg_report(entry, tail_lines=t_lines) + [header]))
    return "\n\n---\n\n".join(blocks)


def _stop_bg_entry_locked(entry: Dict[str, Any]) -> None:
    rc = _refresh_bg_entry_locked(entry)
    if rc is None:
        proc = entry["proc"]
        job = entry.get("job")
        entry["stop_requested_at"] = time.time()
        entry["stop_method"] = "graceful"
        try:
            if sys.platform == "win32":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=2.0)
        except (OSError, subprocess.SubprocessError) as exc:
            entry["graceful_stop_error"] = str(exc)
            entry["stop_method"] = "forced"
            if job:
                try:
                    job.terminate()
                except OSError as force_exc:
                    entry["stop_error"] = str(force_exc)
            else:
                _kill_process_tree(entry["pid"])
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=2.0)
            except (OSError, subprocess.SubprocessError) as force_exc:
                entry["stop_error"] = str(force_exc)
        try:
            if job:
                deadline = time.monotonic() + 2.0
                while job.active_count() and time.monotonic() < deadline:
                    time.sleep(0.05)
                if job.active_count():
                    entry["stop_method"] = "forced"
                    job.terminate()
                    deadline = time.monotonic() + 2.0
                    while job.active_count() and time.monotonic() < deadline:
                        time.sleep(0.05)
            _refresh_bg_entry_locked(entry)
        except OSError as exc:
            entry["stop_error"] = str(exc)
            entry["stop_method"] = "failed"
        if entry.get("exit_code") is None:
            entry["stop_method"] = "failed"
            entry.setdefault("stop_error", "Process is still running after termination attempts")


def _stop_background(name: str) -> str:
    key = str(name or "").strip()
    if not key:
        return "[Error] stop_background requires a process `name` or tracked PID."
    with _bg_lock:
        entry = _bg_processes.get(key)
        if entry is None:
            entry = next((e for e in _bg_processes.values() if str(e["pid"]) == key), None)
        if entry is None:
            return (
                f"[Error] '{key}' was NOT started by WSA's start_background in this session; "
                "refusing to touch external OS processes."
            )
        was_running = _refresh_bg_entry_locked(entry) is None
        if was_running:
            _stop_bg_entry_locked(entry)
        snapshot = dict(entry)
    report = "\n".join(_format_bg_report(snapshot, tail_lines=20))
    if snapshot.get("exit_code") is None or snapshot.get("stop_method") == "failed":
        return f"[Error] Failed to stop WSA background process '{snapshot['name']}':\n{report}"
    if was_running:
        return f"[OK] Stopped WSA background process '{snapshot['name']}' (PID {snapshot['pid']}):\n{report}"
    return f"[Info] Background process '{snapshot['name']}' (PID {snapshot['pid']}) had already exited:\n{report}"


def cleanup_background_processes() -> None:
    """Terminate any running WSA-managed background processes cleanly on exit/reset."""
    with _bg_lock:
        entries = list(_bg_processes.values())
        for entry in entries:
            try:
                _stop_bg_entry_locked(entry)
            except Exception:
                pass
        for name, entry in list(_bg_processes.items()):
            if entry.get("exit_code") is not None:
                del _bg_processes[name]


atexit.register(cleanup_background_processes)


def _restart_service(
    port: int,
    command: Optional[str] = None,
    name: Optional[str] = None,
    wait_ms: int = 2000,
) -> str:
    try:
        port_num = int(port)
    except (TypeError, ValueError):
        return f"[Error] Invalid TCP port: {port!r}"
    if port_num < 1 or port_num > 65535:
        return f"[Error] Port must be between 1 and 65535 (got {port_num})."

    bridge_port = _get_bridge_port()
    if port_num in {bridge_port, 8081}:
        return (
            f"[Self-Preservation Guard] Refused restart_service on protected WSA bridge port {port_num}."
        )

    if command:
        guard_err = _check_self_preservation_guard(command)
        if guard_err:
            return guard_err
        rec_err = _check_recursive_agent_guard(command)
        if rec_err:
            return rec_err

    listeners = _get_listening_pids_on_port(port_num)
    self_pid = os.getpid()
    for pid in listeners:
        if pid == self_pid:
            return (
                f"[Self-Preservation Guard] Refused restart_service on port {port_num}: "
                f"listener PID {pid} is the active WSA process."
            )

    stopped_pids: List[int] = []
    for pid in listeners:
        # Stop via tracked bg entry if WSA spawned it so handles/status update cleanly
        matched_bg = None
        with _bg_lock:
            matched_bg = next((e["name"] for e in _bg_processes.values() if e["pid"] == pid), None)
        if matched_bg:
            stop_result = _stop_background(matched_bg)
            if stop_result.startswith("[Error]"):
                return stop_result
        else:
            _kill_process_tree(pid)
        stopped_pids.append(pid)

    if stopped_pids:
        free_deadline = time.monotonic() + 2.5
        while time.monotonic() < free_deadline:
            if not _get_listening_pids_on_port(port_num) and not _is_tcp_port_open("127.0.0.1", port_num, timeout=0.1):
                break
            time.sleep(0.08)
        if _get_listening_pids_on_port(port_num) or _is_tcp_port_open("127.0.0.1", port_num, timeout=0.1):
            return f"[Error] Port {port_num} is still occupied after termination attempts; replacement was not started."
    elif _is_tcp_port_open("127.0.0.1", port_num, timeout=0.1):
        return f"[Error] Port {port_num} is occupied but its listener could not be identified; replacement was not started."

    if not command or not str(command).strip():
        if not stopped_pids:
            return f"[Info] No process was listening on port {port_num}."
        return f"[OK] Stopped listener(s) scoped strictly to port {port_num}: PID(s) {', '.join(map(str, stopped_pids))}."

    svc_name = str(name or f"service-{port_num}").strip()
    with _bg_lock:
        prev_entry = _bg_processes.get(svc_name)
        if prev_entry is not None and _refresh_bg_entry_locked(prev_entry) is None:
            _stop_bg_entry_locked(prev_entry)
            if prev_entry.get("exit_code") is None or prev_entry.get("stop_method") == "failed":
                return f"[Error] Could not stop existing service '{svc_name}'; replacement was not started."

    started, entry, port_ready = _start_background_proc(
        command,
        name=svc_name,
        wait_ms=min(max(int(wait_ms or 2000), 200), 10_000),
        ready_port=port_num,
    )
    if not started:
        return f"[Error] Could not start replacement service '{svc_name}' on port {port_num}."

    rc = entry.get("exit_code")
    report = "\n".join(_format_bg_report(entry, tail_lines=20))
    stopped_msg = f"Stopped old port {port_num} PID(s): {', '.join(map(str, stopped_pids))}" if stopped_pids else f"Port {port_num} had no prior listener"
    if rc is not None and rc != 0:
        return (
            f"[Error] {stopped_msg}, but replacement service '{entry['name']}' exited immediately (code {rc}):\n{report}"
        )
    ready_line = f"Port {port_num} listening: {'yes' if port_ready else f'not yet within {wait_ms}ms (still starting)'}"
    return f"[OK] {stopped_msg}; started '{entry['name']}' (PID {entry['pid']})\n{ready_line}\n{report}"


def _split_cli_args(raw_args: str) -> List[str]:
    """Split CLI arguments preserving Windows backslashes inside unquoted tokens."""
    if not raw_args or not str(raw_args).strip():
        return []
    tokens = shlex.split(str(raw_args), posix=False)
    cleaned: List[str] = []
    for tok in tokens:
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'"):
            cleaned.append(tok[1:-1])
        else:
            cleaned.append(tok)
    return cleaned


def _parse_test_output(framework: str, returncode: int, stdout: str, stderr: str) -> str:
    """Parse pytest/unittest/vitest/jest outputs into compact pass/fail counts + failing test names."""
    clean_out = _ANSI_ESCAPE_RE.sub("", stdout or "")
    clean_err = _ANSI_ESCAPE_RE.sub("", stderr or "")
    combined = f"{clean_out}\n{clean_err}".strip()

    counts: Dict[str, int] = {}
    duration = ""
    failures: List[str] = []

    if framework in ("pytest", "auto"):
        for key in ("passed", "failed", "error", "errors", "skipped", "xfailed", "xpassed", "warnings"):
            m = re.findall(rf"(\d+)\s+{key}\b", combined)
            if m:
                norm_k = "errors" if key == "error" else key
                counts[norm_k] = counts.get(norm_k, 0) + int(m[-1])
        dur_m = re.findall(r"\bin\s+([0-9.]+s(?:\s*\([^)]+\))?)", combined)
        if dur_m:
            duration = dur_m[-1]
        for line in combined.splitlines():
            s = line.strip()
            if s.startswith(("FAILED ", "ERROR ")) and len(s) > 7:
                if s not in failures:
                    failures.append(s)

    if framework == "unittest" or (framework == "auto" and not counts and re.search(r"^Ran \d+ tests? in ", combined, re.M)):
        framework = "unittest"
        ran_m = re.search(r"^Ran (\d+) tests? in ([0-9.]+s)", combined, re.M)
        if ran_m:
            ran = int(ran_m.group(1))
            duration = ran_m.group(2)
            fail_m = re.search(r"failures=(\d+)", combined)
            err_m = re.search(r"errors=(\d+)", combined)
            skip_m = re.search(r"skipped=(\d+)", combined)
            f_cnt = int(fail_m.group(1)) if fail_m else 0
            e_cnt = int(err_m.group(1)) if err_m else 0
            s_cnt = int(skip_m.group(1)) if skip_m else 0
            counts = {
                "passed": max(0, ran - f_cnt - e_cnt - s_cnt),
                "failed": f_cnt,
                "errors": e_cnt,
                "skipped": s_cnt,
            }
        for line in combined.splitlines():
            s = line.strip()
            if s.startswith(("FAIL: ", "ERROR: ")):
                if s not in failures:
                    failures.append(s)

    if framework in ("vitest", "jest") and not counts:
        pass_m = re.findall(r"(\d+)\s+passed\b", combined)
        fail_m = re.findall(r"(\d+)\s+failed\b", combined)
        skip_m = re.findall(r"(\d+)\s+skipped\b", combined)
        if pass_m or fail_m or skip_m:
            counts = {
                "passed": int(pass_m[-1]) if pass_m else 0,
                "failed": int(fail_m[-1]) if fail_m else 0,
                "skipped": int(skip_m[-1]) if skip_m else 0,
            }
        for line in combined.splitlines():
            s = line.strip()
            if s.startswith(("FAIL ", "× ", "✕ ")):
                if s not in failures:
                    failures.append(s)

    if returncode == 5 and framework == "pytest":
        status = "NO TESTS"
    elif returncode == 0:
        status = "PASS"
    else:
        status = "FAIL"

    ordered_keys = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed", "warnings")
    summary_parts = [f"{counts[k]} {k}" for k in ordered_keys if counts.get(k, 0) > 0 or (k == "passed" and k in counts)]
    summary_str = ", ".join(summary_parts) if summary_parts else "(unparsed runner output)"
    if duration:
        summary_str += f" ({duration})"

    out_lines = [
        f"Status: {status}",
        f"Framework: {framework}",
        f"Summary: {summary_str}",
        f"Exit code: {returncode}",
    ]
    if failures:
        out_lines.append(f"Failing tests ({len(failures)}):")
        for item in failures[:40]:
            out_lines.append(f"  - {item}")
        if len(failures) > 40:
            out_lines.append(f"  ... (+{len(failures) - 40} more)")

    if returncode != 0 or not counts:
        detail_match = re.search(r"(=+\s*(?:FAILURES|ERRORS)\s*=+.*)", combined, re.DOTALL)
        detail = detail_match.group(1) if detail_match else combined
        if detail:
            out_lines.append(
                "Details:\n" + _truncate_tool_output(detail, max_chars=10_000, label="test failures")
            )
    return "\n".join(out_lines)


def _run_tests(
    path: str = "",
    k: str = "",
    args: str = "",
    framework: str = "auto",
    timeout: int = 180,
) -> str:
    """Run project test suite cleanly without shell wrapper and return structured pass/fail counts."""
    timeout = max(5, min(int(timeout or 180), 600))
    fw = str(framework or "auto").strip().lower()
    if fw not in ("auto", "pytest", "unittest", "vitest", "jest"):
        return f"[Error] Unsupported test framework '{framework}'. Use auto, pytest, unittest, vitest, or jest."

    target_arg = str(path or "").strip()
    if target_arg:
        file_part = target_arg.split("::", 1)[0]
        p = _resolve(file_part)
        if not p.exists():
            return f"[Error] Test path not found: {file_part}"

    root_dir = get_project_root()
    extra_args = _split_cli_args(args)
    py_exe = _detect_python_executable(root_dir)

    if fw == "auto":
        low_path = target_arg.lower()
        if low_path.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
            pkg = root_dir / "package.json"
            fw = "vitest" if (pkg.is_file() and "vitest" in pkg.read_text(encoding="utf-8", errors="ignore").lower()) else "jest"
        else:
            fw = "pytest"

    def _build_cmd(runner: str) -> List[str]:
        if runner == "pytest":
            cmd = [py_exe, "-m", "pytest"]
            if not any(a in ("-q", "-v", "-vv", "--verbose", "--quiet") for a in extra_args):
                cmd.append("-q")
            if not any(a.startswith("--tb") for a in extra_args):
                cmd.append("--tb=short")
            if not any(a == "-r" or a.startswith("-r") for a in extra_args):
                cmd.extend(["-r", "fEX"])
            if target_arg:
                cmd.append(target_arg)
            if k:
                cmd.extend(["-k", str(k)])
            cmd.extend(extra_args)
            return cmd
        if runner == "unittest":
            cmd = [py_exe, "-m", "unittest"]
            if target_arg:
                cmd.append(target_arg)
            else:
                cmd.append("discover")
            if k:
                cmd.extend(["-k", str(k)])
            cmd.extend(extra_args)
            return cmd
        npx = shutil.which("npx") or "npx"
        if runner == "vitest":
            cmd = [npx, "--no-install", "vitest", "run"]
        else:
            cmd = [npx, "--no-install", "jest"]
        if target_arg:
            cmd.append(target_arg)
        if k:
            cmd.extend(["-t", str(k)])
        cmd.extend(extra_args)
        return cmd

    try:
        cmd_list = _build_cmd(fw)
        rc, stdout, stderr, timed_out = _run_subprocess_tree_safe(cmd_list, timeout, cwd=root_dir)
        if (
            not timed_out
            and rc != 0
            and framework == "auto"
            and fw == "pytest"
            and "No module named pytest" in (stderr or "")
        ):
            fw = "unittest"
            cmd_list = _build_cmd("unittest")
            rc, stdout, stderr, timed_out = _run_subprocess_tree_safe(cmd_list, timeout, cwd=root_dir)
        if timed_out:
            return _format_timeout_output(
                f"[Error] Test suite ({fw}) timed out after {timeout}s (process tree terminated cleanly).",
                stdout,
                stderr,
            )
        return _parse_test_output(fw, rc, stdout, stderr)
    except Exception as e:
        return f"[Error] {type(e).__name__}: {e}"


def _validate_local_http_url(url: str) -> Tuple[bool, str]:
    """Restrict `http_request` targets strictly to loopback or configured WSA bridge endpoint."""
    raw = str(url or "").strip()
    if not raw:
        return False, "[Error] `url` must be non-empty."
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        return False, f"[Error] Invalid URL: {exc}"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"[Safety Error] Unsupported URL scheme '{scheme}'. Only http/https are allowed."
    host = (parsed.hostname or "").strip("[]").lower()
    if not host:
        return False, "[Error] URL is missing hostname."
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True, ""

    try:
        cfg = urllib.parse.urlsplit(_bridge_api_base or "")
        cfg_host = (cfg.hostname or "").strip("[]").lower()
        cfg_port = cfg.port or (443 if cfg.scheme == "https" else 80)
        req_port = parsed.port or (443 if scheme == "https" else 80)
        if cfg_host and host == cfg_host and req_port == cfg_port:
            return True, ""
    except Exception:
        pass

    return False, (
        f"[Safety Error] http_request is restricted to localhost (127.0.0.1 / [::1]) or configured "
        f"endpoint ({_bridge_api_base}); blocked target '{host}'."
    )


class _LocalOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, err = _validate_local_http_url(newurl)
        if not ok:
            raise urllib.error.URLError(err)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _format_http_response(status: int, reason: str, resp_headers, raw_body: bytes) -> str:
    text = raw_body.decode("utf-8", errors="replace") if raw_body else ""
    ctype = ""
    hdr_lines: List[str] = []
    if resp_headers is not None:
        for h_name in ("Content-Type", "Cache-Control", "Location", "Content-Length"):
            val = resp_headers.get(h_name)
            if val:
                hdr_lines.append(f"  {h_name}: {val}")
                if h_name == "Content-Type":
                    ctype = val.lower()
    body_out = text
    if body_out and ("json" in ctype or body_out.lstrip().startswith(("{", "["))):
        try:
            body_out = json.dumps(json.loads(body_out), indent=2, ensure_ascii=False)
        except Exception:
            pass
    parts = [f"Status: {status} {reason}".strip()]
    if hdr_lines:
        parts.append("Headers:\n" + "\n".join(hdr_lines))
    if body_out:
        parts.append("Body:\n" + _truncate_tool_output(body_out, max_chars=14_000, label="HTTP response body"))
    else:
        parts.append("Body: (empty)")
    return "\n".join(parts)


def _http_request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    json_body: Any = None,
    body: Optional[str] = None,
    timeout: int = 15,
) -> str:
    ok, err = _validate_local_http_url(url)
    if not ok:
        return err

    meth = str(method or "GET").strip().upper()
    if meth not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        return f"[Error] Unsupported HTTP method '{method}'."

    req_headers: Dict[str, str] = {}
    if headers is not None:
        if not isinstance(headers, dict):
            return "[Error] `headers` must be a JSON object mapping header names to values."
        for k, v in headers.items():
            req_headers[str(k)] = str(v)

    data_bytes: Optional[bytes] = None
    if json_body is not None and body is not None:
        return "[Error] Pass either `json_body` or `body`, not both."
    if json_body is not None:
        if isinstance(json_body, str):
            data_bytes = json_body.encode("utf-8")
        else:
            data_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        if not any(k.lower() == "content-type" for k in req_headers):
            req_headers["Content-Type"] = "application/json; charset=utf-8"
    elif body is not None:
        data_bytes = str(body).encode("utf-8")

    req_timeout = max(1, min(int(timeout or 15), 120))
    req = urllib.request.Request(url.strip(), data=data_bytes, headers=req_headers, method=meth)
    opener = urllib.request.build_opener(_LocalOnlyRedirectHandler)
    try:
        with opener.open(req, timeout=req_timeout) as resp:
            raw = resp.read(262_144)
            return _format_http_response(
                getattr(resp, "status", resp.getcode()),
                getattr(resp, "reason", ""),
                resp.headers,
                raw,
            )
    except urllib.error.HTTPError as he:
        try:
            raw = he.read(262_144)
        except Exception:
            raw = b""
        return _format_http_response(he.code, he.reason or "", he.headers, raw)
    except urllib.error.URLError as ue:
        reason = str(getattr(ue, "reason", ue) or ue)
        if reason.startswith("[Safety Error]"):
            return reason
        return f"[Error] HTTP request failed: {reason}"
    except Exception as e:
        return f"[Error] HTTP request failed ({type(e).__name__}): {e}"


def _run_python(
    code: str,
    timeout: int = 120,
    args: Optional[List[str]] = None,
    cwd: Optional[str] = None,
) -> str:
    """Execute Python code in a temporary file using the project's interpreter (or active fallback)."""
    timeout = min(int(timeout), 600)
    run_cwd: Optional[Path] = None
    if cwd is not None and str(cwd).strip():
        resolved_cwd = _resolve(str(cwd))
        if not resolved_cwd.exists() or not resolved_cwd.is_dir():
            return f"[Error] Working directory not found or not a directory: {cwd}"
        run_cwd = resolved_cwd
    if args is not None and (not isinstance(args, list) or any(not isinstance(a, str) for a in args)):
        return "[Validation Error] run_python 'args' must be a list of strings."
    syntax_err = _validate_python_syntax("inline_script.py", code)
    if syntax_err:
        return syntax_err
    temp_path = None
    root_dir = get_project_root()
    py_exe = _detect_python_executable(root_dir)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", dir=str(root_dir), delete=False, encoding="utf-8"
        ) as tf:
            tf.write(code)
            temp_path = Path(tf.name)
        cmd_list = [py_exe, str(temp_path), *(args or [])]
        result_code, stdout, stderr, timed_out = _run_subprocess_tree_safe(
            cmd_list, timeout, cwd=(run_cwd if run_cwd is not None else root_dir)
        )
        if timed_out:
            return _format_timeout_output(
                f"[Error] Python script timed out after {timeout}s (process tree terminated cleanly).",
                stdout,
                stderr,
            )
        return _format_exec_output(result_code, stdout, stderr, argv=cmd_list)
    except Exception as e:
        return f"[Error] {type(e).__name__}: {e}"
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass


def _infer_custom_tool_schema(tree: ast.AST) -> Dict:
    """Infer JSON schema properties/required fields from `def run(...)` signature when omitted."""
    run_node = next(
        (n for n in getattr(tree, "body", []) if isinstance(n, ast.FunctionDef) and n.name == "run"),
        None,
    )
    if run_node is None:
        return {"type": "object", "properties": {}, "required": []}
    args = run_node.args
    pos = list(getattr(args, "posonlyargs", [])) + list(args.args)
    defaults_count = len(args.defaults)
    required_cutoff = len(pos) - defaults_count
    props: Dict[str, Dict[str, str]] = {}
    required: List[str] = []
    for idx, arg in enumerate(pos):
        ann = ast.unparse(arg.annotation).lower() if getattr(arg, "annotation", None) else ""
        if "int" in ann:
            jtype = "integer"
        elif "float" in ann:
            jtype = "number"
        elif "bool" in ann:
            jtype = "boolean"
        elif "list" in ann:
            jtype = "array"
        elif "dict" in ann:
            jtype = "object"
        else:
            jtype = "string"
        props[arg.arg] = {"type": jtype, "description": f"Parameter '{arg.arg}'"}
        if idx < required_cutoff:
            required.append(arg.arg)
    return {"type": "object", "properties": props, "required": required}


def _create_custom_tool(name: str, description: str, python_code: str, parameters_json: str = None) -> str:
    """Validate and register a custom runtime Python tool."""
    name = name.strip()
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name):
        return f"[Error] Invalid tool name '{name}'. Use snake_case letters, digits, and underscores."
    if name in _BUILTIN_TOOL_NAMES:
        return f"[Error] Cannot overwrite built-in tool '{name}'."

    syntax_err = _validate_python_syntax(f"{name}.py", python_code)
    if syntax_err:
        return syntax_err
    tree = ast.parse(python_code, filename=f"{name}.py")
    has_sync_run = any(
        isinstance(node, ast.FunctionDef) and node.name == "run"
        for node in tree.body
    )
    if not has_sync_run:
        return "[Error] `python_code` must define a top-level synchronous `def run(...) -> str:` function."
    try:
        compile(tree, filename=f"<custom_tool:{name}>", mode="exec")
    except Exception as exc:
        return f"[Error] Failed compiling custom tool '{name}': {exc}"

    params_schema = _infer_custom_tool_schema(tree)
    if parameters_json:
        try:
            parsed = json.loads(parameters_json) if isinstance(parameters_json, str) else parameters_json
            if isinstance(parsed, dict):
                params_schema = parsed
        except Exception as e:
            return f"[Error] Invalid parameters_json: {e}"

    load_custom_tools()
    _custom_tools[name] = {
        "description": description,
        "parameters": params_schema,
        "code": python_code,
    }
    save_custom_tools()
    TOOL_DEFINITIONS[:] = [t for t in TOOL_DEFINITIONS if t.get("function", {}).get("name") != name]
    _sync_custom_tool_definitions()
    param_names = ", ".join((params_schema.get("properties") or {}).keys()) or "none"
    return f"[OK] Custom tool '{name}' registered (params: {param_names}) and ready to call!"


def _list_custom_tools() -> str:
    load_custom_tools()
    if not _custom_tools:
        return "[No custom tools registered yet. Use create_custom_tool() to create one.]"
    lines = [f"Registered Custom Tools ({len(_custom_tools)}):"]
    for key, meta in _custom_tools.items():
        lines.append(f"  - {key}: {meta.get('description', '')}")
    return "\n".join(lines)


def remove_custom_tool(name: str) -> bool:
    load_custom_tools()
    if name in _custom_tools:
        del _custom_tools[name]
        save_custom_tools()
        TOOL_DEFINITIONS[:] = [t for t in TOOL_DEFINITIONS if t.get("function", {}).get("name") != name]
        return True
    return False


def _remove_custom_tool_action(name: str) -> str:
    if remove_custom_tool(name):
        return f"[OK] Removed custom tool '{name}'."
    return f"[Error] Custom tool not found: {name}"


def _execute_custom_tool(name: str, args: dict) -> str:
    import collections
    import datetime
    import math
    load_custom_tools()
    meta = _custom_tools.get(name)
    if not meta:
        return f"[Error] Custom tool '{name}' not found."
    code = meta.get("code", "")
    syntax_err = _validate_python_syntax(f"{name}.py", code)
    if syntax_err:
        return syntax_err
    ns: dict = {
        "__builtins__": __builtins__,
        "PROJECT_ROOT": get_project_root(),
        "Path": Path,
        "json": json,
        "re": re,
        "os": os,
        "sys": sys,
        "time": time,
        "math": math,
        "datetime": datetime,
        "collections": collections,
        "subprocess": subprocess,
    }
    try:
        exec(compile(code, filename=f"<custom_tool:{name}>", mode="exec"), ns)
        run_fn = ns.get("run")
        if not callable(run_fn):
            return f"[Error] Custom tool '{name}' does not define a callable `run` function."
        return str(run_fn(**(args or {})))
    except Exception as e:
        tb_lines = traceback.format_exc().strip().splitlines()
        tail = tb_lines[-2] if len(tb_lines) >= 2 else ""
        return f"[Error in custom tool '{name}'] {type(e).__name__}: {e}" + (f" ({tail.strip()})" if tail else "")


def _set_dynamic_rule(key: str, rule: str) -> str:
    load_dynamic_rules()
    key = key.strip()
    rule = rule.strip()
    if not rule:
        if key in _dynamic_rules:
            del _dynamic_rules[key]
            save_dynamic_rules()
            return f"[OK] Removed dynamic rule '{key}'."
        return f"[Info] Dynamic rule '{key}' was not set."
    _dynamic_rules[key] = rule
    save_dynamic_rules()
    return f"[OK] Dynamic rule '{key}' active: {rule}"


def _list_dynamic_rules() -> str:
    rules = load_dynamic_rules()
    if not rules:
        return "[No dynamic rules set.]"
    lines = [f"Dynamic rules ({len(rules)}):"]
    for key, rule in rules.items():
        lines.append(f"  - {key}: {rule}")
    return "\n".join(lines)


# ─── Live Browser Visual Inspection Bridge ────────────────────────────────────

def _call_browser_bridge(action: str, params: Optional[dict] = None, timeout: float = 25.0) -> dict:
    """Invoke a browser inspection/action command through the local WSA proxy server."""
    base = (_bridge_api_base or os.environ.get("WSA_API_BASE") or os.environ.get("GWA_API_BASE") or "http://127.0.0.1:8081").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    url = f"{base}/v1/browser/command"
    payload = json.dumps({
        "action": action,
        "params": params or {},
        "timeout": timeout,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if _bridge_api_key:
        headers["Authorization"] = f"Bearer {_bridge_api_key}"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=max(5.0, timeout + 5.0)) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raw_err = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw_err)
            if isinstance(parsed, dict):
                return parsed if parsed.get("error") else {"ok": False, "error": raw_err[:400]}
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code}: {raw_err[:400]}"}
    except Exception as e:
        return {"ok": False, "error": f"Browser bridge unreachable ({type(e).__name__}: {e})"}


def _queue_browser_screenshot_data_url(data_url: str, label: str = "browser_tab") -> Optional[str]:
    """Decode captureVisibleTab data URI, save temp preview PNG, and queue visual attachment."""
    raw_url = str(data_url or "").strip()
    if not raw_url.startswith("data:image/"):
        return None
    header, _, b64_part = raw_url.partition(",")
    if not b64_part:
        return None
    try:
        raw = base64.b64decode(b64_part, validate=True)
    except Exception:
        return None
    if not raw or len(raw) > 8 * 1024 * 1024:
        return None
    ext = ".jpg" if "jpeg" in header.lower() else ".png"
    info = detect_image_info(raw, ext)
    if not info:
        return None
    shot_dir = Path(tempfile.gettempdir()) / "wsa_browser_shots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{label}_{int(time.time() * 1000)}{ext}"
    shot_path = shot_dir / fname
    try:
        shot_path.write_bytes(raw)
        # Keep only the latest 12 temp browser screenshots so tmp stays clean
        old_shots = sorted(shot_dir.glob("*"), key=lambda p: p.stat().st_mtime)[:-12]
        for old_p in old_shots:
            old_p.unlink(missing_ok=True)
    except Exception:
        pass
    b64 = base64.b64encode(raw).decode("ascii")
    with _pending_images_lock:
        _pending_images.append({
            "filename": fname,
            "path": str(shot_path),
            "mime_type": str(info["mime"]),
            "url": f"data:{info['mime']};base64,{b64}",
            "width": str(info["width"]),
            "height": str(info["height"]),
        })
    dims = f"{info['width']}x{info['height']}" if info["width"] and info["height"] else "captured"
    return f"{shot_path} ({info['format']} {dims}, {len(raw) // 1024 or 1} KB)"


def _format_browser_diagnostics(res: dict) -> str:
    if not res or not res.get("ok"):
        err = res.get("error") if isinstance(res, dict) else "Unknown browser error"
        if isinstance(err, dict):
            err = err.get("message") or str(err)
        return f"[Error] {err}"

    lines = []
    if res.get("interaction"):
        inter = res["interaction"]
        lines.append(f"[OK] Action: {inter.get('detail', 'done')}")

    diag = res.get("diagnostics") or {}
    tab_id = res.get("tab_id", "")
    vp = diag.get("viewport") or {}
    lines.append(
        f"Tab #{tab_id}: {diag.get('title') or '(untitled)'} — {diag.get('url', '')} "
        f"[ready={diag.get('readyState', '?')}, viewport={vp.get('width', 0)}x{vp.get('height', 0)}, "
        f"scrollY={vp.get('scrollY', 0)}/{vp.get('scrollHeight', 0)}]"
    )

    shot_desc = _queue_browser_screenshot_data_url(
        res.get("screenshot_data_url") or "",
        label=f"tab_{tab_id or 'live'}",
    )
    if shot_desc:
        lines.append(f"[OK] Live screenshot queued for visual inspection -> {shot_desc}")
    elif res.get("screenshot_warning"):
        lines.append(f"[Warning] Screenshot capture unavailable: {res['screenshot_warning']}")

    issues = []
    if int(diag.get("horizOverflowPx") or 0) > 0:
        issues.append(f"Horizontal overflow: +{diag['horizOverflowPx']}px beyond viewport width")
    if diag.get("brokenImages"):
        issues.append("Broken <img>: " + ", ".join(diag["brokenImages"][:5]))
    if diag.get("clippedElements"):
        issues.append("Clipped elements: " + "; ".join(diag["clippedElements"][:5]))
    if diag.get("offscreenControls"):
        issues.append("Right-clipped controls: " + "; ".join(diag["offscreenControls"][:5]))
    if diag.get("alerts"):
        issues.append("Visible alerts/dialogs: " + " | ".join(diag["alerts"][:4]))
    if diag.get("consoleErrors"):
        errs = [f"{e.get('kind')}: {e.get('msg')}" for e in diag["consoleErrors"][-8:]]
        issues.append("Console/HTTP errors:\n    - " + "\n    - ".join(errs))

    lines.append("Layout & Runtime Checks: " + ("ALL CLEAR (0 errors/overflow)" if not issues else "\n  • " + "\n  • ".join(issues)))

    if diag.get("headings"):
        lines.append("Headings: " + " | ".join(diag["headings"][:8]))
    if diag.get("controls"):
        ctrl_strs = [
            f"{c.get('selector', c.get('tag'))}"
            + (f"(\"{c['label']}\")" if c.get("label") else "")
            + (f"[{c['state']}]" if c.get("state") else "")
            for c in diag["controls"][:18]
        ]
        lines.append("Interactive Controls: " + ", ".join(ctrl_strs))
    if diag.get("visibleText"):
        lines.append("Visible Page Text:\n" + diag["visibleText"])

    return _truncate_tool_output("\n".join(lines), max_chars=18_000, label="browser diagnostics")


def _browser_tabs() -> str:
    res = _call_browser_bridge("list_tabs", {}, timeout=12.0)
    if not res or not res.get("ok"):
        err = (res or {}).get("error") or "Failed to list browser tabs"
        return f"[Error] {err}"
    tabs = res.get("tabs") or []
    if not tabs:
        return "[No browser tabs open]"
    out = [f"Open Browser Tabs ({len(tabs)}):"]
    for t in tabs:
        flags = []
        if t.get("active"):
            flags.append("ACTIVE")
        if t.get("isAiRelay") or t.get("isGeminiRelay"):
            flags.append("WSA-RELAY-PROTECTED")
        elif t.get("isProtected"):
            flags.append("PROTECTED")
        if t.get("isLastInspected"):
            flags.append("LAST-INSPECTED")
        tag = f" [{', '.join(flags)}]" if flags else ""
        out.append(f"  #{t.get('id')}{tag}: {t.get('title') or '(untitled)'} — {t.get('url')}")
    return "\n".join(out)


def _browser_inspect(
    url: str = None,
    tab_id: int = None,
    reload: bool = False,
    wait_ms: int = 600,
    capture_screenshot: bool = True,
    restore_focus: bool = True,
) -> str:
    res = _call_browser_bridge(
        "inspect",
        {
            "url": url or "",
            "tab_id": tab_id,
            "reload": bool(reload),
            "wait_ms": int(wait_ms),
            "capture_screenshot": bool(capture_screenshot),
            "restore_focus": bool(restore_focus),
        },
        timeout=30.0,
    )
    return _format_browser_diagnostics(res)


def _browser_interact(
    action: str,
    selector: str = None,
    text: str = None,
    value: str = None,
    scroll_y: str = None,
    url: str = None,
    tab_id: int = None,
    wait_ms: int = 500,
    capture_screenshot: bool = True,
    restore_focus: bool = True,
) -> str:
    res = _call_browser_bridge(
        "interact",
        {
            "action": action,
            "selector": selector or "",
            "text": text or "",
            "value": "" if value is None else str(value),
            "scroll_y": "" if scroll_y is None else str(scroll_y),
            "url": url or "",
            "tab_id": tab_id,
            "wait_ms": int(wait_ms),
            "capture_screenshot": bool(capture_screenshot),
            "restore_focus": bool(restore_focus),
        },
        timeout=30.0,
    )
    return _format_browser_diagnostics(res)


def _queue_visual_bytes(
    raw: bytes,
    mime: str = "image/webp",
    *,
    width: int = 0,
    height: int = 0,
    label: str = "visual_sheet",
) -> Optional[str]:
    """Persist a temporary visual contact sheet and queue it for the next multimodal agent turn."""
    if not raw or len(raw) > 8 * 1024 * 1024:
        return None
    ext_map = {"image/webp": ".webp", "image/jpeg": ".jpg", "image/png": ".png"}
    ext = ext_map.get(str(mime or "").lower(), ".png")
    shot_dir = Path(tempfile.gettempdir()) / "wsa_browser_shots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{label}_{int(time.time() * 1000)}{ext}"
    shot_path = shot_dir / fname
    try:
        shot_path.write_bytes(raw)
        old_shots = sorted(shot_dir.glob("*"), key=lambda p: p.stat().st_mtime)[:-12]
        for old_p in old_shots:
            old_p.unlink(missing_ok=True)
    except Exception:
        pass
    b64 = base64.b64encode(raw).decode("ascii")
    with _pending_images_lock:
        _pending_images.append({
            "source_tool": "visual_inspect",
            "filename": fname,
            "path": str(shot_path),
            "mime_type": str(mime or "image/png"),
            "url": f"data:{mime or 'image/png'};base64,{b64}",
            "width": str(width or ""),
            "height": str(height or ""),
        })
    dims = f"{width}x{height}" if width and height else "captured"
    fmt = ext.lstrip(".").upper()
    return f"{shot_path} ({fmt} {dims}, {len(raw) // 1024 or 1} KB)"


def _visual_inspect(
    target: str = "index.html",
    viewports: Optional[List[Dict[str, Any]]] = None,
    actions: Optional[List[Dict[str, Any]]] = None,
    annotate: bool = True,
    full_page: bool = False,
    include_diff: bool = True,
    color_scheme: str = "",
    disable_webgl: bool = False,
    wait_ms: int = 500,
) -> str:
    """Headless CDP visual inspection tool: zero focus stealing, 2x2 contact sheet + Set-of-Mark + telemetry."""
    from .visual import capture_visual_inspection, format_visual_report

    raw_target = str(target or "index.html").strip()
    root = get_project_root()
    if not raw_target.startswith(("http://", "https://")):
        try:
            resolved_path = _resolve(raw_target)
        except Exception as exc:
            return f"[Error] Visual target outside workspace: {exc}"
        raw_target = str(resolved_path)

    res = capture_visual_inspection(
        raw_target,
        project_root=root,
        viewports=viewports,
        actions=actions,
        annotate=bool(annotate),
        full_page=bool(full_page),
        include_diff=bool(include_diff),
        color_scheme=str(color_scheme or ""),
        disable_webgl=bool(disable_webgl),
        wait_ms=max(0, min(int(wait_ms or 500), 8000)),
    )
    if not res.get("ok"):
        return format_visual_report(res)

    saved_desc = _queue_visual_bytes(
        res.get("image_bytes") or b"",
        res.get("image_mime") or "image/webp",
        width=int(res.get("image_width") or 0),
        height=int(res.get("image_height") or 0),
        label="visual_contact_sheet",
    )
    if not saved_desc:
        return "[Error] Visual capture could not be attached (empty or over the image size limit)."
    return format_visual_report(res, saved_path_summary=saved_desc)
