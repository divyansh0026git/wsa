"""
Core agentic loop: multi-turn conversation with tool execution.
Claude Code-style: tracks written/verified files, test detection,
round counter, verification nudge, smart history compaction.
"""
from __future__ import annotations
import base64
import ast
import inspect
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import List, Dict, Optional, Set, Tuple, Any, Callable

from .task_skills import (
    SkillAppointment,
    analyze_prompt_skills,
    normalize_skill_name,
    select_task_skills,
    skill_guidance,
    skill_read_directive,
)
from .session import SessionState, is_provider_thread_url
from .client import WSAClient, ProviderLimitError, PromptTooLongError
from .visual_status import VisualReviewTracker, describe_capture_viewports
from .tools import (
    execute_tool,
    set_project_root,
    set_bridge_endpoint,
    set_active_file_ledger,
    set_strict_read_before_edit,
    TOOL_DEFINITIONS,
    _custom_tools,
    consume_pending_images,
    detect_image_info,
    truncate_text_with_line_range,
    begin_edit_checkpoint,
    commit_edit_checkpoint,
    _describe_omitted_lines,
    validate_and_normalize_tool_call,
    split_tool_control_args,
)
from .context import (
    capture_user_decisions,
    classify_task_complexity,
    FileContextLedger,
)
from .prompts import build_dynamic_system_prompt
from . import renderer as R
from .runtime import serialized_turn
from .permissions import approval_reason, request_approval, write_scope_error

try:
    from wsa_server.tools import (
        _PLACEHOLDER_TOOL_NAMES as _SRV_PLACEHOLDER_TOOL_NAMES,
        _repair_tool_json_text as _srv_repair_tool_json_text,
        inspect_unparsed_tool_attempts as _srv_inspect_unparsed_tool_attempts,
        inspect_unparsed_tool_attempts_detailed as _srv_inspect_unparsed_tool_attempts_detailed,
        parse_tool_calls_with_meta as _srv_parse_tool_calls_with_meta,
    )
except Exception:  # pragma: no cover - fallback when server package unavailable
    _SRV_PLACEHOLDER_TOOL_NAMES = frozenset({
        "func_name",
        "action_name",
        "tool_name",
        "function_name",
        "example_tool",
        "your_tool_name",
    })
    _srv_repair_tool_json_text = None
    _srv_inspect_unparsed_tool_attempts = None
    _srv_inspect_unparsed_tool_attempts_detailed = None
    _srv_parse_tool_calls_with_meta = None

try:
    from wsa_server.multimodal import is_claude_workflow_refusal as _srv_is_claude_workflow_refusal
except Exception:  # pragma: no cover - fallback when server package unavailable
    def _srv_is_claude_workflow_refusal(_text: Optional[str]) -> bool:
        return False

_ZW_RE = "\u200b\u200c\u200d\ufeff"


MAX_TOOL_ROUNDS = 500   # safety cap per user turn

MAX_WEB_PROMPT_CHARS = 96_000  # Safe ceiling for Gemini/Claude web bridge multi-round tool loops
CHATGPT_WEB_PROMPT_CHARS = 21_500  # Tighter budget for ChatGPT Web's composer ceiling
DEEPSEEK_WEB_PROMPT_CHARS = 18_000  # Keep full ~10.6KB system prompt + ~7.4KB user/tool carryover within 26KB server budget


def _max_web_prompt_chars_for_model(model: Optional[str]) -> int:
    """Return per-provider message-history character ceiling suited for `model`.

    Cross-provider multiagent failover must compact to the *next* agent's
    composer ceiling before transferring context, otherwise ChatGPT/DeepSeek
    reject the wrapped session outright (silent composer limit exceeded).
    """
    m = str(model or "").strip().lower()
    if "chatgpt" in m or "openai" in m or re.match(r"^(?:gpt|o[134])\b", m):
        return CHATGPT_WEB_PROMPT_CHARS
    if "deepseek" in m:
        return DEEPSEEK_WEB_PROMPT_CHARS
    return MAX_WEB_PROMPT_CHARS

_IMAGE_EXT_RE = r"\.(?:png|jpe?g|webp|gif|bmp)"
_QUOTED_OR_AT_IMG_RE = re.compile(
    rf'@?(["\'])([^"\'\r\n]+{_IMAGE_EXT_RE})\1'
    rf'|@([^\s"\'\r\n]+{_IMAGE_EXT_RE})'
    rf'|((?:[A-Za-z]:[\\/]|\\\\|\.\.?[\\/])[^\r\n"\'<>|]+?{_IMAGE_EXT_RE})(?=\s|$|[,;:\)\]])'
    rf'|((?:[^\s"\'\r\n<>|]+[\\/])*[^\s"\'\r\n<>|]+{_IMAGE_EXT_RE})(?=\s|$|[,;:\)\]])',
    re.IGNORECASE,
)


def _content_text_len(content: Any) -> int:
    """Estimate character length of message content without inflating base64 image URLs."""
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    total += len(str(part.get("text") or ""))
                elif part.get("type") in ("image_url", "input_image", "image"):
                    total += 64
            elif isinstance(part, str):
                total += len(part)
        return total
    return len(str(content or ""))


