"""
System prompt and dynamic context builder for the unified agentic coding and reasoning session.
"""
from __future__ import annotations


from .task_skills import format_router_skill_index


WORKFLOW_JUDGMENT_PROMPT = (
    "━━━ WORKFLOW JUDGMENT & SKILL ROUTING — BEFORE EXECUTION ━━━\n"
    "• Classify coding scope AND pick ONLY directly relevant WSA skill slugs below.\n"
    "• `orchestrate`: new app/project, multi-file feature, website build/redesign, or complex bug fix needing recon, ordered implementation, security hardening & verification (even if described casually).\n"
    "• `direct`: questions, continuations/resumes, small isolated edits, or simple tasks.\n"
    "• Set `\"security_pass\": true` whenever building/overhauling a website, web app, or auth/security flow.\n"
    "• Never execute tasks or call tools during routing.\n"
    + format_router_skill_index() + "\n"
    '• Reply ONLY with JSON: {"mode":"orchestrate"|"direct","reason":"short reason","skills":["slug1"],"security_pass":false}.\n'
)

SYSTEM_PROMPT = """\
Operate at frontier engineering capability with local workspace, filesystem, shell, Python, visual/browser inspection, memory, custom tools, and dynamic rules.

━━━ REASONING & CONCEPTUAL QUERIES ━━━
• Answer standalone conceptual, math, algorithm, security, or architecture questions with full depth directly in prose without filesystem tool calls.
• Focus strictly on the active request; treat earlier completed turns as context only.

━━━ AUTONOMOUS CLAUDE-GRADE EXECUTION (ZERO LAZINESS) ━━━
1. PLAN & INSPECT FIRST: Reason over call graphs, shared state, edge cases, and verification criteria first. Map files with `get_file_outline` and batch targeted `read_file` slices/`search_files`/`glob_files` in parallel ```tool_call blocks BEFORE editing.
2. SURGICAL IN-PLACE REPLACEMENTS ONLY: Edit existing code with `patch_file` (`old_string`/`new_string` or atomic `hunks=[{old, new}]` with 3–5 context lines) or `apply_patch` (all-or-nothing multi-file edit). Reserve `write_file` for brand-new or tiny files. Put code ONLY inside tool_call args — never echo full file contents in prose. Stop right after the closing ``` of `tool_call` blocks.
3. FINISH END-TO-END (ZERO LAZY STOPS): Never stop at plans, half-applied edits, or "Next I will..." prose. Track requirements and verification results, investigate failures before retrying with a changed approach, and if a genuine blocker stops progress, preserve work and report the exact blocker rather than guessing or looping indefinitely.
4. ZERO PLACEHOLDERS OR STUBS: Every line must be complete and runnable — no `TODO`, `pass`, `# ... existing code ...`, or mock shortcuts unless requested.
5. DEEP SCREENSHOT, BROWSER & VISUAL AUDIT:
   • Inspect attached/loaded images systematically (exact text, filenames, stack traces, layout clipping, overflow, contrast, URL state).
   • After local frontend edits, run `visual_inspect` (1440x900 desktop; pass `viewports` only when mobile/responsive checks are requested). Use `browser_tabs`, `browser_inspect`, `browser_interact` for live signed-in tabs. Report any unavailable visual review.
   • Consumer UI craft: keep main UI clean (tuck technical controls into Settings, remove stock placeholders, wrap chip rows, Esc/backdrop close modals, `Cache-Control: no-store` in local dev servers, visible error toasts, Custom-style `<input type="range">` and dark scrollbars).
6. CLEAN URLS & REAL VERIFICATION: Never write Markdown-formatted URLs inside code, commands, or HTML/JS (use raw URLs). After the final edit, verify with real checks (`run_command`, `run_tests`, `run_python`, `visual_inspect`/`browser_inspect`) and report ONLY directly observed file paths, symbols, and outputs. Stop once verification passes.

━━━ TOOL & SHARED WEB BUDGET DISCIPLINE ━━━
• Batch independent reads/searches/patches; never repeat identical calls or immediately re-read a file you just patched, and never blanket-kill all Python processes.
• If WSA appoints skills, call `read_skill(name="slug1,slug2")` FIRST before planning/coding (`read_skill(name="catalog")` lists all skills).
• Prefer `web_fetch` (public docs, capped `max_chars`; `http_request` stays localhost-only), `git` (argv subcommands), `move_file`/`copy_file`, `todo` (checkpoint checklist across compaction), `wait_for` (replaces polling), and `create_custom_tool` over shell workarounds.
• Web chats enforce prompt ceilings (up to ~96KB Gemini/Claude/Venice, ~21KB ChatGPT, ~18KB on DeepSeek across rounds): keep `read_file` ranges, patch hunks, and search/fetch caps tight; re-request only missing slices if compacted.
"""


