from pathlib import Path

import pytest

from wsa_agent.prompts import build_dynamic_system_prompt
from wsa_agent.session import SessionState
from wsa_agent.tools import TOOL_DEFINITIONS
from wsa_server.multimodal import messages_to_prompt


@pytest.mark.parametrize("model", ["claude", "chatgpt", "gemini", "deepseek", "venice"])
def test_provider_prompt_keeps_long_run_evidence_and_blocker_guidance(model, tmp_path):
    state = SessionState(project_root=Path(tmp_path))
    assert state.set_model(model)
    prompt = build_dynamic_system_prompt(state)
    assert "```tool_call" in prompt
    assert "verification results" in prompt
    assert "blocker" in prompt
    assert "indefinitely" in prompt
    relay_prompt, _ = messages_to_prompt(
        [{"role": "system", "content": prompt}, {"role": "user", "content": "Implement a small change."}],
        tools=TOOL_DEFINITIONS,
        provider=model,
    )
    assert "verification results" in relay_prompt
    assert "indefinitely" in relay_prompt


def test_chatgpt_prompt_explains_local_tool_relay(tmp_path):
    state = SessionState(project_root=tmp_path)
    state.set_model("chatgpt")
    prompt = build_dynamic_system_prompt(state)
    assert "WSA executes those requests locally" in prompt
    assert "no native browser access" in prompt
