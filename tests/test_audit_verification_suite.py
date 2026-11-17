"""Comprehensive verification tests for WSA multiagent, orchestrator continuity, and limit recovery."""
import json
import pytest
from unittest.mock import Mock, patch
from pathlib import Path

from wsa_agent import tools, agent, subagents
from wsa_agent.session import SessionState
from wsa_agent.client import ProviderLimitError


def test_orchestrator_stage_reuses_same_chat_in_single_mode(tmp_path):
    """In thread_mode='single', orchestrator stages must reuse the active chat URL."""
    state = SessionState(project_root=tmp_path, thread_mode="single")
    state.set_model("claude")
    state.active_chat_url = "https://claude.ai/chat/abc-123"
    state.chat_urls["claude"] = "https://claude.ai/chat/abc-123"

    # Call _run_role_agent with role="explorer"
    captured_kwargs = {}

    def mock_loop(prompt, st, cl, **kwargs):
        captured_kwargs.update(kwargs)
        return {"ok": True, "final_text": "Recon complete.", "written_files": []}

    with patch("wsa_agent.subagents.execute_agent_loop", side_effect=mock_loop):
        res = subagents._run_role_agent(role="explorer", prompt="Explore workspace", state=state, client=Mock())

    assert res["ok"]
    # chat_url_override must be the active chat URL, NOT empty!
    assert captured_kwargs.get("chat_url_override") == "https://claude.ai/chat/abc-123"
    assert captured_kwargs.get("session_id_override") == state.session_id


def test_multiagent_failover_transfers_context_and_compacts(tmp_path):
    """When an agent hits limit in multiagent mode, context transfers to next agent and is compacted."""
    state = SessionState(project_root=tmp_path)
    state.set_model("claude")
    state.agent_mode = "multiagent"
    state.multiagent_queue = ["claude", "gemini"]
    state.multiagent_index = 0

    # Mock client where Claude raises ProviderLimitError, Gemini succeeds
    calls = []

    class MockClient:
        api_base = "http://127.0.0.1:8081/v1"
        api_key = ""

        def get_health(self):
            return {"active_relay": "test", "providers": {}}

        def clear_session_chats(self, *args, **kwargs):
            pass

        def stream(self, **kwargs):
            m = kwargs.get("model")
            calls.append(m)
            if m == "claude":
                raise ProviderLimitError("Claude hourly message limit reached")
            return {
                "content": "Gemini picked up workflow seamlessly.",
                "tool_calls": [],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                "upstream_model": {"chat_url": "https://gemini.google.com/app/xyz-789"},
            }

    client = MockClient()
    previous = tools.get_project_root()
    tools.set_project_root(tmp_path)
    try:
        with patch.object(state, "auto_checkpoint"):
            res = agent.execute_agent_loop(
                "Build full application",
                state,
                client,
                quiet_ui=True,
                max_rounds=5,
            )
    finally:
        tools.set_project_root(previous)

    assert res["ok"]
    assert "claude" in calls
    assert "gemini" in calls
    assert state.model == "gemini"
    assert state.chat_urls.get("gemini") == "https://gemini.google.com/app/xyz-789"


def test_singleagent_account_change_clears_old_chat_url(tmp_path):
    """When singleagent account switch is confirmed, old chat URL is cleared and not reused."""
    state = SessionState(project_root=tmp_path)
    state.set_model("claude")
    state.agent_mode = "singleagent"
    state.active_chat_url = "https://claude.ai/chat/old-account-chat"
    state.chat_urls["claude"] = "https://claude.ai/chat/old-account-chat"

    cleared_urls = []

    class MockClient:
        api_base = "http://127.0.0.1:8081/v1"
        api_key = ""

        def get_health(self):
            return {"active_relay": "test", "providers": {}}

        def clear_session_chats(self, session_id, providers, include_roles, client_urls):
            cleared_urls.extend(client_urls)

    client = MockClient()
    agent._reset_provider_chats_for_account_change(
        state,
        client,
        state.session_id,
        ["claude"],
        clear_roles=True,
        refresh_health=False,
    )

    # Old chat URL must be gone from client state
    assert not state.active_chat_url
    assert "claude" not in state.chat_urls
    # Old URL must be reported as cleared to server
    assert "https://claude.ai/chat/old-account-chat" in cleared_urls


