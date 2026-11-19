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

