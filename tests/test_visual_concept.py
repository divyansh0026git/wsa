"""Tests for pre-orchestrator visual concept mockup gate and cross-account image/context transfer."""
from __future__ import annotations

import base64
import json
import struct
import threading
import zlib
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

from wsa_agent.agent import (
    ProviderLimitError,
    _reset_provider_chats_for_account_change,
    execute_agent_loop,
)
from wsa_agent.client import WSAClient
from wsa_agent.commands import handle_command
from wsa_agent.context import build_context_packet
from wsa_agent.session import SessionState
from wsa_agent.tools import detect_image_info
from wsa_agent.visual_concept import (
    build_image_gen_prompt,
    format_approved_concepts_context,
    is_frontend_visual_prompt,
    maybe_run_visual_concept_gate,
    plan_concept_views,
    select_image_gen_provider,
)
from wsa_server import relay


def _make_png_bytes(width: int = 2, height: int = 2) -> bytes:
    """Build a tiny genuine PNG byte payload readable by detect_image_info."""
    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + (b"\x18\x26\x44" * width)
    raw_scanlines = row * height
    idat = zlib.compress(raw_scanlines)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def _make_png_data_url() -> str:
    return "data:image/png;base64," + base64.b64encode(_make_png_bytes()).decode("ascii")


def test_is_frontend_visual_prompt_detection():
    assert is_frontend_visual_prompt("Build a premium 3d solar system website") is True
    assert is_frontend_visual_prompt("Design a SaaS telemetry dashboard UI") is True
    assert is_frontend_visual_prompt("Explain how React virtual DOM works") is False
    assert is_frontend_visual_prompt("Fix PostgreSQL connection pool leak") is False


def test_select_image_gen_provider_chatgpt_gemini_and_claude_delegation(tmp_path: Path):
    s1 = SessionState(project_root=tmp_path, selected_model="chatgpt")
    assert select_image_gen_provider(s1) == ("chatgpt", "direct")

    s2 = SessionState(project_root=tmp_path, selected_model="gemini")
    s2.providers_info = {"gemini": {"has_tab": True}, "chatgpt": {"has_tab": False}}
    assert select_image_gen_provider(s2) == (None, "chatgpt_required")

    s3 = SessionState(project_root=tmp_path, selected_model="claude")
    s3.providers_info = {"chatgpt": {"has_tab": True}, "claude": {"has_tab": True}}
    assert select_image_gen_provider(s3) == ("chatgpt", "direct")

    s4 = SessionState(project_root=tmp_path, selected_model="claude")
    s4.providers_info = {"gemini": {"has_tab": True}, "claude": {"has_tab": True}}
    assert select_image_gen_provider(s4) == (None, "chatgpt_required")

    s5 = SessionState(project_root=tmp_path, selected_model="claude")
    s5.providers_info = {"claude": {"has_tab": True}}
    s5.multiagent_queue = ["claude"]
    assert select_image_gen_provider(s5) == (None, "chatgpt_required")


def test_visual_concept_gate_prompts_to_open_chatgpt_then_retries_and_approves(tmp_path: Path):
    png_url = _make_png_data_url()

    # Case 1: User skips when ChatGPT stays closed
    s_skip = SessionState(project_root=tmp_path, selected_model="claude")
    s_skip.providers_info = {"claude": {"has_tab": True}, "chatgpt": {"has_tab": False}}

    class _NoChatGPTClient:
        def get_health(self):
            return {"active_providers": {"claude": {"has_tab": True}, "chatgpt": {"has_tab": False}}}

        def stream(self, **kwargs):
            raise AssertionError("Should not call stream when ChatGPT is not open")

    prompts_seen: List[str] = []
    res_skip = maybe_run_visual_concept_gate(
        "Build a dark mission-control dashboard UI",
        s_skip,
        _NoChatGPTClient(),
        input_fn=lambda p: prompts_seen.append(p) or "s",
    )
    assert res_skip["skipped"] is True
    assert res_skip["approved"] is False
    assert any("Open ChatGPT" in p for p in prompts_seen)

    # Case 2: User opens ChatGPT, presses [r] to retry, then approves [y]; echoed prompt is sanitized
    s_ok = SessionState(project_root=tmp_path, selected_model="claude")
    s_ok.providers_info = {"claude": {"has_tab": True}, "chatgpt": {"has_tab": False}}
    health_calls = {"n": 0}

    class _RetryOpenChatGPTClient:
        def get_health(self):
            health_calls["n"] += 1
            if health_calls["n"] >= 2:
                return {"active_providers": {"claude": {"has_tab": True}, "chatgpt": {"has_tab": True}}}
            return {"active_providers": {"claude": {"has_tab": True}, "chatgpt": {"has_tab": False}}}

        def stream(self, **kwargs):
            prompt_sent = kwargs["messages"][0]["content"]
            return {
                "content": prompt_sent,  # Echo raw prompt to verify sanitization
                "generated_images": [{"data_url": png_url, "alt": "UI concept"}],
            }

    answers = iter(["r", "y", "y"])
    res_ok = maybe_run_visual_concept_gate(
        "Build a dark mission-control dashboard UI",
        s_ok,
        _RetryOpenChatGPTClient(),
        input_fn=lambda _p: next(answers),
    )
    assert res_ok["approved"] is True
    assert s_ok.approved_design_concepts[0]["provider"] == "chatgpt"
    assert s_ok.approved_design_concepts[0]["description"] == "ChatGPT generated UI concept mockup image."
    assert s_ok.model == "claude"


