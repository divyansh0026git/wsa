"""Strict fenced/bare tool-call extraction and streaming fence hold helpers."""
from __future__ import annotations

import json
import re
import uuid
from typing import Optional

from .parser import _ZERO_WIDTH_CHARS

_TOOL_BLOCK_OPEN_RE = re.compile(
    rf"(?P<explicit>(?:(?:^|(?<=\n))[ \t]*)?(?P<eticks>`{{3,}}|~{{3,}})[{_ZERO_WIDTH_CHARS} \t]*(?:tool[_-]?calls?|function[_-]?calls?)(?:\b[^\r\n]*)?\r?\n)"
    rf"|(?:^|(?<=\n))[ \t]*(?:"
    rf"(?P<snippet>Code snippet)[ \t]*\r?\n"
    rf"|(?P<generic>(?P<gticks>`{{3,}}|~{{3,}})[{_ZERO_WIDTH_CHARS} \t]*(?P<ginfo>[A-Za-z0-9_+.-]*)[ \t]*\r?\n)"
    rf")"
)


_TOOL_BARE_LINE_RE = re.compile(
    r"(?:^|(?<=\n))[ \t]*(?P<bt>`?)(?:tool_call[ \t]+)?(?=\{|\[\s*\{)",
    re.IGNORECASE,
)


_TOOL_CLOSE_FENCE_RE = re.compile(
    rf"[ \t]*(?:\r?\n[ \t]*)?(?:`{{3,}}|~{{3,}})(?:[{_ZERO_WIDTH_CHARS} \t]*(?:_\s*[\]\*]|_\])?)?(?=[ \t]*(?:\r?\n|$))"
    rf"|\s*(?:`{{3,}}|~{{3,}})(?:[{_ZERO_WIDTH_CHARS} \t]*(?:_\s*[\]\*]|_\])?)?"
)


_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_PLACEHOLDER_TOOL_NAMES = frozenset({
    "func_name",
    "action_name",
    "tool_name",
    "function_name",
    "example_tool",
    "your_tool_name",
})
_VALID_JSON_ESCAPES = set('"\\/bfnrt')
_HEX_DIGITS = set("0123456789abcdefABCDEF")


def _extract_tool_names(tools: Optional[list]) -> Optional[set]:
    if not tools:
        return None
    names = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = str(fn.get("name") or "").strip()
        if name:
            names.add(name)
    return names or None


def _is_valid_tool_call_item(item, strict: bool = False, allowed_names: Optional[set] = None) -> bool:
    if not isinstance(item, dict):
        return False
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        return False
    clean_name = name.strip()
    if clean_name.lower() in _PLACEHOLDER_TOOL_NAMES:
        return False
    if "arguments" not in item and "args" not in item:
        return False
    raw_args = item.get("arguments", item.get("args"))
    parsed_args = raw_args
    if isinstance(raw_args, str):
        try:
            parsed_args = json.loads(raw_args)
        except Exception:
            parsed_args = raw_args
    if isinstance(parsed_args, dict) and parsed_args == {"arg1": "val1"}:
        return False
    if not strict:
        return True
    if not _TOOL_NAME_RE.match(clean_name):
        return False
    if allowed_names and clean_name not in allowed_names:
        return False
    if isinstance(raw_args, dict):
        return True
    if isinstance(raw_args, str):
        stripped = raw_args.strip()
        return stripped.startswith("{")
    return False


def _extract_balanced_json_span(text: str, start_idx: int) -> Optional[int]:
    """Return end index (exclusive) of balanced JSON object/array starting at start_idx, or None."""
    if start_idx >= len(text) or text[start_idx] not in "{[":
        return None
    stack = []
    in_str = False
    escaped = False
    for pos in range(start_idx, len(text)):
        ch = text[pos]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return None
            stack.pop()
            if not stack:
                return pos + 1
    return None


