from unittest.mock import patch
import sys

import pytest

from wsa_agent import tools


def test_server_bind_does_not_resolve_hostname():
    from wsa_server.server import ThreadedServer, WSAHandler

    with patch("socket.getfqdn", side_effect=AssertionError("unexpected DNS lookup")):
        server = ThreadedServer(("127.0.0.1", 0), WSAHandler)
        try:
            assert server.server_name == "127.0.0.1"
            assert server.server_port == server.server_address[1] > 0
        finally:
            server.server_close()


def test_local_visual_preview_serves_without_reverse_dns(tmp_path):
    from urllib.request import urlopen
    from wsa_agent.visual import _EphemeralStaticServer

    (tmp_path / "card.txt").write_bytes(b"preview")
    with patch("socket.getfqdn", side_effect=AssertionError("unexpected DNS lookup")):
        with _EphemeralStaticServer(tmp_path) as preview:
            with urlopen(preview.base_url + "/card.txt", timeout=3) as response:
                assert response.read() == b"preview"


def test_posix_shell_command_uses_native_shell():
    with patch.object(tools.sys, "platform", "darwin"), \
         patch.object(tools, "_run_subprocess_tree_safe", return_value=(37, "", "", False)) as run:
        result = tools._run_command(command="exit 37")
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
