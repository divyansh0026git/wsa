"""Regression checks for issues found while reviewing the reliability changes."""
from pathlib import Path
from unittest.mock import Mock, patch

from wsa_agent import tools


def test_startup_does_not_kill_unknown_port_owner():
    from wsa_agent.__main__ import _ServerManager
    manager = _ServerManager("http://127.0.0.1:8081/v1")
    with patch.object(manager, "_is_server_up", return_value=True), \
         patch.object(manager, "_check_server_authenticated", return_value=(False, None)), \
         patch("wsa_agent.__main__.subprocess.run") as run, \
         patch("wsa_agent.__main__.subprocess.Popen") as spawn:
        assert manager.ensure_running() is False
    run.assert_not_called()
    spawn.assert_not_called()


def test_startup_reuses_cookie_free_wsa_backend():
    from wsa_agent.__main__ import _ServerManager
    manager = _ServerManager("http://127.0.0.1:8081/v1")
    with patch.object(manager, "_is_server_up", return_value=True), \
         patch.object(manager, "_check_server_authenticated", return_value=(False, 12345)), \
         patch("wsa_agent.__main__.subprocess.run") as run:
        assert manager.ensure_running() is True
    run.assert_not_called()


def test_service_restart_refuses_surviving_listener():
    with patch.object(tools, "_get_listening_pids_on_port", return_value=[987654321]), \
         patch.object(tools, "_kill_process_tree"), \
         patch.object(tools, "_is_tcp_port_open", return_value=True), \
         patch.object(tools.time, "monotonic", side_effect=range(100)), \
         patch.object(tools.time, "sleep"), \
         patch.object(tools, "_start_background_proc") as start:
        result = tools._restart_service(54321, command="echo replacement")
    assert result.startswith("[Error]"), result
    start.assert_not_called()


def test_self_edit_rollback_preserves_exact_bytes(tmp_path):
    target = tmp_path / "source.py"
    original = b"# original\r\nVALUE = 1\n"
    target.write_bytes(b"broken")
    with patch.object(tools, "_is_self_code_file", return_value="py"), \
         patch.object(tools.subprocess, "run", return_value=Mock(returncode=1, stderr="bad import")):
        result = tools._verify_self_edit_on_disk(target, True, original.decode(), original)
    assert result.startswith("[Self-Preservation Rollback]"), result
    assert target.read_bytes() == original


def test_self_edit_failed_restoration_is_not_reported_as_reverted(tmp_path):
    target = tmp_path / "source.py"
    target.write_text("broken")
    with patch.object(tools, "_is_self_code_file", return_value="py"), \
         patch.object(tools.subprocess, "run", return_value=Mock(returncode=1, stderr="bad import")), \
         patch.object(tools, "_write_bytes_atomic", side_effect=PermissionError("locked")):
        result = tools._verify_self_edit_on_disk(target, True, "original", b"original")
    assert result.startswith("[Error]"), result
    assert "FAILED" in result
    assert target.read_text() == "broken"


def test_js_verifier_failure_rolls_back(tmp_path):
    target = tmp_path / "source.js"
    target.write_text("broken")
    with patch.object(tools, "_is_self_code_file", return_value="js"), \
         patch.object(tools.shutil, "which", return_value="node"), \
         patch.object(tools.subprocess, "run", side_effect=OSError("verifier unavailable")):
        result = tools._verify_self_edit_on_disk(target, True, "original", b"original")
    assert result.startswith("[Self-Preservation Rollback]"), result
    assert target.read_bytes() == b"original"