def _splice_adjacent_json_strings(raw: str) -> str:
    """Splice `"a" + "b"` string concatenations located OUTSIDE JSON string literals."""
    out = []
    n = len(raw)
    i = 0
    in_str = False
    escaped = False
    while i < n:
        ch = raw[i]
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                # Look ahead for `<ws>+<ws>"` concatenation outside string
                j = i + 1
                while j < n and raw[j] in " \t\r\n":
                    j += 1
                if j < n and raw[j] == "+":
                    k = j + 1
                    while k < n and raw[k] in " \t\r\n":
                        k += 1
                    if k < n and raw[k] == '"':
                        out.pop()  # drop closing quote and splice continuation
                        i = k + 1
                        continue
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
        out.append(ch)
        i += 1
    return "".join(out)


def _repair_missing_json_commas(raw: str) -> str:
    """Insert missing commas between adjacent objects/arrays outside string literals."""
    out = []
    n = len(raw)
    i = 0
    in_str = False
    escaped = False
    while i < n:
        ch = raw[i]
        out.append(ch)
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
        elif ch in "}]":
            j = i + 1
            while j < n and raw[j] in " \t\r\n":
                j += 1
            if j < n and raw[j] in "{[\"":
                out.append(",")
        i += 1
    return "".join(out)


def _escape_unescaped_inner_quotes(raw: str) -> str:
    """Best-effort fallback escaping literal `"` inside JSON values when decoding failed."""
    out = []
    n = len(raw)
    i = 0
    in_str = False
    str_is_key = False
    escaped = False
    # Track container stack: "obj_key", "obj_colon", "obj_val", "arr"
    ctx_stack: list[str] = []

    def _next_non_ws(pos: int) -> int:
        while pos < n and raw[pos] in " \t\r\n":
            pos += 1
        return pos

    def _valid_after_comma(pos: int, parent: str) -> bool:
        nxt = raw[pos] if pos < n else ""
        if parent.startswith("obj"):
            return nxt in ('"', "}")
        return nxt in ('"', "{", "[", "]", "-", "t", "f", "n") or ("0" <= nxt <= "9")

    while i < n:
        ch = raw[i]
        if in_str:
            if escaped:
                out.append(ch)
                escaped = False
                i += 1
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                i += 1
                continue
            if ch == '"':
                k = _next_non_ws(i + 1)
                nxt = raw[k] if k < n else ""
                top = ctx_stack[-1] if ctx_stack else ""
                looks_like_close = False
                if k >= n:
                    looks_like_close = True
                elif str_is_key:
                    looks_like_close = (nxt == ":")
                elif top in ("obj_val", "arr"):
                    if nxt in ("}", "]"):
                        looks_like_close = True
                    elif nxt == ",":
                        k2 = _next_non_ws(k + 1)
                        looks_like_close = _valid_after_comma(k2, top)
                else:
                    if nxt in (":", "}", "]"):
                        looks_like_close = True
                    elif nxt == ",":
                        k2 = _next_non_ws(k + 1)
                        looks_like_close = _valid_after_comma(k2, "arr")
                if looks_like_close:
                    out.append(ch)
                    in_str = False
                    if str_is_key and ctx_stack:
                        ctx_stack[-1] = "obj_colon"
                else:
                    out.append('\\"')
                i += 1
                continue
            out.append(ch)
            i += 1
            continue

        out.append(ch)
        if ch == "{":
            ctx_stack.append("obj_key")
        elif ch == "[":
            ctx_stack.append("arr")
        elif ch in ("}", "]"):
            if ctx_stack:
                ctx_stack.pop()
        elif ch == ":":
            if ctx_stack and ctx_stack[-1] == "obj_colon":
                ctx_stack[-1] = "obj_val"
        elif ch == ",":
            if ctx_stack and ctx_stack[-1] in ("obj_val", "obj_colon"):
                ctx_stack[-1] = "obj_key"
        elif ch == '"':
            in_str = True
            str_is_key = bool(ctx_stack and ctx_stack[-1] == "obj_key")
        i += 1
    return "".join(out)


