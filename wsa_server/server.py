"""HTTP API server (OpenAI Chat Completions, Responses, Google v1beta, Relay & Extension bridges)."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn, TCPServer
from typing import Optional

from .accounts import get_account_pool, load_cookie
from .config import CONFIG, _CONFIG_LOCK, _resolve_local_path, __version__, log
from .gemini import _bridge_provider_for_cfg, gemini_stream_generate, gemini_stream_generate_events
from .models import LAST_UPSTREAM_MODEL, MODELS, WEB_BRIDGE_MODES, WEB_PROVIDER_SPECS
from .multimodal import (
    _get_server_namespace,
    detect_image_mime,
    fetch_image_bytes,
    google_contents_to_prompt,
    messages_to_prompt,
    upload_image,
    upload_images,
)
from .parser import extract_response_text, extract_thinking_text
from .relay import (
    KNOWN_WEB_PROVIDERS,
    PromptTooLongError,
    ProviderLimitError,
    _dom_relay,
    _dom_relay_lock,
    _is_provider_thread_url,
    _normalize_provider,
    _relay_req_ctx,
    _session_relay_key,
    argon_strict_relay_required,
    browser_cmd_get_pending,
    browser_cmd_post_result,
    clear_session_relay_chats,
    dom_relay_cancel_request,
    dom_relay_get_pending,
    dom_relay_poll_for_client,
    dom_relay_post_progress,
    dom_relay_post_response,
    execute_browser_command,
    get_ps_status,
    is_chat_url_stale_for_relay,
    is_dom_relay_active,
    is_dom_relay_cancelled,
    is_prompt_too_long_error,
    is_provider_limit_error,
    list_cancelled_req_ids,
    list_relays_status,
    prepare_session_relay_prompt,
    receive_cookie_sync,
    receive_ps_relay,
    record_session_relay_turn,
    rotate_account,
    seed_session_hint_url,
    select_active_relay,
    switch_account,
)
from .tools import _extract_tool_names, _find_pending_tool_hold_pos, parse_tool_calls


def _build_upstream_error_payload(exc: Exception, provider: str = "") -> tuple:
    """Build (http_status, openai_error_payload) preserving limit or prompt-length metadata."""
    err_str = str(exc)
    if isinstance(exc, PromptTooLongError) or is_prompt_too_long_error(err_str):
        prov_name = (getattr(exc, "provider", "") or provider or "").strip().lower()
        return 400, {
            "error": {
                "message": f"upstream error: {err_str}",
                "type": "invalid_request_error",
                "code": "context_length_exceeded",
                "provider": prov_name,
                "prompt_too_long": True,
            }
        }
    if isinstance(exc, ProviderLimitError) or is_provider_limit_error(err_str):
        prov_name = (getattr(exc, "provider", "") or provider or "").strip().lower()
        return 429, {
            "error": {
                "message": f"upstream error: {err_str}",
                "type": "rate_limit_error",
                "code": "provider_limit_reached",
                "provider": prov_name,
                "limit_reached": True,
            }
        }
    return 502, {"error": {"message": f"upstream error: {err_str}", "type": "upstream_error"}}

# Default server hooks & patchable exports on `wsa_server.server`
__all__ = [
    "WSAHandler",
    "GeminiHandler",
    "ThreadedServer",
    "ThreadedHTTPServer",
    "generate",
    "generate_stream",
    "upload_image",
    "fetch_image_bytes",
    "detect_image_mime",
    "parse_tool_calls",
]

def _default_server_generate(prompt, model_id=1, think_mode=0, file_refs=None, extra_fields=None):
    cfg = dict(extra_fields) if isinstance(extra_fields, dict) else {"mode": model_id, "think": think_mode}
    raw = gemini_stream_generate(prompt, model_cfg=cfg, file_refs=file_refs)
    return extract_response_text(raw)

_default_server_generate._is_default_server_hook = True

def _default_server_generate_stream(prompt, model_id=1, think_mode=0, file_refs=None, extra_fields=None):
    cfg = dict(extra_fields) if isinstance(extra_fields, dict) else {"mode": model_id, "think": think_mode}
    for ev_type, delta in gemini_stream_generate_events(prompt, model_cfg=cfg, file_refs=file_refs):
        if ev_type == "thinking":
            yield ("thinking", delta)
        elif ev_type == "content":
            yield delta

_default_server_generate_stream._is_default_server_hook = True

# Default server hooks (patchable directly on `wsa_server.server.*`)
generate = _default_server_generate
generate_stream = _default_server_generate_stream

class WSAHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        client_ip = self.client_address[0] if self.client_address else "-"
        msg = fmt % args
        if getattr(self, "_suppress_poll_log", False) and (
            '"GET /pending-prompt' in msg or '"POST /pending-progress' in msg
        ):
            return
        log(f"{client_ip} {msg}")

    def send_json(self, data, status=200, extra_headers: Optional[dict] = None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "*")
        if extra_headers:
            for hk, hv in extra_headers.items():
                if hv is not None:
                    self.send_header(str(hk), str(hv))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _authorized(self):
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] in keys:
            return True
        for h in ("x-api-key", "x-goog-api-key"):
            if self.headers.get(h, "") in keys:
                return True
        if "?" in self.path:
            for pair in self.path.split("?", 1)[1].split("&"):
                if pair.startswith("key=") and pair[4:] in keys:
                    return True
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Expose-Headers", "*")
        self.end_headers()

    def do_GET(self):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            req_path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            if req_path == "/pending-prompt":
                if qs.get("client_id") or qs.get("browser") or qs.get("label"):
                    busy_flag = str((qs.get("busy") or ["0"])[0]).strip().lower() in ("1", "true", "yes")
                    generating_flag = str((qs.get("generating") or ["0"])[0]).strip().lower() in ("1", "true", "yes")
                    client_payload = {
                        "client_id": (qs.get("client_id") or [""])[0],
                        "browser": (qs.get("browser") or ["chrome"])[0],
                        "label": (qs.get("label") or [""])[0],
                        "email": (qs.get("email") or [""])[0],
                        "model_badge": (qs.get("model_badge") or [""])[0],
                        "active_site": (qs.get("site") or qs.get("active_site") or [""])[0],
                        "provider": (qs.get("provider") or [""])[0],
                    }
                    raw_auth = str((qs.get("auth_user") or [""])[0]).strip()
                    if raw_auth:
                        client_payload["auth_user"] = raw_auth
                    for prov_key in KNOWN_WEB_PROVIDERS:
                        for q_key in (f"has_{prov_key}", f"{prov_key}_email", f"{prov_key}_model"):
                            if q_key in qs:
                                client_payload[q_key] = (qs.get(q_key) or [""])[0]
                    busy_req_id = str((qs.get("busy_req_id") or [""])[0]).strip()
                    item, reg = dom_relay_poll_for_client(
                        client_payload,
                        busy=busy_flag,
                        busy_req_id=busy_req_id,
                        generating=generating_flag,
                    )
                    extra = {
                        "X-Relay-Active": "1" if reg.get("is_active") else "0",
                        "X-Active-Relay": reg.get("active_relay_label") or "",
                        "X-Active-Relay-Id": reg.get("active_relay_id") or "",
                        "X-Cancelled-Req-Ids": ",".join(list_cancelled_req_ids()),
                        "X-Busy-Req-Valid": "1" if reg.get("busy_req_valid", True) else "0",
                    }
                else:
                    item = dom_relay_get_pending()
                    bcmd = browser_cmd_get_pending()
                    if bcmd:
                        if item is None:
                            item = {"browser_cmd": bcmd}
                        else:
                            item["browser_cmd"] = bcmd
                    extra = None
                if item is None:
                    self._suppress_poll_log = True
                self.send_json(item, extra_headers=extra)
                return
            if req_path == "/ps-status":
                self.send_json(get_ps_status())
                return
            if req_path == "/v1/health":
                cookie_str, sapisid = load_cookie()
                pool = get_account_pool()
                rel_status = list_relays_status()
                with _CONFIG_LOCK:
                    active_file = CONFIG.get("cookie_file")
                    upstream_model = dict(LAST_UPSTREAM_MODEL)
                    single_mode = bool(CONFIG.get("single_account_mode", True))
                with _dom_relay_lock:
                    last_poll = _dom_relay.get("last_poll_ts", 0.0)
                poll_age = round(time.time() - last_poll, 1) if last_poll else None
                self.send_json({
                    "status": "ok",
                    "product": "wsa",
                    "version": __version__,
                    "pid": os.getpid(),
                    "authenticated": bool(cookie_str and sapisid),
                    "has_xsrf": bool(CONFIG.get("xsrf_token")),
                    "gemini_bl": CONFIG.get("gemini_bl"),
                    "single_account_mode": single_mode,
                    "active_account": os.path.basename(active_file) if active_file else None,
                    "active_relay": rel_status.get("active_relay_label"),
                    "active_relay_id": rel_status.get("active_relay_id"),
                    "active_browser": rel_status.get("active_browser"),
                    "active_email": rel_status.get("active_email"),
                    "active_site": rel_status.get("active_site"),
                    "active_provider": rel_status.get("active_provider"),
                    "active_model_badge": rel_status.get("active_model_badge"),
                    "active_providers": rel_status.get("active_providers"),
                    "explicit_relay_lock": rel_status.get("explicit_lock"),
                    "relays": rel_status.get("relays"),
                    "account_pool_size": len(pool),
                    "accounts": [os.path.basename(p) for p in pool],
                    "dom_relay_active": is_dom_relay_active(),
                    "dom_relay_last_poll_age_sec": poll_age,
                    "last_upstream_model": upstream_model,
                })
                return
            if req_path.startswith("/v1") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            if req_path == "/v1/relays":
                self.send_json(list_relays_status())
                return
            if req_path == "/v1/accounts":
                pool = get_account_pool()
                rel_status = list_relays_status()
                with _CONFIG_LOCK:
                    active_file = _resolve_local_path(CONFIG.get("cookie_file"))
                self.send_json({
                    "active_account": os.path.basename(active_file) if active_file else None,
                    "active_path": active_file,
                    "active_relay_id": rel_status.get("active_relay_id"),
                    "active_relay": rel_status.get("active_relay_label"),
                    "active_browser": rel_status.get("active_browser"),
                    "single_account_mode": rel_status.get("single_account_mode", True),
                    "relays": rel_status.get("relays", []),
                    "accounts": [
                        {"index": idx, "name": os.path.basename(p), "path": p, "active": bool(active_file and os.path.abspath(p) == os.path.abspath(active_file))}
                        for idx, p in enumerate(pool)
                    ],
                })
                return
            if req_path == "/v1/models":
                self.send_json({"object": "list", "data": [
                    {
                        "id": n,
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": WEB_PROVIDER_SPECS.get(c.get("provider") or "gemini", {}).get("owned_by", "google"),
                        "description": c["desc"],
                    }
                    for n, c in MODELS.items()
                ]})
            elif req_path.startswith("/v1beta/models"):
                self.send_json({"models": [
                    {"name": f"models/{n}", "displayName": n, "description": c["desc"],
                     "supportedGenerationMethods": ["generateContent", "streamGenerateContent"]}
                    for n, c in MODELS.items()
                ]})
            elif req_path == "/":
                self.send_json({"status": "ok", "version": __version__, "models": list(MODELS.keys())})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"GET error: {e}")

    def do_POST(self):
        try:
            req_path = urllib.parse.urlsplit(self.path).path
            if req_path.startswith("/v1") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            body = self._read_request_body()
            if req_path == "/pending-response":
                req = json.loads(body or b"{}")
                ok = dom_relay_post_response(
                    req.get("id", ""),
                    response=req.get("response", ""),
                    error=req.get("error", ""),
                    thinking=req.get("thinking", ""),
                    model=req.get("model", ""),
                    chat_url=req.get("chat_url", ""),
                    client_id=str(req.get("client_id") or ""),
                    generated_images=req.get("generated_images"),
                )
                self.send_json({"ok": ok})
            elif req_path == "/pending-progress":
                self._suppress_poll_log = True
                req = json.loads(body or b"{}")
                req_id = str(req.get("id") or "")
                raw_attached = req.get("images_attached")
                raw_up_ms = req.get("upload_ms")
                raw_sub_ms = req.get("submit_ms")
                ok = dom_relay_post_progress(
                    req_id,
                    response=req.get("response", ""),
                    thinking=req.get("thinking", ""),
                    model=req.get("model", ""),
                    chat_url=req.get("chat_url", ""),
                    client_id=str(req.get("client_id") or ""),
                    seq=int(req.get("seq") or 0),
                    stage=str(req.get("stage") or ""),
                    images_attached=raw_attached if isinstance(raw_attached, bool) else None,
                    upload_ms=int(raw_up_ms) if isinstance(raw_up_ms, (int, float)) else None,
                    submit_ms=int(raw_sub_ms) if isinstance(raw_sub_ms, (int, float)) else None,
                )
                self.send_json({"ok": ok, "cancelled": is_dom_relay_cancelled(req_id)})
            elif req_path in ("/relay/cancel", "/v1/relays/cancel"):
                req = json.loads(body or b"{}")
                cancelled_ids = dom_relay_cancel_request(
                    client_request_id=str(req.get("client_request_id") or ""),
                    session_id=str(req.get("session_id") or ""),
                    req_id=str(req.get("req_id") or req.get("id") or ""),
                )
                self.send_json({"ok": True, "cancelled": cancelled_ids})
            elif req_path in ("/relay/clear-session", "/v1/relays/clear-session"):
                req = json.loads(body or b"{}")
                raw_provs = req.get("providers")
                if not isinstance(raw_provs, list):
                    p_one = str(req.get("provider") or "").strip()
                    raw_provs = [p_one] if p_one else []
                res = clear_session_relay_chats(
                    session_id=str(req.get("session_id") or ""),
                    providers=raw_provs,
                    include_roles=bool(req.get("include_roles", True)),
                    client_urls=req.get("client_urls") if isinstance(req.get("client_urls"), list) else None,
                )
                self.send_json(res)
            elif req_path in ("/relay/select", "/v1/relays/select"):
                req = json.loads(body or b"{}")
                res = select_active_relay(
                    target=req.get("target", req.get("relay", req.get("account"))),
                    client_id=req.get("client_id"),
                    browser=req.get("browser"),
                    label=req.get("label"),
                    explicit=bool(req.get("explicit", True)),
                    session_id=str(req.get("session_id") or ""),
                    providers=req.get("providers") if isinstance(req.get("providers"), dict) else None,
                    active_site=str(req.get("active_site") or req.get("site") or ""),
                    account_email=str(req.get("account_email") or req.get("email") or ""),
                    model_badge=str(req.get("model_badge") or ""),
                    auth_user=req.get("auth_user"),
                )
                self.send_json(res, 200 if res.get("ok") else 400)
            elif req_path == "/sync-cookies":
                req = json.loads(body or b"{}")
                res = receive_cookie_sync(req)
                self.send_json(res, 200 if res.get("ok") else 400)
            elif req_path == "/v1/browser/command":
                req = json.loads(body or b"{}")
                res = execute_browser_command(
                    action=str(req.get("action") or "list_tabs"),
                    params=req.get("params") if isinstance(req.get("params"), dict) else {},
                    timeout=float(req.get("timeout") or 25.0),
                )
                self.send_json(res, 200 if res.get("ok") else 502)
            elif req_path == "/browser-command-result":
                self._suppress_poll_log = True
                req = json.loads(body or b"{}")
                ok = browser_cmd_post_result(
                    str(req.get("cmd_id") or ""),
                    result=req.get("result") if isinstance(req.get("result"), dict) else req,
                )
                self.send_json({"ok": ok})
            elif req_path == "/ps-relay":
                req = json.loads(body or b"{}")
                res = receive_ps_relay(req)
                self.send_json(res)
            elif req_path == "/v1/chat/completions":
                self.handle_chat(body)
            elif req_path == "/v1/responses":
                self.handle_responses(body)
            elif req_path == "/v1/accounts/switch":
                req = json.loads(body or b"{}")
                target = req.get("account", req.get("relay", req.get("index", "")))
                res = switch_account(target, session_id=str(req.get("session_id") or ""))
                self.send_json(res, 200 if res.get("ok") else 400)
            elif req_path == "/v1/accounts/rotate":
                nxt = rotate_account("manual API request")
                rel_status = list_relays_status()
                self.send_json({
                    "ok": bool(nxt),
                    "active_account": os.path.basename(nxt) if nxt else rel_status.get("active_relay_label"),
                    "active_relay": rel_status.get("active_relay_label"),
                    "active_relay_id": rel_status.get("active_relay_id"),
                    "pool": [os.path.basename(p) for p in get_account_pool()],
                    "relays": rel_status.get("relays", []),
                })
            elif ":streamGenerateContent" in req_path:
                self._handle_google_generate(body, stream=True)
            elif ":generateContent" in req_path:
                self._handle_google_generate(body, stream=False)
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"POST error: {e}")
            try:
                self.send_json({"error": {"message": str(e)}}, 500)
            except Exception:
                pass

    def _read_request_body(self) -> bytes:
        transfer_encoding = self.headers.get("Transfer-Encoding", "")
        if "chunked" in transfer_encoding.lower():
            chunks = []
            while True:
                size_line = self.rfile.readline()
                if not size_line:
                    break
                size = int(size_line.split(b";", 1)[0].strip(), 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _resolve_model(self, model_name):
        raw_name = str(model_name or CONFIG.get("default_model") or "gemini").strip()
        low = raw_name.lower()
        alias_map = {
            "gemini": "gemini",
            "gemini-web": "gemini",
            "gemini.google.com": "gemini",
            "claude": "claude",
            "claude-web": "claude",
            "claude.ai": "claude",
            "chatgpt": "chatgpt",
            "chatgpt-web": "chatgpt",
            "chatgpt.com": "chatgpt",
            "chat.openai.com": "chatgpt",
            "openai": "chatgpt",
            "gpt-6": "chatgpt",
            "gpt-5": "chatgpt",
            "o3": "chatgpt",
            "o4-mini": "chatgpt",
            "deepseek": "deepseek",
            "deepseek-web": "deepseek",
            "deepseek-r1": "deepseek",
            "deepseek-v3": "deepseek",
            "deepseek-chat": "deepseek",
            "chat.deepseek.com": "deepseek",
            "deepseek.com": "deepseek",
            "venice": "venice",
            "venice-web": "venice",
            "veniceai": "venice",
            "venice.ai": "venice",
            "vernice": "venice",
            "vernice-web": "venice",
            "verniceai": "venice",
            "vernice.ai": "venice",
            "deepseek-v4.1-flash": "venice",
            "deepseek-v4-1-flash-uncensored": "venice",
            "deepseek-v4.1-flash-uncensored": "venice",
        }
        if low in alias_map:
            canonical = alias_map[low]
            return canonical, dict(MODELS[canonical]), None
        think_override = None
        base_name = raw_name
        if "@think=" in raw_name:
            base_name, think_str = raw_name.rsplit("@think=", 1)
            try:
                think_override = int(think_str)
            except ValueError:
                return None, None, f"Invalid think depth in model: {raw_name}"
        cfg = MODELS.get(raw_name) or MODELS.get(base_name)
        if not cfg:
            return None, None, f"Unknown model: {raw_name}"
        if think_override is not None:
            cfg = dict(cfg)
            if cfg.get("mode") == 8:
                cfg["slot80"] = {0: 2, 1: 2, 2: 4, 3: 1, 4: 1}.get(think_override, 2)
                cfg["think"] = 0
            else:
                cfg["think"] = think_override
        return base_name, cfg, None

    def _iter_events(self, prompt, model_cfg, file_refs=None):
        srv = _get_server_namespace()
        gen_stream_fn = getattr(srv, "generate_stream", _default_server_generate_stream)
        if getattr(gen_stream_fn, "_is_default_server_hook", None) is not True:
            for item in gen_stream_fn(
                prompt,
                model_cfg.get("mode", 1),
                model_cfg.get("think", 0),
                file_refs,
                model_cfg,
            ):
                if isinstance(item, tuple) and len(item) == 2:
                    yield item
                else:
                    yield ("content", item)
        else:
            yield from gemini_stream_generate_events(prompt, model_cfg=model_cfg, file_refs=file_refs)

    def _call_gemini(self, prompt, model_cfg, tools, file_refs=None):
        srv = _get_server_namespace()
        gen_fn = getattr(srv, "generate", _default_server_generate)
        parse_tc_fn = getattr(srv, "parse_tool_calls", parse_tool_calls)
        if getattr(gen_fn, "_is_default_server_hook", None) is not True:
            text = gen_fn(
                prompt,
                model_cfg.get("mode", 1),
                model_cfg.get("think", 0),
                file_refs,
                model_cfg,
            )
            thinking = ""
        else:
            raw = gemini_stream_generate(prompt, model_cfg=model_cfg, file_refs=file_refs)
            text = extract_response_text(raw)
            thinking = extract_thinking_text(raw)
        tool_calls = None
        if tools and text:
            if parse_tc_fn is parse_tool_calls:
                text, tool_calls = parse_tc_fn(text, allowed_names=_extract_tool_names(tools))
            else:
                text, tool_calls = parse_tc_fn(text)
        return text or "", tool_calls, thinking

    def handle_chat(self, body: bytes):
        req = json.loads(body)
        model_name, model_cfg, err = self._resolve_model(req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return
        provider = _normalize_provider(_bridge_provider_for_cfg(model_cfg))
        tools = req.get("tools")
        messages_in = req.get("messages", [])
        session_id = str(req.get("session_id") or "").strip()
        client_request_id = str(req.get("client_request_id") or "").strip()
        target_relay_id = str(req.get("relay_id") or req.get("target_relay_id") or "").strip()
        force_new_chat = req.get("new_chat") if isinstance(req.get("new_chat"), bool) else None
        hint_chat_url = str(req.get("chat_url") or "").strip()
        if force_new_chat is not True and session_id and _is_provider_thread_url(hint_chat_url, provider):
            seed_session_hint_url(session_id, hint_chat_url, provider=provider, relay_id=target_relay_id)
        if force_new_chat is True and session_id:
            clear_session_relay_chats(
                session_id=session_id,
                providers=[provider],
                include_roles=False,
                client_urls=[hint_chat_url] if hint_chat_url else None,
            )
        prompt, full_images = messages_to_prompt(messages_in, tools, provider=provider)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty prompt"}}, 400)
            return
        relay_prompt, fallback_prompt, relay_images, relay_new_chat, relay_hint_chat_url = prepare_session_relay_prompt(
            session_id,
            messages_in,
            tools,
            prompt,
            full_images,
            provider=provider,
            force_new_chat=force_new_chat,
            relay_id=target_relay_id,
            hint_chat_url=hint_chat_url,
        )
        images = full_images
        stream = req.get("stream", False)
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        will_use_dom_relay = (
            model_cfg.get("mode") in WEB_BRIDGE_MODES
            or (model_cfg.get("mode") in (7, 8) and (is_dom_relay_active() or argon_strict_relay_required()))
        )
        if will_use_dom_relay:
            file_refs = None
        else:
            try:
                file_refs = upload_images(images)
            except RuntimeError as e:
                self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
                return

        relay_meta = {
            "session_id": session_id,
            "provider": provider,
            "client_request_id": client_request_id,
            "target_relay_id": target_relay_id,
            "relay_prompt": relay_prompt,
            "fallback_prompt": fallback_prompt,
            "relay_images": relay_images,
            "image_gen": bool(req.get("image_gen", False)),
            "new_chat": relay_new_chat,
            "hint_chat_url": relay_hint_chat_url,
            "relay_used": False,
            "chat_url": "",
        }
        _relay_req_ctx.relay_meta = relay_meta

        if stream:
            try:
                self._start_sse()

                first_chunk = {
                    "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                }
                self.wfile.write(f"data: {json.dumps(first_chunk)}\n\n".encode("utf-8"))
                self.wfile.flush()

                pending_buf = ""
                streamed_parts = []
                tool_calls = None
                finish_reason = "stop"
                full_text_len = 0
                parse_tc_fn = getattr(_get_server_namespace(), "parse_tool_calls", parse_tool_calls)

                for event_type, delta in self._iter_events(prompt, model_cfg=model_cfg, file_refs=file_refs):
                    if event_type == "keepalive":
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if event_type == "generated_images":
                        img_chunk = {
                            "id": cid, "object": "chat.completion.chunk",
                            "created": int(time.time()), "model": model_name,
                            "choices": [],
                            "x_generated_images": delta,
                        }
                        self.wfile.write(
                            f"data: {json.dumps(img_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                        )
                        self.wfile.flush()
                        continue
                    if event_type == "relay_status":
                        status_chunk = {
                            "id": cid, "object": "chat.completion.chunk",
                            "created": int(time.time()), "model": model_name,
                            "choices": [],
                            "x_relay_status": delta,
                        }
                        self.wfile.write(
                            f"data: {json.dumps(status_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                        )
                        self.wfile.flush()
                        continue
                    if event_type == "reset_content":
                        pending_buf = ""
                        streamed_parts.clear()
                        full_text_len = 0
                        reset_chunk = {
                            "id": cid, "object": "chat.completion.chunk",
                            "created": int(time.time()), "model": model_name,
                            "choices": [{
                                "index": 0,
                                "delta": {"reset": True},
                                "finish_reason": None,
                            }],
                        }
                        self.wfile.write(
                            f"data: {json.dumps(reset_chunk)}\n\n".encode("utf-8")
                        )
                        self.wfile.flush()
                        continue
                    if event_type == "thinking":
                        chunk = {
                            "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                            "model": model_name,
                            "choices": [{"index": 0, "delta": {"thinking": delta}, "finish_reason": None}],
                        }
                        self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    elif event_type == "content":
                        full_text_len += len(delta)
                        if not tools:
                            streamed_parts.append(delta)
                            chunk = {
                                "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                                "model": model_name,
                                "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                            }
                            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        else:
                            pending_buf += delta
                            hold_pos = _find_pending_tool_hold_pos(pending_buf)
                            if hold_pos > 0:
                                prefix_text = pending_buf[:hold_pos]
                                streamed_parts.append(prefix_text)
                                chunk = {
                                    "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                                    "model": model_name,
                                    "choices": [{"index": 0, "delta": {"content": prefix_text}, "finish_reason": None}],
                                }
                                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                                self.wfile.flush()
                                pending_buf = pending_buf[hold_pos:]
                            elif hold_pos == -1:
                                streamed_parts.append(pending_buf)
                                chunk = {
                                    "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                                    "model": model_name,
                                    "choices": [{"index": 0, "delta": {"content": pending_buf}, "finish_reason": None}],
                                }
                                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                                self.wfile.flush()
                                pending_buf = ""

                if tools and pending_buf:
                    if parse_tc_fn is parse_tool_calls:
                        rem_text, tool_calls = parse_tc_fn(
                            pending_buf, allowed_names=_extract_tool_names(tools)
                        )
                    else:
                        rem_text, tool_calls = parse_tc_fn(pending_buf)
                    if rem_text:
                        streamed_parts.append(rem_text)
                        chunk = {
                            "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                            "model": model_name,
                            "choices": [{"index": 0, "delta": {"content": rem_text}, "finish_reason": None}],
                        }
                        self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                    if tool_calls:
                        finish_reason = "tool_calls"

                effective_prompt = relay_prompt if relay_meta.get("relay_used") else prompt
                if session_id and relay_meta.get("relay_used") and (streamed_parts or tool_calls):
                    record_session_relay_turn(
                        session_id,
                        "".join(streamed_parts),
                        tool_calls,
                        chat_url=str(relay_meta.get("chat_url") or ""),
                        relay_id=str(relay_meta.get("relay_id") or ""),
                        provider=provider,
                    )

                usage_obj = {
                    "prompt_tokens": len(effective_prompt) // 4,
                    "completion_tokens": full_text_len // 4,
                    "total_tokens": (len(effective_prompt) + full_text_len) // 4,
                }
                final_delta = {"tool_calls": tool_calls} if tool_calls else {}
                with _CONFIG_LOCK:
                    upstream_model = dict(LAST_UPSTREAM_MODEL)
                _stream_url = str(relay_meta.get("chat_url") or "")
                if _stream_url and not is_chat_url_stale_for_relay(
                    _stream_url,
                    provider=provider,
                    relay_id=str(relay_meta.get("relay_id") or ""),
                ):
                    upstream_model["chat_url"] = _stream_url
                chunk = {
                    "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": model_name,
                    "choices": [{"index": 0, "delta": final_delta, "finish_reason": finish_reason}],
                    "usage": usage_obj,
                    "x_upstream_model": upstream_model if (upstream_model.get("badge") or upstream_model.get("mode") is not None or upstream_model.get("chat_url")) else None,
                    "x_relay_timings": relay_meta.get("relay_timings") if isinstance(relay_meta.get("relay_timings"), dict) else None,
                }
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                rmeta = getattr(_relay_req_ctx, "relay_meta", None) or {}
                dom_relay_cancel_request(
                    client_request_id=str(rmeta.get("client_request_id") or ""),
                    session_id=str(rmeta.get("session_id") or ""),
                    req_id=str(rmeta.get("req_id") or ""),
                )
            except Exception as e:
                log(f"Stream error: {e}")
                try:
                    _, err_chunk = _build_upstream_error_payload(e, provider=provider)
                    self.wfile.write(f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except Exception:
                    pass
            finally:
                _relay_req_ctx.relay_meta = None
            return

        try:
            text, tool_calls, thinking = self._call_gemini(prompt, model_cfg, tools, file_refs)
        except Exception as e:
            err_status, err_body = _build_upstream_error_payload(e, provider=provider)
            self.send_json(err_body, err_status)
            return
        finally:
            _relay_req_ctx.relay_meta = None
        if session_id and relay_meta.get("relay_used") and (text or tool_calls):
            record_session_relay_turn(
                session_id,
                text or "",
                tool_calls,
                chat_url=str(relay_meta.get("chat_url") or ""),
                relay_id=str(relay_meta.get("relay_id") or ""),
                provider=provider,
            )
        effective_prompt = relay_prompt if relay_meta.get("relay_used") else prompt
        msg = {"role": "assistant", "content": text or None}
        if thinking:
            msg["thinking"] = thinking
        if tool_calls:
            msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"
        with _CONFIG_LOCK:
            upstream_model = dict(LAST_UPSTREAM_MODEL)
        _ns_url = str(relay_meta.get("chat_url") or "")
        if _ns_url and not is_chat_url_stale_for_relay(
            _ns_url,
            provider=provider,
            relay_id=str(relay_meta.get("relay_id") or ""),
        ):
            upstream_model["chat_url"] = _ns_url
        self.send_json({
            "id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": model_name,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": len(effective_prompt) // 4, "completion_tokens": len(text) // 4,
                      "total_tokens": (len(effective_prompt) + len(text)) // 4},
            "x_upstream_model": upstream_model if (upstream_model.get("badge") or upstream_model.get("mode") is not None or upstream_model.get("chat_url")) else None,
        })

    def handle_responses(self, body: bytes):
        req = json.loads(body)
        model_name, model_cfg, err = self._resolve_model(req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return
        input_items = req.get("input", [])
        tools = req.get("tools")
        messages = []
        if req.get("instructions"):
            messages.append({"role": "system", "content": req["instructions"]})
        if isinstance(input_items, str):
            messages.append({"role": "user", "content": input_items})
        elif isinstance(input_items, list):
            for item in input_items:
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    if item.get("type") == "function_call_output":
                        messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                                         "name": item.get("name", ""), "content": item.get("output", "")})
                    elif item.get("type") in ("input_text", "input_image", "image"):
                        messages.append({"role": "user", "content": [item]})
                    else:
                        messages.append({"role": item.get("role", "user"), "content": item.get("content", "")})
        if tools:
            tools = [
                {"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
                if t.get("type") == "function" and "function" not in t else t
                for t in tools
            ]
        provider = _normalize_provider(_bridge_provider_for_cfg(model_cfg))
        prompt, images = messages_to_prompt(messages, tools, provider=provider)
        will_use_dom_relay = (
            model_cfg.get("mode") in WEB_BRIDGE_MODES
            or (model_cfg.get("mode") in (7, 8) and (is_dom_relay_active() or argon_strict_relay_required()))
        )
        if will_use_dom_relay:
            file_refs = None
        else:
            try:
                file_refs = upload_images(images)
            except RuntimeError as e:
                self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
                return

        session_id = str(req.get("session_id") or self.headers.get("X-WSA-Session-Id") or "").strip()
        target_relay_id = str(self.headers.get("X-WSA-Relay-Id") or "").strip()
        _relay_req_ctx.relay_meta = {
            "session_id": session_id,
            "provider": provider,
            "client_request_id": "",
            "target_relay_id": target_relay_id,
            "relay_prompt": prompt,
            "fallback_prompt": prompt,
            "relay_images": images,
            "new_chat": bool(req.get("new_chat", False)),
            "relay_used": False,
            "chat_url": str(req.get("chat_url") or ""),
        }
        try:
            text, tool_calls, _ = self._call_gemini(prompt, model_cfg, tools, file_refs)
        except Exception as e:
            err_status, err_body = _build_upstream_error_payload(e, provider=provider)
            self.send_json(err_body, err_status)
            return
        finally:
            _relay_req_ctx.relay_meta = None
        rid = f"resp_{uuid.uuid4().hex[:16]}"
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        output = []
        if tool_calls:
            for tc in tool_calls:
                output.append({"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                               "name": tc["function"]["name"], "arguments": tc["function"]["arguments"], "status": "completed"})
        if text or not tool_calls:
            output.append({"type": "message", "id": mid, "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": text or "", "annotations": []}]})

        usage = {
            "input_tokens": len(prompt) // 4,
            "output_tokens": len(text or "") // 4,
            "total_tokens": (len(prompt) + len(text or "")) // 4,
        }
        if req.get("stream"):
            self._start_sse()
            sequence_number = 0

            def emit(event_type, **fields):
                nonlocal sequence_number
                sequence_number += 1
                event = {"type": event_type, "sequence_number": sequence_number, **fields}
                self.wfile.write(f"event: {event_type}\ndata: {json.dumps(event)}\n\n".encode("utf-8"))

            base_response = {
                "id": rid,
                "object": "response",
                "created_at": int(time.time()),
                "model": model_name,
            }
            emit("response.created", response={**base_response, "status": "in_progress", "output": [], "usage": None})
            emit("response.in_progress", response={**base_response, "status": "in_progress", "output": [], "usage": None})
            for output_index, item in enumerate(output):
                if item["type"] == "function_call":
                    pending_item = {
                        "type": "function_call",
                        "id": item["id"],
                        "call_id": item["call_id"],
                        "name": item["name"],
                        "arguments": "",
                        "status": "in_progress",
                    }
                    emit("response.output_item.added", output_index=output_index, item=pending_item)
                    emit("response.function_call_arguments.delta", item_id=item["id"], output_index=output_index, delta=item["arguments"])
                    emit("response.function_call_arguments.done", item_id=item["id"], output_index=output_index, arguments=item["arguments"])
                    emit("response.output_item.done", output_index=output_index, item=item)
                elif item["type"] == "message":
                    pending_item = {
                        "type": "message",
                        "id": item["id"],
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    }
                    emit("response.output_item.added", output_index=output_index, item=pending_item)
                    for content_index, content_part in enumerate(item["content"]):
                        event_fields = {"item_id": item["id"], "output_index": output_index, "content_index": content_index}
                        emit("response.content_part.added", **event_fields, part={"type": "output_text", "text": "", "annotations": []})
                        emit("response.output_text.delta", **event_fields, delta=content_part["text"])
                        emit("response.output_text.done", **event_fields, text=content_part["text"])
                        emit("response.content_part.done", **event_fields, part=content_part)
                    emit("response.output_item.done", output_index=output_index, item=item)
            emit("response.completed", response={**base_response, "status": "completed", "output": output, "usage": usage})
            self.wfile.flush()
            return

        self.send_json({"id": rid, "object": "response", "created_at": int(time.time()), "status": "completed",
                        "model": model_name, "output": output, "usage": usage})

    def _handle_google_generate(self, body: bytes, stream: bool):
        req = json.loads(body)
        m = re.match(r"/v1beta/models/([^:?]+)", self.path)
        model_name, model_cfg, err = self._resolve_model(m.group(1) if m else CONFIG["default_model"])
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return
        prompt, images = google_contents_to_prompt(req)
        try:
            file_refs = upload_images(images)
        except RuntimeError as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        if stream:
            try:
                self._start_sse()
                full_text = ""
                for ev_type, delta in self._iter_events(prompt, model_cfg=model_cfg, file_refs=file_refs):
                    if ev_type == "keepalive":
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if ev_type != "content" or not delta:
                        continue
                    full_text += delta
                    chunk_obj = {
                        "candidates": [{"content": {"parts": [{"text": delta}], "role": "model"}, "index": 0}],
                        "modelVersion": model_name,
                    }
                    self.wfile.write(f"data: {json.dumps(chunk_obj, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                final_chunk = {
                    "candidates": [{"finishReason": "STOP", "index": 0}],
                    "usageMetadata": {
                        "promptTokenCount": len(prompt) // 4,
                        "candidatesTokenCount": len(full_text) // 4,
                        "totalTokenCount": (len(prompt) + len(full_text)) // 4,
                    },
                    "modelVersion": model_name,
                }
                self.wfile.write(f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                rmeta = getattr(_relay_req_ctx, "relay_meta", None) or {}
                dom_relay_cancel_request(
                    client_request_id=str(rmeta.get("client_request_id") or ""),
                    session_id=str(rmeta.get("session_id") or ""),
                    req_id=str(rmeta.get("req_id") or ""),
                )
            except Exception as e:
                log(f"Google stream error: {e}")
            return

        try:
            text, _, _ = self._call_gemini(prompt, model_cfg, None, file_refs)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return
        response_obj = {
            "candidates": [{"content": {"parts": [{"text": text or ""}], "role": "model"}, "finishReason": "STOP", "index": 0}],
            "modelVersion": model_name,
        }
        self.send_json(response_obj)


GeminiHandler = WSAHandler


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # HTTPServer resolves a hostname synchronously here. A slow reverse DNS
        # lookup can otherwise prevent the local bridge from starting at all.
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


ThreadedHTTPServer = ThreadedServer
