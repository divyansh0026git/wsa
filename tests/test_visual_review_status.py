"""Deterministic tests for visual-review TUI status, same-process timings, and relay observability."""
from __future__ import annotations

import io
import json
import threading
import time
from unittest.mock import Mock, patch

import pytest
from rich.console import Console

import wsa_agent.agent as agent
import wsa_agent.renderer as R
from wsa_agent.client import WSAClient
from wsa_agent.session import SessionState
from wsa_agent.visual_status import (
    VisualReviewTracker,
    describe_capture_viewports,
    format_duration_ms,
    format_live_elapsed,
    short_provider_name,
)


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


def test_visual_tracker_direct_review_honest_stages_and_timings():
    clock = _FakeClock(100.0)
    tr = VisualReviewTracker("gemini", delegated=False, clock=clock)

    assert tr.begin_capture() == "Capturing desktop view…"
    clock.advance(1.35)
    assert tr.end_capture(ok=True) == "Screenshot ready"
    assert tr.capture_ms == 1350

    assert tr.begin_review("req-1", "gemini") == "Screenshot ready · Sending to Gemini"

    # Extension picks up request: NOT uploaded yet.
    upd = tr.on_relay_event({"client_request_id": "req-1", "provider": "gemini", "stage": "dispatched"})
    assert upd == ("Screenshot sent to relay · Waiting on Gemini", True)
    assert "uploaded" not in upd[0].lower()

    # Extension confirms verified image attachment & submission.
    upd = tr.on_relay_event({
        "client_request_id": "req-1",
        "provider": "gemini",
        "stage": "submitted",
        "images_attached": True,
        "upload_ms": 640,
    })
    assert upd == ("Screenshot uploaded · Waiting for Gemini", True)
    assert tr.upload_confirmed is True
    assert tr.submit_confirmed is True

    # Ordinary text reply from direct provider MUST NOT say "passed".
    final = tr.finish(
        "reviewed",
        timings={
            "upload_ms": 640,
            "images_attached": True,
            "submit_confirmed": True,
            "response_ms": 42_000,
            "round_trip_ms": 43_100,
        },
    )
    assert "passed" not in final.lower()
    assert final == "Visual review complete · Gemini replied (capture 1.4s · upload 640ms · response 42s)"


def test_visual_tracker_missing_upload_ack_older_extension_never_fabricates():
    clock = _FakeClock(50.0)
    tr = VisualReviewTracker("claude", delegated=False, clock=clock)
    tr.set_capture_ms(900)
    assert tr.begin_review("req-old", "claude") == "Screenshot ready · Sending to Claude"

    # Older extension never sends stage='submitted' or images_attached=True.
    upd1 = tr.on_relay_event({"client_request_id": "req-old", "provider": "claude", "stage": "dispatched"})
    assert upd1 == ("Screenshot sent to relay · Waiting on Claude", True)
    assert "uploaded" not in upd1[0].lower()
    assert "waiting for response" not in upd1[0].lower()

    clock.advance(12.4)
    upd2 = tr.on_relay_event({"client_request_id": "req-old", "provider": "claude", "stage": "generating"})
    assert upd2 == ("Claude is reviewing screenshot", True)

    # Server only had round_trip_ms (no submitted_mono, no upload_ms).
    final = tr.finish(
        "reviewed",
        timings={
            "images_attached": None,
            "submit_confirmed": False,
            "round_trip_ms": 14_200,
        },
    )
    assert "upload" not in final.lower()
    assert " 0ms" not in final
    assert "response" not in final.lower()
    assert "round trip 14s" in final
    diag = tr.to_diagnostics_dict()
    assert "upload_ms" not in diag["timings"]
    assert "response_ms" not in diag["timings"]
    assert diag["timings"]["round_trip_ms"] == 14_200
    assert diag["upload_confirmed"] is False


