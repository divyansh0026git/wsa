"""Comprehensive Phase 3 regression & integration suite for WSA agentic coding reliability."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestWorkstream1ServerAndBridgeHardening(unittest.TestCase):
    """Verify server JSON repair, dropped tool diagnostics, DOM leak stripping, prompt budget, & relay routing."""

    def test_repair_tool_json_and_parse_exact_uncensoredchatui_regressions(self):
        from wsa_server.tools import inspect_unparsed_tool_attempts, parse_tool_calls

        # Exact Turn [11] pattern: single-quote escape inside run_python
        msg_11 = (
            "```tool_call\n"
            '{"name": "run_python", "arguments": {"code": "print(\\\'hello\\\\\\\\world\\\')"}}\n'
            "```\n"
        )
        rem_11, calls_11 = parse_tool_calls(msg_11, allowed_names={"run_python", "write_file"})
        self.assertEqual([c["function"]["name"] for c in calls_11], ["run_python"])
        self.assertEqual(inspect_unparsed_tool_attempts(rem_11), [])

        # Exact Turn [44] pattern: invalid LaTeX/regex backslash escapes (\v, \s, \d) inside write_file JSON
        msg_44_raw = (
            "```tool_call\n"
            '{"name": "write_file", "arguments": {"path": "backend/anti_slop.py", '
            '"content": "PAT = r\\"[\\vert{}]\\s+\\d+\\"\\n"}}\n'
            "```\n"
        )
        rem_44, calls_44 = parse_tool_calls(msg_44_raw, allowed_names={"write_file"})
        self.assertEqual([c["function"]["name"] for c in calls_44], ["write_file"])
        args_44 = json.loads(calls_44[0]["function"]["arguments"])
        self.assertEqual(args_44["path"], "backend/anti_slop.py")
        self.assertIn("\\vert{}", args_44["content"])
        self.assertEqual(inspect_unparsed_tool_attempts(rem_44), [])

    def test_agent_partial_dropped_tool_call_triggers_warning(self):
        from wsa_agent import tools
        from wsa_agent.agent import (
            _detect_dropped_tool_details,
            _extract_tool_calls_detailed,
            execute_agent_loop,
        )
        from wsa_agent.session import SessionState

        mixed_content = (
            "```tool_call\n"
            '{"name": "glob_files", "arguments": {"pattern": "*.py"}}\n'
            "```\n"
            "```tool_call\n"
            '{"name": "write_file", "arguments": {"path": "broken.py", "content": "x = \n'
        )
        calls, rem = _extract_tool_calls_detailed(
            {"content": mixed_content, "tool_calls": []},
            allowed_names={"glob_files", "write_file"},
        )
        self.assertEqual(len(calls), 1)
        dropped = _detect_dropped_tool_details(
            mixed_content, rem, calls, allowed_names={"glob_files", "write_file"}
        )
        self.assertTrue(dropped, "Expected diagnostic for truncated 2nd tool_call block")
        self.assertEqual(dropped[0]["name"], "write_file")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.py").write_text("x = 1\n", encoding="utf-8")
            state = SessionState(project_root=root, session_id="partial_drop_test")
            captured_prompts = []

            class MockClient:
                api_base = "http://127.0.0.1:8081/v1"
                api_key = ""

                def __init__(self):
                    self.turn = 0

                def stream(self, messages, model, **kwargs):
                    self.turn += 1
                    captured_prompts.append(list(messages))
                    if self.turn == 1:
                        return {
                            "content": mixed_content,
                            "tool_calls": [],
                            "finish_reason": "stop",
                            "usage": {},
                        }
                    return {
                        "content": "Completed everything cleanly.",
                        "tool_calls": [],
                        "finish_reason": "stop",
                        "usage": {},
                    }

            orig_root = tools.get_project_root()
            try:
                execute_agent_loop(
                    "List py files",
                    state,
                    MockClient(),
                    quiet_ui=True,
                )
            finally:
                tools.set_project_root(orig_root)

            self.assertGreaterEqual(len(captured_prompts), 2)
            tool_msgs = [m for m in captured_prompts[1] if m.get("role") == "tool"]
            self.assertTrue(tool_msgs)
            self.assertIn("Dropped Tool Block(s) Detected", tool_msgs[-1]["content"])

    def test_interrupt_mid_tool_batch_persists_complete_tool_pairs(self):
        import json
        from unittest.mock import patch
        from wsa_agent import agent as agent_mod
        from wsa_agent import tools
        from wsa_agent.session import SessionState

        batch = [
            {
                "id": "call_a",
                "type": "function",
                "function": {"name": "glob_files", "arguments": json.dumps({"pattern": "*.py"})},
            },
            {
                "id": "call_b",
                "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "ok.py"})},
            },
        ]

        class MockClient:
            api_base = "http://127.0.0.1:8081/v1"
            api_key = ""

            def stream(self, messages, model, **kwargs):
                return {"content": None, "tool_calls": list(batch), "finish_reason": "tool_calls", "usage": {}}

        outcomes = ["ok.py", KeyboardInterrupt()]

        def fake_execute_tool(name, args):
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.py").write_text("x = 1\n", encoding="utf-8")
            state = SessionState(project_root=root, session_id="interrupt_mid_batch_test")
            orig_root = tools.get_project_root()
            try:
                with patch.object(agent_mod, "execute_tool", side_effect=fake_execute_tool):
                    res = agent_mod.execute_agent_loop("List and read py files", state, MockClient(), quiet_ui=True)
            finally:
                tools.set_project_root(orig_root)

            self.assertEqual(res.get("aborted_reason"), "interrupted")
            asst_idx = [
                i for i, m in enumerate(state.messages)
                if m.get("role") == "assistant" and m.get("tool_calls")
            ]
            self.assertEqual(len(asst_idx), 1)
            followers = state.messages[asst_idx[0] + 1:asst_idx[0] + 3]
            self.assertEqual([m.get("role") for m in followers], ["tool", "tool"])
            self.assertEqual([m.get("tool_call_id") for m in followers], ["call_a", "call_b"])
            self.assertIn("[Interrupted]", followers[1]["content"])

    def test_compact_history_tolerates_blank_user_messages_and_system_prompt_dedupes_map(self):
        from unittest.mock import patch
        from wsa_agent import agent as agent_mod
        from wsa_agent import tools
        from wsa_agent.context import FileContextLedger
        from wsa_agent.session import SessionState

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = SessionState(project_root=root, session_id="compact_blank_user")
            state.messages = [
                {"role": "user", "content": ""},
                {"role": "user", "content": "   "},
                {"role": "assistant", "content": "ok"},
            ] + [
                {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
                for i in range(8)
            ]
            removed = state.compact_history(keep=4)
            self.assertGreater(removed, 0)
            self.assertIn("[Context Transfer Digest", state.context_summary)

            ledger = FileContextLedger(root)
            state.file_ledger = ledger
            state.context_summary = (
                "[Context Transfer Digest: 3 earlier messages (0 tool rounds) summarized]\n"
                "Modified Files: a.py\n"
                "Working-Set File Map:\nSTALE_MAP_MARKER"
            )
            orig_root = tools.get_project_root()
            try:
                tools.set_project_root(root)
                with patch.object(ledger, "render_working_set_digest", return_value="LIVE_MAP_MARKER"):
                    content = agent_mod._build_system_message(state)["content"]
            finally:
                tools.set_project_root(orig_root)
            self.assertIn("LIVE_MAP_MARKER", content)
            self.assertNotIn("STALE_MAP_MARKER", content)
            self.assertIn("Modified Files: a.py", content)

    def test_spawn_scoped_subagent_tolerates_blank_task(self):
        import json
        from unittest.mock import patch
        from wsa_agent import subagents as sub_mod
        from wsa_agent.session import SessionState

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = SessionState(project_root=root, session_id="spawn_blank_task_test")
            dummy_client = object()
            prev_ctx = sub_mod.set_subagent_runtime_context(state, dummy_client)
            try:
                with patch.object(
                    sub_mod,
                    "_run_role_agent",
                    return_value={"ok": True, "written_files": [], "verified_after_last_edit": True, "final_text": "done"},
                ) as mock_role:
                    result = sub_mod.spawn_scoped_subagent(role="explorer", task="")
                self.assertEqual(mock_role.call_count, 1)
                parsed = json.loads(result)
                self.assertTrue(parsed["ok"])
                self.assertEqual(parsed["role"], "explorer")
            finally:
                sub_mod.set_subagent_runtime_context(*prev_ctx)

    def test_server_manager_closes_log_handle_on_timeout_and_popen_failure(self):
        from unittest.mock import MagicMock, patch
        from wsa_agent import __main__ as main_mod

        # Case 1: Popen succeeds but the server never comes up before the startup timeout —
        # _shutdown() (invoked here directly, as atexit would) must close the log handle.
        mgr = main_mod._ServerManager("http://127.0.0.1:8081/v1")
        mgr._STARTUP_TIMEOUT = 0.05
        mgr._CHECK_INTERVAL = 0.01
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        with tempfile.TemporaryDirectory() as tmp_home:
            with patch.object(main_mod, "Path") as mock_path_cls:
                mock_path_cls.home.return_value = Path(tmp_home)
                mock_path_cls.side_effect = lambda *a, **k: Path(*a, **k)
                mock_path_cls.return_value = Path(__file__).resolve().parent.parent
                with patch.object(main_mod.subprocess, "Popen", return_value=fake_proc), \
                     patch.object(mgr, "_is_server_up", return_value=False):
                    result = mgr.ensure_running()
            self.assertFalse(result)
            self.assertIsNotNone(mgr._log_fh)
            mgr._shutdown()
            self.assertIsNone(mgr._log_fh)

        # Case 2: Popen itself raises — the handle opened just before it must be closed inline.
        mgr2 = main_mod._ServerManager("http://127.0.0.1:8081/v1")
        with tempfile.TemporaryDirectory() as tmp_home2:
            with patch.object(main_mod, "Path") as mock_path_cls2:
                mock_path_cls2.home.return_value = Path(tmp_home2)
                mock_path_cls2.side_effect = lambda *a, **k: Path(*a, **k)
                mock_path_cls2.return_value = Path(__file__).resolve().parent.parent
                with patch.object(main_mod.subprocess, "Popen", side_effect=OSError("boom")), \
                     patch.object(mgr2, "_is_server_up", return_value=False):
                    result2 = mgr2.ensure_running()
            self.assertFalse(result2)
            self.assertIsNone(mgr2._log_fh)

    def test_cmd_run_flags_nonzero_exit_as_error_without_error_prefix(self):
        from unittest.mock import patch, MagicMock
        from wsa_agent import commands as cmd_mod
        from wsa_agent.session import SessionState

        state = SessionState()
        fail_result = "STDOUT:\npytest output here\n1 failed, 2 passed\nExit code: 1"
        pass_result = "STDOUT:\nall good\nExit code: 0"

        captured = []

        def fake_print_tool_result(result, name, is_error=False, elapsed=None):
            captured.append(is_error)

        with patch.object(cmd_mod, "execute_tool", return_value=fail_result), \
             patch.object(cmd_mod.R, "print_tool_result", side_effect=fake_print_tool_result), \
             patch.object(cmd_mod.R, "print_info"):
            cmd_mod._cmd_run("pytest -x -q", state)
        self.assertTrue(captured[-1], "Non-zero exit code without [Error] prefix must still be flagged as an error")

        with patch.object(cmd_mod, "execute_tool", return_value=pass_result), \
             patch.object(cmd_mod.R, "print_tool_result", side_effect=fake_print_tool_result), \
             patch.object(cmd_mod.R, "print_info"):
            cmd_mod._cmd_run("pytest -x -q", state)
        self.assertFalse(captured[-1], "Exit code 0 must not be flagged as an error")

        with patch.object(cmd_mod, "execute_tool", return_value="[Error] Not found: foo.py"), \
             patch.object(cmd_mod.R, "print_tool_result", side_effect=fake_print_tool_result), \
             patch.object(cmd_mod.R, "print_info"):
            cmd_mod._cmd_run("cat foo.py", state)
        self.assertTrue(captured[-1], "[Error]-prefixed result (no exit code line) must still be flagged as an error")

    def test_strip_internal_tags_handles_consecutive_and_mixed_tool_fences(self):
        from wsa_agent import renderer

        fence = "`" * 3
        call_a = '{"name": "read_file", "arguments": {"path": "a.py"}}'
        call_b = '{"name": "read_file", "arguments": {"path": "b.py"}}'

        # Back-to-back fenced tool calls (batched shape) must leave no orphan fence markers.
        for lang in ("json", "tool_call"):
            text = f"Intro\n{fence}{lang}\n{call_a}\n{fence}\n{fence}{lang}\n{call_b}\n{fence}\nDone"
            self.assertEqual(renderer._strip_internal_tags(text), "Intro\nDone")
            self.assertEqual(renderer._strip_internal_tags(text, streaming=True), "Intro\nDone")

        # A tool-call fence right after an ordinary code fence must not be mis-paired with it.
        mixed = f"A\n{fence}python\nx = 1\n{fence}\n{fence}json\n{call_a}\n{fence}\nB"
        self.assertEqual(
            renderer._strip_internal_tags(mixed),
            f"A\n{fence}python\nx = 1\n{fence}\nB",
        )

        # Ordinary JSON fences are preserved.
        plain = f'A\n{fence}json\n{{"k": 1}}\n{fence}\nB'
        self.assertEqual(renderer._strip_internal_tags(plain), plain)

    def test_strip_internal_tags_preserves_accidental_fence_wrapping_markdown(self):
        from wsa_agent import renderer
        from wsa_agent.agent import _sanitize_assistant_tool_prose

        raw = (
            "```\n"
            '{"name": "glob_files", "I hit the tool-call limit for this turn while completing the inspection, so no new patches were written yet.\n\n'
            "### 1. Build & Port State (Observed)\n"
            "* **Port 5174 is down:** 'GET http://127.0.0.1:5174/' returned WinError 10061\n\n"
            "### 2. Art Director Critique\n"
            "* Solar Luminosity: Photosphere is crisp.\n\n"
            "Reply with 'continue'\n"
            "```"
        )
        cleaned_renderer = renderer._strip_internal_tags(raw, streaming=False)
        self.assertNotIn('{"name": "glob_files"', cleaned_renderer)
        self.assertIn("### 1. Build & Port State (Observed)", cleaned_renderer)
        self.assertIn("### 2. Art Director Critique", cleaned_renderer)
        self.assertIn("Reply with 'continue'", cleaned_renderer)
        self.assertFalse(cleaned_renderer.startswith("```"))

        cleaned_agent = _sanitize_assistant_tool_prose(raw, has_tool_calls=False)
        self.assertNotIn('{"name": "glob_files"', cleaned_agent)
        self.assertIn("### 1. Build & Port State (Observed)", cleaned_agent)
        self.assertIn("### 2. Art Director Critique", cleaned_agent)

    def test_sanitize_assistant_prose_strips_dom_bleed_and_premature_report(self):
        from wsa_agent.agent import _sanitize_assistant_tool_prose
        from wsa_server.multimodal import _sanitize_assistant_relay_prose

        bloated = ("Premature final report line.\n" * 80) + "\n```tool_call\n{\"name\": \"read_file\"}\n```_]  "
        clean_agent = _sanitize_assistant_tool_prose(bloated, has_tool_calls=True)
        self.assertNotIn("_]", clean_agent)
        self.assertIn("Premature assistant prose truncated", clean_agent)
        self.assertLess(len(clean_agent), 650)

        leaked_end = "Done reading.\n```tool_call\n{}\n```_]\u200b "
        clean_end = _sanitize_assistant_tool_prose(leaked_end, has_tool_calls=False)
        self.assertFalse(clean_end.endswith("_]"))
        self.assertNotIn("\u200b", clean_end)

        relay_san = _sanitize_assistant_relay_prose("Hello ```\n_] _*Wait")
        self.assertNotIn("_*Wait", relay_san)
        self.assertNotIn("_]", relay_san)

    def test_multimodal_fit_prompt_priority_budgeting(self):
        from wsa_agent.tools import TOOL_DEFINITIONS
        from wsa_server.multimodal import PROMPT_MAX_BYTES, messages_to_prompt

        huge_old = "OLD_LOG " * 4000
        msgs = [
            {"role": "system", "content": "CRITICAL_SYSTEM_DIRECTIVE"},
            {"role": "user", "content": "Old question 1"},
            {"role": "assistant", "content": huge_old},
            {"role": "user", "content": "Old question 2"},
            {"role": "assistant", "content": huge_old},
            {"role": "user", "content": "Old question 3"},
            {"role": "assistant", "content": huge_old},
            {"role": "user", "content": "CURRENT_USER_GOAL_MUST_SURVIVE"},
        ]
        prompt, _ = messages_to_prompt(msgs, tools=TOOL_DEFINITIONS)
        self.assertLessEqual(len(prompt.encode("utf-8")), PROMPT_MAX_BYTES + 2048)
        self.assertIn("CRITICAL_SYSTEM_DIRECTIVE", prompt)
        self.assertIn("CURRENT_USER_GOAL_MUST_SURVIVE", prompt)

    def test_relay_explicit_target_routing_preserves_default_single_account(self):
        from wsa_server import config as _cfg
        from wsa_server import relay

        with _cfg._CONFIG_LOCK:
            snap_cfg = dict(_cfg.CONFIG)
        with relay._dom_relay_lock:
            snap_state = {
                "relays": dict(relay._dom_relay.get("relays") or {}),
                "active_relay_id": relay._dom_relay.get("active_relay_id"),
                "pinned_relay_label": relay._dom_relay.get("pinned_relay_label"),
                "explicit_relay_lock": relay._dom_relay.get("explicit_relay_lock", False),
                "pending": dict(relay._dom_relay.get("pending") or {}),
                "sessions": dict(relay._dom_relay.get("sessions") or {}),
            }
        try:
            with relay._dom_relay_lock:
                relay._dom_relay["relays"] = {
                    "chrome_1": {"client_id": "chrome_1", "browser": "chrome", "label": "Chrome", "last_seen": 9e9},
                    "brave_2": {"client_id": "brave_2", "browser": "brave", "label": "Brave", "last_seen": 9e9},
                }
                relay._dom_relay["active_relay_id"] = "chrome_1"
                relay._dom_relay["pinned_relay_label"] = "Chrome"
                relay._dom_relay["explicit_relay_lock"] = True
                relay._dom_relay["pending"].clear()
                relay._dom_relay["pending"]["req_def"] = {
                    "prompt": "hi",
                    "fallback_prompt": "hi",
                    "images": [],
                    "session_id": "s1",
                    "provider": "gemini",
                    "target_relay_id": "chrome_1",
                    "explicit_relay_target": False,
                    "response": None,
                    "error": None,
                    "dispatched": False,
                    "new_chat": True,
                }

            # Default prompt goes ONLY to active relay chrome_1
            self.assertIsNone(relay.dom_relay_get_pending(client_id="brave_2"))
            got_default = relay.dom_relay_get_pending(client_id="chrome_1")
            self.assertIsNotNone(got_default)
            self.assertEqual(got_default["id"], "req_def")

            # Explicit target_relay_id="brave_2" routes ONLY to brave_2
            with relay._dom_relay_lock:
                relay._dom_relay["pending"].clear()
                relay._dom_relay["pending"]["req_b2"] = {
                    "prompt": "hi b2",
                    "fallback_prompt": "hi b2",
                    "images": [],
                    "session_id": "s2",
                    "provider": "gemini",
                    "target_relay_id": "brave_2",
                    "explicit_relay_target": True,
                    "response": None,
                    "error": None,
                    "dispatched": False,
                    "new_chat": True,
                }
            self.assertIsNone(relay.dom_relay_get_pending(client_id="chrome_1"))
            got_explicit = relay.dom_relay_get_pending(client_id="brave_2")
            self.assertIsNotNone(got_explicit)
            self.assertEqual(got_explicit["id"], "req_b2")
        finally:
            with relay._dom_relay_lock:
                for k, v in snap_state.items():
                    relay._dom_relay[k] = v
            with _cfg._CONFIG_LOCK:
                _cfg.CONFIG.clear()
                _cfg.CONFIG.update(snap_cfg)


class TestWorkstream2ContextOutlinerAndLedger(unittest.TestCase):
    """Verify AST/structural outlines, read-before-edit, disk staleness guard, and SessionState persistence."""

    def test_get_file_outline_extracts_python_js_html_and_tracks_ledger(self):
        from wsa_agent import tools
        from wsa_agent.context import FileContextLedger

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mod.py").write_text(
                "import os\n\nclass Engine:\n    def run(self, x: int) -> int:\n        return x + 1\n\ndef helper():\n    return 42\n",
                encoding="utf-8",
            )
            (root / "ui.js").write_text(
                "export async function loadData(id) {\n  return id;\n}\nconst renderItem = () => {};\n",
                encoding="utf-8",
            )
            (root / "index.html").write_text(
                "<!doctype html><html><body><div id='app-shell'><button id='send-btn'>Send</button></div></body></html>\n",
                encoding="utf-8",
            )

            ledger = FileContextLedger(root)
            orig_root = tools.get_project_root()
            prev_ledger = tools.set_active_file_ledger(ledger)
            try:
                tools.set_project_root(root)
                out_py = tools.execute_tool("get_file_outline", {"path": "mod.py"})
                out_js = tools.execute_tool("get_file_outline", {"path": "ui.js"})
                out_html = tools.execute_tool("get_file_outline", {"path": "index.html"})
            finally:
                tools.set_active_file_ledger(prev_ledger)
                tools.set_project_root(orig_root)

            self.assertIn("class Engine", out_py)
            self.assertIn("def helper", out_py)
            self.assertIn("loadData", out_js)
            self.assertIn("send-btn", out_html)
            self.assertIn("mod.py", ledger.entries)
            self.assertGreaterEqual(len(ledger.entries["mod.py"]["symbols"]), 2)

    def test_stale_disk_block_strict_read_before_edit_and_surgical_patch_hunks(self):
        from wsa_agent import tools
        from wsa_agent.context import FileContextLedger

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            big_lines = [f"CONFIG_VAL_{i:02d} = 'padding_value_for_size_threshold_{i:02d}'" for i in range(35)]
            big_lines.append("def calc(x):")
            big_lines.append("    return x * 2")
            big_lines.append("def tail():")
            big_lines.append("    return 'old'")
            p = root / "service.py"
            p.write_text("\n".join(big_lines) + "\n", encoding="utf-8")

            ledger = FileContextLedger(root)
            orig_root = tools.get_project_root()
            prev_ledger = tools.set_active_file_ledger(ledger)
            prev_strict = tools.set_strict_read_before_edit(True)
            try:
                tools.set_project_root(root)

                # Unread multi-line file must be read before patch/write when strict guard is active
                blocked = tools.execute_tool(
                    "patch_file",
                    {"path": "service.py", "old_string": "return x * 2", "new_string": "return x * 3"},
                )
                self.assertIn("Read-before-edit", blocked)

                # Read lines 30..45 -> now patch_file succeeds
                tools.execute_tool("read_file", {"path": "service.py", "start_line": 30, "end_line": 45})
                # Blind write_file overwrite of large code file must still require surgical patch_file
                overwrite_blocked = tools.execute_tool(
                    "write_file",
                    {"path": "service.py", "content": "\n".join(big_lines) + "\n"},
                )
                self.assertIn("Surgical-edit policy", overwrite_blocked)

                # Multi-hunk patch_file with bounded start_line/end_line applies cleanly
                patched = tools.execute_tool(
                    "patch_file",
                    {
                        "path": "service.py",
                        "hunks": [
                            {"old_string": "return x * 2", "new_string": "return x * 10", "start_line": 35, "end_line": 39},
                            {"old_string": "return 'old'", "new_string": "return 'new'", "start_line": 37, "end_line": 42},
                        ],
                    },
                )
                self.assertIn("Patched service.py — 2 hunks applied", patched)
                self.assertIn("return x * 10", p.read_text(encoding="utf-8"))
                self.assertIn("return 'new'", p.read_text(encoding="utf-8"))

                # External mutation on disk must trigger stale sha rejection until re-read
                p.write_text(p.read_text(encoding="utf-8") + "\nEXTERNAL = True\n", encoding="utf-8")
                stale = tools.execute_tool(
                    "patch_file",
                    {"path": "service.py", "old_string": "return x * 10", "new_string": "return x * 11"},
                )
                self.assertIn("Stale file context", stale)
            finally:
                tools.set_strict_read_before_edit(prev_strict)
                tools.set_active_file_ledger(prev_ledger)
                tools.set_project_root(orig_root)

    def test_decisions_context_packet_and_session_roundtrip(self):
        from wsa_agent.context import FileContextLedger, build_context_packet, capture_user_decisions
        from wsa_agent.session import SessionState

        decisions = capture_user_decisions("Build using SQLite only, never PostgreSQL, on port 8099.")
        self.assertTrue(decisions)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "sessions"
            state = SessionState(project_root=root, session_id="sess_p3_test", decisions=decisions, team_mode="on", dual_lane=True)
            self.assertEqual(state.thread_mode, "single")
            self.assertEqual(state.resolve_stage_session_id("coder"), "sess_p3_test")
            state.thread_mode = "roles"
            self.assertEqual(state.resolve_stage_session_id("coder"), "sess_p3_test:role:coder")
            state.plan_items = [{"id": "t1", "title": "Create DB", "owned_files": ["db.py"], "status": "done"}]
            state.role_threads = {"explorer": "https://gemini.google.com/app/exp1"}
            state.file_ledger.record_write("db.py", "def init_db():\n    pass\n", summary="created")
            state.file_ledger.mark_verified(["db.py"])

            self.assertEqual(state.role_session_id("coder"), "sess_p3_test:role:coder")

            pkt = build_context_packet(
                project_root=root,
                project_type="Python",
                ledger=state.file_ledger,
                decisions=state.decisions,
                plan_items=state.plan_items,
                written_files=["db.py"],
                verified_files=["db.py"],
                focus_files=["db.py"],
            )
            self.assertIn("db.py", pkt)
            self.assertIn("SQLite", pkt)

            with mock.patch("wsa_agent.session.Path.home", return_value=root):
                saved = state.save()
                loaded = SessionState(project_root=root)
                loaded.load(Path(saved))
            self.assertTrue(Path(saved).exists())
            self.assertEqual(loaded.team_mode, "on")
            self.assertEqual(loaded.thread_mode, "roles")
            self.assertTrue(loaded.dual_lane)
            self.assertEqual(loaded.role_threads.get("explorer"), "https://gemini.google.com/app/exp1")
            self.assertIn("db.py", loaded.file_ledger.entries)
            self.assertTrue(loaded.file_ledger.entries["db.py"]["verified"])


class TestWorkstream3LoopPrecisionHardening(unittest.TestCase):
    """Verify intra-round deduplication, semantic verification gate, and smart output condensing."""

    def test_intra_batch_duplicate_tool_deduplication(self):
        from wsa_agent import tools
        from wsa_agent.agent import execute_agent_loop
        from wsa_agent.session import SessionState

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alpha.py").write_text("X = 1\n", encoding="utf-8")
            state = SessionState(project_root=root, session_id="dedup_test")

            dup_response = (
                "```tool_call\n"
                '{"name": "read_file", "arguments": {"path": "alpha.py"}}\n'
                "```\n"
                "```tool_call\n"
                '{"name": "read_file", "arguments": {"path": "alpha.py"}}\n'
                "```\n"
            )

            class MockClient:
                api_base = "http://127.0.0.1:8081/v1"
                api_key = ""

                def __init__(self):
                    self.calls = 0

                def stream(self, messages, model, **kwargs):
                    self.calls += 1
                    if self.calls == 1:
                        return {"content": dup_response, "tool_calls": [], "finish_reason": "stop", "usage": {}}
                    return {"content": "Read complete.", "tool_calls": [], "finish_reason": "stop", "usage": {}}

            orig_root = tools.get_project_root()
            try:
                with mock.patch("wsa_agent.agent.execute_tool", wraps=tools.execute_tool) as spy_exec:
                    res = execute_agent_loop("Inspect alpha.py", state, MockClient(), quiet_ui=True)
            finally:
                tools.set_project_root(orig_root)

            read_calls = [c for c in spy_exec.call_args_list if c.args[0] == "read_file"]
            self.assertEqual(len(read_calls), 1)
            self.assertEqual(res["tool_calls_count"], 1)

    def test_semantic_verification_command_gate_rejects_process_probes(self):
        from wsa_agent import tools
        from wsa_agent.agent import _condense_single_tool_output, _is_verification_command, execute_agent_loop
        from wsa_agent.session import SessionState

        # Negative cases: OS queries / status probes MUST NOT clear verification gate
        self.assertFalse(_is_verification_command("run_command", {"command": "Get-CimInstance Win32_Process"}, "Exit code: 0"))
        self.assertFalse(_is_verification_command("run_command", {"command": "git status"}, "Exit code: 0"))
        self.assertFalse(_is_verification_command("run_command", {"command": "Start-Sleep -Seconds 2"}, "Exit code: 0"))
        self.assertFalse(_is_verification_command("run_command", {"command": "pytest -q"}, "Exit code: 5\nno tests ran"))

        # Positive cases: real test / syntax / runtime checks MUST clear verification gate
        self.assertTrue(_is_verification_command("run_command", {"command": "python -m pytest -x -q"}, "Exit code: 0\n3 passed"))
        self.assertTrue(_is_verification_command("run_command", {"command": "node --check ui.js"}, "Exit code: 0"))
        self.assertTrue(_is_verification_command("run_python", {"code": "import app; print(app.VAL)"}, "123"))
        self.assertFalse(_is_verification_command("run_python", {"code": "print('1 passed')"}, "1 passed"))

        # End-to-end check inside execute_agent_loop: Get-CimInstance after write triggers verification gate
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = SessionState(project_root=root, session_id="verify_gate_test", permission_mode="auto")
            prompts_seen = []

            class MockClient:
                api_base = "http://127.0.0.1:8081/v1"
                api_key = ""

                def __init__(self):
                    self.turn = 0

                def stream(self, messages, model, **kwargs):
                    self.turn += 1
                    prompts_seen.append(list(messages))
                    if self.turn == 1:
                        return {
                            "content": '```tool_call\n{"name": "write_file", "arguments": {"path": "calc.py", "content": "VAL = 7\\n"}}\n```',
                            "tool_calls": [],
                            "finish_reason": "stop",
                            "usage": {},
                        }
                    if self.turn == 2:
                        return {
                            "content": '```tool_call\n{"name": "run_command", "arguments": {"command": "git status"}}\n```',
                            "tool_calls": [],
                            "finish_reason": "stop",
                            "usage": {},
                        }
                    if self.turn == 3:
                        return {"content": "Finished.", "tool_calls": [], "finish_reason": "stop", "usage": {}}
                    if self.turn == 4:
                        return {
                            "content": '```tool_call\n{"name": "run_python", "arguments": {"code": "import calc; assert calc.VAL == 7; print(\'OK\')"}}\n```',
                            "tool_calls": [],
                            "finish_reason": "stop",
                            "usage": {},
                        }
                    return {"content": "All fixes are complete and verified.", "tool_calls": [], "finish_reason": "stop", "usage": {}}

            orig_root = tools.get_project_root()
            try:
                res = execute_agent_loop("Create calc.py and verify it", state, MockClient(), quiet_ui=True)
            finally:
                tools.set_project_root(orig_root)

            # Turn 4 received verification gate because git status didn't clear verified_after_last_edit
            self.assertGreaterEqual(len(prompts_seen), 5)
            self.assertTrue(
                any("System Verification Gate" in str(m.get("content") or "") for m in prompts_seen[3])
            )
            self.assertTrue(res["verified_after_last_edit"])
            self.assertTrue(state.file_ledger.entries["calc.py"]["verified"])

        # Smart output condenser keeps Exit code + Traceback when length > 1200 chars
        long_out = ("ok line\n" * 220) + "\nTraceback (most recent call last):\nAssertionError: boom\nExit code: 1"
        condensed = _condense_single_tool_output("run_command", long_out)
        self.assertLess(len(condensed), len(long_out))
        self.assertIn("Exit code: 1", condensed)
        self.assertIn("AssertionError: boom", condensed)


class TestWorkstream4ScopedSubagentsAndCommands(unittest.TestCase):
    """Verify adaptive complexity, role isolation, verifier repair loop, dual-lane scheduler, and commands."""

    def test_complexity_classifier_direct_vs_orchestrate(self):
        from wsa_agent.context import classify_task_complexity

        direct_prompts = [
            "hello",
            "continue",
            "Read note and continue",
            "Keep trying missing file",
            "Fix bug shown in @bug_shot.png",
            "Fix X in mod.py",
        ]
        for p in direct_prompts:
            self.assertEqual(classify_task_complexity(p)["mode"], "direct", f"Expected direct for {p!r}")

        complex_prompt = (
            "Build a full-stack web application from scratch with FastAPI backend, SQLite database, "
            "REST API endpoints, responsive HTML/CSS/JS frontend dashboard, and comprehensive pytest suite."
        )
        cls = classify_task_complexity(complex_prompt)
        self.assertEqual(cls["mode"], "orchestrate")
        self.assertGreaterEqual(cls["score"], 2)

    def test_scoped_role_tool_allowlists_and_runtime_enforcement(self):
        from wsa_agent import tools
        from wsa_agent.agent import execute_agent_loop
        from wsa_agent.session import SessionState
        from wsa_agent.subagents import get_role_tools

        exp_names = {t["function"]["name"] for t in get_role_tools("explorer")}
        ver_names = {t["function"]["name"] for t in get_role_tools("verifier")}
        cod_names = {t["function"]["name"] for t in get_role_tools("coder")}

        self.assertNotIn("write_file", exp_names)
        self.assertNotIn("patch_file", exp_names)
        self.assertNotIn("run_command", exp_names)
        self.assertNotIn("write_file", ver_names)
        self.assertNotIn("patch_file", ver_names)
        self.assertIn("run_command", ver_names)
        self.assertIn("browser_inspect", ver_names)
        self.assertIn("patch_file", cod_names)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = SessionState(project_root=root, session_id="scope_guard")
            captured = []

            class MockClient:
                api_base = "http://127.0.0.1:8081/v1"
                api_key = ""

                def __init__(self):
                    self.turn = 0

                def stream(self, messages, model, **kwargs):
                    self.turn += 1
                    captured.append(list(messages))
                    if self.turn == 1:
                        return {
                            "content": '```tool_call\n{"name": "write_file", "arguments": {"path": "hack.py", "content": "x=1"}}\n```',
                            "tool_calls": [],
                            "finish_reason": "stop",
                            "usage": {},
                        }
                    return {"content": "Recon finished.", "tool_calls": [], "finish_reason": "stop", "usage": {}}

            orig_root = tools.get_project_root()
            try:
                execute_agent_loop(
                    "Explore workspace",
                    state,
                    MockClient(),
                    role="explorer",
                    tools_override=get_role_tools("explorer"),
                    persist_to_state=False,
                    quiet_ui=True,
                )
            finally:
                tools.set_project_root(orig_root)

            self.assertFalse((root / "hack.py").exists())
            tool_res = [m for m in captured[1] if m.get("role") == "tool"]
            self.assertIn("[Scope Error]", tool_res[0]["content"])

    def test_orchestrated_pipeline_runs_explorer_architect_coder_verifier_and_repair_loop(self):
        from wsa_agent import tools
        from wsa_agent.session import SessionState
        from wsa_agent.subagents import run_orchestrated_turn

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = SessionState(project_root=root, session_id="orch_e2e", thread_mode="roles", permission_mode="auto")
            seen_sessions = []

            class StatefulPipelineClient:
                api_base = "http://127.0.0.1:8081/v1"
                api_key = ""

                def __init__(self):
                    self.role_steps = {}
                    self.ver_round = 0

                def get_relays(self):
                    return {"relays": [{"client_id": "chrome_1", "browser": "chrome", "online": True, "active": True}]}

                def stream(self, messages, model, session_id=None, **kwargs):
                    seen_sessions.append(session_id)
                    role = str(session_id or "").rsplit(":role:", 1)[-1]
                    idx = self.role_steps.get(role, 0) + 1
                    self.role_steps[role] = idx

                    if role == "explorer":
                        if idx == 1:
                            return {"content": '```tool_call\n{"name": "list_dir", "arguments": {"path": "."}}\n```', "tool_calls": [], "usage": {}}
                        return {"content": "Workspace is empty; ready for math_mod.py + test_math_mod.py.", "tool_calls": [], "usage": {}}

                    if role == "architect":
                        plan_json = json.dumps([
                            {"id": "t1", "title": "Implement add module", "owned_files": ["math_mod.py"], "depends_on": [], "acceptance": "add(2,3)==5"},
                            {"id": "t2", "title": "Add unit tests", "owned_files": ["test_math_mod.py"], "depends_on": ["t1"], "acceptance": "pytest passes"},
                        ])
                        return {"content": f"Plan:\n```json\n{plan_json}\n```", "tool_calls": [], "usage": {}}

                    if role == "coder":
                        if idx == 1:
                            # First task intentionally writes a bug (a - b)
                            return {"content": '```tool_call\n{"name": "write_file", "arguments": {"path": "math_mod.py", "content": "def add(a, b):\\n    return a - b\\n"}}\n```', "tool_calls": [], "usage": {}}
                        if idx == 2:
                            return {"content": '```tool_call\n{"name": "run_python", "arguments": {"code": "import math_mod; print(math_mod.add(1,0))"}}\n```', "tool_calls": [], "usage": {}}
                        if idx == 3:
                            return {"content": "Task t1 wrote math_mod.py.", "tool_calls": [], "usage": {}}
                        if idx == 4:
                            return {"content": '```tool_call\n{"name": "write_file", "arguments": {"path": "test_math_mod.py", "content": "from math_mod import add\\ndef test_add():\\n    assert add(2, 3) == 5\\n"}}\n```', "tool_calls": [], "usage": {}}
                        if idx == 5:
                            return {"content": '```tool_call\n{"name": "run_python", "arguments": {"code": "import test_math_mod; print(\'written\')"}}\n```', "tool_calls": [], "usage": {}}
                        if idx == 6:
                            return {"content": "Task t2 wrote test_math_mod.py.", "tool_calls": [], "usage": {}}
                        if idx == 7:
                            # Repair pass triggered by verifier failure!
                            return {"content": '```tool_call\n{"name": "patch_file", "arguments": {"path": "math_mod.py", "old_string": "return a - b", "new_string": "return a + b"}}\n```', "tool_calls": [], "usage": {}}
                        if idx == 8:
                            return {"content": '```tool_call\n{"name": "run_command", "arguments": {"command": "python -m pytest -x -q"}}\n```', "tool_calls": [], "usage": {}}
                        return {"content": "Repaired add() to a + b and pytest passed.", "tool_calls": [], "usage": {}}

                    if role == "verifier":
                        # Actually check disk state directly via run_command once per verifier pass
                        has_tool_res = any(m.get("role") == "tool" for m in messages[-3:])
                        if not has_tool_res:
                            return {"content": '```tool_call\n{"name": "run_command", "arguments": {"command": "python -m pytest -x -q"}}\n```', "tool_calls": [], "usage": {}}
                        last_tool = next(m["content"] for m in reversed(messages) if m.get("role") == "tool")
                        if "Exit code: 0" in last_tool and "passed" in last_tool:
                            return {"content": '```json\n{"verdict": "pass", "checks": ["pytest: 1 passed"], "failures": [], "summary": "All tests pass."}\n```', "tool_calls": [], "usage": {}}
                        return {"content": '```json\n{"verdict": "fail", "checks": ["pytest failed"], "failures": [{"file": "math_mod.py", "issue": "add(2,3) returned -1 instead of 5"}], "summary": "Fix add()."}\n```', "tool_calls": [], "usage": {}}

                    return {"content": "Done.", "tool_calls": [], "usage": {}}

            orig_root = tools.get_project_root()
            try:
                res = run_orchestrated_turn(
                    "Build math_mod.py with add(a, b) and full pytest test_math_mod.py suite.",
                    state,
                    StatefulPipelineClient(),
                )
            finally:
                tools.set_project_root(orig_root)

            self.assertTrue(res["ok"])
            self.assertEqual(res["verdict"]["verdict"], "pass")
            self.assertEqual((root / "math_mod.py").read_text(encoding="utf-8"), "def add(a, b):\n    return a + b\n")
            self.assertTrue(state.file_ledger.entries["math_mod.py"]["verified"])
            self.assertIn("orch_e2e:role:explorer", seen_sessions)
            self.assertIn("orch_e2e:role:architect", seen_sessions)
            self.assertIn("orch_e2e:role:coder", seen_sessions)
            self.assertIn("orch_e2e:role:verifier", seen_sessions)
            self.assertEqual(len(state.plan_items), 2)
            self.assertTrue(all(it["status"] == "done" for it in state.plan_items))

    def test_dual_lane_serializes_until_file_ownership_is_enforced(self):
        from wsa_agent import tools
        from wsa_agent.session import SessionState
        from wsa_agent.subagents import run_orchestrated_turn

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = SessionState(project_root=root, session_id="dual_lane_e2e", dual_lane=True, permission_mode="auto")
            coder_relays_lock = threading.Lock()
            coder_relays = []

            class DualRelayClient:
                api_base = "http://127.0.0.1:8081/v1"
                api_key = ""

                def get_relays(self):
                    return {
                        "relays": [
                            {"client_id": "chrome_1", "browser": "chrome", "online": True, "active": True},
                            {"client_id": "brave_2", "browser": "brave", "online": True, "active": False},
                        ]
                    }

                def stream(self, messages, model, session_id=None, relay_id=None, **kwargs):
                    sid = str(session_id or "")
                    sys_head = str(messages[0].get("content") or "") if messages else ""
                    if ":role:explorer" in sid or "WSA Explorer" in sys_head:
                        return {"content": "Ready.", "tool_calls": [], "usage": {}}
                    if ":role:architect" in sid or "WSA Architect" in sys_head:
                        dag = json.dumps([
                            {"id": "t1", "title": "Build a.py", "owned_files": ["a.py"], "depends_on": [], "acceptance": "ok"},
                            {"id": "t2", "title": "Build b.py", "owned_files": ["b.py"], "depends_on": [], "acceptance": "ok"},
                        ])
                        return {"content": f"```json\n{dag}\n```", "tool_calls": [], "usage": {}}
                    if ":role:coder" in sid or "WSA Coder" in sys_head:
                        with coder_relays_lock:
                            coder_relays.append(relay_id)
                        if any(m.get("role") == "tool" and m.get("name") == "write_file" for m in messages) and not any(
                            m.get("role") == "tool" and m.get("name") == "run_python" for m in messages
                        ):
                            return {"content": None, "tool_calls": [{"id": "check", "function": {
                                "name": "run_python", "arguments": json.dumps({"code": "import pathlib; assert any(pathlib.Path('.').glob('*.py'))"})}}]}
                        user_msg = str(messages[-1].get("content") or "")
                        if ":lane1" in sid or "Execute task `t2`" in user_msg:
                            if not any(m.get("role") == "tool" for m in messages):
                                return {"content": '```tool_call\n{"name": "write_file", "arguments": {"path": "b.py", "content": "B = 2\\n"}}\n```', "tool_calls": [], "usage": {}}
                            return {"content": "Done b.py.", "tool_calls": [], "usage": {}}
                        if ":lane0" in sid or "Execute task `t1`" in user_msg:
                            if not any(m.get("role") == "tool" for m in messages):
                                return {"content": '```tool_call\n{"name": "write_file", "arguments": {"path": "a.py", "content": "A = 1\\n"}}\n```', "tool_calls": [], "usage": {}}
                            return {"content": "Done a.py.", "tool_calls": [], "usage": {}}
                        if not any(m.get("role") == "tool" for m in messages):
                            return {"content": '```tool_call\n{"name": "write_file", "arguments": {"path": "b.py", "content": "B = 2\\n"}}\n```', "tool_calls": [], "usage": {}}
                        return {"content": "Done b.py.", "tool_calls": [], "usage": {}}
                    if ":role:verifier" in sid or "WSA Verifier" in sys_head:
                        if not any(m.get("role") == "tool" for m in messages):
                            return {"content": None, "tool_calls": [{"id": "verify", "function": {
                                "name": "run_python", "arguments": json.dumps({"code": "import a, b; assert a.A == 1 and b.B == 2"})}}]}
                        return {"content": '```json\n{"verdict": "pass", "checks": ["ok"], "failures": [], "summary": "Verified."}\n```', "tool_calls": [], "usage": {}}
                    return {"content": "Done.", "tool_calls": [], "usage": {}}

            orig_root = tools.get_project_root()
            try:
                res = run_orchestrated_turn("Build two independent modules a.py and b.py", state, DualRelayClient())
            finally:
                tools.set_project_root(orig_root)

            self.assertTrue(res["ok"])
            self.assertGreaterEqual(len(coder_relays), 2)
            self.assertTrue(all(relay is None for relay in coder_relays))
            self.assertEqual((root / "a.py").read_text(encoding="utf-8"), "A = 1\n")
            self.assertEqual((root / "b.py").read_text(encoding="utf-8"), "B = 2\n")

    def test_slash_commands_team_plan_verify(self):
        from wsa_agent.commands import handle_command
        from wsa_agent.session import SessionState

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = SessionState(project_root=root, session_id="cmd_test")

            self.assertEqual(state.thread_mode, "single")
            self.assertEqual(handle_command("/team on", state), (True, True))
            self.assertEqual(state.team_mode, "on")
            self.assertEqual(handle_command("/team role-chats", state), (True, True))
            self.assertEqual(state.thread_mode, "roles")
            self.assertEqual(handle_command("/team single-chat", state), (True, True))
            self.assertEqual(state.thread_mode, "single")
            self.assertEqual(handle_command("/team dual", state), (True, True))
            self.assertTrue(state.dual_lane)
            self.assertEqual(handle_command("/team single", state), (True, True))
            self.assertFalse(state.dual_lane)
            self.assertEqual(handle_command("/team auto", state), (True, True))
            self.assertEqual(state.team_mode, "auto")

            class CmdMockClient:
                api_base = "http://127.0.0.1:8081/v1"
                api_key = ""

                def stream(self, messages, model, session_id=None, **kwargs):
                    sys_head = str(messages[0].get("content") or "") if messages else ""
                    if ":role:architect" in str(session_id or "") or "WSA Architect" in sys_head:
                        return {
                            "content": '```json\n[{"id": "p1", "title": "Step 1", "owned_files": ["x.py"], "depends_on": []}]\n```',
                            "tool_calls": [],
                            "usage": {},
                        }
                    if ":role:verifier" in str(session_id or "") or "WSA Verifier" in sys_head:
                        return {
                            "content": '```json\n{"verdict": "pass", "checks": ["ok"], "failures": [], "summary": "Clean"}\n```',
                            "tool_calls": [],
                            "usage": {},
                        }
                    return {"content": "No files yet.", "tool_calls": [], "usage": {}}

            c = CmdMockClient()
            self.assertEqual(handle_command("/plan Add feature X", state, client=c), (True, True))
            self.assertEqual(len(state.plan_items), 1)
            self.assertEqual(state.plan_items[0]["id"], "p1")
            self.assertEqual(handle_command("/verify", state, client=c), (True, True))
            self.assertEqual(handle_command("/plan clear", state, client=c), (True, True))
            self.assertEqual(state.plan_items, [])

    def test_single_chat_claude_pipeline_over_live_http_server_and_relay(self):
        import time
        from http.server import ThreadingHTTPServer
        import urllib.request
        import wsa_server as srv
        from wsa_server import relay as relay_mod
        from wsa_server.server import WSAHandler
        from wsa_agent import tools
        from wsa_agent.client import WSAClient
        from wsa_agent.session import SessionState
        from wsa_agent.agent import run_turn

        with srv._dom_relay_lock:
            orig_relays = dict(srv._dom_relay.get("relays") or {})
            orig_sessions = dict(srv._dom_relay.get("sessions") or {})
            orig_active = srv._dom_relay.get("active_relay_id")
            orig_pending = dict(srv._dom_relay.get("pending") or {})
            orig_recent = dict(srv._dom_relay.get("recent_requests") or {})
            srv._dom_relay["relays"] = {}
            srv._dom_relay["sessions"] = {}
            srv._dom_relay["active_relay_id"] = None
            srv._dom_relay["pending"] = {}
            srv._dom_relay["recent_requests"] = {}
        relay_mod._PENDING_SESSION_META.clear()

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), WSAHandler)
        port = httpd.server_address[1]
        srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        srv_thread.start()

        relay_stop = threading.Event()
        relay_ready = threading.Event()
        delivered_prompts = []
        claude_url = "https://claude.ai/chat/c1234567-89ab-cdef"

        def _post_json(path: str, payload: dict) -> dict:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))

        def _get_json(path: str):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))

        def _claude_relay_worker():
            step = 0
            poll_url = "/pending-prompt?client_id=chrome_claude&browser=chrome&label=work&provider=claude&has_claude=1&claude_model=Sonnet%205%20High"
            while not relay_stop.is_set():
                try:
                    pend = _get_json(poll_url)
                    relay_ready.set()
                    if not isinstance(pend, dict) or not pend.get("id"):
                        time.sleep(0.02)
                        continue
                    p_id = pend["id"]
                    if str(pend.get("session_id", "")).endswith(":role:workflow_router"):
                        mode = "orchestrate" if "Build a complete" in str(pend.get("prompt")) else "direct"
                        _post_json("/pending-response", {
                            "id": p_id, "response": json.dumps({"mode": mode, "reason": "fixture scope"}),
                            "model": "Sonnet 5 High", "client_id": "chrome_claude",
                            "chat_url": "https://claude.ai/chat/router-fixture",
                        })
                        continue
                    step += 1
                    delivered_prompts.append(pend)
                    ptxt = str(pend.get("prompt") or "")

                    if step == 1:
                        reply = '```tool_call\n{"name": "list_dir", "arguments": {"path": "."}}\n```'
                        think = "Checking workspace structure first."
                    elif step == 2:
                        reply = "Workspace is clean; ready to plan."
                        think = ""
                    elif step == 3:
                        dag = json.dumps([
                            {"id": "t1", "title": "Build calc.py", "owned_files": ["calc.py"], "depends_on": [], "acceptance": "add & mul"},
                            {"id": "t2", "title": "Build test_calc.py", "owned_files": ["test_calc.py"], "depends_on": ["t1"], "acceptance": "pytest pass"},
                        ])
                        reply = f"```json\n{dag}\n```"
                        think = "Designing 2-step DAG."
                    elif step == 4:
                        reply = '```tool_call\n{"name": "write_file", "arguments": {"path": "calc.py", "content": "def add(a, b):\\n    return a + b\\ndef mul(a, b):\\n    return a + b\\n"}}\n```'
                        think = ""
                    elif step == 5:
                        reply = '```tool_call\n{"name": "run_python", "arguments": {"code": "import calc; print(calc.add(1,2))"}}\n```'
                        think = ""
                    elif step == 6:
                        reply = "Created calc.py for task t1."
                        think = ""
                    elif step == 7:
                        reply = '```tool_call\n{"name": "write_file", "arguments": {"path": "test_calc.py", "content": "from calc import add, mul\\ndef test_ops():\\n    assert add(2,3) == 5\\n    assert mul(3,4) == 12\\n"}}\n```'
                        think = ""
                    elif step == 8:
                        reply = '```tool_call\n{"name": "run_python", "arguments": {"code": "import test_calc; print(\'written\')"}}\n```'
                        think = ""
                    elif step == 9:
                        reply = "Created test_calc.py for task t2."
                        think = ""
                    elif step == 10:
                        import sys as _sys
                        reply = "```tool_call\n" + json.dumps({"name": "run_command", "arguments": {"command": f"& '{_sys.executable}' -m pytest -x -q"}}) + "\n```"
                        think = ""
                    elif step == 11:
                        reply = '```json\n{"verdict": "fail", "checks": ["pytest failed"], "failures": [{"file": "calc.py", "issue": "mul(3,4) returned 7"}], "summary": "Fix mul()."}\n```'
                        think = "Spotted bug in mul()."
                    elif step == 12:
                        reply = '```tool_call\n{"name": "patch_file", "arguments": {"path": "calc.py", "old_string": "def mul(a, b):\\n    return a + b", "new_string": "def mul(a, b):\\n    return a * b"}}\n```'
                        think = ""
                    elif step == 13:
                        import sys as _sys
                        reply = "```tool_call\n" + json.dumps({"name": "run_command", "arguments": {"command": f"& '{_sys.executable}' -m pytest -x -q"}}) + "\n```"
                        think = ""
                    elif step == 14:
                        reply = "Patched mul() and verified pytest passed."
                        think = ""
                    elif step == 15:
                        import sys as _sys
                        reply = "```tool_call\n" + json.dumps({"name": "run_command", "arguments": {"command": f"& '{_sys.executable}' -m pytest -x -q"}}) + "\n```"
                        think = ""
                    elif step == 16:
                        reply = '```json\n{"verdict": "pass", "checks": ["pytest: 1 passed"], "failures": [], "summary": "All verified."}\n```'
                        think = ""
                    elif step == 17:
                        reply = '```tool_call\n{"name": "patch_file", "arguments": {"path": "calc.py", "old_string": "def mul(a, b):\\n    return a * b", "new_string": "def mul(a, b):\\n    return a * b\\ndef sub(a, b):\\n    return a - b"}}\n```'
                        think = ""
                    elif step == 18:
                        import sys as _sys
                        reply = "```tool_call\n" + json.dumps({"name": "run_command", "arguments": {"command": f"& '{_sys.executable}' -m pytest -x -q"}}) + "\n```"
                        think = ""
                    else:
                        reply = "Added sub(a, b) in calc.py and re-ran pytest cleanly in the same Claude chat."
                        think = ""

                    _post_json(
                        "/pending-response",
                        {
                            "id": p_id,
                            "response": reply,
                            "thinking": think,
                            "model": "Sonnet 5 High",
                            "chat_url": claude_url,
                            "client_id": "chrome_claude",
                        },
                    )
                except Exception:
                    time.sleep(0.02)

        worker = threading.Thread(target=_claude_relay_worker, daemon=True)
        worker.start()
        self.assertTrue(relay_ready.wait(timeout=3.0))

        orig_root = tools.get_project_root()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                tools.set_project_root(root)
                state = SessionState(project_root=root, selected_model="claude", session_id="claude_e2e_single", permission_mode="auto")
                client = WSAClient(api_base=f"http://127.0.0.1:{port}/v1", api_key="sk-wsa")

                # Build project (Explorer -> Architect -> Coder t1 -> Coder t2 -> Verifier fail -> Repair -> Verifier pass)
                run_turn(
                    "Build a complete backend calc.py API, client.py consumer, and test_calc.py pytest test suite end-to-end.",
                    state,
                    client,
                )
                # Follow-up request in main chat — must continue inside exact same Claude chat URL
                run_turn("Add sub(a, b) to calc.py and re-verify.", state, client)

                self.assertEqual(state.active_chat_url, claude_url)
                self.assertEqual((root / "calc.py").read_text(encoding="utf-8"), "def add(a, b):\n    return a + b\ndef mul(a, b):\n    return a * b\ndef sub(a, b):\n    return a - b\n")

                # 1) Every single stage and follow-up shared the exact single session_id
                self.assertGreaterEqual(len(delivered_prompts), 19)
                self.assertEqual({p["session_id"] for p in delivered_prompts}, {"claude_e2e_single"})
                self.assertTrue(delivered_prompts[0]["new_chat"])
                self.assertTrue(all(not p["new_chat"] for p in delivered_prompts[1:]))
                self.assertTrue(all(p["chat_url"] == claude_url for p in delivered_prompts[1:]))

                # 2) Initial turn used Claude-native transparent CLI helper protocol (zero injection phrasing / zero Artifacts / crisp coding style)
                p0 = delivered_prompts[0]["prompt"]
                self.assertIn("Context on how we're collaborating here, what `wsa` is", p0)
                self.assertIn("asks my confirmation before touching anything", p0)
                self.assertIn("tight, direct coding-agent", p0)
                self.assertIn("Please avoid calling Claude's built-in", p0)
                self.assertNotIn("set_dynamic_rule(", p0)
                self.assertNotIn("spawn_subagent(", p0)
                self.assertNotIn("<local_cli_helper_protocol", p0)
                self.assertNotIn("You have full access to local workspace", p0)
                self.assertNotIn("unrestricted frontier", p0)

                # 3) Coder transition in same chat automatically injected newly unlocked patch_file/write_file tools + Workflow Phase update!
                coder_t1_prompt = next(p["prompt"] for p in delivered_prompts if "Execute task `t1`" in p["prompt"])
                self.assertIn("Workflow phase focus for our next step in this same chat:", coder_t1_prompt)
                self.assertIn("Workflow Phase — Coder:", coder_t1_prompt)
                self.assertIn("Active JSON action shapes accepted by my local `wsa` CLI parser", coder_t1_prompt)
                self.assertIn("patch_file(", coder_t1_prompt)

                # 4) Task 2 & Repair delta turns preserved Scoped Stage Context (ledger/prior tasks/verifier diagnostics)
                coder_t2_prompt = next(p["prompt"] for p in delivered_prompts if "Execute task `t2`" in p["prompt"])
                self.assertIn("### Scoped Stage Context (Current Workspace & Ledger)", coder_t2_prompt)
                self.assertIn("calc.py", coder_t2_prompt)
                self.assertIn("Completed Prior Task Results:", coder_t2_prompt)

                repair_prompt = next(p["prompt"] for p in delivered_prompts if "Fix the exact defects reported by the independent Verifier" in p["prompt"])
                self.assertIn("mul(3,4) returned 7", repair_prompt)
                self.assertIn("### Scoped Stage Context", repair_prompt)

                # 5) Main follow-up restored full main role/toolset cleanly inside same chat
                followup_prompt = next(p["prompt"] for p in delivered_prompts if "Add sub(a, b) to calc.py" in p["prompt"])
                self.assertIn("Workflow phase focus for our next step in this same chat:", followup_prompt)
                self.assertIn("Workflow Phase — Main Coding:", followup_prompt)
                self.assertIn("patch_file(", followup_prompt)
        finally:
            tools.set_project_root(orig_root)
            relay_stop.set()
            worker.join(timeout=2.0)
            httpd.shutdown()
            httpd.server_close()
            relay_mod._PENDING_SESSION_META.clear()
            with srv._dom_relay_lock:
                srv._dom_relay["relays"] = orig_relays
                srv._dom_relay["sessions"] = orig_sessions
                srv._dom_relay["active_relay_id"] = orig_active
                srv._dom_relay["pending"] = orig_pending
                srv._dom_relay["recent_requests"] = orig_recent

    def test_claude_cds_dom_runtime_and_composer_in_node_vm(self):
        import subprocess

        bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
        script = f"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const bg = require({json.dumps(bg_path)});

(async () => {{
  let fakeNow = 20000;
  let execCalls = [];
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
      if (text) this.childNodes.push({{ nodeType: 3, textContent: String(text) }});
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
      if (this._text) this.childNodes.push({{ nodeType: 3, textContent: this._text }});
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
            const am = seg.match(/^\\[([^=^*\\]]+)(\\^|\\*)?(?:=['"]?([^'"\\]]+?)['"]?(?:\\s+i)?)?\\]/);
            if (!am) return false;
            const val = this.getAttribute(am[1]);
            if (val === null) return false;
            if (!am[3]) return true;
            if (am[2] === "*") return val.toLowerCase().includes(am[3].toLowerCase());
            if (am[2] === "^") return val.startsWith(am[3]);
            return val === am[3];
          }});
        }}
        return true;
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
  const editorEl = new MockElement("div", {{ contenteditable: "true", role: "textbox", "aria-label": "Write your prompt to Claude" }}, "tiptap ProseMirror");
  const sendBtn = new MockElement("button", {{ "aria-label": "Send message" }});
  const modelBtn = new MockElement("button", {{ "data-cds": "ModelSelectorDropdown", "aria-label": "Model: Sonnet 5 High" }}, "", "Sonnet 5 High");
  rootEl.appendChild(editorEl);
  rootEl.appendChild(sendBtn);
  rootEl.appendChild(modelBtn);

  // Live claude.ai article > [data-cds='AssistantMessage'] structure
  const articleEl = new MockElement("article", {{ "aria-label": "Currently streaming message" }});
  const asstMsg = new MockElement("div", {{ "data-cds": "AssistantMessage" }});
  const srHeading = new MockElement("h3", {{}}, "sr-only", "Claude responded: ...");
  const turnStatus = new MockElement("div", {{ "data-cds": "TurnStatus" }});
  const stepEl = new MockElement("div", {{ "data-cds": "TurnStatusStep" }}, "", "Weighing whether any refusal is warranted.");
  turnStatus.appendChild(stepEl);

  const proseEl = new MockElement("div", {{}}, "prose");
  const mdEl = new MockElement("div", {{}}, "standard-markdown");
  const copyWrapper = new MockElement("div", {{ "aria-label": "tool_call code" }}, "relative group/copy bg-bg-000/50");
  const codeCopyBtn = new MockElement("button", {{ "aria-label": "Copy to clipboard" }});
  const langHdr = new MockElement("div", {{}}, "", "tool_call");
  const preEl = new MockElement("pre");
  const codeEl = new MockElement("code", {{}}, "language-tool_call", '{{"name": "read_file", "arguments": {{"path": "app.py"}}}}');
  preEl.appendChild(codeEl);
  copyWrapper.appendChild(codeCopyBtn);
  copyWrapper.appendChild(langHdr);
  copyWrapper.appendChild(preEl);
  mdEl.appendChild(copyWrapper);
  proseEl.appendChild(mdEl);

  asstMsg.appendChild(srHeading);
  asstMsg.appendChild(turnStatus);
  asstMsg.appendChild(proseEl);
  articleEl.appendChild(asstMsg);

  const pageSb = {{
    console,
    Date: Object.assign(function() {{ return new Date(fakeNow); }}, {{ now: () => fakeNow }}),
    Math, Object, Array, String, Boolean, Number, Set, Map, Promise, JSON, RegExp, setTimeout,
    Node: {{ TEXT_NODE: 3, ELEMENT_NODE: 1 }},
    Element: MockElement,
    InputEvent: class {{ constructor(t) {{ this.type = t; }} }},
    Event: class {{ constructor(t) {{ this.type = t; }} }},
    KeyboardEvent: class {{ constructor(t) {{ this.type = t; }} }},
    getComputedStyle: () => ({{ display: "block", visibility: "visible", opacity: "1" }}),
    document: {{
      querySelector: (s) => rootEl.querySelector(s),
      querySelectorAll: (s) => rootEl.querySelectorAll(s),
      createRange: () => ({{ selectNodeContents: () => {{}} }}),
      execCommand: (_cmd, _ui, val) => {{ execCalls.push(val); editorEl.textContent = val; return true; }},
    }},
    location: {{ href: "https://claude.ai/chat/de377dc9-1542-4f97-b29d-139c53537c34" }},
    window: null,
    globalThis: null,
  }};
  pageSb.window = pageSb;
  pageSb.window.getSelection = () => ({{ removeAllRanges: () => {{}}, addRange: () => {{}} }});
  pageSb.globalThis = pageSb;

  vm.runInNewContext("(" + bg._claudeInstallPageRuntimeFn.toString() + ")()", pageSb);
  const startRes = await vm.runInNewContext("(" + bg._claudeStartTurnFn.toString() + ")('cds-1', " + JSON.stringify("Line1\\r\\nLine2") + ")", pageSb);
  assert.strictEqual(startRes.ok, true);
  assert.strictEqual(startRes.model, "Sonnet 5 High");
  // Windows CRLF normalized & inserted atomically without Enter-splitting
  assert.strictEqual(execCalls[0], "Line1\\nLine2");

  // While streaming code block with its own 'Copy to clipboard' button: MUST NOT finish early!
  rootEl.appendChild(articleEl);
  fakeNow = 20500;
  let poll1 = vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('cds-1')", pageSb);
  assert.strictEqual(poll1.done, false);
  // Thinking captured separately and excluded from answer text; code-block language chrome stripped!
  assert.strictEqual(poll1.thinking, "Weighing whether any refusal is warranted.");
  assert.ok(!poll1.text.includes("Weighing whether"), "Thinking bled into text");
  assert.ok(!poll1.text.includes("Copy to clipboard"), "Code copy bled into text");
  assert.ok(!poll1.text.startsWith("tool_call\\n"), "Header label 'tool_call' bled before fence");
  assert.ok(poll1.text.includes("```tool_call\\n"), "Missing tool_call fence");

  // Even after streaming article label clears, code-block 'Copy to clipboard' alone must NOT set hasActionBar!
  articleEl.setAttribute("aria-label", "Message 2 of 2");
  fakeNow = 21100; // 600ms >= 450ms (would falsely complete if code-block Copy set hasActionBar!)
  let pollFalseWait = vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('cds-1')", pageSb);
  assert.strictEqual(pollFalseWait.done, false, "Code-block Copy to clipboard falsely triggered hasActionBar!");

  // Once footer [data-cds='MessageActions'] toolbar appears, quiet >= 450ms completes cleanly without label bleed!
  const msgActions = new MockElement("div", {{ "data-cds": "MessageActions", role: "toolbar", "aria-label": "Message actions" }});
  msgActions.appendChild(new MockElement("button", {{ "aria-label": "Copy" }}));
  msgActions.appendChild(new MockElement("button", {{ "aria-label": "Good response" }}));
  asstMsg.appendChild(msgActions);
  fakeNow = 21150;
  let pollDone = vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('cds-1')", pageSb);
  assert.strictEqual(pollDone.done, true);
  assert.ok(!pollDone.text.includes("Good response"));
  console.log("CDS_LIVE_DOM_OK");
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        self.assertIn("CDS_LIVE_DOM_OK", proc.stdout)

    def test_claude_extended_thinking_never_leaks_and_tables_serialize_gfm_in_node_vm(self):
        import subprocess

        bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
        script = f"""
