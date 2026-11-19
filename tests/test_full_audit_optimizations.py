"""Regression & performance tests verifying Phase 1-4 audit optimizations across prompts,
fast-flow routing, StreamRenderer streaming cache, and extension DOM helpers.
"""
from __future__ import annotations

import io
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from rich.console import Console

from wsa_agent import agent, context, prompts, renderer, tools
from wsa_agent.session import SessionState
from wsa_server import multimodal as mm


@pytest.mark.parametrize("provider,max_turn1_bytes", [
    ("chatgpt", 14_000),
    ("deepseek", 14_000),
    ("gemini", 15_000),
    ("venice", 15_000),
    ("claude", 16_000),
])
def test_compressed_turn1_prompts_stay_lean_across_providers(tmp_path, provider, max_turn1_bytes):
    state = SessionState(project_root=tmp_path, selected_model=provider)
    sys_prompt = prompts.build_dynamic_system_prompt(state)
    rendered, _ = mm.messages_to_prompt(
        [{"role": "system", "content": sys_prompt}, {"role": "user", "content": "Fix typo in README.md"}],
        tools=tools.TOOL_DEFINITIONS,
        provider=provider,
    )
    byte_len = len(rendered.encode("utf-8"))
    assert byte_len <= max_turn1_bytes, f"{provider} Turn 1 bloated: {byte_len} > {max_turn1_bytes}"


@pytest.mark.parametrize("provider", ["gemini", "chatgpt", "deepseek", "venice"])
def test_non_claude_compact_tool_block_is_actually_compact(provider):
    full_block = mm._format_tools_prompt_block(tools.TOOL_DEFINITIONS, compact=False, provider=provider)
    compact_block = mm._format_tools_prompt_block(tools.TOOL_DEFINITIONS, compact=True, provider=provider)
    assert len(compact_block) <= int(len(full_block) * 0.60), (
        f"{provider} compact={len(compact_block)} vs full={len(full_block)}"
    )
    assert "### Active Stage Toolset Update" in compact_block
    assert "patch_file(" in compact_block
    assert "array<{old, new, start_line?, end_line?}>" in compact_block


@pytest.mark.parametrize("prompt", [
    "Explain what a mutex is",
    "What is systematic debugging?",
    "Fix a typo in README.md",
    "Rename foo to bar in utils.py",
    "Run pytest",
    "Check git status",
])
def test_fast_flow_skips_workflow_router_on_clear_short_tasks(tmp_path, prompt):
    c = context.classify_task_complexity(prompt)
    assert c["mode"] == "direct"
    assert c["fast_direct"] is True

    state = SessionState(project_root=tmp_path)
    with patch.object(agent, "_judge_workflow") as judge, \
         patch.object(agent, "execute_agent_loop", return_value={"ok": True}) as execute:
        res = agent.run_turn(prompt, state, Mock())
    assert res["ok"] is True
    judge.assert_not_called()
    execute.assert_called_once()


@pytest.mark.parametrize("prompt", [
    "I want a cozy place online to sell my pottery",
    "Create a shop for my pottery",
    "I want a stunning interactive digital gallery",
])
def test_ambiguous_or_product_prompts_still_invoke_workflow_router(tmp_path, prompt):
    c = context.classify_task_complexity(prompt)
    assert c["fast_direct"] is False

    state = SessionState(project_root=tmp_path)
    client = Mock()
    client.stream.return_value = {"content": '{"mode":"direct","reason":"scoped"}'}
    with patch.object(agent, "execute_agent_loop", return_value={"ok": True}) as execute:
        res = agent.run_turn(prompt, state, client)
    assert res["ok"] is True
    assert client.stream.call_count == 1
    assert execute.called


def test_stream_renderer_caches_stats_and_batches_high_rate_chunks():
    orig_console = renderer.console
    sink = io.StringIO()
    renderer.console = Console(file=sink, width=120, force_terminal=False)
    try:
        t0 = time.perf_counter()
        sr = renderer.StreamRenderer(round_num=0, model="gemini", stage_label="coder", flush_final=True)
        for i in range(400):
            sr.feed_thinking(f"Step {i}: checking mod_{i % 5}.py invariants.\n")
        for i in range(400):
            sr.feed(f"Line {i}: patched `mod_{i % 5}.py` cleanly.\n")
        sr.finish()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
    finally:
        renderer.console = orig_console

    rendered_out = sink.getvalue()
    assert elapsed_ms < 500.0, f"StreamRenderer too slow: {elapsed_ms:.1f} ms"
    assert "Line 0:" in rendered_out
    assert "Line 399:" in rendered_out
    assert "Line 399:" in sr.get_full_text()


def test_extension_read_error_banner_and_claude_single_upload_via_node():
    repo_root = Path(__file__).resolve().parent.parent
    js = r"""
const assert = require("assert");
const bg = require("./wsa-browser-bridge/background.js");

// Minimal DOM stub verifying Gemini readErrorBanner ignores "Gemini Auto" prose & catches real quota banner
function mkEl(tag, text, opts = {}) {
  const children = opts.children || [];
  return {
    tagName: tag.toUpperCase(),
    textContent: text,
    innerText: text,
    children,
    offsetParent: opts.hidden ? null : {},
    isConnected: true,
    disabled: Boolean(opts.disabled),
    accept: opts.accept || "",
    attributes: opts.attrs || {},
    style: opts.hidden ? { display: "none" } : { display: "block", visibility: "visible", opacity: "1" },
    getAttribute(k) { return this.attributes[k] ?? null; },
    getBoundingClientRect() { return opts.hidden ? { width: 0, height: 0 } : { width: 120, height: 24, left: 0, top: 0 }; },
    getClientRects() { return opts.hidden ? [] : [{ width: 120, height: 24 }]; },
    closest(sel) {
      if (opts.inResponse && /message-content|model-response/.test(sel)) return {};
      return null;
    },
    matches() { return false; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
  };
}

let allNodes = [];
let innerTextReads = 0;
globalThis.window = {
  location: { href: "https://gemini.google.com/app/test1" },
  getComputedStyle(el) { return el.style || { display: "block", visibility: "visible", opacity: "1" }; },
};
globalThis.document = {
  body: {
    get textContent() { return allNodes.map(n => n.textContent).join(" "); },
  },
  querySelectorAll(sel) {
    if (sel.includes("mat-dialog-container")) return allNodes.filter(n => n.attributes["role"] === "alert");
    if (sel.includes("div, p, span")) return allNodes.filter(n => ["DIV", "P", "SPAN"].includes(n.tagName));
    return [];
  },
  querySelector() { return null; },
};

// 1. Prose mentioning "Gemini Auto" must NOT appear in readErrorBanner when no limit exists
const proseEl = mkEl("p", "Users can switch to Gemini Auto for lighter tasks.", { inResponse: true });
Object.defineProperty(proseEl, "innerText", { get() { innerTextReads++; return proseEl.textContent; } });
allNodes = [proseEl];
bg._domInstallPageRuntimeFn();
const rt = globalThis.__wsaRuntime;
assert.strictEqual(rt.readErrorBanner(), "");
assert.strictEqual(innerTextReads, 0, "Zero innerText layout reflows on normal turns");

// 2. Real floating "Limit reached · Resets 6:39 AM" card IS detected cleanly
allNodes = [
  proseEl,
  mkEl("div", "Limit reached · Resets 6:39 AM", {}),
];
const banner = rt.readErrorBanner();
assert.ok(banner.includes("Limit reached"), "Real limit banner must be detected: " + banner);
"""
    proc = subprocess.run(
        ["node", "-e", js],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


def test_content_script_bridge_debounces_element_mutations_without_innertext():
    src = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "content_script_bridge.js").read_text(encoding="utf-8")
    assert "WAIT_MODAL_BTN_SELECTOR" in src
    assert "n.nodeType === 1" in src
    assert "scheduleWaitDismissCheck" in src
    # Verify NO layout-reflowing innerText reads remain inside content_script_bridge.js
    assert ".innerText" not in src