def _compact_head_tail_with_lines(content: str, head_n: int = 15, tail_n: int = 5, call_hint: str = "") -> str:
    """Compact tool output to head + tail lines while reporting exact omitted line numbers."""
    lines = content.splitlines()
    if len(lines) <= head_n + tail_n:
        return (
            truncate_text_with_line_range(
                content,
                max_chars=max(512, min(1200, len(content) // 2)),
                label="",
                notice_verb="Compacted",
                extra_hint=call_hint,
            )
        )
    omit_start = head_n
    omit_end = len(lines) - tail_n
    omit_count = omit_end - omit_start
    range_desc = _describe_omitted_lines(lines, omit_start, omit_end)
    preview = "\n".join(lines[:head_n])
    tail = "\n".join(lines[-tail_n:])
    suffix = f". {call_hint}" if call_hint else ""
    return (
        f"{preview}\n"
        f"... [Compacted {range_desc} omitted ({omit_count:,} lines, {len(content):,} chars){suffix}] ...\n"
        f"{tail}"
    )


def _messages_total_chars(messages: List[Dict]) -> int:
    return sum(
        _content_text_len(m.get("content")) + len(json.dumps(m.get("tool_calls") or []))
        for m in messages
    )


def _compact_tool_messages_proportionally(
    messages: List[Dict],
    limit: int,
    *,
    min_floor: int,
    skip_floor: int,
    notice_verb: str,
) -> int:
    """Proportionally compact oversized tool outputs (and older non-active turns if still needed) under `limit`."""
    compacted = 0
    total_chars = _messages_total_chars(messages)
    if total_chars <= limit:
        return 0
    oversized = [
        idx
        for idx in range(1, len(messages))
        if messages[idx].get("role") == "tool" and len(str(messages[idx].get("content") or "")) > skip_floor
    ]
    if oversized:
        oversized_set = set(oversized)
        other_chars = sum(
            _content_text_len(m.get("content")) + len(json.dumps(m.get("tool_calls") or []))
            for idx, m in enumerate(messages)
            if idx not in oversized_set
        )
        avail = max(min_floor * len(oversized), limit - other_chars - 256)
        per_cap = max(min_floor, avail // len(oversized))
        for idx in oversized:
            content = str(messages[idx].get("content") or "")
            if len(content) > per_cap:
                messages[idx]["content"] = truncate_text_with_line_range(
                    content,
                    max_chars=per_cap,
                    label="Output",
                    notice_verb=notice_verb,
                )
                compacted += 1

    # Sequential cleanup if non-oversized items or floor push total slightly above `limit`.
    for idx in range(1, len(messages)):
        excess = _messages_total_chars(messages) - limit
        if excess <= 0:
            break
        message = messages[idx]
        if message.get("role") != "tool":
            continue
        content = str(message.get("content") or "")
        if len(content) <= min_floor:
            continue
        allowed = max(min_floor, len(content) - excess - 200)
        if allowed < len(content):
            message["content"] = truncate_text_with_line_range(
                content,
                max_chars=allowed,
                label="Output",
                notice_verb=notice_verb,
            )
            compacted += 1

    # Last resort: compact oversized prior user/assistant prose (never index 0 system or final user message).
    if _messages_total_chars(messages) > limit and len(messages) > 3:
        last_user_idx = next(
            (i for i in range(len(messages) - 1, 0, -1) if messages[i].get("role") == "user"),
            len(messages) - 1,
        )
        for idx in range(1, len(messages) - 1):
            if idx == last_user_idx:
                continue
            excess = _messages_total_chars(messages) - limit
            if excess <= 0:
                break
            message = messages[idx]
            role = message.get("role")
            if role not in ("assistant", "user") or not isinstance(message.get("content"), str):
                continue
            content = message["content"]
            if len(content) <= 900:
                continue
            allowed = max(600, len(content) - excess - 150)
            message["content"] = truncate_text_with_line_range(
                content,
                max_chars=allowed,
                label="Prior message",
                notice_verb=notice_verb,
            )
            compacted += 1
    return compacted


def _compact_historical_slice(
    messages: List[Dict],
    end_idx: int,
    *,
    head_n: int,
    tail_n: int,
    old_thresh: int,
) -> int:
    """Compact historical tool outputs and oversized tool_call arguments within `messages[1:end_idx]`."""
    compacted = 0
    for idx in range(1, max(1, end_idx)):
        m = messages[idx]
        if m.get("role") == "tool":
            content = str(m.get("content") or "")
            if len(content) > old_thresh and "[Compacted" not in content:
                t_name = m.get("name", "tool")
                m["content"] = _compact_head_tail_with_lines(
                    content,
                    head_n=head_n,
                    tail_n=tail_n,
                    call_hint=f"Call {t_name} again if needed",
                )
                compacted += 1
        elif m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", "")
                if isinstance(raw_args, str) and len(raw_args) > 500:
                    try:
                        parsed = json.loads(raw_args)
                        if isinstance(parsed, dict):
                            slim = {k: (v[:120] + "...[compacted]" if isinstance(v, str) and len(v) > 150 else v) for k, v in parsed.items()}
                            fn["arguments"] = json.dumps(slim, ensure_ascii=False)
                            compacted += 1
                    except Exception:
                        pass
    return compacted


def _compact_messages_for_web_limit(
    messages: List[Dict],
    keep_recent_tools: int = 10,
    force: bool = False,
    *,
    max_chars: int = MAX_WEB_PROMPT_CHARS,
    delta_mode: bool = False,
) -> int:
    """Compact older tool outputs AND older assistant tool_call args when total chars exceed `max_chars`.

    When `delta_mode` is True (active persistent web chat where the server only transmits unread
    messages after the last assistant turn), historical turns are compacted for failover hygiene
    while `max_chars` budgets ONLY the unread trailing delta slice so fresh `read_file` outputs
    are not crushed to stubs by prior-round byte counts.
    """
    limit = max(4_000, int(max_chars or MAX_WEB_PROMPT_CHARS))
    total_chars = _messages_total_chars(messages)
    if not force and total_chars <= limit:
        return 0
    tight_mode = limit < 40_000
    head_n = 8 if tight_mode else 15
    tail_n = 3 if tight_mode else 5
    old_thresh = 800 if tight_mode else 1500
    notice_verb = f"trimmed for {max(1, limit // 1000)}KB limit"
    if force and keep_recent_tools == 0:
        min_floor, skip_floor = 380, 480
    elif tight_mode:
        min_floor, skip_floor = 512, 750
    else:
        min_floor, skip_floor = 1500, 2500

    if delta_mode and not force:
        last_asst_idx = next(
            (i for i in range(len(messages) - 1, 0, -1) if messages[i].get("role") == "assistant"),
            None,
        )
        if last_asst_idx is not None and last_asst_idx + 1 < len(messages):
            delta_start = last_asst_idx + 1
            compacted = _compact_historical_slice(
                messages, delta_start, head_n=head_n, tail_n=tail_n, old_thresh=old_thresh
            )
            delta_slice = messages[delta_start:]
            if _messages_total_chars(delta_slice) > limit:
                compacted += _compact_tool_messages_proportionally(
                    [{"role": "system", "content": ""}, *delta_slice],
                    limit,
                    min_floor=min_floor,
                    skip_floor=skip_floor,
                    notice_verb=notice_verb,
                )
            return compacted

    cutoff = max(1, len(messages) - (keep_recent_tools * 2 + 1))
    compacted = _compact_historical_slice(
        messages, cutoff, head_n=head_n, tail_n=tail_n, old_thresh=old_thresh
    )
    for tighter_keep in (6, 4, 2, 1):
        current_chars = _messages_total_chars(messages)
        if current_chars <= limit and not force:
            break
        tight_cutoff = max(1, len(messages) - (tighter_keep * 2 + 1))
        for idx in range(1, tight_cutoff):
            message = messages[idx]
            if message.get("role") != "tool":
                continue
            content = str(message.get("content") or "")
            if len(content) > 300 and "[Compacted" not in content:
                message["content"] = _compact_head_tail_with_lines(content, head_n=head_n, tail_n=tail_n)
                compacted += 1
    compacted += _compact_tool_messages_proportionally(
        messages,
        limit,
        min_floor=min_floor,
        skip_floor=skip_floor,
        notice_verb=notice_verb,
    )
    return compacted


def _trim_oversized_user_messages(messages: List[Dict], max_user_chars: int) -> int:
    """Compact oversized plain-text user messages (e.g. large subagent handoffs) to fit composer limits."""
    cap = max(1_800, int(max_user_chars))
    trimmed = 0
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= cap:
            continue
        marker = "\n\n... [Compacted middle context to fit web composer limit] ...\n\n"
        keep = max(800, cap - len(marker))
        head_len = int(keep * 0.58)
        tail_len = max(300, keep - head_len)
        msg["content"] = content[:head_len].rstrip() + marker + content[-tail_len:].lstrip()
        trimmed += 1
    return trimmed


def _trim_messages_for_prompt_too_long(messages: List[Dict], retry_num: int, model: Optional[str]) -> int:
    """Aggressively compact tool outputs and evict oldest completed turns after PromptTooLongError."""
    scale = (0.72, 0.48, 0.30)[max(0, min(2, retry_num - 1))]
    base_budget = _max_web_prompt_chars_for_model(model)
    prev_chars = _messages_total_chars(messages)
    retry_budget = max(2_500, int(min(base_budget, prev_chars) * scale))
    _compact_messages_for_web_limit(
        messages,
        keep_recent_tools=0,
        force=True,
        max_chars=retry_budget,
    )
    first_non_sys = 1 if (messages and messages[0].get("role") == "system") else 0
    last_user_idx = next(
        (i for i in range(len(messages) - 1, first_non_sys - 1, -1) if messages[i].get("role") == "user"),
        len(messages) - 1,
    )
    while (
        last_user_idx > first_non_sys
        and (_messages_total_chars(messages) > retry_budget or _messages_total_chars(messages) >= prev_chars)
    ):
        del messages[first_non_sys]
        last_user_idx -= 1
        while last_user_idx > first_non_sys and messages[first_non_sys].get("role") in ("assistant", "tool"):
            del messages[first_non_sys]
            last_user_idx -= 1
    return retry_budget



def _build_system_message(state: SessionState) -> Dict:
    from .tools import execute_tool, get_dynamic_rules, load_custom_tools
    if not getattr(state, "_workspace_snapshot", None):
        try:
            state._workspace_snapshot = execute_tool("list_dir", {"path": ".", "recursive": False})
            entries = [
                line.strip().split("  (", 1)[0]
                for line in state._workspace_snapshot.splitlines()
                if line.strip()
            ]
            state._workspace_snapshot = ", ".join(entries[:25])
            if len(entries) > 25:
                state._workspace_snapshot += f" (+{len(entries) - 25} more)"
        except Exception:
            state._workspace_snapshot = "(unavailable)"

    rules = get_dynamic_rules()
    rules_block = ""
    if rules:
        rules_lines = "\n".join(f"  • [{k}]: {v}" for k, v in rules.items())
        rules_block = f"\n── ACTIVE DYNAMIC RULES (MUST FOLLOW) ──\n{rules_lines}\n"

    custom_tools = load_custom_tools()
    custom_block = ""
    if custom_tools:
        ct_lines = "\n".join(f"  • {k}: {m.get('description', '')}" for k, m in custom_tools.items())
        custom_block = f"\n── REGISTERED CUSTOM TOOLS ──\n{ct_lines}\n"

    mem_text = execute_tool("recall", {})
    mem_block = (
        f"Persistent agent notes:\n{mem_text}\n"
        if "[Memory is empty" not in mem_text
        else ""
    )
    digest = getattr(state, "context_summary", "")
    digest_block = f"Context transfer digest:\n{digest}\n" if digest else ""
    _td = getattr(state, "turn_digest", None) or []
    if _td:
        _td_lines = []
        for _entry in _td[-4:]:
            _g = " | ".join(str(g)[:80] for g in (_entry.get("goals") or [])[-2:])
            _f = ", ".join(str(f) for f in (_entry.get("written") or [])[:4])
            _td_lines.append(f"  \u2022 {_g}" + (f" \u2192 {_f}" if _f else ""))
        turn_digest_block = "Prior sessions (compact history):\n" + "\n".join(_td_lines) + "\n"
    else:
        turn_digest_block = ""
    written = sorted(getattr(state, "written_files", ()) or ())
    verified = sorted(getattr(state, "verified_files", ()) or ())
    file_progress = ""
    if written:
        file_progress += f"Files modified in session: {', '.join(written[:12])}\n"
    if verified:
        file_progress += f"Files verified/read in session: {', '.join(verified[:12])}\n"
    is_claude = getattr(state, "model", "gemini") == "claude"
    acct_lbl = (getattr(state, "active_account_label", "") or "").strip()
    acct_email = (getattr(state, "active_account_email", "") or "").strip()
    acct_browser = (getattr(state, "active_browser", "") or "").strip()
    acct_bits = [b for b in (acct_lbl, acct_email, acct_browser) if b]
    acct_block = (
        f"Active single account: {' · '.join(dict.fromkeys(acct_bits))}\n"
        if acct_bits and not is_claude
        else ""
    )
    self_repo_hint = (
        "Active codebase: wsa_agent/ (TUI agent), wsa_server/ (proxy server), "
        "and wsa-browser-bridge/ (browser relay).\n"
        if (not is_claude and (state.project_root / "wsa_server" / "__init__.py").is_file())
        else ""
    )
    ledger = getattr(state, "file_ledger", None)
    ws_digest = ledger.render_working_set_digest(max_chars=2400) if isinstance(ledger, FileContextLedger) else ""
    ws_block = f"Working-set file map (lines, sha, read ranges, symbols):\n{ws_digest}\n" if ws_digest else ""
    if ws_block and digest_block:
        # The summary embeds a frozen copy of the working-set map; the live map above supersedes it.
        digest_block = digest_block.split("Working-Set File Map:", 1)[0].rstrip("\n") + "\n"
    decisions = getattr(state, "decisions", None) or []
    dec_block = (
        "Pinned user constraints & decisions:\n" + "\n".join(f"  • {d}" for d in decisions[-8:]) + "\n"
        if decisions
        else ""
    )
    plan_items = getattr(state, "plan_items", None) or []
    plan_block = ""
    if plan_items:
        p_lines = [
            f"  - [{str(it.get('status') or 'pending').upper()}] {it.get('title') or it.get('id') or ''}"
            for it in plan_items[:8]
        ]
        plan_block = "Active plan progress:\n" + "\n".join(p_lines) + "\n"

    perm_mode = getattr(state, "permission_mode", "auto")
    if is_claude:
        meta_lines = f"Project: {state.project_root} ({state.project_type})\n"
        if perm_mode == "strict":
            meta_lines += "Permission mode: strict\n"
        tail_lines = f"Workspace root entries: {state._workspace_snapshot}\n"
    else:
        meta_lines = (
            f"Project: {state.project_root} ({state.project_type})\n"
            f"Model: {getattr(state, 'model', 'gemini')} ({getattr(state, 'active_site_name', 'gemini.google.com')})\n"
            f"Permission mode: {perm_mode}\n"
        )
        tail_lines = (
            f"Workspace root entries: {state._workspace_snapshot}\n"
            f"Date/Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )

    return {
        "role": "system",
        "content": (
            build_dynamic_system_prompt(state)
            + "\n── SESSION CONTEXT ──\n"
            + meta_lines
            + acct_block
            + file_progress
            + dec_block
            + plan_block
            + ws_block
            + digest_block
            + turn_digest_block
            + self_repo_hint
            + tail_lines
            + mem_block
            + rules_block
            + custom_block
        ),
    }


def _ensure_unique_tool_call_ids(tool_calls: List[dict], round_num: int = 0) -> List[dict]:
    """Guarantee every tool call in a batch carries a distinct non-empty call ID."""
    seen: Set[str] = set()
    counts: Dict[str, int] = {}
    for tc in tool_calls:
        cid = str(tc.get("id") or "").strip()
        if cid:
            counts[cid] = counts.get(cid, 0) + 1
    normalized: List[dict] = []
    for idx, tc in enumerate(tool_calls):
        item = dict(tc)
        cid = str(item.get("id") or "").strip()
        if cid and counts[cid] > 1:
            item["_wsa_ambiguous_id"] = cid
        if not cid or cid in seen:
            base = cid or f"call_r{round_num}_{idx}"
            candidate = f"{base}_{idx}"
            bump = idx
            while candidate in seen or candidate in counts:
                bump += 1
                candidate = f"{base}_{bump}"
            cid = candidate
        item["id"] = cid
        seen.add(cid)
        normalized.append(item)
    return normalized


def _extract_tool_batch_plan(
    response: dict, allowed_names: Optional[Set[str]] = None
) -> Tuple[List[dict], str, List[int], List[dict]]:
    """Extract tool calls via shared parser, preserving source offsets and dropped-block details."""
    tc_list = list(response.get("tool_calls") or [])
    content = response.get("content") or ""
    if not content:
        calls = _ensure_unique_tool_call_ids(tc_list)
        return calls, "", [-1] * len(calls), []

    if _srv_parse_tool_calls_with_meta is not None:
        rem_text, parsed_calls, call_spans, dropped_details = _srv_parse_tool_calls_with_meta(
            content, allowed_names=allowed_names
        )
        calls: List[dict] = []
        parsed_pos: List[int] = []
        base_idx = len(tc_list)
        for i, tc in enumerate(parsed_calls):
            item = dict(tc)
            item["id"] = f"call_txt_{base_idx + i}"
            calls.append(item)
            sp = call_spans[i][0] if i < len(call_spans) else -1
            parsed_pos.append(sp)
        if not tc_list:
            calls = _ensure_unique_tool_call_ids(calls)
            return calls, rem_text, parsed_pos, dropped_details
        seen = set()
        merged = list(tc_list)
        merged_pos = [-1] * len(tc_list)
        for tc in tc_list:
            fn = tc.get("function", {})
            raw_a = fn.get("arguments", "{}")
            try:
                canon_a = json.dumps(json.loads(raw_a) if isinstance(raw_a, str) else raw_a, sort_keys=True)
            except Exception:
                canon_a = str(raw_a)
            seen.add((str(fn.get("name", "")).strip(), canon_a))
        for i, tc in enumerate(calls):
            fn = tc.get("function", {})
            raw_a = fn.get("arguments", "{}")
            try:
                canon_a = json.dumps(json.loads(raw_a) if isinstance(raw_a, str) else raw_a, sort_keys=True)
            except Exception:
                canon_a = str(raw_a)
            sig = (str(fn.get("name", "")).strip(), canon_a)
            if sig not in seen:
                seen.add(sig)
                merged.append(tc)
                merged_pos.append(parsed_pos[i] if i < len(parsed_pos) else -1)
        merged = _ensure_unique_tool_call_ids(merged)
        return merged, rem_text, merged_pos, dropped_details

    calls_fb, rem_fb = _extract_tool_calls_detailed_fallback(response, allowed_names=allowed_names)
    calls_fb = _ensure_unique_tool_call_ids(calls_fb)
    return calls_fb, rem_fb, [-1] * len(calls_fb), []


def _extract_tool_calls(response: dict, allowed_names: Optional[Set[str]] = None) -> List[dict]:
    """
    Extract tool calls from structured response tool_calls and text-block fallbacks.
    """
    calls, _ = _extract_tool_calls_detailed(response, allowed_names=allowed_names)
    return calls


def _extract_tool_calls_detailed(
    response: dict, allowed_names: Optional[Set[str]] = None
) -> Tuple[List[dict], str]:
    """
    Extract tool calls from structured response tool_calls and text-block fallbacks,
    returning `(calls, remaining_unparsed_text)` so partial dropped blocks can be detected.
    """
    calls, rem, _, _ = _extract_tool_batch_plan(response, allowed_names=allowed_names)
    return calls, rem


def _extract_tool_calls_detailed_fallback(
    response: dict, allowed_names: Optional[Set[str]] = None
) -> Tuple[List[dict], str]:
    """Standalone fallback parser used only when wsa_server.tools is unavailable."""
    import re

    tc_list = list(response.get("tool_calls") or [])
    content = response.get("content") or ""
    if not content:
        return tc_list, ""

    calls = []
    decoder = json.JSONDecoder()
    tool_name_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")

    def _valid_item(item, strict: bool, check_allowed: bool) -> bool:
        if not isinstance(item, dict):
            return False
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            return False
        clean_name = name.strip()
        if clean_name.lower() in _SRV_PLACEHOLDER_TOOL_NAMES:
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
        if not tool_name_re.match(clean_name):
            return False
        if check_allowed and allowed_names and clean_name not in allowed_names:
            return False
        if isinstance(raw_args, dict):
            return True
        return isinstance(raw_args, str) and raw_args.strip().startswith("{")

    def _to_openai_call(item: dict, idx_offset: int) -> dict:
        name = str(item.get("name", "")).strip()
        args = item.get("arguments", item.get("args", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                pass
        return {
            "id": f"call_txt_{idx_offset}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args) if isinstance(args, dict) else args,
            },
        }

    open_re = re.compile(
        rf"(?P<explicit>(?:(?:^|(?<=\n))[ \t]*)?(?:`{{3,}}|~{{3,}})[{_ZW_RE} \t]*(?:tool[_-]?calls?|function[_-]?calls?)(?:\b[^\r\n]*)?\r?\n)"
        rf"|(?:^|(?<=\n))[ \t]*(?:"
        rf"(?P<snippet>Code snippet)[ \t]*\r?\n"
        rf"|(?P<generic>(?:`{{3,}}|~{{3,}})[{_ZW_RE} \t]*[A-Za-z0-9_+.-]*[ \t]*\r?\n)"
        rf")"
    )
    close_re = re.compile(
        rf"[ \t]*(?:\r?\n[ \t]*)?(?:`{{3,}}|~{{3,}})(?:[{_ZW_RE} \t]*(?:_\s*[\]\*]|_\])?)?(?=[ \t]*(?:\r?\n|$))"
        rf"|\s*(?:`{{3,}}|~{{3,}})(?:[{_ZW_RE} \t]*(?:_\s*[\]\*]|_\])?)?"
    )
    bare_re = re.compile(
        r"(?:^|(?<=\n))[ \t]*(?P<bt>`?)(?:tool_call[ \t]+)?(?=\{|\[\s*\{)",
        re.IGNORECASE,
    )

    def _raw_decode_with_repair(src: str, pos: int) -> Tuple[Any, int]:
        try:
            return decoder.raw_decode(src, pos)
        except (json.JSONDecodeError, TypeError):
            if _srv_repair_tool_json_text is None or pos >= len(src) or src[pos] not in "{[":
                raise
            from wsa_server.tools import _extract_balanced_json_span
            bal_end = _extract_balanced_json_span(src, pos)
            if bal_end is None:
                raise
            return json.loads(_srv_repair_tool_json_text(src[pos:bal_end])), bal_end

    consumed_spans = []
    cursor = 0
    while cursor < len(content):
        m = open_re.search(content, cursor)
        if not m:
            break
        is_generic = bool(m.group("generic"))
        idx = m.end()
        last_end = idx
        block_items = []
        valid_block = True
        while idx < len(content):
            while idx < len(content) and content[idx] in " \t\r\n,":
                idx += 1
            if idx >= len(content) or content.startswith("```", idx) or content.startswith("~~~", idx):
                break
            if content[idx] not in "{[":
                if is_generic:
                    valid_block = False
                break
            try:
                data, end_idx = _raw_decode_with_repair(content, idx)
            except (json.JSONDecodeError, TypeError, ValueError):
                if is_generic:
                    valid_block = False
                break
            items = data if isinstance(data, list) else [data]
            good = [it for it in items if _valid_item(it, strict=is_generic, check_allowed=False)]
            if is_generic and (not good or len(good) != len(items)):
                valid_block = False
                break
            block_items.extend(good)
            idx = end_idx
            last_end = end_idx

        if valid_block and block_items:
            for it in block_items:
                calls.append(_to_openai_call(it, len(tc_list) + len(calls)))
            cm = close_re.match(content[last_end:])
            end_pos = last_end + (cm.end() if cm else 0)
            consumed_spans.append((m.start(), end_pos))
            cursor = end_pos
        else:
            cursor = m.end()

    # Pass 2: bare / 4-space indented JSON tool-call lines outside matched fenced spans
    uncovered = []
    prev_end = 0
    for s, e in consumed_spans:
        if s > prev_end:
            uncovered.append((prev_end, s))
        prev_end = max(prev_end, e)
    if prev_end < len(content):
        uncovered.append((prev_end, len(content)))

    bare_consumed = []
    for seg_start, seg_end in uncovered:
        seg_cur = seg_start
        while seg_cur < seg_end:
            bm = bare_re.search(content, seg_cur, seg_end)
            if not bm:
                break
            bt = bm.group("bt")
            j_start = bm.end()
            try:
                data, end_idx = _raw_decode_with_repair(content, j_start)
            except (json.JSONDecodeError, TypeError, ValueError):
                seg_cur = j_start + 1
                continue
            items = data if isinstance(data, list) else [data]
            if (
                items
                and all(_valid_item(it, strict=True, check_allowed=bool(allowed_names)) for it in items)
                and end_idx <= seg_end
            ):
                tail = content[end_idx:seg_end]
                if bt:
                    bt_close = re.match(r"[ \t]*`[ \t]*(?=\r?\n|$)", tail)
                    if bt_close:
                        span_end = end_idx + bt_close.end()
                        for it in items:
                            calls.append(_to_openai_call(it, len(tc_list) + len(calls)))
                        bare_consumed.append((bm.start(), span_end))
                        seg_cur = span_end
                        continue
                else:
                    cm = close_re.match(tail)
                    if cm or re.match(r"[ \t]*(?:\r?\n|$)", tail):
                        span_end = end_idx + (cm.end() if cm else 0)
                        for it in items:
                            calls.append(_to_openai_call(it, len(tc_list) + len(calls)))
                        bare_consumed.append((bm.start(), span_end))
                        seg_cur = span_end
                        continue
            seg_cur = j_start + 1

    all_consumed = sorted(consumed_spans + bare_consumed)
    rem_parts: List[str] = []
    rem_prev = 0
    for s, e in all_consumed:
        if s > rem_prev:
            rem_parts.append(content[rem_prev:s])
        rem_prev = max(rem_prev, e)
    if rem_prev < len(content):
        rem_parts.append(content[rem_prev:])
    remaining_text = "\n".join(p for p in rem_parts if p.strip())

    if not tc_list:
        return calls, remaining_text

    # If structured tool_calls already came from the server, append any missed fallback calls without duplicating.
    seen = set()
    for tc in tc_list:
        fn = tc.get("function", {})
        raw_a = fn.get("arguments", "{}")
        try:
            canon_a = json.dumps(json.loads(raw_a) if isinstance(raw_a, str) else raw_a, sort_keys=True)
        except Exception:
            canon_a = str(raw_a)
        seen.add((str(fn.get("name", "")).strip(), canon_a))

    merged = list(tc_list)
    for tc in calls:
        fn = tc.get("function", {})
        raw_a = fn.get("arguments", "{}")
        try:
            canon_a = json.dumps(json.loads(raw_a) if isinstance(raw_a, str) else raw_a, sort_keys=True)
        except Exception:
            canon_a = str(raw_a)
        sig = (str(fn.get("name", "")).strip(), canon_a)
        if sig not in seen:
            seen.add(sig)
            merged.append(tc)
    return merged, remaining_text


def _active_tool_names() -> Set[str]:
    names: Set[str] = set()
    for t in TOOL_DEFINITIONS:
        if isinstance(t, dict):
            n = (t.get("function") or t).get("name")
            if n:
                names.add(str(n).strip())
    names.update(str(k).strip() for k in _custom_tools.keys() if k)
    return names


def _looks_like_broken_tool_attempt(content: Optional[str], allowed_names: Optional[Set[str]] = None) -> bool:
    """Detect truncated or malformed tool-call attempts at response end so agent can auto-continue."""
    if not isinstance(content, str) or not content.strip():
        return False
    import re
    tail = content.rstrip()
    if re.search(r"```_\][ \t]*$", tail):
        return True
    # Match an unclosed ```tool_call / ```function_call block at the tail of the response
    unclosed = re.search(
        rf"(?:^|\n| )(?P<ticks>`{{3,}}|~{{3,}})[{_ZW_RE} \t]*(?:tool[_-]?calls?|function[_-]?calls?)\b[^\r\n]*\r?\n(?P<body>[\s\S]*)$",
        tail,
    )
    if unclosed and not re.search(r"(?:^|\n)[ \t]*(?:`{3,}|~{3,})[ \t]*(?:\r?\n|$)", unclosed.group("body")):
        return True
    # Match truncated JSON object at EOF for a known tool name (unbalanced braces)
    m = re.search(
        r'(?:^|\n)[ \t]*(?:(?:`{3,}|~{3,})[^\r\n]*\r?\n[ \t]*|`?(?:tool_call[ \t]+)?)?\{\s*"name"\s*:\s*"([A-Za-z_][A-Za-z0-9_.-]*)"\s*,\s*"arguments"\s*:[\s\S]*$',
        tail[-600:],
    )
    if m and m.group(1).lower() not in _SRV_PLACEHOLDER_TOOL_NAMES and (not allowed_names or m.group(1) in allowed_names):
        return True
    return False


def _check_tests_exist(project_root) -> bool:
    """Quick check if the project has any test files (skipping .venv/node_modules/cache dirs)."""
    from pathlib import Path
    from .tools import _effective_ignore_dirs

    root = Path(project_root)
    if (root / "tests").is_dir() or (root / "test").is_dir():
        return True
    ignore_dirs = _effective_ignore_dirs(root)
    for _, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
        for fname in filenames:
            if (fname.startswith("test_") or fname.endswith("_test.py")) and fname.endswith(".py"):
                return True
    return False


def _read_windows_clipboard_image() -> Optional[bytes]:
    """Capture a bitmap/PNG image from the Windows clipboard when user types @screenshot / @clipboard."""
    if sys.platform != "win32":
        return None
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "$img = [System.Windows.Forms.Clipboard]::GetImage(); "
        "if ($null -eq $img) { exit 2 }; "
        "$ms = New-Object System.IO.MemoryStream; "
        "$img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png); "
        "[Convert]::ToBase64String($ms.ToArray())"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=6,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return base64.b64decode(proc.stdout.strip(), validate=True)
    except Exception:
        pass
    return None


def _build_image_part(raw: bytes, filename: str, ext: str = "") -> Optional[Tuple[Dict, Dict]]:
    """Encode image bytes (<8MB) into an OpenAI-compatible image_url part + UI summary."""
    if not raw or len(raw) > 8 * 1024 * 1024:
        return None
    info = detect_image_info(raw, ext)
    if not info:
        return None
    mime = str(info["mime"])
    b64 = base64.b64encode(raw).decode("ascii")
    part = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
    meta = {
        "filename": filename,
        "format": str(info["format"]),
        "width": int(info["width"]),
        "height": int(info["height"]),
        "bytes": len(raw),
    }
    return part, meta


def _prepare_user_turn_content(user_input: str, project_root: Path) -> Tuple[Any, List[Dict]]:
    """Detect local screenshot/image paths (or @screenshot/@clipboard) and build multimodal content when present."""
    text = str(user_input or "")
    parts: List[Dict] = []
    attached: List[Dict] = []
    seen_paths: Set[str] = set()
    root = Path(project_root)

    if re.search(r"(?:^|\s)@(?:screenshot|clipboard)(?=\s|$)", text, re.IGNORECASE):
        clip_raw = _read_windows_clipboard_image()
        if clip_raw:
            built = _build_image_part(clip_raw, "clipboard_screenshot.png", ".png")
            if built:
                part, meta = built
                parts.append(part)
                attached.append(meta)

    for m in _QUOTED_OR_AT_IMG_RE.finditer(text):
        if len(parts) >= 4:
            break
        raw_cand = next((g for g in m.groups()[1:] if g), "")
        cand_clean = raw_cand.strip()
        if not cand_clean:
            continue
        p = Path(cand_clean).expanduser()
        resolved = p.resolve() if p.is_absolute() else (root / p).resolve()
        key = str(resolved).lower()
        if key in seen_paths or not resolved.is_file():
            continue
        seen_paths.add(key)
        try:
            img_bytes = resolved.read_bytes()
        except Exception:
            continue
        built = _build_image_part(img_bytes, resolved.name, resolved.suffix)
        if built:
            part, meta = built
            parts.append(part)
            attached.append(meta)

    if not parts:
        return user_input, []
    return [{"type": "text", "text": text}, *parts], attached


_DEFERRED_TAIL_RE = re.compile(
    r"\b(?:"
    r"next,?\s+(?:i\s+will|i(?:'|’)ll|let\s+me)"
    r"|now\s+(?:i\s+will|i(?:'|’)ll|let\s+me)"
    r"|let\s+me\s+now"
    r"|i\s+will\s+now\s+(?:read|inspect|search|check|edit|patch|update|implement|fix|run|test)"
    r"|i(?:'|’)ll\s+now\s+(?:read|inspect|search|check|edit|patch|update|implement|fix|run|test)"
    r"|let(?:'s|s| me)\s+(?:inspect|read|check)\s+(?:the|those|these|remaining)"
    r"|let\s+me\s+(?:emit|call|run|read|batch|do|start)"
    r"|let(?:'s|s)\s+(?:do|run|start|batch|read|emit)"
    r"|i\s+must\s+emit"
    r"|actually\s+batch"
    r")\b",
    re.IGNORECASE,
)
_FAKE_TOOL_REQUEST_RE = re.compile(
    r"(?:"
    r"\[Tool\s+execution\s+requested\]"
    r"|\b(?:are|is)\s+in\s+flight\b"
    r"|\b(?:have|has)\s+been\s+requested\s+(?:through|via)\s+wsa\b"
    r"|\bno\s+files\s+have\s+been\s+modified\s+yet\b"
    r"|\bthe\s+next\s+step\s+is\s+to\s+(?:apply|edit|patch|implement|fix|write)\b"
    r"|\bi(?:'|’)ll\s+apply\s+(?:the\s+)?(?:changes|edits|fixes|patches)\s+(?:only\s+)?after\b"
    r"|\bafter\s+the\s+[^\n.]{1,80}\s+results\s+return\b"
    r")",
    re.IGNORECASE,
)


def _looks_like_fake_tool_request(content: Optional[str]) -> bool:
    """Detect prose claiming tools were requested / in-flight when no fenced tool_call block was emitted."""
    return bool(isinstance(content, str) and content.strip() and _FAKE_TOOL_REQUEST_RE.search(content))
_LAZY_PUNT_RE = re.compile(
    r"\b(?:"
    r"remaining\s+files\s+follow\s+the\s+same\s+pattern"
    r"|you\s+can\s+(?:now\s+)?apply\s+this\s+to\s+the\s+remaining"
    r"|shall\s+i\s+proceed\s+with\s+(?:applying|implementing|updating|patching)"
    r"|would\s+you\s+like\s+me\s+to\s+(?:apply|implement|proceed|patch|fix)"
    r"|let\s+me\s+know\s+if\s+you\s+want\s+me\s+to\s+(?:apply|implement|proceed|fix)"
    r")\b",
    re.IGNORECASE,
)
_PROSE_STUB_RE = re.compile(
    r"(?:#|//|/\*)\s*(?:\.\.\.|…)\s*(?:existing code|rest of (?:file|code)|remaining|unchanged)",
    re.IGNORECASE,
)
_DONE_SUMMARY_RE = re.compile(
    r"\b(?:all\s+(?:fixes|changes|edits|tests)\s+(?:are\s+)?(?:complete|done|applied|verified|passed)|everything\s+is\s+(?:fixed|verified|complete))\b",
    re.IGNORECASE,
)
_BUDGET_HALLUCINATION_RE = re.compile(
    r"(?:"
    r"(?:tool(?:[- ]?(?:call|execution))?|session|turn)\s+budget\b[^\n.]{0,60}\b(?:exhausted|depleted|reached|exceeded|used\s+up|at\s+0|is\s+0)"
    r"|\b(?:0|zero|no)\s+(?:tool\s+)?calls?\s+(?:remaining|left|available)\b"
    r"|\b(?:no\s+more|out\s+of)\s+tool\s+calls?\b"
    r"|\btool\s+execution\s+is\s+unavailable\b"
    r")",
    re.IGNORECASE,
)


def _looks_like_budget_hallucination(content: Optional[str]) -> bool:
    """Detect false claims by web models that WSA's tool execution budget hit 0 calls remaining."""
    return bool(isinstance(content, str) and content.strip() and _BUDGET_HALLUCINATION_RE.search(content))


_TOOL_UNAVAILABLE_MISCONCEPTION_RE = re.compile(
    r"(?:"
    r"(?:wsa|local(?:[- ]workspace)?|workspace|file)\s+tools?\b[^\n.]{0,80}\b(?:not\s+(?:exposed|available|connected|enabled)|unavailable|disconnected)"
    r"|\bblocked:\s*read_skill\s+unavailable\b"
    r"|\breconnect\s+the\s+wsa\b[^\n.]{0,50}\bsession\b"
    r"|\bnot\s+exposed\s+in\s+(?:my|this)\s+(?:current\s+)?execution\s+environment\b"
    r")",
    re.IGNORECASE,
)


def _looks_like_tool_unavailable_misconception(content: Optional[str]) -> bool:
    """Detect web reasoning models confusing WSA's text-parsed tool_call bridge with missing native sandbox tools."""
    return bool(isinstance(content, str) and content.strip() and _TOOL_UNAVAILABLE_MISCONCEPTION_RE.search(content))


_NON_VERIFY_EXTS = {".md", ".txt", ".rst", ".csv", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_UI_VERIFY_EXTS = {".html", ".htm", ".css", ".js", ".ts", ".jsx", ".tsx", ".svelte", ".vue"}
_BUDGET_NUDGE_MARKERS = ("there is no 0-call cap here", "`wsa` parses plain markdown")


def _is_budget_nudge_text(content: Optional[Any]) -> bool:
    """True if content is a loop continuation note countering a 0-call cap or bridge misconception claim."""
    if not isinstance(content, str):
        return False
    low = content.lower()
    return any(marker in low for marker in _BUDGET_NUDGE_MARKERS)


def _has_renderable_ui_entry(project_root: Optional[Path], ui_written: List[str]) -> bool:
    """True when the workspace has a renderable HTML/component UI surface for visual_inspect."""
    if project_root is None:
        return True
    if any(Path(p).suffix.lower() in {".html", ".htm", ".jsx", ".tsx", ".vue", ".svelte"} for p in ui_written):
        return True
    root = Path(project_root)
    idx_file = root / "index.html"
    if idx_file.is_file() and not (root / "dist" / "index.html").is_file():
        try:
            html_txt = idx_file.read_text(encoding="utf-8", errors="ignore")
            ts_m = re.search(r"""<script\b[^>]*\bsrc=["']([^"']+\.tsx?)["']""", html_txt, re.I)
            if ts_m:
                ts_rel = ts_m.group(1).strip().lstrip("/").lstrip("./")
                if not (root / ts_rel).is_file():
                    return False
        except OSError:
            pass
    for rel in ("index.html", "public/index.html", "dist/index.html", "src/index.html", "templates/index.html"):
        if (root / rel).is_file():
            return True
    try:
        if any(root.glob("*.html")):
            return True
    except OSError:
        pass
    try:
        from .tools import _BG_PROCS
        if _BG_PROCS:
            return True
    except Exception:
        pass
    return False


def _canonical_tool_sig(name: str, raw_args: Any) -> str:
    """Return a deterministic signature for intra-batch tool deduplication."""
    clean_name = str(name or "").strip()
    parsed = raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except Exception:
            return f"{clean_name}:{raw_args.strip()}"
    if isinstance(parsed, dict):
        try:
            return f"{clean_name}:{json.dumps(parsed, sort_keys=True, ensure_ascii=False, separators=(',', ':'))}"
        except Exception:
            pass
    return f"{clean_name}:{str(parsed or '').strip()}"


def _deduplicate_tool_calls(tool_calls: List[dict]) -> Tuple[List[dict], int]:
    """Coalesce repeated reads only until the next mutation, preserving dependencies."""
    for tc in tool_calls:
        raw = (tc.get("function") or {}).get("arguments", {})
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            continue
        if isinstance(args, dict) and any(key in args for key in ("depends_on", "_depends_on")):
            return list(tool_calls), 0
    seen: Set[str] = set()
    unique: List[dict] = []
    dropped = 0
    for tc in tool_calls:
        fn = tc.get("function") or {}
        if fn.get("name") not in _BATCH_READ_ONLY_TOOLS:
            seen.clear()
            unique.append(tc)
            continue
        sig = _canonical_tool_sig(str(fn.get("name") or ""), fn.get("arguments", "{}"))
        if sig in seen:
            dropped += 1
            continue
        seen.add(sig)
        unique.append(tc)
    return unique, dropped


def _sanitize_assistant_tool_prose(content: Optional[str], has_tool_calls: bool) -> Optional[str]:
    """Strip zero-width/DOM fence leaks and cap bloated premature prose when tools run."""
    if not isinstance(content, str):
        return content
    cleaned = re.sub(rf"[{_ZW_RE}]", "", content)
    cleaned = re.sub(r"((?:`{3,}|~{3,}))[ \t]*(?:_\s*[\]\*]|_\])+[ \t]*(?=\r?\n|$)", r"\1", cleaned)
    cleaned = re.sub(
        r"(?im)^[ \t]*(?:(?:tool[_-]?calls?|function[_-]?calls?)[ \t]*)+(?=(?:\r?\n)*[ \t]*(?:`{3,}|~{3,}|<tool_call|<<TOOL_CALL>>))",
        "",
        cleaned,
    )
    cleaned = re.sub(r"(?:\r?\n)?[ \t]*_\s*[\]\*]+\s*$", "", cleaned).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if not has_tool_calls:
        return re.sub(r"(?im)^[ \t]*(?:(?:tool[_-]?calls?|function[_-]?calls?)[ \t]*)+(?:\r?\n|$)", "", cleaned).strip()

    non_tool_prose = re.sub(
        r"(?:`{3,}|~{3,})[ \t]*(?:tool[_-]?calls?|function[_-]?calls?)\b[\s\S]*?(?:`{3,}|~{3,})",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    if len(non_tool_prose) > 1200:
        return (
            non_tool_prose[:400].rstrip()
            + "\n... [Premature assistant prose truncated prior to tool results]"
        )
    return cleaned


def _condense_single_tool_output(name: str, content: str, max_chars: int = 1200) -> str:
    """Condense completed-turn tool output while keeping exit codes, diffs, errors, and signatures."""
    text = str(content or "")
    if len(text) <= max_chars:
        return text
    if name == "read_skill":
        keep_lines: List[str] = []
        used = 0
        for raw_ln in text.splitlines():
            s = raw_ln.strip()
            if not s:
                continue
            if (
                s.startswith(("[OK]", "===", "##", "-", "*", "1.", "2.", "3.", "4.", "5.", "6.", "7.", "8."))
                or "IRON LAW" in s.upper()
                or "NEVER" in s
                or "ALWAYS" in s
            ):
                if used + len(raw_ln) + 1 > max_chars - 70:
                    break
                keep_lines.append(raw_ln)
                used += len(raw_ln) + 1
        if keep_lines:
            keep_lines.append("... [Skill rules compacted; call read_skill(name=..., section=...) if needed]")
            return "\n".join(keep_lines)
    lines = text.splitlines()
    picked: List[str] = []
    seen_idx: Set[int] = set()

    def _add(idx: int) -> None:
        if 0 <= idx < len(lines) and idx not in seen_idx:
            seen_idx.add(idx)
            picked.append(lines[idx])

    head_n = 5 if name in ("run_command", "run_python") else 8
    tail_n = 7 if name in ("run_command", "run_python") else 4
    for i in range(min(head_n, len(lines))):
        _add(i)

    key_re = re.compile(
        r"(?:Exit code:|Traceback|Error|Exception|FAILED|AssertionError|SyntaxError|"
        r"^@@\s|^\+\+\+\s|^---\s|^File\s|Lines?\s+\d+|^\s*\d+\s*\|\s*(?:async\s+)?(?:def|class|function|export|interface|type)\b)",
        re.IGNORECASE,
    )
    for i, ln in enumerate(lines):
        if len(picked) >= 24:
            break
        if key_re.search(ln):
            _add(i)

    for i in range(max(0, len(lines) - tail_n), len(lines)):
        _add(i)

    # Sort selected indices in original appearance order, marking omitted line gaps explicitly
    sorted_idx = sorted(seen_idx)
    ordered_lines: List[str] = []
    prev_idx = -1
    for idx in sorted_idx:
        if prev_idx >= 0 and idx - prev_idx > 1:
            gap_desc = _describe_omitted_lines(lines, prev_idx + 1, idx)
            gap_count = idx - prev_idx - 1
            ordered_lines.append(f"... [{gap_desc} omitted ({gap_count} lines)] ...")
        ordered_lines.append(lines[idx])
        prev_idx = idx
    body = "\n".join(ordered_lines)
    if len(body) > max_chars:
        body = body[: max_chars - 60].rstrip()
    return body + "\n... [Completed turn tool output condensed preserving key lines]"


def _command_passed(result: str) -> bool:
    """True when a command/script result did not return a non-zero Exit code."""
    text = str(result or "")
    # run_tests owns its leading summary; command/Python tools own the trailing
    # footer. Never let an earlier stdout line impersonate the actual exit code.
    header = re.match(r"Status: (?:PASS|FAIL|NO TESTS)\nFramework: [^\n]+\nSummary: [^\n]+\nExit code: (-?\d+)", text)
    codes = re.findall(r"^Exit code:\s*(-?\d+)\s*$", text, re.M)
    if header or codes:
        return int(header.group(1) if header else codes[-1]) == 0
    return True


_ERR_RESULT_PREFIXES = (
    "[Error]",
    "[Validation Error]",
    "[Blocked]",
    "[Permission",
    "[Syntax Error",
    "[Self-Preservation",
    "[Safety Error]",
    "[Scope Error]",
    "[Missing Dependency]",
)


def _is_error_result_text(result: str) -> bool:
    return str(result or "").startswith(_ERR_RESULT_PREFIXES)


def _is_verification_command(name: str, args: Dict[str, Any], result: str) -> bool:
    """True only when a passing command actually ran tests, syntax checks, builds, or code checks."""
    text = str(result or "")
    if not _command_passed(text):
        return False
    if _is_error_result_text(text):
        return False
    if "traceback (most recent call last):" in text.lower():
        return False

    if name == "run_tests":
        return "Status: NO TESTS" not in text

    if name == "run_python":
        code = str(args.get("code") or "").strip()
        if not code:
            return False
        if re.match(r"^(?:import\s+time\s*[;\n]\s*)?(?:time\.sleep\([^)]*\)|pass)\s*$", code):
            return False
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return False
        # Printing a success message is not a verification check. Imports provide
        # a basic module smoke check; assertions/tests provide stronger evidence.
        return any(isinstance(node, (ast.Assert, ast.Import, ast.ImportFrom)) for node in ast.walk(tree))

    if name == "run_command":
        if isinstance(args.get("argv"), list) and args.get("argv"):
            cmd = " ".join(str(x) for x in args["argv"]).strip()
        else:
            cmd = str(args.get("command") or "").strip()
        if not cmd:
            return False
        if re.match(r"^\s*(?:echo|printf|Write-Output|Write-Host)\b", cmd, re.I):
            return False
        low_cmd = cmd.lower()
        runner_re = re.compile(
            r"\b(?:pytest|unittest|py_compile|compileall|mypy|pyright|ruff|flake8|"
            r"vitest|jest|playwright|tsc|eslint|phpunit|rspec|ctest)\b"
            r"|\bcargo\s+(?:test|check|clippy|build)\b"
            r"|\bgo\s+(?:test|vet|build)\b"
            r"|\bdotnet\s+(?:test|build)\b"
            r"|\bphp\s+-l\b"
            r"|\bcomposer\s+(?:test|check)\b"
            r"|\bbundle\s+exec\s+(?:rspec|rake)\b|\brake\s+test\b|\bruby\s+-c\b"
            r"|\bmix\s+(?:test|compile)\b"
            r"|\b(?:dart|flutter)\s+(?:test|analyze)\b"
            r"|\bswift\s+(?:test|build)\b"
            r"|\b(?:mvn|mvnw|gradle|gradlew)(?:\.cmd|\.bat)?\s+[^\n]*\b(?:test|check|compile|verify)\b"
            r"|\bzig\s+build(?:\s+test)?\b"
            r"|\bcmake\s+--build\b"
            r"|\bnode\s+(?:--check|-c\b|-e\b|\S+\.(?:m?js|cjs))"
            r"|\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|build|lint|typecheck|check)\b"
            r"|\bnpx\s+\S+"
            r"|(?:python(?:\.exe)?|py)\s+(?:-m\s+|-c\s+|\S+\.py)",
            re.IGNORECASE,
        )
        non_verify_re = re.compile(
            r"(?:^|[;&|]\s*)(?:Get-CimInstance|Get-Process|tasklist|taskkill|Stop-Process|"
            r"Start-Process|Start-Sleep|sleep|ps|ss|netstat|lsof|dir|ls|pwd|Get-ChildItem|"
            r"cd|mkdir|rmdir|rm|del|Remove-Item|echo|type|cat|Get-Content|ping|"
            r"git\s+(?:status|log|diff|branch))\b",
            re.IGNORECASE,
        )
        if not runner_re.search(cmd):
            return False
        # Do not scan inline `-e` / `-c` script bodies (or structured argv runner invocations)
        # for shell utility names like `;ps` or `;type` that appear as JS/Python identifiers.
        has_argv_runner = bool(
            isinstance(args.get("argv"), list)
            and args.get("argv")
            and re.match(
                r"^(?:node|python3?|py|pytest|cargo|go|dotnet|php|composer|ruby|bundle|rake|mix|dart|flutter|swift|mvn|mvnw|gradle|gradlew|zig|cmake|ctest|tsc|vitest|jest|npm|pnpm|yarn|bun|npx)(?:\.(?:exe|cmd|bat))?$",
                Path(str(args["argv"][0])).name,
                re.IGNORECASE,
            )
        )
        shell_probe = re.sub(r"""(?:\bnode\s+-e|\b(?:python3?|py)\s+-c)\s+[\s\S]*$""", "", cmd, flags=re.IGNORECASE)
        if not has_argv_runner and non_verify_re.search(shell_probe) and not re.search(
            r"\b(?:pytest|unittest|py_compile|compileall|mypy|pyright|ruff|flake8|vitest|jest|node\s+(?:--check|-e)|python3?\s+-c|tsc|cargo\s+(?:test|check|clippy)|go\s+(?:test|vet)|dotnet\s+(?:test|build)|php\s+-l|phpunit|rspec|mix\s+test|dart\s+(?:test|analyze)|swift\s+test|zig\s+build|ctest)\b",
            low_cmd,
        ):
            return False
        if "pytest" in low_cmd and ("no tests ran" in text.lower() or "collected 0 items" in text.lower()):
            return False
        return True

    return False


_BATCH_MUTATING_TOOLS = frozenset({
    "write_file", "patch_file", "delete_file", "rollback",
    "create_custom_tool", "restart_service", "start_background",
    "move_file", "copy_file", "git", "apply_patch",
})
_BATCH_READ_ONLY_TOOLS = frozenset({
    "read_file", "get_file_info", "get_file_outline", "inspect_image",
    "glob_files", "list_dir", "search_files", "recall",
    "list_custom_tools", "list_dynamic_rules", "list_background",
    "todo", "wait_for", "web_fetch", "detect_toolchain",
})
_READ_REPEAT_TOOLS = frozenset({
    "read_file", "search_files", "glob_files", "list_dir", "get_file_outline", "get_file_info",
})
_PROJECT_CHECK_CMD_RE = re.compile(
    r"\b(?:pytest|unittest|py_compile|compileall|mypy|pyright|ruff|flake8|"
    r"vitest|jest|playwright|tsc|eslint|phpunit|rspec|ctest|"
    r"cargo\s+(?:test|check|clippy|build)|go\s+(?:test|vet|build)|dotnet\s+(?:test|build)|"
    r"mix\s+(?:test|compile)|(?:dart|flutter)\s+(?:test|analyze)|swift\s+(?:test|build)|zig\s+build)\b"
    r"|\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|build|lint|typecheck|check)\b",
    re.IGNORECASE,
)


def _detect_dropped_tool_details(
    content: Optional[str],
    remaining_text: str,
    extracted_calls: List[dict],
    allowed_names: Optional[Set[str]],
    preparsed_details: Optional[List[dict]] = None,
) -> List[dict]:
    """Return structured diagnostics (`name`, `target_path`, `reason`, `src_pos`) for dropped blocks."""
    if preparsed_details:
        return list(preparsed_details)
    rem = str(remaining_text or "").strip()
    if rem:
        if _srv_inspect_unparsed_tool_attempts_detailed is not None:
            try:
                details = _srv_inspect_unparsed_tool_attempts_detailed(rem, allowed_names=allowed_names)
                if details:
                    return list(details)
            except Exception:
                pass
        if _srv_inspect_unparsed_tool_attempts is not None:
            try:
                names = _srv_inspect_unparsed_tool_attempts(rem, allowed_names=allowed_names)
                if names:
                    return [
                        {"name": n, "target_path": "", "reason": "Could not parse tool_call JSON", "src_pos": -1}
                        for n in names
                    ]
            except Exception:
                pass
    if not extracted_calls and _looks_like_broken_tool_attempt(content, allowed_names):
        return [{
            "name": "tool_call",
            "target_path": "",
            "reason": "Malformed or unclosed tool_call block",
            "src_pos": 0,
        }]
    return []


def _norm_rel_path_token(raw_path: str) -> str:
    p = str(raw_path or "").strip().replace("\\", "/")
    if not p:
        return ""
    try:
        from .tools import _resolve, _rel_posix
        p = _rel_posix(_resolve(p))
        return p.casefold() if sys.platform == "win32" else p
    except (OSError, ValueError):
        pass
    while p.startswith("./"):
        p = p[2:]
    return p


def _extract_inv_target_paths(name: str, args: Dict[str, Any]) -> Set[str]:
    paths: Set[str] = set()
    if isinstance(args, dict):
        for key in ("path", "new_path"):
            p = args.get(key)
            if isinstance(p, str) and p.strip():
                paths.add(_norm_rel_path_token(p))
        multi = args.get("paths")
        if isinstance(multi, list):
            for item in multi:
                if isinstance(item, str) and item.strip():
                    paths.add(_norm_rel_path_token(item))
        edits = args.get("edits")
        if isinstance(edits, list):
            for item in edits:
                if isinstance(item, dict):
                    ep = item.get("path")
                    if isinstance(ep, str) and ep.strip():
                        paths.add(_norm_rel_path_token(ep))
    return paths


def _rel_tool_path(raw_path: Any, project_root: Path) -> str:
    """Resolve a tool path argument relative to project_root with forward slashes."""
    if not raw_path:
        return ""
    try:
        from .tools import _resolve
        return str(_resolve(raw_path).relative_to(project_root)).replace("\\", "/")
    except Exception:
        return str(raw_path).replace("\\", "/")


def _extract_mutated_and_written_paths(
    name: str, args: Dict[str, Any], project_root: Path
) -> Tuple[List[str], List[str]]:
    """Return (mutated_paths, written_paths) touched by a succeeded file-mutating tool call."""
    src = _rel_tool_path(args.get("path", ""), project_root)
    dst = _rel_tool_path(args.get("new_path", ""), project_root)
    if name in ("write_file", "patch_file"):
        return ([src] if src else []), ([src] if src else [])
    if name == "apply_patch":
        paths = [
            _rel_tool_path(ed.get("path", ""), project_root)
            for ed in (args.get("edits") or ())
            if isinstance(ed, dict)
        ]
        clean = [p for p in paths if p]
        return clean, list(clean)
    if name == "move_file":
        return [p for p in (src, dst) if p], ([dst] if dst else [])
    if name == "copy_file":
        return ([dst] if dst else []), ([dst] if dst else [])
    if name == "delete_file":
        return ([src] if src else []), []
    return [], []


def _is_inv_dependent_on_path(name: str, args: Dict[str, Any], target_path: str) -> bool:
    norm_p = _norm_rel_path_token(target_path)
    if not norm_p:
        return False
    base = Path(norm_p).name
    stem = Path(norm_p).stem if norm_p.endswith(".py") else ""

    if name in (
        "read_file",
        "write_file",
        "patch_file",
        "apply_patch",
        "delete_file",
        "move_file",
        "copy_file",
        "get_file_info",
        "get_file_outline",
        "inspect_image",
    ):
        c_paths = _extract_inv_target_paths(name, args)
        return any(cp == norm_p or (base and Path(cp).name == base and "/" not in norm_p) for cp in c_paths)

    if name == "run_tests":
        t_path = _norm_rel_path_token(str(args.get("path") or ""))
        if not t_path or t_path == ".":
            return True
        return t_path == norm_p or norm_p.startswith(t_path.rstrip("/") + "/")

    if name == "run_command":
        cmd = " ".join(str(x) for x in args["argv"]) if isinstance(args.get("argv"), list) else str(args.get("command") or "")
        win_p = norm_p.replace("/", "\\")
        if norm_p in cmd or win_p in cmd:
            return True
        if base and re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(base)}(?![A-Za-z0-9_.-])", cmd):
            return True
        if stem and re.search(rf"\b{re.escape(stem)}\b", cmd) and re.search(r"\b(?:python|pytest|unittest|mypy|ruff)\b", cmd, re.I):
            return True
        return bool(_PROJECT_CHECK_CMD_RE.search(cmd))

    if name == "run_python":
        code = str(args.get("code") or "")
        if norm_p in code or (base and base in code):
            return True
        if stem and re.search(rf"\b(?:import|from)\s+{re.escape(stem)}\b", code):
            return True
    return False


def _find_blocked_prerequisite(
    inv: Dict[str, Any],
    dropped_details: List[dict],
    prior_invs: List[Dict[str, Any]],
) -> Optional[str]:
    """Gate dependent batch operations when an earlier prerequisite dropped or failed."""
    cname = str(inv.get("name") or "")
    cargs = inv.get("args") or {}
    cdeps = set(inv.get("explicit_depends_on") or [])
    cpos = int(inv.get("src_pos", -1))

    # Explicit prerequisites must identify an already completed call. Never run
    # on an unknown, forward, cyclic, or ambiguous reference.
    failures = {"validation_error", "scope_error", "permission_denied", "error",
                "blocked", "timed_out", "safety_blocked", "validated"}
    for dep in sorted(cdeps):
        if any(p.get("ambiguous_id") == dep for p in prior_invs):
            return f"Prerequisite '{dep}' refers to a duplicated call ID; emit unique IDs"
        matches = [p for p in prior_invs if str(p.get("call_id") or "") == dep]
        if not matches:
            matches = [p for p in prior_invs if str(p.get("name") or "") == dep]
        if not matches and dep.isdigit():
            # Numeric references are one-based positions; IDs take precedence.
            matches = [p for p in prior_invs if str(int(p.get("seq", -1)) + 1) == dep]
        if len(matches) != 1:
            return f"Prerequisite '{dep}' is unresolved or ambiguous; reference an earlier unique call ID"
        prerequisite = matches[0]
        if prerequisite.get("status") in failures:
            return f"Prerequisite '{dep}' did not succeed ({prerequisite.get('reason') or prerequisite.get('status')})"

    # 1. Earlier batch invocations that failed, timed out, were rejected, or were themselves blocked
    for p in prior_invs:
        if p.get("status") not in ("validation_error", "scope_error", "permission_denied", "error", "blocked", "timed_out", "safety_blocked"):
            continue
        pname = str(p.get("name") or "")
        if cdeps and (
            str(p.get("call_id") or "") in cdeps
            or str(p.get("seq", "")) in cdeps
            or str(int(p.get("seq", 0)) + 1) in cdeps
            or pname in cdeps
        ):
            return f"Prerequisite '{pname}' ({p.get('call_id')}) did not succeed ({p.get('reason') or p.get('status')})"
        if pname in _BATCH_MUTATING_TOOLS:
            for tp in p.get("target_paths") or ():
                if _is_inv_dependent_on_path(cname, cargs, tp):
                    return f"Prerequisite '{pname}' on '{tp}' failed ({p.get('status')}: {p.get('reason') or 'error'})"
            if pname == "create_custom_tool" and cname == str((p.get("args") or {}).get("name") or ""):
                return f"Prerequisite custom tool '{cname}' failed to register"

    # 2. Dropped/unparsed blocks from the same assistant response appearing before this call
    for d in dropped_details:
        dpos = int(d.get("src_pos", -1))
        if dpos >= 0 and cpos >= 0 and dpos > cpos:
            continue  # Broken block came strictly AFTER this call in the response
        dname = str(d.get("name") or "tool_call")
        dreason = str(d.get("reason") or "malformed JSON")
        dpath = _norm_rel_path_token(str(d.get("target_path") or ""))
        if cdeps and dname in cdeps:
            return f"Prerequisite '{dname}' was dropped before execution ({dreason})"
        if dname in _BATCH_MUTATING_TOOLS or dname == "tool_call":
            if dpath:
                if _is_inv_dependent_on_path(cname, cargs, dpath):
                    return f"Earlier '{dname}' targeting '{dpath}' failed to parse ({dreason})"
            elif cname not in _BATCH_READ_ONLY_TOOLS:
                cmd_str = " ".join(str(x) for x in cargs["argv"]) if isinstance(cargs.get("argv"), list) else str(cargs.get("command") or "")
                is_harmless_git_ro = cname == "run_command" and bool(re.match(r"^\s*git\s+(?:status|log|branch|rev-parse)\b", cmd_str, re.I))
                if not is_harmless_git_ro:
                    return f"Earlier '{dname}' block failed to parse ({dreason})"

    return None


def _parse_exec_status_from_result(result: str, is_err: bool) -> Tuple[str, str]:
    text = str(result or "")
    test_summary = re.match(r"Status: (PASS|FAIL|NO TESTS)\nFramework: [^\n]+\nSummary: [^\n]+\nExit code: (-?\d+)", text)
    if test_summary:
        if test_summary.group(1) == "NO TESTS":
            return "completed", "no_tests"
        return "completed", "ok" if test_summary.group(1) == "PASS" and int(test_summary.group(2)) == 0 else "non_zero_exit"
    statuses = re.findall(r"^Status:\s*([A-Za-z_]+)\s*\(([^)\s—-]+)", text, re.MULTILINE)
    if statuses:
        return statuses[-1]
    if text.startswith("[Validation Error]"):
        return "rejected", "validation_error"
    if text.startswith("[Blocked]"):
        return "blocked", "dependency_blocked"
    if text.startswith("[Scope Error]"):
        return "rejected", "scope_error"
    if text.startswith("[Permission"):
        return "rejected", "permission_denied"
    if text.startswith("[Syntax Error"):
        return "completed", "syntax_error"
    if text.startswith(("[Self-Preservation", "[Safety Error]", "[Recursive Agent Guard]")):
        return "rejected", "safety_blocked"
    if text.startswith("[Error]"):
        return "failed", "error"
    return ("failed", "error") if is_err else ("completed", "ok")


def _maybe_anti_laziness_nudge(
    *,
    content: Optional[str],
    written_this_turn: Set[str],
    verified_after_last_edit: bool,
    tests_exist: Optional[bool],
    round_num: int,
    verify_nudges_used: int,
    continue_nudges_used: int,
    free_mode: bool,
    browser_verified_after_ui_edit: bool = True,
    ui_verify_nudges_used: int = 0,
    requires_security_pass: bool = False,
    security_verified_after_edit: bool = True,
    security_nudges_used: int = 0,
    project_root: Optional[Path] = None,
    missing_appointed_skills: Tuple[str, ...] = (),
    tool_calls_count: int = 0,
    defer_visual_gate: bool = False,
) -> Optional[Tuple[str, str]]:
    """Return `(kind, continuation_prompt)` if work is unverified/incomplete; never loop after verified completion."""
    if free_mode:
        return None

    code_written = [p for p in sorted(written_this_turn) if Path(p).suffix.lower() not in _NON_VERIFY_EXTS]
    ui_written = [p for p in code_written if Path(p).suffix.lower() in _UI_VERIFY_EXTS]
    if (
        requires_security_pass
        and code_written
        and not security_verified_after_edit
        and security_nudges_used < 1
    ):
        sec_targets = ", ".join(code_written[:8])
        return (
            "security_fix",
            f"[Website Security Fixing Gate]: You built/modified `{sec_targets}`. Before finishing, "
            "call `read_skill(name=\"security-audit,semgrep-guardrails\")` first, audit these files for real "
            "XSS/DOM-injection (`innerHTML`), unsafe links (`noopener`), CSRF/CORS/CSP, auth/IDOR, injection, "
            "input validation, and secret-exposure flaws, apply surgical `patch_file` fixes for any confirmed "
            "issues, and re-verify cleanly.",
        )
    if code_written and not verified_after_last_edit and verify_nudges_used < 1:
        targets = ", ".join(code_written[:6])
        if tests_exist:
            return (
                "verify",
                f"[System Verification Gate]: You edited `{targets}` without running a passing check after your final edit. "
                "Run `python -m pytest -x -q` (and any relevant syntax/runtime check via `run_command` or `run_python`) "
                "now, fix any failure, and then report your verified final summary.",
            )
        verify_hint = (
            "`py_compile`, `node --check`, or targeted runtime checks"
            if defer_visual_gate
            else "`py_compile`, `node --check`, `browser_inspect`, `visual_inspect`, or targeted checks"
        )
        return (
            "verify",
            f"[System Verification Gate]: You edited `{targets}` without running a passing check after your final edit. "
            f"Run a thorough verification command ({verify_hint}) "
            "now so all edits are confirmed working before finishing.",
        )

    if (
        ui_written
        and not defer_visual_gate
        and not browser_verified_after_ui_edit
        and ui_verify_nudges_used < 1
        and verify_nudges_used < 1
        and _has_renderable_ui_entry(project_root, ui_written)
    ):
        ui_targets = ", ".join(ui_written[:6])
        return (
            "ui_verify",
            f"[UI Visual & Interaction Gate]: You edited frontend UI file(s) `{ui_targets}` without verifying rendered "
            "layout or interactive controls. Run `visual_inspect` (background headless full laptop/desktop 1440x900 view with "
            "Set-of-Mark badges and telemetry) or `browser_inspect` / `browser_interact` now to verify zero overflow, "
            "occlusion, blank renders, or console errors before concluding.",
        )

    if not isinstance(content, str) or not content.strip() or continue_nudges_used >= 2:
        return None

    has_fake_tool_req = _looks_like_fake_tool_request(content)
    if has_fake_tool_req:
        return (
            "continue",
            "[Continue Execution — Missing Tool Blocks]: Your last reply stated tool actions were "
            "requested/in flight without emitting any fenced ```tool_call JSON blocks, so WSA executed nothing. "
            "Emit the actual ```tool_call JSON block(s) now (`read_file` / `patch_file` / `write_file` / `visual_inspect`) "
            "and stop right after the closing ```.",
        )

    work_verified = bool(not code_written or verified_after_last_edit)
    non_empty_lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    tail_line = non_empty_lines[-1][-240:] if non_empty_lines else ""
    if work_verified and _DONE_SUMMARY_RE.search(tail_line):
        return None

    has_deferred = bool(_DEFERRED_TAIL_RE.search(tail_line) or _DEFERRED_TAIL_RE.search(content[-400:]))
    truncated_skill_thought = bool(
        missing_appointed_skills
        and tool_calls_count == 0
        and tail_line
        and not re.search(r'[.!?"\'\)\]`:]\s*$', tail_line)
    )
    # Only flag "would you like me to..." punts when code work is NOT yet verified/completed
    has_punt = bool(
        (round_num > 0 or bool(written_this_turn))
        and (
            ("remaining files follow the same pattern" in content.lower())
            or ("you can apply this to the remaining" in content.lower())
            or (not work_verified and _LAZY_PUNT_RE.search(content))
        )
    )
    has_stub = bool(round_num > 0 and _PROSE_STUB_RE.search(content))
    if has_deferred or has_punt or has_stub or truncated_skill_thought:
        if missing_appointed_skills and tool_calls_count == 0:
            skills_csv = ",".join(missing_appointed_skills[:8])
            return (
                "continue",
                f'Please emit the required ```tool_call JSON block(s) now — start by calling `read_skill(name="{skills_csv}")` and inspecting/implementing the workspace files.',
            )
        return (
            "continue",
            "[Continue Execution]: Do not stop at plans, deferred promises, placeholders, or confirmation prompts. "
            "Emit ```tool_call block(s) now to read remaining targets, apply complete surgical edits with `patch_file`, "
            "and run verification until every requirement is finished on disk.",
        )
    return None


class _QuietStreamSink:
    """Minimal silent stream sink used during isolated subagent turns."""

    def __init__(self, event_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None, role: str = "subagent") -> None:
        self._thinking_chars = 0
        self._cb = event_callback
        self._role = role

    def start_thinking(self) -> None:
        pass

    def stop_thinking(self) -> None:
        pass

    def feed(self, token: str) -> None:
        pass

    def feed_thinking(self, token: str) -> None:
        self._thinking_chars += len(token or "")

    def reset_answer(self) -> None:
        pass

    def set_status_hint(self, text: str, **_kwargs) -> None:
        if self._cb and text:
            self._cb("status", {"role": self._role, "hint": text})

    def finish(self) -> None:
        pass

    def close(self) -> None:
        pass

    def get_thinking_text(self) -> str:
        return " " * self._thinking_chars if self._thinking_chars else ""


def _close_stream_ui(sr: Any) -> None:
    if sr is None:
        return
    close_fn = getattr(sr, "close", None)
    if callable(close_fn):
        close_fn()


def _set_renderer_visual_hint(
    sr: Any,
    text: str,
    *,
    with_elapsed: bool = True,
    reset_stage_clock: bool = False,
) -> None:
    """Update a StreamRenderer or compatible mock with an optional live stage timer."""
    if sr is None:
        return
    setter = getattr(sr, "set_status_hint", None)
    if not callable(setter):
        return
    try:
        setter(text, with_elapsed=with_elapsed, reset_stage_clock=reset_stage_clock)
    except TypeError:
        setter(text)


def _call_client_stream(client: Any, stream_kwargs: Dict[str, Any]) -> Any:
    """Call `client.stream(**kwargs)` while staying compatible with mock clients lacking optional kwargs."""
    stream_fn = client.stream
    call_kwargs = dict(stream_kwargs)
    optional_keys = ("on_relay_status", "client_request_id")
    if any(k in call_kwargs for k in optional_keys):
        try:
            sig = inspect.signature(stream_fn)
            has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if not has_var_kw:
                for k in optional_keys:
                    if k in call_kwargs and k not in sig.parameters:
                        call_kwargs.pop(k, None)
        except (TypeError, ValueError):
            pass
    return stream_fn(**call_kwargs)


def _classify_visual_error(exc: BaseException) -> str:
    """Return a concise sanitized error category for visual-review diagnostics."""
    if isinstance(exc, ProviderLimitError):
        return "rate limit"
    if isinstance(exc, PromptTooLongError):
        return "prompt too long"
    low = f"{type(exc).__name__} {exc}".lower()
    if "timeout" in low or isinstance(exc, TimeoutError):
        return "timeout"
    return "transport error"


def _stop_stream_ui(sr: Any) -> None:
    if sr is None:
        return
    try:
        sr.stop_thinking()
    finally:
        _close_stream_ui(sr)


def _record_tool_history(state: Any, entry: Dict[str, Any]) -> None:
    append_fn = getattr(state, "append_tool_history", None)
    if callable(append_fn):
        append_fn(entry)
        return
    history = getattr(state, "tool_history", None)
    if isinstance(history, list):
        history.append(entry)
        if len(history) > 120:
            del history[: len(history) - 120]


ACCOUNT_SWITCH_WAIT_SECS = 60


def _preserve_workflow_state_and_context(
    state: SessionState,
    *,
    partial_checkpoint_fn: Optional[Callable[..., None]] = None,
    persist_to_state: bool = True,
    pending_goal: str = "",
    record_pending_goal: bool = False,
) -> str:
    """Persist in-flight progress, ledger, plan, and compacted context on limit/abort."""
    if partial_checkpoint_fn is not None:
        try:
            partial_checkpoint_fn(partial=True)
        except Exception:
            pass
    if record_pending_goal and pending_goal and persist_to_state:
        goal_line = " ".join(str(pending_goal).split())[:300]
        if goal_line:
            cur_summary = str(getattr(state, "context_summary", "") or "")
            marker = f"[Interrupted Pending Goal]: {goal_line}"
            if marker not in cur_summary:
                state.context_summary = (
                    f"{cur_summary.rstrip()}\n{marker}".strip()
                    if cur_summary.strip()
                    else marker
                )
    if persist_to_state:
        _concepts = getattr(state, "approved_design_concepts", None) or []
        if _concepts:
            from .visual_concept import format_approved_concepts_context
            _vc_marker = "USER-APPROVED VISUAL UI CONCEPT"
            cur_sum = str(getattr(state, "context_summary", "") or "")
            if _vc_marker not in cur_sum:
                _vc_ctx = format_approved_concepts_context(_concepts, max_chars=900)
                if _vc_ctx:
                    state.context_summary = (
                        f"{cur_sum.rstrip()}\n{_vc_ctx}".strip()
                        if cur_sum.strip()
                        else _vc_ctx
                    )
    if persist_to_state and hasattr(state, "save"):
        try:
            return str(state.save() or "")
        except Exception:
            return ""
    return ""


def _reset_provider_chats_for_account_change(
    state: SessionState,
    client: Any,
    session_id: Optional[str],
    providers: List[str],
    *,
    clear_roles: bool = True,
    refresh_health: bool = True,
) -> None:
    """Clear client and server chat bindings for *providers* and sync latest account health."""
    old_urls: Set[str] = set()
    if isinstance(getattr(state, "chat_urls", None), dict):
        old_urls.update(str(v) for v in state.chat_urls.values() if v)
    if clear_roles and isinstance(getattr(state, "role_threads", None), dict):
        old_urls.update(str(v) for v in state.role_threads.values() if v)

    if hasattr(state, "_concept_synced_keys") and isinstance(state._concept_synced_keys, set):
        if not providers:
            state._concept_synced_keys.clear()
        else:
            for _p in providers:
                _pn = str(_p or "").strip().lower()
                for _sk in [k for k in list(state._concept_synced_keys) if k == _pn or k.startswith(f"{_pn}:")]:
                    state._concept_synced_keys.discard(_sk)

    if hasattr(state, "clear_provider_chats"):
        state.clear_provider_chats(providers, clear_roles=clear_roles)
    elif hasattr(state, "chat_urls") and isinstance(state.chat_urls, dict):
        for _pm in providers:
            state.chat_urls.pop(_pm, None)

    # Only invalidate URLs actually removed by this provider-scoped reset.
    # Sending every saved URL also invalidates unrelated providers server-side.
    for bindings in (getattr(state, "chat_urls", {}), getattr(state, "role_threads", {})):
        if isinstance(bindings, dict):
            old_urls.difference_update(str(v) for v in bindings.values() if v)

    if session_id and callable(getattr(client, "clear_session_chats", None)):
        try:
            client.clear_session_chats(
                session_id,
                providers=providers,
                include_roles=clear_roles,
                client_urls=sorted(old_urls),
            )
        except Exception:
            pass

    if refresh_health and callable(getattr(client, "get_health", None)) and hasattr(state, "sync_account_from_health"):
        try:
            _health = client.get_health()
            if isinstance(_health, dict) and _health:
                state.sync_account_from_health(_health)
        except Exception:
            pass


def _wait_for_account_switch_countdown(
    seconds: int = ACCOUNT_SWITCH_WAIT_SECS,
    *,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> bool:
    """Display a live second-by-second countdown while the user switches accounts."""
    total = max(0, int(seconds))
    if total <= 0:
        return True
    do_sleep = sleep_fn or time.sleep
    try:
        from .renderer import console as _console
        from rich.live import Live
        from rich.errors import LiveError
        from rich.text import Text
    except Exception:
        _console = None
        Live = None
        LiveError = Exception
        Text = None
    use_live = bool(
        _console is not None
        and Live is not None
        and Text is not None
        and getattr(_console, "is_terminal", False)
    )
    try:
        if use_live:
            def _render_timer(rem: int) -> Any:
                txt = Text("  Waiting ")
                txt.append(f"{total} secs", style=f"bold {R.Theme.WARN}")
                txt.append(" (live timer: ")
                txt.append(f"{rem}s", style=f"bold {R.Theme.ACCENT}")
                txt.append(") for you to change the current account...")
                return txt

            rem_start = total
            try:
                with Live(
                    _render_timer(total),
                    console=_console,
                    refresh_per_second=4,
                    transient=True,
                ) as live:
                    for rem in range(total, 0, -1):
                        rem_start = rem
                        live.update(_render_timer(rem), refresh=True)
                        do_sleep(1.0)
                _console.print(f"  [{R.Theme.INFO}]Retrying...[/]")
                return True
            except LiveError:
                _console.print(
                    f"  Waiting {total} secs (live timer) for you to change the current account..."
                )
                for rem in range(rem_start, 0, -1):
                    if rem == rem_start or rem <= 5 or rem % 15 == 0:
                        _console.print(f"  [{R.Theme.DIM}]{rem}s remaining...[/]")
                    do_sleep(1.0)
                _console.print(f"  [{R.Theme.INFO}]Retrying...[/]")
                return True
        if _console is not None:
            _console.print(
                f"  Waiting {total} secs (live timer) for you to change the current account..."
            )
        for rem in range(total, 0, -1):
            if _console is not None and (rem == total or rem <= 5 or rem % 15 == 0):
                _console.print(f"  [{R.Theme.DIM}]{rem}s remaining...[/]")
            do_sleep(1.0)
        if _console is not None:
            _console.print(f"  [{R.Theme.INFO}]Retrying...[/]")
        return True
    except KeyboardInterrupt:
        if _console is not None:
            _console.print()
        return False


def _prompt_all_agents_exhausted(
    state: SessionState,
    *,
    model_name: str = "",
    is_multiagent: Optional[bool] = None,
) -> bool:
    """Ask the user whether to change accounts and continue after a provider limit hit.

    This prompt is intentionally bypass-exempt from the auto-approve / bypass
    permission mode: even in `permission_mode='auto'`, we surface this decision
    so the user must acknowledge the account change before resuming. Returns
    True only when the user answers `y`/`yes`.
    """
    if is_multiagent is None:
        is_multiagent = getattr(state, "agent_mode", "singleagent") == "multiagent" and bool(
            getattr(state, "multiagent_queue", None)
        )
    try:
        from .renderer import console as _console
        R._release_console_live(_console)
        R.sync_theme()
        if is_multiagent:
            header = (
                "All queued agents reached their account limits. "
                "Please change your current account to continue the workflow: y/n"
            )
        else:
            target = model_name or getattr(state, "model", "gemini")
            header = (
                f"{R.friendly_model_name(target)} account limit reached. "
                "Please change your current account to continue the workflow: y/n"
            )
        _console.print(f"\n  {header}", markup=False)
        _console.print(
            f"  [bold {R.Theme.ACCENT}]\\[y][/] Yes — wait {ACCOUNT_SWITCH_WAIT_SECS}s to switch account in browser and resume automatically\n"
            f"  [bold {R.Theme.ACCENT}]\\[n][/] No  — stop now (workflow state & context are already preserved)"
        )
        while True:
            raw = _console.input("  Choice [y/n]: ", markup=False).strip().lower().strip("[](). ")
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no", ""):
                return False
            R.print_warn("Choose y (yes) or n (no).")
    except (EOFError, KeyboardInterrupt):
        return False
    except Exception:
        return False


def _offer_limit_multiagent(state: SessionState, client: Any, failed_model: str) -> str:
    """Offer failover queue, single-agent model switch, account retry, or stop after a provider limit."""
    from .renderer import console
    from .session import MODEL_CATALOG, normalize_web_model, get_model_info
    R._release_console_live(console)
    R.sync_theme()
    norm_failed = normalize_web_model(failed_model)
    if not norm_failed:
        raw_fm = str(failed_model or "").strip().lower()
        for cand in ("claude", "gemini", "chatgpt", "deepseek", "venice"):
            if cand in raw_fm:
                norm_failed = cand
                break
    norm_failed = norm_failed or getattr(state, "model", "") or "gemini"

    while True:
        try:
            console.print(
                "\n  How would you like to continue after this provider limit?",
                markup=False,
            )
            console.print(
                f"  [bold {R.Theme.ACCENT}]\\[a][/] Default queue  — switch to multiagent mode using the default failover queue\n"
                f"  [bold {R.Theme.ACCENT}]\\[b][/] Custom queue   — switch to multiagent mode and pick your own provider order\n"
                f"  [bold {R.Theme.ACCENT}]\\[c][/] Single agent   — stay on this provider (option to switch account and retry)\n"
                f"  [bold {R.Theme.ACCENT}]\\[d][/] Switch model   — switch to another model in single-agent mode (transfers session & context)\n"
                f"  [bold {R.Theme.ACCENT}]\\[e][/] Stop           — pause workflow cleanly (context already preserved)"
            )
            raw_choice = console.input("  Choice [a/b/c/d/e]: ", markup=False).strip().lower()
            choice = raw_choice.strip("[](). ")
        except (EOFError, KeyboardInterrupt):
            return "stop"

        if choice in ("e", "stop"):
            return "stop"
        if choice in ("c", "single", "same", "retry"):
            return "single"

        # Check for single-agent model switch: 'd', 'switch', 'model', or 'd <model>' / 'switch <model>'
        is_model_switch = False
        target_arg = ""
        if choice in ("d", "switch", "model", "switch model", "switch_model"):
            is_model_switch = True
        elif choice.startswith("d ") or choice.startswith("switch ") or choice.startswith("model "):
            parts = choice.split(None, 1)
            if len(parts) > 1:
                is_model_switch = True
                target_arg = parts[1].strip()

        if is_model_switch:
            candidates = [m["id"] for m in MODEL_CATALOG if m["id"] != norm_failed]
            if not candidates:
                R.print_warn("No alternative models available; choose another option.")
                continue

            chosen_model: Optional[str] = None
            if target_arg:
                if target_arg.isdigit():
                    idx = int(target_arg)
                    if 1 <= idx <= len(candidates):
                        chosen_model = candidates[idx - 1]
                else:
                    norm_target = normalize_web_model(target_arg)
                    if not norm_target:
                        for cand in candidates:
                            if cand in target_arg:
                                norm_target = cand
                                break
                    if norm_target in candidates:
                        chosen_model = norm_target

            while chosen_model is None:
                console.print(f"\n  [bold {R.Theme.PRIMARY}]Select model for single-agent mode:[/]")
                for idx, mid in enumerate(candidates, 1):
                    info = get_model_info(mid, state=state)
                    console.print(
                        f"  [bold {R.Theme.ACCENT}]\\[{idx}][/] {info['name']:<14} [{R.Theme.DIM}]({info['site']})[/]"
                    )
                try:
                    raw_sub = console.input(
                        f"  Choice [1-{len(candidates)} or name, Enter to cancel]: ",
                        markup=False,
                    ).strip().lower().strip("[](). ")
                except (EOFError, KeyboardInterrupt):
                    break
                if not raw_sub or raw_sub in ("cancel", "back", "q", "exit"):
                    break
                if raw_sub.isdigit():
                    num = int(raw_sub)
                    if 1 <= num <= len(candidates):
                        chosen_model = candidates[num - 1]
                        break
                    R.print_warn(f"Choose a number between 1 and {len(candidates)}.")
                    continue
                norm_sub = normalize_web_model(raw_sub)
                if not norm_sub:
                    for cand in candidates:
                        if cand in raw_sub:
                            norm_sub = cand
                            break
                if norm_sub in candidates:
                    chosen_model = norm_sub
                    break
                if norm_sub == norm_failed:
                    R.print_warn(f"{R.friendly_model_name(norm_failed)} reached its limit; pick another provider.")
                    continue
                R.print_warn(f"Unknown model '{raw_sub}'. Choose from: {', '.join(candidates)}.")

            if not chosen_model:
                continue

            state.configure_singleagent(chosen_model)
            _reset_provider_chats_for_account_change(
                state, client, getattr(state, "session_id", None),
                [chosen_model], clear_roles=True, refresh_health=True,
            )
            state._force_new_chat_next = True
            _preserve_workflow_state_and_context(state)
            R.print_success(
                f"Switched to [bold {R.Theme.ACCENT}]{R.friendly_model_name(chosen_model)}[/] with full context (single-agent mode)."
            )
            return "switch"

        if choice not in ("a", "b", "default", "custom"):
            R.print_warn("Choose a, b, c, d, or e.")
            continue

        from .commands import _cmd_multiagent
        _cmd_multiagent("default" if choice in ("a", "default") else "custom", state, client=client)
        queue = list(getattr(state, "multiagent_queue", []) or [])
        if getattr(state, "agent_mode", "singleagent") != "multiagent" or not queue:
            continue
        candidates = [model for model in queue if model != norm_failed]
        if not candidates:
            R.print_warn("The selected queue has no other provider; choose another option.")
            state.configure_singleagent(norm_failed)
            continue
        # The exhausted provider must not reappear later in this recovery.
        # configure_multiagent also keeps model/index/queue consistent on resume.
        state.configure_multiagent(candidates)
        _preserve_workflow_state_and_context(state)
        return "switch"


def _run_visual_critic_bridge(
    pending_imgs: List[Dict[str, Any]],
    telemetry_text: str,
    state: SessionState,
    client: Any,
    vision_model: str,
    *,
    relay_id: Optional[str] = None,
    worker_session_id: Optional[str] = None,
    on_relay_status: Optional[Callable[[Dict[str, Any]], None]] = None,
    client_request_id: Optional[str] = None,
    meta_out: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Delegate a 1-turn multimodal visual critique to a synced vision-capable web bridge."""
    if not pending_imgs or not vision_model or not callable(getattr(client, "stream", None)):
        if isinstance(meta_out, dict):
            meta_out["error_kind"] = "unavailable"
        return None
    critic_parts: List[Dict[str, Any]] = [{
        "type": "text",
        "text": (
            "Act as a senior Awwwards / Igloo.inc Visual Art Director and Dream-Loop Critic. "
            "Inspect the attached UI/visual contact sheet and telemetry critically:\n"
            "1. 3D & Background Craft: Flag cheap 'Saturn-toy' primitive collages (sphere + torus rings + cones) or harsh two-tone horizon cuts (e.g. pitch-black sky meeting a bright gray floor behind text without unified fog/atmosphere).\n"
            "2. Text-Over-Canvas Contrast & Safe Zones: Flag unbacked dark text over dark 3D sky/geometry (or light text over bright terrain), low WCAG contrast, or 3D geometry slicing behind headline typography.\n"
            "3. Navbar & Layout Hygiene: Flag transparent sticky headers where scrolled text/tables bleed behind nav links, text clipping, overflow, or occluded controls.\n"
            "Reference Set-of-Mark badge numbers [1], [2], etc. when visible.\n\n"
            f"Telemetry:\n{str(telemetry_text or '')[:2200]}\n\n"
            'Return ONLY concise JSON: {"pass": true|false, "defects": [{"tile": "...", "mark": "[N]", '
            '"severity": "high|medium|low", "issue": "...", "suggested_fix": "..."}], "summary": "..."}'
        ),
    }]
    for im in pending_imgs:
        if im.get("url"):
            critic_parts.append({"type": "image_url", "image_url": {"url": im["url"]}})
    stream_kwargs: Dict[str, Any] = dict(
        messages=[
            {
                "role": "system",
                "content": "You are WSA Visual Critic. Inspect screenshots strictly from observed pixels and telemetry; never invent unseen defects.",
            },
            {"role": "user", "content": critic_parts},
        ],
        model=vision_model,
        use_tools=False,
        session_id=f"{worker_session_id or state.session_id}:role:visual_critic",
        new_chat=True,
        relay_id=relay_id,
    )
    if callable(on_relay_status):
        stream_kwargs["on_relay_status"] = on_relay_status
    if client_request_id:
        stream_kwargs["client_request_id"] = client_request_id
    try:
        resp = _call_client_stream(client, stream_kwargs)
        if isinstance(meta_out, dict) and isinstance(resp, dict) and isinstance(resp.get("relay_timings"), dict):
            meta_out["relay_timings"] = dict(resp["relay_timings"])
        txt = str((resp or {}).get("content") or "").strip() if isinstance(resp, dict) else ""
        # Accept a fenced reply from a web tab, but only consume an explicit verdict.
        body = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", txt, flags=re.I)
        verdict = json.loads(body)
        if (not isinstance(verdict, dict) or type(verdict.get("pass")) is not bool
                or not isinstance(verdict.get("defects"), list)):
            if isinstance(meta_out, dict):
                meta_out["error_kind"] = "unusable_reply"
            return None
        if isinstance(meta_out, dict):
            meta_out["error_kind"] = ""
        return verdict
    except (json.JSONDecodeError, ValueError, TypeError):
        if isinstance(meta_out, dict):
            meta_out["error_kind"] = "unusable_reply"
        return None
    except Exception as exc:
        if isinstance(meta_out, dict):
            meta_out["error_kind"] = _classify_visual_error(exc)
        return None


def _attach_approved_concepts_if_needed(
    msgs: List[Dict[str, Any]],
    state: Any,
    effective_model: str,
    role: str,
    effective_chat_url: Optional[str],
    is_new_chat: bool,
    local_synced_threads: Set[str],
    *,
    quiet_ui: bool = False,
) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
    """Attach approved concept mockup images + visual brief on fresh chats, role threads, failovers, or account changes."""
    concepts = getattr(state, "approved_design_concepts", None) or []
    if not concepts or not msgs:
        return msgs, None, None

    scope_tag = f"{effective_model}:{role}"
    thread_key = f"{scope_tag}:{effective_chat_url}" if (effective_chat_url and not is_new_chat) else ""
    synced_keys = getattr(state, "_concept_synced_keys", None)
    if thread_key:
        if isinstance(synced_keys, set) and thread_key in synced_keys:
            return msgs, None, None
    elif not is_new_chat and scope_tag in local_synced_threads:
        return msgs, None, None

    from .visual_concept import format_approved_concepts_context, load_approved_concept_image_blocks

    img_parts, _loaded_paths = load_approved_concept_image_blocks(state, max_images=4)
    concept_summary = format_approved_concepts_context(concepts, max_chars=1400)
    if not img_parts and not concept_summary:
        return msgs, None, None

    transfer_note = (
        f"[APPROVED VISUAL DESIGN TRANSFER -> {str(effective_model).upper()}]\n"
        "We are building this exact user-approved website/software/UI/dashboard shown in the attached "
        "concept mockup image(s). Keep the approved visual style, layout, palette, and component hierarchy:\n"
        f"{concept_summary}"
    ).strip()

    outgoing_msgs = [dict(m) if isinstance(m, dict) else m for m in msgs]
    target_idx = -1
    for i in range(len(outgoing_msgs) - 1, -1, -1):
        if isinstance(outgoing_msgs[i], dict) and outgoing_msgs[i].get("role") == "user":
            target_idx = i
            break

    if target_idx < 0:
        content_parts: List[Dict[str, Any]] = [{"type": "text", "text": transfer_note}, *img_parts]
        outgoing_msgs.append({"role": "user", "content": content_parts})
    else:
        cur_content = outgoing_msgs[target_idx].get("content")
        if isinstance(cur_content, list):
            existing_urls = {
                str((p.get("image_url") or {}).get("url") or "")
                for p in cur_content
                if isinstance(p, dict) and p.get("type") == "image_url"
            }
            new_imgs = [
                p for p in img_parts
                if str((p.get("image_url") or {}).get("url") or "") not in existing_urls
            ]
            has_note = any(
                isinstance(p, dict)
                and p.get("type") == "text"
                and "[APPROVED VISUAL DESIGN TRANSFER" in str(p.get("text") or "")
                for p in cur_content
            )
            patched = list(cur_content)
            if not has_note and transfer_note:
                patched.append({"type": "text", "text": transfer_note})
            patched.extend(new_imgs)
            outgoing_msgs[target_idx]["content"] = patched
        else:
            cur_text = str(cur_content or "")
            if "[APPROVED VISUAL DESIGN TRANSFER" in cur_text:
                combined_text = cur_text
            elif cur_text:
                combined_text = f"{cur_text}\n\n{transfer_note}"
            else:
                combined_text = transfer_note
            if img_parts:
                outgoing_msgs[target_idx]["content"] = [
                    {"type": "text", "text": combined_text},
                    *img_parts,
                ]
            else:
                outgoing_msgs[target_idx]["content"] = combined_text

    if img_parts and not quiet_ui:
        try:
            R.print_info(
                f"Transferred {len(img_parts)} approved concept image(s) + visual brief -> "
                f"{R.friendly_model_name(effective_model)}"
            )
        except Exception:
            pass
    return outgoing_msgs, scope_tag, (thread_key or None)


@serialized_turn
def execute_agent_loop(
    user_input: str,
    state: SessionState,
    client: WSAClient,
    *,
    role: str = "main",
    tools_override: Optional[List[Dict]] = None,
    system_prompt_override: Optional[str] = None,
    history_messages: Optional[List[Dict]] = None,
    persist_to_state: bool = True,
    session_id_override: Optional[str] = None,
    chat_url_override: Optional[str] = None,
    relay_id_override: Optional[str] = None,
    new_chat_override: Optional[bool] = None,
    model_override: Optional[str] = None,
    max_rounds: int = MAX_TOOL_ROUNDS,
    strict_read_before_edit: Optional[bool] = None,
    enforce_verify_gate: bool = True,
    quiet_ui: bool = False,
    allow_auto_decisions: bool = True,
    extra_context_block: str = "",
    event_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    allowed_write_paths: Optional[List[str]] = None,
    stage_label: str = "",
    live_ui: Optional[bool] = None,
    flush_final_stream: bool = True,
    skill_appointment: Optional[SkillAppointment] = None,
    defer_visual_gate: bool = False,
) -> Dict[str, Any]:
    """
    Execute an agentic tool loop for either the main session (`run_turn`) or a scoped subagent role.
    Returns structured telemetry and outputs (`final_text`, `written_files`, `verified_files`, etc.).
    """
    set_project_root(state.project_root)
    set_bridge_endpoint(getattr(client, "api_base", ""), getattr(client, "api_key", ""))
    consume_pending_images()
    verification_evidence: Dict[str, Dict[str, Any]] = {}
    denied_permissions: Set[str] = set()

    free_mode = getattr(state, "free_mode", False)
    if persist_to_state and allow_auto_decisions and not free_mode and hasattr(state, "decisions"):
        state.decisions = capture_user_decisions(user_input, getattr(state, "decisions", None))

    user_content, attached_imgs = _prepare_user_turn_content(user_input, state.project_root)
    if not quiet_ui:
        for meta in attached_imgs:
            dims = f"{meta['width']}x{meta['height']}" if meta["width"] and meta["height"] else "image"
            R.print_info(f"Attached screenshot: {meta['filename']} ({meta['format']} {dims}, {meta['bytes'] // 1024 or 1} KB)")

    def _make_system_msg(target_model: Optional[str] = None) -> Dict[str, str]:
        if system_prompt_override is not None:
            sys_txt = system_prompt_override
            if extra_context_block:
                sys_txt = f"{sys_txt.rstrip()}\n\n{extra_context_block.strip()}"
            return {"role": "system", "content": sys_txt}
        eff_model = target_model or model_override
        orig_model = getattr(state, "model", None)
        swapped = bool(eff_model and orig_model and eff_model != orig_model)
        if swapped:
            state.model = eff_model
        try:
            base_msg = _build_system_message(state)
        finally:
            if swapped:
                state.model = orig_model
        if extra_context_block:
            base_msg["content"] = f"{base_msg['content'].rstrip()}\n\n{extra_context_block.strip()}"
        return base_msg

    # Free mode intentionally omits local system/tool context.
    messages: List[Dict] = []
    if not free_mode:
        messages.append(_make_system_msg())
    if history_messages is not None:
        messages.extend(list(history_messages))
    elif persist_to_state:
        messages.extend(state.messages)
    outgoing_content = user_content
    active_appointment: Optional[SkillAppointment] = None
    if role == "main" and not free_mode:
        active_appointment = skill_appointment if isinstance(skill_appointment, SkillAppointment) else analyze_prompt_skills(user_input)
        if active_appointment.skills or active_appointment.requires_security_pass:
            block = skill_read_directive(
                active_appointment,
                already_loaded=getattr(state, "loaded_skills", ()),
            )
            if block:
                if isinstance(user_content, list):
                    outgoing_content = [*user_content, {"type": "text", "text": block}]
                else:
                    outgoing_content = f"{user_content}\n\n{block}"
            if not quiet_ui:
                R.print_skill_appointment(active_appointment)
        from .toolchain import build_toolchain_directive, explicit_languages, task_wants_backend, task_wants_frontend
        if (
            (active_appointment and (active_appointment.skills or active_appointment.requires_security_pass))
            or task_wants_frontend(user_input)
            or task_wants_backend(user_input)
            or bool(explicit_languages(user_input))
        ):
            _tc_main = build_toolchain_directive(
                user_input,
                state.project_root,
                role="main",
                skills=(active_appointment.skills if active_appointment else ()),
            )
            if _tc_main and messages and messages[0].get("role") == "system":
                extra_context_block = (f"{extra_context_block.strip()}\n\n{_tc_main[:1500]}").strip()
                messages[0] = _make_system_msg()
    messages.append({"role": "user", "content": outgoing_content})

    new_messages: List[Dict] = [{"role": "user", "content": user_content}]

    written_this_turn: Set[str] = set()
    verified_this_turn: Set[str] = set()
    skills_read_this_turn: Set[str] = set()
    active_requires_security_pass: bool = bool(
        role == "main" and active_appointment is not None and active_appointment.requires_security_pass
    )
    security_skills_read_this_turn: bool = bool(
        {"security-audit", "semgrep-guardrails"} & set(getattr(state, "loaded_skills", set()))
    )
    verified_after_last_edit: bool = True
    browser_verified_after_ui_edit: bool = True
    security_verified_after_edit: bool = True
    verify_nudges_used: int = 0
    ui_verify_nudges_used: int = 0
    security_nudges_used: int = 0
    continue_nudges_used: int = 0
    budget_override_nudges_used: int = 0
    budget_hallucinated: bool = False
    pending_new_chat_override: Optional[bool] = new_chat_override
    test_check_done: bool = getattr(state, "_test_check_done", False)
    tests_exist: Optional[bool] = getattr(state, "_tests_exist", None)
    last_tool_hint: str = ""
    flushed_msg_count: int = 0
    turn_counted: bool = False
    last_failure_sig: str = ""
    consecutive_same_failures: int = 0
    unmutated_read_counts: Dict[str, int] = {}
    local_canned_retries: int = 0
    local_parse_retries: int = 0
    final_assistant_text: str = ""
    rounds_used: int = 0
    tool_calls_count: int = 0
    aborted_reason: str = ""
    limit_exhausted: bool = False

    def _abort_interrupted() -> None:
        nonlocal aborted_reason
        aborted_reason = "interrupted"
        if not quiet_ui:
            R.print_warn("Interrupted by user — saving completed progress from this turn.")
        _checkpoint_turn_progress(partial=True)

    if tools_override is not None:
        scoped_tool_names: Optional[Set[str]] = {
            str((t.get("function") or t).get("name") or "").strip()
            for t in tools_override
            if isinstance(t, dict)
        } - {""}
    else:
        scoped_tool_names = None

    def _condense_tool_messages(msgs: List[Dict]) -> None:
        for message in msgs:
            if message.get("role") == "tool":
                tname = str(message.get("name") or "")
                content = str(message.get("content") or "")
                message["content"] = _condense_single_tool_output(tname, content)
            elif message.get("role") == "user" and isinstance(message.get("content"), list):
                text_bits = []
                img_count = 0
                for part in message["content"]:
                    if isinstance(part, dict):
                        if part.get("type") == "text" and part.get("text"):
                            text_bits.append(str(part["text"]))
                        elif part.get("type") in ("image_url", "input_image", "image"):
                            img_count += 1
                suffix = f" [Attached {img_count} screenshot(s) in prior turn]" if img_count else ""
                message["content"] = (" ".join(text_bits) + suffix).strip()

    def _checkpoint_turn_progress(partial: bool = False) -> None:
        nonlocal flushed_msg_count, turn_counted
        if not persist_to_state:
            return
        safe_end = len(new_messages)
        if partial:
            while safe_end > flushed_msg_count and (
                new_messages[safe_end - 1].get("role") == "assistant"
                and new_messages[safe_end - 1].get("tool_calls")
            ):
                safe_end -= 1
            if safe_end <= 1:
                return
        if safe_end > flushed_msg_count:
            chunk = new_messages[flushed_msg_count:safe_end]
            if not partial:
                _condense_tool_messages(chunk)
            state.messages.extend(chunk)
            flushed_msg_count = safe_end
        if not partial and not aborted_reason and getattr(state, "context_summary", ""):
            cleaned_lines = [
                ln for ln in str(state.context_summary).splitlines()
                if not ln.startswith("[Interrupted Pending Goal]:")
            ]
            state.context_summary = "\n".join(cleaned_lines).strip()
        if safe_end > 0 and not turn_counted:
            state.turn_count += 1
            turn_counted = True
        if not partial or len(state.messages) > 80:
            _compact_messages_for_web_limit(state.messages, keep_recent_tools=10, force=False)
            if len(state.messages) > 60:
                state.compact_history(keep=40)
        if hasattr(state, "auto_checkpoint"):
            state.auto_checkpoint()

    active_ledger = getattr(state, "file_ledger", None)
    prev_ledger = set_active_file_ledger(active_ledger if not free_mode else None)
    use_strict = (not free_mode) if strict_read_before_edit is None else bool(strict_read_before_edit)
    prev_strict = set_strict_read_before_edit(use_strict)
    prev_subagent_ctx: Optional[Tuple[Any, Any]] = None
    if role == "main" and persist_to_state:
        try:
            from .subagents import set_subagent_runtime_context
            prev_subagent_ctx = set_subagent_runtime_context(state, client)
        except Exception:
            prev_subagent_ctx = None

    try:
        effective_model = model_override or state.model
        effective_session_id = session_id_override if session_id_override is not None else getattr(state, "session_id", None)
        uses_main_thread = effective_session_id == getattr(state, "session_id", None)
        failover_new_chat: Optional[bool] = None
        if role == "main" and callable(getattr(client, "get_health", None)):
            try:
                _had_saved_url = bool((getattr(state, "chat_urls", {}) or {}).get(effective_model))
                _pre_health = client.get_health()
                if isinstance(_pre_health, dict) and _pre_health and hasattr(state, "sync_account_from_health"):
                    state.sync_account_from_health(_pre_health)
                    if _had_saved_url and not (getattr(state, "chat_urls", {}) or {}).get(effective_model):
                        failover_new_chat = True
            except Exception:
                pass
        if chat_url_override:
            effective_chat_url: Optional[str] = chat_url_override
        elif uses_main_thread:
            effective_chat_url = (
                (getattr(state, "chat_urls", {}) or {}).get(effective_model)
                or (getattr(state, "active_chat_url", "") if effective_model == getattr(state, "model", None) else "")
                or None
            )
        elif role != "main":
            if hasattr(state, "get_role_chat_url"):
                effective_chat_url = state.get_role_chat_url(role, model=effective_model) or None
            else:
                cand_url = (getattr(state, "role_threads", {}) or {}).get(role) or ""
                effective_chat_url = cand_url if (cand_url and is_provider_thread_url(cand_url)) else None
        else:
            effective_chat_url = (
                (getattr(state, "chat_urls", {}) or {}).get(effective_model)
                or (getattr(state, "active_chat_url", "") if effective_model == getattr(state, "model", None) else "")
                or None
            )

        use_live_ui = (not quiet_ui) if live_ui is None else bool(live_ui)

        def _make_renderer(
            cur_model: str,
            hint: str,
            r_idx: int,
            *,
            stage_override: Optional[str] = None,
            flush_override: Optional[bool] = None,
        ) -> Any:
            if not use_live_ui:
                return _QuietStreamSink(event_callback=event_callback, role=role)
            eff_stage = stage_label if stage_override is None else stage_override
            eff_flush = flush_final_stream if flush_override is None else flush_override
            try:
                return R.StreamRenderer(
                    round_num=r_idx,
                    status_hint=hint,
                    model=cur_model,
                    stage_label=eff_stage,
                    flush_final=eff_flush,
                )
            except TypeError:
                try:
                    return R.StreamRenderer(round_num=r_idx, status_hint=hint, model=cur_model)
                except TypeError:
                    try:
                        return R.StreamRenderer(round_num=r_idx, status_hint=hint)
                    except TypeError:
                        return R.StreamRenderer()

        def _emit_visual_final(tracker: VisualReviewTracker, final_line: str, outcome: str) -> None:
            diag = tracker.to_diagnostics_dict()
            try:
                state.last_visual_review = diag
            except Exception:
                pass
            if not quiet_ui:
                print_fn = getattr(R, "print_visual_status", None)
                if callable(print_fn):
                    print_fn(final_line, outcome=outcome)
                else:
                    R.print_info(final_line)
            if event_callback:
                try:
                    event_callback(
                        "visual_status",
                        {
                            "role": role,
                            "outcome": outcome,
                            "message": final_line,
                            "diagnostics": diag,
                        },
                    )
                except Exception:
                    pass

        pending_direct_visual_tracker: Optional[VisualReviewTracker] = None
        pending_direct_visual_local_clean: Optional[bool] = None
        local_concept_synced: Set[str] = set()
        concept_synced_scope_this_turn: Optional[str] = None

        for round_num in range(max_rounds):
            rounds_used = round_num + 1
            budget_hallucinated = False
            active_budget = _max_web_prompt_chars_for_model(effective_model)
            is_delta_continuation = bool(
                round_num > 0
                and effective_chat_url
                and failover_new_chat is not True
                and pending_new_chat_override is not True
                and not getattr(state, "_force_new_chat_next", False)
            )
            if (is_delta_continuation or bool(effective_chat_url)) and active_budget < MAX_WEB_PROMPT_CHARS:
                delta_budget = min(active_budget, 8_200)
            else:
                delta_budget = active_budget
            compacted_n = _compact_messages_for_web_limit(
                messages,
                max_chars=delta_budget,
                delta_mode=is_delta_continuation,
            )
            if active_budget < MAX_WEB_PROMPT_CHARS:
                compacted_n += _trim_oversized_user_messages(messages, min(delta_budget, 7_600))
            if compacted_n and not quiet_ui:
                R.print_info(
                    f"Auto-compacted {compacted_n} tool output(s) to stay within "
                    f"{R.friendly_model_name(effective_model)} prompt limits ({active_budget // 1000}KB)."
                )
            if round_num == 0 and not quiet_ui:
                R.begin_agent_response()

            if event_callback:
                event_callback("round", {"role": role, "round": round_num + 1, "activity": last_tool_hint})

            response: Optional[Dict[str, Any]] = None
            stream_aborted = False
            active_visual_tracker = pending_direct_visual_tracker
            active_visual_local_clean = pending_direct_visual_local_clean
            pending_direct_visual_tracker = None
            pending_direct_visual_local_clean = None
            visual_fallback_from = ""
            cur_status_hint = last_tool_hint
            prompt_too_long_retries = 0
            composer_submit_retries = 0

            def _apply_model_failover(
                next_model: str,
                *,
                reason_text: str,
                event_prefix: str,
                is_limit: bool = True,
            ) -> None:
                nonlocal effective_model, system_prompt_override, effective_chat_url
                nonlocal failover_new_chat, is_delta_continuation
                nonlocal prompt_too_long_retries, composer_submit_retries
                nonlocal cur_status_hint, visual_fallback_from
                if callable(getattr(client, "get_health", None)) and hasattr(state, "sync_account_from_health"):
                    try:
                        _fo_health = client.get_health()
                        if isinstance(_fo_health, dict) and _fo_health:
                            state.sync_account_from_health(_fo_health)
                    except Exception:
                        pass
                prev_model = effective_model
                effective_model = next_model
                if hasattr(state, "set_model"):
                    state.set_model(next_model)
                effective_chat_url = None
                failover_new_chat = True
                is_delta_continuation = False
                prompt_too_long_retries = 0
                composer_submit_retries = 0
                if hasattr(state, "loaded_skills") and isinstance(state.loaded_skills, set):
                    state.loaded_skills.clear()
                if hasattr(state, "clear_provider_chats"):
                    state.clear_provider_chats([next_model], clear_roles=(role != "main"))
                elif hasattr(state, "chat_urls") and isinstance(state.chat_urls, dict):
                    state.chat_urls.pop(next_model, None)
                if role != "main":
                    if hasattr(state, "clear_role_chat_url"):
                        state.clear_role_chat_url(role, model=next_model)
                    elif hasattr(state, "chat_urls") and isinstance(state.chat_urls, dict):
                        state.chat_urls.pop(f"{next_model}:role:{role}", None)
                if effective_session_id and callable(getattr(client, "clear_session_chats", None)):
                    try:
                        client.clear_session_chats(
                            effective_session_id,
                            providers=[next_model],
                            include_roles=(role != "main"),
                        )
                    except Exception:
                        pass
                if not free_mode and messages and messages[0].get("role") == "system":
                    if role != "main" and system_prompt_override is not None:
                        try:
                            from .subagents import get_role_system_prompt
                            role_sys = get_role_system_prompt(role, next_model)
                            system_prompt_override = role_sys
                            messages[0] = _make_system_msg(next_model)
                        except Exception:
                            pass
                    elif role == "main":
                        messages[0] = _make_system_msg(next_model)
                while len(messages) >= 2 and messages[-1].get("role") == "user" and _is_budget_nudge_text(messages[-1].get("content")):
                    messages.pop()
                    if messages and messages[-1].get("role") == "assistant" and (
                        _looks_like_budget_hallucination(messages[-1].get("content"))
                        or _looks_like_tool_unavailable_misconception(messages[-1].get("content"))
                    ):
                        messages.pop()
                next_budget = _max_web_prompt_chars_for_model(next_model)
                _compact_messages_for_web_limit(
                    messages,
                    keep_recent_tools=2 if next_budget < MAX_WEB_PROMPT_CHARS else 6,
                    max_chars=next_budget,
                )
                if next_budget < MAX_WEB_PROMPT_CHARS:
                    _trim_oversized_user_messages(messages, min(next_budget, 7_600))
                is_single_switch = is_limit and getattr(state, "agent_mode", "singleagent") == "singleagent"
                q_list = getattr(state, "multiagent_queue", []) or []
                if is_single_switch:
                    msg = (
                        f"{event_prefix} {R.friendly_model_name(prev_model)} — "
                        f"switched to {R.friendly_model_name(next_model)} with full context (single-agent mode)."
                    )
                    cur_status_hint = f"Switch -> {next_model}"
                else:
                    slot_num = (q_list.index(next_model) + 1) if next_model in q_list else getattr(state, "multiagent_index", 0) + 1
                    msg = (
                        f"{event_prefix} {R.friendly_model_name(prev_model)} — "
                        f"switching with full context to {R.friendly_model_name(next_model)} "
                        f"(slot {slot_num}/{max(len(q_list), slot_num)})."
                    )
                    cur_status_hint = f"Failover -> {next_model}"
                if active_visual_tracker is not None:
                    visual_fallback_from = prev_model
                if event_callback:
                    event_callback(
                        "switch_model" if is_single_switch else "failover",
                        {
                            "from": prev_model,
                            "to": next_model,
                            "from_model": prev_model,
                            "to_model": next_model,
                            "message": msg,
                            "reason": reason_text[:160],
                            "mode": "singleagent" if is_single_switch else "multiagent",
                        },
                    )
                if not quiet_ui:
                    if is_single_switch:
                        R.print_success(msg)
                    else:
                        R.print_warn(msg)

            sr = None
            try:
                while True:
                    visual_req_id = ""
                    if active_visual_tracker is not None:
                        visual_req_id = uuid.uuid4().hex[:12]
                        cur_status_hint = active_visual_tracker.begin_review(
                            visual_req_id,
                            effective_model,
                            fallback_from=visual_fallback_from,
                        )
                        visual_fallback_from = ""
                    sr = _make_renderer(effective_model, cur_status_hint, round_num)
                    if active_visual_tracker is not None:
                        _set_renderer_visual_hint(
                            sr,
                            cur_status_hint,
                            with_elapsed=True,
                            reset_stage_clock=True,
                        )
                    sr.start_thinking()
                    try:
                        will_be_new_chat = bool(
                            failover_new_chat is True
                            or pending_new_chat_override is True
                            or getattr(state, "_force_new_chat_next", False)
                            or not effective_chat_url
                        )
                        outgoing_stream_messages, _concept_scope, _concept_thread_key = _attach_approved_concepts_if_needed(
                            messages,
                            state,
                            effective_model,
                            role,
                            effective_chat_url,
                            will_be_new_chat,
                            local_concept_synced,
                            quiet_ui=quiet_ui,
                        )
                        stream_kwargs: Dict[str, Any] = dict(
                            messages=outgoing_stream_messages,
                            model=effective_model,
                            on_token=sr.feed,
                            on_thinking=sr.feed_thinking,
                            on_reset=sr.reset_answer,
                            use_tools=not free_mode,
                            session_id=effective_session_id,
                            chat_url=effective_chat_url,
                        )
                        if hasattr(sr, "set_status_hint"):
                            stream_kwargs["on_status"] = sr.set_status_hint
                        if active_visual_tracker is not None and visual_req_id:
                            stream_kwargs["client_request_id"] = visual_req_id

                            def _on_direct_relay_status(
                                ev: Dict[str, Any],
                                _tr: VisualReviewTracker = active_visual_tracker,
                                _rid: str = visual_req_id,
                                _sr: Any = sr,
                            ) -> None:
                                if _tr.closed or _tr.active_request_id != _rid:
                                    return
                                upd = _tr.on_relay_event(ev)
                                if upd is not None:
                                    txt, with_el = upd
                                    st = str(ev.get("stage") or "").strip().lower()
                                    _set_renderer_visual_hint(
                                        _sr,
                                        txt,
                                        with_elapsed=with_el,
                                        reset_stage_clock=(st == "submitted"),
                                    )

                            stream_kwargs["on_relay_status"] = _on_direct_relay_status
                        if relay_id_override is not None:
                            stream_kwargs["relay_id"] = relay_id_override
                        if failover_new_chat is not None:
                            stream_kwargs["new_chat"] = bool(failover_new_chat)
                        elif pending_new_chat_override is not None:
                            stream_kwargs["new_chat"] = bool(pending_new_chat_override)
                        elif getattr(state, "_force_new_chat_next", False):
                            stream_kwargs["new_chat"] = True
                        if tools_override is not None:
                            stream_kwargs["tools_override"] = tools_override
                        response = _call_client_stream(client, stream_kwargs)
                        if _concept_scope:
                            local_concept_synced.add(_concept_scope)
                            concept_synced_scope_this_turn = _concept_scope
                        if _concept_thread_key:
                            if not isinstance(getattr(state, "_concept_synced_keys", None), set):
                                state._concept_synced_keys = set()
                            state._concept_synced_keys.add(_concept_thread_key)
                        failover_new_chat = None
                        if pending_new_chat_override is True:
                            pending_new_chat_override = None
                        if getattr(state, "_force_new_chat_next", False):
                            state._force_new_chat_next = False
                        # Multiagent-only: a silent model downgrade reported on
                        # THIS request (e.g. Gemini falling back to Flash-Lite or Auto)
                        # means the active web account hit its limit. Escalate to
                        # the ProviderLimitError failover path so context transfers
                        # to the next queued agent, or offers the user model switch in singleagent mode.
                        _um = response.get("upstream_model") if isinstance(response, dict) else None
                        if (
                            not model_override
                            and isinstance(_um, dict)
                            and _um.get("is_downgraded_flash_lite") is True
                        ):
                            _badge = str(_um.get("badge") or "Flash-Lite / Auto")
                            raise ProviderLimitError(
                                f"Provider limit reached ({effective_model}): silently "
                                f"downgraded to {_badge} (upstream model downgrade counts "
                                "as account limit exceeded)",
                                provider=effective_model,
                                raw_error=str(_um),
                            )
                        break
                    except KeyboardInterrupt:
                        _stop_stream_ui(sr)
                        if active_visual_tracker is not None and not active_visual_tracker.closed:
                            _vis_line = active_visual_tracker.finish("cancelled")
                            _emit_visual_final(active_visual_tracker, _vis_line, "cancelled")
                            active_visual_tracker = None
                        _abort_interrupted()
                        stream_aborted = True
                        break
                    except PromptTooLongError as ptle:
                        _stop_stream_ui(sr)
                        prompt_too_long_retries += 1
                        if prompt_too_long_retries <= 3:
                            retry_budget = _trim_messages_for_prompt_too_long(
                                messages, prompt_too_long_retries, effective_model
                            )
                            cur_status_hint = f"Retry compact (~{max(1, retry_budget // 1000)}KB)"
                            if not quiet_ui:
                                R.print_warn(
                                    f"{R.friendly_model_name(effective_model)} rejected prompt as too long "
                                    f"(retry {prompt_too_long_retries}/3) — compacting context to ~{max(1, retry_budget // 1000)}KB & retrying..."
                                )
                            continue
                        if active_visual_tracker is not None and not active_visual_tracker.closed:
                            _vis_line = active_visual_tracker.finish("transport_error", error_kind="prompt too long")
                            _emit_visual_final(active_visual_tracker, _vis_line, "transport_error")
                            active_visual_tracker = None
                        aborted_reason = str(ptle)
                        if not quiet_ui:
                            R.print_error(aborted_reason)
                        _checkpoint_turn_progress(partial=True)
                        stream_aborted = True
                        break
                    except ProviderLimitError as ple:
                        _stop_stream_ui(sr)
                        chosen_next_model = None
                        if (getattr(state, "agent_mode", "singleagent") == "singleagent"
                                and role == "main" and not quiet_ui and not model_override):
                            _preserve_workflow_state_and_context(
                                state, partial_checkpoint_fn=_checkpoint_turn_progress,
                                persist_to_state=persist_to_state, pending_goal=user_input,
                            )
                            choice = _offer_limit_multiagent(state, client, effective_model)
                            if choice == "switch":
                                chosen_next_model = state.model
                            elif choice == "stop":
                                # Avoid the account-switch prompt; the normal stop path persists context.
                                choice = "stop"
                        else:
                            choice = "single"
                        can_failover = (
                            getattr(state, "agent_mode", "singleagent") == "multiagent"
                            and not model_override
                            and hasattr(state, "advance_multiagent_queue")
                        )
                        next_model = chosen_next_model or (state.advance_multiagent_queue(failed_model=effective_model) if can_failover else None)
                        if next_model:
                            _apply_model_failover(
                                next_model,
                                reason_text=str(ple),
                                event_prefix="Limit reached on",
                                is_limit=True,
                            )
                            continue

                        # Queue exhausted (multiagent) OR single-agent limit hit:
                        # preserve current workflow state & context completely before prompting.
                        q_list = list(getattr(state, "multiagent_queue", []) or [])
                        is_multi_exhausted = (
                            getattr(state, "agent_mode", "singleagent") == "multiagent"
                            and bool(q_list)
                        )
                        saved_ckpt = _preserve_workflow_state_and_context(
                            state,
                            partial_checkpoint_fn=_checkpoint_turn_progress,
                            persist_to_state=persist_to_state,
                            pending_goal=user_input if role == "main" else "",
                            record_pending_goal=False,
                        )
                        can_prompt_switch = role == "main" and not quiet_ui and choice != "stop"
                        wants_continue = False
                        if can_prompt_switch:
                            if is_multi_exhausted:
                                R.print_warn(
                                    f"All queued agents reached their account limits ({str(ple)[:120]})."
                                )
                            else:
                                R.print_warn(
                                    f"{R.friendly_model_name(effective_model)} account limit reached ({str(ple)[:120]})."
                                )
                            R.print_info(
                                "Current workflow state & context preserved automatically"
                                + (f" ({saved_ckpt})." if saved_ckpt else ".")
                            )
                            # Bypass-exempt: always prompt Y/N even in permission_mode='auto'.
                            wants_continue = _prompt_all_agents_exhausted(
                                state,
                                model_name=effective_model,
                                is_multiagent=is_multi_exhausted,
                            )

                        if wants_continue:
                            waited_ok = _wait_for_account_switch_countdown(ACCOUNT_SWITCH_WAIT_SECS)
                            if not waited_ok:
                                stop_providers = list(q_list) if (is_multi_exhausted and q_list) else [effective_model]
                                _reset_provider_chats_for_account_change(
                                    state,
                                    client,
                                    effective_session_id,
                                    stop_providers,
                                    clear_roles=True,
                                    refresh_health=False,
                                )
                                if persist_to_state and role == "main":
                                    state._force_new_chat_next = True
                                _preserve_workflow_state_and_context(
                                    state,
                                    partial_checkpoint_fn=_checkpoint_turn_progress,
                                    persist_to_state=persist_to_state,
                                    pending_goal=user_input if role == "main" else "",
                                    record_pending_goal=(len(new_messages) <= 1),
                                )
                                if not quiet_ui:
                                    R.print_info("Context preserved, stopping workflow...")
                                aborted_reason = "interrupted"
                                limit_exhausted = True
                                stream_aborted = True
                                break

                            if is_multi_exhausted and q_list:
                                restart_model = q_list[0]
                                state.multiagent_index = 0
                                providers_to_clear = list(q_list)
                            else:
                                restart_model = effective_model
                                providers_to_clear = [restart_model]

                            prev_model = effective_model
                            effective_model = restart_model
                            if hasattr(state, "set_model"):
                                state.set_model(restart_model)

                            effective_chat_url = None
                            failover_new_chat = True
                            prompt_too_long_retries = 0
                            _reset_provider_chats_for_account_change(
                                state,
                                client,
                                effective_session_id,
                                providers_to_clear,
                                clear_roles=True,
                                refresh_health=True,
                            )

                            if not free_mode and messages and messages[0].get("role") == "system":
                                if role == "main":
                                    messages[0] = _make_system_msg(restart_model)
                            next_budget = _max_web_prompt_chars_for_model(restart_model)
                            _compact_messages_for_web_limit(
                                messages,
                                keep_recent_tools=2 if next_budget < MAX_WEB_PROMPT_CHARS else 6,
                                max_chars=next_budget,
                            )
                            if not quiet_ui:
                                R.print_info(
                                    f"Retrying {R.friendly_model_name(restart_model)} in new account's fresh chat "
                                    f"with preserved context ({next_budget // 1000}KB budget)."
                                )
                            if event_callback:
                                event_callback(
                                    "failover",
                                    {
                                        "from": prev_model,
                                        "to": restart_model,
                                        "from_model": prev_model,
                                        "to_model": restart_model,
                                        "message": "Account changed; retrying in fresh chat with preserved context.",
                                        "reason": str(ple)[:160],
                                    },
                                )
                            cur_status_hint = f"Account switched -> {restart_model}"
                            continue

                        # User declined (`n`) or non-interactive fallback:
                        # invalidate limit-hit chat bindings and persist full context before stopping.
                        stop_providers = list(q_list) if (is_multi_exhausted and q_list) else [effective_model]
                        _reset_provider_chats_for_account_change(
                            state,
                            client,
                            effective_session_id,
                            stop_providers,
                            clear_roles=(role == "main"),
                            refresh_health=False,
                        )
                        if persist_to_state and role == "main":
                            state._force_new_chat_next = True
                        saved_stop = _preserve_workflow_state_and_context(
                            state,
                            partial_checkpoint_fn=_checkpoint_turn_progress,
                            persist_to_state=persist_to_state,
                            pending_goal=user_input if role == "main" else "",
                            record_pending_goal=(len(new_messages) <= 1),
                        )
                        if not quiet_ui:
                            if can_prompt_switch:
                                R.print_info(
                                    "Context preserved, stopping workflow..."
                                    + (f" (saved: {saved_stop})" if saved_stop else "")
                                )
                            elif choice != "stop":
                                R.print_error(str(ple))
                            else:
                                R.print_info("Context preserved, stopping workflow...")
                        if active_visual_tracker is not None and not active_visual_tracker.closed:
                            _vis_line = active_visual_tracker.finish("transport_error", error_kind="rate limit")
                            _emit_visual_final(active_visual_tracker, _vis_line, "transport_error")
                            active_visual_tracker = None
                        aborted_reason = str(ple)
                        limit_exhausted = True
                        stream_aborted = True
                        break
                    except ConnectionError as e:
                        _stop_stream_ui(sr)
                        err_low = str(e).lower()
                        is_editor_missing = "prompt editor not found" in err_low
                        is_stream_stalled = "stream stalled on" in err_low
                        is_unsubmitted = (
                            "prompt remained unsubmitted in composer" in err_low
                            or "did not start or produce a text response" in err_low
                            or "dom relay timeout (" in err_low
                            or "picked up the prompt in" in err_low
                            or is_editor_missing
                            or is_stream_stalled
                        )
                        if is_unsubmitted and composer_submit_retries < 2:
                            composer_submit_retries += 1
                            if composer_submit_retries == 1 and not (is_editor_missing or is_stream_stalled):
                                retry_cap = min(active_budget, 7_200) if active_budget < MAX_WEB_PROMPT_CHARS else min(active_budget, 24_000)
                                _compact_messages_for_web_limit(
                                    messages,
                                    keep_recent_tools=1 if active_budget < MAX_WEB_PROMPT_CHARS else 2,
                                    force=True,
                                    max_chars=retry_cap,
                                    delta_mode=is_delta_continuation,
                                )
                                _trim_oversized_user_messages(messages, max(2_400, retry_cap - 1_200))
                                cur_status_hint = "Retry composer submit (1/2)"
                                if not quiet_ui:
                                    R.print_warn(
                                        f"{R.friendly_model_name(effective_model)} composer did not submit — "
                                        "compacting context & retrying (1/2)..."
                                    )
                            else:
                                effective_chat_url = None
                                if isinstance(getattr(state, "chat_urls", None), dict):
                                    state.chat_urls.pop(effective_model, None)
                                failover_new_chat = True
                                is_delta_continuation = False
                                fresh_cap = min(active_budget, 5_000) if active_budget < MAX_WEB_PROMPT_CHARS else min(active_budget, 20_000)
                                _compact_messages_for_web_limit(
                                    messages,
                                    keep_recent_tools=1 if active_budget < MAX_WEB_PROMPT_CHARS else 3,
                                    force=True,
                                    max_chars=fresh_cap,
                                )
                                _trim_oversized_user_messages(messages, max(2_200, fresh_cap - 800))
                                cur_status_hint = "Retry in fresh chat (2/2)"
                                if not quiet_ui:
                                    R.print_warn(
                                        f"{R.friendly_model_name(effective_model)} composer remained unsubmitted — "
                                        "resetting to fresh chat & retrying (2/2)..."
                                    )
                            try:
                                time.sleep(1.2)
                            except KeyboardInterrupt:
                                _abort_interrupted()
                                stream_aborted = True
                                break
                            continue
                        can_failover = (
                            is_unsubmitted
                            and getattr(state, "agent_mode", "singleagent") == "multiagent"
                            and not model_override
                            and hasattr(state, "advance_multiagent_queue")
                        )
                        next_model = state.advance_multiagent_queue(failed_model=effective_model) if can_failover else None
                        if next_model:
                            _apply_model_failover(
                                next_model,
                                reason_text=str(e),
                                event_prefix="Composer stalled on",
                                is_limit=False,
                            )
                            continue
                        if active_visual_tracker is not None and not active_visual_tracker.closed:
                            _vis_line = active_visual_tracker.finish("transport_error", error_kind=_classify_visual_error(e))
                            _emit_visual_final(active_visual_tracker, _vis_line, "transport_error")
                            active_visual_tracker = None
                        aborted_reason = str(e)
                        if not quiet_ui:
                            R.print_error(str(e))
                        _checkpoint_turn_progress(partial=True)
                        stream_aborted = True
                        break
                    except Exception as e:
                        _stop_stream_ui(sr)
                        if active_visual_tracker is not None and not active_visual_tracker.closed:
                            _vis_line = active_visual_tracker.finish("transport_error", error_kind=_classify_visual_error(e))
                            _emit_visual_final(active_visual_tracker, _vis_line, "transport_error")
                            active_visual_tracker = None
                        aborted_reason = f"Unexpected error: {e}"
                        if not quiet_ui:
                            R.print_error(aborted_reason)
                        _checkpoint_turn_progress(partial=True)
                        stream_aborted = True
                        break

                if stream_aborted or response is None:
                    break

                sr.finish()
                if active_visual_tracker is not None and not active_visual_tracker.closed:
                    _r_timings = response.get("relay_timings") if isinstance(response, dict) else None
                    _direct_outcome = (
                        "reviewed_with_local_defects"
                        if active_visual_local_clean is False
                        else "reviewed"
                    )
                    _vis_line = active_visual_tracker.finish(_direct_outcome, timings=_r_timings)
                    _emit_visual_final(active_visual_tracker, _vis_line, _direct_outcome)
                    active_visual_tracker = None
            finally:
                _close_stream_ui(sr)
                if active_visual_tracker is not None and not active_visual_tracker.closed:
                    active_visual_tracker.closed = True
                    active_visual_tracker = None

            if sr.get_thinking_text() and persist_to_state:
                state.last_thinking_chars = len(sr.get_thinking_text())
            if isinstance(response.get("upstream_model"), dict):
                um = response["upstream_model"]
                if persist_to_state and role == "main":
                    state.last_upstream_model = um
                    if hasattr(state, "sync_account_from_res"):
                        state.sync_account_from_res(um)
                    lbl = um.get("relay_label") or um.get("account_file") or ""
                    if lbl and not getattr(state, "active_account_label", ""):
                        state.active_account_label = str(lbl)
                c_url = str(um.get("chat_url") or "")
                if c_url and is_provider_thread_url(c_url):
                    if uses_main_thread:
                        state.active_chat_url = c_url
                        if hasattr(state, "chat_urls") and isinstance(state.chat_urls, dict):
                            state.chat_urls[effective_model] = c_url
                        effective_chat_url = c_url
                    elif role != "main":
                        if hasattr(state, "set_role_chat_url"):
                            state.set_role_chat_url(role, c_url, model=effective_model)
                        elif hasattr(state, "role_threads"):
                            state.role_threads[role] = c_url
                        effective_chat_url = c_url
                    cur_scope = f"{effective_model}:{role}"
                    if concept_synced_scope_this_turn == cur_scope:
                        if not isinstance(getattr(state, "_concept_synced_keys", None), set):
                            state._concept_synced_keys = set()
                        state._concept_synced_keys.add(f"{cur_scope}:{c_url}")
                        concept_synced_scope_this_turn = None

            usage = response.get("usage", {})
            state.total_tokens_in += usage.get("prompt_tokens", 0)
            state.total_tokens_out += usage.get("completion_tokens", 0)

            allowed_tool_names = None if free_mode else (scoped_tool_names if scoped_tool_names is not None else _active_tool_names())
            content = response.get("content")
            if free_mode:
                raw_tool_calls, remaining_text = [], ""
                src_pos_by_id: Dict[str, int] = {}
                dropped_details: List[dict] = []
                dropped_warnings: List[str] = []
                tool_calls: List[dict] = []
                dup_count = 0
            else:
                raw_tool_calls, remaining_text, raw_positions, preparsed_dropped = _extract_tool_batch_plan(
                    response, allowed_names=allowed_tool_names
                )
                src_pos_by_id = {
                    str(tc.get("id") or f"call_{idx}"): (raw_positions[idx] if idx < len(raw_positions) else -1)
                    for idx, tc in enumerate(raw_tool_calls)
                }
                dropped_details = _detect_dropped_tool_details(
                    content, remaining_text, raw_tool_calls, allowed_tool_names, preparsed_details=preparsed_dropped
                )
                dropped_warnings = [
                    f"{d['name']}{('(' + d['target_path'] + ')') if d.get('target_path') else ''}: {d.get('reason', 'could not parse')}"
                    for d in dropped_details
                ]
                tool_calls, dup_count = _deduplicate_tool_calls(raw_tool_calls)
                tool_calls = _ensure_unique_tool_call_ids(tool_calls, round_num=round_num)
                if dup_count and not quiet_ui:
                    R.print_info(f"Skipped {dup_count} duplicate intra-round tool call(s).")

            canned_phrases = (
                "i seem to be encountering an error",
                "i'm having a hard time fulfilling your request",
                "i am having a hard time fulfilling your request",
                "sorry, something went wrong",
                "can i help you with something else instead",
                "can i try something else for you",
            )
            if (
                not tool_calls
                and isinstance(content, str)
                and len(content.strip()) < 220
                and any(p in content.lower() for p in canned_phrases)
            ):
                local_canned_retries += 1
                if persist_to_state:
                    state._canned_retries = local_canned_retries
                if local_canned_retries <= 3:
                    if not quiet_ui:
                        R.print_warn(f"Upstream returned canned web filter message (retry {local_canned_retries}/3) — compacting & resuming...")
                    _compact_messages_for_web_limit(
                        messages,
                        keep_recent_tools=0 if local_canned_retries >= 2 else 1,
                        force=True,
                        max_chars=_max_web_prompt_chars_for_model(effective_model),
                    )
                    if not str(messages[-1].get("content") or "").startswith("[Continue]:"):
                        continuation = (
                            "[Continue]: Please continue executing the next step using a "
                            "```tool_call block. Treat the preceding tool results as still active."
                            if round_num > 0 and not free_mode
                            else "[Continue]: Please provide the complete response to the user's request above."
                        )
                        messages.append({"role": "user", "content": continuation})
                    try:
                        time.sleep(2.0)
                    except KeyboardInterrupt:
                        _abort_interrupted()
                        break
                    continue
            local_canned_retries = 0
            if persist_to_state:
                state._canned_retries = 0

            is_workflow_refusal = (not tool_calls and _srv_is_claude_workflow_refusal(content))
            if is_workflow_refusal:
                if persist_to_state:
                    if hasattr(state, "chat_urls") and isinstance(state.chat_urls, dict):
                        state.chat_urls.pop(getattr(state, "model", "claude"), None)
                    state._force_new_chat_next = True
                if not quiet_ui:
                    R.print_warn("Claude declined the pasted/carried-over chat framing — reset active browser thread; retry in a fresh chat or use `/new`.")
            elif not tool_calls and not free_mode and (dropped_warnings or _looks_like_broken_tool_attempt(content, allowed_tool_names)):
                local_parse_retries += 1
                if persist_to_state:
                    state._tool_parse_retries = local_parse_retries
                if local_parse_retries <= 2:
                    if not quiet_ui:
                        R.print_warn(f"Malformed/incomplete tool_call block detected (retry {local_parse_retries}/2) — auto-resuming...")
                    messages.append({"role": "assistant", "content": content})
                    detail = f" ({'; '.join(dropped_warnings[:3])})" if dropped_warnings else ""
                    messages.append({
                        "role": "user",
                        "content": (
                            f"[Continue]: Your previous tool_call block was incomplete or used invalid fencing/JSON{detail}. "
                            "Re-emit ONLY the valid ```tool_call JSON block(s) and continue the task."
                        ),
                    })
                    try:
                        time.sleep(1.0)
                    except KeyboardInterrupt:
                        _abort_interrupted()
                        break
                    continue
            local_parse_retries = 0
            if persist_to_state:
                state._tool_parse_retries = 0

            sanitized_content = _sanitize_assistant_tool_prose(content, has_tool_calls=bool(tool_calls))
            final_assistant_text = sanitized_content if isinstance(sanitized_content, str) else str(content or "")

            asst_msg: Dict = {"role": "assistant", "content": sanitized_content}
            if tool_calls:
                asst_msg["tool_calls"] = [
                    {key: value for key, value in tc.items() if key != "_wsa_ambiguous_id"}
                    for tc in tool_calls
                ]

            messages.append(asst_msg)
            new_messages.append(asst_msg)

            if not tool_calls:
                is_budget_claim = (
                    not free_mode
                    and not is_workflow_refusal
                    and _looks_like_budget_hallucination(sanitized_content)
                )
                is_tool_misconception = (
                    not free_mode
                    and not is_workflow_refusal
                    and not is_budget_claim
                    and _looks_like_tool_unavailable_misconception(sanitized_content)
                )
                if is_budget_claim or is_tool_misconception:
                    budget_hallucinated = True
                    remaining_rounds = max(0, max_rounds - round_num - 1)
                    if remaining_rounds > 0 and budget_override_nudges_used < 1:
                        budget_override_nudges_used += 1
                        if is_tool_misconception:
                            if not quiet_ui:
                                R.print_warn(
                                    "Detected native-tool misconception — clarifying local `wsa` text tool_call bridge…"
                                )
                            last_tool_hint = "Clarifying local wsa tool_call bridge…"
                            override_msg = {
                                "role": "user",
                                "content": (
                                    "You ARE connected to my local `wsa` CLI bridge right now — these tools do not appear in your "
                                    "built-in sandbox tool list because `wsa` parses plain markdown ```tool_call JSON blocks directly "
                                    "from your reply, runs them locally on my PC, and pastes the real output back in the next message. "
                                    "Please emit the required ```tool_call JSON block(s) now."
                                ),
                            }
                        else:
                            if not quiet_ui:
                                R.print_warn(
                                    f"Detected hallucinated tool-budget exhaustion — overriding ({remaining_rounds} rounds still available)…"
                                )
                            last_tool_hint = "Continuing with available tool rounds…"
                            override_msg = {
                                "role": "user",
                                "content": (
                                    f"This workspace tool session still has {remaining_rounds} rounds available "
                                    "(there is no 0-call cap here). Please go ahead and emit the next ```tool_call JSON block(s) "
                                    "to complete or verify the assigned files on disk."
                                ),
                            }
                        messages.append(override_msg)
                        new_messages.append(override_msg)
                        continue
                if enforce_verify_gate and not is_workflow_refusal:
                    missing_skills = tuple(
                        s
                        for s in (active_appointment.skills if active_appointment else ())
                        if s not in skills_read_this_turn and s not in getattr(state, "loaded_skills", set())
                    )
                    nudge = _maybe_anti_laziness_nudge(
                        content=sanitized_content,
                        written_this_turn=written_this_turn,
                        verified_after_last_edit=verified_after_last_edit,
                        tests_exist=tests_exist,
                        round_num=round_num,
                        verify_nudges_used=verify_nudges_used,
                        continue_nudges_used=continue_nudges_used,
                        free_mode=free_mode,
                        browser_verified_after_ui_edit=browser_verified_after_ui_edit,
                        ui_verify_nudges_used=ui_verify_nudges_used,
                        requires_security_pass=active_requires_security_pass,
                        security_verified_after_edit=security_verified_after_edit,
                        security_nudges_used=security_nudges_used,
                        project_root=state.project_root,
                        missing_appointed_skills=missing_skills,
                        tool_calls_count=tool_calls_count,
                        defer_visual_gate=defer_visual_gate,
                    )
                    if nudge:
                        kind, nudge_prompt = nudge
                        if kind == "security_fix":
                            security_nudges_used += 1
                            security_verified_after_edit = True
                            if not quiet_ui:
                                R.print_info(
                                    "Website security fixing pass active — forcing web agent to read security skills "
                                    "(security-audit, semgrep-guardrails) and harden code…"
                                )
                            last_tool_hint = "Running post-build security audit & hardening…"
                        elif kind == "verify":
                            verify_nudges_used += 1
                            if not quiet_ui:
                                R.print_info("Verification gate active — prompting final check after edits…")
                            last_tool_hint = "Running final verification checks after edits…"
                        elif kind == "ui_verify":
                            ui_verify_nudges_used += 1
                            if not quiet_ui:
                                R.print_info("UI interaction gate active — verifying rendered UI & controls…")
                            last_tool_hint = "Verifying rendered UI layout & interactive controls…"
                        else:
                            continue_nudges_used += 1
                            if not quiet_ui:
                                R.print_info("Resuming incomplete step automatically…")
                            last_tool_hint = "Completing remaining steps autonomously…"
                        nudge_msg = {"role": "user", "content": nudge_prompt}
                        messages.append(nudge_msg)
                        new_messages.append(nudge_msg)
                        continue
                break

            if not quiet_ui:
                R.console.print()
            tool_result_messages: List[Dict] = []
            capture_keys: List[str] = []
            batch_visual_capture_ms: Optional[int] = None
            interrupted_in_tools = False
            has_file_mutations = any(
                (tc.get("function") or {}).get("name") in (
                    "write_file", "patch_file", "apply_patch", "delete_file", "move_file", "copy_file"
                )
                for tc in tool_calls
            )
            if has_file_mutations:
                begin_edit_checkpoint(f"turn {state.turn_count}")

            batch_invs: List[Dict[str, Any]] = []
            try:
              for seq_idx, tc in enumerate(tool_calls):
                tool_calls_count += 1
                tc_id = str(tc.get("id") or f"call_{seq_idx}")
                fn = tc.get("function", {})
                name = str(fn.get("name") or "").strip()
                raw_args = fn.get("arguments", "{}")

                explicit_deps: List[str] = []
                if isinstance(raw_args, dict):
                    _, explicit_deps = split_tool_control_args(raw_args)
                elif isinstance(raw_args, str) and raw_args.strip():
                    try:
                        decoded_tmp = json.loads(raw_args)
                        if isinstance(decoded_tmp, dict):
                            _, explicit_deps = split_tool_control_args(decoded_tmp)
                    except Exception:
                        pass

                val_ok, norm_args, val_err = validate_and_normalize_tool_call(
                    name,
                    raw_args,
                    scoped_tool_names=scoped_tool_names,
                    json_repair_fn=_srv_repair_tool_json_text,
                )
                args = norm_args if isinstance(norm_args, dict) else {}
                inv: Dict[str, Any] = {
                    "call_id": tc_id,
                    "ambiguous_id": tc.get("_wsa_ambiguous_id"),
                    "seq": seq_idx,
                    "src_pos": src_pos_by_id.get(tc_id, -1),
                    "name": name,
                    "args": args,
                    "explicit_depends_on": explicit_deps,
                    "target_paths": _extract_inv_target_paths(name, args),
                    "status": "validated" if val_ok else "validation_error",
                    "reason": val_err or "",
                }

                if not quiet_ui:
                    R.print_tool_call(name, args)
                if event_callback:
                    event_callback("tool_call", {"role": role, "call_id": tc_id, "tool": name, "args": args})

                if not val_ok:
                    result = val_err or f"[Validation Error] Invalid arguments for '{name}'."
                    exec_status, rclass = _parse_exec_status_from_result(result, True)
                    inv["status"] = "scope_error" if result.startswith("[Scope Error]") else "validation_error"
                    inv["reason"] = result
                    batch_invs.append(inv)
                    if not quiet_ui:
                        R.print_tool_result(result, name, is_error=True)
                    if event_callback:
                        event_callback(
                            "tool_result",
                            {
                                "role": role,
                                "call_id": tc_id,
                                "tool": name,
                                "ok": False,
                                "execution_status": exec_status,
                                "result_class": rclass,
                                "elapsed": 0.0,
                                "preview": result[:160],
                                "result": result,
                            },
                        )
                    _record_tool_history(state, {
                        "turn": state.turn_count,
                        "role": role,
                        "call_id": tc_id,
                        "tool": name,
                        "args": args,
                        "ok": False,
                        "execution_status": exec_status,
                        "result_class": rclass,
                        "ts": time.time(),
                    })
                    tool_result_messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": name,
                        "content": result,
                    })
                    continue

                block_reason = _find_blocked_prerequisite(inv, dropped_details, batch_invs)
                if block_reason:
                    result = (
                        f"[Blocked] Skipped '{name}' ({tc_id}): {block_reason}.\n"
                        "Status: blocked (dependency_blocked — re-run after resolving the prerequisite)."
                    )
                    inv["status"] = "blocked"
                    inv["reason"] = block_reason
                    batch_invs.append(inv)
                    if not quiet_ui:
                        R.print_tool_result(result, name, is_error=True)
                    if event_callback:
                        event_callback(
                            "tool_result",
                            {
                                "role": role,
                                "call_id": tc_id,
                                "tool": name,
                                "ok": False,
                                "execution_status": "blocked",
                                "result_class": "dependency_blocked",
                                "elapsed": 0.0,
                                "preview": result[:160],
                                "result": result,
                            },
                        )
                    _record_tool_history(state, {
                        "turn": state.turn_count,
                        "role": role,
                        "call_id": tc_id,
                        "tool": name,
                        "args": args,
                        "ok": False,
                        "execution_status": "blocked",
                        "result_class": "dependency_blocked",
                        "ts": time.time(),
                    })
                    tool_result_messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": name,
                        "content": result,
                    })
                    continue

                perm_mode = getattr(state, "permission_mode", "smart")
                scope_error = write_scope_error(name, args, allowed_write_paths)
                confirm_reason = approval_reason(name, args, state) if not scope_error else ""
                approval_key = name + ":" + json.dumps(args, sort_keys=True)
                approved = True
                if denied_permissions:
                    approved = False
                    confirm_reason = "An earlier action was denied; resume only after the user reviews the blocked work"
                elif confirm_reason:
                    try:
                        approved = approval_key not in denied_permissions and request_approval(name, args, state, confirm_reason)
                    except KeyboardInterrupt:
                        interrupted_in_tools = True
                        break
                if scope_error or not approved:
                    status = "scope_error" if scope_error else "permission_denied"
                    if not approved:
                        denied_permissions.add(approval_key)
                    result = (f"[Scope Error] {scope_error}" if scope_error else
                              f"[Permission denied] Approval not granted in {perm_mode} mode: {confirm_reason}")
                    inv["status"], inv["reason"] = status, result
                    batch_invs.append(inv)
                    if not quiet_ui:
                        R.print_tool_result(result, name, is_error=True)
                    if event_callback:
                        event_callback("tool_result", {"role": role, "call_id": tc_id, "tool": name,
                            "ok": False, "execution_status": "rejected", "result_class": status,
                            "elapsed": 0.0, "preview": result[:160], "result": result})
                    _record_tool_history(state, {"turn": state.turn_count, "role": role, "call_id": tc_id,
                        "tool": name, "args": args, "ok": False, "execution_status": "rejected",
                        "result_class": status, "ts": time.time()})
                    tool_result_messages.append({"role": "tool", "tool_call_id": tc_id,
                                                 "name": name, "content": result})
                    continue

                t_start = time.monotonic()
                cap_sr = None
                if name == "visual_inspect" and use_live_ui:
                    cap_label = describe_capture_viewports(args)
                    cap_sr = _make_renderer(
                        effective_model,
                        cap_label,
                        round_num,
                        stage_override=stage_label or "visual-capture",
                        flush_override=False,
                    )
                    _set_renderer_visual_hint(
                        cap_sr,
                        cap_label,
                        with_elapsed=True,
                        reset_stage_clock=True,
                    )
                    cap_sr.start_thinking()
                try:
                    result = execute_tool(name, args)
                except KeyboardInterrupt:
                    interrupted_in_tools = True
                    break
                finally:
                    _close_stream_ui(cap_sr)
                t_elapsed = max(0.0, time.monotonic() - t_start)
                is_err = _is_error_result_text(result)
                if name == "visual_inspect":
                    cap_ms = max(0, int(round(t_elapsed * 1000.0)))
                    if not is_err:
                        batch_visual_capture_ms = (batch_visual_capture_ms or 0) + cap_ms
                    else:
                        _fail_tr = VisualReviewTracker(effective_model, delegated=False)
                        _fail_tr.set_capture_ms(cap_ms)
                        _fail_line = _fail_tr.finish("capture_failed")
                        _emit_visual_final(_fail_tr, _fail_line, "capture_failed")
                exec_status, rclass = _parse_exec_status_from_result(result, is_err)
                if name == "spawn_subagent" and not is_err:
                    try:
                        subagent_result = json.loads(result)
                        if not isinstance(subagent_result, dict) or subagent_result.get("ok") is not True:
                            is_err, exec_status, rclass = True, "failed", "subagent_failed"
                    except (TypeError, ValueError):
                        is_err, exec_status, rclass = True, "failed", "subagent_failed"
                # Command failures carry structured status rather than an
                # [Error] prefix. Treat them as failures everywhere, including
                # dependency gating, tool history, and result callbacks.
                is_err = is_err or exec_status == "timed_out" or rclass in (
                    "non_zero_exit", "command_not_found", "syntax_error", "permission_error", "timeout"
                )
                if name in ("run_command", "run_python", "run_tests"):
                    is_check = _is_verification_command(name, args, "Exit code: 0")
                    if (is_err and is_check) or _is_verification_command(name, args, result):
                        verification_evidence.pop(approval_key, None)
                        verification_evidence[approval_key] = {"tool": name, "ok": not is_err,
                                                              "result_class": rclass}
                inv["status"] = (
                    "timed_out" if exec_status == "timed_out"
                    else ("error" if (is_err or rclass in ("command_not_found", "syntax_error", "permission_error")) else rclass)
                )
                inv["reason"] = result.splitlines()[0][:160] if (is_err or inv["status"] == "error") else ""
                batch_invs.append(inv)
                if not quiet_ui:
                    R.print_tool_result(result, name, is_error=is_err, elapsed=t_elapsed)
                if event_callback:
                    event_callback(
                        "tool_result",
                        {
                            "role": role,
                            "call_id": tc_id,
                            "tool": name,
                            "ok": not is_err,
                            "execution_status": exec_status,
                            "result_class": rclass,
                            "elapsed": t_elapsed,
                            "preview": result[:160],
                            "result": result,
                        },
                    )

                path_arg = _rel_tool_path(args.get("path", ""), state.project_root)
                if name in ("write_file", "patch_file", "apply_patch", "delete_file", "move_file", "copy_file") and not is_err:
                    unmutated_read_counts.clear()
                    verification_evidence.clear()  # prior checks describe the pre-edit version
                    state._workspace_snapshot = None
                    verified_after_last_edit = False
                    mutated_paths, written_paths = _extract_mutated_and_written_paths(name, args, state.project_root)
                    if name == "move_file" and path_arg:
                        written_this_turn.discard(path_arg)
                        state.written_files.discard(path_arg)
                    for mp in mutated_paths:
                        verified_this_turn.discard(mp)
                        state.verified_files.discard(mp)
                        if Path(mp).suffix.lower() in _UI_VERIFY_EXTS:
                            browser_verified_after_ui_edit = False
                    for wp in written_paths:
                        written_this_turn.add(wp)
                        state.written_files.add(wp)
                    if written_paths and active_requires_security_pass and not security_skills_read_this_turn:
                        security_verified_after_edit = False
                    if written_paths and not test_check_done:
                        test_check_done = True
                        state._test_check_done = True
                        tests_exist = _check_tests_exist(state.project_root)
                        state._tests_exist = tests_exist
                if name == "read_skill" and not is_err:
                    raw_snames = str(args.get("name") or "")
                    for tok in re.split(r"[,;\s]+", raw_snames):
                        norm_s = normalize_skill_name(tok)
                        if norm_s:
                            skills_read_this_turn.add(norm_s)
                            if hasattr(state, "loaded_skills") and isinstance(state.loaded_skills, set):
                                state.loaded_skills.add(norm_s)
                            if norm_s in ("security-audit", "semgrep-guardrails"):
                                security_skills_read_this_turn = True
                                security_verified_after_edit = True
                if name == "rollback" and not is_err and str(args.get("action") or "rollback").lower() == "rollback":
                    unmutated_read_counts.clear()
                    verification_evidence.clear()
                    state._workspace_snapshot = None
                    verified_after_last_edit = False

                if name == "read_file" and not is_err and path_arg:
                    verified_this_turn.add(path_arg)
                    state.verified_files.add(path_arg)

                if name in _READ_REPEAT_TOOLS and not is_err:
                    rsig = _canonical_tool_sig(name, args)
                    unmutated_read_counts[rsig] = unmutated_read_counts.get(rsig, 0) + 1
                    if unmutated_read_counts[rsig] >= 3 and result:
                        result += (
                            "\n[Note: You already executed this exact read earlier in this turn with no file edits since. "
                            "Synthesize what you have and proceed directly to write_file / patch_file or verification.]"
                        )

                if name in ("run_command", "run_python", "run_tests") and not is_err and _is_verification_command(name, args, result):
                    verified_after_last_edit = True
                    if isinstance(active_ledger, FileContextLedger):
                        active_ledger.mark_verified(written_this_turn or verified_this_turn)
                    lower_res = result.lower()
                    if any(tok in lower_res for tok in ("dom ", "playwright", "headless", "selector", "all buttons ok")):
                        browser_verified_after_ui_edit = True
                elif name in ("browser_inspect", "browser_interact") and not is_err:
                    if "ALL CLEAR (0 errors/overflow)" in result:
                        browser_verified_after_ui_edit = True
                        if not tests_exist:
                            verified_after_last_edit = True
                elif name == "visual_inspect":
                    unrenderable_intermediate = (
                        is_err
                        and role == "coder"
                        and "No index.html found in directory:" in result
                        and not _has_renderable_ui_entry(state.project_root, list(written_this_turn))
                    )
                    visual_clean = not is_err and result.rstrip().endswith("\nVisual checks: pass")
                    has_console_errors = bool(
                        re.search(r"(?m)^Console/JS Errors\s*\(", result)
                        or re.search(r"(?m)^Console/JS Errors:\s*[1-9]\d*", result)
                        or re.search(r"(?m)^Failed Network Requests\s*\(", result)
                    )
                    browser_verified_after_ui_edit = visual_clean
                    verification_evidence.pop(approval_key, None)
                    if not unrenderable_intermediate:
                        verification_evidence[approval_key] = {
                            "tool": name,
                            "ok": bool(visual_clean),
                            "result_class": "capture_failed" if is_err else ("ok" if visual_clean else "visual_defects"),
                            "has_console_errors": has_console_errors,
                        }
                    if not is_err:
                        capture_keys.append(approval_key)
                    if (visual_clean or (defer_visual_gate and not is_err and not has_console_errors)) and not tests_exist:
                        verified_after_last_edit = True

                _record_tool_history(state, {
                    "turn": state.turn_count,
                    "role": role,
                    "call_id": tc_id,
                    "tool": name,
                    "args": args,
                    "ok": not is_err,
                    "execution_status": exec_status,
                    "result_class": rclass,
                    "ts": time.time(),
                })

                tool_result_messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": name,
                    "content": result,
                })
            finally:
                if has_file_mutations:
                    commit_edit_checkpoint()

            if tool_calls:
                failed_sigs = []
                all_failed = True
                for tc_idx, tc_item in enumerate(tool_calls):
                    tc_fn = tc_item.get("function") or {}
                    tc_name = str(tc_fn.get("name") or "")
                    tc_raw = tc_fn.get("arguments", "{}")
                    tc_res = tool_result_messages[tc_idx]["content"] if tc_idx < len(tool_result_messages) else ""
                    tc_err = _is_error_result_text(tc_res)
                    if not tc_err:
                        all_failed = False
                        break
                    failed_sigs.append(f"{tc_name}:{str(tc_raw)[:120]}:{tc_res[:120]}")
                if all_failed and failed_sigs:
                    round_sig = "|".join(failed_sigs)
                    if round_sig == last_failure_sig:
                        consecutive_same_failures += 1
                    else:
                        last_failure_sig = round_sig
                        consecutive_same_failures = 1
                    if consecutive_same_failures == 3 and tool_result_messages:
                        tool_result_messages[-1]["content"] += (
                            "\n\n[System: Identical tool failure repeated 3 times. "
                            "Re-read the target file or inspect exact paths/errors before retrying.]"
                        )
                    elif consecutive_same_failures >= 5:
                        messages.extend(tool_result_messages)
                        new_messages.extend(tool_result_messages)
                        aborted_reason = "identical_tool_failure_loop"
                        if not quiet_ui:
                            R.print_error("Stopped runaway loop: identical tool failure repeated 5 times consecutively.")
                        break
                else:
                    last_failure_sig = ""
                    consecutive_same_failures = 0

                last_fn = (tool_calls[-1].get("function") or {})
                last_name = last_fn.get("name", "tool")
                try:
                    last_args = json.loads(last_fn.get("arguments", "{}")) if isinstance(last_fn.get("arguments"), str) else (last_fn.get("arguments") or {})
                except Exception:
                    last_args = {}
                verb, preview_arg = R._format_tool_header(last_name, last_args)
                if len(tool_calls) > 1:
                    last_tool_hint = f"Synthesizing results from {len(tool_calls)} tool calls (latest: {verb}({preview_arg}))…" if preview_arg else f"Synthesizing results from {len(tool_calls)} tool calls…"
                else:
                    last_tool_hint = f"Analyzing {verb}({preview_arg}) output…" if preview_arg else f"Analyzing {verb} output…"

            if interrupted_in_tools:
                # Results are appended 1:1 in call order, so every call past this index was never answered.
                # Persist a complete assistant/tool pair so the final non-partial checkpoint cannot flush
                # an assistant message carrying orphaned tool_calls into state.messages.
                for unanswered in tool_calls[len(tool_result_messages):]:
                    tool_result_messages.append({
                        "role": "tool",
                        "tool_call_id": unanswered.get("id", "call_0"),
                        "name": str((unanswered.get("function") or {}).get("name") or ""),
                        "content": "[Interrupted] Tool call was not executed: the user interrupted this turn.",
                    })
                messages.extend(tool_result_messages)
                new_messages.extend(tool_result_messages)
                _abort_interrupted()
                break

            if dropped_warnings and tool_result_messages:
                tool_result_messages[-1]["content"] += (
                    "\n\n[System Warning — Dropped Tool Block(s) Detected]: Additional tool_call block(s) in your response "
                    f"could not be parsed ({'; '.join(dropped_warnings[:4])}). Fix JSON/schema errors and re-emit ONLY missing ```tool_call block(s)."
                )

            if (
                tests_exist
                and written_this_turn
                and not verified_after_last_edit
                and not any(m.get("name") in ("run_command", "run_python") for m in tool_result_messages)
                and tool_result_messages
                and not tool_result_messages[-1]["content"].startswith("[System: Tests")
            ):
                tool_result_messages[-1]["content"] += (
                    "\n\n[System: Test files detected in this project. "
                    "After all edits are complete, run: python -m pytest -x -q]"
                )

            messages.extend(tool_result_messages)
            new_messages.extend(tool_result_messages)
            if denied_permissions:
                aborted_reason = "permission_denied"
                final_assistant_text = "Stopped: an action needs your approval. Review the denied action before resuming."
                break
            pending_imgs = consume_pending_images()
            if pending_imgs:
                from .session import vision_critic_providers, provider_supports_vision
                critic_text = None
                critic_model = None
                sheets = [img for img in pending_imgs if img.get("source_tool") == "visual_inspect"]
                needs_critic = bool(sheets) and not provider_supports_vision(effective_model)
                critic_interrupted = False
                if needs_critic:
                    delegated_tracker = VisualReviewTracker(effective_model, delegated=True)
                    if batch_visual_capture_ms is not None:
                        delegated_tracker.set_capture_ms(batch_visual_capture_ms)
                    reviewers = (
                        vision_critic_providers(state, exclude=effective_model)
                        if len(sheets) <= 4
                        else []
                    )
                    telemetry_blob = "\n".join(
                        str(m.get("content") or "")
                        for m in tool_result_messages
                        if m.get("name") == "visual_inspect"
                    )
                    critic_sr = None
                    prev_reviewer = ""
                    last_timings: Optional[Dict[str, Any]] = None
                    last_error_kind = ""
                    had_transport_error = False
                    had_unusable = False
                    try:
                        if reviewers and use_live_ui:
                            critic_sr = _make_renderer(
                                reviewers[0],
                                "",
                                round_num,
                                stage_override=stage_label or "visual-review",
                                flush_override=False,
                            )
                        for idx_rev, cand_model in enumerate(reviewers):
                            critic_model = cand_model
                            critic_req_id = uuid.uuid4().hex[:12]
                            hint_line = delegated_tracker.begin_review(
                                critic_req_id,
                                cand_model,
                                fallback_from=prev_reviewer,
                            )
                            if critic_sr is not None:
                                _set_renderer_visual_hint(
                                    critic_sr,
                                    hint_line,
                                    with_elapsed=True,
                                    reset_stage_clock=True,
                                )
                                if idx_rev == 0:
                                    critic_sr.start_thinking()

                            def _on_critic_relay(
                                ev: Dict[str, Any],
                                _tr: VisualReviewTracker = delegated_tracker,
                                _rid: str = critic_req_id,
                                _sr: Any = critic_sr,
                            ) -> None:
                                if _tr.closed or _tr.active_request_id != _rid:
                                    return
                                upd = _tr.on_relay_event(ev)
                                if upd is not None and _sr is not None:
                                    txt, with_el = upd
                                    st = str(ev.get("stage") or "").strip().lower()
                                    _set_renderer_visual_hint(
                                        _sr,
                                        txt,
                                        with_elapsed=with_el,
                                        reset_stage_clock=(st == "submitted"),
                                    )

                            attempt_meta: Dict[str, Any] = {}
                            critic_text = _run_visual_critic_bridge(
                                sheets,
                                telemetry_blob,
                                state,
                                client,
                                cand_model,
                                relay_id=relay_id_override,
                                worker_session_id=effective_session_id,
                                on_relay_status=_on_critic_relay,
                                client_request_id=critic_req_id,
                                meta_out=attempt_meta,
                            )
                            if isinstance(attempt_meta.get("relay_timings"), dict):
                                last_timings = attempt_meta["relay_timings"]
                            if critic_text is not None:
                                break
                            prev_reviewer = cand_model
                            ek = str(attempt_meta.get("error_kind") or "")
                            if ek in ("transport error", "timeout", "rate limit", "prompt too long"):
                                had_transport_error = True
                                last_error_kind = ek
                            else:
                                had_unusable = True
                    except KeyboardInterrupt:
                        critic_interrupted = True
                    finally:
                        _close_stream_ui(critic_sr)

                    if critic_interrupted:
                        _cancel_line = delegated_tracker.finish("cancelled")
                        _emit_visual_final(delegated_tracker, _cancel_line, "cancelled")
                        _abort_interrupted()
                        break

                    if critic_text is not None:
                        _defects_n = len(critic_text.get("defects") or [])
                        if critic_text.get("pass") is True:
                            _del_outcome = "passed"
                            _del_line = delegated_tracker.finish(
                                "passed", defects_count=0, timings=last_timings
                            )
                        else:
                            _del_outcome = "defects"
                            _del_line = delegated_tracker.finish(
                                "defects", defects_count=_defects_n, timings=last_timings
                            )
                    elif reviewers and had_transport_error and not had_unusable:
                        _del_outcome = "transport_error"
                        _del_line = delegated_tracker.finish(
                            "transport_error",
                            error_kind=last_error_kind or "transport error",
                            timings=last_timings,
                        )
                    else:
                        _del_outcome = "unavailable"
                        _del_line = delegated_tracker.finish("unavailable", timings=last_timings)
                    _emit_visual_final(delegated_tracker, _del_line, _del_outcome)
                if needs_critic:
                    for key in capture_keys:
                        evidence = verification_evidence.get(key)
                        if evidence is None:
                            continue  # A later edit in this batch invalidated the capture.
                        if critic_text is not None:
                            evidence["reviewer"] = critic_model
                        if critic_text is None or not critic_text["pass"]:
                            evidence.update(ok=False, result_class="review_unavailable" if critic_text is None else "visual_defects")
                            browser_verified_after_ui_edit = verified_after_last_edit = False
                if critic_text and critic_model:
                    vis_msg = {
                        "role": "user",
                        "content": (
                            f"[Visual Critic via {R.friendly_model_name(critic_model)}]:\n"
                            f"{json.dumps(critic_text, ensure_ascii=False)[:3000]}\n\n"
                            "Use these visual findings together with the exact telemetry above to fix any defects "
                            "and verify cleanly."
                        ),
                    }
                    messages.append(vis_msg)
                    new_messages.append(vis_msg)
                elif needs_critic:
                    vis_msg = {"role": "user", "content": (
                        "[Visual inspection] No visual critique was available. Use the reported telemetry "
                        "and identify any visual judgments that still require inspection."
                        + (" Request at most four captures per review." if len(sheets) > 4 else "")
                    )}
                    messages.append(vis_msg)
                    new_messages.append(vis_msg)
                else:
                    direct_tracker = VisualReviewTracker(effective_model, delegated=False)
                    if batch_visual_capture_ms is not None:
                        direct_tracker.set_capture_ms(batch_visual_capture_ms)
                    pending_direct_visual_tracker = direct_tracker
                    if sheets and capture_keys:
                        pending_direct_visual_local_clean = all(
                            bool((verification_evidence.get(k) or {}).get("ok"))
                            for k in capture_keys
                        )
                    else:
                        pending_direct_visual_local_clean = None
                    visual_parts: List[Dict] = [{
                        "type": "text",
                        "text": (
                            "[Visual Attachment — Art-Director & Dream-Loop Critique]: Carefully inspect the attached "
                            "screenshot/image(s) ("
                            + ", ".join(im.get("filename", "image") for im in pending_imgs)
                            + ") and telemetry (`[1][2][3]` Set-of-Mark badges) as a ruthless senior Awwwards / Igloo.inc Art Director:\n"
                            "- 3D & Background Cohesion: Reject crude 'Saturn-toy' primitive collages (sphere + torus rings + cones) or harsh two-tone horizon cuts (black sky abruptly meeting a bright floor mesh). Ensure unified `scene.background`, `FogExp2`, and CSS `--bg` continuity.\n"
                            "- Text-Over-Canvas Readability & Layering: Reject unbacked dark text over dark 3D sky/geometry, 3D geometry slicing behind headlines, or transparent sticky headers that let scrolled content bleed behind nav links.\n"
                            "If ANY visual, aesthetic, contrast, or layout defect is visible, refine the shaders, lighting/fog, layout safe-zones, or frosted CSS surfaces and re-verify before concluding."
                        ),
                    }]
                    for im in pending_imgs[:4]:
                        if im.get("url"):
                            visual_parts.append({"type": "image_url", "image_url": {"url": im["url"]}})
                    vis_msg = {"role": "user", "content": visual_parts}
                    messages.append(vis_msg)
                    new_messages.append(vis_msg)
            if messages and messages[0].get("role") == "system":
                messages[0] = _make_system_msg(effective_model)
            if (round_num + 1) % 5 == 0:
                _checkpoint_turn_progress(partial=True)
            if not quiet_ui:
                R.console.print()
        else:
            aborted_reason = f"max_rounds_{max_rounds}"
            if not quiet_ui:
                R.print_warn(f"Reached {max_rounds} tool rounds — stopping.")

        _checkpoint_turn_progress(partial=bool(aborted_reason))
    finally:
        set_active_file_ledger(prev_ledger)
        set_strict_read_before_edit(prev_strict)
        if prev_subagent_ctx is not None:
            try:
                from .subagents import set_subagent_runtime_context
                set_subagent_runtime_context(prev_subagent_ctx[0], prev_subagent_ctx[1])
            except Exception:
                pass

    if denied_permissions and not aborted_reason:
        aborted_reason = "permission_denied"
    if enforce_verify_gate and written_this_turn and not verified_after_last_edit and not aborted_reason:
        aborted_reason = "verification_incomplete"
    if verification_evidence and not aborted_reason:
        # A coding turn can correct a faulty check command and rerun it. Keep
        # all evidence for the independent verifier; do not permanently poison
        # coder completion with a historical failed attempt followed by success.
        # Read-only exploration/planning, zero-write baseline visual checks, and
        # downstream-deferred visual defect checks on intermediate DAG slices
        # must not fail as verification_failed unless capture/console/code checks fail.
        checks = list(verification_evidence.values())
        if role not in ("explorer", "architect"):
            is_baseline_visual_only = (
                role == "coder"
                and not written_this_turn
                and not allowed_write_paths
                and any(not e["ok"] for e in checks)
                and all(e.get("tool") == "visual_inspect" for e in checks if not e["ok"])
            )
            if not is_baseline_visual_only:
                if defer_visual_gate and role == "coder":
                    effective_checks = [
                        e for e in checks
                        if not (
                            e.get("tool") == "visual_inspect"
                            and e.get("result_class") == "visual_defects"
                            and not e.get("has_console_errors")
                        )
                    ]
                else:
                    effective_checks = checks
                if effective_checks:
                    unresolved = any(not e["ok"] for e in effective_checks) if role == "verifier" else not effective_checks[-1]["ok"]
                    if any(e["tool"] == "visual_inspect" and not e["ok"] for e in effective_checks):
                        browser_verified_after_ui_edit = False
                        unresolved = True
                    if unresolved:
                        aborted_reason = "verification_failed"
    if not quiet_ui and aborted_reason in ("verification_incomplete", "verification_failed", "permission_denied"):
        R.print_warn("Turn incomplete: " + aborted_reason.replace("_", " ") + ".")
    return {
        "ok": not bool(aborted_reason),
        "role": role,
        "final_text": final_assistant_text,
        "written_files": sorted(written_this_turn),
        "verified_files": sorted(verified_this_turn),
        "verified_after_last_edit": bool(verified_after_last_edit),
        "browser_verified": bool(browser_verified_after_ui_edit),
        "rounds": rounds_used,
        "tool_calls_count": tool_calls_count,
        "aborted_reason": aborted_reason,
        "limit_exhausted": bool(limit_exhausted),
        "budget_hallucinated": bool(budget_hallucinated),
        "messages": new_messages,
        "verification_evidence": list(verification_evidence.values()),
        "skills_read": sorted(skills_read_this_turn),
        "model": effective_model,
        "chat_url": str(effective_chat_url or ""),
    }


@serialized_turn
def run_turn(user_input: str, state: SessionState, client: WSAClient) -> Dict[str, Any]:
    """
    Execute one full agentic turn:
    1. Capture persistent constraints/decisions cleanly from user instructions.
    2. Use adaptive complexity & skill routing (`/team auto|on|off`) to either run the surgical
       single-agent loop or trigger the scoped multi-role subagent pipeline.
    """
    free_mode = getattr(state, "free_mode", False)
    if not free_mode and hasattr(state, "decisions"):
        state.decisions = capture_user_decisions(user_input, getattr(state, "decisions", None))

    team_mode = str(getattr(state, "team_mode", "auto") or "auto").lower()
    ai_skills: List[str] = []
    ai_security_pass: Optional[bool] = None
    if not free_mode and team_mode != "off":
        complexity = classify_task_complexity(user_input, state)
        # Cheap conversational turns and natural resume/continuation replies need no extra
        # model round. Substantive auto-mode requests get a tool-free judgment.
        _u_norm = " ".join(str(user_input or "").strip().lower().split())
        simple = _u_norm in {
            "hi", "hello", "thanks", "thank you", "continue", "continue your task",
            "please continue", "go on", "keep going", "proceed", "resume",
        }
        simple = simple or bool(re.fullmatch(
            r"\s*(?:try again(?: last call)?|retry(?: the)?(?: last)?(?: call| step| request)?|resume(?: the)?(?: workflow| task)?)\s*[.!]?\s*",
            str(user_input or ""), re.I,
        ))
        if not simple and len(_u_norm.split()) <= 28 and not re.search(
            r"\b(?:build|create|make|develop|implement|redesign|refactor|migrate|add\s+(?:a|an|new)|write\s+(?:a|an|new))\b",
            _u_norm,
        ):
            if re.search(
                r"\b(?:please\s+(?:continue|resume|proceed)|continue\s+please|continue\s+(?:from\s+)?where|continue\s+(?:with\s+)?(?:the\s+)?(?:remaining|next|rest|task|work|workflow)|pick\s+up\s+where|resume\s+(?:from\s+)?where|go\s+ahead(?:\s+and\s+(?:continue|proceed|resume))?|sorry\s+i\s+interrupted|keep\s+going|carry\s+on)\b",
                _u_norm,
            ):
                simple = True
        if team_mode == "auto" and not simple and not complexity.get("fast_direct") and complexity.get("mode") != "orchestrate":
            decision = _judge_workflow(user_input, state, client, complexity)
            if decision.get("stopped"):
                return decision["result"]
            complexity = decision
        ai_skills = list(complexity.get("skills") or [])
        if isinstance(complexity.get("security_pass"), bool):
            ai_security_pass = complexity["security_pass"]
        should_orchestrate = (
            complexity.get("mode") == "orchestrate"
            or (team_mode == "on" and len(str(user_input or "").split()) >= 4)
        )
        if should_orchestrate:
            from .subagents import run_orchestrated_turn
            from .visual_concept import maybe_run_visual_concept_gate
            appointment = analyze_prompt_skills(
                user_input,
                ai_skills=ai_skills,
                ai_security_pass=ai_security_pass,
            )
            vc_gate = maybe_run_visual_concept_gate(
                user_input,
                state,
                client,
                appointment=appointment,
            )
            eff_input = str(vc_gate.get("enriched_prompt") or user_input)
            R.print_info("This certain prompt Requires Long Agentic Workflow, forcing web agents in deep thinking mode...")
            R.print_info("Deep reasoning is requested in the role prompts; website thinking settings remain controlled by the selected web agent.")
            orch_kwargs: Dict[str, Any] = {"reasons": complexity.get("reasons") or []}
            if ai_skills or ai_security_pass is not None:
                orch_kwargs["skill_appointment"] = appointment
            return run_orchestrated_turn(eff_input, state, client, **orch_kwargs)

    exec_kwargs: Dict[str, Any] = {"allow_auto_decisions": False}
    if not free_mode and (ai_skills or ai_security_pass is not None):
        exec_kwargs["skill_appointment"] = analyze_prompt_skills(
            user_input,
            ai_skills=ai_skills,
            ai_security_pass=ai_security_pass,
        )
    return execute_agent_loop(
        user_input,
        state,
        client,
        **exec_kwargs,
    )


def _judge_workflow(user_input: str, state: SessionState, client: WSAClient, fallback: Dict) -> Dict:
    """One bounded, tool-free routing & skill-judging decision; never execute classifier tool calls."""
    from .prompts import WORKFLOW_JUDGMENT_PROMPT
    from .subagents import _recover_orchestrated_stage_limit
    R.print_info("Assessing workflow scope and checking relevant WSA skills...")
    while True:
        sr = None
        try:
            active_url = (
                getattr(state, "active_chat_url", "")
                or (getattr(state, "chat_urls", {}) or {}).get(state.model)
                or ""
            )
            try:
                sr = R.StreamRenderer(
                    round_num=0,
                    status_hint="Assessing workflow scope & relevant WSA skills…",
                    model=state.model,
                    stage_label="router",
                    flush_final=False,
                )
            except TypeError:
                sr = None
            if sr is not None:
                sr.start_thinking()
            req_txt = user_input.encode("utf-8", "replace")[:10000].decode("utf-8", "ignore").strip()
            prior_ctx = str(getattr(state, "context_summary", "") or "").encode("utf-8", "replace")[:1500].decode("utf-8", "ignore").strip()
            router_user_msg = (
                "Workflow routing check for the next user request (do not execute the task or call tools; "
                'reply only with JSON: {"mode":"orchestrate"|"direct","reason":"short scope-based reason","skills":["relevant-slug"],"security_pass":false}):\n\n'
                f"User request:\n{req_txt}"
            )
            if prior_ctx:
                router_user_msg += f"\n\nPrior session summary:\n{prior_ctx}"
            stream_kwargs: Dict[str, Any] = dict(
                messages=[{"role": "system", "content": WORKFLOW_JUDGMENT_PROMPT},
                          {"role": "user", "content": router_user_msg}],
                model=state.model,
                use_tools=False,
                session_id=f"{state.session_id}:role:workflow_router",
                chat_url=active_url or None,
                new_chat=False,
            )
            if sr is not None:
                stream_kwargs["on_thinking"] = sr.feed_thinking
                if hasattr(sr, "set_status_hint"):
                    stream_kwargs["on_status"] = sr.set_status_hint
            try:
                response = client.stream(**stream_kwargs)
            finally:
                _close_stream_ui(sr)
                sr = None
            if not isinstance(response, dict) or response.get("tool_calls"):
                raise ValueError("invalid workflow decision")
            if isinstance(response.get("upstream_model"), dict):
                um = response["upstream_model"]
                c_url = str(um.get("chat_url") or "")
                if c_url and is_provider_thread_url(c_url):
                    if not getattr(state, "active_chat_url", ""):
                        state.active_chat_url = c_url
                    if hasattr(state, "chat_urls") and isinstance(state.chat_urls, dict):
                        if not state.chat_urls.get(state.model):
                            state.chat_urls[state.model] = c_url
            raw = str(response.get("content") or "").strip()
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
            decision = json.loads(raw)
            if not isinstance(decision, dict) or decision.get("mode") not in ("direct", "orchestrate"):
                raise ValueError("invalid workflow mode")
            reason = decision.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("missing workflow reason")
            out_dec: Dict[str, Any] = {"mode": decision["mode"], "reasons": [reason[:240]]}
            raw_skills = decision.get("skills")
            if isinstance(raw_skills, list):
                valid_skills = [
                    norm for item in raw_skills
                    if isinstance(item, str) and (norm := normalize_skill_name(item))
                ]
                if valid_skills:
                    out_dec["skills"] = list(dict.fromkeys(valid_skills))
            if isinstance(decision.get("security_pass"), bool):
                out_dec["security_pass"] = decision["security_pass"]
            return out_dec
        except ProviderLimitError as exc:
            if state.agent_mode == "multiagent" and state.advance_multiagent_queue(state.model):
                continue
            failed = {"ok": False, "limit_exhausted": True, "aborted_reason": str(exc)}
            if _recover_orchestrated_stage_limit(state, client, failed, pending_goal=user_input, stage_label="workflow judgment"):
                continue
            return {"stopped": True, "result": failed}
        except KeyboardInterrupt:
            _preserve_workflow_state_and_context(state, pending_goal=user_input, record_pending_goal=True)
            return {"stopped": True, "result": {"ok": False, "aborted_reason": "interrupted"}}
        except (ConnectionError, ValueError, TypeError):
            R.print_warn("Workflow judgment unavailable; using local scope detection.")
            return fallback

