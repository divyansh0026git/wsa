/**
 * popup.js — WSA Web Browser Bridge v2.0
 * Engineered by Divyansh Saraf
 */

const $ = (id) => document.getElementById(id);

const PROVIDER_META = [
  { key: "claude", label: "Claude", host: "claude.ai" },
  { key: "deepseek", label: "DeepSeek", host: "chat.deepseek.com" },
  { key: "gemini", label: "Gemini", host: "gemini.google.com" },
  { key: "chatgpt", label: "ChatGPT", host: "chatgpt.com" },
  { key: "venice", label: "Venice", host: "venice.ai" },
];

let detectedIsBrave = null;
let statusPollId = null;
let tickerIntervalId = null;
let lastAutoSyncTimestamp = 0;

async function detectIsBrave() {
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

try {
  const port = chrome.runtime.connect({ name: "relay-keepalive" });
  port.onDisconnect.addListener(() => {
    const _err = (typeof chrome !== "undefined" && chrome.runtime ? chrome.runtime.lastError : null) || port.error;
    void _err;
  });
} catch { /* ignore */ }

function setOutput(message, kind = "") {
  const out = $("output");
  if (!out) return;
  out.textContent = message || "";
  out.className = kind ? `output ${kind}` : "output";
}

function formatBrowserName(browser) {
  const b = String(browser || "chrome").toLowerCase();
  if (b === "brave") return "Brave";
  if (b === "edge") return "Edge";
  return "Chrome";
}

function formatRelativeTime(ts) {
  if (!ts || typeof ts !== "number" || ts <= 0) return "Standby";
  const diffSec = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (diffSec < 4) return "Just now";
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHours = Math.floor(diffMin / 60);
  return `${diffHours}h ago`;
}

function updateRelativeTimeDisplay() {
  const timeEl = $("autoSyncTime");
  if (timeEl && lastAutoSyncTimestamp > 0) {
    timeEl.textContent = formatRelativeTime(lastAutoSyncTimestamp);
  }
}

function applyStatusUI(res) {
  if (!res) return;

  const browserDisplay = formatBrowserName(res.browser);
  const providers = res.providers || {};
  const activeProvider = res.provider || "";
  const srvReachable = Boolean(res.serverReachable);
  const isBound = srvReachable && res.serverBinding?.isActive === true;

  let openCount = 0;
  for (const meta of PROVIDER_META) {
    const info = providers[meta.key] || {};
    const hasTab = Boolean(info.has_tab);
    if (hasTab) openCount += 1;

    const row = document.querySelector(`.site-row[data-provider="${meta.key}"]`);
    const dotEl = $(`dot-${meta.key}`);
    const modelEl = $(`model-${meta.key}`);
    const emailEl = $(`email-${meta.key}`);

    if (row) {
      row.classList.toggle("is-open", hasTab);
      row.classList.toggle("is-closed", !hasTab);
      row.classList.toggle("is-active", hasTab && activeProvider === meta.key);
    }
    if (dotEl) {
      if (!hasTab) dotEl.className = "dot";
      else if (isBound) dotEl.className = "dot ok";
      else dotEl.className = "dot warn";
    }
    if (modelEl) {
      const badge = (info.model_badge || "").trim();
      modelEl.textContent = hasTab ? (badge || "Auto-detecting") : "Not open";
      modelEl.title = badge || meta.host;
    }
    if (emailEl) {
      const email = (info.email || "").trim();
      emailEl.textContent = hasTab ? (email || "Open tab") : "";
      emailEl.title = email || "";
    }
  }

  const browserEl = $("metaBrowser");
  if (browserEl) {
    browserEl.textContent = browserDisplay;
    browserEl.className = "meta-val";
    browserEl.title = `${browserDisplay} · ${res.clientId || ""}`;
  }

  const siteEl = $("metaSite");
  if (siteEl) {
    const activeInfo = PROVIDER_META.find(m => m.key === activeProvider);
    const activeTxt = activeInfo ? activeInfo.host : (res.activeSite || "");
    siteEl.textContent = activeTxt || "None";
    siteEl.className = activeTxt ? "meta-val" : "meta-val muted";
    siteEl.title = activeTxt || "No open web agent tab";
  }

  const totalProviders = PROVIDER_META.length;
  const countEl = $("siteCount");
  if (countEl) {
    countEl.textContent = `${openCount}/${totalProviders} open`;
  }

  const dot = $("statusDot");
  const statusText = $("statusText");
  if (dot && statusText) {
    if (!srvReachable) {
      dot.className = "dot warn";
      statusText.textContent = "Server offline";
    } else if (openCount === 0) {
      dot.className = "dot warn";
      statusText.textContent = "Open AI tab";
    } else if (res.injecting) {
      dot.className = "dot ok";
      statusText.textContent = "Streaming";
    } else if (isBound) {
      dot.className = "dot ok";
      statusText.textContent = `Auto-Synced ${openCount}/${totalProviders}`;
    } else {
      dot.className = "dot";
      statusText.textContent = "Standby";
    }
  }

  // Auto-Sync Mode Status & Controls
  const autoSync = res.autoSync || res.status?.autoSync || {};
  const isAutoSyncEnabled = autoSync.enabled !== false;
  const toggle = $("autoSyncToggle");
  if (toggle && toggle.checked !== isAutoSyncEnabled) {
    toggle.checked = isAutoSyncEnabled;
  }

  const autoSyncSub = $("autoSyncSub");
  const badge = $("autoSyncBadge");
  const badgeText = $("autoSyncStatusText");
  const timeEl = $("autoSyncTime");

  if (autoSync.lastSyncAt) {
    lastAutoSyncTimestamp = autoSync.lastSyncAt;
  }

  if (!isAutoSyncEnabled) {
    if (autoSyncSub) autoSyncSub.textContent = "Auto-sync paused";
    if (badge) badge.className = "autosync-badge paused";
    if (badgeText) badgeText.textContent = "Paused";
  } else if (!srvReachable) {
    if (autoSyncSub) autoSyncSub.textContent = "Waiting for local WSA server";
    if (badge) badge.className = "autosync-badge warn";
    if (badgeText) badgeText.textContent = "Offline";
  } else {
    if (autoSyncSub) autoSyncSub.textContent = "Continuous background sync";
    if (badge) badge.className = "autosync-badge ok";
    if (badgeText) badgeText.textContent = "Active";
  }

  if (timeEl) {
    timeEl.textContent = formatRelativeTime(lastAutoSyncTimestamp);
  }
}

async function refreshStatus() {
  const isBrave = await detectIsBrave();
  chrome.runtime.sendMessage({ type: "GET_STATUS", isBrave }, (res) => {
    if (chrome.runtime.lastError || !res) return;
    applyStatusUI(res);
  });
}

// Auto-Sync Toggle Listener
const toggle = $("autoSyncToggle");
if (toggle) {
  toggle.addEventListener("change", async () => {
    const isBrave = await detectIsBrave();
    const enabled = Boolean(toggle.checked);
    chrome.runtime.sendMessage({ type: "SET_AUTOSYNC", enabled, isBrave }, (res) => {
      if (chrome.runtime.lastError || !res?.ok) return;
      refreshStatus();
    });
  });
}

// Force Sync Button (Secondary Manual Trigger)
const syncBtn = $("syncBtn");
if (syncBtn) {
  syncBtn.addEventListener("click", async () => {
    const btn = $("syncBtn");
    const btnText = $("syncBtnText");
    const isBrave = await detectIsBrave();
    btn.disabled = true;
    if (btnText) btnText.textContent = "Syncing...";
    setOutput("");

    chrome.runtime.sendMessage({ type: "SYNC_NOW", isBrave }, (res) => {
      btn.disabled = false;
      if (btnText) btnText.textContent = "Force Sync Now";

      if (chrome.runtime.lastError || !res?.ok) {
        setOutput(res?.error || chrome.runtime.lastError?.message || "Sync failed", "err");
        refreshStatus();
        return;
      }

      const providers = res.providers || {};
      const syncedLabels = PROVIDER_META
        .filter(m => Boolean(providers[m.key]?.has_tab))
        .map(m => m.label);
      const browserName = formatBrowserName(res.browser);
      const summary = syncedLabels.length
        ? `Synced ${syncedLabels.join(", ")} in ${browserName}`
        : `Synced ${browserName}`;
      setOutput(summary, "ok");

      if (res.autoSync?.lastSyncAt) {
        lastAutoSyncTimestamp = res.autoSync.lastSyncAt;
      } else {
        lastAutoSyncTimestamp = Date.now();
      }

      if (res.status) {
        applyStatusUI(res.status);
      } else {
        refreshStatus();
      }
    });
  });
}

if (typeof window !== "undefined" && typeof chrome !== "undefined" && chrome.runtime?.sendMessage) {
  refreshStatus();
  statusPollId = setInterval(refreshStatus, 2500);
  tickerIntervalId = setInterval(updateRelativeTimeDisplay, 1000);
  window.addEventListener("unload", () => {
    if (statusPollId) clearInterval(statusPollId);
    if (tickerIntervalId) clearInterval(tickerIntervalId);
  });
}
