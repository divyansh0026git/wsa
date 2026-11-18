from unittest.mock import patch

import pytest

from wsa_agent import agent, context, subagents
from wsa_agent.client import ProviderLimitError
from wsa_agent.session import SessionState
from unittest.mock import Mock


@pytest.mark.parametrize("prompt", ["try again last call", "retry the last call", "resume the workflow"])
def test_recovery_prompt_keeps_existing_workflow(tmp_path, prompt):
    state = SessionState(project_root=tmp_path, selected_model="chatgpt")
    state.chat_urls["chatgpt"] = "https://chatgpt.com/c/existing"
    with patch.object(agent, "_judge_workflow") as judge, patch.object(agent, "execute_agent_loop", return_value={"ok": True}) as execute:
        agent.run_turn(prompt, state, Mock())
    judge.assert_not_called()
    execute.assert_called_once()
    assert state.active_chat_url == "https://chatgpt.com/c/existing"


@pytest.mark.parametrize("mode", ["direct", "orchestrate"])
def test_ai_judgment_routes_without_executing_tools(tmp_path, mode):
    state = SessionState(project_root=tmp_path)
    client = Mock()
    client.stream.return_value = {"content": '{"mode":"' + mode + '","reason":"scope"}'}
    with patch.object(agent, "execute_agent_loop", return_value={"ok": True}) as direct, patch.object(subagents, "run_orchestrated_turn", return_value={"ok": True}) as team:
        assert agent.run_turn("I want a cozy place online to sell my pottery", state, client)["ok"]
    assert client.stream.call_args.kwargs["use_tools"] is False
    assert direct.called == (mode == "direct")
    assert team.called == (mode == "orchestrate")
    assert not state.messages


def test_judgment_invalid_output_falls_back(tmp_path):
    state = SessionState(project_root=tmp_path)
    client = Mock()
    client.stream.return_value = {"content": "not json", "tool_calls": [{"name": "delete_file"}]}
    fallback = {"mode": "direct"}
    assert agent._judge_workflow("task", state, client, fallback) is fallback


def test_judgment_limit_stop_does_not_execute(tmp_path):
    state = SessionState(project_root=tmp_path)
    client = Mock()
    client.stream.side_effect = ProviderLimitError("limit", provider=state.model)
    with patch.object(subagents, "_recover_orchestrated_stage_limit", return_value=False), patch.object(agent, "execute_agent_loop") as execute:
        result = agent.run_turn("Create a shop for my pottery", state, client)
    assert result["limit_exhausted"] and not execute.called


@pytest.mark.parametrize("prompt", [
    "Build me a website for my bakery with online ordering.",
    "Create a game where players explore a mysterious island.",
    "I want a website for my bakery.",
    "Investigate and fix these complex bugs across the entire project.",
    "Develop a feature for accounts with detailed validation and tests.",
])
def test_natural_language_builds_route_to_team(prompt):
    assert context.classify_task_complexity(prompt)["mode"] == "orchestrate"


def test_explicit_team_off_and_simple_request_remain_direct(tmp_path):
    assert context.classify_task_complexity("Fix a typo")["mode"] == "direct"
    assert context.classify_task_complexity("Create a README file in this project")["mode"] == "direct"
    assert context.classify_task_complexity("Explain how to build a website for my bakery")["mode"] == "direct"
    assert context.classify_task_complexity("Build a website", SessionState(project_root=tmp_path, team_mode="off"))["mode"] == "direct"


@pytest.mark.parametrize("answer, expected", [("c", "single"), ("e", "stop")])
def test_limit_menu_single_and_stop(tmp_path, answer, expected):
    state = SessionState(project_root=tmp_path, permission_mode="auto", auto_approve=True)
    with patch.object(agent.R.console, "input", return_value=answer) as ask:
        assert agent._offer_limit_multiagent(state, object(), state.model) == expected
    ask.assert_called_once()


def test_account_choice_is_not_bypassed(tmp_path):
    state = SessionState(project_root=tmp_path, permission_mode="auto", auto_approve=True)
    with patch.object(agent.R.console, "input", return_value="n") as ask:
        assert not agent._prompt_all_agents_exhausted(state, is_multiagent=False)
    ask.assert_called_once()