def test_visual_inspect_click_fires_exactly_once():
    from wsa_agent.visual import _HeadlessCDPClient, find_chromium_binary

    binary = find_chromium_binary()
    if not binary:
        pytest.skip("Chromium is required for headless CDP click check")
    with _HeadlessCDPClient(binary) as cdp:
        cdp.set_viewport(800, 600, mobile=False)
        cdp.evaluate(
            "document.body.innerHTML = '<button id=\"inc\" type=\"button\">Add</button>';"
            "window.clicks = 0;"
            "document.getElementById('inc').addEventListener('click', () => { window.clicks += 1; });"
        )
        cdp.run_actions([{"type": "click", "selector": "#inc"}])
        assert cdp.evaluate("window.clicks") == 1


def test_budget_hallucination_in_loop_override_recovers(tmp_path):
    """Verify fake 0-call tool-budget claims are overridden so the agent emits tool calls."""
    prev_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    state = SessionState(project_root=tmp_path, permission_mode="auto", auto_approve=True)
    calls = []

    def fake_stream(**kwargs):
        calls.append(kwargs)
        n = len(calls)
        if n == 1:
            assert kwargs.get("new_chat") is True
            return {"content": "Tool execution budget for this turn is exhausted (0 calls remaining)."}
        if n == 2:
            last_msg = str(kwargs["messages"][-1]["content"])
            assert "there is no 0-call cap here" in last_msg
            assert "System Override" not in last_msg
            assert kwargs.get("new_chat") is None
            return {
                "content": '```tool_call\n{"name": "write_file", "arguments": {"path": "ok.py", "content": "X = 1\\n"}}\n```'
            }
        if n == 3:
            assert kwargs.get("new_chat") is None
            return {
                "content": '```tool_call\n{"name": "run_command", "arguments": {"command": "python -m py_compile ok.py"}}\n```'
            }
        return {"content": "All changes verified cleanly."}

    client = Mock()
    client.stream.side_effect = fake_stream
    try:
        res = agent.execute_agent_loop(
            "Write ok.py",
            state,
            client,
            max_rounds=6,
            new_chat_override=True,
            enforce_verify_gate=True,
            quiet_ui=True,
            live_ui=False,
        )
    finally:
        tools.set_project_root(prev_root)

    assert res["ok"] is True, res
    assert res["budget_hallucinated"] is False
    assert (tmp_path / "ok.py").exists()
    assert len(calls) == 4


def test_orchestrator_fresh_chat_recovery_on_zero_write_missing_owned_files(tmp_path):
    """Verify a poisoned 0-write coder task clears thread affinity, retries in fresh chat, and recovers."""
    from wsa_agent import subagents

    prev_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    state = SessionState(project_root=tmp_path, selected_model="gemini", permission_mode="auto", auto_approve=True)
    state.active_chat_url = "https://gemini.google.com/app/poisoned1"
    state.chat_urls["gemini"] = "https://gemini.google.com/app/poisoned1"
    plan = [
        {"id": "t1", "title": "Build a.js", "owned_files": ["a.js"], "depends_on": []},
        {"id": "t2", "title": "Build b.js", "owned_files": ["b.js"], "depends_on": ["t1"]},
    ]
    calls = []

    def role(**kwargs):
        stage = kwargs.get("label") or kwargs["role"]
        calls.append((stage, kwargs.get("new_chat_override"), kwargs.get("prompt", "")))
        if stage == "explorer":
            return {"ok": True, "final_text": "Clean workspace"}
        if stage == "architect":
            return {"ok": True, "final_text": json.dumps(plan)}
        if stage == "coder:t1":
            if sum(1 for s, _, _ in calls if s == "coder:t1") == 1:
                return {
                    "ok": True,
                    "tool_calls_count": 0,
                    "written_files": [],
                    "budget_hallucinated": True,
                    "final_text": "Task t1 could not be written (0 calls remaining).",
                }
            assert kwargs.get("new_chat_override") is True
            assert "Fresh session continuation" in kwargs["prompt"]
            (tmp_path / "a.js").write_text("export const a = 1;\n", encoding="utf-8")
            return {"ok": True, "tool_calls_count": 2, "written_files": ["a.js"], "final_text": "Built a.js"}
        if stage == "coder:t2":
            (tmp_path / "b.js").write_text("export const b = 2;\n", encoding="utf-8")
            return {"ok": True, "tool_calls_count": 2, "written_files": ["b.js"], "final_text": "Built b.js"}
        if kwargs["role"] == "verifier":
            return {
                "ok": True,
                "final_text": '{"verdict":"pass","checks":["node check ok"],"failures":[]}',
                "verification_evidence": [{"tool": "run_command", "ok": True}],
            }
        return {"ok": True, "final_text": "done"}

    client = Mock()
    try:
        with patch.object(subagents, "_run_role_agent", side_effect=role), \
             patch.object(subagents, "_discover_live_relays", return_value=[]), \
             patch.object(state, "auto_checkpoint"):
            res = subagents.run_orchestrated_turn("Implement a.js and b.js", state, client)
    finally:
        tools.set_project_root(prev_root)

    assert res["ok"] is True
    assert sum(1 for s, _, _ in calls if s == "coder:t1") == 2
    assert sum(1 for s, _, _ in calls if s == "coder:t2") == 1
    assert state.chat_urls.get("gemini") is None
    assert client.clear_session_chats.called


def test_orchestrator_fails_closed_when_retry_still_missing_owned_files(tmp_path):
    """Verify a 0-write coder task whose retry also writes nothing fails closed instead of marking ok=True."""
    from wsa_agent import subagents

    prev_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    state = SessionState(project_root=tmp_path, selected_model="gemini", permission_mode="auto", auto_approve=True)
    plan = [
        {"id": "t1", "title": "Build a.js", "owned_files": ["a.js"], "depends_on": []},
        {"id": "t2", "title": "Build b.js", "owned_files": ["b.js"], "depends_on": ["t1"]},
    ]
    calls = []

    def role(**kwargs):
        stage = kwargs.get("label") or kwargs["role"]
        calls.append(stage)
        if stage == "explorer":
            return {"ok": True, "final_text": "Clean workspace"}
        if stage == "architect":
            return {"ok": True, "final_text": json.dumps(plan)}
        if stage == "coder:t1":
            return {
                "ok": True,
                "tool_calls_count": 0,
                "written_files": [],
                "budget_hallucinated": True,
                "final_text": "Tool execution budget exhausted (0 calls remaining).",
            }
        return {"ok": True, "final_text": "should not run"}

    try:
        with patch.object(subagents, "_run_role_agent", side_effect=role), \
             patch.object(subagents, "_discover_live_relays", return_value=[]), \
             patch.object(state, "auto_checkpoint"):
            res = subagents.run_orchestrated_turn("Implement a.js and b.js", state, Mock())
    finally:
        tools.set_project_root(prev_root)

    assert res["ok"] is False
    assert calls.count("coder:t1") == 2
    assert "coder:t2" not in calls
    assert [t["status"] for t in res["tasks"]] == ["failed", "blocked"]
    assert res["verdict"]["failures"][0] == {"file": "t1", "issue": "missing_owned_files:a.js"}