def test_judge_workflow_reuses_same_chat_in_single_and_multiagent(tmp_path):
    """Workflow judgment must never open a new chat; it must reuse the active chat URL."""
    from wsa_agent.context import classify_task_complexity

    # 1. Verify heuristic classifier catches user's extend/research prompt
    user_prompt = "now do some more things, explain me what did you make and extend the more features in it, do more detailed research and extend it"
    complexity = classify_task_complexity(user_prompt)
    assert complexity["mode"] == "orchestrate"
    assert complexity["score"] >= 4

    # 2. Verify singleagent mode reuses active chat URL
    state = SessionState(project_root=tmp_path)
    state.set_model("claude")
    state.agent_mode = "singleagent"
    state.active_chat_url = "https://claude.ai/chat/singleagent-thread-123"
    state.chat_urls["claude"] = "https://claude.ai/chat/singleagent-thread-123"

    recorded_kwargs = {}

    class MockClient:
        def stream(self, **kwargs):
            recorded_kwargs.update(kwargs)
            return {
                "content": '{"mode": "orchestrate", "reason": "extend features and research"}',
                "upstream_model": {"chat_url": "https://claude.ai/chat/singleagent-thread-123"},
            }

    client = MockClient()
    dec = agent._judge_workflow("ambiguous build prompt", state, client, fallback={"mode": "direct"})
    assert dec["mode"] == "orchestrate"
    assert recorded_kwargs.get("session_id") == f"{state.session_id}:role:workflow_router"
    assert recorded_kwargs.get("chat_url") == "https://claude.ai/chat/singleagent-thread-123"
    assert recorded_kwargs.get("new_chat") is False

    # 3. Verify multiagent mode reuses active chat URL
    state.agent_mode = "multiagent"
    state.multiagent_queue = ["claude", "gemini"]
    state.multiagent_index = 0
    state.active_chat_url = "https://claude.ai/chat/multiagent-thread-456"
    state.chat_urls["claude"] = "https://claude.ai/chat/multiagent-thread-456"

    recorded_kwargs.clear()
    dec = agent._judge_workflow("another prompt", state, client, fallback={"mode": "direct"})
    assert dec["mode"] == "orchestrate"
    assert recorded_kwargs.get("session_id") == f"{state.session_id}:role:workflow_router"
    assert recorded_kwargs.get("chat_url") == "https://claude.ai/chat/multiagent-thread-456"
    assert recorded_kwargs.get("new_chat") is False

    # 4. Verify turn 0 binds fresh chat_url to state
    state.active_chat_url = ""
    state.chat_urls.clear()
    recorded_kwargs.clear()

    class FreshChatClient:
        def stream(self, **kwargs):
            recorded_kwargs.update(kwargs)
            return {
                "content": '{"mode": "direct", "reason": "quick query"}',
                "upstream_model": {"chat_url": "https://claude.ai/chat/freshly-created-thread"},
            }

    fresh_client = FreshChatClient()
    dec = agent._judge_workflow("first prompt", state, fresh_client, fallback={"mode": "direct"})
    assert dec["mode"] == "direct"
    assert recorded_kwargs.get("session_id") == f"{state.session_id}:role:workflow_router"
    assert recorded_kwargs.get("chat_url") is None
    assert recorded_kwargs.get("new_chat") is False
    assert state.active_chat_url == "https://claude.ai/chat/freshly-created-thread"
    assert state.chat_urls.get("claude") == "https://claude.ai/chat/freshly-created-thread"

