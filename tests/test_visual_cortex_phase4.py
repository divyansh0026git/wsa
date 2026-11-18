"""Phase 4 verification: Vision-aware Visual Critic routing, Verifier allowlist, and UI verification gate."""
from __future__ import annotations

import json

import pytest

from wsa_agent import agent, session, subagents, tools, visual
from wsa_agent.session import SessionState


def test_vision_capabilities_and_critic_selection(tmp_path):
    assert session.provider_supports_vision("claude") is True
    assert session.provider_supports_vision("gemini") is True
    assert session.provider_supports_vision("chatgpt") is True
    assert session.provider_supports_vision("deepseek") is False
    assert session.provider_supports_vision("venice") is False

    st = SessionState(project_root=tmp_path, selected_model="deepseek")
    st.providers_info = {
        "deepseek": {"has_tab": True},
        "chatgpt": {"has_tab": True},
        "gemini": {"has_tab": True},
    }
    assert session.pick_vision_critic_provider(st, exclude="deepseek") == "gemini"
    st.providers_info["claude"] = {"has_tab": True}
    assert session.pick_vision_critic_provider(st, exclude="deepseek") == "claude"
    assert "visual_inspect" in subagents.CODER_TOOLS
    assert "visual_inspect" in subagents.VERIFIER_TOOLS


def test_deepseek_coder_delegates_visual_critique_to_synced_vision_tab(tmp_path):
    if not visual.find_chromium_binary():
        pytest.skip("No Chromium binary available")

    (tmp_path / "index.html").write_text(
        "<!doctype html><html><head><meta name='viewport' content='width=device-width, initial-scale=1'></head>"
        "<body style='margin:0;background:#141824;color:#fff;padding:24px;'>"
        "<h1>Product Card</h1><button id='buy'>Buy Now</button></body></html>",
        encoding="utf-8",
    )
    st = SessionState(project_root=tmp_path, selected_model="deepseek", permission_mode="smart")
    st.providers_info = {
        "deepseek": {"has_tab": True},
        "gemini": {"has_tab": True},
    }

    calls = []

    class HybridClient:
        def stream(self, messages, model, use_tools=True, **kwargs):
            calls.append({"model": model, "use_tools": use_tools, "messages": list(messages)})
            if model == "gemini" and use_tools is False:
                # Verify the Visual Critic actually received the contact sheet image_url
                user_content = messages[-1]["content"]
                assert isinstance(user_content, list)
                assert any(isinstance(p, dict) and p.get("type") == "image_url" for p in user_content)
                return {
                    "content": json.dumps({
                        "pass": True,
                        "defects": [],
                        "summary": "Header and Buy Now button render cleanly with high contrast.",
                    }),
                    "tool_calls": [],
                }
            ds_calls = [c for c in calls if c["model"] == "deepseek"]
            if len(ds_calls) == 1:
                return {
                    "content": "Inspecting rendered UI via visual_inspect.",
                    "tool_calls": [{
                        "id": "call_vis_1",
                        "type": "function",
                        "function": {
                            "name": "visual_inspect",
                            "arguments": json.dumps({"target": "index.html", "include_diff": False, "wait_ms": 200}),
                        },
                    }],
                }
            return {"content": "Verified via Visual Critic: layout and button render cleanly.", "tool_calls": []}

    res = agent.execute_agent_loop("Verify index.html visually", st, HybridClient(), quiet_ui=True, enforce_verify_gate=False)
    assert res["ok"]
    assert res["browser_verified"] is True
    assert any(c["model"] == "gemini" and c["use_tools"] is False for c in calls)
    ds_round2 = [c for c in calls if c["model"] == "deepseek"][1]
    joined_r2 = json.dumps(ds_round2["messages"])
    assert "Visual Critic via Gemini Web" in joined_r2
    assert "Header and Buy Now button render cleanly" in joined_r2


def test_claude_direct_visual_inspect_attaches_image_and_satisfies_ui_gate(tmp_path):
    if not visual.find_chromium_binary():
        pytest.skip("No Chromium binary available")

    st = SessionState(project_root=tmp_path, selected_model="claude", permission_mode="smart")
    calls = []

    class ClaudeVisionClient:
        def stream(self, messages, model, use_tools=True, **kwargs):
            calls.append({"model": model, "messages": list(messages)})
            if len(calls) == 1:
                return {
                    "content": "Creating index.html.",
                    "tool_calls": [{
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({
                                "path": "index.html",
                                "content": (
                                    "<!doctype html><html><head><meta name='viewport' content='width=device-width, initial-scale=1'></head>"
                                    "<body style='margin:0;background:#10141f;color:#fff;padding:24px;'><h1>Live UI</h1><button>Go</button></body></html>"
                                ),
                            }),
                        },
                    }],
                }
            if len(calls) == 2:
                return {
                    "content": "Running visual_inspect.",
                    "tool_calls": [{
                        "id": "c2",
                        "type": "function",
                        "function": {
                            "name": "visual_inspect",
                            "arguments": json.dumps({"target": "index.html", "include_diff": False, "wait_ms": 200}),
                        },
                    }],
                }
            # Round 3 must directly contain the multimodal image_url part for Claude!
            has_img = any(
                isinstance(m.get("content"), list)
                and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
                for m in messages
            )
            assert has_img
            return {"content": "Verified rendered desktop and mobile contact sheet cleanly.", "tool_calls": []}

    res = agent.execute_agent_loop("Create and verify index.html", st, ClaudeVisionClient(), quiet_ui=True)
    assert res["ok"]
    assert res["browser_verified"] is True
    assert any(ev.get("tool") == "visual_inspect" and ev.get("ok") is True for ev in res["verification_evidence"])