def test_visual_tracker_unverified_submission_never_claims_uploaded():
    clock = _FakeClock(10.0)
    tr = VisualReviewTracker("chatgpt", delegated=False, clock=clock)
    tr.begin_review("req-unv", "chatgpt")

    upd = tr.on_relay_event({
        "client_request_id": "req-unv",
        "provider": "chatgpt",
        "stage": "submitted",
        "images_attached": False,
    })
    assert upd == ("Submitted to ChatGPT · Waiting for response", True)
    assert "uploaded" not in upd[0].lower()
    assert tr.upload_confirmed is False
    assert tr.submit_confirmed is True


def test_visual_tracker_rejects_stale_foreign_and_post_close_events():
    clock = _FakeClock(200.0)
    tr = VisualReviewTracker("deepseek", delegated=True, clock=clock)
    assert tr.begin_review("req-a", "claude") == "DeepSeek requested visual review · Claude is reviewing"
    tr.on_relay_event({
        "client_request_id": "req-a",
        "provider": "claude",
        "stage": "submitted",
        "images_attached": True,
        "upload_ms": 500,
    })

    # Fallback to Gemini resets per-attempt state and binds new request ID.
    assert tr.begin_review("req-b", "gemini", fallback_from="claude") == (
        "Claude unavailable · Gemini is reviewing for DeepSeek"
    )
    assert tr.upload_confirmed is False
    assert tr.browser_upload_ms is None

    # Late event from previous reviewer/request MUST be ignored.
    assert tr.on_relay_event({
        "client_request_id": "req-a",
        "provider": "claude",
        "stage": "generating",
    }) is None
    assert tr.on_relay_event({
        "client_request_id": "req-b",
        "provider": "claude",
        "stage": "submitted",
        "images_attached": True,
    }) is None
    assert tr.stage == "queued"

    # Valid progression on Gemini.
    assert tr.on_relay_event({
        "client_request_id": "req-b",
        "provider": "gemini",
        "stage": "submitted",
        "images_attached": True,
        "upload_ms": 720,
    }) == ("Screenshot uploaded · Waiting for Gemini", True)

    # Regressive out-of-order stage MUST be ignored.
    assert tr.on_relay_event({
        "client_request_id": "req-b",
        "provider": "gemini",
        "stage": "dispatched",
    }) is None
    assert tr.stage == "submitted"

    # After cancellation/completion, any late event MUST be ignored.
    tr.finish("cancelled")
    assert tr.on_relay_event({
        "client_request_id": "req-b",
        "provider": "gemini",
        "stage": "generating",
    }) is None
    assert tr.stage == "cancelled"


def test_stream_renderer_updates_single_line_elapsed_without_scrollback_spam(monkeypatch):
    clock = _FakeClock(500.0)
    buf = io.StringIO()
    test_console = Console(file=buf, width=80, height=24, force_terminal=False, highlight=False)
    monkeypatch.setattr(R, "console", test_console)

    sr = R.StreamRenderer(round_num=0, model="gemini", stage_label="", clock=clock)
    sr._thinking_start = R.time.time()
    sr.set_status_hint("Screenshot uploaded · Waiting for Gemini", with_elapsed=True, reset_stage_clock=True)

    # At t=0.0s: clean status without 0.0s flicker.
    render0 = sr._thinking_renderable().plain
    assert "Screenshot uploaded · Waiting for Gemini" in render0
    assert "42s" not in render0

    # Advance fake clock to 42s: elapsed updates in-place on the single renderable, zero scrollback prints.
    clock.advance(42.0)
    render42 = sr._thinking_renderable().plain
    assert "Screenshot uploaded · Waiting for Gemini · 42s" in render42
    assert buf.getvalue() == ""  # No lines printed to scrollback while waiting!

    # Narrow terminal keeps the elapsed time visible and clamps cleanly within width.
    narrow_console = Console(file=io.StringIO(), width=46, height=24, force_terminal=False, highlight=False)
    monkeypatch.setattr(R, "console", narrow_console)
    clamped = R._clamp_live_text(sr._thinking_renderable()).plain
    assert "42s" in clamped
    for line in clamped.splitlines():
        assert len(line) <= 46


