/**
 * content_script_main.js — WSA — Web Browser Bridge v2.0 (MAIN world)
 *
 * Patches native XHR/fetch before Gemini loads to forward processSession tokens
 * to content_script_bridge.js via window.postMessage.
 */
(function () {
  "use strict";

  if (window.__wsaInterceptorActive) return;
  window.__wsaInterceptorActive = true;

  const PS_HOST = "geminiweb-pa.clients6.google.com";
  const PS_PATH = "/v1/processSession";

  // ── Helpers ───────────────────────────────────────────────────────────────

  function _extractHeader(headersBlock, name) {
    const lines = headersBlock.split(/\r?\n/);
    for (const line of lines) {
      const idx = line.indexOf(":");
      if (idx === -1) continue;
      if (line.slice(0, idx).trim().toLowerCase() === name.toLowerCase()) {
        return line.slice(idx + 1).trim();
      }
    }
    return "";
  }

  function _parseHttpHeaders(urlStr) {
    try {
      const u = new URL(urlStr);
      const encoded = u.searchParams.get("$httpHeaders") || "";
      return encoded ? decodeURIComponent(encoded) : "";
    } catch {
      return "";
    }
  }

  function _extractUrlParam(urlStr, name) {
    try {
      return new URL(urlStr).searchParams.get(name) || "";
    } catch {
      return "";
    }
  }

  function _shouldCapture(urlStr, bodyStr) {
    if (!urlStr.includes(PS_HOST) || !urlStr.includes(PS_PATH)) return false;
    if (!bodyStr.includes("req0___data__")) return false;
    if (!bodyStr.includes("ofs=0")) return false;
    return true;
  }

  function _headersToObj(block) {
    const obj = {};
    for (const line of block.split(/\r?\n/)) {
      const idx = line.indexOf(":");
      if (idx === -1) continue;
      const k = line.slice(0, idx).trim();
      const v = line.slice(idx + 1).trim();
      if (k) obj[k] = v;
    }
    return obj;
  }

  function _buildCapture(urlStr, bodyStr) {
    const httpHeaders = _parseHttpHeaders(urlStr);
    const serverToken = _extractHeader(httpHeaders, "X-Server-Token");
    const sapisidHash = _extractHeader(httpHeaders, "Authorization");
    const xExtBin = _extractHeader(httpHeaders, "x-goog-ext-525001261-bin");
    const gsessionid = _extractUrlParam(urlStr, "gsessionid");
    const sid = _extractUrlParam(urlStr, "SID");
    const rid = _extractUrlParam(urlStr, "RID");

    return {
      type: "WSA_PS_CAPTURE",          // postMessage type (not chrome.runtime)
      url: urlStr,
      rid,
      gsessionid,
      sid,
      body: bodyStr,
      server_token: serverToken,
      sapisid_hash: sapisidHash,
      x_ext_bin: xExtBin,
      headers: _headersToObj(httpHeaders),
      captured_at: Date.now(),
    };
  }

  function _relay(capture) {
    // Send to ISOLATED world bridge via postMessage (the bridge script can
    // access chrome.runtime and will forward to the service worker).
    window.postMessage(capture, "*");

    console.log(
      "%c[WSA] ✓ processSession captured! (relaying to bridge...)",
      "color:#2a7a3b;font-weight:bold",
      `RID=${capture.rid}`,
      `token_len=${capture.server_token.length}`
    );
  }

  // ── XHR Interceptor (PRIMARY – BrowserChannel uses XHR) ──────────────────

  const _origOpen = XMLHttpRequest.prototype.open;
  const _origSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__wsa_url = typeof url === "string" ? url : (url?.toString() || "");
    this.__wsa_method = (method || "").toUpperCase();
    return _origOpen.apply(this, [method, url, ...rest]);
  };

  XMLHttpRequest.prototype.send = function (body) {
    const urlStr = this.__wsa_url || "";
    const method = this.__wsa_method || "";

    if (method === "POST" && urlStr.includes(PS_HOST) && urlStr.includes(PS_PATH)) {
      let bodyStr = "";
      if (typeof body === "string") {
        bodyStr = body;
      } else if (body instanceof ArrayBuffer || ArrayBuffer.isView(body)) {
        try { bodyStr = new TextDecoder().decode(body); } catch { bodyStr = ""; }
      }

      if (_shouldCapture(urlStr, bodyStr)) {
        const capture = _buildCapture(urlStr, bodyStr);
        if (capture.server_token) {
          _relay(capture);
        } else {
          console.debug("[WSA] processSession POST found but no X-Server-Token (skipping)");
        }
      }
    }

    return _origSend.apply(this, [body]);
  };

  // ── Fetch Interceptor (FALLBACK) ──────────────────────────────────────────

  const _origFetch = window.fetch;

  window.fetch = async function (...args) {
    const input = args[0];
    const init = args[1] || {};
    const urlStr = typeof input === "string" ? input : (input?.url || "");

    if (
      (init.method === "POST" || !init.method) &&
      urlStr.includes(PS_HOST) &&
      urlStr.includes(PS_PATH)
    ) {
      try {
        let bodyStr = "";
        if (init.body) {
          if (typeof init.body === "string") {
            bodyStr = init.body;
          } else if (init.body instanceof ArrayBuffer || ArrayBuffer.isView(init.body)) {
            bodyStr = new TextDecoder().decode(init.body);
          } else {
            try { bodyStr = await new Response(init.body).text(); } catch { bodyStr = ""; }
          }
        }
        if (_shouldCapture(urlStr, bodyStr)) {
          const capture = _buildCapture(urlStr, bodyStr);
          if (capture.server_token) _relay(capture);
        }
      } catch (e) {
        console.warn("[WSA] fetch intercept error:", e);
      }
    }

    return _origFetch.apply(this, args);
  };

  console.log(
    "%c[WSA] MAIN world interceptor active (XHR + fetch)",
    "color:#1769e0;font-weight:bold"
  );
})();