@pytest.mark.parametrize("answer, selection", [("a", "default"), ("b", "custom")])
@pytest.mark.parametrize("queue", [["claude", "gemini"], ["gemini", "claude"]])
def test_limit_menu_activates_selected_queue(tmp_path, answer, selection, queue):
    state = SessionState(project_root=tmp_path, selected_model="claude")
    def configure(arg, state, client):
        assert arg == selection
        state.configure_multiagent(queue)
    with patch.object(agent.R.console, "input", return_value=answer), patch("wsa_agent.commands._cmd_multiagent", side_effect=configure), patch.object(state, "save", return_value=""):
        assert agent._offer_limit_multiagent(state, object(), "claude") == "switch"
    assert state.model == "gemini" and state.multiagent_index == 0
    assert state.multiagent_queue == ["gemini"]
    assert state.advance_multiagent_queue("gemini") is None


def test_limit_switch_resumes_with_context_in_fresh_chat(tmp_path):
    state = SessionState(project_root=tmp_path, selected_model="claude")
    calls = []
    class Client:
        def stream(self, messages, model, **kwargs):
            calls.append((model, list(messages), kwargs))
            if len(calls) == 1:
                raise ProviderLimitError("limit", provider="claude")
            return {"content": "done", "tool_calls": []}
    def choose(state, client, failed):
        state.configure_multiagent(["claude", "gemini"])
        state.multiagent_index = 1
        state.set_model("gemini")
        return "switch"
    with patch.object(agent, "_offer_limit_multiagent", side_effect=choose), patch.object(state, "save", return_value=""):
        result = agent.execute_agent_loop("Build a project with context", state, Client(), quiet_ui=False)
    assert result["ok"]
    assert calls[1][0] == "gemini" and calls[1][2]["new_chat"]
    assert "Build a project with context" in str(calls[1][1])


def test_orchestrated_limit_switch_retries_stage(tmp_path):
    state = SessionState(project_root=tmp_path, selected_model="claude")
    def choose(state, client, failed):
        state.configure_multiagent(["gemini", "claude"])
        return "switch"
    with patch.object(subagents, "_offer_limit_multiagent", side_effect=choose), patch.object(state, "save", return_value=""), patch.object(subagents, "_reset_provider_chats_for_account_change") as reset:
        assert subagents._recover_orchestrated_stage_limit(state, object(), {"limit_exhausted": True}, pending_goal="build")
    assert state.model == "gemini"
    assert reset.call_args.args[3] == ["gemini"]


def test_orchestrated_stage_limit_switch_model_singleagent(tmp_path):
    state = SessionState(project_root=tmp_path, selected_model="claude")
    state.agent_mode = "singleagent"
    def choose(state, client, failed):
        state.configure_singleagent("gemini")
        return "switch"
    with patch.object(subagents, "_offer_limit_multiagent", side_effect=choose), patch.object(state, "save", return_value=""), patch.object(subagents, "_reset_provider_chats_for_account_change") as reset:
        assert subagents._recover_orchestrated_stage_limit(state, object(), {"limit_exhausted": True}, pending_goal="build")
    assert state.model == "gemini"
    assert state.agent_mode == "singleagent"
    assert state.multiagent_queue == []
    assert reset.call_args.args[3] == ["gemini"]


def test_limit_menu_renders_all_five_options_without_rich_conceal(tmp_path):
    from io import StringIO
    from rich.console import Console
    from wsa_agent import renderer

    state = SessionState(project_root=tmp_path, selected_model="claude")
    buf = StringIO()
    test_console = Console(file=buf, force_terminal=True, width=100)
    with patch.object(renderer, "console", test_console), patch.object(test_console, "input", return_value="e"):
        assert agent._offer_limit_multiagent(state, object(), "claude") == "stop"

    raw_ansi = buf.getvalue()
    # Rich's [c] tag emits ANSI SGR 8 (conceal); escaped \[c] must never emit it.
    assert "\x1b[8m" not in raw_ansi
    plain = Console(file=StringIO(), force_terminal=False, width=100)
    with patch.object(renderer, "console", plain), patch.object(plain, "input", return_value="[c]"):
        assert agent._offer_limit_multiagent(state, object(), "claude") == "single"
    out = plain.file.getvalue()
    for expected in (
        "[a] Default queue",
        "[b] Custom queue",
        "[c] Single agent",
        "[d] Switch model",
        "[e] Stop",
    ):
        assert expected in out