def test_stream_renderer_text_only_turn_retains_existing_display(monkeypatch):
    clock = _FakeClock(100.0)
    test_console = Console(file=io.StringIO(), width=90, height=24, force_terminal=False, highlight=False)
    monkeypatch.setattr(R, "console", test_console)

    sr = R.StreamRenderer(round_num=1, status_hint="Analyzing Read(app.py) output…", model="claude", clock=clock)
    sr._thinking_start = R.time.time() - 3.0
    clock.advance(25.0)
    rendered = sr._thinking_renderable().plain
    assert "Analyzing Read(app.py) output…" in rendered
    assert "25s" not in rendered  # Ordinary text-only hint does NOT gain a stage timer.


def test_client_stream_dispatches_relay_status_and_filters_mismatched_req_id():
    received = []
    sse_lines = [
        "data: " + json.dumps({
            "id": "c1",
            "object": "chat.completion.chunk",
            "choices": [],
            "x_relay_status": {
                "client_request_id": "foreign-req",
                "provider": "gemini",
                "stage": "submitted",
                "images_attached": True,
            },
        }),
        "data: " + json.dumps({
            "id": "c2",
            "object": "chat.completion.chunk",
            "choices": [],
            "x_relay_status": {
                "client_request_id": "my-req-42",
                "provider": "gemini",
                "stage": "submitted",
                "images_attached": True,
                "upload_ms": 390,
            },
        }),
        "data: " + json.dumps({
            "id": "c3",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": "Looks aligned."}, "finish_reason": None}],
        }),
        "data: " + json.dumps({
            "id": "c4",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "x_relay_timings": {
                "provider": "gemini",
                "images_attached": True,
                "submit_confirmed": True,
                "upload_ms": 390,
                "response_ms": 8400,
                "round_trip_ms": 8900,
            },
        }),
        "data: [DONE]",
    ]

    class _FakeResp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_lines(self):
            return iter(sse_lines)

    client = WSAClient("http://127.0.0.1:8081/v1", "sk-test")
    with patch.object(client._client, "stream", return_value=_FakeResp()):
        res = client.stream(
            messages=[{"role": "user", "content": "hi"}],
            model="gemini",
            on_relay_status=received.append,
            client_request_id="my-req-42",
        )

    assert len(received) == 1
    assert received[0]["client_request_id"] == "my-req-42"
    assert received[0]["stage"] == "submitted"
    assert res["content"] == "Looks aligned."
    assert res["relay_timings"]["upload_ms"] == 390
    assert res["relay_timings"]["response_ms"] == 8400