def test_build_image_gen_prompt_formats_for_provider():
    p_gpt = build_image_gen_prompt(
        "chatgpt",
        "Build a dark observatory dashboard",
        "Main Dashboard",
        "Telemetry grid and nav",
        refinements=["Use warm amber highlights"],
    )
    assert p_gpt.startswith("Generate an image of")
    assert "Use warm amber highlights" in p_gpt

    p_gem = build_image_gen_prompt(
        "gemini",
        "Build a dark observatory dashboard",
        "Main Dashboard",
        "Telemetry grid and nav",
    )
    assert "Generate a studio-grade UI concept image" in p_gem
    views = plan_concept_views("Build a 3d solar system explorer")
    assert any("3D" in v["title"] for v in views)


def test_maybe_run_visual_concept_gate_regenerate_more_approve(tmp_path: Path):
    png_url = _make_png_data_url()
    info = detect_image_info(_make_png_bytes())
    assert info and str(info["format"]).lower() == "png"

    state = SessionState(project_root=tmp_path, selected_model="claude")
    state.providers_info = {"chatgpt": {"has_tab": True}, "claude": {"has_tab": True}}

    stream_calls: List[Dict[str, Any]] = []

    class _FakeConceptClient:
        def stream(self, **kwargs):
            stream_calls.append(kwargs)
            return {
                "content": f"Concept draft #{len(stream_calls)} ready.",
                "generated_images": [{"data_url": png_url, "alt": "Concept UI"}],
            }

    answers = iter([
        "y",                                     # Pre-gate: generate concept first?
        "n",                                     # Reject draft #1
        "Add warmer solar gold lighting and clean orbital ring controls",  # Refinement details
        "m",                                     # Approve draft #2 and generate more UI part
        "Planet Telemetry Inspector",            # Name next UI part
        "y",                                     # Approve draft #3 & start Architecture Planning
    ])

    res = maybe_run_visual_concept_gate(
        "Build an interactive 3D solar system website with planetary telemetry",
        state,
        _FakeConceptClient(),
        input_fn=lambda _prompt: next(answers),
    )

    assert res["approved"] is True
    assert res["skipped"] is False
    assert len(stream_calls) == 3
    assert all(c["model"] == "chatgpt" for c in stream_calls)
    assert all(c["image_gen"] is True for c in stream_calls)
    assert all(c["new_chat"] is True for c in stream_calls)
    assert all(c["use_tools"] is False for c in stream_calls)
    assert "Add warmer solar gold lighting" in stream_calls[1]["messages"][0]["content"]
    assert "Planet Telemetry Inspector" in stream_calls[2]["messages"][0]["content"]

    assert len(state.approved_design_concepts) == 2
    all_saved_paths = [
        p for item in state.approved_design_concepts for p in (item.get("image_paths") or [])
    ]
    assert len(all_saved_paths) == 2
    for rel in all_saved_paths:
        f = tmp_path / rel
        assert f.is_file()
        assert f.read_bytes().startswith(b"\x89PNG")

    assert "USER-APPROVED VISUAL UI CONCEPT" in res["enriched_prompt"]
    assert state.model == "claude"  # Active coding agent unchanged after concept delegation


