"""CLI entry point for the WSA server (`python -m wsa_server` / `wsa-server`)."""
from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

_here = Path(__file__).resolve().parent.parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))
try:
    from wsa_agent.bootstrap import ensure_wsa_dependencies
    ensure_wsa_dependencies()
except Exception:
    pass

from . import config as _cfg
from .accounts import get_account_pool
from .config import CONFIG, _CONFIG_LOCK, __version__, load_config
from .gemini import refresh_xsrf, update_bl_if_needed
from .models import MODELS
from .relay import load_relay_sessions
from .server import ThreadedServer, WSAHandler

def main():
    parser = argparse.ArgumentParser(description="WSA Web Server Agent (Claude & Gemini Web Proxy)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--cookie-file", type=str, default=None)
    parser.add_argument("--proxy", type=str, default=None)
    parser.add_argument("--version", action="version", version=f"wsa-server {__version__}")
    args = parser.parse_args()

    config_path = args.config or os.environ.get("WSA_CONFIG")
    if not config_path:
        for p in [
            os.path.join(_cfg.SCRIPT_DIR, "config.json"),
            "./config.json",
            os.path.expanduser("~/.config/wsa/config.json"),
        ]:
            if os.path.exists(p):
                config_path = p
                break
    load_config(config_path)
    load_relay_sessions()

    with _CONFIG_LOCK:
        if args.port:
            CONFIG["port"] = args.port
        if args.cookie_file:
            CONFIG["cookie_file"] = args.cookie_file
        if args.proxy:
            CONFIG["proxy"] = args.proxy

    port = CONFIG["port"]
    srv = ThreadedServer((CONFIG["host"], port), WSAHandler)
    def _startup_sync():
        update_bl_if_needed()
        refresh_xsrf()

    threading.Thread(target=_startup_sync, daemon=True).start()
    pool = get_account_pool()
    print(f"WSA (Web Server Agent) v{__version__}")
    print(f"  Listening: http://{CONFIG['host']}:{port}")
    print(f"  Base URL:  http://localhost:{port}/v1")
    print(f"  Models:    {', '.join(MODELS.keys())}")
    print(f"  Cookie:    {'yes (' + CONFIG['cookie_file'] + ')' if CONFIG.get('cookie_file') else 'none (anonymous)'}")
    if len(pool) > 1:
        print(f"  Accounts:  {len(pool)} Pro accounts in pool ({', '.join(os.path.basename(p) for p in pool)})")
    print(f"  BL:        {CONFIG['gemini_bl']}")
    print()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        srv.shutdown()


if __name__ == "__main__":
    main()