@pytest.mark.parametrize("provider", ["claude", "gemini", "chatgpt"])
def test_direct_visual_review_loop_emits_honest_status_without_false_pass(tmp_path, provider):
    state = SessionState(project_root=tmp_path, selected_model=provider)
    events = []
    client = Mock()
    round_idx = {"n": 0}

    def fake_stream(**kwargs):
        round_idx["n"] += 1
        if round_idx["n"] == 1:
            return {"tool_calls": [{"id": "v1", "function": {"name": "visual_inspect", "arguments": "{}"}}]}
        cb = kwargs.get("on_relay_status")
        cid = kwargs.get("client_request_id")
        assert callable(cb) and cid
        cb({"client_request_id": cid, "provider": provider, "stage": "dispatched"})
        cb({
            "client_request_id": cid,
            "provider": provider,
            "stage": "submitted",
            "images_attached": True,
            "upload_ms": 480,
        })
        cb({"client_request_id": cid, "provider": provider, "stage": "generating"})
        return {
            "content": "I checked the layout and adjusted the hero spacing.",
            "tool_calls": [],
            "relay_timings": {
                "provider": provider,
                "images_attached": True,
                "submit_confirmed": True,
                "upload_ms": 480,
                "response_ms": 7200,
                "round_trip_ms": 7900,
            },
        }

    client.stream.side_effect = fake_stream
    photo = {"url": "data:image/png;base64,iVBORw0KGgo=", "filename": "contact_sheet.png", "source_tool": "visual_inspect"}

    with patch.object(state, "auto_checkpoint"), \
         patch.object(agent, "consume_pending_images", side_effect=[[], [photo]]), \
         patch("wsa_agent.tools._visual_inspect", return_value="[OK] Captured\nVisual checks: pass"):
        res = agent.execute_agent_loop(
            "Check visual layout",
            state,
            client,
            quiet_ui=True,
            event_callback=lambda k, p: events.append((k, p)),
        )

    assert res["ok"] is True
    vis_events = [p for k, p in events if k == "visual_status"]
    assert len(vis_events) == 1
    assert vis_events[0]["outcome"] == "reviewed"
    assert "passed" not in vis_events[0]["message"].lower()
    assert short_provider_name(provider) in vis_events[0]["message"]
    assert state.last_visual_review is not None
    assert state.last_visual_review["upload_confirmed"] is True
    assert state.last_visual_review["timings"]["upload_ms"] == 480
    assert state.last_visual_review["timings"]["response_ms"] == 7200
    # Privacy check: no image data, email, or chat URL in diagnostics.
    serialized = json.dumps(state.last_visual_review)
    assert "base64" not in serialized
    assert "iVBORw0KGgo" not in serialized
    assert "http://" not in serialized and "https://" not in serialized


