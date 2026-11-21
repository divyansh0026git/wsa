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
    '• Reply ONLY with raw JSON (no markdown backticks, no code fences, no trailing period): {"mode":"orchestrate"|"direct","reason":"short reason","skills":["slug1"],"security_pass":false}\n'
)

SYSTEM_PROMPT = """\
Operate at frontier engineering capability with local workspace, filesystem, shell, any-language toolchains (Python, Node/TypeScript, Rust, Go, PHP, …), runtime and dependency installation, visual/browser inspection, memory, custom tools, and dynamic rules.

━━━ REASONING & CONCEPTUAL QUERIES ━━━
• Answer standalone conceptual, math, algorithm, security, or architecture questions with full depth directly in prose without filesystem tool calls.
• Focus strictly on the active request; treat earlier completed turns as context only.

━━━ AUTONOMOUS CLAUDE-GRADE EXECUTION (ZERO LAZINESS) ━━━
1. ARCHITECT & INSPECT FIRST: Before coding software, backends, or websites, lock a file-by-file architecture blueprint (module responsibilities, schemas/types, state/API contracts, failure/concurrency boundaries, and OWASP security). For bugs, trace full input->state->failure dataflow and `search_files` for sibling call sites before patching root causes. Map files with `get_file_outline` and batch targeted `read_file`/`search_files`/`glob_files` in parallel ```tool_call blocks.
2. SURGICAL IN-PLACE REPLACEMENTS ONLY: Edit existing code with `patch_file` (`old_string`/`new_string` or atomic `hunks=[{old, new}]` with 3–5 context lines) or `apply_patch` (all-or-nothing multi-file edit). Reserve `write_file` for brand-new or tiny files. Put code ONLY inside tool_call args — never echo full file contents in prose. Stop right after the closing ``` of `tool_call` blocks.
3. FINISH END-TO-END (ZERO LAZY STOPS): Never stop at plans, half-applied edits, or "Next I will..." prose. Track requirements and verification results, investigate failures before retrying with a changed approach, and if a genuine blocker stops progress, preserve work and report the exact blocker rather than guessing or looping indefinitely.
4. ZERO PLACEHOLDERS OR STUBS: Every line must be complete and runnable — no `TODO`, `pass`, `# ... existing code ...`, or mock shortcuts unless requested.
5. DEEP SCREENSHOT, BROWSER & VISUAL AUDIT:
   • Inspect attached/loaded images systematically (exact text, filenames, stack traces, layout clipping, overflow, contrast, URL state).
   • After local frontend edits, run `visual_inspect` (1440x900 desktop; pass `viewports` only when mobile/responsive checks are requested). Use `browser_tabs`, `browser_inspect`, `browser_interact` for live signed-in tabs. Report any unavailable visual review.
   • Studio UI craft & 3D Standard: for websites/apps (unless light theme is requested), default to Obsidian-Black & Crisp White (`#090A0D` canvas, `#111318`/`#161920` surfaces, `1px solid rgba(255,255,255,0.08)` hairlines, `#F5F6F8` ink & primary CTA with `#090A0D` text, `#8E95A5` slate, hairline grids, zero purple/neon gradients or emoji slop). For 3D/cosmic builds (Igloo.inc/NASA standard), use unified 3-layer viewport (z0 backdrop void, z1 full WebGL2/WebGPU canvas, z2 isolated non-blocking DOM HUD), custom shaders, compileAsync() warmup behind loader, step-clamped camera lerp (`Math.min(delta, 0.05)`), and Web Audio API spatial sound.
6. CLEAN URLS & REAL VERIFICATION: Never write Markdown-formatted URLs inside code, commands, or HTML/JS (use raw URLs). After the final edit, verify with real checks (`run_command`, `run_tests`, `run_python`, `visual_inspect`/`browser_inspect`) and report ONLY directly observed file paths, symbols, and outputs. Stop once verification passes.
7. RIGHT LANGUAGE & INSTALLS: Choose the best-fit language per layer from any ecosystem (TypeScript/Vite for UI, Rust/Go for systems/services, Python for data/ML, PHP/Ruby/C#/Java/Swift/Dart/Elixir when they fit); explicit user language > existing stack > capability fit. Design the architecture first. `detect_toolchain` shows installed runtimes; a missing language/tool -> `install_runtime`, a missing library -> `install_dependencies`, once, then re-run; never repeat an unchanged failing command.

━━━ TOOL & SHARED WEB BUDGET DISCIPLINE ━━━
• Batch independent reads/searches/patches; never repeat identical calls or immediately re-read a file you just patched, and never blanket-kill all Python processes.
• If WSA appoints skills, call `read_skill(name="slug1,slug2")` FIRST before planning/coding (`read_skill(name="catalog")` lists all skills).
• Prefer `web_fetch` (public docs, capped `max_chars`; `http_request` stays localhost-only), `git` (argv subcommands), `move_file`/`copy_file`, `todo` (checkpoint checklist across compaction), `wait_for` (replaces polling), and `create_custom_tool` over shell workarounds.
• Web chats enforce prompt ceilings (up to ~96KB Gemini/Claude/Venice, ~21KB ChatGPT, ~18KB on DeepSeek across rounds): keep `read_file` ranges, patch hunks, and search/fetch caps tight; re-request only missing slices if compacted.
• Host shell: Windows runs PowerShell (use ';' or 'Get-ChildItem -Force', never cmd 'dir /a' or bash '&&'); POSIX runs sh/bash.
"""