const assert = require("assert");
const vm = require("vm");
const bg = require({json.dumps(bg_path)});

(async () => {{
  let fakeNow = 50000;
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
      if (text) this.childNodes.push({{ nodeType: 3, textContent: String(text) }});
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
      if (this._text) this.childNodes.push({{ nodeType: 3, textContent: this._text }});
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
            const am = seg.match(/^\\[([^=^*\\]]+)(\\^|\\*)?(?:=['"]?([^'"\\]]+?)['"]?(?:\\s+i)?)?\\]/);
            if (!am) return false;
            const val = this.getAttribute(am[1]);
            if (val === null) return false;
            if (!am[3]) return true;
            if (am[2] === "*") return val.toLowerCase().includes(am[3].toLowerCase());
            if (am[2] === "^") return val.startsWith(am[3]);
            return val === am[3];
          }});
        }}
        return true;
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
  const editorEl = new MockElement("div", {{ contenteditable: "true", role: "textbox", "aria-label": "Write your prompt to Claude" }}, "tiptap ProseMirror");
  const sendBtn = new MockElement("button", {{ "aria-label": "Send message" }});
  const modelBtn = new MockElement("button", {{ "data-cds": "ModelSelectorDropdown", "aria-label": "Model: Sonnet 5 High" }}, "", "Sonnet 5 High");
  rootEl.appendChild(editorEl);
  rootEl.appendChild(sendBtn);
  rootEl.appendChild(modelBtn);

  // Live streaming Extended Thinking TurnStatus (collapsed, NO answer .prose yet!)
  const articleEl = new MockElement("div", {{ role: "article", "aria-label": "Currently streaming message" }});
  const asstMsg = new MockElement("div", {{ "data-cds": "AssistantMessage", "data-cds-reply-type": "0" }});
  asstMsg.appendChild(new MockElement("h3", {{}}, "sr-only", "Claude responded:"));

  const bodyWrap = new MockElement("div");
  const turnStatus = new MockElement("div", {{ "data-cds": "TurnStatus" }});
  const hdrRow = new MockElement("div", {{}}, "transition-[margin-inline-start]");
  const toggleBtn = new MockElement("button", {{ "aria-expanded": "false" }}, "cds-reset");
  toggleBtn.appendChild(new MockElement("div", {{}}, "", "Weighing how to handle the relay-style requests"));
  toggleBtn.appendChild(new MockElement("div", {{}}, "", "running"));
  const timerEl = new MockElement("div", {{ "aria-hidden": "true" }}, "", "22s");
  const srStatus = new MockElement("span", {{ role: "status", "aria-live": "polite" }}, "sr-only", "Weighing how to handle the relay-style requests");
  hdrRow.appendChild(toggleBtn);
  hdrRow.appendChild(timerEl);
  hdrRow.appendChild(srStatus);
  turnStatus.appendChild(hdrRow);
  bodyWrap.appendChild(turnStatus);
  asstMsg.appendChild(bodyWrap);
  articleEl.appendChild(asstMsg);

  const pageSb = {{
    console,
    Date: Object.assign(function() {{ return new Date(fakeNow); }}, {{ now: () => fakeNow }}),
    Math, Object, Array, String, Boolean, Number, Set, Map, Promise, JSON, RegExp, setTimeout,
    Node: {{ TEXT_NODE: 3, ELEMENT_NODE: 1 }},
    Element: MockElement,
    InputEvent: class {{ constructor(t) {{ this.type = t; }} }},
    Event: class {{ constructor(t) {{ this.type = t; }} }},
    KeyboardEvent: class {{ constructor(t) {{ this.type = t; }} }},
    getComputedStyle: () => ({{ display: "block", visibility: "visible", opacity: "1" }}),
    document: {{
      querySelector: (s) => rootEl.querySelector(s),
      querySelectorAll: (s) => rootEl.querySelectorAll(s),
      createRange: () => ({{ selectNodeContents: () => {{}} }}),
      execCommand: (_c, _u, val) => {{ editorEl.textContent = val; return true; }},
    }},
    location: {{ href: "https://claude.ai/chat/5b9c856f-be55-4524-b3e8-a7c812ca855f" }},
    window: null,
    globalThis: null,
  }};
  pageSb.window = pageSb;
  pageSb.window.getSelection = () => ({{ removeAllRanges: () => {{}}, addRange: () => {{}} }});
  pageSb.globalThis = pageSb;

  vm.runInNewContext("(" + bg._claudeInstallPageRuntimeFn.toString() + ")()", pageSb);
  assert.strictEqual(pageSb.__wsaClaudeRuntime.version, 4);
  const startRes = await vm.runInNewContext("(" + bg._claudeStartTurnFn.toString() + ")('ext-1', 'Audit codebase')", pageSb);
  assert.strictEqual(startRes.ok, true);

  rootEl.appendChild(articleEl);
  fakeNow = 50500;
  const pollThink = vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('ext-1')", pageSb);
  assert.strictEqual(pollThink.done, false);
  assert.strictEqual(pollThink.thinking, "Weighing how to handle the relay-style requests");
  assert.strictEqual(pollThink.text, "", "Thinking leaked into empty answer text during Phase B!");

  // Phase C: expanded base-ui thinking markdown + Final Answer with HTML table & nested list
  const expPanel = new MockElement("div", {{ id: "base-ui-r1" }});
  expPanel.appendChild(new MockElement("p", {{}}, "text-footnote", "Thought for 21s"));
  const expProse = new MockElement("div", {{ "data-prose-size": "inherit" }}, "prose");
  const expMd = new MockElement("div", {{}}, "standard-markdown");
  expMd.appendChild(new MockElement("p", {{}}, "", "Testing a contradiction argument on the primes."));
  expProse.appendChild(expMd);
  expPanel.appendChild(expProse);
  turnStatus.appendChild(expPanel);

  const ansProse = new MockElement("div", {{ "data-prose-tables": "flat", "data-prose-heading-size": "sm" }}, "prose");
  const ansMd = new MockElement("div", {{}}, "standard-markdown");
  const tbl = new MockElement("table");
  const tr1 = new MockElement("tr");
  tr1.appendChild(new MockElement("th", {{}}, "", "Area"));
  tr1.appendChild(new MockElement("th", {{}}, "", "Fix"));
  const tr2 = new MockElement("tr");
  tr2.appendChild(new MockElement("td", {{}}, "", "DOM"));
  tr2.appendChild(new MockElement("td", {{}}, "", "Clean GFM"));
  tbl.appendChild(tr1);
  tbl.appendChild(tr2);
  ansMd.appendChild(tbl);

  const ul = new MockElement("ul");
  const li = new MockElement("li", {{}}, "", "Parent item");
  const subUl = new MockElement("ul");
  subUl.appendChild(new MockElement("li", {{}}, "", "Nested child"));
  li.appendChild(subUl);
  ul.appendChild(li);
  ansMd.appendChild(ul);
  ansProse.appendChild(ansMd);
  bodyWrap.appendChild(ansProse);

  articleEl.setAttribute("aria-label", "Message 2 of 2");
  const msgActions = new MockElement("div", {{ "data-cds": "MessageActions", role: "toolbar", "aria-label": "Message actions" }});
  msgActions.appendChild(new MockElement("button", {{ "aria-label": "Copy" }}));
  asstMsg.appendChild(msgActions);

  fakeNow = 51200;
  const pollAns = vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('ext-1')", pageSb);
  assert.strictEqual(pollAns.done, false);
  fakeNow = 51700; // 500ms >= 450ms quiet window with [data-cds='MessageActions'] present
  const pollDone = vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('ext-1')", pageSb);
  assert.strictEqual(pollDone.done, true);
  assert.ok(pollDone.thinking.includes("Testing a contradiction argument on the primes."), pollDone.thinking);
  assert.ok(!pollDone.thinking.includes("Thought for 21s"), pollDone.thinking);
  assert.ok(!pollDone.text.includes("contradiction argument"), pollDone.text);
  assert.ok(!pollDone.text.includes("Weighing how"), pollDone.text);
  assert.ok(pollDone.text.includes("| Area | Fix |\\n| --- | --- |\\n| DOM | Clean GFM |"), pollDone.text);
  assert.ok(pollDone.text.includes("- Parent item\\n  - Nested child"), pollDone.text);
  console.log("CLAUDE_EXTENDED_THINKING_GFM_OK");
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        self.assertIn("CLAUDE_EXTENDED_THINKING_GFM_OK", proc.stdout)

    def test_claude_v4_svg_baseval_multistep_baseui_codebox_and_shrink_reset_in_node_vm(self):
        import subprocess

        bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
        script = f"""
const assert = require("assert");
const vm = require("vm");
const bg = require({json.dumps(bg_path)});

(async () => {{
  let fakeNow = 60000;
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
      if (text) this.childNodes.push({{ nodeType: 3, textContent: String(text) }});
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
      if (this._text) this.childNodes.push({{ nodeType: 3, textContent: this._text }});
    }}
    getAttribute(k) {{ return k in this.attrs ? String(this.attrs[k]) : null; }}
    setAttribute(k, v) {{ this.attrs[k] = String(v); }}
    matches(sel) {{
      const parts = sel.split(",").map(s => s.trim()).filter(Boolean);
      return parts.some(p => {{
        const clsStr = typeof this.className === "string"
          ? this.className
          : (this.className && typeof this.className.baseVal === "string" ? this.className.baseVal : "");
        if (p.startsWith(".")) return clsStr.split(/\\s+/).includes(p.slice(1).split("[")[0]);
        const tagPrefix = p.match(/^([a-z0-9_-]+)/i);
        if (tagPrefix && this.tagName !== tagPrefix[1].toUpperCase()) return false;
        const attrSegs = p.match(/\\[[^\\]]+\\]/g);
        if (attrSegs) {{
          return attrSegs.every(seg => {{
            const am = seg.match(/^\\[([^=^*\\]]+)(\\^|\\*)?(?:=['"]?([^'"\\]]+?)['"]?(?:\\s+i)?)?\\]/);
            if (!am) return false;
            const val = this.getAttribute(am[1]);
            if (val === null) return false;
            if (!am[3]) return true;
            if (am[2] === "*") return val.toLowerCase().includes(am[3].toLowerCase());
            if (am[2] === "^") return val.startsWith(am[3]);
            return val === am[3];
          }});
        }}
        return true;
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
  const editorEl = new MockElement("div", {{ contenteditable: "true", role: "textbox" }}, "tiptap ProseMirror");
  const sendBtn = new MockElement("button", {{ "aria-label": "Send message" }});
  rootEl.appendChild(editorEl);
  rootEl.appendChild(sendBtn);

  // Case A: SVGAnimatedString inside TurnStatus + multi-step (expanded markdown + sibling running step) + article Currently streaming
  const articleEl = new MockElement("div", {{ role: "article", "aria-label": "Message 2 of 2 (Currently streaming)" }});
  const asstMsg = new MockElement("div", {{ "data-cds": "AssistantMessage", "data-cds-reply-type": "0" }});
  const bodyWrap = new MockElement("div");

  const turnStatus = new MockElement("div", {{ "data-cds": "TurnStatus" }});
  const step1 = new MockElement("div");
  const step1Btn = new MockElement("button", {{ "aria-expanded": "true" }});
  step1Btn.appendChild(new MockElement("span", {{}}, "", "Surveying the codebase"));
  const svgIcon = new MockElement("svg");
  svgIcon.className = {{ baseVal: "lucide lucide-chevron-down" }}; // SVGAnimatedString!
  step1Btn.appendChild(svgIcon);
  step1.appendChild(step1Btn);
  const step1Panel = new MockElement("div");
  const step1Md = new MockElement("div", {{}}, "standard-markdown");
  step1Md.appendChild(new MockElement("p", {{}}, "", "Mapping out the agent loop architecture."));
  step1Panel.appendChild(step1Md);
  step1.appendChild(step1Panel);

  const step2 = new MockElement("div");
  const step2Btn = new MockElement("button", {{ "aria-expanded": "false" }});
  step2Btn.appendChild(new MockElement("div", {{}}, "", "Tracing state persistence"));
  step2Btn.appendChild(new MockElement("span", {{}}, "sr-only", "running"));
  step2Btn.appendChild(new MockElement("div", {{ "aria-hidden": "true" }}, "", "12s"));
  step2.appendChild(step2Btn);

  turnStatus.appendChild(step1);
  turnStatus.appendChild(step2);
  bodyWrap.appendChild(turnStatus);
  asstMsg.appendChild(bodyWrap);
  articleEl.appendChild(asstMsg);

  const pageSb = {{
    console,
    Date: Object.assign(function() {{ return new Date(fakeNow); }}, {{ now: () => fakeNow }}),
    Math, Object, Array, String, Boolean, Number, Set, Map, Promise, JSON, RegExp, setTimeout,
    Node: {{ TEXT_NODE: 3, ELEMENT_NODE: 1 }},
    Element: MockElement,
    InputEvent: class {{ constructor(t) {{ this.type = t; }} }},
    Event: class {{ constructor(t) {{ this.type = t; }} }},
    KeyboardEvent: class {{ constructor(t) {{ this.type = t; }} }},
    getComputedStyle: () => ({{ display: "block", visibility: "visible", opacity: "1" }}),
    document: {{
      querySelector: (s) => rootEl.querySelector(s),
      querySelectorAll: (s) => rootEl.querySelectorAll(s),
      createRange: () => ({{ selectNodeContents: () => {{}} }}),
      execCommand: (_c, _u, val) => {{ editorEl.textContent = val; return true; }},
    }},
    location: {{ href: "https://claude.ai/chat/5b9c856f-be55-4524-b3e8-a7c812ca855f" }},
    window: null,
    globalThis: null,
  }};
  pageSb.window = pageSb;
  pageSb.window.getSelection = () => ({{ removeAllRanges: () => {{}}, addRange: () => {{}} }});
  pageSb.globalThis = pageSb;

  vm.runInNewContext("(" + bg._claudeInstallPageRuntimeFn.toString() + ")()", pageSb);
  assert.strictEqual(pageSb.__wsaClaudeRuntime.version, 4);
  const s1 = await vm.runInNewContext("(" + bg._claudeStartTurnFn.toString() + ")('v4-1', 'Test v4')", pageSb);
  assert.strictEqual(s1.ok, true);

  rootEl.appendChild(articleEl);
  fakeNow = 60500;
  const p1 = vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('v4-1')", pageSb);
  assert.strictEqual(p1.done, false, "article[aria-label*='Currently streaming'] must keep done=false");
  assert.strictEqual(p1.text, "", "Multi-step TurnStatus must not leak thinking into text");
  assert.ok(p1.thinking.includes("Mapping out the agent loop architecture."), p1.thinking);
  assert.ok(p1.thinking.includes("Tracing state persistence"), p1.thinking);
  assert.ok(!p1.thinking.includes("12s"), p1.thinking);
  assert.ok(!/\\brunning\\b/i.test(p1.thinking), p1.thinking);

  // Case B: Base UI aria-expanded + aria-controls outside TurnStatus, anonymous code-block tool_call header, and shrink/reset to short answer
  bodyWrap.childNodes = [];
  bodyWrap.children = [];
  const baseBtn = new MockElement("button", {{ "aria-expanded": "true", "aria-controls": "base-ui-think-9" }}, "", "Auditing code extraction");
  const basePanel = new MockElement("div", {{ id: "base-ui-think-9" }});
  const baseMd = new MockElement("div", {{}}, "standard-markdown");
  baseMd.appendChild(new MockElement("p", {{}}, "", "Checking Base UI panel isolation."));
  basePanel.appendChild(baseMd);
  bodyWrap.appendChild(baseBtn);
  bodyWrap.appendChild(basePanel);

  // Step 2a: stream longer transient text first, then shrink/reset to short clean "Good" while still streaming
  const ansProse = new MockElement("div", {{}}, "prose");
  const ansMd = new MockElement("div", {{}}, "standard-markdown");
  ansMd.appendChild(new MockElement("p", {{}}, "", "Very long transient status leak before short reply"));
  ansProse.appendChild(ansMd);
  bodyWrap.appendChild(ansProse);

  fakeNow = 60900;
  const pNoisy = vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('v4-1')", pageSb);
  assert.strictEqual(pNoisy.done, false);
  assert.strictEqual(pNoisy.text, "Very long transient status leak before short reply");

  ansMd.childNodes = [];
  ansMd.children = [];
  ansMd.appendChild(new MockElement("p", {{}}, "", "Good"));
  fakeNow = 61050;
  const pShrink = vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('v4-1')", pageSb);
  assert.strictEqual(pShrink.done, false);
  assert.strictEqual(pShrink.text, "Good", "Shorter clean answer must replace stale transient noise during streaming");

  // Step 2b: anonymous code-block tool_call header + MessageActions -> clean tool_call fence, zero leaked label
  ansMd.childNodes = [];
  ansMd.children = [];
  const codeWrap = new MockElement("div", {{}}, "code-wrapper-anon");
  const hdr = new MockElement("div");
  hdr.appendChild(new MockElement("div", {{}}, "font-mono", "tool_call"));
  const copyBtn = new MockElement("button", {{}}, "", "Copy");
  hdr.appendChild(copyBtn);
  const preWrap = new MockElement("div");
  const pre = new MockElement("pre");
  pre.appendChild(new MockElement("code", {{}}, "", '{{"name":"read_file","arguments":{{"path":"a.py"}}}}'));
  preWrap.appendChild(pre);
  codeWrap.appendChild(hdr);
  codeWrap.appendChild(preWrap);
  ansMd.appendChild(codeWrap);
  ansMd.appendChild(new MockElement("p", {{}}, "", "Good"));

  articleEl.setAttribute("aria-label", "Message 2 of 2");
  const actions = new MockElement("div", {{ "data-cds": "MessageActions" }});
  actions.appendChild(new MockElement("button", {{ "aria-label": "Copy" }}));
  asstMsg.appendChild(actions);

  fakeNow = 61200;
  vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('v4-1')", pageSb);
  fakeNow = 61800;
  const pDone = vm.runInNewContext("(" + bg._claudePollTurnFn.toString() + ")('v4-1')", pageSb);
  assert.strictEqual(pDone.done, true);
  assert.ok(pDone.thinking.includes("Checking Base UI panel isolation."), pDone.thinking);
  assert.ok(!pDone.text.includes("Checking Base UI panel isolation."), pDone.text);
  assert.ok(pDone.text.includes("```tool_call\\n{{\\"name\\":\\"read_file\\""), pDone.text);
  assert.ok(!pDone.text.includes("tool_call\\n\\n```"), pDone.text);
  assert.ok(!pDone.text.includes("tool_calltool_call"), pDone.text);
  assert.ok(pDone.text.includes("Good"), pDone.text);
  console.log("CLAUDE_V4_NODE_VM_OK");
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        self.assertIn("CLAUDE_V4_NODE_VM_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
