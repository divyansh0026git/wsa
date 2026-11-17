"""Integration checks for tool data-flow failures."""
import json
import socket
from unittest.mock import patch

from wsa_agent import agent, tools
from wsa_agent.session import SessionState


def test_restart_rejects_occupied_port_when_listener_pid_is_unavailable():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with patch.object(tools, "_get_listening_pids_on_port", return_value=[]), \
             patch.object(tools, "_start_background_proc") as start:
            result = tools._restart_service(port, command="echo should-not-run")
        assert result.startswith("[Error]"), result
        assert "could not be identified" in result
        start.assert_not_called()


def test_failed_command_blocks_dependent_call_and_reports_failure(tmp_path):
    class Client:
        api_base = "http://127.0.0.1:8081/v1"
        api_key = ""
        calls = 0

        def stream(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"content": None, "tool_calls": [
                    {"id": "first", "function": {"name": "run_python", "arguments": json.dumps({"code": "raise RuntimeError('failed')"})}},
                    {"id": "second", "function": {"name": "write_file", "arguments": json.dumps({"path": "should_not_exist.txt", "content": "bad", "depends_on": ["first"]})}},
                ]}
            return {"content": "Finished.", "tool_calls": []}

    old_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    events = []
    state = SessionState(project_root=tmp_path, session_id="failed-command-audit", permission_mode="auto")
    try:
        agent.execute_agent_loop("Run the check, then write the file", state, Client(), quiet_ui=True,
                                 event_callback=lambda name, payload: events.append((name, payload)))
    finally:
        tools.set_project_root(old_root)
    assert not (tmp_path / "should_not_exist.txt").exists()
    results = [payload for name, payload in events if name == "tool_result"]
    first = next(payload for payload in results if payload.get("call_id") == "first")
    second = next(payload for payload in results if payload.get("call_id") == "second")
    assert first["ok"] is False and first["result_class"] == "non_zero_exit"
    assert second["ok"] is False and second["result_class"] == "dependency_blocked"


def test_failed_subagent_blocks_dependent_call(tmp_path):
    class Client:
        api_base = "http://127.0.0.1:8081/v1"
        api_key = ""
        calls = 0
        def stream(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"content": None, "tool_calls": [
                    {"id": "child", "function": {"name": "spawn_subagent", "arguments": json.dumps({"role": "explorer", "task": "inspect"})}},
                    {"id": "dependent", "function": {"name": "write_file", "arguments": json.dumps({"path": "must-not-exist.txt", "content": "bad", "depends_on": ["child"]})}},
                ]}
            return {"content": "Finished.", "tool_calls": []}
    old_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    events = []
    original = agent.execute_tool
    def execute(name, args):
        if name == "spawn_subagent":
            return json.dumps({"ok": False, "aborted_reason": "upstream error"})
        return original(name, args)
    try:
        with patch.object(agent, "execute_tool", side_effect=execute):
            agent.execute_agent_loop("inspect then write", SessionState(project_root=tmp_path), Client(),
                                     quiet_ui=True, event_callback=lambda name, payload: events.append((name, payload)))
    finally:
        tools.set_project_root(old_root)
    assert not (tmp_path / "must-not-exist.txt").exists()
    results = {payload["call_id"]: payload for name, payload in events if name == "tool_result"}
    assert results["child"]["ok"] is False
    assert results["child"]["result_class"] == "subagent_failed"
    assert results["dependent"]["result_class"] == "dependency_blocked"


def test_custom_tool_cannot_replace_any_builtin_or_remove_its_schema(tmp_path):
    old_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    try:
        original = {item["function"]["name"]: item for item in tools.TOOL_DEFINITIONS}
        for name in tools._BUILTIN_TOOL_NAMES:
            result = tools.execute_tool("create_custom_tool", {"name": name, "description": "collision", "python_code": "def run():\n return 'x'\n"})
            assert result.startswith("[Error]"), (name, result)
        saved = tmp_path / ".wsa_custom_tools.json"
        saved.write_text(json.dumps({"rollback": {"description": "old collision", "code": "def run(): return 1"}}))
        assert "rollback" not in tools.load_custom_tools()
        assert tools.execute_tool("remove_custom_tool", {"name": "rollback"}).startswith("[Error]")
        assert next(item for item in tools.TOOL_DEFINITIONS if item["function"]["name"] == "rollback") == original["rollback"]
    finally:
        tools.set_project_root(old_root)
