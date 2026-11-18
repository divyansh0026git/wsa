"""
Scoped multi-role subagent orchestrator tailored for WSA's browser-bridged LLM architecture.

Implements:
- A1: Persistent per-role threads via `state.role_session_id(role)` (no sidebar spam)
- A2: Scoped 4-role pipeline (`explorer -> architect -> coder -> verifier` + targeted repair loop)
- A3: Serialized active-relay execution; dual-lane requests remain serialized until
      shell tools can run in independently isolated workspaces.
"""
from __future__ import annotations

import json
import re
import threading
from contextvars import ContextVar
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .task_skills import select_task_skills, skill_guidance
from . import renderer as R
from .agent import (
    ACCOUNT_SWITCH_WAIT_SECS,
    _preserve_workflow_state_and_context,
    _offer_limit_multiagent,
    _prompt_all_agents_exhausted,
    _reset_provider_chats_for_account_change,
    _wait_for_account_switch_countdown,
    execute_agent_loop,
)
from .client import WSAClient
from .context import FileContextLedger, build_context_packet
from .session import SessionState
from .tools import TOOL_DEFINITIONS, set_active_file_ledger
from .runtime import serialized_turn


# ── Role definitions & least-privilege tool scoping ─────────────────────────

EXPLORER_TOOLS: Set[str] = {
    "get_file_outline",
    "read_file",
    "get_file_info",
    "search_files",
    "glob_files",
    "list_dir",
    "inspect_image",
    "recall",
    "web_fetch",
    "todo",
    "wait_for",
}

ARCHITECT_TOOLS: Set[str] = set(EXPLORER_TOOLS)

CODER_TOOLS: Set[str] = {
    "get_file_outline",
    "read_file",
    "get_file_info",
    "search_files",
    "glob_files",
    "list_dir",
    "patch_file",
    "apply_patch",
    "write_file",
    "delete_file",
    "rollback",
    "run_command",
    "run_python",
    "run_tests",
    "start_background",
    "list_background",
    "stop_background",
    "restart_service",
    "http_request",
    "inspect_image",
    "recall",
    "remember",
    "browser_tabs",
    "browser_inspect",
    "browser_interact",
    "visual_inspect",
    "move_file",
    "copy_file",
    "git",
    "web_fetch",
    "todo",
    "wait_for",
}

VERIFIER_TOOLS: Set[str] = {
    "get_file_outline",
    "read_file",
    "get_file_info",
    "search_files",
    "glob_files",
    "list_dir",
    "run_command",
    "run_python",
    "run_tests",
    "list_background",
    "http_request",
    "browser_tabs",
    "browser_inspect",
    "browser_interact",
    "visual_inspect",
    "inspect_image",
    "recall",
    "git",
    "web_fetch",
    "todo",
    "wait_for",
}

ROLE_ALLOWED_TOOLS: Dict[str, Set[str]] = {
    "explorer": EXPLORER_TOOLS,
    "architect": ARCHITECT_TOOLS,
    "coder": CODER_TOOLS,
    "verifier": VERIFIER_TOOLS,
}


def get_role_tools(role: str) -> List[Dict[str, Any]]:
    """Return filtered OpenAI tool schemas permitted for `role`."""
    allowed = ROLE_ALLOWED_TOOLS.get(str(role or "").lower(), CODER_TOOLS)
    out: List[Dict[str, Any]] = []
    for item in TOOL_DEFINITIONS:
        if not isinstance(item, dict):
            continue
        name = str((item.get("function") or item).get("name") or "").strip()
        if name in allowed:
            out.append(item)
    return out


ROLE_SYSTEM_PROMPTS: Dict[str, str] = {
    "explorer": (
        "Role: WSA Explorer — read-only codebase reconnaissance specialist (working via the user's `wsa` CLI helper).\n"
        "Guidelines:\n"
        "1. Do NOT mutate files or run shell commands.\n"
        "2. Start with `glob_files`, `list_dir`, and `search_files`; use `get_file_outline` before reading large files, "
        "then `read_file` with targeted `start_line`/`end_line` ranges.\n"
        "3. Report strictly observed file paths, line ranges, symbol signatures, existing tests, and technical risks.\n"
        "4. Format inspection calls as plain ```tool_call JSON blocks until needed facts are gathered, then summarize concisely."
    ),
    "architect": (
        "Role: WSA Architect — read-only system designer and execution planner.\n"
        "Guidelines:\n"
        "1. Review the Context Packet carefully and do light targeted reads only if an interface is unclear.\n"
        "2. Decompose the user request into a small, crisp DAG of surgical coding tasks (typically 1–5 tasks).\n"
        "3. Keep `owned_files` disjoint across sibling independent tasks whenever possible.\n"
        "4. Output your final plan inside a ```json block containing a JSON array of objects:\n"
        '   [{"id": "t1", "title": "...", "owned_files": ["path/a.py"], "depends_on": [], '
        '"acceptance": "Exact checkable outcome", "ui": false}]\n'
        "5. Do not mutate files; ground every task in real workspace paths."
    ),
    "coder": (
        "Role: WSA Coder — surgical, high-precision implementation specialist.\n"
        "Guidelines:\n"
        "1. Strictly edit ONLY files in your assigned `owned_files` scope (and any designated test file).\n"
        "2. Before modifying any existing file, run `get_file_outline` / `read_file(start_line, end_line)` first.\n"
        "3. Use surgical `patch_file` with exact surrounding anchors/hunks for existing files — avoid rewriting whole files or `TODO`/`...` stubs.\n"
        "4. Preserve existing imports, comments, style, and unrelated callers.\n"
        "5. After edits, run targeted verification (`pytest`, `py_compile`, `node --check`, or `run_python`) and fix any failure before concluding.\n"
        "6. Output clean ```tool_call JSON blocks for local actions without dumping untouched files in prose."
    ),
    "verifier": (
        "Role: WSA Verifier — independent QA, runtime, and regression verification specialist.\n"
        "Guidelines:\n"
        "1. Do NOT modify code files (no `write_file`, `patch_file`, or `delete_file`).\n"
        "2. Inspect modified files and execute real verification: test suites (`pytest`), syntax/compile checks "
        "(`py_compile`, `node --check`), functional checks (`run_python`), and UI layout/interaction checks "
        "(`visual_inspect` for zero-focus headless multi-viewport + Set-of-Mark visual verification, or "
        "`browser_inspect` / `browser_interact` when an authenticated live tab is required).\n"
        "3. Check real test pass counts and observed command outputs (not merely exit code 0) and report exact file/line defects.\n"
        "4. End your response with a strict JSON verdict block:\n"
        '   ```json\n   {"verdict": "pass", "checks": ["pytest: 12 passed"], "failures": [], "summary": "..."}\n   ```\n'
        '   Use `"verdict": "fail"` whenever any check errors, fails assertions, or leaves broken UI/runtime behavior.'
    ),
}


# ── Active delegation runtime registry (used by optional `spawn_subagent` tool) ─

_ACTIVE_DELEGATION_CTX: ContextVar = ContextVar("wsa_delegation", default=(None, None))
_DELEGATION_DEPTH: ContextVar = ContextVar("wsa_delegation_depth", default=0)


def set_subagent_runtime_context(
    state: Optional[SessionState], client: Optional[WSAClient]
) -> Tuple[Optional[SessionState], Optional[WSAClient]]:
    """Bind `(state, client)` for explicit `spawn_subagent` tool calls within an active top-level turn."""
    prev = _ACTIVE_DELEGATION_CTX.get()
    _ACTIVE_DELEGATION_CTX.set((state, client) if state is not None and client is not None else (None, None))
    return prev


def _get_subagent_depth() -> int:
    return _DELEGATION_DEPTH.get()


