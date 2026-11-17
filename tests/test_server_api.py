import http.client
import base64
import json
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs

import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import wsa_server as wsa_server_module

CONFIG = wsa_server_module.CONFIG
DEFAULT_CONFIG = wsa_server_module.DEFAULT_CONFIG
def _build_payload(prompt, mode, slot80, file_refs=None):
    from urllib.parse import urlencode
    url, params, headers = wsa_server_module._build_upstream_request(prompt, {"mode": mode, "slot80": slot80}, file_refs)
    return urlencode(params)
WSAHandler = wsa_server_module.WSAHandler
GeminiHandler = wsa_server_module.GeminiHandler
ThreadedServer = getattr(wsa_server_module, 'ThreadedServer', None)
google_contents_to_prompt = getattr(wsa_server_module, 'google_contents_to_prompt', None)
messages_to_prompt = getattr(wsa_server_module, 'messages_to_prompt', None)


def _decode_payload(payload):
    outer = json.loads(parse_qs(payload)["f.req"][0])
    return json.loads(outer[1])


def _decode_sse(body):
    events = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event_type = next(
            (line[len("event: "):] for line in lines if line.startswith("event: ")),
            None,
        )
        data = next(
            (line[len("data: "):] for line in lines if line.startswith("data: ")),
            None,
        )
        if event_type and data:
            events.append((event_type, json.loads(data)))
    return events


class PayloadPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(CONFIG)

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)

    def test_temporary_chats_default_to_disabled(self):
        self.assertIs(DEFAULT_CONFIG["temporary_chats"], False)

    def test_persistent_chat_payload(self):
        CONFIG["temporary_chats"] = False

        inner = _decode_payload(_build_payload("hello", 1, 4))

        self.assertEqual(inner[41], [2])
        self.assertIsNone(inner[45])

    def test_temporary_chat_payload(self):
        CONFIG["temporary_chats"] = True

        inner = _decode_payload(_build_payload("hello", 1, 4))

        self.assertEqual(inner[41], [1])
        self.assertEqual(inner[45], 1)

    def test_payload_includes_uploaded_image_refs(self):
        inner = _decode_payload(_build_payload("describe", 1, 4, ["/uploaded/image-ref"]))

        self.assertEqual(inner[0][0], "describe")
        self.assertEqual(inner[0][3], [[None, None, "/uploaded/image-ref"]])


class MessageParsingTests(unittest.TestCase):
    def test_messages_to_prompt_extracts_openai_image_url_data_url(self):
        image_data = base64.b64encode(b"fake png").decode()

        prompt, images = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}},
            ],
        }])

        self.assertEqual(prompt, "Describe [Image attached]")
        self.assertEqual(images, [(b"fake png", "image/png")])

    def test_messages_to_prompt_extracts_responses_input_image_url(self):
        prompt, images = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Describe"},
                {"type": "input_image", "image_url": "https://example.com/image.png"},
            ],
        }])

        self.assertEqual(prompt, "Describe [Image attached]")
        self.assertEqual(images, [("https://example.com/image.png", "image/png")])

    def test_messages_to_prompt_ignores_malformed_image_data_url(self):
        prompt, images = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,%%%"}},
            ],
        }])

        self.assertEqual(prompt, "Describe")
        self.assertEqual(images, [])

    def test_google_contents_to_prompt_extracts_inline_image_data(self):
        image_data = base64.b64encode(b"fake png").decode()

        prompt, images = google_contents_to_prompt({
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": "Describe"},
                    {"inlineData": {"mimeType": "image/png", "data": image_data}},
                ],
            }],
        })

        self.assertEqual(prompt, "Describe\n[Image attached]")
        self.assertEqual(images, [(b"fake png", "image/png")])

    def test_google_contents_to_prompt_ignores_malformed_inline_image_data(self):
        prompt, images = google_contents_to_prompt({
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": "Describe"},
                    {"inlineData": {"mimeType": "image/png", "data": "%%%"}},
                ],
            }],
        })

        self.assertEqual(prompt, "Describe")
        self.assertEqual(images, [])


class StreamingEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.original_config = dict(CONFIG)
        CONFIG["api_keys"] = []
        CONFIG["log_requests"] = False

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)

    def post_json(self, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            path,
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read().decode()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    def post_chunked_json(self, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            path,
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            encode_chunked=True,
        )
        response = connection.getresponse()
        body = response.read().decode()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    @mock.patch("wsa_server.server.generate_stream")
    def test_chat_stream_starts_with_assistant_role(self, generate_stream):
        generate_stream.return_value = iter(["hel", "lo"])

        status, headers, body = self.post_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        chunks = [
            json.loads(line[len("data: "):])
            for line in body.splitlines()
            if line.startswith("data: {")
        ]
        self.assertEqual(chunks[0]["choices"][0]["delta"], {"role": "assistant"})
        self.assertEqual(chunks[1]["choices"][0]["delta"], {"content": "hel"})
        self.assertEqual(chunks[2]["choices"][0]["delta"], {"content": "lo"})
        self.assertTrue(body.endswith("data: [DONE]\n\n"))

    @mock.patch("wsa_server.server.generate", return_value="chunked ok")
    def test_chat_accepts_chunked_body(self, _generate):
        status, _, body = self.post_chunked_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "chunked ok")

    @mock.patch("wsa_server.server.upload_image", return_value="/uploaded/image-ref")
    @mock.patch("wsa_server.server.generate", return_value="looks good")
    def test_chat_accepts_openai_image_url_data_url(self, generate, upload_image):
        image_data = base64.b64encode(b"fake png").decode()

        status, _, body = self.post_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_data}"
                            },
                        },
                    ],
                }],
            },
        )

        self.assertEqual(status, 200)
        upload_image.assert_called_once_with(b"fake png", "image.png", "image/png")
        self.assertEqual(generate.call_args.args[3], ["/uploaded/image-ref"])
        self.assertIn("[Image attached]", generate.call_args.args[0])
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "looks good")

    @mock.patch("wsa_server.server.fetch_image_bytes", return_value=b"\xff\xd8\xffremote jpeg")
    @mock.patch("wsa_server.server.upload_image", return_value="/uploaded/remote-ref")
    @mock.patch("wsa_server.server.generate", return_value="remote ok")
    def test_responses_accepts_input_image_url(self, generate, upload_image, fetch_image_bytes):
        status, _, _ = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": [{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "What is shown?"},
                        {
                            "type": "input_image",
                            "image_url": "https://example.com/image.jpg",
                        },
                    ],
                }],
            },
        )

        self.assertEqual(status, 200)
        fetch_image_bytes.assert_called_once_with("https://example.com/image.jpg")
        upload_image.assert_called_once_with(b"\xff\xd8\xffremote jpeg", "image.png", "image/jpeg")
        self.assertEqual(generate.call_args.args[3], ["/uploaded/remote-ref"])
        self.assertIn("[Image attached]", generate.call_args.args[0])

    @mock.patch("wsa_server.server.upload_image", return_value="/uploaded/image-ref")
    @mock.patch("wsa_server.server.generate", return_value="top-level image ok")
    def test_responses_accepts_top_level_input_image(self, generate, upload_image):
        image_data = base64.b64encode(b"fake png").decode()

        status, _, _ = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": [
                    {"type": "input_text", "text": "What is shown?"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{image_data}",
                    },
                ],
            },
        )

        self.assertEqual(status, 200)
        upload_image.assert_called_once_with(b"fake png", "image.png", "image/png")
        self.assertEqual(generate.call_args.args[3], ["/uploaded/image-ref"])
        self.assertIn("What is shown?", generate.call_args.args[0])
        self.assertIn("[Image attached]", generate.call_args.args[0])

    @mock.patch("wsa_server.server.upload_image", side_effect=RuntimeError("upload denied"))
    def test_google_image_upload_failure_returns_502(self, _upload_image):
        image_data = base64.b64encode(b"fake png").decode()

        status, _, body = self.post_json(
            "/v1beta/models/gemini-3.6-flash:generateContent",
            {
                "contents": [{
                    "role": "user",
                    "parts": [{
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": image_data,
                        },
                    }],
                }],
            },
        )

        self.assertEqual(status, 502)
        self.assertIn("image upload failed: upload denied", json.loads(body)["error"]["message"])

    @mock.patch("wsa_server.server.generate_stream", return_value=iter(["streamed"]))
    def test_google_stream_generate_content_uses_sse(self, _generate_stream):
        status, headers, body = self.post_json(
            "/v1beta/models/gemini-3.6-flash:streamGenerateContent",
            {
                "contents": [{
                    "role": "user",
                    "parts": [{"text": "Stream this"}],
                }],
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertIn('"text": "streamed"', body)

    @mock.patch("wsa_server.server.generate", return_value="hello")
    def test_responses_text_stream_has_complete_event_sequence(self, _generate):
        status, headers, body = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": "hello",
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        events = _decode_sse(body)
        self.assertEqual(
            [event_type for event_type, _ in events],
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.content_part.added",
                "response.output_text.delta",
                "response.output_text.done",
                "response.content_part.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        self.assertEqual(
            [event["sequence_number"] for _, event in events],
            list(range(1, len(events) + 1)),
        )
        self.assertEqual(events[4][1]["delta"], "hello")
        self.assertEqual(events[-1][1]["response"]["status"], "completed")
        self.assertEqual(events[-1][1]["response"]["output"][0]["content"][0]["text"], "hello")

    @mock.patch("wsa_server.server.parse_tool_calls")
    @mock.patch("wsa_server.server.generate", return_value="tool output")
    def test_responses_function_call_stream_has_complete_event_sequence(
        self, _generate, parse_tool_calls
    ):
        parse_tool_calls.return_value = (
            "",
            [
                {
                    "id": "call_test",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Shanghai"}'},
                }
            ],
        )

        status, _, body = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": "weather",
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {"type": "object"},
                    }
                ],
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        events = _decode_sse(body)
        self.assertEqual(
            [event_type for event_type, _ in events],
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        self.assertEqual(
            [event["sequence_number"] for _, event in events],
            list(range(1, len(events) + 1)),
        )
        self.assertEqual(events[2][1]["output_index"], 0)
        self.assertEqual(events[3][1]["delta"], '{"city":"Shanghai"}')
        self.assertEqual(events[4][1]["arguments"], '{"city":"Shanghai"}')
        self.assertEqual(events[-1][1]["response"]["output"][0]["name"], "get_weather")




class EndToEndDataFlowTests(unittest.TestCase):
    """End-to-end HTTP + browser relay + wsa_agent client/command data-flow verification."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]
        cls.base_v1 = f"http://127.0.0.1:{cls.port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        import tempfile
        from wsa_server import config as cfg_mod, relay
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._orig_script_dir = cfg_mod.SCRIPT_DIR
        cfg_mod.SCRIPT_DIR = self._tmp_dir.name
        with relay._dom_relay_lock:
            self.orig_relay = {
                "last_poll_ts": relay._dom_relay["last_poll_ts"],
                "active_relay_id": relay._dom_relay["active_relay_id"],
                "pinned_relay_label": relay._dom_relay["pinned_relay_label"],
                "explicit_relay_lock": relay._dom_relay["explicit_relay_lock"],
                "relays": dict(relay._dom_relay["relays"]),
                "pending": dict(relay._dom_relay["pending"]),
                "sessions": dict(relay._dom_relay["sessions"]),
            }
            relay._dom_relay["relays"] = {}
            relay._dom_relay["active_relay_id"] = None
            relay._dom_relay["pinned_relay_label"] = None
            relay._dom_relay["explicit_relay_lock"] = False
            relay._dom_relay["pending"].clear()
            relay._dom_relay["sessions"] = {}
        self.orig_config = dict(CONFIG)
        CONFIG["api_keys"] = []
        CONFIG["log_requests"] = False

    def tearDown(self):
        from wsa_server import config as cfg_mod, relay
        cfg_mod.SCRIPT_DIR = self._orig_script_dir
        self._tmp_dir.cleanup()
        with relay._dom_relay_lock:
            relay._dom_relay.update(self.orig_relay)
        CONFIG.clear()
        CONFIG.update(self.orig_config)

    def _request_json(self, method: str, path: str, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps(payload) if payload is not None else None
        headers = {"Content-Type": "application/json"} if body else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        return resp.status, json.loads(raw)

    def test_health_models_and_relays_endpoints(self):
        status_r, root_info = self._request_json("GET", "/")
        self.assertEqual(status_r, 200)
        self.assertEqual(root_info["status"], "ok")
        self.assertIn("gemini-4-argon", root_info["models"])
        self.assertIn("gemini-3.6-flash", root_info["models"])

        status_h, health = self._request_json("GET", "/v1/health")
        self.assertEqual(status_h, 200)
        self.assertEqual(health["status"], "ok")
        self.assertIn("relays", health)

        status_m, models_resp = self._request_json("GET", "/v1/models")
        self.assertEqual(status_m, 200)
        ids = {m["id"] for m in models_resp["data"]}
        self.assertIn("gemini-4-argon", ids)

    def test_extension_relay_end_to_end_chat_completion_and_selection(self):
        s1, r1 = self._request_json("POST", "/sync-cookies", {
            "client_id": "relay-e2e-brave",
            "browser": "brave",
            "label": "brave",
            "cookie": "SAPISID=b_sapi; __Secure-1PSID=b_psid",
            "email": "brave.e2e@example.com",
            "auth_user": "0",
            "model_badge": "4 Argon High",
        })
        self.assertEqual(s1, 200)
        self.assertTrue(r1["ok"])

        s2, r2 = self._request_json("POST", "/sync-cookies", {
            "client_id": "relay-e2e-chrome",
            "browser": "chrome",
            "label": "chrome",
            "cookie": "SAPISID=c_sapi; __Secure-1PSID=c_psid",
            "email": "chrome.e2e@example.com",
            "auth_user": "0",
            "model_badge": "4 Argon High",
        })
        self.assertEqual(s2, 200)
        self.assertTrue(r2["ok"])

        sel_status, sel_data = self._request_json("POST", "/v1/relays/select", {"relay": "chrome"})
        self.assertEqual(sel_status, 200)
        self.assertTrue(sel_data["ok"])
        self.assertEqual(sel_data["active_relay_id"], "relay-e2e-chrome")

        def worker():
            for _ in range(120):
                st, pending = self._request_json(
                    "GET",
                    "/pending-prompt?client_id=relay-e2e-chrome&browser=chrome&label=chrome&model_badge=4%20Argon%20High",
                )
                if st == 200 and pending and pending.get("id"):
                    self._request_json("POST", "/pending-progress", {
                        "id": pending["id"],
                        "client_id": "relay-e2e-chrome",
                        "thinking": "Reasoning step",
                        "response": "Partial",
                        "seq": 1,
                    })
                    self._request_json("POST", "/pending-response", {
                        "id": pending["id"],
                        "client_id": "relay-e2e-chrome",
                        "thinking": "Reasoning step",
                        "response": "Final verified e2e answer",
                        "model": "4 Argon High",
                    })
                    return
                time.sleep(0.02)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        try:
            chat_status, chat_data = self._request_json("POST", "/v1/chat/completions", {
                "model": "gemini-4-argon",
                "messages": [{"role": "user", "content": "Say hello"}],
                "stream": False,
            })
            self.assertEqual(chat_status, 200)
            self.assertEqual(chat_data["choices"][0]["message"]["content"], "Final verified e2e answer")
        finally:
            t.join(timeout=5)

    def test_wsa_agent_client_and_command_roundtrip(self):
        from pathlib import Path
        from wsa_agent.client import WSAClient, GeminiClient
        from wsa_agent.commands import handle_command
        from wsa_agent.session import SessionState

        self.assertIs(GeminiClient, WSAClient)
        client = WSAClient(self.base_v1, api_key="test")
        try:
            self.assertTrue(client.check_connection())
            health = client.get_health()
            self.assertEqual(health["status"], "ok")

            state = SessionState(project_root=Path(ROOT_DIR))
            self.assertEqual(handle_command("/model claude", state), (True, True))
            self.assertEqual(state.model, "claude")
            self.assertEqual(handle_command("/model gemini", state), (True, True))
            self.assertEqual(state.model, "gemini")
            self.assertEqual(handle_command("/model gemini-3.6-flash", state), (True, True))
            self.assertEqual(state.model, "gemini")
            self.assertEqual(handle_command("/effort high", state), (False, True))
            self.assertEqual(handle_command("/think 2", state), (False, True))
        finally:
            client._client.close()

    def test_claude_relay_sse_streaming_thinking_and_chat_continuity(self):
        s_brave, r_brave = self._request_json("POST", "/sync-cookies", {
            "client_id": "relay-e2e-claude-brave",
            "browser": "brave",
            "label": "brave",
            "cookie": "",
            "email": "claude.brave@example.com",
            "active_site": "claude.ai",
            "model_badge": "Claude Sonnet 4.6 · Extended thinking",
            "sync_only": True,
            "providers": {
                "claude": {
                    "present": True,
                    "email": "claude.brave@example.com",
                    "model_badge": "Claude Sonnet 4.6 · Extended thinking",
                },
                "gemini": {"present": False, "email": "", "model_badge": ""},
            },
        })
        self.assertEqual(s_brave, 200)
        self.assertTrue(r_brave["ok"])

        s_chrome, r_chrome = self._request_json("POST", "/sync-cookies", {
            "client_id": "relay-e2e-gemini-chrome",
            "browser": "chrome",
            "label": "chrome",
            "cookie": "SAPISID=c_sapi; __Secure-1PSID=c_psid",
            "email": "gem.chrome@example.com",
            "active_site": "gemini.google.com",
            "model_badge": "Gemini 2.5 Pro",
            "providers": {
                "gemini": {
                    "present": True,
                    "email": "gem.chrome@example.com",
                    "model_badge": "Gemini 2.5 Pro",
                },
                "claude": {"present": False, "email": "", "model_badge": ""},
            },
        })
        self.assertEqual(s_chrome, 200)
        self.assertTrue(r_chrome["ok"])

        sel_status, sel_data = self._request_json("POST", "/v1/relays/select", {"relay": "brave"})
        self.assertEqual(sel_status, 200)
        self.assertTrue(sel_data["ok"])
        self.assertEqual(sel_data["active_relay_id"], "relay-e2e-claude-brave")

        observed_turns = []

        def worker():
            handled = 0
            for _ in range(240):
                st, pending = self._request_json(
                    "GET",
                    "/pending-prompt?client_id=relay-e2e-claude-brave&browser=brave&label=brave"
                    "&provider=claude&site=claude.ai&has_claude=1"
                    "&claude_email=claude.brave%40example.com&claude_model=Claude%20Sonnet%204.6",
                )
                if st == 200 and pending and pending.get("id"):
                    observed_turns.append(dict(pending))
                    if handled == 0:
                        self._request_json("POST", "/pending-progress", {
                            "id": pending["id"],
                            "client_id": "relay-e2e-claude-brave",
                            "thinking": "Thinking step 1",
                            "response": "Partial claude ",
                            "model": "Claude Sonnet 4.6 · Extended thinking",
                            "chat_url": "https://claude.ai/chat/c-101",
                            "seq": 1,
                        })
                        time.sleep(0.05)
                        self._request_json("POST", "/pending-response", {
                            "id": pending["id"],
                            "client_id": "relay-e2e-claude-brave",
                            "thinking": "Thinking step 1\nStep 2",
                            "response": "Final Claude answer",
                            "model": "Claude Sonnet 4.6 · Extended thinking",
                            "chat_url": "https://claude.ai/chat/c-101",
                        })
                        handled += 1
                    else:
                        self._request_json("POST", "/pending-response", {
                            "id": pending["id"],
                            "client_id": "relay-e2e-claude-brave",
                            "response": "Follow-up Claude answer",
                            "model": "Claude Sonnet 4.6 · Extended thinking",
                            "chat_url": "https://claude.ai/chat/c-101",
                        })
                        return
                time.sleep(0.02)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            body = json.dumps({
                "model": "claude",
                "session_id": "sess-claude-e2e",
                "messages": [{"role": "user", "content": "Explain WSA briefly"}],
                "stream": True,
            })
            conn.request("POST", "/v1/chat/completions", body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw_sse = resp.read().decode("utf-8")
            conn.close()

            self.assertEqual(resp.status, 200)
            think_parts = []
            content_parts = []
            saw_done = False
            saw_stop = False
            for line in raw_sse.splitlines():
                if not line.startswith("data: "):
                    continue
                payload_str = line[6:].strip()
                if payload_str == "[DONE]":
                    saw_done = True
                    continue
                ev = json.loads(payload_str)
                choice = (ev.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                if delta.get("thinking"):
                    think_parts.append(delta["thinking"])
                if delta.get("content"):
                    content_parts.append(delta["content"])
                if choice.get("finish_reason") == "stop":
                    saw_stop = True

            self.assertTrue(saw_done)
            self.assertTrue(saw_stop)
            self.assertIn("Thinking step 1", "".join(think_parts))
            self.assertIn("Final Claude answer", "".join(content_parts))

            h_status, health = self._request_json("GET", "/v1/health")
            self.assertEqual(h_status, 200)
            self.assertEqual(health.get("active_relay"), "brave")
            self.assertEqual(health.get("active_browser"), "brave")
            self.assertEqual((health.get("last_upstream_model") or {}).get("provider"), "claude")

            c2_status, c2_data = self._request_json("POST", "/v1/chat/completions", {
                "model": "claude",
                "session_id": "sess-claude-e2e",
                "messages": [{"role": "user", "content": "And the next step?"}],
                "stream": False,
            })
            self.assertEqual(c2_status, 200)
            self.assertEqual(c2_data["choices"][0]["message"]["content"], "Follow-up Claude answer")
            self.assertEqual(len(observed_turns), 2)
            self.assertEqual(observed_turns[0].get("provider"), "claude")
            self.assertEqual(observed_turns[1].get("provider"), "claude")
            self.assertEqual(observed_turns[1].get("chat_url"), "https://claude.ai/chat/c-101")
        finally:
            t.join(timeout=5)


ROOT_DIR = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))


if __name__ == "__main__":
    unittest.main()

