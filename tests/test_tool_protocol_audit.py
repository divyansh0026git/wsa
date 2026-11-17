"""Regression & integration tests for the 4 tool-protocol bottlenecks (Issues A-D)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestIssueAMultiHunkPatchAndJsonRecovery(unittest.TestCase):
    """Issue A: schema exposure, JSON recovery, canonical normalization, and atomic multi-hunk patch."""

    def test_tool_prompt_exposes_nested_hunks_fields_within_chatgpt_budget(self):
        from wsa_agent.tools import TOOL_DEFINITIONS
        from wsa_server import multimodal as mm

        block = mm._format_tools_prompt_block(TOOL_DEFINITIONS, provider="chatgpt")
        self.assertIn("patch_file(", block)
        self.assertIn("array<{old, new, start_line?, end_line?}>", block)

        full_prompt, _ = mm.messages_to_prompt(
            [{"role": "user", "content": "Edit multiple functions cleanly."}],
            tools=TOOL_DEFINITIONS,
            provider="chatgpt",
        )
        self.assertLessEqual(len(full_prompt.encode("utf-8")), mm.CHATGPT_PROMPT_MAX_BYTES)

    def test_parser_recovers_malformed_multihunk_json_variants(self):
        from wsa_agent.agent import _extract_tool_batch_plan
        from wsa_server.tools import inspect_unparsed_tool_attempts_detailed, parse_tool_calls_with_meta

        # Case 1: unescaped inner quotes + missing comma between hunk objects + string concat
        raw_reply = (
            "Updating taskboard across 3 sections.\n"
            "```tool_call\n"
            "{\n"
            '  "name": "patch_file",\n'
            '  "arguments": {\n'
            '    "path": "taskboard.py",\n'
            '    "hunks": [\n'
            '      {"old": "label = "old_title"", "new": "label = "new_title""}\n'
            '      {"old": "x = 1", "new": "x = 2" + \n " # bumped"},\n'
            '      {"old": "y = 3", "new": "y = 4",},\n'
            "    ]\n"
            "  }\n"
            "}\n"
            "```\n"
        )
        rem, calls, spans, dropped = parse_tool_calls_with_meta(raw_reply, allowed_names={"patch_file"})
        self.assertEqual(len(calls), 1, msg=f"rem={rem!r} dropped={dropped!r}")
        self.assertEqual(dropped, [])
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(len(args["hunks"]), 3)
        self.assertIn('"new_title"', args["hunks"][0]["new"])
        self.assertEqual(args["hunks"][1]["new"], "x = 2 # bumped")

        batch_calls, batch_rem, batch_pos, batch_dropped = _extract_tool_batch_plan(
            {"content": raw_reply, "tool_calls": []}, allowed_names={"patch_file"}
        )
        self.assertEqual(len(batch_calls), 1)
        self.assertEqual(batch_dropped, [])
        self.assertEqual(batch_rem, "Updating taskboard across 3 sections.")
        self.assertEqual(len(batch_pos), 1)

        # Case 2: real Python multi-hunk code with regex backslashes and quotes parses cleanly via server & agent
        py_hunks_reply = (
            "```tool_call\n"
            '{"name": "patch_file", "arguments": {"path": "wsa_agent/taskboard.py", "hunks": [\n'
            '  {"old": "PAT = r\\"\\\\s+\\\\d+\\"", "new": "PAT = r\\"\\\\s+\\\\w+\\""},\n'
            '  {"old": "def foo(x):\\n    return x + 1", "new": "def foo(x):\\n    return x + 2"},\n'
            '  {"old": "LABEL = \'old\'", "new": "LABEL = \'new\'"}\n'
            "]}}\n"
            "```\n"
        )
        rem_py, calls_py, _, dropped_py = parse_tool_calls_with_meta(py_hunks_reply, allowed_names={"patch_file"})
        self.assertEqual(len(calls_py), 1)
        self.assertEqual(dropped_py, [])
        self.assertEqual(len(json.loads(calls_py[0]["function"]["arguments"])["hunks"]), 3)

        # Case 3: fallback parser path when server parser is unavailable still extracts calls cleanly
        import wsa_agent.agent as agent_mod
        with mock.patch.object(agent_mod, "_srv_parse_tool_calls_with_meta", None):
            fb_calls, fb_rem = agent_mod._extract_tool_calls_detailed({"content": py_hunks_reply, "tool_calls": []})
        self.assertEqual(len(fb_calls), 1)
        self.assertEqual(fb_calls[0]["function"]["name"], "patch_file")
        self.assertEqual(fb_rem.strip(), "")

        # Case 4: genuinely truncated/invalid multi-hunk returns exact failure reason + target_path
        broken_reply = (
            "Trying edit.\n"
            "```tool_call\n"
            '{"name": "patch_file", "arguments": {"path": "taskboard.py", "hunks": [{"old": "a", "new": "b"'
        )
        details = inspect_unparsed_tool_attempts_detailed(broken_reply, allowed_names={"patch_file"})
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["name"], "patch_file")
        self.assertEqual(details[0]["target_path"], "taskboard.py")
        self.assertIn("Truncated", details[0]["reason"])

    def test_validate_and_normalize_patch_aliases_and_reject_silent_deletion(self):
        from wsa_agent.tools import execute_tool, get_project_root, set_project_root, validate_and_normalize_tool_call

        # 1. Alias normalization: edits + search/replace -> canonical hunks[{old, new}]
        ok, norm, err = validate_and_normalize_tool_call(
            "patch_file",
            {
                "path": "mod.py",
                "edits": [
                    {"search": "ALPHA = 1", "replace": "ALPHA = 2"},
                    {"find": "BETA = 3", "replacement": "BETA = 4", "start_line": 2, "end_line": 5},
                ],
            },
        )
        self.assertTrue(ok, msg=err)
        self.assertIsNone(err)
        self.assertEqual(
            norm["hunks"],
            [
                {"old": "ALPHA = 1", "new": "ALPHA = 2"},
                {"old": "BETA = 3", "new": "BETA = 4", "start_line": 2, "end_line": 5},
            ],
        )

        # 2. Missing 'new' replacement text MUST NOT silently default to ""
        bad_ok, _, bad_err = validate_and_normalize_tool_call(
            "patch_file",
            {"path": "mod.py", "hunks": [{"old": "ALPHA = 1"}]},
        )
        self.assertFalse(bad_ok)
        self.assertIn("[Validation Error]", bad_err)
        self.assertIn("missing 'new'", bad_err)

        # 3. Malformed raw JSON string is rejected cleanly (never silent args={})
        json_ok, _, json_err = validate_and_normalize_tool_call(
            "patch_file", '{"path": "mod.py", "hunks": ['
        )
        self.assertFalse(json_ok)
        self.assertIn("[Validation Error]", json_err)
        self.assertIn("Malformed JSON", json_err)

        # 4. Atomic multi-hunk execution: if hunk 2 fails, hunk 1 MUST NOT persist to disk
        orig_root = get_project_root()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "taskboard.py"
            orig_src = "A = 1\nB = 2\nC = 3\n"
            target.write_text(orig_src, encoding="utf-8")
            set_project_root(root)
            try:
                fail_res = execute_tool(
                    "patch_file",
                    {
                        "path": "taskboard.py",
                        "edits": [
                            {"before": "A = 1", "after": "A = 10"},
                            {"before": "NONEXISTENT = 999", "after": "C = 30"},
                        ],
                    },
                )
                self.assertIn("[Error]", fail_res)
                self.assertEqual(target.read_text(encoding="utf-8"), orig_src)

                ok_res = execute_tool(
                    "patch_file",
                    {
                        "path": "taskboard.py",
                        "edits": [
                            {"search": "A = 1", "replace": "A = 10"},
                            {"find": "B = 2", "replacement": "B = 20"},
                            {"old": "C = 3", "new": "C = 30"},
                        ],
                    },
                )
                self.assertIn("3 hunks applied", ok_res)
                self.assertEqual(target.read_text(encoding="utf-8"), "A = 10\nB = 20\nC = 30\n")
            finally:
                set_project_root(orig_root)


class TestIssueBBatchLifecycleAndDependencyGating(unittest.TestCase):
    """Issue B: unique call IDs, validation rejection, and prerequisite gating."""

    def test_dropped_write_blocks_dependent_command_without_blocking_independent_git_status(self):
        from wsa_agent import agent as agent_mod, tools
        from wsa_agent.session import SessionState

        mixed_response = (
            "Creating stress script and running it.\n"
            "```tool_call\n"
            '{"name": "write_file", "arguments": {"path": "_stress_check.py", "content": "print(\'unterminated\n'
            "```\n"
            "```tool_call\n"
            '{"name": "run_command", "arguments": {"command": "python _stress_check.py"}}\n'
            "```\n"
            "```tool_call\n"
            '{"name": "run_command", "arguments": {"command": "git status"}}\n'
            "```\n"
        )

        class MockClient:
            api_base = "http://127.0.0.1:8081/v1"
            api_key = ""

            def __init__(self):
                self.turn = 0
                self.seen_messages = []

            def stream(self, messages, model, **kwargs):
                self.turn += 1
                self.seen_messages.append(list(messages))
                if self.turn == 1:
                    return {"content": mixed_response, "tool_calls": [], "finish_reason": "stop", "usage": {}}
                return {"content": "Fixed missing write_file cleanly.", "tool_calls": [], "finish_reason": "stop", "usage": {}}

        dispatched = []

        def fake_exec(name, args):
            dispatched.append((name, dict(args)))
            return "STDOUT:\nnothing to commit\nExit code: 0\nStatus: completed (ok)"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = SessionState(project_root=root, session_id="issue_b_test", permission_mode="auto")
            orig_root = tools.get_project_root()
            cli = MockClient()
            try:
                with mock.patch.object(agent_mod, "execute_tool", side_effect=fake_exec):
                    agent_mod.execute_agent_loop("Write and run _stress_check.py", state, cli, quiet_ui=True)
            finally:
                tools.set_project_root(orig_root)

            # Dependent `python _stress_check.py` MUST NOT run; independent `git status` ran once.
            self.assertEqual(len(dispatched), 1, msg=f"Unexpected dispatch list: {dispatched}")
            self.assertEqual(dispatched[0][1].get("command"), "git status")

            tool_msgs = [m for m in cli.seen_messages[1] if m.get("role") == "tool"]
            asst_calls = next(m["tool_calls"] for m in cli.seen_messages[1] if m.get("role") == "assistant" and m.get("tool_calls"))
            self.assertEqual(len(tool_msgs), len(asst_calls))
            self.assertEqual([m["tool_call_id"] for m in tool_msgs], [c["id"] for c in asst_calls])
            self.assertIn("[Blocked]", tool_msgs[0]["content"])
            self.assertIn("_stress_check.py", tool_msgs[0]["content"])
            self.assertIn("Dropped Tool Block(s) Detected", tool_msgs[-1]["content"])

    def test_failed_syntax_write_and_explicit_depends_on_gate_subsequent_calls(self):
        from wsa_agent import agent as agent_mod, tools
        from wsa_agent.session import SessionState

        batch = [
            {
                "id": "dup_id",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "broken_mod.py", "content": "def oops(:\n  pass\n"}),
                },
            },
            {
                "id": "dup_id",
                "type": "function",
                "function": {
                    "name": "run_python",
                    "arguments": json.dumps({"code": "import broken_mod; print('ran')"}),
                },
            },
            {
                "id": "call_dep_3",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "other.txt", "depends_on": ["write_file"]}),
                },
            },
            {
                "id": "call_ok_4",
                "type": "function",
                "function": {
                    "name": "list_dir",
                    "arguments": json.dumps({"path": "."}),
                },
            },
        ]

        class MockClient:
            api_base = "http://127.0.0.1:8081/v1"
            api_key = ""

            def __init__(self):
                self.turn = 0
                self.prompts = []

            def stream(self, messages, model, **kwargs):
                self.turn += 1
                self.prompts.append(list(messages))
                if self.turn == 1:
                    return {"content": None, "tool_calls": batch, "finish_reason": "tool_calls", "usage": {}}
                return {"content": "Stopped cleanly.", "tool_calls": [], "finish_reason": "stop", "usage": {}}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "other.txt").write_text("hello\n", encoding="utf-8")
            state = SessionState(project_root=root, session_id="dep_gate_test")
            orig_root = tools.get_project_root()
            cli = MockClient()
            try:
                agent_mod.execute_agent_loop("Write and import broken_mod", state, cli, quiet_ui=True)
            finally:
                tools.set_project_root(orig_root)

            tool_msgs = [m for m in cli.prompts[1] if m.get("role") == "tool"]
            self.assertEqual(len(tool_msgs), 4)
            # Colliding IDs must have been made distinct and matched 1:1
            ids = [m["tool_call_id"] for m in tool_msgs]
            self.assertEqual(len(set(ids)), 4)
            self.assertIn("[Syntax Error", tool_msgs[0]["content"])
            self.assertIn("[Blocked]", tool_msgs[1]["content"])
            self.assertIn("[Blocked]", tool_msgs[2]["content"])
            self.assertIn("other.txt", tool_msgs[3]["content"])
            statuses = [h["execution_status"] for h in state.tool_history]
            self.assertEqual(statuses, ["completed", "blocked", "blocked", "completed"])


class TestIssueCPowerShellAndStructuredExec(unittest.TestCase):
    """Issue C: safe WinPS 5.1 multiline scripts, direct argv execution, and run_python args."""

    def test_real_argv_and_winps51_multiline_and_run_python_args(self):
        from wsa_agent.tools import execute_tool, get_project_root, set_project_root

        orig_root = get_project_root()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sub = root / "subpkg"
            sub.mkdir()
            (sub / "marker.txt").write_text("inside-sub\n", encoding="utf-8")
            set_project_root(root)
            try:
                # 1. Direct argv preserves quotes, spaces, and embedded newlines cleanly without PowerShell mangling
                argv_res = execute_tool(
                    "run_command",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "import json, sys; print(json.dumps(sys.argv[1:]))",
                            "space arg",
                            'quote "inner"',
                            "line1\nline2",
                        ]
                    },
                )
                self.assertIn("Exit code: 0", argv_res)
                self.assertIn("Status: completed (ok)", argv_res)
                self.assertIn('"quote \\"inner\\""', argv_res)
                self.assertIn('"line1\\nline2"', argv_res)

                # 2. Real Windows PowerShell 5.1 multiline script runs cleanly via -EncodedCommand
                ps_multi = execute_tool(
                    "run_command",
                    {
                        "command": "$a = 'multi-ps'\n$b = 40 + 2\nWrite-Output \"$a=$b\"",
                        "cwd": "subpkg",
                    },
                )
                self.assertIn("Exit code: 0", ps_multi)
                self.assertIn("multi-ps=42", ps_multi)

                # 3. run_python passes args & respects cwd
                py_res = execute_tool(
                    "run_python",
                    {
                        "code": "import pathlib, sys\nprint(pathlib.Path('marker.txt').read_text().strip(), sys.argv[1:])",
                        "args": ["--mode", "check zero"],
                        "cwd": "subpkg",
                    },
                )
                self.assertIn("Exit code: 0", py_res)
                self.assertIn("inside-sub ['--mode', 'check zero']", py_res)

                # 4. Path traversal via cwd is rejected by workspace sandbox
                escape_res = execute_tool("run_command", {"argv": [sys.executable, "-V"], "cwd": ".."})
                self.assertIn("Permission denied", escape_res)
            finally:
                set_project_root(orig_root)


class TestIssueDExecutionStatusAndStaleGuard(unittest.TestCase):
    """Issue D & P1/P2: exit code classification and external stale-file modification detection."""

    def test_classify_exec_result_negative_probes_vs_failures(self):
        from wsa_agent.agent import _is_error_result_text, _is_verification_command, _parse_exec_status_from_result
        from wsa_agent.tools import _format_exec_output, classify_exec_result

        meta_ignore = classify_exec_result(1, "", "", command="git check-ignore __pycache__")
        self.assertEqual(meta_ignore["result_class"], "no_match")
        self.assertFalse(meta_ignore["is_error"])

        formatted = _format_exec_output(1, "", "", command="git check-ignore __pycache__")
        self.assertIn("Exit code: 1", formatted)
        self.assertIn("Status: completed (no_match", formatted)
        self.assertFalse(_is_error_result_text(formatted))
        self.assertFalse(_is_verification_command("run_command", {"command": "git check-ignore __pycache__"}, formatted))
        self.assertEqual(_parse_exec_status_from_result(formatted, False), ("completed", "no_match"))

        meta_rg = classify_exec_result(1, "", "", argv=["rg", "MISSING_TOKEN", "wsa_agent"])
        self.assertEqual(meta_rg["result_class"], "no_match")

        meta_not_found = classify_exec_result(1, "", "The term 'nope_bin' is not recognized", command="nope_bin")
        self.assertEqual(meta_not_found["result_class"], "command_not_found")
        self.assertTrue(meta_not_found["is_error"])

        meta_timeout = classify_exec_result(-1, "", "", timed_out=True, command="sleep 99")
        self.assertEqual(meta_timeout["execution_status"], "timed_out")
        self.assertEqual(meta_timeout["result_class"], "timeout")

    def test_real_subprocess_git_check_ignore_classified_as_no_match(self):
        import shutil
        import subprocess
        from wsa_agent.tools import execute_tool, get_project_root, set_project_root

        if not shutil.which("git"):
            self.skipTest("git executable not installed")
        orig_root = get_project_root()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
            set_project_root(root)
            try:
                out = execute_tool("run_command", {"argv": ["git", "check-ignore", "__not_ignored_file.py"]})
                self.assertIn("Exit code: 1", out)
                self.assertIn("Status: completed (no_match", out)
            finally:
                set_project_root(orig_root)

    def test_concurrent_external_file_modification_blocks_stale_patch(self):
        from wsa_agent.context import FileContextLedger
        from wsa_agent.tools import (
            execute_tool,
            get_project_root,
            set_active_file_ledger,
            set_project_root,
        )

        orig_root = get_project_root()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = root / "config.py"
            fp.write_text("PORT = 8000\n", encoding="utf-8")
            ledger = FileContextLedger(root)
            set_project_root(root)
            prev_ledger = set_active_file_ledger(ledger)
            try:
                execute_tool("read_file", {"path": "config.py"})
                # Simulate another external editor/process modifying the file on disk
                fp.write_text("PORT = 9000\n", encoding="utf-8")
                stale_res = execute_tool(
                    "patch_file",
                    {"path": "config.py", "old_string": "PORT = 8000", "new_string": "PORT = 8080"},
                )
                self.assertIn("Stale file context", stale_res)
                self.assertEqual(fp.read_text(encoding="utf-8"), "PORT = 9000\n")
            finally:
                set_active_file_ledger(prev_ledger)
                set_project_root(orig_root)


if __name__ == "__main__":
    unittest.main()
