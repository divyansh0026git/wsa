"""
Tests verifying deep cleaning of junk message transfer and elimination of premature tool output trimming.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wsa_agent import agent, client as cli, prompts, session, tools
from wsa_server import multimodal as mm, relay


def test_delta_prompt_does_not_echo_assistant_messages():
    """Assistant prose and tool_calls must not be re-echoed to the assistant in delta prompts."""
    delta_msgs = [
        {"role": "assistant", "content": "I will inspect the file now.", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "1 | hello world\n2 | second line\n"},
    ]
    full_msgs = [
        {"role": "user", "content": "Inspect a.txt"},
        *delta_msgs,
    ]
    prompt, images = mm.messages_to_delta_prompt(
        delta_msgs,
        full_msgs,
        tools=tools.TOOL_DEFINITIONS,
        provider="claude",
    )
    # The tool result must be present
    assert "hello world" in prompt
    assert "second line" in prompt
    assert "Local output returned by my `wsa` CLI for `read_file`:" in prompt
    # The assistant's own prose and tool call must NOT be echoed
    assert "I will inspect the file now." not in prompt
    assert "[Assistant]:" not in prompt


def test_delta_prompt_preserves_full_tool_output_untrimmed():
    """A realistic 150-line code read must pass through delta formatting completely untrimmed."""
    code_lines = [f"  {i:>4} | const vWorld{i} = modelMatrix * vec4(position, 1.0);" for i in range(1, 151)]
    full_tool_text = "\n".join(code_lines) + "\n[Showing lines 1-150 of 150 (6,200 bytes)]"

    delta_msgs = [
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": full_tool_text},
    ]
    full_msgs = [
        {"role": "user", "content": "Read the shader file"},
        *delta_msgs,
    ]
    for provider in ("claude", "gemini", "chatgpt"):
        prompt, _ = mm.messages_to_delta_prompt(
            delta_msgs,
            full_msgs,
            tools=tools.TOOL_DEFINITIONS,
            provider=provider,
        )
        assert "vWorld1 =" in prompt
        assert "vWorld75 =" in prompt
        assert "vWorld150 =" in prompt
        assert "[Compacted" not in prompt
        assert "omitted" not in prompt
        assert "trimmed for" not in prompt


def test_completed_prior_tools_are_summarized_not_dumped_raw():
    """Completed prior tool outputs in Turn 0/fresh chat prompts should not bloat context with thousands of raw chars."""
    huge_prior_dump = "\n".join(f"line {i}: content {i}" for i in range(1, 500))
    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "Prior request (already handled)"},
        {"role": "assistant", "content": "Prior assistant note"},
        {"role": "tool", "name": "read_file", "content": huge_prior_dump},
        {"role": "user", "content": "Active request needing work"},
        {"role": "tool", "name": "tsc", "content": "tsc exit=0"},
    ]
    # In Claude
    claude_prompt, _ = mm.messages_to_prompt(messages, tools=tools.TOOL_DEFINITIONS, provider="claude")
    assert "Active request needing work" in claude_prompt
    assert "tsc exit=0" in claude_prompt
    assert "Prior request (already handled)" in claude_prompt
    # The huge raw content from the prior turn should NOT be dumped raw
    assert "line 250: content 250" not in claude_prompt
    assert "[read_file output (" in claude_prompt

    # In Gemini / other
    gemini_prompt, _ = mm.messages_to_prompt(messages, tools=tools.TOOL_DEFINITIONS, provider="gemini")
    assert "Active request needing work" in gemini_prompt
    assert "tsc exit=0" in gemini_prompt
    assert "line 250: content 250" not in gemini_prompt
    assert "[read_file output (" in gemini_prompt


def test_effective_chat_url_is_captured_from_stream_response():
    """chat_url returned from stream response (direct or in upstream_model) properly populates effective_chat_url."""
    st = session.SessionState()
    client = MagicMock()
    # Mock client.stream returning chat_url directly
    client.stream.return_value = {
        "content": "Done with task.",
        "thinking": None,
        "tool_calls": [],
        "finish_reason": "stop",
        "chat_url": "https://claude.ai/chat/abc-123-def",
        "upstream_model": {"badge": "Claude 3.5 Sonnet", "chat_url": "https://claude.ai/chat/abc-123-def"},
    }
    msgs = [{"role": "user", "content": "Hello"}]
    res = agent._call_client_stream(client, {"messages": msgs})
    assert res.get("chat_url") == "https://claude.ai/chat/abc-123-def"


def test_visual_directive_avoids_art_director_saturn_toy_critique_loop():
    """Visual directive text must not include the hallucinatory 'Saturn-toy' or loop-forcing text."""
    # Check SYSTEM_PROMPT and agent prompts
    assert "Saturn-toy" not in prompts.SYSTEM_PROMPT
    assert "ruthless senior Awwwards" not in prompts.SYSTEM_PROMPT
    assert "Saturn-toy" not in prompts.CLAUDE_SYSTEM_PROMPT


def test_visual_diff_never_injects_neon_pink_missing_texture_artifacts():
    """Primary contact sheet must never stitch diff heatmaps, only clean real viewports."""
    import io
    from PIL import Image
    from wsa_agent import visual

    # Test capture_visual_inspection mock: ensure tiles for contact sheet contains NO diff tile
    fake_cdp = MagicMock()
    fake_cdp.console_errors = []
    fake_cdp.page_exceptions = []
    fake_cdp.failed_requests = []
    fake_cdp.evaluate.return_value = {"viewport": {"width": 1440, "height": 900}, "marks": []}
    
    clean_im = Image.new("RGB", (400, 300), (10, 15, 25))
    buf = io.BytesIO()
    clean_im.save(buf, format="PNG")
    clean_bytes = buf.getvalue()

    fake_cdp.screenshot_png.return_value = clean_bytes
    fake_cdp.run_actions.return_value = []
    fake_cdp.eval_results = []

    with patch.object(visual, "_HeadlessCDPClient", return_value=fake_cdp):
        with patch.object(visual, "find_chromium_binary", return_value="fake_chrome.exe"):
            res = visual.capture_visual_inspection(
                "http://127.0.0.1:5173",
                project_root=Path("."),
                include_diff=True,
            )
            assert res["ok"] is True
            # Contact sheet tiles must contain ONLY real viewports, never PIXEL DIFF
            for tile_lbl in res["tiles"]:
                assert "PIXEL DIFF" not in tile_lbl, f"Synthetic diff leaked into primary sheet tiles: {tile_lbl}"
            assert len(res["tiles"]) == 1
            assert "DESKTOP" in res["tiles"][0]