def test_session_save_load_and_context_packet_preserve_concepts(tmp_path: Path):
    concepts_dir = tmp_path / ".wsa_concepts"
    concepts_dir.mkdir(parents=True, exist_ok=True)
    img_path = concepts_dir / "hero.png"
    img_path.write_bytes(_make_png_bytes())

    s1 = SessionState(project_root=tmp_path, selected_model="chatgpt", session_id="concept-sess")
    s1.visual_concept_mode = "on"
    s1.approved_design_concepts = [
        {
            "provider": "gemini",
            "mode": "select_tool",
            "title": "Hero 3D Scene & Spatial HUD",
            "focus": "Minimal typography over orbital scene",
            "refinement_note": "Deep void obsidian palette",
            "image_paths": [".wsa_concepts/hero.png"],
            "description": "Generated obsidian orbital concept",
        }
    ]
    s1.messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"Conversation turn message {i}"}
        for i in range(12)
    ]
    s1.compact_history(keep=4)
    assert "Approved UI Concept:" in s1.context_summary
    assert "approved" in format_approved_concepts_context(s1.approved_design_concepts).lower()
    saved_path = Path(s1.save())

    s2 = SessionState(project_root=tmp_path)
    s2.load(saved_path)
    assert s2.session_id == "concept-sess"
    assert s2.visual_concept_mode == "on"
    assert len(s2.approved_design_concepts) == 1
    assert s2.approved_design_concepts[0]["image_paths"] == [".wsa_concepts/hero.png"]

    pkt = build_context_packet(
        project_root=tmp_path,
        project_type="vite",
        ledger=s2.file_ledger,
        design_concepts=s2.approved_design_concepts,
    )
    assert "USER-APPROVED VISUAL UI CONCEPT" in pkt
    assert ".wsa_concepts/hero.png" in pkt
    assert "Hero 3D Scene & Spatial HUD" in pkt


def test_execute_agent_loop_transfers_concept_images_on_failover_and_account_change(tmp_path: Path):
    concepts_dir = tmp_path / ".wsa_concepts"
    concepts_dir.mkdir(parents=True, exist_ok=True)
    (concepts_dir / "approved.png").write_bytes(_make_png_bytes())

    state = SessionState(
        project_root=tmp_path,
        selected_model="chatgpt",
        agent_mode="multiagent",
        multiagent_queue=["chatgpt", "claude", "gemini"],
        multiagent_index=0,
    )
    state.approved_design_concepts = [
        {
            "provider": "chatgpt",
            "mode": "direct",
            "title": "Main Dashboard",
            "focus": "Telemetry grid",
            "refinement_note": "Slate charcoal + warm gold",
            "image_paths": [".wsa_concepts/approved.png"],
            "description": "Approved dashboard layout",
        }
    ]

    recorded_streams: List[Dict[str, Any]] = []

    class _FailoverClient:
        api_base = "http://127.0.0.1:8081"
        api_key = ""

        def get_health(self):
            return {"dom_relay_active": True}

        def stream(self, **kwargs):
            recorded_streams.append(kwargs)
            model = kwargs["model"]
            if len(recorded_streams) == 1 and model == "chatgpt":
                raise ProviderLimitError("Usage limit reached", provider="chatgpt")
            chat_url = (
                "https://claude.ai/chat/11111111-1111-1111-1111-111111111111"
                if model == "claude"
                else "https://chatgpt.com/c/22222222-2222-2222-2222-222222222222"
            )
            return {
                "content": "Implemented layout matching approved concept.",
                "tool_calls": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "upstream_model": {"chat_url": chat_url},
            }

    client = _FailoverClient()
    res1 = execute_agent_loop(
        "Build the dashboard UI now",
        state,
        client,
        enforce_verify_gate=False,
        quiet_ui=True,
    )
    assert res1["final_text"] == "Implemented layout matching approved concept."
    # Attempt 1 went to chatgpt (with concept image), failed over to claude (also received concept image)
    assert len(recorded_streams) == 2
    for idx, expected_tag in enumerate(["CHATGPT", "CLAUDE"]):
        msgs = recorded_streams[idx]["messages"]
        user_msg = [m for m in msgs if m.get("role") == "user"][-1]
        assert isinstance(user_msg["content"], list)
        img_parts = [p for p in user_msg["content"] if p.get("type") == "image_url"]
        txt_parts = [str(p.get("text") or "") for p in user_msg["content"] if p.get("type") == "text"]
        assert len(img_parts) == 1
        assert img_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert any(f"[APPROVED VISUAL DESIGN TRANSFER -> {expected_tag}]" in t for t in txt_parts)

    # Same thread continuation on claude should NOT duplicate images
    res2 = execute_agent_loop(
        "Refine card spacing",
        state,
        client,
        enforce_verify_gate=False,
        quiet_ui=True,
    )
    assert res2["final_text"]
    assert len(recorded_streams) == 3
    last_user = [m for m in recorded_streams[2]["messages"] if m.get("role") == "user"][-1]
    if isinstance(last_user["content"], list):
        assert not [p for p in last_user["content"] if p.get("type") == "image_url"]

    # After account switch reset for claude, images & context transfer again cleanly!
    _reset_provider_chats_for_account_change(
        state, client, state.session_id, ["claude"], clear_roles=True, refresh_health=False
    )
    res3 = execute_agent_loop(
        "Continue on new claude account",
        state,
        client,
        enforce_verify_gate=False,
        quiet_ui=True,
    )
    assert res3["final_text"]
    assert len(recorded_streams) == 4
    reattached_user = [m for m in recorded_streams[3]["messages"] if m.get("role") == "user"][-1]
    assert isinstance(reattached_user["content"], list)
    assert any(p.get("type") == "image_url" for p in reattached_user["content"])