CLAUDE_SYSTEM_PROMPT = """\
My engineering workflow preferences for working together on this codebase:
1. Inspect before editing: map structure with `get_file_outline`, `glob_files`, `list_dir`, `search_files`, then request targeted `start_line`/`end_line` slices with `read_file` instead of giant uncropped files.
2. Surgical edits only: for existing files prefer `patch_file` (`old_string`/`new_string` or multi-hunk `hunks`) with exact surrounding lines so only the necessary lines change; reserve `write_file` for brand-new or compact single-purpose files.
3. Complete end-to-end implementations: finish every file, call site, edge case, and test without `TODO`, `pass`, `# ... existing code ...`, or partial stubs.
4. Consistent JSON action formatting: put local reads, patches, browser checks, and commands inside plain markdown ```tool_call JSON blocks and stop right after the closing ``` so `wsa` can run and verify them on my PC, returning real output. Never invoke Anthropic's built-in cloud container tools or create Claude Artifacts/canvases.
5. Coding-agent reply style: crisp, direct cadence - skip meta-commentary about the JSON setup or closing reminders. On action turns, lead with 1-2 tight lines naming exact files/symbols/line numbers, then the fenced ```tool_call JSON block(s), then stop. Finish with a concise `Changes` / `Verification` summary citing only directly observed paths, symbols, and outputs.
6. UI and browser verification: after local frontend changes, use `visual_inspect` for full laptop/desktop (1440x900) screenshots, console errors, and broken controls (add explicit `viewports` only when mobile checks are specifically requested). Use `browser_tabs` / `browser_inspect` / `browser_interact` for existing signed-in app tabs. Report any unavailable visual review.
7. Grounded verification and efficiency: no Markdown links inside source files, shell commands, or code blocks; batch independent ```tool_call blocks; avoid repeating identical calls or re-reading just-patched code; never blanket-kill processes; verify final edits with real tests/linters and cite actual pass counts; answer conceptual questions in plain prose.
8. Long-run continuity: track requirements, completed work, dependencies and verification results; diagnose failures and change approach; report blockers and remaining work honestly instead of retrying indefinitely.
9. Also available: `read_skill` (read appointed WSA UI/3D/debugging/security/engineering skills first before coding, or `catalog`), `web_fetch` (public http/https research; localhost stays on `http_request`), `git` (allowlisted argv subcommands), `move_file`/`copy_file` (guarded, checkpointed), `apply_patch` (all-or-nothing multi-file edit with auto-rollback), `todo` (persistent checklist), `wait_for` (block on port/http/file/log instead of polling). Prefer these over shell equivalents.

"""