def test_limit_menu_switch_model_singleagent(tmp_path):
    state = SessionState(project_root=tmp_path, selected_model="claude")
    state.agent_mode = "singleagent"
    with patch.object(agent.R.console, "input", side_effect=["d", "gemini"]), patch.object(state, "save", return_value=""):
        res = agent._offer_limit_multiagent(state, object(), "claude")
    assert res == "switch"
    assert state.model == "gemini"
    assert state.agent_mode == "singleagent"
    assert state.multiagent_queue == []


def test_limit_menu_switch_model_shortcut(tmp_path):
    state = SessionState(project_root=tmp_path, selected_model="claude")
    with patch.object(agent.R.console, "input", return_value="d gemini"), patch.object(state, "save", return_value=""):
        res = agent._offer_limit_multiagent(state, object(), "claude")
    assert res == "switch"
    assert state.model == "gemini"
    assert state.agent_mode == "singleagent"


def test_limit_menu_switch_model_cancel_returns_to_menu(tmp_path):
    state = SessionState(project_root=tmp_path, selected_model="claude")
    # First choose 'd', cancel with empty Enter, then choose 'e' (stop)
    with patch.object(agent.R.console, "input", side_effect=["d", "", "e"]):
        res = agent._offer_limit_multiagent(state, object(), "claude")
    assert res == "stop"


def test_singleagent_model_switch_repeats_on_subsequent_limits(tmp_path):
    state = SessionState(project_root=tmp_path, selected_model="claude")
    calls = []

    class Client:
        def stream(self, messages, model, **kwargs):
            calls.append((model, list(messages), kwargs))
            if len(calls) == 1:
                raise ProviderLimitError("limit claude", provider="claude")
            if len(calls) == 2:
                raise ProviderLimitError("limit gemini", provider="gemini")
            return {"content": "completed on chatgpt", "tool_calls": []}

    # Recovery 1: claude limit -> user switches to gemini (singleagent)
    # Recovery 2: gemini limit -> user switches to chatgpt (singleagent)
    with patch.object(agent.R.console, "input", side_effect=["d gemini", "d chatgpt"]), patch.object(state, "save", return_value=""):
        result = agent.execute_agent_loop("Build multi-stage task", state, Client(), quiet_ui=False)

    assert result["ok"]
    assert len(calls) == 3
    assert calls[0][0] == "claude"
    assert calls[1][0] == "gemini"
    assert calls[2][0] == "chatgpt"
    assert state.model == "chatgpt"
    assert state.agent_mode == "singleagent"
    assert state.multiagent_queue == []


def test_yes_no_prompts_define_explicit_options(tmp_path):
    from io import StringIO
    from rich.console import Console
    from wsa_agent import renderer

    state = SessionState(project_root=tmp_path, selected_model="claude")
    plain = Console(file=StringIO(), force_terminal=False, width=120)
    with patch.object(renderer, "console", plain), patch.object(plain, "input", side_effect=["maybe", "y"]):
        assert agent._prompt_all_agents_exhausted(state, model_name="claude", is_multiagent=False) is True
    out = plain.file.getvalue()
    assert "[y] Yes" in out and "resume automatically" in out
    assert "[n] No" in out and "stop now" in out
    assert "Choose y (yes) or n (no)." in out

    # Multiagent exhaustion also defines explicit [y]/[n] options and honors "n" under auto-approve.
    state_multi = SessionState(
        project_root=tmp_path,
        selected_model="claude",
        permission_mode="auto",
        auto_approve=True,
    )
    state_multi.configure_multiagent(["claude", "gemini"])
    plain2 = Console(file=StringIO(), force_terminal=False, width=120)
    with patch.object(renderer, "console", plain2), patch.object(plain2, "input", return_value="n"):
        assert agent._prompt_all_agents_exhausted(state_multi, is_multiagent=True) is False
    out2 = plain2.file.getvalue()
    assert "All queued agents reached their account limits" in out2
    assert "[y] Yes" in out2 and "[n] No" in out2
    assert "already preserved" in out2

    # End-to-end stop path under permission bypass: prompts are not bypassed and checkpoint is auto-saved for resume.
    class LimitClient:
        def stream(self, *args, **kwargs):
            raise ProviderLimitError("limit", provider="claude")

    stop_state = SessionState(
        project_root=tmp_path,
        selected_model="claude",
        permission_mode="auto",
        auto_approve=True,
    )
    stop_console = Console(file=StringIO(), force_terminal=False, width=120)
    with patch.object(renderer, "console", stop_console), \
         patch.object(stop_console, "input", side_effect=["c", "n"]) as ask, \
         patch.object(stop_state, "save", return_value=str(tmp_path / "ckpt.json")) as save_ckpt:
        res = agent.execute_agent_loop("Finish feature", stop_state, LimitClient(), quiet_ui=False)
    assert res["ok"] is False
    assert res["limit_exhausted"] is True
    assert ask.call_count == 2  # Both limit menu ([c]) and account-switch prompt ([n]) shown despite auto_approve.
    assert save_ckpt.call_count >= 1
    assert getattr(stop_state, "_force_new_chat_next", False) is True
    assert "Context preserved, stopping workflow" in stop_console.file.getvalue()