def _repair_tool_json_text(raw: str) -> str:
    """Repair invalid escape sequences, unescaped control chars inside strings, and trailing commas."""
    out = []
    in_str = False
    n = len(raw)
    i = 0
    while i < n:
        ch = raw[i]
        if in_str:
            if ch == "\\":
                if i + 1 < n:
                    nxt = raw[i + 1]
                    if nxt in _VALID_JSON_ESCAPES:
                        out.append(ch)
                        out.append(nxt)
                        i += 2
                        continue
                    if nxt == "u" and i + 5 < n and all(c in _HEX_DIGITS for c in raw[i + 2 : i + 6]):
                        out.append(raw[i : i + 6])
                        i += 6
                        continue
                    # Invalid JSON escape such as \' \s \d \w \v \. \vert etc. — preserve literal backslash
                    out.append("\\\\")
                    i += 1
                    continue
                out.append("\\\\")
                i += 1
                continue
            if ch == '"':
                out.append(ch)
                in_str = False
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            elif ord(ch) < 0x20:
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
        elif ch == ",":
            j = i + 1
            while j < n and raw[j] in " \t\r\n":
                j += 1
            if j < n and raw[j] in "}]":
                i += 1
                continue
            out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _loads_repaired_tool_json(raw_slice: str):
    """Attempt standard repair first, escalating to comma/concat/inner-quote recovery only on failure."""
    primary = _repair_tool_json_text(raw_slice)
    try:
        return json.loads(primary)
    except Exception:
        pass
    # Escalate carefully on malformed nested payloads (e.g., multi-hunk patch_file strings)
    stage2 = _repair_tool_json_text(_repair_missing_json_commas(_splice_adjacent_json_strings(raw_slice)))
    try:
        return json.loads(stage2)
    except Exception:
        pass
    stage3 = _repair_tool_json_text(_escape_unescaped_inner_quotes(stage2))
    return json.loads(stage3)


def _find_explicit_fence_end(text: str, start_idx: int, open_ticks: str = "```") -> Optional[int]:
    """Locate the line ending of a fenced block's closing delimiter when brace balance fails."""
    fence_ch = re.escape(open_ticks[0]) if open_ticks else "`"
    m = re.search(rf"(?:\r?\n|^)[ \t]*{fence_ch}{{3,}}[ \t]*(?=\r?\n|$)", text[start_idx:])
    return (start_idx + m.start()) if m else None


def _decode_tool_json_sequence(
    text: str,
    start_idx: int,
    decoder: json.JSONDecoder,
    strict: bool = False,
    allowed_names: Optional[set] = None,
    stop_on_single_backtick: bool = False,
    open_ticks: str = "",
) -> tuple:
    """Decode one or more JSON tool-call objects/arrays starting at start_idx.

    Returns (decoded_calls_list, last_end_idx, valid_block).
    """
    idx = start_idx
    last_end_idx = start_idx
    decoded_calls = []
    while idx < len(text):
        while idx < len(text) and text[idx] in " \t\r\n,":
            idx += 1
        if idx >= len(text) or text.startswith("```", idx) or text.startswith("~~~", idx):
            break
        if stop_on_single_backtick and decoded_calls and text.startswith("`", idx):
            break
        if text[idx] not in "{[":
            if strict:
                return [], start_idx, False
            break
        try:
            data, end_idx = decoder.raw_decode(text, idx)
        except Exception:
            bal_end = _extract_balanced_json_span(text, idx)
            if bal_end is None and not strict and open_ticks:
                bal_end = _find_explicit_fence_end(text, idx, open_ticks=open_ticks)
            if bal_end is None:
                if strict:
                    return [], start_idx, False
                break
            try:
                data = _loads_repaired_tool_json(text[idx:bal_end])
                end_idx = bal_end
            except Exception:
                # If balanced span cut short due to unescaped inner quote inside explicit block, retry up to fence
                if not strict and open_ticks:
                    fence_end = _find_explicit_fence_end(text, idx, open_ticks=open_ticks)
                    if fence_end is not None and fence_end > bal_end:
                        try:
                            data = _loads_repaired_tool_json(text[idx:fence_end])
                            end_idx = fence_end
                        except Exception:
                            break
                    else:
                        break
                else:
                    if strict:
                        return [], start_idx, False
                    break
        items = data if isinstance(data, list) else [data]
        valid_items = [
            item for item in items
            if _is_valid_tool_call_item(item, strict=strict, allowed_names=allowed_names)
        ]
        if strict and (not valid_items or len(valid_items) != len(items)):
            return [], start_idx, False
        decoded_calls.extend(valid_items)
        idx = end_idx
        last_end_idx = end_idx
    return decoded_calls, last_end_idx, bool(decoded_calls)


