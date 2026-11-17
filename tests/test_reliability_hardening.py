"""Reproducers for the November reliability audit; no live upstream required."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from wsa_agent import agent, tools
from wsa_agent.session import SessionState
from wsa_server.tools import parse_tool_calls_with_meta


@pytest.fixture
def workspace(tmp_path):
    previous = tools.get_project_root()
    tools.set_project_root(tmp_path)
    try:
        yield tmp_path
    finally:
        tools.set_project_root(previous)


@pytest.mark.parametrize("prefix", [".__wsa_stress__", "./.__wsa_stress__", "unicodé space/नमस्ते"])
def test_literal_paths_roundtrip_across_tools(workspace, prefix):
    name = prefix + "/probe.txt"
    assert "[OK]" in tools.execute_tool("write_file", {"path": name, "content": "original\n"})
    payload = "```tool_call\n" + json.dumps({"name": "read_file", "arguments": {"path": name}}) + "\n```"
    _, calls, _, dropped = parse_tool_calls_with_meta(payload, allowed_names={"read_file"})
    assert not dropped
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["path"] == name
    assert "original" in tools.execute_tool("read_file", args)
    assert "File:" in tools.execute_tool("get_file_info", {"path": name})
    assert "[OK]" in tools.execute_tool("patch_file", {"path": name, "old_string": "original", "new_string": "updated"})
    assert "updated" in tools.execute_tool("search_files", {"pattern": "updated", "path": prefix})
    assert "probe.txt" in tools.execute_tool("glob_files", {"pattern": prefix + "/*.txt"})
    assert "[OK]" in tools.execute_tool("rollback", {})
    assert (workspace / name).read_text() == "original\n"


def test_missing_directory_checkpoint_removes_nested_tree(workspace):
    assert "[OK]" in tools.execute_tool("rollback", {"action": "create", "paths": ["created"]})
    (workspace / "created/nested").mkdir(parents=True)
    (workspace / "created/nested/x.txt").write_text("created")
    result = tools.execute_tool("rollback", {})
    assert result.startswith("[OK]"), result
    assert not (workspace / "created").exists()


def test_directory_snapshot_restores_bytes_empty_dirs_and_removes_additions(workspace):
    root = workspace / "tree"
    (root / "empty").mkdir(parents=True)
    original = b"\xff\x00\xfe\r\ntext\n"
    (root / "binary.bin").write_bytes(original)
    (root / "keep.txt").write_text("keep")
    assert "[OK]" in tools.execute_tool("rollback", {"action": "create", "paths": ["tree"]})
    (root / "binary.bin").write_bytes(b"changed")
    (root / "keep.txt").unlink()
    (root / "empty").rmdir()
    (root / "extra/deep").mkdir(parents=True)
    (root / "extra/deep/new.txt").write_text("new")
    result = tools.execute_tool("rollback", {})
    assert result.startswith("[OK]"), result
    assert (root / "binary.bin").read_bytes() == original
    assert (root / "keep.txt").read_text() == "keep"
    assert (root / "empty").is_dir()
    assert not (root / "extra").exists()


def test_file_rollback_is_byte_exact(workspace):
    target = workspace / "binary.bin"
    original = b"\xff\x00\x80\r\nmixed\nend\r"
    target.write_bytes(original)
    tools.execute_tool("rollback", {"action": "create", "paths": ["binary.bin"]})
    target.write_bytes(b"replaced")
    assert "[OK]" in tools.execute_tool("rollback", {})
    assert target.read_bytes() == original


def test_partial_rollback_reports_path_retains_checkpoint_and_can_retry(workspace):
    tools.execute_tool("rollback", {"action": "create", "paths": ["locked.txt", "ok.txt"]})
    for name in ("locked.txt", "ok.txt"):
        (workspace / name).write_text("new")
    real_unlink = Path.unlink

    def locked(path, *args, **kwargs):
        if path.name == "locked.txt":
            raise PermissionError(13, "simulated sharing violation", str(path))
        return real_unlink(path, *args, **kwargs)

    with patch.object(Path, "unlink", locked), patch.object(tools.time, "sleep"):
        result = tools.execute_tool("rollback", {})
    assert result.startswith("[Error] Partial rollback"), result
    assert "locked.txt" in result
    assert tools.get_checkpoints()
    assert not (workspace / "ok.txt").exists()
    assert "[OK]" in tools.execute_tool("rollback", {})
    assert not (workspace / "locked.txt").exists()
    assert not tools.get_checkpoints()


def test_transient_access_denied_is_retried(workspace):
    tools.execute_tool("rollback", {"action": "create", "paths": ["new.txt"]})
    (workspace / "new.txt").write_text("new")
    real_unlink = Path.unlink
    attempts = []

    def transient(path, *args, **kwargs):
        attempts.append(path)
        if len(attempts) < 3:
            raise PermissionError(13, "busy", str(path))
        return real_unlink(path, *args, **kwargs)

    with patch.object(Path, "unlink", transient), patch.object(tools.time, "sleep"):
        result = tools.execute_tool("rollback", {})
    assert result.startswith("[OK]"), result
    assert len(attempts) == 3


def test_failed_dependency_and_unknown_dependency_fail_closed():
    inv = {"name": "run_python", "args": {"code": "print('side effect')"}, "explicit_depends_on": ["missing"]}
    assert agent._find_blocked_prerequisite(inv, [], [])


def test_background_stop_does_not_report_success_when_process_survives():
    proc = Mock()
    proc.poll.return_value = None
    proc.wait.side_effect = subprocess.TimeoutExpired("worker", 1)
    entry = {"name": "stuck", "pid": 987654321, "proc": proc, "exit_code": None,
             "started_at": 0, "stdout_path": Path("missing.stdout"), "stderr_path": Path("missing.stderr")}
    with patch.dict(tools._bg_processes, {"stuck": entry}, clear=True), patch.object(tools, "_kill_process_tree"):
        result = tools._stop_background("stuck")
    assert result.startswith("[Error]"), result


def test_background_stop_reports_surviving_job_children():
    proc = Mock()
    proc.poll.return_value = 0
    job = Mock()
    job.active_count.return_value = 1
    entry = {"name": "stuck-child", "pid": 987654321, "proc": proc, "job": job,
             "exit_code": None, "started_at": 0, "stdout_path": Path("missing.stdout"),
             "stderr_path": Path("missing.stderr")}
    with patch.dict(tools._bg_processes, {"stuck-child": entry}, clear=True), \
         patch.object(tools.time, "sleep"), patch.object(tools.time, "monotonic", side_effect=range(100)):
        result = tools._stop_background("stuck-child")
    assert result.startswith("[Error]"), result
    assert entry["stop_method"] == "failed"
    job.terminate.assert_called_once()


def test_background_graceful_stop_precedes_force():
    proc = Mock()
    proc.poll.side_effect = [None, 0]
    proc.wait.return_value = 0
    entry = {"proc": proc, "pid": 987654321, "exit_code": None}
    with patch.object(tools, "_kill_process_tree") as kill_tree:
        tools._stop_bg_entry_locked(entry)
    proc.send_signal.assert_called_once()
    kill_tree.assert_not_called()
    assert entry["stop_method"] == "graceful"


def test_noninteractive_strict_mode_requires_approval(workspace):
    class Client:
        api_base = "http://127.0.0.1:8081/v1"
        api_key = ""
        def __init__(self):
            self.calls = 0
        def stream(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"content": None, "tool_calls": [{"id": "write", "function": {
                    "name": "write_file", "arguments": json.dumps({"path": "denied.txt", "content": "no"})}}]}
            return {"content": "Done.", "tool_calls": []}

    state = SessionState(project_root=workspace, session_id="strict-audit", permission_mode="strict", auto_approve=False)
    agent.execute_agent_loop("test strict mode", state, Client(), quiet_ui=True)
    assert not (workspace / "denied.txt").exists()


@pytest.mark.parametrize("result,expected", [
    ({"ok": True}, None),
    ({"ok": False, "aborted_reason": "upstream error"}, 1),
    ({"ok": False, "aborted_reason": "interrupted"}, 130),
])
def test_oneshot_exit_status_reflects_turn_result(result, expected):
    from wsa_agent import __main__ as main
    with patch.object(main, "run_turn", return_value=result), patch.object(main.R, "print_user_message"):
        if expected is None:
            main.run_oneshot("test", Mock(), Mock())
        else:
            with pytest.raises(SystemExit) as exc:
                main.run_oneshot("test", Mock(), Mock())
            assert exc.value.code == expected


def test_run_turn_propagates_single_agent_result(workspace):
    state = SessionState(project_root=workspace, team_mode="off")
    result = {"ok": False, "aborted_reason": "upstream error"}
    with patch.object(agent, "execute_agent_loop", return_value=result):
        assert agent.run_turn("test", state, Mock()) is result


def test_run_turn_propagates_orchestrated_result(workspace):
    from wsa_agent import subagents
    state = SessionState(project_root=workspace, team_mode="on")
    result = {"ok": False, "aborted_reason": "task failed"}
    with patch.object(agent, "classify_task_complexity", return_value={"mode": "orchestrate"}), \
         patch.object(subagents, "run_orchestrated_turn", return_value=result):
        assert agent.run_turn("do a complex test", state, Mock()) is result


def test_concurrent_independent_writes_and_checkpoint_ids(workspace):
    def write(index):
        return tools.execute_tool("write_file", {"path": f"f{index}.txt", "content": str(index)})
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(write, range(20)))
    assert all(result.startswith("[OK]") for result in results)
    checkpoints = tools.get_checkpoints()
    assert len(checkpoints) == len({cp["id"] for cp in checkpoints}) == 20
    assert "[OK]" in tools.execute_tool("rollback", {"steps": 20})
    assert not list(workspace.glob("f*.txt"))


@pytest.mark.parametrize("deps", [["failed"], ["missing"], ["dependent"]])
def test_orchestrator_never_runs_unsatisfied_dependencies(workspace, deps):
    from wsa_agent import subagents
    tasks = [
        {"id": "failed", "title": "prerequisite", "owned_files": ["a.txt"], "depends_on": []},
        {"id": "dependent", "title": "must not run", "owned_files": ["b.txt"], "depends_on": deps},
    ]
    dispatched = []

    def run_role(**kwargs):
        role = kwargs["role"]
        if role == "architect":
            return {"ok": True, "final_text": "```json\n" + json.dumps(tasks) + "\n```"}
        if role == "coder":
            dispatched.append(kwargs.get("label"))
            return {"ok": False, "final_text": "failed prerequisite"}
        return {"ok": True, "final_text": '{"verdict": "pass", "checks": [], "failures": []}'}

    state = SessionState(project_root=workspace, session_id="dependency-audit")
    with patch.object(subagents, "_run_role_agent", side_effect=run_role), patch.object(subagents, "_discover_live_relays", return_value=[]):
        result = subagents.run_orchestrated_turn("audit failure recovery", state, Mock())
    assert dispatched == ["coder:failed"]
    assert not result["ok"]
    assert result["tasks"][1]["status"] == "blocked"


def test_atomic_replace_failure_leaves_original_and_no_checkpoint(workspace):
    target = workspace / "original.txt"
    target.write_bytes(b"original\r\n")
    with patch.object(tools.os, "replace", side_effect=PermissionError("simulated lock")), patch.object(tools.time, "sleep"):
        result = tools.execute_tool("write_file", {"path": "original.txt", "content": "replacement"})
    assert result.startswith("[Permission denied]")
    assert target.read_bytes() == b"original\r\n"
    assert not tools.get_checkpoints()
    assert not list(workspace.glob(".wsa-write-*"))


def test_atomic_staging_permission_denied_fails_promptly(workspace):
    target = workspace / "original.txt"
    target.write_bytes(b"original")
    with patch.object(tools.os, "open", side_effect=PermissionError("denied")) as open_file:
        result = tools.execute_tool("write_file", {"path": "original.txt", "content": "replacement"})
    assert result.startswith("[Permission denied]"), result
    assert open_file.call_count == 1
    assert target.read_bytes() == b"original"
    assert not tools.get_checkpoints()


@pytest.mark.parametrize("name,args", [
    ("read_file", {}), ("read_file", {"path": 5}), ("missing_alias", {}),
    ("patch_file", {"path": "a.txt", "old_string": "old"}),
    ("run_command", {"argv": ["python", 7]}), ("read_file", "{broken"),
    ("read_file", []),
])
def test_malformed_calls_are_rejected(name, args):
    ok, _, error = tools.validate_and_normalize_tool_call(name, args)
    assert not ok
    assert error.startswith("[Validation Error]")


def test_dependency_paths_use_canonical_resolver(workspace):
    failed = {"call_id": "write", "seq": 0, "name": "write_file", "status": "error",
              "target_paths": {"dir/../probe.txt"}, "reason": "failed write"}
    inv = {"name": "read_file", "args": {"path": str(workspace / "probe.txt")}}
    assert agent._find_blocked_prerequisite(inv, [], [failed])


def test_invalid_dependency_metadata_is_rejected():
    ok, _, _ = tools.validate_and_normalize_tool_call("list_dir", {"depends_on": {"id": "missing"}})
    assert not ok


def test_no_argument_tool_rejects_unknown_parameters():
    ok, _, _ = tools.validate_and_normalize_tool_call("browser_tabs", {"unexpected": "bad"})
    assert not ok


def test_concurrent_batches_keep_separate_checkpoints(workspace):
    barrier = threading.Barrier(2)
    def worker(index):
        tools.begin_edit_checkpoint(f"worker-{index}")
        barrier.wait(timeout=5)
        result = tools.execute_tool("write_file", {"path": f"worker{index}.txt", "content": str(index)})
        barrier.wait(timeout=5)
        return result, tools.commit_edit_checkpoint()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))
    assert all(result.startswith("[OK]") and cp for result, cp in results)
    assert len({cp for _, cp in results}) == 2
    assert all(len(cp["files"]) == 1 for cp in tools.get_checkpoints())


def test_rollback_in_active_batch_reverts_partial_execution(workspace):
    tools.begin_edit_checkpoint("partial workflow")
    assert "[OK]" in tools.execute_tool("write_file", {"path": "step1.txt", "content": "written"})
    assert "[Error]" in tools.execute_tool("patch_file", {"path": "missing.txt", "old_string": "x", "new_string": "y"})
    result = tools.execute_tool("rollback", {})
    assert result.startswith("[OK]"), result
    assert not (workspace / "step1.txt").exists()
    assert tools.commit_edit_checkpoint() is None


def test_interleaved_stream_fragments_preserve_twenty_call_identities():
    from wsa_agent.client import _accumulate_tool_deltas
    acc = {}
    payloads = [json.dumps({"path": f".__wsa_stress__/नमस्ते {i}.txt"}, ensure_ascii=False) for i in range(20)]
    for index in reversed(range(20)):
        _accumulate_tool_deltas(acc, [{"index": index, "id": f"id-{index}", "function": {"name": "read_", "arguments": ""}}])
    for offset in range(max(map(len, payloads))):
        for index in range(20):
            _accumulate_tool_deltas(acc, [{"index": index, "function": {
                "name": "file" if offset == 0 else None,
                "arguments": payloads[index][offset:offset + 1],
            }}])
    for index, payload in enumerate(payloads):
        assert acc[index]["id"] == f"id-{index}"
        assert acc[index]["function"] == {"name": "read_file", "arguments": payload}


@pytest.mark.parametrize("delta", [
    [{"index": -1}], [{"index": True}], [{"index": "0"}],
    [{"index": []}], [{"index": 0, "function": {"arguments": {"path": "x"}}}],
    [{"id": "a"}, {"id": "b"}], [None], {"index": 0},
])
def test_malformed_stream_deltas_fail_closed(delta):
    from wsa_agent.client import _accumulate_tool_deltas
    with pytest.raises(ValueError):
        _accumulate_tool_deltas({}, delta)


@pytest.mark.parametrize("index,cid", [(0, "changed-id"), (1, "original-id")])
def test_stream_id_collisions_are_rejected(index, cid):
    from wsa_agent.client import _accumulate_tool_deltas
    acc = {}
    _accumulate_tool_deltas(acc, [{"index": 0, "id": "original-id", "function": {"name": "read_file"}}])
    with pytest.raises(ValueError):
        _accumulate_tool_deltas(acc, [{"index": index, "id": cid}])


def test_background_forced_stop_and_already_exited_capture_status():
    proc = Mock()
    proc.poll.side_effect = [None, 1]
    proc.wait.side_effect = [subprocess.TimeoutExpired("worker", 2), 1]
    entry = {"proc": proc, "pid": 987654321, "exit_code": None}
    with patch.object(tools, "_kill_process_tree") as kill_tree:
        tools._stop_bg_entry_locked(entry)
    kill_tree.assert_called_once_with(987654321)
    proc.kill.assert_called_once()
    assert entry["exit_code"] == 1
    assert entry["stop_method"] == "forced"
    proc.reset_mock()
    proc.poll.side_effect = None
    proc.poll.return_value = 1
    tools._stop_bg_entry_locked(entry)
    proc.send_signal.assert_not_called()
    proc.kill.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell background integration")
def test_real_background_natural_exit_preserves_logs_and_code(workspace):
    result = tools.execute_tool("start_background", {
        "command": "Write-Output 'STDOUT_DONE'; [Console]::Error.WriteLine('STDERR_DONE'); exit 7",
        "name": "audit-normal-exit", "wait_ms": 3000,
    })
    assert "exit code 7" in result, result
    assert "STDOUT_DONE" in result and "STDERR_DONE" in result
    stopped = tools.execute_tool("stop_background", {"name": "audit-normal-exit"})
    assert "had already exited" in stopped
    assert "exit code 7" in stopped


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process tree integration")
@pytest.mark.parametrize("parent_exits_early", [False, True])
def test_real_background_stop_terminates_child_process(workspace, parent_exits_early):
    import ctypes
    from ctypes import wintypes

    child = workspace / "child.py"
    child.write_text(
        "import os, signal, sys, time\n"
        "signal.signal(signal.SIGBREAK, lambda *_: sys.exit(0))\n"
        "print('CHILD_PID=' + str(os.getpid()), flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    executable = sys.executable.replace("'", "''")
    child_path = str(child).replace("'", "''")
    command = (f"Start-Process -FilePath '{executable}' -ArgumentList '\"{child_path}\"' -NoNewWindow"
               if parent_exits_early else f"& '{executable}' '{child_path}'")
    started, entry, _ = tools._start_background_proc(command, name="audit-child", wait_ms=0)
    assert started
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = None
    child_pid = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            output = tools._tail_log_file(entry["stdout_path"])
            if "CHILD_PID=" in output:
                child_pid = int(output.split("CHILD_PID=", 1)[1].splitlines()[0])
                break
            time.sleep(0.05)
        assert child_pid, "child did not start"
        if parent_exits_early:
            entry["proc"].wait(timeout=5)
            assert tools._refresh_bg_entry_locked(entry) is None
        handle = kernel.OpenProcess(0x00100000, False, child_pid)  # SYNCHRONIZE
        assert handle, ctypes.get_last_error()
        result = tools._stop_background("audit-child")
        assert result.startswith("[OK]"), result
        assert kernel.WaitForSingleObject(handle, 5000) == 0, "child survived background stop"
    finally:
        if handle:
            if kernel.WaitForSingleObject(handle, 0) != 0 and child_pid:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(child_pid)], capture_output=True, timeout=5)
            kernel.CloseHandle(handle)
        tools.cleanup_background_processes()


def test_large_output_truncation_preserves_ends_and_budget():
    raw = "BEGIN\n" + "x" * 50_000 + "\nEND"
    result = tools.truncate_text_with_line_range(raw, max_chars=2500)
    assert len(result) <= 2500
    assert "BEGIN" in result and "END" in result and "omitted" in result


def test_duplicate_ids_do_not_steal_later_real_ids():
    calls = [{"id": cid, "function": {"name": "list_dir", "arguments": "{}"}} for cid in ("same", "same", "same_1")]
    normalized = agent._ensure_unique_tool_call_ids(calls)
    assert normalized[2]["id"] == "same_1"
    assert len({tc["id"] for tc in normalized}) == 3
    assert normalized[0]["_wsa_ambiguous_id"] == "same"
    inv = {"name": "list_dir", "args": {}, "explicit_depends_on": ["same"]}
    prior = [{"call_id": "same", "ambiguous_id": "same", "name": "list_dir", "status": "ok"}]
    assert "duplicated" in agent._find_blocked_prerequisite(inv, [], prior)


def test_deduplication_keeps_side_effects_and_reads_after_mutations():
    read = {"id": "read", "function": {"name": "read_file", "arguments": '{"path":"x.txt"}'}}
    write = {"id": "write", "function": {"name": "write_file", "arguments": '{"path":"x.txt","content":"new"}'}}
    command = {"id": "cmd", "function": {"name": "run_python", "arguments": '{"code":"print(1)"}'}}
    calls = [read, read, write, read, command, command]
    result, count = agent._deduplicate_tool_calls(calls)
    assert count == 1
    assert result == [read, write, read, command, command]


def test_deduplication_preserves_referenced_call_ids():
    calls = [{"id": cid, "function": {"name": "list_dir", "arguments": "{}"}} for cid in ("first", "second")]
    calls.append({"id": "last", "function": {"name": "list_dir", "arguments": '{"depends_on":["second"]}'}})
    result, count = agent._deduplicate_tool_calls(calls)
    assert count == 0 and len(result) == 3


def test_concurrent_background_names_spawn_once(workspace):
    proc = Mock()
    proc.pid = 987654321
    proc.poll.return_value = None
    with patch.object(tools.subprocess, "Popen", return_value=proc) as popen:
        try:
            with ThreadPoolExecutor(max_workers=10) as pool:
                results = list(pool.map(lambda _: tools._start_background_proc("echo test", name="unique-worker", wait_ms=0), range(10)))
            assert sum(started for started, _, _ in results) == 1
            assert popen.call_count == 1
        finally:
            proc.poll.return_value = 0
            tools.cleanup_background_processes()


@pytest.mark.parametrize("relative", ["wsa_agent/tools.py", "folder/../wsa_agent/tools.py", "./wsa_server/server.py"])
def test_core_path_approval_uses_resolved_paths(workspace, relative):
    assert tools.is_low_level_change("write_file", {"path": str(workspace / relative)})[0]


def test_worker_edit_policy_does_not_leak_to_other_worker(workspace):
    barrier = threading.Barrier(2)
    (workspace / "strict.txt").write_text("old\n" + "untouched\n" * 20)
    (workspace / "normal.txt").write_text("old\n" + "untouched\n" * 20)
    def worker(strict):
        previous = tools.set_strict_read_before_edit(strict)
        try:
            barrier.wait(timeout=5)
            name = "strict.txt" if strict else "normal.txt"
            return tools.execute_tool("patch_file", {"path": name, "old_string": "old", "new_string": "new"})
        finally:
            tools.set_strict_read_before_edit(previous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        strict_result, normal_result = list(pool.map(worker, [True, False]))
    assert not strict_result.startswith("[OK]")
    assert normal_result.startswith("[OK]")


def test_rollback_rejects_redirected_checkpoint_target(workspace):
    tools.execute_tool("rollback", {"action": "create", "paths": ["new"]})
    unrelated = workspace / "unrelated"
    unrelated.mkdir()
    (unrelated / "important.txt").write_text("preserve")
    resolve = tools._resolve
    def redirected(path):
        return unrelated if path == "new" else resolve(path)
    with patch.object(tools, "_resolve", side_effect=redirected):
        result = tools.execute_tool("rollback", {})
    assert result.startswith("[Error] Partial rollback"), result
    assert "redirected" in result
    assert (unrelated / "important.txt").read_text() == "preserve"


@pytest.mark.parametrize("initial_directory", [True, False])
def test_rollback_restores_path_type_changes(workspace, initial_directory):
    target = workspace / "changed"
    if initial_directory:
        target.mkdir()
        (target / "original.txt").write_text("original")
    else:
        target.write_bytes(b"\xfforiginal")
    tools.execute_tool("rollback", {"action": "create", "paths": ["changed"]})
    if initial_directory:
        (target / "original.txt").unlink()
        target.rmdir()
        target.write_text("now a file")
    else:
        target.unlink()
        target.mkdir()
        (target / "extra.txt").write_text("extra")
    result = tools.execute_tool("rollback", {})
    assert result.startswith("[OK]"), result
    if initial_directory:
        assert (target / "original.txt").read_text() == "original"
    else:
        assert target.read_bytes() == b"\xfforiginal"


def test_delete_without_readable_backup_does_not_destroy_file(workspace):
    target = workspace / "unreadable.txt"
    target.write_text("preserve")
    with patch.object(Path, "read_bytes", side_effect=PermissionError("unreadable")):
        result = tools.execute_tool("delete_file", {"path": "unreadable.txt"})
    assert result.startswith("[Error]"), result
    assert target.read_text() == "preserve"


def test_rollback_stops_before_older_checkpoint_on_partial_failure(workspace):
    target = workspace / "existing.txt"
    target.write_bytes(b"old")
    tools.execute_tool("rollback", {"action": "create", "paths": ["existing.txt"]})
    target.write_bytes(b"middle")
    tools.execute_tool("rollback", {"action": "create", "paths": ["existing.txt"]})
    target.write_bytes(b"new")
    with patch.object(tools, "_write_bytes_atomic", side_effect=PermissionError("locked")):
        result = tools.execute_tool("rollback", {"steps": 2})
    assert result.startswith("[Error] Partial rollback")
    assert len(tools.get_checkpoints()) == 2
    assert target.read_bytes() == b"new"
    assert "[OK]" in tools.execute_tool("rollback", {"steps": 2})
    assert target.read_bytes() == b"old"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory sharing semantics")
def test_real_windows_directory_lock_reports_partial_then_recovers(workspace):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    tools.execute_tool("rollback", {"action": "create", "paths": ["locked-dir"]})
    target = workspace / "locked-dir"
    target.mkdir()
    handle = kernel.CreateFileW(str(target), 0x80000000, 0, None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = tools.execute_tool("rollback", {})
        assert result.startswith("[Error] Partial rollback"), result
        assert str(target) in result
        assert target.is_dir()
        assert tools.get_checkpoints()
    finally:
        kernel.CloseHandle(handle)
    assert "[OK]" in tools.execute_tool("rollback", {})
    assert not target.exists()


def test_implicit_parent_directories_are_removed_after_file_rollback(workspace):
    assert "[OK]" in tools.execute_tool("write_file", {"path": "new/nested/file.txt", "content": "test"})
    assert "[OK]" in tools.execute_tool("rollback", {})
    assert not (workspace / "new").exists()


def test_implicit_parent_cleanup_preserves_unrelated_files(workspace):
    tools.execute_tool("write_file", {"path": "new/file.txt", "content": "test"})
    (workspace / "new/other.txt").write_text("untracked work")
    result = tools.execute_tool("rollback", {})
    assert result.startswith("[Error] Partial rollback"), result
    assert (workspace / "new/other.txt").read_text() == "untracked work"


def test_rejected_write_does_not_create_directories(workspace):
    result = tools.execute_tool("write_file", {"path": "new/invalid.py", "content": "def broken(:"})
    assert "[Syntax Error" in result
    assert not (workspace / "new").exists()