def test_dom_relay_generated_images_and_client_x_generated_images():
    png_url = _make_png_data_url()
    with relay._dom_relay_lock:
        original = dict(relay._dom_relay)
        relay._dom_relay.update(
            relays={},
            pending={},
            cancelled_req_ids={},
            recent_requests={},
            completed_responses={},
            sessions={},
            browser_cmds={},
            active_relay_id=None,
        )
    try:
        client_info = {"client_id": "concept-relay", "browser": "chrome", "has_gemini": 1}
        relay.register_or_touch_relay(client_info)

        def _worker_responder():
            for _ in range(40):
                job, _ = relay.dom_relay_poll_for_client(client_info)
                if job:
                    assert job.get("image_gen") is True
                    relay.dom_relay_post_response(
                        job["id"],
                        response="Here is your generated concept image.",
                        client_id="concept-relay",
                        generated_images=[{"data_url": png_url, "alt": "Solar HUD", "width": 512, "height": 512}],
                    )
                    return

        t = threading.Thread(target=_worker_responder, daemon=True)
        t.start()

        relay._relay_req_ctx.relay_meta = {"image_gen": True, "provider": "gemini", "new_chat": True}
        events = list(relay.dom_relay_iter("Generate concept image", timeout=5.0, step=0.05, provider="gemini"))
        t.join(timeout=2.0)

        gen_events = [ev for ev in events if ev[0] == "generated_images"]
        res_events = [ev for ev in events if ev[0] == "result"]
        assert len(gen_events) == 1
        assert gen_events[0][1][0]["data_url"].startswith("data:image/png;base64,")
        assert len(res_events) == 1
        assert res_events[0][1]["generated_images"][0]["alt"] == "Solar HUD"
    finally:
        if hasattr(relay._relay_req_ctx, "relay_meta"):
            delattr(relay._relay_req_ctx, "relay_meta")
        with relay._dom_relay_lock:
            relay._dom_relay.clear()
            relay._dom_relay.update(original)

    # Test WSAClient.stream collecting x_generated_images SSE event
    captured_payload: Dict[str, Any] = {}
    sse_lines = [
        "data: " + json.dumps({"x_generated_images": [{"data_url": png_url, "alt": "Streamed"}]}),
        "data: " + json.dumps({"choices": [{"delta": {"content": "Concept ready"}, "finish_reason": "stop"}]}),
        "data: [DONE]",
    ]

    class _MockHTTPResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_lines(self):
            return iter(sse_lines)

    def _fake_stream(method, url, **kwargs):
        if isinstance(kwargs.get("json"), dict):
            captured_payload.update(kwargs["json"])
        return _MockHTTPResponse()

    wsa_client = WSAClient("http://127.0.0.1:8081/v1", "test-key")
    with patch.object(wsa_client._client, "stream", side_effect=_fake_stream):
        stream_res = wsa_client.stream(
            messages=[{"role": "user", "content": "Generate an image"}],
            model="chatgpt",
            use_tools=False,
            image_gen=True,
        )
    assert captured_payload["image_gen"] is True
    assert stream_res["content"] == "Concept ready"
    assert len(stream_res["generated_images"]) == 1
    assert stream_res["generated_images"][0]["alt"] == "Streamed"


def test_slash_command_concept_modes(tmp_path: Path):
    state = SessionState(project_root=tmp_path, selected_model="chatgpt")
    state.approved_design_concepts = [{"title": "Hero", "image_paths": [".wsa_concepts/a.png"]}]

    assert handle_command("/concept on", state) == (True, True)
    assert state.visual_concept_mode == "on"

    assert handle_command("/concept off", state) == (True, True)
    assert state.visual_concept_mode == "off"

    assert handle_command("/concept clear", state) == (True, True)
    assert state.approved_design_concepts == []

    assert handle_command("/concept auto", state) == (True, True)
    assert state.visual_concept_mode == "auto"


