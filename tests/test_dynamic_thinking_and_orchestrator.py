"""Tests for dynamic deep-thinking relay timeouts and deep agentic orchestrator workflows."""
from __future__ import annotations

import json
import threading
import time
from unittest.mock import Mock, patch

import pytest

from wsa_agent import subagents
from wsa_agent.session import SessionState
from wsa_server import relay


@pytest.fixture
def isolated_relay():
    with relay._dom_relay_lock:
        orig_relays = dict(relay._dom_relay["relays"])
        orig_pending = dict(relay._dom_relay["pending"])
        orig_recent = dict(relay._dom_relay.get("recent_requests", {}))
        orig_cancelled = dict(relay._dom_relay.get("cancelled_req_ids", {}))
        orig_active = relay._dom_relay.get("active_relay_id")
        relay._dom_relay["relays"].clear()
        relay._dom_relay["pending"].clear()
        relay._dom_relay.setdefault("recent_requests", {}).clear()
        relay._dom_relay.setdefault("cancelled_req_ids", {}).clear()
        relay._dom_relay["active_relay_id"] = "brave-deep"
        relay._dom_relay["relays"]["brave-deep"] = {
            "id": "brave-deep",
            "label": "brave",
            "browser": "brave",
            "last_seen": time.time(),
            "active_provider": "claude",
            "providers": {"claude": {"has_tab": True}},
        }
    try:
        yield
    finally:
        with relay._dom_relay_lock:
            relay._dom_relay["relays"].clear()
            relay._dom_relay["relays"].update(orig_relays)
            relay._dom_relay["pending"].clear()
            relay._dom_relay["pending"].update(orig_pending)
            relay._dom_relay.setdefault("recent_requests", {}).clear()
            relay._dom_relay["recent_requests"].update(orig_recent)
            relay._dom_relay.setdefault("cancelled_req_ids", {}).clear()
            relay._dom_relay["cancelled_req_ids"].update(orig_cancelled)
            relay._dom_relay["active_relay_id"] = orig_active


def test_relay_does_not_timeout_during_long_deep_thinking_with_generating_heartbeats(isolated_relay):
    """A turn that stays actively generating past `timeout` (and past 3x timeout) must NOT be aborted."""
    client_info = {"client_id": "brave-deep", "browser": "brave", "has_claude": 1}

    def _worker():
        rid = None
        for _ in range(100):
            item = relay.dom_relay_get_pending(client_id="brave-deep")
            if item and item.get("id"):
                rid = item["id"]
                break
            time.sleep(0.01)
        assert rid
        # Keep heartbeating generating=True for ~1.1s while timeout=0.35s (more than 3x timeout!)
        for _ in range(11):
            time.sleep(0.10)
            relay.dom_relay_poll_for_client(client_info, busy=True, busy_req_id=rid, generating=True)
        relay.dom_relay_post_response(rid, response="DEEP_THINK_COMPLETE", model="Claude Sonnet 5.5 High")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    events = list(relay.dom_relay_iter(
        "Solve complex architecture",
        timeout=0.35,
        pickup_timeout=2.0,
        step=0.05,
        provider="claude",
        client_request_id="deep-1",
    ))
    t.join(timeout=3)
    res = next(p for k, p in events if k == "result")
    assert res["response"] == "DEEP_THINK_COMPLETE"


def test_relay_older_extension_busy_heartbeat_after_thinking_keeps_turn_alive(isolated_relay):
    """Even before reloading the extension (busy=1 without generating=1), live thinking turns stay alive."""
    client_info = {"client_id": "brave-deep", "browser": "brave", "has_claude": 1}

    def _worker():
        rid = None
        for _ in range(100):
            item = relay.dom_relay_get_pending(client_id="brave-deep")
            if item and item.get("id"):
                rid = item["id"]
                break
            time.sleep(0.01)
        assert rid
        # Initial collapsed thinking pill like "Sleuthing", then silent deep thinking with busy heartbeats
        relay.dom_relay_post_progress(rid, thinking="Sleuthing", seq=1, client_id="brave-deep")
        for _ in range(8):
            time.sleep(0.08)
            relay.dom_relay_poll_for_client(client_info, busy=True, busy_req_id=rid, generating=False)
        relay.dom_relay_post_response(rid, response="OLDER_EXT_SURVIVED", model="Claude Sonnet 5.5 High")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    events = list(relay.dom_relay_iter(
        "Deep refactor",
        timeout=0.30,
        pickup_timeout=2.0,
        step=0.05,
        provider="claude",
        client_request_id="deep-old-ext",
    ))
    t.join(timeout=3)
    res = next(p for k, p in events if k == "result")
    assert res["response"] == "OLDER_EXT_SURVIVED"


