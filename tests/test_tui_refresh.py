import io
from unittest.mock import patch

from rich.console import Console

from wsa_agent import commands, renderer
from wsa_agent.session import SessionState


def test_status_refreshes_same_browser_without_switching_session(tmp_path):
    state = SessionState(project_root=tmp_path, active_browser="brave", active_account_label="brave")
    health = {"dom_relay_active": True, "active_browser": "brave", "active_relay": "brave",
              "active_providers": {"gemini": {"model_badge": "New model", "email": "user@example.com"}}}
    with patch("wsa_agent.client.WSAClient.get_health", return_value=health), patch.object(renderer, "print_status"):
        commands._cmd_status("", state)
    assert state.active_web_model == "New model"
    assert state.active_account_email == "user@example.com"
    assert state._ui_connection == "Server ready · browser relay active"
    health["active_browser"] = "chrome"
    health["active_relay"] = "chrome"
    with patch("wsa_agent.client.WSAClient.get_health", return_value=health), patch.object(renderer, "print_status"):
        commands._cmd_status("", state)
    assert state.active_browser == "brave"
    assert state.active_account_label == "brave"


def test_welcome_fits_small_terminal_and_keeps_account_visible(tmp_path):
    state = SessionState(project_root=tmp_path, active_account_label="brave",
                         active_account_email="long.account.name@example.com")
    stream = io.StringIO()
    old = renderer.console
    try:
        renderer.console = Console(file=stream, width=80, force_terminal=False)
        with patch.object(state, "list_saved_sessions", return_value=[]):
            renderer.print_welcome(state)
    finally:
        renderer.console = old
    assert "long.account.name@example.com" in stream.getvalue()
    assert max(map(len, stream.getvalue().splitlines())) <= 80


def test_status_discloses_serialized_workers(tmp_path):
    state = SessionState(project_root=tmp_path, dual_lane=True)
    stream = io.StringIO()
    old = renderer.console
    try:
        renderer.console = Console(file=stream, width=100, force_terminal=False)
        renderer.print_status(state)
    finally:
        renderer.console = old
    assert "dual requested; serialized" in stream.getvalue()


def test_footer_shows_full_account_model_and_permissions_at_all_widths(tmp_path):
    import re
    from wsa_agent import __main__ as main
    state = SessionState(project_root=tmp_path, permission_mode='auto', active_browser='brave',
                         active_account_label='brave', active_account_email='long.account.name@example.com',
                         active_web_model='Gemini Hydrogen Extended Thinking')
    for width in (30, 50, 80, 120):
        with patch.object(renderer, 'console', Console(file=io.StringIO(), width=width)):
            text = re.sub(r'\x1b\[[0-9;]*m', '', main._build_bottom_toolbar(state))
        assert max(map(len, text.splitlines())) <= width
        joined = ''.join(line.strip() for line in text.splitlines())
        assert 'long.account.name@example.com' in joined
        assert 'GeminiHydrogenExtendedThinking' in ''.join(joined.split())
        assert 'bypass permissions on' in joined
    state.active_account_email = 'changed@example.com'
    assert 'changed@example.com' in main._build_bottom_toolbar(state)