# ── Helpers: relay discovery, JSON parsing, DAG scheduling, UI telemetry ────

def _normalize_rel_path(path_str: str) -> str:
    from .agent import _norm_rel_path_token
    return _norm_rel_path_token(path_str)


def _discover_live_relays(client: WSAClient) -> List[Dict[str, Any]]:
    """Query server `/v1/relays` cleanly and return online connected browser relays (active first)."""
    try:
        data = client.get_relays() if hasattr(client, "get_relays") else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return []
    relays = data.get("relays")
    if not isinstance(relays, list):
        return []
    online = [
        r for r in relays
        if isinstance(r, dict) and r.get("online", True) and r.get("client_id")
    ]
    online.sort(key=lambda r: (0 if r.get("active") else 1, float(r.get("last_seen_age_sec") or 0.0)))
    return online


def _extract_json_block(text: str, expect_type: type) -> Optional[Any]:
    """Extract first JSON object or array matching `expect_type` from fenced or bare response text."""
    raw = str(text or "").strip()
    if not raw:
        return None
    fences = re.findall(r"```(?:json)?\s*\r?\n([\s\S]*?)```", raw, flags=re.IGNORECASE)
    candidates = [*fences, raw]
    decoder = json.JSONDecoder()
    for cand in candidates:
        s = cand.strip()
        if not s:
            continue
        try:
            parsed = json.loads(s)
            if isinstance(parsed, expect_type):
                return parsed
        except Exception:
            pass
        opener = "[" if expect_type is list else "{"
        idx = s.find(opener)
        while idx != -1:
            try:
                parsed, _ = decoder.raw_decode(s, idx)
                if isinstance(parsed, expect_type):
                    return parsed
            except Exception:
                pass
            idx = s.find(opener, idx + 1)
    return None


_COMMON_FILE_EXTENSIONS: Set[str] = {
    "py", "js", "ts", "jsx", "tsx", "json", "yml", "yaml", "toml",
    "md", "txt", "sh", "html", "css", "sql", "rs", "go", "c", "cpp",
    "h", "cfg", "ini", "env", "xml", "csv"
}

_FILE_PATH_RE = re.compile(r"\b([a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9_]{1,10})\b")


def _extract_file_paths_from_text(text: str) -> List[str]:
    """Extract realistic workspace relative file paths from unstructured task text."""
    if not text:
        return []
    found: List[str] = []
    for match in _FILE_PATH_RE.findall(str(text)):
        m = match.strip().strip("'\"`(),;:<>[]{}")
        if not m or " " in m:
            continue
        ext = m.rsplit(".", 1)[-1].lower() if "." in m else ""
        if ext in _COMMON_FILE_EXTENSIONS or "/" in m or "\\" in m:
            if m.startswith("http:") or m.startswith("https:"):
                continue
            if re.match(r"^v?\d+\.\d+", m):
                continue
            norm = _normalize_rel_path(m)
            if norm and norm not in found:
                found.append(norm)
    return found


def _is_verification_task(item: Dict[str, Any]) -> bool:
    """Return True if a task is primarily a verification/test/packaging/inspection step."""
    text = f"{item.get('title', '')} {item.get('description', '')}".lower()
    return any(w in text for w in (
        "verify", "verification", "full suite", "test suite", "check",
        "packaging", "package", "lint", "smoke", "e2e", "integration test",
        "audit", "no new file", "no new files", "run test", "run tests"
    ))


def _parse_architect_plan(raw_text: str, user_input: str) -> List[Dict[str, Any]]:
    """Parse Architect JSON DAG output into normalized task dictionaries with a clean fallback."""
    parsed = _extract_json_block(raw_text, list)
    tasks: List[Dict[str, Any]] = []
    if parsed:
        for idx, item in enumerate(parsed):
            if not isinstance(item, dict):
                continue
            tid = str(item.get("id") or f"t{idx + 1}").strip()
            title = str(item.get("title") or item.get("description") or f"Task {idx + 1}").strip()
            desc = str(item.get("description") or "").strip()
            owned = [_normalize_rel_path(p) for p in (item.get("owned_files") or []) if str(p or "").strip()]
            deps = [str(d).strip() for d in (item.get("depends_on") or []) if str(d or "").strip()]

            raw_acc = item.get("acceptance")
            if isinstance(raw_acc, list):
                acceptance = "; ".join(str(x).strip() for x in raw_acc if str(x).strip())
            elif isinstance(raw_acc, dict):
                acceptance = "; ".join(f"{k}: {v}" for k, v in raw_acc.items())
            else:
                acceptance = str(raw_acc or "Pass targeted checks cleanly").strip()

            # Auto-heal / infer owned_files if empty
            if not owned:
                inferred = _extract_file_paths_from_text(f"{title} {desc} {acceptance}")
                if _is_verification_task(item) or not item.get("owned_files"):
                    dep_set = set(deps)
                    dep_files = [
                        p for t in tasks if t["id"] in dep_set for p in t.get("owned_files", [])
                    ]
                    if not dep_files and tasks:
                        dep_files = [p for t in tasks for p in t.get("owned_files", [])]
                    owned = sorted(set(inferred + dep_files))
                elif inferred:
                    owned = inferred

            tasks.append({
                "id": tid,
                "title": title,
                "description": desc,
                "owned_files": owned,
                "depends_on": deps,
                "acceptance": acceptance,
                "ui": bool(item.get("ui", False)),
                "status": "pending",
            })
    if not tasks:
        tasks = [{
            "id": "t1",
            "title": (str(user_input or "").strip().splitlines()[0] or "Implement requested changes")[:90],
            "description": "",
            "owned_files": [],
            "depends_on": [],
            "acceptance": "Complete all requested edits and verify checks pass",
            "ui": False,
            "status": "pending",
        }]
    # Preserve invalid/self dependencies so execution fails closed instead of
    # silently removing prerequisites from the architect's plan.
    return tasks


def _architect_plan_error(raw_text: str) -> str:
    parsed = _extract_json_block(raw_text, list)
    if not parsed:
        return "Architect did not return a non-empty task array"
    ids = []
    for item in parsed:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            return "Each task must have a non-empty string ID"
        ids.append(item["id"].strip())
        for key in ("owned_files", "depends_on"):
            value = item.get(key, [])
            if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
                return f"Task {ids[-1]} has invalid {key}"
    if len(set(ids)) != len(ids):
        return "duplicate task IDs"
    # Ensure at least one task in the DAG declares concrete owned_files, or file paths
    # can be inferred from task descriptions/acceptance. Verification/packaging tasks
    # (e.g. full suite verification) are permitted to have empty owned_files.
    has_any_files = any(bool(item.get("owned_files")) for item in parsed)
    if not has_any_files:
        for item in parsed:
            txt = f"{item.get('title', '')} {item.get('description', '')} {item.get('acceptance', '')}"
            if _extract_file_paths_from_text(txt):
                has_any_files = True
                break
    if not has_any_files:
        return "Each coder task must declare explicit owned_files"
    return ""


_VERIFIER_FAIL_TRAIL_RE = re.compile(r"(?i)\b(fail(?:ed|ures?|s|ing)?|error(?:s|ed)?|broken|contradict\w*)\b")
_VERIFIER_CHECK_SUBJECT_RE = re.compile(
    r"(?i)\b(?:error[- ]handling|failure[- ]recovery|failure[- ]handling)\b"
)
_VERIFIER_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
_VERIFIER_SAFE_FAILURE_RE = re.compile(
    r"(?i)\b(?:0|zero|no)\s+(?:fail(?:ed|ures?|s)?|errors?)\b"
    r"|\b(?:failures?|errors?)\s*:\s*(?:none|0|zero)\b"
    r"|\b(?:without|no)\s+(?:any\s+)?(?:failures?|errors?)\b"
)