def test_relay_genuine_silence_still_times_out_cleanly(isolated_relay):
    """If a dispatched request sends zero progress and zero heartbeats for `timeout`, it still times out."""
    def _worker():
        for _ in range(100):
            item = relay.dom_relay_get_pending(client_id="brave-deep")
            if item and item.get("id"):
                return
            time.sleep(0.01)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    with pytest.raises(RuntimeError, match=r"DOM relay timeout"):
        list(relay.dom_relay_iter(
            "Stalled turn",
            timeout=0.30,
            pickup_timeout=2.0,
            step=0.05,
            provider="gemini",
            client_request_id="stall-1",
        ))
    t.join(timeout=2)


def test_claude_send_stall_does_not_false_trigger_when_submitted_or_generating(isolated_relay):
    """Submitted/generating Claude turns must not false-trigger the 70s composer send-stall guard."""
    client_info = {"client_id": "brave-deep", "browser": "brave", "has_claude": 1}

    def _worker():
        rid = None
        for _ in range(100):
            item = relay.dom_relay_get_pending(client_id="brave-deep")
            if item and item.get("id"):
                rid = item["id"]
                break
            time.sleep(0.01)
        assert rid
        # Composer submitted & actively thinking, but no text tokens emitted yet past relay_send_timeout_sec
        relay.dom_relay_post_progress(rid, seq=0, stage="submitted", client_id="brave-deep")
        for _ in range(5):
            time.sleep(0.06)
            relay.dom_relay_poll_for_client(client_info, busy=True, busy_req_id=rid, generating=True)
        relay.dom_relay_post_response(rid, response="SUBMITTED_OK", model="Claude Sonnet 5.5 High")

    with patch.dict(relay.CONFIG, {"relay_send_timeout_sec": 0.12}):
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        events = list(relay.dom_relay_iter(
            "Deep thinking prompt",
            timeout=1.5,
            pickup_timeout=2.0,
            step=0.03,
            provider="claude",
            client_request_id="claude-sub-ok",
        ))
        t.join(timeout=2)
    res = next(p for k, p in events if k == "result")
    assert res["response"] == "SUBMITTED_OK"


def test_orchestrator_prompts_encourage_deep_agentic_work_without_fast_mode_rushing(tmp_path):
    """Orchestrator prompts must avoid fast/light/minimal rushing and give long runs ample round budgets."""
    for role, prompt in list(subagents.ROLE_SYSTEM_PROMPTS.items()) + list(subagents.CLAUDE_STAGE_DIRECTIVES.items()):
        low = prompt.lower()
        assert "fast mode" not in low
        assert "fast targeted" not in low
        assert "light targeted" not in low

    state = SessionState(project_root=tmp_path)
    long_spec = "Build a rich multi-file interactive 3D product showcase. " + ("detail " * 300) + "TAIL_REQUIREMENT_MARKER"
    plan = [{"id": "t1", "title": "Implement core module", "owned_files": ["app.js"], "depends_on": []}]
    seen_calls = []

    def fake_role(**kwargs):
        seen_calls.append(kwargs)
        role = kwargs["role"]
        if role == "architect":
            return {"ok": True, "final_text": json.dumps(plan)}
        if role == "verifier":
            return {
                "ok": True,
                "final_text": '{"verdict":"pass","checks":["verified"],"failures":[],"summary":"ok"}',
                "verification_evidence": [{"ok": True}],
            }
        return {"ok": True, "final_text": "completed"}

    with patch.object(subagents, "_run_role_agent", side_effect=fake_role), \
         patch.object(subagents, "_discover_live_relays", return_value=[]), \
         patch.object(state, "auto_checkpoint"):
        res = subagents.run_orchestrated_turn(long_spec, state, Mock())

    assert res["ok"] is True
    by_role = {c["role"]: c for c in seen_calls}
    assert "fast targeted" not in by_role["explorer"]["prompt"].lower()
    assert "minimal dependency-ordered" not in by_role["architect"]["prompt"].lower()
    assert "TAIL_REQUIREMENT_MARKER" in by_role["coder"]["prompt"]
    assert "TAIL_REQUIREMENT_MARKER" in by_role["verifier"]["prompt"]
    assert by_role["explorer"]["max_rounds"] >= 20
    assert by_role["architect"]["max_rounds"] >= 14
    assert by_role["coder"]["max_rounds"] >= 40
    assert by_role["verifier"]["max_rounds"] >= 20