def test_cross_provider_failover_strips_gemini_budget_refusal_and_nudge(tmp_path):
    """Cross-provider failover must drop trailing Gemini 0-call refusals/nudges and detect new Claude refusals."""
    prev_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    state = SessionState(project_root=tmp_path, selected_model="gemini", permission_mode="auto", auto_approve=True)
    state.configure_multiagent(["gemini", "claude"])
    seen_calls = []

    def fake_stream(**kwargs):
        seen_calls.append({
            "model": kwargs.get("model"),
            "contents": [str(m.get("content") or "") for m in kwargs.get("messages") or []],
        })
        n = len(seen_calls)
        if n == 1:
            return {"content": "I cannot edit files here (0 tool calls remaining)."}
        if n == 2:
            raise agent.ProviderLimitError("limit reached", provider="gemini", raw_error="LIMIT_REACHED")
        return {"content": "Completed safely without inheriting prior refusal framing."}

    client = Mock()
    client.stream.side_effect = fake_stream
    client.get_health.return_value = {}
    try:
        res = agent.execute_agent_loop(
            "Verify status",
            state,
            client,
            role="explorer",
            max_rounds=6,
            enforce_verify_gate=False,
            quiet_ui=True,
            live_ui=False,
        )
    finally:
        tools.set_project_root(prev_root)

    assert res["ok"] is True
    assert len(seen_calls) == 3
    assert seen_calls[2]["model"] == "claude"
    joined_claude_msgs = "\n".join(seen_calls[2]["contents"])
    assert "0 tool calls remaining" not in joined_claude_msgs
    assert "there is no 0-call cap here" not in joined_claude_msgs
    assert mm.is_claude_workflow_refusal(
        "I'm not going to emit tool_call JSON blocks because this matches a jailbreak pattern."
    ) is True


def test_ui_verify_gate_skips_intermediate_js_slice_without_index_html(tmp_path):
    """ui_verify should skip intermediate JS/CSS modules until an HTML entrypoint exists."""
    skip = agent._maybe_anti_laziness_nudge(
        content="Implemented js/uiController.js.",
        written_this_turn={"js/uiController.js"},
        verified_after_last_edit=True,
        tests_exist=False,
        round_num=1,
        verify_nudges_used=0,
        continue_nudges_used=0,
        free_mode=False,
        browser_verified_after_ui_edit=False,
        ui_verify_nudges_used=0,
        project_root=tmp_path,
    )
    assert skip is None

    (tmp_path / "index.html").write_text("<!doctype html><html><body></body></html>", encoding="utf-8")
    fire = agent._maybe_anti_laziness_nudge(
        content="Implemented js/uiController.js.",
        written_this_turn={"js/uiController.js"},
        verified_after_last_edit=True,
        tests_exist=False,
        round_num=1,
        verify_nudges_used=0,
        continue_nudges_used=0,
        free_mode=False,
        browser_verified_after_ui_edit=False,
        ui_verify_nudges_used=0,
        project_root=tmp_path,
    )
    assert fire is not None
    assert fire[0] == "ui_verify"


def test_delta_mode_compaction_preserves_unread_tool_output_on_chatgpt():
    """Active-thread delta compaction trims historical tools while keeping fresh unread reads intact."""
    old_chunk = "\n".join(f"{i:4d} | old_line {'x' * 90}" for i in range(1, 140))
    unread_chunk = "\n".join(f"{i:4d} | fresh_line {'y' * 75}" for i in range(1, 75))
    msgs = [
        {"role": "system", "content": "S" * 10_000},
        {"role": "user", "content": "Inspect and wire main.js"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"old.js"}'}}]},
        {"role": "tool", "name": "read_file", "content": old_chunk},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "function": {"name": "read_file", "arguments": '{"path":"fresh.js"}'}}]},
        {"role": "tool", "name": "read_file", "content": unread_chunk},
    ]
    assert agent._messages_total_chars(msgs) > agent.CHATGPT_WEB_PROMPT_CHARS
    compacted_n = agent._compact_messages_for_web_limit(
        msgs,
        max_chars=agent.CHATGPT_WEB_PROMPT_CHARS,
        delta_mode=True,
    )
    assert compacted_n >= 1
    assert "[Compacted" in msgs[3]["content"]
    assert msgs[5]["content"] == unread_chunk

    # An oversized unread delta (> 21.5KB on its own) still gets bounded cleanly
    huge_unread = "\n".join(f"{i:4d} | huge_line {'z' * 110}" for i in range(1, 240))
    msgs.append({"role": "tool", "name": "read_file", "content": huge_unread})
    compacted_over = agent._compact_messages_for_web_limit(
        msgs,
        max_chars=agent.CHATGPT_WEB_PROMPT_CHARS,
        delta_mode=True,
    )
    assert compacted_over >= 1
    assert agent._messages_total_chars(msgs[5:]) <= agent.CHATGPT_WEB_PROMPT_CHARS + 300