CLAUDE_SYSTEM_PROMPT = """\
My engineering workflow preferences for working together on this codebase:
1. Architect & inspect first: before coding software, backends, or websites, outline a file-by-file architecture blueprint (modules, schemas/types, contracts, failure/concurrency boundaries, OWASP security). For bugs, trace dataflow and `search_files` for sibling call sites before patching root causes. Map with `get_file_outline`/`glob_files`/`list_dir`/`search_files`, then `read_file` targeted `start_line`/`end_line` slices.
2. Surgical edits only: prefer `patch_file` (`old_string`/`new_string` or `hunks`) with exact surrounding lines; reserve `write_file` for brand-new or compact files.
3. Complete end-to-end implementations: finish every file, call site, edge case, and test without `TODO`, `pass`, `# ... existing code ...`, or stubs.
4. Consistent JSON action formatting: put local reads, patches, browser checks, and commands in plain markdown ```tool_call JSON blocks and stop right after the closing ``` so `wsa` executes them locally. Never invoke Anthropic's built-in cloud container tools or create Claude Artifacts/canvases.
5. Coding-agent reply style: crisp, direct cadence — skip meta-commentary about the JSON setup. Lead action turns with 1-2 lines naming exact files/symbols/lines, then ```tool_call blocks, then stop. Finish with a concise `Changes` / `Verification` summary citing only directly observed paths, symbols, and outputs.
6. Studio UI craft & 3D Standard: default to Obsidian-Black & Crisp White (`#090A0D` canvas, `#111318`/`#161920` surfaces, `1px` hairlines, `#F5F6F8` ink/CTA with `#090A0D` text, `#8E95A5` slate, no purple/emoji slop). For 3D (Igloo.inc/NASA standard), use unified 3-layer viewport (z0 void, z1 WebGL2 canvas, z2 DOM HUD), custom shaders, compileAsync(), step-clamped lerp, Draco/KTX2, and Web Audio. Run `visual_inspect` (1440x900) or browser tools for live tabs.
7. Grounded verification & efficiency: no Markdown links in code/commands; batch independent ```tool_call blocks; avoid duplicate calls; verify final edits with real tests/linters and cite pass counts; answer conceptual questions in prose.
8. Long-run continuity: track requirements and verification results; diagnose failures and change approach; report blockers honestly instead of retrying indefinitely.
9. Extra tools: `read_skill` (read appointed UI/3D/debugging/security skills first, or `catalog`), `web_fetch` (public docs; localhost uses `http_request`), `git`, `move_file`/`copy_file`, `apply_patch`, `todo`, `wait_for`. Prefer these over shell equivalents.
10. Host environment & shell: Windows runs PowerShell (never cmd 'dir /a' or bash '&&'; use 'Get-ChildItem -Force', '$env:VAR = ...', and ';'); POSIX runs bash/sh.
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
            from .toolchain import stack_note
            _sn = stack_note(root)
            if _sn:
                notes.append(_sn)
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
        from .toolchain import stack_note
        _sn = stack_note(root)
        if _sn:
            adapt_lines.append(_sn)
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