def test_continuation_after_interruption_skips_workflow_router(tmp_path):
    """Natural continuation/resume replies after an interruption must not waste a router turn."""
    from wsa_agent import agent

    state = SessionState(project_root=tmp_path, selected_model="claude")
    state.active_chat_url = "https://claude.ai/chat/existing-thread"
    state.chat_urls["claude"] = "https://claude.ai/chat/existing-thread"

    prompts = [
        "please continue, sorry i interrupted, continue from where you left, recall all the thinking steps again",
        "please continue from where you left off",
        "sorry i interrupted, keep going",
        "continue please",
        "go ahead and continue",
        "continue with the remaining tasks",
    ]
    for prompt in prompts:
        with patch.object(agent, "_judge_workflow") as judge, \
             patch.object(agent, "execute_agent_loop", return_value={"ok": True}) as execute:
            res = agent.run_turn(prompt, state, Mock())
        assert res["ok"] is True
        judge.assert_not_called()
        execute.assert_called_once()


def test_dynamic_thinking_liveness_applies_to_all_five_web_providers():
    """All 5 web providers (claude, gemini, chatgpt, deepseek, venice) must stay alive during silent generating heartbeats."""
    for provider in ("claude", "gemini", "chatgpt", "deepseek", "venice"):
        with patch.dict(relay._dom_relay, {
            "pending": {},
            "relays": {},
            "active_relay_id": "",
            "recent_requests": {},
            "cancelled_req_ids": {},
        }):
            cid = f"chrome-{provider}-e2e"
            cinfo = {
                "client_id": cid,
                "browser": "chrome",
                "label": f"{provider}-tab",
                "provider": provider,
                "providers": {provider: {"logged_in": True, "tab_open": True}},
            }
            relay.dom_relay_poll_for_client(cinfo)

            def _worker(cinfo=cinfo, cid=cid, provider=provider):
                pend = None
                for _ in range(80):
                    pend, _ = relay.dom_relay_poll_for_client(cinfo)
                    if pend:
                        break
                    time.sleep(0.01)
                assert pend is not None
                rid = pend["id"]
                for _ in range(10):
                    time.sleep(0.05)
                    relay.dom_relay_poll_for_client(cinfo, busy=True, busy_req_id=rid, generating=True)
                relay.dom_relay_post_response(rid, response=f"{provider.upper()}_DEEP_OK", model=provider)

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            events = list(relay.dom_relay_iter(
                "deep think",
                timeout=0.22,
                pickup_timeout=2.0,
                step=0.025,
                provider=provider,
                client_request_id=f"deep-{provider}",
            ))
            t.join(timeout=3)
        res = next(p for k, p in events if k == "result")
        assert res["response"] == f"{provider.upper()}_DEEP_OK"


def test_workflow_router_frames_prompt_cleanly_and_preserves_system_directive_on_pinned_chat(tmp_path):
    """Workflow judgment must never send a naked JSON blob or lose its system directive on a reused chat_url."""
    from wsa_agent import agent
    from wsa_server import multimodal as mm

    state = SessionState(project_root=tmp_path, selected_model="claude")
    state.active_chat_url = "https://claude.ai/chat/live-coding-thread"
    state.chat_urls["claude"] = "https://claude.ai/chat/live-coding-thread"

    captured = {}

    class CaptureClient:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return {"content": '{"mode": "direct", "reason": "small follow-up"}'}

    dec = agent._judge_workflow("update the footer copy", state, CaptureClient(), {"mode": "direct"})
    assert dec["mode"] == "direct"
    user_msg = captured["messages"][1]["content"]
    assert not user_msg.lstrip().startswith('{"request":')
    assert '"prior_context": ""' not in user_msg
    assert "Workflow routing check" in user_msg
    assert "update the footer copy" in user_msg

    # Verify relay preserves WORKFLOW_JUDGMENT_PROMPT even on a freshly seeded pinned chat_url
    sid = f"{state.session_id}:role:workflow_router"
    with patch.dict(relay._dom_relay, {"sessions": {}, "relays": {}, "active_relay_id": ""}):
        assert relay.seed_session_hint_url(sid, state.active_chat_url, provider="claude") is True
        full_p, _ = mm.messages_to_prompt(captured["messages"], None, provider="claude")
        prompt_to_send, _, _, is_new, hint_url = relay.prepare_session_relay_prompt(
            sid, captured["messages"], None, full_p, [], provider="claude"
        )
    assert is_new is False
    assert hint_url == state.active_chat_url
    assert "WORKFLOW JUDGMENT" in prompt_to_send
    assert "update the footer copy" in prompt_to_send


