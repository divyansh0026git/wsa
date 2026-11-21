"""
Rich TUI renderer for WSA — authentic Claude Code TUI look, theme, and layout.
"""
from __future__ import annotations

import os
import re
import sys
import textwrap
import threading
import time
import weakref
from pathlib import Path
from typing import Callable, Iterable, Optional

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import CodeBlock as _RichCodeBlock, Heading, Markdown, TableElement as _RichTableElement
from rich.markup import escape
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax as _RichSyntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme as RichTheme

from .session import MODEL_CATALOG, get_model_info
from .theme import ThemePalette, get_theme, set_theme as _set_active_theme
from .visual_status import format_live_elapsed


# Open-right copy-safe container box: prevents trailing `│` borders when selecting text in terminal
_CODE_BOX = box.Box(
    "╭─  \n"
    "│   \n"
    "├─  \n"
    "│   \n"
    "│   \n"
    "├─  \n"
    "│   \n"
    "╰─  \n"
)
_CODE_BG = "#141519"
_INLINE_BACKTICK_RE = re.compile(r"`[^`\n]+`")
_URL_RE = re.compile(r"https?://[^\s)>\]`'\"]+")
_PROMPT_STEP_RE = re.compile(r"^(\s*(?:\d+\.|[-*+])\s+)(.*)$")


def _is_prompt_like_text(lexer_lower: str, code: str) -> bool:
    if lexer_lower == "prompt":
        return True
    if lexer_lower not in ("", "text", "txt", "plain"):
        return False
    low = (code or "").lower()
    return any(
        cue in low
        for cue in (
            "i am on",
            "make sure",
            "you are ",
            "paste this",
            "system prompt",
            "instruction:",
            "follow these",
        )
    )


def _format_prompt_block_text(code: str) -> Text:
    """Format prompt/text fenced blocks with high-contrast prose and highlighted inline commands."""
    out = Text(overflow="fold")
    base_fg = "#f4f4f6"
    lines = (code or "").splitlines()
    for idx, line in enumerate(lines):
        if idx > 0:
            out.append("\n")
        m_step = _PROMPT_STEP_RE.match(line)
        prefix = m_step.group(1) if m_step else ""
        body = m_step.group(2) if m_step else line
        if prefix:
            out.append(prefix, style=f"bold {Theme.ACCENT}")
        cursor = 0
        # Highlight inline `commands/flags` and https:// URLs while keeping exact copyable characters
        spans = []
        for bm in _INLINE_BACKTICK_RE.finditer(body):
            spans.append((bm.start(), bm.end(), f"bold {Theme.SHIMMER} on #1e2028"))
        for um in _URL_RE.finditer(body):
            if not any(s <= um.start() < e for s, e, _ in spans):
                spans.append((um.start(), um.end(), f"underline {Theme.PERMISSION}"))
        spans.sort(key=lambda sp: sp[0])
        for start, end, style in spans:
            if start < cursor:
                continue
            if start > cursor:
                out.append(body[cursor:start], style=base_fg)
            out.append(body[start:end], style=style)
            cursor = end
        if cursor < len(body):
            out.append(body[cursor:], style=base_fg)
    return out


class _QuietHeading(Heading):
    """Render Markdown headings left-aligned without Rich's loud centered H1 panel."""

    def __rich_console__(self, console_obj, options):
        src = self.text
        plain = src.plain
        if self.tag == "h1":
            style = f"bold {Theme.ACCENT}"
        elif self.tag == "h2":
            style = f"bold {Theme.PRIMARY}"
        else:
            style = f"bold {Theme.SECONDARY}"
        out = Text(plain, style=style, justify="left")
        out.spans.extend(src.spans)
        yield out


class _StyledCodeBlock(_RichCodeBlock):
    """Render fenced code & prompt templates inside a high-contrast, copy-safe card."""

    def __rich_console__(self, _console, _options):
        code = str(self.text).rstrip()
        if _looks_like_tool_json_body(code, unclosed=True):
            return
        lexer = (self.lexer_name or "text").strip() or "text"
        lexer_lower = lexer.lower()
        border_col = "#52525b" if Theme.BORDER == "#3f3f46" else Theme.BORDER
        if lexer_lower in ("text", "txt", "plain", "prompt", "markdown", "md"):
            body = _format_prompt_block_text(code)
            label = "prompt" if _is_prompt_like_text(lexer_lower, code) else lexer_lower
            title = f"[bold {Theme.ACCENT}]{escape(label)}[/]"
        else:
            body = _RichSyntax(
                code,
                lexer,
                theme=Theme.CODE_THEME or self.theme,
                word_wrap=True,
                padding=0,
                background_color=_CODE_BG,
            )
            title = f"[bold {Theme.SECONDARY}]{escape(lexer)}[/]"
        yield Panel(
            body,
            title=title,
            title_align="left",
            box=_CODE_BOX,
            border_style=border_col,
            padding=(0, 1),
            style=f"on {_CODE_BG}",
            expand=True,
        )


class _StyledTableElement(_RichTableElement):
    """Render Markdown data tables with clean rounded borders and readable folded cells."""

    def __rich_console__(self, _console, _options):
        border_col = "#52525b" if Theme.BORDER == "#3f3f46" else Theme.BORDER
        table = Table(
            box=box.ROUNDED,
            show_edge=True,
            show_header=self.header is not None,
            header_style=f"bold {Theme.ACCENT}",
            border_style=border_col,
            pad_edge=True,
            padding=(0, 1),
            collapse_padding=False,
            expand=False,
        )
        if self.header is not None and self.header.row is not None:
            for column in self.header.row.cells:
                heading = column.content.copy()
                heading.stylize(f"bold {Theme.ACCENT}")
                table.add_column(heading, overflow="fold")
        if self.body is not None:
            for row in self.body.rows:
                row_content = [element.content for element in row.cells]
                table.add_row(*row_content)
        yield table


class _CleanMarkdown(Markdown):
    elements = {
        **Markdown.elements,
        "heading_open": _QuietHeading,
        "fence": _StyledCodeBlock,
        "code_block": _StyledCodeBlock,
        "table_open": _StyledTableElement,
    }

# Windows terminal UTF-8 safety
if sys.platform == "win32":
    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(sys, _stream_name, None)
        if _stream and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def sanitize_surrogates(text: str) -> str:
    """Repair valid UTF-16 surrogate pairs and replace lone surrogates safely."""
    if not text:
        return ""
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return text
    try:
        return text.encode("utf-16", errors="surrogatepass").decode("utf-16", errors="replace")
    except Exception:
        return "".join("\ufffd" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text)


console = Console(highlight=False)

# Vertical-only center divider box for Rich 2-column welcome card
_WELCOME_SPLIT_BOX = box.Box(
    "    \n"
    "  │ \n"
    "  │ \n"
    "  │ \n"
    "  │ \n"
    "  │ \n"
    "  │ \n"
    "    \n"
)


# ─── Live-Synced Claude Code Theme Class ──────────────────────────────────────

class Theme:
    ACCENT = "#d77757"
    SHIMMER = "#eb9f7f"
    PRIMARY = "#ededed"
    SECONDARY = "#a1a1a6"
    DIM = "#76767c"
    BORDER = "#3f3f46"
    WELCOME_BORDER = "#d77757"
    OK = "#4cc38a"
    ERR = "#e5484d"
    WARN = "#d99b38"
    PERMISSION = "#b1b9f9"
    AUTO_ACCEPT = "#af87ff"
    PLAN_MODE = "#48968c"
    CODE_THEME = "ansi_dark"

    # Backwards-compatible aliases
    USER = "#ededed"
    AGENT = "#d77757"
    TOOL_CALL = "#ededed"
    TOOL_OK = "#4cc38a"
    TOOL_ERR = "#e5484d"
    TOOL_OUT = "#a1a1a6"
    INFO = "#a1a1a6"
    SUCCESS = "#4cc38a"
    ERROR = "#e5484d"
    SEP = "#3f3f46"


def sync_theme(palette: Optional[ThemePalette] = None) -> ThemePalette:
    """Sync Theme attributes from the active ThemePalette."""
    p = palette or get_theme()
    Theme.ACCENT = p.claude
    Theme.SHIMMER = p.claude_shimmer
    Theme.PRIMARY = p.text
    Theme.SECONDARY = p.secondary_text
    Theme.DIM = p.subtle
    Theme.BORDER = p.border
    Theme.WELCOME_BORDER = p.welcome_border
    Theme.OK = p.success
    Theme.ERR = p.error
    Theme.WARN = p.warning
    Theme.PERMISSION = p.permission
    Theme.AUTO_ACCEPT = p.auto_accept
    Theme.PLAN_MODE = p.plan_mode
    Theme.CODE_THEME = p.code_theme

    Theme.USER = p.text
    Theme.AGENT = p.claude
    Theme.TOOL_CALL = p.text
    Theme.TOOL_OK = p.diff_added
    Theme.TOOL_ERR = p.diff_removed
    Theme.TOOL_OUT = p.secondary_text
    Theme.INFO = p.secondary_text
    Theme.SUCCESS = p.success
    Theme.ERROR = p.error
    Theme.SEP = p.border
    _apply_console_theme(console)
    return p


def _build_rich_theme() -> RichTheme:
    border_col = "#52525b" if Theme.BORDER == "#3f3f46" else Theme.BORDER
    return RichTheme({
        "markdown.paragraph": Theme.PRIMARY,
        "markdown.text": Theme.PRIMARY,
        "markdown.code": f"bold {Theme.SHIMMER} on #1b1b22",
        "markdown.code_block": f"{Theme.PRIMARY} on {_CODE_BG}",
        "markdown.block_quote": Theme.SECONDARY,
        "markdown.link": f"underline {Theme.PERMISSION}",
        "markdown.link_url": Theme.DIM,
        "markdown.item.bullet": f"bold {Theme.ACCENT}",
        "markdown.item.number": f"bold {Theme.ACCENT}",
        "markdown.hr": border_col,
        "markdown.table.border": border_col,
        "markdown.table.header": f"bold {Theme.ACCENT}",
    })


def _apply_console_theme(target_console: Console) -> None:
    try:
        stack = getattr(target_console, "_theme_stack", None)
        entries = getattr(stack, "_entries", None)
        if isinstance(entries, list):
            while len(entries) > 1:
                target_console.pop_theme()
        target_console.push_theme(_build_rich_theme(), inherit=True)
    except Exception:
        pass


sync_theme()


def apply_theme(theme_id: str, persist: bool = True) -> ThemePalette:
    """Switch active theme and sync renderer palette tokens."""
    p = _set_active_theme(theme_id, persist=persist)
    return sync_theme(p)


# ─── Label / Text Helpers ─────────────────────────────────────────────────────

_THINK_NOISE_PATTERNS = (
    re.compile(r"^gemini\s+(?:is\s+typing|replied|responded|thinking).*$", re.IGNORECASE),
    re.compile(r"^claude\s+(?:is\s+responding|responded|replied|thinking).*$", re.IGNORECASE),
    re.compile(r"^(?:chatgpt|deepseek|venice)\s+(?:said|is\s+typing|is\s+responding|replied|responded|thinking|reasoning).*$", re.IGNORECASE),
    re.compile(r"^(?:show|hide)\s+(?:thinking|message\s+actions).*$", re.IGNORECASE),
    re.compile(r"^(?:thinking|thought)\s*(?:process)?:?$", re.IGNORECASE),
    re.compile(r"^extended\s+thinking:?$", re.IGNORECASE),
    re.compile(r"^(?:thought|reasoned|worked)\s+for\s+(?:a\s+(?:couple|few)\s+of\s+seconds|\d+(?:\.\d+)?\s*(?:s|sec|seconds?)?(?:\s*·\s*\d+\s*steps?)?(?:\s+running)?)$", re.IGNORECASE),
    re.compile(r"^(?:running|done|loading(?:\.{3}|…)?|\d+(?:\.\d+)?s)$", re.IGNORECASE),
    re.compile(r"^updated\s+memory.*$", re.IGNORECASE),
)
_INLINE_THINK_SUFFIX_RE = re.compile(r"(?:\s+(?:\brunning|\d{1,3}(?:\.\d+)?s))+$", re.IGNORECASE)
_GLUED_THINK_CHROME_RE = re.compile(
    r"(?:\d{1,4}(?:\.\d+)?s\s*running"
    r"|\d{1,4}(?:\.\d+)?s(?=[A-Z][a-z]{2,})"
    r"|(?<=[a-z]{4})\d{1,4}(?:\.\d+)?s(?=$|\s|[A-Z])"
    r"|(?<=[.?!\)])\d{1,4}(?:\.\d+)?s(?=$|\s|[A-Z])"
    r"|(?:(?<=[.?!)])|(?<=\d s)|(?<=\ds))running(?=$|\s|[A-Z])"
    r"|(?<=[a-z.?!)])running(?=[A-Z][a-z]{2,}))",
    re.IGNORECASE,
)
_REPEAT_THINK_PHRASE_RE = re.compile(r"(\S.{9,}?)\1+")
_ORPHAN_TOOL_HEADER_LINE_RE = re.compile(
    r"(?im)^[ \t]*(?:(?:tool[_-]?calls?|function[_-]?calls?)[ \t]*)+(?:\r?\n|$)"
)
_GLUED_TOOL_HEADER_BEFORE_JSON_RE = re.compile(
    r"(?im)^[ \t]*(?:(?:tool[_-]?calls?|function[_-]?calls?)[ \t]*)+(?=\{)"
)