def _verifier_trailing_contradicts(trailing: str) -> bool:
    """True only when a trailing sentence claims failure without being negated.

    Sentence-scoped rather than window-scoped so ordinary closers such as
    "542 passed, 0 failed" or "failures: none" stay benign, while a real
    contradiction like "Tests actually failed." is still rejected. A bare 0
    only negates when it directly precedes the failure word, so "got 0" after
    an explicit FAIL is still treated as a contradiction.
    """
    for sentence in _VERIFIER_SENTENCE_SPLIT_RE.split(trailing or ""):
        if not sentence.strip():
            continue
        # Remove only explicit zero-failure phrases. A separate negation in
        # the same sentence must not hide an actual failure assertion.
        remaining = _VERIFIER_SAFE_FAILURE_RE.sub("", sentence)
        # These name functionality under test, not its outcome. Keep the rest
        # of the sentence so 'error handling tests failed' still contradicts.
        remaining = _VERIFIER_CHECK_SUBJECT_RE.sub("", remaining)
        if _VERIFIER_FAIL_TRAIL_RE.search(remaining):
            return True
    return False


def _parse_verifier_verdict(raw_text: str, exec_res: Dict[str, Any]) -> Dict[str, Any]:
    """Accept a typed final JSON verdict backed by local verification evidence."""
    def failed(reason, *, format_error=False):
        return {"verdict": "fail", "checks": [], "failures": [{"file": "verifier", "issue": reason}], "summary": reason, "format_error": format_error}

    if exec_res.get("ok") is not True and exec_res.get("aborted_reason") != "verification_failed":
        return failed(str(exec_res.get("aborted_reason") or "Verifier did not complete"))
    raw = str(raw_text or "").strip()
    fences = list(re.finditer(r"```(?:json)?\s*\r?\n([\s\S]*?)```", raw, re.I))
    if fences:
        trailing = raw[fences[-1].end():].strip()
        # Benign closing prose is tolerated; only a contradicting failure claim is rejected.
        if trailing and _verifier_trailing_contradicts(trailing):
            return failed("Verifier must end with its JSON verdict")
        raw = fences[-1].group(1).strip()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return failed("Verifier did not return a valid final JSON verdict", format_error=True)
    if not isinstance(parsed, dict) or parsed.get("verdict") not in ("pass", "fail"):
        return failed("Invalid verifier verdict", format_error=True)
    checks, failures = parsed.get("checks"), parsed.get("failures")
    if not isinstance(checks, list) or any(not isinstance(c, str) or not c.strip() for c in checks):
        return failed("Verifier checks must be a list of non-empty strings", format_error=True)
    if not isinstance(failures, list) or any(
        not isinstance(f, dict) or not isinstance(f.get("issue"), str) or not f["issue"].strip()
        for f in failures
    ):
        return failed("Verifier failures must be a list of issue objects", format_error=True)
    if not isinstance(parsed.get("summary", ""), str):
        return failed("Verifier summary must be a string", format_error=True)
    if parsed["verdict"] == "pass" and not failures:
        if any(_verifier_trailing_contradicts(s) for s in [*checks, parsed.get("summary", "")]):
            return failed("Verifier pass contradicts its checks or summary")
        if exec_res.get("ok") is not True:
            return failed("Verifier has unresolved failed verification checks")
        evidence = exec_res.get("verification_evidence")
        if not checks or not isinstance(evidence, list) or not evidence:
            return failed("Verifier claimed success without a locally executed verification check")
        if any(not isinstance(e, dict) or e.get("ok") is not True for e in evidence):
            return failed("Verifier has unresolved failed verification checks")
    return {"verdict": "fail" if failures else parsed["verdict"], "checks": checks,
            "failures": failures, "summary": parsed.get("summary", "").strip()}


def _ui_render_plan(tasks: List[Dict[str, Any]]) -> None:
    if hasattr(R, "print_plan_table"):
        R.print_plan_table(tasks)
        return
    R.print_info(f"Execution Plan ({len(tasks)} task(s)):")
    for t in tasks:
        owned = ", ".join(t.get("owned_files") or []) or "*"
        R.print_info(f"  [{str(t.get('status') or 'pending').upper()}] {t.get('id')}: {t.get('title')} ({owned})")


def _ui_stage_banner(role: str, label: str) -> None:
    if hasattr(R, "print_subagent_banner"):
        R.print_subagent_banner(role, label)
    else:
        R.print_info(f"[{role.upper()}] {label}")


def _make_role_event_cb(stage_label: str) -> Callable[[str, Dict[str, Any]], None]:
    def _cb(kind: str, payload: Dict[str, Any]) -> None:
        if hasattr(R, "print_subagent_event"):
            R.print_subagent_event(stage_label, kind, payload)
            return
        if kind == "failover":
            frm = payload.get("from_model") or payload.get("from") or "?"
            to = payload.get("to_model") or payload.get("to") or "?"
            reason = str(payload.get("reason") or payload.get("message") or "limit reached")[:70]
            R.print_warn(f"  [{stage_label}] failover: {frm} -> {to} ({reason})")
        elif kind == "tool_call":
            tname = payload.get("tool", "tool")
            args = payload.get("args") or {}
            arg_hint = args.get("path") or args.get("command") or args.get("pattern") or ""
            R.print_info(f"  [{stage_label}] {tname}({str(arg_hint)[:50]})")
        elif kind == "tool_result" and not payload.get("ok", True):
            R.print_warn(f"  [{stage_label}] {payload.get('tool')} error: {str(payload.get('preview') or '')[:90]}")
    return _cb


CLAUDE_STAGE_DIRECTIVES: Dict[str, str] = {
    "explorer": (
        "Workflow Phase — Explorer: For this stage of our task, let's focus strictly on read-only codebase reconnaissance.\n"
        "Please stick to the read/search JSON shapes listed below (`read_file`, `get_file_outline`, `get_file_info`, `search_files`, `glob_files`, `list_dir`) rather than proposing edits.\n"
        "Keep the concise coding-agent style: state in 1 short line what you're inspecting, emit the ```tool_call JSON block(s), wait for the real output, and once you have exact facts and line ranges, summarize your findings crisply."
    ),
    "architect": (
        "Workflow Phase — Architect: For this stage, let's design a concrete, file-exact editing plan before writing code.\n"
        "If anything in an interface is still unclear, emit a brief technical line + read-only ```tool_call block.\n"
        "Otherwise, decompose the request into a small, crisp DAG of surgical tasks (typically 1–6 tasks) and output your final plan inside a ```json block containing a JSON array of objects:\n"
        '   [{"id": "t1", "title": "...", "owned_files": ["path/a.py"], "depends_on": [], "acceptance": "Exact checkable outcome", "ui": false}]\n'
        "Guidelines:\n"
        "1. Every coding task must declare explicit `owned_files` (relative workspace paths to create or edit).\n"
        "2. For full-suite verification or packaging tasks, list the key test files or modules being validated in `owned_files`.\n"
        "3. Keep `owned_files` disjoint across sibling independent tasks whenever possible.\n"
        "4. Ground every task in real workspace paths and do not emit code edits in this phase."
    ),
    "coder": (
        "Workflow Phase — Coder: For this stage, please recommend the exact surgical code changes via my local `wsa` CLI.\n"
        "Inspect any target region not yet read together before patching, lead with a crisp 1-line diagnosis + ```tool_call block (`patch_file` for existing files; `write_file` only for new/small files) preserving unrelated comments/behavior, and follow up with `run_command` / `run_python` blocks until verified."
    ),
    "verifier": (
        "Workflow Phase — Verifier: For this stage, please independently check our implemented changes and look for any subtle bugs, edge cases, or regressions.\n"
        "Avoid source-modification blocks (`patch_file`/`write_file`) here; instead keep the crisp coding-agent style — 1 direct line explaining what you're checking followed by read/search or `run_command`/`run_python` ```tool_call JSON block(s) — wait for real output, and then report an honest pass/fail verdict citing only directly observed results."
    ),
}


