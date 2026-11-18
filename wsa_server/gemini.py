"""Direct Google Gemini upstream StreamGenerate client, BL/XSRF sync, and SSE decoder."""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Optional

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    httpx = None
    HAS_HTTPX = False

from .accounts import (
    _is_auto_rotate_allowed,
    account_prefix,
    apply_chat_persistence_flags,
    load_cookie,
    make_sapisidhash,
    update_rotating_cookies_from_headers,
)
from .config import CONFIG, _CONFIG_LOCK, _resolve_local_path, log
from .models import (
    LAST_UPSTREAM_MODEL,
    MODE_CATEGORY_NAMES,
    MODELS,
    THINKING_LEVEL_NAMES,
    WEB_BRIDGE_MODES,
    _normalize_model_cfg,
    extract_upstream_model_info,
    update_last_upstream_model,
)
from .parser import clean_gemini_text, extract_response_text, extract_thinking_text
from .protobuf import build_argon_headers, build_argon_jspb_headers
from .relay import (
    _compute_thinking_delta,
    argon_strict_relay_required,
    dom_relay_iter,
    dom_relay_put_prompt,
    is_dom_relay_active,
    rotate_account,
)


class _UpstreamRejectedError(RuntimeError):
    """Non-retriable Gemini upstream rejection (e.g. BardErrorInfo)."""


def _bridge_provider_for_cfg(model_cfg: dict) -> str:
    """Resolve the web bridge provider name from model_cfg."""
    explicit = str(model_cfg.get("provider") or "").strip().lower()
    if explicit:
        return explicit
    return {
        -1: "gemini",
        -2: "claude",
        -3: "chatgpt",
        -4: "deepseek",
        -5: "venice",
    }.get(model_cfg.get("mode"), "gemini")