def _match_closing_fence_end(text: str, last_end_idx: int, open_ticks: str = "") -> int:
    remainder = text[last_end_idx:]
    close_match = _TOOL_CLOSE_FENCE_RE.match(remainder)
    if not close_match:
        return last_end_idx
    span_end = last_end_idx + close_match.end()
    # If an outer 4+ backtick wrapper (e.g. DOM <code-block>) wrapped a broken ```_] block,
    # also consume the matching outer closing fence line right after it.
    if len(open_ticks) >= 4:
        outer_fence_ch = re.escape(open_ticks[0])
        outer_match = re.match(
            rf"[ \t]*\r?\n[ \t]*{outer_fence_ch}{{{len(open_ticks)},}}[ \t]*(?=\r?\n|$)",
            text[span_end:],
        )
        if outer_match:
            span_end += outer_match.end()
    return span_end


def _find_pending_tool_hold_pos(pending_buf: str) -> int:
    """Return index in pending_buf to hold for potential tool call completion, or -1 to flush all."""
    if not pending_buf:
        return -1

    hold_candidates = []
    explicit_or_snippet = re.search(
        rf"(?:(?:^|\n)[ \t]*)?(?:(?:`{{3,}}|~{{3,}})[{_ZERO_WIDTH_CHARS} \t]*(?:tool[_-]?calls?|function[_-]?calls?)(?:\b[^\r\n]*)?\r?\n)"
        rf"|(?:^|\n)[ \t]*Code snippet[ \t]*\r?\n"
        rf"|(?:^|\n)[ \t]*(?:`{{3,}}|~{{3,}})[^\r\n]*\r?\n[ \t\r\n]*(?:\{{\s*\"(?:name|arguments|args|function)\"|\[\s*\{{\s*\"(?:name|arguments|args|function)\")"
        rf"|(?:^|\n)[ \t]*`?(?:tool_call[ \t]+)?(?:\{{\s*\"(?:name|arguments|args|function)\"\s*:|\[\s*\{{\s*\"(?:name|arguments|args|function)\"\s*:)",
        pending_buf,
    )
    if explicit_or_snippet:
        hold_candidates.append(explicit_or_snippet.start())

    # Tail check: hold partial opening fences, "Code", unverified short fence bodies, or line-start "{".
    tail_start = max(0, len(pending_buf) - 64)
    tail_slice = pending_buf[tail_start:]
    tail_patterns = [
        r"`{3,6}[^\r\n]{0,32}$",
        r"(?:^|\n)[ \t]*`{1,2}$",
        r"~{3,}[^\r\n]{0,32}$",
        r"(?:^|\n)[ \t]*(?:`{3,}|~{3,})[^\r\n]*\r?\n[ \t\r\n]*(?:[\{\[][\s\{\[]{0,24}(?:\"[^\r\n]{0,20})?)?$",
        r"(?:^|\n)[ \t]*Code[^\r\n]{0,16}$",
        r"(?:^|\n)[ \t]*`?(?:tool_call[ \t]+)?[\{\[][^\r\n]{0,32}$",
    ]
    for pat in tail_patterns:
        tm = re.search(pat, tail_slice)
        if tm:
            hold_candidates.append(tail_start + tm.start())

    return min(hold_candidates) if hold_candidates else -1