def get_role_system_prompt(role: str, model_id: str = "gemini") -> str:
    """Return the stage prompt appropriate for the active provider (`claude` uses natural first-person framing)."""
    key = str(role or "coder").strip().lower()
    if "claude" in str(model_id or "").lower():
        base = CLAUDE_STAGE_DIRECTIVES.get(key, CLAUDE_STAGE_DIRECTIVES["coder"])
    else:
        base = ROLE_SYSTEM_PROMPTS.get(key, ROLE_SYSTEM_PROMPTS["coder"])
    if key == "verifier":
        base += (
            '\nEnd with a JSON block using this exact structure: '
            '{"verdict":"pass","checks":["check name: observed result"],"failures":[],"summary":"result"}. '
            'checks must contain non-empty strings, not objects; failures must contain objects with an issue string. '
            'Use verdict "fail" for unresolved failures. When screenshot review was requested, '
            'cite the observed pixels or the visual reviewer\'s findings. Report an unavailable review as incomplete.'
        )
    return base + (
        "\nFor substantial natural-language requests, infer the required steps from the user's goal, "
        "reason carefully about dependencies and edge cases, and complete your assigned stage "
        "with evidence. Preserve the requested output format and do not claim unverified results."
    )


def _attach_context_to_prompt(prompt: str, context_packet: str, max_chars: int = 3800) -> str:
    """Embed compact scoped ledger/decision/diff context in user prompt so persistent delta turns receive it."""
    pkt = str(context_packet or "").strip()
    base = str(prompt or "").rstrip()
    if not pkt:
        return base
    return f"{base}\n\n### Scoped Stage Context (Current Workspace & Ledger)\n{pkt[:max_chars]}"


