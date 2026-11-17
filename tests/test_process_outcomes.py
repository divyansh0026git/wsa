import sys
from pathlib import Path
from unittest.mock import Mock, patch
import pytest
from wsa_agent import tools, permissions
from wsa_agent.session import SessionState

@pytest.fixture(autouse=True)
def isolated_root(tmp_path):
    old_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    yield
    tools.set_project_root(old_root)

@pytest.mark.parametrize('code', [0, 1, 2, 37, 127])
@pytest.mark.parametrize('multiline', [False, True])
def test_native_shell_exit_matches_argv(code, multiline):
    command = "& '" + sys.executable.replace("'", "''") + "' -c 'import sys; sys.exit(" + str(code) + ")'"
    if multiline:
        command = "Write-Output 'before'\n" + command
    for result in (tools._run_command(command=command), tools._run_command(argv=[sys.executable, '-c', f'import sys; sys.exit({code})'])):
        assert f'Exit code: {code}\n' in result, result

@pytest.mark.parametrize('code', [0, 1, 2, 37, 127])
def test_cmd_shell_string_preserves_exit_code(code):
    result = tools._run_command(command=f'cmd /d /c "exit /b {code}"')
    assert f'Exit code: {code}\n' in result, result

@pytest.mark.parametrize('command,code', [
    ('exit 37', 37), ('Write-Output ok', 0), ('Write-Error bad', 1),
    ('cmd /c exit 37; Write-Output recovered', 0),
    ('cmd /c exit 37; Write-Error bad', 1),
    ('throw "bad"', 1),
])
def test_shell_outcomes(command, code):
    assert f'Exit code: {code}\n' in tools._run_command(command=command)

@pytest.mark.parametrize('requested', [False, True])
def test_background_preserves_exit_and_reason(tmp_path, requested):
    proc = Mock()
    proc.poll.side_effect = [None, 1] if requested else [1]
    proc.wait.return_value = 1
    entry = dict(proc=proc, pid=99999999, exit_code=None, name='test', started_at=0,
                 stdout_path=tmp_path/'out', stderr_path=tmp_path/'err')
    if requested:
        tools._stop_bg_entry_locked(entry)
    else:
        tools._refresh_bg_entry_locked(entry)
    assert entry['exit_code'] == 1
    assert entry['termination_reason'] == ('wsa_requested' if requested else 'process_failure')
    report = '\n'.join(tools._format_bg_report(entry))
    assert ('stopped by WSA' in report) == requested
    assert 'exit code 1' in report

@pytest.mark.parametrize('flags,mode', [([], 'smart'), (['--full-permissions'], 'auto'), (['--permissions', 'strict'], 'strict')])
def test_cli_permission_policy(tmp_path, flags, mode):
    from wsa_agent import __main__ as main
    with patch.object(sys, 'argv', ['wsa', '--no-server', '--project', str(tmp_path), *flags]), patch.object(main, 'WSAClient'), patch.object(main, 'run_repl') as run:
        main.main()
    state = run.call_args.args[0]
    assert state.permission_mode == mode
    assert bool(permissions.approval_reason('run_python', {}, state)) == (mode != 'auto')


def test_real_background_exit_and_requested_shutdown(tmp_path):
    import time
    old_root = tools.get_project_root()
    tools.set_project_root(tmp_path)
    names = ['outcome-exit', 'outcome-stop']
    try:
        exe = "& '" + sys.executable.replace("'", "''") + "'"
        tools._start_background(exe + " -c 'import sys; sys.exit(37)'", name=names[0], wait_ms=1000)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            report = tools._list_background(names[0])
            if 'exit code 37' in report:
                break
            time.sleep(0.1)
        assert 'exit code 37' in report, report
        assert 'Termination reason: process_failure' in report
        tools._start_background(exe + " -c 'import time; time.sleep(60)'", name=names[1], wait_ms=500)
        result = tools._stop_background(names[1])
        assert result.startswith('[OK]'), result
        assert 'stopped by WSA' in result
        assert 'Termination reason: wsa_requested' in result
        assert tools._bg_processes[names[1]]['proc'].poll() is not None
    finally:
        for name in names:
            if name in tools._bg_processes:
                tools._stop_background(name)
                tools._bg_processes.pop(name)
        tools.set_project_root(old_root)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows CTRL_BREAK test')
def test_real_background_child_handles_break_and_exits_zero(tmp_path):
    import time
    script = tmp_path / 'signal_child.py'
    script.write_text("import signal, sys, time\n"
                      "def stop(sig, frame):\n"
                      "    print('HANDLED_BREAK_EXIT_0', flush=True)\n"
                      "    sys.exit(0)\n"
                      "signal.signal(signal.SIGBREAK, stop)\n"
                      "print('READY', flush=True)\n"
                      "while True: time.sleep(0.1)\n", encoding='utf-8')
    name = 'signal-handler-test'
    try:
        command = f"& '{sys.executable}' '{script}'"
        tools._start_background(command, name=name, wait_ms=500)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if 'READY' in tools._list_background(name):
                break
            time.sleep(0.1)
        else:
            pytest.fail('Child did not install its signal handler')
        result = tools._stop_background(name)
        assert result.startswith('[OK]'), result
        assert 'HANDLED_BREAK_EXIT_0' in result, result
        assert 'Termination reason: wsa_requested' in result
        assert 'Exit code source: tracked PowerShell process' in result
        assert tools._bg_processes[name]['proc'].poll() is not None
    finally:
        if name in tools._bg_processes:
            tools._stop_background(name)
            tools._bg_processes.pop(name)
