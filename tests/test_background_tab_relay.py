"""Background-tab lifecycle regressions, without a live provider account."""
import json
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from wsa_server import relay


@pytest.fixture(params=["chatgpt", "claude", "gemini", "deepseek", "venice"])
def active_request(request):
    with relay._dom_relay_lock:
        original = dict(relay._dom_relay)
        relay._dom_relay.update(relays={}, pending={}, cancelled_req_ids={}, recent_requests={}, completed_responses={},
                                sessions={}, browser_cmds={}, active_relay_id=None)
    client = {"client_id": "background-test", "browser": "brave", f"has_{request.param}": 1}
    relay.register_or_touch_relay(client)
    item = {"event": threading.Event(), "wake_event": threading.Event(), "dispatched": True,
            "dispatched_to": "background-test", "dispatched_at": 100.0,
            "response": None, "error": None, "provider": request.param}
    relay._dom_relay["pending"]["job"] = item
    try:
        yield client, item
    finally:
        with relay._dom_relay_lock:
            relay._dom_relay.clear()
            relay._dom_relay.update(original)


def test_overlapping_idle_polls_do_not_cancel_active_request(active_request):
    client, item = active_request
    with patch.object(relay.time, "time", return_value=101.0):
        for _ in range(20):
            relay.dom_relay_poll_for_client(client)
    assert item["error"] is None
    assert not item["event"].is_set()


def test_busy_heartbeat_resets_idle_suspicion(active_request):
    client, item = active_request
    with patch.object(relay.time, "time", return_value=101.0):
        relay.dom_relay_poll_for_client(client)
    with patch.object(relay.time, "time", return_value=180.0):
        relay.dom_relay_poll_for_client(client, busy=True, busy_req_id="job")
    with patch.object(relay.time, "time", return_value=181.0):
        relay.dom_relay_poll_for_client(client)
    assert item["error"] is None
    assert item["idle_polls_since_dispatch"] == 1


def test_truly_orphaned_request_still_fails_after_grace(active_request):
    client, item = active_request
    with patch.object(relay.time, "time", return_value=101.0):
        relay.dom_relay_poll_for_client(client)
    with patch.object(relay.time, "time", return_value=250.0):
        relay.dom_relay_poll_for_client(client)
    assert "returned idle" in item["error"]
    assert item["event"].is_set()


def test_idle_overlap_cannot_dispatch_a_second_job(active_request):
    client, item = active_request
    queued = dict(response=None, error=None, dispatched=False, prompt="next",
                  provider=item["provider"])
    relay._dom_relay["pending"]["next"] = queued
    with patch.object(relay.time, "time", return_value=101.0):
        for _ in range(20):
            pending, _ = relay.dom_relay_poll_for_client(client)
            assert pending is None
    assert not queued["dispatched"]
    relay.dom_relay_post_response("job", response="done", client_id=client["client_id"])
    pending, _ = relay.dom_relay_poll_for_client(client)
    assert pending["id"] == "next"


def test_progress_resets_idle_suspicion(active_request):
    client, item = active_request
    with patch.object(relay.time, "time", return_value=101.0):
        relay.dom_relay_poll_for_client(client)
    with patch.object(relay.time, "time", return_value=210.0):
        assert relay.dom_relay_post_progress("job", response="part", seq=1,
                                            client_id=client["client_id"])
    with patch.object(relay.time, "time", return_value=250.0):
        relay.dom_relay_poll_for_client(client)
    assert item["error"] is None
    assert item["idle_polls_since_dispatch"] == 1


def test_other_relay_heartbeat_does_not_extend_ownership(active_request):
    client, item = active_request
    with patch.object(relay.time, "time", return_value=101.0):
        relay.dom_relay_poll_for_client(client)
    with patch.object(relay.time, "time", return_value=230.0):
        relay.dom_relay_poll_for_client({"client_id": "another"}, busy=True, busy_req_id="job")
    with patch.object(relay.time, "time", return_value=250.0):
        relay.dom_relay_poll_for_client(client)
    assert item["event"].is_set()


def test_settled_result_cannot_be_overwritten(active_request):
    client, item = active_request
    assert relay.dom_relay_post_response("job", response="answer", client_id=client["client_id"])
    assert relay.dom_relay_post_response("job", response="answer", client_id=client["client_id"])
    assert not relay.dom_relay_post_response("job", error="late error", client_id=client["client_id"])
    assert item["response"] == "answer"
    assert item["error"] is None


def test_cancelled_request_rejects_late_answer(active_request):
    client, item = active_request
    relay.dom_relay_cancel_request(req_id="job")
    assert not relay.dom_relay_post_response("job", response="late answer", client_id=client["client_id"])
    assert item["response"] is None


def test_lost_ack_retry_after_waiter_consumes_result(active_request):
    client, item = active_request
    assert relay.dom_relay_post_response("job", response="answer", client_id=client["client_id"])
    relay._dom_relay["pending"].pop("job")
    assert relay.dom_relay_post_response("job", response="answer", client_id=client["client_id"])
    assert not relay.dom_relay_post_response("job", response="changed", client_id=client["client_id"])
    assert not relay.dom_relay_post_response("job", response="answer", client_id="wrong-browser")
    receipt = relay._dom_relay["completed_responses"]["job"]
    with patch.object(relay.time, "time", return_value=receipt["at"] + 901):
        assert not relay.dom_relay_post_response("job", response="answer", client_id=client["client_id"])


def test_extension_background_lifecycle_in_node():
    script = Path(__file__).with_name("background_tab_relay.test.cjs")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
