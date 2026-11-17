"""Exercise the actual interactive terminal and managed server on POSIX."""
import os
import sys
import time
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="requires a POSIX pseudo-terminal")
def test_interactive_terminal_starts_server_and_exits(tmp_path):
    import pty
    import select

    master, slave = pty.openpty()
    env = {**os.environ, "TERM": "xterm-256color", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "wsa_agent", "--project", str(tmp_path)],
        stdin=slave, stdout=slave, stderr=slave, env=env,
    )
    os.close(slave)
    data = bytearray()
    sent = False
    deadline = time.monotonic() + 45
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.2)
            if readable:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                data.extend(chunk)
            if not sent and b"shortcuts" in data.lower():
                os.write(master, b"/exit\r")
                sent = True
            if proc.poll() is not None:
                break
        code = proc.wait(timeout=3)
        server_log = Path.home() / ".wsa-server.log"
        diagnostic = server_log.read_text(encoding="utf-8", errors="replace")[-5000:] if server_log.exists() else "missing server log"
        assert code == 0, data.decode(errors="replace")[-5000:] + "\nBackend log:\n" + diagnostic
        output = data.decode(errors="replace")
        assert "Backend ready" in output, output[-5000:]
        assert sent and "Goodbye" in output, output[-5000:]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        os.close(master)