_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>[\s\S]*?</tool_call>", re.IGNORECASE)
_THINK_TAG_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_ALT_TOOL_TAG_RE = re.compile(r"<<TOOL_CALL>>[\s\S]*?<<END_TOOL_CALL>>", re.IGNORECASE)
_STREAM_UNCLOSED_XML_RE = re.compile(
    r"(?:^|\n)[ \t]*(?:<think>(?:[ \t]*\r?\n|$)|<(?:tool_call)(?:[ \t]*\r?\n|[ \t]*\{)|<<TOOL_CALL>>|"
    r"<(?:t(?:h(?:i(?:nk?)?)?|o(?:o(?:l(?:_(?:c(?:a(?:l(?:l)?)?)?)?)?)?)?)?)?$)"
    r"[\s\S]*$",
    re.IGNORECASE,
)
_LIVE_TOOL_FENCE_RE = re.compile(
    r"(?:^|(?<=\n))[ \t]*(?:`{3,}|~{3,})[\u200b\u200c\u200d\ufeff \t]*(?:tool[_-]?calls?|function[_-]?calls?)\b[\s\S]*?(?:(?:`{3,}|~{3,})(?:[\u200b\u200c\u200d\ufeff \t]*_\])?[ \t]*(?:\r?\n|$)|$)",
    re.IGNORECASE,
)
_STREAM_PARTIAL_TOOL_FENCE_RE = re.compile(
    r"(?:^|\n)[ \t]*(?:`{3,}|~{3,})[\u200b\u200c\u200d\ufeff \t]*(?:"
    r"(?:t(?:o(?:o(?:l(?:[_-]?(?:c(?:a(?:l(?:ls?)?)?)?)?)?)?)?)?|"
    r"f(?:u(?:n(?:c(?:t(?:i(?:o(?:n(?:[_-]?(?:c(?:a(?:l(?:ls?)?)?)?)?)?)?)?)?)?)?)?)?)[ \t]*$|"
    r"(?:tool(?:[_-]?calls?)?|function[_-]?calls?)\b[ \t]*\r?\n(?:(?!(?:\r?\n)[ \t]*(?:`{3,}|~{3,})[ \t]*(?:\r?\n|$))[\s\S])*$)",
    re.IGNORECASE,
)
_GENERIC_JSON_FENCE_RE = re.compile(
    r"(?:^|(?<=\n))[ \t]*(?P<ticks>`{3,}|~{3,})[\u200b\u200c\u200d\ufeff \t]*(?:json|javascript|js|text|txt|plain|tool_call|code)?[ \t]*\r?\n(?P<body>(?![ \t]*(?:`{3,}|~{3,}))[\s\S]*?)(?:(?P=ticks)(?:[\u200b\u200c\u200d\ufeff \t]*_\])?[ \t]*(?:\r?\n|$)|$)",
    re.IGNORECASE,
)
_CODE_SNIPPET_TOOL_RE = re.compile(
    r"(?:^|\n)[ \t]*Code snippet[ \t]*\r?\n(?P<body>[ \t]*[\{\[][\s\S]*?)(?=\r?\n[ \t]*\r?\n|$)",
    re.IGNORECASE,
)
_TOOL_JSON_HEAD_RE = re.compile(
    r'^(?:\{\s*"(?:name|arguments|args|function)"\s*:|\[\s*\{\s*"(?:name|arguments|args|function)"\s*:)'
)
_STRAY_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")


def _looks_like_tool_json_body(body: str, unclosed: bool = False) -> bool:
    s = (body or "").lstrip()
    if not s or s[0] not in "{[":
        return False
    if _TOOL_JSON_HEAD_RE.match(s):
        if ('"name"' in s and ('"arguments"' in s or '"args"' in s)) or unclosed:
            return True
        if re.match(
            r'^(?:\{\s*"(?:name|tool|function|action)"\s*:\s*"[^"]{0,64}"|\{\s*"(?:name|arguments|args)"\s*:|\{\s*"\s*$|\[\s*\{\s*")',
            s,
        ):
            return True
    if re.match(
        r'^[\{\[](?:[\s\{\[]{0,24}(?:"[A-Za-z_]{0,12}"?)?)?$',
        s,
    ):
        return True
    if '"name"' in s and ('"arguments"' in s or '"args"' in s):
        return bool(re.search(r'"name"\s*:\s*"[A-Za-z_][A-Za-z0-9_.-]{0,63}"', s))
    return False


def _clean_aborted_tool_prose(text: str) -> str:
    """Unwrap whole-response code blocks and strip aborted tool-call prefixes when the payload is rich markdown."""
    if not text:
        return ""
    s = text.strip()
    m = re.match(r"^(`{3,}|~{3,})[a-zA-Z0-9_-]*\r?\n([\s\S]*?)\r?\n\1$", s)
    if m:
        candidate = m.group(2).strip()
        if re.search(r"(?:^|\n)[ \t]*#{1,6}\s", candidate) or re.search(r"(?:^|\n)[ \t]*\*\s+\*\*", candidate):
            s = candidate

    s = re.sub(
        r'^\s*\{\s*"name"\s*:\s*"[A-Za-z0-9_.-]+"\s*,\s*"?([A-Z][\s\S]*)$',
        r'\1',
        s,
    )
    s = re.sub(r'"\s*(?=\r?\n[ \t]*#{1,6}\s)', '', s)
    if re.match(r"^`{1,3}\s*$", s):
        return ""
    return s


def _strip_leaked_tool_json(text: str, streaming: bool = False) -> str:
    """Remove leaked tool-call JSON fences, Code snippet wrappers, or bare tool objects."""
    if not text:
        return ""

    def _repl_fence(m):
        full = m.group(0)
        body = m.group("body")
        ticks = m.group("ticks")
        closed = bool(re.search(rf"{re.escape(ticks)}(?:[\u200b\u200c\u200d\ufeff \t]*_\])?[ \t]*(?:\r?\n|$)", full[m.start("body") - m.start():]))
        return "" if _looks_like_tool_json_body(body, unclosed=(streaming and not closed)) else full

    cleaned = _GENERIC_JSON_FENCE_RE.sub(_repl_fence, text)

    def _repl_snippet(m):
        return "" if _looks_like_tool_json_body(m.group("body"), unclosed=False) else m.group(0)

    cleaned = _CODE_SNIPPET_TOOL_RE.sub(_repl_snippet, cleaned)

    # Strip bare line-start tool JSON (complete balanced blocks or streaming EOF tail)
    out = []
    pos = 0
    for bm in re.finditer(r"(?:^|(?<=\n))[ \t]*(?=[\{\[])", cleaned):
        start = bm.start()
        if start < pos:
            continue
        idx = bm.end()
        tail = cleaned[idx:]
        if not _TOOL_JSON_HEAD_RE.match(tail):
            continue
        stack = []
        in_str = False
        esc = False
        end_pos = None
        for cur in range(idx, len(cleaned)):
            ch = cleaned[cur]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]":
                if not stack or stack[-1] != ch:
                    break
                stack.pop()
                if not stack:
                    end_pos = cur + 1
                    break
        if end_pos is not None:
            candidate = cleaned[idx:end_pos]
            if _looks_like_tool_json_body(candidate, unclosed=False):
                out.append(cleaned[pos:start])
                pos = end_pos
        elif streaming and stack and _looks_like_tool_json_body(tail, unclosed=True):
            if not re.search(r"\n[ \t]*#{1,6}\s", tail) and len(tail) < 1500:
                out.append(cleaned[pos:start])
                pos = len(cleaned)
                break
    if pos:
        out.append(cleaned[pos:])
        cleaned = "".join(out)
    return _STRAY_ZERO_WIDTH_RE.sub("", cleaned)


_FENCE_OPEN_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})([^\n]*)$")


def _fence_state_per_line(lines: list[str]) -> list[str]:
    """Return opening fence header active BEFORE each line (or empty string if outside fence)."""
    state: list[str] = []
    open_char = ""
    open_len = 0
    open_header = ""
    for line in lines:
        state.append(open_header)
        m = _FENCE_OPEN_RE.match(line)
        if not m:
            continue
        marker, rest = m.group(1), m.group(2).strip()
        if not open_header:
            open_char = marker[0]
            open_len = len(marker)
            open_header = f"{marker}{rest}" if rest else marker
        elif marker[0] == open_char and len(marker) >= open_len and not rest:
            open_char = ""
            open_len = 0
            open_header = ""
    return state


def _ends_inside_non_tool_fence(text: str) -> bool:
    lines = (text or "").replace("\r\n", "\n").split("\n")
    if not lines:
        return False
    # Check active fence right before the final line (so trailing partial fence line isn't treated as closing inside prose)
    active = _fence_state_per_line(lines)[-1]
    if not active:
        return False
    info = active.lstrip("`~ \t").strip().lower()
    return not info.startswith(("tool", "function", "json"))


def _has_active_tool_fence(text: str) -> bool:
    if not text:
        return False
    if _LIVE_TOOL_FENCE_RE.search(text):
        return True
    if not _ends_inside_non_tool_fence(text) and _STREAM_PARTIAL_TOOL_FENCE_RE.search(text):
        return True
    for m in _GENERIC_JSON_FENCE_RE.finditer(text):
        full = m.group(0)
        ticks = m.group("ticks")
        closed = bool(re.search(rf"{re.escape(ticks)}(?:[\u200b\u200c\u200d\ufeff \t]*_\])?[ \t]*(?:\r?\n|$)", full[m.start("body") - m.start():]))
        if _looks_like_tool_json_body(m.group("body"), unclosed=not closed):
            return True
    for bm in re.finditer(r"(?:^|(?<=\n))[ \t]*(?=[\{\[])", text):
        if _TOOL_JSON_HEAD_RE.match(text[bm.end():]):
            return True
    return False


def _clamp_live_text(renderable):
    """Keep every Live line strictly within console width to prevent ConPTY scrollback spam."""
    from rich.text import Text as _RichText

    if not isinstance(renderable, _RichText):
        return renderable
    width = max(24, (console.size.width or 80) - 4)
    lines = renderable.split("\n", include_separator=False)
    clamped = []
    for ln in lines[:4]:
        if len(ln) > width:
            ln.truncate(width, overflow="ellipsis", pad=False)
        ln.no_wrap = True
        ln.overflow = "ellipsis"
        clamped.append(ln)
    if not clamped:
        return _RichText("")
    return _RichText("\n").join(clamped)