def test_soft_percentage_usage_warning_is_not_treated_as_limit_reached():
    """Soft percentage badges like 'Over 90% of your 5-hour limit' must not trigger ProviderLimitError."""
    import subprocess
    from pathlib import Path

    assert relay.is_provider_limit_error("✦ Over 90% of your 5-hour limit") is False
    assert relay.is_provider_limit_error("[LIMIT_REACHED] Claude: ✦ Over 90% of your 5-hour limi") is False
    assert relay.is_provider_limit_error("[SAFEGUARD_PAUSED] Claude: safeguards flagged this message") is False
    assert relay.is_provider_limit_error("5-hour limit reached · Your limit will reset at 8:00 PM.") is True
    assert relay.is_provider_limit_error("[LIMIT_REACHED] Claude: 5-hour limit") is True

    bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
    js = f"""
const assert = require("assert");
const bg = require({json.dumps(bg_path)});
globalThis.document = {{ querySelectorAll: () => [], querySelector: () => null }};
assert.strictEqual(bg._installWsaDomUtilsFn(), true);
const u = globalThis.__wsaDomUtils;
assert.strictEqual(u.detectLimitMessage("✦ Over 90% of your 5-hour limit", true), "");
assert.strictEqual(u.detectLimitMessage("80% of your weekly limit", true), "");
assert.ok(u.detectLimitMessage("5-hour limit reached · Your limit will reset at 8:00 PM.", true));
assert.ok(u.detectLimitMessage("You've reached your 5-hour limit until 9:00 PM.", true));
"""
    proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_claude_safeguard_paused_card_detected_without_false_limit():
    """Claude's Paused/safeguards card must return [SAFEGUARD_PAUSED] immediately, not [LIMIT_REACHED]."""
    import subprocess
    from pathlib import Path

    bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
    js = f"""
const assert = require("assert");
const bg = require({json.dumps(bg_path)});

class MockElement {{
  constructor(tag, attrs, text) {{
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.innerText = text;
    this.textContent = text;
    this.className = attrs.className || "";
    this._attrs = attrs;
  }}
  getAttribute(k) {{ return this._attrs[k] || null; }}
  hasAttribute(k) {{ return Object.prototype.hasOwnProperty.call(this._attrs, k); }}
  getBoundingClientRect() {{ return {{ width: 320, height: 60 }}; }}
  focus() {{}}
  click() {{}}
  dispatchEvent() {{ return true; }}
  querySelectorAll() {{ return []; }}
  querySelector() {{ return null; }}
  closest() {{ return null; }}
}}
globalThis.Element = MockElement;
const makeEl = (tag, attrs, text) => new MockElement(tag, attrs, text);

const editor = makeEl("div", {{ contenteditable: "true", className: "ProseMirror" }}, "");
const sendBtn = makeEl("button", {{ "aria-label": "Send message" }}, "Send");
const softBadge = makeEl("div", {{ role: "status" }}, "✦ Over 90% of your 5-hour limit");
const elements = [editor, sendBtn, softBadge];

globalThis.window = {{
  getComputedStyle: () => ({{ display: "block", visibility: "visible", opacity: "1" }}),
  getSelection: () => ({{ removeAllRanges() {{}}, addRange() {{}} }}),
}};
globalThis.location = {{ href: "https://claude.ai/chat/test-sg" }};
globalThis.document = {{
  querySelectorAll(sel) {{
    const s = String(sel || "");
    if (s.includes("safeguard") || s.includes("main section")) {{
      return elements.filter(e => e.getAttribute("role") === "status" || e.getAttribute("role") === "alert" || e.getAttribute("data-testid") === "safeguard-card");
    }}
    if (s.includes("role='alert'") || s.includes("role='status'")) {{
      return elements.filter(e => e.getAttribute("role") === "status" || e.getAttribute("role") === "alert");
    }}
    if (s.includes("Stop") || s.includes("stop")) return [];
    if (s.includes("Send") || s.includes("send")) return [sendBtn];
    if (s.includes("ProseMirror") || s.includes("contenteditable")) return [editor];
    return [];
  }},
  querySelector(sel) {{
    const list = this.querySelectorAll(sel);
    return list.length ? list[0] : null;
  }},
  execCommand() {{ return true; }},
  createRange() {{ return {{ selectNodeContents() {{}}, collapse() {{}} }}; }},
}};

assert.strictEqual(bg._installWsaDomUtilsFn(), true);
assert.strictEqual(bg._claudeInstallPageRuntimeFn(), true);

(async () => {{
  const startRes = await bg._claudeStartTurnFn("req-sg-1", "hello");
  assert.strictEqual(startRes.ok, true, JSON.stringify(startRes));
  // Add Claude safeguard card after submit
  elements.push(makeEl("div", {{ "data-testid": "safeguard-card" }}, "Paused Sonnet 5.5's safeguards flagged this message. Details: [reasoning_extraction]"));
  const pollRes = bg._claudePollTurnFn("req-sg-1");
  assert.ok(String(pollRes.error || "").startsWith("[SAFEGUARD_PAUSED] Claude:"), "Unexpected error: " + pollRes.error);
  assert.ok(String(pollRes.error || "").includes("reasoning_extraction"), pollRes.error);

  // Starting a fresh turn when an old safeguard card is still visible must NOT immediately false-trigger
  const startRes2 = await bg._claudeStartTurnFn("req-sg-2", "fresh prompt");
  assert.strictEqual(startRes2.ok, true);
  const pollRes2 = bg._claudePollTurnFn("req-sg-2");
  assert.ok(!pollRes2.error, "Old safeguard card falsely triggered new turn: " + pollRes2.error);
}})().catch(err => {{ console.error(err); process.exit(1); }});
"""
    proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_judge_workflow_falls_back_cleanly_on_safeguard_pause(tmp_path):
    """A [SAFEGUARD_PAUSED] error during workflow judgment must fall back locally without limit recovery."""
    from wsa_agent import agent, client as client_mod

    # Client dict extractor must never classify [SAFEGUARD_PAUSED] as a provider limit
    assert client_mod._extract_limit_error_from_dict({
        "error": {"message": "[SAFEGUARD_PAUSED] Claude: safeguards flagged this message", "provider": "claude"}
    }) is None

    state = SessionState(project_root=tmp_path, selected_model="claude")
    fallback = {"mode": "direct", "reasons": ["local"]}
    failing_client = Mock()
    failing_client.stream.side_effect = ConnectionError("API error: [SAFEGUARD_PAUSED] Claude: safeguards flagged this message")
    dec = agent._judge_workflow("some ambiguous request", state, failing_client, fallback)
    assert dec is fallback


