from unittest.mock import patch
import sys

import pytest

from wsa_agent import tools


def test_posix_shell_command_uses_native_shell(tmp_path):
    with patch.object(tools.sys, "platform", "darwin"), \
         patch.object(tools, "_run_subprocess_tree_safe", return_value=(37, "", "", False)) as run:
        result = tools._run_command(command="exit 37", cwd=str(tmp_path))
    assert run.call_args.args[0] == ["/bin/sh", "-c", "exit 37"]
    assert "Exit code: 37" in result


def test_posix_background_uses_own_session(tmp_path):
    from unittest.mock import Mock
    proc = Mock(pid=42)
    with patch.object(tools.sys, "platform", "darwin"), \
         patch.object(tools, "_bg_dir", return_value=tmp_path), \
         patch.object(tools.subprocess, "Popen", return_value=proc) as spawn:
        entry = tools._spawn_background_entry("sleep 30", "probe", 1, cwd=tmp_path)
    try:
        assert spawn.call_args.args[0] == ["/bin/sh", "-c", "sleep 30"]
        assert spawn.call_args.kwargs["start_new_session"] is True
    finally:
        entry["stdout_fh"].close()
        entry["stderr_fh"].close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell integration")
def test_posix_shell_and_background_integration(tmp_path):
    old_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    name = "posix-integration"
    try:
        assert "Exit code: 37" in tools._run_command(command="exit 37")
        assert tools._start_background("exec sleep 30", name=name, wait_ms=100).startswith("[OK]")
        result = tools._stop_background(name)
        assert result.startswith("[OK]")
        assert "Termination reason: wsa_requested" in result
    finally:
        if name in tools._bg_processes:
            tools._stop_background(name)
            tools._bg_processes.pop(name)
        tools.set_project_root(old_root)