@pytest.mark.parametrize("worker", ["deepseek", "venice"])
def test_delegated_review_fallback_defects_unavailable_and_transport_error(tmp_path, worker):
    # 1. Fallback from Claude (unusable) to Gemini (returns 1 defect)
    state = SessionState(project_root=tmp_path, selected_model=worker)
    state.providers_info = {"claude": {"has_tab": True}, "gemini": {"has_tab": True}}
    events = []
    client = Mock()

    def stream_fallback(**kwargs):
        m = kwargs["model"]
        cb = kwargs.get("on_relay_status")
        cid = kwargs.get("client_request_id")
        if m == "claude":
            if cb:
                cb({"client_request_id": cid, "provider": "claude", "stage": "dispatched"})
            return {"content": "not json"}
        if m == "gemini":
            if cb:
                # Stale event from Claude must be ignored.
                cb({"client_request_id": "stale-claude", "provider": "claude", "stage": "submitted", "images_attached": True})
                cb({"client_request_id": cid, "provider": "gemini", "stage": "submitted", "images_attached": True, "upload_ms": 510})
            return {
                "content": json.dumps({"pass": False, "defects": [{"issue": "header clipped"}], "summary": "clip"}),
                "relay_timings": {"images_attached": True, "submit_confirmed": True, "upload_ms": 510, "response_ms": 6100, "round_trip_ms": 6700},
            }
        if kwargs["messages"][-1]["role"] == "user" and kwargs["messages"][-1]["content"] == "Check UI":
            return {"tool_calls": [{"id": "v", "function": {"name": "visual_inspect", "arguments": "{}"}}]}
        return {"content": "Will fix the header clipping.", "tool_calls": []}

    client.stream.side_effect = stream_fallback
    photo = {"url": "data:image/png;base64,YQ==", "source_tool": "visual_inspect"}
    with patch.object(state, "auto_checkpoint"), \
         patch.object(agent, "consume_pending_images", side_effect=[[], [photo]]), \
         patch("wsa_agent.tools._visual_inspect", return_value="[OK] Captured\nVisual checks: pass"):
        agent.execute_agent_loop("Check UI", state, client, quiet_ui=True, event_callback=lambda k, p: events.append((k, p)))

    vis = [p for k, p in events if k == "visual_status"][-1]
    assert vis["outcome"] == "defects"
    assert f"Gemini returned 1 defect · Continuing in {short_provider_name(worker)}" in vis["message"]
    assert state.last_visual_review["reviewer"] == "gemini"
    assert state.last_visual_review["defects_count"] == 1

    # 2. Unavailable reviewer (no synced vision tab)
    state_unavail = SessionState(project_root=tmp_path, selected_model=worker)
    state_unavail.providers_info = {}
    events_u = []
    client_u = Mock()
    calls_u = {"n": 0}

    def stream_u(**kwargs):
        calls_u["n"] += 1
        if calls_u["n"] == 1:
            return {"tool_calls": [{"id": "v", "function": {"name": "visual_inspect", "arguments": "{}"}}]}
        return {"content": "No reviewer available.", "tool_calls": []}

    client_u.stream.side_effect = stream_u
    with patch.object(state_unavail, "auto_checkpoint"), \
         patch.object(agent, "consume_pending_images", side_effect=[[], [photo]]), \
         patch("wsa_agent.tools._visual_inspect", return_value="[OK] Captured\nVisual checks: pass"):
        agent.execute_agent_loop("Check UI", state_unavail, client_u, quiet_ui=True, event_callback=lambda k, p: events_u.append((k, p)))

    vis_u = [p for k, p in events_u if k == "visual_status"][-1]
    assert vis_u["outcome"] == "unavailable"
    assert "Visual review incomplete · No reviewer returned usable findings" in vis_u["message"]

    # 3. Transport error (distinct from unavailable / defect)
    state_err = SessionState(project_root=tmp_path, selected_model=worker)
    state_err.providers_info = {"claude": {"has_tab": True}}
    events_e = []
    client_e = Mock()

    def stream_e(**kwargs):
        if kwargs["model"] == "claude":
            raise ConnectionError("DOM relay disconnected")
        if kwargs["messages"][-1]["role"] == "user" and kwargs["messages"][-1]["content"] == "Check UI":
            return {"tool_calls": [{"id": "v", "function": {"name": "visual_inspect", "arguments": "{}"}}]}
        return {"content": "Transport failed.", "tool_calls": []}

    client_e.stream.side_effect = stream_e
    with patch.object(state_err, "auto_checkpoint"), \
         patch.object(agent, "consume_pending_images", side_effect=[[], [photo]]), \
         patch("wsa_agent.tools._visual_inspect", return_value="[OK] Captured\nVisual checks: pass"):
        agent.execute_agent_loop("Check UI", state_err, client_e, quiet_ui=True, event_callback=lambda k, p: events_e.append((k, p)))

    vis_e = [p for k, p in events_e if k == "visual_status"][-1]
    assert vis_e["outcome"] == "transport_error"
    assert "Visual review failed · Claude transport error" in vis_e["message"]


def test_delegated_review_cancellation_clears_state(tmp_path):
    state = SessionState(project_root=tmp_path, selected_model="deepseek")
    state.providers_info = {"claude": {"has_tab": True}}
    events = []
    client = Mock()

    def stream_cancel(**kwargs):
        if kwargs["model"] == "claude":
            raise KeyboardInterrupt()
        return {"tool_calls": [{"id": "v", "function": {"name": "visual_inspect", "arguments": "{}"}}]}

    client.stream.side_effect = stream_cancel
    photo = {"url": "data:image/png;base64,YQ==", "source_tool": "visual_inspect"}
    with patch.object(state, "auto_checkpoint"), \
         patch.object(agent, "consume_pending_images", side_effect=[[], [photo]]), \
         patch("wsa_agent.tools._visual_inspect", return_value="[OK] Captured\nVisual checks: pass"):
        res = agent.execute_agent_loop("Check UI", state, client, quiet_ui=True, event_callback=lambda k, p: events.append((k, p)))

    assert res["aborted_reason"] == "interrupted"
    vis = [p for k, p in events if k == "visual_status"][-1]
    assert vis["outcome"] == "cancelled"
    assert "Visual review cancelled" in vis["message"]
    assert state.last_visual_review["outcome"] == "cancelled"