def _clip_live_preview_rows(preview: str, max_rows: Optional[int] = None) -> str:
    """Keep trailing Markdown preview within viewport rows without breaking open code fences."""
    if not preview:
        return ""
    term_h = console.size.height or 24
    term_w = max(24, (console.size.width or 80) - 6)
    row_budget = max_rows if max_rows is not None else max(8, min(20, term_h - 8))
    lines = preview.splitlines()
    fence_before = _fence_state_per_line(lines)
    kept_indices = []
    used_rows = 0
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        rows = max(1, (len(line) + term_w - 1) // term_w)
        # Reserve room for ellipsis / fence reopen header if truncating top lines
        overhead = (2 if fence_before[idx] else 1) if idx > 0 else 0
        if kept_indices and (used_rows + rows + overhead) > row_budget:
            break
        kept_indices.append(idx)
        used_rows += rows
        if used_rows >= row_budget:
            break
    if not kept_indices:
        last_line = lines[-1]
        return last_line[-(term_w * row_budget):]
    kept_indices.reverse()
    start_idx = kept_indices[0]
    tail = "\n".join(lines[start_idx:])
    if start_idx > 0:
        active_fence = fence_before[start_idx]
        if active_fence:
            return f"…\n{active_fence}\n{tail}"
        return "…\n" + tail
    return tail


def _render_live_markdown_bounded(preview: str, reserve_rows: int = 4) -> tuple[str, Padding]:
    """Slice and measure Markdown preview so actual wrapped Rich output stays inside terminal height."""
    term_h = console.size.height or 24
    term_w = max(24, console.size.width or 80)
    max_render_rows = max(3, term_h - reserve_rows)
    budget = max(4, min(20, max_render_rows))
    opts = console.options.update(width=term_w)
    clipped = _clip_live_preview_rows(preview, max_rows=budget)
    candidate = Padding(_CleanMarkdown(clipped, code_theme=Theme.CODE_THEME), (0, 2))
    for _ in range(4):
        try:
            rendered = console.render_lines(candidate, opts, pad=False, new_lines=False)
        except Exception:
            break
        n_rows = len(rendered)
        if n_rows <= max_render_rows or budget <= 3:
            break
        next_budget = max(3, int(budget * max_render_rows / max(1, n_rows)) - 1)
        if next_budget >= budget:
            next_budget = max(3, budget - 2)
        budget = next_budget
        clipped = _clip_live_preview_rows(preview, max_rows=budget)
        candidate = Padding(_CleanMarkdown(clipped, code_theme=Theme.CODE_THEME), (0, 2))
    return clipped, candidate


Markdown = _CleanMarkdown


def friendly_model_name(model: str) -> str:
    """Return a clean human-readable Web Bridge label for UI chrome."""
    m = (model or "").strip().lower()
    if m.startswith("claude"):
        return "Claude Web"
    if m.startswith(("venice", "vernice")):
        return "Venice Web"
    if m.startswith("deepseek"):
        return "DeepSeek Web"
    if m.startswith("chatgpt") or m.startswith("openai") or m.startswith("gpt"):
        return "ChatGPT Web"
    return "Gemini Web"


def _short_path(path_obj: Path | str) -> str:
    raw = str(path_obj or ".")
    try:
        p = Path(raw).resolve()
        home = Path.home().resolve()
        if p == home:
            return "~"
        try:
            rel = p.relative_to(home)
            return "~/" + rel.as_posix()
        except ValueError:
            return p.as_posix() if os.name != "nt" else str(p)
    except Exception:
        return raw


_MARKDOWN_FENCE_OR_CODE_RE = re.compile(
    r"(?P<fence>(?:^|\n)[ \t]*(?P<fticks>`{3,}|~{3,})[^\n]*(?:\n[\s\S]*?(?:\n[ \t]*(?P=fticks)[ \t]*(?=\n|$)|$)))"
    r"|(?P<inline>`[^`\r\n]+`|``(?:(?!``)(?!\r?\n\r?\n)[\s\S])+?``)",
)


def _map_outside_code(text: str, transform) -> str:
    """Apply `transform` only to segments outside Markdown fenced code blocks and inline code spans."""
    if not text:
        return ""
    parts = []
    pos = 0
    for m in _MARKDOWN_FENCE_OR_CODE_RE.finditer(text):
        if m.start() > pos:
            parts.append(transform(text[pos:m.start()]))
        parts.append(m.group(0))
        pos = m.end()
    if pos < len(text):
        parts.append(transform(text[pos:]))
    return "".join(parts)


def _strip_xml_tags_outside_code(text: str, streaming: bool = False) -> str:
    """Strip internal <think>/<tool_call> blocks only outside Markdown code spans and fences."""
    if not text or "<" not in text:
        return text or ""

    def _clean_plain_segment(seg: str) -> str:
        if not seg or "<" not in seg:
            return seg
        out = _THINK_TAG_RE.sub("", seg)
        out = _TOOL_CALL_TAG_RE.sub("", out)
        out = _ALT_TOOL_TAG_RE.sub("", out)
        if streaming:
            out = _STREAM_UNCLOSED_XML_RE.sub("", out)
        return out

    return _map_outside_code(text, _clean_plain_segment)


def _strip_internal_tags(text: str, streaming: bool = False) -> str:
    """Strip internal XML/protocol tool-call and think tags before rendering Markdown."""
    if not text:
        return ""
    cleaned = _clean_aborted_tool_prose(text)
    cleaned = _strip_xml_tags_outside_code(cleaned, streaming=streaming)
    cleaned = _LIVE_TOOL_FENCE_RE.sub("", cleaned)
    if streaming and not _ends_inside_non_tool_fence(cleaned):
        cleaned = _STREAM_PARTIAL_TOOL_FENCE_RE.sub("", cleaned)
    cleaned = _map_outside_code(cleaned, lambda seg: _GLUED_TOOL_HEADER_BEFORE_JSON_RE.sub("", seg))
    cleaned = _strip_leaked_tool_json(cleaned, streaming=streaming)
    cleaned = _map_outside_code(cleaned, lambda seg: _ORPHAN_TOOL_HEADER_LINE_RE.sub("", seg))
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if re.match(r"^[ \t]*(`{1,3}|~{1,3})[ \t]*$", cleaned.strip()):
        return ""
    return cleaned.strip()


def _clean_thinking_lines(raw: str) -> list[str]:
    """Normalize raw reasoning text into deduplicated, chrome-free thought steps."""
    text = sanitize_surrogates(raw or "").replace("\r\n", "\n")
    text = _GLUED_THINK_CHROME_RE.sub("\n", text)
    lines: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        expanded = _REPEAT_THINK_PHRASE_RE.sub(r"\1\n", raw_line)
        for sub_line in expanded.splitlines():
            item = re.sub(r"\s+", " ", sub_line).strip().strip("*`>-• ")
            if not item or any(p.match(item) for p in _THINK_NOISE_PATTERNS):
                continue
            item = _INLINE_THINK_SUFFIX_RE.sub("", item).strip()
            if not item or any(p.match(item) for p in _THINK_NOISE_PATTERNS):
                continue
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            lines.append(item)
    return lines


def _get_windows_console_font() -> str:
    """Return active Windows console font family name lowercased, or empty string."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class _COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class _CONSOLE_FONT_INFOEX(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.ULONG),
                ("nFont", wintypes.DWORD),
                ("dwFontSize", _COORD),
                ("FontFamily", ctypes.c_uint),
                ("FontWeight", ctypes.c_uint),
                ("FaceName", ctypes.c_wchar * 32),
            ]

        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if not h or h == -1:
            return ""
        info = _CONSOLE_FONT_INFOEX()
        info.cbSize = ctypes.sizeof(_CONSOLE_FONT_INFOEX)
        if k32.GetCurrentConsoleFontEx(h, False, ctypes.byref(info)):
            return str(info.FaceName or "").strip().lower()
    except Exception:
        pass
    return ""


def is_legacy_windows_console() -> bool:
    """True when running interactively in legacy Windows conhost with limited glyph coverage."""
    if sys.platform != "win32":
        return False
    try:
        if not (sys.stdout and hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
            return False
    except Exception:
        return False
    if any(os.environ.get(k) for k in ("WT_SESSION", "TERM_PROGRAM", "ConEmuPID", "ANSICON")):
        return False
    font = _get_windows_console_font()
    modern_fonts = ("cascadia", "jetbrains", "fira", "hack", "nerd", "meslo", "iosevka", "sarasa")
    if any(m in font for m in modern_fonts):
        return False
    return True


_LEGACY_GLYPHS = {
    "chevron": "›",
    "mode_bypass": "»",
    "mode_single": "›",
    "thought": "·",
    "branch": "└",
    "ok": "✓",
    "err": "×",
    "bullet": "●",
}

_MODERN_GLYPHS = {
    "chevron": "❯",
    "mode_bypass": "▸▸",
    "mode_single": "▸",
    "thought": "∴",
    "branch": "⎿",
    "ok": "✓",
    "err": "✗",
    "bullet": "●",
}


def ui_glyph(name: str) -> str:
    """Return terminal-safe UI glyph (avoiding missing-glyph tofu boxes on Windows conhost)."""
    if is_legacy_windows_console():
        if name in ("ok", "bullet") and "lucida" in _get_windows_console_font():
            return "+" if name == "ok" else "*"
        return _LEGACY_GLYPHS.get(name, _MODERN_GLYPHS.get(name, ""))
    return _MODERN_GLYPHS.get(name, "")


def ui_frame_width() -> int:
    """Unified responsive horizontal frame width shared by welcome card, rules, and toolbar."""
    try:
        term_w = getattr(console.size, "width", 92) or 92
    except Exception:
        term_w = 92
    return max(12, min(term_w - 4, 120))


def _format_duration(elapsed: Optional[float]) -> str:
    if elapsed is None or elapsed < 1.0:
        return ""
    return f" ({elapsed:.1f}s)"


# ─── Claude Code Startup Card & Shortcuts ─────────────────────────────────────

def _get_version() -> str:
    try:
        from . import __version__
        return str(__version__)
    except Exception:
        return "2.0.0"


def _format_display_version(ver: str) -> str:
    parts = (ver or "").split(".")
    if len(parts) == 3 and parts[2] == "0":
        return f"{parts[0]}.{parts[1]}"
    return ver


def _recent_activity_lines(state) -> list[tuple[str, str]]:
    """Build concise recent activity summary for the welcome card."""
    sync_theme()
    lines: list[tuple[str, str]] = []
    try:
        sessions = state.list_saved_sessions() if hasattr(state, "list_saved_sessions") else []
    except Exception:
        sessions = []

    project_str = str(getattr(state, "project_root", ""))
    proj_matches = [s for s in sessions if s.get("project") == project_str]
    total_n = len(sessions)
    sess_word = "session" if total_n == 1 else "sessions"
    if proj_matches:
        latest = proj_matches[0]
        turns = latest.get("turns", 0)
        turn_word = "turn" if turns == 1 else "turns"
        lines.append((
            f"Last session · {turns} {turn_word} ({latest.get('saved_at', '?')})",
            Theme.SECONDARY,
        ))
        lines.append((f"{total_n} saved {sess_word} · /load to resume", Theme.DIM))
    elif total_n:
        lines.append((f"{total_n} saved {sess_word} across projects", Theme.SECONDARY))
        lines.append(("New session in this workspace", Theme.DIM))
    else:
        lines.append(("No saved sessions", Theme.DIM))
        lines.append(("Enter a task below to start", Theme.DIM))
    return lines


def print_welcome(state, server_ok: bool = True) -> None:
    """Render quiet architectural startup card tailored for wsa."""
    sync_theme()
    ver = _format_display_version(_get_version())
    model_id = getattr(state, "model", "gemini")
    info = get_model_info(model_id, state=state)
    model_name = info.get("name") or friendly_model_name(model_id)
    web_badge = str(getattr(state, "active_web_model", "") or "").strip()
    if web_badge and web_badge != info.get("name"):
        model_display = f"{model_name} ({web_badge})"
    else:
        model_display = model_name
    transport = info.get("transport") or info.get("site") or "Web Bridge"
    proj_raw = getattr(state, "project_root", ".")
    proj_name = getattr(state, "project_name", "") or ""
    if not proj_name or proj_name == "workspace":
        try:
            proj_name = Path(proj_raw).resolve().name or "workspace"
        except Exception:
            proj_name = proj_name or "workspace"
    proj_path = _short_path(proj_raw)
    proj_type = getattr(state, "project_type", "Workspace")
    acct_lbl = (getattr(state, "active_account_label", "") or "").strip()
    acct_email = (getattr(state, "active_account_email", "") or "").strip()
    acct_desc = f"{acct_lbl} · {acct_email}" if (acct_lbl and acct_email) else (acct_lbl or acct_email)
    width = getattr(console.size, "width", 92) or 92
    panel_w = ui_frame_width()

    title = f"[bold {Theme.ACCENT}]wsa[/] [{Theme.DIM}]v{escape(ver)} · {escape(proj_name)}[/]"
    subtitle = f"[{Theme.DIM}]Engineered by Divyansh Saraf[/]"

    # Left column: product identity mark + active bridge, transport, account, workspace path
    left = Text()
    left.append("\n")
    left.append("    ▐▛███▜▌\n", style=Theme.ACCENT)
    left.append("   ▝▜█████▛▘\n", style=Theme.SHIMMER)
    left.append("     ▘▘ ▝▝\n\n", style=Theme.ACCENT)
    left.append(f"{model_display}", style=f"bold {Theme.PRIMARY}")
    if getattr(state, "agent_mode", "singleagent") == "multiagent" and getattr(state, "multiagent_queue", None):
        q_list = [str(x).strip() for x in state.multiagent_queue if str(x).strip()]
        if q_list:
            q_str = " → ".join(q_list)
            if len(q_str) <= 22:
                left.append(f"\nmulti · {q_str}", style=Theme.ACCENT)
            else:
                left.append(f"\nmulti · {len(q_list)} sites · {q_list[0]}", style=Theme.ACCENT)
    left.append(f"\n{transport}\n", style=Theme.SECONDARY)
    if acct_lbl:
        left.append(f"{acct_lbl}\n", style=Theme.SECONDARY)
    if acct_email:
        left.append(f"{acct_email}\n", style=Theme.SECONDARY)
    disp_path = proj_path if len(proj_path) <= 34 else ("…/" + proj_path.rsplit("/", 1)[-1])
    left.append(disp_path, style=Theme.DIM)
    if proj_type and proj_type != "Unknown":
        left.append(f"\n{proj_type}", style=Theme.DIM)

    bullet = ui_glyph("bullet")

    # Compact stacked layout on narrow terminals (< 78 cols)
    if width < 96:
        status_text = str(getattr(state, "_ui_connection", "")) or (
            "Server ready" if server_ok else "Server offline (run wsa.ps1)"
        )
        status_style = Theme.OK if server_ok else Theme.ERR
        compact = Text()
        compact.append_text(left)
        compact.append("\n\n")
        compact.append(f"{bullet} ", style=status_style)
        compact.append(f"{status_text}\n", style=Theme.SECONDARY)
        compact.append("/models  /account  /singleagent  /multiagent  /tools  ? shortcuts", style=Theme.DIM)
        card = Panel(
            compact,
            title=title,
            title_align="left",
            subtitle=subtitle,
            subtitle_align="right",
            box=box.ROUNDED,
            border_style=Theme.WELCOME_BORDER,
            padding=(0, 2),
            width=panel_w,
        )
        console.print()
        console.print(Padding(card, (0, 2)))
        console.print()
        return

    # Wide two-column architectural layout (subtract 16 cols of panel/table padding+borders)
    usable_w = max(50, panel_w - 16)
    left_w = max(24, min(34, usable_w * 36 // 100))
    right_w = max(36, usable_w - left_w)

    cmd_items = (
        ("/models", "claude | deepseek | gemini | chatgpt | venice"),
        ("/account", "Sync Chrome/Brave & live site tabs"),
        ("/singleagent", "Pin one site (default mode)"),
        ("/multiagent", "Queue 5 sites w/ limit failover"),
        ("/tools", "Workflows, browser & shell tools"),
        ("/team /theme", "Subagents · palettes · ? shortcuts"),
    ) if right_w >= 54 else (
        ("/models", "5-site web bridge picker"),
        ("/account", "Sync browser & open tabs"),
        ("/singleagent", "One site (default mode)"),
        ("/multiagent", "5-site limit failover"),
        ("/tools", "All workflows & local tools"),
        ("/theme", "Palettes · ? shortcuts"),
    )

    right = Text()
    right.append("Commands\n", style=f"bold {Theme.PRIMARY}")
    for cmd, desc in cmd_items:
        right.append(f"  {cmd:<13}", style=Theme.ACCENT)
        right.append(f"{desc}\n", style=Theme.SECONDARY)

    right.append("\n")
    right.append("Session\n", style=f"bold {Theme.PRIMARY}")
    for msg, style in _recent_activity_lines(state):
        shown_msg = msg if len(msg) <= (right_w - 3) else (msg[: right_w - 4].rstrip() + "…")
        right.append(f"  {shown_msg}\n", style=style)

    conn_tag = str(getattr(state, "_ui_connection", "")) or "Server ready"
    full_conn = f"{conn_tag} · {len(MODEL_CATALOG)} bridges"
    shown_conn = (
        full_conn if len(full_conn) + 4 <= right_w
        else (conn_tag if len(conn_tag) + 4 <= right_w else conn_tag[: max(16, right_w - 5)].rstrip() + "…")
    )
    if server_ok:
        right.append(f"  {bullet} ", style=Theme.OK)
        right.append(shown_conn, style=Theme.DIM)
    else:
        right.append(f"  {bullet} ", style=Theme.ERR)
        right.append("Server offline · run wsa.ps1", style=Theme.WARN)

    split_border = "#52525b" if Theme.BORDER == "#3f3f46" else Theme.BORDER
    grid = Table(
        box=_WELCOME_SPLIT_BOX,
        show_header=False,
        show_edge=False,
        pad_edge=False,
        border_style=split_border,
        padding=(0, 2),
        expand=True,
    )
    grid.add_column(width=left_w, vertical="top")
    grid.add_column(width=right_w, vertical="top")
    grid.add_row(left, right)

    card = Panel(
        grid,
        title=title,
        title_align="left",
        subtitle=subtitle,
        subtitle_align="right",
        box=box.ROUNDED,
        border_style=Theme.WELCOME_BORDER,
        padding=(1, 2),
        width=panel_w,
    )
    console.print()
    console.print(Padding(card, (0, 2)))
    console.print()


def print_splash() -> None:
    """Minimal fallback title line for callers expecting print_splash()."""
    sync_theme()
    ver = _format_display_version(_get_version())
    console.print()
    console.print(f"  [bold {Theme.ACCENT}]●[/] [bold {Theme.PRIMARY}]wsa[/] [{Theme.DIM}]v{escape(ver)}[/]")


def print_quick_reference() -> None:
    """Minimal one-line shortcut hint for callers expecting print_quick_reference()."""
    sync_theme()
    console.print(f"  [{Theme.DIM}]/help commands  ·  /models  ·  /account  ·  /singleagent  ·  /multiagent  ·  /tools[/]")


def print_shortcuts() -> None:
    """Render keyboard shortcuts reference cleanly."""
    sync_theme()
    console.print()
    console.print(Rule(title=f"[bold {Theme.PRIMARY}]Keyboard Shortcuts[/]", align="left", style=Theme.BORDER))
    table = Table(box=None, show_header=False, padding=(0, 3, 0, 2), expand=False)
    table.add_column("Key", style=f"bold {Theme.ACCENT}", no_wrap=True)
    table.add_column("Action", style=Theme.SECONDARY)

    bindings = [
        ("Shift+Tab", "Cycle permission mode (bypass → accept edits → strict)"),
        ("?", "Toggle shortcuts reference on empty prompt"),
        ("Alt+M", "Open Web Bridge selector (claude | deepseek | gemini | chatgpt | venice)"),
        ("Tab", "Autocomplete /commands, /model, /multiagent, /account, and @files"),
        ("Ctrl+C", "Interrupt active turn cleanly"),
        ("Ctrl+D", "Save session and exit"),
        ("↑ / ↓", "Browse command and prompt history"),
        ("/theme", "Switch palette (Dark / Colorblind / GitHub / Dracula)"),
    ]
    for k, desc in bindings:
        table.add_row(k, desc)

    console.print(table)
    console.print(Rule(style=Theme.BORDER))
    console.print()


# ─── Conversation & Streaming ─────────────────────────────────────────────────

def print_user_message(text: str) -> None:
    """Print user prompt cleanly with high-contrast text and warm accent chevron."""
    sync_theme()
    clean = sanitize_surrogates(text or "").rstrip()
    lines = clean.splitlines() or [""]
    console.print()
    console.print(f"  [bold {Theme.ACCENT}]{ui_glyph('chevron')}[/] [bold #f4f4f6]{escape(lines[0])}[/]")
    for extra in lines[1:]:
        console.print(f"  [{Theme.BORDER}]│[/] [#f4f4f6]{escape(extra)}[/]")


def begin_agent_response() -> None:
    """Provide clean vertical separation before an assistant response."""
    console.print()


def print_agent_response(text: str) -> None:
    """Render a completed assistant Markdown response cleanly."""
    sync_theme()
    clean = _strip_internal_tags(sanitize_surrogates(text or "")).lstrip("\n")
    if not clean:
        return
    console.print(Padding(_CleanMarkdown(clean, code_theme=Theme.CODE_THEME), (0, 2)))


_LIVE_LOCK = threading.RLock()
_ACTIVE_LIVE_RENDERERS: list[weakref.ref["StreamRenderer"]] = []


def _register_active_renderer(renderer_obj: "StreamRenderer") -> None:
    alive: list[weakref.ref[StreamRenderer]] = []
    seen = False
    for ref in _ACTIVE_LIVE_RENDERERS:
        obj = ref()
        if obj is None:
            continue
        if obj is renderer_obj:
            seen = True
        alive.append(ref)
    if not seen:
        alive.append(weakref.ref(renderer_obj))
    _ACTIVE_LIVE_RENDERERS[:] = alive


def _unregister_active_renderer(renderer_obj: "StreamRenderer") -> None:
    _ACTIVE_LIVE_RENDERERS[:] = [
        ref
        for ref in _ACTIVE_LIVE_RENDERERS
        if (obj := ref()) is not None and obj is not renderer_obj
    ]


def _release_console_live(target_console: object, exclude: Optional["StreamRenderer"] = None) -> None:
    """Stop any orphaned or prior active Live display attached to target_console."""
    if target_console is None:
        return
    for ref in list(_ACTIVE_LIVE_RENDERERS):
        owner = ref()
        if owner is None or owner is exclude:
            continue
        if owner._live is not None and (owner._live_console is None or owner._live_console is target_console):
            owner._stop_live()

    # Rich 15+: Console stores active displays on _live_stack (LIFO).
    live_stack = getattr(target_console, "_live_stack", None)
    if isinstance(live_stack, list):
        while live_stack:
            top = live_stack[-1]
            before_len = len(live_stack)
            try:
                top.stop()
            except Exception:
                pass
            if len(live_stack) >= before_len:
                try:
                    target_console.clear_live()
                except Exception:
                    break
        return

    # Rich <=14: Console stores a single _live reference and raises LiveError if occupied.
    stray = getattr(target_console, "_live", None)
    if stray is not None:
        try:
            stray.stop()
        except Exception:
            pass
        if getattr(target_console, "_live", None) is not None:
            try:
                target_console.clear_live()
            except Exception:
                pass


class _DynamicLiveRenderable:
    """Dynamic Rich renderable proxy that re-evaluates on every Live refresh tick."""

    def __init__(self, owner: "StreamRenderer"):
        self._owner = owner

    def __rich__(self):
        return self._owner._build_live_renderable()


class StreamRenderer:
    """Claude Code-style live streaming renderer with shimmer spinner & quiet thinking collapse."""

    _SPINNER_FRAMES = ("·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢")
    _SPINNER_VERBS = (
        "Thinking…",
        "Analyzing…",
        "Planning…",
        "Reading…",
        "Synthesizing…",
        "Evaluating…",
        "Inspecting…",
        "Reasoning…",
    )
    _RENDER_INTERVAL = 0.05  # Adaptive frame batching interval (~20fps max Markdown re-parse)

    def __init__(
        self,
        round_num: int = 0,
        status_hint: str = "",
        model: str = "",
        stage_label: str = "",
        flush_final: bool = True,
        clock: Optional[Callable[[], float]] = None,
    ):
        self._thinking_buf: str = ""
        self._answer_buf: str = ""
        self._phase: str = "thinking"
        self._live: Optional[Live] = None
        self._live_console: Optional[Console] = None
        self._live_proxy = _DynamicLiveRenderable(self)
        self._start_time: Optional[float] = None
        self._thinking_start: Optional[float] = None
        self._in_thinking: bool = False
        self._answered: bool = False
        self._printed_thinking_summary: bool = False
        self._frame_idx: int = 0
        self._last_preview: str = ""
        self._last_raw_preview: str = ""
        self._cached_markdown: Optional[Padding] = None
        self._last_md_render_ts: float = 0.0
        self._last_live_refresh_ts: float = 0.0
        self._cached_think_len: int = -1
        self._cached_think_words: int = 0
        self._cached_think_steps: list[str] = []
        self._cached_ans_len: int = -1
        self._cached_ans_words: int = 0
        self._cached_has_tool_fence: bool = False
        self._pending_preview_dirty: bool = False
        self._round_num: int = max(0, int(round_num or 0))
        self._status_hint: str = sanitize_surrogates(status_hint or "").strip()
        self._stage_clock: Callable[[], float] = clock or time.monotonic
        self._status_hint_with_elapsed: bool = False
        self._status_hint_started_at: Optional[float] = None
        self._model: str = (model or "").strip().lower()
        self._stage_label: str = sanitize_surrogates(stage_label or "").strip()
        self._flush_final: bool = bool(flush_final)

    def set_status_hint(
        self,
        hint: str,
        *,
        with_elapsed: bool = False,
        reset_stage_clock: bool = False,
    ) -> None:
        """Update contextual status hint shown beneath the live thinking spinner."""
        clean_hint = sanitize_surrogates(hint or "").strip()
        self._status_hint = clean_hint
        self._status_hint_with_elapsed = bool(with_elapsed and clean_hint)
        if self._status_hint_with_elapsed:
            if reset_stage_clock or self._status_hint_started_at is None:
                self._status_hint_started_at = self._stage_clock()
        else:
            self._status_hint_started_at = None
        self._throttled_live_refresh(force=True)

    def _get_thinking_stats(self) -> tuple[int, list[str]]:
        buf = self._thinking_buf
        if not buf:
            return 0, []
        blen = len(buf)
        if blen != self._cached_think_len:
            tail = buf[-12_000:] if blen > 12_000 else buf
            self._cached_think_words = len(buf.split())
            self._cached_think_steps = _clean_thinking_lines(tail)
            self._cached_think_len = blen
        return self._cached_think_words, self._cached_think_steps

    def _get_answer_stats(self) -> tuple[int, bool]:
        buf = self._answer_buf
        if not buf:
            return 0, False
        blen = len(buf)
        if blen != self._cached_ans_len:
            self._cached_ans_words = len(buf.split())
            self._cached_has_tool_fence = _has_active_tool_fence(buf)
            self._cached_ans_len = blen
        return self._cached_ans_words, self._cached_has_tool_fence

    def _refresh_answer_preview(self, now: float, *, stop_if_empty: bool = True) -> bool:
        buf = self._answer_buf
        preview = _strip_internal_tags(buf, streaming=True).lstrip("\n")
        self._last_md_render_ts = now
        self._pending_preview_dirty = False
        if not preview:
            _, has_fence = self._get_answer_stats()
            if has_fence:
                return True
            if stop_if_empty and self._live is not None and not self._in_thinking:
                self._stop_live()
            return False
        if preview != self._last_raw_preview or self._cached_markdown is None:
            clipped, bounded = _render_live_markdown_bounded(preview)
            self._last_raw_preview = preview
            self._last_preview = clipped
            self._cached_markdown = bounded
        return True

    def _thinking_renderable(self) -> Text:
        frame_pos = self._frame_idx
        self._frame_idx += 1
        frame = self._SPINNER_FRAMES[frame_pos % len(self._SPINNER_FRAMES)]
        now = time.time()
        elapsed = max(0.0, now - (self._thinking_start or now))
        think_words, steps = self._get_thinking_stats()
        verb_idx = (int(elapsed // 2.0) + (think_words // 18)) % len(self._SPINNER_VERBS)
        verb = self._SPINNER_VERBS[verb_idx]
        pulse_style = Theme.SHIMMER if (frame_pos % 2 == 1) else Theme.ACCENT

        t = Text()
        t.append("  ")
        t.append(f"{frame} ", style=f"bold {pulse_style}")
        t.append(verb, style=f"bold {pulse_style}" if (frame_pos % 4 == 0) else pulse_style)

        meta_parts = []
        if self._stage_label:
            meta_parts.append(self._stage_label)
        if elapsed >= 0.2:
            meta_parts.append(f"{elapsed:.1f}s")
        if self._round_num > 0:
            meta_parts.append(f"step {self._round_num + 1}")
        if think_words > 0:
            meta_parts.append(f"{think_words}w thought")
        meta_parts.append("ctrl+c to interrupt")
        t.append(f" ({' · '.join(meta_parts)})", style=Theme.DIM)

        line_cap = max(20, (console.size.width or 80) - 12)
        branch = ui_glyph("branch")
        if steps:
            prior = steps[:-1][-2:] if len(steps) > 1 else []
            for p_step in prior:
                shown_p = p_step if len(p_step) <= line_cap else p_step[: line_cap - 1].rstrip() + "…"
                t.append("\n")
                t.append("    ·  ", style=Theme.DIM)
                t.append(shown_p, style=Theme.DIM)
            active = steps[-1]
            shown_a = active if len(active) <= line_cap else active[: line_cap - 1].rstrip() + "…"
            t.append("\n")
            t.append(f"    {branch}  ", style=Theme.DIM)
            t.append(shown_a, style=Theme.SECONDARY)
        elif self._status_hint:
            hint = self._status_hint
            suffix = ""
            hint_style = Theme.DIM
            if self._status_hint_with_elapsed:
                hint_style = Theme.SECONDARY
                now_stage = self._stage_clock()
                stage_elapsed = max(
                    0.0,
                    now_stage - (self._status_hint_started_at if self._status_hint_started_at is not None else now_stage),
                )
                if stage_elapsed >= 0.2:
                    suffix = f" · {format_live_elapsed(stage_elapsed)}"
            if suffix:
                prefix_cap = max(8, line_cap - len(suffix))
                prefix = hint if len(hint) <= prefix_cap else hint[: prefix_cap - 1].rstrip() + "…"
                shown_h = prefix + suffix
            else:
                shown_h = hint if len(hint) <= line_cap else hint[: line_cap - 1].rstrip() + "…"
            t.append("\n")
            t.append(f"    {branch}  ", style=Theme.DIM)
            t.append(shown_h, style=hint_style)
        return t

    def _streaming_footer_renderable(self, crafting_tool: bool = False, ans_words: Optional[int] = None) -> Text:
        frame_pos = self._frame_idx
        self._frame_idx += 1
        frame = self._SPINNER_FRAMES[frame_pos % len(self._SPINNER_FRAMES)]
        now = time.time()
        elapsed = max(0.0, now - (self._start_time or now))
        if ans_words is None:
            ans_words, _ = self._get_answer_stats()
        pulse_style = Theme.SHIMMER if (frame_pos % 2 == 1) else Theme.ACCENT
        label = "Crafting tool call…" if crafting_tool else "Streaming…"

        t = Text()
        t.append("  ")
        t.append(f"{frame} ", style=f"bold {pulse_style}")
        t.append(label, style=pulse_style)
        meta = []
        if self._stage_label:
            meta.append(self._stage_label)
        meta.append(f"{elapsed:.1f}s")
        if ans_words > 0:
            meta.append(f"{ans_words}w")
        if self._round_num > 0:
            meta.append(f"step {self._round_num + 1}")
        meta.append("ctrl+c to interrupt")
        t.append(f" ({' · '.join(meta)})", style=Theme.DIM)
        return t

    def _build_live_renderable(self):
        if self._in_thinking or not self._answered:
            return _clamp_live_text(self._thinking_renderable())
        if self._pending_preview_dirty:
            now = time.time()
            if (now - self._last_md_render_ts) >= self._RENDER_INTERVAL:
                self._refresh_answer_preview(now, stop_if_empty=False)
        ans_words, crafting_tool = self._get_answer_stats()
        footer = _clamp_live_text(
            self._streaming_footer_renderable(crafting_tool=crafting_tool, ans_words=ans_words)
        )
        if self._cached_markdown is not None:
            return Group(self._cached_markdown, footer)
        return footer

    def _throttled_live_refresh(self, force: bool = False) -> None:
        with _LIVE_LOCK:
            live = self._live
            if live is None:
                return
            now = time.time()
            if not force and (now - self._last_live_refresh_ts) < self._RENDER_INTERVAL:
                return
            self._last_live_refresh_ts = now
            try:
                live.refresh()
            except Exception:
                pass

    def _ensure_live(self, renderable=None) -> None:
        with _LIVE_LOCK:
            target = self._live_proxy if renderable is None else renderable
            target_console = console
            if self._live is not None and self._live_console is not target_console:
                self._stop_live()
            if self._live is None:
                _release_console_live(target_console, exclude=self)
                live = Live(
                    target,
                    console=target_console,
                    refresh_per_second=12,
                    transient=True,
                    vertical_overflow="ellipsis",
                )
                try:
                    live.start()
                except Exception:
                    try:
                        live.stop()
                    except Exception:
                        pass
                    raise
                self._live = live
                self._live_console = target_console
                _register_active_renderer(self)
            elif target is not self._live.renderable:
                self._live.update(target)

    def _stop_live(self) -> None:
        with _LIVE_LOCK:
            live = self._live
            self._live = None
            self._live_console = None
            _unregister_active_renderer(self)
            if live is not None:
                try:
                    live.stop()
                except Exception:
                    pass

    def close(self) -> None:
        """Release any active Rich Live display without flushing final markdown."""
        self._stop_live()

    def __enter__(self) -> "StreamRenderer":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def start_thinking(self) -> None:
        sync_theme()
        if self._thinking_start is None:
            self._thinking_start = time.time()
        if self._start_time is None:
            self._start_time = self._thinking_start
        self._phase = "thinking"
        self._in_thinking = True
        self._ensure_live()

    def feed_thinking(self, token: str) -> None:
        clean = sanitize_surrogates(token or "")
        if not clean:
            return
        if not self._in_thinking and not self._answered:
            self.start_thinking()
        self._thinking_buf += clean
        if self._in_thinking and not self._answered:
            if self._live is None or self._live_console is not console:
                self._ensure_live()
                self._throttled_live_refresh()
            elif (time.time() - self._last_live_refresh_ts) >= self._RENDER_INTERVAL:
                self._throttled_live_refresh()

    def _print_thinking_summary(self, elapsed: float) -> None:
        if self._printed_thinking_summary:
            return
        raw_think = sanitize_surrogates(self._thinking_buf).strip()
        if not raw_think:
            return
        self._printed_thinking_summary = True
        steps = _clean_thinking_lines(raw_think)
        secs = max(0.0, float(elapsed))
        thought_g = ui_glyph("thought")
        stage_suffix = f" · {escape(self._stage_label)}" if self._stage_label else ""
        if not steps:
            console.print(f"  [{Theme.DIM}]{thought_g} Thought {secs:.1f}s{stage_suffix}[/]")
            return

        width = max(32, (console.size.width or 80) - 8)
        if len(steps) == 1 and len(steps[0]) <= max(24, width - 22):
            console.print(f"  [{Theme.DIM}]{thought_g} Thought {secs:.1f}s{stage_suffix} · {escape(steps[0])}[/]")
            return

        summary = Text()
        summary.append(f"  {thought_g} Thought {secs:.1f}s", style=Theme.DIM)
        if self._stage_label:
            summary.append(f" · {self._stage_label}", style=Theme.DIM)
        if len(steps) > 1:
            summary.append(f" · {len(steps)} steps", style=Theme.DIM)

        trace_lines: list[tuple[str, bool]] = []
        for idx, st in enumerate(steps):
            is_last = idx == len(steps) - 1
            wrapped_parts = textwrap.wrap(
                st,
                width=width,
                break_long_words=True,
                break_on_hyphens=False,
            ) or [st]
            for wrapped in wrapped_parts:
                trace_lines.append((wrapped, is_last))

        max_trace = 10
        omitted = max(0, len(trace_lines) - max_trace)
        visible = trace_lines[-max_trace:] if omitted else trace_lines
        if omitted:
            summary.append("\n    │ ", style=Theme.DIM)
            summary.append(f"… ({omitted} earlier lines)", style=f"italic {Theme.DIM}")
        for ln, is_last in visible:
            summary.append("\n    │ ", style=Theme.DIM)
            summary.append(ln, style=f"italic {Theme.SECONDARY if is_last else Theme.DIM}")
        console.print(summary)

    def stop_thinking(self) -> None:
        if self._in_thinking:
            self._stop_live()
            self._in_thinking = False
            elapsed = time.time() - (self._thinking_start or time.time())
            self._print_thinking_summary(elapsed)

    def feed(self, token: str) -> None:
        clean = sanitize_surrogates(token or "")
        if not clean:
            return
        now = time.time()
        if self._start_time is None:
            self._start_time = now
        if not self._answered:
            if self._in_thinking:
                self.stop_thinking()
            elif self._thinking_buf and not self._printed_thinking_summary:
                elapsed = now - (self._thinking_start or self._start_time or now)
                self._print_thinking_summary(elapsed)
            self._answered = True
            self._phase = "answering"

        self._answer_buf += clean
        if (
            self._live is not None
            and self._live_console is console
            and self._cached_markdown is not None
            and (now - self._last_md_render_ts) < self._RENDER_INTERVAL
        ):
            self._pending_preview_dirty = True
            return

        if not self._refresh_answer_preview(now, stop_if_empty=True):
            return
        if self._live is None or self._live_console is not console:
            self._ensure_live()
        if (now - self._last_live_refresh_ts) >= self._RENDER_INTERVAL:
            self._throttled_live_refresh()

    def reset_answer(self, full_retry: bool = False) -> None:
        """Clear partial streamed answer (and optionally thinking state on hard retry)."""
        self._stop_live()
        self._answer_buf = ""
        self._last_preview = ""
        self._last_raw_preview = ""
        self._cached_markdown = None
        self._last_md_render_ts = 0.0
        self._cached_ans_len = -1
        self._cached_ans_words = 0
        self._cached_has_tool_fence = False
        self._pending_preview_dirty = False
        self._answered = False
        if full_retry:
            self._thinking_buf = ""
            self._cached_think_len = -1
            self._cached_think_words = 0
            self._cached_think_steps = []
            self._phase = "thinking"
            self._in_thinking = False
            self._thinking_start = None
            self._start_time = None
            self._printed_thinking_summary = False

    def finish(self) -> None:
        self._stop_live()
        if self._in_thinking:
            self._in_thinking = False
        if self._thinking_buf and not self._printed_thinking_summary:
            elapsed = time.time() - (self._thinking_start or self._start_time or time.time())
            self._print_thinking_summary(elapsed)

        if self._flush_final:
            final_text = _strip_internal_tags(sanitize_surrogates(self._answer_buf)).lstrip("\n")
            if final_text:
                console.print(Padding(_CleanMarkdown(final_text, code_theme=Theme.CODE_THEME), (0, 2)))

    def get_full_text(self) -> str:
        return self._answer_buf

    def get_thinking_text(self) -> str:
        return self._thinking_buf


# ─── Tool Call & Tree Result Rendering ────────────────────────────────────────

_TOOL_VERBS = {
    "read_file": "Read",
    "read_files": "Read",
    "get_file_outline": "Outline",
    "get_file_info": "FileInfo",
    "inspect_image": "InspectImage",
    "browser_tabs": "BrowserTabs",
    "browser_inspect": "BrowserInspect",
    "browser_interact": "BrowserAct",
    "visual_inspect": "VisualAudit",
    "patch_file": "Update",
    "apply_patch": "Patch",
    "write_file": "Write",
    "delete_file": "Delete",
    "move_file": "Move",
    "copy_file": "Copy",
    "run_command": "Bash",
    "run_python": "Python",
    "run_tests": "Test",
    "install_dependencies": "Install",
    "install_runtime": "InstallRuntime",
    "detect_toolchain": "Toolchain",
    "git": "Git",
    "rollback": "Rollback",
    "start_background": "BgStart",
    "list_background": "BgList",
    "stop_background": "BgStop",
    "restart_service": "RestartSvc",
    "http_request": "HTTP",
    "web_fetch": "Fetch",
    "list_dir": "List",
    "glob_files": "Glob",
    "search_files": "Search",
    "spawn_subagent": "Delegate",
    "read_skill": "ReadSkill",
    "dynamic_architect": "Architect",
    "dynamic_coder": "Coder",
    "dynamic_verifier": "Verifier",
    "dynamic_explorer": "Explorer",
    "ask_user": "Consult",
    "todo": "Todo",
    "wait_for": "Wait",
}


def _format_tool_header(name: str, args: Optional[dict]) -> tuple[str, str]:
    verb = _TOOL_VERBS.get(name, name)
    a = args or {}

    if name == "read_file":
        path = str(a.get("path", ""))
        s_line, e_line = a.get("start_line"), a.get("end_line")
        if e_line is not None:
            return verb, f"{path}:L{s_line or 1}-{e_line}"
        if s_line is not None:
            return verb, f"{path} from L{s_line}"
        return verb, path

    if name == "get_file_outline":
        return verb, str(a.get("path", ""))

    if name == "read_skill":
        sname = str(a.get("name") or "catalog").strip()
        sec = str(a.get("section") or "").strip()
        mode = str(a.get("mode") or "").strip().lower()
        parts = [sname]
        if sec:
            parts.append(sec)
        elif mode == "full":
            parts.append("full")
        return verb, " · ".join(parts)

    if name == "spawn_subagent":
        rname = str(a.get("role") or "subagent")
        task = re.sub(r"\s+", " ", str(a.get("task") or "").strip())
        if len(task) > 54:
            task = task[:51].rstrip() + "…"
        return verb, f"{rname}: {task}" if task else rname

    if name == "read_files":
        paths = a.get("paths") or []
        if isinstance(paths, list):
            shown = ", ".join(str(p) for p in paths[:2])
            if len(paths) > 2:
                shown += f", +{len(paths) - 2}"
            return verb, shown
        return verb, str(paths)

    if name in ("patch_file", "write_file", "delete_file"):
        return verb, str(a.get("path", ""))

    if name == "run_command":
        if isinstance(a.get("argv"), list) and a.get("argv"):
            raw_cmd = " ".join(str(x) for x in a["argv"])
        else:
            raw_cmd = str(a.get("command", ""))
        cmd = re.sub(r"\s+", " ", raw_cmd.strip())
        if len(cmd) > 72:
            cmd = cmd[:69].rstrip() + "…"
        return verb, cmd

    if name == "run_python":
        first = next((ln.strip() for ln in str(a.get("code", "")).splitlines() if ln.strip()), "")
        if len(first) > 64:
            first = first[:61].rstrip() + "…"
        return verb, first

    if name == "run_tests":
        parts = [str(p) for p in (a.get("path"), f"-k {a['k']}" if a.get("k") else "", a.get("framework")) if p]
        return verb, " ".join(parts) if parts else "suite"

    if name == "rollback":
        act = str(a.get("action") or "rollback")
        if a.get("checkpoint_id"):
            return verb, str(a["checkpoint_id"])
        if act == "rollback":
            return verb, f"steps={a.get('steps', 1)}"
        return verb, act

    if name == "restart_service":
        port = a.get("port", "")
        return verb, f"port {port}" if port != "" else ""

    if name == "http_request":
        meth = str(a.get("method") or "GET").upper()
        url = str(a.get("url") or "")
        return verb, f"{meth} {url}".strip()

    if name == "list_dir":
        return verb, str(a.get("path") or ".")

    if name == "glob_files":
        return verb, str(a.get("pattern", "*"))

    if name == "search_files":
        pat = str(a.get("pattern", ""))
        path = str(a.get("path") or "")
        return verb, f"{pat} in {path}".strip() if path else pat

    if name == "browser_inspect":
        target = a.get("url") or (f"tab #{a['tab_id']}" if a.get("tab_id") else "active preview tab")
        return verb, str(target)

    if name == "browser_interact":
        act = str(a.get("action") or "act")
        target = str(a.get("selector") or a.get("text") or "").strip()
        return verb, f"{act} {target}".strip()

    if name in ("dynamic_architect", "dynamic_coder", "dynamic_verifier", "dynamic_explorer", "ask_user"):
        q = str(a.get("question") or "").strip()
        opts = a.get("options") or []
        opts_count = f" [{len(opts)} opts]" if isinstance(opts, list) and opts else ""
        if len(q) > 55:
            q = q[:52].rstrip() + "…"
        return verb, f"{q}{opts_count}" if q else "checkpoint"

    if name == "visual_inspect":
        vps = a.get("viewports")
        names = []
        if isinstance(vps, list):
            names = [str(v.get("name", "")) for v in vps if isinstance(v, dict) and v.get("name")]
        vp_str = f"viewports={','.join(names)}" if names else ""
        act = a.get("action")
        act_str = f"action={act}" if act else ""
        meta = " · ".join(p for p in (vp_str, act_str) if p)
        return verb, meta or "audit viewports"

    if name == "todo":
        action = str(a.get("action") or "list")
        task = str(a.get("task") or a.get("id") or "").strip()
        if len(task) > 50:
            task = task[:47].rstrip() + "…"
        return verb, f"{action} {task}".strip()

    if name == "wait_for":
        cond = str(a.get("condition") or a.get("port") or a.get("seconds") or "").strip()
        return verb, cond

    for val in a.values():
        sval = re.sub(r"\s+", " ", str(val).strip())
        if sval:
            return verb, (sval[:65] + "…") if len(sval) > 68 else sval
    return verb, ""


def print_tool_call(name: str, args: Optional[dict] = None) -> None:
    """Render tool invocation as a crisp bullet: ● Verb(arg) with green status indicator."""
    sync_theme()
    verb, arg_preview = _format_tool_header(name, args)
    t = Text()
    t.append("  ")
    t.append("● ", style=Theme.TOOL_OK)
    t.append(verb, style=f"bold {Theme.PRIMARY}")
    if arg_preview:
        t.append("(", style=Theme.DIM)
        t.append(sanitize_surrogates(arg_preview), style=Theme.SECONDARY)
        t.append(")", style=Theme.DIM)
    console.print(t)


def _print_tree_line(content: str, style: str = Theme.SECONDARY, first: bool = False) -> None:
    prefix = f"    {ui_glyph('branch')}  " if first else "       "
    console.print(f"[{Theme.DIM}]{prefix}[/][{style}]{escape(content)}[/]")


def _render_diff_tree(lines: list[str], dur: str) -> bool:
    """Render concise inline +/- diff preview when unified diff lines exist."""
    diff_lines = [
        ln for ln in lines
        if ln.startswith(("+", "-", "@@")) and not ln.startswith(("+++", "---"))
    ]
    if not diff_lines:
        return False

    header = re.sub(r"^\[OK\]\s*", "", lines[0].strip()) if lines else "Updated"
    _print_tree_line(f"{header}{dur}", style=Theme.TOOL_OK, first=True)
    max_diff = 10
    for dline in diff_lines[:max_diff]:
        if dline.startswith("+"):
            _print_tree_line(dline, style=Theme.TOOL_OK)
        elif dline.startswith("-"):
            _print_tree_line(dline, style=Theme.TOOL_ERR)
        else:
            _print_tree_line(dline, style=Theme.DIM)
    if len(diff_lines) > max_diff:
        _print_tree_line(f"… (+{len(diff_lines) - max_diff} more diff lines)", style=Theme.DIM)
    return True


def print_tool_result(
    result: str,
    name: str,
    is_error: bool = False,
    elapsed: Optional[float] = None,
) -> None:
    """Render tool results as clean indented tree items without multi-line box chrome."""
    sync_theme()
    clean = sanitize_surrogates(result or "").strip()
    dur = _format_duration(elapsed)
    lines = clean.splitlines() if clean else []

    if is_error:
        head = lines[0] if lines else "Tool failed"
        if len(head) > 140:
            head = head[:137] + "…"
        _print_tree_line(f"{head}{dur}", style=Theme.TOOL_ERR, first=True)
        for extra in lines[1:4]:
            _print_tree_line(extra[:140], style=Theme.DIM)
        return

    if name in ("dynamic_architect", "dynamic_coder", "dynamic_verifier", "dynamic_explorer", "ask_user"):
        if "[ABORTED_BY_USER]" in clean:
            _print_tree_line(f"Workflow paused: user cancelled at checkpoint{dur}", style=Theme.TOOL_ERR, first=True)
            _print_tree_line(clean[:130], style=Theme.DIM)
        elif "User selected option" in clean or "User confirmed" in clean or "User response" in clean:
            _print_tree_line(f"Dynamic Checkpoint Resolved{dur}", style=Theme.TOOL_OK, first=True)
            _print_tree_line(clean[:140], style=Theme.SECONDARY)
        else:
            _print_tree_line(f"{clean[:140]}{dur}", style=Theme.INFO, first=True)
        return

    if name == "visual_inspect":
        if is_error:
            _print_tree_line(f"Visual inspection error: {lines[0] if lines else 'failed'}{dur}", style=Theme.TOOL_ERR, first=True)
            return
        passed = "Visual checks: pass" in clean
        status_tag = "✓ Visual Audit: Clean" if passed else "✗ Visual Audit: Defect"
        status_style = Theme.TOOL_OK if passed else Theme.TOOL_ERR
        _print_tree_line(f"{status_tag}{dur}", style=status_style, first=True)

        for ln in lines:
            if ln.startswith("Viewport ["):
                _print_tree_line(ln[:130], style=Theme.SECONDARY)
            elif any(ln.strip().startswith(k) for k in ("Low contrast", "Header bleed", "Broken images", "Occluded controls", "Clipped text", "OVERFLOW_X")):
                _print_tree_line(ln.strip()[:130], style=Theme.WARN if passed else Theme.TOOL_ERR)
            elif ln.startswith("Console/JS Errors") and not ln.endswith(": 0"):
                _print_tree_line(ln[:130], style=Theme.TOOL_ERR)
            elif ln.startswith("[OK] Visual capture attached"):
                summary = re.sub(r"^\[OK\]\s*", "", ln).strip()
                _print_tree_line(summary[:130], style=Theme.DIM)
        return

    if name == "todo":
        summary = re.sub(r"^\[OK\]\s*", "", lines[0])[:140] if lines else "Todo updated"
        _print_tree_line(f"{summary}{dur}", style=Theme.SECONDARY, first=True)
        for extra in lines[1:4]:
            if extra.strip():
                _print_tree_line(extra.strip()[:130], style=Theme.DIM)
        return

    if name == "wait_for":
        summary = lines[0][:140] if lines else "Condition met"
        _print_tree_line(f"{summary}{dur}", style=Theme.TOOL_OK if not is_error else Theme.TOOL_ERR, first=True)
        return

    if name == "read_skill":
        summary = re.sub(r"^\[OK\]\s*", "", lines[0])[:160] if lines else "Skill loaded"
        _print_tree_line(f"{summary}{dur}", style=Theme.INFO, first=True)
        for extra in lines[1:3]:
            if extra.startswith("[Warn]"):
                _print_tree_line(extra[:140], style=Theme.WARN)
        return

    if name in ("read_file", "read_files"):
        header_match = re.search(r"(?:Showing\s+)?lines\s+(\d+)-(\d+)\s+of\s+(\d+)", clean, re.IGNORECASE)
        trunc_match = re.search(r"showing\s+first\s+(\d+)\s+of\s+(\d+)\s+lines", clean, re.IGNORECASE)
        if header_match:
            s_l, e_l, total_l = header_match.groups()
            summary = f"Read lines {s_l}–{e_l} of {total_l}"
        elif trunc_match:
            shown_l, total_l = trunc_match.groups()
            summary = f"Read first {shown_l} of {total_l} lines"
        elif name == "read_file" and lines and re.match(r"^\s*\d+ \|", lines[0]):
            first = lines[0].partition("|")[0].strip()
            last = lines[-1].partition("|")[0].strip()
            summary = f"Read lines {first}-{last}"
        elif len(lines) == 1 and clean.startswith("["):
            summary = clean[:140]
        else:
            summary = f"Read {len(lines)} line{'s' if len(lines) != 1 else ''}"
        _print_tree_line(f"{summary}{dur}", style=Theme.DIM, first=True)
        return

    if name in ("patch_file", "write_file"):
        if _render_diff_tree(lines, dur):
            return
        summary = re.sub(r"^\[OK\]\s*", "", lines[0])[:140] if lines else "Done"
        _print_tree_line(f"{summary}{dur}", style=Theme.SECONDARY, first=True)
        return

    if name in ("glob_files", "list_dir", "search_files"):
        non_empty = [ln for ln in lines if ln.strip()]
        if not non_empty:
            _print_tree_line(f"No matches{dur}", style=Theme.DIM, first=True)
            return
        if len(non_empty) == 1:
            _print_tree_line(f"{non_empty[0][:140]}{dur}", style=Theme.SECONDARY, first=True)
            return
        _print_tree_line(f"{non_empty[0][:140]}{dur}", style=Theme.SECONDARY, first=True)
        for extra in non_empty[1:5]:
            _print_tree_line(extra[:140], style=Theme.DIM)
        if len(non_empty) > 5:
            _print_tree_line(f"… (+{len(non_empty) - 5} more)", style=Theme.DIM)
        return

    if not lines:
        _print_tree_line(f"Done{dur}", style=Theme.DIM, first=True)
        return

    filtered = []
    for raw_ln in lines:
        cleaned_ln = raw_ln.rsplit("\r", 1)[-1].strip()
        if not cleaned_ln:
            continue
        low = cleaned_ln.lower()
        if low in ("[exit: 0]", "exit code: 0", "stdout:", "status: completed (ok)"):
            continue
        cleaned_ln = re.sub(r"\s{4,}\[\s*\d+%\]$", "", cleaned_ln).strip()
        if cleaned_ln:
            filtered.append(cleaned_ln)

    if not filtered:
        _print_tree_line(f"Done{dur}", style=Theme.DIM, first=True)
        return

    max_w = max(48, (getattr(console.size, "width", 80) or 80) - 12)
    clip = lambda s: (s[: max_w - 1].rstrip() + "…") if len(s) > max_w else s

    if len(filtered) <= 6:
        first_style = Theme.TOOL_OK if any(filtered[0].startswith(p) for p in ("[OK]", "OK", "Success", "tsc=0", "Port", "Done", "Listening:")) else Theme.SECONDARY
        _print_tree_line(f"{clip(filtered[0])}{dur}", style=first_style, first=True)
        for ln in filtered[1:]:
            _print_tree_line(clip(ln), style=Theme.SECONDARY)
        return

    first_style = Theme.TOOL_OK if any(filtered[0].startswith(p) for p in ("[OK]", "OK", "Success", "tsc=0", "Port", "Done", "Listening:")) else Theme.SECONDARY
    _print_tree_line(f"{clip(filtered[0])}{dur}", style=first_style, first=True)
    _print_tree_line(clip(filtered[1]), style=Theme.SECONDARY)
    omitted = len(filtered) - 5
    _print_tree_line(f"… ({omitted} more lines)", style=Theme.DIM)
    for ln in filtered[-3:]:
        _print_tree_line(clip(ln), style=Theme.SECONDARY)


# ─── Commands Output (/help, /models, /status) ────────────────────────────────

def print_help() -> None:
    """Display concise, aligned command reference."""
    sync_theme()
    console.print()
    console.print(Rule(title=f"[bold {Theme.PRIMARY}]Commands[/]", align="left", style=Theme.BORDER))
    table = Table(
        box=None,
        show_header=False,
        padding=(0, 3, 0, 2),
        expand=False,
    )
    table.add_column("Command", style=f"bold {Theme.ACCENT}", no_wrap=True)
    table.add_column("Description", style=Theme.SECONDARY)

    groups = [
        ("Session", [
            ("/new [keep]", "Start a fresh browser chat thread (use 'keep' to retain summary)"),
            ("/clear", "Clear conversation and start fresh"),
            ("/compact", "Compact older messages to save context"),
            ("/save  /load", "Save or restore a session"),
            ("/status", "Inspect active bridge, website, browser, and context"),
            ("/theme [name]", "Switch palette (dark / dark-daltonized / github-dark / dracula)"),
            ("/exit", "Exit wsa"),
        ]),
        ("Web Bridges & Browser", [
            ("/model [name]", "Switch web bridge: claude | deepseek | gemini | chatgpt | venice"),
            ("/models", "Show all 5 Web Bridges & live browser state"),
            ("/multiagent", "Queue websites for auto-failover (Default or Custom)"),
            ("/singleagent [m]", "Return to single-website bridge mode (default)"),
            ("/account [name]", "Inspect or bind Chrome / Brave browser session"),
        ]),
        ("Workspace", [
            ("/init", "Index workspace structure into context"),
            ("/project [dir]", "Switch workspace directory"),
            ("/read <path>", "Inspect a file with syntax highlighting"),
            ("/run <cmd>", "Run a shell command"),
            ("/search <pat>", "Search project files"),
            ("/diff  /undo", "Review git diff or revert last file edit"),
        ]),
        ("Orchestration & QA", [
            ("/team [mode]", "Configure subagent pipeline (auto | on | off | single-chat | role-chats | dual | single)"),
            ("/concept [mode]", "Toggle or view pre-planning UI visual concept mockups (auto | on | off | show | clear)"),
            ("/plan [task]", "Build or view a dependency-ordered execution plan"),
            ("/verify [scope]", "Run independent verifier checks on codebase/UI"),
            ("/skills [name|scan]", "Browse 18 bundled WSA skills, read a skill, or run SkillSpector scan"),
        ]),
    ]

    for idx, (group_name, items) in enumerate(groups):
        if idx > 0:
            table.add_row("", "")
        table.add_row(f"[bold {Theme.PRIMARY}]{group_name}[/]", "")
        for cmd, desc in items:
            table.add_row(f"  {cmd}", desc)

    width = getattr(console.size, "width", 96) or 96
    console.print(table)
    footer_txt = (
        "Shift+Tab permissions · ? shortcuts · /tools · /rules · /perm · /tokens · @path"
        if width >= 84
        else "Shift+Tab mode · ? shortcuts · /tools · /tokens · @path"
    )
    console.print(Padding(Text(footer_txt, style=Theme.DIM), (0, 2)))
    console.print(Rule(style=Theme.BORDER))
    console.print()


def print_models_table(
    all_models: Iterable[tuple[str, str]],
    current_model: str = "",
    state=None,
) -> None:
    """Display the 5 supported Web Bridges (claude, deepseek, gemini, chatgpt, venice) with live browser state."""
    sync_theme()
    active_id = (current_model or getattr(state, "model", "gemini") or "gemini").strip().lower()
    active_label = friendly_model_name(active_id)
    acct_lbl = (getattr(state, "active_account_label", "") or "").strip()
    acct_email = (getattr(state, "active_account_email", "") or "").strip()
    acct_suffix = f" · {acct_lbl}" + (f" · {acct_email}" if acct_email else "") if acct_lbl else ""
    mode_tag = ""
    if state is not None and getattr(state, "agent_mode", "singleagent") == "multiagent":
        q = getattr(state, "multiagent_queue", None) or []
        if q:
            mode_tag = f" · multiagent [{' → '.join(str(x) for x in q)}]"
    width = getattr(console.size, "width", 96) or 96
    wide = width >= 84

    console.print()
    console.print(
        Rule(
            title=f"[bold {Theme.PRIMARY}]Web Bridges[/] [{Theme.DIM}]· {escape(active_label)}{escape(acct_suffix)}{escape(mode_tag)}[/]",
            align="left",
            style=Theme.BORDER,
        )
    )

    for entry in MODEL_CATALOG:
        dyn = get_model_info(entry["id"], state=state)
        mid = dyn["id"]
        is_active = active_id.startswith(mid)
        marker = f"[bold {Theme.ACCENT}]✓[/]" if is_active else " "
        name_style = f"bold {Theme.ACCENT}" if is_active else f"bold {Theme.PRIMARY}"
        mode_str = f"{dyn['transport']} · {dyn['thinking']}"
        mode_style = Theme.PRIMARY if is_active else Theme.SECONDARY

        if wide:
            console.print(
                f"  {marker} [{name_style}]{escape(dyn['name']):<14}[/] "
                f"[{Theme.DIM}]{escape(mid):<10}[/] "
                f"[{mode_style}]{escape(mode_str)}[/]"
            )
            console.print(Padding(Text(dyn["features"], style=Theme.SECONDARY), (0, 2, 0, 6)))
        else:
            console.print(
                f"  {marker} [{name_style}]{escape(dyn['name'])}[/]  "
                f"[{mode_style}]{escape(mode_str)}[/]"
            )
            console.print(Padding(Text(f"{mid} · {dyn['features']}", style=Theme.SECONDARY), (0, 2, 0, 6)))

    console.print()
    footer_txt = "Switch /model claude|deepseek|gemini|chatgpt|venice · Queue /multiagent\nSelect model & thinking in browser"
    console.print(Padding(Text(footer_txt, style=Theme.DIM), (0, 2)))
    console.print(Rule(style=Theme.BORDER))
    console.print()


def print_status(state) -> None:
    """Display quiet aligned session diagnostics for /status."""
    sync_theme()
    palette = get_theme()
    console.print()
    console.print(Rule(title=f"[bold {Theme.PRIMARY}]Session Status[/]", align="left", style=Theme.BORDER))
    table = Table(box=None, show_header=False, padding=(0, 3, 0, 2), expand=False)
    table.add_column("Key", style=Theme.DIM, no_wrap=True)
    table.add_column("Value", style=Theme.PRIMARY)

    model_id = getattr(state, "model", "gemini")
    info = get_model_info(model_id, state=state)
    model_friendly = info.get("name") or friendly_model_name(model_id)
    acct_lbl = (getattr(state, "active_account_label", "") or "").strip()
    acct_email = (getattr(state, "active_account_email", "") or "").strip()
    if acct_lbl and acct_email:
        acct_val = f"{escape(acct_lbl)} · {escape(acct_email)}"
    elif acct_lbl:
        acct_val = escape(acct_lbl)
    else:
        acct_val = f"[{Theme.DIM}]Unbound (/account chrome | brave)[/]"
    chat_url = (getattr(state, "active_chat_url", "") or "").strip()
    if "venice.ai" in chat_url.lower() and re.search(r"/chat/(?:agent|classic)/[A-Za-z0-9_-]+", chat_url, re.I):
        tid = chat_url.rstrip("/").rsplit("/", 1)[-1][:18]
        thread_label = f"{escape(tid)}  [{Theme.DIM}]· pinned venice.ai thread[/]"
    elif "/a/chat/s/" in chat_url:
        tid = chat_url.rsplit("/a/chat/s/", 1)[-1][:18]
        thread_label = f"{escape(tid)}  [{Theme.DIM}]· pinned deepseek thread[/]"
    elif "/chat/" in chat_url:
        tid = chat_url.rsplit("/chat/", 1)[-1][:18]
        thread_label = f"{escape(tid)}  [{Theme.DIM}]· pinned claude.ai thread[/]"
    elif "/app/" in chat_url:
        tid = chat_url.rsplit("/app/", 1)[-1][:18]
        thread_label = f"{escape(tid)}  [{Theme.DIM}]· pinned gemini thread[/]"
    elif "/c/" in chat_url:
        tid = chat_url.rsplit("/c/", 1)[-1][:18]
        thread_label = f"{escape(tid)}  [{Theme.DIM}]· pinned chatgpt thread[/]"
    else:
        thread_label = f"[{Theme.DIM}]New thread on first turn[/]"
    agent_mode = str(getattr(state, "agent_mode", "singleagent") or "singleagent")
    m_queue = list(getattr(state, "multiagent_queue", None) or [])
    if agent_mode == "multiagent" and m_queue:
        parts_q = []
        for qp in m_queue:
            if qp == model_id:
                parts_q.append(f"[bold {Theme.ACCENT}]{escape(f'[{qp}]')}[/]")
            else:
                parts_q.append(f"[{Theme.SECONDARY}]{escape(qp)}[/]")
        agent_mode_val = f"[{Theme.ACCENT}]multiagent[/]  " + f" [{Theme.DIM}]→[/] ".join(parts_q)
    else:
        agent_mode_val = f"{escape(agent_mode)}  [{Theme.DIM}]· /multiagent to enable queue[/]"
    web_model = str(getattr(state, "active_web_model", "") or "").strip() or "Select in browser tab"
    plan_items = getattr(state, "plan_items", None) or []
    team_mode = str(getattr(state, "team_mode", "auto") or "auto")
    thread_mode = str(getattr(state, "thread_mode", "single") or "single")
    dual_lane = bool(getattr(state, "dual_lane", False))
    team_label = f"{team_mode} · {'single-chat' if thread_mode == 'single' else 'role-chats'} · {'dual requested; serialized' if dual_lane else 'single-relay'}"
    if plan_items:
        done_n = sum(1 for it in plan_items if str(it.get("status") or "") == "done")
        team_label += f" · plan {done_n}/{len(plan_items)}"
    rows = [
        ("Agent Mode", agent_mode_val),
        ("Bridge", f"{escape(model_friendly)}  [{Theme.DIM}]{escape(model_id)}[/]"),
        ("Website", f"{escape(info.get('site', '-'))}  [{Theme.DIM}]· {escape(info.get('transport', '-'))}[/]"),
        ("Web Model", escape(web_model)),
        ("Account", acct_val),
        ("Thread", thread_label),
        ("", ""),
        ("Permissions", str(getattr(state, "permission_mode", "auto"))),
        ("Connection", escape(str(getattr(state, "_ui_connection", "Not checked")))),
        ("Team", escape(team_label)),
    ]
    last_vr = getattr(state, "last_visual_review", None)
    if isinstance(last_vr, dict) and (last_vr.get("summary") or last_vr.get("outcome")):
        vr_summary = sanitize_surrogates(str(last_vr.get("summary") or last_vr.get("outcome") or "")).strip()
        vr_timing = sanitize_surrogates(str(last_vr.get("timing_text") or "")).strip()
        if vr_timing and vr_timing not in vr_summary:
            vr_summary = f"{vr_summary} ({vr_timing})"
        rows.append(("Visual Review", escape(vr_summary)))
    rows.extend([
        ("Session", f"{getattr(state, 'session_id', '-')[:12]}  [{Theme.DIM}]· {getattr(state, 'turn_count', 0)} turns · {getattr(state, 'session_duration', '0s')}[/]"),
        ("History", f"{len(getattr(state, 'messages', []))} messages"),
        ("Workspace", f"{escape(_short_path(getattr(state, 'project_root', '.')))}  [{Theme.DIM}]· {escape(str(getattr(state, 'project_type', 'Workspace')))}[/]"),
        ("Endpoint", escape(str(getattr(state, "api_base", "http://127.0.0.1:8081/v1")))),
        ("Theme", f"{escape(palette.name)}  [{Theme.DIM}]{escape(palette.id)}[/]"),
    ])
    for k, v in rows:
        table.add_row(k, v)

    console.print(table)
    console.print(Rule(title=f"[{Theme.DIM}]Engineered by Divyansh Saraf[/]", align="right", style=Theme.BORDER))
    console.print()


# ─── Notifications ────────────────────────────────────────────────────────────

def print_info(msg: str) -> None:
    sync_theme()
    console.print(f"  [{Theme.DIM}]{ui_glyph('bullet')}[/] [{Theme.INFO}]{msg}[/]")


def print_success(msg: str) -> None:
    sync_theme()
    console.print(f"  [{Theme.SUCCESS}]{ui_glyph('ok')}[/] [{Theme.PRIMARY}]{msg}[/]")


def print_warn(msg: str) -> None:
    sync_theme()
    console.print(f"  [{Theme.WARN}]![/] [{Theme.WARN}]{msg}[/]")


def print_error(msg: str) -> None:
    sync_theme()
    console.print(f"  [{Theme.ERROR}]{ui_glyph('err')}[/] [{Theme.ERROR}]{msg}[/]")


def print_visual_status(msg: str, *, outcome: str = "reviewed") -> None:
    """Print a concise, theme-consistent final visual-review outcome line."""
    sync_theme()
    clean = sanitize_surrogates(str(msg or "")).strip()
    if not clean:
        return
    t = Text()
    if outcome == "passed":
        t.append(f"{ui_glyph('ok')} ", style=Theme.SUCCESS)
        t.append(clean, style=Theme.PRIMARY)
    elif outcome in ("defects", "reviewed_with_local_defects"):
        t.append("! ", style=Theme.WARN)
        t.append(clean, style=Theme.PRIMARY)
    elif outcome in ("unavailable", "cancelled"):
        t.append("! ", style=Theme.WARN)
        t.append(clean, style=Theme.WARN)
    elif outcome in ("transport_error", "capture_failed"):
        t.append(f"{ui_glyph('err')} ", style=Theme.ERROR)
        t.append(clean, style=Theme.ERROR)
    else:
        t.append(f"{ui_glyph('bullet')} ", style=Theme.DIM)
        t.append(clean, style=Theme.SECONDARY)
    console.print(Padding(t, (0, 2)))


def print_markdown(text: str) -> None:
    """Render Markdown response body cleanly with session padding."""
    sync_theme()
    from rich.markdown import Markdown as _RichMarkdown
    console.print(Padding(_RichMarkdown(str(text or "")), (0, 2, 1, 2)))


_ROLE_PHASE_CARDS = {
    "EXPLORER": ("🔍", "cyan", "Workspace Recon & Deep Research"),
    "ARCHITECT": ("📐", "yellow", "System Architecture & File Blueprint"),
    "CODER": ("⚡", "green", "Surgical Patch & Implementation"),
    "SECURITY": ("🛡️", "red", "Adversarial Security & OWASP Audit"),
    "VERIFIER": ("🧪", "magenta", "Independent Verification & QA Gate"),
    "ORCHESTRATOR": ("◆", Theme.ACCENT, "Multi-Phase Workflow Synthesis"),
}


def print_subagent_banner(role: str, label: str) -> None:
    """Print crisp, defined stage banner with role-specific glyphs and badge styling."""
    sync_theme()
    role_key = str(role or "subagent").upper()
    icon, color, subtitle = _ROLE_PHASE_CARDS.get(
        role_key,
        ("◆", Theme.ACCENT, "Specialized Subagent Phase"),
    )
    console.print()
    rule_title = f"[bold {color}]{icon}  {escape(role_key)}[/] [{Theme.DIM}]· {escape(subtitle)}[/]"
    console.print(Rule(title=rule_title, align="left", style=color))
    if label:
        console.print(f"  [{Theme.SECONDARY}]{escape(str(label))}[/]")
    console.print()


def print_skill_appointment(appointment) -> None:
    """Render crisp TUI visuals when WSA detects prompt signals and appoints skills to read first."""
    if appointment is None:
        return
    skills = tuple(getattr(appointment, "skills", ()) or ())
    requires_sec = bool(getattr(appointment, "requires_security_pass", False))
    if not skills and not requires_sec:
        return
    sync_theme()
    headline = str(getattr(appointment, "headline", "") or "").strip()
    if not headline:
        headline = "This prompt detected specialized signals — forcing web agents to read the relevant skills first..."
    console.print(
        f"  [{Theme.ACCENT}]◆[/] [bold {Theme.INFO}]{escape(sanitize_surrogates(headline))}[/]"
    )
    cues = tuple(getattr(appointment, "matched_cues", ()) or ())
    ai_picks = tuple(getattr(appointment, "ai_recommended", ()) or ())
    sec_skills = tuple(getattr(appointment, "security_skills", ()) or ())

    rows: list[tuple[str, str, str]] = []
    if cues:
        rows.append(("Signals", ", ".join(cues[:8]), Theme.SECONDARY))
    if skills:
        skill_text = ", ".join(skills)
        if ai_picks:
            extra_ai = [s for s in ai_picks if s in skills]
            if extra_ai:
                skill_text += f"  (web-agent judged: {', '.join(extra_ai)})"
        rows.append(("Read first", skill_text, Theme.PRIMARY))
    if requires_sec:
        sec_str = ", ".join(sec_skills or ("security-audit", "semgrep-guardrails"))
        rows.append(("Security pass", f"{sec_str} (auto-enforced after website build)", Theme.WARN))

    for idx, (lbl, val, style) in enumerate(rows):
        branch = "└─" if idx == len(rows) - 1 else "├─"
        console.print(
            f"    [{Theme.DIM}]{branch}[/] [bold {Theme.DIM}]{escape(lbl)}:[/] "
            f"[{style}]{escape(sanitize_surrogates(val))}[/]"
        )


def print_skills_table(specs, category_labels: dict, loaded_skills: Iterable[str] = ()) -> None:
    """Render all bundled WSA skills grouped cleanly by domain for `/skills`."""
    sync_theme()
    loaded = set(loaded_skills or ())
    console.print()
    console.print(Rule(title=f"[bold {Theme.PRIMARY}]WSA Bundled Skills ({len(specs)})[/]", align="left", style=Theme.BORDER))
    table = Table(box=None, show_header=True, header_style=f"bold {Theme.DIM}", padding=(0, 2), expand=False)
    table.add_column("Skill", style=f"bold {Theme.ACCENT}", no_wrap=True)
    table.add_column("Domain", style=Theme.INFO, no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Purpose", style=Theme.SECONDARY)

    for spec in specs:
        cat = category_labels.get(spec.category, spec.category)
        status = f"[{Theme.SUCCESS}]loaded[/]" if spec.name in loaded else f"[{Theme.DIM}]ready[/]"
        table.add_row(escape(spec.name), escape(cat), status, escape(spec.tagline))

    console.print(table)
    console.print(
        Padding(
            Text("Usage: /skills <name>  ·  /skills scan <path>  ·  Agent tool: read_skill(name=\"...\")", style=Theme.DIM),
            (0, 2),
        )
    )
    console.print(Rule(style=Theme.BORDER))
    console.print()


def print_subagent_event(stage_label: str, kind: str, payload: dict) -> None:
    """Print live subagent tool headers, structured tool results, and failover events."""
    sync_theme()
    if kind == "round" and payload.get("activity"):
        activity = Text("  " + sanitize_surrogates(stage_label) + "  ", style=Theme.INFO)
        activity.append(sanitize_surrogates(str(payload["activity"])), style=Theme.DIM)
        console.print(activity)
    elif kind == "tool_call":
        tname = str(payload.get("tool") or "tool")
        verb, preview = _format_tool_header(tname, payload.get("args") or {})
        t = Text()
        t.append("  ")
        t.append("● ", style=Theme.TOOL_OK)
        if stage_label:
            t.append(f"{sanitize_surrogates(str(stage_label))} ", style=Theme.INFO)
        t.append(verb, style=f"bold {Theme.PRIMARY}")
        if preview:
            t.append("(", style=Theme.DIM)
            t.append(sanitize_surrogates(preview), style=Theme.SECONDARY)
            t.append(")", style=Theme.DIM)
        console.print(t)
    elif kind == "failover":
        frm = escape(str(payload.get("from_model") or payload.get("from") or "?"))
        to = escape(str(payload.get("to_model") or payload.get("to") or "?"))
        reason = escape(str(payload.get("reason") or payload.get("message") or "limit reached")[:70])
        console.print(
            f"    [{Theme.DIM}]├─[/] [{Theme.WARN}]{escape(stage_label)}[/] "
            f"[{Theme.WARN}]failover {frm} → {to}[/] [{Theme.DIM}]({reason})[/]"
        )
    elif kind == "visual_status":
        msg = str(payload.get("message") or "").strip()
        outcome = str(payload.get("outcome") or "reviewed").strip()
        if msg:
            prefix = f"{sanitize_surrogates(str(stage_label))} · " if stage_label else ""
            print_visual_status(f"{prefix}{msg}", outcome=outcome)
    elif kind == "tool_result":
        tname = str(payload.get("tool") or "tool")
        ok = bool(payload.get("ok", True))
        raw_res = payload.get("result")
        if raw_res is None:
            raw_res = payload.get("preview") or ""
        elapsed = payload.get("elapsed")
        print_tool_result(str(raw_res), tname, is_error=not ok, elapsed=elapsed)
        if ok and tname == "read_file":
            for line in str(raw_res).splitlines()[:2]:
                if re.match(r"^\s*\d+ \|", line):
                    _print_tree_line(line[:120], style=Theme.SECONDARY)


def print_plan_table(tasks: list) -> None:
    """Display the Architect task DAG as a clean aligned Rich table."""
    sync_theme()
    console.print()
    console.print(Rule(title=f"[bold {Theme.PRIMARY}]Execution Plan ({len(tasks)} task(s))[/]", align="left", style=Theme.BORDER))
    table = Table(box=None, show_header=True, header_style=f"bold {Theme.DIM}", padding=(0, 2), expand=False)
    table.add_column("ID", style=Theme.ACCENT, no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Task", style=Theme.PRIMARY)
    table.add_column("Owned Files", style=Theme.SECONDARY)
    table.add_column("Deps", style=Theme.DIM, no_wrap=True)

    for item in tasks:
        st = str(item.get("status") or "pending").lower()
        if st == "done":
            st_fmt = f"[{Theme.SUCCESS}]done[/]"
        elif st == "in_progress":
            st_fmt = f"[{Theme.WARN}]running[/]"
        elif st == "failed":
            st_fmt = f"[{Theme.ERROR}]failed[/]"
        else:
            st_fmt = f"[{Theme.DIM}]pending[/]"
        owned = ", ".join(item.get("owned_files") or []) or "*"
        deps = ",".join(item.get("depends_on") or []) or "-"
        table.add_row(
            escape(str(item.get("id") or "")),
            st_fmt,
            escape(str(item.get("title") or "")[:64]),
            escape(owned[:42]),
            escape(deps),
        )
    console.print(table)
    console.print()
