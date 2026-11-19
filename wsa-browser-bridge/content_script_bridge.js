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

  const WAIT_MODAL_BTN_SELECTOR =
    "[role='dialog'] button, [role='alertdialog'] button, mat-dialog-container button, .cdk-overlay-container button, dialog button, .modal button, .mdc-dialog button";
  const WAIT_MODAL_CONTAINER_SELECTOR =
    "[role='dialog'], [role='alertdialog'], mat-dialog-container, dialog, .cdk-overlay-pane, .modal, .mdc-dialog";
  const WAIT_BTN_TEXT_RE = /^(?:wait|keep waiting|wait for page|continue waiting)$/i;
  let lastWaitDismissCheckAt = 0;
  let waitCheckTimer = null;

  function autoDismissWaitPrompts() {
    try {
      const now = Date.now();
      if ((now - lastWaitDismissCheckAt) < 1200) return;
      lastWaitDismissCheckAt = now;
      const candidates = document.querySelectorAll(WAIT_MODAL_BTN_SELECTOR);
      for (const b of candidates) {
        const txt = (b.textContent || "").replace(/\s+/g, " ").trim();
        if (WAIT_BTN_TEXT_RE.test(txt)) {
          b.click();
          console.log("[WSA bridge] Auto-clicked 'Wait' prompt on page.");
          break;
        }
      }
    } catch { /* ignore */ }
  }

  function scheduleWaitDismissCheck() {
    if (waitCheckTimer !== null) return;
    waitCheckTimer = setTimeout(() => {
      waitCheckTimer = null;
      autoDismissWaitPrompts();
    }, 1500);
  }

  // Observe DOM ONLY for added element nodes (never streaming text chunks) to catch unresponsive modals
  try {
    const waitObserver = new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (m.addedNodes && m.addedNodes.length) {
          for (const n of m.addedNodes) {
            if (n.nodeType === 1) {
              scheduleWaitDismissCheck();
              return;
            }
          }
        }
      }
    });
    const startWaitObserver = () => {
      if (document.body) waitObserver.observe(document.body, { childList: true, subtree: true });
    };
    if (document.body) {
      startWaitObserver();
    } else {
      document.addEventListener("DOMContentLoaded", startWaitObserver, { once: true });
    }
  } catch { /* ignore */ }

  setInterval(() => {
    try {
      if (document.querySelector(WAIT_MODAL_CONTAINER_SELECTOR)) {
        autoDismissWaitPrompts();
      }
    } catch { /* ignore */ }
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
