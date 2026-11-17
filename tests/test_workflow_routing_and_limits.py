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


@pytest.mark.parametrize("answer, expected", [("c", "single"), ("d", "stop")])
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