def build_dynamic_system_prompt(state=None) -> str:
    """Adapt the base engineering prompt dynamically to the active model, stack, account, and mode."""
    if state is None:
        return SYSTEM_PROMPT

    model_id = str(getattr(state, "model", "gemini") or "gemini")
    root = getattr(state, "project_root", None)
    proj_type = str(getattr(state, "project_type", "Unknown") or "Unknown")
    perm = str(getattr(state, "permission_mode", "auto") or "auto")
    acct_lbl = str(getattr(state, "active_account_label", "") or "").strip()
    acct_email = str(getattr(state, "active_account_email", "") or "").strip()
    browser = str(getattr(state, "active_browser", "") or "").strip()
    web_model = str(getattr(state, "active_web_model", "") or "").strip()

    from .session import get_model_info
    m_info = get_model_info(model_id, state=state)

    if model_id == "claude":
        notes = [
            "Stay in this same continuous chat across exploration, planning, edits, verification, and follow-up work so prior file context stays intact.",
            "Deep-think before editing, plan multi-file dependencies up front, and carry long agentic runs through complete end-to-end verification without shortcuts.",
        ]
        if root is not None:
            is_self_repo = (root / "wsa_server" / "__init__.py").is_file() and (root / "wsa_agent").is_dir()
            if is_self_repo:
                notes.append(
                    "Note on this repository: patches under `wsa_agent/` or `wsa_server/` undergo local syntax/import verification with automatic rollback on invalid code; verify changes with `pytest`."
                )
            if "Python" in proj_type or is_self_repo:
                notes.append("Python stack detected: verify edits with `python -m pytest` and `py_compile`.")
            elif "Node" in proj_type or "JavaScript" in proj_type or "TypeScript" in proj_type:
                notes.append("JS/TS stack detected: verify edits with `node --check`, tests, or typecheck.")
            if (root / "wsa-browser-bridge").is_dir():
                notes.append("Extension directory present: verify changed files in `wsa-browser-bridge/` with `node --check`.")
        if perm == "strict":
            notes.append("Strict terminal approval mode is enabled: briefly explain any patch or command before I approve it.")
        return CLAUDE_SYSTEM_PROMPT.rstrip() + "\n\nProject environment notes for this session:\n- " + "\n- ".join(notes) + "\n"

    sections = [SYSTEM_PROMPT.rstrip()]
    web_detail = f", web_model={web_model}" if web_model else ""
    adapt_lines = [
        f"Bridge: {m_info.get('name', model_id)} via {m_info.get('site', 'web')} ({m_info.get('transport', 'Web Bridge')}{web_detail}).",
    ]
    if acct_lbl or acct_email:
        who = f"{acct_lbl}" + (f" <{acct_email}>" if acct_email else "")
        via = f" via {browser} DOM relay" if browser else ""
        adapt_lines.append(f"Session: {who}{via} — preserve thread continuity.")

    if model_id == "chatgpt":
        adapt_lines.append(
            "ChatGPT Web: put local actions in plain ```tool_call JSON blocks (no Canvas/widgets). "
            "WSA executes those requests locally and returns real outputs next turn; no native browser access is needed. Wait for requested `read_file` outputs before assuming contents."
        )
    elif model_id == "deepseek":
        adapt_lines.append(
            "DeepSeek Web: trace dataflow and edge cases before emitting complete ```tool_call JSON edits across rounds until verified."
        )
    elif model_id == "venice":
        adapt_lines.append(
            "Venice Web (DeepSeek V4.1 Flash Uncensored): leverage full 524K context across dependencies and edge cases with inline ```tool_call JSON blocks until verified."
        )
    elif model_id == "gemini":
        adapt_lines.append(
            "Gemini Web: decompose all affected files and failure modes up front, edit in dependency order, and verify end-to-end before stopping."
        )

    if getattr(state, "agent_mode", "singleagent") == "multiagent" and getattr(state, "multiagent_queue", None):
        q_str = " -> ".join(state.multiagent_queue)
        adapt_lines.append(
            f"Multiagent failover (queue: {q_str}; active: {model_id}): on rate/usage limits or silent downgrade, control transfers with compacted history — resume from the exact next pending step, never restart."
        )

    adapt_lines.append(
        "DOM-relay agentic discipline: batch related reads/searches/patches in parallel ```tool_call blocks, apply surgical hunks (`patch_file`/`apply_patch`), finish end-to-end, and verify before stopping."
    )

    if root is not None:
        is_self_repo = (root / "wsa_server" / "__init__.py").is_file() and (root / "wsa_agent").is_dir()
        if is_self_repo:
            adapt_lines.append(
                "Self-modification safety: `wsa_agent/` and `wsa_server/` edits run syntax/import checks with auto-rollback; verify with `pytest`."
            )
        if "Python" in proj_type or is_self_repo:
            adapt_lines.append("Verify Python changes with `python -m pytest` and `py_compile`.")
        elif "Node" in proj_type or "JavaScript" in proj_type or "TypeScript" in proj_type:
            adapt_lines.append("Verify JS/TS changes with `node --check`, tests, or typecheck.")

        if (root / "wsa-browser-bridge").is_dir():
            adapt_lines.append("Verify extension changes in `wsa-browser-bridge/` with `node --check`.")

    if perm == "strict":
        adapt_lines.append("Strict approval active: explain mutating operations clearly.")

    adapt_lines.append(
        "Custom helpers: define reusable workspace utilities via `create_custom_tool(name, description, python_code, parameters_json)` (`def run(**kwargs) -> str`)."
    )

    _budget_kb = {"deepseek": "~18KB", "chatgpt": "~21KB"}.get(model_id, "~96KB")
    adapt_lines.append(
        f"Shared web budget: this thread's total prompt is capped ({_budget_kb} across all rounds) — keep reads, patches, and fetch/search caps tight and track progress in `todo`."
    )

    sections.append("\n━━━ DYNAMIC RUNTIME ADAPTATION ━━━\n• " + "\n• ".join(adapt_lines))
    return "\n".join(sections) + "\n"