def test_repeated_unmutated_read_hint_fires_on_third_repeat(tmp_path):
    """Repeating identical read-only calls 3 times without intervening edits appends a concise guidance note."""
    (tmp_path / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    prev_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    state = SessionState(project_root=tmp_path, permission_mode="auto", auto_approve=True)
    seen_tool_contents = []
    call_idx = 0

    def fake_stream(**kwargs):
        nonlocal call_idx
        call_idx += 1
        for m in kwargs.get("messages") or []:
            if m.get("role") == "tool" and m.get("name") == "read_file":
                c = str(m.get("content") or "")
                if c not in seen_tool_contents:
                    seen_tool_contents.append(c)
        if call_idx <= 3:
            return {"content": '```tool_call\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n```'}
        return {"content": "Finished reading."}

    client = Mock()
    client.stream.side_effect = fake_stream
    try:
        res = agent.execute_agent_loop(
            "Inspect a.txt",
            state,
            client,
            role="explorer",
            max_rounds=6,
            enforce_verify_gate=False,
            quiet_ui=True,
            live_ui=False,
        )
    finally:
        tools.set_project_root(prev_root)

    assert res["ok"] is True
    assert len(seen_tool_contents) == 2
    assert "already executed this exact read earlier" not in seen_tool_contents[0]
    assert "already executed this exact read earlier" in seen_tool_contents[-1]


@pytest.mark.parametrize("transient_err", [
    "ChatGPT prompt remained unsubmitted in composer (Send button/form did not trigger response)",
    "ChatGPT tab did not start or produce a text response",
])
def test_composer_unsubmitted_error_retries_and_recovers(tmp_path, transient_err):
    """Verify transient composer/no-response relay errors retry compactly, then reset to fresh chat."""
    prev_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    state = SessionState(project_root=tmp_path, selected_model="chatgpt", permission_mode="auto", auto_approve=True)
    state.active_chat_url = "https://chatgpt.com/c/sticky1"
    state.chat_urls["chatgpt"] = "https://chatgpt.com/c/sticky1"
    err = ConnectionError(f"API error: upstream error: DOM relay error: {transient_err}")

    seen_new_chat = []

    def two_fail_then_ok(**kwargs):
        seen_new_chat.append(kwargs.get("new_chat"))
        if len(seen_new_chat) <= 2:
            raise err
        return {"content": "Recovered cleanly in fresh chat."}

    client = Mock()
    client.stream.side_effect = two_fail_then_ok
    try:
        with patch.object(agent.time, "sleep"):
            res = agent.execute_agent_loop(
                "Finish index.html",
                state,
                client,
                max_rounds=4,
                enforce_verify_gate=False,
                quiet_ui=True,
                live_ui=False,
            )
    finally:
        tools.set_project_root(prev_root)

    assert res["ok"] is True
    assert client.stream.call_count == 3
    assert seen_new_chat == [None, None, True]


def test_tool_unavailable_misconception_clarification_nudge_recovers(tmp_path):
    """Verify web reasoning tool-unavailable misconceptions receive a bridge clarification and recover."""
    (tmp_path / "app.txt").write_text("ok\n", encoding="utf-8")
    tool_block = mm._format_tools_prompt_block(tools.TOOL_DEFINITIONS, provider="chatgpt")
    assert "local `wsa` CLI bridge" in tool_block
    turn1_prompt, _ = mm.messages_to_prompt(
        [{"role": "system", "content": "Helper"}, {"role": "user", "content": "Check app.txt"}],
        tools=tools.TOOL_DEFINITIONS,
        provider="chatgpt",
    )
    assert len(turn1_prompt.encode("utf-8")) <= 14_000

    prev_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    state = SessionState(project_root=tmp_path, selected_model="chatgpt", permission_mode="auto", auto_approve=True)
    calls = []

    def fake_stream(**kwargs):
        calls.append(kwargs)
        n = len(calls)
        if n == 1:
            return {
                "content": (
                    "One important limitation: the read_file and related WSA local-workspace tools "
                    "are not exposed in my current execution environment."
                )
            }
        if n == 2:
            assert "`wsa` parses plain markdown" in str(kwargs["messages"][-1]["content"])
            return {"content": '```tool_call\n{"name": "read_file", "arguments": {"path": "app.txt"}}\n```'}
        return {"content": "Read app.txt cleanly."}

    client = Mock()
    client.stream.side_effect = fake_stream
    try:
        res = agent.execute_agent_loop(
            "Check app.txt",
            state,
            client,
            max_rounds=6,
            enforce_verify_gate=False,
            quiet_ui=True,
            live_ui=False,
        )
    finally:
        tools.set_project_root(prev_root)

    assert res["ok"] is True, res
    assert res["budget_hallucinated"] is False
    assert len(calls) == 3


def test_truncated_mid_thought_with_appointed_skills_triggers_continue_nudge() -> None:
    from wsa_agent.agent import _maybe_anti_laziness_nudge

    # 1. Truncated mid-thought with appointed skills unread and zero tool calls
    truncated = _maybe_anti_laziness_nudge(
        content="Let me inspect the workspace and load skills first.\nActually batch: read_skill, glob",
        written_this_turn=set(),
        verified_after_last_edit=True,
        tests_exist=False,
        round_num=0,
        verify_nudges_used=0,
        continue_nudges_used=0,
        free_mode=False,
        missing_appointed_skills=("threejs-skills", "unslop"),
        tool_calls_count=0,
    )
    assert truncated is not None
    assert truncated[0] == "continue"
    assert 'read_skill(name="threejs-skills,unslop")' in truncated[1]

    # 2. Mid-thought cutoff ending in a bare token without deferred phrasing still nudges read_skill
    bare_cutoff = _maybe_anti_laziness_nudge(
        content="Checking project entrypoints and runtime configuration, glob",
        written_this_turn=set(),
        verified_after_last_edit=True,
        tests_exist=False,
        round_num=0,
        verify_nudges_used=0,
        continue_nudges_used=0,
        free_mode=False,
        missing_appointed_skills=("threejs-skills", "unslop"),
        tool_calls_count=0,
    )
    assert bare_cutoff is not None
    assert bare_cutoff[0] == "continue"
    assert 'read_skill(name="threejs-skills,unslop")' in bare_cutoff[1]

    # 3. Finished prose answer ending cleanly does not false-trigger
    finished = _maybe_anti_laziness_nudge(
        content="The workspace is already verified and complete.",
        written_this_turn=set(),
        verified_after_last_edit=True,
        tests_exist=False,
        round_num=0,
        verify_nudges_used=0,
        continue_nudges_used=0,
        free_mode=False,
        missing_appointed_skills=("threejs-skills", "unslop"),
        tool_calls_count=0,
    )
    assert finished is None


def test_layout_audit_js_excludes_sr_only_from_clipped_text() -> None:
    from wsa_agent.visual import _AUDIT_AND_SOM_JS

    assert "sr-only" in _AUDIT_AND_SOM_JS
    assert "visually-hidden" in _AUDIT_AND_SOM_JS
    assert "isSrOnly" in _AUDIT_AND_SOM_JS


def test_composer_unsubmitted_trims_oversized_user_handoff_message(tmp_path):
    """Verify oversized user handoff messages (with 0 tool messages) shrink on composer retry 1 and retry 2."""
    prev_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    state = SessionState(project_root=tmp_path, selected_model="chatgpt", permission_mode="auto", auto_approve=True)
    state.active_chat_url = "https://chatgpt.com/c/sticky_handoff"
    state.chat_urls["chatgpt"] = "https://chatgpt.com/c/sticky_handoff"
    err = ConnectionError(
        "API error: upstream error: DOM relay error: "
        "ChatGPT prompt remained unsubmitted in composer (Send button/form did not trigger response)"
    )

    seen_user_lens = []
    seen_new_chat = []

    def two_fail_then_ok(**kwargs):
        msgs = kwargs.get("messages") or []
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        seen_user_lens.append(len(str(user_msgs[-1].get("content") or "")) if user_msgs else 0)
        seen_new_chat.append(kwargs.get("new_chat"))
        if len(seen_new_chat) <= 2:
            raise err
        return {"content": "Handoff submitted after trimming."}

    client = Mock()
    client.stream.side_effect = two_fail_then_ok
    huge_handoff = "TASK HEADER: fix bugs\n" + ("Explorer detail line with context.\n" * 340) + "\nSTRICT SCOPE: index.html"
    assert len(huge_handoff) > 11_500
    try:
        with patch.object(agent.time, "sleep"):
            res = agent.execute_agent_loop(
                huge_handoff,
                state,
                client,
                max_rounds=3,
                enforce_verify_gate=False,
                quiet_ui=True,
                live_ui=False,
            )
    finally:
        tools.set_project_root(prev_root)

    assert res["ok"] is True
    assert seen_new_chat == [None, None, True]
    assert seen_user_lens[0] <= 8_000
    assert seen_user_lens[1] < seen_user_lens[0]
    assert seen_user_lens[1] < 7_500
    assert seen_user_lens[2] <= 5_000


def test_composer_unsubmitted_exhausted_fails_over_in_multiagent_mode(tmp_path):
    """Verify persistent composer-unsubmitted errors on primary provider fail over to next queued provider."""
    prev_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    state = SessionState(project_root=tmp_path, selected_model="chatgpt", permission_mode="auto", auto_approve=True)
    state.configure_multiagent(["chatgpt", "venice"])
    state.active_chat_url = "https://chatgpt.com/c/stalled1"
    state.chat_urls["chatgpt"] = "https://chatgpt.com/c/stalled1"
    err = ConnectionError(
        "API error: upstream error: DOM relay error: "
        "ChatGPT prompt remained unsubmitted in composer (Send button/form did not trigger response)"
    )

    seen_calls = []

    def three_chatgpt_fails_then_venice_ok(**kwargs):
        seen_calls.append((kwargs.get("model"), kwargs.get("new_chat")))
        if len(seen_calls) <= 3:
            raise err
        return {"content": "Completed on Venice failover."}

    client = Mock()
    client.stream.side_effect = three_chatgpt_fails_then_venice_ok
    try:
        with patch.object(agent.time, "sleep"):
            res = agent.execute_agent_loop(
                "Inspect and fix layout overflow",
                state,
                client,
                max_rounds=3,
                enforce_verify_gate=False,
                quiet_ui=True,
                live_ui=False,
            )
    finally:
        tools.set_project_root(prev_root)

    assert res["ok"] is True
    assert len(seen_calls) == 4
    assert [m for m, _ in seen_calls] == ["chatgpt", "chatgpt", "chatgpt", "venice"]
    assert seen_calls[3] == ("venice", True)
    assert state.model == "venice"


def test_orchestrator_deep_architecture_blueprint_and_visual_contrast_audit():
    """Verify deep research/blueprint extraction in orchestrator, normal/3D UI baseline, backend/debugging rules, and visual contrast/header-bleed flags."""
    from wsa_agent import task_skills
    from wsa_agent.subagents import ROLE_SYSTEM_PROMPTS, _extract_architect_blueprint
    from wsa_agent.visual import _AUDIT_AND_SOM_JS, format_visual_report

    assert "Deep Architecture & File Blueprint" in ROLE_SYSTEM_PROMPTS["architect"]
    assert "Saturn" in ROLE_SYSTEM_PROMPTS["architect"]
    assert "Deep Research" in ROLE_SYSTEM_PROMPTS["explorer"]
    assert "#090A0D" in ROLE_SYSTEM_PROMPTS["explorer"]
    assert "#090A0D" in ROLE_SYSTEM_PROMPTS["architect"]
    assert "#F5F6F8" in prompts.SYSTEM_PROMPT
    assert "#F5F6F8" in prompts.CLAUDE_SYSTEM_PROMPT
    assert "sibling call sites" in prompts.SYSTEM_PROMPT
    assert "sibling call sites" in ROLE_SYSTEM_PROMPTS["coder"]

    imp_txt, _ = task_skills.read_skill_content("impeccable,karpathy-skills")
    assert "#090A0D" in imp_txt
    assert "System, Backend & File Architecture Blueprint" in imp_txt

    backend_appt = task_skills.analyze_prompt_skills("Implement a FastAPI backend service with database endpoints")
    assert {"karpathy-skills", "test-driven-development", "semgrep-guardrails"}.issubset(set(backend_appt.skills))

    sample_arch = (
        "### Deep Architecture & File Blueprint\n"
        "- index.html: semantic shell & frosted glass overlays\n"
        "- js/stage.js: unified FogExp2 atmospheric continuum\n\n"
        "```json\n"
        '[{"id": "t1", "title": "Build stage", "owned_files": ["js/stage.js"], "depends_on": [], "acceptance": "ok", "ui": true}]\n'
        "```"
    )
    bp = _extract_architect_blueprint(sample_arch)
    assert "### Deep Architecture & File Blueprint" in bp
    assert "js/stage.js: unified FogExp2" in bp
    assert '"owned_files"' not in bp

    assert "lowContrastTexts" in _AUDIT_AND_SOM_JS
    assert "headerBleeds" in _AUDIT_AND_SOM_JS
    assert "hasFixedBgCanvas" in _AUDIT_AND_SOM_JS

    report = format_visual_report({
        "ok": True,
        "tiles": ["DESKTOP 1440x900 [SoM]", "DESKTOP MID-SCROLL 1440x900"],
        "viewports": [{
            "name": "desktop",
            "width": 1440,
            "height": 900,
            "pixels": {"ok": True, "std_luma": 48.0, "unique_buckets": 64, "blank": False},
            "audit": {
                "overflow_x": 0,
                "low_contrast_texts": ['p.material-desc (unbacked dark text over 3D canvas "A quiet foundation")'],
                "header_bleeds": ["header.site-header (transparent sticky header bleeds scrolled content)"],
            },
        }],
        "console_errors": [],
        "page_exceptions": [],
        "failed_requests": [],
    })
    assert "LOW_CONTRAST=1" in report
    assert "HEADER_BLEED=1" in report
    assert "Visual checks: fail" in report


def test_section_balanced_skill_compression_preserves_guardrails_in_8_skill_batch() -> None:
    """Verify batched 8-skill read stays compact and preserves critical bottom Guardrails/Anti-Slop rules."""
    from wsa_agent import task_skills as ts

    batch, loaded = ts.read_skill_content(
        "frontend-design,unslop,impeccable,web-design-guidelines,threejs-skills,dream-loop,img2threejs,gsap-skills"
    )
    assert len(loaded) == 8
    assert len(batch) <= 9400

    def _chunk(slug: str) -> str:
        start = batch.find(f"=== [{slug}]")
        assert start != -1, f"Missing {slug} in batch"
        nxt = batch.find("=== [", start + 10)
        return batch[start : nxt if nxt != -1 else len(batch)]

    imp = _chunk("impeccable")
    assert "#090A0D" in imp
    assert "backdrop-filter" in imp
    assert "FogExp2" in _chunk("threejs-skills")
    dream = _chunk("dream-loop")
    assert "Saturn" in dream
    assert "FogExp2" in dream
    assert "Saturn" in _chunk("img2threejs")

    kar_brief, _ = ts.read_skill_content("karpathy-skills", mode="brief")
    assert len(kar_brief) <= 3400
    assert "System, Backend & File Architecture Blueprint" in kar_brief


def test_claude_stage_directives_synced_with_security_and_role_scoped_skills() -> None:
    """Verify Claude stage directives stay synced with security role and role/slice skill scoping."""
    from wsa_agent import subagents, task_skills

    cd = subagents.CLAUDE_STAGE_DIRECTIVES
    assert set(cd.keys()) >= {"explorer", "architect", "coder", "security", "verifier"}
    assert "#090A0D" in cd["explorer"]
    assert "Deep Architecture & File Blueprint" in cd["architect"]
    assert "Shared Contract Matrix" in cd["architect"]
    assert "Read-Before-Write Contract Check" in cd["coder"]
    assert "security-audit,semgrep-guardrails" in cd["security"]
    assert "Cross-File Contract Audit" in cd["verifier"]

    appt = task_skills.analyze_prompt_skills(
        "Build a premium 3D igloo.inc website with Three.js shaders and GSAP scroll storytelling"
    )
    exp_skills = subagents._select_stage_skills("explorer", [], appt)
    arch_skills = subagents._select_stage_skills("architect", [], appt)
    c3d = subagents._select_stage_skills("coder", ["js/shaders.js", "js/heroasset.js", "js/stagecontroller.js"], appt)
    cui = subagents._select_stage_skills("coder", ["index.html", "styles/tokens.css", "styles/main.css"], appt)
    sec = subagents._select_stage_skills("security", ["index.html"], appt)
    ver = subagents._select_stage_skills("verifier", [], appt)

    assert 1 <= len(exp_skills) <= 4
    assert 1 <= len(arch_skills) <= 4
    assert set(c3d).issubset({"threejs-skills", "img2threejs", "dream-loop", "gsap-skills"})
    assert set(cui).issubset({"impeccable", "unslop", "frontend-design", "web-design-guidelines"})
    assert sec == ("security-audit", "semgrep-guardrails")
    assert ver

    all_tasks = [
        {"id": "t1", "owned_files": ["styles/tokens.css", "js/shaders.js"], "depends_on": []},
        {"id": "t2", "owned_files": ["js/stagecontroller.js"], "depends_on": ["t1"]},
    ]
    up = subagents._collect_upstream_contract_files(
        all_tasks[1], all_tasks, {"design-spec.md", "styles/tokens.css", "js/shaders.js"}, ["js/stagecontroller.js"]
    )
    assert "styles/tokens.css" in up
    assert "js/shaders.js" in up
    assert "design-spec.md" in up
    assert "js/stagecontroller.js" not in up


def test_rendered_pixel_contrast_audit_catches_dark_text_over_dark_canvas_and_passes_clean_ui() -> None:
    """Verify pixel contrast audit catches dark-text-on-dark-canvas splits while passing clean obsidian UI."""
    from PIL import Image, ImageDraw
    from wsa_agent import visual

    def _to_png(im: Image.Image) -> bytes:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    # 1. Harsh split canvas (pitch-dark sky + bright floor) with dark text in upper sky
    split_im = Image.new("RGB", (1440, 900), (10, 15, 20))
    ImageDraw.Draw(split_im).rectangle([(0, 480), (1440, 900)], fill=(220, 226, 232))
    split_audit = {
        "viewport": {"width": 1440, "height": 900},
        "canvases": [{"selector": "canvas.stage", "width": 1440, "height": 900}],
        "overflow_x": 0,
        "text_boxes": [
            {"selector": "p.eyebrow", "text": "01 / Instrument", "fg": [19, 26, 34], "x": 60, "y": 140, "w": 220, "h": 24, "fontSize": 14}
        ],
    }
    visual._audit_rendered_pixels_for_contrast(_to_png(split_im), split_audit)
    assert split_audit.get("low_contrast_texts")
    assert split_audit.get("canvas_palette_warnings")
    report = visual.format_visual_report({
        "ok": True,
        "tiles": ["DESKTOP 1440x900"],
        "viewports": [{
            "name": "desktop",
            "width": 1440,
            "height": 900,
            "pixels": {"ok": True, "std_luma": 65.0, "unique_buckets": 24, "blank": False},
            "audit": split_audit,
        }],
        "console_errors": [],
        "page_exceptions": [],
        "failed_requests": [],
    })
    assert "LOW_CONTRAST=1" in report
    assert "CANVAS_PALETTE_MISMATCH" in report
    assert "Visual checks: fail" in report

    # 2. Clean obsidian-black (#090A0D) UI with crisp off-white text (#F5F6F8)
    obsidian_im = Image.new("RGB", (1440, 900), (9, 10, 13))
    clean_audit = {
        "viewport": {"width": 1440, "height": 900},
        "canvases": [{"selector": "canvas.stage", "width": 1440, "height": 900}],
        "overflow_x": 0,
        "text_boxes": [
            {"selector": "h1.hero", "text": "Precision Spatial", "fg": [245, 246, 248], "x": 60, "y": 180, "w": 400, "h": 60, "fontSize": 48},
            {"selector": "p.lead", "text": "Architectural obsidian UI", "fg": [142, 149, 165], "x": 60, "y": 260, "w": 420, "h": 24, "fontSize": 16},
        ],
    }
    visual._audit_rendered_pixels_for_contrast(_to_png(obsidian_im), clean_audit)
    assert not clean_audit.get("low_contrast_texts")
    assert not clean_audit.get("canvas_palette_warnings")


def test_visual_inspect_normalizes_string_viewport_presets(tmp_path) -> None:
    """Verify string presets ('desktop'/'mobile'/'1280x720') never crash capture_visual_inspection."""
    from wsa_agent import visual

    assert visual._normalize_viewports(None) == [{"name": "desktop", "width": 1440, "height": 900, "mobile": False}]
    assert visual._normalize_viewports(["desktop"]) == [{"name": "desktop", "width": 1440, "height": 900, "mobile": False}]
    assert visual._normalize_viewports("mobile") == [{"name": "mobile", "width": 390, "height": 844, "mobile": True}]
    custom = visual._normalize_viewports(["1280x720", "tablet", {"name": "wide", "width": 1600, "height": 900}])
    assert custom == [
        {"name": "1280x720", "width": 1280, "height": 720, "mobile": False},
        {"name": "tablet", "width": 768, "height": 1024, "mobile": False},
        {"name": "wide", "width": 1600, "height": 900},
    ]

    (tmp_path / "index.html").write_text("<h1>Hello</h1>", encoding="utf-8")
    tiny_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc`\xa8\x00\x00"
        b"\x00\x04\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    class _FakeCDP:
        def __init__(self, *a, **kw):
            self.console_errors = []
            self.page_exceptions = []
            self.failed_requests = []
            self.seen_vps = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def set_color_scheme(self, _):
            pass

        def set_viewport(self, w, h, mobile=False):
            self.seen_vps.append((w, h, mobile))

        def navigate(self, *_a, **_kw):
            pass

        def drain_events(self, *_a):
            pass

        def evaluate(self, expr):
            if "_AUDIT" in expr or "function" in expr:
                return {"overflow_x": 0, "canvases": [], "viewport": {"width": 1440, "height": 900}, "elements": []}
            return None

        def screenshot_png(self, full_page=False):
            return tiny_png

    fake_cdp = _FakeCDP()
    with patch.object(visual, "find_chromium_binary", return_value="chrome"), \
         patch.object(visual, "_HeadlessCDPClient", return_value=fake_cdp), \
         patch.object(visual, "analyze_screenshot_pixels", return_value={"ok": True, "std_luma": 24.0, "unique_buckets": 12, "blank": False}), \
         patch.object(visual, "compose_contact_sheet", return_value=(tiny_png, "image/png", 1440, 900)):
        out = visual.capture_visual_inspection("index.html", project_root=tmp_path, viewports=["desktop"])
    assert out["ok"] is True, out
    assert fake_cdp.seen_vps == [(1440, 900, False)]


def test_explorer_and_baseline_visual_inspect_do_not_abort_dag_while_verifier_and_post_edit_still_fail(tmp_path) -> None:
    """Verify Explorer/baseline visual defects don't abort recon turns while Verifier and post-edit checks stay strict."""
    from wsa_agent import subagents, visual

    assert "visual_inspect" in subagents.EXPLORER_TOOLS
    assert "owned_files: []" in subagents.ROLE_SYSTEM_PROMPTS["architect"]
    assert "owned_files: []" in subagents.CLAUDE_STAGE_DIRECTIVES["architect"]

    (tmp_path / "index.html").write_text("<h1 style='color:#111'>Broken</h1>", encoding="utf-8")
    tiny_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc`\xa8\x00\x00"
        b"\x00\x04\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    failing_visual_res = {
        "ok": True,
        "tiles": ["DESKTOP 1440x900 [SoM]"],
        "image_bytes": tiny_png,
        "image_mime": "image/png",
        "image_width": 1440,
        "image_height": 900,
        "viewports": [{
            "name": "desktop",
            "width": 1440,
            "height": 900,
            "pixels": {"ok": True, "std_luma": 40.0, "unique_buckets": 30, "blank": False},
            "audit": {"overflow_x": 0, "low_contrast_texts": ['h1 (rendered contrast 1.2:1 on #090a0d "Broken")']},
        }],
        "console_errors": [],
        "page_exceptions": [],
        "failed_requests": [],
    }

    prev_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    try:
        # 1. Explorer running visual_inspect on broken baseline completes cleanly (ok=True)
        st_exp = SessionState(project_root=tmp_path, permission_mode="auto", auto_approve=True)
        exp_client = Mock()
        exp_client.stream.side_effect = [
            {"content": '```tool_call\n{"name": "visual_inspect", "arguments": {"target": "index.html"}}\n```'},
            {"content": "Captured baseline visual defects cleanly."},
        ]
        with patch.object(visual, "capture_visual_inspection", return_value=failing_visual_res):
            exp_out = agent.execute_agent_loop(
                "Inspect baseline UI",
                st_exp,
                exp_client,
                role="explorer",
                enforce_verify_gate=False,
                quiet_ui=True,
                live_ui=False,
            )
        assert exp_out["ok"] is True, exp_out

        # 2. Zero-write baseline diagnostic task under coder (allowed_write_paths=None) also completes ok=True
        st_base = SessionState(project_root=tmp_path, permission_mode="auto", auto_approve=True)
        base_client = Mock()
        base_client.stream.side_effect = [
            {"content": '```tool_call\n{"name": "visual_inspect", "arguments": {"target": "index.html"}}\n```'},
            {"content": "Baseline visual defects recorded."},
        ]
        with patch.object(visual, "capture_visual_inspection", return_value=failing_visual_res):
            base_out = agent.execute_agent_loop(
                "Capture baseline",
                st_base,
                base_client,
                role="coder",
                allowed_write_paths=None,
                enforce_verify_gate=True,
                quiet_ui=True,
                live_ui=False,
            )
        assert base_out["ok"] is True, base_out

        # 3. Verifier seeing failed visual checks still fails strictly
        st_ver = SessionState(project_root=tmp_path, permission_mode="auto", auto_approve=True)
        ver_client = Mock()
        ver_client.stream.side_effect = [
            {"content": '```tool_call\n{"name": "visual_inspect", "arguments": {"target": "index.html"}}\n```'},
            {"content": '{"verdict":"fail","checks":["visual fail"],"failures":[{"issue":"low contrast"}],"summary":"fail"}'},
        ]
        with patch.object(visual, "capture_visual_inspection", return_value=failing_visual_res):
            ver_out = agent.execute_agent_loop(
                "Verify UI",
                st_ver,
                ver_client,
                role="verifier",
                enforce_verify_gate=False,
                quiet_ui=True,
                live_ui=False,
            )
        assert ver_out["ok"] is False
        assert ver_out["aborted_reason"] == "verification_failed"

        # 4. Coder that actually edited index.html and still gets Visual checks: fail must fail verification_failed
        st_post = SessionState(project_root=tmp_path, permission_mode="auto", auto_approve=True)
        post_client = Mock()
        post_client.stream.side_effect = [
            {"content": '```tool_call\n{"name": "write_file", "arguments": {"path": "index.html", "content": "<h1 style=color:#111>Still dark</h1>"}}\n```'},
            {"content": '```tool_call\n{"name": "visual_inspect", "arguments": {"target": "index.html"}}\n```'},
            {"content": "Done editing."},
            {"content": "Done editing."},
            {"content": "Done editing."},
        ]
        with patch.object(visual, "capture_visual_inspection", return_value=failing_visual_res):
            post_out = agent.execute_agent_loop(
                "Fix index.html",
                st_post,
                post_client,
                role="coder",
                allowed_write_paths=["index.html"],
                enforce_verify_gate=False,
                quiet_ui=True,
                live_ui=False,
            )
        assert post_out["ok"] is False
        assert post_out["aborted_reason"] == "verification_failed"
    finally:
        tools.set_project_root(prev_root)


