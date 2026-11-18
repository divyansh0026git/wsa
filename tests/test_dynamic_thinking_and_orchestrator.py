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
