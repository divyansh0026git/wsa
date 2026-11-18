import json
from unittest.mock import Mock, patch

import pytest

from wsa_agent import agent, renderer, subagents, visual
from wsa_agent.session import SessionState
from wsa_agent.task_skills import select_task_skills, skill_guidance


@pytest.mark.parametrize(("prompt", "names"), [
    ("Make a premium 3D ecommerce website", ("frontend-design", "unslop")),
    ("Build a clean analytics interface", ("frontend-design", "unslop")),
    ("Redesign the frontend dashboard", ("frontend-design", "unslop")),
    ("Clean up the admin panel", ("frontend-design", "unslop")),
    ("Unslop this page", ("unslop",)),
    ("Add OAuth2 to the API", ("test-driven-development",)),
    ("Add a TypeScript interface for the API", ("test-driven-development",)),
    ("Debug a timeout in checkout", ("systematic-debugging",)),
    ("Fix the broken dashboard layout", ("frontend-design", "unslop", "systematic-debugging")),
    ("Build a responsive web app", ("frontend-design", "unslop", "test-driven-development")),
    ("Explain what an API is", ()),
    ("What is systematic debugging?", ()),
    ("Thank you", ()),
])
def test_skill_selection(prompt, names):
    assert select_task_skills(prompt) == names
    guidance = skill_guidance(names)
    for name in names:
        assert "[" + name + "]" in guidance
    if "unslop" in names:
        assert "@heydamir" in guidance
        assert "One bright element" in guidance
        assert "No emoji left in the interface" in guidance
        assert "references/visuals.md" not in guidance
        assert "references/rtl.md" not in guidance


def test_skill_reaches_each_role_and_single_mode_without_polluting_history(tmp_path):
    client = Mock()
    prompt = "Make a premium website"
    state = SessionState(project_root=tmp_path)
    plan = [{"id": "ui", "title": "Landing page", "owned_files": ["index.html"]}]
    seen = []

    def worker(**kwargs):
        seen.append(kwargs["role"])
        assert "[frontend-design]" in kwargs["prompt"]
        assert "[unslop]" in kwargs["prompt"]
        if kwargs["role"] == "architect":
            return {"ok": True, "final_text": json.dumps(plan)}
        if kwargs["role"] == "verifier":
            return {"ok": True, "final_text": '{"verdict":"pass","checks":["checked"],"failures":[]}',
                    "verification_evidence": [{"ok": True}]}
        return {"ok": True}

    with patch.object(subagents, "_run_role_agent", side_effect=worker), \
         patch.object(subagents, "_discover_live_relays", return_value=[]), \
         patch.object(state, "auto_checkpoint"):
        assert subagents.run_orchestrated_turn(prompt, state, client)["ok"]
    assert seen == ["explorer", "architect", "coder", "verifier"]
    state = SessionState(project_root=tmp_path, team_mode="off")
    client.stream.return_value = {"content": "Ready", "tool_calls": []}
    with patch.object(state, "auto_checkpoint"):
        result = agent.execute_agent_loop(prompt, state, client, quiet_ui=True)
    assert result["ok"]
    sent = client.stream.call_args.kwargs["messages"]
    assert any("[frontend-design]" in str(m["content"]) and "[unslop]" in str(m["content"]) for m in sent if m["role"] == "user")
    assert state.messages[0]["content"] == prompt


def test_scoped_role_stream_keeps_public_answer_and_handles_tool_events(tmp_path):
    state = SessionState(project_root=tmp_path)
    client = Mock()
    (tmp_path / "app.py").write_text("a = 1\nb = 2\n")
    outputs = []

    def stream(**kwargs):
        if not outputs:
            outputs.append("Inspecting the module.")
            kwargs["on_token"](outputs[0])
            return {"content": outputs[0], "tool_calls": [{"id": "r", "function": {"name": "read_file",
                    "arguments": json.dumps({"path": "app.py", "start_line": 2, "end_line": 2})}}]}
        kwargs["on_token"]("Read complete.")
        return {"content": "Read complete.", "tool_calls": []}

    client.stream.side_effect = stream
    from rich.console import Console
    from io import StringIO
    sink = StringIO()
    with patch.object(renderer, "console", Console(file=sink, force_terminal=False, width=100)), \
         patch.object(state, "auto_checkpoint"):
        result = subagents._run_role_agent(role="explorer", prompt="Inspect app.py", state=state, client=client)
    assert result["ok"]
    output = sink.getvalue().replace("\u2013", "-")
    for line in ("Inspecting the module.", "app.py:L2-2", "Read lines 2-2 of 2", "Analyzing Read(app.py:L2-2) output", "Read complete."):
        assert line in output


def test_unavailable_or_clipped_viewport_cannot_pass():
    report = {"ok": True, "viewports": [{"audit": {"clipped_texts": ["h1"]}, "pixels": {"ok": True}}]}
    assert visual.format_visual_report(report).endswith("Visual checks: fail")
    report["viewports"] = [{"audit": {}, "pixels": {"ok": False}}]
    assert visual.format_visual_report(report).endswith("Visual checks: fail")