def _parse_tool_calls_internal(text: str, allowed_names: Optional[set] = None) -> tuple:
    """Internal worker returning `(cleaned_out, tool_calls, call_spans, removed_spans)`."""
    raw_entries = []
    spans_to_remove = []
    decoder = json.JSONDecoder()
    cursor = 0

    def _collect_calls(items, span_start: int, span_end: int):
        for item in items:
            args = item.get("arguments", item.get("args", {}))
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            raw_entries.append((span_start, span_end, item["name"].strip(), args))

    # Pass 1: fenced tool blocks and DOM "Code snippet" blocks
    while cursor < len(text):
        m = _TOOL_BLOCK_OPEN_RE.search(text, cursor)
        if not m:
            break
        is_generic = bool(m.group("generic"))
        open_ticks = m.group("eticks") or m.group("gticks") or ""
        decoded_items, last_end_idx, ok = _decode_tool_json_sequence(
            text,
            m.end(),
            decoder,
            strict=is_generic,
            allowed_names=None,
            open_ticks=open_ticks,
        )
        if ok:
            if is_generic and not (
                text[last_end_idx:] == ""
                or _TOOL_CLOSE_FENCE_RE.match(text[last_end_idx:])
                or re.match(r"[ \t]*(?:\r?\n|$)", text[last_end_idx:])
            ):
                cursor = m.end()
                continue
            span_end = _match_closing_fence_end(text, last_end_idx, open_ticks=open_ticks)
            _collect_calls(decoded_items, m.start(), span_end)
            spans_to_remove.append((m.start(), span_end))
            cursor = span_end
        else:
            cursor = m.end()

    # Pass 2: bare / indented standalone tool-call JSON lines not inside already-matched spans
    uncovered_ranges = []
    prev_end = 0
    for s, e in spans_to_remove:
        if s > prev_end:
            uncovered_ranges.append((prev_end, s))
        prev_end = max(prev_end, e)
    if prev_end < len(text):
        uncovered_ranges.append((prev_end, len(text)))

    for seg_start, seg_end in uncovered_ranges:
        seg_cursor = seg_start
        while seg_cursor < seg_end:
            bm = _TOOL_BARE_LINE_RE.search(text, seg_cursor, seg_end)
            if not bm:
                break
            line_start = bm.start()
            bt = bm.group("bt")
            json_start = bm.end()
            decoded_items, last_end_idx, ok = _decode_tool_json_sequence(
                text,
                json_start,
                decoder,
                strict=True,
                allowed_names=allowed_names,
                stop_on_single_backtick=bool(bt),
            )
            if ok and last_end_idx <= seg_end:
                tail = text[last_end_idx:seg_end]
                if bt:
                    bt_close = re.match(r"[ \t]*`[ \t]*(?=\r?\n|$)", tail)
                    if bt_close:
                        span_end = last_end_idx + bt_close.end()
                        _collect_calls(decoded_items, line_start, span_end)
                        spans_to_remove.append((line_start, span_end))
                        seg_cursor = span_end
                        continue
                else:
                    close_m = _TOOL_CLOSE_FENCE_RE.match(tail)
                    if close_m or re.match(r"[ \t]*(?:\r?\n|$)", tail):
                        span_end = last_end_idx + (close_m.end() if close_m else 0)
                        _collect_calls(decoded_items, line_start, span_end)
                        spans_to_remove.append((line_start, span_end))
                        seg_cursor = span_end
                        continue
            seg_cursor = json_start + 1

    # Preserve exact source order of appearance in response text
    raw_entries.sort(key=lambda row: row[0])
    tool_calls = []
    call_spans = []
    for idx_pos, (sp_s, sp_e, tname, args) in enumerate(raw_entries):
        tool_calls.append({
            "index": idx_pos,
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": tname, "arguments": args},
        })
        call_spans.append((sp_s, sp_e))

    if not spans_to_remove:
        cleaned = re.sub(
            rf"^\s*```[{_ZERO_WIDTH_CHARS}]*(?:tool_call|function_call)[ \t]*(?=[A-Za-z])", "", text
        ).strip()
        return cleaned, [], [], []

    _orphan_label_re = re.compile(
        rf"(?:^|(?<=\n))[ \t]*(?:(?:tool[_-]?calls?|function[_-]?calls?)[{_ZERO_WIDTH_CHARS} \t]*)+(?:\r?\n[ \t]*)*$",
        re.IGNORECASE,
    )
    spans_to_remove.sort()
    clean_parts = []
    last_pos = 0
    for s, e in spans_to_remove:
        if s < last_pos:
            continue
        seg = text[last_pos:s]
        seg = _orphan_label_re.sub("", seg)
        clean_parts.append(seg)
        last_pos = e
    tail = re.sub(
        rf"^[\r\n \t]*(?:(?:tool[_-]?calls?|function[_-]?calls?)[{_ZERO_WIDTH_CHARS} \t]*(?:\r?\n|$))+",
        "",
        text[last_pos:],
        flags=re.IGNORECASE,
    )
    clean_parts.append(tail)
    cleaned_out = re.sub(r"\n{3,}", "\n\n", "".join(clean_parts)).strip()
    return cleaned_out, tool_calls, call_spans, spans_to_remove


