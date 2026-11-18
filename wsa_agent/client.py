"""
API client: streaming completions from the WSA local bridge server.

Handles:
  - Live token streaming (on_token callback, called per delta)
  - Thinking/reasoning streaming (on_thinking callback, separate from answer)
  - OpenAI-format tool_calls accumulation in streaming mode
  - Automatic retry on transient connection errors
  - Usage tracking
"""
from __future__ import annotations
import json
import os
import time
import sys
import traceback
import uuid
from typing import List, Dict, Optional, Callable

import httpx

from .tools import TOOL_DEFINITIONS


# ── Verbose logging control ──────────────────────────────────────────────────
VERBOSE_DEBUG = os.environ.get("WSA_DEBUG", os.environ.get("GWA_DEBUG", "0")) == "1"


def _accumulate_tool_deltas(acc: dict, deltas) -> None:
    """Keep interleaved stream fragments correlated; reject ambiguous protocol data."""
    if deltas is None:
        return
    if not isinstance(deltas, list):
        raise ValueError("tool_calls delta must be an array")
    for tc in deltas:
        if not isinstance(tc, dict):
            raise ValueError("Each tool call delta must be an object")
        if "index" not in tc and (len(deltas) > 1 or len(acc) > 1):
            raise ValueError("Missing tool call index in a multi-call stream")
        idx = tc.get("index", 0)
        if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
            raise ValueError("Tool call index must be a non-negative integer")
        fn = tc.get("function")
        if fn is None:
            fn = {}
        if not isinstance(fn, dict):
            raise ValueError("Tool call function must be an object")
        name = fn.get("name", "")
        args = fn.get("arguments", "")
        name = "" if name is None else name
        args = "" if args is None else args
        if not isinstance(name, str) or not isinstance(args, str):
            raise ValueError("Streamed function name and arguments must be string fragments")
        cid = tc.get("id")
        if cid is not None and (not isinstance(cid, str) or not cid.strip()):
            raise ValueError("Tool call ID must be a non-empty string")
        if idx not in acc:
            if len(acc) >= 128:
                raise ValueError("Too many tool calls in one streamed response (maximum 128)")
            acc[idx] = {"id": cid or f"call_{idx}", "type": "function", "function": {"name": "", "arguments": ""}}
        entry = acc[idx]
        if cid:
            if entry.get("_wire_id") and entry["_wire_id"] != cid:
                raise ValueError(f"Tool call ID changed at index {idx}")
            if any(k != idx and item.get("_wire_id") == cid for k, item in acc.items()):
                raise ValueError(f"Duplicate tool call ID across stream indices: {cid}")
            entry["_wire_id"] = cid
            entry["id"] = cid
        if len(entry["function"]["arguments"]) + len(args) > 2_000_000:
            raise ValueError("Tool arguments exceeded stream safety limit (2,000,000 characters)")
        entry["function"]["name"] += name
        entry["function"]["arguments"] += args


def _sanitize_payload(value):
    """Make pasted Windows UTF-16 surrogate text safe for JSON/HTTP encoding."""
    if isinstance(value, str):
        if value.isascii():
            return value  # Includes base64 screenshots, which need no Unicode repair.
        try:
            return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        except Exception:
            return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    if isinstance(value, dict):
        return {_sanitize_payload(key): _sanitize_payload(item) for key, item in value.items()}
    return value

def log_debug(msg: str):
    """Print debug message with timestamp."""
    if VERBOSE_DEBUG:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [CLIENT] {msg}", file=sys.stderr, flush=True)


class ProviderLimitError(ConnectionError):
    """Raised when the active web provider hits a message, usage, rate, or capacity limit."""

    def __init__(self, message: str, *, provider: str = "", raw_error: str = ""):
        super().__init__(message)
        self.provider = str(provider or "").strip().lower()
        self.raw_error = str(raw_error or message)


class PromptTooLongError(ConnectionError):
    """Raised when the submitted prompt exceeds a web provider's composer length ceiling."""

    def __init__(self, message: str, *, provider: str = "", raw_error: str = ""):
        super().__init__(message)
        self.provider = str(provider or "").strip().lower()
        self.raw_error = str(raw_error or message)