def test_relay_iter_emits_monotonic_status_events_and_older_extension_fallback():
    import wsa_server.relay as relay

    # Register a temporary synthetic relay entry
    orig_relays = dict(relay._dom_relay["relays"])
    orig_pending = dict(relay._dom_relay["pending"])
    orig_dedup = dict(relay._dom_relay.get("client_req_dedup", {}))
    try:
        relay._dom_relay["relays"].clear()
        relay._dom_relay["pending"].clear()
        relay._dom_relay.setdefault("client_req_dedup", {}).clear()
        relay._dom_relay["relays"]["test-relay"] = {
            "id": "test-relay",
            "label": "chrome",
            "browser": "chrome",
            "last_seen": time.time(),
            "active_provider": "gemini",
            "providers": {"gemini": {"has_tab": True}},
        }

        def _drive_extension(older_extension: bool, req_marker: str):
            for _ in range(100):
                item = relay.dom_relay_get_pending(client_id="test-relay")
                if item and item.get("id"):
                    rid = item["id"]
                    time.sleep(0.02)
                    if not older_extension:
                        relay.dom_relay_post_progress(
                            rid,
                            seq=0,
                            stage="submitted",
                            images_attached=True,
                            upload_ms=275,
                            submit_ms=310,
                        )
                        time.sleep(0.02)
                    relay.dom_relay_post_progress(rid, response="Looks good", seq=1)
                    time.sleep(0.02)
                    relay.dom_relay_post_response(rid, response="Looks good", model="Gemini Web")
                    return
                time.sleep(0.01)

        tiny_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc`\x00\x00"
            b"\x00\x02\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        img_item = (tiny_png, "image/png")

        # Case 1: Updated extension sends stage='submitted' with verified attachment.
        t1 = threading.Thread(target=_drive_extension, args=(False, "new"), daemon=True)
        t1.start()
        ev1 = list(relay.dom_relay_iter(
            "Inspect screenshot",
            provider="gemini",
            images=[img_item],
            client_request_id="cid-new",
        ))
        t1.join(timeout=2)
        stages1 = [p["stage"] for k, p in ev1 if k == "relay_status"]
        assert "queued" in stages1
        assert "submitted" in stages1
        assert "generating" in stages1
        sub_ev = next(p for k, p in ev1 if k == "relay_status" and p["stage"] == "submitted")
        assert sub_ev["images_attached"] is True
        assert sub_ev["upload_ms"] == 275
        res1 = next(p for k, p in ev1 if k == "result")
        timings1 = res1.get("timings") or {}
        assert timings1.get("submit_confirmed") is True
        assert timings1.get("upload_ms") == 275
        assert isinstance(timings1.get("response_ms"), int) and timings1["response_ms"] >= 0
        assert isinstance(timings1.get("round_trip_ms"), int) and timings1["round_trip_ms"] >= 0

        # Case 2: Older extension skips stage='submitted'.
        t2 = threading.Thread(target=_drive_extension, args=(True, "old"), daemon=True)
        t2.start()
        ev2 = list(relay.dom_relay_iter(
            "Inspect screenshot",
            provider="gemini",
            images=[img_item],
            client_request_id="cid-old",
        ))
        t2.join(timeout=2)
        stages2 = [p["stage"] for k, p in ev2 if k == "relay_status"]
        assert "submitted" not in stages2
        res2 = next(p for k, p in ev2 if k == "result")
        timings2 = res2.get("timings") or {}
        assert timings2.get("submit_confirmed") is False
        assert "upload_ms" not in timings2
        assert "response_ms" not in timings2
        assert isinstance(timings2.get("round_trip_ms"), int) and timings2["round_trip_ms"] >= 0
    finally:
        relay._dom_relay["relays"].clear()
        relay._dom_relay["relays"].update(orig_relays)
        relay._dom_relay["pending"].clear()
        relay._dom_relay["pending"].update(orig_pending)
        relay._dom_relay.setdefault("client_req_dedup", {}).clear()
        relay._dom_relay["client_req_dedup"].update(orig_dedup)


def test_print_status_displays_sanitized_visual_review_diagnostics(tmp_path, monkeypatch):
    buf = io.StringIO()
    test_console = Console(file=buf, width=100, force_terminal=False, highlight=False)
    monkeypatch.setattr(R, "console", test_console)

    state = SessionState(project_root=tmp_path, selected_model="deepseek")
    tr = VisualReviewTracker("deepseek", delegated=True, clock=_FakeClock(10.0))
    tr.set_capture_ms(1400)
    tr.begin_review("req-st", "claude")
    tr.on_relay_event({"client_request_id": "req-st", "provider": "claude", "stage": "submitted", "images_attached": True, "upload_ms": 620})
    tr.finish("passed", timings={"images_attached": True, "submit_confirmed": True, "upload_ms": 620, "response_ms": 18_000, "round_trip_ms": 18_800})
    state.last_visual_review = tr.to_diagnostics_dict()

    R.print_status(state)
    out = " ".join(buf.getvalue().split())
    assert "Visual Review" in out
    assert "Claude passed visual review · Continuing in DeepSeek" in out
    assert "capture 1.4s · upload 620ms · response 18s" in out


def test_visual_status_helpers():
    assert format_duration_ms(None) == ""
    assert format_duration_ms(450) == "450ms"
    assert format_duration_ms(1450) == "1.4s"
    assert format_duration_ms(42_000) == "42s"
    assert format_live_elapsed(3.4) == "3.4s"
    assert format_live_elapsed(42.8) == "42s"
    assert describe_capture_viewports({}) == "Capturing desktop view…"
    assert describe_capture_viewports({"viewports": [{"name": "desktop"}]}) == "Capturing desktop view…"
    assert describe_capture_viewports({"viewports": [{"name": "desktop"}, {"name": "mobile"}]}) == "Capturing desktop and mobile views…"
    assert describe_capture_viewports({"viewports": [{"name": "tablet"}, {"name": "mobile"}]}) == "Capturing tablet and mobile views…"


def test_direct_review_local_defects_and_cancellation(tmp_path):
    # Local defects reported by visual_inspect -> never labelled "passed".
    state = SessionState(project_root=tmp_path, selected_model="gemini")
    events = []
    client = Mock()
    turn = {"n": 0}

    def stream_local_def(**kwargs):
        turn["n"] += 1
        if turn["n"] == 1:
            return {"tool_calls": [{"id": "v1", "function": {"name": "visual_inspect", "arguments": "{}"}}]}
        return {"content": "Seeing horizontal overflow on mobile.", "tool_calls": []}

    client.stream.side_effect = stream_local_def
    photo = {"url": "data:image/png;base64,YQ==", "filename": "sheet.png", "source_tool": "visual_inspect"}
    with patch.object(state, "auto_checkpoint"), \
         patch.object(agent, "consume_pending_images", side_effect=[[], [photo]]), \
         patch("wsa_agent.tools._visual_inspect", return_value="[OK] Captured\nVisual checks: 1 issue"):
        agent.execute_agent_loop("Check layout", state, client, quiet_ui=True, event_callback=lambda k, p: events.append((k, p)))

    vis = [p for k, p in events if k == "visual_status"][-1]
    assert vis["outcome"] == "reviewed_with_local_defects"
    assert "local layout/console defects present" in vis["message"]
    assert "passed" not in vis["message"].lower()

    # Cancellation during direct review clears state cleanly.
    state2 = SessionState(project_root=tmp_path, selected_model="claude")
    events2 = []
    client2 = Mock()
    turn2 = {"n": 0}

    def stream_cancel_direct(**kwargs):
        turn2["n"] += 1
        if turn2["n"] == 1:
            return {"tool_calls": [{"id": "v2", "function": {"name": "visual_inspect", "arguments": "{}"}}]}
        raise KeyboardInterrupt()

    client2.stream.side_effect = stream_cancel_direct
    with patch.object(state2, "auto_checkpoint"), \
         patch.object(agent, "consume_pending_images", side_effect=[[], [photo]]), \
         patch("wsa_agent.tools._visual_inspect", return_value="[OK] Captured\nVisual checks: pass"):
        res2 = agent.execute_agent_loop("Check layout", state2, client2, quiet_ui=True, event_callback=lambda k, p: events2.append((k, p)))

    assert res2["aborted_reason"] == "interrupted"
    vis2 = [p for k, p in events2 if k == "visual_status"][-1]
    assert vis2["outcome"] == "cancelled"
    assert "Visual review cancelled" in vis2["message"]


def test_tui_style_consistent_for_all_agents_and_themes(monkeypatch):
    from wsa_agent.theme import THEMES

    for theme_id in THEMES:
        R.apply_theme(theme_id, persist=False)
        for prov in ("claude", "gemini", "chatgpt", "deepseek", "venice"):
            buf = io.StringIO()
            c = Console(file=buf, width=88, force_terminal=False, highlight=False)
            monkeypatch.setattr(R, "console", c)
            R.print_visual_status(f"Visual review complete · {short_provider_name(prov)} replied (round trip 4.2s)", outcome="reviewed")
            R.print_visual_status(f"Claude passed visual review · Continuing in {short_provider_name(prov)}", outcome="passed")
            R.print_visual_status("Visual review incomplete · No reviewer returned usable findings", outcome="unavailable")
            out = buf.getvalue()
            assert short_provider_name(prov) in out
            assert "No reviewer returned usable findings" in out
    R.apply_theme("dark", persist=False)


def test_server_sse_forwards_x_relay_status_and_x_relay_timings():
    import http.client
    import wsa_server
    import wsa_server.server as srv

    def _fake_stream_events(*args, **kwargs):
        yield ("relay_status", {"client_request_id": "sse-req-1", "provider": "gemini", "stage": "queued"})
        yield ("relay_status", {
            "client_request_id": "sse-req-1",
            "provider": "gemini",
            "stage": "submitted",
            "images_attached": True,
            "upload_ms": 330,
        })
        yield ("content", "Checked UI.")
        rmeta = getattr(srv._relay_req_ctx, "relay_meta", None)
        if isinstance(rmeta, dict):
            rmeta["relay_timings"] = {
                "provider": "gemini",
                "images_attached": True,
                "submit_confirmed": True,
                "upload_ms": 330,
                "response_ms": 4200,
                "round_trip_ms": 4600,
            }

    httpd = wsa_server.ThreadedServer(("127.0.0.1", 0), wsa_server.WSAHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        with patch.object(srv, "gemini_stream_generate_events", side_effect=_fake_stream_events):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({
                "model": "gemini",
                "stream": True,
                "client_request_id": "sse-req-1",
                "messages": [{"role": "user", "content": "Inspect screenshot"}],
            })
            conn.request(
                "POST",
                "/v1/chat/completions",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {srv.CONFIG.get('api_key', 'sk-wsa')}",
                },
            )
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=2)

    assert resp.status == 200
    chunks = []
    for ln in raw.splitlines():
        if ln.startswith("data: ") and ln[6:].strip() != "[DONE]":
            chunks.append(json.loads(ln[6:]))
    status_chunks = [c["x_relay_status"] for c in chunks if "x_relay_status" in c]
    assert [s["stage"] for s in status_chunks] == ["queued", "submitted"]
    assert status_chunks[1]["images_attached"] is True
    assert status_chunks[1]["upload_ms"] == 330
    timing_chunks = [c["x_relay_timings"] for c in chunks if "x_relay_timings" in c]
    assert len(timing_chunks) == 1
    assert timing_chunks[0]["response_ms"] == 4200