def parse_tool_calls(text: str, allowed_names: Optional[set] = None) -> tuple:
    """Extract fenced tool_call / function_call / json / Code snippet or bare tool-call JSON blocks."""
    cleaned_out, tool_calls, _, _ = _parse_tool_calls_internal(text, allowed_names=allowed_names)
    return cleaned_out, tool_calls


def parse_tool_calls_with_meta(text: str, allowed_names: Optional[set] = None) -> tuple:
    """Return `(cleaned_out, tool_calls, call_spans, dropped_details)` with source offsets."""
    cleaned_out, tool_calls, call_spans, spans_to_remove = _parse_tool_calls_internal(
        text, allowed_names=allowed_names
    )
    dropped_details: list[dict] = []
    if cleaned_out:
        # Collect uncovered segments with exact original coordinates for dropped-order correlation
        uncovered = []
        prev_e = 0
        for s, e in sorted(spans_to_remove):
            if s > prev_e:
                uncovered.append((prev_e, text[prev_e:s]))
            prev_e = max(prev_e, e)
        if prev_e < len(text):
            uncovered.append((prev_e, text[prev_e:]))
        for orig_off, seg in uncovered:
            if not seg.strip():
                continue
            for item in inspect_unparsed_tool_attempts_detailed(seg, allowed_names=allowed_names):
                item["src_pos"] = orig_off + int(item.get("src_pos") or 0)
                dropped_details.append(item)
        if not dropped_details:
            dropped_details = inspect_unparsed_tool_attempts_detailed(
                cleaned_out, allowed_names=allowed_names
            )
    return cleaned_out, tool_calls, call_spans, dropped_details


_UNPARSED_TOOL_HINT_RE = re.compile(
    r'(?:```(?:tool[_-]?calls?|function[_-]?calls?|json)?[^\n]*\n\s*|^[ \t]*`?(?:tool_call[ \t]+)?)'
    r'(?:\{\s*"name"\s*:\s*"([A-Za-z_][A-Za-z0-9_.-]*)"\s*,\s*"(?:arguments|args)"\s*:)',
    re.MULTILINE | re.IGNORECASE,
)
_UNPARSED_EXPLICIT_FENCE_RE = re.compile(
    rf"(?:^|\n)[ \t]*(?:`{{3,}}|~{{3,}})[{_ZERO_WIDTH_CHARS} \t]*(?:tool[_-]?calls?|function[_-]?calls?)\b[^\r\n]*\r?\n",
    re.IGNORECASE,
)
_TARGET_PATH_HINT_RE = re.compile(r'"path"\s*:\s*"([^"\r\n]{1,260})"')