def _extract_prompt_too_long_from_dict(err_obj: object, status_code: int = 0) -> Optional[tuple[str, str]]:
    """Return `(provider, message)` if an error payload indicates composer prompt-too-long rejection."""
    if not isinstance(err_obj, dict):
        return None
    inner = err_obj.get("error") if isinstance(err_obj.get("error"), dict) else err_obj
    if not isinstance(inner, dict):
        return None
    code = str(inner.get("code") or "").strip().lower()
    msg = str(inner.get("message") or "").strip()
    prov = str(inner.get("provider") or "").strip().lower()
    too_long_flag = bool(inner.get("prompt_too_long"))
    low_msg = msg.lower()
    if (
        code in ("context_length_exceeded", "prompt_too_long")
        or too_long_flag
        or "[prompt_too_long]" in low_msg
        or any(
            kw in low_msg
            for kw in (
                "message you submitted was too long",
                "please edit it and resubmit",
                "exceeds maximum length",
                "exceeds the maximum message length",
                "prompt is too long",
                "character limit exceeded",
            )
        )
    ):
        return (prov, msg or "Prompt exceeds web composer message length limit")
    if status_code == 400 and "too long" in low_msg:
        return (prov, msg)
    return None


def _extract_limit_error_from_dict(err_obj: object, status_code: int = 0) -> Optional[tuple[str, str]]:
    """Return `(provider, message)` if an error payload indicates a provider usage/rate limit."""
    if not isinstance(err_obj, dict):
        return None
    if _extract_prompt_too_long_from_dict(err_obj, status_code=status_code) is not None:
        return None
    inner = err_obj.get("error") if isinstance(err_obj.get("error"), dict) else err_obj
    if not isinstance(inner, dict):
        return None
    code = str(inner.get("code") or "").strip().lower()
    err_type = str(inner.get("type") or "").strip().lower()
    msg = str(inner.get("message") or "").strip()
    prov = str(inner.get("provider") or "").strip().lower()
    if "[safeguard_paused]" in msg.lower():
        return None
    limit_flag = bool(inner.get("limit_reached"))
    if (
        status_code == 429
        or code == "provider_limit_reached"
        or err_type == "rate_limit_error"
        or limit_flag
        or "[limit_reached]" in msg.lower()
    ):
        return (prov, msg or "Provider usage/rate limit reached")
    low_msg = msg.lower()
    if any(
        kw in low_msg
        for kw in (
            "usage limit",
            "message limit",
            "rate limit",
            "too many requests",
            "current usage cap",
            "reached your limit",
            "reached our limit",
            "server is busy",
            "limit resets",
            "responses will use another model",
        )
    ):
        return (prov, msg)
    return None