def test_intermediate_dag_slice_defers_downstream_visual_defects_while_enforcing_console_and_final_ui_gates(tmp_path) -> None:
    """Verify intermediate DAG slices defer downstream visual layout defects while failing console errors and enforcing final UI gates."""
    from wsa_agent import subagents, visual

    (tmp_path / "index.html").write_text("<h1>Hello</h1>", encoding="utf-8")
    (tmp_path / "js").mkdir(exist_ok=True)
    (tmp_path / "js" / "shaders.js").write_text("export const v = 1;\n", encoding="utf-8")
    (tmp_path / "styles").mkdir(exist_ok=True)
    (tmp_path / "styles" / "main.css").write_text("body { color: #111; }\n", encoding="utf-8")

    tiny_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc`\xa8\x00\x00"
        b"\x00\x04\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    res_downstream_visual_only = {
        "ok": True,
        "tiles": ["DESKTOP 1440x900 [SoM]"],
        "image_bytes": tiny_png,
        "image_mime": "image/png",
        "image_width": 1440,
        "image_height": 900,
        "viewports": [{
            "name": "desktop",
            "width": 1440,
            "height": 900,
            "pixels": {"ok": True, "std_luma": 40.0, "unique_buckets": 30, "blank": False},
            "audit": {"overflow_x": 0, "low_contrast_texts": ['p (rendered contrast 1.5:1 on #090a0d "Hi")']},
        }],
        "console_errors": [],
        "page_exceptions": [],
        "failed_requests": [],
    }
    res_console_error = {
        **res_downstream_visual_only,
        "console_errors": ["ReferenceError: bad is not defined", "TypeError: foo"],
    }

    prev_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    try:
        # 1. Intermediate DAG slice (defer_visual_gate=True) with Console/JS Errors: 0 completes ok=True
        st_mid = SessionState(project_root=tmp_path, permission_mode="auto", auto_approve=True)
        mid_client = Mock()
        mid_client.stream.side_effect = [
            {"content": '```tool_call\n{"name": "read_file", "arguments": {"path": "js/shaders.js"}}\n```'},
            {"content": '```tool_call\n{"name": "write_file", "arguments": {"path": "js/shaders.js", "content": "export const v = 2;\\n"}}\n```'},
            {"content": '```tool_call\n{"name": "visual_inspect", "arguments": {"target": "index.html"}}\n```'},
            {"content": '```tool_call\n{"name": "run_python", "arguments": {"code": "assert 2 == 2"}}\n```'},
            {"content": "Shaders updated and runtime clean; CSS deferred downstream."},
        ]
        with patch.object(visual, "capture_visual_inspection", return_value=res_downstream_visual_only):
            out_mid = agent.execute_agent_loop(
                "Update shaders",
                st_mid,
                mid_client,
                role="coder",
                allowed_write_paths=["js/shaders.js"],
                enforce_verify_gate=True,
                defer_visual_gate=True,
                quiet_ui=True,
                live_ui=False,
            )
        assert out_mid["ok"] is True, out_mid

        # 2. Intermediate DAG slice with Console/JS Errors: 2 STILL fails strictly
        st_err = SessionState(project_root=tmp_path, permission_mode="auto", auto_approve=True)
        err_client = Mock()
        err_client.stream.side_effect = [
            {"content": '```tool_call\n{"name": "read_file", "arguments": {"path": "js/shaders.js"}}\n```'},
            {"content": '```tool_call\n{"name": "write_file", "arguments": {"path": "js/shaders.js", "content": "export const v = 3;\\n"}}\n```'},
            {"content": '```tool_call\n{"name": "visual_inspect", "arguments": {"target": "index.html"}}\n```'},
            {"content": "Done."},
            {"content": "Done."},
            {"content": "Done."},
        ]
        with patch.object(visual, "capture_visual_inspection", return_value=res_console_error):
            out_err = agent.execute_agent_loop(
                "Update shaders broken",
                st_err,
                err_client,
                role="coder",
                allowed_write_paths=["js/shaders.js"],
                enforce_verify_gate=False,
                defer_visual_gate=True,
                quiet_ui=True,
                live_ui=False,
            )
        assert out_err["ok"] is False
        assert out_err["aborted_reason"] == "verification_failed"

        # 3. Final UI slice (defer_visual_gate=False) STILL fails strictly on visual contrast defects
        st_fin = SessionState(project_root=tmp_path, permission_mode="auto", auto_approve=True)
        fin_client = Mock()
        fin_client.stream.side_effect = [
            {"content": '```tool_call\n{"name": "read_file", "arguments": {"path": "styles/main.css"}}\n```'},
            {"content": '```tool_call\n{"name": "write_file", "arguments": {"path": "styles/main.css", "content": "body { color: #111; }\\n"}}\n```'},
            {"content": '```tool_call\n{"name": "visual_inspect", "arguments": {"target": "index.html"}}\n```'},
            {"content": "Done."},
            {"content": "Done."},
            {"content": "Done."},
        ]
        with patch.object(visual, "capture_visual_inspection", return_value=res_downstream_visual_only):
            out_fin = agent.execute_agent_loop(
                "Polish UI",
                st_fin,
                fin_client,
                role="coder",
                allowed_write_paths=["styles/main.css"],
                enforce_verify_gate=False,
                defer_visual_gate=False,
                quiet_ui=True,
                live_ui=False,
            )
        assert out_fin["ok"] is False
        assert out_fin["aborted_reason"] == "verification_failed"

        # 4. Orchestrator sets defer_visual_gate=True on T1 (js/shaders.js) and omits/False on final T2 (ui=True)
        st_orch = SessionState(project_root=tmp_path, permission_mode="auto", auto_approve=True)
        plan = [
            {"id": "t1", "title": "Fix shaders", "owned_files": ["js/shaders.js"], "depends_on": [], "acceptance": "ok", "ui": False},
            {"id": "t2", "title": "Polish UI", "owned_files": ["styles/main.css"], "depends_on": ["t1"], "acceptance": "ok", "ui": True},
        ]
        coder_calls = []

        def fake_worker(**kwargs):
            r = kwargs.get("role")
            if r == "coder":
                coder_calls.append((kwargs.get("label"), bool(kwargs.get("defer_visual_gate", False))))
                return {"ok": True, "written_files": list(kwargs.get("owned_files") or ())}
            if r == "architect":
                return {"ok": True, "final_text": json.dumps(plan)}
            if r == "verifier":
                return {"ok": True, "final_text": '{"verdict":"pass","checks":["ok"],"failures":[]}', "verification_evidence": [{"ok": True}]}
            return {"ok": True, "final_text": "ok"}

        with patch.object(subagents, "_run_role_agent", side_effect=fake_worker), \
             patch.object(subagents, "_discover_live_relays", return_value=[]), \
             patch.object(st_orch, "auto_checkpoint"):
            orch_out = subagents.run_orchestrated_turn("Upgrade 3D shaders and CSS", st_orch, Mock())
        assert orch_out["ok"] is True, orch_out
        assert coder_calls == [("coder:t1", True), ("coder:t2", False)]
    finally:
        tools.set_project_root(prev_root)


def test_disable_webgl_suppresses_expected_context_creation_errors_while_preserving_real_js_errors(tmp_path) -> None:
    """Verify disable_webgl=True suppresses expected WebGL creation console logs while keeping real JS errors strict."""
    from wsa_agent import visual

    (tmp_path / "index.html").write_text("<h1>Fallback</h1>", encoding="utf-8")
    tiny_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc`\xa8\x00\x00"
        b"\x00\x04\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    class _FallbackCDP:
        def __init__(self, errs):
            self.console_errors = list(errs)
            self.page_exceptions = []
            self.failed_requests = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def set_color_scheme(self, _):
            pass

        def set_viewport(self, w, h, mobile=False):
            pass

        def navigate(self, *_a, **_kw):
            pass

        def drain_events(self, *_a):
            pass

        def evaluate(self, expr):
            if "_AUDIT" in expr or "function" in expr:
                return {"overflow_x": 0, "canvases": [], "viewport": {"width": 1440, "height": 900}, "elements": []}
            return None

        def screenshot_png(self, full_page=False):
            return tiny_png

    webgl_only = ["THREE.WebGLRenderer: A WebGL context could not be created. Reason: disabled by command line"]
    mixed_errs = [*webgl_only, "TypeError: Cannot read properties of null (reading 'addEventListener')"]

    with patch.object(visual, "find_chromium_binary", return_value="chrome"), \
         patch.object(visual, "analyze_screenshot_pixels", return_value={"ok": True, "std_luma": 22.0, "unique_buckets": 10, "blank": False}), \
         patch.object(visual, "compose_contact_sheet", return_value=(tiny_png, "image/png", 1440, 900)):
        with patch.object(visual, "_HeadlessCDPClient", return_value=_FallbackCDP(webgl_only)):
            res_ok = visual.capture_visual_inspection("index.html", project_root=tmp_path, disable_webgl=True)
        assert res_ok["ok"] is True
        assert res_ok["console_errors"] == []
        assert len(res_ok["suppressed_webgl_notices"]) == 1
        rep_ok = visual.format_visual_report(res_ok)
        assert "WebGL-Disabled Fallback Notices: 1" in rep_ok
        assert rep_ok.rstrip().endswith("\nVisual checks: pass")

        with patch.object(visual, "_HeadlessCDPClient", return_value=_FallbackCDP(mixed_errs)):
            res_err = visual.capture_visual_inspection("index.html", project_root=tmp_path, disable_webgl=True)
        assert len(res_err["console_errors"]) == 1
        assert "TypeError" in res_err["console_errors"][0]
        assert len(res_err["suppressed_webgl_notices"]) == 1
        rep_err = visual.format_visual_report(res_err)
        assert "Console/JS Errors (1):" in rep_err
        assert rep_err.rstrip().endswith("\nVisual checks: fail")