def test_subagent_role_uses_live_renderer_and_prints_read_line_range(tmp_path):
    from io import StringIO
    from rich.console import Console
    from wsa_agent import renderer

    sample = tmp_path / "sample.py"
    sample.write_text("line_one = 1\nline_two = 2\n", encoding="utf-8")
    state = SessionState(project_root=tmp_path, selected_model="claude", permission_mode="smart")

    created_renderers = []

    class TrackingRenderer(renderer.StreamRenderer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.started = False
            self.thought_chunks = []
            created_renderers.append(self)

        def start_thinking(self) -> None:
            self.started = True
            super().start_thinking()

        def feed_thinking(self, token: str) -> None:
            self.thought_chunks.append(token)
            super().feed_thinking(token)

    class TwoRoundClient:
        def __init__(self):
            self.calls = 0

        def stream(self, messages, model, on_token=None, on_thinking=None, **kwargs):
            self.calls += 1
            if on_thinking:
                on_thinking("Inspecting sample.py lines before editing.")
            if self.calls == 1:
                return {
                    "content": "Reading sample.py now.",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "sample.py", "start_line": 1, "end_line": 2}',
                        },
                    }],
                }
            if on_token:
                on_token("Checked sample.py cleanly.")
            return {"content": "Checked sample.py cleanly.", "tool_calls": []}

    buf = StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120)
    with patch.object(renderer, "console", test_console), patch.object(renderer, "StreamRenderer", TrackingRenderer):
        res = subagents._run_role_agent(
            role="coder",
            prompt="Inspect sample.py",
            state=state,
            client=TwoRoundClient(),
            label="coder:t3",
        )

    assert res["ok"]
    assert len(created_renderers) == 2
    assert all(r.started for r in created_renderers)
    assert created_renderers[0]._stage_label == "coder:t3"
    assert "Analyzing Read(sample.py:L1-2)" in created_renderers[1]._status_hint
    out = buf.getvalue()
    assert "coder:t3" in out
    assert "Read(sample.py:L1-2)" in out
    assert "Read lines 1–2 of 2" in out


def test_judge_workflow_runs_live_thinking_spinner_and_closes(tmp_path):
    from wsa_agent import renderer

    state = SessionState(project_root=tmp_path, selected_model="claude")
    events = []

    class FakeJudgeRenderer:
        def __init__(self, **kwargs):
            events.append(("init", kwargs.get("stage_label"), kwargs.get("flush_final")))

        def start_thinking(self):
            events.append(("start",))

        def feed_thinking(self, token):
            events.append(("think", token))

        def set_status_hint(self, hint):
            events.append(("hint", hint))

        def close(self):
            events.append(("close",))

    client = Mock()
    def _fake_stream(**kwargs):
        if kwargs.get("on_thinking"):
            kwargs["on_thinking"]("Evaluating scope")
        return {"content": '{"mode":"direct","reason":"small fix"}'}
    client.stream.side_effect = _fake_stream

    with patch.object(renderer, "StreamRenderer", FakeJudgeRenderer):
        decision = agent._judge_workflow("Fix typo in README", state, client, {"mode": "direct"})

    assert decision["mode"] == "direct"
    assert ("init", "router", False) in events
    assert ("start",) in events
    assert ("think", "Evaluating scope") in events
    assert ("close",) in events