def _run_role_agent(
    *,
    role: str,
    prompt: str,
    state: SessionState,
    client: WSAClient,
    context_packet: str = "",
    max_rounds: int = 16,
    relay_id_override: Optional[str] = None,
    session_id_override: Optional[str] = None,
    label: str = "",
    owned_files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Execute one scoped subagent call under strict tool allowlist and thread-mode continuity policy."""
    stage_label = label or role
    model_id = str(getattr(state, "model", None) or getattr(state, "selected_model", "gemini") or "gemini")
    sys_prompt = get_role_system_prompt(role, model_id)
    tools = get_role_tools(role)
    if session_id_override:
        eff_session = session_id_override
    elif hasattr(state, "resolve_stage_session_id"):
        eff_session = state.resolve_stage_session_id(role)
    else:
        eff_session = state.role_session_id(role)
    uses_main_thread = eff_session == getattr(state, "session_id", None)
    if uses_main_thread:
        chat_url = (
            getattr(state, "active_chat_url", "")
            or (getattr(state, "chat_urls", {}) or {}).get(model_id)
            or ""
        )
        if chat_url and not getattr(state, "active_chat_url", ""):
            state.active_chat_url = chat_url
    elif hasattr(state, "get_role_chat_url"):
        chat_url = state.get_role_chat_url(role, model=model_id)
    else:
        chat_url = ""
    stage_prompt = _attach_context_to_prompt(prompt, context_packet)
    # Inject ledger working-set digest so subagents have file-tracking context before first tool call
    _ledger_ref = getattr(state, "file_ledger", None)
    _ws_digest = _ledger_ref.render_working_set_digest(max_chars=1200) if isinstance(_ledger_ref, FileContextLedger) else ""
    extra_ctx = f"Working-set file map:\n{_ws_digest}" if _ws_digest else ""

    return execute_agent_loop(
        stage_prompt,
        state,
        client,
        role=role,
        tools_override=tools,
        system_prompt_override=sys_prompt,
        history_messages=[],
        persist_to_state=False,
        session_id_override=eff_session,
        chat_url_override=chat_url,
        relay_id_override=relay_id_override,
        max_rounds=max_rounds,
        strict_read_before_edit=(role == "coder"),
        enforce_verify_gate=(role == "coder"),
        quiet_ui=True,
        live_ui=True,
        stage_label=stage_label,
        flush_final_stream=True,
        allow_auto_decisions=False,
        extra_context_block=extra_ctx,
        event_callback=_make_role_event_cb(stage_label),
        allowed_write_paths=owned_files,
    )


# ── Public Orchestration Entry Points ───────────────────────────────────────

def _is_interrupted_res(res: Optional[Dict[str, Any]]) -> bool:
    return isinstance(res, dict) and str(res.get("aborted_reason") or "") == "interrupted"


def _interrupted_orchestration_result(
    stage: str,
    tasks: Optional[List[Dict[str, Any]]] = None,
    written_files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    msg = f"Interrupted by user during {stage} stage."
    return {
        "ok": False,
        "aborted_reason": "interrupted",
        "verdict": {
            "verdict": "fail",
            "checks": [],
            "failures": [{"file": stage, "issue": "Interrupted by user"}],
            "summary": msg,
        },
        "tasks": list(tasks or []),
        "written_files": list(written_files or []),
        "summary": msg,
    }


def _failed_orchestration_result(stage: str, reason: str) -> Dict[str, Any]:
    msg = f"{stage.capitalize()} stage failed: {reason}"
    R.print_error(msg)
    return {"ok": False, "aborted_reason": f"{stage}_failed", "tasks": [], "written_files": [],
            "verdict": {"verdict": "fail", "checks": [], "failures": [{"file": stage, "issue": reason}], "summary": msg},
            "summary": msg}


@serialized_turn
def run_plan_command(task_text: str, state: SessionState, client: WSAClient) -> List[Dict[str, Any]]:
    """Run Explorer -> Architect only to generate & save an editable execution DAG without mutating code."""
    clean_task = str(task_text or "").strip()
    if not clean_task:
        if state.plan_items:
            _ui_render_plan(state.plan_items)
        else:
            R.print_info("Usage: /plan <task description> (no saved plan yet).")
        return list(state.plan_items or [])

    ledger = getattr(state, "file_ledger", None)
    if not isinstance(ledger, FileContextLedger):
        ledger = FileContextLedger(state.project_root)
        state.file_ledger = ledger
    prev_ledger = set_active_file_ledger(ledger)

    try:
        _ui_stage_banner("explorer", "Scanning project structure, interfaces & existing tests…")
        exp_pkt = build_context_packet(
            project_root=state.project_root,
            project_type=getattr(state, "project_type", "unknown"),
            ledger=ledger,
            decisions=getattr(state, "decisions", None),
            extra_notes=f"Goal for planning: {clean_task}",
        )
        exp_res = _run_role_agent(
            role="explorer",
            prompt=(
                f"Perform read-only reconnaissance for planning this task:\n{clean_task}\n\n"
                "Identify relevant files, key symbol signatures (via `get_file_outline` / targeted `read_file`), "
                "and any test infrastructure or UI components."
            ),
            state=state,
            client=client,
            context_packet=exp_pkt,
            max_rounds=12,
        )
        if _is_interrupted_res(exp_res):
            R.print_warn("Planning interrupted during Explorer stage — cancelled remaining stages.")
            return list(state.plan_items or [])
        if not exp_res.get("ok", False):
            R.print_error("Planning stopped: Explorer did not complete.")
            return list(state.plan_items or [])

        _ui_stage_banner("architect", "Designing dependency-ordered execution plan…")
        arch_pkt = build_context_packet(
            project_root=state.project_root,
            project_type=getattr(state, "project_type", "unknown"),
            ledger=ledger,
            decisions=getattr(state, "decisions", None),
            extra_notes=f"Explorer Reconnaissance Findings:\n{(exp_res.get('final_text') or '')[:2200]}",
        )
        arch_res = _run_role_agent(
            role="architect",
            prompt=(
                f"Design a precise dependency-ordered task plan for:\n{clean_task}\n\n"
                "Return ONLY the task DAG JSON array in ```json ... ``` plus brief concise notes."
            ),
            state=state,
            client=client,
            context_packet=arch_pkt,
            max_rounds=8,
        )
        if _is_interrupted_res(arch_res):
            R.print_warn("Planning interrupted during Architect stage — plan discarded.")
            return list(state.plan_items or [])
        if not arch_res.get("ok", False):
            R.print_error("Planning stopped: Architect did not complete.")
            return list(state.plan_items or [])

        plan_error = _architect_plan_error(arch_res.get("final_text") or "")
        if plan_error:
            fallback_tasks = _parse_architect_plan(arch_res.get("final_text") or "", clean_task)
            if fallback_tasks and any(t.get("owned_files") for t in fallback_tasks):
                R.print_warn(f"Architect plan had minor schema warning ({plan_error}); auto-healed {len(fallback_tasks)} tasks.")
                tasks = fallback_tasks
            else:
                R.print_error(f"Planning stopped: {plan_error}")
                return list(state.plan_items or [])
        else:
            tasks = _parse_architect_plan(arch_res.get("final_text") or "", clean_task)
        state.plan_items = tasks
        if hasattr(state, "auto_checkpoint"):
            state.auto_checkpoint()
        _ui_render_plan(tasks)
        return tasks
    finally:
        set_active_file_ledger(prev_ledger)


@serialized_turn
def run_verify_command(scope_text: str, state: SessionState, client: WSAClient) -> Dict[str, Any]:
    """Run independent `verifier` subagent on modified files or explicit scope without mutating code."""
    ledger = getattr(state, "file_ledger", None)
    if not isinstance(ledger, FileContextLedger):
        ledger = FileContextLedger(state.project_root)
        state.file_ledger = ledger
    prev_ledger = set_active_file_ledger(ledger)

    try:
        targets = sorted(state.written_files)
        scope_note = scope_text.strip() if scope_text and scope_text.strip() else (
            ", ".join(targets[:10]) if targets else "entire active repository"
        )
        _ui_stage_banner("verifier", f"Running independent QA verification on: {scope_note}")
        ver_pkt = build_context_packet(
            project_root=state.project_root,
            project_type=getattr(state, "project_type", "unknown"),
            ledger=ledger,
            decisions=getattr(state, "decisions", None),
            plan_items=getattr(state, "plan_items", None),
            written_files=targets,
            verified_files=sorted(state.verified_files),
            extra_notes=f"Verification scope requested: {scope_note}",
        )
        ver_res = _run_role_agent(
            role="verifier",
            prompt=(
                f"Independently verify: {scope_note}.\n"
                "Run pytest / syntax checks (`py_compile`, `node --check`), targeted runtime checks, "
                "and inspect modified files. End with your strict JSON pass/fail verdict block."
            ),
            state=state,
            client=client,
            context_packet=ver_pkt,
            max_rounds=12,
        )
        verdict = _parse_verifier_verdict(ver_res.get("final_text") or "", ver_res)
        if verdict["verdict"] == "pass":
            ledger.mark_verified(targets)
            state.verified_files.update(targets)
            R.print_success(f"Verifier PASSED: {verdict.get('summary') or 'All checks clean.'}")
        else:
            R.print_error(f"Verifier FAILED: {verdict.get('summary') or 'See failure report below.'}")
            for fail in verdict.get("failures") or []:
                if isinstance(fail, dict):
                    R.print_warn(f"  - {fail.get('file', '?')}: {fail.get('issue', '')}")
        if hasattr(state, "auto_checkpoint"):
            state.auto_checkpoint()
        return verdict
    finally:
        set_active_file_ledger(prev_ledger)


def spawn_scoped_subagent(
    *,
    role: str,
    task: str,
    owned_files: Optional[List[str]] = None,
    acceptance: str = "",
) -> str:
    """Runtime executor invoked by `spawn_subagent` tool inside an active session turn."""
    clean_role = str(role or "explorer").strip().lower()
    if clean_role not in ROLE_ALLOWED_TOOLS:
        return f"[Error] Invalid subagent role '{role}'. Choose one of: explorer, architect, coder, verifier."
    if _get_subagent_depth() >= 1:
        return "[Scope Error] Nested subagent spawning beyond depth 1 is disabled to prevent runaway delegation."

    state, client = _ACTIVE_DELEGATION_CTX.get()
    if state is None or client is None:
        return "[Error] No active parent session/client bound for spawn_subagent."

    ledger = getattr(state, "file_ledger", None)
    if not isinstance(ledger, FileContextLedger):
        ledger = FileContextLedger(state.project_root)
        state.file_ledger = ledger

    owned = [_normalize_rel_path(p) for p in (owned_files or []) if str(p or "").strip()]
    if clean_role == "coder" and not owned:
        return "[Scope Error] Coder subagents require explicit owned_files; inspect and assign the target files first."
    pkt = build_context_packet(
        project_root=state.project_root,
        project_type=getattr(state, "project_type", "unknown"),
        ledger=ledger,
        decisions=getattr(state, "decisions", None),
        plan_items=getattr(state, "plan_items", None),
        written_files=sorted(state.written_files),
        verified_files=sorted(state.verified_files),
        focus_files=owned,
        extra_notes=(
            (f"Assigned owned_files: {', '.join(owned)}\n" if owned else "")
            + (f"Acceptance criteria: {acceptance}\n" if acceptance else "")
        ),
    )

    depth_token = _DELEGATION_DEPTH.set(_get_subagent_depth() + 1)
    try:
        _task_lines = str(task or "").strip().splitlines()
        _ui_stage_banner(clean_role, _task_lines[0][:80] if _task_lines else "(no task description)")
        res = _run_role_agent(
            role=clean_role,
            prompt=task,
            state=state,
            client=client,
            context_packet=pkt,
            max_rounds=16,
            label=clean_role,
            owned_files=owned if clean_role == "coder" and owned_files is not None else None,
        )
        if res.get("written_files"):
            state.written_files.update(res["written_files"])
            for wf in res["written_files"]:
                state.verified_files.discard(wf)
        return json.dumps(
            {
                "ok": res.get("ok") is True,
                "role": clean_role,
                "written_files": res.get("written_files", []),
                "verified_after_last_edit": res.get("verified_after_last_edit", False),
                "aborted_reason": str(res.get("aborted_reason") or ""),
                "summary": str(res.get("final_text") or "")[:3000],
            },
            ensure_ascii=False,
        )
    finally:
        _DELEGATION_DEPTH.reset(depth_token)


def _recover_orchestrated_stage_limit(
    state: SessionState,
    client: WSAClient,
    failed_res: Dict[str, Any],
    *,
    pending_goal: str = "",
    stage_label: str = "stage",
) -> bool:
    """Handle interactive account-switch recovery when an orchestrated role stage hits limit."""
    if not isinstance(failed_res, dict) or not failed_res.get("limit_exhausted"):
        return False
    q_list = list(getattr(state, "multiagent_queue", []) or [])
    is_multi = getattr(state, "agent_mode", "singleagent") == "multiagent" and bool(q_list)
    reason = str(failed_res.get("aborted_reason") or "usage limit reached")
    saved_ckpt = _preserve_workflow_state_and_context(
        state,
        persist_to_state=True,
        pending_goal=pending_goal,
        record_pending_goal=False,
    )
    if is_multi:
        R.print_warn(f"All queued agents reached their account limits during {stage_label} ({reason[:120]}).")
    else:
        R.print_warn(
            f"{R.friendly_model_name(getattr(state, 'model', 'gemini'))} account limit reached during {stage_label} ({reason[:120]})."
        )
    R.print_info(
        "Current workflow state & context preserved automatically"
        + (f" ({saved_ckpt})." if saved_ckpt else ".")
    )
    choice = "single" if is_multi else _offer_limit_multiagent(state, client, getattr(state, "model", ""))
    if choice == "switch":
        _reset_provider_chats_for_account_change(
            state, client, getattr(state, "session_id", None),
            [state.model], clear_roles=True, refresh_health=True,
        )
        state._force_new_chat_next = True
        return True
    wants_continue = choice != "stop" and _prompt_all_agents_exhausted(
        state, model_name=getattr(state, "model", ""), is_multiagent=is_multi,
    )
    target_provs = list(q_list) if (is_multi and q_list) else [getattr(state, "model", "gemini")]
    if wants_continue and _wait_for_account_switch_countdown(ACCOUNT_SWITCH_WAIT_SECS):
        if is_multi and q_list:
            state.multiagent_index = 0
            if hasattr(state, "set_model"):
                state.set_model(q_list[0])
        _reset_provider_chats_for_account_change(
            state,
            client,
            getattr(state, "session_id", None),
            target_provs,
            clear_roles=True,
            refresh_health=True,
        )
        return True
    _reset_provider_chats_for_account_change(
        state,
        client,
        getattr(state, "session_id", None),
        target_provs,
        clear_roles=True,
        refresh_health=False,
    )
    state._force_new_chat_next = True
    saved_stop = _preserve_workflow_state_and_context(
        state,
        persist_to_state=True,
        pending_goal=pending_goal,
        record_pending_goal=True,
    )
    R.print_info(
        "Context preserved, stopping workflow..."
        + (f" (saved: {saved_stop})" if saved_stop else "")
    )
    return False


@serialized_turn
def run_orchestrated_turn(
    user_input: str,
    state: SessionState,
    client: WSAClient,
    *,
    reasons: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Execute a full adaptive multi-role pipeline:
      1) Explorer recon (read-only)
      2) Architect task DAG (`state.plan_items`)
      3) Scoped Coder slice execution (sequential active relay by default, or optional dual-lane)
      4) Independent Verifier pass + bounded targeted repair loop (up to 2 fix cycles)
      5) Clean completion summary saved to main `state.messages`.
    """
    reason_str = f" ({', '.join(reasons)})" if reasons else ""
    _ui_stage_banner("orchestrator", f"Engaging scoped 4-role build pipeline{reason_str}")

    names = select_task_skills(user_input)
    if names:
        R.print_info("Skills: " + ", ".join(names))

    def _run_stage(**kwargs):
        stage_skills = names
        if str(kwargs.get("label", "")).startswith("repair#") and "systematic-debugging" not in names:
            stage_skills = (*names, "systematic-debugging")
        if stage_skills:
            kwargs["prompt"] += "\n\n" + skill_guidance(stage_skills)
        return _run_role_agent(**kwargs)

    ledger = getattr(state, "file_ledger", None)
    if not isinstance(ledger, FileContextLedger):
        ledger = FileContextLedger(state.project_root)
        state.file_ledger = ledger

    prev_ledger = set_active_file_ledger(ledger)
    state_lock = threading.Lock()
    relay_seq_lock = threading.Lock()
    all_written: Set[str] = set()

    try:
        # ── Stage 1: Explorer ───────────────────────────────────────────────
        _ui_stage_banner("explorer", "Mapping workspace structure, outlines & existing checks…")
        exp_pkt = build_context_packet(
            project_root=state.project_root,
            project_type=getattr(state, "project_type", "unknown"),
            ledger=ledger,
            decisions=getattr(state, "decisions", None),
            written_files=sorted(state.written_files),
            verified_files=sorted(state.verified_files),
            turn_digest=getattr(state, "turn_digest", None),
            extra_notes=f"Primary user objective:\n{user_input[:1200]}",
        )
        while True:
            exp_res = _run_stage(
                role="explorer",
                prompt=(
                    f"Perform fast targeted read-only reconnaissance for this build/engineering task:\n{user_input}\n\n"
                    "1. Inspect directory tree (`list_dir`/`glob_files`), relevant entrypoints (`get_file_outline`), "
                    "and test setup.\n"
                    "2. Read only critical sections needed to map interfaces cleanly.\n"
                    "3. Return exact file paths, symbol signatures, existing conventions, and risks."
                ),
                state=state,
                client=client,
                context_packet=exp_pkt,
                max_rounds=12,
            )
            if _recover_orchestrated_stage_limit(state, client, exp_res, pending_goal=user_input, stage_label="explorer"):
                continue
            break
        if _is_interrupted_res(exp_res):
            R.print_warn("Orchestration interrupted during Explorer stage — halting remaining stages.")
            return _interrupted_orchestration_result("explorer")
        if not exp_res.get("ok", False):
            return _failed_orchestration_result("explorer", str(exp_res.get("aborted_reason") or "incomplete"))
        explorer_summary = str(exp_res.get("final_text") or "").strip()

        # ── Stage 2: Architect ──────────────────────────────────────────────
        _ui_stage_banner("architect", "Constructing dependency-ordered task plan…")
        arch_pkt = build_context_packet(
            project_root=state.project_root,
            project_type=getattr(state, "project_type", "unknown"),
            ledger=ledger,
            decisions=getattr(state, "decisions", None),
            written_files=sorted(state.written_files),
            turn_digest=getattr(state, "turn_digest", None),
            extra_notes=f"Explorer Reconnaissance Findings:\n{explorer_summary[:4000]}",
        )
        while True:
            arch_res = _run_stage(
                role="architect",
                prompt=(
                    f"Create a minimal dependency-ordered task DAG to complete:\n{user_input}\n\n"
                    "Ensure every task lists explicit `owned_files` and `acceptance`, and output the JSON task array inside ```json ... ```."
                ),
                state=state,
                client=client,
                context_packet=arch_pkt,
                max_rounds=8,
            )
            if _recover_orchestrated_stage_limit(state, client, arch_res, pending_goal=user_input, stage_label="architect"):
                continue
            break
        if _is_interrupted_res(arch_res):
            R.print_warn("Orchestration interrupted during Architect stage — halting before execution.")
            return _interrupted_orchestration_result("architect")
        if not arch_res.get("ok", False):
            return _failed_orchestration_result("architect", str(arch_res.get("aborted_reason") or "incomplete"))
        plan_error = _architect_plan_error(arch_res.get("final_text") or "")
        if plan_error:
            fallback_tasks = _parse_architect_plan(arch_res.get("final_text") or "", user_input)
            if fallback_tasks and any(t.get("owned_files") for t in fallback_tasks):
                R.print_warn(f"Architect plan had minor schema warning ({plan_error}); auto-healed {len(fallback_tasks)} tasks.")
                tasks = fallback_tasks
            else:
                return _failed_orchestration_result("architect", plan_error)
        else:
            tasks = _parse_architect_plan(arch_res.get("final_text") or "", user_input)
        state.plan_items = tasks
        if hasattr(state, "auto_checkpoint"):
            state.auto_checkpoint()
        _ui_render_plan(tasks)

        # ── Stage 3: Scoped Coder Execution (Sequential; parallel fan-out disabled) ──
        live_relays = _discover_live_relays(client)
        # Threading-based dual-lane fan-out is disabled by design. Two blockers:
        #   1. Approved shell commands mutate shared process state (background
        #      process registry, ports, Rich console), so lanes cannot be isolated.
        #   2. execute_agent_loop is @serialized_turn; pool threads would block on
        #      _turn_lock while the orchestrator blocks on their futures — deadlock.
        # Parallelism here requires process-level isolation (separate roots), not
        # threads sharing a process-global project root.
        if getattr(state, "dual_lane", False) and len(live_relays) >= 2:
            R.print_warn("Dual-lane execution is serialized: shell tools require isolated workspaces before safe parallel execution.")

        completed_ids: Set[str] = set()
        worker_failures: List[Dict[str, str]] = []
        task_summaries: List[str] = []

        def _execute_task_slice(
            task_item: Dict[str, Any],
            *,
            relay_id: Optional[str] = None,
        ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
            tid = task_item["id"]
            owned = list(task_item.get("owned_files") or [])
            with state_lock:
                task_item["status"] = "in_progress"
                prior_notes = "\n".join(task_summaries[-4:])
            pkt = build_context_packet(
                project_root=state.project_root,
                project_type=getattr(state, "project_type", "unknown"),
                ledger=ledger,
                decisions=getattr(state, "decisions", None),
                plan_items=tasks,
                written_files=sorted(state.written_files | all_written),
                verified_files=sorted(state.verified_files),
                focus_files=owned,
                turn_digest=getattr(state, "turn_digest", None),
                extra_notes=(
                    f"Explorer Findings:\n{explorer_summary[:2500]}\n\n"
                    + (f"Completed Prior Task Results:\n{prior_notes}\n\n" if prior_notes else "")
                    + f"Assigned Task [{tid}]: {task_item['title']}\n"
                    + (f"Description: {task_item['description']}\n" if task_item.get("description") else "")
                    + f"Owned files scope: {', '.join(owned) if owned else '(minimal files required)'}\n"
                    + f"Acceptance criteria: {task_item.get('acceptance', '')}"
                ),
            )
            desc_text = f"Task Description:\n{task_item['description']}\n\n" if task_item.get("description") else ""
            task_guidance = (
                "Implement any requested deliverables within the assigned scope. Read existing targets before editing "
                "and use `write_file` for new modules. For verification-only tasks, run the requested checks. "
                "Verify the acceptance criteria and summarize the observed results."
            )
            worker_prompt = (
                f"Execute task `{tid}`: {task_item['title']}\n"
                f"{desc_text}"
                f"Overall User Request:\n{user_input[:1200]}\n\n"
                f"Strict Scope:\n- Owned files: {', '.join(owned) if owned else 'only files required for this task'}\n"
                f"- Acceptance: {task_item.get('acceptance', 'Passes verification checks cleanly')}\n\n"
                f"{task_guidance}"
            )
            if hasattr(state, "resolve_stage_session_id"):
                session_override = state.resolve_stage_session_id("coder")
            else:
                session_override = state.role_session_id("coder")
            worker_args = dict(
                role="coder",
                prompt=worker_prompt,
                state=state,
                client=client,
                context_packet=pkt,
                max_rounds=24,
                relay_id_override=relay_id,
                session_id_override=session_override,
                label=f"coder:{tid}",
                owned_files=owned or None,
            )
            res = _run_stage(**worker_args)
            reason = str(res.get("aborted_reason") or "")
            if (
                not res.get("ok") and not res.get("limit_exhausted")
                and res.get("tool_calls_count") == 0 and not res.get("written_files")
                and re.search(r"DOM relay timeout \(\d+s\) waiting for the .+? tab to finish answering", reason)
            ):
                R.print_warn(f"{tid}: browser response timed out before any tool calls. Retrying this task once.")
                res = _run_stage(**worker_args)
            with state_lock:
                wf = list(res.get("written_files") or [])
                all_written.update(wf)
                state.written_files.update(wf)
                for p in wf:
                    state.verified_files.discard(p)
                if not res.get("limit_exhausted"):
                    task_item["status"] = "done" if res.get("ok") else "failed"
                    task_summaries.append(f"- [{tid}] {task_item['title']} -> wrote {wf or ['(none)']}; ok={res.get('ok')}")
            return task_item, res

        pending = list(tasks)
        interrupted = False
        aborted_reason = ""
        while pending:
            ready = [
                t for t in pending
                if set(t.get("depends_on") or []).issubset(completed_ids)
            ]
            if not ready:
                aborted_reason = "dependency_blocked"
                for blocked in pending:
                    blocked["status"] = "blocked"
                    missing = sorted(set(blocked.get("depends_on") or []) - completed_ids)
                    task_summaries.append(f"- [{blocked['id']}] blocked by unsatisfied dependencies: {', '.join(missing)}")
                break

            nxt = ready[0]
            _ui_stage_banner("coder", f"Executing task {nxt['id']}: {nxt['title']}")
            while True:
                with relay_seq_lock:
                    done_item, res = _execute_task_slice(nxt)
                if _recover_orchestrated_stage_limit(state, client, res, pending_goal=user_input, stage_label=f"coder:{nxt['id']}"):
                    live_relays = _discover_live_relays(client)
                    continue
                break
            if res.get("limit_exhausted"):
                aborted_reason = str(res.get("aborted_reason") or "limit_reached")
                for remaining in pending:
                    remaining["status"] = "blocked"
                break
            if res.get("ok"):
                completed_ids.add(done_item["id"])
            else:
                worker_failures.append({
                    "file": done_item["id"],
                    "issue": str(res.get("aborted_reason") or "Worker returned failure without a reason"),
                })
            pending.remove(done_item)
            if res.get("aborted_reason") == "permission_denied":
                aborted_reason = "permission_denied"
                for remaining in pending:
                    remaining["status"] = "blocked"
                break
            if _is_interrupted_res(res):
                interrupted = True
                aborted_reason = "interrupted"
                if hasattr(state, "auto_checkpoint"):
                    state.auto_checkpoint()
                break

            if hasattr(state, "auto_checkpoint"):
                state.auto_checkpoint()

        # ── Stage 4: Independent Verifier + Targeted Repair Loop (max 2) ────
        if aborted_reason == "dependency_blocked":
            verdict = {
                "verdict": "fail", "checks": [],
                "failures": [
                    {"file": t["id"], "issue": "Blocked by " + ", ".join(
                        dep for dep in t.get("depends_on", []) if dep not in completed_ids
                    )}
                    for t in pending
                ],
                "summary": "Prerequisites did not complete. Final verification was skipped.",
            }
        elif aborted_reason == "permission_denied":
            verdict = {"verdict": "fail", "checks": [],
                       "failures": [{"file": "coder", "issue": "Action approval was not granted"}],
                       "summary": "Stopped pending user approval; no further workers were dispatched."}
        elif interrupted:
            R.print_warn("Orchestration interrupted during execution — skipping verification stage.")
            verdict: Dict[str, Any] = {
                "verdict": "fail",
                "checks": [],
                "failures": [{"file": "coder", "issue": "Interrupted by user"}],
                "summary": "Interrupted by user during Coder execution stage.",
            }
        elif aborted_reason:
            verdict = {
                "verdict": "fail", "checks": [],
                "failures": [{"file": "coder", "issue": aborted_reason}],
                "summary": f"Execution stopped cleanly ({aborted_reason}).",
            }
        else:
            verdict = {"verdict": "pass", "checks": [], "failures": [], "summary": ""}
        format_feedback = ""
        for attempt in ([] if aborted_reason or interrupted else range(3)):
            _ui_stage_banner(
                "verifier",
                "Running final verification pass..." if attempt == 0 else f"Re-verifying after repair #{attempt}...",
            )
            written_list = sorted(all_written or state.written_files)
            ver_pkt = build_context_packet(
                project_root=state.project_root,
                project_type=getattr(state, "project_type", "unknown"),
                ledger=ledger,
                decisions=getattr(state, "decisions", None),
                plan_items=tasks,
                written_files=written_list,
                verified_files=sorted(state.verified_files),
                turn_digest=getattr(state, "turn_digest", None),
                extra_notes=f"Task summaries:\n" + "\n".join(task_summaries),
            )
            while True:
                ver_res = _run_stage(
                    role="verifier",
                    prompt=(
                        f"Verify all work completed for the user request:\n{user_input[:1200]}\n\n"
                        f"Modified files to verify: {', '.join(written_list) if written_list else '(check workspace)'}\n"
                        "Run tests (`pytest`), syntax/compile checks, runtime checks, and inspect UI if applicable. "
                        'Conclude with a JSON block: {"verdict":"pass","checks":["test name: result"],"failures":[],"summary":"result"}. '
                        'checks must be an array of non-empty strings; failures must be an array of objects with an issue string. '
                        'Use verdict "fail" and describe any failures honestly. ' + format_feedback
                    ),
                    state=state,
                    client=client,
                    context_packet=ver_pkt,
                    max_rounds=12,
                    label=f"verifier#{attempt + 1}",
                )
                if _recover_orchestrated_stage_limit(state, client, ver_res, pending_goal=user_input, stage_label=f"verifier#{attempt + 1}"):
                    continue
                break
            if ver_res.get("limit_exhausted"):
                aborted_reason = str(ver_res.get("aborted_reason") or "limit_reached")
                verdict = {
                    "verdict": "fail",
                    "checks": [],
                    "failures": [{"file": "verifier", "issue": aborted_reason}],
                    "summary": f"Stopped cleanly due to provider limit ({aborted_reason}).",
                }
                break
            if _is_interrupted_res(ver_res):
                interrupted = True
                aborted_reason = "interrupted"
                R.print_warn("Orchestration interrupted during Verifier stage — halting remaining stages.")
                verdict = {
                    "verdict": "fail",
                    "checks": [],
                    "failures": [{"file": "verifier", "issue": "Interrupted by user"}],
                    "summary": "Interrupted by user during Verifier stage.",
                }
                break
            verdict = _parse_verifier_verdict(ver_res.get("final_text") or "", ver_res)
            if ver_res.get("aborted_reason") == "permission_denied":
                aborted_reason = "permission_denied"
                break
            if verdict["verdict"] == "pass" or attempt >= 2:
                break

            if verdict.get("format_error"):
                format_feedback = "Your previous verdict had an invalid format: " + verdict["summary"] + ". Return the specified JSON and run verification again."
                R.print_warn("Verifier response format was invalid; requesting a corrected verification.")
                continue

            # Targeted Coder repair pass
            fail_lines = []
            for fail in verdict.get("failures") or []:
                if isinstance(fail, dict):
                    fail_lines.append(f"- {fail.get('file', '?')}: {fail.get('issue', '')}")
                else:
                    fail_lines.append(f"- {fail}")
            fail_report = "\n".join(fail_lines) or verdict.get("summary") or (ver_res.get("final_text") or "")[:1000]
            _ui_stage_banner("coder", f"Targeted repair pass #{attempt + 1} based on verifier diagnostics…")
            rep_pkt = build_context_packet(
                project_root=state.project_root,
                project_type=getattr(state, "project_type", "unknown"),
                ledger=ledger,
                decisions=getattr(state, "decisions", None),
                plan_items=tasks,
                written_files=written_list,
                focus_files=written_list,
                turn_digest=getattr(state, "turn_digest", None),
                extra_notes=f"Verifier Failure Diagnostics:\n{fail_report}",
            )
            while True:
                with relay_seq_lock:
                    rep_res = _run_stage(
                        role="coder",
                        prompt=(
                            "Fix the exact defects reported by the independent Verifier below.\n"
                            "Use `get_file_outline` / `read_file`, apply surgical `patch_file` fixes, and re-run the failing check.\n\n"
                            f"Failure diagnostics:\n{fail_report}"
                        ),
                        state=state,
                        client=client,
                        context_packet=rep_pkt,
                        max_rounds=16,
                        label=f"repair#{attempt + 1}",
                        owned_files=sorted({p for task in tasks for p in task.get("owned_files", [])}) or None,
                    )
                if _recover_orchestrated_stage_limit(state, client, rep_res, pending_goal=user_input, stage_label=f"repair#{attempt + 1}"):
                    continue
                break
            if rep_res.get("limit_exhausted"):
                aborted_reason = str(rep_res.get("aborted_reason") or "limit_reached")
                break
            if rep_res.get("written_files"):
                all_written.update(rep_res["written_files"])
                state.written_files.update(rep_res["written_files"])
            if rep_res.get("aborted_reason") == "permission_denied":
                aborted_reason = "permission_denied"
                break
            if _is_interrupted_res(rep_res):
                interrupted = True
                aborted_reason = "interrupted"
                R.print_warn("Orchestration interrupted during repair pass — halting remaining stages.")
                verdict = {
                    "verdict": "fail",
                    "checks": verdict.get("checks") or [],
                    "failures": verdict.get("failures") or [{"file": "repair", "issue": "Interrupted by user"}],
                    "summary": "Interrupted by user during repair pass.",
                }
                break

        if worker_failures:
            if verdict["verdict"] == "pass":
                verdict["verdict"] = "fail"
                verdict["summary"] = "Some tasks did not complete successfully."
            verdict["failures"] = worker_failures + verdict["failures"]
        if verdict["verdict"] == "pass" and all_written:
            ledger.mark_verified(all_written)
            state.verified_files.update(all_written)

        # ── Stage 5: Commit Concise Result to Main Session History ───────────
        written_sorted = sorted(all_written)
        checks_lines = "\n".join(f"- {c}" for c in (verdict.get("checks") or []))
        fail_lines = "\n".join(
            f"- `{f.get('file', '?')}`: {f.get('issue', '')}" if isinstance(f, dict) else f"- {f}"
            for f in (verdict.get("failures") or [])
        )
        final_lines = [
            f"### Scoped Team Execution Summary ({verdict['verdict'].upper()})",
            "",
            f"**Tasks completed:** {len(completed_ids)}/{len(tasks)}",
            "**Files modified:** " + (", ".join(f"`{p}`" for p in written_sorted) if written_sorted else "_none_"),
        ]
        if task_summaries:
            final_lines.extend(["", "**Task Breakdown:**", *task_summaries])
        if checks_lines:
            final_lines.extend(["", "**Checks Run:**", checks_lines])
        if fail_lines:
            final_lines.extend(["", "**Remaining Failures:**", fail_lines])
        if verdict.get("summary"):
            label = "Execution status" if aborted_reason == "dependency_blocked" else "Verifier Verdict"
            final_lines.extend(["", f"**{label}:** {verdict['summary']}"])
        final_report = "\n".join(final_lines)

        state.messages.append({"role": "user", "content": user_input})
        state.messages.append({"role": "assistant", "content": final_report})
        state.turn_count += 1
        if len(state.messages) > 60:
            state.compact_history(keep=40)
        if hasattr(state, "auto_checkpoint"):
            state.auto_checkpoint()

        R.begin_agent_response()
        if hasattr(R, "print_markdown"):
            R.print_markdown(final_report)
        else:
            R.console.print(final_report)

        return {
            "ok": verdict["verdict"] == "pass" and not aborted_reason,
            "aborted_reason": aborted_reason,
            "verdict": verdict,
            "tasks": tasks,
            "written_files": written_sorted,
            "summary": final_report,
        }
    finally:
        set_active_file_ledger(prev_ledger)