class WSAClient:
    def __init__(self, api_base: str, api_key: str, timeout: int = 300):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.last_upstream_model: Optional[Dict] = None
        self.last_relay_timings: Optional[Dict] = None
        read_timeout = max(320.0, float(timeout or 300) + 20.0)
        self._client = httpx.Client(timeout=httpx.Timeout(connect=10.0, read=read_timeout, write=30.0, pool=10.0))

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _url(self) -> str:
        return f"{self.api_base}/chat/completions"

    def stream(
        self,
        messages: List[Dict],
        model: str,
        on_token: Callable[[str], None] = None,
        on_thinking: Callable[[str], None] = None,
        on_reset: Callable[..., None] = None,
        on_status: Callable[[str], None] = None,
        on_relay_status: Optional[Callable[[Dict], None]] = None,
        use_tools: bool = True,
        session_id: Optional[str] = None,
        chat_url: Optional[str] = None,
        relay_id: Optional[str] = None,
        new_chat: Optional[bool] = None,
        tools_override: Optional[List[Dict]] = None,
        client_request_id: Optional[str] = None,
    ) -> dict:
        """
        Stream a completion with live token delivery.

        Args:
            messages:    Full conversation history (system + turns).
            model:       Model identifier string.
            on_token:    Called with each answer text delta as it arrives.
            on_thinking: Called with each thinking/reasoning delta as it arrives.
                         Thinking tokens come before answer tokens.

        Returns:
            {
              "content":    str | None,       # full accumulated answer
              "thinking":   str | None,       # full accumulated thinking text
              "tool_calls": list[dict],       # OpenAI-format tool_calls
              "finish_reason": str,
              "usage": {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
            }
        """
        client_req_id = (
            str(client_request_id).strip()[:64]
            if client_request_id and str(client_request_id).strip()
            else uuid.uuid4().hex[:12]
        )
        log_debug(f"stream() called: model={model}, messages={len(messages)}, req_id={client_req_id}, timeout={self._client.timeout}")
        
        payload = _sanitize_payload({
            "model": model,
            "messages": messages,
            "stream": True,
            "client_request_id": client_req_id,
        })
        if session_id:
            payload["session_id"] = session_id
        if chat_url:
            payload["chat_url"] = chat_url
        if relay_id:
            payload["relay_id"] = relay_id
        if new_chat is not None:
            payload["new_chat"] = bool(new_chat)
        if use_tools:
            payload["tools"] = tools_override if tools_override is not None else TOOL_DEFINITIONS
            payload["tool_choice"] = "auto"

        full_content = ""
        full_thinking = ""
        tool_calls_acc: dict = {}   # index → {id, type, function: {name, arguments}}
        finish_reason = "stop"
        usage = {}
        last_err = None
        self.last_upstream_model = None
        self.last_relay_timings = None
        relay_timings: Optional[Dict] = None
        
        start_time = time.time()

        for attempt in range(3):
            log_debug(f"Attempt {attempt + 1}/3 starting...")
            if attempt > 0 and on_reset:
                try:
                    on_reset(True)
                except TypeError:
                    on_reset()
            full_content = ""
            full_thinking = ""
            tool_calls_acc = {}
            relay_timings = None
            chunk_count = 0
            thinking_chunks = 0
            content_chunks = 0
            got_done = False
            
            try:
                log_debug(f"Opening POST stream to {self._url()}")
                with self._client.stream(
                    "POST", self._url(),
                    headers=self._headers(),
                    json=payload,
                ) as resp:
                    log_debug(f"Response status: {resp.status_code}")
                    if resp.status_code != 200:
                        err = resp.read().decode("utf-8", errors="replace")
                        log_debug(f"API error response: {err[:500]}")
                        try:
                            err_json = json.loads(err)
                        except Exception:
                            err_json = None
                        ptl = _extract_prompt_too_long_from_dict(err_json, status_code=resp.status_code)
                        if ptl is not None:
                            prov, ptl_msg = ptl
                            raise PromptTooLongError(
                                f"Prompt too long for ({prov or model}): {ptl_msg}",
                                provider=prov or model,
                                raw_error=ptl_msg,
                            )
                        lim = _extract_limit_error_from_dict(err_json, status_code=resp.status_code)
                        if lim is not None:
                            prov, lim_msg = lim
                            raise ProviderLimitError(
                                f"Provider limit reached ({prov or model}): {lim_msg}",
                                provider=prov or model,
                                raw_error=lim_msg,
                            )
                        if resp.status_code == 429:
                            raise ProviderLimitError(
                                f"Provider limit reached ({model}): {err[:300]}",
                                provider=model,
                                raw_error=err,
                            )
                        raise ConnectionError(f"API error {resp.status_code}: {err}")

                    log_debug("Starting to iterate response lines...")
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            got_done = True
                            log_debug(f"Received [DONE] marker. Total chunks: {chunk_count}, thinking: {thinking_chunks}, content: {content_chunks}")
                            break
                        try:
                            chunk = json.loads(data_str)
                            chunk_count += 1
                        except json.JSONDecodeError as je:
                            log_debug(f"JSON decode error: {je}, line: {data_str[:100]}")
                            continue

                        # Usage (may appear in last chunk)
                        if isinstance(chunk.get("error"), dict):
                            err_msg = chunk["error"].get("message") or "unknown upstream error"
                            log_debug(f"Server reported stream error: {err_msg}")
                            ptl = _extract_prompt_too_long_from_dict(chunk.get("error"), status_code=0)
                            if ptl is not None:
                                prov, ptl_msg = ptl
                                raise PromptTooLongError(
                                    f"Prompt too long for ({prov or model}): {ptl_msg}",
                                    provider=prov or model,
                                    raw_error=ptl_msg,
                                )
                            lim = _extract_limit_error_from_dict(chunk.get("error"), status_code=0)
                            if lim is not None:
                                prov, lim_msg = lim
                                raise ProviderLimitError(
                                    f"Provider limit reached ({prov or model}): {lim_msg}",
                                    provider=prov or model,
                                    raw_error=lim_msg,
                                )
                            raise ConnectionError(f"API error: {err_msg}")
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                            log_debug(f"Usage received: {usage}")
                        if isinstance(chunk.get("x_upstream_model"), dict):
                            self.last_upstream_model = chunk["x_upstream_model"]
                        if isinstance(chunk.get("x_relay_timings"), dict):
                            relay_timings = dict(chunk["x_relay_timings"])
                            self.last_relay_timings = relay_timings
                        if isinstance(chunk.get("x_relay_status"), dict) and callable(on_relay_status):
                            ev = dict(chunk["x_relay_status"])
                            ev_cid = str(ev.get("client_request_id") or "").strip()
                            if not ev_cid or ev_cid == client_req_id:
                                if not ev_cid:
                                    ev["client_request_id"] = client_req_id
                                try:
                                    on_relay_status(ev)
                                except Exception:
                                    pass

                        choices = chunk.get("choices")
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = choice.get("delta", {})
                        if delta.get("reset"):
                            full_content = ""
                            if on_reset:
                                on_reset()
                            continue
                        fr = choice.get("finish_reason")
                        if fr:
                            finish_reason = fr
                            log_debug(f"Finish reason: {finish_reason}")

                        # ── Thinking delta ────────────────────────────────────
                        thinking_delta = delta.get("thinking") or ""
                        if thinking_delta:
                            thinking_chunks += 1
                            full_thinking += thinking_delta
                            if thinking_chunks % 10 == 0:  # Log every 10 thinking chunks
                                log_debug(f"Thinking chunks: {thinking_chunks}, total thinking bytes: {len(full_thinking)}")
                            if on_thinking:
                                on_thinking(thinking_delta)

                        # ── Answer text delta ─────────────────────────────────
                        content_delta = delta.get("content") or ""
                        if content_delta:
                            content_chunks += 1
                            full_content += content_delta
                            if content_chunks % 10 == 0:  # Log every 10 content chunks
                                log_debug(f"Content chunks: {content_chunks}, total content bytes: {len(full_content)}")
                            if on_token:
                                on_token(content_delta)

                        # ── Tool call deltas (OpenAI streaming format) ─────────
                        _accumulate_tool_deltas(tool_calls_acc, delta.get("tool_calls"))

                if not got_done:
                    raise RuntimeError("Upstream stream closed prematurely without [DONE] marker")
                elapsed = time.time() - start_time
                log_debug(f"Stream completed successfully in {elapsed:.2f}s. Total chunks: {chunk_count}, thinking: {thinking_chunks}, content: {content_chunks}")
                # Success — break retry loop
                break

            except KeyboardInterrupt:
                log_debug(f"Stream interrupted by user (req_id={client_req_id})")
                self.cancel_stream(
                    client_request_id=client_req_id,
                    session_id=session_id or "",
                )
                raise
            except httpx.ConnectError as ce:
                log_debug(f"Connection error: {ce}")
                log_debug(traceback.format_exc())
                raise ConnectionError(
                    f"Cannot reach WSA server at {self.api_base}.\n"
                    "Make sure wsa.ps1 or the WSA server (wsa-server) is running."
                )
            except ConnectionError:
                raise
            except httpx.ReadTimeout as te:
                elapsed = time.time() - start_time
                log_debug(f"HTTP read timeout after {elapsed:.2f}s. Chunks received: {chunk_count}, thinking: {thinking_chunks}, content: {content_chunks}")
                log_debug(f"Full exception: {traceback.format_exc()}")
                last_err = te
                if attempt < 2 and (content_chunks == 0 and not tool_calls_acc):
                    wait_s = 1.5 * (attempt + 1)
                    log_debug(f"Retrying after {wait_s}s...")
                    if on_status:
                        try:
                            on_status(f"Upstream read timeout — reconnecting (retry {attempt + 1}/2)…")
                        except Exception:
                            pass
                    try:
                        time.sleep(wait_s)
                    except KeyboardInterrupt:
                        self.cancel_stream(
                            client_request_id=client_req_id,
                            session_id=session_id or "",
                        )
                        raise
                    continue
                raise
            except Exception as e:
                elapsed = time.time() - start_time
                log_debug(f"Unexpected exception after {elapsed:.2f}s: {type(e).__name__}: {e}")
                log_debug(f"Full traceback:\n{traceback.format_exc()}")
                log_debug(f"Chunks received before failure: {chunk_count}, thinking: {thinking_chunks}, content: {content_chunks}")
                last_err = e
                # Only retry transport-level drops before final content or cleanly via idempotent client_request_id
                if attempt < 2 and (chunk_count == 0 or isinstance(e, (httpx.RemoteProtocolError, httpx.ReadError, RuntimeError))):
                    wait_s = 1.5 * (attempt + 1)
                    log_debug(f"Retrying after {wait_s}s...")
                    if on_status:
                        try:
                            on_status(f"Stream interrupted — reconnecting (retry {attempt + 1}/2)…")
                        except Exception:
                            pass
                    try:
                        time.sleep(wait_s)
                    except KeyboardInterrupt:
                        self.cancel_stream(
                            client_request_id=client_req_id,
                            session_id=session_id or "",
                        )
                        raise
                    continue
                raise

        tool_calls_list = [
            {key: value for key, value in tool_calls_acc[k].items() if key != "_wire_id"}
            for k in sorted(tool_calls_acc)
        ]
        log_debug(f"Returning response: content={len(full_content or '')} bytes, thinking={len(full_thinking or '')} bytes, tool_calls={len(tool_calls_list)}, finish_reason={finish_reason}")
        return {
            "content": full_content or None,
            "thinking": full_thinking or None,
            "tool_calls": tool_calls_list,
            "finish_reason": finish_reason,
            "usage": usage,
            "upstream_model": self.last_upstream_model,
            "relay_timings": relay_timings,
        }

    def check_connection(self) -> bool:
        """Ping /v1/models to check server is alive."""
        try:
            r = self._client.get(
                f"{self.api_base}/models",
                headers=self._headers(),
                timeout=5,
            )
            return r.status_code == 200
        except Exception:
            return False

    def get_health(self) -> dict:
        """Fetch /v1/health telemetry including last_upstream_model and account pool."""
        try:
            r = self._client.get(f"{self.api_base}/health", headers=self._headers(), timeout=5)
            return r.json() if r.status_code == 200 else {}
        except Exception:
            return {}

    def get_accounts(self) -> dict:
        """Fetch /v1/accounts pool & connected browser relay details."""
        try:
            r = self._client.get(f"{self.api_base}/accounts", headers=self._headers(), timeout=5)
            return r.json() if r.status_code == 200 else {}
        except Exception:
            return {}

    def get_relays(self) -> dict:
        """Fetch /v1/relays connected Chromium/Brave extension relay details."""
        try:
            r = self._client.get(f"{self.api_base}/relays", headers=self._headers(), timeout=5)
            return r.json() if r.status_code == 200 else {}
        except Exception:
            return {}

    def bind_relay(self, target: str, session_id: str = "") -> dict:
        """Bind a single browser relay (e.g. 'brave' or 'chrome') via POST /v1/relays/select."""
        try:
            r = self._client.post(
                f"{self.api_base}/relays/select",
                headers=self._headers(),
                json={"target": target, "session_id": session_id},
                timeout=10,
            )
            return r.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def switch_account(self, target: str, session_id: str = "") -> dict:
        """Switch active single Pro account or browser relay via POST /v1/accounts/switch."""
        try:
            r = self._client.post(
                f"{self.api_base}/accounts/switch",
                headers=self._headers(),
                json={"account": target, "session_id": session_id},
                timeout=10,
            )
            return r.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def rotate_account(self) -> dict:
        """Rotate to next Pro account via POST /v1/accounts/rotate."""
        try:
            r = self._client.post(
                f"{self.api_base}/accounts/rotate",
                headers=self._headers(),
                json={},
                timeout=10,
            )
            return r.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def cancel_stream(
        self,
        client_request_id: str = "",
        session_id: str = "",
        req_id: str = "",
    ) -> bool:
        """Best-effort cancellation of an in-flight DOM/SSE relay stream."""
        payload = {}
        if client_request_id:
            payload["client_request_id"] = str(client_request_id)
        if session_id:
            payload["session_id"] = str(session_id)
        if req_id:
            payload["req_id"] = str(req_id)
        if not payload:
            return False
        try:
            r = self._client.post(
                f"{self.api_base}/relays/cancel",
                headers=self._headers(),
                json=payload,
                timeout=1.5,
            )
            return r.status_code == 200
        except Exception:
            return False

    def clear_session_chats(
        self,
        session_id: str = "",
        providers: Optional[List[str]] = None,
        include_roles: bool = True,
        client_urls: Optional[List[str]] = None,
    ) -> dict:
        """Clear saved server chat thread affinity for *session_id* after account change."""
        payload: dict = {
            "session_id": str(session_id or ""),
            "include_roles": bool(include_roles),
        }
        if providers:
            payload["providers"] = [str(p) for p in providers if str(p or "").strip()]
        if client_urls:
            payload["client_urls"] = [str(u) for u in client_urls if str(u or "").strip()]
        try:
            r = self._client.post(
                f"{self.api_base}/relays/clear-session",
                headers=self._headers(),
                json=payload,
                timeout=2.5,
            )
            return r.json() if r.status_code == 200 else {"ok": False}
        except Exception:
            return {"ok": False}


# Backward-compatible alias for legacy callers/tests
GeminiClient = WSAClient