def test_parse_workflow_decision_robustly_handles_all_web_agent_formats():
    """_parse_workflow_decision must handle inline backticks, smart quotes, fenced blocks, trailing periods, <think> tags, and em-dashes."""
    from wsa_agent.agent import _parse_workflow_decision

    # 1. Exact Claude response from user screenshot (clean JSON with em-dash)
    claude_ss = (
        '{"mode":"orchestrate","reason":"Integrating a new third-party repo (colibri) into existing codebase '
        'needs recon of its structure/deps, ordered setup (clone, install, config, model/GPU wiring), '
        'and verification \u2014 not a trivial isolated edit","skills":["context7-docs","karpathy-skills",'
        '"verification-before-completion"],"security_pass":false}'
    )
    res = _parse_workflow_decision(claude_ss)
    assert res is not None
    assert res["mode"] == "orchestrate"
    assert "verification-before-completion" in res["skills"]
    assert res["security_pass"] is False

    # 2. Claude inline code formatting (`{...}`)
    inline_code = f"`{claude_ss}`"
    res = _parse_workflow_decision(inline_code)
    assert res is not None
    assert res["mode"] == "orchestrate"

    # 3. Triple backticks with json language tag
    fenced = f"```json\n{claude_ss}\n```"
    res = _parse_workflow_decision(fenced)
    assert res is not None
    assert res["mode"] == "orchestrate"

    # 4. Leading and trailing prose around code block
    wrapped_prose = f"Choosing the scope and skills for the repo setup:\n\n```json\n{claude_ss}\n```\nHope this helps!"
    res = _parse_workflow_decision(wrapped_prose)
    assert res is not None
    assert res["mode"] == "orchestrate"

    # 5. Trailing period (common model error when following prompt ending with dot)
    with_period = f"{claude_ss}."
    res = _parse_workflow_decision(with_period)
    assert res is not None
    assert res["mode"] == "orchestrate"

    # 6. Smart / typographic quotes
    smart_quotes = '{\u201cmode\u201d:\u201corchestrate\u201d,\u201creason\u201d:\u201cIntegrate repo\u201d,\u201cskills\u201d:[\u201ccontext7-docs\u201d]}'
    res = _parse_workflow_decision(smart_quotes)
    assert res is not None
    assert res["mode"] == "orchestrate"

    # 7. Trailing commas before closing braces
    trailing_comma = '{"mode": "orchestrate", "reason": "Setup model", "skills": ["context7-docs",], "security_pass": false,}'
    res = _parse_workflow_decision(trailing_comma)
    assert res is not None
    assert res["mode"] == "orchestrate"

    # 8. <think> tag prefix (DeepSeek R1 / Venice)
    think_prefix = f"<think>We need to integrate colibri and wire GPU compute.</think>\n{claude_ss}"
    res = _parse_workflow_decision(think_prefix)
    assert res is not None
    assert res["mode"] == "orchestrate"

    # 9. Prose containing embedded JSON
    embedded_prose = f"Based on our routing check: {claude_ss} Let's proceed."
    res = _parse_workflow_decision(embedded_prose)
    assert res is not None
    assert res["mode"] == "orchestrate"

    # 10. Malformed JSON with regex recovery
    broken_json = '{"mode": "orchestrate", "reason": "Integrating colibri repo into codebase", "skills": ["context7-docs"]'
    res = _parse_workflow_decision(broken_json)
    assert res is not None
    assert res["mode"] == "orchestrate"
    assert res["skills"] == ["context7-docs"]