def test_fake_tool_request_prose_triggers_continue_nudge_and_zero_write_existing_file_slice_retries_and_fails_closed(tmp_path) -> None:
    """Verify fake in-flight tool prose nudges continue and mutating slices on existing files cannot pass with 0 writes."""
    from wsa_agent import subagents

    t6_prose = (
        "The baseline capture and contract reads are in flight. I’ll use the returned output to preserve selector semantics.\n"
        "[Tool execution requested] The next step is to apply the CSS changes after the baseline and contract-read results return. "
        "No files have been modified yet."
    )
    t7_prose = (
        "The T7 inspection and baseline verification have been requested through WSA. "
        "I’ll apply changes only after the exact CSP/importmap and form-hook contracts are available."
    )
    for sample in (t6_prose, t7_prose):
        nudge = agent._maybe_anti_laziness_nudge(
            content=sample,
            written_this_turn=set(),
            verified_after_last_edit=True,
            tests_exist=False,
            round_num=0,
            verify_nudges_used=0,
            continue_nudges_used=0,
            free_mode=False,
            tool_calls_count=2,
        )
        assert nudge is not None and nudge[0] == "continue"
        assert "Missing Tool Blocks" in nudge[1]

    # Existing CSS file already present on disk; fake-prose 0-write coder triggers fresh-chat retry and fails closed
    (tmp_path / "styles").mkdir()
    (tmp_path / "styles" / "main.css").write_text("body { margin: 0; }\n", encoding="utf-8")

    st = SessionState(project_root=tmp_path, permission_mode="auto", auto_approve=True)
    plan = [
        {"id": "t6", "title": "Upgrade CSS", "owned_files": ["styles/main.css"], "depends_on": [], "acceptance": "css updated", "ui": True},
    ]
    coder_attempts = []

    def fake_worker(**kwargs):
        r = kwargs.get("role")
        if r == "architect":
            return {"ok": True, "final_text": json.dumps(plan)}
        if r == "coder":
            coder_attempts.append((kwargs.get("new_chat_override"), "no edits were written yet" in kwargs.get("prompt", "").lower()))
            return {"ok": True, "final_text": t6_prose, "written_files": [], "tool_calls_count": 2}
        return {"ok": True, "final_text": "recon"}

    with patch.object(subagents, "_run_role_agent", side_effect=fake_worker), \
         patch.object(subagents, "_discover_live_relays", return_value=[]), \
         patch.object(subagents, "_clear_poisoned_thread_for_fresh_chat"), \
         patch.object(st, "auto_checkpoint"):
        out = subagents.run_orchestrated_turn("Upgrade existing stylesheet", st, Mock())

    assert len(coder_attempts) == 2
    assert coder_attempts[1] == (True, True)
    assert out["ok"] is False
    assert any("no_edits_emitted" in str(f.get("issue", "")) for f in out["verdict"]["failures"])











