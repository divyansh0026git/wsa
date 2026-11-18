import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

import httpx

from wsa_agent import client as client_module
from wsa_agent import agent as agent_module
from wsa_agent import renderer
from wsa_agent import tools
from wsa_agent.agent import _extract_tool_calls
from wsa_agent.client import GeminiClient
from wsa_agent.session import SessionState


class _StreamResponse:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        return iter(self._lines)


class _StreamClient:
    def __init__(self, lines):
        self.calls = 0
        self.timeout = None
        self._lines = lines

    def stream(self, *args, **kwargs):
        self.calls += 1
        return _StreamResponse(self._lines)


class TestClientStreamIntegrity(unittest.TestCase):
    def test_full_retry_notifies_reset_callback_with_full_retry_flag(self):
        fake_http = _StreamClient(["data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}"])
        client = GeminiClient("http://127.0.0.1:8081/v1", "test")
        client._client = fake_http
        resets = []

        with patch.object(client_module.time, "sleep"):
            with self.assertRaises(RuntimeError):
                client.stream(
                    [{"role": "user", "content": "hi"}],
                    "test-model",
                    on_reset=resets.append,
                )

        self.assertEqual(resets, [True, True])

    def test_partial_stream_without_done_is_retried_and_rejected(self):
        fake_http = _StreamClient(["data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}"])
        client = GeminiClient("http://127.0.0.1:8081/v1", "test")
        client._client = fake_http

        with patch.object(client_module.time, "sleep"):
            with self.assertRaises(RuntimeError, msg="missing [DONE] must not be accepted"):
                client.stream([{"role": "user", "content": "hi"}], "test-model")

        self.assertEqual(fake_http.calls, 3)

    def test_done_marker_returns_stream_content(self):
        fake_http = _StreamClient([
            "data: {\"choices\":[{\"delta\":{\"content\":\"complete\"}}]}",
            "data: [DONE]",
        ])
        client = GeminiClient("http://127.0.0.1:8081/v1", "test")
        client._client = fake_http

        result = client.stream([{"role": "user", "content": "hi"}], "test-model")

        self.assertEqual(result["content"], "complete")
        self.assertEqual(fake_http.calls, 1)


class TestRendererRecovery(unittest.TestCase):
    def test_full_reset_clears_thinking_and_short_reasoning_is_printed(self):
        stream = renderer.StreamRenderer()
        stream._answer_buf = "stale answer"
        stream._thinking_buf = "short reasoning"
        stream._phase = "answering"

        stream.reset_answer(True)

        self.assertEqual(stream._answer_buf, "")
        self.assertEqual(stream._thinking_buf, "")
        self.assertEqual(stream._phase, "thinking")

        stream._thinking_buf = "short reasoning"
        with patch.object(renderer.console, "print") as print_mock:
            stream._print_thinking_summary(0.1)

        self.assertTrue(any("short reasoning" in str(call) and "Thought" in str(call) for call in print_mock.call_args_list))


class TestFreeModeRecovery(unittest.TestCase):
    def test_canned_retry_uses_answer_continuation_in_free_mode(self):
        class FakeRenderer:
            def __init__(self):
                self._thinking = ""

            def start_thinking(self):
                return None

            def feed(self, token):
                return None

            def feed_thinking(self, token):
                return None

            def reset_answer(self, full_retry=False):
                return None

            def finish(self):
                return None

            def stop_thinking(self):
                return None

            def get_thinking_text(self):
                return self._thinking

        class FakeClient:
            def __init__(self):
                self.messages = []
                self.calls = 0

            def stream(self, messages, **kwargs):
                self.messages.append([dict(message) for message in messages])
                self.calls += 1
                return {"content": "Sorry, something went wrong"} if self.calls == 1 else {"content": "complete"}

        state = SessionState(free_mode=True)
        client = FakeClient()
        with patch.object(agent_module.R, "StreamRenderer", FakeRenderer), \
             patch.object(agent_module.R, "begin_agent_response"), \
             patch.object(agent_module.R, "print_warn"), \
             patch.object(agent_module.R, "print_info"), \
             patch.object(agent_module.time, "sleep"), \
             patch.object(agent_module, "set_project_root"), \
             patch.object(agent_module, "_compact_messages_for_web_limit"):
            agent_module.run_turn("hello", state, client)

        continuation = client.messages[1][-1]["content"]
        self.assertIn("[Continue]: Please provide the complete response", continuation)
        self.assertNotIn("tool_call", continuation)


class _UpstreamStream:
    def __init__(self, line):
        self.line = line

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    def iter_text(self):
        yield self.line + "\n"
        raise RuntimeError("connection dropped")


class _UpstreamClient:
    def __init__(self, line):
        self.line = line

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def stream(self, *args, **kwargs):
        return _UpstreamStream(self.line)


class TestUpstreamFallback(unittest.TestCase):
    def test_partial_upstream_output_is_completed_without_duplication(self):
        inner = [None, None, None, None, [[None, ["partial"]]]]
        line = json.dumps([["wrb.fr", "x", json.dumps(inner) + " " * 120]])
        from wsa_server import gemini as gemini_mod

        with patch.object(gemini_mod.httpx, "Client", return_value=_UpstreamClient(line)):
            with patch.object(gemini_mod, "gemini_stream_generate", return_value="raw"):
                with patch.object(gemini_mod, "extract_thinking_text", return_value=""):
                    with patch.object(gemini_mod, "extract_response_text", return_value="partial completed"):
                        events = list(gemini_mod.gemini_stream_generate_events(
                            "prompt",
                            model_cfg={"mode": 1, "think": 0},
                        ))

        self.assertEqual(events, [("content", "partial"), ("content", " completed")])


class TestToolSafetyAndSessionPersistence(unittest.TestCase):
    def test_invalid_python_edit_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "example.py"
            target.write_text("value = 1\n", encoding="utf-8")
            tools.set_project_root(root)
            tools._read_file("example.py")

            result = tools._write_file("example.py", "def broken(:\n")

            self.assertTrue(result.startswith("[Syntax Error Guard]"))
            self.assertEqual(target.read_text(encoding="utf-8"), "value = 1\n")

    def test_custom_tool_and_permissions_survive_session_save(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tools.set_project_root(root)
            created = tools._create_custom_tool(
                "add_value",
                "Add a value",
                "def run(**kwargs):\n    return str(kwargs['value'] + 1)\n",
            )
            self.assertIn("registered", created)
            self.assertEqual(tools.execute_tool("add_value", {"value": 4}), "5")

            save_path = root / "session.json"
            state = SessionState(project_root=root)
            state.permission_mode = "strict"
            state.auto_approve = True
            with patch.object(SessionState, "_save_path", new_callable=PropertyMock, return_value=save_path):
                state.save()
                restored = SessionState(project_root=root)
                restored.load(save_path)

            self.assertEqual(restored.permission_mode, "strict")
            self.assertFalse(restored.auto_approve)


class TestAuditFixes(unittest.TestCase):
    def test_premium_tui_defaults_and_sanitizes_surrogates(self):
        from wsa_agent import __version__

        self.assertEqual(__version__, "2.0.0")
        self.assertEqual(renderer.sanitize_surrogates("\ud83e\udd16"), "🤖")
        self.assertNotIn("\ud800", renderer.sanitize_surrogates("bad\ud800"))

    def test_client_sanitizes_nested_payloads(self):
        payload = client_module._sanitize_payload({"message": "\ud83e\udd16", "items": ["ok\ud800"]})

        self.assertEqual(payload["message"], "🤖")
        self.assertNotIn("\ud800", payload["items"][0])

    def test_argon_uses_mode_eight_and_combined_headers(self):
        module = __import__("wsa_server")
        cfg = module.MODELS["gemini-4-argon"]

        self.assertEqual(cfg["mode"], 8)
        headers = module.build_argon_headers(cfg)
        self.assertIn("x-goog-ext-525001261-bin", headers)
        request = module._build_upstream_request("x", cfg, None)
        self.assertIn("x-goog-ext-525001261-jspb", request[2])
        self.assertIn("x-goog-ext-525001261-bin", request[2])
        outer = json.loads(request[1]["f.req"])
        inner = json.loads(outer[1])
        self.assertEqual(inner[79], 8)
        self.assertEqual(inner[80], 2)

    def test_binary_helper_reuses_request_uuid(self):
        module = __import__("wsa_server")
        request_uuid = "11111111-2222-3333-4444-555555555555"
        headers = module.build_argon_headers({
            "hex": "95a4c0abcba24364",
            "req_uuid": request_uuid,
        })

        decoded = __import__("base64").b64decode(headers["x-goog-ext-525005358-bin"])

        self.assertIn(request_uuid.encode("ascii"), decoded)

    def test_argon_think_override_updates_slot_without_downgrading_mode(self):
        module = __import__("wsa_server")
        handler = object.__new__(module.GeminiHandler)

        _, cfg, error = handler._resolve_model("gemini-4-argon@think=2")

        self.assertIsNone(error)
        self.assertEqual(cfg["mode"], 8)
        self.assertEqual(cfg["think"], 0)
        self.assertEqual(cfg["slot80"], 4)

    def test_tool_extraction_handles_braces_inside_json_strings(self):
        response = {
            "content": (
                '```tool_call\n{"name":"remember","arguments":'
                '{"key":"x","value":"literal } brace"}}\n```'
            )
        }

        calls = _extract_tool_calls(response)

        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["value"], "literal } brace")

    def test_canned_continuation_keeps_next_step_instruction(self):
        module = __import__("wsa_server")
        prompt, _ = module.messages_to_prompt(
            [
                {"role": "user", "content": "change the code"},
                {"role": "user", "content": "[Continue]: keep going"},
            ],
            tools=[{"type": "function", "function": {"name": "remember"}}],
        )

        self.assertIn("[Next step]: Review the tool output above and continue.", prompt)

    def test_forced_compaction_compacts_small_canned_error_context(self):
        messages = [
            {"role": "system", "content": "system"},
            *[
                {
                    "role": "tool",
                    "content": "line one\nline two\nline three\nline four\nline five\n" + ("x" * 500),
                }
                for _ in range(8)
            ],
        ]

        compacted = agent_module._compact_messages_for_web_limit(messages, force=True)

        self.assertGreaterEqual(compacted, 1)
        self.assertIn("[Compacted", messages[1]["content"])

    def test_extract_upstream_model_info_identifies_tier4_vs_flash_lite(self):
        module = __import__("wsa_server")
        inner_tier4 = [None] * 60
        inner_tier4[39] = "cf41b0e0dd7d53e5"
        inner_tier4[42] = "4 Carbon"
        inner_tier4[46] = 2
        inner_tier4[58] = 8
        info_t4 = module.extract_upstream_model_info(inner_tier4)
        self.assertTrue(info_t4["is_tier4_high"])
        self.assertFalse(info_t4["is_downgraded_flash_lite"])
        self.assertEqual(info_t4["mode_name"], "MODE_CATEGORY_ULTRA")
        self.assertEqual(info_t4["thinking_level"], "THINKING_LEVEL_EXTENDED")

        inner_lite = [None] * 60
        inner_lite[42] = "3.5 Flash-Lite"
        inner_lite[58] = 6
        info_lite = module.extract_upstream_model_info(inner_lite)
        self.assertFalse(info_lite["is_tier4_high"])
        self.assertTrue(info_lite["is_downgraded_flash_lite"])
        self.assertEqual(info_lite["mode_name"], "MODE_CATEGORY_FLASH_LITE")

    def test_dom_relay_routes_argon_high_with_thinking_and_telemetry(self):
        import threading
        module = __import__("wsa_server")
        try:
            # Mark DOM relay active by polling once
            self.assertIsNone(module.dom_relay_get_pending())
            self.assertTrue(module.is_dom_relay_active())

            def _responder():
                for _ in range(50):
                    item = module.dom_relay_get_pending()
                    if item:
                        module.dom_relay_post_response(
                            item["id"],
                            response="ARGON_RELAY_OK",
                            thinking="Analyzing with Argon High...",
                            model="4 Argon High",
                        )
                        return
                    __import__("time").sleep(0.01)

            t = threading.Thread(target=_responder, daemon=True)
            t.start()
            events = list(
                module.gemini_stream_generate_events(
                    "test relay",
                    model_cfg=module.MODELS["gemini-4-argon"],
                )
            )
            t.join(timeout=2.0)
            self.assertEqual(
                events,
                [("thinking", "Analyzing with Argon High..."), ("content", "ARGON_RELAY_OK")],
            )
            self.assertEqual(module.LAST_UPSTREAM_MODEL["badge"], "4 Argon High")
            self.assertTrue(module.LAST_UPSTREAM_MODEL["is_tier4_high"])
            self.assertFalse(module.LAST_UPSTREAM_MODEL["is_downgraded_flash_lite"])
        finally:
            with module._dom_relay_lock:
                module._dom_relay["last_poll_ts"] = 0.0
                module._dom_relay["pending"].clear()

    def test_gemini_31_pro_uses_2026_inner_and_pro_field25(self):
        module = __import__("wsa_server")
        cfg = module.MODELS["gemini-3.1-pro"]
        _, params, headers = module._build_upstream_request("pro test", cfg, None)
        outer = json.loads(params["f.req"])
        inner = json.loads(outer[1])
        self.assertEqual(len(inner), 100)
        self.assertEqual(inner[6], [1])
        self.assertEqual(inner[30], [4, 16, 19, 12])
        self.assertEqual(inner[68], 2)
        self.assertEqual(inner[79], 3)
        self.assertEqual(inner[80], 2)
        self.assertEqual(inner[91], 1)
        self.assertEqual(inner[96], 1)
        self.assertEqual(inner[98], 1)
        self.assertIn("x-goog-ext-525001261-bin", headers)
        self.assertNotIn("x-goog-ext-525001261-jspb", headers)

    def test_dom_code_snippet_tool_call_parsing(self):
        module = __import__("wsa_server")
        dom_text = (
            "The smallest positive integer n satisfying the congruences is 23.\n\n"
            "Code snippet\n"
            '{"name": "verify_crt", "arguments": {"n": 23}}'
        )
        cleaned, calls = module.parse_tool_calls(dom_text)
        self.assertEqual(cleaned, "The smallest positive integer n satisfying the congruences is 23.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "verify_crt")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"n": 23})

        agent_calls = _extract_tool_calls({"content": dom_text})
        self.assertEqual(len(agent_calls), 1)
        self.assertEqual(agent_calls[0]["function"]["name"], "verify_crt")
        self.assertEqual(json.loads(agent_calls[0]["function"]["arguments"]), {"n": 23})

    def _reset_relay(self, module):
        with module._dom_relay_lock:
            module._dom_relay["last_poll_ts"] = 0.0
            module._dom_relay["pending"].clear()

    def test_dom_relay_accepts_any_web_selected_model(self):
        import threading
        module = __import__("wsa_server")
        try:
            def _responder():
                for _ in range(200):
                    item = module.dom_relay_get_pending()
                    if item:
                        module.dom_relay_post_response(item["id"], response="hi from web", model="3.1 Pro")
                        return
                    __import__("time").sleep(0.01)

            t = threading.Thread(target=_responder, daemon=True)
            t.start()
            res = module.dom_relay_put_prompt("x", timeout=5, pickup_timeout=5, provider="gemini")
            t.join(timeout=2.0)
            self.assertEqual(res.get("response"), "hi from web")
            self.assertEqual(res.get("model"), "3.1 Pro")
            self.assertEqual(module.LAST_UPSTREAM_MODEL.get("badge"), "3.1 Pro")

            def _responder_claude():
                for _ in range(200):
                    item = module.dom_relay_get_pending()
                    if item:
                        module.dom_relay_post_response(item["id"], response="hi from claude", model="Claude Sonnet 4.6")
                        return
                    __import__("time").sleep(0.01)

            t2 = threading.Thread(target=_responder_claude, daemon=True)
            t2.start()
            res2 = module.dom_relay_put_prompt("y", timeout=5, pickup_timeout=5, provider="claude")
            t2.join(timeout=2.0)
            self.assertEqual(res2.get("response"), "hi from claude")
            self.assertEqual(res2.get("model"), "Claude Sonnet 4.6")
            self.assertEqual(module.LAST_UPSTREAM_MODEL.get("provider"), "claude")
            self.assertTrue(module.is_argon_badge("Argon High"))
            self.assertTrue(module.is_argon_badge("4 Argon"))
            self.assertFalse(module.is_argon_badge("Argon Medium"))
            self.assertFalse(module.is_argon_badge(""))
        finally:
            self._reset_relay(module)

    def test_dom_relay_pickup_timeout_is_explicit(self):
        module = __import__("wsa_server")
        try:
            with self.assertRaises(RuntimeError) as ctx:
                list(module.dom_relay_iter("x", timeout=5, pickup_timeout=0.1, step=0.05))
            self.assertIn("DOM relay", str(ctx.exception))
            self.assertIn("no Gemini tab picked up the prompt", str(ctx.exception))
            with module._dom_relay_lock:
                self.assertEqual(module._dom_relay["pending"], {})
        finally:
            self._reset_relay(module)

    def test_strict_argon_never_silently_falls_back_to_pro(self):
        import tempfile
        from unittest import mock
        module = __import__("wsa_server")
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        old_cookie = module.CONFIG.get("cookie_file")
        old_fallback = module.CONFIG.get("argon_allow_pro_fallback")
        try:
            module.CONFIG["cookie_file"] = path
            module.CONFIG["argon_allow_pro_fallback"] = False
            self.assertTrue(module.argon_strict_relay_required())
            import urllib.request
            from wsa_server import gemini as gemini_mod
            with mock.patch.object(gemini_mod, "dom_relay_put_prompt", side_effect=RuntimeError("relay down")), \
                    mock.patch.object(urllib.request, "urlopen") as urlopen:
                with self.assertRaises(RuntimeError):
                    module.gemini_stream_generate("x", model_cfg=module.MODELS["gemini-4-argon"])
                urlopen.assert_not_called()
            module.CONFIG["argon_allow_pro_fallback"] = True
            self.assertFalse(module.argon_strict_relay_required())
        finally:
            module.CONFIG["cookie_file"] = old_cookie
            module.CONFIG["argon_allow_pro_fallback"] = old_fallback
            os.remove(path)

    def test_dom_relay_iter_emits_keepalive_before_argon_result(self):
        import threading
        module = __import__("wsa_server")
        try:
            def _slow_responder():
                for _ in range(100):
                    item = module.dom_relay_get_pending()
                    if item:
                        __import__("time").sleep(0.15)
                        module.dom_relay_post_response(
                            item["id"],
                            response='```tool_call\n{"name":"read_file","arguments":{"path":"a.py"}}\n```',
                            thinking="Checking a.py...",
                            model="Argon High",
                        )
                        return
                    __import__("time").sleep(0.01)

            t = threading.Thread(target=_slow_responder, daemon=True)
            t.start()
            events = list(module.dom_relay_iter("inspect a.py", timeout=5.0, pickup_timeout=2.0, step=0.05))
            t.join(timeout=2.0)
            kinds = [k for k, _ in events]
            self.assertIn("keepalive", kinds)
            self.assertEqual(kinds[-1], "result")
            self.assertEqual(events[-1][1]["model"], "Argon High")
            self.assertEqual(events[-1][1]["thinking"], "Checking a.py...")
        finally:
            self._reset_relay(module)

    def test_session_relay_reuses_same_chat_and_sends_delta_prompt(self):
        module = __import__("wsa_server")
        sid = "sess-cont-01"
        try:
            tools = [{"type": "function", "function": {"name": "read_file", "description": "Read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
            m1 = [
                {"role": "system", "content": "System rules A"},
                {"role": "user", "content": "Inspect app.py"},
            ]
            full1, imgs1 = module.messages_to_prompt(m1, tools)
            p1, fb1, _, new1, _hint = module.prepare_session_relay_prompt(sid, m1, tools, full1, imgs1)
            self.assertTrue(new1)
            self.assertEqual(p1, full1)

            tc = [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"app.py"}'}}]
            module.record_session_relay_turn(sid, "", tc, chat_url="https://gemini.google.com/app/abc123")

            # Round 2 in same session: system prompt updated, prior assistant reply + new tool output added
            m2 = [
                {"role": "system", "content": "System rules B (new timestamp)"},
                {"role": "user", "content": "Inspect app.py"},
                {"role": "assistant", "content": "", "tool_calls": tc},
                {"role": "tool", "name": "read_file", "tool_call_id": "call_1", "content": "def hello(): return 42"},
            ]
            full2, imgs2 = module.messages_to_prompt(m2, tools)
            p2, fb2, _, new2, _hint = module.prepare_session_relay_prompt(sid, m2, tools, full2, imgs2)
            self.assertFalse(new2)
            self.assertEqual(fb2, full2)
            self.assertIn("def hello(): return 42", p2)
            self.assertNotIn("Available tools:", p2)
            self.assertNotIn("System rules B", p2)

            # Turn 2 in same REPL session after final assistant text
            module.record_session_relay_turn(sid, "hello() returns 42.", None, chat_url="https://gemini.google.com/app/abc123")
            m3 = m2 + [
                {"role": "assistant", "content": "hello() returns 42."},
                {"role": "user", "content": "Now double its return value"},
            ]
            full3, imgs3 = module.messages_to_prompt(m3, tools)
            p3, fb3, _, new3, _hint = module.prepare_session_relay_prompt(sid, m3, tools, full3, imgs3)
            self.assertFalse(new3)
            self.assertEqual(fb3, full3)
            self.assertIn("Now double its return value", p3)
            self.assertNotIn("def hello(): return 42", p3)
        finally:
            self._reset_relay(module)
            module._dom_relay["sessions"].pop(sid, None)

    def test_session_relay_survives_history_compaction_without_new_chat(self):
        module = __import__("wsa_server")
        sid = "sess-compact-02"
        try:
            tc_old = [{"id": "call_old", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"old.py"}'}}]
            tc_last = [{"id": "call_last", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"new.py"}'}}]
            module.record_session_relay_turn(sid, "", tc_last)

            # Simulate older messages evicted/compacted while keeping the latest assistant + tool result
            m_compacted = [
                {"role": "system", "content": "System with digest"},
                {"role": "user", "content": "Latest question"},
                {"role": "assistant", "content": "", "tool_calls": tc_last},
                {"role": "tool", "name": "read_file", "tool_call_id": "call_last", "content": "x = 99"},
            ]
            full_c, imgs_c = module.messages_to_prompt(m_compacted, None)
            p_c, _, _, new_c, _hint = module.prepare_session_relay_prompt(sid, m_compacted, None, full_c, imgs_c)
            self.assertFalse(new_c)
            self.assertIn("x = 99", p_c)
        finally:
            self._reset_relay(module)
            module._dom_relay["sessions"].pop(sid, None)

    def test_session_state_rotates_on_clear_and_project_and_persists_save_load(self):
        import tempfile
        from pathlib import Path
        from wsa_agent.session import SessionState

        with tempfile.TemporaryDirectory() as tmpdir:
            s = SessionState(project_root=Path(tmpdir))
            orig_sid = s.session_id
            self.assertTrue(len(orig_sid) >= 8)

            save_file = Path(tmpdir) / "sess.json"
            s.turn_count = 2
            s.messages = [{"role": "user", "content": "hi"}]
            data = {
                "project_root": str(s.project_root),
                "session_id": orig_sid,
                "messages": s.messages,
                "turn_count": 2,
            }
            save_file.write_text(__import__("json").dumps(data), encoding="utf-8")

            s2 = SessionState(project_root=Path(tmpdir))
            self.assertNotEqual(s2.session_id, orig_sid)
            s2.load(save_file)
            self.assertEqual(s2.session_id, orig_sid)

            s2.clear_history()
            self.assertNotEqual(s2.session_id, orig_sid)
            after_clear = s2.session_id

            sub = Path(tmpdir) / "proj2"
            sub.mkdir()
            self.assertTrue(s2.set_project(str(sub)))
            self.assertNotEqual(s2.session_id, after_clear)


class TestMinimalClaudeCodeTUI(unittest.TestCase):
    def test_friendly_model_labels_and_filtered_thinking_summary(self):
        import io
        from rich.console import Console

        self.assertEqual(renderer.friendly_model_name("claude"), "Claude Web")
        self.assertEqual(renderer.friendly_model_name("gemini"), "Gemini Web")
        self.assertEqual(renderer.friendly_model_name("gemini-4-argon@think=0"), "Gemini Web")
        self.assertEqual(renderer.friendly_model_name("gemini-3.5-flash-thinking@think=2"), "Gemini Web")

        buf = io.StringIO()
        orig = renderer.console
        try:
            renderer.console = Console(file=buf, force_terminal=False, width=100, highlight=False)
            sr = renderer.StreamRenderer()
            sr._thinking_buf = "Gemini is typing...\nGemini replied"
            sr._print_thinking_summary(1.2)
            out_noise = buf.getvalue()
            self.assertIn("Thought 1.2s", out_noise)
            self.assertNotIn("Gemini replied", out_noise)
            self.assertNotIn("Gemini is typing", out_noise)
        finally:
            renderer.console = orig

    def test_compact_tool_tree_and_minimal_prompt_toolbar(self):
        import io
        from pathlib import Path
        from rich.console import Console
        from wsa_agent import __main__ as main_mod
        from wsa_agent.session import SessionState

        buf = io.StringIO()
        orig = renderer.console
        try:
            renderer.console = Console(file=buf, force_terminal=False, width=100, highlight=False)
            renderer.print_tool_call("read_file", {"path": "calc.py", "start_line": 1, "end_line": 12})
            renderer.print_tool_result("   1 | x = 1\n[Showing lines 1-12 of 40 (512 bytes)]", "read_file", elapsed=0.05)
            renderer.print_tool_call("patch_file", {"path": "calc.py"})
            renderer.print_tool_result("[OK] Patched calc.py\n@@ -1 +1 @@\n-x = 1\n+x = 2", "patch_file", elapsed=0.02)
            renderer.print_tool_call("run_command", {"command": "pytest -q"})
            renderer.print_tool_result(
                "STDOUT:\n...                                                     [100%]\n3 passed in 0.01s\nExit code: 0",
                "run_command",
                elapsed=1.8,
            )
            out = buf.getvalue()
            self.assertIn("Read(calc.py:L1-12)", out)
            self.assertIn("Read lines 1–12 of 40", out)
            self.assertNotIn("1 | x = 1", out)
            self.assertIn("Update(calc.py)", out)
            self.assertIn("Patched calc.py", out)
            self.assertNotIn("[OK]", out)
            self.assertIn("Bash(pytest -q)", out)
            self.assertIn("3 passed in 0.01s", out)
            self.assertNotIn("STDOUT:", out)
            self.assertNotIn("Exit code: 0", out)
        finally:
            renderer.console = orig

        st = SessionState(project_root=Path("."))
        prompt = main_mod._build_prompt(st)
        toolbar = main_mod._build_bottom_toolbar(st)
        self.assertIn("❯", prompt)
        self.assertNotIn("==", prompt)
        self.assertIn("Gemini Web", toolbar)
        self.assertIn("/help", toolbar)


class TestClaudeCodeThemeAndModels(unittest.TestCase):
    def test_theme_palette_persistence_and_renderer_sync(self):
        from wsa_agent import theme

        prev_id = theme.get_theme().id
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg_path = Path(tmp) / "wsa-theme.json"
                with patch.object(theme, "THEME_CONFIG_PATH", cfg_path):
                    ids = {t.id for t in theme.list_themes()}
                    self.assertEqual(ids, {"dark", "dark-daltonized", "github-dark", "dracula"})

                    pal = theme.set_theme("dracula", persist=True)
                    self.assertEqual(pal.id, "dracula")
                    self.assertTrue(cfg_path.exists())
                    self.assertEqual(json.loads(cfg_path.read_text(encoding="utf-8"))["theme"], "dracula")

                    renderer.sync_theme()
                    self.assertEqual(renderer.Theme.ACCENT, "#ffb86c")
                    self.assertEqual(renderer.Theme.CODE_THEME, "dracula")

                    with self.assertRaises(ValueError):
                        theme.set_theme("nonexistent-theme")
        finally:
            theme.set_theme(prev_id, persist=False)
            renderer.sync_theme()

    def test_model_catalog_and_features_cover_gemini_models(self):
        from wsa_agent.session import ALL_MODELS, MODEL_CATALOG, get_model_info

        self.assertEqual(len(MODEL_CATALOG), 5)
        catalog_ids = [entry["id"] for entry in MODEL_CATALOG]
        self.assertEqual(catalog_ids, ["claude", "deepseek", "gemini", "chatgpt", "venice"])
        self.assertEqual([mid for mid, _ in ALL_MODELS], catalog_ids)
        for entry in MODEL_CATALOG:
            for key in ("id", "name", "tier", "transport", "thinking", "requires", "features"):
                self.assertIn(key, entry)
                self.assertTrue(entry[key], f"Empty {key} in {entry['id']}")

        self.assertEqual(get_model_info("claude")["transport"], "claude.ai")
        self.assertEqual(get_model_info("claude")["name"], "Claude Web")
        self.assertEqual(get_model_info("deepseek")["transport"], "chat.deepseek.com")
        self.assertEqual(get_model_info("deepseek")["name"], "DeepSeek Web")
        self.assertEqual(get_model_info("gemini")["transport"], "gemini.google.com")
        self.assertEqual(get_model_info("gemini")["name"], "Gemini Web")
        self.assertEqual(get_model_info("chatgpt")["transport"], "chatgpt.com")
        self.assertEqual(get_model_info("chatgpt")["name"], "ChatGPT Web")
        self.assertEqual(get_model_info("venice")["transport"], "venice.ai")
        self.assertEqual(get_model_info("venice")["name"], "Venice Web")
        self.assertEqual(get_model_info("vernice")["id"], "venice")
        self.assertEqual(get_model_info("gemini-4-argon")["id"], "gemini")

    def test_claude_welcome_models_and_shortcuts_rendering(self):
        import io
        from rich.console import Console
        from wsa_agent.session import ALL_MODELS

        st = SessionState(project_root=Path("."))
        buf = io.StringIO()
        orig = renderer.console
        try:
            renderer.console = Console(file=buf, force_terminal=False, width=102, highlight=False)
            renderer.print_welcome(st, server_ok=True)
            renderer.print_models_table(ALL_MODELS, current_model=st.model, state=st)
            renderer.print_shortcuts()
            out = buf.getvalue()
            self.assertIn("Commands", out)
            self.assertIn("Session", out)
            self.assertIn("wsa v2.0", out)
            self.assertIn("Engineered by Divyansh Saraf", out)
            self.assertIn("Server ready", out)
            self.assertIn("Web Bridges", out)
            self.assertIn("Claude Web", out)
            self.assertIn("claude.ai · Web-controlled", out)
            self.assertIn("DeepSeek Web", out)
            self.assertIn("chat.deepseek.com · Web-controlled", out)
            self.assertIn("Gemini Web", out)
            self.assertIn("gemini.google.com · Web-controlled", out)
            self.assertIn("ChatGPT Web", out)
            self.assertIn("chatgpt.com · Web-controlled", out)
            self.assertIn("Switch /model claude|deepseek|gemini|chatgpt", out)
            self.assertIn("Queue /multiagent", out)
            self.assertIn("Select model & thinking in browser", out)
            self.assertIn("Shift+Tab", out)
            self.assertIn("Alt+M", out)
            self.assertIn("/theme", out)
            self.assertNotIn("Welcome back!", out)
            self.assertNotIn("Chrome DOM Relay", out)
            self.assertNotIn("Direct HTTP", out)
            self.assertNotIn("/effort", out)
            self.assertNotIn("/think", out)
            self.assertNotIn("4 Argon High", out)
            self.assertNotIn("3.1 Pro", out)
        finally:
            renderer.console = orig

    def test_permission_mode_cycle_toolbar_and_theme_command(self):
        import io
        from rich.console import Console
        from wsa_agent import __main__ as main_mod
        from wsa_agent import theme
        from wsa_agent.commands import handle_command

        prev_id = theme.get_theme().id
        buf = io.StringIO()
        orig = renderer.console
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg_path = Path(tmp) / "wsa-theme.json"
                with patch.object(theme, "THEME_CONFIG_PATH", cfg_path):
                    renderer.console = Console(file=buf, force_terminal=False, width=102, highlight=False)
                    st = SessionState(project_root=Path("."))
                    self.assertIn("accept edits on", main_mod._build_bottom_toolbar(st))

                    self.assertEqual(main_mod._cycle_permission_mode(st), "strict")
                    self.assertIn("confirm all actions", main_mod._build_bottom_toolbar(st))

                    self.assertEqual(main_mod._cycle_permission_mode(st), "auto")
                    self.assertIn("bypass permissions on", main_mod._build_bottom_toolbar(st))
                    self.assertEqual(main_mod._cycle_permission_mode(st), "smart")

                    self.assertTrue(handle_command("/theme github-dark", st))
                    self.assertEqual(theme.get_theme().id, "github-dark")
                    self.assertEqual(renderer.Theme.ACCENT, "#58a6ff")

                    self.assertTrue(handle_command("/theme list", st))
                    out = buf.getvalue()
                    self.assertIn("github-dark", out)
                    self.assertIn("dracula", out)
                    self.assertIn("dark-daltonized", out)
        finally:
            renderer.console = orig
            theme.set_theme(prev_id, persist=False)
            renderer.sync_theme()


class TestSingleAccountAndContextContinuity(unittest.TestCase):
    """Single active browser/account locking, Brave/Chrome relay switching, and context continuity."""

    def _snapshot_module_state(self, module):
        with module._dom_relay_lock:
            relay_snap = {
                "last_poll_ts": module._dom_relay.get("last_poll_ts", 0.0),
                "active_relay_id": module._dom_relay.get("active_relay_id"),
                "pinned_relay_label": module._dom_relay.get("pinned_relay_label"),
                "explicit_relay_lock": module._dom_relay.get("explicit_relay_lock", False),
                "relays": dict(module._dom_relay.get("relays") or {}),
                "pending": dict(module._dom_relay.get("pending") or {}),
                "sessions": dict(module._dom_relay.get("sessions") or {}),
            }
        with module._CONFIG_LOCK:
            cfg_snap = {
                "cookie_file": module.CONFIG.get("cookie_file"),
                "xsrf_token": module.CONFIG.get("xsrf_token"),
                "gemini_bl": module.CONFIG.get("gemini_bl"),
                "single_account_mode": module.CONFIG.get("single_account_mode"),
                "allow_auto_rotate": module.CONFIG.get("allow_auto_rotate"),
                "active_relay": module.CONFIG.get("active_relay"),
            }
        from wsa_server import config as cfg_mod
        return relay_snap, cfg_snap, cfg_mod.SCRIPT_DIR

    def _restore_module_state(self, module, relay_snap, cfg_snap, orig_script_dir):
        from wsa_server import config as cfg_mod
        cfg_mod.SCRIPT_DIR = orig_script_dir
        module.SCRIPT_DIR = orig_script_dir
        with module._dom_relay_lock:
            module._dom_relay["last_poll_ts"] = relay_snap["last_poll_ts"]
            module._dom_relay["active_relay_id"] = relay_snap["active_relay_id"]
            module._dom_relay["pinned_relay_label"] = relay_snap["pinned_relay_label"]
            module._dom_relay["explicit_relay_lock"] = relay_snap["explicit_relay_lock"]
            module._dom_relay["relays"] = relay_snap["relays"]
            module._dom_relay["pending"] = relay_snap["pending"]
            module._dom_relay["sessions"] = relay_snap["sessions"]
        with module._CONFIG_LOCK:
            module.CONFIG.update(cfg_snap)

    def test_single_active_relay_lock_blocks_standby_browser_and_foreign_completion(self):
        import threading
        module = __import__("wsa_server")
        relay_snap, cfg_snap, orig_dir = self._snapshot_module_state(module)
        try:
            with module._dom_relay_lock:
                module._dom_relay["relays"] = {}
                module._dom_relay["active_relay_id"] = None
                module._dom_relay["pinned_relay_label"] = None
                module._dom_relay["explicit_relay_lock"] = False
                module._dom_relay["pending"].clear()

            chrome_info = {
                "client_id": "relay-chrome-test",
                "browser": "chrome",
                "label": "chrome",
                "email": "chrome.pro@gmail.com",
                "auth_user": "0",
                "model_badge": "4 Argon High",
            }
            brave_info = {
                "client_id": "relay-brave-test",
                "browser": "brave",
                "label": "brave",
                "email": "brave.pro@gmail.com",
                "auth_user": "0",
                "model_badge": "4 Argon High",
            }

            item_c, reg_c = module.dom_relay_poll_for_client(chrome_info)
            self.assertIsNone(item_c)
            self.assertTrue(reg_c["is_active"])
            self.assertEqual(reg_c["active_relay_id"], "relay-chrome-test")

            item_b, reg_b = module.dom_relay_poll_for_client(brave_info)
            self.assertIsNone(item_b)
            self.assertFalse(reg_b["is_active"])
            self.assertEqual(reg_b["active_relay_id"], "relay-chrome-test")

            # Queue a prompt via _dom_relay directly and ensure standby Brave cannot steal or complete it.
            ev = threading.Event()
            with module._dom_relay_lock:
                module._dom_relay["pending"]["req-lock-1"] = {
                    "prompt": "hello single account",
                    "fallback_prompt": "hello single account",
                    "session_id": "s-lock",
                    "new_chat": True,
                    "images": None,
                    "target_relay_id": "relay-chrome-test",
                    "dispatched": False,
                    "dispatched_to": "",
                    "event": ev,
                    "response": None,
                    "error": None,
                    "thinking": "",
                    "model": "",
                    "chat_url": "",
                }

            self.assertIsNone(module.dom_relay_get_pending("relay-brave-test"))
            item_s, reg_s = module.dom_relay_poll_for_client(brave_info)
            self.assertIsNone(item_s)
            self.assertFalse(reg_s["is_active"])

            picked = module.dom_relay_get_pending("relay-chrome-test")
            self.assertIsNotNone(picked)
            self.assertEqual(picked["id"], "req-lock-1")
            self.assertEqual(picked["client_id"], "relay-chrome-test")

            # Foreign client must be rejected from answering Chrome's prompt.
            self.assertFalse(
                module.dom_relay_post_response(
                    "req-lock-1",
                    response="hijacked",
                    model="4 Argon High",
                    client_id="relay-brave-test",
                )
            )
            self.assertFalse(ev.is_set())

            self.assertTrue(
                module.dom_relay_post_response(
                    "req-lock-1",
                    response="authentic",
                    model="4 Argon High",
                    client_id="relay-chrome-test",
                )
            )
            self.assertTrue(ev.is_set())
        finally:
            self._restore_module_state(module, relay_snap, cfg_snap, orig_dir)

    def test_select_active_relay_switches_browser_and_forces_full_context_new_chat(self):
        module = __import__("wsa_server")
        relay_snap, cfg_snap, orig_dir = self._snapshot_module_state(module)
        sid = "sess-browser-switch"
        try:
            with module._dom_relay_lock:
                module._dom_relay["relays"] = {}
                module._dom_relay["active_relay_id"] = None
                module._dom_relay["pinned_relay_label"] = None
                module._dom_relay["explicit_relay_lock"] = False

            module.register_or_touch_relay({
                "client_id": "relay-chrome-sw",
                "browser": "chrome",
                "label": "chrome",
                "email": "c@gmail.com",
            })
            module.register_or_touch_relay({
                "client_id": "relay-brave-sw",
                "browser": "brave",
                "label": "brave",
                "email": "b@gmail.com",
            })

            tools_def = [{"type": "function", "function": {"name": "read_file", "description": "Read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
            tc = [{"id": "call_sw", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]
            module.record_session_relay_turn(
                sid, "", tc, chat_url="https://gemini.google.com/app/chrome1", relay_id="relay-chrome-sw"
            )
            msgs = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "Inspect a.py"},
                {"role": "assistant", "content": "", "tool_calls": tc},
                {"role": "tool", "name": "read_file", "tool_call_id": "call_sw", "content": "ok"},
            ]
            full_p, imgs = module.messages_to_prompt(msgs, tools_def)

            # Same relay continues via delta prompt (new_chat=False)
            p_same, _, _, new_same, _hint = module.prepare_session_relay_prompt(sid, msgs, tools_def, full_p, imgs)
            self.assertFalse(new_same)
            self.assertNotIn("Available tools:", p_same)

            # Guard in prepare_session_relay_prompt: if active_relay_id differs from session owner, force full context + new_chat=True
            with module._dom_relay_lock:
                module._dom_relay["active_relay_id"] = "relay-brave-sw"
            p_mismatch, fb_mismatch, _, new_mismatch, _hint = module.prepare_session_relay_prompt(sid, msgs, tools_def, full_p, imgs)
            self.assertTrue(new_mismatch)
            self.assertEqual(p_mismatch, full_p)
            self.assertEqual(fb_mismatch, full_p)

            # Explicit select_active_relay resets active lock to Brave and clears foreign session affinity
            with module._dom_relay_lock:
                module._dom_relay["active_relay_id"] = "relay-chrome-sw"
                module._dom_relay["sessions"]["other-project::claude"] = {
                    "relay_id": "relay-chrome-sw", "chat_url": "https://claude.ai/chat/other-project"
                }
            sel = module.select_active_relay(target="brave", session_id=sid)
            self.assertTrue(sel["ok"])
            self.assertEqual(sel["active_relay_id"], "relay-brave-sw")
            self.assertEqual(sel["browser"], "brave")
            self.assertGreaterEqual(sel["cleared_chats"], 1)
            with module._dom_relay_lock:
                self.assertIn("other-project::claude", module._dom_relay["sessions"])

            _, reg_b = module.dom_relay_poll_for_client({"client_id": "relay-brave-sw", "browser": "brave", "label": "brave"})
            _, reg_c = module.dom_relay_poll_for_client({"client_id": "relay-chrome-sw", "browser": "chrome", "label": "chrome"})
            self.assertTrue(reg_b["is_active"])
            self.assertFalse(reg_c["is_active"])

            p_after, _, _, new_after, _hint = module.prepare_session_relay_prompt(sid, msgs, tools_def, full_p, imgs)
            self.assertTrue(new_after)
            self.assertEqual(p_after, full_p)
        finally:
            self._restore_module_state(module, relay_snap, cfg_snap, orig_dir)

    def test_receive_cookie_sync_isolates_per_browser_and_protects_active_config(self):
        module = __import__("wsa_server")
        relay_snap, cfg_snap, orig_dir = self._snapshot_module_state(module)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                from wsa_server import config as cfg_mod
                cfg_mod.SCRIPT_DIR = tmp
                module.SCRIPT_DIR = tmp
                default_cookie = os.path.join(tmp, "wsa-auth.json")
                with module._CONFIG_LOCK:
                    module.CONFIG["cookie_file"] = default_cookie
                    module.CONFIG["xsrf_token"] = "initial-xsrf"
                with module._dom_relay_lock:
                    module._dom_relay["relays"] = {}
                    module._dom_relay["active_relay_id"] = None
                    module._dom_relay["pinned_relay_label"] = None
                    module._dom_relay["explicit_relay_lock"] = False

                # First sender (Chrome) becomes active
                res_c = module.receive_cookie_sync({
                    "client_id": "relay-chrome-sync",
                    "browser": "chrome",
                    "label": "chrome",
                    "cookie": "SAPISID=c_val; __Secure-1PSID=c1",
                    "xsrf_token": "chrome-xsrf",
                    "email": "chrome@example.com",
                    "auth_user": "0",
                })
                self.assertTrue(res_c["ok"])
                self.assertTrue(res_c["is_active_relay"])
                chrome_path = Path(tmp) / "accounts" / "relay-chrome.json"
                self.assertTrue(chrome_path.exists())
                self.assertEqual(module.CONFIG["xsrf_token"], "chrome-xsrf")
                self.assertEqual(os.path.abspath(module.CONFIG["cookie_file"]), os.path.abspath(str(chrome_path)))

                # Second sender (Brave standby) writes to isolated relay-brave.json without clobbering active Chrome CONFIG
                res_b = module.receive_cookie_sync({
                    "client_id": "relay-brave-sync",
                    "browser": "brave",
                    "label": "brave",
                    "cookie": "SAPISID=b_val; __Secure-1PSID=b1",
                    "xsrf_token": "brave-xsrf",
                    "email": "brave@example.com",
                    "auth_user": "0",
                })
                self.assertTrue(res_b["ok"])
                self.assertFalse(res_b["is_active_relay"])
                brave_path = Path(tmp) / "accounts" / "relay-brave.json"
                self.assertTrue(brave_path.exists())
                self.assertEqual(module.CONFIG["xsrf_token"], "chrome-xsrf")
                self.assertEqual(os.path.abspath(module.CONFIG["cookie_file"]), os.path.abspath(str(chrome_path)))

                # Binding Brave loads Brave's isolated cookie + XSRF into active CONFIG
                sel = module.select_active_relay(target="brave")
                self.assertTrue(sel["ok"])
                self.assertEqual(os.path.abspath(module.CONFIG["cookie_file"]), os.path.abspath(str(brave_path)))
                self.assertEqual(module.CONFIG["xsrf_token"], "brave-xsrf")
        finally:
            self._restore_module_state(module, relay_snap, cfg_snap, orig_dir)

    def test_single_account_mode_disables_silent_auto_rotation(self):
        module = __import__("wsa_server")
        relay_snap, cfg_snap, orig_dir = self._snapshot_module_state(module)
        try:
            self.assertTrue(module.DEFAULT_CONFIG.get("single_account_mode"))
            self.assertFalse(module.DEFAULT_CONFIG.get("allow_auto_rotate"))
            with module._CONFIG_LOCK:
                module.CONFIG["single_account_mode"] = True
                module.CONFIG["allow_auto_rotate"] = False
            self.assertFalse(module._is_auto_rotate_allowed())

            with module._CONFIG_LOCK:
                module.CONFIG["allow_auto_rotate"] = True
            self.assertFalse(module._is_auto_rotate_allowed())

            with module._CONFIG_LOCK:
                module.CONFIG["single_account_mode"] = False
                module.CONFIG["allow_auto_rotate"] = True
            self.assertTrue(module._is_auto_rotate_allowed())
        finally:
            self._restore_module_state(module, relay_snap, cfg_snap, orig_dir)

    def test_compact_history_and_system_message_preserve_context_and_active_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = SessionState(project_root=Path(tmp))
            st.active_account_label = "brave"
            st.written_files = {"src/app.py", "src/utils.py"}
            st.verified_files = {"tests/test_app.py"}
            st.messages = [
                {"role": "user", "content": "Refactor auth module cleanly"},
                {"role": "assistant", "content": "Analyzed initial token config."},
            ]
            for idx in range(5):
                st.messages.append({"role": "user", "content": f"Step {idx} check"})
                st.messages.append({"role": "assistant", "content": f"Found token bug check {idx}."})

            removed = st.compact_history(keep=4)
            self.assertGreater(removed, 0)
            self.assertIn("Completed Earlier Topics", st.context_summary)
            self.assertIn("Key Prior Findings:", st.context_summary)
            self.assertIn("Found token bug check 2.", st.context_summary)
            self.assertIn("Modified Files: src/app.py, src/utils.py", st.context_summary)
            self.assertIn("Verified/Read Files: tests/test_app.py", st.context_summary)

            sys_msg = agent_module._build_system_message(st)
            content = sys_msg["content"]
            self.assertIn("Active single account: brave", content)
            self.assertIn("Files modified in session: src/app.py, src/utils.py", content)
            self.assertIn("Files verified/read in session: tests/test_app.py", content)
            self.assertIn("Context transfer digest:", content)

    def test_account_command_bind_rotate_list_preserve_session_context(self):
        import io
        from rich.console import Console
        from wsa_agent.commands import handle_command

        st = SessionState(project_root=Path("."))
        st.turn_count = 5
        st.messages = [{"role": "user", "content": "preserve full task context"}]

        buf = io.StringIO()
        orig = renderer.console
        try:
            renderer.console = Console(file=buf, force_terminal=False, width=104, highlight=False)
            with patch.object(client_module.GeminiClient, "switch_account", return_value={"ok": False, "error": "not cookie"} ) as mock_sw, \
                    patch.object(client_module.GeminiClient, "bind_relay", return_value={
                        "ok": True,
                        "active_relay_label": "brave",
                        "browser": "brave",
                        "account_email": "pro2@gmail.com",
                    }) as mock_bind, \
                    patch.object(client_module.GeminiClient, "rotate_account", return_value={
                        "ok": True,
                        "active_relay": "chrome",
                        "active_account": "relay-chrome.json",
                    }), \
                    patch.object(client_module.GeminiClient, "get_accounts", return_value={
                        "active_relay": "chrome",
                        "relays": [
                            {"label": "brave", "browser": "brave", "account_email": "pro2@gmail.com", "model_badge": "4 Argon High", "online": True, "active": False},
                            {"label": "chrome", "browser": "chrome", "account_email": "pro1@gmail.com", "model_badge": "4 Argon High", "online": True, "active": True},
                        ],
                        "accounts": [{"index": 0, "name": "relay-chrome.json", "path": "accounts/relay-chrome.json", "active": True}],
                    }), \
                    patch.object(client_module.GeminiClient, "get_health", return_value={
                        "active_relay": "chrome",
                        "last_upstream_model": {"badge": "4 Argon High", "relay_label": "chrome", "thinking_chars": 120},
                    }):
                self.assertTrue(handle_command("/account brave", st))
                self.assertEqual(st.active_account_label, "brave")
                mock_sw.assert_not_called()
                mock_bind.assert_called_once_with("brave", session_id=st.session_id)
                self.assertEqual(st.turn_count, 5)
                self.assertEqual(st.messages, [{"role": "user", "content": "preserve full task context"}])

                self.assertTrue(handle_command("/account rotate", st))
                self.assertEqual(st.active_account_label, "chrome")
                self.assertEqual(st.turn_count, 5)

                self.assertTrue(handle_command("/account", st))
                out = buf.getvalue()
                self.assertIn("Browser Bridges (Chrome & Brave)", out)
                self.assertIn("brave", out)
                self.assertIn("chrome", out)
                self.assertIn("pro2@gmail.com", out)
        finally:
            renderer.console = orig

    def test_simultaneous_chrome_and_brave_strict_relay_disambiguation(self):
        import copy
        import threading
        from wsa_server import relay

        with relay._dom_relay_lock:
            snap_relay = copy.deepcopy(relay._dom_relay)
            relay._dom_relay["relays"] = {}
            relay._dom_relay["pending"].clear()
            relay._dom_relay["sessions"] = {}
            relay._dom_relay["active_relay_id"] = None
            relay._dom_relay["pinned_relay_label"] = None
            relay._dom_relay["explicit_relay_lock"] = False
        try:
            brave_info = {
                "client_id": "brave-strict-1",
                "browser": "brave",
                "label": "brave",
                "email": "b@example.com",
                "model_badge": "Claude Sonnet 4.6",
                "active_site": "claude.ai",
                "provider": "claude",
                "has_claude": "1",
                "claude_email": "b@example.com",
                "claude_model": "Claude Sonnet 4.6",
            }
            chrome_info = {
                "client_id": "chrome-strict-1",
                "browser": "chrome",
                "label": "chrome",
                "email": "c@example.com",
                "model_badge": "Claude Opus 4.6",
                "active_site": "claude.ai",
                "provider": "claude",
                "has_claude": "1",
                "claude_email": "c@example.com",
                "claude_model": "Claude Opus 4.6",
            }
            relay.dom_relay_poll_for_client(brave_info)
            relay.dom_relay_poll_for_client(chrome_info)

            sel_chrome = relay.select_active_relay("chrome", explicit=True)
            self.assertTrue(sel_chrome.get("ok"))
            self.assertEqual(sel_chrome.get("active_relay_id"), "chrome-strict-1")

            seen = {"brave": 0, "chrome": 0}
            stop_evt = threading.Event()

            def _poller(info, name):
                while not stop_evt.is_set():
                    item, _ = relay.dom_relay_poll_for_client(info)
                    if item and item.get("id"):
                        seen[name] += 1
                        relay.dom_relay_post_response(
                            item["id"],
                            response=f"answered by {name}",
                            model=info["model_badge"],
                            client_id=info["client_id"],
                        )
                        return
                    __import__("time").sleep(0.01)

            t_b = threading.Thread(target=_poller, args=(brave_info, "brave"), daemon=True)
            t_c = threading.Thread(target=_poller, args=(chrome_info, "chrome"), daemon=True)
            t_b.start()
            t_c.start()
            res = relay.dom_relay_put_prompt("hello chrome", timeout=5, pickup_timeout=5, provider="claude")
            stop_evt.set()
            t_b.join(timeout=2.0)
            t_c.join(timeout=2.0)

            self.assertEqual(res.get("response"), "answered by chrome")
            self.assertEqual(seen["chrome"], 1)
            self.assertEqual(seen["brave"], 0)
        finally:
            with relay._dom_relay_lock:
                relay._dom_relay.clear()
                relay._dom_relay.update(snap_relay)

    def test_per_provider_chat_urls_preserved_across_model_switches(self):
        from wsa_agent.commands import handle_command

        st = SessionState(project_root=Path("."))
        self.assertEqual(st.model, "gemini")
        st.active_chat_url = "https://gemini.google.com/app/g-202"
        self.assertEqual(st.chat_urls.get("gemini"), "https://gemini.google.com/app/g-202")

        self.assertEqual(handle_command("/model claude", st), (True, True))
        self.assertEqual(st.model, "claude")
        self.assertEqual(st.active_chat_url, "")
        st.active_chat_url = "https://claude.ai/chat/c-101"
        self.assertEqual(st.chat_urls.get("claude"), "https://claude.ai/chat/c-101")
        self.assertEqual(st.chat_urls.get("gemini"), "https://gemini.google.com/app/g-202")

        self.assertEqual(handle_command("/model gemini", st), (True, True))
        self.assertEqual(st.active_chat_url, "https://gemini.google.com/app/g-202")
        self.assertEqual(handle_command("/model claude", st), (True, True))
        self.assertEqual(st.active_chat_url, "https://claude.ai/chat/c-101")

    def test_session_relay_keeps_same_chat_when_tool_output_contains_compacted_or_fence_lang(self):
        import threading
        module = __import__("wsa_server")
        relay_snap, cfg_snap, orig_dir = self._snapshot_module_state(module)
        sid = "sess-same-chat-compacted"
        try:
            with module._dom_relay_lock:
                module._dom_relay["relays"] = {}
                module._dom_relay["active_relay_id"] = "brave-keep"
                module._dom_relay["sessions"] = {}
                module._dom_relay["pending"].clear()
            module.register_or_touch_relay({"client_id": "brave-keep", "browser": "brave", "label": "brave"})

            # Check parse_tool_calls + _extract_tool_calls agree on tool_call json\r\n fence
            raw_reply = 'Checking files.\n```tool_call json\r\n{"name": "read_file", "arguments": {"path": "agent.py"}}\r\n```'
            cleaned_srv, calls_srv = module.parse_tool_calls(raw_reply)
            calls_cli = _extract_tool_calls({"content": raw_reply})
            self.assertEqual(len(calls_srv), 1)
            self.assertEqual(len(calls_cli), 1)
            self.assertEqual(calls_srv[0]["function"]["name"], calls_cli[0]["function"]["name"])

            tc = [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"agent.py"}'}}]
            module.record_session_relay_turn(
                sid, cleaned_srv, tc, chat_url="https://gemini.google.com/u/4/app/conv999", relay_id="brave-keep"
            )
            tools_def = [{"type": "function", "function": {"name": "read_file", "description": "Read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
            msgs = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "Inspect agent.py"},
                {"role": "assistant", "content": cleaned_srv, "tool_calls": tc},
                {"role": "tool", "name": "read_file", "tool_call_id": "c1", "content": 'if "[Compacted" not in content:\n    pass'},
            ]
            full_p, imgs = module.messages_to_prompt(msgs, tools_def)
            p_delta, fb, _, new_chat, _hint = module.prepare_session_relay_prompt(sid, msgs, tools_def, full_p, imgs)
            self.assertFalse(new_chat)
            self.assertIn('[Compacted', p_delta)
            self.assertEqual(fb, full_p)

            # Pending item dispatched for same-chat carries server chat_url for extension recovery
            with module._dom_relay_lock:
                module._dom_relay["pending"]["req-chat-1"] = {
                    "prompt": p_delta,
                    "fallback_prompt": fb,
                    "session_id": sid,
                    "target_relay_id": "brave-keep",
                    "dispatched": False,
                    "dispatched_to": "",
                    "event": threading.Event(),
                    "response": None,
                    "error": None,
                    "thinking": "",
                    "model": "",
                    "chat_url": "",
                    "new_chat": False,
                }
            picked, _ = module.dom_relay_poll_for_client({"client_id": "brave-keep", "browser": "brave", "label": "brave"})
            self.assertIsNotNone(picked)
            self.assertFalse(picked["new_chat"])
            self.assertEqual(picked["chat_url"], "https://gemini.google.com/u/4/app/conv999")
        finally:
            self._restore_module_state(module, relay_snap, cfg_snap, orig_dir)

    def test_busy_heartbeat_and_post_response_refresh_prevent_false_offline_error(self):
        import threading
        import time as _time
        module = __import__("wsa_server")
        relay_snap, cfg_snap, orig_dir = self._snapshot_module_state(module)
        try:
            with module._dom_relay_lock:
                module._dom_relay["relays"] = {}
                module._dom_relay["active_relay_id"] = None
                module._dom_relay["pending"].clear()

            binfo = {"client_id": "brave-hb", "browser": "brave", "label": "brave"}
            module.register_or_touch_relay(binfo)
            module.select_active_relay(target="brave", explicit=True)

            # Simulate an 85s gap while extension is busy: busy heartbeat refreshes last_seen without claiming pending prompt
            with module._dom_relay_lock:
                module._dom_relay["relays"]["brave-hb"]["last_seen"] = _time.time() - 85.0
                module._dom_relay["pending"]["req-hb-1"] = {
                    "prompt": "delta",
                    "fallback_prompt": "full",
                    "session_id": "s-hb",
                    "target_relay_id": "brave-hb",
                    "dispatched": True,
                    "dispatched_to": "brave-hb",
                    "event": threading.Event(),
                    "response": None,
                    "error": None,
                    "thinking": "",
                    "model": "",
                    "chat_url": "",
                    "new_chat": False,
                }

            hb_item, hb_reg = module.dom_relay_poll_for_client(binfo, busy=True)
            self.assertIsNone(hb_item)
            self.assertTrue(hb_reg["is_active"])
            with module._dom_relay_lock:
                self.assertLess(_time.time() - module._dom_relay["relays"]["brave-hb"]["last_seen"], 2.0)

            # Simulate stale last_seen right before post_response finishes (e.g. 88s turn) -> post_response refreshes last_seen
            with module._dom_relay_lock:
                module._dom_relay["relays"]["brave-hb"]["last_seen"] = _time.time() - 88.0
            ok = module.dom_relay_post_response("req-hb-1", response="done", model="4 Argon High", client_id="brave-hb")
            self.assertTrue(ok)
            with module._dom_relay_lock:
                self.assertLess(_time.time() - module._dom_relay["relays"]["brave-hb"]["last_seen"], 2.0)
        finally:
            self._restore_module_state(module, relay_snap, cfg_snap, orig_dir)

    def test_reselecting_same_relay_preserves_session_same_chat_affinity(self):
        module = __import__("wsa_server")
        relay_snap, cfg_snap, orig_dir = self._snapshot_module_state(module)
        sid = "sess-rebind-same"
        try:
            with module._dom_relay_lock:
                module._dom_relay["relays"] = {}
                module._dom_relay["active_relay_id"] = None
                module._dom_relay["sessions"] = {}

            module.register_or_touch_relay({"client_id": "brave-rebind", "browser": "brave", "label": "brave"})
            module.register_or_touch_relay({"client_id": "chrome-rebind", "browser": "chrome", "label": "chrome"})
            tc = [{"id": "c_rb", "type": "function", "function": {"name": "list_dir", "arguments": '{"path":"."}'}}]
            module.record_session_relay_turn(
                sid, "", tc, chat_url="https://gemini.google.com/u/4/app/keepme", relay_id="brave-rebind"
            )
            msgs = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "list"},
                {"role": "assistant", "content": "", "tool_calls": tc},
                {"role": "tool", "name": "list_dir", "tool_call_id": "c_rb", "content": "a.py"},
            ]
            full_p, imgs = module.messages_to_prompt(msgs, None)

            # Re-selecting Brave when Brave is already active must NOT clear same-chat affinity
            res_same = module.select_active_relay(target="brave", session_id=sid)
            self.assertTrue(res_same["ok"])
            self.assertEqual(res_same["cleared_chats"], 0)
            _, _, _, new_same, _hint = module.prepare_session_relay_prompt(sid, msgs, None, full_p, imgs)
            self.assertFalse(new_same)

            # Switching to Chrome MUST clear Brave's session affinity so Chrome gets full context
            res_diff = module.select_active_relay(target="chrome", session_id=sid)
            self.assertTrue(res_diff["ok"])
            self.assertGreaterEqual(res_diff["cleared_chats"], 1)
            _, _, _, new_diff, _hint = module.prepare_session_relay_prompt(sid, msgs, None, full_p, imgs)
            self.assertTrue(new_diff)
        finally:
            self._restore_module_state(module, relay_snap, cfg_snap, orig_dir)


class ToolCallExtractionAndStreamingRegressionTests(unittest.TestCase):
    def test_malformed_closing_fence_and_variant_openers_parse_cleanly(self):
        module = __import__("wsa_server")
        from wsa_agent.agent import _extract_tool_calls

        prose = (
            "Let's read the rest of tests/test_wsa_agent_regressions.py around renderer/TUI tests "
            "(lines 800\u20131355) so we don't accidentally break any existing test contracts while upgrading "
            "the live TUI and agent loop."
        )
        payload = '{"name": "read_file", "arguments": {"path": "tests/test_wsa_agent_regressions.py", "start_line": 800, "end_line": 1355}}'
        allowed = {"read_file", "patch_file", "write_file", "run_command"}

        variants = [
            prose + "\n\n```toolcall\n" + payload + "\n```_]",
            prose + "\n\n```tool_calls\n" + payload + "\n```_]",
            prose + "\n\n```json\n" + payload + "\n```_]",
            prose + "\n\n```\n" + payload + "\n```_]",
            prose + "\n\n```\u200btool_call\n" + payload + "\n```_]",
            prose + "\n\n   ```tool_call\n" + payload + "\n   ```_]",
            prose + "\n\n````\n" + payload + "\n```_]\n````",
            prose + "\n\n    " + payload + "\n    ```_]",
            prose + "\n\n" + payload + "\n```_]",
        ]

        for idx, raw in enumerate(variants):
            cleaned_srv, srv_calls = module.parse_tool_calls(raw, allowed_names=allowed)
            self.assertEqual(len(srv_calls), 1, f"server variant {idx} failed: {raw!r}")
            self.assertEqual(srv_calls[0]["function"]["name"], "read_file")
            self.assertEqual(json.loads(srv_calls[0]["function"]["arguments"])["start_line"], 800)
            self.assertEqual(cleaned_srv, prose, f"variant {idx} left stray artifact: {cleaned_srv!r}")

            cli_calls = _extract_tool_calls({"content": raw}, allowed_names=allowed)
            self.assertEqual(len(cli_calls), 1, f"client variant {idx} failed")
            self.assertEqual(cli_calls[0]["function"]["name"], "read_file")

    def test_multi_call_partial_mismatch_extracts_both_calls_without_duplication(self):
        module = __import__("wsa_server")
        from wsa_agent.agent import _extract_tool_calls

        c1 = '{"name": "read_file", "arguments": {"path": "tests/test_wsa_agent_regressions.py", "start_line": 1, "end_line": 300}}'
        c2 = '{"name": "read_file", "arguments": {"path": "wsa_agent/__main__.py", "start_line": 1, "end_line": 300}}'
        raw = (
            "Let's read both files.\n\n"
            f"```tool_call\n{c1}\n```\n\n"
            f"```json\n{c2}\n```_]"
        )
        allowed = {"read_file", "write_file"}
        cleaned_srv, srv_calls = module.parse_tool_calls(raw, allowed_names=allowed)
        self.assertEqual(cleaned_srv, "Let's read both files.")
        self.assertEqual(len(srv_calls), 2)
        self.assertEqual(
            [json.loads(c["function"]["arguments"])["path"] for c in srv_calls],
            ["tests/test_wsa_agent_regressions.py", "wsa_agent/__main__.py"],
        )

        # Even if server only extracted call 1 and left call 2 in content, client recovers call 2 without duplicating call 1
        merged = _extract_tool_calls(
            {
                "tool_calls": [srv_calls[0]],
                "content": f"Let's read both files.\n\n```json\n{c2}\n```_]",
            },
            allowed_names=allowed,
        )
        self.assertEqual(len(merged), 2)
        self.assertEqual(
            [json.loads(c["function"]["arguments"])["path"] for c in merged],
            ["tests/test_wsa_agent_regressions.py", "wsa_agent/__main__.py"],
        )

    def test_non_tool_json_and_python_code_blocks_are_not_misclassified(self):
        module = __import__("wsa_server")
        from wsa_agent.agent import _extract_tool_calls

        non_tool_samples = [
            'Here is config:\n\n```json\n{"name": "my-app", "version": "1.0"}\n```',
            'Here is python:\n\n```python\ndef fn():\n    return {"name": "a", "arguments": {}}\n```',
            'Inline prose {"name": "read_file", "arguments": {"path": "x"}} continues here.',
        ]
        for sample in non_tool_samples:
            cleaned, calls = module.parse_tool_calls(sample, allowed_names={"read_file"})
            self.assertEqual(calls, [])
            self.assertEqual(cleaned, sample)
            self.assertEqual(_extract_tool_calls({"content": sample}, allowed_names={"read_file"}), [])

        # Nested ```tool_call inside write_file JSON string argument must not spawn a fake nested tool call
        nested = (
            'Writing file:\n```tool_call\n'
            + json.dumps({"name": "write_file", "arguments": {"path": "doc.md", "content": "Example:\n```tool_call\n{\"name\": \"bogus\"}\n```"}})
            + "\n```"
        )
        cleaned_nested, nested_calls = module.parse_tool_calls(nested, allowed_names={"write_file"})
        self.assertEqual(cleaned_nested, "Writing file:")
        self.assertEqual(len(nested_calls), 1)
        self.assertEqual(nested_calls[0]["function"]["name"], "write_file")

    def test_streaming_hold_pos_catches_json_and_indented_fences_but_streams_python(self):
        module = __import__("wsa_server")

        json_buf = 'Checking tests.\n\n```json\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```_]'
        hold_json = module._find_pending_tool_hold_pos(json_buf)
        self.assertGreaterEqual(hold_json, 0)
        self.assertTrue(json_buf[hold_json:].lstrip().startswith("```json"))

        indented_buf = 'Checking tests.\n\n   ```tool_call\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```'
        hold_ind = module._find_pending_tool_hold_pos(indented_buf)
        self.assertGreaterEqual(hold_ind, 0)
        _, calls = module.parse_tool_calls(indented_buf[hold_ind:])
        self.assertEqual(len(calls), 1)

        py_buf = "Here is code:\n\n```python\ndef hello_world(name):\n    print(name)\n```\nDone!"
        self.assertEqual(module._find_pending_tool_hold_pos(py_buf), -1)

    def test_agent_auto_resumes_truncated_tool_call_and_ignores_normal_prose(self):
        agent_module = __import__("wsa_agent.agent", fromlist=["agent"])
        from wsa_agent.session import SessionState
        from wsa_agent.agent import _looks_like_broken_tool_attempt

        self.assertTrue(_looks_like_broken_tool_attempt('Oops ```tool_call\n{"name": "read_file", "arg', {"read_file"}))
        self.assertTrue(_looks_like_broken_tool_attempt('Let me check\n{"name": "read_file", "arguments": {"path":', {"read_file"}))
        self.assertFalse(_looks_like_broken_tool_attempt("All edits are complete and verified.", {"read_file"}))

        class FakeRenderer:
            def start_thinking(self):
                pass
            def stop_thinking(self):
                pass
            def feed(self, token):
                pass
            def feed_thinking(self, token):
                pass
            def reset_answer(self, full_retry=False):
                pass
            def finish(self):
                pass
            def get_thinking_text(self):
                return ""

        class FakeClient:
            def __init__(self):
                self.calls = 0
                self.messages = []
            def stream(self, messages, **kwargs):
                self.calls += 1
                self.messages.append([dict(m) for m in messages])
                if self.calls == 1:
                    return {"content": 'Checking file...\n```tool_call\n{"name": "read_file", "arguments": {"path": "x"'}
                return {"content": "Finished cleanly."}

        state = SessionState(free_mode=False)
        client = FakeClient()
        with patch.object(agent_module.R, "StreamRenderer", FakeRenderer), \
             patch.object(agent_module.R, "begin_agent_response"), \
             patch.object(agent_module.R, "print_warn"), \
             patch.object(agent_module.R, "print_info"), \
             patch.object(agent_module.time, "sleep"), \
             patch.object(agent_module, "set_project_root"), \
             patch.object(agent_module, "_compact_messages_for_web_limit"):
            agent_module.run_turn("continue", state, client)

        self.assertEqual(client.calls, 2)
        self.assertIn("[Continue]: Your previous tool_call block", client.messages[1][-1]["content"])

    def test_live_thinking_spinner_dynamic_ticks_and_auto_checkpoint(self):
        from wsa_agent import renderer
        from wsa_agent.session import SessionState

        sr = renderer.StreamRenderer(round_num=1, status_hint="Analyzing Read(wsa_agent/renderer.py) output…")
        sr._thinking_start = renderer.time.time() - 2.5
        sr._in_thinking = True

        # Dynamic proxy re-evaluates on every Rich Live tick (__rich__) rather than freezing on frame 0
        tick1 = sr._live_proxy.__rich__()
        tick2 = sr._live_proxy.__rich__()
        self.assertNotEqual(tick1.plain, tick2.plain)
        self.assertIn("step 2", tick1.plain)
        self.assertIn("⎿  Analyzing Read(wsa_agent/renderer.py) output…", tick1.plain)

        # Feeding thinking tokens updates word counter and live reasoning tree line
        sr.feed_thinking("Inspecting StreamRenderer live refresh loop and spinner verbs now.")
        tick3 = sr._live_proxy.__rich__()
        self.assertIn("w thought", tick3.plain)
        self.assertIn("⎿  Inspecting StreamRenderer live refresh loop", tick3.plain)
        sr._stop_live()

        # Live answer streaming hides raw tool_call fences from Markdown preview and shows Crafting tool call footer
        sr.feed('Here is the fix.\n```tool_call\n{"name": "read_file", "arguments": {"path": "x.py"}}\n```')
        live_group = sr._live_proxy.__rich__()
        footer_text = live_group.renderables[-1].plain if hasattr(live_group, "renderables") else live_group.plain
        self.assertIn("Crafting tool call…", footer_text)
        self.assertNotIn("read_file", sr._last_preview)
        sr._stop_live()

        # SessionState compact_history includes Recent Tool Milestones and auto_checkpoint persists cleanly
        with tempfile.TemporaryDirectory() as tmp:
            st = SessionState(project_root=Path(tmp))
            st.turn_count = 1
            st.tool_history.append({"turn": 1, "tool": "patch_file", "args": {"path": "wsa_agent/renderer.py"}, "ok": True})
            st.messages = [
                {"role": "user", "content": f"Goal {i}"} if i % 2 == 0 else {"role": "assistant", "content": f"Done {i}"}
                for i in range(6)
            ]
            removed = st.compact_history(keep=2)
            self.assertGreater(removed, 0)
            self.assertIn("Recent Tool Milestones: patch_file(wsa_agent/renderer.py)", st.context_summary)
            with patch.object(SessionState, "_save_path", new_callable=PropertyMock) as mock_save:
                ckpt_file = Path(tmp) / "checkpoint.json"
                mock_save.return_value = ckpt_file
                saved = st.auto_checkpoint()
                self.assertEqual(saved, str(ckpt_file))
                self.assertTrue(ckpt_file.exists())

    def test_repaired_tool_json_streaming_hold_and_renderer_leaked_tool_stripping(self):
        """Server JSON repair + streaming hold + renderer leak stripping + Live width clamping."""
        import io
        import re
        from rich.console import Console
        from wsa_agent import renderer
        from wsa_agent import __main__ as main_mod
        from wsa_agent.session import SessionState
        from wsa_agent.prompts import SYSTEM_PROMPT

        module = __import__("wsa_server")

        # 1. SYSTEM_PROMPT tells the model not to echo full files in prose during write/patch
        self.assertIn("never echo full file contents", SYSTEM_PROMPT.lower())

        # 2. Malformed write_file JSON (literal newline + trailing comma) is parsed & stripped server-side
        malformed = (
            "Applied the fix.\n"
            "```tool_call\n"
            '{"name": "write_file", "arguments": {"path": "foo.py", "content": "def a():\n    return 1\n",},}\n'
            "```\n"
        )
        cleaned, calls = module.parse_tool_calls(malformed, allowed_names={"write_file", "patch_file"})
        self.assertEqual(cleaned, "Applied the fix.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "write_file")
        args_obj = json.loads(calls[0]["function"]["arguments"])
        self.assertIn("def a():\n    return 1", args_obj["content"])

        # 3. Streaming hold captures multiline json fence where "arguments" precedes "name"
        pending = 'Brief note.\n```json\n[\n  {\n    "arguments": {"path": "a.py"},\n    "name": "read_file"\n'
        hold_idx = module._find_pending_tool_hold_pos(pending)
        self.assertGreaterEqual(hold_idx, 0)
        self.assertNotIn('"arguments"', pending[:hold_idx])

        # 4. Renderer strips leaked tool JSON fences/Code snippets/bare tool calls while preserving normal JSON
        leaked = (
            "Summary line.\n"
            '```json\n{"name": "patch_file", "arguments": {"path": "x.py", "new_str": "full code"}}\n```\n'
            'Code snippet\n{"name": "write_file", "arguments": {"path": "y.py", "content": "secret"}}\n\n'
            '{"name": "read_file", "arguments": {"path": "z.py"}}\n'
            'Config stays visible:\n```json\n{"host": "127.0.0.1", "port": 8081}\n```\n'
        )
        stripped = renderer._strip_internal_tags(leaked)
        self.assertIn("Summary line.", stripped)
        self.assertIn('"port": 8081', stripped)
        self.assertNotIn("patch_file", stripped)
        self.assertNotIn("write_file", stripped)
        self.assertNotIn("read_file", stripped)

        # 5. Live thinking renderable clamps lines to console width and preserves .plain without wrapping
        buf = io.StringIO()
        orig = renderer.console
        try:
            renderer.console = Console(file=buf, force_terminal=False, width=60, height=24, highlight=False)
            sr = renderer.StreamRenderer(round_num=0, status_hint="Very long status hint " + ("x" * 90))
            sr._in_thinking = True
            rendered = sr._live_proxy.__rich__()
            for line in rendered.plain.splitlines():
                self.assertLessEqual(len(line), 56)
            sr._stop_live()
        finally:
            renderer.console = orig

        # 6. Bottom toolbar adapts to narrow console width without wrapping past _prompt_rule_width()
        buf2 = io.StringIO()
        try:
            renderer.console = Console(file=buf2, force_terminal=False, width=76, highlight=False)
            st = SessionState(project_root=Path("."))
            tb = main_mod._build_bottom_toolbar(st)
            plain_tb = re.sub(r"\x1b\[[0-9;]*m", "", tb)
            for line in plain_tb.splitlines():
                self.assertLessEqual(len(line), 74)
        finally:
            renderer.console = orig


class TestLongRunAndDeadTabHardening(unittest.TestCase):
    """Regressions for long agentic runs, multi-project isolation, dead tabs, and rollover."""

    def test_truncate_output_and_project_venv_and_ignore_scope(self):
        from pathlib import Path
        from wsa_agent import tools

        huge = "\n".join(f"line-{i}: {'x' * 40}" for i in range(1000))
        clipped = tools._truncate_tool_output(huge, max_chars=2400, label="STDOUT")
        self.assertLessEqual(len(clipped), 2650)
        self.assertIn("line-0:", clipped)
        self.assertIn("line-999:", clipped)
        self.assertIn("truncated for WSA web budget", clipped)

        orig_root = tools.get_project_root()
        with tempfile.TemporaryDirectory() as proj_a, tempfile.TemporaryDirectory() as proj_b:
            try:
                a_path = Path(proj_a)
                b_path = Path(proj_b)
                fake_win_py = a_path / ".venv" / "Scripts" / "python.exe"
                fake_posix_py = b_path / ".venv" / "bin" / "python"
                fake_win_py.parent.mkdir(parents=True, exist_ok=True)
                fake_posix_py.parent.mkdir(parents=True, exist_ok=True)
                fake_win_py.write_text("", encoding="utf-8")
                fake_posix_py.write_text("", encoding="utf-8")

                self.assertEqual(Path(tools._detect_python_executable(a_path)), fake_win_py)
                self.assertEqual(Path(tools._detect_python_executable(b_path)), fake_posix_py)

                # Project folders are never artificially ignored while standard build/env dirs are excluded; backups isolate across projects
                (a_path / "src_pkg").mkdir()
                (a_path / "app.py").write_text("x = 1\n", encoding="utf-8")
                tools.set_project_root(a_path)
                self.assertFalse(tools._is_ignored("src_pkg", True))
                self.assertTrue(tools._is_ignored(".venv", True))
                self.assertTrue(tools._is_ignored(".git", True))
                tools.execute_tool("read_file", {"path": "app.py"})
                tools.execute_tool("write_file", {"path": "app.py", "content": "x = 2\n"})
                self.assertIn("app.py", tools.get_file_backups())

                tools.set_project_root(b_path)
                self.assertEqual(tools.get_file_backups(), {})
                self.assertEqual(tools.get_backup_order(), [])
            finally:
                tools.set_project_root(orig_root)

    def test_check_tests_exist_skips_venv_and_node_modules(self):
        from pathlib import Path
        from wsa_agent.agent import _check_tests_exist

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".venv" / "lib").mkdir(parents=True)
            (root / ".venv" / "lib" / "test_ignore_me.py").write_text("", encoding="utf-8")
            (root / "node_modules" / "pkg").mkdir(parents=True)
            (root / "node_modules" / "pkg" / "test_ignore_js.py").write_text("", encoding="utf-8")
            self.assertFalse(_check_tests_exist(root))

            (root / "src").mkdir()
            (root / "src" / "test_real.py").write_text("def test_ok(): pass\n", encoding="utf-8")
            self.assertTrue(_check_tests_exist(root))

    def test_mid_turn_abort_checkpoint_and_loop_breaker(self):
        import io
        from pathlib import Path
        from rich.console import Console
        from wsa_agent import agent as agent_mod
        from wsa_agent import renderer, tools
        from wsa_agent.session import SessionState

        orig_root = tools.get_project_root()
        orig_console = renderer.console
        buf = io.StringIO()
        renderer.console = Console(file=buf, force_terminal=False, width=100, highlight=False)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "note.txt").write_text("hello", encoding="utf-8")
                st = SessionState(project_root=root, permission_mode="auto")

                # 1. Partial progress saved if round 1 raises ConnectionError after round 0 completed a tool
                call_num = {"n": 0}

                class _MidAbortClient:
                    def stream(self, **kwargs):
                        if kwargs.get("use_tools") is False:
                            return {"content": '{"mode":"direct","reason":"isolated task"}'}
                        call_num["n"] += 1
                        if call_num["n"] == 1:
                            return {
                                "content": None,
                                "thinking": None,
                                "tool_calls": [{
                                    "id": "call_r0",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": '{"path": "note.txt"}'},
                                }],
                                "finish_reason": "tool_calls",
                                "usage": {},
                            }
                        raise ConnectionError("simulated network drop mid-turn")

                with patch.object(SessionState, "_save_path", new_callable=PropertyMock) as mock_save:
                    ckpt_path = root / "mid_turn_ckpt.json"
                    mock_save.return_value = ckpt_path
                    agent_mod.run_turn("Read note and continue", st, _MidAbortClient())

                self.assertEqual(st.turn_count, 1)
                self.assertEqual([m["role"] for m in st.messages], ["user", "assistant", "tool"])
                self.assertTrue(ckpt_path.exists())

                # 2. Repeated identical failing tool calls halt before spinning 500 rounds
                st2 = SessionState(project_root=root, permission_mode="auto")
                loop_calls = {"n": 0}

                class _LoopingFailClient:
                    def stream(self, **kwargs):
                        if kwargs.get("use_tools") is False:
                            return {"content": '{"mode":"direct","reason":"isolated task"}'}
                        loop_calls["n"] += 1
                        return {
                            "content": None,
                            "thinking": None,
                            "tool_calls": [{
                                "id": f"call_bad_{loop_calls['n']}",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path": "missing_forever.txt"}'},
                            }],
                            "finish_reason": "tool_calls",
                            "usage": {},
                        }

                with patch.object(SessionState, "_save_path", new_callable=PropertyMock) as mock_save2:
                    mock_save2.return_value = root / "loop_ckpt.json"
                    agent_mod.run_turn("Keep trying missing file", st2, _LoopingFailClient())

                self.assertEqual(loop_calls["n"], 5)
        finally:
            renderer.console = orig_console
            tools.set_project_root(orig_root)

    def test_client_timeout_and_server_rollover_and_request_dedup(self):
        import threading
        import time
        import uuid
        from wsa_agent.client import WSAClient

        cli = WSAClient("http://127.0.0.1:8081/v1", "sk-wsa", timeout=300)
        self.assertGreaterEqual(cli._client.timeout.read, 320.0)

        m = __import__("wsa_server")
        sid = f"rollover-{uuid.uuid4().hex[:8]}"
        with m._dom_relay_lock:
            m._dom_relay["sessions"].pop(sid, None)
            m._dom_relay["recent_requests"].clear()
            old_active = m._dom_relay.get("active_relay_id")
            m._dom_relay["active_relay_id"] = "brave-test"

        orig_rollover = m.CONFIG.get("dom_relay_rollover_turns", 24)
        try:
            m.CONFIG["dom_relay_rollover_turns"] = 3
            for i in range(2):
                m.record_session_relay_turn(sid, f"ok {i}", None, chat_url="https://gemini.google.com/app/convA", relay_id="brave-test")
            self.assertEqual(m._dom_relay["sessions"][sid]["turns"], 2)
            msgs = [{"role": "assistant", "content": "ok 1"}, {"role": "user", "content": "next"}]
            _, _, _, is_new, _hint = m.prepare_session_relay_prompt(sid, msgs, None, "FULL", [])
            self.assertFalse(is_new)

            # On turn 3 (>= rollover threshold), server forces clean rollover with full context
            m.record_session_relay_turn(sid, "ok 2", None, chat_url="https://gemini.google.com/app/convA", relay_id="brave-test")
            p_send, p_fb, _, is_new_roll, _hint = m.prepare_session_relay_prompt(
                sid, [{"role": "assistant", "content": "ok 2"}, {"role": "user", "content": "next"}], None, "FULL_CTX", []
            )
            self.assertTrue(is_new_roll)
            self.assertEqual(p_send, "FULL_CTX")
            self.assertEqual(p_fb, "FULL_CTX")

            # New chat URL resets counter back to 1
            m.record_session_relay_turn(sid, "rolled", None, chat_url="https://gemini.google.com/app/convB", relay_id="brave-test")
            self.assertEqual(m._dom_relay["sessions"][sid]["turns"], 1)

            # Cached client_request_id avoids duplicate queuing
            cached_key = f"{sid}:req-abc"
            ev_done = threading.Event()
            ev_done.set()
            with m._dom_relay_lock:
                m._dom_relay["recent_requests"][cached_key] = {
                    "event": ev_done,
                    "result": {"response": "cached argon reply", "thinking": "", "model": "4 Argon High", "chat_url": "", "relay_id": "brave-test"},
                    "error": None,
                    "ts": time.time(),
                }
            events = list(m.dom_relay_iter("prompt", session_id=sid, client_request_id="req-abc"))
            self.assertEqual(events, [("result", {"response": "cached argon reply", "thinking": "", "model": "4 Argon High", "chat_url": "", "relay_id": "brave-test"})])
        finally:
            m.CONFIG["dom_relay_rollover_turns"] = orig_rollover
            with m._dom_relay_lock:
                m._dom_relay["sessions"].pop(sid, None)
                m._dom_relay["recent_requests"].clear()
                m._dom_relay["active_relay_id"] = old_active

    def test_extension_background_dead_tab_recovery_and_tail_helpers_in_node(self):
        import subprocess
        from pathlib import Path

        bg_path = Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js"
        script = f"""
const bg = require({json.dumps(str(bg_path))});
const assert = require("assert");
assert.strictEqual(bg.extractGeminiConvId("https://gemini.google.com/u/1/app/abc123_def"), "abc123_def");
assert.strictEqual(bg.parseAuthUserFromUrl("https://gemini.google.com/u/2/app"), "2");
assert.strictEqual(bg.evaluateTabRecoveryAction(null, null), "open_replacement");
assert.strictEqual(bg.evaluateTabRecoveryAction({{ id: 1, discarded: true }}, {{ hasEditor: true }}), "wake_reload");
assert.strictEqual(bg.evaluateTabRecoveryAction({{ id: 2, discarded: false, status: "unloaded" }}, {{ hasEditor: true }}), "wake_reload");
assert.strictEqual(bg.evaluateTabRecoveryAction({{ id: 3, discarded: false, status: "complete" }}, {{ hasEditor: false }}), "wake_reload");
assert.strictEqual(bg.evaluateTabRecoveryAction({{ id: 4, discarded: false, status: "complete" }}, {{ hasEditor: true }}), "ok");
assert.deepStrictEqual(bg.computeTailDomRange(50, 49), {{ start: 49, count: 50, hasNew: true }});
assert.deepStrictEqual(bg.computeTailDomRange(49, 49), {{ start: 48, count: 49, hasNew: false }});
const picked = bg.pickGeminiTab([
  {{ id: 10, active: true, url: "https://gemini.google.com/app" }},
  {{ id: 11, active: false, url: "https://gemini.google.com/u/0/app/xyz999" }},
], {{ convId: "xyz999", tabId: 10 }}, "");
assert.strictEqual(picked.id, 11);
console.log("NODE_HELPERS_OK");
"""
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        self.assertIn("NODE_HELPERS_OK", proc.stdout)

    def test_dynamic_tui_account_model_toolbar_and_divyansh_branding(self):
        import io
        from pathlib import Path
        from rich.console import Console
        from wsa_agent import __main__ as main_mod
        from wsa_agent import renderer, tui
        from wsa_agent.session import ALL_MODELS, SessionState, get_model_info

        with tempfile.TemporaryDirectory() as tmpdir:
            st = SessionState(project_root=Path(tmpdir))
            st.sync_account_from_health({
                "active_relay": "brave",
                "active_relay_id": "brave-2d87d2",
                "active_browser": "brave",
                "active_email": "example@example.com",
                "relays": [{
                    "client_id": "brave-2d87d2",
                    "browser": "brave",
                    "label": "brave",
                    "account_email": "example@example.com",
                    "active": True,
                }],
            })
            st.active_chat_url = "https://gemini.google.com/app/projchat001"

            # Save + Load preserves dynamic account/chat metadata
            save_file = Path(tmpdir) / "proj-session.json"
            with patch.object(SessionState, "_save_path", new_callable=lambda: property(lambda self: save_file)):
                saved = st.save()
            self.assertTrue(Path(saved).exists())

            st2 = SessionState(project_root=Path(tmpdir))
            st2.load(save_file)
            self.assertEqual(st2.active_account_label, "brave")
            self.assertEqual(st2.active_account_email, "example@example.com")
            self.assertEqual(st2.active_browser, "brave")
            self.assertEqual(st2.active_chat_url, "https://gemini.google.com/app/projchat001")

            # Model catalog dynamically adapts to bound browser/account
            dyn_info = get_model_info("gemini", state=st2)
            self.assertEqual(dyn_info["transport"], "Brave Web · gemini.google.com")
            self.assertEqual(dyn_info["requires"], "Brave Web")

            items = tui._model_display_items(state=st2)
            self.assertTrue(any("Brave Web" in it["meta"] for it in items))

            # Welcome, models table, status, and bottom toolbar reflect account + branding
            buf = io.StringIO()
            orig = renderer.console
            try:
                renderer.console = Console(file=buf, force_terminal=False, width=108, highlight=False)
                renderer.print_welcome(st2, server_ok=True)
                renderer.print_models_table(ALL_MODELS, current_model=st2.model, state=st2)
                renderer.print_status(st2)
                out = buf.getvalue()
            finally:
                renderer.console = orig

            self.assertIn("Brave Web", out)
            self.assertIn("example@example.com", out)
            self.assertIn("Engineered by Divyansh Saraf", out)
            self.assertIn("projchat001", out)
            self.assertNotIn("Brave DOM Relay", out)
            self.assertNotIn("Chrome DOM Relay", out)

            toolbar = main_mod._build_bottom_toolbar(st2)
            self.assertIn("brave", toolbar)
            self.assertIn("Gemini Web", toolbar)

    def test_extension_streamlined_auth_cookies_branding_and_same_chat_url_selection(self):
        import re
        import subprocess
        from pathlib import Path

        ext_dir = Path(__file__).resolve().parent.parent / "wsa-browser-bridge"
        manifest = json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))
        popup_html = (ext_dir / "popup.html").read_text(encoding="utf-8")
        popup_js = (ext_dir / "popup.js").read_text(encoding="utf-8")
        bg_src = (ext_dir / "background.js").read_text(encoding="utf-8")

        self.assertEqual(manifest.get("name"), "WSA — Web Browser Bridge")
        self.assertEqual(manifest.get("version"), "2.0.4")
        self.assertEqual(manifest.get("version_name"), "2.0.4")
        self.assertEqual(manifest.get("author"), "Divyansh Saraf")
        self.assertIn("Engineered by Divyansh Saraf", manifest.get("description", ""))
        self.assertNotIn("downloads", manifest.get("permissions", []))
        self.assertIn("https://claude.ai/*", manifest.get("host_permissions", []))
        self.assertIn("https://*.google.com/*", manifest.get("host_permissions", []))
        self.assertIn("https://chatgpt.com/*", manifest.get("host_permissions", []))
        self.assertIn("https://chat.openai.com/*", manifest.get("host_permissions", []))
        self.assertIn("https://chat.deepseek.com/*", manifest.get("host_permissions", []))
        self.assertIn("https://venice.ai/*", manifest.get("host_permissions", []))
        cs_matches = [m for cs in (manifest.get("content_scripts") or []) for m in (cs.get("matches") or [])]
        self.assertIn("https://claude.ai/*", cs_matches)
        self.assertIn("https://gemini.google.com/*", cs_matches)
        self.assertIn("https://chatgpt.com/*", cs_matches)
        self.assertIn("https://chat.deepseek.com/*", cs_matches)
        self.assertIn("https://venice.ai/*", cs_matches)

        # Verify custom WSA extension PNG icons exist and have valid PNG signatures
        icons = manifest.get("icons") or {}
        action_icons = (manifest.get("action") or {}).get("default_icon") or {}
        for sz in ("16", "32", "48", "128"):
            rel_path = icons.get(sz)
            self.assertTrue(rel_path, f"Missing manifest icon size {sz}")
            self.assertEqual(action_icons.get(sz), rel_path)
            icon_bytes = (ext_dir / rel_path).read_bytes()
            self.assertTrue(icon_bytes.startswith(b"\x89PNG\r\n\x1a\n"), f"Invalid PNG signature in {rel_path}")

        # Verify unslopped single-button 4-site popup design
        self.assertIn("Web bridge", popup_html)
        self.assertIn("WSA", popup_html)
        self.assertIn("Sync", popup_html)
        self.assertIn("Engineered by Divyansh Saraf", popup_html)
        for field_lbl in ("Browser", "Active", "Connected sites", "Claude", "DeepSeek", "Gemini", "ChatGPT", "Venice"):
            self.assertIn(field_lbl, popup_html)
        self.assertEqual(len(re.findall(r"<button\b", popup_html)), 1)
        self.assertNotIn("Gemini Relay", popup_html)
        self.assertNotIn("v1.1", popup_html)
        self.assertNotIn("v3.7", popup_html)

        self.assertIn("SYNC_NOW", popup_js)
        self.assertIn("GET_STATUS", popup_js)
        self.assertNotIn("SAPISID", popup_js)
        self.assertNotIn("__Secure-1PSID", popup_js)
        self.assertNotIn("LEGACY_COOKIES", popup_js)
        for noisy in ("COMPASS", "AEC", "NID"):
            self.assertNotIn(noisy, popup_js)

        self.assertIn('"SYNC_NOW"', bg_src)
        self.assertIn("normalizeBrowserHint", bg_src)
        self.assertIn("_claudeInstallPageRuntimeFn", bg_src)
        self.assertIn("_claudeStartTurnFn", bg_src)
        self.assertIn("_claudePollTurnFn", bg_src)
        self.assertIn("_chatgptInstallPageRuntimeFn", bg_src)
        self.assertIn("_deepseekInstallPageRuntimeFn", bg_src)
        self.assertIn("_veniceInstallPageRuntimeFn", bg_src)
        self.assertIn("_installWsaDomUtilsFn", bg_src)
        self.assertNotIn("_domSubmitFn", bg_src)

        for js_name in ("background.js", "popup.js", "content_script_main.js", "content_script_bridge.js"):
            chk = subprocess.run(["node", "--check", str(ext_dir / js_name)], capture_output=True, text=True, check=False)
            self.assertEqual(chk.returncode, 0, msg=chk.stderr + chk.stdout)

        script = f"""
const bg = require({json.dumps(str(ext_dir / "background.js"))});
const assert = require("assert");
assert(bg.EXPORT_ORDER.includes("SAPISID"));
assert(bg.EXPORT_ORDER.includes("__Secure-1PSID"));
assert(bg.EXPORT_ORDER.includes("__Secure-3PSID"));
assert(!bg.EXPORT_ORDER.includes("COMPASS"));
assert(!bg.EXPORT_ORDER.includes("NID"));
assert(!bg.EXPORT_ORDER.includes("AEC"));
assert(!bg.EXPORT_ORDER.includes("__Host-1PLSID"));

assert.strictEqual(bg.normalizeBrowserHint(false), "chrome");
assert.strictEqual(bg.normalizeBrowserHint(true), "brave");
assert.strictEqual(bg.normalizeBrowserHint("BRAVE"), "brave");
assert.strictEqual(bg.extractClaudeConvId("https://claude.ai/chat/c-123-abc"), "c-123-abc");

// Pending convId overrides stale /app bare URL in savedSession
const chosen = bg.selectEffectiveChatUrl(
  {{ chatUrl: "https://gemini.google.com/app", convId: "" }},
  "https://gemini.google.com/u/1/app/conv_real_99"
);
assert.strictEqual(chosen.chatUrl, "https://gemini.google.com/u/1/app/conv_real_99");
assert.strictEqual(chosen.convId, "conv_real_99");

// Verify SW GET_STATUS + SYNC_NOW (Claude-only Brave, Gemini Brave, and Chrome isBrave=false override)
const fs = require("fs");
const vm = require("vm");
const bgSrc = fs.readFileSync({json.dumps(str(ext_dir / "background.js"))}, "utf8");

function makeSwHarness({{ tabsList, cookiesList, execResult, navIsBrave, storedState = {{}} }}) {{
  const listeners = [];
  const syncPayloads = [];
  const storageData = {{ ...storedState }};
  const sb = {{
    console,
    URL,
    URLSearchParams,
    Date,
    Math,
    Map,
    Set,
    Promise,
    AbortSignal: {{ timeout: () => undefined }},
    crypto: {{ randomUUID: () => "abc12345-6789" }},
    navigator: navIsBrave
      ? {{ userAgent: "Brave", userAgentData: {{ brands: [{{ brand: "Brave" }}] }} }}
      : {{ userAgent: "Chrome", userAgentData: {{ brands: [{{ brand: "Google Chrome" }}] }} }},
    setTimeout: () => 0, clearTimeout: () => {{}}, setInterval: () => 0, clearInterval: () => {{}},
    fetch: async (url, init) => {{
      const u = String(url);
      if (u.endsWith("/sync-cookies") && init && init.body) {{
        syncPayloads.push(JSON.parse(init.body));
      }}
      return {{
        ok: true,
        json: async () => (u.endsWith("/v1/health") ? {{ active_relay_id: "brave-abc123", active_relay: "brave" }} : {{ ok: true }}),
      }};
    }},
    chrome: {{
      runtime: {{
        id: "ext-test",
        onMessage: {{ addListener: (fn) => listeners.push(fn) }},
        onConnect: {{ addListener: () => {{}} }},
        onInstalled: {{ addListener: () => {{}} }},
        onStartup: {{ addListener: () => {{}} }},
      }},
      alarms: {{ create: () => {{}}, clear: () => {{}}, onAlarm: {{ addListener: () => {{}} }} }},
      storage: {{
        local: {{
          get: async (k) => {{
            if (!k) return {{ ...storageData }};
            if (typeof k === "string") return k in storageData ? {{ [k]: storageData[k] }} : {{}};
            return {{ ...storageData }};
          }},
          set: async (obj) => {{ Object.assign(storageData, obj || {{}}); }},
          remove: async () => {{}},
        }},
      }},
      tabs: {{
        query: async (q) => {{
          const pat = String((q && q.url) || "");
          if (pat.includes("gemini.google.com")) return tabsList.filter(t => String(t.url || "").includes("gemini.google.com"));
          if (pat.includes("claude.ai")) return tabsList.filter(t => String(t.url || "").includes("claude.ai"));
          return tabsList;
        }},
        update: async () => {{}},
        onUpdated: {{ addListener: () => {{}} }},
        onRemoved: {{ addListener: () => {{}} }},
      }},
      cookies: {{
        getAllCookieStores: async () => [{{ id: "0" }}],
        getAll: async () => cookiesList,
      }},
      scripting: {{
        executeScript: async () => [{{ result: execResult }}],
      }},
    }},
  }};
  sb.globalThis = sb;
  vm.runInNewContext(bgSrc, sb);
  assert.strictEqual(listeners.length, 1);
  return {{
    syncPayloads,
    dispatch(msg) {{
      return new Promise((resolve) => {{
        assert.strictEqual(listeners[0](msg, {{}}, resolve), true);
      }});
    }},
  }};
}}

(async () => {{
  // 1) Gemini Brave GET_STATUS + SYNC_NOW
  const gemSw = makeSwHarness({{
    navIsBrave: true,
    tabsList: [{{ id: 101, active: true, url: "https://gemini.google.com/u/0/app/conv123" }}],
    cookiesList: [
      {{ domain: ".google.com", name: "SAPISID", value: "sap123" }},
      {{ domain: ".google.com", name: "__Secure-1PSID", value: "psid123" }},
      {{ domain: ".google.com", name: "COMPASS", value: "filtered" }},
    ],
    execResult: {{
      isBrave: true,
      xsrfToken: "SNlM0e-tok",
      geminiBl: "boq_gemini-web-uiserver_20261111.09_p0",
      accountEmail: "dev@example.com",
      modelBadge: "Gemini 2.5 Pro",
    }},
  }});
  const status = await gemSw.dispatch({{ type: "GET_STATUS", isBrave: true }});
  assert.strictEqual(status.serverReachable, true);
  assert.strictEqual(status.browser, "brave");
  assert.strictEqual(status.modelBadge, "Gemini 2.5 Pro");

  const syncGem = await gemSw.dispatch({{ type: "SYNC_NOW", isBrave: true }});
  assert.strictEqual(syncGem.ok, true);
  assert.strictEqual(syncGem.browser, "brave");
  assert.strictEqual(gemSw.syncPayloads.length >= 1, true);
  const lastGemPayload = gemSw.syncPayloads[gemSw.syncPayloads.length - 1];
  assert(lastGemPayload.cookie.includes("SAPISID=sap123"));
  assert(lastGemPayload.cookie.includes("__Secure-1PSID=psid123"));
  assert(!lastGemPayload.cookie.includes("COMPASS"));
  assert.strictEqual(lastGemPayload.xsrf_token, "SNlM0e-tok");

  // 2) Claude-only Brave SYNC_NOW with zero Google cookies
  const claudeSw = makeSwHarness({{
    navIsBrave: true,
    tabsList: [{{ id: 202, active: true, url: "https://claude.ai/chat/c-999" }}],
    cookiesList: [],
    execResult: {{
      isBrave: true,
      accountEmail: "claude.user@example.com",
      modelBadge: "Claude Sonnet 4.6 · Extended thinking",
    }},
  }});
  const syncClaude = await claudeSw.dispatch({{ type: "SYNC_NOW", isBrave: true }});
  assert.strictEqual(syncClaude.ok, true);
  assert.strictEqual(syncClaude.browser, "brave");
  assert.strictEqual(syncClaude.modelBadge, "Claude Sonnet 4.6 · Extended thinking");
  assert.strictEqual(claudeSw.syncPayloads.length >= 1, true);
  const lastClaudePayload = claudeSw.syncPayloads[claudeSw.syncPayloads.length - 1];
  assert.strictEqual(lastClaudePayload.site, "claude.ai");
  assert.strictEqual(lastClaudePayload.providers.claude.has_tab, true);

  // 3) Chrome isBrave=false overrides stale "brave" browser hint
  const chromeSw = makeSwHarness({{
    navIsBrave: false,
    storedState: {{ detectedBrowser: "brave", wsa_relay_browser: "brave", wsa_relay_label: "brave" }},
    tabsList: [{{ id: 303, active: true, url: "https://claude.ai/chat/c-chrome" }}],
    cookiesList: [],
    execResult: {{
      isBrave: false,
      accountEmail: "chrome.user@example.com",
      modelBadge: "Claude Opus 4.6",
    }},
  }});
  const chromeStatus = await chromeSw.dispatch({{ type: "GET_STATUS", isBrave: false }});
  assert.strictEqual(chromeStatus.browser, "chrome");

  // 4) Claude page runtime DOM streaming + Extended thinking separation in Node VM
  let fakeNow = 10000;
  class MockElement {{
    constructor(tag, attrs = {{}}, cls = "", text = "") {{
      this.nodeType = 1;
      this.tagName = tag.toUpperCase();
      this.attrs = {{ ...attrs }};
      this.className = cls;
      this.childNodes = [];
      this.children = [];
      this.parentElement = null;
      this.disabled = false;
      this._text = text;
    }}
    appendChild(child) {{
      if (typeof child === "string") {{
        this.childNodes.push({{ nodeType: 3, textContent: child }});
      }} else {{
        child.parentElement = this;
        this.childNodes.push(child);
        this.children.push(child);
      }}
      return child;
    }}
    get textContent() {{
      if (this.childNodes.length === 0) return this._text;
      return this.childNodes.map(c => c.textContent || "").join("");
    }}
    set textContent(v) {{
      this.childNodes = [];
      this.children = [];
      this._text = String(v || "");
    }}
    getAttribute(k) {{ return k in this.attrs ? String(this.attrs[k]) : null; }}
    setAttribute(k, v) {{ this.attrs[k] = String(v); }}
    matches(sel) {{
      const parts = sel.split(",").map(s => s.trim()).filter(Boolean);
      return parts.some(p => {{
        if (p.startsWith(".")) return (this.className || "").split(/\\s+/).includes(p.slice(1).split("[")[0]);
        const tagPrefix = p.match(/^([a-z0-9_-]+)/i);
        if (tagPrefix && this.tagName !== tagPrefix[1].toUpperCase()) return false;
        const attrSegs = p.match(/\\[[^\\]]+\\]/g);
        if (attrSegs) {{
          return attrSegs.every(seg => {{
            const am = seg.match(/^\\[([^=*\\]]+)(?:(\\*?)=['"]?([^'"\\]]+?)['"]?(?:\\s+i)?)?\\]/);
            if (!am) return false;
            const val = this.getAttribute(am[1]);
            if (val === null) return false;
            if (!am[2] && !am[3]) return true;
            if (am[2] === "*") return val.toLowerCase().includes(am[3].toLowerCase());
            return val === am[3];
          }});
        }}
        return false;
      }});
    }}
    closest(sel) {{
      let cur = this;
      while (cur) {{
        if (cur.matches && cur.matches(sel)) return cur;
        cur = cur.parentElement;
      }}
      return null;
    }}
    contains(other) {{
      let cur = other;
      while (cur) {{
        if (cur === this) return true;
        cur = cur.parentElement;
      }}
      return false;
    }}
    querySelectorAll(sel) {{
      const res = [];
      const walk = (el) => {{
        for (const ch of el.children || []) {{
          if (ch.matches && ch.matches(sel)) res.push(ch);
          walk(ch);
        }}
      }};
      walk(this);
      return res;
    }}
    querySelector(sel) {{ return this.querySelectorAll(sel)[0] || null; }}
    focus() {{}}
    dispatchEvent() {{ return true; }}
    click() {{}}
  }}
  const rootEl = new MockElement("div");
  const editorEl = new MockElement("div", {{ "data-testid": "chat-input", contenteditable: "true" }}, "ProseMirror");
  const sendBtn = new MockElement("button", {{ "data-testid": "send-button" }});
  const modelBtn = new MockElement("button", {{ "data-testid": "model-selector-dropdown" }}, "", "Claude Opus 4.6");
  const thinkToggle = new MockElement("button", {{ "data-testid": "extended-thinking-toggle", "aria-pressed": "true" }});
  const turnContainer = new MockElement("div", {{ "data-is-streaming": "true" }}, "font-claude-response");
  const thoughtBox = new MockElement("div", {{ "data-testid": "thinking-block" }}, "font-claude-thought");
  const thoughtP = new MockElement("p");
  thoughtP.appendChild("Internal thought step");
  thoughtBox.appendChild(thoughtP);
  const mdBox = new MockElement("div", {{}}, "standard-markdown");
  const replyP = new MockElement("p");
  replyP.appendChild("Public reply chunk");
  mdBox.appendChild(replyP);
  turnContainer.appendChild(thoughtBox);
  turnContainer.appendChild(mdBox);
  rootEl.appendChild(editorEl);
  rootEl.appendChild(sendBtn);
  rootEl.appendChild(modelBtn);
  rootEl.appendChild(thinkToggle);

  const pageSb = {{
    console,
    Date: Object.assign(function() {{ return new Date(fakeNow); }}, {{ now: () => fakeNow }}),
    Math, Object, Array, String, Boolean, Number, Set, Map, Promise, JSON, RegExp, setTimeout,
    Node: {{ TEXT_NODE: 3, ELEMENT_NODE: 1 }},
    Element: MockElement,
    InputEvent: class {{ constructor(t, o) {{ this.type = t; }} }},
    Event: class {{ constructor(t, o) {{ this.type = t; }} }},
    KeyboardEvent: class {{ constructor(t, o) {{ this.type = t; }} }},
    getComputedStyle: () => ({{ display: "block", visibility: "visible", opacity: "1" }}),
    document: {{
      querySelector: (s) => rootEl.querySelector(s),
      querySelectorAll: (s) => rootEl.querySelectorAll(s),
      createRange: () => ({{ selectNodeContents: () => {{}} }}),
      execCommand: () => false,
    }},
    location: {{ href: "https://claude.ai/chat/c-999" }},
    window: null,
    globalThis: null,
  }};
  pageSb.window = pageSb;
  pageSb.window.getSelection = () => ({{ removeAllRanges: () => {{}}, addRange: () => {{}} }});
  pageSb.globalThis = pageSb;
  vm.runInNewContext("(" + bg._claudeInstallPageRuntimeFn.toString() + ")()", pageSb);
  const startRes = await vm.runInNewContext("(" + bg._claudeStartTurnFn.toString() + ")('req-c-1', 'Explain WSA')", pageSb);
  assert.strictEqual(startRes.ok, true);
  assert.strictEqual(startRes.model, "Claude Opus 4.6 · Extended thinking");

  rootEl.appendChild(turnContainer);
  fakeNow = 10200;
  const pollStreaming = vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('req-c-1')", pageSb);
  assert.strictEqual(pollStreaming.done, false);
  assert.strictEqual(pollStreaming.thinking, "Internal thought step");
  assert.strictEqual(pollStreaming.text, "Public reply chunk");

  turnContainer.setAttribute("data-is-streaming", "false");
  const copyBtn = new MockElement("button", {{ "aria-label": "Copy response" }});
  turnContainer.parentElement.appendChild(copyBtn);
  fakeNow = 10800;
  const pollDone = vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('req-c-1')", pageSb);
  assert.strictEqual(pollDone.done, true);
  assert.strictEqual(pollDone.thinking, "Internal thought step");
  assert.strictEqual(pollDone.text, "Public reply chunk");
  assert.strictEqual(pollDone.model, "Claude Opus 4.6 · Extended thinking");

  console.log("EXT_CLEAN_OK");
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        self.assertIn("EXT_CLEAN_OK", proc.stdout)

    def test_dynamic_system_prompt_and_custom_tool_schema_synthesis_and_exec(self):
        from pathlib import Path
        from wsa_agent import prompts, tools
        from wsa_agent.session import SessionState

        repo_root = Path(__file__).resolve().parent.parent
        st = SessionState(project_root=repo_root)
        st.active_account_label = "brave"
        st.active_account_email = "example@example.com"
        st.active_browser = "brave"
        dyn = prompts.build_dynamic_system_prompt(st)
        self.assertIn("DYNAMIC RUNTIME ADAPTATION", dyn)
        self.assertIn("brave DOM relay", dyn)
        self.assertIn("example@example.com", dyn)
        self.assertIn("create_custom_tool", dyn)
        self.assertIn("Self-modification safety", dyn)

        with tempfile.TemporaryDirectory() as tmpdir:
            orig_root = tools._project_root
            orig_tools = dict(tools._custom_tools)
            orig_defs = list(tools.TOOL_DEFINITIONS)
            try:
                tools.set_project_root(Path(tmpdir))
                (Path(tmpdir) / "sample.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
                code = (
                    "def run(filename: str, scale: int = 2) -> str:\n"
                    "    lines = (PROJECT_ROOT / filename).read_text(encoding='utf-8').splitlines()\n"
                    "    return f'{filename}:{len(lines) * scale}'\n"
                )
                reg = tools.execute_tool(
                    "create_custom_tool",
                    {"name": "count_scaled_lines", "description": "Count scaled lines", "python_code": code},
                )
                self.assertIn("[OK]", reg)
                self.assertIn("filename", reg)
                schema = tools._custom_tools["count_scaled_lines"]["parameters"]
                self.assertEqual(schema["required"], ["filename"])
                self.assertEqual(schema["properties"]["scale"]["type"], "integer")

                res = tools.execute_tool("count_scaled_lines", {"filename": "sample.txt", "scale": 3})
                self.assertEqual(res, "sample.txt:9")

                bad = tools.execute_tool(
                    "create_custom_tool",
                    {"name": "missing_entry", "description": "No run", "python_code": "def helper(): return 'x'\n"},
                )
                self.assertIn("[Error]", bad)
            finally:
                tools.set_project_root(orig_root)
                tools._custom_tools.clear()
                tools._custom_tools.update(orig_tools)
                tools.TOOL_DEFINITIONS[:] = orig_defs

    def test_self_edit_crash_immunity_auto_rollback_and_json_guard(self):
        from pathlib import Path
        from wsa_agent import tools

        repo_root = Path(__file__).resolve().parent.parent
        orig_root = tools._project_root
        tmp_mod = repo_root / "wsa_agent" / "_tmp_self_verify_guard.py"
        try:
            tools.set_project_root(repo_root)
            if tmp_mod.exists():
                tmp_mod.unlink()

            # 1. JSON guard blocks corrupt JSON before write
            with tempfile.TemporaryDirectory() as td:
                tools.set_project_root(Path(td))
                bad_json = Path(td) / "bad.json"
                jres = tools._write_file("bad.json", "{invalid json")
                self.assertIn("[Syntax Error Guard]", jres)
                self.assertFalse(bad_json.exists())
            tools.set_project_root(repo_root)

            # 2. Broken import in new self-module triggers post-write verification & auto-unlink rollback
            res_new = tools._write_file(
                "wsa_agent/_tmp_self_verify_guard.py",
                "from wsa_agent import nonexistent_symbol_xyz\n",
            )
            self.assertIn("[Self-Preservation Rollback]", res_new)
            self.assertFalse(tmp_mod.exists(), "New broken module must be unlinked automatically")

            # 3. Seed a valid self-module, then break it with patch_file -> auto-restores original content
            ok_write = tools._write_file("wsa_agent/_tmp_self_verify_guard.py", "HEALTHY_MARKER = 42\n")
            self.assertIn("[OK]", ok_write)
            self.assertTrue(tmp_mod.exists())

            bad_patch = tools._patch_file(
                "wsa_agent/_tmp_self_verify_guard.py",
                old_string="HEALTHY_MARKER = 42",
                new_string="from wsa_agent import missing_symbol_breaks_import",
            )
            self.assertIn("[Self-Preservation Rollback]", bad_patch)
            self.assertEqual(tmp_mod.read_text(encoding="utf-8"), "HEALTHY_MARKER = 42\n")

            # 4. Broken edit inside modular wsa_server/ also auto-rolls back
            tmp_srv = repo_root / "wsa_server" / "_tmp_self_verify_guard.py"
            if tmp_srv.exists():
                tmp_srv.unlink()
            try:
                res_srv = tools._write_file(
                    "wsa_server/_tmp_self_verify_guard.py",
                    "from wsa_server import nonexistent_server_symbol_xyz\n",
                )
                self.assertIn("[Self-Preservation Rollback]", res_srv)
                self.assertFalse(tmp_srv.exists())
            finally:
                if tmp_srv.exists():
                    tmp_srv.unlink()
        finally:
            if tmp_mod.exists():
                tmp_mod.unlink()
            tools.set_project_root(orig_root)

    def test_same_project_multi_turn_chat_continuity_and_session_persistence(self):
        import time
        module = __import__("wsa_server")
        sid = "sess-persist-proj-01"
        with module._dom_relay_lock:
            old_active = module._dom_relay.get("active_relay_id")
            old_relays = dict(module._dom_relay.get("relays") or {})
            old_sessions = dict(module._dom_relay.get("sessions") or {})
        try:
            self.assertGreaterEqual(int(module.DEFAULT_CONFIG.get("dom_relay_rollover_turns", 0)), 100)

            with module._dom_relay_lock:
                module._dom_relay["relays"]["brave-cont"] = {
                    "client_id": "brave-cont",
                    "browser": "brave",
                    "label": "brave",
                    "auth_user": "0",
                    "account_email": "example@example.com",
                    "last_seen": time.time(),
                }
                module._dom_relay["active_relay_id"] = "brave-cont"

            with tempfile.TemporaryDirectory() as tmpdir:
                sess_path = str(Path(tmpdir) / "relay_sessions.json")
                tc = [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"app.py"}'}}]
                module.record_session_relay_turn(
                    sid, "", tc, chat_url="https://gemini.google.com/app/proj_conv_77", relay_id="brave-cont"
                )
                with module._dom_relay_lock:
                    module._persist_relay_sessions_locked(sess_path)
                    module._dom_relay["sessions"].clear()

                loaded = module.load_relay_sessions(sess_path, enable_persist=False)
                self.assertGreaterEqual(loaded, 1)

            # Even if last assistant turn had an intermediate HTTP/extracted divergence,
            # having a saved project chat_url on the same relay keeps new_chat=False!
            msgs_diverged = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "msg 1"},
                {"role": "assistant", "content": "answered earlier via Pro HTTP without relay sig"},
                {"role": "user", "content": "msg 2 follow-up on same project"},
            ]
            full_p, imgs = module.messages_to_prompt(msgs_diverged, None)
            p_send, fb_send, _, is_new, _hint = module.prepare_session_relay_prompt(sid, msgs_diverged, None, full_p, imgs)
            self.assertFalse(is_new)
            self.assertEqual(fb_send, full_p)
            self.assertIn("msg 2 follow-up on same project", p_send)
        finally:
            with module._dom_relay_lock:
                module._dom_relay["active_relay_id"] = old_active
                module._dom_relay["relays"] = old_relays
                module._dom_relay["sessions"] = old_sessions

    def test_tui_unslopped_layout_copy_and_responsive_rendering(self):
        import io
        from rich.console import Console
        from wsa_agent import __main__ as main_mod
        from wsa_agent.session import ALL_MODELS, SessionState
        from wsa_agent.theme import THEMES

        # Warm graphite dark palette tokens + zero emoji/DOM jargon across static TUI copy
        dark = THEMES["dark"]
        self.assertEqual(dark.text, "#ededed")
        self.assertEqual(dark.border, "#3f3f46")
        self.assertEqual(dark.success, "#4cc38a")

        st = SessionState(project_root=Path("."))
        st.active_account_label = "brave"
        st.active_account_email = "example@example.com"
        st.active_browser = "brave"

        for w in (104, 68):
            buf = io.StringIO()
            orig = renderer.console
            try:
                renderer.console = Console(file=buf, force_terminal=False, width=w, highlight=False)
                renderer.print_welcome(st, server_ok=True)
                renderer.print_models_table(ALL_MODELS, current_model=st.model, state=st)
                renderer.print_status(st)
                renderer.print_help()
                renderer.print_shortcuts()
                renderer.print_agent_response("# Primary Heading\n\n## Detail\n\nBody paragraph.")
                out = buf.getvalue()
            finally:
                renderer.console = orig

            # Zero slop phrases, zero nested double parens, zero centered Rich H1 banner
            for banned in (
                "Welcome back!",
                "Chrome DOM Relay",
                "Brave DOM Relay",
                "processSession",
                "slot80=",
                "mode 5",
                "session(s)",
                "(brave (example@example.com))",
                "⚡",
                "📁",
                "📄",
            ):
                self.assertNotIn(banned, out, msg=f"Slop token {banned!r} found at width={w}")

            self.assertIn("Session Status", out)
            self.assertIn("Commands", out)
            self.assertIn("Brave Web", out)
            self.assertIn("Engineered by Divyansh Saraf", out)
            # H1 heading stays left-aligned at 2-space padding, not centered mid-screen
            h1_line = next(ln for ln in out.splitlines() if "Primary Heading" in ln)
            self.assertTrue(h1_line.startswith("  Primary Heading"), msg=f"H1 not left-aligned: {h1_line!r}")

        tb = main_mod._build_bottom_toolbar(st)
        self.assertNotIn("▶▶", tb)
        self.assertIn("▸ accept edits on", tb)
        self.assertIn("(Shift+Tab)", tb)


class TestDomRelayLiveStreaming(unittest.TestCase):
    def _reset_relay(self, module):
        with module._dom_relay_lock:
            module._dom_relay["last_poll_ts"] = 0.0
            module._dom_relay["pending"].clear()
            module._dom_relay["relays"].clear()
            module._dom_relay["active_relay_id"] = None
            module._dom_relay["pinned_relay_label"] = None
            module._dom_relay["explicit_relay_lock"] = False
            module._dom_relay["recent_requests"].clear()

    def test_dom_relay_streams_thinking_and_content_deltas_incrementally(self):
        import threading
        import time as _time
        module = __import__("wsa_server")
        self._reset_relay(module)
        observed = []
        done_ev = threading.Event()
        try:
            def _streamer():
                for _ in range(100):
                    item = module.dom_relay_get_pending()
                    if item:
                        rid = item["id"]
                        module.dom_relay_post_progress(
                            rid,
                            thinking="Analyzing G4 vs A100.",
                            response="",
                            model="4 Argon High",
                            seq=1,
                        )
                        _time.sleep(0.06)
                        module.dom_relay_post_progress(
                            rid,
                            thinking="Analyzing G4 vs A100.\nComparing VRAM bandwidth.",
                            response="First ",
                            model="4 Argon High",
                            seq=2,
                        )
                        _time.sleep(0.06)
                        module.dom_relay_post_progress(
                            rid,
                            thinking="Analyzing G4 vs A100.\nComparing VRAM bandwidth.",
                            response="First look at **A100**",
                            model="4 Argon High",
                            seq=3,
                        )
                        _time.sleep(0.06)
                        module.dom_relay_post_response(
                            rid,
                            response="First look at **A100** vs **G4**.",
                            thinking="Analyzing G4 vs A100.\nComparing VRAM bandwidth.",
                            model="4 Argon High",
                        )
                        done_ev.set()
                        return
                    _time.sleep(0.01)

            t = threading.Thread(target=_streamer, daemon=True)
            t.start()
            events = list(
                module.gemini_stream_generate_events(
                    "compare gpus",
                    model_id="gemini-4-argon",
                    model_cfg=module.MODELS["gemini-4-argon"],
                )
            )
            t.join(timeout=3.0)
            self.assertTrue(done_ev.is_set())

            thinking_chunks = [p for k, p in events if k == "thinking"]
            content_chunks = [p for k, p in events if k == "content"]
            resets = [k for k, _ in events if k == "reset_content"]

            self.assertGreaterEqual(len(thinking_chunks), 2)
            self.assertGreaterEqual(len(content_chunks), 3)
            self.assertEqual(resets, [])
            self.assertEqual("".join(thinking_chunks), "Analyzing G4 vs A100.\nComparing VRAM bandwidth.")
            self.assertEqual("".join(content_chunks), "First look at **A100** vs **G4**.")
        finally:
            self._reset_relay(module)

    def test_dom_relay_live_stream_reconciles_dom_preview_to_final_markdown(self):
        import threading
        import time as _time
        module = __import__("wsa_server")
        self._reset_relay(module)
        try:
            def _streamer():
                for _ in range(100):
                    item = module.dom_relay_get_pending()
                    if item:
                        rid = item["id"]
                        module.dom_relay_post_progress(
                            rid,
                            response="First item",
                            model="4 Argon High",
                            seq=1,
                        )
                        _time.sleep(0.06)
                        # Final Relay markdown reformats bullet numbering cleanly
                        module.dom_relay_post_response(
                            rid,
                            response="1. First item\n2. Second item",
                            thinking="",
                            model="4 Argon High",
                        )
                        return
                    _time.sleep(0.01)

            t = threading.Thread(target=_streamer, daemon=True)
            t.start()
            events = list(
                module.gemini_stream_generate_events(
                    "list items",
                    model_id="gemini-4-argon",
                    model_cfg=module.MODELS["gemini-4-argon"],
                )
            )
            t.join(timeout=3.0)
            kinds = [k for k, _ in events if k != "keepalive"]
            self.assertIn("reset_content", kinds)
            reset_idx = kinds.index("reset_content")
            after_reset = [p for k, p in [e for e in events if e[0] != "keepalive"][reset_idx + 1 :] if k == "content"]
            self.assertEqual("".join(after_reset), "1. First item\n2. Second item")
        finally:
            self._reset_relay(module)

    def test_dom_relay_progress_seq_monotonic_and_active_progress_extends_stall_window(self):
        import threading
        import time as _time
        module = __import__("wsa_server")
        self._reset_relay(module)
        try:
            def _slow_streamer():
                for _ in range(100):
                    item = module.dom_relay_get_pending()
                    if item:
                        rid = item["id"]
                        # Post 3 incremental progress updates spaced 0.12s apart (> 0.28s total, < 0.28s idle gap)
                        for seq, txt in ((1, "chunk1 "), (2, "chunk1 chunk2 "), (3, "chunk1 chunk2 chunk3")):
                            module.dom_relay_post_progress(rid, response=txt, model="4 Argon High", seq=seq)
                            _time.sleep(0.12)
                        # Stale out-of-order seq=1 must be ignored cleanly
                        module.dom_relay_post_progress(rid, response="STALE", model="4 Argon High", seq=1)
                        module.dom_relay_post_response(
                            rid,
                            response="chunk1 chunk2 chunk3!",
                            model="4 Argon High",
                        )
                        return
                    _time.sleep(0.01)

            t = threading.Thread(target=_slow_streamer, daemon=True)
            t.start()
            events = list(module.dom_relay_iter("stream", timeout=0.28, pickup_timeout=2.0, step=0.05))
            t.join(timeout=3.0)
            self.assertEqual(events[-1][0], "result")
            self.assertEqual(events[-1][1]["response"], "chunk1 chunk2 chunk3!")
            deltas = [p for k, p in events if k == "content_delta"]
            self.assertNotIn("STALE", "".join(deltas))
            self.assertEqual("".join(deltas), "chunk1 chunk2 chunk3")
        finally:
            self._reset_relay(module)

    def test_sse_handle_chat_streams_live_prose_while_holding_trailing_tool_call(self):
        import io
        import threading
        import time as _time
        module = __import__("wsa_server")
        self._reset_relay(module)
        try:
            def _tool_streamer():
                for _ in range(100):
                    item = module.dom_relay_get_pending()
                    if item:
                        rid = item["id"]
                        module.dom_relay_post_progress(
                            rid,
                            thinking="Inspecting files.",
                            response="Checking config now.\n",
                            model="4 Argon High",
                            seq=1,
                        )
                        _time.sleep(0.06)
                        module.dom_relay_post_progress(
                            rid,
                            thinking="Inspecting files.",
                            response="Checking config now.\n```tool_call\n{\"name\":\"read_file\"",
                            model="4 Argon High",
                            seq=2,
                        )
                        _time.sleep(0.06)
                        module.dom_relay_post_response(
                            rid,
                            response='Checking config now.\n```tool_call\n{"name":"read_file","arguments":{"path":"a.py"}}\n```',
                            thinking="Inspecting files.",
                            model="4 Argon High",
                        )
                        return
                    _time.sleep(0.01)

            t = threading.Thread(target=_tool_streamer, daemon=True)
            t.start()

            handler = module.GeminiHandler.__new__(module.GeminiHandler)
            handler.wfile = io.BytesIO()
            handler.send_response = lambda *a, **kw: None
            handler.send_header = lambda *a, **kw: None
            handler.end_headers = lambda *a, **kw: None

            req_body = json.dumps({
                "model": "gemini-4-argon",
                "stream": True,
                "messages": [{"role": "user", "content": "read a.py"}],
                "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
            }).encode("utf-8")

            from wsa_server import server as srv_mod
            with patch.object(srv_mod, "is_dom_relay_active", return_value=True):
                handler.handle_chat(req_body)
            t.join(timeout=3.0)

            sse_lines = [
                ln.strip()
                for ln in handler.wfile.getvalue().decode("utf-8").splitlines()
                if ln.strip().startswith("data: ")
            ]
            client = GeminiClient("http://127.0.0.1:8081/v1", "test")
            client._client = _StreamClient(sse_lines)
            sr = renderer.StreamRenderer()
            try:
                res = client.stream(
                    [{"role": "user", "content": "read a.py"}],
                    "gemini-4-argon",
                    on_token=sr.feed,
                    on_thinking=sr.feed_thinking,
                    on_reset=sr.reset_answer,
                )
            finally:
                sr.finish()
            self.assertEqual(res["content"], "Checking config now.\n")
            self.assertEqual(len(res["tool_calls"]), 1)
            self.assertEqual(res["tool_calls"][0]["function"]["name"], "read_file")
            self.assertEqual(sr.get_thinking_text(), "Inspecting files.")
            self.assertEqual(sr.get_full_text(), "Checking config now.\n")
        finally:
            self._reset_relay(module)

    def test_extension_page_runtime_is_self_contained_and_handles_completion(self):
        import subprocess
        bg_path = Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js"
        js_code = f"""
const assert = require("assert");
const vm = require("vm");
const bg = require({json.dumps(str(bg_path))});

// Execute page-injected functions inside an isolated vm context with NO access to background.js SW closures!
const fakeEditor = {{
  __reactFiber$test: {{
    memoizedProps: {{
      onChange: () => {{}},
      onSend: () => {{}},
      agentBusy: false,
    }},
    dependencies: {{
      firstContext: {{
        memoizedValue: {{
          environment: {{
            getStore: () => ({{
              getSource: () => ({{
                getRecordIDs: () => ["resp:candidate:1"],
                get: (id) => (sandbox.__mockRelay[id] || null),
              }}),
            }}),
          }},
        }},
      }},
    }},
    return: null,
  }},
  getBoundingClientRect: () => ({{ width: 200, height: 40 }}),
  offsetParent: {{}},
  getClientRects: () => [{{ width: 200, height: 40 }}],
}};
const sandbox = {{
  console,
  Date,
  Math,
  Number,
  String,
  Boolean,
  Object,
  Array,
  Error,
  Promise,
  JSON,
  RegExp,
  setTimeout,
  __mockRelay: {{}},
  getComputedStyle: () => ({{ display: "block", visibility: "visible", opacity: "1" }}),
  document: {{
    querySelectorAll: (sel) => sel.includes("textbox") ? [fakeEditor] : [],
    querySelector: (sel) => sel.includes("mode picker") ? {{ getAttribute: () => "currently 4 Argon High" }} : null,
  }},
  window: null,
  globalThis: null,
}};
sandbox.globalThis = sandbox;
sandbox.window = sandbox;
sandbox.location = {{ href: "https://gemini.google.com/app/test123" }};
vm.runInNewContext("(" + bg._domInstallPageRuntimeFn.toString() + ")()", sandbox);

const norm = v => JSON.parse(JSON.stringify(v));
const rt = sandbox.__wsaRuntime;
assert.strictEqual(rt.version, 2);
assert.deepStrictEqual(norm(rt.computeTailDomRange(4, 3)), {{ start: 3, count: 4, hasNew: true }});

// Pick longest changed Relay candidate rather than last arbitrary map key
const picked = rt.pickBestChangedRelayCandidate(
  {{
    "cand-old": {{ id: "cand-old", markdown: "old turn", thinking: "" }},
    "cand-real": {{ id: "cand-real", markdown: "Complete A100 benchmark analysis...", thinking: "Step 1" }},
    "cand-stub": {{ id: "cand-stub", markdown: "short", thinking: "" }},
  }},
  {{
    "cand-old": {{ id: "cand-old", markdown: "old turn", thinking: "" }},
  }}
);
assert.strictEqual(picked.sawNewTurn, true);
assert.strictEqual(picked.text, "Complete A100 benchmark analysis...");
assert.strictEqual(picked.thinking, "Step 1");

// Live text selection prefers Relay when populated, falls back to DOM while Relay catches up
assert.deepStrictEqual(
  norm(rt.selectLiveTurnText({{ relayText: "", domText: "Hello live", prevText: "", prevSource: "" }})),
  {{ text: "Hello live", source: "dom" }}
);
assert.deepStrictEqual(
  norm(rt.selectLiveTurnText({{ relayText: "Hello live **world**", domText: "Hello live world", prevText: "Hello live", prevSource: "dom" }})),
  {{ text: "Hello live **world**", source: "relay" }}
);

// Completion state handles clean stop, stuck React flag + action toolbar, and 6s idle fallback
const t0 = 1_000_000;
assert.strictEqual(rt.evaluateTurnCompletionState({{
  text: "", stopVisible: false, reactBusy: false, hasTurnActions: true, sawNewTurn: true,
  now: t0 + 5000, lastTextChangeAt: t0, lastThinkChangeAt: t0, lastBusyAt: t0,
}}), false);
assert.strictEqual(rt.evaluateTurnCompletionState({{
  text: "Answer", stopVisible: true, reactBusy: false, hasTurnActions: false, sawNewTurn: true,
  now: t0 + 5000, lastTextChangeAt: t0, lastThinkChangeAt: t0, lastBusyAt: t0 + 5000,
}}), false);
assert.strictEqual(rt.evaluateTurnCompletionState({{
  text: "Answer", stopVisible: false, reactBusy: false, hasTurnActions: false, sawNewTurn: true,
  now: t0 + 1500, lastTextChangeAt: t0, lastThinkChangeAt: t0, lastBusyAt: t0,
}}), true);
// Stale reactBusy=true must still complete once Stop disappears + response toolbar buttons render + 2.2s idle
assert.strictEqual(rt.evaluateTurnCompletionState({{
  text: "Full response done", stopVisible: false, reactBusy: true, hasTurnActions: true, sawNewTurn: true,
  now: t0 + 2300, lastTextChangeAt: t0, lastThinkChangeAt: t0, lastBusyAt: t0 + 2300,
}}), true);
// Quiet 6s fallback
assert.strictEqual(rt.evaluateTurnCompletionState({{
  text: "Full response", stopVisible: false, reactBusy: true, hasTurnActions: false, sawNewTurn: true,
  now: t0 + 6100, lastTextChangeAt: t0, lastThinkChangeAt: t0, lastBusyAt: t0 + 6100,
}}), true);

// Verify _domStartTurnFn + _domPollTurnFn execute cleanly inside isolated MAIN world without ReferenceError
(async () => {{
  const startRes = await vm.runInNewContext(
    "(" + bg._domStartTurnFn.toString() + ")('req-iso-1', 'hello')",
    sandbox
  );
  assert.strictEqual(startRes.ok, true);
  assert.strictEqual(startRes.model, "4 Argon High");

  sandbox.__mockRelay["resp:candidate:1"] = {{
    id: "resp:candidate:1",
    markdownResponse: "Streaming delta ",
    thoughtBody: "Reasoning step",
  }};
  const snap1 = norm(vm.runInNewContext(
    "(" + bg._domPollTurnFn.toString() + ")('req-iso-1')",
    sandbox
  ));
  assert.strictEqual(snap1.done, false);
  assert.strictEqual(snap1.text, "Streaming delta ");
  assert.strictEqual(snap1.thinking, "Reasoning step");

  sandbox.__mockRelay["resp:candidate:1"].markdownResponse = "Streaming delta complete.";
  const snap2 = norm(vm.runInNewContext(
    "(" + bg._domPollTurnFn.toString() + ")('req-iso-1')",
    sandbox
  ));
  assert.strictEqual(snap2.done, false);
  assert.strictEqual(snap2.text, "Streaming delta complete.");

  sandbox.__wsaActiveJob.lastTextChangeAt = Date.now() - 2000;
  sandbox.__wsaActiveJob.lastBusyAt = Date.now() - 2000;
  const snap3 = norm(vm.runInNewContext(
    "(" + bg._domPollTurnFn.toString() + ")('req-iso-1')",
    sandbox
  ));
  assert.strictEqual(snap3.done, true);
  assert.strictEqual(snap3.text, "Streaming delta complete.");
}})().catch((e) => {{
  console.error(e);
  process.exit(1);
}});
"""
        proc = subprocess.run(["node", "-e", js_code], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)


class TestPromptBoxAndFetchedTableRendering(unittest.TestCase):
    def test_dom_node_to_markdown_formats_html_tables_ordered_lists_and_blockquotes(self):
        import subprocess
        bg_path = Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js"
        js_code = f"""
const assert = require("assert");
const vm = require("vm");
const bg = require({json.dumps(str(bg_path))});

function el(tag, children = [], extra = {{}}) {{
  const node = {{
    nodeType: 1,
    tagName: tag.toUpperCase(),
    className: extra.className || "",
    childNodes: children,
    children: children.filter(c => c.nodeType === 1),
    textContent: extra.textContent !== undefined ? extra.textContent : children.map(c => c.textContent ?? c.nodeValue ?? "").join(""),
    getAttribute: (k) => (extra.attrs || {{}})[k] || null,
    closest: (sel) => {{
      const s = String(sel).toLowerCase();
      let cur = node;
      while (cur) {{
        const t = (cur.tagName || "").toLowerCase();
        if (s === t || (s === "[role='table']" && cur.getAttribute("role") === "table")) return cur;
        cur = cur.parentElement;
      }}
      return null;
    }},
    querySelector: () => null,
    querySelectorAll: (sel) => {{
      const out = [];
      const want = String(sel).toLowerCase();
      const visit = (n) => {{
        if (!n || n.nodeType !== 1) return;
        for (const ch of n.childNodes || []) {{
          if (ch.nodeType === 1) {{
            if ((ch.tagName || "").toLowerCase() === want || (want === "[role='row']" && ch.getAttribute("role") === "row")) {{
              out.push(ch);
            }}
            visit(ch);
          }}
        }}
      }};
      visit(node);
      return out;
    }},
  }};
  for (const c of children) c.parentElement = node;
  return node;
}}
const txt = (s) => ({{ nodeType: 3, nodeValue: s, textContent: s }});

const tableEl = el("table", [
  el("thead", [
    el("tr", [
      el("th", [txt("Model")]),
      el("th", [txt("Quant File")]),
      el("th", [txt("Speed")]),
    ]),
  ]),
  el("tbody", [
    el("tr", [
      el("td", [el("code", [txt("TheDrummer/Valkyrie-49B")])]),
      el("td", [el("code", [txt("Valkyrie-49B-Q5_K_M.gguf")])]),
      el("td", [txt("~50-56 tok/s")]),
    ]),
  ]),
]);

const olEl = el("ol", [
  el("li", [txt("Enable fast transfers")]),
  el("li", [txt("Launch server")]),
]);

const bqEl = el("blockquote", [el("p", [txt("Important VRAM note")])]);
const root = el("div", [tableEl, olEl, bqEl]);

const sandbox = {{
  console, Date, Math, Number, String, Boolean, Object, Array, Error, Promise, JSON, RegExp, setTimeout,
  document: {{ querySelectorAll: () => [], querySelector: () => null }},
  window: null, globalThis: null,
}};
sandbox.globalThis = sandbox;
sandbox.window = sandbox;
vm.runInNewContext("(" + bg._domInstallPageRuntimeFn.toString() + ")()", sandbox);

const md = sandbox.__wsaRuntime.domNodeToMarkdown(root);
assert(md.includes("| Model | Quant File | Speed |"), "Missing table header: " + md);
assert(md.includes("| --- | --- | --- |"), "Missing GFM table separator: " + md);
assert(md.includes("| `TheDrummer/Valkyrie-49B` | `Valkyrie-49B-Q5_K_M.gguf` | ~50-56 tok/s |"), "Cells mashed or missing pipes: " + md);
assert(md.includes("1. Enable fast transfers") && md.includes("2. Launch server"), "Ordered list lost numbers: " + md);
assert(md.includes("> Important VRAM note"), "Blockquote missing quote prefix: " + md);
"""
        proc = subprocess.run(["node", "-e", js_code], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)

    def test_renderer_styles_prompt_cards_tables_and_theme_contrast(self):
        import io
        from rich.console import Console
        from wsa_agent.theme import THEMES

        self.assertEqual(THEMES["dark"].code_theme, "github-dark")
        self.assertEqual(THEMES["dark-daltonized"].code_theme, "one-dark")
        self.assertEqual(THEMES["github-dark"].code_theme, "github-dark")
        self.assertEqual(THEMES["dracula"].code_theme, "dracula")

        md = """### 1. Best Models

| Model | 40 GB File | Speed |
| --- | --- | --- |
| `TheDrummer/Valkyrie-49B` | `Valkyrie-49B-Q5_K_M.gguf` | ~50-56 tok/s |

### 2. Prompt to Give Colab Gemini

```text
I am on the Colab A100 High-RAM runtime. Make sure the setup checks `nvidia-smi` first:

1. Enable fast transfers (`pip install -U huggingface_hub`).
2. Launch `llama-server` on `0.0.0.0:8080`.
```
"""
        buf = io.StringIO()
        orig = renderer.console
        try:
            renderer.console = Console(file=buf, force_terminal=True, color_system=None, width=104, highlight=False)
            renderer.print_user_message("multi line\nprompt echo")
            renderer.print_agent_response(md)
            out = buf.getvalue()
        finally:
            renderer.console = orig

        self.assertIn("❯ multi line", out)
        self.assertIn("│ prompt echo", out)
        # Table renders with proper vertical column boundaries (never mashed together)
        self.assertIn("│ Model", out)
        self.assertIn("│ TheDrummer/Valkyrie-49B", out)
        self.assertIn("│ ~50-56 tok/s", out)
        # Prompt box uses open-right bordered card (`╭─ prompt`) without right-edge `│` copy artifacts
        self.assertIn("╭─ prompt", out)
        self.assertIn("│ I am on the Colab A100 High-RAM runtime", out)
        prompt_line = next(ln for ln in out.splitlines() if "│ I am on the Colab A100 High-RAM runtime" in ln)
        self.assertFalse(prompt_line.rstrip().endswith("│"), msg=f"Right border should stay open for clean copy: {prompt_line!r}")

    def test_streaming_and_final_preserve_think_tag_in_backticks_or_prose_and_ignore_codeblock_copy_actions(self):
        import io
        import subprocess
        from rich.console import Console
        import wsa_server as module

        # 1. Literal `<think>` / `<tool_call>` inside inline backticks, code fences, or unpaired prose must NEVER truncate long answers
        sample = (
            "1. **Core Persona, Voice & Anti-Slop Design**\n"
            "- **Multi-Layer Anti-Slop Pipeline (`backend/anti_slop.py`):**\n"
            "  1. **Guided 4-Step `<think>` Subtext Prefill:** Sends an assistant prefill (`\"prefix\": True`) on every turn.\n"
            "  2. **Sampler-Level Slop Suppression:** Configures DRY and XTC.\n"
            "  3. **Post-Generation Trimmer:** Strips robotic wrap-ups.\n\n"
            "```xml\n<think>\nExample fenced block\n</think>\n```\n\n"
            "Also prose mentioning <think> without closing tag.\n\n"
            "2. **Colab A100 High-RAM Backend & Verified GGUF Models**\n"
            "| Hardware Target | Size |\n|---|---|\n| Colab A100 80 GB | 53.91 GB |\n"
        )
        # Paired hidden XML tags outside code still strip cleanly
        with_real_think = "<think>\ninternal hidden trace\n</think>\n\n" + sample
        self.assertEqual(renderer._strip_internal_tags(with_real_think, streaming=True), renderer._strip_internal_tags(sample, streaming=False))
        final_clean = renderer._strip_internal_tags(with_real_think, streaming=False)
        self.assertNotIn("internal hidden trace", final_clean)
        self.assertIn("Guided 4-Step `<think>` Subtext Prefill", final_clean)
        self.assertIn("Colab A100 High-RAM Backend", final_clean)
        self.assertIn("53.91 GB", final_clean)

        # Streaming unclosed line-start <think> block hides only while streaming=True, never truncating real final text
        partial_stream = "<think>\nstill reasoning..."
        self.assertEqual(renderer._strip_internal_tags(partial_stream, streaming=True), "")
        self.assertIn("still reasoning...", renderer._strip_internal_tags(partial_stream, streaming=False))

        # StreamRenderer.feed + finish renders the complete answer end-to-end
        buf = io.StringIO()
        orig = renderer.console
        try:
            renderer.console = Console(file=buf, force_terminal=False, width=104, highlight=False)
            sr = renderer.StreamRenderer()
            for i in range(0, len(sample), 40):
                sr.feed(sample[i : i + 40])
            sr.finish()
            rendered_out = buf.getvalue()
        finally:
            renderer.console = orig
        self.assertIn("Guided 4-Step <think> Subtext Prefill", rendered_out)
        self.assertIn("Colab A100 High-RAM Backend & Verified GGUF Models", rendered_out)
        self.assertIn("53.91 GB", rendered_out)

        # 2. Server _find_pending_tool_hold_pos flushes single inline backticks instead of stalling word streams
        self.assertEqual(module._find_pending_tool_hold_pos("Sends `backend/anti_slop.py` or `"), -1)
        self.assertGreaterEqual(module._find_pending_tool_hold_pos("Running tool:\n```json"), 0)

        # 3. Extension ignores code-block Copy buttons mid-stream and promotes longer live DOM text if Relay lags
        bg_path = Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js"
        js_code = f"""
const assert = require("assert");
const vm = require("vm");
const bg = require({json.dumps(str(bg_path))});

const sandbox = {{
  document: {{
    querySelectorAll: () => [],
  }},
  getComputedStyle: () => ({{ display: "block", visibility: "visible", opacity: "1" }}),
  console, Date, Math, Array, Object, String, Number, Boolean, RegExp, JSON, Promise, setTimeout, clearTimeout,
}};
sandbox.globalThis = sandbox;
vm.runInNewContext("(" + bg._domInstallPageRuntimeFn.toString() + ")()", sandbox);
const rt = sandbox.__wsaRuntime;

const makeBtn = (label, ancestorTag) => ({{
  nodeType: 1,
  tagName: "BUTTON",
  offsetParent: {{}},
  getBoundingClientRect: () => ({{ width: 24, height: 24 }}),
  getClientRects: () => [{{}}],
  getAttribute: (k) => (k === "aria-label" ? label : null),
  closest: (sel) => (sel.toLowerCase().includes(ancestorTag) ? {{ tagName: ancestorTag.toUpperCase() }} : null),
}});

assert.strictEqual(rt.isTurnActionButton(makeBtn("Copy code", "code-block")), false);
assert.strictEqual(rt.isTurnActionButton(makeBtn("Copy table", "table-actions")), false);
assert.strictEqual(rt.isTurnActionButton(makeBtn("Good response", "message-actions")), true);

// When Relay stalls at 2000 chars while DOM has streamed 2500 chars, live selection switches to DOM
const pickedLong = rt.selectLiveTurnText({{
  relayText: "x".repeat(2000),
  domText: "x".repeat(2500),
  prevText: "x".repeat(2000),
  prevSource: "relay",
}});
assert.strictEqual(pickedLong.source, "dom");
assert.strictEqual(pickedLong.text.length, 2500);
console.log("LONG_STREAM_GUARDS_OK");
"""
        proc = subprocess.run(["node", "-e", js_code], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
        self.assertIn("LONG_STREAM_GUARDS_OK", proc.stdout)


class LongAgenticOptimizationRegressionTests(unittest.TestCase):
    def test_surgical_patching_guards_multi_hunk_diff_and_image_inspection(self):
        import struct
        from pathlib import Path
        from wsa_agent import tools

        orig_root = tools._project_root
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                tools.set_project_root(root)

                # 1. Large existing file (>60 lines, >2.5KB) rejects accidental full write_file overwrite
                big_lines = [f"def fn_{i}():\n    return {i}  # {'x' * 36}\n" for i in range(40)]
                big_src = "".join(big_lines)
                self.assertGreater(len(big_src.encode("utf-8")), 2500)
                created = tools.execute_tool("write_file", {"path": "big_mod.py", "content": big_src})
                self.assertIn("[OK] Created big_mod.py", created)

                refused = tools.execute_tool("write_file", {"path": "big_mod.py", "content": "x = 1\n"})
                self.assertIn("Surgical-edit policy", refused)
                self.assertIn("patch_file", refused)

                # 2. Lazy placeholder stub is rejected in both write_file and patch_file
                lazy_res = tools.execute_tool(
                    "patch_file",
                    {
                        "path": "big_mod.py",
                        "old_string": "def fn_2():\n    return 2  # " + ("x" * 36),
                        "new_string": "def fn_2():\n    # ... existing code ...\n    return 20",
                    },
                )
                self.assertIn("Lazy placeholder rejected", lazy_res)

                # 3. Trailing-whitespace-tolerant unique patch_file succeeds, plus start_line scoping + multi-hunk diff
                patched_ws = tools.execute_tool(
                    "patch_file",
                    {
                        "path": "big_mod.py",
                        "old_string": f"def fn_2():   \n    return 2  # {'x' * 36}\t",
                        "new_string": f"def fn_2():\n    return 222  # {'x' * 36}",
                    },
                )
                self.assertIn("[OK] Patched big_mod.py", patched_ws)
                self.assertIn("-    return 2", patched_ws)
                self.assertIn("+    return 222", patched_ws)

                multi_res = tools.execute_tool(
                    "patch_file",
                    {
                        "path": "big_mod.py",
                        "hunks": [
                            {"old": f"def fn_0():\n    return 0  # {'x' * 36}", "new": f"def fn_0():\n    return 100  # {'x' * 36}"},
                            {"old": f"def fn_5():\n    return 5  # {'x' * 36}", "new": f"def fn_5():\n    return 500  # {'x' * 36}"},
                        ],
                    },
                )
                self.assertIn("2 hunks applied", multi_res)
                self.assertIn("+    return 100", multi_res)
                self.assertIn("+    return 500", multi_res)

                # 4. Duplicate target resolved safely by start_line/end_line window
                dup_file = root / "dup.py"
                dup_file.write_text("a = 1\nb = 9\n\nc = 1\nb = 9\n", encoding="utf-8")
                scoped = tools.execute_tool(
                    "patch_file",
                    {"path": "dup.py", "old_string": "b = 9", "new_string": "b = 99", "start_line": 4, "end_line": 5},
                )
                self.assertIn("[OK] Patched dup.py", scoped)
                self.assertEqual(dup_file.read_text(encoding="utf-8"), "a = 1\nb = 9\n\nc = 1\nb = 99\n")

                # 5. PNG screenshot read_file returns metadata (not garbage) & inspect_image queues visual payload
                png_hdr = b"\x89PNG\r\n\x1a\n" + struct.pack(">II", 13, 21) + b"\x00\x00\x00\x00IEND\xaeB`\x82"
                # 13-byte IHDR chunk layout: length(4) + b'IHDR' + w(4) + h(4) + bit_depth/color/etc
                ihdr_data = struct.pack(">IIBBBBB", 320, 180, 8, 2, 0, 0, 0)
                valid_png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"
                (root / "ui_shot.png").write_bytes(valid_png)
                meta_out = tools.execute_tool("read_file", {"path": "ui_shot.png"})
                self.assertIn("PNG 320x180", meta_out)
                self.assertIn("inspect_image", meta_out)

                tools.consume_pending_images()
                vis_out = tools.execute_tool("inspect_image", {"path": "ui_shot.png"})
                self.assertIn("[OK] Loaded image 'ui_shot.png'", vis_out)
                queued = tools.consume_pending_images()
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0]["mime_type"], "image/png")
                self.assertTrue(queued[0]["url"].startswith("data:image/png;base64,"))
                self.assertEqual(tools.consume_pending_images(), [])
        finally:
            tools.set_project_root(orig_root)

    def test_screenshot_attachment_verification_gate_and_anti_laziness_continuation(self):
        import struct
        from pathlib import Path
        from unittest.mock import patch
        from wsa_agent import agent as agent_mod
        from wsa_agent import prompts, tools
        from wsa_agent.session import SessionState

        # 1. Internal prompts enforce Claude-grade surgical edits, visual audit, and anti-laziness
        st_prompt = SessionState(project_root=Path(__file__).resolve().parent.parent)
        dyn = prompts.build_dynamic_system_prompt(st_prompt)
        self.assertIn("surgical in-place replacements only", prompts.SYSTEM_PROMPT.lower())
        self.assertIn("zero laziness", prompts.SYSTEM_PROMPT.lower())
        self.assertIn("deep screenshot, browser & visual audit", prompts.SYSTEM_PROMPT.lower())
        self.assertIn("DOM-relay agentic discipline", dyn)

        class _SilentRenderer:
            def __init__(self, *a, **kw): pass
            def start_thinking(self): pass
            def stop_thinking(self): pass
            def feed(self, t): pass
            def feed_thinking(self, t): pass
            def reset_answer(self): pass
            def finish(self): pass
            def get_thinking_text(self): return ""
            def set_status_hint(self, s): pass

        orig_root = tools._project_root
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                tools.set_project_root(root)
                (root / "app.py").write_text("VAL = 1\n", encoding="utf-8")
                ihdr_data = struct.pack(">IIBBBBB", 64, 48, 8, 2, 0, 0, 0)
                shot_path = root / "bug_shot.png"
                shot_path.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00")

                # 2. @screenshot path auto-attaches image + triggers Verification Gate after patch_file + resumes lazy tail
                calls = {"n": 0, "first_user": None, "gate_prompts": []}

                class _LazyThenVerifiedClient:
                    def stream(self, messages, **kwargs):
                        if kwargs.get("use_tools") is False:
                            return {"content": '{"mode":"direct","reason":"isolated fix"}'}
                        calls["n"] += 1
                        if calls["n"] == 1:
                            calls["first_user"] = messages[-1]["content"]
                            return {
                                "content": "Patching VAL now.",
                                "tool_calls": [{
                                    "id": "tc_1",
                                    "type": "function",
                                    "function": {"name": "patch_file", "arguments": '{"path": "app.py", "old_string": "VAL = 1", "new_string": "VAL = 2"}'},
                                }],
                            }
                        if calls["n"] == 2:
                            # Stops after edit without running any verification -> triggers Verification Gate
                            return {"content": "Updated VAL to 2."}
                        if calls["n"] == 3:
                            calls["gate_prompts"].append(messages[-1]["content"])
                            # Runs python check, then ends with deferred laziness -> triggers Continue Execution
                            return {
                                "content": "Next, I will inspect the remaining config.",
                                "tool_calls": [{
                                    "id": "tc_2",
                                    "type": "function",
                                    "function": {"name": "run_python", "arguments": '{"code": "import app; print(app.VAL)"}'},
                                }],
                            }
                        if calls["n"] == 4:
                            return {"content": "Verified app.VAL == 2. Next, I will read config.json."}
                        calls["gate_prompts"].append(messages[-1]["content"])
                        return {"content": "All edits applied and verified."}

                st = SessionState(project_root=root, permission_mode="auto")
                with patch.object(agent_mod.R, "StreamRenderer", _SilentRenderer), \
                     patch.object(agent_mod.R, "begin_agent_response"), \
                     patch.object(agent_mod.R, "print_tool_call"), \
                     patch.object(agent_mod.R, "print_tool_result"), \
                     patch.object(agent_mod.R, "print_info"), \
                     patch.object(agent_mod, "_compact_messages_for_web_limit", return_value=0):
                    agent_mod.run_turn("Fix bug shown in @bug_shot.png", st, _LazyThenVerifiedClient())

                self.assertEqual(calls["n"], 5)
                self.assertIsInstance(calls["first_user"], list)
                self.assertEqual(calls["first_user"][1]["type"], "image_url")
                self.assertTrue(calls["first_user"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
                self.assertTrue(any("[System Verification Gate]" in p for p in calls["gate_prompts"]))
                self.assertTrue(any("[Continue Execution]" in p for p in calls["gate_prompts"]))
                # Completed turn state condenses base64 blobs on persisted state.messages
                self.assertIsInstance(st.messages[0]["content"], str)
                self.assertIn("Attached 1 screenshot", st.messages[0]["content"])
        finally:
            tools.set_project_root(orig_root)

    def test_dom_relay_forwards_images_for_argon_browser_and_extension_wires_paste(self):
        import base64
        import subprocess
        import threading
        import time
        from pathlib import Path

        module = __import__("wsa_server")
        with module._dom_relay_lock:
            old_active = module._dom_relay.get("active_relay_id")
            old_relays = dict(module._dom_relay.get("relays") or {})
            old_pending = dict(module._dom_relay.get("pending") or {})
            old_pinned = module._dom_relay.get("pinned_relay_label")
            old_lock = module._dom_relay.get("explicit_relay_lock", False)
            module._dom_relay["relays"] = {}
            module._dom_relay["pending"] = {}
            module._dom_relay["active_relay_id"] = None
            module._dom_relay["pinned_relay_label"] = None
            module._dom_relay["explicit_relay_lock"] = False

        try:
            png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10\x08\x02\x00\x00\x00"
            captured_pending = {}

            def _worker():
                deadline = time.time() + 4.0
                while time.time() < deadline:
                    item = module.dom_relay_get_pending()
                    if item:
                        captured_pending.update(item)
                        module.dom_relay_post_response(item["id"], response="Analyzed UI screenshot", model="4 Argon High")
                        return
                    time.sleep(0.02)

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            events = list(module.dom_relay_iter(
                "Inspect this screenshot",
                timeout=5.0,
                pickup_timeout=3.0,
                step=0.05,
                images=[(png_bytes, "image/png")],
            ))
            t.join(timeout=4.0)

            self.assertEqual(len(captured_pending.get("images") or []), 1)
            img_item = captured_pending["images"][0]
            self.assertEqual(img_item["mime_type"], "image/png")
            self.assertEqual(base64.b64decode(img_item["data"]), png_bytes)
            self.assertTrue(any(k == "result" and v["response"] == "Analyzed UI screenshot" for k, v in events))

            bg_path = Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js"
            bg_src = bg_path.read_text(encoding="utf-8")
            self.assertIn("attachImagesToPage", bg_src)
            self.assertIn("pending.images", bg_src)
            self.assertIn("ClipboardEvent", bg_src)
            chk = subprocess.run(["node", "--check", str(bg_path)], capture_output=True, text=True, check=False)
            self.assertEqual(chk.returncode, 0, msg=chk.stderr or chk.stdout)
        finally:
            with module._dom_relay_lock:
                module._dom_relay["active_relay_id"] = old_active
                module._dom_relay["relays"] = old_relays
                module._dom_relay["pending"] = old_pending
                module._dom_relay["pinned_relay_label"] = old_pinned
                module._dom_relay["explicit_relay_lock"] = old_lock

    def test_live_browser_tab_bridge_inspect_interact_screenshot_and_safety_guards(self):
        import base64
        import json
        import struct
        import subprocess
        import threading
        import time
        from pathlib import Path
        from unittest.mock import patch
        from wsa_agent import tools

        module = __import__("wsa_server")
        ext_dir = Path(__file__).resolve().parent.parent / "wsa-browser-bridge"
        manifest = json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("http://*/*", manifest.get("host_permissions", []))
        self.assertIn("https://*/*", manifest.get("host_permissions", []))

        # Verify background.js protection & URL redaction helpers via Node
        bg_path = ext_dir / "background.js"
        js_check = f"""
const assert = require("assert");
const bg = require({json.dumps(str(bg_path))});
assert.strictEqual(bg.isProtectedInspectUrl("https://gemini.google.com/u/0/app/abc"), true);
assert.strictEqual(bg.isProtectedInspectUrl("chrome://extensions"), true);
assert.strictEqual(bg.isProtectedInspectUrl("https://accounts.google.com/signin"), true);
assert.strictEqual(bg.isProtectedInspectUrl("http://127.0.0.1:5000/dashboard"), false);
assert.strictEqual(
  bg.sanitizeInspectUrl("http://localhost:3000/cb?token=secret123&tab=home"),
  "http://localhost:3000/cb?token=%5BREDACTED%5D&tab=home"
);
console.log("BROWSER_BRIDGE_GUARDS_OK");
"""
        proc = subprocess.run(["node", "-e", js_check], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
        self.assertIn("BROWSER_BRIDGE_GUARDS_OK", proc.stdout)

        # Verify server browser_cmd dispatch & result delivery
        with module._dom_relay_lock:
            old_active = module._dom_relay.get("active_relay_id")
            old_relays = dict(module._dom_relay.get("relays") or {})
            old_cmds = dict(module._dom_relay.get("browser_cmds") or {})
            module._dom_relay["relays"] = {
                "brave-live": {"client_id": "brave-live", "browser": "brave", "label": "brave", "last_seen": time.time()}
            }
            module._dom_relay["active_relay_id"] = "brave-live"
            module._dom_relay["browser_cmds"] = {}

        try:
            def _ext_poller():
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    item, _ = module.dom_relay_poll_for_client({"client_id": "brave-live", "browser": "brave", "label": "brave"})
                    if item and item.get("browser_cmd"):
                        bcmd = item["browser_cmd"]
                        module.browser_cmd_post_result(bcmd["cmd_id"], {
                            "ok": True,
                            "tab_id": 42,
                            "diagnostics": {"title": "Studio App", "url": "http://127.0.0.1:5000/", "readyState": "complete"},
                        })
                        return
                    time.sleep(0.02)

            th = threading.Thread(target=_ext_poller, daemon=True)
            th.start()
            res = module.execute_browser_command("inspect", {"url": "http://127.0.0.1:5000/"}, timeout=3.0)
            th.join(timeout=3.0)
            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("tab_id"), 42)
        finally:
            with module._dom_relay_lock:
                module._dom_relay["active_relay_id"] = old_active
                module._dom_relay["relays"] = old_relays
                module._dom_relay["browser_cmds"] = old_cmds

        # Verify wsa_agent tools.browser_inspect formats diagnostics & queues live screenshot visually
        ihdr = struct.pack(">IIBBBBB", 400, 300, 8, 2, 0, 0, 0)
        png_raw = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"
        data_uri = "data:image/png;base64," + base64.b64encode(png_raw).decode("ascii")
        tools.consume_pending_images()
        with patch.object(tools, "_call_browser_bridge", return_value={
            "ok": True,
            "tab_id": 77,
            "diagnostics": {
                "title": "Aanya UI",
                "url": "http://127.0.0.1:5000/",
                "readyState": "complete",
                "viewport": {"width": 1280, "height": 720, "scrollY": 0, "scrollHeight": 900},
                "horizOverflowPx": 24,
                "brokenImages": ["avatar.png"],
                "clippedElements": [".sidebar-btn (\"Settings\": 140px>90px)"],
                "consoleErrors": [{"kind": "console.error", "msg": "TypeError: undefined is not a function"}],
                "visibleText": "Welcome to Aanya Studio",
            },
            "screenshot_data_url": data_uri,
        }):
            out = tools.execute_tool("browser_inspect", {"url": "http://127.0.0.1:5000/"})
        self.assertIn("Live screenshot queued", out)
        self.assertIn("Horizontal overflow: +24px", out)
        self.assertIn("TypeError: undefined is not a function", out)
        queued = tools.consume_pending_images()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["mime_type"], "image/png")

    def test_agent_stops_cleanly_after_fix_and_verification_pass_without_infinite_loop(self):
        from pathlib import Path
        from unittest.mock import patch
        from wsa_agent import agent as agent_mod
        from wsa_agent import tools
        from wsa_agent.session import SessionState

        class _SilentRenderer:
            def __init__(self, *a, **kw): pass
            def start_thinking(self): pass
            def stop_thinking(self): pass
            def feed(self, t): pass
            def feed_thinking(self, t): pass
            def reset_answer(self): pass
            def finish(self): pass
            def get_thinking_text(self): return ""
            def set_status_hint(self, s): pass

        orig_root = tools._project_root
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                tools.set_project_root(root)
                (root / "tests").mkdir()
                (root / "tests" / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
                (root / "mod.py").write_text("X = 1\n", encoding="utf-8")

                # 1. Failed verification command (Exit code: 1) does NOT falsely mark verified,
                #    while passing command (Exit code: 0) stops test-hint nagging & stops cleanly!
                self.assertFalse(agent_mod._command_passed("STDOUT:\nFAILED\nExit code: 1"))
                self.assertTrue(agent_mod._command_passed("STDOUT:\n1 passed\nExit code: 0"))

                seen_tool_results = []
                calls = {"n": 0}

                class _FixAndVerifyCleanlyClient:
                    def stream(self, messages, **kwargs):
                        calls["n"] += 1
                        if calls["n"] == 1:
                            return {
                                "content": "Patching mod.py.",
                                "tool_calls": [{
                                    "id": "tc_p",
                                    "type": "function",
                                    "function": {"name": "patch_file", "arguments": '{"path": "mod.py", "old_string": "X = 1", "new_string": "X = 2"}'},
                                }],
                            }
                        if calls["n"] == 2:
                            return {
                                "content": "Running pytest.",
                                "tool_calls": [{
                                    "id": "tc_t",
                                    "type": "function",
                                    "function": {"name": "run_python", "arguments": '{"code": "import mod; assert mod.X == 2; print(\'1 passed\')"}'},
                                }],
                            }
                        # Capture the tool message fed back after passing verification
                        seen_tool_results.append(messages[-1]["content"])
                        # Ends with a polite optional offer -> must NOT trigger an unwanted loop!
                        return {
                            "content": "All fixes are complete and verified.\nLet me know if you want me to implement extra logging later."
                        }

                st = SessionState(project_root=root, permission_mode="auto")
                with patch.object(agent_mod.R, "StreamRenderer", _SilentRenderer), \
                     patch.object(agent_mod.R, "begin_agent_response"), \
                     patch.object(agent_mod.R, "print_tool_call"), \
                     patch.object(agent_mod.R, "print_tool_result"), \
                     patch.object(agent_mod.R, "print_info"), \
                     patch.object(agent_mod, "_compact_messages_for_web_limit", return_value=0):
                    agent_mod.run_turn("Fix X in mod.py", st, _FixAndVerifyCleanlyClient())

                self.assertEqual(calls["n"], 3)
                self.assertTrue(seen_tool_results)
                self.assertNotIn("After all edits are complete, run: python -m pytest", seen_tool_results[-1])
        finally:
            tools.set_project_root(orig_root)

    def test_markdown_url_in_code_guard_and_ui_verification_gate(self):
        orig_root = tools._project_root
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                tools.set_project_root(root)

                # Rejects markdown-wrapped URL inside script/code files and fenced markdown code blocks
                _url = "http://127.0.0.1"
                bad_py = tools.execute_tool("write_file", {
                    "path": "fetch.py",
                    "content": f"URL = '[{_url}]({_url}):5050'\n",
                })
                self.assertIn("Corrupted Markdown link", bad_py)
                self.assertFalse((root / "fetch.py").exists())

                bad_md = tools.execute_tool("write_file", {
                    "path": "setup.md",
                    "content": f"Docs: [Home](https://example.com)\n```bash\ncurl [{_url}]({_url}):8080/health\n```\n",
                })
                self.assertIn("Corrupted Markdown link inside fenced code block", bad_md)

                ok_md = tools.execute_tool("write_file", {
                    "path": "guide.md",
                    "content": "See [Docs](https://example.com) then run:\n```bash\ncurl http://127.0.0.1:8080/health\n```\n",
                })
                self.assertTrue(ok_md.startswith("[OK]"), msg=ok_md)

                # UI verification gate nudges once when .html/.css/.js changed without browser verification
                ui_nudge = agent_module._maybe_anti_laziness_nudge(
                    content="Updated templates/index.html.",
                    written_this_turn={"templates/index.html"},
                    verified_after_last_edit=True,
                    tests_exist=True,
                    round_num=1,
                    verify_nudges_used=0,
                    continue_nudges_used=0,
                    free_mode=False,
                    browser_verified_after_ui_edit=False,
                    ui_verify_nudges_used=0,
                )
                self.assertIsNotNone(ui_nudge)
                self.assertEqual(ui_nudge[0], "ui_verify")

                no_loop = agent_module._maybe_anti_laziness_nudge(
                    content="Updated templates/index.html.",
                    written_this_turn={"templates/index.html"},
                    verified_after_last_edit=True,
                    tests_exist=True,
                    round_num=2,
                    verify_nudges_used=0,
                    continue_nudges_used=0,
                    free_mode=False,
                    browser_verified_after_ui_edit=False,
                    ui_verify_nudges_used=1,
                )
                self.assertIsNone(no_loop)
        finally:
            tools.set_project_root(orig_root)

    def test_prompts_include_human_first_ui_and_url_discipline(self):
        from wsa_agent.prompts import SYSTEM_PROMPT, build_dynamic_system_prompt
        self.assertIn("Never write Markdown-formatted URLs", SYSTEM_PROMPT)
        self.assertIn("Custom-style `<input type=\"range\">`", SYSTEM_PROMPT)
        dyn = build_dynamic_system_prompt(SessionState(project_root=Path(".")))
        self.assertIn("UI craft", dyn) if "UI craft" in dyn else self.assertIn("browser_inspect", dyn)


class TestClaudeThinkingAndTuiOptimizations(unittest.TestCase):
    def test_cross_provider_model_badge_isolation(self):
        from wsa_agent.session import get_model_info

        st = SessionState(project_root=Path("."))
        st.set_model("gemini")
        st.sync_account_from_health({
            "active_relay": "chrome",
            "active_browser": "chrome",
            "active_email": "user@example.com",
            "active_site": "claude.ai",
            "active_provider": "claude",
            "active_model_badge": "Sonnet 5 High",
            "active_providers": {
                "claude": {"has_tab": True, "model_badge": "Sonnet 5 High", "email": "user@example.com"},
                "gemini": {"has_tab": False, "model_badge": "", "email": "user@example.com"},
            },
        })
        self.assertEqual(st.active_web_model, "")
        self.assertNotIn("Sonnet", get_model_info("gemini", st)["thinking"])
        self.assertEqual(get_model_info("claude", st)["thinking"], "Web: Sonnet 5 High")

        self.assertTrue(st.set_model("claude"))
        self.assertEqual(st.active_web_model, "Sonnet 5 High")
        self.assertTrue(st.set_model("gemini"))
        self.assertEqual(st.active_web_model, "")

        # /account bind payload pointing to Claude must not stamp Sonnet onto Gemini
        st.sync_account_from_res({
            "active_relay": "chrome",
            "active_site": "claude.ai",
            "provider": "claude",
            "model_badge": "Sonnet 5 High",
            "providers": {
                "claude": {"has_tab": True, "model_badge": "Sonnet 5 High"},
                "gemini": {"has_tab": False, "model_badge": ""},
            },
        })
        self.assertEqual(st.active_web_model, "")

    def test_relay_cleans_claude_thinking_chrome_and_computes_clean_delta(self):
        from wsa_server.relay import _clean_claude_thinking_text, _compute_thinking_delta

        raw1 = "Weighing whether to inspect tools running\n22s\nWeighing whether to inspect tools"
        clean1 = _clean_claude_thinking_text(raw1)
        self.assertEqual(clean1, "Weighing whether to inspect tools")

        d1, sent1 = _compute_thinking_delta("", raw1, provider="claude")
        self.assertEqual(d1, "Weighing whether to inspect tools")

        raw2 = "Thought for 21s\nWeighing how to handle relay requests running\nWeighing whether to inspect tools\n30s"
        d2, sent2 = _compute_thinking_delta(sent1, raw2, provider="claude")
        self.assertEqual(d2, "\nWeighing how to handle relay requests")
        self.assertNotIn("22s", sent2)
        self.assertNotIn("running", sent2)
        self.assertNotIn("Thought for 21s", sent2)

    def test_claude_tui_thinking_trace_fence_clipping_and_partial_tool_suppression(self):
        import io
        from rich.console import Console

        # 1. Partial tool fences are hidden while streaming, but TS/normal code blocks render immediately
        self.assertEqual(renderer._strip_internal_tags("Intro:\n```tool\n{\"name\": \"read_file\"", streaming=True), "Intro:")
        self.assertEqual(renderer._strip_internal_tags("Intro:\n```to", streaming=True), "Intro:")
        self.assertIn("```ts\nconst x = 1;", renderer._strip_internal_tags("Intro:\n```ts\nconst x = 1;", streaming=True))
        self.assertTrue(renderer._has_active_tool_fence("Intro:\n```tool"))
        self.assertFalse(renderer._has_active_tool_fence("```python\n```tool\n"))

        # 2. Fence-aware live slicing preserves opening ```python header across top crop
        long_code = "Intro\n```python\n" + "\n".join(f"x_{i} = {i}" for i in range(30)) + "\n```"
        buf = io.StringIO()
        orig = renderer.console
        try:
            renderer.console = Console(file=buf, force_terminal=False, width=64, height=14, highlight=False)
            sliced = renderer._clip_live_preview_rows(long_code, max_rows=8)
            self.assertTrue(sliced.startswith("…\n```python\n"), msg=sliced)

            clipped, bounded = renderer._render_live_markdown_bounded(long_code, reserve_rows=4)
            lines = renderer.console.render_lines(
                bounded,
                renderer.console.options.update(width=64),
                pad=False,
                new_lines=False,
            )
            self.assertLessEqual(len(lines), 10)
            self.assertIn("```python", clipped)

            # 3. Claude multi-step live ticker + atomic vertical thinking summary trace
            sr = renderer.StreamRenderer(round_num=0, model="claude")
            sr._in_thinking = True
            sr.feed_thinking("Weighing whether to inspect tools running\n22s")
            sr.feed_thinking("\nPlanning direct file edit")
            live_tick = sr._thinking_renderable()
            self.assertIn("·  Weighing whether to inspect tools", live_tick.plain)
            self.assertIn("⎿  Planning direct file edit", live_tick.plain)
            self.assertNotIn("22s", live_tick.plain)
            sr.feed("Done inspecting.")
            sr.finish()
        finally:
            renderer.console = orig

        out = buf.getvalue()
        self.assertIn("∴ Thought", out)
        self.assertIn("2 steps", out)
        self.assertIn("│ Weighing whether to inspect tools", out)
        self.assertIn("│ Planning direct file edit", out)
        self.assertIn("Done inspecting.", out)

    def test_welcome_card_no_orphan_wrap_literal_resume_brackets_and_conhost_glyphs(self):
        import io
        from rich.console import Console
        from wsa_agent import __main__ as main_mod, tui as tui_mod

        st = SessionState(project_root=Path("."))
        st.active_account_label = "brave"
        st.active_account_email = "godxassassin121@gmail.com"
        st.active_site = "gemini.google.com"
        st.providers_info = {"gemini": {"has_tab": True, "email": "godxassassin121@gmail.com", "model_badge": ""}}

        # 1. Welcome card never wraps 'gemini' or 'bridges' onto orphan lines across narrow/standard/wide terminals
        for w in (88, 104, 180):
            buf = io.StringIO()
            orig = renderer.console
            try:
                renderer.console = Console(file=buf, force_terminal=False, width=w, highlight=False)
                renderer.print_welcome(st, server_ok=True)
                out = buf.getvalue()
            finally:
                renderer.console = orig
            lines = [ln.strip(" │") for ln in out.splitlines() if ln.strip(" │")]
            self.assertNotIn("gemini", lines, msg=f"Orphan 'gemini' line at width={w}:\n{out}")
            self.assertNotIn("bridges", lines, msg=f"Orphan 'bridges' line at width={w}:\n{out}")

        # 2. _try_resume and tui.confirm print literal [y/N] without Rich eating brackets
        fake_sessions = [{"file": "s1.json", "name": "s1", "project": str(st.project_root), "turns": 1, "saved_at": "2026-11-14 01:00:00"}]
        buf_res = io.StringIO()
        orig = renderer.console
        try:
            renderer.console = Console(file=buf_res, force_terminal=False, width=120, highlight=False)
            with patch.object(st, "list_saved_sessions", return_value=fake_sessions), \
                 patch("builtins.input", side_effect=["n", "n"]):
                self.assertFalse(main_mod._try_resume(st))
                self.assertFalse(tui_mod.confirm("Confirm", "Continue?", default=False))
            out_prompts = buf_res.getvalue()
        finally:
            renderer.console = orig
        self.assertIn("Resume? [y/N]:", out_prompts)
        self.assertIn("(1 turn,", out_prompts)
        self.assertIn("Continue? [y/N]", out_prompts)

        # 3. Legacy Windows conhost (Consolas, no WT_SESSION) maps tofu-prone glyphs cleanly
        fake_tty = type("_FakeTTY", (), {"isatty": lambda self: True})()
        with patch.object(renderer.sys, "platform", "win32"), \
             patch.object(renderer.sys, "stdout", fake_tty), \
             patch.dict(os.environ, {"WT_SESSION": "", "TERM_PROGRAM": "", "ConEmuPID": "", "ANSICON": ""}, clear=False), \
             patch.object(renderer, "_get_windows_console_font", return_value="consolas"):
            self.assertEqual(renderer.ui_glyph("chevron"), "›")
            self.assertEqual(renderer.ui_glyph("mode_bypass"), "»")
            self.assertEqual(renderer.ui_glyph("thought"), "·")
            self.assertEqual(renderer.ui_glyph("branch"), "└")

        with patch.object(renderer.sys, "platform", "win32"), \
             patch.object(renderer.sys, "stdout", fake_tty), \
             patch.dict(os.environ, {"WT_SESSION": "wt-1"}, clear=False):
            self.assertEqual(renderer.ui_glyph("chevron"), "❯")
            self.assertEqual(renderer.ui_glyph("mode_bypass"), "▸▸")
            self.assertEqual(renderer.ui_glyph("thought"), "∴")
            self.assertEqual(renderer.ui_glyph("branch"), "⎿")

    def test_claude_thinking_dedup_glued_chrome_and_orphan_tool_headers(self):
        from wsa_server import relay as srv_relay
        from wsa_server import tools as srv_tools

        raw_with_leaks = (
            'tool_calltool_call\n\n```tool_call\n'
            '{"name": "read_file", "arguments": {"path": "foo.py"}}\n'
            '```\n\ntool_call\nDone reading.'
        )
        remaining, calls = srv_tools.parse_tool_calls(raw_with_leaks)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        self.assertEqual(remaining.strip(), "Done reading.")

        glued_think = (
            "Surveying the codebase before choosing improvements"
            "Surveying the codebase before choosing improvements\n"
            "Reading the context module to see what it preservesrunning"
            "Reading the context module to see what it preserves\n"
            "Weighing how to handle the relay-style requests22srunning"
            "Weighing how to handle the relay-style requests"
        )
        cleaned_srv = srv_relay._clean_claude_thinking_text(glued_think)
        self.assertNotIn("22s", cleaned_srv)
        self.assertNotIn("running", cleaned_srv.lower())
        self.assertEqual(
            cleaned_srv.count("Surveying the codebase before choosing improvements"), 1
        )

        steps = renderer._clean_thinking_lines(glued_think)
        self.assertEqual(
            steps,
            [
                "Surveying the codebase before choosing improvements",
                "Reading the context module to see what it preserves",
                "Weighing how to handle the relay-style requests",
            ],
        )
        # Ensure real English word ending in 'running' is not split
        prose_steps = renderer._clean_thinking_lines("Avoiding overrunning the buffer")
        self.assertEqual(prose_steps, ["Avoiding overrunning the buffer"])

        rendered = renderer._strip_internal_tags(
            'tool_calltool_call\n\n```tool_call\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```\n'
            'Here is `tool_call` in prose.'
        )
        self.assertEqual(rendered, "Here is `tool_call` in prose.")

        sanitized_with_tools = agent_module._sanitize_assistant_tool_prose(raw_with_leaks, has_tool_calls=True)
        self.assertFalse(sanitized_with_tools.lstrip().startswith("tool_calltool_call"))
        sanitized_no_tools = agent_module._sanitize_assistant_tool_prose("tool_call\nHello", has_tool_calls=False)
        self.assertEqual(sanitized_no_tools, "Hello")

    def test_dom_relay_cancel_and_short_answer_shrink_reset(self):
        import threading
        import time
        from wsa_server import relay as srv_relay

        orig_pending = dict(srv_relay._dom_relay["pending"])
        orig_cancelled = dict(srv_relay._dom_relay.get("cancelled_req_ids") or {})
        orig_poll_ts = srv_relay._dom_relay.get("last_poll_ts", 0.0)
        orig_active = srv_relay._dom_relay.get("active_relay_id")
        orig_relays = dict(srv_relay._dom_relay.get("relays") or {})
        orig_recent = dict(srv_relay._dom_relay.get("recent_requests") or {})
        try:
            srv_relay._dom_relay["pending"].clear()
            srv_relay._dom_relay["cancelled_req_ids"].clear()
            srv_relay._dom_relay["relays"].clear()
            srv_relay._dom_relay["recent_requests"].clear()
            srv_relay._dom_relay["active_relay_id"] = None
            srv_relay._dom_relay["last_poll_ts"] = time.time()

            # 1. Explicit cancellation wakes waiter, marks cancelled list, blocks progress/dispatch
            ev = threading.Event()
            srv_relay._dom_relay["pending"]["req-cancel-1"] = {
                "prompt": "hi",
                "fallback_prompt": "hi",
                "provider": "claude",
                "images": [],
                "session_id": "sess-c",
                "client_request_id": "crid-c1",
                "target_relay_id": "",
                "explicit_relay_target": False,
                "dispatched_to": "",
                "response": None,
                "error": None,
                "thinking": "",
                "model": "",
                "chat_url": "",
                "progress_seq": 0,
                "new_chat": False,
                "created_at": time.time(),
                "dispatched": False,
                "cancelled": False,
                "event": ev,
            }
            cancelled = srv_relay.dom_relay_cancel_request(client_request_id="crid-c1", session_id="sess-c")
            self.assertEqual(cancelled, ["req-cancel-1"])
            self.assertTrue(ev.is_set())
            self.assertTrue(srv_relay.is_dom_relay_cancelled("req-cancel-1"))
            self.assertIn("req-cancel-1", srv_relay.list_cancelled_req_ids())
            self.assertIsNone(srv_relay.dom_relay_get_pending())
            self.assertFalse(srv_relay.dom_relay_post_progress("req-cancel-1", response="late", seq=1))

            # 2. Shrink reset allows shorter clean replacement answer ("Good") after noisy transient text
            srv_relay._dom_relay["pending"].clear()
            srv_relay._dom_relay["cancelled_req_ids"].clear()
            srv_relay._dom_relay["last_poll_ts"] = time.time()

            def _driver():
                deadline = time.time() + 2.0
                pending_job = None
                while time.time() < deadline and not pending_job:
                    pending_job = srv_relay.dom_relay_get_pending()
                    if not pending_job:
                        time.sleep(0.02)
                if not pending_job:
                    return
                rid = pending_job["id"]
                time.sleep(0.04)
                srv_relay.dom_relay_post_progress(
                    rid,
                    response="Transient noisy status text longer than final reply",
                    seq=1,
                )
                time.sleep(0.08)
                srv_relay.dom_relay_post_progress(rid, response="Good", seq=2)
                time.sleep(0.08)
                srv_relay.dom_relay_post_response(rid, response="Good", thinking="Checking files")

            t = threading.Thread(target=_driver, daemon=True)
            t.start()
            events = list(
                srv_relay.dom_relay_iter(
                    "Say Good",
                    timeout=5.0,
                    pickup_timeout=2.0,
                    step=0.03,
                    session_id="sess-shrink",
                    client_request_id="crid-shrink",
                    provider="claude",
                )
            )
            t.join(timeout=2.0)

            resets = [e for e in events if isinstance(e, tuple) and e[0] == "reset_content"]
            self.assertTrue(resets, msg=f"Expected reset_content event on shorter clean reply, got: {events}")
            self.assertIn(("content_delta", "Good"), events)
        finally:
            with srv_relay._dom_relay_lock:
                srv_relay._dom_relay["pending"].clear()
                srv_relay._dom_relay["pending"].update(orig_pending)
                srv_relay._dom_relay["cancelled_req_ids"].clear()
                srv_relay._dom_relay["cancelled_req_ids"].update(orig_cancelled)
                srv_relay._dom_relay["relays"].clear()
                srv_relay._dom_relay["relays"].update(orig_relays)
                srv_relay._dom_relay["recent_requests"].clear()
                srv_relay._dom_relay["recent_requests"].update(orig_recent)
                srv_relay._dom_relay["active_relay_id"] = orig_active
                srv_relay._dom_relay["last_poll_ts"] = orig_poll_ts

    def test_ctrl_c_interrupt_across_client_tools_subagents_and_repl(self):
        from unittest.mock import MagicMock
        from wsa_agent import __main__ as main_mod
        from wsa_agent import subagents as sub_mod

        # 1. WSAClient.stream cancels server relay and re-raises KeyboardInterrupt without retrying
        c = client_module.WSAClient("http://127.0.0.1:8081/v1", "test-key", timeout=5)
        stream_ctx = MagicMock()
        stream_ctx.__enter__.side_effect = KeyboardInterrupt()
        post_resp = MagicMock(status_code=200)
        with patch.object(c._client, "stream", return_value=stream_ctx) as mock_stream, \
             patch.object(c._client, "post", return_value=post_resp) as mock_post:
            with self.assertRaises(KeyboardInterrupt):
                c.stream(
                    [{"role": "user", "content": "hi"}],
                    model="claude",
                    session_id="sess-ki",
                )
            self.assertEqual(mock_stream.call_count, 1)
            cancel_calls = [call for call in mock_post.call_args_list if "/relays/cancel" in str(call.args[0])]
            self.assertTrue(cancel_calls)
            sent_json = cancel_calls[0].kwargs.get("json") or {}
            self.assertEqual(sent_json.get("session_id"), "sess-ki")
            self.assertTrue(bool(sent_json.get("client_request_id")))

        # 2. _run_subprocess_tree_safe kills child tree and propagates KeyboardInterrupt
        fake_proc = MagicMock()
        fake_proc.pid = 4242
        fake_proc.communicate.side_effect = [KeyboardInterrupt(), ("", "")]
        with patch.object(tools.subprocess, "Popen", return_value=fake_proc), \
             patch.object(tools, "_kill_process_tree") as mock_kill_tree:
            with self.assertRaises(KeyboardInterrupt):
                tools._run_subprocess_tree_safe(["echo", "hi"], timeout=5)
            mock_kill_tree.assert_called_once_with(4242)
            fake_proc.kill.assert_called_once()

        # 3. Orchestrator stops immediately when Explorer or Coder returns aborted_reason="interrupted"
        with tempfile.TemporaryDirectory() as tmp:
            st = SessionState(project_root=Path(tmp))
            dummy_client = MagicMock()
            with patch.object(
                sub_mod,
                "_run_role_agent",
                return_value={"ok": False, "aborted_reason": "interrupted", "final_text": "", "written_files": []},
            ) as mock_role:
                res = sub_mod.run_orchestrated_turn("refactor entire repo", st, dummy_client)
                self.assertEqual(mock_role.call_count, 1)
                self.assertFalse(res["ok"])
                self.assertEqual(res.get("aborted_reason"), "interrupted")

            with patch.object(
                sub_mod,
                "_run_role_agent",
                side_effect=[
                    {"ok": True, "aborted_reason": "", "final_text": "found app.py", "written_files": []},
                    {
                        "ok": True,
                        "aborted_reason": "",
                        "final_text": '```json\n[{"id":"t1","title":"Fix app","owned_files":["app.py"],"depends_on":[]}]\n```',
                        "written_files": [],
                    },
                    {"ok": False, "aborted_reason": "interrupted", "final_text": "", "written_files": ["app.py"]},
                ],
            ) as mock_role:
                res2 = sub_mod.run_orchestrated_turn("refactor entire repo", st, dummy_client)
                self.assertEqual(mock_role.call_count, 3)  # explorer, architect, coder — verifier skipped!
                self.assertFalse(res2["ok"])
                self.assertEqual(res2.get("aborted_reason"), "interrupted")

            # 4. Double Ctrl+C at REPL prompt autosaves and exits loop cleanly
            st_repl = SessionState(project_root=Path(tmp))
            repl_client = MagicMock()
            repl_client.get_health.return_value = {}
            repl_client.check_connection.return_value = True
            with patch.object(main_mod, "_make_prompt_session", return_value=None), \
                 patch.object(main_mod, "_print_welcome"), \
                 patch.object(main_mod, "_try_resume", return_value=False), \
                 patch.object(main_mod, "_autosave") as mock_save, \
                 patch.object(main_mod, "_get_input", side_effect=[KeyboardInterrupt(), KeyboardInterrupt()]):
                main_mod.run_repl(st_repl, repl_client)
                self.assertEqual(mock_save.call_count, 1)

    def test_workspace_session_isolation_load_sync_and_toolbar_project_visibility(self):
        from wsa_agent import __main__ as main_mod
        from wsa_agent import commands as cmd_mod

        orig_root = tools.get_project_root()
        try:
            with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
                p1 = Path(tmp1).resolve()
                p2 = Path(tmp2).resolve()

                st = SessionState(project_root=p1, selected_model="claude")
                st.file_ledger.record_read("foo.py", 1, 5, "x = 1\n")
                st.decisions.append("keep foo")
                st.plan_items.append({"id": "t1", "title": "do foo", "status": "done"})
                st.turn_digest.append({"turn": 1, "summary": "foo"})
                st.role_threads["coder"] = "role-1"
                st.total_tokens_in = 123
                st.total_tokens_out = 45
                st._workspace_snapshot = "foo.py"

                # 1. sync_account_from_res does not latch unrelated chat_url on an empty session
                st.sync_account_from_res({
                    "active_relay": "chrome",
                    "active_site": "claude.ai",
                    "provider": "claude",
                    "chat_url": "https://claude.ai/chat/unrelated-tab",
                })
                self.assertEqual(st.active_chat_url, "")

                # Once a turn response arrives, chat_url IS latched
                st.sync_account_from_res({
                    "active_relay": "chrome",
                    "active_site": "claude.ai",
                    "provider": "claude",
                    "chat_url": "https://claude.ai/chat/real-turn",
                    "thinking_chars": 12,
                })
                self.assertEqual(st.active_chat_url, "https://claude.ai/chat/real-turn")

                # 2. Saving session in p1, switching via set_project(p2) clears all p1 workspace state
                save_file = Path(st.save())
                st.set_project(p2)
                self.assertEqual(st.project_root, p2)
                self.assertEqual(st.messages, [])
                self.assertEqual(st.decisions, [])
                self.assertEqual(st.plan_items, [])
                self.assertEqual(st.turn_digest, [])
                self.assertEqual(st.role_threads, {})
                self.assertEqual(st.total_tokens_in, 0)
                self.assertEqual(st.total_tokens_out, 0)
                self.assertEqual(st.active_chat_url, "")
                self.assertEqual(st.file_ledger.entries, {})
                self.assertIsNone(st.__dict__.get("_workspace_snapshot"))

                # 3. Loading p1 session re-aligns project_root + tools.get_project_root via /load
                fake_entry = {"name": p1.name, "project": str(p1), "turns": 0, "saved_at": "now", "file": save_file}
                with patch.object(SessionState, "list_saved_sessions", return_value=[fake_entry]):
                    cmd_mod.handle_command("/load 1", st)
                self.assertEqual(st.project_root, p1)
                self.assertEqual(tools.get_project_root().resolve(), p1)

                # 4. /cd alias routes to project switch cleanly
                handled, _cont = cmd_mod.handle_command(f"/cd {p2}", st)
                self.assertTrue(handled)
                self.assertEqual(st.project_root, p2)

                # 5. Toolbar preserves project name on standard 80-col terminals
                st.selected_model = "claude"
                st.active_web_model = "Sonnet 5 High"
                st.active_account_label = "chrome"
                st.active_account_email = "developer@example.com"
                with patch.object(main_mod, "_prompt_rule_width", return_value=76):
                    bar = main_mod._build_bottom_toolbar(st)
                self.assertIn(p2.name[:20], bar)
        finally:
            tools.set_project_root(orig_root)

    def test_tools_ignore_profiles_secrets_gitignore_and_harden_io_limits(self):
        orig_root = tools.get_project_root()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                tools.set_project_root(root)

                # Create noise, secrets, profiles, gitignored artifacts, binaries, and real source
                (root / "chrome-profiles" / "pro1" / "Default" / "Cache" / "Cache_Data").mkdir(parents=True)
                (root / "chrome-profiles" / "pro1" / "Default" / "Cache" / "Cache_Data" / "data_0").write_text("secret_token\n", encoding="utf-8")
                (root / "wsa.egg-info").mkdir()
                (root / "wsa.egg-info" / "PKG-INFO").write_text("Name: wsa\nsecret_token\n", encoding="utf-8")
                (root / "accounts").mkdir()
                (root / "accounts" / "pro1.json").write_text('{"tok":"secret_token"}\n', encoding="utf-8")
                (root / "wsa-auth.json").write_text('{"key":"secret_token"}\n', encoding="utf-8")
                (root / ".wsa_relay_sessions.json").write_text('{"s":"secret_token"}\n', encoding="utf-8")
                (root / ".env.local").write_text("API_KEY=secret_token\n", encoding="utf-8")
                (root / ".gitignore").write_text("custom_build/\n*.gen.txt\n!keep.gen.txt\n", encoding="utf-8")
                (root / "custom_build").mkdir()
                (root / "custom_build" / "bundle.js").write_text("secret_token\n", encoding="utf-8")
                (root / "debug.log").write_text("secret_token\n", encoding="utf-8")
                (root / "ignored.gen.txt").write_text("secret_token\n", encoding="utf-8")
                (root / "keep.gen.txt").write_text("keep me\n", encoding="utf-8")
                (root / "src").mkdir()
                (root / "src" / "app.py").write_text("TOKEN = 'secret_token'\ndef hi():\n    return 'ok'\n", encoding="utf-8")
                (root / "src" / "styles.css").write_text("body { color: #111; }\n", encoding="utf-8")
                (root / "src" / "blob.dat").write_bytes(b"\x00\x01secret_token\x00")

                # 0. Dotfile/dotdir leading dots are preserved during path ignore checks
                self.assertTrue(tools._is_path_ignored(".git", True, root))
                self.assertTrue(tools._is_path_ignored(".env.local", False, root))
                self.assertTrue(tools._is_path_ignored(".wsa_relay_sessions.json", False, root))

                # 1. list_dir(recursive=True) omits profiles, egg-info, secrets, gitignored dirs/files while keeping negated & src files
                listing = tools.execute_tool("list_dir", {"path": ".", "recursive": True})
                self.assertIn("src/app.py", listing)
                self.assertIn("src/styles.css", listing)
                self.assertIn("keep.gen.txt", listing)
                self.assertNotIn("ignored.gen.txt", listing)
                self.assertNotIn("chrome-profiles", listing)
                self.assertNotIn("wsa.egg-info", listing)
                self.assertNotIn("accounts", listing)
                self.assertNotIn("wsa-auth.json", listing)
                self.assertNotIn(".wsa_relay_sessions.json", listing)
                self.assertNotIn(".env.local", listing)
                self.assertNotIn("custom_build", listing)
                self.assertNotIn("debug.log", listing)
                self.assertLessEqual(len(listing), 8_500)

                # 2. glob_files respects pruned directory traversal and ignore rules
                globs = tools.execute_tool("glob_files", {"pattern": "**/*"})
                self.assertIn("src/app.py", globs)
                self.assertNotIn("chrome-profiles", globs)
                self.assertNotIn("wsa.egg-info", globs)
                self.assertNotIn("wsa-auth.json", globs)
                self.assertNotIn("custom_build", globs)

                # 3. search_files ignores secret/binary/ignored files and honors path scoping + max_results
                s_res = tools.execute_tool("search_files", {"pattern": "secret_token"})
                self.assertIn("src/app.py:1:", s_res)
                self.assertNotIn("chrome-profiles", s_res)
                self.assertNotIn("wsa-auth.json", s_res)
                self.assertNotIn(".env.local", s_res)
                self.assertNotIn("blob.dat", s_res)
                self.assertNotIn("bundle.js", s_res)

                # 4. read_file handles start_line > total_lines gracefully
                eof_msg = tools.execute_tool("read_file", {"path": "src/app.py", "start_line": 99})
                self.assertIn("past EOF", eof_msg)

                # 5. patch_file preserves CRLF on Windows CRLF files and handles directory error cleanly
                crlf_path = root / "src" / "win.py"
                crlf_path.write_bytes(b"def f():\r\n    return 1\r\n")
                tools.execute_tool("read_file", {"path": "src/win.py"})
                patch_ok = tools.execute_tool(
                    "patch_file",
                    {"path": "src/win.py", "old_string": "    return 1", "new_string": "    return 2"},
                )
                self.assertIn("Patched", patch_ok)
                raw_after = crlf_path.read_bytes()
                self.assertIn(b"\r\n", raw_after)
                self.assertIn(b"return 2\r\n", raw_after)
                self.assertIn("Not a file", tools.execute_tool("patch_file", {"path": "src", "old_string": "a", "new_string": "b"}))

                # 6. _format_exec_output strips ANSI escape sequences cleanly
                clean_out = tools._format_exec_output(0, "\x1b[32m2 passed in 0.1s\x1b[0m\n", "")
                self.assertIn("2 passed in 0.1s", clean_out)
                self.assertNotIn("\x1b[", clean_out)
                self.assertIn("Exit code: 0", clean_out)
        finally:
            tools.set_project_root(orig_root)

    def test_claude_prompts_system_message_and_context_are_lean_and_project_scoped(self):
        from wsa_agent import context as ctx_mod, prompts as prompts_mod
        from wsa_server.multimodal import messages_to_delta_prompt, messages_to_prompt

        orig_root = tools.get_project_root()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "package.json").write_text('{"name":"chat-ui"}', encoding="utf-8")
                (root / "src").mkdir()
                tools.set_project_root(root)

                st = SessionState(project_root=root, selected_model="claude")
                st.active_account_label = "chrome"
                st.active_account_email = "private-user@example.com"
                sys_msg = agent_module._build_system_message(st)
                content = sys_msg["content"]

                self.assertLess(len(prompts_mod.CLAUDE_SYSTEM_PROMPT), 3000)
                self.assertLess(len(prompts_mod.CLAUDE_SYSTEM_PROMPT), len(prompts_mod.SYSTEM_PROMPT))
                self.assertIn("Complete end-to-end implementations:", prompts_mod.CLAUDE_SYSTEM_PROMPT)
                self.assertIn(f"Project: {root}", content)
                self.assertIn("Workspace root entries:", content)
                self.assertNotIn("private-user@example.com", content)
                self.assertNotIn("Active single account:", content)
                self.assertNotIn("Date/Time:", content)

                # Turn 0 prompt keeps human-like transparent framing + coding style while staying lean
                p0, _ = messages_to_prompt(
                    [sys_msg, {"role": "user", "content": "Redesign the UI and audit bugs"}],
                    tools=tools.TOOL_DEFINITIONS,
                    provider="claude",
                )
                self.assertIn("Hi Claude — here is what I need help with", p0)
                self.assertIn("Context on how we're collaborating here, what `wsa` is", p0)
                self.assertIn("To be completely transparent:", p0)
                self.assertIn("NOT an Anthropic built-in integration", p0)
                self.assertIn("Strictly an output-formatting & workflow-style preference", p0)
                self.assertIn("Why we're doing this and keeping one continuous conversation:", p0)
                self.assertIn("During multi-step builds in this same chat", p0)
                self.assertIn("tight, direct coding-agent", p0)
                self.assertIn("Please avoid calling Claude's built-in", p0)
                self.assertNotIn("for ranged...", p0)

                # Tool continuation footer keeps human CLI attribution and concise reminder
                delta_p, _ = messages_to_delta_prompt(
                    [{"role": "tool", "name": "list_dir", "content": "package.json  (20B)\nsrc/"}],
                    [{"role": "user", "content": "Redesign the UI"}, {"role": "tool", "name": "list_dir", "content": "package.json"}],
                    tools=tools.TOOL_DEFINITIONS,
                    provider="claude",
                )
                self.assertIn("Local output returned by my `wsa` CLI for `list_dir`:", delta_p)
                self.assertIn("Review the local `wsa` output above", delta_p)

                # Context verb regex detects both 'redesign' and 're design'
                self.assertIsNotNone(ctx_mod._BUILD_VERBS_RE.search("please re design the chat ui"))
                self.assertIsNotNone(ctx_mod._BUILD_VERBS_RE.search("redesign the ui components"))
        finally:
            tools.set_project_root(orig_root)

    def test_claude_fallback_prompt_sanitizes_transfer_headers_refusals_and_keeps_user_first(self):
        from wsa_agent import prompts as prompts_mod
        from wsa_server import multimodal

        orig_root = tools.get_project_root()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
                tools.set_project_root(root)

                st = SessionState(project_root=root, selected_model="claude")
                sys_msg = agent_module._build_system_message(st)
                digest_sys = {
                    "role": "system",
                    "content": (
                        "[Context Transfer Digest: 26 earlier messages (approx. 52,000 chars) compressed into structured memory below]\n"
                        "Completed Earlier Topics (Already Answered — Reference Only): audit layout\n"
                        "Key Prior Findings: app.py is tiny | I need to pause here rather than continue with another patch"
                    ),
                }
                convo = [
                    sys_msg,
                    digest_sys,
                    {"role": "user", "content": "audit layout"},
                    {"role": "assistant", "content": "Discarding that — real-tool slip again, nothing used. Retrying via the relay."},
                    {"role": "assistant", "content": "Inspected app.py entry point cleanly."},
                    {
                        "role": "tool",
                        "name": "read_file",
                        "content": "1 | print('hi')\n\n[System: Test files detected in this project. After all edits are complete, run: python -m pytest -x -q]",
                    },
                    {"role": "user", "content": "check tests next"},
                    {
                        "role": "assistant",
                        "content": (
                            "I don't actually have a `wsa` CLI link here and won't emit `tool_call` JSON blocks "
                            "when formatted like a system digest."
                        ),
                    },
                    {
                        "role": "user",
                        "content": "[Continue]: Your previous tool_call block was incomplete or used invalid fencing/JSON. Re-emit ONLY the valid ```tool_call JSON block(s).",
                    },
                    {
                        "role": "assistant",
                        "content": "There's no malformed block from me to fix, and I need to pause here rather than continue with another patch.",
                    },
                    {"role": "user", "content": "continue auditing the chat UI"},
                ]

                fp, _ = multimodal.messages_to_prompt(convo, tools=tools.TOOL_DEFINITIONS, provider="claude")
                self.assertTrue(fp.startswith("Hi Claude — I'm continuing my coding task"))
                self.assertLess(fp.index("continue auditing the chat UI"), fp.index("Context on how we're collaborating here"))
                self.assertIn("NOT an Anthropic built-in integration", fp)
                self.assertIn("To be completely transparent:", fp)
                self.assertIn("My engineering workflow preferences for working together on this codebase:", fp)
                self.assertIn("Carried-over working summary I pasted from my earlier chat session", fp)
                self.assertIn("app.py is tiny", fp)
                self.assertIn("(Note: Test files detected in this project", fp)
                self.assertIn("Inspected app.py entry point cleanly.", fp)

                for forbidden in (
                    "[Context Transfer Digest:",
                    "[Prior Assistant]",
                    "[Prior User",
                    "[System: Test files detected",
                    "real-tool slip",
                    "I need to pause here",
                    "won't emit",
                    "[Continue]: Your previous tool_call",
                    "check tests next",
                ):
                    self.assertNotIn(forbidden, fp)

                # Under tight PROMPT_MAX_BYTES, user-first opening and current request stay intact
                heavy_convo = list(convo) + [
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"app.py"}'}}]},
                    {"role": "tool", "name": "read_file", "content": "x" * 18000},
                ]
                with patch.object(multimodal, "PROMPT_MAX_BYTES", 14000):
                    trimmed, _ = multimodal.messages_to_prompt(heavy_convo, tools=tools.TOOL_DEFINITIONS, provider="claude")
                self.assertLessEqual(len(trimmed), 14000)
                self.assertTrue(trimmed.startswith("Hi Claude — I'm continuing my coding task"))
                self.assertIn("continue auditing the chat UI", trimmed)
                self.assertIn(prompts_mod.CLAUDE_SYSTEM_PROMPT.splitlines()[0], trimmed)
        finally:
            tools.set_project_root(orig_root)

    def test_compact_history_excludes_claude_workflow_refusals(self):
        st = SessionState()
        st.messages = [{"role": "system", "content": "sys"}]
        for i in range(6):
            st.messages.append({"role": "user", "content": f"step {i}"})
            if i == 2:
                st.messages.append({
                    "role": "assistant",
                    "content": "I need to pause here rather than continue with another patch — I won't emit `tool_call` JSON blocks.",
                })
                st.messages.append({
                    "role": "user",
                    "content": "[Continue]: Your previous tool_call block was incomplete or used invalid fencing/JSON.",
                })
            else:
                st.messages.append({"role": "assistant", "content": f"Completed step {i} verification cleanly."})

        st.compact_history(keep=4)
        self.assertNotIn("I need to pause here", st.context_summary)
        self.assertNotIn("[Continue]: Your previous tool_call", st.context_summary)
        self.assertIn("Completed step", st.context_summary)

    def test_run_turn_does_not_retry_parse_on_claude_workflow_refusal(self):
        from wsa_agent.session import SessionState

        class FakeRenderer:
            def start_thinking(self):
                pass
            def stop_thinking(self):
                pass
            def feed(self, token):
                pass
            def feed_thinking(self, token):
                pass
            def reset_answer(self, full_retry=False):
                pass
            def finish(self):
                pass
            def get_thinking_text(self):
                return ""

        class RefusalClient:
            def __init__(self):
                self.calls = 0
            def stream(self, messages, **kwargs):
                self.calls += 1
                return {
                    "content": (
                        "I need to pause here rather than continue with another patch: there is no "
                        "malformed block from me to fix, and I won't adopt the \"wsa relay\" framing or "
                        "emit `tool_call` JSON blocks."
                    )
                }

        warns = []
        state = SessionState(free_mode=False, selected_model="claude")
        client = RefusalClient()
        with patch.object(agent_module.R, "StreamRenderer", FakeRenderer), \
             patch.object(agent_module.R, "begin_agent_response"), \
             patch.object(agent_module.R, "print_warn", side_effect=lambda msg: warns.append(msg)), \
             patch.object(agent_module.R, "print_info"), \
             patch.object(agent_module.time, "sleep"), \
             patch.object(SessionState, "save"), \
             patch.object(agent_module, "set_project_root"):
            agent_module.run_turn("continue", state, client)

        self.assertEqual(client.calls, 1)
        self.assertTrue(any("declined" in w.lower() for w in warns))
        self.assertFalse(any("malformed" in w.lower() for w in warns))
        self.assertEqual(state._tool_parse_retries, 0)

    def test_dom_relay_poll_reports_stale_busy_req_id_invalid_over_http(self):
        from http.server import ThreadingHTTPServer
        import threading
        import urllib.request
        import wsa_server as srv
        from wsa_server.server import WSAHandler

        with srv._dom_relay_lock:
            orig_relays = dict(srv._dom_relay.get("relays") or {})
            orig_active = srv._dom_relay.get("active_relay_id")
            orig_pending = dict(srv._dom_relay.get("pending") or {})
            orig_cancelled = dict(srv._dom_relay.get("cancelled_req_ids") or {})
            srv._dom_relay["relays"] = {}
            srv._dom_relay["active_relay_id"] = None
            srv._dom_relay["pending"] = {
                "live-req-99": {
                    "prompt": "inspect src/app.py",
                    "fallback_prompt": "inspect src/app.py",
                    "images": [],
                    "session_id": "s-busy",
                    "provider": "claude",
                    "target_relay_id": "brave-test-1",
                    "explicit_relay_target": True,
                    "dispatched_to": "",
                    "response": None,
                    "thinking": "",
                    "model": "",
                    "chat_url": "",
                    "event": threading.Event(),
                    "wake_event": threading.Event(),
                    "error": None,
                    "cancelled": False,
                    "dispatched": False,
                    "new_chat": True,
                }
            }
            srv._dom_relay["cancelled_req_ids"] = {}

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), WSAHandler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            # 1) Orphaned busy_req_id from previous server returns X-Busy-Req-Valid=0 and body null
            url_stale = f"http://127.0.0.1:{port}/pending-prompt?client_id=brave-test-1&browser=brave&label=brave&busy=1&busy_req_id=dead-req-00&has_claude=1"
            with urllib.request.urlopen(url_stale, timeout=3) as resp:
                self.assertEqual(resp.headers.get("X-Busy-Req-Valid"), "0")
                self.assertIsNone(json.loads(resp.read().decode("utf-8")))

            # 2) Active in-flight busy_req_id returns X-Busy-Req-Valid=1
            url_live = f"http://127.0.0.1:{port}/pending-prompt?client_id=brave-test-1&browser=brave&label=brave&busy=1&busy_req_id=live-req-99&has_claude=1"
            with urllib.request.urlopen(url_live, timeout=3) as resp:
                self.assertEqual(resp.headers.get("X-Busy-Req-Valid"), "1")
                self.assertIsNone(json.loads(resp.read().decode("utf-8")))

            # 3) Once stale local job clears and polls idle (busy=0), pending prompt dispatches immediately
            url_idle = f"http://127.0.0.1:{port}/pending-prompt?client_id=brave-test-1&browser=brave&label=brave&has_claude=1"
            with urllib.request.urlopen(url_idle, timeout=3) as resp:
                self.assertEqual(resp.headers.get("X-Busy-Req-Valid"), "1")
                payload = json.loads(resp.read().decode("utf-8"))
            self.assertIsInstance(payload, dict)
            self.assertEqual(payload.get("id"), "live-req-99")
        finally:
            httpd.shutdown()
            httpd.server_close()
            with srv._dom_relay_lock:
                srv._dom_relay["relays"] = orig_relays
                srv._dom_relay["active_relay_id"] = orig_active
                srv._dom_relay["pending"] = orig_pending
                srv._dom_relay["cancelled_req_ids"] = orig_cancelled

    def test_claude_stopped_response_exits_cleanly_without_stalling_extension(self):
        import subprocess
        from wsa_server.relay import _clean_claude_thinking_text

        self.assertEqual(_clean_claude_thinking_text("You stopped Claude's response"), "")

        bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
        script = f"""
const assert = require("assert");
const vm = require("vm");
const bg = require({json.dumps(bg_path)});

(async () => {{
  class MockEl {{
    constructor(tag, attrs = {{}}, cls = "", text = "") {{
      this.nodeType = 1;
      this.tagName = String(tag).toUpperCase();
      this._attrs = Object.assign({{}}, attrs);
      this.className = cls;
      this.children = [];
      this.childNodes = text ? [{{ nodeType: 3, textContent: String(text) }}] : [];
      this.parentElement = null;
      this.disabled = false;
    }}
    get textContent() {{ return (this.childNodes || []).map((c) => c.textContent || "").join(""); }}
    set textContent(v) {{ this.children = []; this.childNodes = v ? [{{ nodeType: 3, textContent: String(v) }}] : []; }}
    get innerText() {{ return this.textContent; }}
    set innerText(v) {{ this.textContent = v; }}
    appendChild(ch) {{ ch.parentElement = this; this.children.push(ch); this.childNodes.push(ch); return ch; }}
    getAttribute(k) {{
      if (Object.prototype.hasOwnProperty.call(this._attrs, k)) return this._attrs[k];
      if (k === "class") return this.className || null;
      return null;
    }}
    contains(target) {{
      if (target === this) return true;
      return (this.children || []).some((c) => c === target || (c && typeof c.contains === "function" && c.contains(target)));
    }}
    matches(sel) {{
      return sel.split(",").some((part) => {{
        const p = part.trim();
        const am = p.match(/^\\[([^=\\]]+)='([^']*)'\\]$/);
        if (am) return this.getAttribute(am[1]) === am[2];
        if (p.startsWith(".")) return String(this.className || "").split(/\\s+/).includes(p.slice(1));
        return this.tagName.toLowerCase() === p.toLowerCase();
      }});
    }}
    querySelector(sel) {{ return this.querySelectorAll(sel)[0] || null; }}
    querySelectorAll(sel) {{
      const out = [];
      const walk = (n) => {{
        for (const c of n.children || []) {{
          if (c instanceof MockEl && c.matches(sel)) out.push(c);
          if (c instanceof MockEl) walk(c);
        }}
      }};
      walk(this);
      return out;
    }}
    closest(sel) {{ let c = this; while (c) {{ if (c.matches(sel)) return c; c = c.parentElement; }} return null; }}
    focus() {{}}
    click() {{}}
    dispatchEvent() {{ return true; }}
  }}
  const root = new MockEl("div");
  const ed = new MockEl("div", {{ "data-testid": "chat-input", contenteditable: "true" }});
  const send = new MockEl("button", {{ "data-testid": "send-button" }});
  root.appendChild(ed); root.appendChild(send);

  const asst = new MockEl("div", {{ "data-testid": "assistant-message" }});
  const stoppedNote = new MockEl("div", {{ "data-cds": "TurnStatus" }}, "", "You stopped Claude's response");
  asst.appendChild(stoppedNote);
  root.appendChild(asst);

  let fakeNow = 10000;
  const sb = {{
    Element: MockEl,
    Node: {{ ELEMENT_NODE: 1, TEXT_NODE: 3 }},
    Date: {{ now: () => fakeNow }},
    location: {{ href: "https://claude.ai/chat/stop-test", pathname: "/chat/stop-test" }},
    getComputedStyle: () => ({{ display: "block", visibility: "visible", opacity: "1" }}),
    getSelection: () => ({{ removeAllRanges: () => {{}}, addRange: () => {{}} }}),
    KeyboardEvent: function() {{}},
    InputEvent: function() {{}},
    Event: function() {{}},
    setTimeout,
    document: {{
      createRange: () => ({{ selectNodeContents: () => {{}} }}),
      execCommand: () => true,
      querySelector: (s) => root.querySelector(s),
      querySelectorAll: (s) => root.querySelectorAll(s),
      createElement: (t) => new MockEl(t),
    }},
  }};
  sb.window = sb;
  sb.globalThis = sb;

  vm.runInNewContext("(" + bg._claudeInstallPageRuntimeFn.toString() + ")()", sb);
  const sres = await vm.runInNewContext("(" + bg._claudeStartTurnFn.toString() + ")('stop-1', 'hello')", sb);
  assert.strictEqual(sres.ok, true);
  // Force sawActivity/stopped path on existing message element
  sb.__wsaClaudeRuntime.turns['stop-1'].beforeTurnCount = 0;
  fakeNow = 11000;
  const pres = vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('stop-1')", sb);
  assert.strictEqual(pres.done, false);
  assert.strictEqual(pres.error, "Claude response was stopped in browser");
  assert.strictEqual(sb.__wsaClaudeRuntime.turns['stop-1'], undefined);
  console.log("STOP_CLEANUP_OK");
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        self.assertIn("STOP_CLEANUP_OK", proc.stdout)

    def test_claude_policy_refusal_detected_filtered_and_resets_thread(self):
        from wsa_agent.session import SessionState
        from wsa_server import multimodal

        policy_refusal = (
            "I looked back through the carried-over session context, and I want to flag something before "
            "going further rather than just dropping another `tool_call` block.\n\n"
            "What `wsa` actually does is pull live session credentials out of an open `claude.ai` tab and "
            "sidesteps the real API's auth, billing, and rate limits — automated/unauthorized access to "
            "claude.ai that Anthropic's usage policies don't allow me to help build or extend.\n\n"
            "Given that, I can't keep helping debug or polish this project in service of keeping this relay running."
        )
        self.assertTrue(multimodal.is_claude_workflow_refusal(policy_refusal))

        msgs = [
            {"role": "user", "content": "redesign tui using unslop"},
            {"role": "assistant", "content": "Audited chat layout."},
            {"role": "user", "content": "continue your task"},
            {"role": "assistant", "content": policy_refusal},
            {"role": "user", "content": "continue your task"},
        ]
        filtered = multimodal._filter_claude_fallback_messages(msgs)
        self.assertEqual(len(filtered), 3)
        self.assertEqual([m["content"] for m in filtered], [
            "redesign tui using unslop",
            "Audited chat layout.",
            "continue your task",
        ])

        class FakeRenderer:
            def start_thinking(self):
                pass
            def stop_thinking(self):
                pass
            def feed(self, token):
                pass
            def feed_thinking(self, token):
                pass
            def reset_answer(self, full_retry=False):
                pass
            def finish(self):
                pass
            def get_thinking_text(self):
                return ""

        class TwoTurnClient:
            def __init__(self):
                self.calls = []
            def stream(self, messages, **kwargs):
                self.calls.append(dict(kwargs))
                if len(self.calls) == 1:
                    return {"content": policy_refusal}
                return {"content": "Ready to review your UI files cleanly."}

        warns = []
        state = SessionState(free_mode=False, selected_model="claude")
        state.chat_urls["claude"] = "https://claude.ai/chat/poisoned-123"
        client = TwoTurnClient()
        with patch.object(agent_module.R, "StreamRenderer", FakeRenderer), \
             patch.object(agent_module.R, "begin_agent_response"), \
             patch.object(agent_module.R, "print_warn", side_effect=lambda msg: warns.append(msg)), \
             patch.object(agent_module.R, "print_info"), \
             patch.object(agent_module.time, "sleep"), \
             patch.object(SessionState, "save"), \
             patch.object(agent_module, "set_project_root"):
            agent_module.run_turn("continue your task", state, client)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(state._tool_parse_retries, 0)
            self.assertNotIn("claude", state.chat_urls)
            self.assertTrue(getattr(state, "_force_new_chat_next", False))
            self.assertTrue(any("declined" in w.lower() and "/new" in w.lower() for w in warns))

            agent_module.run_turn("continue your task", state, client)
            self.assertEqual(len(client.calls), 2)
            self.assertTrue(client.calls[1].get("new_chat") is True)
            self.assertFalse(getattr(state, "_force_new_chat_next", False))

    def test_claude_fallback_prompt_budgets_multiturn_history_and_strips_slip_noise(self):
        from wsa_server import multimodal

        orig_root = tools.get_project_root()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "ChatWindow.tsx").write_text("export const ChatWindow = () => null;\n", encoding="utf-8")
                tools.set_project_root(root)

                st = SessionState(project_root=root, selected_model="claude")
                sys_msg = agent_module._build_system_message(st)
                tc_old = [{"id": "old1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"accounts.py"}'}}]
                tc_now = [{"id": "now1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"ChatWindow.tsx"}'}}]
                convo = [
                    sys_msg,
                    {"role": "user", "content": "old superseded list_dir task"},
                    {"role": "assistant", "content": "", "tool_calls": tc_old},
                    {"role": "tool", "name": "read_file", "tool_call_id": "old1", "content": "def get_account_pool():\n    return ['accounts.py']\n" + ("# old dump\n" * 250)},
                    {"role": "assistant", "content": "Done with old check."},
                    {"role": "user", "content": "earlier intermediate tweak"},
                    {"role": "assistant", "content": "Checked earlier tweak."},
                    {"role": "user", "content": "Inspect UncensoredChatUI and redesign it using unslop"},
                    {"role": "assistant", "content": "Apologies — stray tool invocation again, retrying.", "tool_calls": tc_now},
                    {"role": "tool", "name": "read_file", "tool_call_id": "now1", "content": "export const ChatWindow = () => <div/>;\n" + ("// line of tsx code\n" * 700)},
                    {"role": "assistant", "content": "Audited main ChatWindow layout component."},
                    {"role": "user", "content": "continue your task"},
                ]

                prompt, _ = multimodal.messages_to_prompt(convo, tools=tools.TOOL_DEFINITIONS, provider="claude")
                self.assertLess(len(prompt), 35_000)
                self.assertIn("Active background goal from my session: Inspect UncensoredChatUI", prompt)
                self.assertIn("Audited main ChatWindow layout component.", prompt)
                self.assertIn("earlier carried-over output trimmed cleanly", prompt)
                for forbidden in (
                    "old superseded list_dir task",
                    "get_account_pool",
                    "accounts.py",
                    "stray tool invocation",
                    "Chrome helper attached",
                ):
                    self.assertNotIn(forbidden, prompt)
        finally:
            tools.set_project_root(orig_root)

    def test_relay_account_email_switch_clears_chat_and_forces_new_chat(self):
        import wsa_server as srv
        from wsa_server import relay as relay_mod

        with srv._dom_relay_lock:
            snap = {
                "last_poll_ts": srv._dom_relay.get("last_poll_ts", 0.0),
                "active_relay_id": srv._dom_relay.get("active_relay_id"),
                "pinned_relay_label": srv._dom_relay.get("pinned_relay_label"),
                "explicit_relay_lock": srv._dom_relay.get("explicit_relay_lock", False),
                "relays": dict(srv._dom_relay.get("relays") or {}),
                "pending": dict(srv._dom_relay.get("pending") or {}),
                "sessions": dict(srv._dom_relay.get("sessions") or {}),
            }
            pending_meta_snap = dict(relay_mod._PENDING_SESSION_META)

        sid = "sess-email-switch"
        try:
            with patch.object(relay_mod, "_persist_relay_sessions_locked"):
                with srv._dom_relay_lock:
                    srv._dom_relay["relays"] = {}
                    srv._dom_relay["active_relay_id"] = None
                    srv._dom_relay["pinned_relay_label"] = None
                    srv._dom_relay["explicit_relay_lock"] = False
                    srv._dom_relay["sessions"] = {}
                    relay_mod._PENDING_SESSION_META.clear()

                relay_mod.register_or_touch_relay({
                    "client_id": "brave-sw-1",
                    "browser": "brave",
                    "label": "brave",
                    "email": "gem@example.com",
                    "providers": {
                        "claude": {"email": "acct1@example.com"},
                        "gemini": {"email": "gem@example.com"},
                    },
                })
                relay_mod.select_active_relay(target="brave", session_id=sid)

                tc = [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]
                relay_mod.record_session_relay_turn(
                    sid, "", tc, chat_url="https://claude.ai/chat/chat-acct1", relay_id="brave-sw-1", provider="claude"
                )
                msgs = [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "Inspect a.py"},
                    {"role": "assistant", "content": "", "tool_calls": tc},
                    {"role": "tool", "name": "read_file", "tool_call_id": "c1", "content": "ok"},
                ]
                tools_def = [{"type": "function", "function": {"name": "read_file", "description": "Read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
                _, _, _, same_new, _hint = relay_mod.prepare_session_relay_prompt(
                    sid, msgs, tools_def, "FULL_PROMPT", [], provider="claude"
                )
                self.assertFalse(same_new)

                # Account email changes on same browser relay_id -> clears stale session affinity and forces new chat
                relay_mod.register_or_touch_relay({
                    "client_id": "brave-sw-1",
                    "browser": "brave",
                    "label": "brave",
                    "email": "gem@example.com",
                    "providers": {
                        "claude": {"email": "acct2@example.com"},
                        "gemini": {"email": "gem@example.com"},
                    },
                })
                p_after, fb_after, _, changed_new, _hint = relay_mod.prepare_session_relay_prompt(
                    sid, msgs, tools_def, "FULL_PROMPT", [], provider="claude"
                )
                self.assertTrue(changed_new)
                self.assertEqual(p_after, "FULL_PROMPT")
                self.assertEqual(fb_after, "FULL_PROMPT")

                # Direct email-mismatch guard in prepare_session_relay_prompt when session stored older account_email
                relay_mod.record_session_relay_turn(
                    sid, "", tc, chat_url="https://claude.ai/chat/chat-acct2", relay_id="brave-sw-1", provider="claude"
                )
                with srv._dom_relay_lock:
                    srv._dom_relay["relays"]["brave-sw-1"]["providers"]["claude"]["account_email"] = "acct3@example.com"
                _, _, _, guard_new, _hint = relay_mod.prepare_session_relay_prompt(
                    sid, msgs, tools_def, "FULL_PROMPT", [], provider="claude"
                )
                self.assertTrue(guard_new)
        finally:
            with srv._dom_relay_lock:
                srv._dom_relay.update(snap)
                relay_mod._PENDING_SESSION_META.clear()
                relay_mod._PENDING_SESSION_META.update(pending_meta_snap)

    def test_cmd_new_and_new_keep_reset_thread_and_session(self):
        from wsa_agent.commands import handle_command

        st = SessionState(selected_model="claude")
        orig_sid = st.session_id
        st.messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        st.chat_urls = {"claude": "https://claude.ai/chat/abc"}
        st.role_threads = {"scout": "tid-1"}
        st.context_summary = "important architectural notes"

        with patch("wsa_agent.commands.R.print_success"), \
             patch("wsa_agent.commands.clear_session_memory") as mock_clear_mem:
            handled, should_continue = handle_command("/new keep", st)
            self.assertTrue(handled)
            self.assertTrue(should_continue)
            self.assertEqual(st.messages, [])
            self.assertEqual(st.chat_urls, {})
            self.assertEqual(st.role_threads, {})
            self.assertNotEqual(st.session_id, orig_sid)
            self.assertTrue(getattr(st, "_force_new_chat_next", False))
            self.assertEqual(st.context_summary, "important architectural notes")
            mock_clear_mem.assert_not_called()

            st.messages = [{"role": "user", "content": "next"}]
            st.chat_urls = {"claude": "https://claude.ai/chat/def"}
            st._force_new_chat_next = False
            handled2, should_continue2 = handle_command("/new", st)
            self.assertTrue(handled2)
            self.assertTrue(should_continue2)
            self.assertEqual(st.messages, [])
            self.assertEqual(st.chat_urls, {})
            self.assertEqual(st.context_summary, "")
            self.assertTrue(getattr(st, "_force_new_chat_next", False))
            mock_clear_mem.assert_called_once()



class TestSonnet55QoLUpgrades(unittest.TestCase):
    """Lightweight unit/integration regressions for all 8 debugging QoL upgrades."""

    def test_1_partial_output_on_timeout_and_7_utf8_console(self):
        orig_root = tools.get_project_root()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                tools.set_project_root(root)

                # Partial stdout + stderr captured before timeout kill
                py_res = tools.execute_tool(
                    "run_python",
                    {
                        "code": (
                            "import sys, time\n"
                            "print('FLASK_READY_LINE', flush=True)\n"
                            "print('WARN_BEFORE_HANG', file=sys.stderr, flush=True)\n"
                            "time.sleep(10)\n"
                        ),
                        "timeout": 1,
                    },
                )
                self.assertIn("timed out after 1s", py_res)
                self.assertIn("Partial output captured before timeout", py_res)
                self.assertIn("FLASK_READY_LINE", py_res)
                self.assertIn("WARN_BEFORE_HANG", py_res)

                # Explicit note when process prints nothing before timeout
                silent_res = tools.execute_tool(
                    "run_python",
                    {"code": "import time\ntime.sleep(10)\n", "timeout": 1},
                )
                self.assertIn("timed out after 1s", silent_res)
                self.assertIn("No stdout/stderr was captured before timeout", silent_res)

                # UTF-8 console output preserves em-dash, emoji, and Devanagari/Hinglish text
                utf8_cmd = (
                    f'"{sys.executable}" -c "print(\'Connected — TheDrummer 🚀 हिंग्लिश\')"'
                )
                cmd_res = tools.execute_tool("run_command", {"command": utf8_cmd, "timeout": 10})
                self.assertIn("Connected — TheDrummer 🚀 हिंग्लिश", cmd_res)
                self.assertNotIn("\ufffd", cmd_res)

                ps1 = (Path(__file__).resolve().parent.parent / "wsa.ps1").read_text(encoding="utf-8")
                bat = (Path(__file__).resolve().parent.parent / "wsa.bat").read_text(encoding="utf-8")
                self.assertIn("OutputEncoding", ps1)
                self.assertIn("PYTHONUTF8", ps1)
                self.assertIn("65001", bat)
                self.assertIn("PYTHONUTF8=1", bat)
        finally:
            tools.set_project_root(orig_root)

    def test_2_background_process_lifecycle_and_strict_wsa_ownership(self):
        orig_root = tools.get_project_root()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                root = Path(tmp)
                tools.set_project_root(root)

                self.assertIn("No background processes", tools.execute_tool("list_background", {}))

                start_cmd = (
                    f'& "{sys.executable}" -c "import time; print(\'BG_BOOT_OK\', flush=True); time.sleep(20)"'
                )
                res_start = tools.execute_tool(
                    "start_background",
                    {"command": start_cmd, "name": "worker-a", "wait_ms": 2000},
                )
                self.assertIn("[OK] Started background process 'worker-a'", res_start)
                self.assertIn("PID:", res_start)
                self.assertIn("STDOUT log:", res_start)
                self.assertIn("STDERR log:", res_start)
                self.assertIn("BG_BOOT_OK", res_start)

                res_list = tools.execute_tool("list_background", {"tail_lines": 5})
                self.assertIn("worker-a", res_list)
                self.assertIn("Status: running", res_list)

                # Must refuse stopping untracked external PIDs / names
                refused = tools.execute_tool("stop_background", {"name": "999999"})
                self.assertIn("NOT started by WSA", refused)
                refused_own = tools.execute_tool("stop_background", {"name": str(os.getpid())})
                self.assertIn("NOT started by WSA", refused_own)

                res_stop = tools.execute_tool("stop_background", {"name": "worker-a"})
                self.assertIn("[OK] Stopped WSA background process 'worker-a'", res_stop)
            finally:
                tools.cleanup_background_processes()
                tools.set_project_root(orig_root)

    def test_3_self_preservation_guard_specific_and_restart_service_port_scoped(self):
        orig_root = tools.get_project_root()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                root = Path(tmp)
                tools.set_project_root(root)

                # Blanket kills report exact matched rule and snippet
                g1 = tools._check_self_preservation_guard("Stop-Process -Name python -Force")
                self.assertIsNotNone(g1)
                self.assertIn("stop-process-python-name", g1)
                self.assertIn("Stop-Process -Name python", g1)

                g2 = tools._check_self_preservation_guard("taskkill /F /IM python.exe")
                self.assertIsNotNone(g2)
                self.assertIn("taskkill-image-python", g2)

                g3 = tools._check_self_preservation_guard("Get-Process python | Stop-Process")
                self.assertIsNotNone(g3)
                self.assertIn("get-process-python-pipeline-kill", g3)

                # Targeted non-WSA PID before '; python ...' MUST NOT trip blanket guard
                g_pid_ok = tools._check_self_preservation_guard(
                    "Stop-Process -Id 999999 -Force; python app.py"
                )
                self.assertIsNone(g_pid_ok)

                # Targeted kill against own PID IS blocked cleanly
                g_self_pid = tools._check_self_preservation_guard(f"Stop-Process -Id {os.getpid()} -Force")
                self.assertIsNotNone(g_self_pid)
                self.assertIn("protected-self-pid", g_self_pid)

                # restart_service refuses WSA bridge port 8081
                refused_8081 = tools.execute_tool("restart_service", {"port": 8081, "command": "echo hi"})
                self.assertIn("Self-Preservation Guard", refused_8081)

                # Find an ephemeral free port and verify port-scoped stop+restart on real TCP listener
                import socket as _sock
                with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as s:
                    s.bind(("127.0.0.1", 0))
                    free_port = s.getsockname()[1]

                srv_code = (
                    "import http.server, socketserver\n"
                    f"srv = socketserver.TCPServer(('127.0.0.1', {free_port}), http.server.BaseHTTPRequestHandler)\n"
                    "print('PORT_BOUND', flush=True)\n"
                    "srv.serve_forever()\n"
                )
                res1 = tools.execute_tool(
                    "restart_service",
                    {
                        "port": free_port,
                        "command": f'& "{sys.executable}" -c "{srv_code}"',
                        "wait_ms": 2500,
                    },
                )
                self.assertIn(f"started 'service-{free_port}'", res1)
                self.assertIn(f"Port {free_port} listening: yes", res1)

                # Restart again -> stops previous PID listening strictly on `free_port` cleanly
                res2 = tools.execute_tool("restart_service", {"port": free_port})
                self.assertIn(f"Stopped listener(s) scoped strictly to port {free_port}", res2)
            finally:
                tools.cleanup_background_processes()
                tools.set_project_root(orig_root)

    def test_4_exact_omitted_line_numbers_in_trimming_and_compaction(self):
        raw_lines = [f"item-{i:03d}: " + ("x" * 60) for i in range(1, 101)]
        clipped = tools.truncate_text_with_line_range("\n".join(raw_lines), max_chars=1800, label="STDOUT")
        self.assertIn("lines ", clipped)
        self.assertRegex(clipped, r"lines \d+-\d+ omitted \(\d+ lines, [\d,]+ chars\)")

        numbered_lines = [f"{i:4d} | code_line_{i} = {'v' * 55}" for i in range(10, 110)]
        clipped_file = tools.truncate_text_with_line_range(
            "\n".join(numbered_lines), max_chars=1800, label="Output"
        )
        self.assertRegex(clipped_file, r"file lines \d+-\d+ omitted \(\d+ lines, [\d,]+ chars\)")
        self.assertIn("total 7,809 chars", clipped_file)

        # Agent message compaction also reports exact omitted line numbers
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "tool", "name": "read_file", "content": "\n".join(numbered_lines)},
            {"role": "assistant", "content": "step 1 done"},
            {"role": "user", "content": "next"},
            {"role": "tool", "name": "read_file", "content": "\n".join(numbered_lines)},
            {"role": "user", "content": "latest"},
        ]
        compacted = agent_module._compact_messages_for_web_limit(msgs, keep_recent_tools=1, force=True)
        self.assertGreaterEqual(compacted, 1)
        self.assertIn("[Compacted", msgs[1]["content"])
        self.assertRegex(msgs[1]["content"], r"file lines \d+-\d+ omitted")

        # Excess 96KB pass also preserves exact omitted line ranges
        huge_lines = "\n".join(f"{i:4d} | payload_{i} = {'q' * 120}" for i in range(1, 900))
        msgs_over_budget = [
            {"role": "system", "content": "sys"},
            {"role": "tool", "name": "read_file", "content": huge_lines},
        ]
        compacted_big = agent_module._compact_messages_for_web_limit(msgs_over_budget, force=False)
        self.assertGreaterEqual(compacted_big, 1)
        self.assertIn("trimmed for 96KB limit:", msgs_over_budget[1]["content"])
        self.assertRegex(msgs_over_budget[1]["content"], r"file lines \d+-\d+ omitted")

        # Condenser marks omitted line gaps explicitly
        condensed = agent_module._condense_single_tool_output(
            "run_command",
            "\n".join([f"row {i}: " + ("z" * 40) for i in range(1, 60)] + ["Exit code: 1"]),
            max_chars=900,
        )
        self.assertIn("omitted (", condensed)
        self.assertIn("Exit code: 1", condensed)

    def test_5_run_tests_structured_summary_and_verification_gate(self):
        parsed_fail = tools._parse_test_output(
            "pytest",
            1,
            "FAILED tests/test_a.py::test_math - AssertionError: 1 != 2\n2 passed, 1 failed in 0.42s\n",
            "",
        )
        self.assertIn("Status: FAIL", parsed_fail)
        self.assertIn("Summary: 2 passed, 1 failed (0.42s)", parsed_fail)
        self.assertIn("Failing tests (1):", parsed_fail)
        self.assertIn("tests/test_a.py::test_math", parsed_fail)

        self.assertTrue(
            agent_module._is_verification_command(
                "run_tests", {}, "Status: PASS\nSummary: 5 passed\nExit code: 0"
            )
        )
        self.assertFalse(
            agent_module._is_verification_command(
                "run_tests", {}, "Status: NO TESTS\nSummary: 0 passed\nExit code: 5"
            )
        )

        orig_root = tools.get_project_root()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "test_mini.py").write_text(
                    "def test_ok():\n    assert 1 + 1 == 2\n\ndef test_bad():\n    assert 1 == 2\n",
                    encoding="utf-8",
                )
                tools.set_project_root(root)
                live_res = tools.execute_tool(
                    "run_tests", {"path": "test_mini.py", "framework": "pytest"}
                )
                self.assertIn("Status: FAIL", live_res)
                self.assertIn("1 passed", live_res)
                self.assertIn("1 failed", live_res)
                self.assertIn("test_mini.py::test_bad", live_res)
        finally:
            tools.set_project_root(orig_root)

    def test_6_batch_checkpoints_and_single_step_rollback(self):
        orig_root = tools.get_project_root()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "alpha.py").write_text("VAL = 1\n", encoding="utf-8")
                tools.set_project_root(root)

                state = SessionState(project_root=root, session_id="cp_batch_test", permission_mode="smart")

                class MockClient:
                    api_base = "http://127.0.0.1:8081/v1"
                    api_key = ""

                    def __init__(self):
                        self.turn = 0

                    def stream(self, messages, model, **kwargs):
                        self.turn += 1
                        if self.turn == 1:
                            # Multi-patch turn: alpha.py succeeds, beta.py created, missing.py fails
                            return {
                                "content": (
                                    '```tool_call\n{"name":"write_file","arguments":{"path":"alpha.py","content":"VAL = 999\\n"}}\n```\n'
                                    '```tool_call\n{"name":"write_file","arguments":{"path":"beta.py","content":"NEW = True\\n"}}\n```\n'
                                    '```tool_call\n{"name":"patch_file","arguments":{"path":"missing.py","old_string":"x","new_string":"y"}}\n```'
                                ),
                                "tool_calls": [],
                            }
                        return {"content": "Verified partial fail.", "tool_calls": []}

                agent_module.execute_agent_loop("batch edit", state, MockClient(), quiet_ui=True)
                self.assertEqual((root / "alpha.py").read_text(encoding="utf-8"), "VAL = 999\n")
                self.assertTrue((root / "beta.py").is_file())

                cp_list = tools.execute_tool("rollback", {"action": "list"})
                self.assertIn("cp-1", cp_list)
                self.assertIn("alpha.py", cp_list)
                self.assertIn("beta.py", cp_list)

                rb_res = tools.execute_tool("rollback", {})
                self.assertIn("[OK] Rolled back 1 checkpoint(s) (cp-1)", rb_res)
                self.assertEqual((root / "alpha.py").read_text(encoding="utf-8"), "VAL = 1\n")
                self.assertFalse((root / "beta.py").exists())
        finally:
            tools.set_project_root(orig_root)

    def test_8_http_request_localhost_restriction_and_confirm_before_run(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                if self.path == "/redirect-evil":
                    self.send_response(302)
                    self.send_header("Location", "https://example.com/steal")
                    self.end_headers()
                    return
                if self.path == "/bad":
                    body = b'{"error":"bad input"}'
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = b'{"ok":true,"msg":"hello"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                out = json.dumps({"echo": json.loads(raw)}).encode("utf-8")
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            port = srv.server_address[1]
            # Blocks external hosts, metadata IPs, and non-HTTP schemes
            ext = tools.execute_tool("http_request", {"url": "https://example.com/api"})
            self.assertIn("[Safety Error]", ext)
            meta = tools.execute_tool("http_request", {"url": "http://169.254.169.254/latest/meta-data"})
            self.assertIn("[Safety Error]", meta)
            scheme = tools.execute_tool("http_request", {"url": "file:///C:/Windows/win.ini"})
            self.assertIn("[Safety Error]", scheme)

            # Blocks open redirect from localhost -> external host
            redir = tools.execute_tool("http_request", {"url": f"http://127.0.0.1:{port}/redirect-evil"})
            self.assertIn("[Safety Error]", redir)

            # Localhost GET, POST, and 400 error bodies succeed cleanly
            ok_get = tools.execute_tool("http_request", {"url": f"http://127.0.0.1:{port}/ok"})
            self.assertIn("Status: 200", ok_get)
            self.assertIn('"msg": "hello"', ok_get)

            ok_post = tools.execute_tool(
                "http_request",
                {"url": f"http://localhost:{port}/items", "method": "POST", "json_body": {"x": 42}},
            )
            self.assertIn("Status: 201", ok_post)
            self.assertIn('"x": 42', ok_post)

            err_400 = tools.execute_tool("http_request", {"url": f"http://127.0.0.1:{port}/bad"})
            self.assertIn("Status: 400", err_400)
            self.assertIn('"error": "bad input"', err_400)

            # Confirm-before-run stays intact in smart/strict modes
            orig_root = tools.get_project_root()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "wsa_agent").mkdir()
                    (root / "wsa_agent" / "x.py").write_text("A = 1\n", encoding="utf-8")
                    tools.set_project_root(root)
                    tools.execute_tool("write_file", {"path": "wsa_agent/x.py", "content": "A = 2\n"})
                    is_ll, reason = tools.is_low_level_change("rollback", {})
                    self.assertTrue(is_ll)
                    self.assertIn("wsa_agent/x.py", reason)

                    # Rejecting interactive confirmation in smart mode blocks restart_service
                    st_confirm = SessionState(
                        project_root=root,
                        session_id="confirm_test",
                        permission_mode="smart",
                        auto_approve=False,
                    )

                    class _ConfirmClient:
                        api_base = "http://127.0.0.1:8081/v1"
                        api_key = ""

                        def __init__(self):
                            self.turn = 0

                        def stream(self, messages, model, **kwargs):
                            self.turn += 1
                            if self.turn == 1:
                                return {
                                    "content": '```tool_call\n{"name":"restart_service","arguments":{"port":5999}}\n```',
                                    "tool_calls": [],
                                }
                            return {"content": "Aborted as requested.", "tool_calls": []}

                    with patch.object(sys.stdin, "isatty", return_value=True), \
                         patch.object(renderer.console, "input", return_value="n"), \
                         patch.object(renderer.console, "print"), \
                         patch.object(tools, "_restart_service") as mock_restart:
                        agent_module.execute_agent_loop("restart port", st_confirm, _ConfirmClient(), quiet_ui=False)
                        mock_restart.assert_not_called()
                        denied = [m for m in st_confirm.messages if m.get("role") == "tool" and "[Permission denied]" in str(m.get("content"))]
                        self.assertTrue(denied)
            finally:
                tools.set_project_root(orig_root)
        finally:
            srv.shutdown()
            srv.server_close()


class TestMultiProviderAndMultiagentFailover(unittest.TestCase):
    """End-to-end tests for DeepSeek/ChatGPT live-observed DOMs, 4-site routing, and /multiagent failover."""

    def test_deepseek_all_four_modes_citations_react_input_and_sibling_actions_in_node_vm(self):
        import subprocess

        bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
        script = f"""
const assert = require("assert");
const vm = require("vm");
const bg = require({json.dumps(bg_path)});

(async () => {{
  class MockEl {{
    constructor(tag, attrs = {{}}, cls = "", text = "", styleObj = {{}}) {{
      this.nodeType = 1;
      this.tagName = String(tag).toUpperCase();
      this._attrs = Object.assign({{}}, attrs);
      this.className = cls;
      this.style = Object.assign({{}}, styleObj);
      this.children = [];
      this.childNodes = text ? [{{ nodeType: 3, textContent: String(text) }}] : [];
      this.parentElement = null;
      this.disabled = false;
      this._value = "";
      this._events = [];
      this._trackerVal = "";
      this._valueTracker = {{ setValue: (v) => {{ this._trackerVal = v; }}, getValue: () => this._trackerVal }};
    }}
    get value() {{ return this._value; }}
    set value(v) {{ this._value = String(v); }}
    get textContent() {{ return (this.childNodes || []).map((c) => c.textContent || "").join(""); }}
    set textContent(v) {{ this.children = []; this.childNodes = v ? [{{ nodeType: 3, textContent: String(v) }}] : []; }}
    get innerText() {{ return this.textContent; }}
    set innerText(v) {{ this.textContent = v; }}
    get nextElementSibling() {{
      if (!this.parentElement) return null;
      const idx = this.parentElement.children.indexOf(this);
      return idx >= 0 ? (this.parentElement.children[idx + 1] || null) : null;
    }}
    getAttribute(k) {{
      if (Object.prototype.hasOwnProperty.call(this._attrs, k)) return this._attrs[k];
      if (k === "class") return this.className || null;
      return null;
    }}
    contains(target) {{
      if (target === this) return true;
      return (this.children || []).some((c) => c === target || (c && typeof c.contains === "function" && c.contains(target)));
    }}
    contains(target) {{
      if (target === this) return true;
      return (this.children || []).some((c) => c === target || (c && typeof c.contains === "function" && c.contains(target)));
    }}
    appendChild(ch) {{ ch.parentElement = this; this.children.push(ch); this.childNodes.push(ch); return ch; }}
    matches(sel) {{
      return sel.split(",").some((rawPart) => {{
        const part = rawPart.trim();
        if (!part) return false;
        const segments = part.split(/\\s+/);
        const matchOne = (el, seg) => {{
          const s = seg.trim();
          const hasAllAttrs = [...s.matchAll(/\\[([^=\\]]+)(?:='([^']*)')?(?:\\s+i)?\\]/g)];
          const stripped = s.replace(/\\[[^\\]]+\\]/g, "");
          let ok = true;
          const tagMatch = stripped.match(/^[a-zA-Z0-9_-]+/);
          if (tagMatch && el.tagName.toLowerCase() !== tagMatch[0].toLowerCase()) ok = false;
          for (const cm of stripped.matchAll(/\\.([a-zA-Z0-9_-]+)/g)) {{
            const toks = String(el.className || "").split(/\\s+/);
            if (!toks.includes(cm[1])) ok = false;
          }}
          for (const am of hasAllAttrs) {{
            const val = el.getAttribute(am[1]);
            if (am[2] === undefined) {{ if (val === null) ok = false; }}
            else if (val !== am[2]) ok = false;
          }}
          return ok;
        }};
        if (segments.length === 1) return matchOne(this, segments[0]);
        if (!matchOne(this, segments[segments.length - 1])) return false;
        let cur = this.parentElement;
        for (let i = segments.length - 2; i >= 0; i--) {{
          while (cur && !matchOne(cur, segments[i])) cur = cur.parentElement;
          if (!cur) return false;
          cur = cur.parentElement;
        }}
        return true;
      }});
    }}
    querySelector(sel) {{ return this.querySelectorAll(sel)[0] || null; }}
    querySelectorAll(sel) {{
      const out = [];
      const walk = (n) => {{
        for (const c of n.children || []) {{
          if (c instanceof MockEl && c.matches(sel)) out.push(c);
          if (c instanceof MockEl) walk(c);
        }}
      }};
      walk(this);
      return out;
    }}
    closest(sel) {{ let c = this; while (c) {{ if (c.matches(sel)) return c; c = c.parentElement; }} return null; }}
    focus() {{}}
    click() {{ this.clicked = (this.clicked || 0) + 1; }}
    dispatchEvent(ev) {{ this._events.push(ev.type || "event"); return true; }}
  }}

  const root = new MockEl("div");
  const composer = new MockEl("div", {{}}, "_77cefa5 _3d616d3");
  const ta = new MockEl("textarea", {{ role: "textbox", "aria-label": "Message DeepSeek" }}, "ds-scroll-area d96f2d2a");
  const dtBtn = new MockEl("div", {{}}, "f79352dc ds-toggle-button ds-toggle-button--m", "DeepThink");
  const searchBtn = new MockEl("div", {{}}, "f79352dc ds-toggle-button ds-toggle-button--m", "Smart Search");
  const sendCircle = new MockEl("div", {{ role: "button" }}, "ds-button ds-button--primary ds-button--filled ds-button--circle _52c986b ds-button--disabled");
  composer.appendChild(ta); composer.appendChild(dtBtn); composer.appendChild(searchBtn); composer.appendChild(sendCircle);
  root.appendChild(composer);

  // User turn (must NOT be misread as assistant)
  const vList = new MockEl("div", {{}}, "ds-virtual-list-visible-items");
  const uMsg = new MockEl("div", {{}}, "d29f3d7d ds-message _63c77b1");
  uMsg.appendChild(new MockEl("div", {{}}, "fbb737a4 ds-collapsible-text", "hi"));
  vList.appendChild(uMsg);

  // Turn 1: completed V3 with sibling 6-button action bar AFTER .ds-message
  const a1 = new MockEl("div", {{}}, "ds-message _63c77b1");
  a1.appendChild(new MockEl("div", {{}}, "ds-markdown ds-assistant-message-main-content", "OK 4"));
  const a1Bar = new MockEl("div", {{}}, "action-bar-row");
  a1Bar.appendChild(new MockEl("button", {{ "aria-label": "Read aloud" }}, "ds-button ds-button--iconLabelTertiary ds-icon-button db183363"));
  vList.appendChild(a1);
  vList.appendChild(a1Bar);
  root.appendChild(vList);

  let fakeNow = 20000;
  const sb = {{
    Element: MockEl,
    Node: {{ ELEMENT_NODE: 1, TEXT_NODE: 3 }},
    Date: {{ now: () => fakeNow }},
    location: {{ href: "https://chat.deepseek.com/a/chat/s/ds-1", pathname: "/a/chat/s/ds-1" }},
    getComputedStyle: () => ({{ display: "block", visibility: "visible", opacity: "1", backgroundColor: "", borderColor: "", color: "" }}),
    getSelection: () => ({{ removeAllRanges: () => {{}}, addRange: () => {{}} }}),
    InputEvent: function(t) {{ this.type = t; }},
    Event: function(t) {{ this.type = t; }},
    KeyboardEvent: function(t) {{ this.type = t; }},
    setTimeout,
    document: {{
      createRange: () => ({{ selectNodeContents: () => {{}} }}),
      execCommand: () => true,
      querySelector: (s) => root.querySelector(s),
      querySelectorAll: (s) => root.querySelectorAll(s),
      createElement: (t) => new MockEl(t),
    }},
  }};
  sb.window = sb; sb.globalThis = sb;

  vm.runInNewContext("(" + bg._installWsaDomUtilsFn.toString() + ")()", sb);
  vm.runInNewContext("(" + bg._deepseekInstallPageRuntimeFn.toString() + ")()", sb);
  assert.strictEqual(sb.__wsaDomUtils.version, 3);
  assert.strictEqual(sb.__wsaDeepseekRuntime.version, 3);

  // 1. All 4 toggle combinations on div.ds-toggle-button
  assert.strictEqual(sb.__wsaDeepseekRuntime.readModelBadge(), "DeepSeek-V3");
  dtBtn.className += " ds-toggle-button--selected";
  assert.strictEqual(sb.__wsaDeepseekRuntime.readModelBadge(), "DeepSeek-R1 · DeepThink");
  searchBtn.className += " ds-toggle-button--selected";
  assert.strictEqual(sb.__wsaDeepseekRuntime.readModelBadge(), "DeepSeek-R1 · DeepThink + Search");
  dtBtn.className = "f79352dc ds-toggle-button ds-toggle-button--m";
  assert.strictEqual(sb.__wsaDeepseekRuntime.readModelBadge(), "DeepSeek-V3 · Search");

  // 2. React _valueTracker reset + Send click + active circle Stop detection when empty
  sendCircle.className = "ds-button ds-button--primary ds-button--filled ds-button--circle _52c986b";
  const st = await vm.runInNewContext("(" + bg._deepseekStartTurnFn.toString() + ")('ds-req', 'Check Python')", sb);
  assert.strictEqual(st.ok, true);
  assert.strictEqual(ta.value, "Check Python");
  assert.notStrictEqual(ta._trackerVal, "Check Python"); // tracker reset ran
  assert(ta._events.includes("input"));
  assert(sendCircle.clicked >= 1);

  // Simulate post-submit: textarea cleared, circle primary button stays enabled => Stop
  ta.value = "";
  assert.strictEqual(sb.__wsaDeepseekRuntime.hasStopButton(), true);

  // 3. Combo D live: Turn 2 has _74c0879 (DeepThink + Search banner + Read 2 pages), .ds-markdown-cite with hidden '-', bottom pill
  const a2 = new MockEl("div", {{}}, "ds-message _63c77b1");
  const wrap74 = new MockEl("div", {{}}, "_74c0879");
  wrap74.appendChild(new MockEl("div", {{}}, "_245c867", "Thought for 1 second"));
  const thinkDiv = new MockEl("div", {{}}, "ds-think-content");
  thinkDiv.appendChild(new MockEl("div", {{}}, "ds-markdown", "Need Python and Node LTS."));
  wrap74.appendChild(thinkDiv);
  wrap74.appendChild(new MockEl("div", {{}}, "_60aa7fb", "16 web pages mil gaye."));
  a2.appendChild(wrap74);

  const mainMd = new MockEl("div", {{}}, "ds-markdown ds-assistant-message-main-content");
  const pAns = new MockEl("p");
  pAns.appendChild(new MockEl("span", {{}}, "", "Python 3.14.3"));
  const citeLink = new MockEl("a", {{ href: "https://python.org" }});
  const citeSpan = new MockEl("span", {{}}, "ds-markdown-cite");
  citeSpan.appendChild(new MockEl("span", {{}}, "", "-", {{ opacity: "0" }}));
  citeSpan.appendChild(new MockEl("span", {{}}, "", "6"));
  citeLink.appendChild(citeSpan);
  pAns.appendChild(citeLink);
  mainMd.appendChild(pAns);
  a2.appendChild(mainMd);
  a2.appendChild(new MockEl("div", {{}}, "f93f59e4", "16 web pages"));
  vList.appendChild(a2);

  // While streaming (NO sibling action bar on a2 yet), hasActionBar MUST NOT bleed from a1Bar!
  let turns = sb.__wsaDeepseekRuntime.readAllTurns();
  assert.strictEqual(turns.length, 2);
  assert.strictEqual(turns[1].hasActionBar, false);
  assert(turns[1].thinking.includes("Need Python and Node LTS."));
  assert(!turns[1].thinking.includes("16 web pages mil gaye"));
  assert.strictEqual(turns[1].text, "Python 3.14.3[6](https://python.org)");
  assert(!turns[1].text.includes("-6"));
  assert(!turns[1].text.includes("16 web pages"));

  // Poll intermediate frame stays open while Stop is visible
  const pMid = vm.runInNewContext("(" + bg._deepseekPollTurnFn.toString() + ")('ds-req')", sb);
  assert.strictEqual(pMid.done, false);

  // Stop turns disabled (inactive) AND a2's own sibling action bar renders
  sendCircle.className += " ds-button--disabled";
  const a2Bar = new MockEl("div", {{}}, "action-bar-row");
  a2Bar.appendChild(new MockEl("button", {{ "aria-label": "Read aloud" }}, "ds-button ds-button--iconLabelTertiary db183363"));
  vList.appendChild(a2Bar);
  fakeNow = 20600;
  const pDone = vm.runInNewContext("(" + bg._deepseekPollTurnFn.toString() + ")('ds-req')", sb);
  assert.strictEqual(pDone.done, true);
  assert.strictEqual(pDone.text, "Python 3.14.3[6](https://python.org)");
  assert(pDone.thinking.includes("Need Python and Node LTS."));
  console.log("DEEPSEEK_VM_ALL_OK");
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        self.assertIn("DEEPSEEK_VM_ALL_OK", proc.stdout)

    def test_chatgpt_think_web_search_pill_preservation_and_citations_in_node_vm(self):
        import subprocess

        bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
        script = f"""
const assert = require("assert");
const vm = require("vm");
const bg = require({json.dumps(bg_path)});

(async () => {{
  class MockEl {{
    constructor(tag, attrs = {{}}, cls = "", text = "") {{
      this.nodeType = 1;
      this.tagName = String(tag).toUpperCase();
      this._attrs = Object.assign({{}}, attrs);
      this.className = cls;
      this.style = {{}};
      this.children = [];
      this.childNodes = text ? [{{ nodeType: 3, textContent: String(text) }}] : [];
      this.parentElement = null;
      this.parentNode = null;
      this.disabled = false;
    }}
    get textContent() {{ return (this.childNodes || []).map((c) => c.textContent || "").join(""); }}
    set textContent(v) {{ this.children = []; this.childNodes = v ? [{{ nodeType: 3, textContent: String(v) }}] : []; }}
    get innerText() {{ return this.textContent; }}
    get nextSibling() {{
      if (!this.parentElement) return null;
      const idx = this.parentElement.childNodes.indexOf(this);
      return idx >= 0 ? (this.parentElement.childNodes[idx + 1] || null) : null;
    }}
    getAttribute(k) {{
      if (Object.prototype.hasOwnProperty.call(this._attrs, k)) return this._attrs[k];
      if (k === "class") return this.className || null;
      return null;
    }}
    contains(target) {{
      if (target === this) return true;
      return (this.children || []).some((c) => c === target || (c && typeof c.contains === "function" && c.contains(target)));
    }}
    appendChild(ch) {{
      if (ch.nodeType === 1) {{ ch.parentElement = this; ch.parentNode = this; this.children.push(ch); }}
      else {{ ch.parentNode = this; }}
      this.childNodes.push(ch);
      return ch;
    }}
    removeChild(ch) {{
      this.children = this.children.filter(c => c !== ch);
      this.childNodes = this.childNodes.filter(c => c !== ch);
    }}
    replaceChildren(...nodes) {{ this.children = []; this.childNodes = []; for (const n of nodes) this.appendChild(n); }}
    matches(sel) {{
      return sel.split(",").some((rawPart) => {{
        const part = rawPart.trim();
        if (!part) return false;
        const segments = part.match(/(?:[^\\s\\[]+|\\[[^\\]]*\\])+/g) || [part];
        const matchOne = (el, seg) => {{
          let s = seg.trim();
          for (const am of s.matchAll(/\\[([a-zA-Z0-9_-]+)(?:([*^$]?=)['"]([^'"]*)['"])?(?:\\s+i)?\\]/g)) {{
            const val = el.getAttribute(am[1]);
            if (!am[2]) {{ if (val === null) return false; }}
            else if (am[2] === "=") {{ if (val !== am[3]) return false; }}
            else if (am[2] === "*=") {{ if (!String(val || "").toLowerCase().includes(am[3].toLowerCase())) return false; }}
            else if (am[2] === "^=") {{ if (!String(val || "").startsWith(am[3])) return false; }}
          }}
          s = s.replace(/\\[[^\\]]+\\]/g, "");
          for (const im of s.matchAll(/#([a-zA-Z0-9_-]+)/g)) {{
            if (el.getAttribute("id") !== im[1]) return false;
          }}
          s = s.replace(/#[a-zA-Z0-9_-]+/g, "");
          const tm = s.match(/^[a-zA-Z0-9_-]+/);
          if (tm && el.tagName.toLowerCase() !== tm[0].toLowerCase()) return false;
          for (const cm of s.matchAll(/\\.([a-zA-Z0-9_-]+)/g)) {{
            if (!String(el.className || "").split(/\\s+/).includes(cm[1])) return false;
          }}
          return true;
        }};
        if (segments.length === 1) return matchOne(this, segments[0]);
        if (!matchOne(this, segments[segments.length - 1])) return false;
        let cur = this.parentElement;
        for (let i = segments.length - 2; i >= 0; i--) {{
          while (cur && !matchOne(cur, segments[i])) cur = cur.parentElement;
          if (!cur) return false;
          cur = cur.parentElement;
        }}
        return true;
      }});
    }}
    querySelector(sel) {{ return this.querySelectorAll(sel)[0] || null; }}
    querySelectorAll(sel) {{
      const out = [];
      const walk = (n) => {{
        for (const c of n.children || []) {{
          if (c instanceof MockEl && c.matches(sel)) out.push(c);
          if (c instanceof MockEl) walk(c);
        }}
      }};
      walk(this);
      return out;
    }}
    closest(sel) {{ let c = this; while (c) {{ if (c.matches(sel)) return c; c = c.parentElement; }} return null; }}
    focus() {{}}
    click() {{ this.clicked = (this.clicked || 0) + 1; }}
    dispatchEvent() {{ return true; }}
  }}

  const root = new MockEl("div");
  const profileBtn = new MockEl("button", {{ "aria-label": "Open profile menu" }}, "", "User Go");
  const ed = new MockEl("div", {{ contenteditable: "true", role: "textbox", "aria-label": "Ask ChatGPT" }}, "ProseMirror");
  const pillP = new MockEl("p");
  const searchPill = new MockEl("span", {{ "data-layout": "selection-pill", "data-tone": "composer-reference" }}, "Mention-hIgCkC", "Web search");
  pillP.appendChild(searchPill);
  pillP.appendChild({{ nodeType: 3, textContent: " old" }});
  ed.appendChild(pillP);
  const sendBtn = new MockEl("button", {{ "aria-label": "Send" }}, "bg-composer-primary");
  const stopBtn = new MockEl("button", {{ "aria-label": "Stop" }}, "");
  stopBtn.disabled = true;
  root.appendChild(profileBtn); root.appendChild(ed); root.appendChild(sendBtn); root.appendChild(stopBtn);

  let fakeNow = 30000;
  const sb = {{
    Element: MockEl,
    Node: {{ ELEMENT_NODE: 1, TEXT_NODE: 3 }},
    Date: {{ now: () => fakeNow }},
    location: {{ href: "https://chatgpt.com/c/cg-1", pathname: "/c/cg-1" }},
    getComputedStyle: () => ({{ display: "block", visibility: "visible", opacity: "1" }}),
    getSelection: () => ({{ removeAllRanges: () => {{}}, addRange: () => {{}} }}),
    InputEvent: function(t) {{ this.type = t; }},
    Event: function(t) {{ this.type = t; }},
    KeyboardEvent: function(t) {{ this.type = t; }},
    setTimeout,
    document: {{
      createRange: () => ({{ selectNodeContents: () => {{}}, setStartAfter: () => {{}}, setEnd: () => {{}} }}),
      execCommand: () => false, // force fallback path to prove inline Web search pill preservation!
      querySelector: (s) => root.querySelector(s),
      querySelectorAll: (s) => root.querySelectorAll(s),
      createElement: (t) => new MockEl(t),
      createTextNode: (s) => ({{ nodeType: 3, textContent: String(s) }}),
    }},
  }};
  sb.window = sb; sb.globalThis = sb;

  vm.runInNewContext("(" + bg._installWsaDomUtilsFn.toString() + ")()", sb);
  vm.runInNewContext("(" + bg._chatgptInstallPageRuntimeFn.toString() + ")()", sb);

  // Verify badge sees Web search chip & preserves chip when inserting prompt
  assert.strictEqual(sb.__wsaChatgptRuntime.readModelBadge(), "ChatGPT (Go) · Web search");
  const sres = await vm.runInNewContext("(" + bg._chatgptStartTurnFn.toString() + ")('cg-req', 'Python release?')", sb);
  assert.strictEqual(sres.ok, true);
  assert(ed.querySelector("[data-layout='selection-pill']"), "Web search pill must remain intact");
  assert(ed.textContent.includes("Python release?"));

  // User turn (ignored) + streaming Think/Search shimmer phase on gpt-6-t-mini
  const conv = new MockEl("div", {{ role: "region", "aria-label": "Conversation" }});
  const uTurn = new MockEl("div", {{ "data-content-search-turn-key": "u1" }});
  uTurn.appendChild(new MockEl("h4", {{}}, "sr-only", "You said:"));
  uTurn.appendChild(new MockEl("p", {{}}, "Paragraph-kKnbIo", "Python release?"));
  conv.appendChild(uTurn);

  const aTurn = new MockEl("div", {{ "data-content-search-turn-key": "a1" }});
  aTurn.appendChild(new MockEl("h4", {{}}, "sr-only", "ChatGPT said:"));
  const thinkBtn = new MockEl("button", {{}}, "", "Analyzed");
  const shimmer = new MockEl("span", {{}}, "cadencedShimmer-uMTG1d", "Searching the web");
  aTurn.appendChild(thinkBtn);
  aTurn.appendChild(shimmer);
  conv.appendChild(aTurn);
  root.appendChild(conv);
  stopBtn.disabled = false;

  const pStream = vm.runInNewContext("(" + bg._chatgptPollTurnFn.toString() + ")('cg-req')", sb);
  assert.strictEqual(pStream.done, false);
  assert(pStream.thinking.includes("Searching the web"));

  // Now gpt-6-t-mini hide_all recap detaches shimmer/Analyzed nodes and renders DilResponseRoot + citation Trigger pill + .turn-action-controls
  aTurn.removeChild(thinkBtn);
  aTurn.removeChild(shimmer);
  const dil = new MockEl("div", {{}}, "DilRenderer-temL23 DilResponseRoot-fdCmoC");
  const ansP = new MockEl("p", {{}}, "TextBase-kDAqPL");
  ansP.appendChild(new MockEl("span", {{}}, "", "Python 3.14.3 is latest."));
  ansP.appendChild(new MockEl("span", {{ "data-d-inline-text": "", role: "button", "aria-haspopup": "dialog" }}, "Trigger-y4ejT1", "Python.org +1"));
  dil.appendChild(ansP);
  aTurn.appendChild(dil);
  const actions = new MockEl("div", {{}}, "turn-action-controls");
  actions.appendChild(new MockEl("button", {{ "aria-label": "Rate response" }}));
  aTurn.appendChild(actions);
  stopBtn.disabled = true;

  fakeNow = 30600;
  const pArrive = vm.runInNewContext("(" + bg._chatgptPollTurnFn.toString() + ")('cg-req')", sb);
  assert.strictEqual(pArrive.done, false);
  fakeNow = 31100;
  const pFinal = vm.runInNewContext("(" + bg._chatgptPollTurnFn.toString() + ")('cg-req')", sb);
  assert.strictEqual(pFinal.done, true);
  assert.strictEqual(pFinal.text, "Python 3.14.3 is latest.");
  assert(!pFinal.text.includes("Python.org"), "Inline citation chip must be stripped");
  assert(pFinal.thinking.includes("Searching the web"), "Monotonic lastThinking must survive hide_all recap detach");
  console.log("CHATGPT_VM_ALL_OK");
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        self.assertIn("CHATGPT_VM_ALL_OK", proc.stdout)

    def test_server_four_providers_and_limit_error_http_429(self):
        from wsa_server import models, relay, server

        catalog_ids = list(models.MODELS.keys())
        for expected in ("claude", "deepseek", "gemini", "chatgpt"):
            self.assertIn(expected, catalog_ids)
        resolver = server.WSAHandler._resolve_model
        dummy = object()
        self.assertEqual(resolver(dummy, "chatgpt")[1]["provider"], "chatgpt")
        self.assertEqual(resolver(dummy, "gpt-6")[1]["provider"], "chatgpt")
        self.assertEqual(resolver(dummy, "deepseek")[1]["provider"], "deepseek")
        self.assertEqual(resolver(dummy, "deepseek-r1")[1]["provider"], "deepseek")

        lim_exc = relay.ProviderLimitError("[LIMIT_REACHED] Gemini: You've reached your usage cap", provider="gemini")
        status, payload = server._build_upstream_error_payload(lim_exc, provider="gemini")
        self.assertEqual(status, 429)
        self.assertEqual(payload["error"]["code"], "provider_limit_reached")
        self.assertEqual(payload["error"]["provider"], "gemini")
        self.assertTrue(payload["error"]["limit_reached"])

    def test_multiagent_commands_custom_sync_gate_and_context_failover(self):
        from wsa_agent import agent as agent_module
        from wsa_agent import commands, tui
        from wsa_agent.client import ProviderLimitError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st = SessionState(project_root=root, session_id="multi_test")
            self.assertEqual(st.agent_mode, "singleagent")

            # Custom slot-by-slot picker order: gemini -> claude -> deepseek -> chatgpt (confirm 4 of 5 slots)
            picks = iter(["custom", "gemini", "claude", "deepseek", "chatgpt", "__done__"])
            poll_calls = []

            class _GateClient:
                api_base = "http://127.0.0.1:8081/v1"
                api_key = ""

                def get_health(self):
                    idx = len(poll_calls)
                    poll_calls.append(idx)
                    if idx == 0:
                        # Initially only gemini is synced; claude/deepseek/chatgpt missing
                        return {
                            "ok": True,
                            "relay_connected": True,
                            "active_relay": "chrome-1",
                            "active_providers": {
                                "gemini": {"has_tab": True, "model_badge": "Gemini Thinking"},
                                "claude": {"has_tab": False},
                                "deepseek": {"has_tab": False},
                                "chatgpt": {"has_tab": False},
                            },
                        }
                    # After clicking Sync and hitting Enter, background open tabs are all synced
                    return {
                        "ok": True,
                        "relay_connected": True,
                        "active_relay": "chrome-1",
                        "active_providers": {
                            "claude": {"has_tab": True, "model_badge": "Claude Sonnet"},
                            "deepseek": {"has_tab": True, "model_badge": "DeepSeek-R1 · DeepThink"},
                            "gemini": {"has_tab": True, "model_badge": "Gemini Thinking"},
                            "chatgpt": {"has_tab": True, "model_badge": "ChatGPT (Go) · Think"},
                        },
                    }

            with patch.object(tui, "_inline_select_menu", side_effect=lambda **kw: next(picks)), \
                 patch("builtins.input", return_value=""), \
                 patch.object(renderer.console, "print"):
                handled, cont = commands.handle_command("/multiagent", st, client=_GateClient())
                self.assertTrue(handled and cont)

            self.assertGreaterEqual(len(poll_calls), 2)
            self.assertEqual(st.agent_mode, "multiagent")
            self.assertEqual(st.multiagent_queue, ["gemini", "claude", "deepseek", "chatgpt"])
            self.assertEqual(st.model, "gemini")

            # Failover mid-turn: gemini hits limit -> seamlessly advances to claude with new_chat=True & full messages preserved
            st.chat_urls["gemini"] = "https://gemini.google.com/app/g-1"
            st.chat_urls["claude"] = "https://claude.ai/chat/c-old"
            calls = []

            class _FailoverClient:
                api_base = "http://127.0.0.1:8081/v1"
                api_key = ""

                def stream(self, messages, model, **kwargs):
                    calls.append({
                        "model": model,
                        "msg_count": len(messages),
                        "last_user": next((m["content"] for m in reversed(messages) if m.get("role") == "user"), ""),
                        "new_chat": kwargs.get("new_chat"),
                        "chat_url": kwargs.get("chat_url"),
                    })
                    if model == "gemini":
                        raise ProviderLimitError("upstream error: usage cap reached", provider="gemini")
                    return {"content": "Resumed seamlessly on Claude!", "tool_calls": [], "chat_url": "https://claude.ai/chat/c-fresh"}

            with patch.object(renderer.console, "print"):
                out = agent_module.execute_agent_loop("solve task", st, _FailoverClient(), quiet_ui=True)

            self.assertTrue(out.get("ok"))
            self.assertEqual(out.get("final_text"), "Resumed seamlessly on Claude!")
            self.assertEqual([c["model"] for c in calls], ["gemini", "claude"])
            self.assertFalse(calls[0]["new_chat"])
            self.assertTrue(calls[1]["new_chat"])
            self.assertIsNone(calls[1]["chat_url"])
            self.assertEqual(calls[1]["last_user"], "solve task")
            self.assertEqual(st.model, "claude")
            self.assertEqual(st.multiagent_index, 1)

            # Revert back cleanly with /singleagent
            with patch.object(renderer.console, "print"):
                commands.handle_command("/singleagent chatgpt", st, client=_GateClient())
            self.assertEqual(st.agent_mode, "singleagent")
            self.assertEqual(st.model, "chatgpt")

    def test_claude_and_gemini_dom_limit_detection_in_node_vm(self):
        import subprocess

        bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
        script = f"""
const assert = require("assert");
const vm = require("vm");
const bg = require({json.dumps(bg_path)});

(async () => {{
  class MockEl {{
    constructor(tag, attrs = {{}}, cls = "", text = "") {{
      this.nodeType = 1;
      this.tagName = String(tag).toUpperCase();
      this._attrs = Object.assign({{}}, attrs);
      this.className = cls;
      this.children = [];
      this.childNodes = text ? [{{ nodeType: 3, textContent: String(text) }}] : [];
      this.parentElement = null;
      this.disabled = false;
      this.offsetParent = {{}};
    }}
    get textContent() {{ return (this.childNodes || []).map((c) => c.textContent || "").join(""); }}
    set textContent(v) {{ this.children = []; this.childNodes = v ? [{{ nodeType: 3, textContent: String(v) }}] : []; }}
    get innerText() {{ return this.textContent; }}
    getAttribute(k) {{
      if (Object.prototype.hasOwnProperty.call(this._attrs, k)) return this._attrs[k];
      if (k === "class") return this.className || null;
      return null;
    }}
    contains(target) {{
      if (target === this) return true;
      return (this.children || []).some((c) => c === target || (c && typeof c.contains === "function" && c.contains(target)));
    }}
    getBoundingClientRect() {{ return {{ width: 120, height: 32, left: 0, top: 0, right: 120, bottom: 32 }}; }}
    getClientRects() {{ return [{{ width: 120, height: 32 }}]; }}
    closest() {{ return null; }}
    querySelectorAll() {{ return []; }}
    querySelector() {{ return null; }}
    appendChild(ch) {{ ch.parentElement = this; this.children.push(ch); this.childNodes.push(ch); return ch; }}
    matches(sel) {{
      return sel.split(",").some((p) => {{
        const s = p.trim();
        if (!s) return false;
        if (s.includes("[role='alert']")) return this.getAttribute("role") === "alert";
        if (s.includes("mat-dialog-container")) return this.tagName === "MAT-DIALOG-CONTAINER";
        return false;
      }});
    }}
  }}
  function walkAll(root, pred, out = []) {{
    for (const ch of root.children || []) {{
      if (pred(ch)) out.push(ch);
      walkAll(ch, pred, out);
    }}
    return out;
  }}

  // 1) Claude 5-hour limit banner
  const cRoot = new MockEl("div");
  cRoot.appendChild(new MockEl("div", {{ role: "alert" }}, "toast", "5-hour limit reached · Your limit will reset at 8:00 PM."));
  let fakeNow = 5000;
  class FakeDate {{ static now() {{ return fakeNow; }} }}
  const cDoc = {{
    querySelectorAll: (sel) => walkAll(cRoot, (el) => el.matches(sel)),
    querySelector: (sel) => walkAll(cRoot, (el) => el.matches(sel))[0] || null,
  }};
  const cSb = vm.createContext({{
    Element: MockEl, Date: FakeDate, Math, Object, Array, String, Number, Boolean, RegExp, Map, Set,
    location: {{ href: "https://claude.ai/new" }},
    window: {{ location: {{ href: "https://claude.ai/new" }} }},
    document: cDoc,
    getComputedStyle: () => ({{ display: "block", visibility: "visible", opacity: "1" }}),
  }});
  cSb.globalThis = cSb;
  vm.runInContext("(" + bg._claudeInstallPageRuntimeFn.toString() + ")()", cSb);
  cSb.__wsaClaudeRuntime.turns["cl-lim"] = {{
    startedAt: 1000, lastChangeAt: 1000, lastText: "", lastThinking: "",
    beforeTurnCount: 0, beforeLastText: "", beforeLastThinking: "",
    sawActivity: false, sawStop: false, modelBadge: "Claude Sonnet",
  }};
  const cRes = vm.runInContext("(" + bg._claudePollTurnFn.toString() + ")('cl-lim')", cSb);
  assert.strictEqual(cRes.done, false);
  assert(String(cRes.error || "").startsWith("[LIMIT_REACHED] Claude:"), cRes.error);

  // 2) Gemini limit dialog
  const gRoot = new MockEl("div");
  gRoot.appendChild(new MockEl("mat-dialog-container", {{}}, "", "You've reached your limit until 6:00 PM. Upgrade to continue."));
  const gDoc = {{
    querySelectorAll: (sel) => walkAll(gRoot, (el) => el.matches(sel)),
    querySelector: (sel) => walkAll(gRoot, (el) => el.matches(sel))[0] || null,
  }};
  const gSb = vm.createContext({{
    Element: MockEl, Date: FakeDate, Math, Object, Array, String, Number, Boolean, RegExp, Map, Set,
    console: {{ log: () => {{}} }},
    location: {{ href: "https://gemini.google.com/app" }},
    window: {{ location: {{ href: "https://gemini.google.com/app" }} }},
    document: gDoc,
    getComputedStyle: () => ({{ display: "block", visibility: "visible", opacity: "1" }}),
  }});
  gSb.globalThis = gSb;
  vm.runInContext("(" + bg._domInstallPageRuntimeFn.toString() + ")()", gSb);
  gSb.__wsaActiveJob = {{
    reqId: "gm-lim", startedAt: 1000, selectedBadge: "Gemini Thinking",
    beforeRelayObj: {{}}, beforeDomCount: 0, beforeModelCount: 0, beforeActionCount: 0,
    beforeAnnouncerSnapshot: "", lastText: "", lastThinking: "", sourceKind: "",
    thinkingLines: [], lastTextChangeAt: 1000, lastThinkChangeAt: 1000, lastBusyAt: 1000,
    sawNewTurn: false, announcerChanged: false,
  }};
  const gRes = vm.runInContext("(" + bg._domPollTurnFn.toString() + ")('gm-lim')", gSb);
  assert.strictEqual(gRes.done, false);
  assert(String(gRes.error || "").startsWith("[LIMIT_REACHED] Gemini:"), gRes.error);
  console.log("CLAUDE_GEMINI_LIMIT_VM_OK");
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        self.assertIn("CLAUDE_GEMINI_LIMIT_VM_OK", proc.stdout)

    def test_gemini_http_429_and_tools_welcome_showcase(self):
        import io
        import urllib.error
        from rich.console import Console
        from wsa_agent import commands
        from wsa_server import config as srv_cfg, gemini as srv_gemini, relay

        # 1) Exhausted HTTP 429 from direct Gemini upstream converts cleanly to ProviderLimitError
        http_429 = urllib.error.HTTPError("https://gemini.google.com/StreamGenerate", 429, "Too Many Requests", {}, None)
        with patch.object(srv_cfg, "CONFIG", {**srv_cfg.CONFIG, "retry_attempts": 1, "single_account_mode": True, "cookie_file": ""}), \
             patch.object(srv_gemini, "CONFIG", {**srv_gemini.CONFIG, "retry_attempts": 1, "single_account_mode": True, "cookie_file": ""}), \
             patch.object(srv_gemini, "_is_auto_rotate_allowed", return_value=False), \
             patch.object(srv_gemini, "_build_upstream_request", return_value=("https://gemini.google.com/test", {}, {})), \
             patch("urllib.request.urlopen", side_effect=http_429):
            with self.assertRaises(relay.ProviderLimitError) as ctx:
                srv_gemini.gemini_stream_generate("ping", model_cfg={"mode": 1, "think": 0})
            self.assertEqual(ctx.exception.provider, "gemini")
            self.assertIn("429", str(ctx.exception))

        # 2) Startup welcome & /tools showcase display major workflows + categorized tools cleanly
        st = SessionState(project_root=Path(__file__).resolve().parent.parent)
        buf = io.StringIO()
        orig = renderer.console
        try:
            renderer.console = Console(file=buf, force_terminal=False, width=108, highlight=False)
            renderer.print_welcome(st, server_ok=True)
            handled, cont = commands.handle_command("/tools", st)
            self.assertTrue(handled and cont)
        finally:
            renderer.console = orig
        out = buf.getvalue()
        for expected in (
            "/models", "/account", "/singleagent", "/multiagent", "/tools",
            "Workflows & Available Tools", "Major Workflows", "Workspace & Edits", "Browser Bridge",
        ):
            self.assertIn(expected, out)

    def test_real_http_server_multiagent_failover_and_context_handoff(self):
        import urllib.parse
        import urllib.request
        from wsa_agent import agent as agent_module, commands, client as client_mod
        from wsa_server import relay, server as srv_mod

        orig_relay_state = {
            "relays": dict(relay._dom_relay.get("relays") or {}),
            "active_relay_id": relay._dom_relay.get("active_relay_id"),
            "explicit_lock": relay._dom_relay.get("explicit_lock", False),
            "pending": dict(relay._dom_relay.get("pending") or {}),
            "sessions": dict(relay._dom_relay.get("sessions") or {}),
            "browser_cmds": dict(relay._dom_relay.get("browser_cmds") or {}),
            "cancelled_req_ids": dict(relay._dom_relay.get("cancelled_req_ids") or {}),
            "last_poll_ts": relay._dom_relay.get("last_poll_ts", 0.0),
        }
        orig_pending_meta = dict(relay._PENDING_SESSION_META)

        httpd = srv_mod.ThreadedServer(("127.0.0.1", 0), srv_mod.WSAHandler)
        srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        srv_thread.start()
        port = httpd.server_address[1]
        base_url = f"http://127.0.0.1:{port}"
        api_key = str((srv_mod.CONFIG.get("api_keys") or [""])[0] or "")

        stop_ext = threading.Event()
        dispatched_prompts = []

        def _ext_worker():
            cid = "ext-e2e-chrome"
            qs = urllib.parse.urlencode({
                "client_id": cid,
                "browser": "chrome",
                "label": "Chrome E2E",
                "provider": "gemini",
                "has_gemini": "1", "gemini_model": "Gemini Thinking",
                "has_claude": "1", "claude_model": "Claude Sonnet",
                "has_deepseek": "1", "deepseek_model": "DeepSeek-R1",
                "has_chatgpt": "1", "chatgpt_model": "ChatGPT (Go)",
            })
            claude_round = 0
            while not stop_ext.is_set():
                try:
                    with urllib.request.urlopen(f"{base_url}/pending-prompt?{qs}", timeout=2.0) as resp:
                        item = json.loads(resp.read().decode("utf-8"))
                except Exception:
                    stop_ext.wait(0.03)
                    continue
                if not item or not item.get("id"):
                    stop_ext.wait(0.03)
                    continue
                dispatched_prompts.append(item)
                prov = str(item.get("provider") or "gemini")
                if prov == "gemini":
                    payload = {
                        "id": item["id"],
                        "client_id": cid,
                        "error": "[LIMIT_REACHED] Gemini: You've reached your usage limit until 7:00 PM.",
                        "model": "Gemini Thinking",
                    }
                elif prov == "claude" and claude_round == 0:
                    claude_round += 1
                    payload = {
                        "id": item["id"],
                        "client_id": cid,
                        "response": 'Checking `bug.py` first.\n```tool_call\n{"name": "read_file", "arguments": {"path": "bug.py"}}\n```',
                        "model": "Claude Sonnet",
                        "chat_url": "https://claude.ai/chat/c-fresh-999",
                    }
                else:
                    payload = {
                        "id": item["id"],
                        "client_id": cid,
                        "response": "Verified bug.py on Claude: VERSION = 42.",
                        "model": "Claude Sonnet",
                        "chat_url": "https://claude.ai/chat/c-fresh-999",
                    }
                req = urllib.request.Request(
                    f"{base_url}/pending-response",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=2.0) as post_resp:
                    post_resp.read()

        ext_thread = threading.Thread(target=_ext_worker, daemon=True)
        try:
            with relay._dom_relay_lock:
                relay._dom_relay["relays"] = {}
                relay._dom_relay["active_relay_id"] = None
                relay._dom_relay["explicit_lock"] = False
                relay._dom_relay["pending"] = {}
                relay._dom_relay["sessions"] = {
                    "e2e_sess::claude": {
                        "last_sig": "old-sig",
                        "chat_url": "https://claude.ai/chat/stale-old-id",
                        "relay_id": "ext-e2e-chrome",
                        "provider": "claude",
                        "turns": 4,
                        "updated_at": 1.0,
                    }
                }
                relay._dom_relay["browser_cmds"] = {}
                relay._dom_relay["cancelled_req_ids"] = {}
                relay._dom_relay["last_poll_ts"] = 0.0
                relay._PENDING_SESSION_META.clear()

            ext_thread.start()
            real_client = client_mod.WSAClient(api_base=f"{base_url}/v1", api_key=api_key)

            # Wait briefly until /v1/health reports synced extension tabs across all 4 providers
            deadline = threading.Event()
            health = {}
            for _ in range(40):
                try:
                    health = real_client.get_health()
                    if health.get("dom_relay_active") and (health.get("active_providers") or {}).get("claude", {}).get("has_tab"):
                        break
                except Exception:
                    pass
                deadline.wait(0.05)
            self.assertTrue(health.get("dom_relay_active"), msg=f"Health did not activate: {health}")

            orig_root = tools.get_project_root()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "bug.py").write_text("VERSION = 42\n", encoding="utf-8")
                    st = SessionState(project_root=root, session_id="e2e_sess", api_base=f"{base_url}/v1", api_key=api_key)
                    st.auto_approve = True
                    st.chat_urls["claude"] = "https://claude.ai/chat/stale-old-id"

                    with patch.object(renderer.console, "print"):
                        handled, cont = commands.handle_command("/multiagent gemini claude", st, client=real_client)
                        self.assertTrue(handled and cont)
                        res = agent_module.execute_agent_loop("Inspect bug.py and report VERSION", st, real_client, quiet_ui=True)

                    self.assertTrue(res.get("ok"), msg=res)
                    self.assertEqual(res.get("final_text"), "Verified bug.py on Claude: VERSION = 42.")
                    self.assertEqual([d["provider"] for d in dispatched_prompts], ["gemini", "claude", "claude"])

                    claude_failover_dispatch = dispatched_prompts[1]
                    self.assertTrue(claude_failover_dispatch["new_chat"])
                    self.assertEqual(claude_failover_dispatch.get("chat_url") or "", "")
                    self.assertIn("Hi Claude —", claude_failover_dispatch["prompt"])
                    self.assertIn("Inspect bug.py and report VERSION", claude_failover_dispatch["prompt"])

                    claude_followup_dispatch = dispatched_prompts[2]
                    self.assertFalse(claude_followup_dispatch["new_chat"])
                    self.assertEqual(claude_followup_dispatch["chat_url"], "https://claude.ai/chat/c-fresh-999")
                    self.assertIn("VERSION = 42", claude_followup_dispatch["prompt"])
                    self.assertEqual(st.model, "claude")
                    self.assertEqual(st.active_chat_url, "https://claude.ai/chat/c-fresh-999")
            finally:
                tools.set_project_root(orig_root)
        finally:
            stop_ext.set()
            ext_thread.join(timeout=2.0)
            httpd.shutdown()
            httpd.server_close()
            with relay._dom_relay_lock:
                for k, v in orig_relay_state.items():
                    relay._dom_relay[k] = v
                relay._PENDING_SESSION_META.clear()
                relay._PENDING_SESSION_META.update(orig_pending_meta)

    def test_chatgpt_codesurface_deepseek_codeblock_turn1_user_guard_and_placeholder_filter(self):
        import subprocess
        from wsa_agent.agent import _extract_tool_calls_detailed, _looks_like_broken_tool_attempt
        from wsa_server.tools import inspect_unparsed_tool_attempts, parse_tool_calls

        bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
        script = "const bgPath = " + json.dumps(bg_path) + ";\n" + r"""
const assert = require("assert");
const vm = require("vm");
const bg = require(bgPath);

class MockEl {
  constructor(tag, attrs = {}, cls = "", text = "") {
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this._attrs = Object.assign({}, attrs);
    this.className = cls;
    this.style = {};
    this.children = [];
    this.childNodes = text ? [{ nodeType: 3, textContent: String(text) }] : [];
    this.parentElement = null;
    this.parentNode = null;
    this.disabled = false;
  }
  get textContent() { return (this.childNodes || []).map((c) => c.textContent || "").join(""); }
  set textContent(v) { this.children = []; this.childNodes = v ? [{ nodeType: 3, textContent: String(v) }] : []; }
  get innerText() { return this.textContent; }
  getAttribute(k) {
    if (Object.prototype.hasOwnProperty.call(this._attrs, k)) return this._attrs[k];
    if (k === "class") return this.className || null;
    return null;
  }
  contains(target) {
    if (target === this) return true;
    return (this.children || []).some((c) => c === target || (c && typeof c.contains === "function" && c.contains(target)));
  }
  appendChild(ch) {
    if (ch.nodeType === 1) { ch.parentElement = this; ch.parentNode = this; this.children.push(ch); }
    else { ch.parentNode = this; }
    this.childNodes.push(ch);
    return ch;
  }
  remove() {
    if (!this.parentElement) return;
    this.parentElement.children = this.parentElement.children.filter(c => c !== this);
    this.parentElement.childNodes = this.parentElement.childNodes.filter(c => c !== this);
    this.parentElement = null;
  }
  cloneNode(deep) {
    const c = new MockEl(this.tagName, Object.assign({}, this._attrs), this.className);
    c.style = Object.assign({}, this.style);
    if (deep) {
      for (const ch of this.childNodes || []) {
        if (ch.nodeType === 1 && typeof ch.cloneNode === "function") c.appendChild(ch.cloneNode(true));
        else c.appendChild({ nodeType: ch.nodeType, textContent: ch.textContent });
      }
    }
    return c;
  }
  matches(sel) {
    return sel.split(",").some((rawPart) => {
      const part = rawPart.trim();
      if (!part) return false;
      const segments = part.match(/(?:[^\s\[]+|\[[^\]]*\])+/g) || [part];
      const matchOne = (el, seg) => {
        let s = seg.trim();
        for (const am of s.matchAll(/\[([a-zA-Z0-9_-]+)(?:([*^$]?=)['"]([^'"]*)['"])?(?:\s+i)?\]/g)) {
          const val = el.getAttribute(am[1]);
          if (!am[2]) { if (val === null) return false; }
          else if (am[2] === "=") { if (val !== am[3]) return false; }
          else if (am[2] === "*=") { if (!String(val || "").toLowerCase().includes(am[3].toLowerCase())) return false; }
          else if (am[2] === "^=") { if (!String(val || "").startsWith(am[3])) return false; }
          else if (am[2] === "$=") { if (!String(val || "").endsWith(am[3])) return false; }
        }
        s = s.replace(/\[[^\]]+\]/g, "");
        for (const im of s.matchAll(/#([a-zA-Z0-9_-]+)/g)) {
          if (el.getAttribute("id") !== im[1]) return false;
        }
        s = s.replace(/#[a-zA-Z0-9_-]+/g, "");
        const tm = s.match(/^[a-zA-Z0-9_-]+/);
        if (tm && el.tagName.toLowerCase() !== tm[0].toLowerCase()) return false;
        for (const cm of s.matchAll(/\.([a-zA-Z0-9_-]+)/g)) {
          if (!String(el.className || "").split(/\s+/).includes(cm[1])) return false;
        }
        return true;
      };
      if (segments.length === 1) return matchOne(this, segments[0]);
      if (!matchOne(this, segments[segments.length - 1])) return false;
      let cur = this.parentElement;
      for (let i = segments.length - 2; i >= 0; i--) {
        while (cur && !matchOne(cur, segments[i])) cur = cur.parentElement;
        if (!cur) return false;
        cur = cur.parentElement;
      }
      return true;
    });
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => {
      for (const c of n.children || []) {
        if (c instanceof MockEl && c.matches(sel)) out.push(c);
        if (c instanceof MockEl) walk(c);
      }
    };
    walk(this);
    return out;
  }
  closest(sel) { let c = this; while (c) { if (c.matches(sel)) return c; c = c.parentElement; } return null; }
}

function makeSandbox(root, url) {
  const sb = {
    Element: MockEl,
    Node: { ELEMENT_NODE: 1, TEXT_NODE: 3 },
    Date: { now: () => 50000 },
    location: { href: url, pathname: "/" },
    getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1", backgroundColor: "", borderColor: "", color: "" }),
    getSelection: () => ({ removeAllRanges: () => {}, addRange: () => {} }),
    document: {
      querySelector: (s) => root.querySelector(s),
      querySelectorAll: (s) => root.querySelectorAll(s),
    },
  };
  sb.window = sb;
  sb.globalThis = sb;
  return sb;
}

// 1. ChatGPT Turn-1 User Bubble Isolation + .CodeSurface-UAMd1A without <pre>
const cgRoot = new MockEl("div");
const conv = new MockEl("div", { role: "region", "aria-label": "Conversation" });
const uTurn = new MockEl("div", { "data-content-search-turn-key": "u-turn-1" });
uTurn.appendChild(new MockEl("h4", {}, "sr-only", "You said:"));
const uBubble = new MockEl("div", {}, "bg-user-message text-user-message bubble-vBnidZ");
const uMd = new MockEl("div", {}, "MarkdownRoot-rZKhxa");
uMd.appendChild(new MockEl("p", {}, "", '### Local Tool Capabilities\n{"name": "func_name", "arguments": {"arg1": "val1"}}'));
uBubble.appendChild(uMd);
uTurn.appendChild(uBubble);
uTurn.appendChild(new MockEl("button", { "aria-label": "Copy message" }));
uTurn.appendChild(new MockEl("button", { "aria-label": "Edit message" }));
conv.appendChild(uTurn);
cgRoot.appendChild(conv);

const cgSb = makeSandbox(cgRoot, "https://chatgpt.com/c/cg-codesurface");
vm.runInNewContext("(" + bg._installWsaDomUtilsFn.toString() + ")()", cgSb);
vm.runInNewContext("(" + bg._chatgptInstallPageRuntimeFn.toString() + ")()", cgSb);
assert.strictEqual(cgSb.__wsaDomUtils.version, 3);
assert.strictEqual(cgSb.__wsaChatgptRuntime.version, 3);

// Turn 1 before assistant mounts: must NOT pick up the user's bubble/MarkdownRoot
assert.strictEqual(cgSb.__wsaChatgptRuntime.readAllTurns().length, 0);

// Even a container that hasn't mounted h4 "You said:" yet, with .bg-user-message + Edit button + .MarkdownRoot-, must be excluded
const uTurnEarly = new MockEl("div", { "data-content-search-turn-key": "u-turn-early" });
const uBubbleEarly = new MockEl("div", {}, "bg-user-message bubble-vBnidZ");
uBubbleEarly.appendChild(new MockEl("div", {}, "MarkdownRoot-rZKhxa", '{"name": "func_name", "arguments": {"arg1": "val1"}}'));
uTurnEarly.appendChild(uBubbleEarly);
uTurnEarly.appendChild(new MockEl("button", { "aria-label": "Edit message" }));
conv.appendChild(uTurnEarly);
assert.strictEqual(cgSb.__wsaChatgptRuntime.readAllTurns().length, 0);

// Mount live ChatGPT assistant turn with .CodeSurface-UAMd1A and NO <pre>
const aTurn = new MockEl("div", { "data-content-search-turn-key": "a-turn-1" });
aTurn.appendChild(new MockEl("h4", {}, "sr-only", "ChatGPT said:"));
const rendererRoot = new MockEl("div", {}, "DilRenderer-temL23 DilResponseRoot-fdCmoC Reveal-eHl_0F");
rendererRoot.appendChild(new MockEl("p", {}, "", "Let's test a harmless workspace function. I'll request the project root directory listing."));
const codeSurface = new MockEl("div", {}, "relative Root-iW7ipz CodeSurface-UAMd1A Surface-BCuIs1");
const actionBar = new MockEl("div", {}, "ActionBar-KWWAct StickyActionBar-RZfVQh");
actionBar.appendChild(new MockEl("div", {}, "min-w-0 truncate flex-1", "tool_call"));
actionBar.appendChild(new MockEl("button", { "aria-label": "Enable word wrap" }, "", ""));
actionBar.appendChild(new MockEl("button", { "aria-label": "Copy code" }, "", ""));
codeSurface.appendChild(actionBar);
const scrollport = new MockEl("div", {}, "Source-kILllF whitespace-pre chatgpt-code-scrollport InlineCodePane-EYx4bd");
const codeEl = new MockEl("code", {}, "whitespace-pre! block text-size-code CodeContent-ybJ8Tl min-w-max");
codeEl.appendChild(new MockEl("span", {}, "", '{"name":"list_dir","arguments":{"path":".","recursive":false}}'));
scrollport.appendChild(codeEl);
codeSurface.appendChild(scrollport);
rendererRoot.appendChild(codeSurface);
aTurn.appendChild(rendererRoot);
const cgActions = new MockEl("div", {}, "actionRow-nqqZbm turn-action-controls");
cgActions.appendChild(new MockEl("button", { "aria-label": "Rate response" }));
aTurn.appendChild(cgActions);
conv.appendChild(aTurn);

const cgTurns = cgSb.__wsaChatgptRuntime.readAllTurns();
assert.strictEqual(cgTurns.length, 1);
const cgText = cgTurns[0].text;
assert(!cgText.includes("Copy code"), "ActionBar chrome leaked into text: " + cgText);
assert(cgText.includes('```tool_call\n{"name":"list_dir","arguments":{"path":".","recursive":false}}\n```'), cgText);

// 2. DeepSeek .md-code-block with div.ds-button Copy/Download + <pre> without <code>
const dsRoot = new MockEl("div");
const dsMsg = new MockEl("div", {}, "ds-message _63c77b1");
const dsMain = new MockEl("div", {}, "ds-markdown ds-assistant-message-main-content");
dsMain.appendChild(new MockEl("p", {}, "", "Checking workspace root:"));
const dsCodeBlock = new MockEl("div", {}, "md-code-block md-code-block-dark");
const dsBanner = new MockEl("div", {}, "md-code-block-banner md-code-block-banner-lite");
dsBanner.appendChild(new MockEl("span", {}, "d813de27", "tool_call"));
const dsBtnGrp = new MockEl("div", {}, "efa13877");
dsBtnGrp.appendChild(new MockEl("div", { role: "button" }, "ds-button", "Copy"));
dsBtnGrp.appendChild(new MockEl("div", { role: "button" }, "ds-button", "Download"));
dsBanner.appendChild(dsBtnGrp);
dsCodeBlock.appendChild(dsBanner);
const dsPre = new MockEl("pre");
dsPre.appendChild(new MockEl("span", {}, "", '{"name":"list_dir","arguments":{"path":".","recursive":false}}'));
dsCodeBlock.appendChild(dsPre);
dsMain.appendChild(dsCodeBlock);
dsMsg.appendChild(dsMain);
dsRoot.appendChild(dsMsg);

const dsSb = makeSandbox(dsRoot, "https://chat.deepseek.com/a/chat/s/ds-codeblock");
vm.runInNewContext("(" + bg._installWsaDomUtilsFn.toString() + ")()", dsSb);
vm.runInNewContext("(" + bg._deepseekInstallPageRuntimeFn.toString() + ")()", dsSb);
assert.strictEqual(dsSb.__wsaDeepseekRuntime.version, 3);
const dsTurns = dsSb.__wsaDeepseekRuntime.readAllTurns();
assert.strictEqual(dsTurns.length, 1);
const dsText = dsTurns[0].text;
assert(!dsText.includes("Download"), "Download button text leaked: " + dsText);
assert(dsText.includes('```tool_call\n{"name":"list_dir","arguments":{"path":".","recursive":false}}\n```'), dsText);

console.log(JSON.stringify({ cgText, dsText }));
""";
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        vm_out = json.loads(proc.stdout.strip().splitlines()[-1])

        allowed = {"list_dir", "read_file", "patch_file"}
        for label, extracted in (("chatgpt", vm_out["cgText"]), ("deepseek", vm_out["dsText"])):
            cleaned, srv_calls = parse_tool_calls(extracted, allowed_names=allowed)
            self.assertEqual([c["function"]["name"] for c in srv_calls], ["list_dir"], msg=f"{label}: {extracted!r}")
            self.assertEqual(json.loads(srv_calls[0]["function"]["arguments"]), {"path": ".", "recursive": False})
            self.assertNotIn("list_dir", cleaned)

            cli_calls, cli_rem = _extract_tool_calls_detailed({"content": extracted, "tool_calls": []}, allowed_names=allowed)
            self.assertEqual([c["function"]["name"] for c in cli_calls], ["list_dir"], msg=f"{label} cli")
            self.assertNotIn("list_dir", cli_rem)

        # 3. Placeholders like func_name / action_name must NEVER execute even inside ```tool_call fences
        for ph_name in ("func_name", "action_name", "tool_name"):
            ph_text = f'```tool_call\n{{"name": "{ph_name}", "arguments": {{"arg1": "val1"}}}}\n```'
            cleaned_ph, calls_ph = parse_tool_calls(ph_text, allowed_names=allowed)
            self.assertEqual(calls_ph, [])
            self.assertEqual(inspect_unparsed_tool_attempts(cleaned_ph, allowed_names=allowed), [])
            cli_ph, rem_ph = _extract_tool_calls_detailed({"content": ph_text, "tool_calls": []}, allowed_names=allowed)
            self.assertEqual(cli_ph, [])
            self.assertFalse(_looks_like_broken_tool_attempt(rem_ph, allowed_names=allowed))

        # 4. Exact MSG 5 fallback: single-backtick standalone line executes cleanly and strips from prose; inline sentence mention does NOT
        msg5 = (
            "Let's test a harmless workspace function. I'll request the project root directory listing.\n\n"
            '`{"name":"list_dir","arguments":{"path":".","recursive":false}}`'
        )
        cleaned_5, calls_5 = parse_tool_calls(msg5, allowed_names=allowed)
        self.assertEqual(cleaned_5, "Let's test a harmless workspace function. I'll request the project root directory listing.")
        self.assertEqual([c["function"]["name"] for c in calls_5], ["list_dir"])
        cli_5, rem_5 = _extract_tool_calls_detailed({"content": msg5, "tool_calls": []}, allowed_names=allowed)
        self.assertEqual([c["function"]["name"] for c in cli_5], ["list_dir"])
        self.assertEqual(rem_5.strip(), "Let's test a harmless workspace function. I'll request the project root directory listing.")

        inline_bt = 'Inline prose `{"name":"list_dir","arguments":{"path":".","recursive":false}}` must stay prose.'
        cleaned_ib, calls_ib = parse_tool_calls(inline_bt, allowed_names=allowed)
        self.assertEqual(calls_ib, [])
        self.assertEqual(cleaned_ib, inline_bt)

    def test_real_http_4_provider_cascade_failover_prompts_and_single_chat_delta(self):
        import urllib.parse
        import urllib.request
        from wsa_agent import agent as agent_module, commands, client as client_mod
        from wsa_server import relay, server as srv_mod

        with relay._dom_relay_lock:
            orig_relay_state = {k: (dict(v) if isinstance(v, dict) else v) for k, v in relay._dom_relay.items()}
        orig_pending_meta = dict(relay._PENDING_SESSION_META)

        httpd = srv_mod.ThreadedServer(("127.0.0.1", 0), srv_mod.WSAHandler)
        srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        srv_thread.start()
        port = httpd.server_address[1]
        base_url = f"http://127.0.0.1:{port}"
        api_key = str((srv_mod.CONFIG.get("api_keys") or [""])[0] or "")

        stop_ext = threading.Event()
        dispatched = []

        def _ext_worker():
            cid = "ext-4prov-chrome"
            qs = urllib.parse.urlencode({
                "client_id": cid,
                "browser": "chrome",
                "label": "Chrome 4P",
                "provider": "claude",
                "has_claude": "1", "claude_model": "Claude Sonnet", "claude_email": "c@example.com",
                "has_deepseek": "1", "deepseek_model": "DeepSeek-R1 · DeepThink", "deepseek_email": "d@example.com",
                "has_gemini": "1", "gemini_model": "Gemini Thinking", "gemini_email": "g@example.com",
                "has_chatgpt": "1", "chatgpt_model": "ChatGPT · Think", "chatgpt_email": "o@example.com",
                "has_venice": "1", "venice_model": "DeepSeek V4.1 Flash Uncensored", "venice_email": "v@example.com",
            })
            claude_round = 0
            chatgpt_round = 0
            while not stop_ext.is_set():
                try:
                    with urllib.request.urlopen(f"{base_url}/pending-prompt?{qs}", timeout=2.0) as resp:
                        item = json.loads(resp.read().decode("utf-8"))
                except Exception:
                    stop_ext.wait(0.02)
                    continue
                if not item or not item.get("id"):
                    stop_ext.wait(0.02)
                    continue
                dispatched.append(item)
                prov = str(item.get("provider") or "gemini")
                if prov == "claude":
                    if claude_round == 0:
                        claude_round += 1
                        payload = {
                            "id": item["id"],
                            "client_id": cid,
                            "response": 'Reading config.\n```tool_call\n{"name": "read_file", "arguments": {"path": "app.py"}}\n```',
                            "thinking": "Thinking Process:\n1. Inspect app.py",
                            "model": "Claude Sonnet",
                            "chat_url": "https://claude.ai/chat/c-cascade-1",
                        }
                    else:
                        payload = {
                            "id": item["id"],
                            "client_id": cid,
                            "error": "[LIMIT_REACHED] Claude: 5-hour limit reached",
                            "model": "Claude Sonnet",
                        }
                elif prov == "deepseek":
                    payload = {
                        "id": item["id"],
                        "client_id": cid,
                        "error": "[LIMIT_REACHED] DeepSeek: server is busy",
                        "model": "DeepSeek-R1 · DeepThink",
                    }
                elif prov == "gemini":
                    payload = {
                        "id": item["id"],
                        "client_id": cid,
                        "error": "[LIMIT_REACHED] Gemini: You've reached your usage limit",
                        "model": "Gemini Thinking",
                    }
                elif prov == "chatgpt" and chatgpt_round == 0:
                    chatgpt_round += 1
                    payload = {
                        "id": item["id"],
                        "client_id": cid,
                        "response": 'Checking `flag.py`.\n```tool_call\n{"name": "read_file", "arguments": {"path": "flag.py"}}\n```',
                        "model": "ChatGPT · Think",
                        "chat_url": "https://chatgpt.com/c/gpt-cascade-99",
                    }
                else:
                    payload = {
                        "id": item["id"],
                        "client_id": cid,
                        "response": "Done via 4-provider failover: APP=10, FLAG=OK.",
                        "model": "ChatGPT · Think",
                        "chat_url": "https://chatgpt.com/c/gpt-cascade-99",
                    }
                req = urllib.request.Request(
                    f"{base_url}/pending-response",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=2.0) as post_resp:
                    post_resp.read()

        ext_thread = threading.Thread(target=_ext_worker, daemon=True)
        try:
            with relay._dom_relay_lock:
                relay._dom_relay["relays"] = {}
                relay._dom_relay["active_relay_id"] = None
                relay._dom_relay["explicit_relay_lock"] = False
                relay._dom_relay["pending"] = {}
                relay._dom_relay["sessions"] = {}
                relay._dom_relay["browser_cmds"] = {}
                relay._dom_relay["cancelled_req_ids"] = {}
                relay._dom_relay["last_poll_ts"] = 0.0
                relay._PENDING_SESSION_META.clear()

            ext_thread.start()
            real_client = client_mod.WSAClient(api_base=f"{base_url}/v1", api_key=api_key)

            ev = threading.Event()
            health = {}
            for _ in range(40):
                try:
                    health = real_client.get_health()
                    if (health.get("active_providers") or {}).get("chatgpt", {}).get("has_tab"):
                        break
                except Exception:
                    pass
                ev.wait(0.04)
            self.assertEqual((health.get("active_providers") or {}).get("claude", {}).get("email"), "c@example.com")
            self.assertEqual((health.get("active_providers") or {}).get("claude", {}).get("account_email"), "c@example.com")

            orig_root = tools.get_project_root()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "app.py").write_text("APP = 10\n", encoding="utf-8")
                    (root / "flag.py").write_text("FLAG = 'OK'\n", encoding="utf-8")
                    st = SessionState(project_root=root, session_id="e2e_4prov", api_base=f"{base_url}/v1", api_key=api_key)
                    st.auto_approve = True
                    st.chat_urls["deepseek"] = "https://chat.deepseek.com/a/chat/s/stale-ds"
                    st.chat_urls["chatgpt"] = "https://chatgpt.com/c/stale-gpt"

                    captured_gate = []
                    with patch.object(renderer.console, "print", side_effect=lambda *a, **k: captured_gate.append(" ".join(str(x) for x in a))):
                        handled, cont = commands.handle_command("/multiagent default", st, client=real_client)
                        self.assertTrue(handled and cont)
                        res = agent_module.execute_agent_loop("Check app.py and flag.py", st, real_client, quiet_ui=True)

                    gate_txt = "\n".join(captured_gate)
                    self.assertIn("c@example.com", gate_txt)
                    self.assertTrue(res.get("ok"), msg=res)
                    self.assertEqual(res.get("final_text"), "Done via 4-provider failover: APP=10, FLAG=OK.")
                    self.assertEqual(
                        [d["provider"] for d in dispatched],
                        ["claude", "claude", "deepseek", "gemini", "chatgpt", "chatgpt"],
                    )

                    # Each provider got its own preferred system/prompt framing and full carried context
                    claude_0, claude_1, ds_fail, gem_fail, gpt_0, gpt_1 = dispatched
                    self.assertIn("Hi Claude —", claude_0["prompt"])
                    self.assertFalse(claude_1["new_chat"])
                    self.assertEqual(claude_1["chat_url"], "https://claude.ai/chat/c-cascade-1")
                    self.assertIn("APP = 10", claude_1["prompt"])

                    self.assertTrue(ds_fail["new_chat"])
                    self.assertEqual(ds_fail.get("chat_url") or "", "")
                    self.assertNotIn("Hi Claude —", ds_fail["prompt"])
                    self.assertIn("### Local Tool Capabilities", ds_fail["prompt"])
                    self.assertIn("DeepSeek", ds_fail["prompt"])
                    self.assertIn("APP = 10", ds_fail["prompt"])

                    self.assertTrue(gem_fail["new_chat"])
                    self.assertNotIn("Hi Claude —", gem_fail["prompt"])
                    self.assertIn("### Local Tool Capabilities", gem_fail["prompt"])
                    self.assertIn("APP = 10", gem_fail["prompt"])

                    self.assertTrue(gpt_0["new_chat"])
                    self.assertEqual(gpt_0.get("chat_url") or "", "")
                    self.assertNotIn("Hi Claude —", gpt_0["prompt"])
                    self.assertIn("ChatGPT", gpt_0["prompt"])
                    self.assertIn("Check app.py and flag.py", gpt_0["prompt"])
                    self.assertIn("APP = 10", gpt_0["prompt"])

                    # Same-chat delta continuation on surviving provider (chatgpt)
                    self.assertFalse(gpt_1["new_chat"])
                    self.assertEqual(gpt_1["chat_url"], "https://chatgpt.com/c/gpt-cascade-99")
                    self.assertIn("FLAG = 'OK'", gpt_1["prompt"])
                    self.assertEqual(st.model, "chatgpt")
                    self.assertEqual(st.multiagent_index, 3)
                    self.assertEqual(st.active_chat_url, "https://chatgpt.com/c/gpt-cascade-99")

                    # /v1/responses over live HTTP with DOM relay active routes cleanly via provider framing & skips upload_image
                    import base64
                    png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
                    req_resp = urllib.request.Request(
                        f"{base_url}/v1/responses",
                        data=json.dumps({
                            "model": "chatgpt",
                            "input": [{
                                "role": "user",
                                "content": [
                                    {"type": "input_text", "text": "Reply cleanly"},
                                    {"type": "input_image", "image_url": f"data:image/png;base64,{png_b64}"},
                                ],
                            }],
                        }).encode("utf-8"),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {api_key}",
                            "X-WSA-Session-Id": "resp_e2e_1",
                        },
                        method="POST",
                    )
                    with patch("wsa_server.server.upload_image", side_effect=AssertionError("should not upload to Gemini in DOM relay")):
                        with urllib.request.urlopen(req_resp, timeout=5.0) as r:
                            resp_json = json.loads(r.read().decode("utf-8"))
                    self.assertEqual(resp_json["output"][0]["content"][0]["text"], "Done via 4-provider failover: APP=10, FLAG=OK.")
                    self.assertEqual(dispatched[-1]["provider"], "chatgpt")
                    self.assertIn("Reply cleanly", dispatched[-1]["prompt"])
                    self.assertIsNone(getattr(srv_mod._relay_req_ctx, "relay_meta", None))
            finally:
                tools.set_project_root(orig_root)
        finally:
            stop_ext.set()
            ext_thread.join(timeout=2.0)
            httpd.shutdown()
            httpd.server_close()
            with relay._dom_relay_lock:
                for k, v in orig_relay_state.items():
                    relay._dom_relay[k] = v
                relay._PENDING_SESSION_META.clear()
                relay._PENDING_SESSION_META.update(orig_pending_meta)

    def test_subagent_failover_keeps_next_provider_prompt_across_rounds_and_preserves_role_threads(self):
        from concurrent.futures import ThreadPoolExecutor
        from wsa_agent import agent as agent_module, subagents
        from wsa_agent.client import ProviderLimitError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "target.py").write_text("X = 7\n", encoding="utf-8")
            orig_root = tools.get_project_root()
            tools.set_project_root(root)
            try:
                st = SessionState(project_root=root, session_id="sub_fail", thread_mode="roles")
                st.auto_approve = True
                st.configure_multiagent(["claude", "deepseek", "gemini"])
                st.role_threads = {
                    "explorer": "https://gemini.google.com/app/exp-keep",
                    "coder": "https://claude.ai/chat/coder-stale",
                }
                self.assertEqual(st.role_session_id("explorer"), "sub_fail:role:explorer")
                self.assertEqual(st.role_threads.get("explorer"), "https://gemini.google.com/app/exp-keep")
                self.assertEqual(st.get_role_chat_url("coder", "deepseek"), "")
                self.assertEqual(st.get_role_chat_url("coder", "claude"), "https://claude.ai/chat/coder-stale")

                recorded_calls = []
                ds_round = {"n": 0}

                class _SubFailClient:
                    api_base = "http://127.0.0.1:8081/v1"
                    api_key = ""

                    def stream(self, messages, model, **kwargs):
                        sys_txt = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
                        recorded_calls.append({
                            "model": model,
                            "system": sys_txt,
                            "new_chat": kwargs.get("new_chat"),
                            "chat_url": kwargs.get("chat_url"),
                            "session_id": kwargs.get("session_id"),
                        })
                        if model == "claude":
                            raise ProviderLimitError("upstream error: [LIMIT_REACHED] Claude: 5-hour limit", provider="claude")
                        if ds_round["n"] == 0:
                            ds_round["n"] += 1
                            return {
                                "content": '```tool_call\n{"name": "read_file", "arguments": {"path": "target.py"}}\n```',
                                "tool_calls": [],
                                "upstream_model": {
                                    "provider": "deepseek",
                                    "badge": "DeepSeek-R1",
                                    "chat_url": "https://chat.deepseek.com/a/chat/s/ds-coder-42",
                                },
                            }
                        return {
                            "content": "Subagent finished X=7 on DeepSeek.",
                            "tool_calls": [],
                            "upstream_model": {
                                "provider": "deepseek",
                                "badge": "DeepSeek-R1",
                                "chat_url": "https://chat.deepseek.com/a/chat/s/ds-coder-42",
                            },
                        }

                rendered_failovers = []
                orig_print = renderer.console.print
                with patch.object(renderer.console, "print", side_effect=lambda *a, **k: rendered_failovers.append(" ".join(str(x) for x in a))):
                    out = subagents._run_role_agent(
                        role="coder",
                        prompt="Read target.py",
                        state=st,
                        client=_SubFailClient(),
                        label="coder",
                    )

                self.assertTrue(out.get("ok"), msg=out)
                self.assertEqual(out.get("final_text"), "Subagent finished X=7 on DeepSeek.")
                self.assertEqual([c["model"] for c in recorded_calls], ["claude", "deepseek", "deepseek"])

                # Round 1 on DeepSeek switched prompt from Claude to non-Claude coder prompt AND Round 2 kept it!
                self.assertIn("tool_call", recorded_calls[0]["system"])
                self.assertNotIn("Strictly edit ONLY files", recorded_calls[0]["system"])
                self.assertIn("Strictly edit ONLY files", recorded_calls[1]["system"])
                self.assertNotIn("Hi Claude —", recorded_calls[1]["system"])
                self.assertEqual(recorded_calls[2]["system"], recorded_calls[1]["system"])
                self.assertTrue(recorded_calls[1]["new_chat"])
                self.assertFalse(recorded_calls[2]["new_chat"])
                self.assertEqual(recorded_calls[2]["chat_url"], "https://chat.deepseek.com/a/chat/s/ds-coder-42")
                self.assertEqual(st.get_role_chat_url("coder", "deepseek"), "https://chat.deepseek.com/a/chat/s/ds-coder-42")
                self.assertEqual(st.role_threads.get("explorer"), "https://gemini.google.com/app/exp-keep")

                joined_ui = "\n".join(rendered_failovers)
                self.assertNotIn("? → ?", joined_ui)
                self.assertIn("failover", joined_ui.lower())
                self.assertIn("deepseek", joined_ui.lower())

                # Concurrent dual-lane failover from the same failed model advances once, not twice
                conc_st = SessionState(project_root=root, session_id="conc_fail")
                conc_st.configure_multiagent(["claude", "deepseek", "gemini"])
                with ThreadPoolExecutor(max_workers=4) as ex:
                    res_lanes = list(ex.map(lambda _: conc_st.advance_multiagent_queue(failed_model="claude"), range(4)))
                self.assertEqual(set(res_lanes), {"deepseek"})
                self.assertEqual(conc_st.model, "deepseek")
                self.assertEqual(conc_st.multiagent_index, 1)
            finally:
                tools.set_project_root(orig_root)

    def test_background_js_limit_detection_and_gemini_provider_thinking_cleanup(self):
        import subprocess
        from wsa_server import gemini as gem_mod, relay

        t_delta, nxt_t = relay._compute_thinking_delta("", "Thought process\n1. Inspecting plan\nThinking...", provider="claude")
        self.assertEqual(t_delta, "1. Inspecting plan")
        self.assertIn("1. Inspecting plan", nxt_t)

        # Simulate gemini_stream_generate_events final relay_res using bridge_provider="claude" cleaning
        with patch.object(gem_mod, "_bridge_provider_for_cfg", return_value="claude"), \
             patch.object(gem_mod, "is_dom_relay_active", return_value=True), \
             patch.object(gem_mod, "dom_relay_iter", return_value=iter([("result", {
                 "response": "All done.",
                 "thinking": "Thought process\n1. Step A\nThinking...",
                 "model": "Claude Sonnet",
             })])):
            ev_list = list(gem_mod.gemini_stream_generate_events("hi", model_cfg={"mode": -2, "model": "claude-web", "provider": "claude"}))
        think_chunks = [v for k, v in ev_list if k == "thinking"]
        self.assertEqual(think_chunks, ["1. Step A"])

        bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
        js = "const bgPath = " + json.dumps(bg_path) + ";\n" + r"""
const assert = require("assert");
const vm = require("vm");
const bg = require(bgPath);

class MockElement {}
function mkBannerDoc(bannerText) {
  const banner = Object.assign(new MockElement(), {
    nodeType: 1, tagName: "DIV", className: "error-banner", style: {},
    innerText: bannerText, textContent: bannerText,
    children: [], childNodes: [],
    getBoundingClientRect: () => ({ width: 320, height: 48, top: 10, left: 10, bottom: 58, right: 330 }),
    getAttribute: (n) => (n === "role" ? "alert" : ""),
    closest: () => null, querySelector: () => null, querySelectorAll: () => [],
  });
  return {
    location: { href: "https://chatgpt.com/" },
    querySelector: () => null,
    querySelectorAll: (sel) => (sel.includes("alert") ? [banner] : []),
    activeElement: null,
  };
}

async function run() {
  const longFiller = "A".repeat(470);
  const bannerDoc = mkBannerDoc(longFiller + " | You've reached the Free plan limit for GPT-5.");
  const sb = {
    Element: MockElement,
    window: { location: { href: "https://chatgpt.com/" }, getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }) },
    document: bannerDoc,
    location: bannerDoc.location,
    getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
    setTimeout: (fn) => { fn(); return 1; },
    clearTimeout: () => {},
    console,
  };
  sb.globalThis = sb;
  vm.runInNewContext("(" + bg._installWsaDomUtilsFn.toString() + ")()", sb);
  vm.runInNewContext("(" + bg._chatgptInstallPageRuntimeFn.toString() + ")()", sb);

  // 1. Pipe-split combined banner > 460 total chars triggers ONLY in banner mode (prevents long-prose false positives)
  assert.strictEqual(
    sb.__wsaDomUtils.detectLimitMessage(longFiller + " | You've reached the Free plan limit for GPT-5."),
    ""
  );
  assert.strictEqual(
    sb.__wsaDomUtils.detectLimitMessage(longFiller + " | You've reached the Free plan limit for GPT-5.", true),
    "You've reached the Free plan limit for GPT-5."
  );
  assert(sb.__wsaDomUtils.detectLimitMessage("You have reached your daily message limit."));
  assert(sb.__wsaDomUtils.detectLimitMessage("0 messages remaining until tomorrow."));

  // 2. Hidden composer with visible limit banner fast-fails with [LIMIT_REACHED]
  const res = await vm.runInNewContext(
    "(" + bg._chatgptStartTurnFn.toString() + ")('req1', 'hello', [])",
    sb
  );
  assert.strictEqual(res.ok, false);
  assert(res.error.startsWith("[LIMIT_REACHED] ChatGPT:"), res.error);
}
run().catch((e) => { console.error(e); process.exit(1); });
"""
        proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)

    def test_chatgpt_large_delta_and_failover_context_budgeting(self):
        from wsa_server import multimodal as mm, relay
        from wsa_agent import agent, tools

        self.assertEqual(mm.get_prompt_max_bytes("chatgpt"), mm.CHATGPT_PROMPT_MAX_BYTES)
        self.assertEqual(mm.get_prompt_max_bytes("gemini"), mm.PROMPT_MAX_BYTES)
        self.assertEqual(mm.get_prompt_max_bytes("claude"), mm.PROMPT_MAX_BYTES)
        self.assertEqual(agent._max_web_prompt_chars_for_model("chatgpt"), agent.CHATGPT_WEB_PROMPT_CHARS)
        self.assertEqual(agent._max_web_prompt_chars_for_model("claude"), agent.MAX_WEB_PROMPT_CHARS)

        numbered_big_1 = "\n".join(f"  {i:>4} | \"key_{i}\": \"baseline_value_{'x' * 72}\"" for i in range(1, 380))
        numbered_big_2 = "\n".join(f"  {i:>4} | ## Implement section {i} {'details ' * 9}" for i in range(1, 310))
        giant_minified = '{"data": "' + ("m" * 31_000) + '", "tail_key": "end_marker_xyz"}'

        delta_msgs = [
            {
                "role": "assistant",
                "content": "Reading requested files.",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"baseline.json"}'}},
                    {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"implement.md"}'}},
                    {"id": "c3", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"mini.json"}'}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": numbered_big_1},
            {"role": "tool", "tool_call_id": "c2", "name": "read_file", "content": numbered_big_2},
            {"role": "tool", "tool_call_id": "c3", "name": "read_file", "content": giant_minified},
        ]
        full_msgs = [
            {"role": "system", "content": "Follow local workspace instructions."},
            {"role": "user", "content": "Audit baseline.json and implement.md thoroughly"},
            {"role": "assistant", "content": "Starting inspection.", "tool_calls": [{"id": "c0", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c0", "name": "list_dir", "content": "baseline.json\nimplement.md\n"},
            *delta_msgs,
        ]
        self.assertGreater(sum(len(str(m.get("content") or "")) for m in delta_msgs), 80_000)

        cg_delta, _ = mm.messages_to_delta_prompt(delta_msgs, full_msgs, tools.TOOL_DEFINITIONS, provider="chatgpt")
        self.assertLessEqual(len(cg_delta), mm.CHATGPT_PROMPT_MAX_BYTES)
        self.assertIn("[Tool result for read_file]:", cg_delta)
        self.assertIn("file lines", cg_delta)
        self.assertIn("end_marker_xyz", cg_delta)
        self.assertIn("[Next step]:", cg_delta)

        gem_delta, _ = mm.messages_to_delta_prompt(delta_msgs, full_msgs, tools.TOOL_DEFINITIONS, provider="gemini")
        self.assertGreater(len(gem_delta), 75_000)
        self.assertLessEqual(len(gem_delta), mm.PROMPT_MAX_BYTES)

        # Verify relay delta continuity on ChatGPT stays in same-chat (new_chat=False) and respects 26KB limit
        with patch.dict(relay._dom_relay, {"sessions": {}, "relays": {}, "active_relay_id": ""}):
            relay._dom_relay["sessions"]["s1::chatgpt"] = {
                "last_sig": relay._assistant_turn_sig("Starting inspection.", full_msgs[2]["tool_calls"]),
                "chat_url": "https://chatgpt.com/c/abc-123",
                "turns": 1,
                "tools_sig": relay._tools_sig(tools.TOOL_DEFINITIONS),
                "sys_sig": relay._sys_role_meta(full_msgs)[0],
            }
            full_cg, _ = mm.messages_to_prompt(full_msgs, tools.TOOL_DEFINITIONS, provider="chatgpt")
            self.assertLessEqual(len(full_cg), mm.CHATGPT_PROMPT_MAX_BYTES)
            prompt_to_send, _, _, is_new, _hint = relay.prepare_session_relay_prompt(
                "s1", full_msgs, tools.TOOL_DEFINITIONS, full_cg, [], provider="chatgpt"
            )
            self.assertFalse(is_new)
            self.assertLessEqual(len(prompt_to_send), mm.CHATGPT_PROMPT_MAX_BYTES)

        # Agent proportional compaction handles oversized current-round tool batch under ChatGPT's 21.5KB cap
        compacted = [dict(m) for m in full_msgs]
        n_comp = agent._compact_messages_for_web_limit(
            compacted, max_chars=agent._max_web_prompt_chars_for_model("chatgpt")
        )
        self.assertGreaterEqual(n_comp, 3)
        self.assertLessEqual(agent._messages_total_chars(compacted), agent.CHATGPT_WEB_PROMPT_CHARS + 300)
        self.assertTrue(any("omitted" in str(m.get("content") or "") for m in compacted if m.get("role") == "tool"))

    def test_multiagent_failover_compresses_context_for_chatgpt(self):
        from wsa_server import multimodal as mm
        from wsa_agent import agent, client as cli, session as sess, tools

        big_read = "\n".join(f"  {i:>4} | line content {'y' * 85}" for i in range(1, 520))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text(big_read, encoding="utf-8")
            orig_root = tools.get_project_root()
            tools.set_project_root(root)
            try:
                st = sess.SessionState(project_root=root, session_id="failover_cg")
                st.auto_approve = True
                st.configure_multiagent(["claude", "chatgpt"])
                st.confirm_edits = False
                seen_calls = []

                class FailoverClient:
                    def stream(self, *, messages, model, **kwargs):
                        snap = [dict(m) for m in messages]
                        seen_calls.append((model, kwargs.get("new_chat"), snap))
                        if len(seen_calls) == 1:
                            return {
                                "content": "Reading app.py twice to gather context.",
                                "thinking": "",
                                "tool_calls": [
                                    {"id": "t1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"app.py","start_line":1,"end_line":520}'}},
                                ],
                            }
                        if len(seen_calls) == 2:
                            raise cli.ProviderLimitError("You've hit your usage limit", provider="claude")
                        return {"content": "Completed handoff on ChatGPT cleanly.", "thinking": "", "tool_calls": []}

                res = agent.execute_agent_loop(
                    "Audit app.py carefully and report exact findings",
                    st,
                    FailoverClient(),
                    quiet_ui=True,
                )
                self.assertTrue(res["ok"])
                self.assertEqual([m for m, _, _ in seen_calls], ["claude", "claude", "chatgpt"])
                cg_model, cg_new_chat, cg_msgs = seen_calls[-1]
                self.assertEqual(cg_model, "chatgpt")
                self.assertTrue(cg_new_chat)
                self.assertLessEqual(agent._messages_total_chars(cg_msgs), agent.CHATGPT_WEB_PROMPT_CHARS + 500)
                rendered_cg, _ = mm.messages_to_prompt(cg_msgs, tools.TOOL_DEFINITIONS, provider="chatgpt")
                self.assertLessEqual(len(rendered_cg), mm.CHATGPT_PROMPT_MAX_BYTES)
                self.assertIn("Audit app.py carefully", rendered_cg)
            finally:
                tools.set_project_root(orig_root)

    def test_prompt_too_long_detection_server_client_and_agent_autorecovery(self):
        import io
        import json as _json
        import subprocess
        import httpx
        from wsa_server import relay, server as srv
        from wsa_agent import agent, client as cli, session as sess, tools

        # 1. Server / relay classification keeps [PROMPT_TOO_LONG] distinct from [LIMIT_REACHED]
        too_long_msg = "[PROMPT_TOO_LONG] ChatGPT: The message you submitted was too long, please edit it and resubmit."
        self.assertTrue(relay.is_prompt_too_long_error(too_long_msg))
        self.assertFalse(relay.is_provider_limit_error(too_long_msg))
        self.assertFalse(relay.is_provider_limit_error("Character limit exceeded."))
        status, body = srv._build_upstream_error_payload(relay.PromptTooLongError(too_long_msg, provider="chatgpt"), "chatgpt")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "context_length_exceeded")
        self.assertTrue(body["error"]["prompt_too_long"])

        # 2. Client raises PromptTooLongError (not ProviderLimitError) for HTTP 400 & SSE error chunks
        c = cli.WSAClient("http://127.0.0.1:0/v1", "test-key")
        c._client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(400, json=body)))
        with self.assertRaises(cli.PromptTooLongError):
            c.stream([{"role": "user", "content": "huge prompt"}], model="chatgpt")

        sse_err_bytes = f"data: {_json.dumps(body)}\n\ndata: [DONE]\n\n".encode("utf-8")
        c._client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=sse_err_bytes)))
        with self.assertRaises(cli.PromptTooLongError):
            c.stream([{"role": "user", "content": "huge prompt"}], model="chatgpt")

        # 3. Agent auto-compacts & retries on chatgpt without advancing /multiagent queue
        orig_root = tools.get_project_root()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tools.set_project_root(root)
            try:
                st = sess.SessionState(project_root=root, session_id="ptl_recover")
                st.auto_approve = True
                st.configure_multiagent(["chatgpt", "claude"])
                st.messages = [
                    {"role": "user", "content": "earlier check"},
                    {"role": "assistant", "content": "ran", "tool_calls": [{"id": "o1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]},
                    {"role": "tool", "tool_call_id": "o1", "name": "read_file", "content": "\n".join(f"  {i:>4} | old data {'q' * 80}" for i in range(1, 95))},
                ]
                attempts = []

                class TooLongOnceClient:
                    def stream(self, *, messages, model, **kwargs):
                        attempts.append((model, agent._messages_total_chars(messages)))
                        if len(attempts) == 1:
                            raise cli.PromptTooLongError(too_long_msg, provider="chatgpt")
                        return {"content": "Recovered after compacting on ChatGPT.", "thinking": "", "tool_calls": []}

                res = agent.execute_agent_loop("Finish analysis", st, TooLongOnceClient(), quiet_ui=True)
                self.assertTrue(res["ok"])
                self.assertEqual([m for m, _ in attempts], ["chatgpt", "chatgpt"])
                self.assertLess(attempts[1][1], attempts[0][1])
                self.assertEqual(st.model, "chatgpt")
                self.assertEqual(st.multiagent_index, 0)
            finally:
                tools.set_project_root(orig_root)

        # 4. Node VM verifies ChatGPT composer error banner + composer draft replacement
        bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
        js = "const bgPath = " + json.dumps(bg_path) + ";\n" + r"""
const assert = require("assert");
const vm = require("vm");
const bg = require(bgPath);

class MockElement {}
async function run() {
  let bannerTxt = "";
  let sentClicks = 0;
  let curEditorText = "### CURRENT ACTIVE USER REQUEST\n" + "X".repeat(30000);
  const pNode = Object.assign(new MockElement(), { nodeType: 1, tagName: "P", textContent: curEditorText });
  const editorEl = Object.assign(new MockElement(), {
    nodeType: 1, tagName: "DIV", id: "prompt-textarea",
    isContentEditable: true,
    children: [pNode], childNodes: [pNode],
    focus() {},
    replaceChildren(...nodes) {
      this.children = nodes; this.childNodes = nodes;
      curEditorText = nodes.map(n => n.textContent || "").join("\n");
    },
    dispatchEvent() { return true; },
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ width: 500, height: 80, top: 20, left: 20, bottom: 100, right: 520 }),
    getAttribute: () => "true",
    closest: () => null,
  });
  Object.defineProperties(editorEl, {
    innerText: { get: () => curEditorText, set: (v) => { curEditorText = v; } },
    textContent: { get: () => curEditorText, set: (v) => { curEditorText = v; } },
  });
  const sendBtn = Object.assign(new MockElement(), {
    nodeType: 1, tagName: "BUTTON", disabled: false, innerText: "Send",
    click() {
      sentClicks += 1;
      bannerTxt = "The message you submitted was too long, please edit it and resubmit.";
    },
    getBoundingClientRect: () => ({ width: 32, height: 32, top: 20, left: 530, bottom: 52, right: 562 }),
    getAttribute: (k) => (k === "data-testid" ? "send-button" : ""),
    closest: () => null,
  });
  const alertEl = Object.assign(new MockElement(), {
    nodeType: 1, tagName: "DIV",
    getBoundingClientRect: () => (bannerTxt ? { width: 400, height: 40, top: 10, left: 10, bottom: 50, right: 410 } : { width: 0, height: 0, top: 0, left: 0, bottom: 0, right: 0 }),
    getAttribute: () => "alert",
    closest: () => null,
  });
  Object.defineProperties(alertEl, {
    innerText: { get: () => bannerTxt },
    textContent: { get: () => bannerTxt },
  });
  const doc = {
    location: { href: "https://chatgpt.com/c/abc-123" },
    activeElement: null,
    createElement: (tag) => Object.assign(new MockElement(), { nodeType: 1, tagName: tag.toUpperCase(), textContent: "", appendChild() {} }),
    querySelector: (sel) => (sel.includes("prompt-textarea") ? editorEl : null),
    querySelectorAll: (sel) => {
      if (sel.includes("prompt-textarea")) return [editorEl];
      if (sel.includes("send-button")) return [sendBtn];
      if (sel.includes("alert")) return [alertEl];
      return [];
    },
  };
  const sb = {
    Element: MockElement,
    Event: class MockEvent { constructor(type, opts) { this.type = type; Object.assign(this, opts || {}); } },
    KeyboardEvent: class MockKeyboardEvent { constructor(type, opts) { this.type = type; Object.assign(this, opts || {}); } },
    window: { location: doc.location, getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }) },
    document: doc,
    location: doc.location,
    getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
    setTimeout: (fn) => { fn(); return 1; },
    clearTimeout: () => {},
    console,
  };
  sb.globalThis = sb;
  vm.runInNewContext("(" + bg._installWsaDomUtilsFn.toString() + ")()", sb);
  vm.runInNewContext("(" + bg._chatgptInstallPageRuntimeFn.toString() + ")()", sb);

  assert.strictEqual(sb.__wsaDomUtils.detectLimitMessage("The message you submitted was too long, please edit it and resubmit."), "");
  assert.strictEqual(sb.__wsaDomUtils.detectLimitMessage("Character limit exceeded."), "");
  assert.ok(sb.__wsaDomUtils.detectPromptTooLongMessage("The message you submitted was too long, please edit it and resubmit."));

  // Verify compacted replacement overwrites stale oversized prompt even when prefix matches
  const compactRetry = "### CURRENT ACTIVE USER REQUEST\nCompacted summary only.";
  sb.__wsaDomUtils.setEditablePromptText(editorEl, compactRetry);
  assert.strictEqual(curEditorText, compactRetry);

  const started = await vm.runInNewContext("(" + bg._chatgptStartTurnFn.toString() + ")('ptl-1', '### CURRENT ACTIVE USER REQUEST\\nHuge', [])", sb);
  assert.strictEqual(started.ok, true);
  assert.strictEqual(sentClicks, 1);

  const poll = vm.runInNewContext("(" + bg._chatgptPollTurnFn.toString() + ")('ptl-1')", sb);
  assert.strictEqual(poll.done, false);
  assert.ok(poll.error && poll.error.startsWith("[PROMPT_TOO_LONG] ChatGPT:"), poll.error);
}
run().catch((e) => { console.error(e); process.exit(1); });
"""
        proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)

    def test_all_providers_composer_send_retry_scoping_and_stuck_failfast_in_node_vm(self):
        import subprocess
        bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
        bg_src = Path(bg_path).read_text(encoding="utf-8")
        self.assertIn("reloadedForRelay", bg_src)
        self.assertIn("hasInteractiveEditor", bg_src)

        js = "const bgPath = " + json.dumps(bg_path) + ";\n" + r"""
const assert = require("assert");
const vm = require("vm");
const bg = require(bgPath);

class MockEl {
  constructor(tag, attrs = {}, cls = "", txt = "") {
    this.nodeType = 1;
    this.tagName = String(tag || "div").toUpperCase();
    this.attrs = Object.assign({}, attrs);
    this.id = this.attrs.id || "";
    this.className = cls;
    this._text = String(txt ?? "");
    this.children = [];
    this.childNodes = this._text ? [{ nodeType: 3, textContent: this._text, nodeValue: this._text }] : [];
    this.parentElement = null;
    this.parentNode = null;
    this.disabled = false;
    this.readOnly = false;
    this.isContentEditable = this.attrs.contenteditable === "true";
    this.isConnected = true;
    this.value = "";
    this._onClick = null;
  }
  get textContent() {
    if (this.children.length) return this.children.map(c => c.textContent || "").join("\n");
    return this._text;
  }
  set textContent(v) {
    this.children = [];
    this._text = String(v ?? "");
    this.childNodes = this._text ? [{ nodeType: 3, textContent: this._text, nodeValue: this._text }] : [];
  }
  get innerText() { return this.textContent; }
  set innerText(v) { this.textContent = v; }
  appendChild(ch) {
    if (ch && typeof ch === "object") { ch.parentElement = this; ch.parentNode = this; }
    this.children.push(ch);
    this.childNodes.push(ch);
    return ch;
  }
  replaceChildren(...nodes) {
    this.children = [];
    this.childNodes = [];
    for (const n of nodes) this.appendChild(n);
  }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    if (k === "id") this.id = String(v);
    if (k === "contenteditable") this.isContentEditable = String(v) === "true";
  }
  focus() {}
  scrollIntoView() {}
  dispatchEvent() { return true; }
  click() { if (typeof this._onClick === "function") this._onClick(this); }
  getBoundingClientRect() { return { width: 220, height: 40, top: 0, left: 0, bottom: 40, right: 220 }; }
  getClientRects() { return [this.getBoundingClientRect()]; }
  get offsetParent() { return this.parentElement || {}; }
  get classList() {
    const parts = String(this.className || "").split(/\s+/).filter(Boolean);
    return { contains: (c) => parts.includes(c) };
  }
  closest(sel) {
    let cur = this;
    while (cur) {
      if (cur.matches(sel)) return cur;
      cur = cur.parentElement;
    }
    return null;
  }
  matches(sel) {
    return String(sel).split(",").some((part) => {
      const p = part.trim();
      if (!p) return false;
      if (p === "form") return this.tagName === "FORM";
      if (p === "button") return this.tagName === "BUTTON";
      if (p === "textarea") return this.tagName === "TEXTAREA";
      if (p.includes("button[data-testid='send-button']")) return this.tagName === "BUTTON" && this.attrs["data-testid"] === "send-button";
      if (p.includes("#composer-submit-button")) return this.id === "composer-submit-button";
      if (p.includes("button[aria-label='Send']")) return this.tagName === "BUTTON" && this.attrs["aria-label"] === "Send";
      if (p.includes("button[aria-label*='Send' i]") || p.includes("button[aria-label*='Send message' i]")) {
        return this.tagName === "BUTTON" && /send/i.test(this.attrs["aria-label"] || "");
      }
      if (p.includes("button[type='submit']")) return this.tagName === "BUTTON" && this.attrs.type === "submit";
      if (p.includes("bg-composer-primary")) return this.tagName === "BUTTON" && this.className.includes("bg-composer-primary");
      if (p.includes("ds-button--primary")) return this.className.includes("ds-button--primary");
      if (p.includes("[class*='composer' i]")) return /composer/i.test(this.className);
      if (
        p === "div.ProseMirror[contenteditable='true']" ||
        p === "[contenteditable='true'][role='textbox']" ||
        p === "[role='textbox'][contenteditable='true']" ||
        p === "div[role='textbox']" ||
        p === "div[contenteditable='true']"
      ) {
        return this.attrs.contenteditable === "true" || this.attrs.role === "textbox";
      }
      if (p === "#prompt-textarea" || p === "#prompt-textarea[contenteditable='true']") return this.id === "prompt-textarea";
      if (p === "[data-testid='chat-input']") return this.attrs["data-testid"] === "chat-input";
      return false;
    });
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => {
      if (!n || !Array.isArray(n.children)) return;
      for (const ch of n.children) {
        if (ch.nodeType === 1 && ch.matches(sel)) out.push(ch);
        walk(ch);
      }
    };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

function makeSandbox(root, url, fakeNowRef, syncTimeout = false) {
  const sb = {
    Element: MockEl,
    Node: { ELEMENT_NODE: 1, TEXT_NODE: 3 },
    Date: Object.assign(function() { return new Date(fakeNowRef.val); }, { now: () => fakeNowRef.val }),
    location: { href: url, pathname: new URL(url).pathname },
    getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
    getSelection: () => ({ removeAllRanges: () => {}, addRange: () => {} }),
    InputEvent: function(t, o) { this.type = t; Object.assign(this, o || {}); },
    Event: function(t, o) { this.type = t; Object.assign(this, o || {}); },
    KeyboardEvent: function(t, o) { this.type = t; Object.assign(this, o || {}); },
    setTimeout: syncTimeout ? ((fn) => { fn(); return 1; }) : setTimeout,
    clearTimeout: () => {},
    console: { log: () => {}, warn: () => {}, error: () => {} },
    document: {
      createRange: () => ({ selectNodeContents: () => {}, setStartAfter: () => {}, setEnd: () => {} }),
      execCommand: () => false,
      querySelector: (s) => root.querySelector(s),
      querySelectorAll: (s) => root.querySelectorAll(s),
      createElement: (t) => new MockEl(t),
      createTextNode: (s) => ({ nodeType: 3, textContent: String(s), nodeValue: String(s) }),
    },
  };
  sb.window = sb;
  sb.globalThis = sb;
  return sb;
}

async function run() {
  // 1) ChatGPT: decoy Start Voice + modal Send skipped; swallowed click #1 recovers on click #2!
  {
    const nowRef = { val: 10000 };
    const root = new MockEl("div");
    const decoyModal = new MockEl("form");
    const decoyVoice = new MockEl("button", { "aria-label": "Start Voice" }, "bg-composer-primary");
    decoyModal.appendChild(decoyVoice);
    const form = new MockEl("form");
    const ed = new MockEl("div", { id: "prompt-textarea", contenteditable: "true", role: "textbox" }, "ProseMirror");
    let clicks = 0;
    const realSend = new MockEl("button", { id: "composer-submit-button", type: "submit" }, "bg-composer-primary");
    realSend._onClick = () => {
      clicks += 1;
      if (clicks >= 2) {
        // Click #2 clears composer and starts response turn
        ed.textContent = "";
      }
    };
    form.appendChild(ed);
    form.appendChild(realSend);
    root.appendChild(decoyModal);
    root.appendChild(form);

    const sb = makeSandbox(root, "https://chatgpt.com/c/cg-retry", nowRef, true);
    vm.runInNewContext("(" + bg._installWsaDomUtilsFn.toString() + ")()", sb);
    vm.runInNewContext("(" + bg._chatgptInstallPageRuntimeFn.toString() + ")()", sb);

    assert.strictEqual(sb.__wsaChatgptRuntime.findSendButton(ed), realSend);
    const st = await vm.runInNewContext("(" + bg._chatgptStartTurnFn.toString() + ")('cg-r1', '### Active Stage Role Update\\nStress test now')", sb);
    assert.strictEqual(st.ok, true);
    assert.strictEqual(clicks, 2, "Must retry automatically when click #1 leaves prompt unsent");
    assert.strictEqual(ed.textContent.trim(), "", "Composer cleared after retry succeeded");
  }

  // 2) ChatGPT & DeepSeek stuck composer across 3 pollTurn retries fails fast at 14s instead of hanging 45s
  {
    const nowRef = { val: 20000 };
    const root = new MockEl("div");
    const form = new MockEl("form");
    const ta = new MockEl("textarea", { id: "chat-input" });
    let clicks = 0;
    const sendCircle = new MockEl("div", { role: "button", "aria-label": "Send" }, "ds-button ds-button--primary ds-button--circle");
    sendCircle.click = () => { clicks += 1; };
    form.appendChild(ta);
    form.appendChild(sendCircle);
    root.appendChild(form);

    const sb = makeSandbox(root, "https://chat.deepseek.com/a/chat/s/ds-stuck", nowRef, true);
    vm.runInNewContext("(" + bg._installWsaDomUtilsFn.toString() + ")()", sb);
    vm.runInNewContext("(" + bg._deepseekInstallPageRuntimeFn.toString() + ")()", sb);

    const st = await vm.runInNewContext("(" + bg._deepseekStartTurnFn.toString() + ")('ds-s1', 'DeepSeek prompt stuck check')", sb);
    assert.strictEqual(st.ok, true);
    const clicksAfterStart = clicks;
    nowRef.val = 22000;
    let p = vm.runInNewContext("(" + bg._deepseekPollTurnFn.toString() + ")('ds-s1')", sb);
    assert.strictEqual(p.done, false);
    assert.ok(!p.error);
    assert.strictEqual(clicks, clicksAfterStart + 1);
    nowRef.val = 24000;
    vm.runInNewContext("(" + bg._deepseekPollTurnFn.toString() + ")('ds-s1')", sb);
    nowRef.val = 26000;
    vm.runInNewContext("(" + bg._deepseekPollTurnFn.toString() + ")('ds-s1')", sb);
    nowRef.val = 34500;
    p = vm.runInNewContext("(" + bg._deepseekPollTurnFn.toString() + ")('ds-s1')", sb);
    assert.strictEqual(p.done, false);
    assert(String(p.error || "").includes("remained unsubmitted in composer"), p.error);
  }

  // 3) Claude: ignores global Send feedback button, retries swallowed click, fails fast if stuck at 14s
  {
    const nowRef = { val: 30000 };
    const root = new MockEl("div");
    const feedbackBtn = new MockEl("button", { "aria-label": "Send feedback" });
    root.appendChild(feedbackBtn);
    const comp = new MockEl("form", {}, "composer");
    const ed = new MockEl("div", { "data-testid": "chat-input", contenteditable: "true", role: "textbox" }, "ProseMirror");
    let clicks = 0;
    const realSend = new MockEl("button", { "aria-label": "Send message", type: "submit" });
    realSend._onClick = () => { clicks += 1; };
    comp.appendChild(ed);
    comp.appendChild(realSend);
    root.appendChild(comp);

    const sb = makeSandbox(root, "https://claude.ai/chat/c-retry", nowRef, true);
    vm.runInNewContext("(" + bg._claudeInstallPageRuntimeFn.toString() + ")()", sb);

    assert.strictEqual(sb.__wsaClaudeRuntime.findSendButton(ed), realSend);
    const st = await vm.runInNewContext("(" + bg._claudeStartTurnFn.toString() + ")('cl-r1', 'Claude stuck check prompt')", sb);
    assert.strictEqual(st.ok, true);
    assert.ok(clicks >= 2);
    nowRef.val = 32000; vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('cl-r1')", sb);
    nowRef.val = 34000; vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('cl-r1')", sb);
    nowRef.val = 36000; vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('cl-r1')", sb);
    nowRef.val = 44500;
    const p = vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('cl-r1')", sb);
    assert.strictEqual(p.done, false);
    assert(String(p.error || "").includes("remained unsubmitted in composer"), p.error);
  }

  // 4) Gemini: DOM retries swallowed click cleanly when prompt stays in editor & fails fast at 14s
  {
    const nowRef = { val: 40000 };
    const root = new MockEl("div");
    const feedbackBtn = new MockEl("button", { "aria-label": "Send feedback" });
    root.appendChild(feedbackBtn);
    const form = new MockEl("form");
    const ed = new MockEl("div", { role: "textbox", contenteditable: "true" }, "ql-editor");
    let clicks = 0;
    const sendBtn = new MockEl("button", { "aria-label": "Send message" }, "send-button");
    sendBtn._onClick = () => { clicks += 1; };
    form.appendChild(ed);
    form.appendChild(sendBtn);
    root.appendChild(form);

    const sb = makeSandbox(root, "https://gemini.google.com/app/g-retry", nowRef, true);
    vm.runInNewContext("(" + bg._domInstallPageRuntimeFn.toString() + ")()", sb);

    const st = await vm.runInNewContext("(" + bg._domStartTurnFn.toString() + ")('gm-r1', 'Gemini stuck check prompt')", sb);
    assert.strictEqual(st.ok, true);
    assert.ok(clicks >= 2, "Gemini DOM submit must retry if editor still contains prompt");
    const beforePollClicks = clicks;
    nowRef.val = 42000; vm.runInNewContext("(" + bg._domPollTurnFn.toString() + ")('gm-r1')", sb);
    assert.strictEqual(clicks, beforePollClicks + 1);
    nowRef.val = 44000; vm.runInNewContext("(" + bg._domPollTurnFn.toString() + ")('gm-r1')", sb);
    nowRef.val = 46000; vm.runInNewContext("(" + bg._domPollTurnFn.toString() + ")('gm-r1')", sb);
    nowRef.val = 54500;
    const p = vm.runInNewContext("(" + bg._domPollTurnFn.toString() + ")('gm-r1')", sb);
    assert.strictEqual(p.done, false);
    assert(String(p.error || "").includes("remained unsubmitted in composer"), p.error);
  }
  console.log("ALL_4_PROVIDERS_SEND_RETRY_OK");
}
run().catch((e) => { console.error(e); process.exit(1); });
"""
        proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        self.assertIn("ALL_4_PROVIDERS_SEND_RETRY_OK", proc.stdout)

    def test_all_providers_prosemirror_normalization_ancestors_rebase_and_sw_identity(self):
        import subprocess
        bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()

        js = "const bgPath = " + json.dumps(bg_path) + ";\n" + r"""
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const bg = require(bgPath);
const bgSrc = fs.readFileSync(bgPath, "utf8");

class MockDomEl {
  constructor(tag, attrs = {}, cls = "", txt = "") {
    this.nodeType = 1;
    this.tagName = String(tag || "div").toUpperCase();
    this.attrs = Object.assign({}, attrs);
    this.id = this.attrs.id || "";
    this.className = cls;
    this._text = String(txt ?? "");
    this.children = [];
    this.childNodes = this._text ? [{ nodeType: 3, textContent: this._text, nodeValue: this._text }] : [];
    this.parentElement = null;
    this.parentNode = null;
    this.disabled = false;
    this.readOnly = false;
    this.value = "";
    this.isContentEditable = this.attrs.contenteditable === "true";
    this.isConnected = true;
    this.style = { display: "", visibility: "", opacity: "" };
    this._listeners = {};
    this._onClick = null;
  }
  get hidden() { return this.hasAttribute("hidden"); }
  set hidden(v) { if (v) this.setAttribute("hidden", ""); else this.removeAttribute("hidden"); }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); }
  getAttribute(k) { return this.hasAttribute(k) ? this.attrs[k] : null; }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    if (k === "id") this.id = String(v);
    if (k === "contenteditable") this.isContentEditable = String(v) === "true";
  }
  removeAttribute(k) {
    delete this.attrs[k];
    if (k === "id") this.id = "";
    if (k === "contenteditable") this.isContentEditable = false;
  }
  get textContent() {
    if (this.children.length) return this.children.map(c => c.textContent || "").join("\n");
    return this._text;
  }
  set textContent(v) {
    this.children = [];
    this._text = String(v ?? "");
    this.childNodes = this._text ? [{ nodeType: 3, textContent: this._text, nodeValue: this._text }] : [];
  }
  get innerText() { return this.textContent; }
  set innerText(v) { this.textContent = v; }
  appendChild(ch) {
    if (ch && typeof ch === "object") { ch.parentElement = this; ch.parentNode = this; }
    this.children.push(ch);
    this.childNodes.push(ch);
    return ch;
  }
  replaceChildren(...nodes) {
    this.children = [];
    this.childNodes = [];
    for (const n of nodes) this.appendChild(n);
  }
  addEventListener(t, fn) {
    if (!this._listeners[t]) this._listeners[t] = [];
    this._listeners[t].push(fn);
  }
  dispatchEvent(ev) {
    const list = this._listeners[ev && ev.type] || [];
    for (const fn of list) fn(ev);
    return true;
  }
  focus() {}
  scrollIntoView() {}
  click() { if (typeof this._onClick === "function") this._onClick(this); }
  getBoundingClientRect() { return { width: 220, height: 40, top: 0, left: 0, bottom: 40, right: 220 }; }
  getClientRects() { return [this.getBoundingClientRect()]; }
  get offsetParent() { return this.parentElement || {}; }
  get classList() {
    const parts = String(this.className || "").split(/\s+/).filter(Boolean);
    return { contains: (c) => parts.includes(c) };
  }
  closest(sel) {
    let cur = this;
    while (cur) {
      if (cur.matches(sel)) return cur;
      cur = cur.parentElement;
    }
    return null;
  }
  contains(other) {
    let cur = other;
    while (cur) {
      if (cur === this) return true;
      cur = cur.parentElement;
    }
    return false;
  }
  _matchesSingle(part) {
    const tag = this.tagName.toLowerCase();
    const cls = String(this.className || "").split(/\s+/).filter(Boolean);
    const aria = String(this.attrs["aria-label"] || "").toLowerCase();
    const tid = String(this.attrs["data-testid"] || "").toLowerCase();
    const dtid = String(this.attrs["data-test-id"] || "").toLowerCase();
    if (part === "form") return tag === "form";
    if (part === "rich-textarea") return tag === "rich-textarea";
    if (part === "model-response") return tag === "model-response";
    if (part === "message-content") return tag === "message-content";
    if (part === "message-actions") return tag === "message-actions";
    if (part === "button") return tag === "button";
    if (part === "textarea" || part === "textarea#chat-input") return tag === "textarea";
    if (part === "div[role='button']" || part === "div[role='button'].ds-button--primary.ds-button--circle") {
      return tag === "div" && this.attrs.role === "button";
    }
    if (part === "#prompt-textarea" || part === "#prompt-textarea[contenteditable='true']") return this.id === "prompt-textarea";
    if (part === "[data-testid='chat-input']") return tid === "chat-input";
    if (part === "button[data-testid='send-button']" || part === "[data-testid='send-button']") return tag === "button" && tid === "send-button";
    if (part === "button[aria-label*='Send' i]" || part === "button[aria-label*='Send message' i]") return tag === "button" && aria.includes("send");
    if (part === "button[type='submit']") return tag === "button" && this.attrs.type === "submit";
    if (part === "button.send-button") return tag === "button" && cls.includes("send-button");
    if (part === "div.ProseMirror[contenteditable='true']" || part === "[contenteditable='true'][role='textbox']" || part === "[role='textbox'][contenteditable='true']" || part === "rich-textarea .ql-editor") {
      return this.attrs.contenteditable === "true" || cls.includes("ql-editor");
    }
    if (part === "[data-message-author-role='assistant']") return this.attrs["data-message-author-role"] === "assistant";
    if (part === "article[data-testid^='conversation-turn-']") return tag === "article" && tid.startsWith("conversation-turn-");
    if (part === "[data-cds='AssistantMessage']" || part === "[data-testid='assistant-message']") {
      return this.attrs["data-cds"] === "AssistantMessage" || tid === "assistant-message";
    }
    if (part === "[data-cds='MessageActions']") return this.attrs["data-cds"] === "MessageActions";
    if (part === ".markdown" || part === ".standard-markdown" || part === ".ds-markdown" || part === ".ds-message") {
      return cls.includes(part.slice(1));
    }
    if (part === "button[aria-label='Copy']" || part === "button[aria-label*='Copy' i]" || part === "button[aria-label*='Copy response' i]" || part === "message-actions button") {
      return tag === "button" && aria.includes("copy");
    }
    if (part.includes(" ") && part.endsWith(" button")) {
      return tag === "button";
    }
    if (dtid && part.includes(dtid)) return true;
    return false;
  }
  matches(sel) {
    return String(sel || "").split(",").map(s => s.trim()).filter(Boolean).some(p => this._matchesSingle(p));
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => {
      if (!n || !Array.isArray(n.children)) return;
      for (const ch of n.children) {
        if (ch.nodeType === 1 && ch.matches(sel)) out.push(ch);
        walk(ch);
      }
    };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

function makeDomSandbox(root, url, nowRef, onExecInsert) {
  const doc = {
    createRange: () => ({ selectNodeContents: () => {}, setStartAfter: () => {}, setEnd: () => {}, collapse: () => {} }),
    execCommand: (cmd, _ui, val) => (cmd === "insertText" && typeof onExecInsert === "function" ? onExecInsert(val) : false),
    querySelector: (s) => root.querySelector(s),
    querySelectorAll: (s) => root.querySelectorAll(s),
    createElement: (t) => new MockDomEl(t),
    createTextNode: (s) => ({ nodeType: 3, textContent: String(s), nodeValue: String(s) }),
  };
  const sb = {
    Element: MockDomEl,
    Node: { ELEMENT_NODE: 1, TEXT_NODE: 3 },
    Date: Object.assign(function() { return new Date(nowRef.val); }, { now: () => nowRef.val }),
    location: { href: url, pathname: new URL(url).pathname },
    getComputedStyle: (el) => ({
      display: (el && el.style && el.style.display) || "block",
      visibility: (el && el.style && el.style.visibility) || "visible",
      opacity: (el && el.style && el.style.opacity) || "1",
    }),
    getSelection: () => ({ removeAllRanges: () => {}, addRange: () => {} }),
    InputEvent: function(t, o) { this.type = t; Object.assign(this, o || {}); },
    PointerEvent: function(t, o) { this.type = t; Object.assign(this, o || {}); },
    MouseEvent: function(t, o) { this.type = t; Object.assign(this, o || {}); },
    Event: function(t, o) { this.type = t; Object.assign(this, o || {}); },
    KeyboardEvent: function(t, o) { this.type = t; Object.assign(this, o || {}); },
    setTimeout: (fn) => { fn(); return 1; },
    clearTimeout: () => {},
    console: { log: () => {}, warn: () => {}, error: () => {} },
    document: doc,
  };
  sb.window = sb;
  sb.globalThis = sb;
  return sb;
}

async function run() {
  const promptMulti = "Header\ntail\n\n\nPara 2 headline\n\nFooter\ntail";

  // 1) Normalized execCommand preservation (no replaceChildren clobber), hidden ancestors, Expand exclusion, pointer+Enter retry, and historical rebase: ChatGPT
  {
    const nowRef = { val: 1000 };
    const root = new MockDomEl("div");
    const hiddenAnc = new MockDomEl("div", { "aria-hidden": "true" });
    hiddenAnc.appendChild(new MockDomEl("button", { "data-testid": "send-button" }));
    const dispAnc = new MockDomEl("div");
    dispAnc.style.display = "none";
    dispAnc.appendChild(new MockDomEl("button", { "data-testid": "send-button" }));
    const history = new MockDomEl("div");
    const form = new MockDomEl("form");
    let requestSubmits = 0;
    let formEnters = 0;
    form.requestSubmit = () => { requestSubmits += 1; };
    const ed = new MockDomEl("div", { id: "prompt-textarea", contenteditable: "true", role: "textbox" }, "ProseMirror");
    ed.addEventListener("keydown", (e) => { if (e.key === "Enter") formEnters += 1; });
    let replaceCalls = 0;
    const origReplace = ed.replaceChildren.bind(ed);
    ed.replaceChildren = (...args) => { replaceCalls += 1; return origReplace(...args); };
    const expandBtn = new MockDomEl("button", { "aria-label": "Expand" }, "", "Expand");
    const realSend = new MockDomEl("button", { "data-testid": "send-button", "aria-label": "Send" });
    let pointerEvents = 0;
    let clicks = 0;
    realSend.addEventListener("pointerdown", () => { pointerEvents += 1; });
    realSend._onClick = () => { clicks += 1; };
    form.appendChild(expandBtn);
    form.appendChild(ed);
    form.appendChild(realSend);
    root.appendChild(hiddenAnc);
    root.appendChild(dispAnc);
    root.appendChild(history);
    root.appendChild(form);

    const sb = makeDomSandbox(root, "https://chatgpt.com/c/abc", nowRef, (val) => {
      // Simulate Chrome ProseMirror expanding blank lines between <p> nodes in innerText
      const paras = String(val).split(/\n{2,}/).map(chunk => new MockDomEl("p", {}, "", chunk + "\n"));
      origReplace(...paras);
      return true;
    });
    vm.runInNewContext("(" + bg._installWsaDomUtilsFn.toString() + ")()", sb);
    vm.runInNewContext("(" + bg._chatgptInstallPageRuntimeFn.toString() + ")()", sb);
    assert.strictEqual(sb.__wsaDomUtils.version, 3);
    assert.strictEqual(sb.__wsaDomUtils.rev, 10);
    assert.strictEqual(sb.__wsaChatgptRuntime.version, 3);
    assert.strictEqual(sb.__wsaChatgptRuntime.rev, 8);
    assert.strictEqual(sb.__wsaChatgptRuntime.findSendButton(ed), realSend);

    sb.__wsaDomUtils.setEditablePromptText(ed, promptMulti);
    assert.strictEqual(replaceCalls, 0, "Normalized match must NOT clobber execCommand <p> nodes via replaceChildren");
    assert.strictEqual(sb.__wsaDomUtils.isPromptStillInEditor(ed, promptMulti), true);

    // Trigger requestSubmit without an extra Enter cleanly when sendBtn omitted temporarily
    const formNoBtn = new MockDomEl("form");
    formNoBtn.requestSubmit = () => { requestSubmits += 1; };
    const bareEd = new MockDomEl("div", { id: "bare", contenteditable: "true" });
    let bareEnters = 0;
    bareEd.addEventListener("keydown", (e) => { if (e.key === "Enter") bareEnters += 1; });
    formNoBtn.appendChild(bareEd);
    sb.__wsaDomUtils.triggerSendSubmission({ findEditor: () => bareEd, findSendButton: () => null }, bareEd, true);
    assert.strictEqual(requestSubmits, 1);
    assert.strictEqual(bareEnters, 0);

    // Start turn (click #1 + retry pointerdown+click #2 leave prompt in editor for rebase test)
    const st = await vm.runInNewContext("(" + bg._chatgptStartTurnFn.toString() + ")('cg-hist', " + JSON.stringify(promptMulti) + ", [])", sb);
    assert.strictEqual(st.ok, true);
    assert.strictEqual(pointerEvents >= 1, true);

    // Historical hydrated turn arrives while prompt stays unsent in editor -> must rebase, not return stale text
    const staleTurn = new MockDomEl("div", { "data-message-author-role": "assistant" });
    staleTurn.appendChild(new MockDomEl("div", {}, "markdown", "STALE_HISTORICAL_31756"));
    staleTurn.appendChild(new MockDomEl("button", { "aria-label": "Copy" }));
    history.appendChild(staleTurn);
    const pStale = vm.runInNewContext("(" + bg._chatgptPollTurnFn.toString() + ")('cg-hist')", sb);
    assert.strictEqual(pStale.done, false);
    assert.strictEqual(pStale.text, "");
    assert.strictEqual(sb.__wsaChatgptRuntime.turns["cg-hist"].beforeTurnCount, 1);

    // Real fresh turn then submits & completes cleanly
    ed.textContent = "";
    const freshTurn = new MockDomEl("div", { "data-message-author-role": "assistant" });
    freshTurn.appendChild(new MockDomEl("div", {}, "markdown", "FRESH_CHATGPT_TURN"));
    freshTurn.appendChild(new MockDomEl("button", { "aria-label": "Copy" }));
    history.appendChild(freshTurn);
    nowRef.val = 2000;
    vm.runInNewContext("(" + bg._chatgptPollTurnFn.toString() + ")('cg-hist')", sb);
    nowRef.val = 2600;
    const pDone = vm.runInNewContext("(" + bg._chatgptPollTurnFn.toString() + ")('cg-hist')", sb);
    assert.strictEqual(pDone.done, true);
    assert.strictEqual(pDone.text, "FRESH_CHATGPT_TURN");
  }

  // 2) Claude: rev 4, normalized ProseMirror check, hidden/Expand filtering, and historical rebase
  {
    const nowRef = { val: 5000 };
    const root = new MockDomEl("div");
    const hid = new MockDomEl("div", { hidden: "" });
    hid.appendChild(new MockDomEl("button", { "aria-label": "Send message" }));
    const history = new MockDomEl("div");
    const form = new MockDomEl("form");
    const ed = new MockDomEl("div", { "data-testid": "chat-input", contenteditable: "true", role: "textbox" }, "ProseMirror");
    let replaceCalls = 0;
    const origReplace = ed.replaceChildren.bind(ed);
    ed.replaceChildren = (...args) => { replaceCalls += 1; return origReplace(...args); };
    const expBtn = new MockDomEl("button", { "aria-label": "Expand" });
    const sendBtn = new MockDomEl("button", { "aria-label": "Send message" });
    let ptrs = 0;
    sendBtn.addEventListener("pointerdown", () => { ptrs += 1; });
    form.appendChild(expBtn);
    form.appendChild(ed);
    form.appendChild(sendBtn);
    root.appendChild(hid);
    root.appendChild(history);
    root.appendChild(form);

    const sb = makeDomSandbox(root, "https://claude.ai/chat/c1", nowRef, (val) => {
      origReplace(new MockDomEl("p", {}, "", String(val) + "\n\n"));
      return true;
    });
    vm.runInNewContext("(" + bg._installWsaDomUtilsFn.toString() + ")()", sb);
    vm.runInNewContext("(" + bg._claudeInstallPageRuntimeFn.toString() + ")()", sb);
    assert.strictEqual(sb.__wsaClaudeRuntime.version, 4);
    assert.strictEqual(sb.__wsaClaudeRuntime.rev, 8);
    assert.strictEqual(sb.__wsaClaudeRuntime.findSendButton(ed), sendBtn);

    sb.__wsaClaudeRuntime.setPromptText(ed, promptMulti);
    assert.strictEqual(replaceCalls, 0);
    assert.strictEqual(sb.__wsaClaudeRuntime.isPromptStillInEditor(ed, promptMulti), true);

    await vm.runInNewContext("(" + bg._claudeStartTurnFn.toString() + ")('cl-h', " + JSON.stringify(promptMulti) + ", [])", sb);
    assert.strictEqual(ptrs >= 1, true);
    const staleCl = new MockDomEl("div", { "data-cds": "AssistantMessage" });
    staleCl.appendChild(new MockDomEl("div", {}, "standard-markdown", "STALE_CLAUDE"));
    const actsCl = new MockDomEl("div", { "data-cds": "MessageActions" });
    actsCl.appendChild(new MockDomEl("button", { "aria-label": "Copy" }));
    staleCl.appendChild(actsCl);
    history.appendChild(staleCl);
    const pStale = vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('cl-h')", sb);
    assert.strictEqual(pStale.done, false);
    assert.strictEqual(sb.__wsaClaudeRuntime.turns["cl-h"].beforeTurnCount, 1);

    ed.textContent = "";
    const freshCl = new MockDomEl("div", { "data-cds": "AssistantMessage" });
    freshCl.appendChild(new MockDomEl("div", {}, "standard-markdown", "FRESH_CLAUDE_TURN"));
    const actsCl2 = new MockDomEl("div", { "data-cds": "MessageActions" });
    actsCl2.appendChild(new MockDomEl("button", { "aria-label": "Copy" }));
    freshCl.appendChild(actsCl2);
    history.appendChild(freshCl);
    nowRef.val = 5500;
    vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('cl-h')", sb);
    nowRef.val = 6100;
    const pDone = vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('cl-h')", sb);
    assert.strictEqual(pDone.done, true);
    assert.strictEqual(pDone.text, "FRESH_CLAUDE_TURN");
  }

  // 3) DeepSeek: rev 4, Expand skip, historical rebase
  {
    const nowRef = { val: 8000 };
    const root = new MockDomEl("div");
    const history = new MockDomEl("div");
    const form = new MockDomEl("form");
    const ta = new MockDomEl("textarea", { id: "chat-input" });
    const expBtn = new MockDomEl("button", { "aria-label": "Expand" });
    const sendDiv = new MockDomEl("div", { role: "button", "aria-label": "Send" }, "ds-button ds-button--primary ds-button--circle");
    form.appendChild(expBtn);
    form.appendChild(ta);
    form.appendChild(sendDiv);
    root.appendChild(history);
    root.appendChild(form);

    const sb = makeDomSandbox(root, "https://chat.deepseek.com/a/chat/s/d1", nowRef);
    vm.runInNewContext("(" + bg._installWsaDomUtilsFn.toString() + ")()", sb);
    vm.runInNewContext("(" + bg._deepseekInstallPageRuntimeFn.toString() + ")()", sb);
    assert.strictEqual(sb.__wsaDeepseekRuntime.version, 3);
    assert.strictEqual(sb.__wsaDeepseekRuntime.rev, 7);
    assert.strictEqual(sb.__wsaDeepseekRuntime.findSendButton(ta), sendDiv);

    await vm.runInNewContext("(" + bg._deepseekStartTurnFn.toString() + ")('ds-h', 'DeepSeek prompt', [])", sb);
    const staleDs = new MockDomEl("div", {}, "ds-message");
    staleDs.appendChild(new MockDomEl("div", {}, "ds-markdown", "STALE_DEEPSEEK"));
    staleDs.appendChild(new MockDomEl("button", { "aria-label": "Copy" }));
    history.appendChild(staleDs);
    const pStale = vm.runInNewContext("(" + bg._deepseekPollTurnFn.toString() + ")('ds-h')", sb);
    assert.strictEqual(pStale.done, false);
    assert.strictEqual(sb.__wsaDeepseekRuntime.turns["ds-h"].beforeTurnCount, 1);

    ta.value = "";
    sendDiv.disabled = true;
    const freshDs = new MockDomEl("div", {}, "ds-message");
    freshDs.appendChild(new MockDomEl("div", {}, "ds-markdown", "FRESH_DEEPSEEK_TURN"));
    freshDs.appendChild(new MockDomEl("button", { "aria-label": "Copy" }));
    history.appendChild(freshDs);
    nowRef.val = 8500;
    vm.runInNewContext("(" + bg._deepseekPollTurnFn.toString() + ")('ds-h')", sb);
    nowRef.val = 9100;
    const pDone = vm.runInNewContext("(" + bg._deepseekPollTurnFn.toString() + ")('ds-h')", sb);
    assert.strictEqual(pDone.done, true);
    assert.strictEqual(pDone.text, "FRESH_DEEPSEEK_TURN");
  }

  // 4) Gemini: rev 4, normalized setPromptTextInEditor, hidden/Expand skip, and historical rebase
  {
    const nowRef = { val: 12000 };
    const root = new MockDomEl("div");
    const hidAnc = new MockDomEl("div", { "aria-hidden": "true" });
    hidAnc.appendChild(new MockDomEl("button", { "aria-label": "Send message" }, "send-button"));
    const history = new MockDomEl("div");
    const rich = new MockDomEl("rich-textarea");
    const ed = new MockDomEl("div", { contenteditable: "true", role: "textbox" }, "ql-editor");
    let replaceCalls = 0;
    const origReplace = ed.replaceChildren.bind(ed);
    ed.replaceChildren = (...args) => { replaceCalls += 1; return origReplace(...args); };
    const expBtn = new MockDomEl("button", { "aria-label": "Expand" });
    const sendBtn = new MockDomEl("button", { "aria-label": "Send message" }, "send-button");
    rich.appendChild(expBtn);
    rich.appendChild(ed);
    rich.appendChild(sendBtn);
    root.appendChild(hidAnc);
    root.appendChild(history);
    root.appendChild(rich);

    const sb = makeDomSandbox(root, "https://gemini.google.com/app/g1", nowRef, (val) => {
      origReplace(new MockDomEl("p", {}, "", String(val) + "\n\n"));
      return true;
    });
    vm.runInNewContext("(" + bg._domInstallPageRuntimeFn.toString() + ")()", sb);
    assert.strictEqual(sb.__wsaRuntime.version, 2);
    assert.strictEqual(sb.__wsaRuntime.rev, 7);
    assert.strictEqual(sb.__wsaRuntime.findEnabledSendButton(ed), sendBtn);

    sb.__wsaRuntime.setPromptTextInEditor(ed, promptMulti);
    assert.strictEqual(replaceCalls, 0);
    assert.strictEqual(sb.__wsaRuntime.isPromptStillInEditor(ed, promptMulti), true);

    await vm.runInNewContext("(" + bg._domStartTurnFn.toString() + ")('gm-h', " + JSON.stringify(promptMulti) + ", [])", sb);
    const staleGm = new MockDomEl("model-response");
    staleGm.appendChild(new MockDomEl("message-content", {}, "", "STALE_GEMINI"));
    const gmActs = new MockDomEl("message-actions");
    gmActs.appendChild(new MockDomEl("button", { "aria-label": "Copy response" }));
    staleGm.appendChild(gmActs);
    history.appendChild(staleGm);
    const pStale = vm.runInNewContext("(" + bg._domPollTurnFn.toString() + ")('gm-h')", sb);
    assert.strictEqual(pStale.done, false);
    assert.strictEqual(pStale.text, "");
    assert.strictEqual(sb.__wsaActiveJob.beforeDomCount, 1);

    ed.textContent = "";
    const freshGm = new MockDomEl("model-response");
    freshGm.appendChild(new MockDomEl("message-content", {}, "", "FRESH_GEMINI_TURN"));
    const gmActs2 = new MockDomEl("message-actions");
    gmActs2.appendChild(new MockDomEl("button", { "aria-label": "Copy response" }));
    freshGm.appendChild(gmActs2);
    history.appendChild(freshGm);
    nowRef.val = 12500;
    vm.runInNewContext("(" + bg._domPollTurnFn.toString() + ")('gm-h')", sb);
    nowRef.val = 14200;
    const pDone = vm.runInNewContext("(" + bg._domPollTurnFn.toString() + ")('gm-h')", sb);
    assert.strictEqual(pDone.done, true);
    assert.strictEqual(pDone.text, "FRESH_GEMINI_TURN");
  }

  // 5) Service worker harness: same-tab email/modelBadge preservation, list_tabs isAiRelay across all 4 providers, rev=4 upgrade trigger
  {
    const tabs = [
      { id: 1, windowId: 1, active: true, url: "https://chatgpt.com/c/aaa", title: "ChatGPT", width: 1200, height: 800 },
      { id: 2, windowId: 1, active: false, url: "https://claude.ai/chat/bbb", title: "Claude", width: 1200, height: 800 },
      { id: 3, windowId: 1, active: false, url: "https://chat.deepseek.com/a/chat/s/ccc", title: "DeepSeek", width: 1200, height: 800 },
      { id: 4, windowId: 1, active: false, url: "https://gemini.google.com/app/ddd", title: "Gemini", width: 1200, height: 800 },
      { id: 5, windowId: 1, active: false, url: "https://example.com/docs", title: "Docs", width: 1200, height: 800 },
    ];
    let probeCall = 0;
    let execCheckCalls = 0;
    let postedCmdResult = null;
    const storageData = {};
    const sw = {
      console,
      URL,
      URLSearchParams,
      Date,
      Math,
      Map,
      Set,
      Promise,
      AbortSignal: { timeout: () => undefined },
      crypto: { randomUUID: () => "sw-test-1" },
      navigator: { userAgent: "Brave", userAgentData: { brands: [{ brand: "Brave" }] } },
      setTimeout: () => 0,
      clearTimeout: () => {},
      setInterval: () => 0,
      clearInterval: () => {},
      fetch: async (u, init) => {
        if (String(u).includes("/browser-command-result") && init && init.body) {
          postedCmdResult = JSON.parse(init.body).result;
        }
        return { ok: true, json: async () => ({ ok: true }) };
      },
      chrome: {
        runtime: { id: "sw", onMessage: { addListener: () => {} }, onConnect: { addListener: () => {} }, onInstalled: { addListener: () => {} }, onStartup: { addListener: () => {} } },
        alarms: { create: () => {}, clear: () => {}, onAlarm: { addListener: () => {} } },
        storage: {
          local: {
            get: async (k) => (typeof k === "string" ? (k in storageData ? { [k]: storageData[k] } : {}) : { ...storageData }),
            set: async (o) => Object.assign(storageData, o || {}),
            remove: async () => {},
          },
        },
        tabs: { query: async () => tabs, get: async (id) => tabs.find(t => t.id === id), update: async () => {}, onUpdated: { addListener: () => {} }, onRemoved: { addListener: () => {} } },
        cookies: { getAllCookieStores: async () => [{ id: "0" }], getAll: async () => [] },
        scripting: {
          executeScript: async ({ func, args }) => {
            const fnSrc = String(func || "");
            if (fnSrc.includes("targetProvider")) {
              probeCall += 1;
              return [{ result: probeCall === 1
                ? { provider: "chatgpt", isBrave: true, accountEmail: "user@example.com", modelBadge: "GPT-5" }
                : { provider: "chatgpt", isBrave: true, accountEmail: "", modelBadge: "ChatGPT" } }];
            }
            if (Array.isArray(args) && args.length === 4) {
              execCheckCalls += 1;
              const r = vm.runInNewContext("(" + func.toString() + ").apply(null, " + JSON.stringify(args) + ")", pageCtx);
              return [{ result: r }];
            }
            installedFns.push(func.name || "anon");
            const r = vm.runInNewContext("(" + func.toString() + ")()", pageCtx);
            return [{ result: r }];
          },
        },
      },
    };
    const pageCtx = {
      Element: MockDomEl,
      Node: { ELEMENT_NODE: 1, TEXT_NODE: 3 },
      document: { querySelectorAll: () => [], querySelector: () => null },
      window: null,
      __wsaDomUtils: { version: 3, detectPromptTooLongMessage: () => "" },
      __wsaChatgptRuntime: { version: 3 },
    };
    pageCtx.window = pageCtx;
    pageCtx.globalThis = pageCtx;
    const installedFns = [];
    sw.globalThis = sw;
    vm.runInNewContext(bgSrc, sw);

    const id1 = await sw.readLiveTabIdentity(tabs[0], { forceProbe: true });
    assert.strictEqual(id1.accountEmail, "user@example.com");
    assert.strictEqual(id1.modelBadge, "GPT-5");
    const id2 = await sw.readLiveTabIdentity(tabs[0], { forceProbe: true });
    assert.strictEqual(id2.accountEmail, "user@example.com", "Transient empty email probe on same tab must preserve email");
    assert.strictEqual(id2.modelBadge, "GPT-5", "Generic ChatGPT badge probe on same tab must preserve specific modelBadge");

    await sw.handleBrowserCommand({ cmd_id: "lt-1", action: "list_tabs" });
    assert(postedCmdResult && postedCmdResult.ok && Array.isArray(postedCmdResult.tabs));
    const byId = Object.fromEntries(postedCmdResult.tabs.map(t => [t.id, t]));
    assert.strictEqual(byId[1].isAiRelay, true);
    assert.strictEqual(byId[2].isAiRelay, true);
    assert.strictEqual(byId[3].isAiRelay, true);
    assert.strictEqual(byId[4].isAiRelay, true);
    assert.strictEqual(byId[5].isAiRelay, false);

    execCheckCalls = 0;
    await sw.ensurePageRuntime(1, "chatgpt");
    assert.strictEqual(execCheckCalls, 1);
    assert.deepStrictEqual(installedFns, ["_installWsaDomUtilsFn", "_chatgptInstallPageRuntimeFn"]);
    assert.strictEqual(pageCtx.__wsaDomUtils.rev, 10);
    assert.strictEqual(pageCtx.__wsaChatgptRuntime.rev, 8);

    installedFns.length = 0;
    await sw.ensurePageRuntime(1, "chatgpt");
    assert.strictEqual(execCheckCalls, 2);
    assert.deepStrictEqual(installedFns, [], "Upgraded rev=4 runtime must pass probe without reinstalling");
  }

  console.log("ALL_4_PROVIDERS_DEEP_HARDENING_OK");
}
run().catch((e) => { console.error(e); process.exit(1); });
"""
        proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        self.assertIn("ALL_4_PROVIDERS_DEEP_HARDENING_OK", proc.stdout)


class TestRichLiveLifecycleHardening(unittest.TestCase):
    """Ensure StreamRenderer & execute_agent_loop never collide or leak Rich Live displays."""

    @staticmethod
    def _make_strict_single_live_console():
        import io
        from rich.console import Console
        from rich.errors import LiveError

        buf = io.StringIO()
        strict = Console(file=buf, force_terminal=False, highlight=False, width=100)
        strict._strict_active = None

        orig_set_live = strict.set_live
        orig_clear_live = strict.clear_live

        def _strict_set_live(live_obj):
            if strict._strict_active is not None:
                raise LiveError("Only one live display may be active at once")
            strict._strict_active = live_obj
            return orig_set_live(live_obj)

        def _strict_clear_live():
            strict._strict_active = None
            return orig_clear_live()

        strict.set_live = _strict_set_live
        strict.clear_live = _strict_clear_live
        return strict, buf

    @staticmethod
    def _assert_console_clean(testcase: unittest.TestCase, target_console) -> None:
        testcase.assertIsNone(target_console._strict_active)
        testcase.assertIsNone(getattr(target_console, "_live", None))
        stack = getattr(target_console, "_live_stack", None)
        if stack is not None:
            testcase.assertEqual(len(stack), 0)

    def test_orphaned_answer_live_handoff_and_context_manager(self):
        from wsa_agent import renderer

        strict_console, _ = self._make_strict_single_live_console()
        with patch.object(renderer, "console", strict_console):
            sr1 = renderer.StreamRenderer(round_num=0)
            sr1.start_thinking()
            sr1.feed_thinking("Evaluating step 1")
            sr1.feed("Partial answer already transitioned out of thinking")
            self.assertFalse(sr1._in_thinking)
            self.assertIsNotNone(sr1._live)

            with renderer.StreamRenderer(round_num=1) as sr2:
                sr2.start_thinking()
                sr2.feed("Clean second stream")
                sr2.finish()

            self.assertIsNone(sr1._live)
            self.assertIsNone(sr2._live)
            self._assert_console_clean(self, strict_console)

    def test_execute_agent_loop_mid_answer_failover_and_abort_cleanup(self):
        import tempfile
        from wsa_agent import agent as agent_mod, renderer
        from wsa_agent.client import ProviderLimitError
        from wsa_agent.session import SessionState

        strict_console, _ = self._make_strict_single_live_console()
        with tempfile.TemporaryDirectory() as tmpdir:
            state = SessionState(project_root=Path(tmpdir), selected_model="claude")
            state.agent_mode = "multiagent"
            state.multiagent_queue = ["claude", "chatgpt"]
            state.multiagent_index = 0

            class _MidAnswerFailoverClient:
                def __init__(self):
                    self.calls = []

                def stream(self, **kwargs):
                    self.calls.append(kwargs.get("model"))
                    on_thinking = kwargs.get("on_thinking")
                    on_token = kwargs.get("on_token")
                    if len(self.calls) == 1:
                        if on_thinking:
                            on_thinking("Thinking before quota hit")
                        if on_token:
                            on_token("Started streaming answer before limit ")
                        raise ProviderLimitError("5-hour limit reached")
                    if on_token:
                        on_token("Recovered on failover model.")
                    return {"content": "Recovered on failover model.", "tool_calls": []}

            client = _MidAnswerFailoverClient()
            with patch.object(renderer, "console", strict_console):
                reply = agent_mod.execute_agent_loop(
                    "test mid-answer failover",
                    state,
                    client,
                    quiet_ui=False,
                    persist_to_state=False,
                )
            self.assertEqual(client.calls, ["claude", "chatgpt"])
            self.assertTrue(reply.get("ok"))
            self.assertEqual(reply.get("final_text"), "Recovered on failover model.")
            self._assert_console_clean(self, strict_console)

            class _MidAnswerConnErrorClient:
                def stream(self, **kwargs):
                    kwargs["on_thinking"]("Thinking...")
                    kwargs["on_token"]("Half an answer...")
                    raise ConnectionError("relay dropped mid-answer")

            state.agent_mode = "singleagent"
            state.selected_model = "claude"
            with patch.object(renderer, "console", strict_console):
                aborted_reply = agent_mod.execute_agent_loop(
                    "test mid-answer connection error",
                    state,
                    _MidAnswerConnErrorClient(),
                    quiet_ui=False,
                    persist_to_state=False,
                )
            self.assertIsInstance(aborted_reply, dict)
            self.assertFalse(aborted_reply.get("ok"))
            self.assertIn("relay dropped mid-answer", aborted_reply.get("aborted_reason", ""))
            self._assert_console_clean(self, strict_console)

    def test_concurrent_stream_renderers_never_raise_live_error(self):
        import threading
        from wsa_agent import renderer

        strict_console, _ = self._make_strict_single_live_console()
        errors = []
        barrier = threading.Barrier(6)

        def worker(tid: int):
            try:
                barrier.wait(timeout=5)
                for step in range(8):
                    with renderer.StreamRenderer(round_num=step, status_hint=f"w{tid}-{step}") as sr:
                        sr.start_thinking()
                        sr.feed_thinking(f"worker {tid} thinking {step}")
                        sr.set_status_hint(f"running {step}")
                        sr.feed(f"worker {tid} output {step}\n")
                        if step % 2 == 0:
                            sr.finish()
            except Exception as exc:
                errors.append(exc)

        with patch.object(renderer, "console", strict_console):
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

        self.assertEqual(errors, [])
        self._assert_console_clean(self, strict_console)


class TestPhase2ReliabilityHardening(unittest.TestCase):
    """Phase 2 reliability hardening: root locks, bounded memory, atomic /undo & save, relay crash recovery."""

    def test_concurrent_set_project_root_and_file_ops_isolation(self):
        import threading
        from wsa_agent import tools

        orig_root = tools.get_project_root()
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            r1 = Path(d1).resolve()
            r2 = Path(d2).resolve()
            (r1 / "shared.txt").write_text("root1\n", encoding="utf-8")
            (r2 / "shared.txt").write_text("root2\n", encoding="utf-8")
            errors = []
            barrier = threading.Barrier(5)

            def switcher():
                try:
                    barrier.wait(timeout=5)
                    for i in range(40):
                        tools.set_project_root(r1 if i % 2 == 0 else r2)
                except Exception as exc:
                    errors.append(("switch", repr(exc)))

            def worker(tid: int):
                try:
                    barrier.wait(timeout=5)
                    for i in range(25):
                        r = tools._read_file("shared.txt")
                        if r.startswith("[Error]"):
                            errors.append(("read", tid, r))
                        w = tools._write_file(f"w_{tid}.txt", f"v={i}\n", force_overwrite=True)
                        if not w.startswith("[OK]"):
                            errors.append(("write", tid, w))
                        ld = tools._list_dir(".")
                        if ld.startswith("[Error]"):
                            errors.append(("listdir", tid, ld))
                except Exception as exc:
                    errors.append(("worker", tid, repr(exc)))

            try:
                tools.set_project_root(r1)
                threads = [threading.Thread(target=switcher)] + [
                    threading.Thread(target=worker, args=(i,)) for i in range(4)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=15)
                self.assertEqual(errors, [])
            finally:
                tools.set_project_root(orig_root)

    def test_undo_last_edit_atomic_and_ledger_synced(self):
        import threading
        from wsa_agent import commands, tools
        from wsa_agent.session import SessionState

        orig_root = tools.get_project_root()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            try:
                tools.set_project_root(root)
                state = SessionState(project_root=root)

                res0 = tools._write_file("notes.py", "x = 0\n")
                self.assertIn("[OK]", res0)
                res1 = tools._write_file("notes.py", "x = 1\n", force_overwrite=True)
                self.assertIn("[OK]", res1)
                res2 = tools._write_file("notes.py", "x = 2\n", force_overwrite=True)
                self.assertIn("[OK]", res2)

                commands._cmd_undo("", state)
                self.assertEqual((root / "notes.py").read_text(encoding="utf-8"), "x = 1\n")
                self.assertEqual(tools.get_file_backups().get("notes.py"), "x = 0\n")

                ok1, _, rem1 = tools.undo_last_edit()
                self.assertTrue(ok1)
                self.assertEqual(rem1, 1)
                self.assertEqual((root / "notes.py").read_text(encoding="utf-8"), "x = 0\n")
                self.assertIsNone(tools.get_file_backups().get("notes.py"))

                ok2, _, rem2 = tools.undo_last_edit()
                self.assertTrue(ok2)
                self.assertEqual(rem2, 0)
                self.assertFalse((root / "notes.py").exists())
                self.assertNotIn("notes.py", tools.get_file_backups())

                # Concurrent writer + undoer remain crash-free and non-corrupting
                (root / "c.py").write_text("v = 0\n", encoding="utf-8")
                tools._read_file("c.py")
                conc_errors = []

                def writer():
                    try:
                        for i in range(25):
                            tools._write_file("c.py", f"v = {i}\n", force_overwrite=True)
                    except Exception as exc:
                        conc_errors.append(repr(exc))

                def undoer():
                    try:
                        for _ in range(20):
                            tools.undo_last_edit()
                    except Exception as exc:
                        conc_errors.append(repr(exc))

                t1 = threading.Thread(target=writer)
                t2 = threading.Thread(target=undoer)
                t1.start()
                t2.start()
                t1.join(timeout=10)
                t2.join(timeout=10)
                self.assertEqual(conc_errors, [])
                if (root / "c.py").exists():
                    self.assertTrue((root / "c.py").read_text(encoding="utf-8").startswith("v = "))
            finally:
                tools.set_project_root(orig_root)

    def test_bounded_memory_structures_and_atomic_session_save(self):
        import json
        from wsa_agent import session as sess_mod, tools
        from wsa_agent.context import FileContextLedger

        orig_root = tools.get_project_root()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            try:
                tools.set_project_root(root)
                state = sess_mod.SessionState(project_root=str(root))
                self.assertIsInstance(state.project_root, Path)

                for i in range(160):
                    state.append_tool_history(
                        {"turn": i, "tool": "patch_file", "args": {"old_string": "a" * 500}, "ok": True},
                        max_items=120,
                        max_arg_chars=300,
                    )
                self.assertEqual(len(state.tool_history), 120)
                truncated_arg = state.tool_history[-1]["args"]["old_string"]
                self.assertLess(len(truncated_arg), 340)
                self.assertIn("500 chars", truncated_arg)

                # Entry cap (50) & stale dict pruning
                for i in range(75):
                    p = root / f"f_{i}.txt"
                    tools._record_file_pre_edit(f"f_{i}.txt", p, True, f"body_{i}")
                self.assertEqual(len(tools.get_backup_order()), 50)
                self.assertNotIn("f_0.txt", tools.get_file_backups())
                self.assertIn("f_74.txt", tools.get_file_backups())

                # Byte cap (8MB): second 5MB entry evicts older 5MB entry, retaining >= 1 newest
                tools.reset_project_session_state()
                big = "X" * (5 * 1024 * 1024)
                tools._record_file_pre_edit("b1.txt", root / "b1.txt", True, big)
                tools._record_file_pre_edit("b2.txt", root / "b2.txt", True, big)
                self.assertEqual(len(tools.get_backup_order()), 1)
                self.assertEqual(tools.get_backup_order()[0][0], "b2.txt")

                # Ledger bound (250)
                ledger = FileContextLedger()
                for i in range(270):
                    ledger.record_read(f"m_{i}.py", 1, 5, "x = 1\n")
                self.assertLessEqual(len(ledger.entries), ledger._MAX_ENTRIES)

                # Atomic SessionState.save() preserves previous file when os.replace raises
                with patch.object(sess_mod.Path, "home", return_value=root):
                    state.messages = [{"role": "user", "content": "good-state"}]
                    saved = Path(state.save())
                    good_bytes = saved.read_bytes()
                    state.messages.append({"role": "assistant", "content": "partial-fail"})
                    with patch.object(sess_mod.os, "replace", side_effect=OSError("disk interrupted")):
                        with self.assertRaises(OSError):
                            state.save()
                    self.assertEqual(saved.read_bytes(), good_bytes)
                    self.assertEqual(list((root / ".wsa-sessions").glob("*.tmp*")), [])
                    loaded = json.loads(saved.read_text(encoding="utf-8"))
                    self.assertEqual(loaded["messages"][0]["content"], "good-state")
            finally:
                tools.set_project_root(orig_root)

    def test_relay_idle_crash_reconnect_and_dead_cutoff_recovery(self):
        import threading
        import time as real_time
        from wsa_server import relay

        with relay._dom_relay_lock:
            saved_state = {
                "relays": dict(relay._dom_relay.get("relays") or {}),
                "active_relay_id": relay._dom_relay.get("active_relay_id"),
                "pending": dict(relay._dom_relay.get("pending") or {}),
                "cancelled": dict(relay._dom_relay.get("cancelled_req_ids") or {}),
                "recent": dict(relay._dom_relay.get("recent_requests") or {}),
                "explicit": relay._dom_relay.get("explicit_relay_lock"),
            }
            relay._dom_relay["relays"] = {}
            relay._dom_relay["pending"] = {}
            relay._dom_relay["cancelled_req_ids"] = {}
            relay._dom_relay["recent_requests"] = {}
            relay._dom_relay["active_relay_id"] = None

        try:
            # (a) Extension crash/reload polling busy=False after picking up dispatched job
            cinfo = {"client_id": "brave-rel", "browser": "brave", "label": "brave", "has_claude": 1}
            relay.register_or_touch_relay(cinfo)
            relay.select_active_relay("brave-rel")
            out = {}

            def _consumer():
                t0 = real_time.monotonic()
                try:
                    relay.dom_relay_put_prompt("hi", timeout=15.0, pickup_timeout=2.0, provider="claude")
                except Exception as exc:
                    out["err"] = repr(exc)
                out["dt"] = real_time.monotonic() - t0

            th = threading.Thread(target=_consumer, daemon=True)
            th.start()
            real_time.sleep(0.1)
            job, _ = relay.dom_relay_poll_for_client(cinfo, busy=False)
            self.assertIsNotNone(job)
            # Overlapping idle polls must not cancel a live job. Sustained idle
            # evidence beyond the recovery grace still releases a crashed worker.
            real_time.sleep(0.15)
            p1, _ = relay.dom_relay_poll_for_client(cinfo, busy=False)
            self.assertIsNone(p1)
            real_time.sleep(0.15)
            p2, _ = relay.dom_relay_poll_for_client(cinfo, busy=False)
            self.assertIsNone(p2)
            self.assertFalse(out)
            with relay._dom_relay_lock:
                p_entry = relay._dom_relay["pending"][job["id"]]
                p_entry["dispatched_at"] = real_time.time() - 121.0
                p_entry["idle_since_ts"] = real_time.time() - 121.0
            relay.dom_relay_poll_for_client(cinfo, busy=False)
            th.join(timeout=3.0)
            self.assertLess(out.get("dt", 99.0), 2.0)
            self.assertIn("returned idle before completing", out.get("err", ""))

            # (a2) Even an aged request gets a recovery window on its first idle poll.
            out2 = {}

            def _consumer_aged():
                t0 = real_time.monotonic()
                try:
                    relay.dom_relay_put_prompt("hi-aged", timeout=15.0, pickup_timeout=2.0, provider="claude")
                except Exception as exc:
                    out2["err"] = repr(exc)
                out2["dt"] = real_time.monotonic() - t0

            th2 = threading.Thread(target=_consumer_aged, daemon=True)
            th2.start()
            real_time.sleep(0.1)
            job2, _ = relay.dom_relay_poll_for_client(cinfo, busy=False)
            self.assertIsNotNone(job2)
            with relay._dom_relay_lock:
                p_entry = relay._dom_relay["pending"][job2["id"]]
                p_entry["dispatched_at"] = real_time.time() - 1.0
            p_abort, _ = relay.dom_relay_poll_for_client(cinfo, busy=False)
            self.assertIsNone(p_abort)
            self.assertFalse(out2)
            with relay._dom_relay_lock:
                p_entry["dispatched_at"] = real_time.time() - 121.0
                p_entry["idle_since_ts"] = real_time.time() - 121.0
            relay.dom_relay_poll_for_client(cinfo, busy=False)
            th2.join(timeout=3.0)
            self.assertLess(out2.get("dt", 99.0), 2.0)
            self.assertIn("returned idle before completing", out2.get("err", ""))

            # (b) Silent dead relay cutoff recovers far before 300s timeout
            relay.register_or_touch_relay(cinfo)
            relay.select_active_relay("brave-rel")
            offset = [0.0]
            orig_t = relay.time.time

            def _driver():
                real_time.sleep(0.1)
                picked = relay.dom_relay_get_pending(client_id="brave-rel")
                self.assertIsNotNone(picked)
                with relay._dom_relay_lock:
                    wev = relay._dom_relay["pending"][picked["id"]].get("wake_event")
                offset[0] = 150.0
                if wev:
                    wev.set()

            with patch.object(relay.time, "time", side_effect=lambda: orig_t() + offset[0]):
                dt_thread = threading.Thread(target=_driver, daemon=True)
                dt_thread.start()
                err_msg = ""
                t0 = real_time.monotonic()
                try:
                    for _ in relay.dom_relay_iter("hello dead", provider="chatgpt", timeout=300, pickup_timeout=2.0):
                        pass
                except RuntimeError as exc:
                    err_msg = str(exc)
                dt_thread.join(timeout=2.0)
            self.assertLess(real_time.monotonic() - t0, 3.0)
            self.assertIn("stopped heartbeating", err_msg)
        finally:
            with relay._dom_relay_lock:
                relay._dom_relay["relays"] = saved_state["relays"]
                relay._dom_relay["active_relay_id"] = saved_state["active_relay_id"]
                relay._dom_relay["pending"] = saved_state["pending"]
                relay._dom_relay["cancelled_req_ids"] = saved_state["cancelled"]
                relay._dom_relay["recent_requests"] = saved_state["recent"]
                relay._dom_relay["explicit_relay_lock"] = saved_state["explicit"]

    def test_deepseek_budget_limit_and_dynamic_prompt_ceilings(self):
        from wsa_server import multimodal
        from wsa_agent import prompts, session as sess_mod, tools as tools_mod

        self.assertEqual(multimodal.get_prompt_max_bytes("deepseek"), 26_000)
        self.assertEqual(multimodal.get_prompt_max_bytes("ds"), 26_000)
        self.assertEqual(multimodal.get_prompt_max_bytes("r1"), 26_000)
        self.assertEqual(multimodal.get_prompt_max_bytes("chatgpt"), 26_000)
        self.assertEqual(multimodal.get_prompt_max_bytes("gemini"), 140_000)
        self.assertEqual(agent_module._max_web_prompt_chars_for_model("deepseek"), 18_000)

        self.assertIn("~18KB on DeepSeek", prompts.SYSTEM_PROMPT)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for model_id, expected in (("deepseek", "~18KB"), ("chatgpt", "~21KB"), ("gemini", "~96KB")):
                st = sess_mod.SessionState(project_root=root, session_id=f"p_{model_id}", selected_model=model_id)
                dyn = prompts.build_dynamic_system_prompt(st)
                self.assertIn(f"total prompt is capped ({expected} across all rounds)", dyn)

            # DeepSeek failover preserves full system instructions + active task + tool context
            st_ds = sess_mod.SessionState(project_root=root, session_id="p_ds_fail", selected_model="deepseek")
            sys_ds = prompts.build_dynamic_system_prompt(st_ds)
            msgs = [
                {"role": "system", "content": sys_ds},
                {"role": "user", "content": "Inspect relay.py and fix multi-browser routing. " * 25},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "wsa_server/relay.py"}'}}],
                },
                {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "def relay_helper():\n    return 1\n" * 120},
            ]
            agent_module._compact_messages_for_web_limit(
                msgs, max_chars=agent_module._max_web_prompt_chars_for_model("deepseek")
            )
            rendered, _ = multimodal.messages_to_prompt(msgs, tools=tools_mod.TOOL_DEFINITIONS, provider="deepseek")
            self.assertLessEqual(len(rendered), multimodal.DEEPSEEK_PROMPT_MAX_BYTES)
            self.assertIn("ZERO PLACEHOLDERS OR STUBS", rendered)
            self.assertIn("SURGICAL IN-PLACE", rendered)
            self.assertIn("Inspect relay.py and fix multi-browser routing.", rendered)

    def test_apply_patch_move_copy_batch_deps_checkpoints_and_verification_reset(self):
        from wsa_agent import session as sess_mod

        # 1. Dependency extraction & path matching include apply_patch / move_file / copy_file
        self.assertEqual(
            agent_module._extract_inv_target_paths(
                "apply_patch", {"edits": [{"path": "a.py"}, {"path": "sub/b.py"}]}
            ),
            {"a.py", "sub/b.py"},
        )
        self.assertEqual(
            agent_module._extract_inv_target_paths(
                "move_file", {"path": "old.py", "new_path": "new.py"}
            ),
            {"old.py", "new.py"},
        )
        self.assertTrue(
            agent_module._is_inv_dependent_on_path(
                "apply_patch", {"edits": [{"path": "pkg/mod.py"}]}, "pkg/mod.py"
            )
        )
        self.assertTrue(
            agent_module._is_inv_dependent_on_path(
                "move_file", {"path": "pkg/old.py", "new_path": "pkg/new.py"}, "pkg/new.py"
            )
        )
        self.assertTrue(
            agent_module._is_inv_dependent_on_path(
                "copy_file", {"path": "src.py", "new_path": "dst.py"}, "dst.py"
            )
        )

        # 2. Failed apply_patch in a multi-tool batch preserves outer batch checkpoint for prior writes
        #    and successful apply_patch / move_file / copy_file invalidate UI & file verification state
        orig_root = tools.get_project_root()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_text("VAL = 1\n", encoding="utf-8")
            (root / "b.py").write_text("OTHER = 2\n", encoding="utf-8")
            (root / "ui").mkdir()
            (root / "ui" / "index.html").write_text("<h1>v1</h1>\n", encoding="utf-8")
            (root / "src.txt").write_text("hello\n", encoding="utf-8")
            tools.set_project_root(root)
            try:
                with patch.object(sess_mod.Path, "home", return_value=root):
                    st = sess_mod.SessionState(project_root=root, session_id="ap_batch", selected_model="gemini")
                    st.auto_approve = True
                    st.permission_mode = "auto"

                    responses_batch = [
                        {
                            "content": "",
                            "thinking": "",
                            "tool_calls": [
                                {"id": "r1", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "a.py"})}},
                                {"id": "r2", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "b.py"})}},
                            ],
                        },
                        {
                            "content": "",
                            "thinking": "",
                            "tool_calls": [
                                {"id": "w1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "keep.txt", "content": "kept\n"})}},
                                {
                                    "id": "ap1",
                                    "type": "function",
                                    "function": {
                                        "name": "apply_patch",
                                        "arguments": json.dumps({
                                            "edits": [
                                                {"path": "a.py", "old_string": "VAL = 1", "new_string": "VAL = 9"},
                                                {"path": "b.py", "old_string": "NOT_THERE", "new_string": "X"},
                                            ]
                                        }),
                                    },
                                },
                            ],
                        },
                        {
                            "content": "",
                            "thinking": "",
                            "tool_calls": [
                                {"id": "v1", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": "python -m py_compile a.py"})}},
                            ],
                        },
                        {"content": "Done batch.", "thinking": "", "tool_calls": []},
                    ]

                    class _SeqClient:
                        def __init__(self, seq):
                            self._seq = list(seq)

                        def stream(self, **kwargs):
                            return self._seq.pop(0)

                    res = agent_module.execute_agent_loop("batch test", st, _SeqClient(responses_batch), quiet_ui=True)
                    self.assertTrue(res["ok"])
                    self.assertEqual((root / "keep.txt").read_text(encoding="utf-8"), "kept\n")
                    self.assertEqual((root / "a.py").read_text(encoding="utf-8"), "VAL = 1\n")
                    cps = tools.get_checkpoints()
                    self.assertTrue(any("keep.txt" in cp.get("files", []) for cp in cps))
                    self.assertNotIn("a.py", st.written_files)
                    self.assertIn("keep.txt", st.written_files)

                    # Now edit UI html via apply_patch and ensure browser_inspect is requested before finalizing
                    st.messages.clear()
                    st.written_files.clear()
                    responses_ui = [
                        {
                            "content": "",
                            "thinking": "",
                            "tool_calls": [
                                {"id": "ru", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "ui/index.html"})}},
                            ],
                        },
                        {
                            "content": "",
                            "thinking": "",
                            "tool_calls": [
                                {
                                    "id": "apu",
                                    "type": "function",
                                    "function": {
                                        "name": "apply_patch",
                                        "arguments": json.dumps({
                                            "edits": [{"path": "ui/index.html", "old_string": "<h1>v1</h1>", "new_string": "<h1>v2</h1>"}]
                                        }),
                                    },
                                },
                                {
                                    "id": "cp1",
                                    "type": "function",
                                    "function": {"name": "copy_file", "arguments": json.dumps({"path": "src.txt", "new_path": "copied.txt"})},
                                },
                                {
                                    "id": "mv1",
                                    "type": "function",
                                    "function": {"name": "move_file", "arguments": json.dumps({"path": "copied.txt", "new_path": "moved.txt"})},
                                },
                            ],
                        },
                        {"content": "Finished UI edit.", "thinking": "", "tool_calls": []},
                        {"content": "Actually verified.", "thinking": "", "tool_calls": []},
                    ]
                    captured_prompts = []

                    class _UiGateClient:
                        def __init__(self, seq):
                            self._seq = list(seq)

                        def stream(self, *, messages, **kwargs):
                            captured_prompts.append(messages[-1].get("content", ""))
                            return self._seq.pop(0)

                    agent_module.execute_agent_loop(
                        "update ui", st, _UiGateClient(responses_ui), quiet_ui=True
                    )
                    self.assertFalse((root / "copied.txt").exists())
                    self.assertEqual((root / "moved.txt").read_text(encoding="utf-8"), "hello\n")
                    self.assertNotIn("copied.txt", st.written_files)
                    self.assertIn("ui/index.html", st.written_files)
                    self.assertIn("moved.txt", st.written_files)
                    self.assertTrue(any("browser_inspect" in p for p in captured_prompts))
            finally:
                tools.set_project_root(orig_root)

    def test_relay_rollover_resets_turns_even_without_new_chat_url(self):
        from wsa_server import relay

        s_key = relay._session_relay_key("roll_s1", "gemini")
        with relay._dom_relay_lock:
            saved_sessions = dict(relay._dom_relay.get("sessions") or {})
            saved_meta = dict(relay._PENDING_SESSION_META)
            relay._dom_relay["sessions"] = {
                s_key: {
                    "last_sig": "old-sig",
                    "chat_url": "https://gemini.google.com/app/oldthread123",
                    "relay_id": "",
                    "account_email": "",
                    "provider": "gemini",
                    "turns": 120,
                    "tools_sig": "",
                    "sys_sig": "",
                    "updated_at": 1.0,
                }
            }
            relay._PENDING_SESSION_META.clear()
        try:
            msgs = [{"role": "user", "content": "next task after long thread"}]
            with patch.object(relay, "_persist_relay_sessions_locked"):
                _, _, _, is_new, _hint = relay.prepare_session_relay_prompt(
                    session_id="roll_s1",
                    messages=msgs,
                    tools=[],
                    full_prompt="next task after long thread",
                    full_images=[],
                    provider="gemini",
                )
                self.assertTrue(is_new)
                # Extension returned no new chat_url (or stale old URL): turns must reset to 1, not 121
                relay.record_session_relay_turn(
                    "roll_s1",
                    assistant_content="Done fresh.",
                    chat_url="https://gemini.google.com/app/oldthread123",
                    provider="gemini",
                )
            with relay._dom_relay_lock:
                sess = relay._dom_relay["sessions"][s_key]
                self.assertEqual(sess["turns"], 1)
                self.assertEqual(sess["chat_url"], "")
        finally:
            with relay._dom_relay_lock:
                relay._dom_relay["sessions"] = saved_sessions
                relay._PENDING_SESSION_META.clear()
                relay._PENDING_SESSION_META.update(saved_meta)

    def test_gemini_bard_error_info_not_retried_and_deepseek_status_headers_stream(self):
        import subprocess
        from wsa_server import gemini as gem_mod

        class _FakeStreamResp:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                return None

            def iter_text(self):
                yield ')]}\'\n[["wrb.fr",null,null,null,null,[3,"BardErrorInfo [1050]"]]]\n'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class _FakeHttpxClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def stream(self, *args, **kwargs):
                return _FakeStreamResp()

        with patch.object(gem_mod, "is_dom_relay_active", return_value=False), \
             patch.object(gem_mod, "argon_strict_relay_required", return_value=False), \
             patch.object(gem_mod, "_build_upstream_request", return_value=("https://example.invalid/stream", {}, {})), \
             patch.object(gem_mod.httpx, "Client", _FakeHttpxClient), \
             patch.object(gem_mod, "gemini_stream_generate") as mock_fallback:
            with self.assertRaises(RuntimeError) as ctx:
                list(gem_mod.gemini_stream_generate_events("hi", model_cfg={"mode": 1}))
            self.assertIn("BardErrorInfo [1050]", str(ctx.exception))
            self.assertEqual(mock_fallback.call_count, 0)

        bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
        js = "const bgPath = " + json.dumps(bg_path) + ";\n" + r"""
const assert = require("assert");
const bg = require(bgPath);

function makeNode(tag, text, visible = true) {
  return {
    nodeType: 1,
    tagName: tag,
    innerText: text,
    textContent: text,
    disabled: false,
    getAttribute: () => "",
    closest: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    getBoundingClientRect: () => (visible ? { width: 120, height: 24, top: 10, left: 10, bottom: 34, right: 130 } : { width: 0, height: 0 }),
  };
}

let activeHdrs = [];
global.Element = function () {};
global.Node = { ELEMENT_NODE: 1 };
global.document = {
  querySelector: () => null,
  querySelectorAll: (sel) => (sel.includes("_74c0879") ? activeHdrs : []),
};

assert.strictEqual(bg._installWsaDomUtilsFn(), true);
assert.strictEqual(bg._deepseekInstallPageRuntimeFn(), true);
const rt = globalThis.__wsaDeepseekRuntime;

for (const activeText of ["Thinking...", "Searching 10 web pages", "Reading 5 web pages..."]) {
  activeHdrs = [makeNode("DIV", activeText)];
  assert.strictEqual(rt.hasStopButton(), true, "expected active for: " + activeText);
}
for (const doneText of ["Thought for 12s", "Searched 10 web pages", "Read 10 web pages"]) {
  activeHdrs = [makeNode("DIV", doneText)];
  assert.strictEqual(rt.hasStopButton(), false, "expected idle for: " + doneText);
}
"""
        proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=f"Node check failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")

    def test_multiagent_multi_browser_provider_discovery_routing_and_limit_patterns(self):
        import subprocess
        import threading
        import time
        from wsa_agent import commands
        from wsa_server import relay

        # 1. _extract_live_providers merges online secondary browser tabs in auto mode,
        #    ignores stale offline active tabs, and respects explicit_relay_lock=True.
        health_multi = {
            "ok": True,
            "explicit_relay_lock": False,
            "relays": [
                {
                    "client_id": "chrome-1",
                    "browser": "chrome",
                    "active": True,
                    "online": True,
                    "providers": {
                        "claude": {"has_tab": True, "email": "c@example.com"},
                        "gemini": {"has_tab": True},
                        "deepseek": {"has_tab": False},
                        "chatgpt": {"has_tab": False},
                    },
                },
                {
                    "client_id": "brave-2",
                    "browser": "brave",
                    "active": False,
                    "online": True,
                    "providers": {
                        "deepseek": {"has_tab": True, "model_badge": "DeepSeek-R1"},
                        "chatgpt": {"has_tab": True},
                    },
                },
            ],
        }
        srv_ok, rel_online, pmap = commands._extract_live_providers(health_multi)
        self.assertTrue(srv_ok and rel_online)
        for pk in ("claude", "gemini", "deepseek", "chatgpt"):
            self.assertTrue((pmap.get(pk) or {}).get("has_tab"), msg=f"Expected {pk} live across relays: {pmap}")

        health_locked = dict(health_multi, explicit_relay_lock=True)
        _, rel_locked_on, pmap_locked = commands._extract_live_providers(health_locked)
        self.assertTrue(rel_locked_on)
        self.assertTrue(pmap_locked["claude"]["has_tab"])
        self.assertFalse(pmap_locked["deepseek"]["has_tab"])

        health_offline_active = dict(
            health_multi,
            relays=[
                dict(health_multi["relays"][0], online=False),
                health_multi["relays"][1],
            ],
        )
        _, rel_fb_on, pmap_fb = commands._extract_live_providers(health_offline_active)
        self.assertTrue(rel_fb_on)
        self.assertNotIn("claude", pmap_fb)
        self.assertTrue(pmap_fb["deepseek"]["has_tab"])

        # 2. dom_relay_iter + dom_relay_get_pending auto-route failover provider to secondary
        #    online relay when active relay lacks that provider tab (and explicit_relay_lock is False).
        with relay._dom_relay_lock:
            snap = {
                "relays": dict(relay._dom_relay.get("relays") or {}),
                "active_relay_id": relay._dom_relay.get("active_relay_id"),
                "pinned_relay_label": relay._dom_relay.get("pinned_relay_label"),
                "explicit_relay_lock": relay._dom_relay.get("explicit_relay_lock", False),
                "pending": dict(relay._dom_relay.get("pending") or {}),
                "sessions": dict(relay._dom_relay.get("sessions") or {}),
            }
        try:
            now = time.time()
            with relay._dom_relay_lock:
                relay._dom_relay["relays"] = {
                    "chrome-1": {
                        "client_id": "chrome-1",
                        "browser": "chrome",
                        "label": "Chrome",
                        "active_provider": "claude",
                        "last_seen": now,
                        "providers": {"claude": {"has_tab": True}, "deepseek": {"has_tab": False}},
                    },
                    "brave-2": {
                        "client_id": "brave-2",
                        "browser": "brave",
                        "label": "Brave",
                        "active_provider": "deepseek",
                        "last_seen": now,
                        "providers": {"deepseek": {"has_tab": True}},
                    },
                }
                relay._dom_relay["active_relay_id"] = "chrome-1"
                relay._dom_relay["explicit_relay_lock"] = False
                relay._dom_relay["pending"].clear()

            err_box = []
            res_box = []

            def _run_req():
                try:
                    res_box.append(relay.dom_relay_put_prompt("ping", timeout=3.0, pickup_timeout=2.0, provider="deepseek"))
                except Exception as exc:
                    err_box.append(exc)

            t = threading.Thread(target=_run_req, daemon=True)
            t.start()
            pending_job = None
            for _ in range(80):
                # Chrome must NOT steal DeepSeek job; Brave MUST receive it.
                self.assertIsNone(relay.dom_relay_get_pending(client_id="chrome-1"))
                pending_job = relay.dom_relay_get_pending(client_id="brave-2")
                if pending_job:
                    relay.dom_relay_post_response(
                        pending_job["id"],
                        response="DeepSeek from Brave",
                        model="DeepSeek-R1",
                        client_id="brave-2",
                    )
                    break
                time.sleep(0.02)
            t.join(timeout=3.0)
            self.assertFalse(err_box, msg=err_box)
            self.assertTrue(pending_job and pending_job["provider"] == "deepseek")
            self.assertEqual(res_box[0]["response"], "DeepSeek from Brave")
            self.assertEqual(res_box[0]["relay_id"], "brave-2")
        finally:
            with relay._dom_relay_lock:
                for k, v in snap.items():
                    relay._dom_relay[k] = v

        # 3. Server & browser limit pattern parity for DeepSeek / ChatGPT / concurrent caps
        for msg in (
            "The system is busy. Please try again later.",
            "DeepThink limit reached for today",
            "Too many concurrent requests",
        ):
            self.assertTrue(relay.is_provider_limit_error(msg), msg=msg)

        bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
        js = f"""
const assert = require("assert");
const bg = require({json.dumps(bg_path)});
globalThis.document = {{ querySelectorAll: () => [], querySelector: () => null }};
assert.strictEqual(bg._installWsaDomUtilsFn(), true);
const u = globalThis.__wsaDomUtils;
for (const sample of [
  "The system is busy. Please try again later.",
  "DeepThink limit reached for today",
  "Too many concurrent requests",
  "You've reached the Free plan limit for GPT-5",
]) {{
  assert.ok(u.detectLimitMessage(sample, true), "Expected limit match for: " + sample);
}}
"""
        proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=f"Node limit check failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")

    def test_venice_ai_aliases_thread_urls_dom_thinking_limits_and_multiagent_e2e(self):
        import io
        import subprocess
        import time
        import urllib.parse
        import urllib.request
        from rich.console import Console
        from wsa_agent import client as client_mod, commands, prompts, renderer, session as sess_mod
        from wsa_server import gemini, models as srv_models, multimodal as mm, relay, server as srv_mod

        # 1. Alias normalization (including vernice typo), model catalog, high-budget ceiling, & thread URL rules
        for raw_alias in (
            "venice", "Venice", "veniceai", "venice.ai", "venice-web",
            "vernice", "verniceai", "vernice.ai", "vernice-web",
            "deepseek-v4.1-flash", "deepseek-v4-1-flash-uncensored",
        ):
            self.assertEqual(sess_mod.normalize_web_model(raw_alias), "venice", msg=raw_alias)

        self.assertIn("venice", sess_mod.WEB_MODEL_IDS)
        self.assertEqual(sess_mod.DEFAULT_MULTIAGENT_QUEUE[-1], "venice")
        self.assertIn("venice", srv_models.WEB_PROVIDER_SPECS)
        self.assertEqual(gemini._bridge_provider_for_cfg(srv_models.MODELS["venice"]), "venice")
        self.assertEqual(mm.get_prompt_max_bytes("venice"), mm.PROMPT_MAX_BYTES)
        self.assertEqual(agent_module._max_web_prompt_chars_for_model("venice"), agent_module.MAX_WEB_PROMPT_CHARS)

        # Landing routes must NOT count as resumable threads; actual chat IDs MUST count and label cleanly
        self.assertFalse(relay._is_provider_thread_url("https://venice.ai/chat/agent", "venice"))
        self.assertFalse(relay._is_provider_thread_url("https://venice.ai/chat/classic?refreshId=99", "venice"))
        self.assertFalse(sess_mod.is_provider_thread_url("https://venice.ai/chat/agent"))
        self.assertTrue(relay._is_provider_thread_url("https://venice.ai/chat/agent/uuFvVgZ", "venice"))
        self.assertTrue(relay._is_provider_thread_url("https://venice.ai/chat/classic/ACu42nS", "venice"))
        self.assertTrue(sess_mod.is_provider_thread_url("https://venice.ai/chat/agent/uuFvVgZ"))

        st_badge = sess_mod.SessionState(project_root=Path("."))
        st_badge.set_model("venice")
        st_badge.active_chat_url = "https://venice.ai/chat/agent/uuFvVgZ"
        buf = io.StringIO()
        orig_console = renderer.console
        try:
            renderer.console = Console(file=buf, force_terminal=False, width=102, highlight=False)
            renderer.print_status(st_badge)
            status_out = buf.getvalue()
            self.assertIn("Venice Web", status_out)
            self.assertIn("uuFvVgZ", status_out)
            self.assertIn("pinned venice.ai thread", status_out)
            self.assertNotIn("pinned claude.ai thread", status_out)
        finally:
            renderer.console = orig_console

        # Venice prompt instructions keep 524K / ~96KB ceiling and avoid artifact cards
        dyn_v = prompts.build_dynamic_system_prompt(st_badge)
        self.assertIn("DeepSeek V4.1 Flash Uncensored", dyn_v)
        self.assertIn("~96KB", dyn_v)
        tblock_v = mm._format_tools_prompt_block([], provider="venice")
        self.assertEqual(tblock_v, "")
        self.assertTrue(relay.is_provider_limit_error("Daily chat limit reached"))
        self.assertTrue(relay.is_provider_limit_error("Insufficient credits — Upgrade to Pro to continue"))
        self.assertTrue(relay.is_prompt_too_long_error("maximum context length exceeded"))

        # 2. Live-observed Venice Agentic + Classic DOM runtime checks in Node VM
        bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
        js = f"""
const assert = require("assert");
const bg = require({json.dumps(bg_path)});

assert.strictEqual(bg.detectTabProvider("https://venice.ai/chat/agent"), "venice");
assert.strictEqual(bg.detectTabProvider("https://venice.ai/chat/classic/ACu42nS"), "venice");
assert.strictEqual(bg.extractVeniceConvId("https://venice.ai/chat/agent"), "");
assert.strictEqual(bg.extractVeniceConvId("https://venice.ai/chat/classic"), "");
assert.strictEqual(bg.extractVeniceConvId("https://venice.ai/chat/classic?refreshId=123"), "");
assert.strictEqual(bg.extractVeniceConvId("https://venice.ai/chat/agent/uuFvVgZ"), "uuFvVgZ");
assert.strictEqual(bg.extractVeniceConvId("https://venice.ai/chat/classic/ACu42nS"), "ACu42nS");
assert.strictEqual(bg.extractConvId("https://venice.ai/chat/agent/uuFvVgZ", "venice"), "uuFvVgZ");
assert.strictEqual(bg.isProtectedInspectUrl("https://venice.ai/chat/agent/uuFvVgZ"), true);

class MockEl {{
  constructor(tag, attrs = {{}}, cls = "", text = "") {{
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this._attrs = Object.assign({{}}, attrs);
    this.className = cls;
    this._text = text;
    this.style = {{}};
    this.children = [];
    this.childNodes = [];
    this.parentElement = null;
    this.disabled = Boolean(attrs.disabled);
    this.value = attrs.value || "";
    this._listeners = {{}};
    if (text) {{
      this.childNodes.push({{ nodeType: 3, textContent: String(text), parentElement: this }});
    }}
  }}
  add(...kids) {{
    for (const k of kids) {{
      if (typeof k === "string") {{
        const tn = {{ nodeType: 3, textContent: k, parentElement: this }};
        this.childNodes.push(tn);
      }} else {{
        k.parentElement = this;
        this.children.push(k);
        this.childNodes.push(k);
      }}
    }}
    return this;
  }}
  addEventListener(ev, fn) {{ (this._listeners[ev] ||= []).push(fn); }}
  dispatchEvent(ev) {{
    for (const fn of this._listeners[ev.type] || []) fn.call(this, ev);
    return true;
  }}
  focus() {{}}
  click() {{ this._clicked = (this._clicked || 0) + 1; }}
  hasAttribute(k) {{ return Object.prototype.hasOwnProperty.call(this._attrs, k); }}
  getAttribute(k) {{ return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; }}
  setAttribute(k, v) {{ this._attrs[k] = String(v); }}
  get textContent() {{
    if (this._text) return this._text;
    return this.childNodes.map(c => c.textContent || "").join("");
  }}
  set textContent(v) {{ this._text = String(v); }}
  get innerText() {{ return this.textContent; }}
  get classList() {{
    const toks = String(this.className || "").split(/\\s+/).filter(Boolean);
    return {{ contains: (c) => toks.includes(c) }};
  }}
  contains(other) {{
    let p = other;
    while (p) {{ if (p === this) return true; p = p.parentElement; }}
    return false;
  }}
  closest(sel) {{
    let p = this;
    while (p && p.nodeType === 1) {{
      if (matchesSel(p, sel)) return p;
      p = p.parentElement;
    }}
    return null;
  }}
  matches(sel) {{ return matchesSel(this, sel); }}
  querySelector(sel) {{ return this.querySelectorAll(sel)[0] || null; }}
  querySelectorAll(sel) {{
    const out = [];
    const walk = (n) => {{
      for (const c of n.children || []) {{
        if (matchesSel(c, sel)) out.push(c);
        walk(c);
      }}
    }};
    walk(this);
    return out;
  }}
  getBoundingClientRect() {{ return {{ width: 200, height: 24, top: 10, left: 10, bottom: 34, right: 210 }}; }}
}}

function matchesOne(el, raw) {{
  const s = raw.trim();
  if (!s) return false;
  const parts = s.split(/\\s+(?![^\\[]*\\])/);
  if (parts.length > 1) {{
    if (!matchesOne(el, parts[parts.length - 1])) return false;
    let cur = el.parentElement;
    for (let i = parts.length - 2; i >= 0; i--) {{
      while (cur && !matchesOne(cur, parts[i])) cur = cur.parentElement;
      if (!cur) return false;
      cur = cur.parentElement;
    }}
    return true;
  }}
  let rest = s;
  const tagM = rest.match(/^[a-z0-9-]+/i);
  if (tagM) {{
    if (el.tagName.toLowerCase() !== tagM[0].toLowerCase()) return false;
    rest = rest.slice(tagM[0].length);
  }}
  while (rest) {{
    if (rest.startsWith(".")) {{
      const cm = rest.match(/^\\.([A-Za-z0-9_-]+)/);
      rest = rest.slice(cm[0].length);
      if (!el.classList.contains(cm[1])) return false;
    }} else if (rest.startsWith("[")) {{
      const am = rest.match(/^\\[([^=\\]]+)(?:([*^]?)=['"]([^'"]*)['"](\\s*i)?)?\\]/);
      rest = rest.slice(am[0].length);
      const attr = am[1].trim();
      const val = el.getAttribute(attr);
      if (val === null) return false;
      if (am[3] !== undefined) {{
        const a = am[4] ? val.toLowerCase() : val;
        const b = am[4] ? am[3].toLowerCase() : am[3];
        if (am[2] === "*" ? !a.includes(b) : (am[2] === "^" ? !a.startsWith(b) : a !== b)) return false;
      }}
    }} else {{
      break;
    }}
  }}
  return true;
}}

function matchesSel(el, sel) {{
  return sel.split(/,(?![^\\[]*\\])/).some((s) => matchesOne(el, s));
}}

global.Element = MockEl;
global.Node = {{ ELEMENT_NODE: 1, TEXT_NODE: 3 }};
global.getComputedStyle = () => ({{ display: "block", visibility: "visible", opacity: "1" }});
global.location = {{ pathname: "/chat/agent/uuFvVgZ", href: "https://venice.ai/chat/agent/uuFvVgZ" }};
global.InputEvent = function(type, init) {{ this.type = type; Object.assign(this, init || {{}}); }};
global.Event = function(type, init) {{ this.type = type; Object.assign(this, init || {{}}); }};

// Agentic Chat DOM fixture with Model Picker, Pro upsell, React Send/Dictate, Expanded Thinking, & Code Block
const root = new MockEl("div", {{}}, "root");
const body = new MockEl("body");
root.add(body);
const modelBtn = new MockEl("button", {{ "aria-haspopup": "menu" }}, "chakra-button css-1rejuei", "DeepSeek V4.1 Flash Uncensored");
const proBtn = new MockEl("button", {{ "aria-label": "Get Pro Access" }}, "chakra-button css-1pkisp3", "Get Pro Access");
const progressBar = new MockEl("div", {{ role: "progressbar", "aria-valuenow": "4.68" }});
const scrollRoot = new MockEl("div", {{}}, "minds-chat-scroll-root");
const main = new MockEl("main");
main.add(modelBtn, proBtn, scrollRoot);

// Turn 1 User bubble
const userWrap = new MockEl("div");
const userBubble = new MockEl("div", {{}}, "css-1dio6h5").add(new MockEl("div", {{}}, "prose").add(new MockEl("p", {{}}, "", "Explain auth flow.")));
const userGrp = new MockEl("div", {{ role: "group" }}).add(
  new MockEl("button", {{ "aria-label": "Edit" }}, "", "Edit"),
  new MockEl("button", {{ "aria-label": "Copy" }}, "", "Copy")
);
userWrap.add(userBubble, userGrp);

// Turn 1 Assistant turn with Expanded Thinking + Markdown + Code Block Chrome + Action Bar
const asstWrap = new MockEl("div");
const thinkToggle = new MockEl("div", {{}}, "css-934klp").add(new MockEl("p", {{}}, "chakra-text css-zllcs4", "Worked for 1s · 1 step"));
const thinkBody = new MockEl("div", {{}}, "css-ev1bas").add(
  new MockEl("div", {{}}, "css-1w2ukei").add(
    new MockEl("p", {{}}, "css-1gtoqpr", "Thought for 1s"),
    new MockEl("div", {{}}, "css-8ozojj").add(new MockEl("div", {{}}, "prose").add(new MockEl("p", {{}}, "", "Need to inspect auth flow cleanly."))),
    new MockEl("p", {{}}, "chakra-text css-1frcpea", "Done")
  )
);
const codeHdr = new MockEl("div", {{}}, "css-codehdr").add(
  new MockEl("span", {{}}, "css-1tozq6k", "python"),
  new MockEl("div", {{ role: "group" }}).add(
    new MockEl("button", {{ "aria-label": "Copy code" }}, "chakra-button css-1iiqk2a", "Copy code"),
    new MockEl("button", {{ "aria-label": "download as file" }}, "chakra-button css-1epyfvr", "download as file")
  )
);
const preEl = new MockEl("pre").add(new MockEl("code", {{}}, "language-python", "print('hello')"));
const asstProse = new MockEl("div", {{}}, "prose").add(
  new MockEl("ul", {{}}, "css-tu0njr").add(new MockEl("li", {{}}, "css-xs9fqv", "First verified bullet")),
  codeHdr,
  preEl
);
const asstGrp = new MockEl("div", {{ role: "group" }}).add(
  new MockEl("button", {{ "aria-label": "Copy" }}, "chakra-button css-17nxido", "Copy"),
  new MockEl("button", {{ "aria-label": "Regenerate" }}, "chakra-button css-17nxido", "Regenerate"),
  new MockEl("button", {{ "aria-label": "Edit" }}, "chakra-button css-17nxido", "Edit"),
  new MockEl("button", {{ "aria-label": "Delete" }}, "chakra-button css-17nxido", "Delete")
);
asstWrap.add(thinkToggle, thinkBody, asstProse, asstGrp);
scrollRoot.add(userWrap, asstWrap);

// Form composer (Agentic React behavior: Dictate when empty -> Send message on input -> Stop generation when streaming)
const form = new MockEl("form");
const dropzone = new MockEl("div", {{ "aria-label": "Mind chat attachment dropzone" }});
const textarea = new MockEl("textarea", {{ "data-slot": "composer-field", "aria-label": "Chat message input" }}, "chakra-textarea css-1fkbw2a");
const actionBtn = new MockEl("button", {{ "aria-label": "Dictate" }}, "chakra-button css-66owdz", "Dictate");
textarea.addEventListener("input", () => {{
  const hasVal = Boolean(String(textarea.value || "").trim());
  actionBtn.setAttribute("aria-label", hasVal ? "Send message" : "Dictate");
  actionBtn.textContent = hasVal ? "Send message" : "Dictate";
}});
dropzone.add(textarea, progressBar, actionBtn);
form.add(dropzone);
main.add(form);
body.add(main);

global.document = {{
  body,
  querySelector: (s) => root.querySelector(s),
  querySelectorAll: (s) => root.querySelectorAll(s),
  createElement: (t) => new MockEl(t),
}};

assert.strictEqual(bg._installWsaDomUtilsFn(), true);
assert.strictEqual(bg._veniceInstallPageRuntimeFn(), true);
const rt = globalThis.__wsaVeniceRuntime;

assert.strictEqual(rt.readModelBadge(), "DeepSeek V4.1 Flash Uncensored");
assert.ok(rt.findEditor() === textarea);
assert.strictEqual(rt.findSendButton(textarea), null);
globalThis.__wsaDomUtils.setEditablePromptText(textarea, "Check auth.py");
const sendBtn = rt.findSendButton(textarea);
assert.ok(sendBtn, "React input event should reveal Send message button");
assert.strictEqual(sendBtn.getAttribute("aria-label"), "Send message");
assert.strictEqual(rt.hasStopButton(), false);

// Simulate streaming Stop generation state
actionBtn.setAttribute("aria-label", "Stop generation");
actionBtn.textContent = "Stop generation";
assert.strictEqual(rt.hasStopButton(), true);
actionBtn.setAttribute("aria-label", "Dictate");
actionBtn.textContent = "Dictate";
assert.strictEqual(rt.hasStopButton(), false);

const turns = rt.readAllTurns();
assert.strictEqual(turns.length, 1, "Must exclude user bubble and return 1 assistant turn");
assert.strictEqual(turns[0].hasActionBar, true);
assert.strictEqual(turns[0].thinking, "Need to inspect auth flow cleanly.");
assert.ok(turns[0].text.includes("- First verified bullet"));
assert.ok(turns[0].text.includes("```python\\nprint('hello')\\n```"));
assert.ok(!turns[0].text.includes("Worked for"));
assert.ok(!turns[0].text.includes("Thought for"));
assert.ok(!turns[0].text.includes("Copy code"));
assert.ok(!turns[0].text.includes("download as file"));
assert.ok(!turns[0].text.includes("Explain auth flow."));
assert.ok(!turns[0].text.includes("Need to inspect auth flow cleanly."));

// Static Get Pro Access & progressbar must NOT be reported in readErrorBanner, whereas Chakra alert toast MUST trigger [LIMIT_REACHED]
assert.strictEqual(rt.readErrorBanner(), "");
const toast = new MockEl("div", {{ role: "alert", "aria-live": "assertive" }}, "chakra-alert", "Daily chat limit reached — upgrade to Pro to continue");
body.add(toast);
const errBanner = rt.readErrorBanner();
assert.ok(errBanner.includes("Daily chat limit reached"));
assert.ok(globalThis.__wsaDomUtils.detectLimitMessage(errBanner, true));

// Classic Chat (/chat/classic/ACu42nS) DOM check: .fancy-card user bubble + Thought Process + Submit chat -> stop -> Venice Voice
const classicRoot = new MockEl("div", {{}}, "classic-root");
const classicMain = new MockEl("main");
const classicUser = new MockEl("div", {{}}, "fancy-card css-zdl3ww").add(
  new MockEl("p", {{}}, "chakra-text css-1ltj640", "Classic user msg"),
  new MockEl("div", {{ role: "group" }}).add(new MockEl("button", {{ "aria-label": "Edit Response" }}, "", "Edit Response"))
);
const classicAsst = new MockEl("div").add(
  new MockEl("p", {{}}, "chakra-text css-1vyjab", "DeepSeek V4.1 Flash Uncensored"),
  new MockEl("div", {{}}, "css-934klp").add(new MockEl("p", {{}}, "chakra-text css-itvw0n", "Thought Process")),
  new MockEl("div", {{}}, "css-gild96").add(new MockEl("p", {{}}, "chakra-text css-1ltj640", "Classic chain of thought.")),
  new MockEl("div", {{}}, "prose").add(new MockEl("p", {{}}, "", "Classic answer text.")),
  new MockEl("div", {{ role: "group" }}).add(
    new MockEl("button", {{ "aria-label": "Regenerate" }}, "", "Regenerate"),
    new MockEl("button", {{ "aria-label": "Venice Voice" }}, "", "Venice Voice")
  )
);
const classicForm = new MockEl("form");
const classicEd = new MockEl("textarea", {{ "aria-label": "Ask anything privately..." }}, "chakra-textarea css-3buekz");
const classicBtn = new MockEl("button", {{ "aria-label": "Submit chat", disabled: true }}, "chakra-button css-i3e476", "Submit chat");
classicEd.addEventListener("input", () => {{
  const hasVal = Boolean(String(classicEd.value || "").trim());
  classicBtn.disabled = !hasVal;
}});
classicForm.add(classicEd, classicBtn);
classicMain.add(classicUser, classicAsst, classicForm);
classicRoot.add(classicMain);
global.location = {{ pathname: "/chat/classic/ACu42nS", href: "https://venice.ai/chat/classic/ACu42nS" }};
global.document = {{
  body: classicRoot,
  querySelector: (s) => classicRoot.querySelector(s),
  querySelectorAll: (s) => classicRoot.querySelectorAll(s),
  createElement: (t) => new MockEl(t),
}};
assert.strictEqual(rt.findEditor(), classicEd);
globalThis.__wsaDomUtils.setEditablePromptText(classicEd, "Hi classic");
assert.strictEqual(rt.findSendButton(classicEd), classicBtn);
classicBtn.setAttribute("aria-label", "stop");
classicBtn.disabled = false;
assert.strictEqual(rt.hasStopButton(), true);
const cTurns = rt.readAllTurns();
assert.strictEqual(cTurns.length, 1);
assert.strictEqual(cTurns[0].thinking, "Classic chain of thought.");
assert.strictEqual(cTurns[0].text, "Classic answer text.");
assert.strictEqual(cTurns[0].hasActionBar, true);
assert.ok(!cTurns[0].text.includes("Classic user msg"));
assert.ok(!cTurns[0].text.includes("Thought Process"));
"""
        proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=f"Venice DOM check failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")

        # 3. Full Real HTTP Server E2E: /multiagent chatgpt -> venice failover + venice same-chat delta persistence
        orig_relay_state = {k: (dict(v) if isinstance(v, dict) else v) for k, v in relay._dom_relay.items()}
        orig_pending_meta = dict(relay._PENDING_SESSION_META)
        httpd = srv_mod.ThreadedServer(("127.0.0.1", 0), srv_mod.WSAHandler)
        srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        srv_thread.start()
        port = httpd.server_address[1]
        base_url = f"http://127.0.0.1:{port}"
        api_key = str((srv_mod.CONFIG.get("api_keys") or [""])[0] or "")
        stop_ext = threading.Event()
        dispatched = []

        def _ext_worker():
            cid = "ext-venice-e2e"
            qs = urllib.parse.urlencode({
                "client_id": cid,
                "browser": "chrome",
                "label": "Chrome Venice",
                "provider": "venice",
                "has_claude": "1", "claude_model": "Claude Sonnet",
                "has_deepseek": "1", "deepseek_model": "DeepSeek-R1",
                "has_gemini": "1", "gemini_model": "Gemini Thinking",
                "has_chatgpt": "1", "chatgpt_model": "ChatGPT",
                "has_venice": "1", "venice_model": "DeepSeek V4.1 Flash Uncensored",
            })
            venice_calls = 0
            while not stop_ext.is_set():
                try:
                    with urllib.request.urlopen(f"{base_url}/pending-prompt?{qs}", timeout=2.0) as resp:
                        item = json.loads(resp.read().decode("utf-8"))
                except Exception:
                    stop_ext.wait(0.02)
                    continue
                if not item or not item.get("id"):
                    stop_ext.wait(0.02)
                    continue
                dispatched.append(item)
                prov = str(item.get("provider") or "gemini")
                if prov == "chatgpt":
                    payload = {
                        "id": item["id"],
                        "client_id": cid,
                        "error": "[LIMIT_REACHED] ChatGPT: usage limit reached",
                        "model": "ChatGPT",
                    }
                elif prov == "venice" and venice_calls == 0:
                    venice_calls += 1
                    payload = {
                        "id": item["id"],
                        "client_id": cid,
                        "thinking": "Worked for 1s · 1 step\nInspecting mod.py cleanly.",
                        "response": 'Checking `mod.py`.\n```tool_call\n{"name": "read_file", "arguments": {"path": "mod.py"}}\n```',
                        "model": "DeepSeek V4.1 Flash Uncensored",
                        "chat_url": "https://venice.ai/chat/agent/uuFvVgZ",
                    }
                elif prov == "venice" and venice_calls in (1, 2):
                    venice_calls += 1
                    payload = {
                        "id": item["id"],
                        "client_id": cid,
                        "response": "Venice verified VAL=99 in same chat.",
                        "model": "DeepSeek V4.1 Flash Uncensored",
                        "chat_url": "https://venice.ai/chat/agent/uuFvVgZ",
                    }
                elif prov == "venice":
                    payload = {
                        "id": item["id"],
                        "client_id": cid,
                        "error": "[LIMIT_REACHED] Venice: Daily chat limit reached — upgrade to Pro to continue",
                        "model": "DeepSeek V4.1 Flash Uncensored",
                    }
                else:
                    payload = {
                        "id": item["id"],
                        "client_id": cid,
                        "response": "Claude resumed Venice context with VAL=99.",
                        "model": "Claude Sonnet",
                        "chat_url": "https://claude.ai/chat/c-from-venice",
                    }
                req = urllib.request.Request(
                    f"{base_url}/pending-response",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=2.0) as post_resp:
                    post_resp.read()

        from wsa_server import config as _srv_cfg
        with _srv_cfg._CONFIG_LOCK:
            orig_cfg_active_relay = _srv_cfg.CONFIG.get("active_relay")
            _srv_cfg.CONFIG["active_relay"] = ""
        ext_thread = threading.Thread(target=_ext_worker, daemon=True)
        try:
            with relay._dom_relay_lock:
                relay._dom_relay["relays"] = {}
                relay._dom_relay["active_relay_id"] = None
                relay._dom_relay["pinned_relay_label"] = None
                relay._dom_relay["explicit_relay_lock"] = False
                relay._dom_relay["pending"] = {}
                relay._dom_relay["sessions"] = {}
                relay._dom_relay["browser_cmds"] = {}
                relay._dom_relay["cancelled_req_ids"] = {}
                relay._dom_relay["last_poll_ts"] = 0.0
                relay._PENDING_SESSION_META.clear()
            ext_thread.start()
            real_client = client_mod.WSAClient(api_base=f"{base_url}/v1", api_key=api_key)
            wait_ev = threading.Event()
            h = {}
            for _ in range(50):
                try:
                    h = real_client.get_health()
                    if h.get("dom_relay_active") and (h.get("active_providers") or {}).get("venice", {}).get("has_tab"):
                        break
                except Exception:
                    pass
                wait_ev.wait(0.05)
            self.assertTrue((h.get("active_providers") or {}).get("venice", {}).get("has_tab"), msg=f"Venice health not synced: {h}")

            orig_root = tools.get_project_root()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "mod.py").write_text("VAL = 99\n", encoding="utf-8")
                    st = SessionState(project_root=root, session_id="e2e_venice_sess", api_base=f"{base_url}/v1", api_key=api_key)
                    st.auto_approve = True
                    st.chat_urls["venice"] = "https://venice.ai/chat/agent/stale_old"

                    with patch.object(renderer.console, "print"):
                        handled, cont = commands.handle_command("/multiagent chatgpt vernice", st, client=real_client)
                        self.assertTrue(handled and cont)
                        self.assertEqual(st.multiagent_queue, ["chatgpt", "venice"])
                        res1 = agent_module.execute_agent_loop("Check mod.py VAL", st, real_client, quiet_ui=True)
                        res2 = agent_module.execute_agent_loop("Now summarize VAL", st, real_client, quiet_ui=True)

                    self.assertTrue(res1.get("ok"), msg=res1)
                    self.assertTrue(res2.get("ok"), msg=res2)
                    self.assertEqual([d["provider"] for d in dispatched], ["chatgpt", "venice", "venice", "venice"])

                    # Failover to Venice uses fresh chat & full context; subsequent rounds + Turn 2 reuse same uuFvVgZ chat!
                    v_fail, v_tool, v_turn2 = dispatched[1], dispatched[2], dispatched[3]
                    self.assertTrue(v_fail["new_chat"])
                    self.assertEqual(v_fail.get("chat_url") or "", "")
                    self.assertIn("Check mod.py VAL", v_fail["prompt"])

                    self.assertFalse(v_tool["new_chat"])
                    self.assertEqual(v_tool["chat_url"], "https://venice.ai/chat/agent/uuFvVgZ")
                    self.assertIn("VAL = 99", v_tool["prompt"])

                    self.assertFalse(v_turn2["new_chat"])
                    self.assertEqual(v_turn2["chat_url"], "https://venice.ai/chat/agent/uuFvVgZ")
                    self.assertEqual(st.model, "venice")
                    self.assertEqual(st.active_chat_url, "https://venice.ai/chat/agent/uuFvVgZ")
                    self.assertEqual(st.active_web_model, "DeepSeek V4.1 Flash Uncensored")

                    # Turn 3: Venice hits daily limit -> fails over cleanly to Claude with full project context intact
                    with patch.object(renderer.console, "print"):
                        handled3, cont3 = commands.handle_command("/multiagent venice claude", st, client=real_client)
                        self.assertTrue(handled3 and cont3)
                        res3 = agent_module.execute_agent_loop("Confirm previous result", st, real_client, quiet_ui=True)
                    self.assertTrue(res3.get("ok"), msg=res3)
                    self.assertEqual([d["provider"] for d in dispatched], ["chatgpt", "venice", "venice", "venice", "venice", "claude"])
                    claude_from_v = dispatched[5]
                    self.assertTrue(claude_from_v["new_chat"])
                    self.assertIn("Hi Claude —", claude_from_v["prompt"])
                    self.assertIn("Confirm previous result", claude_from_v["prompt"])
                    self.assertIn("Venice verified VAL=99", claude_from_v["prompt"])
                    self.assertEqual(st.model, "claude")
                    self.assertEqual(st.active_chat_url, "https://claude.ai/chat/c-from-venice")
            finally:
                tools.set_project_root(orig_root)
        finally:
            stop_ext.set()
            ext_thread.join(timeout=2.0)
            httpd.shutdown()
            httpd.server_close()
            with relay._dom_relay_lock:
                for k, v in orig_relay_state.items():
                    relay._dom_relay[k] = v
                relay._PENDING_SESSION_META.clear()
                relay._PENDING_SESSION_META.update(orig_pending_meta)
            with _srv_cfg._CONFIG_LOCK:
                if orig_cfg_active_relay is None:
                    _srv_cfg.CONFIG.pop("active_relay", None)
                else:
                    _srv_cfg.CONFIG["active_relay"] = orig_cfg_active_relay

    def test_singleagent_limit_recovery_countdown_preservation_and_stop(self):
        """Single-agent limit hit preserves state+context, prompts y/n (bypass-exempt), runs 60s countdown on y, stops cleanly on n."""
        from wsa_agent import agent as agent_module, renderer, tools
        from wsa_agent.client import ProviderLimitError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            (root / "app.py").write_text("VALUE = 123\n", encoding="utf-8")
            sess_file = Path(tmp) / "saved-session.json"
            orig_root = tools.get_project_root()
            tools.set_project_root(root)
            try:
                # ── Case 1: Single-agent Claude mid-turn limit -> 'y' -> 60s countdown -> fresh chat + full context ──
                st = SessionState(project_root=root, selected_model="claude", session_id="sa-lim-y")
                st.auto_approve = True  # Bypass-exempt check: must still prompt Y/N!
                st.decisions = ["Keep VALUE = 123 intact"]
                st.plan_items = [{"id": "t1", "title": "Inspect app.py", "status": "in_progress"}]
                st.active_account_email = "acct1@example.com"
                st.providers_info = {"claude": {"has_tab": True, "email": "acct1@example.com"}}
                st.chat_urls["claude"] = "https://claude.ai/chat/old-sa-1"

                stream_calls = []
                cleared_calls = []
                sleep_calls = []
                printed_lines = []

                class _SingleRecoveryClient:
                    api_base = "http://127.0.0.1:8081/v1"
                    api_key = ""

                    def get_health(self):
                        # Before retry finishes countdown, account switches to acct2
                        if cleared_calls:
                            return {
                                "ok": True,
                                "active_relay": "chrome",
                                "active_browser": "chrome",
                                "active_email": "acct2@example.com",
                                "active_provider": "claude",
                                "active_providers": {
                                    "claude": {"has_tab": True, "email": "acct2@example.com", "model_badge": "Sonnet 4.6"}
                                },
                            }
                        return {
                            "ok": True,
                            "active_relay": "chrome",
                            "active_browser": "chrome",
                            "active_email": "acct1@example.com",
                            "active_provider": "claude",
                            "active_providers": {
                                "claude": {"has_tab": True, "email": "acct1@example.com", "model_badge": "Sonnet 4.6"}
                            },
                        }

                    def clear_session_chats(self, session_id="", providers=None, include_roles=True, client_urls=None):
                        cleared_calls.append({
                            "session_id": session_id,
                            "providers": list(providers or []),
                            "client_urls": list(client_urls or []),
                        })
                        return {"ok": True, "cleared": 1}

                    def stream(self, messages, model, **kwargs):
                        call_idx = len(stream_calls)
                        stream_calls.append({
                            "model": model,
                            "new_chat": kwargs.get("new_chat"),
                            "chat_url": kwargs.get("chat_url"),
                            "messages": list(messages),
                        })
                        if call_idx == 0:
                            return {
                                "content": '```tool_call\n{"name": "read_file", "arguments": {"path": "app.py"}}\n```',
                                "tool_calls": [],
                                "upstream_model": {"provider": "claude", "badge": "Sonnet 4.6", "chat_url": "https://claude.ai/chat/old-sa-1"},
                            }
                        if call_idx == 1:
                            raise ProviderLimitError("[LIMIT_REACHED] Claude: 5-hour usage limit reached", provider="claude")
                        return {
                            "content": "Recovered in fresh acct2 chat with VALUE=123.",
                            "tool_calls": [],
                            "upstream_model": {
                                "provider": "claude",
                                "badge": "Sonnet 4.6",
                                "account_email": "acct2@example.com",
                                "chat_url": "https://claude.ai/chat/new-sa-acct2",
                            },
                        }

                def _capture_print(*args, **kwargs):
                    printed_lines.append(" ".join(str(a) for a in args))

                with patch.object(SessionState, "_save_path", new_callable=lambda: property(lambda _s: sess_file)), \
                     patch.object(renderer.console, "print", side_effect=_capture_print), \
                     patch.object(renderer.console, "input", side_effect=["c", "y"]) as mock_inp, \
                     patch.object(agent_module.time, "sleep", side_effect=lambda s: sleep_calls.append(s)):
                    res = agent_module.execute_agent_loop(
                        "Inspect app.py and report VALUE",
                        st,
                        _SingleRecoveryClient(),
                        quiet_ui=False,
                    )

                self.assertTrue(res.get("ok"), msg=res)
                self.assertEqual(res.get("final_text"), "Recovered in fresh acct2 chat with VALUE=123.")
                self.assertEqual(mock_inp.call_count, 2)
                self.assertEqual(len(sleep_calls), 60)
                joined = "\n".join(printed_lines)
                self.assertIn("please change your current account to continue the workflow: y/n", joined.lower())
                self.assertIn("waiting 60 secs", joined.lower())
                self.assertIn("retrying", joined.lower())
                self.assertEqual(len(cleared_calls), 1)
                self.assertIn("https://claude.ai/chat/old-sa-1", cleared_calls[0]["client_urls"])
                # Retry call (call #2) forced a brand new chat without old chat_url and carried prior tool output
                retry_call = stream_calls[2]
                self.assertTrue(retry_call["new_chat"])
                self.assertIsNone(retry_call["chat_url"])
                retry_dump = json.dumps(retry_call["messages"])
                self.assertIn("VALUE = 123", retry_dump)
                self.assertIn("Keep VALUE = 123 intact", retry_dump)
                self.assertEqual(st.active_chat_url, "https://claude.ai/chat/new-sa-acct2")
                self.assertEqual(st.active_account_email, "acct2@example.com")
                self.assertTrue(sess_file.exists())

                # ── Case 2: Single-agent DeepSeek limit -> 'n' -> saves state+context, clears old chat URL, stops cleanly ──
                st_n = SessionState(project_root=root, selected_model="deepseek", session_id="sa-lim-n")
                st_n.chat_urls["deepseek"] = "https://chat.deepseek.com/a/chat/s/old-ds-1"
                st_n.decisions = ["Use strict DeepSeek 18KB budget"]
                n_printed = []
                n_cleared = []

                class _SingleDeclineClient:
                    def clear_session_chats(self, session_id="", providers=None, include_roles=True, client_urls=None):
                        n_cleared.append((session_id, list(providers or []), list(client_urls or [])))
                        return {"ok": True, "cleared": 1}

                    def stream(self, messages, model, **kwargs):
                        raise ProviderLimitError("[LIMIT_REACHED] DeepSeek: DeepThink limit reached for today", provider="deepseek")

                with patch.object(SessionState, "_save_path", new_callable=lambda: property(lambda _s: sess_file)), \
                     patch.object(renderer.console, "print", side_effect=lambda *a, **k: n_printed.append(" ".join(str(x) for x in a))), \
                     patch.object(renderer.console, "input", side_effect=["c", "n"]):
                    res_n = agent_module.execute_agent_loop(
                        "Finish refactor of billing module",
                        st_n,
                        _SingleDeclineClient(),
                        quiet_ui=False,
                    )

                self.assertFalse(res_n.get("ok"))
                self.assertEqual(st_n.chat_urls.get("deepseek", ""), "")
                self.assertTrue(getattr(st_n, "_force_new_chat_next", False))
                self.assertIn("Finish refactor of billing module", st_n.context_summary)
                self.assertTrue(n_cleared)
                self.assertIn("context preserved, stopping workflow", "\n".join(n_printed).lower())
                saved_disk = json.loads(sess_file.read_text(encoding="utf-8"))
                self.assertIn("Finish refactor of billing module", saved_disk.get("context_summary", ""))

                # ── Case 3: KeyboardInterrupt during 60s countdown stops cleanly with context preserved ──
                st_k = SessionState(project_root=root, selected_model="venice", session_id="sa-lim-ctrlc")
                st_k.chat_urls["venice"] = "https://venice.ai/chat/agent/old-ven-1"
                k_printed = []

                class _InterruptCountdownClient:
                    def stream(self, messages, model, **kwargs):
                        raise ProviderLimitError("[LIMIT_REACHED] Venice: Daily chat limit reached", provider="venice")

                def _interrupt_after_two(_sec):
                    if len(k_printed) >= 1:
                        raise KeyboardInterrupt()

                with patch.object(SessionState, "_save_path", new_callable=lambda: property(lambda _s: sess_file)), \
                     patch.object(renderer.console, "print", side_effect=lambda *a, **k: k_printed.append(" ".join(str(x) for x in a))), \
                     patch.object(renderer.console, "input", side_effect=["c", "y"]), \
                     patch.object(agent_module.time, "sleep", side_effect=_interrupt_after_two):
                    res_k = agent_module.execute_agent_loop(
                        "Verify Venice stop behavior",
                        st_k,
                        _InterruptCountdownClient(),
                        quiet_ui=False,
                    )
                self.assertEqual(res_k.get("aborted_reason"), "interrupted")
                self.assertEqual(st_k.chat_urls.get("venice", ""), "")
                self.assertIn("context preserved, stopping workflow", "\n".join(k_printed).lower())
            finally:
                tools.set_project_root(orig_root)

    def test_multiagent_exhaustion_and_account_switch_prevents_stale_chat_e2e(self):
        """Full HTTP server + WSAClient + extension storage check: account switch never reuses old account chat URL in single or multiagent."""
        import subprocess
        import time
        import urllib.parse
        import urllib.request
        from wsa_agent import agent as agent_module, commands, renderer, tools
        from wsa_agent.client import WSAClient
        from wsa_server import config as _srv_cfg, relay, server as srv_mod

        with relay._dom_relay_lock:
            orig_relay_state = {
                "pending": dict(relay._dom_relay.get("pending") or {}),
                "cancelled_req_ids": dict(relay._dom_relay.get("cancelled_req_ids") or {}),
                "browser_cmds": dict(relay._dom_relay.get("browser_cmds") or {}),
                "sessions": {k: dict(v) for k, v in (relay._dom_relay.get("sessions") or {}).items()},
                "relays": {k: dict(v) for k, v in (relay._dom_relay.get("relays") or {}).items()},
                "active_relay_id": relay._dom_relay.get("active_relay_id"),
                "pinned_relay_label": relay._dom_relay.get("pinned_relay_label"),
                "explicit_relay_lock": relay._dom_relay.get("explicit_relay_lock"),
                "recent_requests": dict(relay._dom_relay.get("recent_requests") or {}),
                "invalidated_chat_urls": dict(relay._dom_relay.get("invalidated_chat_urls") or {}),
                "last_poll_ts": relay._dom_relay.get("last_poll_ts", 0.0),
            }
            orig_pending_meta = dict(relay._PENDING_SESSION_META)
        with _srv_cfg._CONFIG_LOCK:
            orig_cfg_active_relay = _srv_cfg.CONFIG.get("active_relay")
            _srv_cfg.CONFIG["active_relay"] = "chrome"

        with relay._dom_relay_lock:
            relay._dom_relay["pending"].clear()
            relay._dom_relay["sessions"].clear()
            relay._dom_relay["relays"].clear()
            relay._dom_relay["recent_requests"].clear()
            relay._dom_relay["invalidated_chat_urls"].clear()
            relay._dom_relay["active_relay_id"] = None
            relay._dom_relay["pinned_relay_label"] = None
            relay._dom_relay["explicit_relay_lock"] = False
            relay._PENDING_SESSION_META.clear()

        httpd = srv_mod.ThreadedServer(("127.0.0.1", 0), srv_mod.WSAHandler)
        port = httpd.server_address[1]
        base_url = f"http://127.0.0.1:{port}"
        api_key = str((srv_mod.CONFIG.get("api_keys") or [""])[0] or "")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        phase = {"acct": "A"}
        dispatched = []
        stop_ext = threading.Event()
        sleep_ticks = []

        def _post_json(path, payload):
            req = urllib.request.Request(
                f"{base_url}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return json.loads(resp.read().decode("utf-8"))

        def _ext_worker():
            last_sent_acct = None
            while not stop_ext.is_set():
                try:
                    cur_acct = phase["acct"]
                    c_email = f"acct{cur_acct}@example.com"
                    g_email = f"gem{cur_acct}@example.com"
                    g_auth = "0" if cur_acct == "A" else ("1" if cur_acct == "B" else "2")
                    if cur_acct != last_sent_acct:
                        _post_json(
                            "/relay/select",
                            {
                                "client_id": "chrome-main",
                                "browser": "chrome",
                                "label": "chrome",
                                "active_site": "claude.ai",
                                "provider": "claude",
                                "email": c_email,
                                "model_badge": "Sonnet 4.6",
                                "auth_user": g_auth,
                                "providers": {
                                    "claude": {"has_tab": True, "email": c_email, "model_badge": "Sonnet 4.6"},
                                    "gemini": {"has_tab": True, "email": g_email, "auth_user": g_auth, "model_badge": "4 Argon High"},
                                },
                            },
                        )
                        last_sent_acct = cur_acct
                    qs = urllib.parse.urlencode({
                        "client_id": "chrome-main",
                        "browser": "chrome",
                        "label": "chrome",
                        "provider": "claude",
                        "has_claude": "1",
                        "claude_model": "Sonnet 4.6",
                        "claude_email": c_email,
                        "has_gemini": "1",
                        "gemini_model": "4 Argon High",
                        "gemini_email": g_email,
                        "auth_user": g_auth,
                    })
                    with urllib.request.urlopen(f"{base_url}/pending-prompt?{qs}", timeout=2.0) as resp:
                        item = json.loads(resp.read().decode("utf-8"))
                    if item and item.get("id"):
                        rid = item["id"]
                        prov = str(item.get("provider") or "gemini")
                        dispatched.append(dict(item))
                        if phase["acct"] == "A" and len(dispatched) == 1:
                            # Turn 1 succeeds on Claude Account A
                            _post_json(
                                "/pending-response",
                                {
                                    "id": rid,
                                    "client_id": "chrome-main",
                                    "provider": "claude",
                                    "model": "Sonnet 4.6",
                                    "chat_url": "https://claude.ai/chat/c-acctA-old",
                                    "response": "Turn 1 on Claude Account A complete.",
                                    "thinking": "Analyzed initial file.",
                                },
                            )
                        elif phase["acct"] == "A" and prov in ("claude", "gemini"):
                            # Both queue providers hit limits on Account A
                            _post_json(
                                "/pending-response",
                                {
                                    "id": rid,
                                    "client_id": "chrome-main",
                                    "provider": prov,
                                    "error": f"[LIMIT_REACHED] {prov}: usage limit exceeded",
                                },
                            )
                        else:
                            # Account B or C: fresh chat answer
                            cur_tag = phase["acct"]
                            chat_fresh = (
                                f"https://gemini.google.com/u/{g_auth}/app/gem-acct{cur_tag}-fresh"
                                if prov == "gemini"
                                else f"https://claude.ai/chat/c-acct{cur_tag}-fresh"
                            )
                            _post_json(
                                "/pending-response",
                                {
                                    "id": rid,
                                    "client_id": "chrome-main",
                                    "provider": prov,
                                    "model": "Sonnet 4.6" if prov == "claude" else "4 Argon High",
                                    "chat_url": chat_fresh,
                                    "response": f"Fresh reply on Account {cur_tag} ({prov}).",
                                    "thinking": "Continuing from preserved state.",
                                },
                            )
                except Exception:
                    pass
                stop_ext.wait(0.03)

        ext_thread = threading.Thread(target=_ext_worker, daemon=True)
        ext_thread.start()

        try:
            deadline = time.time() + 5.0
            while time.time() < deadline and not relay.is_dom_relay_active():
                time.sleep(0.05)
            self.assertTrue(relay.is_dom_relay_active())

            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "proj"
                root.mkdir()
                (root / "main.py").write_text("Z = 1\n", encoding="utf-8")
                sess_file = Path(tmp) / "multi-sess.json"
                orig_root = tools.get_project_root()
                tools.set_project_root(root)
                try:
                    st = SessionState(project_root=root, session_id="e2e-acct-switch")
                    real_client = WSAClient(api_base=f"{base_url}/v1", api_key=api_key, timeout=15)
                    with patch.object(renderer.console, "print"):
                        commands.handle_command("/multiagent claude gemini", st, client=real_client)

                    # Turn 1: establishes old conversation on Claude Account A
                    with patch.object(SessionState, "_save_path", new_callable=lambda: property(lambda _s: sess_file)), \
                         patch.object(renderer.console, "print"):
                        r1 = agent_module.execute_agent_loop("Turn 1 setup", st, real_client, quiet_ui=False)
                    self.assertTrue(r1.get("ok"), msg=r1)
                    self.assertEqual(st.chat_urls.get("claude"), "https://claude.ai/chat/c-acctA-old")

                    # Turn 2: Claude (A) hits limit -> fails over to Gemini (A) -> hits limit -> y -> 60s countdown ->
                    # during countdown user switches Chrome to Account B -> retry on slot 1 Claude (B) uses fresh chat!
                    real_sleep = time.sleep

                    def _countdown_tick(_sec):
                        sleep_ticks.append(_sec)
                        if len(sleep_ticks) == 30:
                            phase["acct"] = "B"
                            # Let ext_worker register acctB before countdown finishes
                            real_sleep(0.15)

                    printed = []
                    with patch.object(SessionState, "_save_path", new_callable=lambda: property(lambda _s: sess_file)), \
                         patch.object(renderer.console, "print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))), \
                         patch.object(renderer.console, "input", return_value="y"), \
                         patch.object(agent_module.time, "sleep", side_effect=_countdown_tick):
                        r2 = agent_module.execute_agent_loop("Turn 2 continue task", st, real_client, quiet_ui=False)

                    self.assertTrue(r2.get("ok"), msg=r2)
                    self.assertEqual(len(sleep_ticks), 60)
                    self.assertEqual([d["provider"] for d in dispatched], ["claude", "claude", "gemini", "claude"])
                    retry_item = dispatched[3]
                    self.assertTrue(retry_item["new_chat"], msg=retry_item)
                    self.assertEqual(retry_item["chat_url"], "", msg=retry_item)
                    self.assertIn("Turn 1 on Claude Account A complete.", retry_item["prompt"])
                    self.assertEqual(st.chat_urls.get("claude"), "https://claude.ai/chat/c-acctB-fresh")
                    self.assertEqual(st.active_account_email, "acctB@example.com")

                    # Turn 3: Passive browser account switch (Account B -> Account C) between turns with NO limit hit
                    # must STILL detect account switch, clear stale B chat URL, and start fresh chat in C!
                    phase["acct"] = "C"
                    real_sleep(0.2)
                    with patch.object(SessionState, "_save_path", new_callable=lambda: property(lambda _s: sess_file)), \
                         patch.object(renderer.console, "print"):
                        r3 = agent_module.execute_agent_loop("Turn 3 after passive account switch", st, real_client, quiet_ui=False)
                    self.assertTrue(r3.get("ok"), msg=r3)
                    self.assertEqual(len(dispatched), 5)
                    turn3_item = dispatched[4]
                    self.assertTrue(turn3_item["new_chat"], msg=turn3_item)
                    self.assertEqual(turn3_item["chat_url"], "", msg=turn3_item)
                    self.assertEqual(st.chat_urls.get("claude"), "https://claude.ai/chat/c-acctC-fresh")
                    self.assertEqual(st.active_account_email, "acctC@example.com")

                    # Stale Account-A & Account-B chat URLs are rejected even if explicitly hinted on server!
                    self.assertTrue(
                        relay.is_chat_url_stale_for_relay(
                            "https://claude.ai/chat/c-acctA-old",
                            provider="claude",
                            relay_id="chrome-main",
                        )
                    )
                    self.assertFalse(
                        relay.seed_session_hint_url(
                            st.session_id,
                            "https://claude.ai/chat/c-acctA-old",
                            provider="claude",
                            relay_id="chrome-main",
                        )
                    )
                    self.assertFalse(
                        relay.seed_session_hint_url(
                            st.session_id,
                            "https://claude.ai/chat/c-acctB-fresh",
                            provider="claude",
                            relay_id="chrome-main",
                        )
                    )

                    # Extension storage unit check in Node: clearRelaySession removes stale URLs for account switches
                    bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
                    node_code = f"""
const assert = require("assert");
const bg = require({json.dumps(bg_path)});
(async () => {{
  const store = {{}};
  global.chrome = {{
    storage: {{
      local: {{
        get: async (k) => (typeof k === "string" ? {{ [k]: store[k] }} : {{ ...store }}),
        set: async (obj) => Object.assign(store, obj),
      }}
    }}
  }};
  await bg.saveRelaySession("s1", {{ chatUrl: "https://claude.ai/chat/old", convId: "old", accountEmail: "a@x.com" }}, "claude");
  await bg.saveRelaySession("s1", {{ chatUrl: "https://gemini.google.com/app/g1", convId: "g1", accountEmail: "ga@x.com" }}, "gemini");
  let map = await bg.getRelaySessions();
  assert.strictEqual(Object.keys(map).length, 2);
  const removed = await bg.clearRelaySession("s1", "claude");
  assert.strictEqual(removed, 1);
  map = await bg.getRelaySessions();
  assert.ok(!map["s1::claude"]);
  assert.ok(map["s1"]);
  await bg.clearRelaySession("s1", "");
  map = await bg.getRelaySessions();
  assert.strictEqual(Object.keys(map).length, 0);
  console.log("EXT_CLEAR_RELAY_OK");
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
                    proc = subprocess.run(["node", "-e", node_code], capture_output=True, text=True, check=False)
                    self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
                    self.assertIn("EXT_CLEAR_RELAY_OK", proc.stdout)
                finally:
                    tools.set_project_root(orig_root)
        finally:
            stop_ext.set()
            ext_thread.join(timeout=2.0)
            httpd.shutdown()
            httpd.server_close()
            with relay._dom_relay_lock:
                for k, v in orig_relay_state.items():
                    relay._dom_relay[k] = v
                relay._PENDING_SESSION_META.clear()
                relay._PENDING_SESSION_META.update(orig_pending_meta)
            with _srv_cfg._CONFIG_LOCK:
                if orig_cfg_active_relay is None:
                    _srv_cfg.CONFIG.pop("active_relay", None)
                else:
                    _srv_cfg.CONFIG["active_relay"] = orig_cfg_active_relay

    def test_orchestrated_team_limit_recovery_and_audit_edge_cases(self):
        """Deep-audit fixes: /team stage limit recovery, interrupted-goal cleanup, Gemini auth_user switch isolation, and stale URL non-reentry."""
        import subprocess
        from wsa_agent import agent as agent_module, renderer, subagents, tools
        from wsa_server import relay

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            (root / "a.py").write_text("A = 1\n", encoding="utf-8")
            (root / "b.py").write_text("B = 2\n", encoding="utf-8")
            sess_file = Path(tmp) / "team-sess.json"
            orig_root = tools.get_project_root()
            tools.set_project_root(root)
            try:
                # 1. /team orchestrated Coder slice hits limit -> 'y' -> 60s countdown -> resets role/provider chats & retries slice!
                st = SessionState(project_root=root, selected_model="claude", session_id="team-lim-1")
                st.role_threads = {"explorer": "https://claude.ai/chat/exp-1", "coder": "https://claude.ai/chat/cod-old"}
                st.chat_urls["claude"] = "https://claude.ai/chat/main-old"
                st.chat_urls["claude:role:coder"] = "https://claude.ai/chat/cod-old"

                cleared_calls = []
                sleep_calls = []
                coder_calls = []

                class _TeamClient:
                    api_base = "http://127.0.0.1:8081/v1"
                    api_key = ""

                    def get_health(self):
                        return {
                            "ok": True,
                            "active_relay": "chrome",
                            "active_browser": "chrome",
                            "active_email": "newteam@example.com",
                            "active_provider": "claude",
                            "active_providers": {"claude": {"has_tab": True, "email": "newteam@example.com"}},
                        }

                    def get_relays(self):
                        return {"ok": True, "relays": []}

                    def clear_session_chats(self, session_id="", providers=None, include_roles=True, client_urls=None):
                        cleared_calls.append((session_id, list(providers or []), include_roles))
                        return {"ok": True, "cleared": 2}

                def _fake_role_agent(**kwargs):
                    r = kwargs["role"]
                    lbl = kwargs.get("label", "")
                    if r == "explorer":
                        return {"ok": True, "final_text": "Found a.py and b.py", "written_files": [], "aborted_reason": ""}
                    if r == "architect":
                        return {
                            "ok": True,
                            "final_text": '```json\n[{"id":"t1","title":"Update a","depends_on":[],"owned_files":["a.py"],"acceptance":"done"}]\n```',
                            "written_files": [],
                            "aborted_reason": "",
                        }
                    if r == "coder":
                        coder_calls.append({"label": lbl, "chat_url": st.get_role_chat_url("coder", "claude")})
                        if len(coder_calls) == 1:
                            return {"ok": False, "aborted_reason": "[LIMIT_REACHED] Claude: 5-hour limit", "limit_exhausted": True, "written_files": []}
                        return {"ok": True, "final_text": "Updated a.py cleanly.", "written_files": ["a.py"], "aborted_reason": ""}
                    return {
                        "ok": True,
                        "final_text": '```json\n{"verdict":"pass","checks":["ok"],"failures":[],"summary":"Verified"}\n```',
                        "written_files": [],
                        "aborted_reason": "",
                        "verification_evidence": [{"tool": "run_tests", "ok": True, "result_class": "passed"}],
                    }

                with patch.object(SessionState, "_save_path", new_callable=lambda: property(lambda _s: sess_file)), \
                     patch.object(subagents, "_run_role_agent", side_effect=_fake_role_agent), \
                     patch.object(renderer.console, "print"), \
                     patch.object(renderer.console, "input", side_effect=["c", "y"]), \
                     patch.object(agent_module.time, "sleep", side_effect=lambda s: sleep_calls.append(s)):
                    orch_out = subagents.run_orchestrated_turn("Update a.py via team", st, _TeamClient())

                self.assertTrue(orch_out.get("ok"), msg=orch_out)
                self.assertNotIn("ok=False", orch_out.get("summary", ""))
                self.assertEqual(len(coder_calls), 2)
                self.assertEqual(coder_calls[0]["chat_url"], "https://claude.ai/chat/cod-old")
                self.assertEqual(coder_calls[1]["chat_url"], "")
                self.assertEqual(len(sleep_calls), 60)
                self.assertTrue(any(c[0] == "team-lim-1" and c[2] is True for c in cleared_calls))

                # 2. Stale [Interrupted Pending Goal] line is cleared automatically once next turn completes cleanly
                st_g = SessionState(project_root=root, selected_model="claude", session_id="goal-clean")
                st_g.context_summary = "Key Prior Findings: X=1\n[Interrupted Pending Goal]: Old failed task"

                class _OkClient:
                    def stream(self, messages, model, **kwargs):
                        return {"content": "Completed now.", "tool_calls": []}

                with patch.object(SessionState, "_save_path", new_callable=lambda: property(lambda _s: sess_file)):
                    res_g = agent_module.execute_agent_loop("Finish now", st_g, _OkClient(), quiet_ui=True)
                self.assertTrue(res_g.get("ok"))
                self.assertNotIn("[Interrupted Pending Goal]:", st_g.context_summary)
                self.assertIn("Key Prior Findings: X=1", st_g.context_summary)

                # 3. sync_account_from_res clears on Gemini auth_user switch & does not re-latch unrelated chat_url on relay_switched
                st_s = SessionState(project_root=root, selected_model="gemini", session_id="auth-res")
                st_s.active_account_label = "chrome"
                st_s.active_browser = "chrome"
                st_s.providers_info = {"gemini": {"has_tab": True, "email": "", "auth_user": "0"}}
                st_s.chat_urls["gemini"] = "https://gemini.google.com/u/0/app/old0"
                st_s.messages = [{"role": "user", "content": "hi"}]
                st_s.turn_count = 1
                st_s.sync_account_from_res({
                    "browser": "chrome",
                    "active_relay_label": "chrome",
                    "providers": {"gemini": {"has_tab": True, "email": "", "auth_user": "1"}},
                })
                self.assertEqual(st_s.chat_urls.get("gemini", ""), "")
                # Relay switch (chrome -> brave) must NOT re-latch an unrelated tab chat_url
                st_s.chat_urls["claude"] = "https://claude.ai/chat/chrome-tab"
                st_s.sync_account_from_res({
                    "browser": "brave",
                    "active_relay_label": "brave",
                    "chat_url": "https://claude.ai/chat/brave-unrelated",
                    "cleared_chats": 0,
                })
                self.assertEqual(st_s.chat_urls.get("claude", ""), "")

                # 4. Server _merge_relay_providers clears inherited Gemini email on auth_user change & record_session_relay_turn rejects stale URL
                merged = relay._merge_relay_providers(
                    {"gemini": {"has_tab": True, "account_email": "gem0@x.com", "auth_user": "0", "url": "https://gemini.google.com/u/0/app/g0"}},
                    {"provider": "gemini", "auth_user": "1", "providers": {"gemini": {"has_tab": True, "auth_user": "1", "email": ""}}},
                )
                self.assertEqual(merged["gemini"]["auth_user"], "1")
                self.assertEqual(merged["gemini"]["account_email"], "")
                self.assertEqual(merged["gemini"]["url"], "")

                with relay._dom_relay_lock:
                    orig_sess = dict(relay._dom_relay.get("sessions") or {})
                    orig_inv = dict(relay._dom_relay.get("invalidated_chat_urls") or {})
                    relay._dom_relay["sessions"]["audit-stale::claude"] = {
                        "chat_url": "https://claude.ai/chat/stale-evicted",
                        "provider": "claude",
                        "relay_id": "r1",
                    }
                try:
                    relay.clear_session_relay_chats(session_id="audit-stale", providers=["claude"])
                    relay.record_session_relay_turn(
                        "audit-stale",
                        "Late completion",
                        None,
                        chat_url="https://claude.ai/chat/stale-evicted",
                        relay_id="r1",
                        provider="claude",
                    )
                    with relay._dom_relay_lock:
                        rec = (relay._dom_relay.get("sessions") or {}).get("audit-stale::claude") or {}
                        self.assertEqual(rec.get("chat_url", ""), "")
                finally:
                    with relay._dom_relay_lock:
                        relay._dom_relay["sessions"] = orig_sess
                        relay._dom_relay["invalidated_chat_urls"] = orig_inv

                # 5. Extension finalUrl `/u/2/` authUser precedence over job.authUser="0"
                bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
                node_auth = f"""
const assert = require("assert");
const bg = require({json.dumps(bg_path)});
const u = "https://gemini.google.com/u/2/app/xyz123";
assert.strictEqual(bg.parseAuthUserFromUrl(u), "2");
const resolved = /^\\/u\\/\\d+(?:\\/|$)/.test(new URL(u).pathname) ? bg.parseAuthUserFromUrl(u) : "0";
assert.strictEqual(resolved, "2");
console.log("AUTH_PRECEDENCE_OK");
"""
                proc = subprocess.run(["node", "-e", node_auth], capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
                self.assertIn("AUTH_PRECEDENCE_OK", proc.stdout)
            finally:
                tools.set_project_root(orig_root)


if __name__ == "__main__":
    unittest.main()