def _diagnose_json_failure(snippet: str) -> str:
    """Explain precisely why a candidate tool JSON slice failed to parse."""
    s = (snippet or "").lstrip()
    if not s:
        return "Empty tool_call body"
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(s)
        if isinstance(obj, dict) and not obj.get("name"):
            return "JSON object missing required 'name' field"
        return "Tool call object failed schema/argument validation"
    except json.JSONDecodeError as exc:
        if "Expecting value" in exc.msg or exc.pos >= max(0, len(s) - 2):
            return f"Truncated/incomplete JSON ({exc.msg} at char {exc.pos})"
        return f"JSON syntax error ({exc.msg} at line {exc.lineno}, col {exc.colno})"
    except Exception as exc:
        return f"Parse error ({type(exc).__name__}: {exc})"


def inspect_unparsed_tool_attempts_detailed(
    remaining_text: str,
    allowed_names: Optional[set] = None,
) -> list[dict]:
    """Return structured diagnostics (`name`, `target_path`, `reason`, `src_pos`) for dropped blocks."""
    if not remaining_text:
        return []
    text = str(remaining_text)
    results: list[dict] = []
    seen_spans: list[tuple[int, int]] = []

    def _already_covered(pos: int) -> bool:
        return any(s <= pos < e for s, e in seen_spans)

    for m in _UNPARSED_EXPLICIT_FENCE_RE.finditer(text):
        body_start = m.end()
        fence_close = _find_explicit_fence_end(text, body_start)
        body_end = fence_close if fence_close is not None else len(text)
        body = text[body_start:body_end]
        if '"arg1"' in body and '"val1"' in body:
            continue
        nm = re.search(r'"name"\s*:\s*"([A-Za-z_][A-Za-z0-9_.-]*)"', body)
        tname = (nm.group(1) if nm else "").strip()
        if tname and tname.lower() in _PLACEHOLDER_TOOL_NAMES:
            continue
        if tname and allowed_names and tname not in allowed_names:
            continue
        pm = _TARGET_PATH_HINT_RE.search(body)
        tpath = pm.group(1).replace("\\\\", "\\") if pm else ""
        j_start = body.find("{")
        if j_start < 0:
            j_start = body.find("[")
        reason = (
            _diagnose_json_failure(body[j_start:])
            if j_start >= 0
            else "Missing JSON object inside tool_call fence"
        )
        if fence_close is None and "Truncated" not in reason:
            reason = f"Unclosed ```tool_call fence ({reason})"
        results.append({
            "name": tname or "tool_call",
            "target_path": tpath,
            "reason": reason,
            "src_pos": m.start(),
            "preview": re.sub(r"\s+", " ", body.strip())[:140],
        })
        seen_spans.append((m.start(), body_end))

    if '"name"' in text:
        for m in _UNPARSED_TOOL_HINT_RE.finditer(text):
            if _already_covered(m.start()):
                continue
            tname = (m.group(1) or "").strip()
            if not tname or tname.lower() in _PLACEHOLDER_TOOL_NAMES:
                continue
            if allowed_names and tname not in allowed_names:
                continue
            brace_off = m.group(0).rfind("{")
            json_pos = m.start() + brace_off if brace_off >= 0 else m.start()
            tail = text[json_pos:json_pos + 2400]
            fence_end = _find_explicit_fence_end(tail, 0)
            cand = tail[:fence_end] if fence_end is not None else tail
            if '"arg1"' in cand and '"val1"' in cand:
                continue
            pm = _TARGET_PATH_HINT_RE.search(cand[:600])
            tpath = pm.group(1).replace("\\\\", "\\") if pm else ""
            results.append({
                "name": tname,
                "target_path": tpath,
                "reason": _diagnose_json_failure(cand),
                "src_pos": m.start(),
                "preview": re.sub(r"\s+", " ", cand.strip())[:140],
            })
            seen_spans.append((m.start(), m.end()))

    results.sort(key=lambda item: int(item.get("src_pos") or 0))
    return results


def inspect_unparsed_tool_attempts(remaining_text: str, allowed_names: Optional[set] = None) -> list[str]:
    """Detect likely tool_call blocks that remained unparsed after parse_tool_calls()."""
    details = inspect_unparsed_tool_attempts_detailed(remaining_text, allowed_names=allowed_names)
    return [d["name"] for d in details if d.get("name") and d["name"] != "tool_call"]