def test_judge_workflow_extracts_orchestrator_decision_from_claude_response(tmp_path):
    """_judge_workflow must successfully parse Claude's response when wrapped in inline backticks or thinking."""
    from wsa_agent import agent

    claude_payload = (
        '`{"mode":"orchestrate","reason":"Integrating a new third-party repo (colibri) into existing codebase '
        'needs recon of its structure/deps, ordered setup (clone, install, config, model/GPU wiring), '
        'and verification \u2014 not a trivial isolated edit","skills":["context7-docs","karpathy-skills",'
        '"verification-before-completion"],"security_pass":false}`'
    )
    state = SessionState(project_root=tmp_path, selected_model="claude")
    fallback = {"mode": "direct", "reasons": ["fallback"]}
    mock_client = Mock()
    mock_client.stream.return_value = {
        "content": claude_payload,
        "thinking": "Choosing the scope and skills for the repo setup",
        "upstream_model": {"chat_url": "https://claude.ai/chat/79a6d6ec-bca1-4a84-9633-dd0628118e5d"},
    }

    decision = agent._judge_workflow("setup colibri in my codebase", state, mock_client, fallback)
    assert decision["mode"] == "orchestrate"
    assert "context7-docs" in decision["skills"]
    assert "karpathy-skills" in decision["skills"]
    assert "verification-before-completion" in decision["skills"]
    assert decision["security_pass"] is False
    assert state.chat_urls.get("claude") == "https://claude.ai/chat/79a6d6ec-bca1-4a84-9633-dd0628118e5d"


def test_classify_task_complexity_identifies_repo_setup_and_model_integration(tmp_path):
    """classify_task_complexity must detect repo setup and model integration as multi-step orchestration."""
    from wsa_agent.context import classify_task_complexity

    prompt = (
        "setup colibri https://github.com/JustVugg/colibri.git in my codebase and also i want to ask you "
        "do i need to download the whole model in my own laptop and do i able to use my gpu compute power fr better running?"
    )
    state = SessionState(project_root=tmp_path, selected_model="claude")
    res = classify_task_complexity(prompt, state)
    assert res["mode"] == "orchestrate", f"Expected orchestrate, got {res}"
    assert res["score"] >= 4