def test_failed_cdp_start_releases_profile_and_process(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(visual.tempfile, "mkdtemp", lambda **kwargs: str(profile))
    proc = Mock()
    monkeypatch.setattr(visual.subprocess, "Popen", lambda *a, **kw: proc)
    client = visual._HeadlessCDPClient("test-browser")
    monkeypatch.setattr(client, "_wait_for_debug_port", Mock(side_effect=RuntimeError("startup failed")))
    with pytest.raises(RuntimeError, match="startup failed"), client:
        pytest.fail("Unexpectedly entered CDP")
    assert not profile.exists()
    proc.terminate.assert_called_once()


@pytest.mark.parametrize(("verdict", "passed"), [({"pass": False, "defects": ["clipped heading"]}, False),
                                                ({"pass": True, "defects": []}, True)])
def test_visual_critic_verdict_controls_worker_evidence(tmp_path, verdict, passed):
    state = SessionState(project_root=tmp_path, selected_model="deepseek")
    state.providers_info = {"gemini": {"has_tab": True}}
    client = Mock()
    calls = []

    def stream(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "gemini":
            album = kwargs["messages"][-1]["content"]
            assert [p["image_url"]["url"] for p in album if "image_url" in p] == ["data:image/png;base64,YQ=="]
            return {"content": json.dumps(verdict)}
        if len(calls) == 1:
            return {"tool_calls": [{"id": "v", "function": {"name": "visual_inspect", "arguments": "{}"}}]}
        return {"content": "Reviewed", "tool_calls": []}

    photos = [{"url": "data:image/png;base64,YQ==", "source_tool": "visual_inspect"},
              {"url": "data:image/png;base64,Yg==", "source_tool": "inspect_image"}]
    client.stream.side_effect = stream
    with patch.object(state, "auto_checkpoint"), \
         patch.object(agent, "consume_pending_images", side_effect=[[], photos]), \
         patch("wsa_agent.tools._visual_inspect", return_value="[OK] Captured\nVisual checks: pass"):
        result = agent.execute_agent_loop("Review layout", state, client, quiet_ui=True)
    assert calls == ["deepseek", "gemini", "deepseek"]
    assert result["ok"] is passed
    assert result["browser_verified"] is passed


@pytest.mark.parametrize("worker", ["deepseek", "venice"])
@pytest.mark.parametrize("available", [(), ("claude",), ("claude", "gemini")])
def test_worker_records_review_only_after_a_reviewer_answers(tmp_path, worker, available):
    state = SessionState(project_root=tmp_path, selected_model=worker)
    state.providers_info = {name: {"has_tab": True} for name in available}
    reviews = []
    client = Mock()

    def stream(**request):
        model = request["model"]
        if model != worker:
            reviews.append(model)
            assert request["relay_id"] == "worker-browser"
            assert request["session_id"] == "worker-session:role:visual_critic"
            assert request["use_tools"] is False
            return {"content": "invalid" if model == "claude" else '{"pass":true,"defects":[]}'}
        if request["messages"][-1]["role"] == "user" and request["messages"][-1]["content"] == "Inspect layout":
            return {"tool_calls": [{"id": "v", "function": {"name": "visual_inspect", "arguments": "{}"}}]}
        return {"content": "Inspection finished", "tool_calls": []}

    client.stream.side_effect = stream
    photo = {"url": "data:image/png;base64,YQ==", "source_tool": "visual_inspect"}
    with patch.object(agent, "consume_pending_images", side_effect=[[], [photo]]), \
         patch("wsa_agent.tools._visual_inspect", return_value="[OK] Captured\nVisual checks: pass"):
        result = agent.execute_agent_loop("Inspect layout", state, client, role="verifier",
                                          session_id_override="worker-session", relay_id_override="worker-browser",
                                          persist_to_state=False, quiet_ui=True)
    assert reviews == list(available)
    passed = "gemini" in available
    assert result["ok"] is passed
    assert result["browser_verified"] is passed
    evidence, = result["verification_evidence"]
    assert evidence["result_class"] == ("ok" if passed else "review_unavailable")
    assert evidence.get("reviewer") == ("gemini" if passed else None)


def test_visual_baselines_and_interactions_use_actual_viewport(tmp_path, monkeypatch):
    monkeypatch.setattr(visual, "_PREVIOUS_CAPTURES", {})
    with patch.object(visual, "_HeadlessCDPClient") as constructor, \
         patch.object(visual, "analyze_screenshot_pixels", return_value={"ok": True}), \
         patch.object(visual, "compose_contact_sheet", return_value=(b"sheet", "image/png", 1, 1)), \
         patch.object(visual, "build_pixel_diff_tile", return_value=(b"diff", 0.0)) as diff:
        cdp = constructor.return_value.__enter__.return_value
        cdp.screenshot_png.return_value = b"capture"
        def capture(width, theme):
            result = visual.capture_visual_inspection(
                "http://localhost/", project_root=tmp_path, browser_bin="test-browser",
                viewports=[{"width": width, "height": 400}], color_scheme=theme,
                actions=[{"type": "wait", "ms": 1}], annotate=False,
            )
            assert result["ok"], result
            return result["diff_pct"]
        assert capture(200, "light") is None
        assert cdp.set_viewport.call_args.args == (240, 400)
        assert cdp.set_viewport.call_args.kwargs == {"mobile": True}
        assert capture(700, "light") is None
        assert capture(240, "dark") is None
        assert capture(240, "light") == 0.0
        diff.assert_called_once()


@pytest.mark.parametrize("kind", ["click", "hover", "drag", "scroll", "type"])
def test_missing_action_target_fails_inspection(kind):
    client = visual._HeadlessCDPClient("test-browser")
    with patch.object(client, "evaluate", return_value=None), pytest.raises(ValueError):
        client.run_actions([{"type": kind, "selector": "#missing", "text": "hello"}])


def test_local_capture_stays_within_project(tmp_path):
    with pytest.raises(ValueError, match="within the project"):
        visual._resolve_target_url(str(tmp_path.parent), tmp_path)