def fetch_latest_bl() -> Optional[str]:
    """Fetch latest gemini_bl (authenticated with cookie so Pro uiserver is preserved)."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        cookie_str, _ = load_cookie()
        if cookie_str:
            headers["Cookie"] = cookie_str
        req = urllib.request.Request("https://gemini.google.com/app", headers=headers)
        ctx = ssl.create_default_context()
        proxy = CONFIG.get("proxy")
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                urllib.request.HTTPSHandler(context=ctx),
            )
            resp = opener.open(req, timeout=15)
        else:
            resp = urllib.request.urlopen(req, context=ctx, timeout=15)
        update_rotating_cookies_from_headers(getattr(resp, "headers", None))
        html = resp.read().decode("utf-8", errors="replace")
        m = re.search(r"(boq_gemini-web-uiserver_\d+\.\d+_p\d+)", html)
        if m:
            return m.group(1)
        m = re.search(r"(boq_assistant-bard-web-server_\d+\.\d+_p\d+)", html)
        if m:
            return m.group(1)
    except Exception as e:
        log(f"BL auto-update fetch failed: {e}")
    return None


def update_bl_if_needed() -> bool:
    """Update gemini_bl while blocking downgrade from uiserver to anonymous bard server."""
    new_bl = fetch_latest_bl()
    if not new_bl:
        return False
    with _CONFIG_LOCK:
        current_bl = CONFIG["gemini_bl"]
        if current_bl.startswith("boq_gemini-web-uiserver_") and new_bl.startswith("boq_assistant-bard-web-server_"):
            log(f"BL downgrade blocked: keeping {current_bl} (rejected {new_bl})")
            return False
        if new_bl != current_bl:
            log(f"BL auto-updated: {current_bl} -> {new_bl}")
            CONFIG["gemini_bl"] = new_bl
            return True
    return False


def fetch_xsrf_with_cookie() -> Optional[str]:
    """Fetch a fresh XSRF token from Gemini. Returns the token, or None."""
    cookie_str, _ = load_cookie()
    if not cookie_str:
        return None
    ctx = ssl.create_default_context()
    proxy = CONFIG.get("proxy")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie_str,
    }
    req = urllib.request.Request("https://gemini.google.com/app", headers=headers)
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=ctx),
        )
        resp = opener.open(req, timeout=15)
    else:
        resp = urllib.request.urlopen(req, context=ctx, timeout=15)
    update_rotating_cookies_from_headers(getattr(resp, "headers", None))
    html = resp.read().decode("utf-8", errors="replace")
    match = re.search(r'"SNlM0e"\s*:\s*"([^"]+)"', html) or re.search(
        r'["\']at["\']\s*:\s*["\']([A-Za-z0-9_\-:]+)["\']', html
    )
    return match.group(1) if match else None


def refresh_xsrf() -> bool:
    """Auto-refresh XSRF token using stored cookie and update CONFIG + auth/config files."""
    try:
        new_token = fetch_xsrf_with_cookie()
    except Exception as e:
        log(f"XSRF refresh failed: {e}")
        return False
    if not new_token:
        return False
    with _CONFIG_LOCK:
        CONFIG["xsrf_token"] = new_token
        cookie_file = _resolve_local_path(CONFIG.get("cookie_file"))
        if cookie_file and os.path.exists(cookie_file):
            try:
                with open(cookie_file, "r", encoding="utf-8") as f:
                    auth_data = json.load(f)
                if isinstance(auth_data, dict):
                    auth_data["xsrf_token"] = new_token
                    with open(cookie_file, "w", encoding="utf-8") as f:
                        json.dump(auth_data, f, indent=2)
            except Exception:
                pass
    return True


def _build_upstream_request(prompt: str, model_cfg: dict, file_refs: list = None):
    actual_model_id = model_cfg.get("mode", 8)
    actual_think_mode = model_cfg.get("think", 0)
    slot80 = model_cfg.get("slot80")
    model_hex = model_cfg.get("hex") or (
        MODELS["gemini-4-argon"]["hex"] if actual_model_id in (7, 8) else None
    )
    req_uuid = str(uuid.uuid4()).upper()
    use_inner2026 = bool(model_cfg.get("inner2026"))

    if use_inner2026:
        inner = [None] * 100
    else:
        inner = [None] * (81 if slot80 is not None else 80)
    if file_refs:
        refs = [[None, None, ref] for ref in file_refs]
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [1] if use_inner2026 else [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[actual_think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4, 16, 19, 12] if use_inner2026 else [4]
    apply_chat_persistence_flags(inner)
    if use_inner2026:
        inner[41] = [1]
        inner[49] = 0
    inner[53] = 0
    inner[59] = req_uuid
    inner[61] = []
    if use_inner2026:
        inner[67] = 0
        inner[68] = 2
        inner[91] = 1
        inner[96] = 1
        inner[98] = 1
    else:
        inner[68] = 1
    inner[79] = actual_model_id
    if slot80 is not None:
        inner[80] = slot80

    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}

    with _CONFIG_LOCK:
        xsrf_token = CONFIG.get("xsrf_token")
        gemini_bl = CONFIG["gemini_bl"]
        auth_user = CONFIG.get("auth_user")

    if xsrf_token:
        params["at"] = xsrf_token

    reqid = int(time.time()) % 1000000
    prefix = account_prefix()
    url = (
        f"https://gemini.google.com{prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={gemini_bl}&hl=en&_reqid={reqid}&rt=c"
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    cookie_str, sapisid = load_cookie()
    if cookie_str or prefix:
        headers["X-Goog-AuthUser"] = str(auth_user) if auth_user not in (None, "") else "0"
    if model_hex:
        header_cfg = {
            "model_hex": model_hex,
            "gemini_bl": gemini_bl,
            "mode": actual_model_id,
            "slot80": slot80 if slot80 is not None else 2,
            "req_uuid": req_uuid,
            "session_uuid": req_uuid,
            "inner2026": use_inner2026,
            "field25": model_cfg.get("field25"),
        }
        headers.update(build_argon_headers(header_cfg))
        if actual_model_id != 7 and not use_inner2026:
            headers.update(build_argon_jspb_headers(header_cfg))
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    return url, params, headers


def _build_synthetic_wrb_frame(response_text: str, thinking_text: str = "", model_badge: str = "4 Argon") -> str:
    """Wrap DOM relay output into a valid wrb.fr string so extract_response_text/extract_thinking_text work."""
    cand = [None] * 38
    cand[0] = f"rc_{uuid.uuid4().hex[:12]}"
    cand[1] = [response_text]
    if thinking_text:
        cand[37] = [[[thinking_text]]]
    inner = [None] * 60
    inner[4] = [cand]
    inner[39] = MODELS["gemini-4-argon"]["hex"]
    inner[42] = model_badge
    inner[46] = 2
    inner[58] = 8
    line = json.dumps([["wrb.fr", None, json.dumps(inner)]])
    if len(line) < 160:
        line += " " * (160 - len(line))
    return line


def _read_stream_response(resp) -> str:
    """Read StreamGenerate response and return immediately once af.httprm arrives."""
    import http.client
    if not isinstance(resp, http.client.HTTPResponse):
        return resp.read().decode("utf-8", errors="replace")
    lines = []
    while True:
        line = resp.readline()
        if not line:
            break
        s = line.decode("utf-8", errors="replace")
        lines.append(s)
        if '"af.httprm"' in s:
            break
    try:
        resp.close()
    except Exception:
        pass
    return "".join(lines)


def gemini_stream_generate(
    prompt: str,
    model_id: int = None,
    think_mode: int = 4,
    file_refs: list = None,
    model_cfg: dict = None,
) -> str:
    model_cfg = _normalize_model_cfg(model_id=model_id, think_mode=think_mode, model_cfg=model_cfg)

    with _CONFIG_LOCK:
        retry_attempts = CONFIG["retry_attempts"]
        retry_delay_sec = CONFIG["retry_delay_sec"]
        request_timeout_sec = CONFIG["request_timeout_sec"]
        proxy = CONFIG.get("proxy")

    is_web_bridge = model_cfg.get("mode") in WEB_BRIDGE_MODES
    bridge_provider = _bridge_provider_for_cfg(model_cfg)
    is_argon = model_cfg.get("mode") in (7, 8)
    strict_argon = is_argon and argon_strict_relay_required()
    relay_timeout = max(float(request_timeout_sec or 180), 300.0)
    if is_web_bridge or strict_argon or (is_argon and is_dom_relay_active()):
        try:
            relay_res = dom_relay_put_prompt(prompt, timeout=relay_timeout, provider=bridge_provider)
            return _build_synthetic_wrb_frame(
                relay_res.get("response", ""),
                relay_res.get("thinking", ""),
                relay_res.get("model", ""),
            )
        except Exception as exc:
            if is_web_bridge or strict_argon:
                raise
            log(f"DOM relay failed ({exc}); falling back to direct StreamGenerate...")

    with _CONFIG_LOCK:
        active_cfile = _resolve_local_path(CONFIG.get("cookie_file"))
    if is_argon and active_cfile and os.path.exists(active_cfile):
        log("argon_allow_pro_fallback=true: serving 3.1 Pro High (mode=3) instead of 4 Argon")
        slot_override = model_cfg.get("slot80", 2)
        model_cfg = dict(MODELS["gemini-3.1-pro"])
        model_cfg["slot80"] = slot_override

    attempt_timeout = request_timeout_sec

    last_err = None
    last_http_status = 0
    for attempt in range(retry_attempts):
        url, params, headers = _build_upstream_request(prompt, model_cfg, file_refs)
        body = urllib.parse.urlencode(params).encode()
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            ctx = ssl.create_default_context()
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx),
                )
                resp = opener.open(req, timeout=attempt_timeout)
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=attempt_timeout)
            update_rotating_cookies_from_headers(getattr(resp, "headers", None))
            raw_out = _read_stream_response(resp)
            err_m = re.search(r'\[\["wrb\.fr",null,null,null,null,\[(\d+)\]\]\]', raw_out)
            if err_m and not extract_response_text(raw_out):
                raise RuntimeError(
                    f"Gemini upstream returned error frame [{err_m.group(1)}] with no answer "
                    "(request was queued/stalled upstream)"
                )
            info = update_last_upstream_model(raw_out)
            if bool(model_cfg.get("hex")) and info.get("is_downgraded_flash_lite"):
                if attempt < retry_attempts - 1:
                    log(
                        f"Upstream downgraded request to {info.get('badge')} (mode={info.get('mode')}); "
                        "switching to 3.1 Pro High (mode=3, slot80=2) to prevent Flash-Lite downgrade"
                    )
                    if _is_auto_rotate_allowed():
                        rotate_account("silent Flash-Lite downgrade")
                    update_bl_if_needed()
                    if CONFIG.get("cookie_file"):
                        refresh_xsrf()
                    model_cfg = dict(MODELS["gemini-3.1-pro"])
                    continue
                elif not model_cfg.get("inner2026"):
                    log("Final anti-downgrade fallback: executing 3.1 Pro High (mode=3, slot80=2) on StreamGenerate")
                    pro_cfg = dict(MODELS["gemini-3.1-pro"])
                    url2, params2, headers2 = _build_upstream_request(prompt, pro_cfg, file_refs)
                    req2 = urllib.request.Request(url2, data=urllib.parse.urlencode(params2).encode(), headers=headers2, method="POST")
                    resp2 = opener.open(req2, timeout=38.0) if proxy else urllib.request.urlopen(req2, context=ctx, timeout=38.0)
                    update_rotating_cookies_from_headers(getattr(resp2, "headers", None))
                    raw_out2 = _read_stream_response(resp2)
                    update_last_upstream_model(raw_out2)
                    return raw_out2
            return raw_out
        except urllib.error.HTTPError as e:
            last_http_status = int(getattr(e, "code", 0) or 0)
            if e.code == 405 and update_bl_if_needed():
                last_err = e
                continue
            if e.code == 429 and _is_auto_rotate_allowed() and rotate_account("HTTP 429"):
                last_err = e
                continue
            if e.code in (400, 401, 403) and CONFIG.get("cookie_file") and refresh_xsrf():
                last_err = e
                continue
            last_err = e
            if attempt < retry_attempts - 1:
                time.sleep(retry_delay_sec)
        except Exception as e:
            last_http_status = 0
            last_err = e
            if attempt < retry_attempts - 1:
                time.sleep(retry_delay_sec)
    if last_http_status == 429:
        from .relay import ProviderLimitError

        raise ProviderLimitError(f"Gemini HTTP 429 Too Many Requests: {last_err}", provider="gemini")
    raise last_err


def gemini_stream_generate_iter(
    prompt: str,
    model_id: int = None,
    think_mode: int = 4,
    file_refs: list = None,
    model_cfg: dict = None,
):
    for ev_type, delta in gemini_stream_generate_events(
        prompt, model_id=model_id, think_mode=think_mode, file_refs=file_refs, model_cfg=model_cfg
    ):
        if ev_type == "content":
            yield delta


def gemini_stream_generate_events(
    prompt: str,
    model_id: int = None,
    think_mode: int = 4,
    file_refs: list = None,
    model_cfg: dict = None,
):
    """Dual-stream generator yielding ('thinking', delta_str) and ('content', delta_str)."""
    model_cfg = _normalize_model_cfg(model_id=model_id, think_mode=think_mode, model_cfg=model_cfg)

    with _CONFIG_LOCK:
        proxy = CONFIG.get("proxy")
        req_timeout = float(CONFIG.get("request_timeout_sec") or 180.0)

    is_web_bridge = model_cfg.get("mode") in WEB_BRIDGE_MODES
    bridge_provider = _bridge_provider_for_cfg(model_cfg)
    is_argon = model_cfg.get("mode") in (7, 8)
    strict_argon = is_argon and argon_strict_relay_required()
    if is_web_bridge or strict_argon or (is_argon and is_dom_relay_active()):
        relay_res = None
        emitted_thinking = ""
        emitted_content = ""
        try:
            for kind, payload in dom_relay_iter(prompt, timeout=max(req_timeout, 300.0), provider=bridge_provider):
                if kind == "keepalive":
                    yield ("keepalive", "")
                elif kind == "relay_status":
                    yield ("relay_status", payload)
                elif kind == "thinking_delta":
                    if payload:
                        emitted_thinking += payload
                        yield ("thinking", payload)
                elif kind == "content_delta":
                    if payload:
                        emitted_content += payload
                        yield ("content", payload)
                elif kind == "reset_content":
                    emitted_content = ""
                    yield ("reset_content", "")
                elif kind == "result":
                    relay_res = payload
        except Exception as exc:
            if is_web_bridge or strict_argon:
                raise
            if emitted_content:
                yield ("reset_content", "")
            log(f"DOM relay stream failed ({exc}); falling back to StreamGenerate...")
        if relay_res is not None:
            thinking = relay_res.get("thinking") or ""
            text = clean_gemini_text(relay_res.get("response") or "", strip=False)
            if thinking:
                t_tail, _ = _compute_thinking_delta(emitted_thinking, thinking, provider=bridge_provider)
                if t_tail:
                    yield ("thinking", t_tail)
            if text:
                if text.startswith(emitted_content):
                    c_tail = text[len(emitted_content):]
                    if c_tail:
                        yield ("content", c_tail)
                else:
                    yield ("reset_content", "")
                    yield ("content", text)
            return

    with _CONFIG_LOCK:
        active_cfile = _resolve_local_path(CONFIG.get("cookie_file"))
    if model_cfg.get("mode") in (7, 8) and active_cfile and os.path.exists(active_cfile):
        log("argon_allow_pro_fallback=true: serving 3.1 Pro High (mode=3) instead of 4 Argon")
        slot_override = model_cfg.get("slot80", 2)
        model_cfg = dict(MODELS["gemini-3.1-pro"])
        model_cfg["slot80"] = slot_override

    if not HAS_HTTPX:
        raw = gemini_stream_generate(prompt, model_cfg=model_cfg, file_refs=file_refs)
        thinking = extract_thinking_text(raw)
        text = extract_response_text(raw)
        if thinking:
            yield ("thinking", thinking)
        if text:
            yield ("content", text)
        return

    url, params, headers = _build_upstream_request(prompt, model_cfg, file_refs)
    body = urllib.parse.urlencode(params)
    prev_text = ""
    prev_thinking = ""
    expects_thinking = bool(model_cfg.get("hex"))
    upstream_verified_tier4 = False
    upstream_downgraded = False
    buffered_unverified_content = []
    transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
    read_timeout = max(req_timeout, 180.0)
    stream_timeout = httpx.Timeout(connect=10.0, read=read_timeout, write=30.0, pool=10.0)

    with httpx.Client(transport=transport, timeout=stream_timeout, verify=True) as client:
        try:
            with client.stream("POST", url, content=body, headers=headers) as resp:
                resp.raise_for_status()
                update_rotating_cookies_from_headers(getattr(resp, "headers", None))
                buf = ""
                saw_httprm = False
                for chunk in resp.iter_text():
                    buf += chunk
                    if "BardErrorInfo" in buf:
                        m = re.search(r"BardErrorInfo\s*\[(\d+)\]", buf)
                        if m:
                            raise _UpstreamRejectedError(
                                f"Gemini upstream rejected request: BardErrorInfo [{m.group(1)}]"
                            )
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        if '"af.httprm"' in line:
                            saw_httprm = True
                        if '"wrb.fr"' not in line or len(line) < 100:
                            continue
                        try:
                            arr = json.loads(line)
                            inner_str = arr[0][2]
                            if not inner_str or len(inner_str) < 50:
                                continue
                            inner2 = json.loads(inner_str)
                            if not isinstance(inner2, list) or len(inner2) < 5 or not inner2[4]:
                                continue
                            cand = inner2[4][0] if isinstance(inner2[4], list) and inner2[4] else None
                            if not cand:
                                continue

                            frame_info = extract_upstream_model_info(inner2)
                            if frame_info.get("badge") or frame_info.get("mode") is not None:
                                update_last_upstream_model(inner2)
                                if frame_info.get("is_tier4_high") or (
                                    not frame_info.get("is_downgraded_flash_lite")
                                    and frame_info.get("mode") == 3
                                ):
                                    upstream_verified_tier4 = True
                                    if buffered_unverified_content:
                                        for buffered_delta in buffered_unverified_content:
                                            yield ("content", buffered_delta)
                                        buffered_unverified_content.clear()
                                elif frame_info.get("is_downgraded_flash_lite"):
                                    upstream_downgraded = True

                            curr_thinking = extract_thinking_text(cand)
                            if curr_thinking and len(curr_thinking) > len(prev_thinking):
                                delta = curr_thinking[len(prev_thinking):]
                                if delta:
                                    yield ("thinking", delta)
                                prev_thinking = curr_thinking
                                update_last_upstream_model(inner2, thinking_chars=len(prev_thinking))
                                if buffered_unverified_content:
                                    for buffered_delta in buffered_unverified_content:
                                        yield ("content", buffered_delta)
                                    buffered_unverified_content.clear()

                            for part in inner2[4]:
                                if isinstance(part, list) and len(part) > 1 and isinstance(part[1], list):
                                    for t in part[1]:
                                        if isinstance(t, str) and len(t) > len(prev_text):
                                            delta = clean_gemini_text(t[len(prev_text):], strip=False)
                                            if delta:
                                                if expects_thinking and not prev_thinking and not upstream_verified_tier4:
                                                    buffered_unverified_content.append(delta)
                                                else:
                                                    yield ("content", delta)
                                            prev_text = t
                        except (json.JSONDecodeError, IndexError, TypeError):
                            pass
                    if saw_httprm or '"af.httprm"' in buf:
                        break
        except _UpstreamRejectedError:
            raise
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", 0)
            if status == 405:
                update_bl_if_needed()
            elif status == 429 and _is_auto_rotate_allowed():
                rotate_account("HTTP 429 stream")
            elif status in (400, 401, 403) and CONFIG.get("cookie_file"):
                refresh_xsrf()
            # Recover a dropped HTTP 200 stream, including after partial output.
            # The non-streaming response is deduplicated against already yielded text.
            log(f"Stream interrupted ({type(e).__name__}: {e}) — recovering via retry generator...")
            raw = gemini_stream_generate(prompt, model_cfg=model_cfg, file_refs=file_refs)
            thinking = extract_thinking_text(raw)
            text = extract_response_text(raw)
            if thinking and len(thinking) > len(prev_thinking):
                thinking_delta = (
                    thinking[len(prev_thinking):]
                    if thinking.startswith(prev_thinking)
                    else thinking
                )
                yield ("thinking", thinking_delta)
            if text:
                if prev_text and not buffered_unverified_content and text.startswith(prev_text):
                    text_delta = text[len(prev_text):]
                else:
                    if prev_text and not buffered_unverified_content:
                        yield ("reset_content", "")
                    buffered_unverified_content.clear()
                    text_delta = text
                if text_delta:
                    yield ("content", clean_gemini_text(text_delta, strip=False))
            return
    if expects_thinking and upstream_downgraded and not prev_thinking and not model_cfg.get("_retried_downgrade"):
        log("StreamGenerate downgraded mode=8 to 3.5 Flash-Lite (mode=6); falling back to real 3.1 Pro High (mode=3, slot80=2).")
        alternate_cfg = dict(MODELS["gemini-3.1-pro"])
        alternate_cfg["_retried_downgrade"] = True
        try:
            raw = gemini_stream_generate(prompt, model_cfg=alternate_cfg, file_refs=file_refs)
            retry_info = update_last_upstream_model(raw)
            thinking = extract_thinking_text(raw)
            text = extract_response_text(raw)
            if thinking or retry_info.get("is_tier4_high") or not retry_info.get("is_downgraded_flash_lite"):
                buffered_unverified_content.clear()
                if thinking:
                    yield ("thinking", thinking)
                if text:
                    yield ("content", clean_gemini_text(text, strip=False))
                return
        except Exception as exc:
            log(f"Argon -> 3.1 Pro High downgrade recovery failed: {exc}")
        yield (
            "thinking",
            "[UPSTREAM DOWNGRADE WARNING: Google returned 3.5 Flash-Lite (mode=6) instead of Tier-4 Argon. "
            "Open https://gemini.google.com/app, https://claude.ai, https://chatgpt.com, https://chat.deepseek.com, "
            "or https://venice.ai/chat/agent in Chrome/Brave with the 'WSA — Web Browser Bridge' extension active, or click Sync / refresh wsa-auth.json.]\n",
        )
    if buffered_unverified_content:
        for buffered_delta in buffered_unverified_content:
            yield ("content", buffered_delta)
