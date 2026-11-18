/**
 * content_script_bridge.js — WSA Browser Bridge v2.0 (ISOLATED world)
 *
 * Forwards processSession captures from the MAIN world interceptor on Gemini Web
 * and maintains the MV3 keepalive channel for all four supported AI providers.
 */
(function () {
  "use strict";

  window.addEventListener("message", function (event) {
    if (event.source !== window) return;

    const msg = event.data;
    if (!msg || (msg.type !== "WSA_PS_CAPTURE" && msg.type !== "GCS_PS_CAPTURE")) return;

    chrome.runtime.sendMessage(
      {
        type: "PS_CAPTURE",
        url: msg.url,
        rid: msg.rid,
        gsessionid: msg.gsessionid,
        sid: msg.sid,
        body: msg.body,
        server_token: msg.server_token,
        sapisid_hash: msg.sapisid_hash,
        x_ext_bin: msg.x_ext_bin,
        headers: msg.headers,
        captured_at: msg.captured_at,
      },
      function (response) {
        if (chrome.runtime.lastError) {
          console.warn(
            "[WSA bridge] sendMessage error:",
            chrome.runtime.lastError.message
          );
          return;
        }
        if (response && response.ok) {
          console.log(
            "%c[WSA bridge] Capture forwarded to service worker",
            "color:#2a7a3b;font-weight:bold"
          );
        }
      }
    );
  });

  // Tab pings are supplementary: the worker also uses alarms and durable jobs,
  // because hidden-page timers can be throttled or suspended by the browser.
  let keepalivePort = null;
  let lastKeepalivePingAt = 0;
  let detectedIsBrave = null;

  async function resolveIsBrave() {
    if (detectedIsBrave !== null) return detectedIsBrave;
    try {
      if (navigator.brave && typeof navigator.brave.isBrave === "function") {
        detectedIsBrave = Boolean(await navigator.brave.isBrave());
        return detectedIsBrave;
      }
    } catch { /* ignore */ }
    detectedIsBrave = /Brave/i.test(navigator.userAgent || "");
    return detectedIsBrave;
  }

  async function sendTabKeepalive(force = false) {
    const now = Date.now();
    if (!force && (now - lastKeepalivePingAt) < 4000) return;
    lastKeepalivePingAt = now;
    const isBrave = await resolveIsBrave();
    try {
      chrome.runtime.sendMessage(
        { type: "TAB_KEEPALIVE", isBrave, href: location.href },
        () => { void chrome.runtime.lastError; }
      );
    } catch { /* ignore */ }
  }

  function connectKeepalive() {
    try {
      keepalivePort = chrome.runtime.connect({ name: "wsa-tab-keepalive" });
      keepalivePort.onDisconnect.addListener(() => {
        keepalivePort = null;
      });
      sendTabKeepalive(true);
    } catch {
      keepalivePort = null;
    }
  }

  resolveIsBrave().finally(() => {
    connectKeepalive();
  });

  function autoDismissWaitPrompts() {
    try {
      const candidates = document.querySelectorAll(
        "button, [role='button'], .mat-mdc-button-base, [role='dialog'] button, mat-dialog-container button, .modal button"
      );
      for (const b of candidates) {
        const txt = (b.innerText || b.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
        if (txt === "wait" || txt === "keep waiting" || txt === "wait for page" || txt === "continue waiting") {
          b.click();
          console.log("[WSA bridge] Auto-clicked 'Wait' prompt on page.");
          break;
        }
      }
    } catch { /* ignore */ }
  }

  // Observe DOM for any unresponsive/wait modals appearing dynamically
  try {
    const waitObserver = new MutationObserver(() => {
      autoDismissWaitPrompts();
    });
    if (document.body) {
      waitObserver.observe(document.body, { childList: true, subtree: true });
    } else {
      document.addEventListener("DOMContentLoaded", () => {
        if (document.body) waitObserver.observe(document.body, { childList: true, subtree: true });
      });
    }
  } catch { /* ignore */ }

  setInterval(() => {
    autoDismissWaitPrompts();
    if (!keepalivePort) {
      connectKeepalive();
    } else {
      resolveIsBrave().then((isBrave) => {
        try {
          keepalivePort.postMessage({ type: "PING", ts: Date.now(), isBrave, href: location.href });
        } catch {
          keepalivePort = null;
          connectKeepalive();
        }
      });
    }
  }, 3000);

  document.addEventListener("visibilitychange", () => {
    // Wake the worker immediately when switching AWAY as well as returning.
    if (!keepalivePort) connectKeepalive();
    else sendTabKeepalive(true);
  });
  window.addEventListener("focus", () => {
    if (!keepalivePort) connectKeepalive();
  });

  console.log("[WSA bridge] ISOLATED world bridge active");
})();