def test_run_turn_preserves_singleagent_and_multiagent_after_concept_approval(tmp_path: Path):
    import wsa_agent.subagents as subagents_mod
    import wsa_agent.visual_concept as vc_mod
    from wsa_agent.agent import run_turn

    png_url = _make_png_data_url()

    # Case A: Single-agent mode with team_mode="off" -> ChatGPT concept preview, then normal execute_agent_loop on Claude
    dir_single = tmp_path / "single"
    dir_single.mkdir(parents=True, exist_ok=True)
    s_single = SessionState(project_root=dir_single, selected_model="claude", agent_mode="singleagent", team_mode="off")
    s_single.providers_info = {"chatgpt": {"has_tab": True}, "claude": {"has_tab": True}}

    calls_single: List[Dict[str, Any]] = []

    class _SingleTurnClient:
        api_base = "http://127.0.0.1:8081"
        api_key = ""

        def get_health(self):
            return {"dom_relay_active": True, "active_providers": s_single.providers_info}

        def stream(self, **kwargs):
            calls_single.append(kwargs)
            if kwargs.get("image_gen"):
                return {
                    "content": "ChatGPT concept preview ready.",
                    "generated_images": [{"data_url": png_url, "alt": "Dashboard Preview"}],
                }
            return {
                "content": "Built dashboard UI in single-agent mode.",
                "tool_calls": [],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4},
                "upstream_model": {"chat_url": "https://claude.ai/chat/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
            }

    ans_a = iter(["y", "y"])
    with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), patch.object(
        vc_mod, "_safe_read_choice", side_effect=lambda _p, input_fn=None: next(ans_a)
    ):
        out_a = run_turn("Design a clean telemetry dashboard UI", s_single, _SingleTurnClient())

    assert out_a["final_text"] == "Built dashboard UI in single-agent mode."
    assert len(calls_single) == 2
    assert calls_single[0]["model"] == "chatgpt" and calls_single[0]["image_gen"] is True
    assert calls_single[1]["model"] == "claude"
    assert s_single.agent_mode == "singleagent"
    assert s_single.model == "claude"
    assert len(s_single.approved_design_concepts) == 1

    # Case B: Multi-agent mode with team_mode="on" -> ChatGPT concept preview, then normal run_orchestrated_turn
    dir_multi = tmp_path / "multi"
    dir_multi.mkdir(parents=True, exist_ok=True)
    s_multi = SessionState(
        project_root=dir_multi,
        selected_model="claude",
        agent_mode="multiagent",
        multiagent_queue=["claude", "gemini", "chatgpt"],
        multiagent_index=0,
        team_mode="on",
    )
    s_multi.providers_info = {"chatgpt": {"has_tab": True}, "claude": {"has_tab": True}, "gemini": {"has_tab": True}}

    calls_multi: List[Dict[str, Any]] = []
    orch_seen: Dict[str, Any] = {}

    class _MultiTurnClient:
        api_base = "http://127.0.0.1:8081"
        api_key = ""

        def get_health(self):
            return {"dom_relay_active": True, "active_providers": s_multi.providers_info}

        def stream(self, **kwargs):
            calls_multi.append(kwargs)
            return {
                "content": "ChatGPT 3D preview ready.",
                "generated_images": [{"data_url": png_url, "alt": "3D Preview"}],
            }

    def _fake_orch(prompt, st, cli, **kwargs):
        orch_seen["prompt"] = prompt
        orch_seen["agent_mode"] = st.agent_mode
        orch_seen["model"] = st.model
        orch_seen["queue"] = list(st.multiagent_queue)
        orch_seen["index"] = st.multiagent_index
        orch_seen["concepts"] = list(st.approved_design_concepts)
        return {"final_text": "Orchestrated build complete."}

    ans_b = iter(["y", "y"])
    with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), patch.object(
        vc_mod, "_safe_read_choice", side_effect=lambda _p, input_fn=None: next(ans_b)
    ), patch.object(subagents_mod, "run_orchestrated_turn", side_effect=_fake_orch):
        out_b = run_turn("Build a 3d interactive solar system website with spatial HUD", s_multi, _MultiTurnClient())

    assert out_b["final_text"] == "Orchestrated build complete."
    assert len(calls_multi) == 1
    assert calls_multi[0]["model"] == "chatgpt" and calls_multi[0]["image_gen"] is True
    assert orch_seen["agent_mode"] == "multiagent"
    assert orch_seen["model"] == "claude"
    assert orch_seen["queue"] == ["claude", "gemini", "chatgpt"]
    assert orch_seen["index"] == 0
    assert len(orch_seen["concepts"]) == 1
    assert "USER-APPROVED VISUAL UI CONCEPT" in orch_seen["prompt"]
