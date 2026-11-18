/**
 * background.js — WSA Web Browser Bridge v2.0
 * Engineered by Divyansh Saraf
 *
 * Auto-detecting browser bridge connecting Brave/Chrome (claude.ai, chat.deepseek.com, gemini.google.com, chatgpt.com, venice.ai)
 * with WSA at http://127.0.0.1:8081: live status sync, DOM streaming, and same-chat routing.
 */

const LOCAL_API = "http://127.0.0.1:8081";

const EXPORT_ORDER = [
  "SID", "HSID", "SSID", "APISID", "SAPISID", "SIDCC", "OSID",
  "__Secure-1PAPISID", "__Secure-1PSID", "__Secure-1PSIDTS", "__Secure-1PSIDCC",
  "__Secure-3PAPISID", "__Secure-3PSID", "__Secure-3PSIDTS", "__Secure-3PSIDCC",
  "__Secure-OSID",
];

// ── Browser & Account Identity + State ────────────────────────────────────────

let injecting = false;
// Alarm, port and fast-loop callbacks can overlap at every await.
let acquisitionInFlight = false;
let deliveryInFlight = false;
// Serialize only durable state transitions, never page/network waits. Every
// asynchronous result must still belong to the stored request before mutation.
let relayStateMutation = Promise.resolve();
let lastBusyHeartbeatAt = 0;
let fastPollTimer = null;
let fastPollRunning = false;
let fastBurstUntil = 0;
let lastInspectTabId = null;
let browserCmdInFlight = false;
let activePollTimer = null;
let pollInFlight = false;
let lastReconciledReqId = "";
let serverBinding = { isActive: null, activeRelayLabel: "", activeRelayId: "", updatedAt: 0 };
let cachedTabIdentity = { provider: "gemini", site: "gemini.google.com", tabId: null, modelBadge: "", accountEmail: "", authUser: "0", xsrfToken: null, geminiBl: null, isBrave: false, ts: 0 };
let cachedTabIdentities = { claude: null, deepseek: null, gemini: null, chatgpt: null, venice: null };

async function getRelayActive() {
  const r = await chrome.storage.local.get(["relayActive", "relayUserPaused"]);
  if (r.relayUserPaused === true) return false;
  return r.relayActive !== false;
}

async function setRelayActive(val, explicitUser = false) {
  const update = { relayActive: Boolean(val) };
  if (explicitUser) {
    update.relayUserPaused = !val;
  } else if (val) {
    update.relayUserPaused = false;
  }
  await chrome.storage.local.set(update);
  if (val) {
    ensureFastPollLoop();
  }
}

function inferBrowserFromUA() {
  try {
    const brands = navigator.userAgentData?.brands || [];
    if (brands.some(b => /brave/i.test(b.brand || ""))) return "brave";
    if (brands.some(b => /edge/i.test(b.brand || ""))) return "edge";
  } catch { /* ignore */ }
  const ua = navigator.userAgent || "";
  if (/Brave/i.test(ua)) return "brave";
  if (/Edg\//i.test(ua)) return "edge";
  return "chrome";
}

function normalizeBrowserHint(hint) {
  if (hint === true) return "brave";
  if (hint === false) {
    const uaBrowser = inferBrowserFromUA();
    return uaBrowser === "edge" ? "edge" : "chrome";
  }
  const s = String(hint || "").trim().toLowerCase();
  if (s === "brave" || s === "chrome" || s === "edge") return s;
  return "";
}

async function getRelayIdentity(hintBrowser = "") {
  const stored = await chrome.storage.local.get(["relayClientId", "relayLabel", "detectedBrowser"]);
  const normalizedHint = normalizeBrowserHint(hintBrowser);
  let browser = normalizedHint || stored.detectedBrowser || inferBrowserFromUA();
  if (normalizedHint && normalizedHint !== stored.detectedBrowser) {
    await chrome.storage.local.set({ detectedBrowser: normalizedHint });
    browser = normalizedHint;
  }

  let clientId = stored.relayClientId || "";
  let label = stored.relayLabel || "";
  const idParts = clientId.split("-");
  const knownPrefixes = new Set(["chrome", "brave", "edge"]);
  if (!clientId) {
    const rnd = (crypto.randomUUID ? crypto.randomUUID().slice(0, 6) : Math.random().toString(16).slice(2, 8));
    clientId = `${browser}-${rnd}`;
    await chrome.storage.local.set({ relayClientId: clientId });
  } else if (idParts.length >= 2 && knownPrefixes.has(idParts[0]) && idParts[0] !== browser) {
    const suffix = idParts.slice(1).join("-");
    clientId = `${browser}-${suffix}`;
    await chrome.storage.local.set({ relayClientId: clientId });
  }

  const labelLower = label.toLowerCase();
  if (!label || knownPrefixes.has(labelLower)) {
    label = browser;
    await chrome.storage.local.set({ relayLabel: label });
  }
  return { clientId, browser, label };
}

const PROVIDER_KEYS = ["claude", "deepseek", "gemini", "chatgpt", "venice"];

const PROVIDERS = {
  claude: {
    key: "claude",
    label: "Claude",
    site: "claude.ai",
    defaultUrl: "https://claude.ai/new",
    urlPatterns: ["https://claude.ai/*", "https://www.claude.ai/*"],
    hostRe: /^https:\/\/(?:www\.)?claude\.ai(?:\/|$)/i,
    pathRe: /\/(?:chat|new)/i,
    runtimeKey: "__wsaClaudeRuntime",
    runtimeVersion: 4,
    needsDomUtils: true,
  },
  deepseek: {
    key: "deepseek",
    label: "DeepSeek",
    site: "chat.deepseek.com",
    defaultUrl: "https://chat.deepseek.com/",
    urlPatterns: ["https://chat.deepseek.com/*"],
    hostRe: /^https:\/\/(?:www\.)?chat\.deepseek\.com(?:\/|$)/i,
    pathRe: /\/a\/chat|^\/$/i,
    runtimeKey: "__wsaDeepseekRuntime",
    runtimeVersion: 3,
    needsDomUtils: true,
  },
  gemini: {
    key: "gemini",
    label: "Gemini",
    site: "gemini.google.com",
    defaultUrl: "https://gemini.google.com/app",
    urlPatterns: ["https://gemini.google.com/*"],
    hostRe: /^https:\/\/gemini\.google\.com(?:\/|$)/i,
    pathRe: /\/app/i,
    runtimeKey: "__wsaRuntime",
    runtimeVersion: 2,
    needsDomUtils: false,
  },
  chatgpt: {
    key: "chatgpt",
    label: "ChatGPT",
    site: "chatgpt.com",
    defaultUrl: "https://chatgpt.com/",
    urlPatterns: ["https://chatgpt.com/*", "https://chat.openai.com/*"],
    hostRe: /^https:\/\/(?:www\.)?(?:chatgpt\.com|chat\.openai\.com)(?:\/|$)/i,
    pathRe: /\/c\/|^\/$/i,
    runtimeKey: "__wsaChatgptRuntime",
    runtimeVersion: 3,
    needsDomUtils: true,
  },
  venice: {
    key: "venice",
    label: "Venice",
    site: "venice.ai",
    defaultUrl: "https://venice.ai/chat/agent",
    urlPatterns: ["https://venice.ai/*", "https://*.venice.ai/*"],
    hostRe: /^https:\/\/(?:www\.)?venice\.ai(?:\/|$)/i,
    pathRe: /\/chat\//i,
    runtimeKey: "__wsaVeniceRuntime",
    runtimeVersion: 1,
    needsDomUtils: true,
  },
};

function normalizeProviderKey(raw) {
  const s = String(raw || "").trim().toLowerCase();
  return PROVIDER_KEYS.includes(s) ? s : "";
}

function detectTabProvider(url) {
  const s = String(url || "");
  for (const pk of PROVIDER_KEYS) {
    if (PROVIDERS[pk].hostRe.test(s)) return pk;
  }
  return "";
}

function providerDisplaySite(provider) {
  return PROVIDERS[normalizeProviderKey(provider)]?.site || "";
}

function providerDisplayLabel(provider) {
  return PROVIDERS[normalizeProviderKey(provider)]?.label || "AI";
}

function sessionStorageKey(sessionId, provider = "gemini") {
  const sid = String(sessionId || "").trim();
  if (!sid) return "";
  const p = normalizeProviderKey(provider) || "gemini";
  return p === "gemini" ? sid : `${sid}::${p}`;
}

function parseAuthUserFromUrl(url) {
  try {
    const m = new URL(url || "").pathname.match(/^\/u\/(\d+)(?:\/|$)/);
    return m ? m[1] : "0";
  } catch {
    return "0";
  }
}

async function readLiveTabIdentity(tab, options = {}) {
  const forceProbe = Boolean(options?.forceProbe);
  const provider = detectTabProvider(tab?.url || "") || "gemini";
  const site = providerDisplaySite(provider);
  const authUser = provider === "gemini" ? parseAuthUserFromUrl(tab?.url || "") : "0";
  if (!tab?.id) {
    return { provider, site, authUser, modelBadge: "", accountEmail: "", isBrave: false, xsrfToken: null, geminiBl: null };
  }
  const now = Date.now();
  const prevCached = cachedTabIdentities[provider];
  if (
    !forceProbe &&
    prevCached &&
    prevCached.tabId === tab.id &&
    prevCached.authUser === authUser &&
    (now - prevCached.ts) < 12000
  ) {
    return prevCached;
  }
  if (!forceProbe && (tab.discarded || tab.status === "unloaded")) {
    const stored = await chrome.storage.local.get(["detectedBrowser"]).catch(() => ({}));
    return {
      ...(prevCached || cachedTabIdentity),
      provider,
      site,
      tabId: tab.id,
      authUser,
      isBrave: Boolean(prevCached?.isBrave ?? (stored?.detectedBrowser === "brave")),
    };
  }
  try {
    const res = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      func: async (targetProvider) => {
        let isBrave = false;
        try {
          if (navigator.brave && typeof navigator.brave.isBrave === "function") {
            isBrave = Boolean(await navigator.brave.isBrave());
          }
        } catch { /* ignore */ }

        const emailRe = /([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})/;
        if (targetProvider === "claude") {
          let accountEmail = "";
          try {
            const acctCandidates = [
              document.querySelector("[data-testid='user-menu-button'], button[aria-label*='@'], [data-testid*='account']"),
              document.querySelector("nav [aria-label*='@']"),
            ].filter(Boolean);
            for (const el of acctCandidates) {
              const txt = `${el.getAttribute("aria-label") || ""} ${el.innerText || el.textContent || ""}`;
              const m = txt.match(emailRe);
              if (m) { accountEmail = m[1]; break; }
            }
            if (!accountEmail && globalThis.__NEXT_DATA__) {
              const rawNext = JSON.stringify(globalThis.__NEXT_DATA__).slice(0, 8000);
              const m = rawNext.match(emailRe);
              if (m) accountEmail = m[1];
            }
            if (!accountEmail && typeof localStorage !== "undefined") {
              for (let i = 0; i < Math.min(localStorage.length, 40); i++) {
                const k = localStorage.key(i) || "";
                if (!/user|acct|account|auth|session|email/i.test(k)) continue;
                const v = String(localStorage.getItem(k) || "");
                const m = v.match(emailRe);
                if (m) { accountEmail = m[1]; break; }
              }
            }
            if (!accountEmail && typeof fetch === "function") {
              try {
                const ar = await fetch("/api/account", { credentials: "include", signal: AbortSignal.timeout(1200) });
                if (ar.ok) {
                  const data = await ar.json().catch(() => null);
                  const found = data?.email_address || data?.email || data?.account?.email_address || "";
                  if (typeof found === "string" && found.includes("@")) {
                    accountEmail = found.trim();
                  }
                }
              } catch { /* ignore */ }
            }
          } catch { /* ignore */ }

          let modelBadge = "";
          try {
            const selectors = [
              "[data-testid='model-selector-dropdown']",
              "button[data-testid*='model-selector']",
              "button[data-testid*='model']",
              "button[aria-label*='model' i]",
            ];
            for (const sel of selectors) {
              const btn = document.querySelector(sel);
              if (!btn) continue;
              const txt = (btn.innerText || btn.textContent || btn.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
              if (txt && txt.length <= 60) {
                modelBadge = txt;
                break;
              }
            }
            if (!modelBadge) {
              for (const b of document.querySelectorAll("button")) {
                const txt = (b.innerText || b.textContent || "").replace(/\s+/g, " ").trim();
                if (txt && txt.length <= 48 && /\b(?:Claude|Opus|Sonnet|Haiku)\b/i.test(txt)) {
                  modelBadge = txt;
                  break;
                }
              }
            }
            const thinkBtn = document.querySelector(
              "button[aria-label*='thinking' i][aria-pressed='true'], [data-testid*='thinking'][aria-pressed='true'], button[aria-label*='Extended thinking' i][data-state='checked']"
            );
            if (thinkBtn && modelBadge && !/thinking|extended/i.test(modelBadge)) {
              modelBadge = `${modelBadge} · Extended thinking`;
            }
          } catch { /* ignore */ }

          return {
            provider: "claude",
            site: "claude.ai",
            isBrave,
            xsrfToken: null,
            geminiBl: null,
            accountEmail,
            modelBadge,
            url: location.href,
          };
        }

        if (targetProvider === "chatgpt") {
          let accountEmail = "";
          try {
            const acctCandidates = [
              document.querySelector("[data-testid='accounts-profile-button'], button[aria-label*='Open profile menu' i], button[aria-label*='@']"),
              document.querySelector("nav [aria-label*='@']"),
            ].filter(Boolean);
            for (const el of acctCandidates) {
              const txt = `${el.getAttribute("aria-label") || ""} ${el.innerText || el.textContent || ""}`;
              const m = txt.match(emailRe);
              if (m) { accountEmail = m[1]; break; }
            }
            if (!accountEmail && globalThis.__NEXT_DATA__) {
              const rawNext = JSON.stringify(globalThis.__NEXT_DATA__).slice(0, 8000);
              const m = rawNext.match(emailRe);
              if (m) accountEmail = m[1];
            }
            if (!accountEmail && typeof localStorage !== "undefined") {
              for (let i = 0; i < Math.min(localStorage.length, 40); i++) {
                const k = localStorage.key(i) || "";
                if (!/user|acct|account|auth|session|email|oai/i.test(k)) continue;
                const v = String(localStorage.getItem(k) || "");
                const m = v.match(emailRe);
                if (m) { accountEmail = m[1]; break; }
              }
            }
            if (!accountEmail && typeof fetch === "function") {
              try {
                const ar = await fetch("/backend-api/me", { credentials: "include", signal: AbortSignal.timeout(1200) });
                if (ar.ok) {
                  const data = await ar.json().catch(() => null);
                  const found = data?.email || data?.user?.email || "";
                  if (typeof found === "string" && found.includes("@")) {
                    accountEmail = found.trim();
                  }
                }
              } catch { /* ignore */ }
            }
          } catch { /* ignore */ }

          let modelBadge = "";
          try {
            const selectors = [
              "[data-testid='model-switcher-dropdown-button']",
              "button[aria-label*='Model selector' i]",
              "button[data-testid*='model-switcher']",
            ];
            for (const sel of selectors) {
              const btn = document.querySelector(sel);
              if (!btn) continue;
              const txt = (btn.innerText || btn.textContent || btn.getAttribute("aria-label") || "").replace(/^Model selector,?\s*current model is\s*/i, "").replace(/\s+/g, " ").trim();
              if (txt && txt.length <= 60) {
                modelBadge = txt;
                break;
              }
            }
            if (!modelBadge) {
              for (const b of document.querySelectorAll("header button, main header button, [role='banner'] button")) {
                const txt = (b.innerText || b.textContent || "").replace(/\s+/g, " ").trim();
                if (txt && txt.length <= 48 && /\b(?:ChatGPT|GPT-[456]|o[134]|Instant|Thinking)\b/i.test(txt)) {
                  modelBadge = txt;
                  break;
                }
              }
            }
            if (!modelBadge) {
              const profileBtn = document.querySelector("button[aria-label*='Open profile menu' i], [data-testid='accounts-profile-button']");
              const pTxt = (profileBtn?.innerText || profileBtn?.textContent || "").replace(/\s+/g, " ").trim();
              const tierMatch = pTxt.match(/\b(Go|Plus|Pro|Team|Enterprise|Free)\b/i);
              modelBadge = tierMatch ? `ChatGPT (${tierMatch[1]})` : "ChatGPT";
            }
            let thinkActive = Boolean(
              document.querySelector(
                "button[aria-label*='Thinking Selected' i], [aria-label='Thinking Selected'], button[aria-pressed='true'][aria-label*='Think' i]"
              )
            );
            if (!thinkActive) {
              for (const b of document.querySelectorAll("form button, button")) {
                const lbl = (b.getAttribute("aria-label") || b.innerText || b.textContent || "").replace(/\s+/g, " ").trim();
                if (!/^Think(?:ing)?$/i.test(lbl)) continue;
                const pressed = b.getAttribute("aria-pressed") === "true" || b.getAttribute("data-state") === "checked";
                const cls = typeof b.className === "string" ? b.className : "";
                if (pressed || /accentSubtle|accent/i.test(cls)) {
                  thinkActive = true;
                  break;
                }
              }
            }
            let webSearchActive = false;
            const edEl = document.querySelector("#prompt-textarea, div.ProseMirror[contenteditable='true']");
            if (edEl) {
              for (const pill of edEl.querySelectorAll("[data-layout='selection-pill'], [data-tone='composer-reference'], [data-ecosystem-mention], span[class*='Mention-'], span[contenteditable='false']")) {
                const pTxt = (pill.innerText || pill.textContent || "").replace(/\s+/g, " ").trim();
                if (/\bWeb\s*search\b/i.test(pTxt)) {
                  webSearchActive = true;
                  break;
                }
              }
            }
            if (thinkActive && modelBadge && !/\bthink(?:ing)?\b/i.test(modelBadge)) {
              modelBadge = `${modelBadge} · Think`;
            } else if (webSearchActive && modelBadge && !/\bweb\s*search\b/i.test(modelBadge)) {
              modelBadge = `${modelBadge} · Web search`;
            }
          } catch { /* ignore */ }

          return {
            provider: "chatgpt",
            site: "chatgpt.com",
            isBrave,
            xsrfToken: null,
            geminiBl: null,
            accountEmail,
            modelBadge,
            url: location.href,
          };
        }

        if (targetProvider === "deepseek") {
          let accountEmail = "";
          try {
            const acctCandidates = [
              document.querySelector("[aria-label*='@'], [class*='email' i], [class*='profile' i]"),
              document.querySelector("nav [class*='user' i], aside [class*='user' i]"),
            ].filter(Boolean);
            for (const el of acctCandidates) {
              const txt = `${el.getAttribute("aria-label") || ""} ${el.innerText || el.textContent || ""}`;
              const m = txt.match(emailRe);
              if (m) { accountEmail = m[1]; break; }
            }
            if (!accountEmail && typeof localStorage !== "undefined") {
              for (let i = 0; i < Math.min(localStorage.length, 40); i++) {
                const k = localStorage.key(i) || "";
                if (!/user|acct|account|auth|session|email|deepseek/i.test(k)) continue;
                const v = String(localStorage.getItem(k) || "");
                const m = v.match(emailRe);
                if (m) { accountEmail = m[1]; break; }
              }
            }
            if (!accountEmail && typeof fetch === "function") {
              try {
                const ar = await fetch("/api/v0/users/current", { credentials: "include", signal: AbortSignal.timeout(1200) });
                if (ar.ok) {
                  const data = await ar.json().catch(() => null);
                  const found = data?.data?.biz_data?.email || data?.data?.email || data?.email || "";
                  if (typeof found === "string" && found.includes("@")) {
                    accountEmail = found.trim();
                  }
                }
              } catch { /* ignore */ }
            }
          } catch { /* ignore */ }

          let modelBadge = "DeepSeek-V3";
          try {
            let deepThinkActive = false;
            let searchActive = false;
            for (const b of document.querySelectorAll(".ds-toggle-button, button, [role='button']")) {
              const lbl = (b.getAttribute("aria-label") || b.innerText || b.textContent || "").replace(/\s+/g, " ").trim();
              if (!lbl || lbl.length > 48) continue;
              const isThinkBtn = /\bDeepThink\b|Deep\s*Think|\bR1\b/i.test(lbl);
              const isSearchBtn = !isThinkBtn && /\b(?:Smart\s+)?Search\b/i.test(lbl);
              if (!isThinkBtn && !isSearchBtn) continue;
              const pressed = b.getAttribute("aria-pressed") === "true"
                || b.getAttribute("data-selected") === "true"
                || b.getAttribute("data-state") === "checked";
              const cls = typeof b.className === "string" ? b.className : "";
              let activeColor = false;
              try {
                const st = getComputedStyle(b);
                const styles = `${st.backgroundColor || ""} ${st.borderColor || ""} ${st.color || ""}`;
                if (/40,\s*49,\s*66|103,\s*158,\s*254|72,\s*104,\s*178|4d6bfe|77,\s*107,\s*254|59,\s*130,\s*246|96,\s*165,\s*250/i.test(styles)) {
                  activeColor = true;
                }
              } catch { /* ignore */ }
              const isSelected = pressed
                || activeColor
                || /(?:ds-toggle-button--selected|--selected|--active|\bselected\b|\bactive\b|\bchecked\b)/i.test(cls);
              if (!isSelected) continue;
              if (isThinkBtn) deepThinkActive = true;
              if (isSearchBtn) searchActive = true;
            }
            if (deepThinkActive && searchActive) {
              modelBadge = "DeepSeek-R1 · DeepThink + Search";
            } else if (deepThinkActive) {
              modelBadge = "DeepSeek-R1 · DeepThink";
            } else if (searchActive) {
              modelBadge = "DeepSeek-V3 · Search";
            } else {
              modelBadge = "DeepSeek-V3";
            }
          } catch { /* ignore */ }

          return {
            provider: "deepseek",
            site: "chat.deepseek.com",
            isBrave,
            xsrfToken: null,
            geminiBl: null,
            accountEmail,
            modelBadge,
            url: location.href,
          };
        }

        if (targetProvider === "venice") {
          let accountEmail = "";
          try {
            const acctCandidates = [
              document.querySelector("[data-testid*='user' i], [aria-label*='@'], [class*='profile' i], [class*='account' i]"),
              document.querySelector("nav [class*='user' i], aside [class*='user' i]"),
            ].filter(Boolean);
            for (const el of acctCandidates) {
              const txt = `${el.getAttribute("aria-label") || ""} ${el.innerText || el.textContent || ""}`;
              const m = txt.match(emailRe);
              if (m) { accountEmail = m[1]; break; }
            }
            if (!accountEmail && typeof localStorage !== "undefined") {
              for (let i = 0; i < Math.min(localStorage.length, 40); i++) {
                const k = localStorage.key(i) || "";
                if (!/user|acct|account|auth|session|email|venice/i.test(k)) continue;
                const v = String(localStorage.getItem(k) || "");
                const m = v.match(emailRe);
                if (m) { accountEmail = m[1]; break; }
              }
            }
          } catch { /* ignore */ }

          let modelBadge = "";
          const modelKwRe = /\b(?:DeepSeek|Kimi|GLM|Claude|Grok|Llama|Qwen|Mistral|Gemini|Venice\s*Auto|Auto)\b/i;
          try {
            for (const sel of [
              "button[aria-haspopup='menu']",
              "button[aria-label*='model' i]",
              "button.chakra-button",
            ]) {
              for (const b of document.querySelectorAll(sel)) {
                const txt = (b.innerText || b.textContent || b.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
                if (!txt || txt.length > 64) continue;
                if (/^(?:more models|new chat|settings|actions|get pro(?:\s+access)?|dictate|send message|submit chat|stop(?:\s+generation)?)$/i.test(txt)) continue;
                if (modelKwRe.test(txt)) {
                  modelBadge = txt;
                  break;
                }
              }
              if (modelBadge) break;
            }
            if (!modelBadge) {
              for (const pEl of document.querySelectorAll("main p.chakra-text")) {
                const txt = (pEl.innerText || pEl.textContent || "").replace(/\s+/g, " ").trim();
                if (txt && txt.length <= 64 && modelKwRe.test(txt)) {
                  modelBadge = txt;
                  break;
                }
              }
            }
          } catch { /* ignore */ }

          return {
            provider: "venice",
            site: "venice.ai",
            isBrave,
            xsrfToken: null,
            geminiBl: null,
            accountEmail,
            modelBadge: modelBadge || (/\/chat\/agent/i.test(location.pathname || "") ? "DeepSeek V4.1 Flash Uncensored" : "Venice Web"),
            url: location.href,
          };
        }

        const wiz = globalThis.WIZ_global_data || {};
        let accountEmail = "";
        try {
          const acctEl = document.querySelector(
            "a[aria-label*='Google Account' i], button[aria-label*='Google Account' i], [aria-label*='@'][aria-label*='Google' i]"
          );
          const lbl = acctEl?.getAttribute("aria-label") || "";
          const em = lbl.match(emailRe);
          if (em) accountEmail = em[1];
          else if (typeof wiz.oPEP7c === "string" && wiz.oPEP7c.includes("@")) accountEmail = wiz.oPEP7c;
        } catch { /* ignore */ }
        let modelBadge = "";
        try {
          const btn = document.querySelector(
            "button[aria-label*='mode picker' i], button[aria-label*='Switch model' i], button[aria-label*='model picker' i], [data-test-id='bard-mode-menu-button'] button, [data-test-id='bard-mode-menu-button']"
          );
          if (btn) {
            const aria = btn.getAttribute("aria-label") || "";
            const mm = aria.match(/currently\s+(.+)$/i);
            modelBadge = (mm ? mm[1] : (btn.innerText || btn.textContent || "")).replace(/\s+/g, " ").trim();
          }
        } catch { /* ignore */ }
        return {
          provider: "gemini",
          site: "gemini.google.com",
          isBrave,
          xsrfToken: wiz.SNlM0e || null,
          geminiBl: wiz.cfb2h || null,
          accountEmail,
          modelBadge,
          url: location.href,
        };
      },
      args: [provider],
    });
    const r = res?.[0]?.result || {};
    const detected = r.isBrave ? "brave" : (inferBrowserFromUA() === "edge" ? "edge" : "chrome");
    await chrome.storage.local.set({ detectedBrowser: detected });
    const sameTabPrev = prevCached && prevCached.tabId === tab.id && prevCached.authUser === authUser ? prevCached : null;
    const preservedEmail = r.accountEmail || sameTabPrev?.accountEmail || "";
    const genericBadge = !r.modelBadge || (provider === "chatgpt" && r.modelBadge === "ChatGPT") || (provider === "venice" && r.modelBadge === "Venice Web");
    const preservedBadge = (!genericBadge ? r.modelBadge : (sameTabPrev?.modelBadge || r.modelBadge)) || "";
    const entry = {
      provider,
      site,
      tabId: tab.id,
      authUser,
      isBrave: Boolean(r.isBrave),
      modelBadge: preservedBadge,
      accountEmail: preservedEmail,
      xsrfToken: r.xsrfToken || null,
      geminiBl: r.geminiBl || null,
      url: r.url || tab.url || "",
      ts: now,
    };
    cachedTabIdentities[provider] = entry;
    cachedTabIdentity = entry;
    return entry;
  } catch {
    return { provider, site, authUser, isBrave: false, modelBadge: "", accountEmail: "", xsrfToken: null, geminiBl: null };
  }
}

async function scanOpenSiteStates(options = {}) {
  const forceProbe = Boolean(options?.forceProbe);
  const queriedLists = await Promise.all(
    PROVIDER_KEYS.map((pk) =>
      chrome.tabs.query({ url: PROVIDERS[pk].urlPatterns }).catch(() => [])
    )
  );
  const tabsByProvider = {};
  const allTabs = [];
  PROVIDER_KEYS.forEach((pk, idx) => {
    const list = Array.isArray(queriedLists[idx]) ? queriedLists[idx] : [];
    tabsByProvider[pk] = list;
    allTabs.push(...list);
  });
  await protectAiTabs(allTabs);

  const bestByProvider = {};
  for (const pk of PROVIDER_KEYS) {
    bestByProvider[pk] = pickBestTabForProvider(tabsByProvider[pk], pk);
  }

  const metaEntries = await Promise.all(
    PROVIDER_KEYS.map(async (pk) => [
      pk,
      bestByProvider[pk] ? await readLiveTabIdentity(bestByProvider[pk], { forceProbe }) : null,
    ])
  );
  const metaByProvider = Object.fromEntries(metaEntries);

  let activeProvider = "";
  const presentKeys = PROVIDER_KEYS.filter((pk) => Boolean(bestByProvider[pk]));
  if (presentKeys.length) {
    const activeKeys = presentKeys.filter((pk) => Boolean(bestByProvider[pk]?.active));
    const pool = activeKeys.length ? activeKeys : presentKeys;
    pool.sort((a, b) => (bestByProvider[b]?.lastAccessed || 0) - (bestByProvider[a]?.lastAccessed || 0));
    activeProvider = pool[0];
  }

  const activeMeta = activeProvider ? metaByProvider[activeProvider] : null;
  let browserHint = normalizeBrowserHint(options?.explicitBrowser);
  if (!browserHint) {
    const anyBrave = PROVIDER_KEYS.some((pk) => Boolean(metaByProvider[pk]?.isBrave));
    if (PROVIDER_KEYS.some((pk) => Boolean(metaByProvider[pk]))) {
      browserHint = anyBrave ? "brave" : normalizeBrowserHint(false);
    }
  }
  const ident = await getRelayIdentity(browserHint);

  const providers = {};
  for (const pk of PROVIDER_KEYS) {
    const bestTab = bestByProvider[pk];
    const meta = metaByProvider[pk];
    providers[pk] = {
      has_tab: Boolean(bestTab?.id),
      tab_count: tabsByProvider[pk].length,
      email: meta?.accountEmail || "",
      model_badge: meta?.modelBadge || "",
      url: bestTab?.url || "",
      ...(pk === "gemini" ? { auth_user: meta?.authUser || "0" } : {}),
    };
  }

  const fallbackMeta = activeMeta || PROVIDER_KEYS.map((pk) => metaByProvider[pk]).find(Boolean) || null;
  const geminiMeta = metaByProvider.gemini;

  return {
    ident,
    provider: activeProvider,
    site: providerDisplaySite(activeProvider),
    authUser: geminiMeta?.authUser || "0",
    accountEmail: fallbackMeta?.accountEmail || "",
    modelBadge: fallbackMeta?.modelBadge || "",
    xsrfToken: geminiMeta?.xsrfToken || null,
    geminiBl: geminiMeta?.geminiBl || null,
    hasClaudeTab: Boolean(bestByProvider.claude?.id),
    hasDeepseekTab: Boolean(bestByProvider.deepseek?.id),
    hasGeminiTab: Boolean(bestByProvider.gemini?.id),
    hasChatgptTab: Boolean(bestByProvider.chatgpt?.id),
    hasVeniceTab: Boolean(bestByProvider.venice?.id),
    hasAnyAiTab: presentKeys.length > 0,
    tabCount: allTabs.length,
    claudeTabs: tabsByProvider.claude,
    deepseekTabs: tabsByProvider.deepseek,
    geminiTabs: tabsByProvider.gemini,
    chatgptTabs: tabsByProvider.chatgpt,
    veniceTabs: tabsByProvider.venice,
    bestClaude: bestByProvider.claude,
    bestDeepseek: bestByProvider.deepseek,
    bestGemini: bestByProvider.gemini,
    bestChatgpt: bestByProvider.chatgpt,
    bestVenice: bestByProvider.venice,
    tabsByProvider,
    bestByProvider,
    providers,
  };
}

function buildPendingQueryParams(snap, extra = {}) {
  const qs = new URLSearchParams({
    client_id: snap.ident.clientId,
    browser: snap.ident.browser,
    label: snap.ident.label,
    auth_user: snap.authUser || "0",
    email: snap.accountEmail || "",
    model_badge: snap.modelBadge || "",
    site: snap.site || "",
    provider: snap.provider || "",
  });
  for (const pk of PROVIDER_KEYS) {
    const pInfo = snap?.providers?.[pk] || {};
    qs.set(`has_${pk}`, pInfo.has_tab ? "1" : "0");
    qs.set(`${pk}_email`, pInfo.email || "");
    qs.set(`${pk}_model`, pInfo.model_badge || "");
  }
  for (const [k, v] of Object.entries(extra)) {
    if (v !== undefined && v !== null) qs.set(k, String(v));
  }
  return qs;
}

async function clearLocalRelayJobState(burst = true, expectedReqId = undefined) {
  return mutateRelayState(async () => {
    const current = await getStoredActiveJob();
    if (expectedReqId !== undefined && (current?.reqId || null) !== expectedReqId) return false;
    await clearStoredActiveJob();
    clearActivePollTimer();
    injecting = false;
    lastReconciledReqId = "";
    if (burst) fastBurstUntil = Date.now() + 15000;
    return true;
  });
}

async function sendBusyHeartbeat(jobOverride = null, { triggerRelay = true } = {}) {
  try {
    const job = jobOverride || (await getStoredActiveJob());
    // Delivery owns completion until its acknowledgement; a simultaneous busy
    // check can otherwise mistake an accepted result for an orphaned request.
    if (job?.completion) return true;
    // Liveness must not depend on scripts executing in a responsive AI page.
    // A slow/frozen composer can otherwise block every heartbeat behind a scan.
    const ident = job?.relayIdentity || (job?.clientId ? {
      clientId: job.clientId,
      browser: job.clientId.split('-')[0],
      label: job.clientId.split('-')[0],
    } : await getRelayIdentity());
    const qs = new URLSearchParams({
      client_id: ident.clientId,
      browser: ident.browser,
      label: ident.label,
      busy: "1",
      busy_req_id: job?.reqId || "",
    });
    if (job?.isGenerating) {
      qs.set("generating", "1");
    }
    const resp = await fetch(`${LOCAL_API}/pending-prompt?${qs.toString()}`, {
      signal: AbortSignal.timeout(2500),
    });
    if (!resp.ok) return true;
    const hdrCancelled = String(resp.headers?.get("X-Cancelled-Req-Ids") || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const busyReqValidHdr = resp.headers?.get("X-Busy-Req-Valid");
    const data = await resp.json().catch(() => null);
    const bodyCancelled = Array.isArray(data?.cancelled_req_ids) ? data.cancelled_req_ids : [];
    const cancelledIds = [...new Set([...hdrCancelled, ...bodyCancelled])];
    const current = await getStoredActiveJob();
    if (job?.reqId && (current?.reqId !== job.reqId || current?.completion)) return true;
    if (job && cancelledIds.includes(job.reqId)) {
      await cancelActiveRelayJob(job);
      if (triggerRelay) {
        setTimeout(() => { checkAndRelay().catch(() => {}); }, 20);
      }
      return false;
    }
    if (job?.reqId && busyReqValidHdr === "0") {
      console.warn("[WSA] Clearing stale/orphaned relay job unknown to server:", job.reqId);
      await clearLocalRelayJobState(true, job.reqId);
      if (triggerRelay) {
        setTimeout(() => { checkAndRelay().catch(() => {}); }, 20);
      }
      return false;
    }
    return true;
  } catch {
    return true;
  }
}

function ensureFastPollLoop() {
  if (fastPollRunning) return;
  fastPollRunning = true;
  const tick = async () => {
    try {
      if (await getRelayActive()) {
        if (!injecting) {
          await restoreActiveRelayJob();
        }
        if (injecting) {
          if (!activePollTimer && !pollInFlight) {
            scheduleActiveJobPoll(50);
          }
          if ((Date.now() - lastBusyHeartbeatAt) >= 7000) {
            lastBusyHeartbeatAt = Date.now();
            await sendBusyHeartbeat();
          }
        } else {
          await checkAndRelay();
        }
      }
    } catch { /* ignore */ }
    const nextDelay = (!injecting && Date.now() < fastBurstUntil) ? 250 : 1000;
    fastPollTimer = setTimeout(tick, nextDelay);
  };
  fastPollTimer = setTimeout(tick, 100);
}

// Start polling & alarms immediately on service worker load (respecting user pause)
if (typeof chrome !== "undefined" && chrome?.runtime?.id) {
  chrome.alarms.create("dom-relay-poll", { periodInMinutes: 0.5 });
  chrome.alarms.create("cookie-auto-sync", { periodInMinutes: 5 });
  getRelayActive().then(async (active) => {
    if (!active) return;
    await restoreActiveRelayJob();
    ensureFastPollLoop();
  }).catch(() => {});
  setTimeout(() => { autoSyncCookiesToLocalServer().catch(() => {}); }, 1500);

  chrome.runtime.onInstalled.addListener(() => {
    chrome.alarms.create("dom-relay-poll", { periodInMinutes: 0.5 });
    chrome.alarms.create("cookie-auto-sync", { periodInMinutes: 5 });
    getRelayActive().then(async (active) => {
      if (!active) return;
      await restoreActiveRelayJob();
      ensureFastPollLoop();
    }).catch(() => {});
    autoSyncCookiesToLocalServer().catch(() => {});
  });

  if (chrome.runtime.onStartup) {
    chrome.runtime.onStartup.addListener(() => {
      chrome.alarms.create("dom-relay-poll", { periodInMinutes: 0.5 });
      chrome.alarms.create("cookie-auto-sync", { periodInMinutes: 5 });
      getRelayActive().then(async (active) => {
        if (!active) return;
        await restoreActiveRelayJob();
        ensureFastPollLoop();
      }).catch(() => {});
    });
  }

  chrome.alarms.onAlarm.addListener(async (alarm) => {
    if (alarm.name === "cookie-auto-sync") {
      await autoSyncCookiesToLocalServer().catch(() => {});
      return;
    }
    if (alarm.name === "dom-relay-poll") {
      const active = await getRelayActive();
      if (!active) return;
      ensureFastPollLoop();
      await restoreActiveRelayJob();
      if (injecting) {
        await sendBusyHeartbeat(null, { triggerRelay: false });
        await pollActiveRelayJob();
        return;
      }
      await checkAndRelay();
    }
  });

  if (chrome.tabs?.onUpdated) {
    chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
      const isAiUrl = Boolean(detectTabProvider(tab?.url || ""));
      if (isAiUrl && tab.autoDiscardable !== false) {
        chrome.tabs.update(tabId, { autoDiscardable: false }).catch(() => {});
      }
      if (changeInfo.status === "complete" && isAiUrl) {
        getRelayActive().then(async (active) => {
          if (!active) return;
          await restoreActiveRelayJob();
          ensureFastPollLoop();
        }).catch(() => {});
      }
    });
  }

  // Keepalive ports from either popup ("relay-keepalive") or AI tab ("wsa-tab-keepalive")
  chrome.runtime.onConnect.addListener((port) => {
    if (port.name !== "relay-keepalive" && port.name !== "wsa-tab-keepalive" && port.name !== "gcs-tab-keepalive") return;
    if (port.sender?.tab?.id && port.sender.tab.autoDiscardable !== false) {
      chrome.tabs.update(port.sender.tab.id, { autoDiscardable: false }).catch(() => {});
    }
    getRelayActive().then(async (active) => {
      if (!active) return;
      await restoreActiveRelayJob();
      ensureFastPollLoop();
      if (injecting) {
        await pollActiveRelayJob();
      } else {
        checkAndRelay().catch(() => {});
      }
    }).catch(() => {});
    port.onMessage.addListener((m) => {
      if (typeof m?.isBrave === "boolean") {
        chrome.storage.local.set({ detectedBrowser: normalizeBrowserHint(m.isBrave) }).catch(() => {});
      }
      getRelayActive().then(async (active) => {
        if (!active) return;
        await restoreActiveRelayJob();
        ensureFastPollLoop();
        if (injecting && !pollInFlight) {
          pollActiveRelayJob().catch(() => {});
        }
      }).catch(() => {});
    });
    port.onDisconnect.addListener(() => {
      const _lastErr = (typeof chrome !== "undefined" && chrome.runtime ? chrome.runtime.lastError : null) || port.error;
      void _lastErr;
      getRelayActive().then(async (active) => {
        if (!active) return;
        await restoreActiveRelayJob();
        ensureFastPollLoop();
      }).catch(() => {});
    });
  });
}

// ── Shared Essential Auth Collector & Local Sync ──────────────────────────────

async function collectAuthBundle(options = {}) {
  const forceProbe = Boolean(options?.forceProbe);
  const allowWithoutTab = Boolean(options?.allowWithoutTab);
  const explicitBrowser = normalizeBrowserHint(options?.explicitBrowser);
  const snap = await scanOpenSiteStates({ forceProbe, explicitBrowser });
  const tab = snap.bestGemini;
  if (!tab?.id && !allowWithoutTab && !snap.hasAnyAiTab) {
    return { ok: false, error: "Open claude.ai, chat.deepseek.com, gemini.google.com, chatgpt.com, or venice.ai first" };
  }

  const stores = await chrome.cookies.getAllCookieStores();
  const allCookies = [];
  for (const store of stores) {
    const cList = await chrome.cookies.getAll({ storeId: store.id }).catch(() => []);
    for (const c of cList) {
      const d = (c.domain || "").replace(/^\./, "").toLowerCase();
      if ((d === "google.com" || d.endsWith(".google.com")) && c.value) {
        allCookies.push(c);
      }
    }
  }
  const selected = new Map();
  for (const name of EXPORT_ORDER) {
    const cands = allCookies.filter(c => c.name === name);
    if (cands.length) {
      cands.sort((a, b) => (b.domain === ".google.com" ? 1 : 0) - (a.domain === ".google.com" ? 1 : 0));
      selected.set(name, cands[0].value);
    }
  }
  const sapisid = selected.get("SAPISID") || "";
  const cookieNames = EXPORT_ORDER.filter(n => selected.has(n));
  const cookieStr = cookieNames.map(n => `${n}=${selected.get(n)}`).join("; ");

  if (!sapisid && !options?.allowSyncOnly) {
    return { ok: false, error: "SAPISID cookie missing — sign into Google in this browser first" };
  }

  return {
    ok: true,
    cookieCount: cookieNames.length,
    snapshot: snap,
    payload: {
      cookie: cookieStr,
      sapisid,
      sync_only: !sapisid,
      auth_user: snap.authUser || "0",
      xsrf_token: snap.xsrfToken || null,
      gemini_bl: snap.geminiBl || null,
      client_id: snap.ident.clientId,
      browser: snap.ident.browser,
      label: snap.ident.label,
      site: snap.site || "",
      provider: snap.provider || "",
      account_email: snap.accountEmail || "",
      model_badge: snap.modelBadge || "",
      providers: snap.providers,
    },
  };
}

async function autoSyncCookiesToLocalServer() {
  const bundle = await collectAuthBundle({ allowWithoutTab: true, allowSyncOnly: true });
  if (!bundle.ok) return;
  if (!bundle.payload.sapisid && !bundle.snapshot?.hasAnyAiTab) {
    return;
  }
  await fetch(`${LOCAL_API}/sync-cookies`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(bundle.payload),
    signal: AbortSignal.timeout(3000),
  }).catch(() => {});
}

// ── Core Relay Logic ──────────────────────────────────────────────────────────

function extractGeminiConvId(url) {
  const m = String(url || "").match(/^https:\/\/gemini\.google\.com(?:\/u\/\d+)?\/app\/([A-Za-z0-9_-]+)/i);
  return m ? m[1] : "";
}

function extractClaudeConvId(url) {
  const m = String(url || "").match(/^https:\/\/(?:www\.)?claude\.ai\/chat\/([A-Za-z0-9_-]+)/i);
  return m ? m[1] : "";
}

function extractChatgptConvId(url) {
  const m = String(url || "").match(/^https:\/\/(?:www\.)?(?:chatgpt\.com|chat\.openai\.com)\/(?:g\/[^/]+\/)?c\/([A-Za-z0-9_-]+)/i);
  return m ? m[1] : "";
}

function extractDeepseekConvId(url) {
  const m = String(url || "").match(/^https:\/\/(?:www\.)?chat\.deepseek\.com\/a\/chat\/s\/([A-Za-z0-9_-]+)/i);
  return m ? m[1] : "";
}

function extractVeniceConvId(url) {
  const s = String(url || "");
  const m = s.match(/^https:\/\/(?:www\.)?venice\.ai\/chat\/(?:agent|classic)\/([A-Za-z0-9_-]+)/i);
  if (m) return m[1];
  const g = s.match(/^https:\/\/(?:www\.)?venice\.ai\/chat\/(?!agent(?:\/|\?|$)|classic(?:\/|\?|$))([A-Za-z0-9_-]+)/i);
  return g ? g[1] : "";
}

function extractConvId(url, provider = "") {
  const p = normalizeProviderKey(provider) || detectTabProvider(url) || "gemini";
  if (p === "claude") return extractClaudeConvId(url);
  if (p === "chatgpt") return extractChatgptConvId(url);
  if (p === "deepseek") return extractDeepseekConvId(url);
  if (p === "venice") return extractVeniceConvId(url);
  return extractGeminiConvId(url);
}

async function getRelaySessions() {
  const res = await chrome.storage.local.get("relayChatsBySession");
  return res.relayChatsBySession && typeof res.relayChatsBySession === "object"
    ? res.relayChatsBySession
    : {};
}

async function saveRelaySession(sessionId, info, provider = "gemini") {
  const key = sessionStorageKey(sessionId, provider);
  if (!key) return;
  const sessions = await getRelaySessions();
  sessions[key] = {
    provider,
    chatUrl: info.chatUrl || "",
    convId: info.convId || "",
    tabId: info.tabId || null,
    authUser: info.authUser || "0",
    accountEmail: info.accountEmail || "",
    updatedAt: Date.now(),
  };
  const keys = Object.keys(sessions);
  if (keys.length > 24) {
    keys.sort((a, b) => (sessions[a]?.updatedAt || 0) - (sessions[b]?.updatedAt || 0));
    for (const oldKey of keys.slice(0, keys.length - 24)) {
      delete sessions[oldKey];
    }
  }
  await chrome.storage.local.set({ relayChatsBySession: sessions });
}

async function clearRelaySession(sessionId, provider = "") {
  if (!sessionId) return 0;
  const sessions = await getRelaySessions();
  const normProv = provider ? normalizeProviderKey(provider) : "";
  const exactKey = normProv ? sessionStorageKey(sessionId, normProv) : "";
  let removed = 0;
  for (const k of Object.keys(sessions)) {
    if (normProv) {
      if (k === exactKey || (normProv === "gemini" && k === sessionId)) {
        delete sessions[k];
        removed += 1;
      }
    } else if (
      k === sessionId ||
      k.startsWith(`${sessionId}::`) ||
      k.startsWith(`${sessionId}:role:`)
    ) {
      delete sessions[k];
      removed += 1;
    }
  }
  if (removed > 0) {
    await chrome.storage.local.set({ relayChatsBySession: sessions });
  }
  return removed;
}

async function protectAiTabs(tabs) {
  if (!Array.isArray(tabs) || !chrome?.tabs?.update) return;
  for (const t of tabs) {
    if (!t?.id || t.autoDiscardable === false) continue;
    try {
      await chrome.tabs.update(t.id, { autoDiscardable: false });
    } catch { /* ignore */ }
  }
}

function evaluateTabRecoveryAction(tab, convState) {
  if (!tab || !tab.id) return "open_replacement";
  if (tab.discarded || tab.status === "unloaded") return "wake_reload";
  if (!convState || !convState.hasEditor) return "wake_reload";
  return "ok";
}

function computeTailDomRange(totalCount, afterCount = 0) {
  const total = Math.max(0, Number(totalCount) || 0);
  if (total <= 0) return { start: 0, count: 0, hasNew: false };
  const after = Math.max(0, Number(afterCount) || 0);
  return {
    start: total > after ? total - 1 : Math.min(after, total - 1),
    count: total,
    hasNew: total > after,
  };
}

function pickBestTabForProvider(tabs, provider = "gemini", savedSession = null, fallbackChatUrl = "") {
  if (!Array.isArray(tabs) || tabs.length === 0) return null;
  const matching = tabs.filter(t => {
    const p = detectTabProvider(t.url);
    return !p || p === provider;
  });
  const pool = matching.length ? matching : (provider === "gemini" ? tabs : []);
  if (!pool.length) return null;

  const targetConvId = savedSession?.convId || extractConvId(fallbackChatUrl, provider);
  if (targetConvId) {
    const convMatch = pool.find(t => extractConvId(t.url, provider) === targetConvId);
    if (convMatch) return convMatch;
  }
  if (savedSession?.tabId) {
    const idMatch = pool.find(t => t.id === savedSession.tabId);
    if (idMatch) return idMatch;
  }
  const pathRe = PROVIDERS[normalizeProviderKey(provider)]?.pathRe || /\/app/i;
  return (
    pool.find(t => t.active && pathRe.test(t.url || "")) ||
    pool.find(t => t.active) ||
    pool.find(t => pathRe.test(t.url || "")) ||
    pool[0]
  );
}

function pickGeminiTab(tabs, savedSession = null, fallbackChatUrl = "") {
  return pickBestTabForProvider(tabs, "gemini", savedSession, fallbackChatUrl);
}

function selectEffectiveChatUrl(savedSession = null, pendingChatUrl = "", provider = "") {
  const pendingUrl = String(pendingChatUrl || "").trim();
  const inferredProvider = provider || detectTabProvider(pendingUrl) || detectTabProvider(savedSession?.chatUrl || "") || "gemini";
  const pendingConv = extractConvId(pendingUrl, inferredProvider);
  const savedUrl = String(savedSession?.chatUrl || "").trim();
  const savedConv = String(savedSession?.convId || extractConvId(savedUrl, inferredProvider) || "").trim();
  if (pendingConv) {
    return { chatUrl: pendingUrl, convId: pendingConv };
  }
  if (savedConv) {
    return { chatUrl: savedUrl || pendingUrl, convId: savedConv };
  }
  return { chatUrl: pendingUrl || savedUrl, convId: "" };
}

async function resolveFinalChatUrl(tabId, rawUrl = "", provider = "gemini") {
  const liveTab = await chrome.tabs.get(tabId).catch(() => null);
  const liveUrl = liveTab?.url || "";
  if (extractConvId(liveUrl, provider)) return liveUrl;
  if (extractConvId(rawUrl, provider)) return rawUrl;
  const deadline = Date.now() + 3500;
  while (Date.now() < deadline) {
    await sleep(300);
    const updated = await chrome.tabs.get(tabId).catch(() => null);
    if (extractConvId(updated?.url || "", provider)) return updated.url;
    try {
      const probed = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: (targetProvider) => {
          if (targetProvider === "claude") {
            if (/^\/chat\/[A-Za-z0-9_-]+/i.test(location.pathname || "")) {
              return location.href;
            }
            const sel = document.querySelector("a[aria-current='page'][href*='/chat/'], nav a[href*='/chat/'][data-active='true']");
            return sel?.href || "";
          }
          if (targetProvider === "chatgpt") {
            if (/^\/(?:g\/[^/]+\/)?c\/[A-Za-z0-9_-]+/i.test(location.pathname || "")) {
              return location.href;
            }
            const sel = document.querySelector("nav a[aria-current='page'][href*='/c/'], a[data-active='true'][href*='/c/']");
            return sel?.href || "";
          }
          if (targetProvider === "deepseek") {
            if (/^\/a\/chat\/s\/[A-Za-z0-9_-]+/i.test(location.pathname || "")) {
              return location.href;
            }
            const sel = document.querySelector("a[aria-current='page'][href*='/a/chat/s/'], nav a.active[href*='/a/chat/s/']");
            return sel?.href || "";
          }
          if (targetProvider === "venice") {
            const path = location.pathname || "";
            if (/^\/chat\/(?:agent|classic)\/[A-Za-z0-9_-]+/i.test(path) || /^\/chat\/(?!agent\/?$|classic\/?$)[A-Za-z0-9_-]+/i.test(path)) {
              return location.href;
            }
            const sel = document.querySelector("a[aria-current='page'][href*='/chat/'], a[data-active='true'][href*='/chat/']");
            return sel?.href || "";
          }
          if (/\/app\/[A-Za-z0-9_-]+/i.test(location.pathname || "")) {
            return location.href;
          }
          const sel = document.querySelector(
            "a[aria-current='page'][href*='/app/'], a.selected[href*='/app/'], [data-test-id='conversation'].selected a[href*='/app/']"
          );
          return sel?.href || "";
        },
        args: [provider],
      });
      const foundHref = probed?.[0]?.result || "";
      if (extractConvId(foundHref, provider)) return foundHref;
    } catch { /* ignore */ }
  }
  return liveUrl || rawUrl || "";
}

async function checkTabConversationState(tabId, provider = "gemini") {
  try {
    const res = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: (targetProvider) => {
        const isReadyEditor = (el) => {
          if (!el || el.disabled || el.readOnly || el.getAttribute?.("aria-disabled") === "true") return false;
          const tag = (el.tagName || "").toUpperCase();
          if (tag !== "TEXTAREA" && tag !== "INPUT") {
            if (el.isContentEditable === false || el.getAttribute?.("contenteditable") === "false") return false;
          }
          try {
            if (typeof getComputedStyle === "function") {
              const st = getComputedStyle(el);
              if (st && (st.display === "none" || st.visibility === "hidden" || parseFloat(st.opacity || "1") <= 0.05)) {
                return false;
              }
            }
            if (typeof el.getBoundingClientRect === "function") {
              const r = el.getBoundingClientRect();
              if (r.width <= 20 || r.height <= 5) return false;
            }
          } catch { /* ignore */ }
          return true;
        };
        const hasInteractiveEditor = (selectors) => {
          for (const sel of selectors) {
            for (const el of document.querySelectorAll(sel)) {
              if (isReadyEditor(el)) return true;
            }
          }
          return false;
        };
        const readyState = document.readyState || "complete";
        if (targetProvider === "claude") {
          const msgCount = document.querySelectorAll(
            "[data-testid='user-message'], [data-testid='assistant-message'], .font-claude-response, [data-is-streaming]"
          ).length;
          const hasEditor = hasInteractiveEditor([
            "[data-testid='chat-input']",
            "div.tiptap.ProseMirror[contenteditable='true']",
            "div.ProseMirror[contenteditable='true']",
            "[contenteditable='true'][role='textbox']",
          ]);
          const isAppRoot = /^\/(?:new)?\/?$/i.test(location.pathname || "");
          return { msgCount, hasEditor, isAppRoot, href: location.href, readyState };
        }
        if (targetProvider === "chatgpt") {
          const msgCount = document.querySelectorAll(
            "[data-content-search-turn-key], [data-message-author-role], article[data-testid^='conversation-turn-']"
          ).length;
          const hasEditor = hasInteractiveEditor([
            "div.ProseMirror[contenteditable='true']",
            "#prompt-textarea[contenteditable='true']",
            "[aria-label*='Ask ChatGPT' i][contenteditable='true']",
            "[contenteditable='true'][role='textbox']",
            "#prompt-textarea",
          ]);
          const isAppRoot = /^\/(?:g\/[^/]+\/)?$/i.test(location.pathname || "");
          return { msgCount, hasEditor, isAppRoot, href: location.href, readyState };
        }
        if (targetProvider === "deepseek") {
          const msgCount = document.querySelectorAll(
            ".ds-message, .ds-markdown, .ds-think-content"
          ).length;
          const hasEditor = hasInteractiveEditor([
            "textarea.ds-scroll-area",
            "textarea#chat-input",
            "textarea[placeholder*='DeepSeek' i]",
            "textarea",
            "[contenteditable='true'][role='textbox']",
          ]);
          const isAppRoot = /^\/?$/i.test(location.pathname || "");
          return { msgCount, hasEditor, isAppRoot, href: location.href, readyState };
        }
        if (targetProvider === "venice") {
          const msgCount = Array.from(document.querySelectorAll(
            ".minds-chat-scroll-root .prose, .fancy-card, main .prose, main [role='group']"
          )).filter((el) => !el.closest("[role='dialog'], form, header, nav, aside")).length;
          const hasEditor = hasInteractiveEditor([
            "textarea[data-slot='composer-field']",
            "[aria-label='Mind chat attachment dropzone'] textarea",
            "textarea[aria-label='Chat message input']",
            "textarea[aria-label*='Ask a question' i]",
            "textarea[aria-label*='Ask anything' i]",
            "textarea.chakra-textarea",
            "textarea",
            "[contenteditable='true'][role='textbox']",
          ]);
          const isAppRoot = /^\/chat(?:\/(?:agent|classic))?\/?$/i.test(location.pathname || "");
          return { msgCount, hasEditor, isAppRoot, href: location.href, readyState };
        }
        return {
          msgCount: document.querySelectorAll("message-content, model-response").length,
          hasEditor: hasInteractiveEditor([
            "[role='textbox'][contenteditable='true']",
            "rich-textarea .ql-editor",
            "div[role='textbox']",
            "div[contenteditable='true']",
            "textarea",
          ]),
          isAppRoot: /\/app\/?$/i.test(location.pathname || ""),
          href: location.href,
          readyState,
        };
      },
      args: [provider],
    });
    return res?.[0]?.result || null;
  } catch {
    return null;
  }
}

async function waitForConversationReady(tabId, timeoutMs = 4000, requireMessages = false, provider = "gemini") {
  const start = Date.now();
  let lastCount = -1;
  let lastInfo = null;
  while (Date.now() - start < timeoutMs) {
    const info = await checkTabConversationState(tabId, provider);
    if (info) lastInfo = info;
    if (info && info.hasEditor && info.readyState !== "loading") {
      if (!requireMessages) {
        if (info.msgCount === 0) return info;
      } else if (info.msgCount > 0) {
        // Require two matching snapshots so progressively hydrating threads finish mounting before baseline capture.
        if (info.msgCount === lastCount) {
          return info;
        }
        lastCount = info.msgCount;
      }
    } else {
      lastCount = -1;
    }
    await sleep(220);
  }
  return (await checkTabConversationState(tabId, provider)) || lastInfo;
}

async function waitForAnyEditorReady(tabId, timeoutMs = 12000, provider = "gemini") {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const info = await checkTabConversationState(tabId, provider);
    if (info && info.hasEditor && info.readyState !== "loading") {
      return info;
    }
    await sleep(250);
  }
  return await checkTabConversationState(tabId, provider);
}

async function openFreshAiTab(targetUrl = "https://gemini.google.com/app", timeoutMs = 25000, provider = "") {
  const p = normalizeProviderKey(provider) || detectTabProvider(targetUrl) || "gemini";
  const defaultUrl = PROVIDERS[p]?.defaultUrl || "https://gemini.google.com/app";
  const newTab = await chrome.tabs.create({ url: targetUrl || defaultUrl, active: false });
  await protectAiTabs([newTab]);
  await waitForTabComplete(newTab.id, timeoutMs).catch(() => {});
  const readyState = await waitForAnyEditorReady(newTab.id, 15000, p);
  const refreshed = await chrome.tabs.get(newTab.id).catch(() => newTab);
  return { tab: refreshed || newTab, convState: readyState };
}

async function ensureAliveAiTab(tab, preferredUrl = "", provider = "gemini") {
  const p = normalizeProviderKey(provider) || "gemini";
  const defaultUrl = PROVIDERS[p]?.defaultUrl || "https://gemini.google.com/app";
  const openUrl = preferredUrl || defaultUrl;
  let cur = tab?.id ? await chrome.tabs.get(tab.id).catch(() => null) : null;
  if (cur?.status === "loading") {
    await waitForTabComplete(cur.id, 20000).catch(() => {});
    cur = await chrome.tabs.get(cur.id).catch(() => cur);
  }
  let probe = cur ? await checkTabConversationState(cur.id, provider) : null;
  const action = evaluateTabRecoveryAction(cur, probe);
  if (action === "ok") {
    await protectAiTabs([cur]);
    return { tab: cur, recovered: false, convState: probe };
  }
  if (action === "wake_reload" && cur?.id) {
    console.warn(`[WSA] Waking/reloading dead or discarded ${provider} tab:`, cur.id, { discarded: cur.discarded, status: cur.status });
    try {
      await protectAiTabs([cur]);
      if (preferredUrl && cur.url !== preferredUrl) {
        await chrome.tabs.update(cur.id, { url: preferredUrl, autoDiscardable: false });
      } else {
        await chrome.tabs.reload(cur.id);
      }
      await waitForTabComplete(cur.id, 25000).catch(() => {});
      const readyState = await waitForAnyEditorReady(cur.id, 12000, provider);
      const woken = await chrome.tabs.get(cur.id).catch(() => cur);
      if (cachedTabIdentities[provider]?.tabId === cur.id) cachedTabIdentities[provider].ts = 0;
      if (readyState?.hasEditor) {
        return { tab: woken || cur, recovered: true, convState: readyState };
      }
    } catch { /* fall through to opening replacement tab */ }
  }
  console.warn(`[WSA] Opening fresh ${provider} tab after dead-tab recovery failed on:`, tab?.id || "none");
  const created = await openFreshAiTab(openUrl, 25000, provider);
  if (created?.tab?.id && cachedTabIdentities[provider]?.tabId === tab?.id) {
    cachedTabIdentities[provider].ts = 0;
  }
  return { tab: created.tab, recovered: true, convState: created.convState };
}

async function ensureAliveGeminiTab(tab, preferredUrl = "") {
  return ensureAliveAiTab(tab, preferredUrl, "gemini");
}

async function ensureCleanNewChat(tab, provider = "gemini") {
  let alreadyClean = false;
  let clickedSpaNewChat = false;
  try {
    const navRes = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      func: (targetProvider) => {
        if (targetProvider === "claude") {
          const msgCount = document.querySelectorAll(
            "[data-testid='user-message'], [data-testid='assistant-message'], .font-claude-response, [data-is-streaming]"
          ).length;
          const isRoot = /^\/(?:new)?\/?$/i.test(location.pathname || "");
          if (msgCount === 0 && isRoot) {
            return { status: "already_clean" };
          }
          const btn = document.querySelector(
            "a[href='/new'], [data-testid='new-chat-button'], button[aria-label*='New chat' i], a[aria-label*='New chat' i]"
          );
          if (btn) {
            btn.click();
            return { status: "clicked" };
          }
          return { status: "no_button" };
        }
        if (targetProvider === "chatgpt") {
          const msgCount = document.querySelectorAll(
            "[data-content-search-turn-key], [data-message-author-role], article[data-testid^='conversation-turn-']"
          ).length;
          const isRoot = /^\/(?:g\/[^/]+\/)?$/i.test(location.pathname || "");
          if (msgCount === 0 && isRoot) {
            return { status: "already_clean" };
          }
          const btn = document.querySelector(
            "a[data-testid='create-new-chat-button'], button[data-testid='create-new-chat-button'], button[aria-label*='New chat' i], a[aria-label*='New chat' i], nav a[href='/']"
          );
          if (btn) {
            btn.click();
            return { status: "clicked" };
          }
          return { status: "no_button" };
        }
        if (targetProvider === "deepseek") {
          const msgCount = document.querySelectorAll(
            ".ds-message, .ds-markdown, .ds-think-content"
          ).length;
          const isRoot = /^\/?$/i.test(location.pathname || "");
          if (msgCount === 0 && isRoot) {
            return { status: "already_clean" };
          }
          let btn = document.querySelector(
            "[data-testid='new-chat-button'], button[aria-label*='New chat' i], div[role='button'][aria-label*='New chat' i], a[aria-label*='New chat' i]"
          );
          if (!btn) {
            for (const cand of document.querySelectorAll("button, [role='button'], a")) {
              const txt = (cand.innerText || cand.textContent || "").replace(/\s+/g, " ").trim();
              if (/^New\s*chat$/i.test(txt)) { btn = cand; break; }
            }
          }
          if (btn) {
            btn.click();
            return { status: "clicked" };
          }
          return { status: "no_button" };
        }
        if (targetProvider === "venice") {
          const msgCount = Array.from(document.querySelectorAll(
            ".minds-chat-scroll-root .prose, .fancy-card, main .prose, main [role='group']"
          )).filter((el) => !el.closest("[role='dialog'], form, header, nav, aside")).length;
          const isRoot = /^\/chat(?:\/(?:agent|classic))?\/?$/i.test(location.pathname || "");
          if (msgCount === 0 && isRoot) {
            return { status: "already_clean" };
          }
          let btn = document.querySelector(
            "button[aria-label*='New chat' i], a[aria-label*='New chat' i], a[href='/chat/agent'], a[href='/chat/classic']"
          );
          if (!btn) {
            for (const cand of document.querySelectorAll("button, [role='button'], a")) {
              const txt = (cand.innerText || cand.textContent || "").replace(/\s+/g, " ").trim();
              if (/^New\s*chat$/i.test(txt)) { btn = cand; break; }
            }
          }
          if (btn) {
            btn.click();
            return { status: "clicked" };
          }
          return { status: "no_button" };
        }
        const msgCount = document.querySelectorAll("message-content, model-response").length;
        const isAppRoot = /\/app\/?$/i.test(location.pathname || "");
        if (msgCount === 0 && isAppRoot) {
          return { status: "already_clean" };
        }
        const btn = document.querySelector(
          "gem-nav-list-item[data-test-id='new-chat-button'] a, " +
          "[data-test-id='new-chat-button'] a, " +
          "a[aria-label='New chat'], " +
          "button[aria-label='New chat']"
        );
        if (btn) {
          btn.click();
          return { status: "clicked" };
        }
        return { status: "no_button" };
      },
      args: [provider],
    });
    const st = navRes?.[0]?.result?.status;
    if (st === "already_clean") {
      alreadyClean = true;
    } else if (st === "clicked") {
      clickedSpaNewChat = true;
      const info = await waitForConversationReady(tab.id, 3500, false, provider);
      if (info && info.msgCount === 0 && info.hasEditor) {
        alreadyClean = true;
      }
    }
  } catch { /* fallback to hard tab navigation below */ }

  if (!alreadyClean) {
    let target;
    if (provider === "claude") {
      target = "https://claude.ai/new";
    } else if (provider === "chatgpt") {
      const m = (tab.url || "").match(/^https:\/\/(?:www\.)?(?:chatgpt\.com|chat\.openai\.com)/i);
      target = (m ? m[0] : "https://chatgpt.com") + "/";
    } else if (provider === "deepseek") {
      target = "https://chat.deepseek.com/";
    } else if (provider === "venice") {
      target = /\/chat\/classic/i.test(tab.url || "") ? "https://venice.ai/chat/classic" : "https://venice.ai/chat/agent";
    } else {
      const m = (tab.url || "").match(/^https:\/\/gemini\.google\.com(\/u\/\d+)?/);
      target = (m ? m[0] : "https://gemini.google.com") + "/app";
    }
    await chrome.tabs.update(tab.id, { url: target, autoDiscardable: false });
    await waitForTabComplete(tab.id, 30000);
    await waitForAnyEditorReady(tab.id, 12000, provider);
  } else if (clickedSpaNewChat) {
    await sleep(250);
  } else {
    await waitForAnyEditorReady(tab.id, 6000, provider);
  }
}

async function getStoredActiveJob() {
  const r = await chrome.storage.local.get(["activeRelayJob"]);
  return r?.activeRelayJob || null;
}

function mutateRelayState(action) {
  const result = relayStateMutation.then(action);
  relayStateMutation = result.catch(() => {});
  return result;
}

async function setStoredActiveJob(job, create = false) {
  return mutateRelayState(async () => {
    const current = await getStoredActiveJob();
    if (create ? Boolean(current) : current?.reqId !== job.reqId) return false;
    // A late progress/error snapshot cannot replace a pending final result.
    if (current?.completion) return false;
    await chrome.storage.local.set({ activeRelayJob: job });
    return true;
  });
}

async function clearStoredActiveJob() {
  await chrome.storage.local.remove(["activeRelayJob"]);
}

function clearActivePollTimer() {
  if (activePollTimer) {
    clearTimeout(activePollTimer);
    activePollTimer = null;
  }
}

function scheduleActiveJobPoll(delayMs = 260) {
  clearActivePollTimer();
  activePollTimer = setTimeout(() => {
    activePollTimer = null;
    pollActiveRelayJob().catch(() => {});
  }, Math.max(20, Number(delayMs) || 260));
}

async function ensurePageRuntime(tabId, provider = "gemini", world = "MAIN") {
  const p = normalizeProviderKey(provider) || "gemini";
  const spec = PROVIDERS[p];
  const installMap = {
    claude: _claudeInstallPageRuntimeFn,
    deepseek: _deepseekInstallPageRuntimeFn,
    gemini: _domInstallPageRuntimeFn,
    chatgpt: _chatgptInstallPageRuntimeFn,
    venice: _veniceInstallPageRuntimeFn,
  };
  const installFn = installMap[p] || _domInstallPageRuntimeFn;
  const probe = await chrome.scripting.executeScript({
    target: { tabId },
    world,
    func: (rtKey, rtVer, needsUtils, rtRev) => Boolean(
      (!needsUtils || (globalThis.__wsaDomUtils && globalThis.__wsaDomUtils.version === 3 && globalThis.__wsaDomUtils.rev === 10 && typeof globalThis.__wsaDomUtils.detectPromptTooLongMessage === "function")) &&
      globalThis[rtKey] &&
      globalThis[rtKey].version === rtVer &&
      globalThis[rtKey].rev === rtRev
    ),
    args: [spec.runtimeKey, spec.runtimeVersion, Boolean(spec.needsDomUtils), (p === "chatgpt" || p === "claude") ? 8 : 7],
  });
  if (probe?.[0]?.result) return true;
  if (spec.needsDomUtils) {
    const uRes = await chrome.scripting.executeScript({
      target: { tabId },
      world,
      func: _installWsaDomUtilsFn,
    });
    if (uRes?.[0]?.error) {
      throw new Error(uRes[0].error.message || JSON.stringify(uRes[0].error));
    }
  }
  const res = await chrome.scripting.executeScript({
    target: { tabId },
    world,
    func: installFn,
  });
  if (res?.[0]?.error) {
    throw new Error(res[0].error.message || JSON.stringify(res[0].error));
  }
  return Boolean(res?.[0]?.result);
}

async function failActiveRelayJob(job, errMsg) {
  console.error("[WSA] DOM relay error:", errMsg);
  if (job?.reqId) {
    job.completion = {id: job.reqId, client_id: job.clientId || "", error: errMsg};
    if (!await setStoredActiveJob(job)) return;
    await deliverActiveRelayResult(job);
  }
}

async function cancelActiveRelayJob(job) {
  if ((await getStoredActiveJob())?.reqId !== job?.reqId) return;
  if (job?.tabId) {
    try {
      await chrome.scripting.executeScript({
        target: { tabId: job.tabId },
        world: job.world || "MAIN",
        func: (reqId, provider) => {
          const rtKeys = {
            claude: "__wsaClaudeRuntime",
            chatgpt: "__wsaChatgptRuntime",
            deepseek: "__wsaDeepseekRuntime",
            gemini: "__wsaRuntime",
            venice: "__wsaVeniceRuntime",
          };
          const rt = globalThis[rtKeys[provider] || "__wsaRuntime"];
          if (globalThis.__wsaActiveJob?.reqId === reqId) delete globalThis.__wsaActiveJob;
          if (rt?.turns && reqId) {
            delete rt.turns[reqId];
          }
          for (const b of document.querySelectorAll("button[aria-label*='Stop' i], button[data-testid='stop-button']")) {
            try {
              const r = b.getBoundingClientRect();
              const cs = getComputedStyle(b);
              if ((r.width > 0 || r.height > 0) && cs.display !== "none" && cs.visibility !== "hidden" && !b.disabled) {
                b.click();
                break;
              }
            } catch { /* ignore */ }
          }
        },
        args: [job.reqId || "", job.provider || "gemini"],
      });
    } catch { /* ignore tab closed */ }
  }
  await clearLocalRelayJobState(true, job.reqId);
  fastBurstUntil = Date.now() + 5000;
}

async function completeActiveRelayJob(job, pollRes) {
  if ((await getStoredActiveJob())?.reqId !== job.reqId) return;
  const provider = job.provider || "gemini";
  const responseText = typeof pollRes?.text === "string" ? pollRes.text : "";
  const thinkingText = typeof pollRes?.thinking === "string" ? pollRes.thinking : "";
  const modelName = typeof pollRes?.model === "string" ? pollRes.model : (job.modelBadge || "");
  const finalUrl = (await resolveFinalChatUrl(job.tabId, pollRes?.url || "", provider)) || pollRes?.url || "";

  if (!responseText) {
    await failActiveRelayJob(job, `${providerDisplayLabel(provider)} tab returned empty response`);
    return;
  }

  const saved = await mutateRelayState(async () => {
    const current = await getStoredActiveJob();
    if (current?.reqId !== job.reqId || current.completion) return false;
    injecting = true;
  if (job.sessionId) {
    let resolvedAuth = "0";
    if (provider === "gemini") {
      try {
        const uPath = new URL(finalUrl || "").pathname || "";
        resolvedAuth = /^\/u\/\d+(?:\/|$)/.test(uPath)
          ? parseAuthUserFromUrl(finalUrl)
          : (job.authUser || "0");
      } catch {
        resolvedAuth = job.authUser || "0";
      }
    }
    await saveRelaySession(job.sessionId, {
      chatUrl: finalUrl,
      convId: extractConvId(finalUrl, provider),
      tabId: job.tabId,
      authUser: resolvedAuth,
      accountEmail: job.accountEmail || "",
    }, provider);
  }

  job.completion = {
    id: job.reqId,
    client_id: job.clientId,
    provider,
    response: responseText,
    thinking: thinkingText,
    model: modelName,
    chat_url: finalUrl,
  };
    await chrome.storage.local.set({ activeRelayJob: job });
    return true;
  });
  if (!saved) return;
  await deliverActiveRelayResult(job);
}

async function deliverActiveRelayResult(job) {
  if (deliveryInFlight) return;
  deliveryInFlight = true;
  try {
    if ((await getStoredActiveJob())?.reqId !== job.reqId) return;
    injecting = true;
    const resp = await fetch(`${LOCAL_API}/pending-response`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: AbortSignal.timeout(5000),
      body: JSON.stringify(job.completion),
    });
    if (!resp.ok) throw new Error(`Result delivery HTTP ${resp.status}`);
    const ack = await resp.json();
    if (ack?.ok !== true && ack?.ok !== false) throw new Error("Invalid result acknowledgement");
    // ok:false is a definitive rejection (cancelled/expired/unknown job), not
    // success. Transport failures keep the exact payload for the next wakeup.
    if (!await clearLocalRelayJobState(true, job.reqId)) return;
    chrome.runtime.sendMessage({
      type: "RELAY_STATUS", ok: ack.ok && !job.completion.error,
      error: ack.ok ? job.completion.error : "Server no longer accepts this relay result",
      preview: (job.completion.response || "").slice(0, 80),
      model: job.completion.model || "",
    }).catch(() => {});
  } catch (err) {
    console.warn("[WSA] Result delivery deferred:", err?.message || String(err));
    scheduleActiveJobPoll(1500);
  } finally {
    deliveryInFlight = false;
  }
}

async function restoreActiveRelayJob() {
  const job = await getStoredActiveJob();
  if (!job?.reqId) return false;
  injecting = true;
  if (!activePollTimer && !pollInFlight) {
    scheduleActiveJobPoll(60);
  }
  return true;
}

async function pollActiveRelayJob() {
  if (pollInFlight || acquisitionInFlight || deliveryInFlight) return;
  pollInFlight = true;
  let polledReqId = null;
  try {
    const job = await getStoredActiveJob();
    if (!job?.reqId) {
      await clearLocalRelayJobState(false, null);
      return;
    }
    polledReqId = job.reqId;
    if (job.completion) {
      await deliverActiveRelayResult(job);
      return;
    }
    if (job.phase === "preparing" || !job.tabId) {
      await failActiveRelayJob(job, "Browser worker restarted before prompt submission; retry this request.");
      return;
    }
    if (job.reqId !== lastReconciledReqId) {
      lastReconciledReqId = job.reqId;
      const stillValid = await sendBusyHeartbeat(job);
      if (stillValid === false) return;
    }
    const provider = normalizeProviderKey(job.provider) || "gemini";
    const provLabel = providerDisplayLabel(provider);
    const pollFnMap = {
      claude: _claudePollTurnFn,
      deepseek: _deepseekPollTurnFn,
      chatgpt: _chatgptPollTurnFn,
      gemini: _domPollTurnFn,
      venice: _venicePollTurnFn,
    };
    const lastProgAge = Date.now() - (Number(job.lastProgressAt) || Number(job.startedAt) || Date.now());
    // Dynamic liveness timeout: never kill an actively thinking/generating turn on wall-clock duration.
    // Fail only if the tab produces neither text/thinking progress nor an active Stop/streaming signal for >6 minutes.
    if (lastProgAge > 360_000) {
      await failActiveRelayJob(job, `Timeout waiting for ${provLabel} response. Ensure an active conversation tab is open.`);
      return;
    }

    const tab = await chrome.tabs.get(job.tabId).catch(() => null);
    if (!tab) {
      await failActiveRelayJob(job, `${provLabel} tab was closed during response generation`);
      return;
    }
    if (tab.autoDiscardable !== false) {
      await protectAiTabs([tab]);
    }

    await ensurePageRuntime(job.tabId, provider, job.world || "MAIN");
    const resArr = await chrome.scripting.executeScript({
      target: { tabId: job.tabId },
      world: job.world || "MAIN",
      func: pollFnMap[provider] || _domPollTurnFn,
      args: [job.reqId],
    });
    if ((await getStoredActiveJob())?.reqId !== job.reqId) return;
    if (resArr?.[0]?.error) {
      await failActiveRelayJob(job, resArr[0].error.message || JSON.stringify(resArr[0].error));
      return;
    }
    const res = resArr?.[0]?.result;
    job.pollErrCount = 0;
    if (!res) {
      scheduleActiveJobPoll(300);
      return;
    }
    if (res.needRuntime) {
      await ensurePageRuntime(job.tabId, provider, job.world || "MAIN");
      scheduleActiveJobPoll(50);
      return;
    }
    if (res.missing) {
      if (job.phase === "starting" && Date.now() - (job.submissionStartedAt || job.startedAt) < 120_000) {
        scheduleActiveJobPoll(1000);
        return;
      }
      job.missingCount = (job.missingCount || 0) + 1;
      const maxMissing = job.phase === "starting" ? 30 : 12;
      if (job.missingCount >= maxMissing) {
        const detail = job.phase === "starting"
          ? "did not acknowledge submission and its page turn could not be found"
          : "lost the in-flight page turn before finishing the answer";
        await failActiveRelayJob(job, `${provLabel} ${detail}`);
        return;
      }
      if (!await setStoredActiveJob(job)) return;
      scheduleActiveJobPoll(300);
      return;
    }
    job.missingCount = 0;

    if (res.error) {
      await failActiveRelayJob(job, res.error);
      return;
    }

    const curText = typeof res.text === "string" ? res.text : "";
    const curThink = typeof res.thinking === "string" ? res.thinking : "";
    const isActivelyGenerating = Boolean(res.streaming || res.busy);
    const changed = curText !== (job.sentText || "") || curThink !== (job.sentThinking || "");
    job.isGenerating = isActivelyGenerating;
    if (changed || isActivelyGenerating) {
      job.lastProgressAt = Date.now();
    }

    if (res.done) {
      await completeActiveRelayJob(job, res);
      return;
    }

    if (changed) {
      job.seq = (job.seq || 0) + 1;
      job.lastHeartbeatAt = Date.now();
      if (!await setStoredActiveJob(job)) return;
      try {
        const pr = await fetch(`${LOCAL_API}/pending-progress`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          signal: AbortSignal.timeout(2500),
          body: JSON.stringify({
            id: job.reqId,
            client_id: job.clientId,
            provider,
            response: curText,
            thinking: curThink,
            model: res.model || job.modelBadge || "",
            chat_url: res.url || "",
            seq: job.seq,
          }),
        });
        const pjson = await pr.json().catch(() => null);
        if (pjson && pjson.cancelled) {
          await cancelActiveRelayJob(job);
          return;
        }
        if (pjson && pjson.ok === false) {
          job.serverMissCount = (job.serverMissCount || 0) + 1;
          if (job.serverMissCount >= 6) {
            await clearLocalRelayJobState(true, job.reqId);
            return;
          }
          if (!await setStoredActiveJob(job)) return;
        } else if (pr.ok && pjson?.ok) {
          // Watermarks represent acknowledged data. Resend an unchanged snapshot after a lost POST.
          job.sentText = curText;
          job.sentThinking = curThink;
          job.serverMissCount = 0;
          if (!await setStoredActiveJob(job)) return;
        }
      } catch { /* retry next tick */ }
    } else if ((Date.now() - (job.lastHeartbeatAt || 0)) >= 2500) {
      job.lastHeartbeatAt = Date.now();
      if (!await setStoredActiveJob(job)) return;
      const stillValid = await sendBusyHeartbeat(job);
      if (stillValid === false) return;
    }

    scheduleActiveJobPoll(260);
  } catch (e) {
    const msg = e?.message || String(e);
    console.warn("[WSA] Active poll transient error:", msg);
    const job = await getStoredActiveJob();
    if (job?.reqId !== polledReqId) return;
    if (job) {
      job.pollErrCount = (job.pollErrCount || 0) + 1;
      if (job.pollErrCount >= 10) {
        await failActiveRelayJob(job, msg);
      } else {
        await setStoredActiveJob(job);
        scheduleActiveJobPoll(400);
      }
    } else {
      injecting = false;
    }
  } finally {
    pollInFlight = false;
  }
}

async function checkAndRelay() {
  if (injecting || acquisitionInFlight) return;
  acquisitionInFlight = true;
  injecting = true;
  try {
    await acquireAndStartRelay();
  } finally {
    acquisitionInFlight = false;
    injecting = Boolean(await getStoredActiveJob());
    if (injecting && !activePollTimer && !pollInFlight) scheduleActiveJobPoll(60);
  }
}

async function acquireAndStartRelay() {
  if (await restoreActiveRelayJob()) return;

  const snap = await scanOpenSiteStates({ forceProbe: false });
  const ident = snap.ident;
  if (!snap.hasAnyAiTab) {
    const stored = await chrome.storage.local.get(["wasBoundRelay"]).catch(() => ({}));
    const isBoundBrowser = Boolean(stored?.wasBoundRelay) || (serverBinding.activeRelayId && serverBinding.activeRelayId === ident.clientId);
    if (!isBoundBrowser) return;
  }

  const qs = buildPendingQueryParams(snap);

  let pending;
  try {
    const r = await fetch(`${LOCAL_API}/pending-prompt?${qs.toString()}`, {
      signal: AbortSignal.timeout(2500),
    });
    if (!r.ok) return;
    const activeIdHeader = r.headers.get("X-Active-Relay-Id") || "";
    serverBinding = {
      isActive: r.headers.get("X-Relay-Active") === "1",
      activeRelayLabel: r.headers.get("X-Active-Relay") || "",
      activeRelayId: activeIdHeader,
      updatedAt: Date.now(),
    };
    if (activeIdHeader) {
      chrome.storage.local.set({ wasBoundRelay: activeIdHeader === ident.clientId }).catch(() => {});
    }
    pending = await r.json();
    if (pending?.browser_cmd && !browserCmdInFlight) {
      fastBurstUntil = Date.now() + 15000;
      handleBrowserCommand(pending.browser_cmd).catch(() => {});
    }
    if (!pending?.prompt) return;
    if (!snap.hasAnyAiTab && pending.target_relay_id && pending.target_relay_id !== ident.clientId) return;
  } catch {
    return;
  }

  injecting = true;
  lastBusyHeartbeatAt = Date.now();
  const provider = normalizeProviderKey(pending.provider) || detectTabProvider(pending.chat_url) || "gemini";
  // Persist ownership before navigation, identity probes, or page submission.
  const pendingJob = {reqId: pending.id, clientId: ident.clientId, provider,
    relayIdentity: ident,
    phase: "preparing", startedAt: Date.now(), lastProgressAt: Date.now()};
  if (!await setStoredActiveJob(pendingJob, true)) return;
  try {
    const sessionId = pending.session_id || "";
    let needsNewChat = Boolean(pending.new_chat);
    if (needsNewChat && sessionId) {
      await clearRelaySession(sessionId, provider);
    }
    const sessionsMap = (!needsNewChat && sessionId) ? await getRelaySessions() : {};
    let savedSession = (!needsNewChat && sessionId)
      ? (sessionsMap[sessionStorageKey(sessionId, provider)] || (provider === "gemini" ? sessionsMap[sessionId] : null) || null)
      : null;
    // hint_chat_url: server-side known thread URL passed when new_chat=false.
    // Merged into savedSession so the extension can navigate back to the right
    // conversation even if chrome.storage hasn't been updated yet (e.g. first
    // turn after a tab reload or a provider that returns the URL late).
    const hintChatUrl = String(pending.hint_chat_url || "").trim();
    if (!needsNewChat && hintChatUrl && savedSession && !savedSession.chatUrl) {
      savedSession = { ...savedSession, chatUrl: hintChatUrl };
    } else if (!needsNewChat && hintChatUrl && !savedSession) {
      savedSession = { chatUrl: hintChatUrl, convId: "", tabId: null, authUser: "0", accountEmail: "" };
    }
    const chosenChat = needsNewChat
      ? { chatUrl: "", convId: "" }
      : selectEffectiveChatUrl(savedSession, hintChatUrl || pending.chat_url || "", provider);
    const effectiveChatUrl = chosenChat.chatUrl;
    const effectiveConvId = chosenChat.convId;
    const expectedAuthUser = provider === "gemini"
      ? (savedSession?.authUser || (effectiveChatUrl ? parseAuthUserFromUrl(effectiveChatUrl) : ""))
      : "0";
    const expectedEmail = savedSession?.accountEmail || pending.account_email || "";

    const providerTabs = snap.tabsByProvider?.[provider] || [];
    let tab = pickBestTabForProvider(providerTabs, provider, savedSession, effectiveChatUrl) || snap.bestByProvider?.[provider] || null;
    let promptToInject = pending.prompt;
    const defaultUrl = PROVIDERS[provider]?.defaultUrl || "https://gemini.google.com/app";
    const alive = await ensureAliveAiTab(tab, effectiveChatUrl || defaultUrl, provider);
    tab = alive.tab;
    let reloadedForRelay = Boolean(alive?.recovered);
    await protectAiTabs([tab]);
    const curTabMeta = await readLiveTabIdentity(tab, { forceProbe: true });

    if (!needsNewChat && sessionId) {
      const sameEmail =
        Boolean(expectedEmail && curTabMeta.accountEmail) &&
        expectedEmail.toLowerCase() === curTabMeta.accountEmail.toLowerCase();
      const emailMismatch =
        Boolean(expectedEmail && curTabMeta.accountEmail) && !sameEmail;
      const sameAccountMatch = emailMismatch
        ? false
        : (provider === "gemini"
          ? (sameEmail || !expectedAuthUser || expectedAuthUser === (curTabMeta.authUser || "0"))
          : true);

      const currentConvId = extractConvId(tab.url, provider);
      let sameChatReady = false;
      const urlMatches =
        sameAccountMatch &&
        ((effectiveConvId && currentConvId === effectiveConvId) ||
         (!effectiveConvId && savedSession && (tab.id === savedSession.tabId || Boolean(currentConvId))));

      if (urlMatches) {
        const info = await waitForConversationReady(tab.id, 6000, true, provider);
        if (info && info.msgCount > 0 && info.hasEditor) {
          sameChatReady = true;
        }
      }

      if (!sameChatReady && sameAccountMatch && effectiveConvId && effectiveChatUrl) {
        try {
          await chrome.tabs.update(tab.id, { url: effectiveChatUrl, autoDiscardable: false });
          reloadedForRelay = true;
          await waitForTabComplete(tab.id, 20000);
          const restored = await waitForConversationReady(tab.id, 15000, true, provider);
          if (restored && restored.msgCount > 0 && restored.hasEditor) {
            sameChatReady = true;
          }
        } catch { /* fallback below */ }
      }

      if (!sameChatReady) {
        if (sessionId) {
          await clearRelaySession(sessionId, provider);
        }
        needsNewChat = true;
        promptToInject = pending.fallback_prompt || pending.prompt;
      }
    }

    const pendingImages = Array.isArray(pending.images) ? pending.images : [];
    console.log(
      `[WSA] Relaying prompt id=${pending.id} (provider=${provider}, relay=${ident.label}, session=${sessionId || "stateless"}, mode=${needsNewChat ? "new_chat" : "same_chat"}, chars=${promptToInject.length}` +
      (pendingImages.length ? `, images=${pendingImages.length})` : ")")
    );

    if (provider === "gemini" && pendingImages.length) {
      // A root URL can still contain a previous unsent screenshot. Preserve
      // that draft and keep it out of this request's attachment verification.
      const draft = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: "ISOLATED",
        func: () => {
          const editor = document.querySelector("rich-textarea .ql-editor, [role='textbox'][contenteditable='true'], textarea");
          const composer = editor?.closest("input-area-v2, form") || editor?.closest("fieldset");
          return Boolean(composer?.querySelector("uploader-file-preview, gem-media-attachment, button[aria-label='close attachment' i]") ||
            String(editor?.value || editor?.textContent || "").trim());
        },
      });
      if (draft?.[0]?.result === true) {
        if (!needsNewChat) {
          throw new Error("Gemini has an unsent draft. Send or clear it before continuing this chat.");
        }
        const accountBase = (tab.url || "").match(/^https:\/\/gemini\.google\.com(?:\/u\/\d+)?/);
        const fresh = await openFreshAiTab((accountBase?.[0] || "https://gemini.google.com") + "/app", 25000, provider);
        tab = fresh.tab;
      }
    }
    if (needsNewChat) {
      await ensureCleanNewChat(tab, provider);
      reloadedForRelay = false;
    } else if (reloadedForRelay) {
      // Allow freshly reloaded React/ProseMirror/Angular SPAs to finish attaching composer listeners and hydrating turns.
      await sleep(500);
      await waitForConversationReady(tab.id, 6000, Boolean(effectiveConvId), provider);
    }

    // Gemini's image path only needs the DOM. Keep its upload timers and turn
    // state in the extension context across page-script updates and worker wakes.
    const world = ["gemini", "claude", "deepseek"].includes(provider) && pendingImages.length ? "ISOLATED" : "MAIN";
    await ensurePageRuntime(tab.id, provider, world);
    if ((await getStoredActiveJob())?.reqId !== pending.id) return;
    // If the worker dies during submission, poll the existing page turn on
    // restart. Never blindly resubmit a prompt whose outcome is uncertain.
    if (!await setStoredActiveJob({...pendingJob, tabId: tab.id, sessionId,
      world, phase: "starting", submissionStartedAt: Date.now(), modelBadge: curTabMeta.modelBadge || ""})) return;
    const startFnMap = {
      claude: _claudeStartTurnFn,
      deepseek: _deepseekStartTurnFn,
      chatgpt: _chatgptStartTurnFn,
      gemini: _domStartTurnFn,
      venice: _veniceStartTurnFn,
    };
    const startResArr = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world,
      func: startFnMap[provider] || _domStartTurnFn,
      args: [pending.id, promptToInject, pendingImages],
    });
    if (startResArr?.[0]?.error) {
      throw new Error(startResArr[0].error.message || JSON.stringify(startResArr[0].error));
    }
    const startRes = startResArr?.[0]?.result;
    if (startRes == null) {
      // A missing injection return value is an unknown outcome, not proof the
      // prompt failed. The page may already be generating. Recover the saved
      // request by polling its page turn; never submit it a second time.
      scheduleActiveJobPoll(180);
      return;
    }
    if (!startRes?.ok) {
      throw new Error(startRes?.error || `Failed to submit prompt to ${provider} tab`);
    }
    if ((await getStoredActiveJob())?.reqId !== pending.id) return;

    const job = {
      reqId: pending.id,
      phase: "running",
      world,
      tabId: tab.id,
      provider,
      clientId: ident.clientId,
      relayIdentity: ident,
      sessionId,
      authUser: curTabMeta.authUser || (provider === "gemini" ? parseAuthUserFromUrl(tab.url || "") : "0"),
      accountEmail: curTabMeta.accountEmail || "",
      modelBadge: startRes.model || curTabMeta.modelBadge || "",
      startedAt: Date.now(),
      lastProgressAt: Date.now(),
      lastHeartbeatAt: Date.now(),
      seq: 0,
      sentText: "",
      sentThinking: "",
      missingCount: 0,
      serverMissCount: 0,
      pollErrCount: 0,
    };
    if (!await setStoredActiveJob(job)) return;
    // Notify the local server once submission (and any verified image upload) is confirmed.
    try {
      const stagePayload = {
        id: job.reqId,
        client_id: job.clientId,
        provider,
        stage: "submitted",
        model: job.modelBadge || "",
        seq: 0,
      };
      if (typeof startRes.images_attached === "boolean") {
        stagePayload.images_attached = startRes.images_attached;
      }
      if (typeof startRes.upload_ms === "number" && startRes.upload_ms >= 0) {
        stagePayload.upload_ms = Math.round(startRes.upload_ms);
      }
      if (typeof startRes.submit_ms === "number" && startRes.submit_ms >= 0) {
        stagePayload.submit_ms = Math.round(startRes.submit_ms);
      }
      fetch(`${LOCAL_API}/pending-progress`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: AbortSignal.timeout(2500),
        body: JSON.stringify(stagePayload),
      }).catch(() => {});
    } catch { /* best effort stage notification */ }
    scheduleActiveJobPoll(180);
  } catch (e) {
    if ((await getStoredActiveJob())?.reqId === pending.id) {
      await failActiveRelayJob(pendingJob, e?.message || String(e));
    }
  }
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

async function waitForTabComplete(tabId, timeoutMs) {
  const start = Date.now();
  await sleep(600);
  while (Date.now() - start < timeoutMs) {
    const t = await chrome.tabs.get(tabId).catch(() => null);
    if (!t) throw new Error("AI tab was closed during relay");
    if (t.status === "complete") return;
    await sleep(250);
  }
  throw new Error("AI tab did not finish loading within " + Math.round(timeoutMs / 1000) + "s");
}

// ── Message Handler (Popup & Content Script Bridge ↔ Background) ─────────────

if (typeof chrome !== "undefined" && chrome?.runtime?.id) {
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "PS_CAPTURE") {
    (async () => {
      const ident = await getRelayIdentity();
      const payload = Object.assign({}, msg, {
        client_id: ident.clientId,
        browser: ident.browser,
        label: ident.label,
      });
      const r = await fetch(`${LOCAL_API}/ps-relay`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const res = await r.json();
      sendResponse({ ok: Boolean(res?.ok), result: res });
    })().catch(err => sendResponse({ ok: false, error: err.message }));
    return true;
  }

  if (msg.type === "TAB_KEEPALIVE") {
    (async () => {
      if (typeof msg.isBrave === "boolean") {
        await chrome.storage.local.set({ detectedBrowser: normalizeBrowserHint(msg.isBrave) });
      }
      const active = await getRelayActive();
      if (active) {
        ensureFastPollLoop();
      }
      sendResponse({ ok: true, relayActive: active });
    })();
    return true;
  }

  if (msg.type === "SYNC_NOW") {
    (async () => {
      const explicitBrowser = typeof msg.isBrave === "boolean" ? normalizeBrowserHint(msg.isBrave) : "";
      if (explicitBrowser) {
        await chrome.storage.local.set({ detectedBrowser: explicitBrowser });
      }
      await setRelayActive(true, true);
      const bundle = await collectAuthBundle({ forceProbe: true, allowWithoutTab: true, allowSyncOnly: true, explicitBrowser });
      if (!bundle.ok) {
        sendResponse({ ok: false, error: bundle.error });
        return;
      }
      const snap = bundle.snapshot;
      if (!bundle.payload.sapisid && !snap.hasAnyAiTab) {
        sendResponse({ ok: false, error: "Open claude.ai, chat.deepseek.com, gemini.google.com, or chatgpt.com first" });
        return;
      }
      const sr = await fetch(`${LOCAL_API}/sync-cookies`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(bundle.payload),
        signal: AbortSignal.timeout(3500),
      }).catch(() => null);
      if (!sr || !sr.ok) {
        sendResponse({ ok: false, error: "WSA local server is offline (start wsa first)" });
        return;
      }

      const br = await fetch(`${LOCAL_API}/relay/select`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          client_id: snap.ident.clientId,
          browser: snap.ident.browser,
          label: snap.ident.label,
          auth_user: snap.authUser || "0",
          email: snap.accountEmail || "",
          site: snap.site || "",
          provider: snap.provider || "",
          model_badge: snap.modelBadge || "",
          providers: snap.providers,
        }),
        signal: AbortSignal.timeout(3000),
      }).catch(() => null);
      const bjson = br ? await br.json().catch(() => null) : null;
      if (bjson?.ok) {
        serverBinding = {
          isActive: true,
          activeRelayLabel: bjson.active_relay_label || snap.ident.label,
          activeRelayId: bjson.active_relay_id || snap.ident.clientId,
          updatedAt: Date.now(),
        };
        await chrome.storage.local.set({ wasBoundRelay: true });
      }
      if (injecting || (await getStoredActiveJob())) {
        await sendBusyHeartbeat(null, { triggerRelay: false }).catch(() => {});
      }
      await checkAndRelay().catch(() => {});
      sendResponse({
        ok: true,
        browser: snap.ident.browser,
        site: snap.site || "",
        provider: snap.provider || "",
        accountEmail: snap.accountEmail || "",
        modelBadge: snap.modelBadge || "",
        providers: snap.providers,
        status: {
          relayActive: true,
          injecting,
          serverReachable: true,
          authReady: Boolean(snap.xsrfToken || snap.hasClaudeTab || snap.hasDeepseekTab || snap.hasChatgptTab || snap.hasVeniceTab),
          hasGeminiBl: Boolean(snap.geminiBl),
          clientId: snap.ident.clientId,
          browser: snap.ident.browser,
          label: snap.ident.label,
          authUser: snap.authUser || "0",
          accountEmail: snap.accountEmail || "",
          modelBadge: snap.modelBadge || "",
          activeSite: snap.site || "",
          provider: snap.provider || "",
          providers: snap.providers,
          hasAiTab: snap.hasAnyAiTab,
          hasClaudeTab: snap.hasClaudeTab,
          hasDeepseekTab: snap.hasDeepseekTab,
          hasGeminiTab: snap.hasGeminiTab,
          hasChatgptTab: snap.hasChatgptTab,
          hasVeniceTab: snap.hasVeniceTab,
          tabCount: snap.tabCount,
          serverBinding,
        },
      });
    })().catch(err => sendResponse({ ok: false, error: err?.message || "Sync failed" }));
    return true;
  }

  if (msg.type === "GET_STATUS") {
    (async () => {
      const explicitBrowser = typeof msg.isBrave === "boolean" ? normalizeBrowserHint(msg.isBrave) : "";
      if (explicitBrowser) {
        await chrome.storage.local.set({ detectedBrowser: explicitBrowser });
      }
      const snap = await scanOpenSiteStates({ forceProbe: Boolean(msg.forceProbe), explicitBrowser });
      const active = await getRelayActive();

      let serverReachable = false;
      try {
        const hr = await fetch(`${LOCAL_API}/v1/health`, { signal: AbortSignal.timeout(700) });
        if (hr.ok) {
          const h = await hr.json().catch(() => null);
          if (h && typeof h === "object") {
            serverReachable = true;
            serverBinding = {
              isActive: Boolean(h.active_relay_id && h.active_relay_id === snap.ident.clientId),
              activeRelayLabel: h.active_relay || "",
              activeRelayId: h.active_relay_id || "",
              updatedAt: Date.now(),
            };
          }
        }
      } catch {
        serverReachable = false;
      }

      sendResponse({
        relayActive: active,
        injecting,
        serverReachable,
        authReady: Boolean(snap.xsrfToken || snap.hasClaudeTab || snap.hasDeepseekTab || snap.hasChatgptTab || snap.hasVeniceTab),
        hasGeminiBl: Boolean(snap.geminiBl),
        clientId: snap.ident.clientId,
        browser: snap.ident.browser,
        label: snap.ident.label,
        authUser: snap.authUser || "0",
        accountEmail: snap.accountEmail || "",
        modelBadge: snap.modelBadge || "",
        activeSite: snap.site || "",
        provider: snap.provider || "",
        providers: snap.providers,
        hasAiTab: snap.hasAnyAiTab,
        hasClaudeTab: snap.hasClaudeTab,
        hasDeepseekTab: snap.hasDeepseekTab,
        hasGeminiTab: snap.hasGeminiTab,
        hasChatgptTab: snap.hasChatgptTab,
        hasVeniceTab: snap.hasVeniceTab,
        tabCount: snap.tabCount,
        serverBinding,
      });
    })();
    return true;
  }

  if (msg.type === "EXPORT_AUTH") {
    (async () => {
      const explicitBrowser = typeof msg.isBrave === "boolean" ? normalizeBrowserHint(msg.isBrave) : "";
      if (explicitBrowser) {
        await chrome.storage.local.set({ detectedBrowser: explicitBrowser });
      }
      const bundle = await collectAuthBundle({ forceProbe: true, allowWithoutTab: true, allowSyncOnly: true, explicitBrowser });
      if (!bundle.ok) {
        sendResponse({ ok: false, error: bundle.error });
        return;
      }
      sendResponse({
        ok: true,
        cookieCount: bundle.cookieCount,
        payload: {
          cookie: bundle.payload.cookie,
          sapisid: bundle.payload.sapisid,
          auth_user: bundle.payload.auth_user,
          xsrf_token: bundle.payload.xsrf_token,
          gemini_bl: bundle.payload.gemini_bl,
          account_email: bundle.payload.account_email,
          browser: bundle.payload.browser,
          label: bundle.payload.label,
          site: bundle.payload.site,
          provider: bundle.payload.provider,
          model_badge: bundle.payload.model_badge,
          providers: bundle.payload.providers,
          cookie_count: bundle.cookieCount,
          exported_at: new Date().toISOString(),
        },
      });
    })().catch(err => sendResponse({ ok: false, error: err.message }));
    return true;
  }

  if (msg.type === "TEST_INJECT") {
    (async () => {
      const tabs = await chrome.tabs.query({ url: "https://gemini.google.com/*" });
      let tab = pickGeminiTab(tabs);
      if (!tab?.id) {
        sendResponse({ ok: false, error: "No Gemini tab open" });
        return;
      }

      try {
        const alive = await ensureAliveGeminiTab(tab, tab.url || "https://gemini.google.com/app");
        tab = alive.tab;
        const promptText = msg.prompt || "Reply with one short sentence confirming you are online.";
        const testReqId = "test-" + Date.now();
        await ensurePageRuntime(tab.id);
        const startResArr = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          world: "MAIN",
          func: _domStartTurnFn,
          args: [testReqId, promptText],
        });
        const startRes = startResArr?.[0]?.result;
        if (!startRes?.ok) {
          throw new Error(startRes?.error || "Test turn failed to start");
        }
        const deadline = Date.now() + 90_000;
        let finalOut = null;
        while (Date.now() < deadline) {
          await sleep(300);
          await ensurePageRuntime(tab.id);
          const pollArr = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            world: "MAIN",
            func: _domPollTurnFn,
            args: [testReqId],
          });
          const p = pollArr?.[0]?.result;
          if (p?.error) throw new Error(p.error);
          if (p?.done) {
            finalOut = p;
            break;
          }
        }
        if (!finalOut) throw new Error("Test turn timed out");
        sendResponse({ ok: true, result: finalOut.text || "", details: finalOut });
      } catch (e) {
        sendResponse({ ok: false, error: e.message });
      }
    })();
    return true;
  }
  });
}

// ── DOM + React 19 / Relay Injection Function (runs in page MAIN world) ─────

function _domInstallPageRuntimeFn() {
  const log = msg => console.log("%c[WSA-DOM] " + msg, "color:#1769e0;font-weight:bold");

  function computeTailDomRange(totalCount, afterCount = 0) {
    const total = Math.max(0, Number(totalCount) || 0);
    if (total <= 0) return { start: 0, count: 0, hasNew: false };
    const after = Math.max(0, Number(afterCount) || 0);
    return {
      start: total > after ? total - 1 : Math.min(after, total - 1),
      count: total,
      hasNew: total > after,
    };
  }

  function isVisibleElement(el) {
    if (!el) return false;
    try {
      let cur = el;
      for (let depth = 0; cur && depth < 24; depth++) {
        if (cur.hidden === true ||
            (typeof cur.hasAttribute === "function" && cur.hasAttribute("hidden")) ||
            (typeof cur.getAttribute === "function" && cur.getAttribute("aria-hidden") === "true" && cur.getAttribute("__wsa_probe__") !== "true")) {
          return false;
        }
        if (typeof getComputedStyle === "function" && (cur.nodeType === 1 || cur === el)) {
          const cs = getComputedStyle(cur);
          if (cs && (cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity || "1") <= 0.05)) {
            return false;
          }
        }
        cur = cur.parentElement || null;
      }
      const rect = typeof el.getBoundingClientRect === "function" ? el.getBoundingClientRect() : null;
      if (rect && rect.width <= 1 && rect.height <= 1 && (!el.getClientRects || el.getClientRects().length === 0)) {
        if (el.offsetParent === null || rect.width <= 0 || rect.height <= 0) return false;
      }
    } catch { /* ignore */ }
    return Boolean(el.offsetParent !== null || (typeof el.getClientRects === "function" && el.getClientRects().length > 0));
  }

  function findEditor() {
    const selectors = [
      "[role='textbox'][contenteditable='true']",
      "div[role='textbox']",
      "rich-textarea .ql-editor",
      "div[contenteditable='true']",
      "textarea",
    ];
    for (const sel of selectors) {
      for (const el of document.querySelectorAll(sel)) {
        const r = el.getBoundingClientRect();
        if (r.width > 40 && r.height > 5) return el;
      }
    }
    return null;
  }

  function inspectReactContext(el) {
    let composerProps = null;
    let sessionObj = null;
    let relayEnv = null;
    if (!el) return { composerProps, sessionObj, relayEnv };
    const fiberKey = Object.keys(el).find(k => k.startsWith("__reactFiber$"));
    let f = fiberKey ? el[fiberKey] : null;
    let depth = 0;
    while (f && depth < 60) {
      const mp = f.memoizedProps;
      if (mp && typeof mp === "object") {
        if (!composerProps && typeof mp.onSend === "function" && typeof mp.onChange === "function") {
          composerProps = mp;
        }
        if (!sessionObj && mp.session && typeof mp.session.send === "function") {
          sessionObj = mp.session;
        }
      }
      let dep = f.dependencies?.firstContext;
      while (dep) {
        const val = dep.memoizedValue;
        if (val?.environment && typeof val.environment.getStore === "function") {
          relayEnv = val.environment;
        } else if (val && typeof val.getStore === "function") {
          relayEnv = val;
        }
        dep = dep.next;
      }
      f = f.return;
      depth++;
    }
    return { composerProps, sessionObj, relayEnv };
  }

  function readModePickerBadge() {
    const btn = document.querySelector(
      "button[aria-label*='mode picker' i], button[aria-label*='Switch model' i], button[aria-label*='model picker' i], [data-test-id='bard-mode-menu-button'] button, [data-test-id='bard-mode-menu-button']"
    );
    if (btn) {
      const aria = btn.getAttribute("aria-label") || "";
      const m = aria.match(/currently\s+(.+)$/i);
      if (m) return m[1].replace(/\s+/g, " ").trim();
      const txt = (btn.innerText || btn.textContent || "").replace(/\s+/g, " ").trim();
      if (txt) return txt;
    }
    const modelRe = /^(?:\d+(?:\.\d+)?\s*)?(?:Gemini\s+)?(Argon|Carbon|Pro|Flash(?:-Lite|\s+Lite)?|Fast|Thinking|Auto)\b/i;
    for (const b of document.querySelectorAll("button")) {
      if (!isVisibleElement(b)) continue;
      const t = (b.innerText || b.textContent || "").replace(/\s+/g, " ").trim();
      if (t && t.length <= 48 && modelRe.test(t)) return t;
    }
    return "";
  }

  function readRelayCandidates(relayEnv) {
    const obj = {};
    if (!relayEnv) return obj;
    try {
      const source = relayEnv.getStore().getSource();
      const ids = typeof source.getRecordIDs === "function" ? source.getRecordIDs() : [];
      for (const id of ids) {
        const rec = source.get(id);
        if (!rec || typeof rec !== "object") continue;
        const md = typeof rec.markdownResponse === "string"
          ? rec.markdownResponse
          : (typeof rec.getValue === "function" ? rec.getValue("markdownResponse") : null);
        const typeName = rec.__typename || (typeof rec.getType === "function" ? rec.getType() : "");
        if (typeName === "assistant_lamda_ClientModelResponse" || typeof md === "string" || id.includes(":candidate:")) {
          const thinkingParts = [];
          try {
            const prefix = id.split(":candidate:")[0];
            for (const tid of ids) {
              if (!tid.startsWith(prefix)) continue;
              const trec = source.get(tid);
              if (!trec) continue;
              const tHeader = trec.thoughtToolHeader || (typeof trec.getValue === "function" ? trec.getValue("thoughtToolHeader") : null);
              const tBody = trec.thoughtBody || (typeof trec.getValue === "function" ? trec.getValue("thoughtBody") : null);
              if (tHeader) thinkingParts.push(String(tHeader));
              if (tBody) thinkingParts.push(String(tBody));
            }
          } catch { /* ignore */ }
          obj[id] = {
            id,
            markdown: typeof md === "string" ? md : "",
            thinking: thinkingParts.join("\n").trim(),
          };
        }
      }
    } catch (e) {
      log("Relay read warning: " + e.message);
    }
    return obj;
  }

  function stripTrailingFenceArtifact(rawCode) {
    return String(rawCode || "")
      .replace(/[\u200b\u200c\u200d\ufeff]+/g, "")
      .replace(/(?:\r?\n[ \t]*)?(?:`{3,}|~{3,})[ \t]*(?:_\s*[\]\*]|_\])?[ \t]*$/, "")
      .trim();
  }

  function normalizeRelayMarkdown(rawText) {
    if (!rawText) return "";
    return String(rawText)
      .replace(/(^|\n)([ \t]*(?:`{3,}|~{3,}))[\u200b\u200c\u200d\ufeff]+/g, "$1$2")
      .replace(/(^|\n)([ \t]*(?:`{3,}|~{3,})[ \t]*)(?:_\s*[\]\*]|_\])([ \t]*(?=\r?\n|$))/g, "$1$2$3");
  }

  function pickBestChangedRelayCandidate(currentObj, beforeObj) {
    let sawNewTurn = false;
    let bestText = "";
    let bestThinking = "";
    const cur = currentObj || {};
    const prev = beforeObj || {};
    for (const cid of Object.keys(cur)) {
      const entry = cur[cid];
      if (!entry) continue;
      const old = prev[cid];
      const normMd = entry.markdown ? normalizeRelayMarkdown(entry.markdown) : "";
      const normOld = old?.markdown ? normalizeRelayMarkdown(old.markdown) : "";
      const think = entry.thinking || "";
      const oldThink = old?.thinking || "";
      if (!old || normMd !== normOld || think !== oldThink) {
        sawNewTurn = true;
        if (normMd.length >= bestText.length && normMd) {
          bestText = normMd;
        }
        if (think.length >= bestThinking.length && think) {
          bestThinking = think;
        }
      }
    }
    return { sawNewTurn, text: bestText, thinking: bestThinking };
  }

  function isToolCallPayload(rawCode) {
    const trimmed = stripTrailingFenceArtifact(rawCode);
    if (!trimmed || (trimmed[0] !== "{" && trimmed[0] !== "[")) return false;
    try {
      const parsed = JSON.parse(trimmed);
      const items = Array.isArray(parsed) ? parsed : [parsed];
      return items.length > 0 && items.every(
        item => item && typeof item === "object" && typeof item.name === "string" && item.name.length > 0
      );
    } catch {
      return false;
    }
  }

  function extractBalancedJsonSpan(text, startIdx) {
    const openCh = text[startIdx];
    const closeCh = openCh === "{" ? "}" : "]";
    let depth = 0;
    let inStr = false;
    let esc = false;
    for (let i = startIdx; i < text.length; i++) {
      const ch = text[i];
      if (inStr) {
        if (esc) esc = false;
        else if (ch === "\\") esc = true;
        else if (ch === '"') inStr = false;
        continue;
      }
      if (ch === '"') inStr = true;
      else if (ch === openCh) depth++;
      else if (ch === closeCh) {
        depth--;
        if (depth === 0) return i + 1;
      }
    }
    return -1;
  }

  function normalizeDomToolCallBlocks(rawText) {
    if (!rawText) return "";
    const markerRe = /(?:^|\r?\n)Code snippet[ \t]*(?:\r?\n)+/g;
    let out = "";
    let cursor = 0;
    let m;
    while ((m = markerRe.exec(rawText)) !== null) {
      let idx = m.index + m[0].length;
      while (idx < rawText.length && (rawText[idx] === " " || rawText[idx] === "\t")) idx++;
      if (idx >= rawText.length || (rawText[idx] !== "{" && rawText[idx] !== "[")) continue;
      const endIdx = extractBalancedJsonSpan(rawText, idx);
      if (endIdx <= idx) continue;
      const jsonSlice = rawText.slice(idx, endIdx);
      if (!isToolCallPayload(jsonSlice)) continue;
      out += rawText.slice(cursor, m.index) + "\n```tool_call\n" + jsonSlice.trim() + "\n```";
      cursor = endIdx;
      markerRe.lastIndex = endIdx;
    }
    out += rawText.slice(cursor);
    return out;
  }

  function maxBacktickRun(str) {
    let maxRun = 0;
    const matches = String(str || "").match(/`+/g);
    if (matches) {
      for (const m of matches) {
        if (m.length > maxRun) maxRun = m.length;
      }
    }
    return maxRun;
  }

  function domNodeToMarkdown(root) {
    if (!root) return "";
    function walk(node, inHeading = false) {
      if (!node) return "";
      if (node.nodeType === 3) {
        return node.nodeValue || "";
      }
      if (node.nodeType !== 1) return "";
      const tag = (node.tagName || "").toUpperCase();
      if (
        tag === "BUTTON" || tag === "SVG" || tag === "STYLE" || tag === "SCRIPT" ||
        tag === "ANNOTATION" || tag === "MAT-ICON" ||
        tag === "MODEL-THOUGHTS" || tag === "THOUGHT-VIEW" || tag === "THINKING-OVERLAY" ||
        tag === "MESSAGE-ACTIONS" || tag === "TTS-CONTROL" || tag === "TABLE-ACTIONS"
      ) {
        return "";
      }
      const cls = typeof node.className === "string" ? node.className : "";
      if (
        cls.includes("code-block-decoration") ||
        cls.includes("katex-mathml") ||
        cls.includes("katex-html") ||
        cls.includes("thoughts-content") ||
        cls.includes("thought-container") ||
        cls.includes("table-footer") ||
        cls.includes("hide-from-message-actions")
      ) {
        return "";
      }
      if (tag === "CODE-BLOCK") {
        const headerSpan = node.querySelector(".code-block-decoration span, .code-block-decoration");
        const rawHeader = (headerSpan?.textContent || "").replace(/Download code|Copy code/gi, "").trim();
        const codeEl = node.querySelector("pre code, code, pre");
        const codeText = (codeEl?.textContent || "").replace(/\r\n/g, "\n").replace(/\n+$/, "");
        if (isToolCallPayload(codeText)) {
          return "\n\n```tool_call\n" + stripTrailingFenceArtifact(codeText) + "\n```\n\n";
        }
        const lowerHeader = rawHeader.toLowerCase();
        const lang =
          lowerHeader && lowerHeader !== "code snippet" && /^[a-z0-9_+-]{1,20}$/.test(lowerHeader)
            ? lowerHeader
            : "";
        const fence = "`".repeat(Math.max(3, maxBacktickRun(codeText) + 1));
        return "\n\n" + fence + lang + "\n" + codeText + "\n" + fence + "\n\n";
      }
      if (tag === "PRE") {
        const codeEl = node.querySelector("code") || node;
        const codeText = (codeEl.textContent || "").replace(/\r\n/g, "\n").replace(/\n+$/, "");
        if (isToolCallPayload(codeText)) {
          return "\n\n```tool_call\n" + stripTrailingFenceArtifact(codeText) + "\n```\n\n";
        }
        const fence = "`".repeat(Math.max(3, maxBacktickRun(codeText) + 1));
        return "\n\n" + fence + "\n" + codeText + "\n" + fence + "\n\n";
      }
      if (tag === "CODE") {
        const txt = node.textContent || "";
        if (!txt) return "";
        const fence = "`".repeat(maxBacktickRun(txt) + 1);
        const pad = txt.startsWith("`") || txt.endsWith("`") ? " " : "";
        return fence + pad + txt + pad + fence;
      }
      if (tag === "TABLE" || (typeof node.getAttribute === "function" && node.getAttribute("role") === "table")) {
        const isHtmlTable = tag === "TABLE";
        const rowSelector = isHtmlTable ? "tr" : "[role='row']";
        const allRows = [...(node.querySelectorAll ? node.querySelectorAll(rowSelector) : [])]
          .filter(r => !r.closest || r.closest(isHtmlTable ? "table" : "[role='table']") === node);
        const rows = [];
        let maxCols = 0;
        const rawCellRows = [];
        for (const rowEl of allRows) {
          const kids = [...(rowEl.children || [])].filter(c => {
            const ct = (c.tagName || "").toUpperCase();
            if (ct === "TH" || ct === "TD") return true;
            const role = typeof c.getAttribute === "function" ? c.getAttribute("role") : "";
            return role === "columnheader" || role === "cell" || role === "rowheader";
          });
          if (kids.length === 0) continue;
          const cellTexts = kids.map(c =>
            walk(c, false).replace(/\r?\n+/g, " ").replace(/\|/g, "\\|").replace(/\s+/g, " ").trim() || " "
          );
          if (cellTexts.length > maxCols) maxCols = cellTexts.length;
          rawCellRows.push(cellTexts);
        }
        for (let rIdx = 0; rIdx < rawCellRows.length; rIdx++) {
          const padded = rawCellRows[rIdx].slice();
          while (padded.length < maxCols) padded.push(" ");
          rows.push("| " + padded.join(" | ") + " |");
          if (rIdx === 0) {
            rows.push("| " + Array(maxCols).fill("---").join(" | ") + " |");
          }
        }
        return rows.length ? "\n\n" + rows.join("\n") + "\n\n" : "";
      }
      if (tag === "BR") return "\n";
      if (tag === "HR") return "\n\n---\n\n";

      const childHeading = inHeading || /^H[1-6]$/.test(tag);
      let inner = "";
      for (const child of node.childNodes) {
        inner += walk(child, childHeading);
      }
      if (/^H[1-6]$/.test(tag)) {
        const level = Number(tag[1]);
        return "\n\n" + "#".repeat(level) + " " + inner.trim() + "\n\n";
      }
      if ((tag === "STRONG" || tag === "B") && !inHeading) {
        const trimmed = inner.trim();
        return trimmed ? "**" + trimmed + "**" : inner;
      }
      if ((tag === "EM" || tag === "I") && !inHeading) {
        const trimmed = inner.trim();
        return trimmed ? "*" + trimmed + "*" : inner;
      }
      if (tag === "DEL" || tag === "S") {
        const trimmed = inner.trim();
        return trimmed ? "~~" + trimmed + "~~" : inner;
      }
      if (tag === "A") {
        const label = inner.trim();
        const href = typeof node.getAttribute === "function" ? (node.getAttribute("href") || "").trim() : "";
        if (label && href && /^https?:\/\//i.test(href) && label !== href && !label.includes("](")) {
          return `[${label}](${href})`;
        }
        return label || href;
      }
      if (tag === "BLOCKQUOTE") {
        const lines = inner.trim().split("\n").map(ln => ln.trim()).filter(Boolean);
        return lines.length ? "\n\n" + lines.map(ln => "> " + ln).join("\n") + "\n\n" : "";
      }
      if (tag === "LI") {
        const cleanLi = inner.replace(/^\n+|\n+$/g, "");
        if (!cleanLi) return "";
        const parentEl = node.parentElement;
        if (parentEl && (parentEl.tagName || "").toUpperCase() === "OL") {
          const start = Number(parentEl.getAttribute?.("start") || 1) || 1;
          let olPos = 0;
          for (const sib of parentEl.children || []) {
            if ((sib.tagName || "").toUpperCase() === "LI") olPos++;
            if (sib === node) break;
          }
          const num = start + Math.max(0, olPos - 1);
          return `\n${num}. ` + cleanLi.replace(/\n/g, "\n   ");
        }
        return "\n- " + cleanLi.replace(/\n/g, "\n  ");
      }
      if (tag === "P" || tag === "TR") {
        const t = inner.trim();
        return t ? "\n\n" + t + "\n\n" : "";
      }
      if (tag === "UL" || tag === "OL" || tag === "DIV" || tag === "SECTION" || tag === "STRUCTURED-CONTENT-CONTAINER") {
        return inner ? "\n" + inner + "\n" : "";
      }
      return inner;
    }

    const raw = walk(root, false)
      .replace(/[ \t]+\n/g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
    return normalizeDomToolCallBlocks(raw);
  }

  function readDomAssistantState(afterCount = 0) {
    const mcEls = [...document.querySelectorAll("message-content")].filter(isVisibleElement);
    if (mcEls.length > 0) {
      const range = computeTailDomRange(mcEls.length, afterCount);
      const tailText = range.hasNew ? domNodeToMarkdown(mcEls[range.start]) : "";
      return { count: range.count, tailText };
    }
    const h6Matches = [];
    for (const h6 of document.querySelectorAll("h6")) {
      if ((h6.textContent || "").trim() === "Gemini said") {
        const parent = h6.parentElement;
        const next = parent?.nextElementSibling || h6.nextElementSibling;
        const container = next || parent;
        if (container) h6Matches.push(container);
      }
    }
    if (h6Matches.length > 0) {
      const range = computeTailDomRange(h6Matches.length, afterCount);
      const tailText = range.hasNew
        ? domNodeToMarkdown(h6Matches[range.start]).replace(/^######\s*Gemini said\s*/i, "").replace(/^Gemini said\s*/i, "").trim()
        : "";
      return { count: range.count, tailText };
    }
    for (const sel of [".model-response-text", "[data-test-id='model-response-text']", "model-response", "[data-response-index]"]) {
      const els = [...document.querySelectorAll(sel)].filter(isVisibleElement);
      if (els.length > 0) {
        const range = computeTailDomRange(els.length, afterCount);
        const tailText = range.hasNew ? domNodeToMarkdown(els[range.start]) : "";
        return { count: range.count, tailText };
      }
    }
    return { count: 0, tailText: "" };
  }

  const _ACTION_BTN_SEL =
    "message-actions button, button[aria-label*='Copy response' i], button[aria-label*='Good response' i], " +
    "button[aria-label*='Bad response' i], button[aria-label*='Share' i], button[aria-label*='Regenerate' i], " +
    "button[aria-label*='Redo' i], message-actions button[data-test-id='copy-button']";

  function isTurnActionButton(btn) {
    if (!btn || !isVisibleElement(btn)) return false;
    if (typeof btn.closest === "function") {
      if (btn.closest("code-block, .code-block-decoration, pre, table, table-actions, .table-footer, user-query, input-container")) {
        return false;
      }
      if (btn.closest("message-actions")) return true;
    }
    const label = ((btn.getAttribute("aria-label") || "") + " " + (btn.getAttribute("data-test-id") || "")).toLowerCase();
    if (label.includes("copy code") || label.includes("copy table") || label.includes("download")) {
      return false;
    }
    return true;
  }

  function countVisibleActionButtons() {
    let n = 0;
    for (const btn of document.querySelectorAll(_ACTION_BTN_SEL)) {
      if (isTurnActionButton(btn)) n++;
    }
    return n;
  }

  function isStopButtonVisible() {
    const stopBtns = document.querySelectorAll(
      "button[aria-label*='Stop response' i], button[aria-label*='Stop streaming' i], button[aria-label*='Stop generating' i], " +
      "button[aria-label='Stop' i], button[mattooltip*='Stop' i], button[data-test-id*='stop' i]"
    );
    for (const btn of stopBtns) {
      if (isVisibleElement(btn) && !btn.disabled && btn.getAttribute("aria-disabled") !== "true") {
        return true;
      }
    }
    for (const icon of document.querySelectorAll("mat-icon[fonticon='stop'], mat-icon[data-mat-icon-name='stop']")) {
      const btn = typeof icon.closest === "function" ? icon.closest("button") : null;
      if (btn && isVisibleElement(btn) && !btn.disabled && btn.getAttribute("aria-disabled") !== "true") {
        return true;
      }
    }
    return false;
  }

  function detectReactBusy() {
    const ctx = inspectReactContext(findEditor());
    if (ctx.sessionObj && typeof ctx.sessionObj.isStreaming === "boolean" && ctx.sessionObj.isStreaming) {
      return true;
    }
    if (ctx.composerProps && typeof ctx.composerProps.agentBusy === "boolean" && ctx.composerProps.agentBusy) {
      return true;
    }
    return false;
  }

  function detectTurnCompletionActions(beforeModelCount, beforeActionCount) {
    const mrs = [...document.querySelectorAll("model-response")];
    if (mrs.length > beforeModelCount) {
      const latestMr = mrs[mrs.length - 1];
      for (const btn of latestMr.querySelectorAll(_ACTION_BTN_SEL)) {
        if (isTurnActionButton(btn)) return true;
      }
    }
    return countVisibleActionButtons() > beforeActionCount;
  }

  function selectLiveTurnText({ relayText, domText, prevText, prevSource }) {
    const r = String(relayText || "");
    const d = String(domText || "");
    const p = String(prevText || "");
    if (r && (!d || r.length >= Math.floor(d.length * 0.94) || (r.length + 80) >= d.length)) {
      return { text: r, source: "relay" };
    }
    if (d) {
      if (p && prevSource === "relay" && d.length < p.length) {
        return { text: p, source: prevSource };
      }
      return { text: d, source: "dom" };
    }
    if (p) {
      return { text: p, source: prevSource || "" };
    }
    return { text: "", source: "" };
  }

  function evaluateTurnCompletionState({
    text,
    stopVisible,
    reactBusy,
    hasTurnActions,
    sawNewTurn,
    now,
    lastTextChangeAt,
    lastThinkChangeAt,
    lastBusyAt,
  }) {
    if (!text) return false;
    const textStableMs = lastTextChangeAt > 0 ? Math.max(0, now - lastTextChangeAt) : 0;
    const thinkStableMs = lastThinkChangeAt > 0 ? Math.max(0, now - lastThinkChangeAt) : textStableMs;
    const quietSinceBusyMs = lastBusyAt > 0 ? Math.max(0, now - lastBusyAt) : textStableMs;

    if (!stopVisible && !reactBusy && textStableMs >= 1400 && (hasTurnActions || quietSinceBusyMs >= 1400)) {
      return true;
    }
    // Safeguard when React sessionObj.isStreaming stays true even after Copy/Feedback toolbar renders
    if (!stopVisible && hasTurnActions && textStableMs >= 2200) {
      return true;
    }
    // Ironclad idle fallback for slower feedback toolbars / custom UI variants
    if (!stopVisible && sawNewTurn && textStableMs >= 6000 && thinkStableMs >= 4000) {
      return true;
    }
    return false;
  }

  function collectThinkingText(job, candidateText, relayThinking, busy) {
    const liveAnnouncerNow = [...document.querySelectorAll(".cdk-live-announcer-element")]
      .map(el => (el.innerText || el.textContent || "").trim())
      .join("\n");
    if (liveAnnouncerNow !== job.beforeAnnouncerSnapshot) {
      job.announcerChanged = true;
    }
    const thoughtEls = document.querySelectorAll(
      "model-thoughts, thinking-overlay, .thoughts-content, [data-test-id*='thought' i], .thoughts-status-message, [jsname='bC5vW'], thought-view, .thought-container, .cdk-live-announcer-element"
    );
    const ignoreStatus = /^(Show thinking|Hide thinking|Stop response|Gemini said|Gemini is typing|New chat|Ask Gemini|Response ready|Copied to clipboard)$/i;
    let allModelResponses = null;
    for (const sEl of thoughtEls) {
      if (sEl.closest("message-content, code-block, pre, user-query, input-container")) continue;
      const mr = sEl.closest("model-response");
      if (mr) {
        if (!allModelResponses) allModelResponses = [...document.querySelectorAll("model-response")];
        const mrIdx = allModelResponses.indexOf(mr);
        if (mrIdx >= 0 && mrIdx < job.beforeModelCount) continue;
      }
      if (!job.announcerChanged && sEl.classList.contains("cdk-live-announcer-element")) continue;
      const st = (sEl.innerText || sEl.textContent || "").replace(/^(Show|Hide)\s+thinking\s*/i, "").trim();
      if (!st || st.length >= 4000 || ignoreStatus.test(st) || /^Code snippet\b/i.test(st)) continue;
      if (candidateText && ((st.length >= 4 && candidateText.includes(st)) || (candidateText.length >= 20 && st.includes(candidateText.slice(0, 60))))) continue;
      if (busy || sEl.matches("model-thoughts, thinking-overlay, .thoughts-content, [data-test-id*='thought' i], .thoughts-status-message")) {
        const lastIdx = job.thinkingLines.length - 1;
        if (lastIdx >= 0 && st.startsWith(job.thinkingLines[lastIdx])) {
          job.thinkingLines[lastIdx] = st;
        } else if (lastIdx >= 0 && job.thinkingLines[lastIdx].startsWith(st)) {
          continue;
        } else if (!job.thinkingLines.includes(st)) {
          job.thinkingLines.push(st);
        }
      }
    }
    const domThinking = job.thinkingLines.join("\n");
    const rThink = String(relayThinking || "");
    if (rThink && rThink.length >= domThinking.length) return rThink;
    return domThinking || rThink || job.lastThinking || "";
  }

  function findEnabledSendButton(editorEl) {
    const ed = editorEl || findEditor();
    const scope = (ed && typeof ed.closest === "function"
      ? ed.closest("form, .input-area, .text-input-field, [class*='input-area' i], [class*='composer' i]")
      : null) || ed?.parentElement?.parentElement || null;
    const roots = scope ? [scope, document] : [document];
    const isSendCandidate = (btn) => {
      if (!isVisibleElement(btn)) return false;
      // Gemini places .send-button on a component wrapping the native control.
      // Clicking the host bypasses the listener on its child.
      if (btn.tagName !== "BUTTON" && btn.querySelector?.("button")) return false;
      if (btn.disabled || btn.getAttribute("aria-disabled") === "true" || btn.classList.contains("disabled")) return false;
      const meta = (
        (btn.getAttribute("data-test-id") || "") + " " +
        (btn.getAttribute("data-testid") || "") + " " +
        (btn.getAttribute("aria-label") || "") + " " +
        (btn.getAttribute("title") || "") + " " +
        (btn.className || "") + " " +
        (btn.innerText || "")
      ).toLowerCase();
      const isExplicitSend =
        btn.classList.contains("send-button") ||
        (btn.getAttribute("data-test-id") || "").toLowerCase().includes("send-button") ||
        (btn.getAttribute("data-testid") || "").toLowerCase().includes("send-button") ||
        meta.includes("send message") ||
        meta.includes("send prompt") ||
        meta.trim() === "send";
      if (isExplicitSend) {
        if (/\b(?:stop|pause|feedback|voice|dictate|mic|microphone)\b/i.test(meta)) return false;
        return true;
      }
      if (/\b(?:add\s+image|upload\s+image|attach\s+file|insert\s+image|select\s+file|browse\s+files|feedback|stop|mic|microphone|voice|dictate|attach|upload|tool|canvas|gem|model|menu|more|expand|collapse|full\s*screen)\b/i.test(meta)) {
        return false;
      }
      return meta.includes("send message") || meta.trim() === "send" || btn.classList.contains("send-button") || /\bsend\b/.test(meta);
    };
    for (const root of roots) {
      const candidates = [...root.querySelectorAll("button, [role='button'], .send-button, div.send-button, [data-test-id*='send' i], [data-testid*='send' i]")].filter(isSendCandidate);
      if (candidates.length > 0) {
        return candidates.find(b => b.classList.contains("send-button") || (b.getAttribute("aria-label") || "").toLowerCase().includes("send message")) || candidates[0];
      }
    }
    return null;
  }

  function normalizeCompareText(s) {
    return String(s || "").replace(/\r\n/g, "\n").replace(/\s+/g, " ").trim();
  }

  function selectEditorContents(editorEl) {
    try {
      const sel = typeof window !== "undefined" && window.getSelection ? window.getSelection() : null;
      if (sel && typeof document.createRange === "function") {
        const range = document.createRange();
        range.selectNodeContents(editorEl);
        sel.removeAllRanges();
        sel.addRange(range);
      }
    } catch { /* ignore */ }
  }

  function placeCaretAtEditorEnd(editorEl) {
    try {
      const sel = typeof window !== "undefined" && window.getSelection ? window.getSelection() : null;
      if (!sel || typeof document.createRange !== "function") return;
      const range = document.createRange();
      const children = editorEl.childNodes;
      if (children && children.length > 0) {
        const last = children[children.length - 1];
        if (last && last.nodeType === 1) {
          range.selectNodeContents(last);
        } else {
          range.selectNodeContents(editorEl);
        }
      } else {
        range.selectNodeContents(editorEl);
      }
      if (typeof range.collapse === "function") range.collapse(false);
      sel.removeAllRanges();
      sel.addRange(range);
    } catch { /* ignore */ }
  }

  function setPromptTextInEditor(inputEl, promptText) {
    const cleanText = String(promptText || "").replace(/\r\n/g, "\n");
    if (!inputEl) return;
    if (typeof inputEl.focus === "function") inputEl.focus();
    const tag = (inputEl.tagName || "").toUpperCase();
    if (tag === "TEXTAREA" || tag === "INPUT") {
      inputEl.value = cleanText;
      if (typeof InputEvent === "function") {
        inputEl.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText", data: cleanText }));
      } else if (typeof Event === "function") {
        inputEl.dispatchEvent(new Event("input", { bubbles: true }));
      }
      return;
    }
    autoDismissWaitPrompts();
    const isLongText = cleanText.length > 1200 || cleanText.split("\n").length > 15;
    selectEditorContents(inputEl);
    let inserted = false;
    const expNorm = normalizeCompareText(cleanText);
    const probePrefix = expNorm.slice(0, 24).trim();
    const probeTail = expNorm.slice(-24).trim();
    const hasExpectedText = () => {
      const curRaw = String(inputEl.innerText || inputEl.textContent || "").trim();
      const curNorm = normalizeCompareText(curRaw);
      if (!expNorm) return Boolean(curNorm) || cleanText.length === 0;
      if (!curNorm.includes(probePrefix)) return false;
      if (probeTail && !curNorm.includes(probeTail)) return false;
      if (expNorm.length > 64 && Math.abs(curNorm.length - expNorm.length) > Math.max(80, Math.floor(expNorm.length * 0.15))) {
        return false;
      }
      return true;
    };

    // For long text, prefer paste event first to avoid Quill's O(N^2) character-by-character layout freeze
    if (isLongText && typeof ClipboardEvent === "function" && typeof DataTransfer === "function") {
      try {
        const dt = new DataTransfer();
        dt.setData("text/plain", cleanText);
        inputEl.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: dt }));
        if (hasExpectedText()) inserted = true;
      } catch { /* ignore */ }
    }

    if (!inserted) {
      try {
        inserted = Boolean(document.execCommand && document.execCommand("insertText", false, cleanText));
      } catch { /* ignore */ }
    }

    if (!inserted || !hasExpectedText()) {
      try {
        if (typeof ClipboardEvent === "function" && typeof DataTransfer === "function") {
          selectEditorContents(inputEl);
          const dt = new DataTransfer();
          dt.setData("text/plain", cleanText);
          inputEl.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: dt }));
          if (hasExpectedText()) inserted = true;
        }
      } catch { /* ignore */ }
    }
    if (!inserted || !hasExpectedText()) {
      if (typeof document.createElement === "function" && typeof inputEl.replaceChildren === "function") {
        const lines = cleanText.split("\n");
        const frag = typeof document.createDocumentFragment === "function" ? document.createDocumentFragment() : null;
        if (lines.length > 80) {
          for (let i = 0; i < lines.length; i += 20) {
            const chunk = lines.slice(i, i + 20).join("\n");
            const p = document.createElement("p");
            p.textContent = chunk;
            if (frag) frag.appendChild(p); else inputEl.appendChild(p);
          }
          if (frag) inputEl.replaceChildren(frag);
        } else {
          const pNodes = lines.map((line) => {
            const p = document.createElement("p");
            if (line) p.appendChild(document.createTextNode(line));
            else p.appendChild(document.createElement("br"));
            return p;
          });
          inputEl.replaceChildren(...pNodes);
        }
      } else {
        inputEl.textContent = cleanText;
      }
      if (typeof InputEvent === "function") {
        try {
          inputEl.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, composed: true, inputType: "insertText", data: cleanText }));
        } catch { /* ignore */ }
        inputEl.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText", data: cleanText }));
      } else if (typeof Event === "function") {
        inputEl.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
    autoDismissWaitPrompts();
  }

  function autoDismissWaitPrompts() {
    try {
      const candidates = document.querySelectorAll(
        "button, [role='button'], .mat-mdc-button-base, [role='dialog'] button, mat-dialog-container button, .modal button"
      );
      for (const b of candidates) {
        const txt = (b.innerText || b.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
        if (txt === "wait" || txt === "keep waiting" || txt === "wait for page" || txt === "continue waiting") {
          b.click();
          break;
        }
      }
    } catch { /* ignore */ }
  }

  function readEditorPromptText(editorEl) {
    if (!editorEl || editorEl.isConnected === false) return "";
    const tag = (editorEl.tagName || "").toUpperCase();
    if (tag === "TEXTAREA" || tag === "INPUT") {
      return String(editorEl.value || "").trim();
    }
    return String(editorEl.innerText || editorEl.textContent || "").trim();
  }

  function isPromptStillInEditor(editorEl, promptText) {
    const clean = normalizeCompareText(promptText);
    if (!clean) return false;
    const cur = normalizeCompareText(readEditorPromptText(editorEl));
    if (!cur) return false;
    const head = clean.slice(0, 24).trim();
    const tail = clean.slice(-24).trim();
    if (head && cur.includes(head)) return true;
    if (tail && cur.length >= tail.length && cur.includes(tail)) return true;
    return false;
  }

  function triggerDomSubmit(editorEl, allowFormSubmitFallback = false) {
    const liveEd = findEditor() || editorEl;
    if (!liveEd || liveEd.isConnected === false) return "missing_editor";
    let sendBtn = findEnabledSendButton(liveEd);
    if (!sendBtn) {
      if (typeof liveEd.focus === "function") {
        try { liveEd.focus(); } catch { /* ignore */ }
      }
      if (typeof Event === "function" && typeof liveEd.dispatchEvent === "function") {
        try { liveEd.dispatchEvent(new Event("input", { bubbles: true, composed: true })); } catch { /* ignore */ }
        try { liveEd.dispatchEvent(new Event("change", { bubbles: true, composed: true })); } catch { /* ignore */ }
      }
      sendBtn = findEnabledSendButton(liveEd);
    }
    if (sendBtn) {
      if (typeof sendBtn.scrollIntoView === "function") {
        try { sendBtn.scrollIntoView({ block: "nearest" }); } catch { /* ignore */ }
      }
      if (typeof Event === "function" && typeof sendBtn.dispatchEvent === "function") {
        for (const evType of ["pointerdown", "mousedown", "pointerup", "mouseup"]) {
          try {
            const Ctor = evType.startsWith("pointer") && typeof PointerEvent === "function"
              ? PointerEvent
              : (typeof MouseEvent === "function" ? MouseEvent : Event);
            sendBtn.dispatchEvent(new Ctor(evType, { bubbles: true, cancelable: true, composed: true }));
          } catch { /* ignore */ }
        }
      }
      try { sendBtn.click(); } catch { /* ignore */ }
      return "button";
    }
    if (typeof liveEd.focus === "function") {
      try { liveEd.focus(); } catch { /* ignore */ }
    }
    placeCaretAtEditorEnd(liveEd);
    if (allowFormSubmitFallback && typeof liveEd.closest === "function") {
      const form = liveEd.closest("form");
      if (form && typeof form.requestSubmit === "function") {
        try {
          form.requestSubmit();
          return "requestSubmit";
        } catch { /* fall through to Enter */ }
      }
    }
    if (typeof KeyboardEvent === "function" && typeof liveEd.dispatchEvent === "function") {
      for (const kEv of ["keydown", "keyup"]) {
        liveEd.dispatchEvent(new KeyboardEvent(kEv, {
          key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true, composed: true,
        }));
      }
    }
    return "enter";
  }

  function resolveComposerAttachmentRoot(editorEl) {
    if (editorEl && typeof editorEl.closest === "function") {
      return (
        editorEl.closest("form, [role='region'], [class*='input-area'], [class*='composer']") ||
        editorEl.parentElement ||
        document
      );
    }
    return document;
  }

  function countComposerMediaPreviews(rootEl) {
    const scope = rootEl && typeof rootEl.querySelectorAll === "function" ? rootEl : document;
    return scope.querySelectorAll(
      'img[src^="blob:"], img[src^="data:image/"], [data-test-id*="image-preview"], ' +
      '[data-testid*="file-thumbnail"], [data-testid*="attachment"], [class*="thumbnail"], ' +
      'uploader-file-preview, gem-media-attachment, [class*="attachment-preview"], [class*="file-preview"], ' +
      'button[aria-label*="Remove image" i], button[aria-label*="Remove" i], button[aria-label*="Delete image" i]'
    ).length;
  }

  function hasActiveUploadSpinner(rootEl) {
    const scope = rootEl && typeof rootEl.querySelectorAll === "function" ? rootEl : document;
    const spinners = scope.querySelectorAll(
      'mat-progress-spinner, mat-spinner, mat-progress-bar, ' +
      '[role="progressbar"], [aria-busy="true"], [data-testid*="uploading" i], [data-test-id*="uploading" i], ' +
      '[data-testid*="loading" i], [data-test-id*="loading" i], ' +
      '[class*="upload-progress" i], [class*="uploading" i], [class*="loading" i], [class*="spinner" i], ' +
      'svg[class*="animate-spin" i]'
    );
    for (const sp of spinners) {
      if (isVisibleElement(sp)) return true;
    }
    return false;
  }

  async function attachImagesToPage(images, inputEl) {
    if (!Array.isArray(images) || images.length === 0) {
      return { ok: true, attached: 0, expected: 0, verified: true, method: "none", error: "" };
    }
    if (typeof DataTransfer === "undefined") {
      return { ok: false, attached: 0, expected: images.length, verified: false, error: "Image transfer unavailable" };
    }
    if (images.length > 1) {
      let attached = 0;
      for (const image of images) {
        const result = await attachImagesToPage([image], inputEl);
        if (!result.ok) return { ...result, attached, expected: images.length };
        attached++;
      }
      return { ok: true, attached, expected: images.length, verified: true, method: "sequential" };
    }
    const sleepMs = ms => new Promise(r => setTimeout(r, ms));
    const dt = new DataTransfer();
    const names = [];
    for (let idx = 0; idx < images.length; idx++) {
      const item = images[idx];
      if (!item || !item.data) continue;
      try {
        const bin = atob(String(item.data));
        const bytes = new Uint8Array(bin.length);
        for (let j = 0; j < bin.length; j++) bytes[j] = bin.charCodeAt(j);
        const mime = String(item.mime_type || "image/png");
        const name = String(item.filename || ("screenshot_" + (idx + 1) + ".png"));
        dt.items.add(new File([bytes], name, { type: mime }));
        names.push(name);
      } catch (e) {
        log("Image decode skip #" + idx + ": " + e.message);
      }
    }
    if (!dt.files || dt.files.length === 0) {
      return { ok: false, attached: 0, expected: images.length, verified: false, method: "decode", error: "No valid image bytes to attach" };
    }

    const expected = dt.files.length;
    const targetEd = findEditor() || inputEl;
    const currentRoot = () => resolveComposerAttachmentRoot(findEditor() || targetEd);
    const root = currentRoot();
    const beforeCount = countComposerMediaPreviews(root);
    const beforeText = String(root?.textContent || "");
    const hasAttachedSignal = () => {
      const liveRoot = currentRoot();
      if (countComposerMediaPreviews(liveRoot) > beforeCount) return true;
      const txt = String(liveRoot?.textContent || "");
      return names.some(n => n && txt.includes(n) && !beforeText.includes(n));
    };
    const waitForAttachment = async (polls, stepMs) => {
      for (let i = 0; i < polls; i++) {
        await sleepMs(stepMs);
        if (hasAttachedSignal()) return true;
      }
      return false;
    };
    const waitUploadSettled = async () => {
      const deadline = Date.now() + 15000;
      await sleepMs(400);
      while (Date.now() < deadline && hasActiveUploadSpinner(currentRoot())) {
        await sleepMs(200);
      }
      await sleepMs(300);
    };

    const tried = [];
    let attachedMethod = "";

    // Method 1: ClipboardEvent paste
    if (targetEd && typeof ClipboardEvent === "function") {
      tried.push("paste");
      try {
        targetEd.focus();
        targetEd.dispatchEvent(new ClipboardEvent("paste", {
          bubbles: true,
          cancelable: true,
          clipboardData: dt,
        }));
      } catch (e) {
        log("ClipboardEvent paste image error: " + e.message);
      }
      if (await waitForAttachment(10, 180)) {
        attachedMethod = "paste";
      }
    }

    // Method 2: File input (with native HTMLInputElement setter)
    if (!attachedMethod) {
      const fileInput =
        (root && root.querySelector && root.querySelector('input[type="file"][accept*="image" i]')) ||
        document.querySelector('input[type="file"][accept*="image" i]') ||
        document.querySelector('input[type="file"]');
      if (fileInput) {
        tried.push("fileInput");
        try {
          const desc = typeof HTMLInputElement !== "undefined"
            ? Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "files")
            : null;
          if (desc && typeof desc.set === "function") {
            desc.set.call(fileInput, dt.files);
          } else {
            fileInput.files = dt.files;
          }
          fileInput.dispatchEvent(new Event("input", { bubbles: true }));
          fileInput.dispatchEvent(new Event("change", { bubbles: true }));
        } catch (e) {
          log("File input image attach error: " + e.message);
        }
        if (await waitForAttachment(12, 200)) {
          attachedMethod = "fileInput";
        }
      }
    }

    // Method 3: Synthetic drag-and-drop fallback
    if (!attachedMethod && typeof DragEvent === "function" && (targetEd || root)) {
      tried.push("drop");
      const dropTarget = findEditor() || targetEd || root;
      try {
        for (const evType of ["dragenter", "dragover", "drop"]) {
          dropTarget.dispatchEvent(new DragEvent(evType, {
            bubbles: true,
            cancelable: true,
            dataTransfer: dt,
          }));
        }
      } catch (e) {
        log("Drop image attach error: " + e.message);
      }
      if (await waitForAttachment(10, 200)) {
        attachedMethod = "drop";
      }
    }

    if (attachedMethod) {
      await waitUploadSettled();
      if (hasActiveUploadSpinner(currentRoot()) || !hasAttachedSignal()) {
        return { ok: false, attached: 0, expected, verified: false, error: "Upload did not settle" };
      }
      log("Attached " + expected + " image(s) via " + attachedMethod + " (verified)");
      return { ok: true, attached: expected, expected, verified: true, method: attachedMethod, error: "" };
    }
    const err = `Could not verify image upload in Gemini composer (tried: ${tried.join(", ") || "none"})`;
    log(err);
    return { ok: false, attached: 0, expected, verified: false, method: tried.join(","), error: err };
  }

  async function submitPromptToPage(prompt, inputEl, initialCtx, beforeDomCount) {
    const sleepMs = ms => new Promise(r => setTimeout(r, ms));
    let submitted = false;
    if (initialCtx.composerProps) {
      try {
        log("Submitting via React 19 composerProps (onChange -> 400ms prewarm -> onSend)");
        initialCtx.composerProps.onChange(prompt);
        if (initialCtx.sessionObj?.syncDraft) {
          try { initialCtx.sessionObj.syncDraft({ hasText: true }, true); } catch { /* ignore */ }
        }
        await sleepMs(400);
        const latestCtx = inspectReactContext(findEditor() || inputEl);
        const sendFn = latestCtx.composerProps?.onSend || initialCtx.composerProps.onSend;
        sendFn(prompt);
        await sleepMs(350);
        submitted = true;
      } catch (e) {
        log("React composerProps submit error: " + e.message);
      }
    }

    if (!submitted && initialCtx.sessionObj?.send) {
      try {
        log("Submitting via React session.send({ prompt })");
        if (initialCtx.sessionObj.syncDraft) {
          try { initialCtx.sessionObj.syncDraft({ hasText: true }, true); } catch { /* ignore */ }
          await sleepMs(400);
        }
        initialCtx.sessionObj.send({ prompt });
        await sleepMs(350);
        submitted = true;
      } catch (e) {
        log("session.send error: " + e.message);
      }
    }

    if (!submitted) {
      log("Submitting via Angular/Quill DOM (" + prompt.length + " chars)");
      setPromptTextInEditor(inputEl, prompt);
      await sleepMs(160);

      const root = resolveComposerAttachmentRoot(findEditor() || inputEl);
      const beforeMediaCount = countComposerMediaPreviews(root);
      const hasMedia = beforeMediaCount > 0;
      const maxBtnWait = hasMedia ? 40 : 20;

      let sendBtn = null;
      for (let i = 0; i < maxBtnWait; i++) {
        sendBtn = findEnabledSendButton(inputEl);
        if (sendBtn) break;
        if (i === 8 || (hasMedia && i === 24)) {
          setPromptTextInEditor(findEditor() || inputEl, prompt);
        } else if ((i === 4 || i === 12 || i === 20) && typeof Event === "function") {
          const liveEd = findEditor() || inputEl;
          try { liveEd.dispatchEvent(new Event("input", { bubbles: true, composed: true })); } catch { /* ignore */ }
          try { liveEd.dispatchEvent(new Event("change", { bubbles: true, composed: true })); } catch { /* ignore */ }
        }
        await sleepMs(140);
      }

      const beforeBanner = readErrorBanner();
      const hasSubmitEffect = () => {
        if (isStopButtonVisible() || detectReactBusy()) return true;
        if (readDomAssistantState(beforeDomCount).count > beforeDomCount) return true;
        if (!isPromptStillInEditor(findEditor() || inputEl, prompt)) return true;
        if (hasMedia && countComposerMediaPreviews(resolveComposerAttachmentRoot(findEditor() || inputEl)) < beforeMediaCount) return true;
        if (readErrorBanner() !== beforeBanner) return true;
        return false;
      };

      if (hasMedia && prompt && !isPromptStillInEditor(findEditor() || inputEl, prompt)) {
        // The page can replace the editor a second time as its preview renders.
        const liveEd = findEditor();
        if (!liveEd || readEditorPromptText(liveEd)) return false;
        setPromptTextInEditor(liveEd, prompt);
        if (!isPromptStillInEditor(liveEd, prompt)) return false;
      }
      triggerDomSubmit(inputEl, false);
      for (let i = 0; i < 18; i++) {
        await sleepMs(140);
        if (hasSubmitEffect()) {
          submitted = true;
          break;
        }
        if (i === 3 || i === 8 || i === 14) {
          triggerDomSubmit(inputEl, i >= 8);
        }
      }
    }
    return submitted;
  }

  function readErrorBanner() {
    const msgs = [];
    for (const el of document.querySelectorAll(
      "mat-dialog-container, [role='dialog'], [role='alertdialog'], [role='alert'], " +
      ".snackbar, simple-snack-bar, [data-testid*='limit' i], [data-test-id*='limit' i], [class*='error' i]"
    )) {
      if (!isVisibleElement(el)) continue;
      const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      if (t && t.length <= 1500) msgs.push(t);
    }
    return msgs.join(" | ");
  }

  function detectPromptTooLongMessage(text, isBanner = false) {
    if (globalThis.__wsaDomUtils && typeof globalThis.__wsaDomUtils.detectPromptTooLongMessage === "function") {
      return globalThis.__wsaDomUtils.detectPromptTooLongMessage(text, isBanner);
    }
    const raw = String(text || "").trim();
    if (!raw) return "";
    const re = /(?:the\s+message\s+you\s+submitted\s+was\s+too\s+long|message\s+(?:you\s+submitted\s+)?(?:is|was)\s+too\s+long|please\s+edit\s+it\s+and\s+resubmit|prompt\s+(?:is\s+)?too\s+long|input\s+(?:is\s+)?too\s+long|exceeds?\s+(?:the\s+)?max(?:imum)?\s+(?:message\s+)?length|character\s+limit\s+exceeded|too\s+many\s+characters|maximum\s+context\s+(?:length|limit|window)\s+exceeded|context\s+(?:window|limit)\s+exceeded)/i;
    if (isBanner) {
      const parts = raw.split(/\s*\|\s*|\r?\n+|(?<=[.!?])\s+/).map(p => p.replace(/\s+/g, " ").trim()).filter(Boolean);
      for (const seg of parts) {
        if (seg.length <= 460 && re.test(seg)) return seg;
      }
      const sb = raw.replace(/\s+/g, " ").trim();
      if (sb.length <= 1500 && re.test(sb)) return sb.slice(0, 320);
      return "";
    }
    const s = raw.replace(/\s+/g, " ").trim();
    return (s && s.length <= 460 && re.test(s)) ? s : "";
  }

  function detectLimitMessage(text, isBanner = false) {
    if (detectPromptTooLongMessage(text, isBanner)) return "";
    if (globalThis.__wsaDomUtils && typeof globalThis.__wsaDomUtils.detectLimitMessage === "function") {
      return globalThis.__wsaDomUtils.detectLimitMessage(text, isBanner);
    }
    const raw = String(text || "").trim();
    if (!raw) return "";
    const softPctRe = /\b(?:over\s+|nearly\s+|almost\s+|approaching\s+|used\s+)?(?:[1-9]?\d)\s*%\s+of\s+(?:your\s+)?(?:\S+\s+){0,3}(?:limit|limi|quota|usage)\b/i;
    const hardBlockRe = /\b(?:reached|exceeded|hit|exhausted|will\s+reset|resets?\s+(?:at|in|after)|0\s+messages|out\s+of\s+(?:free\s+)?messages|upgrade\s+to\s+pro\s+to\s+(?:continue|chat|send)|cannot\s+send|try\s+again\s+(?:later|at|in)|until\s+\d)\b/i;
    const isSoftOnly = (s) => softPctRe.test(s) && !hardBlockRe.test(s);
    const re = /(?:you(?:'|’)?ve|you\s+have)\s+(?:reached|hit|exceeded)(?:\s+\S+){0,6}\s+(?:cap|limit)|(?:rate|usage|message|hourly|daily(?:\s+(?:chat|message|free))?|weekly|monthly|\d+\s*[- ]?\s*hour|plan|free|session|reasoning|thinking|deepthink|search|credit)\s+limit\b(?:\s+(?:has\s+been\s+)?(?:reached|exceeded|hit|will\s+reset|resets))?|\blimit\s+(?:has\s+been\s+)?(?:reached|exceeded|resets|will\s+reset)|(?:out\s+of\s+(?:free\s+)?messages|(?:0|zero|no)\s+messages\s+(?:left|remaining)|(?:out\s+of|insufficient|no)\s+(?:venice\s+)?credits|upgrade\s+to\s+pro\s+to\s+(?:continue|chat|send)|too\s+many\s+(?:concurrent\s+)?requests|resource[_\s-]*exhausted|quota\s+exceeded|(?:server|service|system|model)\s+is\s+(?:currently\s+)?busy|high\s+(?:demand|traffic)|http\s*(?:error\s*)?429\b|responses\s+will\s+use\s+(?:another|a\s+different|the\s+mini|gemini\s+auto|auto|gemini\s+flash-lite|flash-lite|flash\s+lite|gemini\s+flash|flash)\b|\bswitched\s+to\s+(?:gemini\s+)?(?:auto|flash-lite|flash\s+lite|flash)\b|\busing\s+(?:gemini\s+)?(?:auto|flash-lite|flash\s+lite|flash)\s+(?:until|because|due\s+to|after)\b|\b(?:you(?:'|’)?ve|you\s+have)\s+reached\s+(?:your\s+|the\s+)?(?:gemini\s+)?(?:pro|advanced)?\s*limit\b|\b(?:gemini\s+)?(?:pro|advanced)\s+(?:limit|quota)\s+(?:reached|exceeded|hit)\b|\breached\s+(?:your|the)?\s*(?:daily|hourly|usage|message)?\s*limit\s+for\s+gemini\b)/i;
    if (isBanner) {
      const parts = raw.split(/\s*\|\s*|\r?\n+|(?<=[.!?])\s+/).map(p => p.replace(/\s+/g, " ").trim()).filter(Boolean);
      for (const seg of parts) {
        if (seg.length <= 460 && !isSoftOnly(seg) && re.test(seg)) return seg;
      }
      const sb = raw.replace(/\s+/g, " ").trim();
      if (sb.length <= 1500 && !isSoftOnly(sb) && re.test(sb)) return sb.slice(0, 320);
      return "";
    }
    const s = raw.replace(/\s+/g, " ").trim();
    return (s && s.length <= 460 && !isSoftOnly(s) && re.test(s)) ? s : "";
  }

  globalThis.__wsaRuntime = {
    version: 2,
    rev: 7,
    log,
    computeTailDomRange,
    isVisibleElement,
    findEditor,
    findEnabledSendButton,
    inspectReactContext,
    readModePickerBadge,
    readRelayCandidates,
    normalizeRelayMarkdown,
    pickBestChangedRelayCandidate,
    domNodeToMarkdown,
    readDomAssistantState,
    isTurnActionButton,
    countVisibleActionButtons,
    isStopButtonVisible,
    detectReactBusy,
    detectTurnCompletionActions,
    selectLiveTurnText,
    evaluateTurnCompletionState,
    collectThinkingText,
    attachImagesToPage,
    setPromptTextInEditor,
    submitPromptToPage,
    isPromptStillInEditor,
    triggerDomSubmit,
    resolveComposerAttachmentRoot,
    countComposerMediaPreviews,
    hasActiveUploadSpinner,
    readErrorBanner,
    detectPromptTooLongMessage,
    detectLimitMessage,
  };
  return true;
}

async function _domStartTurnFn(reqId, prompt, images = []) {
  const rt = globalThis.__wsaRuntime;
  if (!rt || rt.version !== 2) {
    return { ok: false, error: "Page relay runtime missing" };
  }
  const sleepMs = ms => new Promise(r => setTimeout(r, ms));
  let inputEl = rt.findEditor();
  for (let i = 0; !inputEl && i < 80; i++) {
    if (i >= 6 && typeof rt.readErrorBanner === "function" && typeof rt.detectLimitMessage === "function") {
      const earlyLimit = rt.detectLimitMessage(rt.readErrorBanner(), true);
      if (earlyLimit) {
        return { ok: false, error: `[LIMIT_REACHED] Gemini: ${earlyLimit}` };
      }
    }
    await sleepMs(250);
    inputEl = rt.findEditor();
  }
  if (!inputEl) {
    const missingBanner = typeof rt.readErrorBanner === "function" ? rt.readErrorBanner() : "";
    const missingLimit = typeof rt.detectLimitMessage === "function" ? rt.detectLimitMessage(missingBanner, true) : "";
    if (missingLimit) {
      return { ok: false, error: `[LIMIT_REACHED] Gemini: ${missingLimit}` };
    }
    return {
      ok: false,
      error: "Gemini prompt editor not found after 20s. Make sure gemini.google.com/app is open and signed in.",
    };
  }

  const selectedBadge = rt.readModePickerBadge();

  const monoNow = () => (typeof performance !== "undefined" && typeof performance.now === "function") ? performance.now() : Date.now();
  const tTurnStart = monoNow();
  let imagesAttached = null;
  let uploadMs = null;

  if (Array.isArray(images) && images.length > 0 && rt.attachImagesToPage) {
    const tAttachStart = monoNow();
    rt.log("Waiting for Gemini attachment preview");
    let attachRes;
    try {
      attachRes = await rt.attachImagesToPage(images, inputEl);
    } catch (error) {
      return { ok: false, error: "[IMAGE_ATTACH_FAILED] Gemini: " + (error?.message || String(error)) };
    }
    if (!attachRes?.verified) {
      const errBanner = typeof rt.readErrorBanner === "function" ? rt.readErrorBanner() : "";
      const limitHit = typeof rt.detectLimitMessage === "function" ? rt.detectLimitMessage(errBanner, true) : "";
      if (limitHit) {
        return { ok: false, error: `[LIMIT_REACHED] Gemini: ${limitHit}` };
      }
      return {
        ok: false,
        error: `[IMAGE_ATTACH_FAILED] Gemini: ${attachRes?.error || "Image upload could not be verified before sending."}`,
      };
    }
    imagesAttached = Boolean(attachRes && attachRes.verified === true);
    if (imagesAttached) {
      uploadMs = Math.max(0, Math.round(monoNow() - tAttachStart));
    }
    inputEl = rt.findEditor() || inputEl;
  }

  const initialCtx = rt.inspectReactContext(inputEl);
  const beforeRelayObj = rt.readRelayCandidates(initialCtx.relayEnv);
  const beforeDomCount = rt.readDomAssistantState(0).count;
  const beforeModelCount = document.querySelectorAll("model-response").length;
  const beforeActionCount = rt.countVisibleActionButtons();
  const beforeAnnouncerSnapshot = [...document.querySelectorAll(".cdk-live-announcer-element")]
    .map(el => (el.innerText || el.textContent || "").trim())
    .join("\n");

  rt.log(
    "Starting turn id=" + reqId + " | model=" + (selectedBadge || "unknown") +
    " | relayBefore=" + Object.keys(beforeRelayObj).length +
    " | domBefore=" + beforeDomCount
  );

  const now = Date.now();
  globalThis.__wsaActiveJob = {
    reqId,
    startedAt: now,
    selectedBadge,
    prompt: String(prompt || ""),
    submitRetries: 0,
    lastRetryAt: now,
    beforeRelayObj,
    beforeDomCount,
    beforeModelCount,
    beforeActionCount,
    beforeAnnouncerSnapshot,
    lastText: "",
    lastThinking: "",
    sourceKind: "",
    thinkingLines: [],
    lastTextChangeAt: now,
    lastThinkChangeAt: now,
    lastBusyAt: now,
    sawNewTurn: false,
    sawEditorCleared: false,
    announcerChanged: false,
  };

  const submitted = await rt.submitPromptToPage(prompt, inputEl, initialCtx, beforeDomCount);
  if (imagesAttached && submitted === false) {
    return { ok: false, error: "Gemini did not acknowledge the screenshot and prompt submission." };
  }
  const afterEd = rt.findEditor() || inputEl;
  if (!afterEd || (typeof rt.isPromptStillInEditor === "function" && !rt.isPromptStillInEditor(afterEd, prompt))) {
    globalThis.__wsaActiveJob.sawEditorCleared = true;
  }
  const afterSubmit = Date.now();
  globalThis.__wsaActiveJob.lastBusyAt = afterSubmit;
  globalThis.__wsaActiveJob.lastTextChangeAt = afterSubmit;
  globalThis.__wsaActiveJob.lastRetryAt = afterSubmit;

  return {
    ok: true,
    model: selectedBadge,
    url: window.location.href,
    images_attached: imagesAttached,
    upload_ms: uploadMs,
    submit_ms: Math.max(0, Math.round(monoNow() - tTurnStart)),
  };
}

function _domPollTurnFn(reqId) {
  const rt = globalThis.__wsaRuntime;
  if (!rt || rt.version !== 2) {
    return { needRuntime: true };
  }
  const job = globalThis.__wsaActiveJob;
  if (!job || job.reqId !== reqId) {
    return { missing: true, url: window.location.href };
  }

  const now = Date.now();
  const liveEd = typeof rt.findEditor === "function" ? rt.findEditor() : null;
  const stopVisible = rt.isStopButtonVisible();
  const reactBusy = rt.detectReactBusy();
  const busy = stopVisible || reactBusy;
  if (busy) {
    job.lastBusyAt = now;
  }
  if (stopVisible || !liveEd || (typeof rt.isPromptStillInEditor === "function" && !rt.isPromptStillInEditor(liveEd, job.prompt))) {
    job.sawEditorCleared = true;
  }
  const hasTurnActions = rt.detectTurnCompletionActions(job.beforeModelCount, job.beforeActionCount);

  const liveCtx = rt.inspectReactContext(liveEd);
  const currentRelay = rt.readRelayCandidates(liveCtx.relayEnv);
  const relayPicked = rt.pickBestChangedRelayCandidate(currentRelay, job.beforeRelayObj);
  const currentDomState = rt.readDomAssistantState(job.beforeDomCount);

  let effectiveDomCount = currentDomState.count;
  if (
    !job.sawNewTurn &&
    !relayPicked.sawNewTurn &&
    !busy &&
    !job.sawEditorCleared &&
    hasTurnActions &&
    effectiveDomCount > job.beforeDomCount &&
    liveEd &&
    typeof rt.isPromptStillInEditor === "function" &&
    rt.isPromptStillInEditor(liveEd, job.prompt)
  ) {
    // Historical thread hydration while our prompt still sits unsent in the composer: rebase instead of returning old turn.
    job.beforeDomCount = effectiveDomCount;
    job.beforeModelCount = document.querySelectorAll("model-response").length;
    job.beforeActionCount = rt.countVisibleActionButtons();
  }

  if (relayPicked.sawNewTurn || effectiveDomCount > job.beforeDomCount) {
    job.sawNewTurn = true;
  }

  const domText = effectiveDomCount > job.beforeDomCount
    ? rt.normalizeRelayMarkdown(currentDomState.tailText || "")
    : "";

  const chosen = rt.selectLiveTurnText({
    relayText: relayPicked.text,
    domText,
    prevText: job.lastText,
    prevSource: job.sourceKind,
  });

  if (chosen.text !== job.lastText) {
    job.lastText = chosen.text;
    job.sourceKind = chosen.source;
    job.lastTextChangeAt = now;
  }

  const nextThinking = rt.collectThinkingText(job, job.lastText, relayPicked.thinking, busy);
  if (nextThinking !== job.lastThinking) {
    job.lastThinking = nextThinking;
    job.lastThinkChangeAt = now;
  }
  if (!busy) {
    const pageBanner = typeof rt.readErrorBanner === "function" ? rt.readErrorBanner() : "";
    if (typeof rt.detectPromptTooLongMessage === "function") {
      const turnTooLongErr = job.lastText ? rt.detectPromptTooLongMessage(job.lastText) : "";
      const pageTooLongErr = pageBanner ? rt.detectPromptTooLongMessage(pageBanner, true) : "";
      const matchedTooLong = turnTooLongErr || (!job.lastText ? pageTooLongErr : "");
      if (matchedTooLong && !job.lastText.includes("```")) {
        delete globalThis.__wsaActiveJob;
        return {
          done: false,
          error: "[PROMPT_TOO_LONG] Gemini: " + matchedTooLong,
          text: "",
          thinking: "",
          model: job.selectedBadge || rt.readModePickerBadge(),
          url: window.location.href,
          busy: false,
          hasTurnActions,
        };
      }
    }
  }
  if (typeof autoDismissWaitPrompts === "function") autoDismissWaitPrompts();
  const livePickerBadge = typeof rt.readModePickerBadge === "function" ? rt.readModePickerBadge() : "";
  const effectiveBadge = (livePickerBadge && /(?:auto|flash(?:-lite|\s+lite)?)/i.test(livePickerBadge))
    ? livePickerBadge
    : (job.selectedBadge || livePickerBadge);

  if (!busy && typeof rt.detectLimitMessage === "function") {
    const turnLimitErr = job.lastText ? rt.detectLimitMessage(job.lastText) : "";
    const pageBannerErr = typeof rt.readErrorBanner === "function" ? rt.detectLimitMessage(rt.readErrorBanner(), true) : "";
    const matchedLimit = turnLimitErr || (!job.lastText ? pageBannerErr : "");
    if (matchedLimit && !job.lastText.includes("```")) {
      delete globalThis.__wsaActiveJob;
      return {
        done: false,
        error: "[LIMIT_REACHED] Gemini: " + matchedLimit,
        text: "",
        thinking: "",
        model: effectiveBadge,
        url: window.location.href,
        busy: false,
        hasTurnActions,
      };
    }
  }
  const isPromptInEd = Boolean(job.prompt && liveEd && typeof rt.isPromptStillInEditor === "function" && rt.isPromptStillInEditor(liveEd, job.prompt));
  const promptUnsent = Boolean(
    !job.sawNewTurn &&
    !busy &&
    !job.lastText &&
    !job.lastThinking &&
    isPromptInEd
  );
  if (promptUnsent && typeof rt.triggerDomSubmit === "function" && (job.submitRetries || 0) < 4 && (now - (job.lastRetryAt || job.startedAt)) >= 1800) {
    const sendMissing = typeof rt.findEnabledSendButton === "function" && !rt.findEnabledSendButton(liveEd);
    if (sendMissing && isPromptInEd && (job.submitRetries || 0) < 2 && typeof rt.setPromptTextInEditor === "function") {
      rt.setPromptTextInEditor(liveEd, job.prompt);
    }
    rt.triggerDomSubmit(liveEd, (job.submitRetries || 0) >= 1);
    job.submitRetries = (job.submitRetries || 0) + 1;
    job.lastRetryAt = now;
  }

  const stuckUnsentStall = Boolean(promptUnsent && (job.submitRetries || 0) >= 3 && (now - job.startedAt) >= 14000);
  const neverStartedStall = Boolean(!job.sawNewTurn && !busy && !job.lastText && !job.lastThinking && (now - job.startedAt) >= 45000);
  if (stuckUnsentStall || neverStartedStall) {
    const fallbackBanner = typeof rt.readErrorBanner === "function" ? rt.readErrorBanner() : "";
    const tooLongMsg = typeof rt.detectPromptTooLongMessage === "function" ? rt.detectPromptTooLongMessage(fallbackBanner, true) : "";
    const limitMsg = !tooLongMsg && typeof rt.detectLimitMessage === "function" ? rt.detectLimitMessage(fallbackBanner, true) : "";
    delete globalThis.__wsaActiveJob;
    return {
      done: false,
      error: tooLongMsg
        ? "[PROMPT_TOO_LONG] Gemini: " + tooLongMsg
        : (limitMsg
          ? "[LIMIT_REACHED] Gemini: " + limitMsg
          : (stuckUnsentStall
            ? "Gemini prompt remained unsubmitted in composer (Send button/form did not trigger response)"
            : "Gemini tab did not start or produce a text response")),
      text: job.lastText,
      thinking: job.lastThinking,
      model: effectiveBadge,
      url: window.location.href,
      busy: false,
      hasTurnActions,
    };
  }

  const done = rt.evaluateTurnCompletionState({
    text: job.lastText,
    stopVisible,
    reactBusy,
    hasTurnActions,
    sawNewTurn: job.sawNewTurn,
    now,
    lastTextChangeAt: job.lastTextChangeAt,
    lastThinkChangeAt: job.lastThinkChangeAt,
    lastBusyAt: job.lastBusyAt,
  });

  if (done) {
    rt.log("Completed turn id=" + reqId + " (" + job.lastText.length + " chars)");
  }

  return {
    done,
    text: job.lastText,
    thinking: job.lastThinking,
    model: effectiveBadge,
    url: window.location.href,
    busy,
    streaming: busy,
    hasTurnActions,
  };
}

// ── Live Browser Tab Visual Inspection & Safe Interaction Bridge ─────────────

const _SENSITIVE_URL_RE = /^(?:chrome|brave|edge|about|chrome-extension|devtools|view-source):/i;
const _PROTECTED_HOSTS_RE = /(?:^|\.)(?:gemini\.google\.com|accounts\.google\.com|mail\.google\.com|claude\.ai|chatgpt\.com|chat\.openai\.com|chat\.deepseek\.com|venice\.ai)$/i;
const _SECRET_QS_RE = /^(?:token|access_token|id_token|code|auth|key|apikey|api_key|secret|password|session)$/i;

function sanitizeInspectUrl(rawUrl) {
  const s = String(rawUrl || "");
  try {
    const u = new URL(s);
    for (const k of [...u.searchParams.keys()]) {
      if (_SECRET_QS_RE.test(k)) {
        u.searchParams.set(k, "[REDACTED]");
      }
    }
    return u.toString();
  } catch {
    return s;
  }
}

function isProtectedInspectUrl(rawUrl) {
  const s = String(rawUrl || "").trim();
  if (!s) return true;
  if (_SENSITIVE_URL_RE.test(s)) return true;
  try {
    const u = new URL(s);
    return _PROTECTED_HOSTS_RE.test(u.hostname);
  } catch {
    return false;
  }
}

function _pageCollectBrowserDiagnosticsFn() {
  if (!globalThis.__wsaDiag) {
    const store = { errors: [], installedAt: Date.now() };
    const pushErr = (kind, msg) => {
      store.errors.push({ kind, msg: String(msg || "").slice(0, 300), ts: Date.now() });
      if (store.errors.length > 30) store.errors.shift();
    };
    try {
      const origErr = console.error;
      console.error = function (...args) {
        try {
          pushErr("console.error", args.map(a => (a && a.message) ? a.message : String(a)).join(" "));
        } catch { /* ignore */ }
        return origErr.apply(this, args);
      };
      window.addEventListener("error", (ev) => {
        pushErr("window.error", ev?.message || ev?.error?.message || "Uncaught error");
      });
      window.addEventListener("unhandledrejection", (ev) => {
        pushErr("unhandledrejection", ev?.reason?.message || String(ev?.reason || "Rejected promise"));
      });
      if (typeof window.fetch === "function") {
        const origFetch = window.fetch;
        window.fetch = function (...args) {
          const reqUrl = typeof args[0] === "string" ? args[0] : (args[0]?.url || "");
          return origFetch.apply(this, args).then(
            (resp) => {
              if (resp && resp.status >= 400) {
                pushErr("http_" + resp.status, reqUrl + " -> HTTP " + resp.status);
              }
              return resp;
            },
            (err) => {
              pushErr("fetch_fail", reqUrl + " -> " + (err?.message || err));
              throw err;
            }
          );
        };
      }
    } catch { /* ignore hook errors under locked descriptors */ }
    globalThis.__wsaDiag = store;
  }

  const vw = window.innerWidth || document.documentElement.clientWidth || 0;
  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
  const docSw = document.documentElement ? document.documentElement.scrollWidth : 0;
  const docSh = document.documentElement ? document.documentElement.scrollHeight : 0;
  const horizOverflowPx = Math.max(0, docSw - vw);

  const brokenImages = [];
  for (const img of document.querySelectorAll("img")) {
    const cs = getComputedStyle(img);
    if (cs.display === "none" || cs.visibility === "hidden") continue;
    if (img.complete && img.naturalWidth === 0) {
      brokenImages.push((img.getAttribute("alt") || img.getAttribute("src") || "(img)").slice(0, 100));
    }
    if (brokenImages.length >= 10) break;
  }

  const clippedElements = [];
  const offscreenControls = [];
  const cssHint = (el) => {
    if (el.id) return "#" + el.id;
    const dt = el.getAttribute("data-testid") || el.getAttribute("data-test-id");
    if (dt) return `[data-testid="${dt}"]`;
    const nm = el.getAttribute("name");
    if (nm) return `${el.tagName.toLowerCase()}[name="${nm}"]`;
    const cls = typeof el.className === "string"
      ? el.className.trim().split(/\s+/).filter(Boolean).slice(0, 2).join(".")
      : "";
    return el.tagName.toLowerCase() + (cls ? "." + cls : "");
  };

  for (const el of document.querySelectorAll("h1,h2,h3,button,a,[role='button'],.nav-link,.tab")) {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") continue;
    if ((cs.overflowX === "hidden" || cs.overflowX === "clip") && el.scrollWidth > el.clientWidth + 6) {
      const label = (el.innerText || el.textContent || "").trim().slice(0, 48);
      if (label) clippedElements.push(`${cssHint(el)} ("${label}": ${el.scrollWidth}px>${el.clientWidth}px)`);
    }
    if (rect.right > vw + 4 && offscreenControls.length < 8) {
      offscreenControls.push(`${cssHint(el)} (right=${Math.round(rect.right)}px > vw=${vw}px)`);
    }
    if (clippedElements.length >= 8 && offscreenControls.length >= 8) break;
  }

  const alerts = [];
  for (const el of document.querySelectorAll('[role="alert"], [aria-live="assertive"], .alert-danger, .error-banner, dialog[open], [role="dialog"]')) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const txt = (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 160);
    if (txt) alerts.push(txt);
    if (alerts.length >= 6) break;
  }

  const controls = [];
  for (const el of document.querySelectorAll('a[href], button, input, select, textarea, [role="button"]')) {
    if (controls.length >= 25) break;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    if (r.width <= 0 || r.height <= 0 || cs.display === "none" || cs.visibility === "hidden") continue;
    const tag = el.tagName.toLowerCase();
    const lbl = (
      el.getAttribute("aria-label") ||
      el.getAttribute("placeholder") ||
      (el.innerText || el.value || "")
    ).trim().replace(/\s+/g, " ").slice(0, 60);
    const extra = [];
    if (el.disabled) extra.push("disabled");
    if (tag === "input" && el.type) extra.push("type=" + el.type);
    controls.push({
      selector: cssHint(el),
      tag,
      label: lbl,
      state: extra.join(", "),
    });
  }

  const headings = [...document.querySelectorAll("h1, h2, h3")]
    .map(h => (h.innerText || "").trim().replace(/\s+/g, " ").slice(0, 90))
    .filter(Boolean)
    .slice(0, 10);

  const rawBody = (document.body ? (document.body.innerText || "") : "").trim();
  const visibleText = rawBody.length > 3500 ? (rawBody.slice(0, 3500) + "\n... [truncated]") : rawBody;

  return {
    title: document.title || "",
    url: window.location.href,
    readyState: document.readyState,
    viewport: { width: vw, height: vh, scrollWidth: docSw, scrollHeight: docSh, scrollY: Math.round(window.scrollY || 0) },
    horizOverflowPx,
    brokenImages,
    clippedElements,
    offscreenControls,
    alerts,
    headings,
    controls,
    consoleErrors: (globalThis.__wsaDiag?.errors || []).slice(-15),
    visibleText,
  };
}

function _pageInteractDomFn(payload) {
  const act = String(payload?.action || "").toLowerCase().trim();
  const sel = payload?.selector ? String(payload.selector) : "";
  const txt = payload?.text ? String(payload.text).trim().toLowerCase() : "";
  const val = payload?.value !== undefined && payload?.value !== null ? String(payload.value) : "";

  const findTarget = () => {
    if (sel) {
      const direct = document.querySelector(sel);
      if (direct) return direct;
    }
    if (txt) {
      const cands = [...document.querySelectorAll('button, a, [role="button"], input[type="submit"], input[type="button"], label, [role="tab"]')];
      const exact = cands.find(el => {
        const s = (el.innerText || el.getAttribute("aria-label") || el.value || "").trim().toLowerCase();
        return s === txt;
      });
      if (exact) return exact;
      const partial = cands.find(el => {
        const s = (el.innerText || el.getAttribute("aria-label") || el.value || "").trim().toLowerCase();
        return s.includes(txt);
      });
      if (partial) return partial;
    }
    return null;
  };

  if (act === "scroll") {
    const sy = payload?.scroll_y;
    const target = sel || txt ? findTarget() : null;
    if (target) {
      target.scrollIntoView({ behavior: "instant", block: "center" });
      return { ok: true, detail: "Scrolled element into view" };
    }
    if (String(sy).toLowerCase() === "bottom") {
      window.scrollTo(0, document.documentElement.scrollHeight);
      return { ok: true, detail: "Scrolled to bottom (y=" + Math.round(window.scrollY) + ")" };
    }
    if (String(sy).toLowerCase() === "top") {
      window.scrollTo(0, 0);
      return { ok: true, detail: "Scrolled to top" };
    }
    const yNum = Number.isFinite(Number(sy)) ? Number(sy) : Math.round((window.innerHeight || 600) * 0.8);
    window.scrollBy(0, yNum);
    return { ok: true, detail: "Scrolled to y=" + Math.round(window.scrollY) };
  }

  if (act === "wait") {
    return { ok: true, detail: "Waited cleanly" };
  }

  const el = findTarget() || (act === "press_enter" ? document.activeElement : null);
  if (!el) {
    return { ok: false, error: `Element not found (selector="${sel}", text="${txt}")` };
  }
  el.scrollIntoView({ behavior: "instant", block: "center" });

  if (act === "click") {
    el.focus?.();
    el.click();
    return { ok: true, detail: "Clicked " + (el.tagName || "ELEMENT").toLowerCase() };
  }

  if (act === "hover") {
    el.dispatchEvent(new MouseEvent("mouseenter", { bubbles: true }));
    el.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
    return { ok: true, detail: "Hovered " + (el.tagName || "ELEMENT").toLowerCase() };
  }

  if (act === "fill") {
    el.focus?.();
    const proto = el.tagName === "TEXTAREA"
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) {
      setter.call(el, val);
    } else if ("value" in el) {
      el.value = val;
    } else {
      el.textContent = val;
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true, detail: `Filled ${val.length} chars into ${(el.tagName || "").toLowerCase()}` };
  }

  if (act === "select") {
    el.focus?.();
    el.value = val;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true, detail: `Selected value "${val}"` };
  }

  if (act === "press_enter") {
    el.focus?.();
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }));
    el.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
    if (el.form && typeof el.form.requestSubmit === "function") {
      try { el.form.requestSubmit(); } catch { /* ignore */ }
    }
    return { ok: true, detail: "Pressed Enter" };
  }

  return { ok: false, error: `Unsupported action '${act}'` };
}

async function captureTabScreenshotSafe(tab, restoreFocus = true) {
  let prevActiveId = null;
  try {
    const activeInWin = await chrome.tabs.query({ windowId: tab.windowId, active: true });
    if (activeInWin?.[0]?.id && activeInWin[0].id !== tab.id) {
      prevActiveId = activeInWin[0].id;
    }
    if (chrome.windows?.get) {
      const win = await chrome.windows.get(tab.windowId).catch(() => null);
      if (win && win.state === "minimized") {
        await chrome.windows.update(tab.windowId, { state: "normal" }).catch(() => {});
        await sleep(180);
      }
    }
    if (prevActiveId !== null) {
      await chrome.tabs.update(tab.id, { active: true });
      await sleep(220);
    } else {
      await sleep(80);
    }
    let dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
    if (typeof dataUrl === "string" && dataUrl.length > 4_800_000) {
      dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 86 });
    }
    return { dataUrl, warning: "" };
  } catch (e) {
    return { dataUrl: "", warning: e?.message || String(e) };
  } finally {
    if (restoreFocus && prevActiveId !== null) {
      await chrome.tabs.update(prevActiveId, { active: true }).catch(() => {});
    }
  }
}

async function resolveTargetInspectTab(params = {}) {
  const allTabs = await chrome.tabs.query({});
  const explicitId = params.tab_id !== undefined && params.tab_id !== null ? Number(params.tab_id) : null;
  const reqUrl = String(params.url || "").trim();

  if (explicitId) {
    const hit = allTabs.find(t => t.id === explicitId);
    if (!hit) throw new Error(`Tab #${explicitId} not found.`);
    if (isProtectedInspectUrl(hit.url)) {
      throw new Error(`Refusing to inspect/mutate protected tab #${explicitId} (${sanitizeInspectUrl(hit.url)}).`);
    }
    if (reqUrl && hit.url !== reqUrl) {
      if (isProtectedInspectUrl(reqUrl)) throw new Error("Refusing navigation to protected URL.");
      await chrome.tabs.update(hit.id, { url: reqUrl, autoDiscardable: false });
      await waitForTabComplete(hit.id, 15000).catch(() => {});
    }
    lastInspectTabId = hit.id;
    return await chrome.tabs.get(hit.id);
  }

  if (reqUrl) {
    if (isProtectedInspectUrl(reqUrl)) {
      throw new Error(`Refusing to navigate to protected URL: ${sanitizeInspectUrl(reqUrl)}`);
    }
    let reqOrigin = "";
    try { reqOrigin = new URL(reqUrl).origin; } catch { /* ignore */ }
    let existing = allTabs.find(t => !isProtectedInspectUrl(t.url) && (t.url === reqUrl || (reqOrigin && t.url.startsWith(reqOrigin) && t.id === lastInspectTabId)));
    if (!existing && lastInspectTabId) {
      const cached = allTabs.find(t => t.id === lastInspectTabId && !isProtectedInspectUrl(t.url));
      if (cached) existing = cached;
    }
    if (existing) {
      if (existing.url !== reqUrl) {
        await chrome.tabs.update(existing.id, { url: reqUrl, active: true, autoDiscardable: false });
        await waitForTabComplete(existing.id, 15000).catch(() => {});
      }
      lastInspectTabId = existing.id;
      return await chrome.tabs.get(existing.id);
    }
    const created = await chrome.tabs.create({ url: reqUrl, active: true });
    await chrome.tabs.update(created.id, { autoDiscardable: false }).catch(() => {});
    await waitForTabComplete(created.id, 15000).catch(() => {});
    lastInspectTabId = created.id;
    return await chrome.tabs.get(created.id);
  }

  if (lastInspectTabId) {
    const cached = allTabs.find(t => t.id === lastInspectTabId && !isProtectedInspectUrl(t.url));
    if (cached) return cached;
  }

  const nonProtected = allTabs.filter(t => !isProtectedInspectUrl(t.url));
  const localDev = nonProtected.find(t => /^https?:\/\/(?:localhost|127\.0\.0\.1|\[::1\])/i.test(t.url || ""));
  const activeNonGem = nonProtected.find(t => t.active);
  const picked = localDev || activeNonGem || nonProtected[0];
  if (!picked) {
    throw new Error("No non-protected app tab is open. Pass `url` (e.g. 'http://127.0.0.1:5000') to open and inspect a preview tab.");
  }
  lastInspectTabId = picked.id;
  return picked;
}

async function handleBrowserCommand(cmd) {
  if (!cmd || !cmd.cmd_id || browserCmdInFlight) return;
  browserCmdInFlight = true;
  let result;
  try {
    const action = String(cmd.action || "list_tabs");
    const params = cmd.params || {};
    if (action === "list_tabs") {
      const allTabs = await chrome.tabs.query({});
      result = {
        ok: true,
        tabs: allTabs.map(t => ({
          id: t.id,
          windowId: t.windowId,
          active: Boolean(t.active),
          isAiRelay: Boolean(detectTabProvider(t.url || "")),
          isGeminiRelay: /:\/\/gemini\.google\.com\b/i.test(t.url || ""),
          isProtected: isProtectedInspectUrl(t.url || ""),
          isLastInspected: t.id === lastInspectTabId,
          title: String(t.title || "").slice(0, 120),
          url: sanitizeInspectUrl(t.url || ""),
          width: t.width || 0,
          height: t.height || 0,
        })),
      };
    } else if (action === "inspect" || action === "interact") {
      let tab = await resolveTargetInspectTab(params);
      if (params.reload) {
        await chrome.tabs.reload(tab.id);
        await waitForTabComplete(tab.id, 15000).catch(() => {});
        tab = await chrome.tabs.get(tab.id);
      }

      let interactRes = null;
      if (action === "interact") {
        const iArr = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          world: "MAIN",
          func: _pageInteractDomFn,
          args: [params],
        });
        if (iArr?.[0]?.error) {
          throw new Error(iArr[0].error.message || "DOM interaction failed");
        }
        interactRes = iArr?.[0]?.result || { ok: false, error: "Empty interact result" };
        if (!interactRes.ok) {
          throw new Error(interactRes.error || "Interaction target not found");
        }
      }

      const waitMs = Math.min(Math.max(Number(params.wait_ms ?? (action === "interact" ? 500 : 600)) || 0, 50), 5000);
      await sleep(waitMs);
      tab = await chrome.tabs.get(tab.id);

      const dArr = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: "MAIN",
        func: _pageCollectBrowserDiagnosticsFn,
      });
      if (dArr?.[0]?.error) {
        throw new Error(dArr[0].error.message || "Diagnostics collection failed");
      }
      const diag = dArr?.[0]?.result || {};
      let screenshotDataUrl = "";
      let screenshotWarning = "";
      if (params.capture_screenshot !== false) {
        const cap = await captureTabScreenshotSafe(tab, params.restore_focus !== false);
        screenshotDataUrl = cap.dataUrl || "";
        screenshotWarning = cap.warning || "";
      }

      result = {
        ok: true,
        tab_id: tab.id,
        interaction: interactRes,
        diagnostics: diag,
        screenshot_data_url: screenshotDataUrl,
        screenshot_warning: screenshotWarning,
      };
    } else {
      result = { ok: false, error: `Unknown browser action: ${action}` };
    }
  } catch (e) {
    result = { ok: false, error: e?.message || String(e) };
  } finally {
    browserCmdInFlight = false;
  }

  try {
    await fetch(`${LOCAL_API}/browser-command-result`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: AbortSignal.timeout(8000),
      body: JSON.stringify({ cmd_id: cmd.cmd_id, result }),
    });
  } catch { /* ignore */ }
}

function _claudeInstallPageRuntimeFn() {
  if (globalThis.__wsaClaudeRuntime && globalThis.__wsaClaudeRuntime.version === 4 && globalThis.__wsaClaudeRuntime.rev === 8) {
    return true;
  }

  function getClassStr(el) {
    if (!el) return "";
    if (typeof el.className === "string") return el.className;
    if (el.className && typeof el.className.baseVal === "string") return el.className.baseVal;
    return "";
  }

  function isVisible(el) {
    if (!el || !(el instanceof Element)) return false;
    try {
      let cur = el;
      for (let depth = 0; cur && depth < 24; depth++) {
        if (cur.hidden === true ||
            (typeof cur.hasAttribute === "function" && cur.hasAttribute("hidden")) ||
            (typeof cur.getAttribute === "function" && cur.getAttribute("aria-hidden") === "true" && cur.getAttribute("__wsa_probe__") !== "true")) {
          return false;
        }
        const style = typeof getComputedStyle === "function" ? getComputedStyle(cur) : null;
        if (style && (style.display === "none" || style.visibility === "hidden" || parseFloat(style.opacity || "1") <= 0.05)) {
          return false;
        }
        cur = cur.parentElement || null;
      }
      if (typeof el.getBoundingClientRect === "function") {
        const rect = el.getBoundingClientRect();
        if (rect && rect.width <= 1 && rect.height <= 1 && (!el.getClientRects || el.getClientRects().length === 0)) {
          if (el.offsetParent === null || rect.width <= 0 || rect.height <= 0) return false;
        }
      }
    } catch { /* ignore */ }
    return true;
  }

  function findClaudeEditor() {
    const selectors = [
      "[data-testid='chat-input']",
      "div.tiptap.ProseMirror[contenteditable='true']",
      "div.ProseMirror[contenteditable='true']",
      "[aria-label*='Write your prompt' i][contenteditable='true']",
      "[contenteditable='true'][role='textbox']",
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && isVisible(el)) return el;
    }
    return null;
  }

  function findClaudeSendButton(editorEl) {
    const scope = editorEl && typeof editorEl.closest === "function"
      ? editorEl.closest("form, fieldset, [class*='composer' i], [data-testid*='composer' i]")
      : null;
    const roots = scope ? [scope, document] : [document];
    const selectors = [
      "button[data-testid='send-button']",
      "[role='button'][data-testid='send-button']",
      "[data-testid='send-button']",
      "button[aria-label*='Send message' i]",
      "[role='button'][aria-label*='Send message' i]",
      "button[aria-label*='Send prompt' i]",
      "[role='button'][aria-label*='Send prompt' i]",
      "button[aria-label*='Send' i]",
      "[role='button'][aria-label*='Send' i]",
      "button[type='submit']",
    ];
    for (let rIdx = 0; rIdx < roots.length; rIdx++) {
      const root = roots[rIdx];
      for (const sel of selectors) {
        if (sel === "button[type='submit']" && (!scope || rIdx > 0)) continue;
        for (const b of root.querySelectorAll(sel)) {
          if (!isVisible(b) || b.disabled || b.getAttribute("aria-disabled") === "true") continue;
          const meta = `${b.getAttribute("data-testid") || ""} ${b.id || ""} ${b.getAttribute("aria-label") || ""} ${b.getAttribute("title") || ""} ${b.innerText || ""}`.toLowerCase();
          const testId = (b.getAttribute("data-testid") || "").toLowerCase();
          const isExplicit = testId === "send-button" || meta.includes("send message") || meta.includes("send prompt") || meta.trim() === "send";
          if (isExplicit) {
            if (/\b(?:feedback|voice|dictate|stop|mic|microphone)\b/i.test(meta)) continue;
            return b;
          }
          if (/\b(?:add\s+attachment|upload\s+image|attach\s+file|feedback|voice|dictate|stop|mic|microphone|attach|upload|model|menu|more|expand|collapse|full\s*screen)\b/i.test(meta)) continue;
          return b;
        }
      }
    }
    return null;
  }

  function hasClaudeStopButton() {
    for (const b of document.querySelectorAll("button[aria-label*='Stop' i], button[data-testid='stop-button']")) {
      if (isVisible(b) && !b.disabled) return true;
    }
    if (document.querySelector("article[aria-label*='Currently streaming' i], [role='article'][aria-label*='Currently streaming' i]")) return true;
    return Boolean(document.querySelector("[data-is-streaming='true']"));
  }

  function readClaudeModelBadge() {
    const selectors = [
      "[data-testid='model-selector-dropdown']",
      "[data-cds^='ModelSelector']",
      "button[data-testid*='model-selector']",
      "button[data-testid*='model']",
      "button[aria-label*='model' i]",
    ];
    let badge = "";
    for (const sel of selectors) {
      let btn = null;
      try { btn = document.querySelector(sel); } catch { btn = null; }
      if (!btn || !isVisible(btn)) continue;
      const rawText = (btn.innerText || btn.textContent || "").replace(/\s+/g, " ").trim();
      const aria = (btn.getAttribute("aria-label") || "").replace(/^Model:\s*/i, "").replace(/\s+/g, " ").trim();
      const t = rawText || aria;
      if (t && t.length <= 60) {
        badge = t;
        break;
      }
    }
    if (!badge) {
      for (const b of document.querySelectorAll("button")) {
        if (!isVisible(b)) continue;
        const cds = b.getAttribute("data-cds") || "";
        const t = (b.innerText || b.textContent || "").replace(/\s+/g, " ").trim();
        if ((cds.startsWith("ModelSelector") && t && t.length <= 60) || (t && t.length <= 48 && /\b(?:Claude|Opus|Sonnet|Haiku)\b/i.test(t))) {
          badge = t;
          break;
        }
      }
    }
    const thinkBtn = document.querySelector(
      "button[aria-label*='thinking' i][aria-pressed='true'], [data-testid*='thinking'][aria-pressed='true'], button[aria-label*='Extended thinking' i][data-state='checked']"
    );
    if (thinkBtn && badge && !/thinking|extended/i.test(badge)) {
      badge = `${badge} · Extended thinking`;
    }
    return badge;
  }

  function maxBacktickRun(str) {
    let maxRun = 0;
    const matches = String(str || "").match(/`+/g);
    if (matches) {
      for (const m of matches) {
        if (m.length > maxRun) maxRun = m.length;
      }
    }
    return maxRun;
  }

  function nodeToMarkdown(root, skipPredicate = null) {
    if (!root) return "";
    function readHeaderNonBtnText(node) {
      if (!node) return "";
      if (node.nodeType === 3) return node.textContent || "";
      if (node.nodeType !== 1) return "";
      const t = (node.tagName || "").toLowerCase();
      const r = (node.getAttribute("role") || "").toLowerCase();
      const c = getClassStr(node);
      if (t === "button" || t === "svg" || t === "style" || t === "script") return "";
      if (r === "button" || /\bds-button\b/i.test(c) || /\bsr-only\b/i.test(c)) return "";
      if (node.childNodes && node.childNodes.length) {
        return Array.from(node.childNodes).map(readHeaderNonBtnText).join(" ");
      }
      return node.textContent || "";
    }
    function cleanCodeHeaderLang(rawText) {
      const cleaned = String(rawText || "")
        .replace(/\b(?:copy(?:\s+code)?|copied|download|enable\s+word\s+wrap|disable\s+word\s+wrap|word\s+wrap|code\s*snippet)\b/gi, "")
        .replace(/\s+/g, " ")
        .trim();
      if (!cleaned || /^(?:copy|copied|download|code|snippet)$/i.test(cleaned)) return "";
      return /^[a-z0-9_+#.-]{1,24}$/i.test(cleaned) ? cleaned.toLowerCase() : "";
    }
    function inferFenceLang(lang, codeText) {
      if (lang) return lang;
      const trimmed = (codeText || "").trim();
      if (trimmed[0] === "{" || trimmed[0] === "[") {
        try {
          const parsed = JSON.parse(trimmed);
          const items = Array.isArray(parsed) ? parsed : [parsed];
          if (items.length && items.every((it) => it && typeof it === "object" && typeof it.name === "string" && it.name.trim())) {
            return "tool_call";
          }
        } catch { /* ignore */ }
      }
      return "";
    }
    function walk(node, ctx = {}) {
      if (!node) return "";
      if (node.nodeType === Node.TEXT_NODE) {
        return node.textContent || "";
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return "";
      const el = node;
      if (skipPredicate && skipPredicate(el)) return "";
      const tag = (el.tagName || "").toLowerCase();
      if (tag === "style" || tag === "script" || tag === "svg" || tag === "button" || tag === "nav" || tag === "noscript" || tag === "template") {
        return "";
      }
      const cls = getClassStr(el);
      if (cls && cls.toLowerCase().split(/\s+/).includes("sr-only")) {
        return "";
      }
      if (!ctx.inPre && (el.getAttribute("aria-hidden") || "").toLowerCase() === "true") {
        return "";
      }
      if (!ctx.inPre && el.style && String(el.style.opacity).trim() === "0") {
        return "";
      }
      const role = (el.getAttribute("role") || "").toLowerCase();
      if (role === "toolbar" || role === "button" || role === "status" || el.getAttribute("aria-live")) return "";
      if (!ctx.inPre && el.getAttribute("data-d-inline-text") !== null && (el.getAttribute("aria-haspopup") === "dialog" || /Trigger-/i.test(cls))) {
        return "";
      }
      if (!ctx.inPre && (el.getAttribute("data-layout") === "selection-pill" || el.getAttribute("data-tone") === "composer-reference" || /Mention-/i.test(cls))) {
        return "";
      }
      const tid = (el.getAttribute("data-testid") || "").toLowerCase();
      if (tid.includes("action-bar") || tid.includes("feedback")) return "";
      const ariaLbl = (el.getAttribute("aria-label") || "").trim();
      if (tag === "div" && !ctx.inPre) {
        if (
          cls.includes("ActionBar-") ||
          cls.includes("StickyActionBar-") ||
          cls.includes("md-code-block-banner") ||
          cls.includes("code-block-header") ||
          cls.includes("code-block__header")
        ) {
          return "";
        }
        const isCodeSurface =
          cls.includes("CodeSurface-") ||
          (cls.includes("md-code-block") && !cls.includes("md-code-block-banner"));
        if (isCodeSurface && !el.querySelector("p, h1, h2, h3, ul, ol, table")) {
          let sLang = "";
          const hdrNode = el.querySelector(
            ".md-code-block-banner .d813de27, [class*='ActionBar-'] .truncate, [class*='ActionBar-'] .flex-1, .md-code-block-banner, [class*='ActionBar-']"
          );
          if (hdrNode) {
            sLang = cleanCodeHeaderLang(readHeaderNonBtnText(hdrNode));
          }
          const preNode = el.querySelector("pre");
          if (preNode) {
            return walk(preNode, { ...ctx, forcedLang: sLang });
          }
          const codeNode =
            el.querySelector(".chatgpt-code-scrollport code, [class*='InlineCodePane-'] code, code[class*='CodeContent-']") ||
            Array.from(el.querySelectorAll("code")).find((c) => !c.closest || !c.closest("[class*='ActionBar-'], .md-code-block-banner"));
          if (codeNode) {
            const code = (codeNode.querySelector && codeNode.querySelector("br")
              ? walk(codeNode, { ...ctx, inPre: true })
              : (codeNode.textContent || "")
            ).replace(/\u00a0/g, " ").replace(/\n+$/, "");
            const finalLang = inferFenceLang(sLang, code);
            const fence = "`".repeat(Math.max(3, maxBacktickRun(code) + 1));
            return `\n${fence}${finalLang}\n${code}\n${fence}\n\n`;
          }
        }
        const isNamedCodeBox = cls.includes("group/copy") || cls.includes("code-block") || cls.includes("md-code-block") || (/code$/i.test(ariaLbl) && Boolean(el.querySelector("pre")));
        if (isNamedCodeBox && !el.querySelector("p, h1, h2, h3, ul, ol, table")) {
          const pres = Array.from(el.querySelectorAll("pre")).filter((p) => !p.parentElement?.closest("pre"));
          if (pres.length) return pres.map((p) => walk(p, ctx)).join("");
        }
        // Fallback: skip anonymous code-block header bars immediately preceding a <pre> or block code pane
        if (!el.querySelector("pre, p, h1, h2, h3, ul, ol, table, [class*='InlineCodePane-'], code[class*='CodeContent-']") && el.parentElement) {
          const sibs = Array.from(el.parentElement.children || []);
          const myIdx = sibs.indexOf(el);
          const nextCode = myIdx >= 0
            ? sibs.slice(myIdx + 1).find((s) =>
                (s.tagName || "").toLowerCase() === "pre" ||
                (s.querySelector && s.querySelector("pre, [class*='InlineCodePane-'], .chatgpt-code-scrollport, code[class*='CodeContent-']"))
              )
            : null;
          if (nextCode) {
            const hasCopy = Boolean(el.querySelector("button[aria-label*='Copy' i], [data-testid*='copy' i], [role='button'], .ds-button"));
            const headerTxt = readHeaderNonBtnText(el).replace(/\s+/g, " ").trim();
            if (hasCopy || (/^[a-z0-9_+#.-]{1,24}$/i.test(headerTxt) && !/[.!?]/.test(headerTxt))) {
              return "";
            }
          }
        }
      }

      if (tag === "br") return "\n";
      if (tag === "hr") return "\n---\n\n";

      if (tag === "pre") {
        const codeEl = el.querySelector("code");
        let srcEl = codeEl;
        if (!srcEl && typeof el.cloneNode === "function") {
          srcEl = el.cloneNode(true);
          for (const chrome of srcEl.querySelectorAll(
            "button, [role='button'], .ds-button, [role='toolbar'], .code-block-header, .code-block__header, .md-code-block-banner, [class*='ActionBar-'], [data-testid*='copy' i]"
          )) {
            chrome.remove();
          }
        } else if (!srcEl) {
          srcEl = el;
        }
        let lang = ctx.forcedLang || srcEl.getAttribute("data-language") || el.getAttribute("data-language") || "";
        for (const cTok of `${getClassStr(srcEl)} ${getClassStr(el)}`.split(/\s+/)) {
          const lm = cTok.match(/^(?:language|lang)-(.+)$/i);
          if (lm) { lang = lm[1]; break; }
        }
        lang = String(lang || "").trim();
        if (!lang && el.parentElement) {
          let probe = el;
          for (let up = 0; up < 2 && probe && probe.parentElement && !lang; up++) {
            const parent = probe.parentElement;
            if (parent.querySelector && parent.querySelector("p, h1, h2, h3, ul, ol, table")) break;
            const sibs = Array.from(parent.children || []);
            const idx = sibs.indexOf(probe);
            for (let si = idx - 1; si >= 0 && !lang; si--) {
              const sib = sibs[si];
              if (!sib || (sib.querySelector && sib.querySelector("pre"))) continue;
              const candLang = cleanCodeHeaderLang(readHeaderNonBtnText(sib));
              if (candLang) {
                lang = candLang;
              }
            }
            probe = parent;
          }
        }
        const code = (srcEl.textContent || "").replace(/\u00a0/g, " ").replace(/\n$/, "");
        lang = inferFenceLang(lang, code);
        const fence = "`".repeat(Math.max(3, maxBacktickRun(code) + 1));
        return `\n${fence}${lang}\n${code}\n${fence}\n\n`;
      }

      if (tag === "code" && !ctx.inPre) {
        const parentCls = el.parentElement ? getClassStr(el.parentElement) : "";
        const isBlockCode =
          cls.includes("CodeContent-") ||
          cls.includes("whitespace-pre") ||
          /\bblock\b/.test(cls) ||
          /InlineCodePane-|chatgpt-code-scrollport|Source-/.test(parentCls) ||
          (!ctx.inline && (el.textContent || "").includes("\n"));
        if (isBlockCode) {
          const code = (el.querySelector && el.querySelector("br")
            ? walk(el, { ...ctx, inPre: true })
            : (el.textContent || "")
          ).replace(/\u00a0/g, " ").replace(/\n+$/, "");
          let bLang = ctx.forcedLang || el.getAttribute("data-language") || "";
          if (!bLang && el.closest) {
            const surface = el.closest("[class*='CodeSurface-'], .md-code-block");
            const hdr = surface
              ? surface.querySelector("[class*='ActionBar-'] .truncate, [class*='ActionBar-'] .flex-1, .md-code-block-banner .d813de27, .md-code-block-banner, [class*='ActionBar-']")
              : null;
            if (hdr) bLang = cleanCodeHeaderLang(readHeaderNonBtnText(hdr));
          }
          bLang = inferFenceLang(bLang, code);
          const fence = "`".repeat(Math.max(3, maxBacktickRun(code) + 1));
          return `\n${fence}${bLang}\n${code}\n${fence}\n\n`;
        }
        const inline = (el.textContent || "").replace(/\s+/g, " ");
        const ticks = "`".repeat(Math.max(1, maxBacktickRun(inline) + 1));
        return `${ticks}${inline}${ticks}`;
      }

      if (tag === "table") {
        const trs = Array.from(el.querySelectorAll("tr")).filter((tr) => !tr.closest || tr.closest("table") === el);
        const rows = [];
        let maxCols = 0;
        for (const tr of trs) {
          const cells = [];
          for (const c of tr.children || []) {
            const cTag = (c.tagName || "").toLowerCase();
            if (cTag === "th" || cTag === "td") {
              cells.push(walk(c, { ...ctx, inline: true }).replace(/\s+/g, " ").replace(/\|/g, "\\|").trim());
            }
          }
          if (cells.length) {
            maxCols = Math.max(maxCols, cells.length);
            rows.push(cells);
          }
        }
        if (!rows.length) return "";
        const norm = (r) => {
          const padded = r.slice();
          while (padded.length < maxCols) padded.push("");
          return `| ${padded.join(" | ")} |`;
        };
        const outLines = [norm(rows[0]), `| ${Array(maxCols).fill("---").join(" | ")} |`];
        for (let rIdx = 1; rIdx < rows.length; rIdx++) {
          outLines.push(norm(rows[rIdx]));
        }
        return `\n${outLines.join("\n")}\n\n`;
      }

      if (tag === "ol" || tag === "ul") {
        const depth = ctx.listDepth || 0;
        let out = depth === 0 ? "\n" : "";
        let idx = 1;
        for (const child of el.children || []) {
          if ((child.tagName || "").toLowerCase() === "li") {
            out += walk(child, { ...ctx, ordered: tag === "ol", index: idx++, listDepth: depth });
          } else {
            out += walk(child, ctx);
          }
        }
        return depth === 0 ? `${out}\n` : out;
      }

      if (tag === "li") {
        const depth = ctx.listDepth || 0;
        const indent = "  ".repeat(depth);
        const prefix = ctx.ordered ? `${ctx.index}. ` : "- ";
        const textParts = [];
        const nestedLists = [];
        for (const child of el.childNodes || []) {
          const cTag = child.nodeType === Node.ELEMENT_NODE ? (child.tagName || "").toLowerCase() : "";
          if (cTag === "ul" || cTag === "ol") {
            const sub = walk(child, { ...ctx, listDepth: depth + 1, ordered: false, index: 0 });
            if (sub.trim()) nestedLists.push(sub.replace(/^\n+|\n+$/g, ""));
          } else {
            textParts.push(walk(child, { ...ctx, listDepth: depth + 1 }));
          }
        }
        const mainText = textParts.join("").replace(/\n{2,}/g, "\n").trim();
        let line = mainText ? `${indent}${prefix}${mainText.replace(/\n/g, `\n${indent}  `)}\n` : `${indent}${prefix}\n`;
        if (nestedLists.length) {
          line += `${nestedLists.join("\n")}\n`;
        }
        return line;
      }

      const nextCtx = (tag === "p" || tag === "a" || /^h[1-6]$/.test(tag)) ? { ...ctx, inline: true } : ctx;
      let inner = "";
      for (const child of el.childNodes || []) {
        inner += walk(child, tag === "pre" ? { ...ctx, inPre: true } : nextCtx);
      }

      if (ctx.inPre) return inner;
      if (/^h[1-6]$/.test(tag)) {
        const lvl = Number(tag[1]);
        return `\n${"#".repeat(lvl)} ${inner.trim()}\n\n`;
      }
      if (tag === "p") {
        const t = inner.trim();
        return t ? `${t}\n\n` : "";
      }
      if (tag === "strong" || tag === "b") return inner ? `**${inner}**` : "";
      if (tag === "em" || tag === "i") return inner ? `*${inner}*` : "";
      if (tag === "a") {
        const href = el.getAttribute("href") || "";
        const label = inner.trim() || href;
        return href ? `[${label}](${href})` : label;
      }
      if (tag === "blockquote") {
        const lines = inner.trim().split("\n").map(l => `> ${l}`);
        return lines.length ? `\n${lines.join("\n")}\n\n` : "";
      }
      if (tag === "tr") {
        const cells = [];
        for (const c of el.children || []) {
          cells.push(walk(c, { ...ctx, inline: true }).replace(/\s+/g, " ").trim());
        }
        return cells.length ? `| ${cells.join(" | ")} |\n` : "";
      }
      if (!ctx.inline && (tag === "div" || tag === "section" || tag === "article" || tag === "aside" || tag === "header" || tag === "footer")) {
        return inner && !inner.endsWith("\n") ? `${inner}\n` : inner;
      }
      return inner;
    }
    return walk(root).replace(/\n{3,}/g, "\n\n").trim();
  }

  function isThinkingNode(el) {
    if (!el || (typeof Element !== "undefined" ? !(el instanceof Element) : el.nodeType !== 1)) return false;
    const cds = (el.getAttribute("data-cds") || "").toLowerCase();
    if (cds === "turnstatus" || cds === "turnstatusstep") return true;
    const proseSize = (el.getAttribute("data-prose-size") || "").toLowerCase();
    const proseTables = (el.getAttribute("data-prose-tables") || "").toLowerCase();
    if (proseSize === "inherit" && !proseTables && el.closest && el.closest("[data-cds='AssistantMessage'], [data-is-streaming], .font-claude-response, .font-claude-message")) {
      return true;
    }
    const elId = (el.getAttribute("id") || "").toLowerCase();
    if (elId.startsWith("base-ui-") && el.closest && el.closest("[data-cds='TurnStatus'], [data-cds='AssistantMessage'], [data-is-streaming], .font-claude-response")) {
      if (!el.querySelector("[data-prose-tables='flat']")) return true;
    }
    const tid = (el.getAttribute("data-testid") || "").toLowerCase();
    const cls = getClassStr(el).toLowerCase();
    if (tid.includes("thinking") || tid.includes("thought") || cls.includes("claude-thought")) return true;
    const aria = (el.getAttribute("aria-label") || "").toLowerCase();
    if (aria.includes("thinking process") || aria.includes("thought process")) return true;
    // Collapsible Extended Thinking / Base UI disclosure toggles (skip containers wrapping answer .prose[data-prose-tables='flat'])
    if (el.matches("details, section, [data-is-streaming], div") && cds !== "assistantmessage" && !el.querySelector("[data-prose-tables='flat']")) {
      let btn = null;
      for (const ch of el.children || []) {
        const cTag = (ch.tagName || "").toLowerCase();
        if (cTag === "button" || cTag === "summary") { btn = ch; break; }
        if (cTag === "div") {
          for (const gch of ch.children || []) {
            if ((gch.tagName || "").toLowerCase() === "button") { btn = gch; break; }
          }
          if (btn) break;
        }
      }
      if (btn) {
        const hasExpandedCtrl = btn.getAttribute?.("aria-expanded") !== null && (
          btn.getAttribute?.("aria-controls") !== null || !el.querySelector(".standard-markdown, .progressive-markdown")
        );
        const lbl = `${btn.getAttribute("aria-label") || ""} ${btn.textContent || ""}`.replace(/\s+/g, " ").trim().toLowerCase();
        if (
          /^(?:thought process|thinking(?:\s+process)?|extended thinking|thought for\b)/.test(lbl) ||
          (hasExpandedCtrl && !el.querySelector("[data-cds='MessageActions']") && (
            !el.querySelector(".standard-markdown, .progressive-markdown") ||
            Boolean(el.querySelector("[id^='base-ui-']")) ||
            /(?:\b\d+(?:\.\d+)?s|\brunning)$/.test(lbl)
          ))
        ) {
          return true;
        }
      }
    }
    return false;
  }

  function isNonContentChromeNode(el) {
    if (isThinkingNode(el)) return true;
    const cds = (el.getAttribute("data-cds") || "").toLowerCase();
    if (cds === "messageactions") return true;
    const cls = getClassStr(el).toLowerCase();
    if (cls.split(/\s+/).includes("sr-only")) return true;
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (role === "status" || el.getAttribute("aria-live")) return true;
    if ((el.getAttribute("aria-hidden") || "").toLowerCase() === "true") return true;
    if (role === "toolbar" && /message actions/i.test(el.getAttribute("aria-label") || "")) {
      return true;
    }
    return false;
  }

  function readClaudeTurnContainers() {
    const explicit = Array.from(document.querySelectorAll(
      "[data-cds='AssistantMessage'], [data-testid='assistant-message'], [data-is-streaming], .font-claude-response, .font-claude-message"
    )).filter((el) => !el.closest("[data-cds='UserMessage'], [data-testid='user-message']") && !el.querySelector("[data-cds='UserMessage'], [data-testid='user-message']"));
    const topLevel = [];
    for (const el of explicit) {
      if (explicit.some(other => other !== el && other.contains(el))) continue;
      topLevel.push(el);
    }
    return topLevel;
  }

  function cleanVisibleThinkText(node) {
    if (!node) return "";
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || "";
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    const cls = getClassStr(node).toLowerCase();
    if (cls.split(/\s+/).includes("sr-only")) return "";
    if ((node.getAttribute("aria-hidden") || "").toLowerCase() === "true") return "";
    const role = (node.getAttribute("role") || "").toLowerCase();
    if (role === "status" || node.getAttribute("aria-live")) return "";
    const tag = (node.tagName || "").toLowerCase();
    if (tag === "style" || tag === "script" || tag === "svg" || tag === "br") return tag === "br" ? "\n" : "";
    let out = "";
    for (const ch of node.childNodes || []) {
      const part = cleanVisibleThinkText(ch);
      if (!part) continue;
      if (
        out &&
        ch.nodeType === Node.ELEMENT_NODE &&
        !/[\s\n]$/.test(out) &&
        !/^[\s\n.,;:!?\)\]]/.test(part) &&
        (tag === "button" || tag === "div")
      ) {
        out += "\n";
      }
      out += part;
    }
    if (tag === "div" || tag === "p" || tag === "li" || tag === "button" || tag === "summary" || tag === "section") {
      return out && !out.endsWith("\n") ? `${out}\n` : out;
    }
    return out;
  }

  function sanitizeThinkingChunk(raw) {
    const out = [];
    const seen = new Set();
    const noiseRe = /^(?:running|\d+(?:\.\d+)?s|thought for\s+\d+(?:\.\d+)?s?(?:\s+running)?|thought process|thinking(?:\s+process)?|extended thinking|you stopped claude['’]?s response|done)$/i;
    const gluedSplit = String(raw || "")
      .replace(/([A-Za-z0-9.?!\]\)])(?:\d{1,3}(?:\.\d+)?s)?running(?=[A-Z][a-z]{2,}|$)/g, "$1\n")
      .replace(/(\S.{11,}?)\1+/g, "$1");
    for (const ln of gluedSplit.split("\n")) {
      const isListItem = /^\s*(?:[-*]|\d+\.)\s+/.test(ln);
      let s = isListItem ? ln.replace(/[ \t]+/g, " ").trimEnd() : ln.replace(/\s+/g, " ").trim();
      const k = s.trim();
      if (!k || noiseRe.test(k)) continue;
      s = s.replace(/(?:\s*(?:\d{1,3}(?:\.\d+)?s\s*running|\brunning|\d{1,3}(?:\.\d+)?s))+$/i, "").trimEnd();
      const k2 = s.trim();
      if (!k2 || noiseRe.test(k2)) continue;
      const dedupKey = k2.toLowerCase();
      if (seen.has(dedupKey)) continue;
      seen.add(dedupKey);
      out.push(s);
    }
    return out.join("\n").trim();
  }

  function extractTurnContent(turnEl) {
    if (!turnEl) return { text: "", thinking: "", isStreaming: false, hasActionBar: false, stoppedByUser: false };
    const isStreaming =
      turnEl.getAttribute("data-is-streaming") === "true" ||
      Boolean(turnEl.querySelector("[data-is-streaming='true']")) ||
      Boolean(turnEl.closest("article[aria-label*='Currently streaming' i], [role='article'][aria-label*='Currently streaming' i]"));

    const explicitThink = Array.from(turnEl.querySelectorAll(
      "[data-cds='TurnStatus'], [data-cds='TurnStatusStep'], [data-testid*='thinking'], [data-testid*='thought'], .font-claude-thought"
    ));
    const disclosurePanels = new Set();
    for (const expBtn of turnEl.querySelectorAll("button[aria-expanded][aria-controls]")) {
      if (expBtn.closest("[data-cds='MessageActions'], .standard-markdown, .progressive-markdown")) continue;
      const ctrlId = (expBtn.getAttribute("aria-controls") || "").trim();
      if (ctrlId) disclosurePanels.add(ctrlId);
      const wrap = expBtn.closest("[data-cds='TurnStatus'], [data-cds='TurnStatusStep']") || expBtn.parentElement;
      if (wrap && wrap !== turnEl && !wrap.querySelector("[data-prose-tables='flat']")) {
        explicitThink.push(wrap);
      }
    }
    const thinkingNodes = Array.from(new Set(explicitThink));
    const thinkLines = [];
    const seenThinkLines = new Set();
    const pushThinkBlock = (raw) => {
      for (const ln of String(raw || "").split("\n")) {
        const t = ln.trimEnd();
        const k = t.trim().toLowerCase();
        if (!k) continue;
        if (seenThinkLines.has(k)) continue;
        seenThinkLines.add(k);
        thinkLines.push(t);
      }
    };
    const isInExpandedThinkMd = (el) => Boolean(el.closest && el.closest(".standard-markdown, .progressive-markdown, [data-testid*='markdown']"));
    for (const tn of thinkingNodes) {
      if (thinkingNodes.some(other => other !== tn && other.contains(tn))) continue;
      const steps = Array.from(tn.querySelectorAll("[data-cds='TurnStatusStep']"));
      const mdInTn = Array.from(tn.querySelectorAll(".standard-markdown, .progressive-markdown, [data-testid*='markdown']"));
      const emittedMd = new Set();
      if (steps.length) {
        for (const st of steps) {
          for (const btn of st.querySelectorAll("button")) {
            if (isInExpandedThinkMd(btn)) continue;
            const bTxt = sanitizeThinkingChunk(cleanVisibleThinkText(btn));
            if (bTxt) pushThinkBlock(bTxt);
          }
          for (const mr of st.querySelectorAll(".standard-markdown, .progressive-markdown, [data-testid*='markdown']")) {
            if (emittedMd.has(mr)) continue;
            emittedMd.add(mr);
            const md = sanitizeThinkingChunk(nodeToMarkdown(mr));
            if (md) pushThinkBlock(md);
          }
          if (!st.querySelector("button") && !st.querySelector(".standard-markdown, .progressive-markdown, [data-testid*='markdown']")) {
            const stTxt = sanitizeThinkingChunk(cleanVisibleThinkText(st));
            if (stTxt) pushThinkBlock(stTxt);
          }
        }
      } else {
        for (const btn of tn.querySelectorAll("button")) {
          if (isInExpandedThinkMd(btn)) continue;
          const bTxt = sanitizeThinkingChunk(cleanVisibleThinkText(btn));
          if (bTxt) pushThinkBlock(bTxt);
        }
      }
      for (const mr of mdInTn) {
        if (emittedMd.has(mr)) continue;
        if (mdInTn.some(other => other !== mr && other.contains(mr))) continue;
        const md = sanitizeThinkingChunk(nodeToMarkdown(mr));
        if (md) pushThinkBlock(md);
      }
      if (!steps.length && !mdInTn.length && !tn.querySelector("button")) {
        const fallbackThink = sanitizeThinkingChunk(cleanVisibleThinkText(tn) || nodeToMarkdown(tn));
        if (fallbackThink) pushThinkBlock(fallbackThink);
      }
    }
    const thinking = thinkLines.join("\n").trim();

    const mdRoots = Array.from(turnEl.querySelectorAll(".standard-markdown, .progressive-markdown, [data-testid*='markdown']"));
    let text = "";
    let sawAnswerMarkdown = false;
    const isInsideThinkingScope = (mr) => {
      if (isThinkingNode(mr)) return true;
      if (mr.closest("[data-cds='TurnStatus'], [data-cds='TurnStatusStep'], [data-prose-size='inherit'], [data-testid*='thinking'], [data-testid*='thought'], .font-claude-thought")) {
        return true;
      }
      let cur = mr;
      while (cur && cur !== turnEl) {
        const cid = (cur.getAttribute && cur.getAttribute("id")) || "";
        if (cid && (cid.startsWith("base-ui-") || disclosurePanels.has(cid))) return true;
        cur = cur.parentElement;
      }
      return false;
    };
    if (mdRoots.length) {
      const chunks = [];
      for (const mr of mdRoots) {
        if (isInsideThinkingScope(mr)) {
          continue;
        }
        sawAnswerMarkdown = true;
        if (mdRoots.some(other => other !== mr && other.contains(mr))) continue;
        const c = nodeToMarkdown(mr, isNonContentChromeNode);
        if (c) chunks.push(c);
      }
      text = chunks.join("\n\n").trim();
    }
    if (!text && !sawAnswerMarkdown) {
      const hasOnlyThinkingChrome = Boolean(thinkingNodes.length || thinking);
      const fb = nodeToMarkdown(turnEl, isNonContentChromeNode);
      if (fb) {
        const fbCleanThink = sanitizeThinkingChunk(fb);
        const fbLooksPureThink = Boolean(
          !fbCleanThink ||
          (thinking && fbCleanThink.split("\n").every((ln) => seenThinkLines.has(ln.trim().toLowerCase())))
        );
        if (!hasOnlyThinkingChrome || (!fbLooksPureThink && fb !== thinking)) {
          text = fb;
        }
      }
    }

    // Action bar ([data-cds='MessageActions'] toolbar with Copy/Thumbs up/Retry) sits OUTSIDE .prose / .standard-markdown / code-block group/copy
    const searchScope = turnEl.parentElement || turnEl;
    const msgActionsEl = searchScope.querySelector("[data-cds='MessageActions']");
    let hasActionBar = Boolean(msgActionsEl && msgActionsEl.querySelector("button"));
    if (!hasActionBar) {
      const actionBtns = searchScope.querySelectorAll(
        "button[aria-label*='Copy' i], button[data-testid*='copy' i], button[aria-label*='Give positive' i], button[aria-label*='Good response' i], button[aria-label*='Thumbs up' i], button[data-testid*='action-bar' i]"
      );
      for (const btn of actionBtns) {
        if (
          btn.closest(
            ".standard-markdown, .progressive-markdown, .code-block-header, .code-block__header, [data-testid*='thinking' i], [data-testid*='thought' i], [data-cds='TurnStatus']"
          )
        ) {
          continue;
        }
        let inCodeBox = false;
        let anc = btn.parentElement;
        while (anc && anc !== searchScope) {
          const aTag = (anc.tagName || "").toLowerCase();
          const aCls = getClassStr(anc);
          const aAria = (anc.getAttribute && anc.getAttribute("aria-label")) || "";
          if (aTag === "pre" || aTag === "code" || aCls.includes("group/copy") || aCls.includes("code-block") || /\bcode$/i.test(aAria)) {
            inCodeBox = true;
            break;
          }
          anc = anc.parentElement;
        }
        if (inCodeBox) continue;
        if (btn.parentElement && Array.from(btn.parentElement.children || []).some(c => (c.tagName || "").toUpperCase() === "PRE")) {
          continue;
        }
        hasActionBar = true;
        break;
      }
    }

    const stoppedByUser = Boolean(
      !text &&
      /\byou stopped claude['’]?s response\b/i.test(String(turnEl.innerText || turnEl.textContent || ""))
    );

    return { text, thinking, isStreaming, hasActionBar, stoppedByUser };
  }

  function readAllClaudeTurns() {
    return readClaudeTurnContainers().map(extractTurnContent);
  }

  async function attachImagesToClaude(images, editorEl) {
    if (!images?.length) return { ok: true, attached: 0, expected: 0, verified: true };
    const utils = globalThis.__wsaDomUtils;
    if (!utils?.attachImagesToEditor) {
      return { ok: false, attached: 0, expected: images.length, verified: false, error: "Image transfer unavailable" };
    }
    return utils.attachImagesToEditor(images, editorEl, findClaudeEditor, "Claude");
  }

  function normalizeCompareText(s) {
    return String(s || "").replace(/\r\n/g, "\n").replace(/\s+/g, " ").trim();
  }

  function selectClaudeEditorContents(editorEl) {
    try {
      const sel = typeof window !== "undefined" && window.getSelection ? window.getSelection() : null;
      if (sel && typeof document.createRange === "function") {
        const range = document.createRange();
        range.selectNodeContents(editorEl);
        sel.removeAllRanges();
        sel.addRange(range);
      }
    } catch { /* ignore */ }
  }

  function placeClaudeCaretAtEnd(editorEl) {
    try {
      const sel = typeof window !== "undefined" && window.getSelection ? window.getSelection() : null;
      if (!sel || typeof document.createRange !== "function") return;
      const range = document.createRange();
      const children = editorEl.childNodes;
      if (children && children.length > 0) {
        const last = children[children.length - 1];
        if (last && last.nodeType === 1) {
          range.selectNodeContents(last);
        } else {
          range.selectNodeContents(editorEl);
        }
      } else {
        range.selectNodeContents(editorEl);
      }
      if (typeof range.collapse === "function") range.collapse(false);
      sel.removeAllRanges();
      sel.addRange(range);
    } catch { /* ignore */ }
  }

  function setClaudePromptText(editorEl, promptText) {
    const cleanText = String(promptText || "").replace(/\r\n/g, "\n");
    editorEl.focus();
    selectClaudeEditorContents(editorEl);
    let inserted = false;
    // Native insertText in Chromium inserts multi-line text into Selection atomically without Enter-key submission
    try {
      inserted = Boolean(document.execCommand && document.execCommand("insertText", false, cleanText));
    } catch { /* ignore */ }
    const expNorm = normalizeCompareText(cleanText);
    const probePrefix = expNorm.slice(0, 24).trim();
    const probeTail = expNorm.slice(-24).trim();
    const hasExpectedText = () => {
      const curRaw = String(editorEl.innerText || editorEl.textContent || "").trim();
      const curNorm = normalizeCompareText(curRaw);
      if (!expNorm) return Boolean(curNorm) || cleanText.length === 0;
      if (!curNorm.includes(probePrefix)) return false;
      if (probeTail && !curNorm.includes(probeTail)) return false;
      if (expNorm.length > 64 && Math.abs(curNorm.length - expNorm.length) > Math.max(80, Math.floor(expNorm.length * 0.15))) {
        return false;
      }
      return true;
    };
    if (!inserted || !hasExpectedText()) {
      try {
        if (typeof ClipboardEvent === "function" && typeof DataTransfer === "function") {
          selectClaudeEditorContents(editorEl);
          const dt = new DataTransfer();
          dt.setData("text/plain", cleanText);
          editorEl.dispatchEvent(new ClipboardEvent("paste", {
            bubbles: true,
            cancelable: true,
            clipboardData: dt,
          }));
          if (hasExpectedText()) {
            inserted = true;
          }
        }
      } catch { /* ignore */ }
    }
    if (!inserted || !hasExpectedText()) {
      if (typeof document.createElement === "function" && typeof editorEl.replaceChildren === "function") {
        const pNodes = cleanText.split("\n").map((line) => {
          const p = document.createElement("p");
          if (line) {
            p.textContent = line;
          } else {
            p.appendChild(document.createElement("br"));
          }
          return p;
        });
        editorEl.replaceChildren(...pNodes);
      } else {
        editorEl.textContent = cleanText;
      }
      if (typeof InputEvent === "function") {
        try {
          editorEl.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertText", data: cleanText }));
        } catch { /* ignore */ }
        editorEl.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: cleanText }));
      } else if (typeof Event === "function") {
        editorEl.dispatchEvent(new Event("input", { bubbles: true }));
      }
      editorEl.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  function isClaudePromptStillInEditor(editorEl, promptText) {
    if (!editorEl || editorEl.isConnected === false) return false;
    const clean = normalizeCompareText(promptText);
    if (!clean) return false;
    const cur = normalizeCompareText(String(editorEl.innerText || editorEl.textContent || ""));
    if (!cur) return false;
    const head = clean.slice(0, 24).trim();
    const tail = clean.slice(-24).trim();
    if (head && cur.includes(head)) return true;
    if (tail && cur.length >= tail.length && cur.includes(tail)) return true;
    return false;
  }

  function triggerClaudeSubmit(editorEl, allowFormSubmitFallback = false) {
    const liveEd = findClaudeEditor() || editorEl;
    if (!liveEd || liveEd.isConnected === false) return "missing_editor";
    let sendBtn = findClaudeSendButton(liveEd);
    if (!sendBtn) {
      if (typeof liveEd.focus === "function") {
        try { liveEd.focus(); } catch { /* ignore */ }
      }
      if (typeof Event === "function" && typeof liveEd.dispatchEvent === "function") {
        try { liveEd.dispatchEvent(new Event("input", { bubbles: true })); } catch { /* ignore */ }
      }
      sendBtn = findClaudeSendButton(liveEd);
    }
    if (sendBtn) {
      if (typeof sendBtn.scrollIntoView === "function") {
        try { sendBtn.scrollIntoView({ block: "nearest" }); } catch { /* ignore */ }
      }
      if (typeof Event === "function" && typeof sendBtn.dispatchEvent === "function") {
        for (const evType of ["pointerdown", "mousedown", "pointerup", "mouseup"]) {
          try {
            const Ctor = evType.startsWith("pointer") && typeof PointerEvent === "function"
              ? PointerEvent
              : (typeof MouseEvent === "function" ? MouseEvent : Event);
            sendBtn.dispatchEvent(new Ctor(evType, { bubbles: true, cancelable: true, composed: true }));
          } catch { /* ignore */ }
        }
      }
      try { sendBtn.click(); } catch { /* ignore */ }
      return "button";
    }
    if (typeof liveEd.focus === "function") {
      try { liveEd.focus(); } catch { /* ignore */ }
    }
    placeClaudeCaretAtEnd(liveEd);
    if (allowFormSubmitFallback && typeof liveEd.closest === "function") {
      const form = liveEd.closest("form");
      if (form && typeof form.requestSubmit === "function") {
        try {
          form.requestSubmit();
          return "requestSubmit";
        } catch { /* fall through to Enter */ }
      }
    }
    if (typeof KeyboardEvent === "function" && typeof liveEd.dispatchEvent === "function") {
      for (const kEv of ["keydown", "keyup"]) {
        liveEd.dispatchEvent(new KeyboardEvent(kEv, {
          key: "Enter",
          code: "Enter",
          keyCode: 13,
          which: 13,
          bubbles: true,
          cancelable: true,
          composed: true,
        }));
      }
    }
    return "enter";
  }

  function readErrorBanner() {
    const msgs = [];
    for (const el of document.querySelectorAll(
      "[role='alert'], [role='status'], [role='dialog'], [data-testid*='limit' i], " +
      "[data-testid*='banner' i], [data-testid*='toast' i], [data-testid*='error' i], " +
      "[class*='toast' i], [class*='banner' i], [class*='alert' i]"
    )) {
      if (!isVisible(el)) continue;
      const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      if (t && t.length <= 1500) msgs.push(t);
    }
    return msgs.join(" | ");
  }

  function detectPromptTooLongMessage(text, isBanner = false) {
    if (globalThis.__wsaDomUtils && typeof globalThis.__wsaDomUtils.detectPromptTooLongMessage === "function") {
      return globalThis.__wsaDomUtils.detectPromptTooLongMessage(text, isBanner);
    }
    const raw = String(text || "").trim();
    if (!raw) return "";
    const re = /(?:the\s+message\s+you\s+submitted\s+was\s+too\s+long|message\s+(?:you\s+submitted\s+)?(?:is|was)\s+too\s+long|please\s+edit\s+it\s+and\s+resubmit|prompt\s+(?:is\s+)?too\s+long|input\s+(?:is\s+)?too\s+long|exceeds?\s+(?:the\s+)?max(?:imum)?\s+(?:message\s+)?length|character\s+limit\s+exceeded|too\s+many\s+characters|maximum\s+context\s+(?:length|limit|window)\s+exceeded|context\s+(?:window|limit)\s+exceeded)/i;
    if (isBanner) {
      const parts = raw.split(/\s*\|\s*|\r?\n+|(?<=[.!?])\s+/).map(p => p.replace(/\s+/g, " ").trim()).filter(Boolean);
      for (const seg of parts) {
        if (seg.length <= 460 && re.test(seg)) return seg;
      }
      const sb = raw.replace(/\s+/g, " ").trim();
      if (sb.length <= 1500 && re.test(sb)) return sb.slice(0, 320);
      return "";
    }
    const s = raw.replace(/\s+/g, " ").trim();
    return (s && s.length <= 460 && re.test(s)) ? s : "";
  }

  function detectLimitMessage(text, isBanner = false) {
    if (detectPromptTooLongMessage(text, isBanner)) return "";
    if (globalThis.__wsaDomUtils && typeof globalThis.__wsaDomUtils.detectLimitMessage === "function") {
      return globalThis.__wsaDomUtils.detectLimitMessage(text, isBanner);
    }
    const raw = String(text || "").trim();
    if (!raw) return "";
    const softPctRe = /\b(?:over\s+|nearly\s+|almost\s+|approaching\s+|used\s+)?(?:[1-9]?\d)\s*%\s+of\s+(?:your\s+)?(?:\S+\s+){0,3}(?:limit|limi|quota|usage)\b/i;
    const hardBlockRe = /\b(?:reached|exceeded|hit|exhausted|will\s+reset|resets?\s+(?:at|in|after)|0\s+messages|out\s+of\s+(?:free\s+)?messages|upgrade\s+to\s+pro\s+to\s+(?:continue|chat|send)|cannot\s+send|try\s+again\s+(?:later|at|in)|until\s+\d)\b/i;
    const isSoftOnly = (s) => softPctRe.test(s) && !hardBlockRe.test(s);
    const re = /(?:you(?:'|’)?ve|you\s+have)\s+(?:reached|hit|exceeded)(?:\s+\S+){0,6}\s+(?:cap|limit)|(?:rate|usage|message|hourly|daily(?:\s+(?:chat|message|free))?|weekly|monthly|\d+\s*[- ]?\s*hour|plan|free|session|reasoning|thinking|deepthink|search|credit)\s+limit\b(?:\s+(?:has\s+been\s+)?(?:reached|exceeded|hit|will\s+reset|resets))?|\blimit\s+(?:has\s+been\s+)?(?:reached|exceeded|resets|will\s+reset)|(?:out\s+of\s+(?:free\s+)?messages|(?:0|zero|no)\s+messages\s+(?:left|remaining)|(?:out\s+of|insufficient|no)\s+(?:venice\s+)?credits|upgrade\s+to\s+pro\s+to\s+(?:continue|chat|send)|too\s+many\s+(?:concurrent\s+)?requests|resource[_\s-]*exhausted|quota\s+exceeded|(?:server|service|system|model)\s+is\s+(?:currently\s+)?busy|high\s+(?:demand|traffic)|http\s*(?:error\s*)?429\b|responses\s+will\s+use\s+(?:another|a\s+different|the\s+mini|gemini\s+auto|auto|gemini\s+flash-lite|flash-lite|flash\s+lite|gemini\s+flash|flash)\b|\bswitched\s+to\s+(?:gemini\s+)?(?:auto|flash-lite|flash\s+lite|flash)\b|\busing\s+(?:gemini\s+)?(?:auto|flash-lite|flash\s+lite|flash)\s+(?:until|because|due\s+to|after)\b|\b(?:you(?:'|’)?ve|you\s+have)\s+reached\s+(?:your\s+|the\s+)?(?:gemini\s+)?(?:pro|advanced)?\s*limit\b|\b(?:gemini\s+)?(?:pro|advanced)\s+(?:limit|quota)\s+(?:reached|exceeded|hit)\b|\breached\s+(?:your|the)?\s*(?:daily|hourly|usage|message)?\s*limit\s+for\s+gemini\b)/i;
    if (isBanner) {
      const parts = raw.split(/\s*\|\s*|\r?\n+|(?<=[.!?])\s+/).map(p => p.replace(/\s+/g, " ").trim()).filter(Boolean);
      for (const seg of parts) {
        if (seg.length <= 460 && !isSoftOnly(seg) && re.test(seg)) return seg;
      }
      const sb = raw.replace(/\s+/g, " ").trim();
      if (sb.length <= 1500 && !isSoftOnly(sb) && re.test(sb)) return sb.slice(0, 320);
      return "";
    }
    const s = raw.replace(/\s+/g, " ").trim();
    return (s && s.length <= 460 && !isSoftOnly(s) && re.test(s)) ? s : "";
  }

  function readSafeguardPausedCards() {
    const cards = [];
    const seen = new Set();
    const re = /\bsafeguards?\s+flagged\s+this\s+message\b/i;
    for (const el of document.querySelectorAll(
      "[role='alert'], [role='status'], [role='dialog'], [data-testid*='safeguard' i], " +
      "[data-testid*='paused' i], [data-testid*='banner' i], [data-testid*='error' i], " +
      "main section, main div, article"
    )) {
      if (!isVisible(el)) continue;
      const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      if (!t || t.length > 650 || !re.test(t)) continue;
      // Skip wrapper ancestors when a tighter descendant holds the same safeguard card
      let hasTighterChild = false;
      try {
        for (const ch of el.children || []) {
          const ct = (ch.innerText || ch.textContent || "").replace(/\s+/g, " ").trim();
          if (ct && re.test(ct)) { hasTighterChild = true; break; }
        }
      } catch { /* ignore */ }
      if (hasTighterChild) continue;
      const key = t.slice(0, 180);
      if (seen.has(key)) continue;
      seen.add(key);
      cards.push(t.slice(0, 320));
    }
    return cards;
  }

  globalThis.__wsaClaudeRuntime = {
    version: 4,
    rev: 8,
    turns: Object.create(null),
    findEditor: findClaudeEditor,
    findSendButton: findClaudeSendButton,
    hasStopButton: hasClaudeStopButton,
    readModelBadge: readClaudeModelBadge,
    readAllTurns: readAllClaudeTurns,
    attachImagesToClaude,
    setPromptText: setClaudePromptText,
    isPromptStillInEditor: isClaudePromptStillInEditor,
    triggerSubmit: triggerClaudeSubmit,
    readErrorBanner,
    readSafeguardPausedCards,
    detectPromptTooLongMessage,
    detectLimitMessage,
  };
  return true;
}

async function _claudeStartTurnFn(reqId, prompt, images = []) {
  const rt = globalThis.__wsaClaudeRuntime;
  if (!rt || rt.version !== 4) {
    return { ok: false, error: "Claude page runtime missing" };
  }
  const sleepMs = ms => new Promise(r => setTimeout(r, ms));
  let editor = rt.findEditor();
  for (let i = 0; !editor && i < 80; i++) {
    if (i >= 6 && typeof rt.readErrorBanner === "function" && typeof rt.detectLimitMessage === "function") {
      const earlyLimit = rt.detectLimitMessage(rt.readErrorBanner(), true);
      if (earlyLimit) {
        return { ok: false, error: `[LIMIT_REACHED] Claude: ${earlyLimit}` };
      }
    }
    await sleepMs(250);
    editor = rt.findEditor();
  }
  if (!editor) {
    const missingBanner = typeof rt.readErrorBanner === "function" ? rt.readErrorBanner() : "";
    const missingLimit = typeof rt.detectLimitMessage === "function" ? rt.detectLimitMessage(missingBanner, true) : "";
    if (missingLimit) {
      return { ok: false, error: `[LIMIT_REACHED] Claude: ${missingLimit}` };
    }
    return {
      ok: false,
      error: "Claude prompt editor not found after 20s. Make sure claude.ai is open and signed in.",
    };
  }

  const monoNow = () => (typeof performance !== "undefined" && typeof performance.now === "function") ? performance.now() : Date.now();
  const tTurnStart = monoNow();
  let imagesAttached = null;
  let uploadMs = null;

  if (Array.isArray(images) && images.length > 0 && rt.attachImagesToClaude) {
    const tAttachStart = monoNow();
    const attachRes = await rt.attachImagesToClaude(images, editor);
    if (attachRes && attachRes.verified === false) {
      const errBanner = typeof rt.readErrorBanner === "function" ? rt.readErrorBanner() : "";
      const limitHit = typeof rt.detectLimitMessage === "function" ? rt.detectLimitMessage(errBanner, true) : "";
      if (limitHit) {
        return { ok: false, error: `[LIMIT_REACHED] Claude: ${limitHit}` };
      }
      return {
        ok: false,
        error: `[IMAGE_ATTACH_FAILED] Claude: ${attachRes.error || "Image upload could not be verified before sending."}`,
      };
    }
    imagesAttached = Boolean(attachRes && attachRes.verified === true);
    if (imagesAttached) {
      uploadMs = Math.max(0, Math.round(monoNow() - tAttachStart));
    }
    editor = rt.findEditor() || editor;
  }

  const beforeTurns = rt.readAllTurns();
  const beforeLast = beforeTurns.length ? beforeTurns[beforeTurns.length - 1] : { text: "", thinking: "" };
  const beforeBanner = typeof rt.readErrorBanner === "function" ? rt.readErrorBanner() : "";
  const beforeSafeguardCount = typeof rt.readSafeguardPausedCards === "function" ? rt.readSafeguardPausedCards().length : 0;
  const modelBadge = rt.readModelBadge();

  rt.setPromptText(editor, prompt);
  await sleepMs(160);

  const maxBtnWait = imagesAttached ? 40 : 20;
  let sendBtn = rt.findSendButton(rt.findEditor() || editor);
  for (let i = 0; !sendBtn && i < maxBtnWait; i++) {
    if (i === 8 || (imagesAttached && i === 24)) {
      rt.setPromptText(rt.findEditor() || editor, prompt);
    } else if ((i === 4 || i === 12 || i === 20) && typeof Event === "function") {
      const liveEd = rt.findEditor() || editor;
      if (liveEd && typeof liveEd.dispatchEvent === "function") {
        try { liveEd.dispatchEvent(new Event("input", { bubbles: true, composed: true })); } catch { /* ignore */ }
        try { liveEd.dispatchEvent(new Event("change", { bubbles: true, composed: true })); } catch { /* ignore */ }
      }
    }
    await sleepMs(150);
    sendBtn = rt.findSendButton(rt.findEditor() || editor);
  }

  const hasSubmitEffect = () => {
    if (rt.hasStopButton()) return true;
    if (rt.readAllTurns().length > beforeTurns.length) return true;
    const liveEd = rt.findEditor() || editor;
    if (typeof rt.isPromptStillInEditor === "function" && !rt.isPromptStillInEditor(liveEd, prompt)) return true;
    if (typeof rt.readErrorBanner === "function" && rt.readErrorBanner() !== beforeBanner) return true;
    return false;
  };

  if (typeof rt.triggerSubmit === "function") {
    rt.triggerSubmit(editor, false);
  } else if (sendBtn) {
    sendBtn.click();
  }
  await sleepMs(180);
  if (!hasSubmitEffect()) {
    await sleepMs(260);
    if (!hasSubmitEffect() && typeof rt.triggerSubmit === "function") {
      rt.triggerSubmit(editor, true);
      await sleepMs(180);
    }
  }

  const now = Date.now();
  const afterEd = rt.findEditor() || editor;
  rt.turns[reqId] = {
    startedAt: now,
    lastChangeAt: now,
    lastRetryAt: now,
    lastText: "",
    lastThinking: "",
    prompt: String(prompt || ""),
    submitRetries: 0,
    beforeTurnCount: beforeTurns.length,
    beforeLastText: beforeLast.text || "",
    beforeLastThinking: beforeLast.thinking || "",
    beforeBanner,
    beforeSafeguardCount,
    sawActivity: false,
    sawStop: false,
    sawEditorCleared: Boolean(!afterEd || (typeof rt.isPromptStillInEditor === "function" && !rt.isPromptStillInEditor(afterEd, prompt))),
    modelBadge,
  };
  return {
    ok: true,
    model: modelBadge || "Claude Web",
    images_attached: imagesAttached,
    upload_ms: uploadMs,
    submit_ms: Math.max(0, Math.round(monoNow() - tTurnStart)),
  };
}

function _claudePollTurnFn(reqId) {
  const rt = globalThis.__wsaClaudeRuntime;
  if (!rt || rt.version !== 4) return { needRuntime: true };
  const turn = rt.turns[reqId];
  if (!turn) return { missing: true };

  const now = Date.now();
  const pageUrl = typeof window !== "undefined" && window.location ? window.location.href : (typeof location !== "undefined" ? location.href : "");
  const allTurns = rt.readAllTurns();
  const stopVisible = rt.hasStopButton();
  const liveEd = typeof rt.findEditor === "function" ? rt.findEditor() : null;
  if (stopVisible || !liveEd || (typeof rt.isPromptStillInEditor === "function" && !rt.isPromptStillInEditor(liveEd, turn.prompt))) {
    turn.sawEditorCleared = true;
  }
  if (stopVisible) {
    turn.sawStop = true;
    turn.sawActivity = true;
  }

  let latest = null;
  if (allTurns.length > turn.beforeTurnCount) {
    const candidate = allTurns[allTurns.length - 1];
    if (
      !turn.sawActivity &&
      !turn.sawStop &&
      !stopVisible &&
      !turn.sawEditorCleared &&
      candidate &&
      !candidate.isStreaming &&
      candidate.hasActionBar &&
      liveEd &&
      typeof rt.isPromptStillInEditor === "function" &&
      rt.isPromptStillInEditor(liveEd, turn.prompt)
    ) {
      turn.beforeTurnCount = allTurns.length;
      turn.beforeLastText = candidate.text || "";
      turn.beforeLastThinking = candidate.thinking || "";
    } else {
      latest = candidate;
      turn.sawActivity = true;
    }
  } else if (allTurns.length > 0 && turn.sawStop) {
    const candidate = allTurns[allTurns.length - 1];
    if (candidate.text !== turn.beforeLastText || candidate.thinking !== turn.beforeLastThinking || candidate.isStreaming) {
      latest = candidate;
      turn.sawActivity = true;
    }
  }

  const isStillStreaming = Boolean(stopVisible || latest?.isStreaming);
  const curText = latest?.text || "";
  const curThinking = latest?.thinking || "";
  if (curText !== turn.lastText && (curText || isStillStreaming)) {
    turn.lastText = curText;
    turn.lastChangeAt = now;
    turn.sawActivity = true;
  }
  if (curThinking && curThinking !== turn.lastThinking && (curThinking.length >= turn.lastThinking.length || curThinking.split("\n").length >= turn.lastThinking.split("\n").length)) {
    turn.lastThinking = curThinking;
    turn.lastChangeAt = now;
    turn.sawActivity = true;
  }

  const quietMs = now - turn.lastChangeAt;
  if (!isStillStreaming) {
    if (!turn.lastText && typeof rt.readSafeguardPausedCards === "function") {
      const sgCards = rt.readSafeguardPausedCards();
      const prevSg = Number(turn.beforeSafeguardCount || 0);
      if (sgCards.length > prevSg) {
        const sgMsg = sgCards[sgCards.length - 1];
        delete rt.turns[reqId];
        return {
          done: false,
          error: `[SAFEGUARD_PAUSED] Claude: ${sgMsg}`,
          text: "",
          thinking: "",
          model: rt.readModelBadge() || turn.modelBadge || "Claude Web",
          url: pageUrl,
        };
      }
    }
    const pageBanner = typeof rt.readErrorBanner === "function" ? rt.readErrorBanner() : "";
    if (typeof rt.detectPromptTooLongMessage === "function") {
      const turnTooLong = curText ? rt.detectPromptTooLongMessage(curText) : "";
      const pageTooLong = pageBanner ? rt.detectPromptTooLongMessage(pageBanner, true) : "";
      const matchedTooLong = turnTooLong || (!turn.lastText ? pageTooLong : (turn.lastText === curText ? pageTooLong : ""));
      if (matchedTooLong && !turn.lastText.includes("```")) {
        delete rt.turns[reqId];
        return {
          done: false,
          error: `[PROMPT_TOO_LONG] Claude: ${matchedTooLong}`,
          text: "",
          thinking: "",
          model: rt.readModelBadge() || turn.modelBadge || "Claude Web",
          url: pageUrl,
        };
      }
    }
    if (typeof rt.detectLimitMessage === "function") {
      const turnLimitErr = curText ? rt.detectLimitMessage(curText) : "";
      const pageBannerErr = pageBanner ? rt.detectLimitMessage(pageBanner, true) : "";
      const matchedLimit = turnLimitErr || (!turn.lastText ? pageBannerErr : (turn.lastText === curText ? pageBannerErr : ""));
      if (matchedLimit && !turn.lastText.includes("```")) {
        delete rt.turns[reqId];
        return {
          done: false,
          error: `[LIMIT_REACHED] Claude: ${matchedLimit}`,
          text: "",
          thinking: "",
          model: rt.readModelBadge() || turn.modelBadge || "Claude Web",
          url: pageUrl,
        };
      }
    }
  }
  const isPromptInEd = Boolean(turn.prompt && liveEd && typeof rt.isPromptStillInEditor === "function" && rt.isPromptStillInEditor(liveEd, turn.prompt));
  const promptUnsent = Boolean(
    !turn.sawActivity &&
    !isStillStreaming &&
    allTurns.length <= turn.beforeTurnCount &&
    isPromptInEd
  );
  if (promptUnsent && typeof rt.triggerSubmit === "function" && (turn.submitRetries || 0) < 4 && (now - (turn.lastRetryAt || turn.startedAt)) >= 1800) {
    const sendMissing = typeof rt.findSendButton === "function" && !rt.findSendButton(liveEd);
    if (sendMissing && isPromptInEd && (turn.submitRetries || 0) < 2 && typeof rt.setPromptText === "function") {
      rt.setPromptText(liveEd, turn.prompt);
    }
    rt.triggerSubmit(liveEd, (turn.submitRetries || 0) >= 1);
    turn.submitRetries = (turn.submitRetries || 0) + 1;
    turn.lastRetryAt = now;
  }

  const stoppedByUser = Boolean(
    !turn.lastText &&
    !isStillStreaming &&
    (latest?.stoppedByUser || /\byou stopped claude['’]?s response\b/i.test(`${curThinking}\n${curText}\n${turn.lastThinking}`))
  );
  const emptyStalledAfterActivity = Boolean(turn.sawActivity && !turn.lastText && !isStillStreaming && quietMs >= 10000);
  const stuckUnsentStall = Boolean(promptUnsent && (turn.submitRetries || 0) >= 3 && (now - turn.startedAt) >= 14000);
  const neverStartedStall = Boolean(!turn.sawActivity && !isStillStreaming && (now - turn.startedAt) >= 45000);
  if (stoppedByUser || emptyStalledAfterActivity || stuckUnsentStall || neverStartedStall) {
    const fallbackBanner = typeof rt.readErrorBanner === "function" ? rt.readErrorBanner() : "";
    const tooLongMsg = typeof rt.detectPromptTooLongMessage === "function" ? rt.detectPromptTooLongMessage(fallbackBanner, true) : "";
    const limitMsg = !tooLongMsg && typeof rt.detectLimitMessage === "function" ? rt.detectLimitMessage(fallbackBanner, true) : "";
    delete rt.turns[reqId];
    return {
      done: false,
      error: tooLongMsg
        ? `[PROMPT_TOO_LONG] Claude: ${tooLongMsg}`
        : (limitMsg
          ? `[LIMIT_REACHED] Claude: ${limitMsg}`
          : (stoppedByUser
            ? "Claude response was stopped in browser"
            : (stuckUnsentStall
              ? "Claude prompt remained unsubmitted in composer (Send button/form did not trigger response)"
              : "Claude tab did not start or produce a text response"))),
      text: turn.lastText,
      thinking: stoppedByUser ? "" : turn.lastThinking,
      model: rt.readModelBadge() || turn.modelBadge || "Claude Web",
      url: pageUrl,
    };
  }

  const done = Boolean(
    turn.sawActivity &&
    turn.lastText &&
    !isStillStreaming &&
    ((latest?.hasActionBar && quietMs >= 450) || quietMs >= 1400)
  );

  if (done) {
    delete rt.turns[reqId];
  }

  return {
    done,
    text: turn.lastText,
    thinking: turn.lastThinking,
    streaming: isStillStreaming,
    model: rt.readModelBadge() || turn.modelBadge || "Claude Web",
    url: pageUrl,
  };
}

function _installWsaDomUtilsFn() {
  if (
    globalThis.__wsaDomUtils &&
    globalThis.__wsaDomUtils.version === 3 &&
    globalThis.__wsaDomUtils.rev === 10 &&
    typeof globalThis.__wsaDomUtils.detectPromptTooLongMessage === "function"
  ) {
    return true;
  }

  function getClassStr(el) {
    if (!el) return "";
    if (typeof el.className === "string") return el.className;
    if (el.className && typeof el.className.baseVal === "string") return el.className.baseVal;
    return "";
  }

  function isVisible(el) {
    if (!el || !(el instanceof Element)) return false;
    try {
      let cur = el;
      for (let depth = 0; cur && depth < 24; depth++) {
        if (cur.hidden === true ||
            (typeof cur.hasAttribute === "function" && cur.hasAttribute("hidden")) ||
            (typeof cur.getAttribute === "function" && cur.getAttribute("aria-hidden") === "true" && cur.getAttribute("__wsa_probe__") !== "true")) {
          return false;
        }
        const style = typeof getComputedStyle === "function" ? getComputedStyle(cur) : null;
        if (style && (style.display === "none" || style.visibility === "hidden" || parseFloat(style.opacity || "1") <= 0.05)) {
          return false;
        }
        cur = cur.parentElement || null;
      }
      if (typeof el.getBoundingClientRect === "function") {
        const rect = el.getBoundingClientRect();
        if (rect && rect.width <= 1 && rect.height <= 1 && (!el.getClientRects || el.getClientRects().length === 0)) {
          if (el.offsetParent === null || rect.width <= 0 || rect.height <= 0) return false;
        }
      }
    } catch { /* ignore */ }
    return true;
  }

  function normalizeCompareText(s) {
    return String(s || "").replace(/\r\n/g, "\n").replace(/\s+/g, " ").trim();
  }

  function maxBacktickRun(str) {
    let maxRun = 0;
    const matches = String(str || "").match(/`+/g);
    if (matches) {
      for (const m of matches) {
        if (m.length > maxRun) maxRun = m.length;
      }
    }
    return maxRun;
  }

  function nodeToMarkdown(root, skipPredicate = null) {
    if (!root) return "";
    function readHeaderNonBtnText(node) {
      if (!node) return "";
      if (node.nodeType === 3) return node.textContent || "";
      if (node.nodeType !== 1) return "";
      const t = (node.tagName || "").toLowerCase();
      const r = (node.getAttribute("role") || "").toLowerCase();
      const c = getClassStr(node);
      if (t === "button" || t === "svg" || t === "style" || t === "script") return "";
      if (r === "button" || /\bds-button\b/i.test(c) || /\bsr-only\b/i.test(c)) return "";
      if (node.childNodes && node.childNodes.length) {
        return Array.from(node.childNodes).map(readHeaderNonBtnText).join(" ");
      }
      return node.textContent || "";
    }
    function cleanCodeHeaderLang(rawText) {
      const cleaned = String(rawText || "")
        .replace(/\b(?:copy(?:\s+code)?|copied|download|enable\s+word\s+wrap|disable\s+word\s+wrap|word\s+wrap|code\s*snippet)\b/gi, "")
        .replace(/\s+/g, " ")
        .trim();
      if (!cleaned || /^(?:copy|copied|download|code|snippet)$/i.test(cleaned)) return "";
      return /^[a-z0-9_+#.-]{1,24}$/i.test(cleaned) ? cleaned.toLowerCase() : "";
    }
    function inferFenceLang(lang, codeText) {
      if (lang) return lang;
      const trimmed = (codeText || "").trim();
      if (trimmed[0] === "{" || trimmed[0] === "[") {
        try {
          const parsed = JSON.parse(trimmed);
          const items = Array.isArray(parsed) ? parsed : [parsed];
          if (items.length && items.every((it) => it && typeof it === "object" && typeof it.name === "string" && it.name.trim())) {
            return "tool_call";
          }
        } catch { /* ignore */ }
      }
      return "";
    }
    function walk(node, ctx = {}) {
      if (!node) return "";
      if (node.nodeType === Node.TEXT_NODE) {
        return node.textContent || "";
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return "";
      const el = node;
      if (skipPredicate && skipPredicate(el)) return "";
      const tag = (el.tagName || "").toLowerCase();
      if (tag === "style" || tag === "script" || tag === "svg" || tag === "button" || tag === "nav" || tag === "noscript" || tag === "template") {
        return "";
      }
      const cls = getClassStr(el);
      if (cls && cls.toLowerCase().split(/\s+/).includes("sr-only")) {
        return "";
      }
      if (!ctx.inPre && (el.getAttribute("aria-hidden") || "").toLowerCase() === "true") {
        return "";
      }
      if (!ctx.inPre && el.style && String(el.style.opacity).trim() === "0") {
        return "";
      }
      const role = (el.getAttribute("role") || "").toLowerCase();
      if (role === "toolbar" || role === "button" || role === "status" || el.getAttribute("aria-live")) return "";
      if (!ctx.inPre && el.getAttribute("data-d-inline-text") !== null && (el.getAttribute("aria-haspopup") === "dialog" || /Trigger-/i.test(cls))) {
        return "";
      }
      if (!ctx.inPre && (el.getAttribute("data-layout") === "selection-pill" || el.getAttribute("data-tone") === "composer-reference" || /Mention-/i.test(cls))) {
        return "";
      }
      const tid = (el.getAttribute("data-testid") || "").toLowerCase();
      if (tid.includes("action-bar") || tid.includes("feedback")) return "";
      const ariaLbl = (el.getAttribute("aria-label") || "").trim();
      if (tag === "div" && !ctx.inPre) {
        if (
          cls.includes("ActionBar-") ||
          cls.includes("StickyActionBar-") ||
          cls.includes("md-code-block-banner") ||
          cls.includes("code-block-header") ||
          cls.includes("code-block__header")
        ) {
          return "";
        }
        const isCodeSurface =
          cls.includes("CodeSurface-") ||
          (cls.includes("md-code-block") && !cls.includes("md-code-block-banner"));
        if (isCodeSurface && !el.querySelector("p, h1, h2, h3, ul, ol, table")) {
          let sLang = "";
          const hdrNode = el.querySelector(
            ".md-code-block-banner .d813de27, [class*='ActionBar-'] .truncate, [class*='ActionBar-'] .flex-1, .md-code-block-banner, [class*='ActionBar-']"
          );
          if (hdrNode) {
            sLang = cleanCodeHeaderLang(readHeaderNonBtnText(hdrNode));
          }
          const preNode = el.querySelector("pre");
          if (preNode) {
            return walk(preNode, { ...ctx, forcedLang: sLang });
          }
          const codeNode =
            el.querySelector(".chatgpt-code-scrollport code, [class*='InlineCodePane-'] code, code[class*='CodeContent-']") ||
            Array.from(el.querySelectorAll("code")).find((c) => !c.closest || !c.closest("[class*='ActionBar-'], .md-code-block-banner"));
          if (codeNode) {
            const code = (codeNode.querySelector && codeNode.querySelector("br")
              ? walk(codeNode, { ...ctx, inPre: true })
              : (codeNode.textContent || "")
            ).replace(/\u00a0/g, " ").replace(/\n+$/, "");
            const finalLang = inferFenceLang(sLang, code);
            const fence = "`".repeat(Math.max(3, maxBacktickRun(code) + 1));
            return `\n${fence}${finalLang}\n${code}\n${fence}\n\n`;
          }
        }
        const isNamedCodeBox = cls.includes("group/copy") || cls.includes("code-block") || cls.includes("md-code-block") || (/code$/i.test(ariaLbl) && Boolean(el.querySelector("pre")));
        if (isNamedCodeBox && !el.querySelector("p, h1, h2, h3, ul, ol, table")) {
          const pres = Array.from(el.querySelectorAll("pre")).filter((p) => !p.parentElement?.closest("pre"));
          if (pres.length) return pres.map((p) => walk(p, ctx)).join("");
        }
        if (!el.querySelector("pre, p, h1, h2, h3, ul, ol, table, [class*='InlineCodePane-'], code[class*='CodeContent-']") && el.parentElement) {
          const sibs = Array.from(el.parentElement.children || []);
          const myIdx = sibs.indexOf(el);
          const nextCode = myIdx >= 0
            ? sibs.slice(myIdx + 1).find((s) =>
                (s.tagName || "").toLowerCase() === "pre" ||
                (s.querySelector && s.querySelector("pre, [class*='InlineCodePane-'], .chatgpt-code-scrollport, code[class*='CodeContent-']"))
              )
            : null;
          if (nextCode) {
            const hasCopy = Boolean(el.querySelector("button[aria-label*='Copy' i], [data-testid*='copy' i], [role='button'], .ds-button"));
            const headerTxt = readHeaderNonBtnText(el).replace(/\s+/g, " ").trim();
            if (hasCopy || (/^[a-z0-9_+#.-]{1,24}$/i.test(headerTxt) && !/[.!?]/.test(headerTxt))) {
              return "";
            }
          }
        }
      }

      if (tag === "br") return "\n";
      if (tag === "hr") return "\n---\n\n";

      if (tag === "pre") {
        const codeEl = el.querySelector("code");
        let srcEl = codeEl;
        if (!srcEl && typeof el.cloneNode === "function") {
          srcEl = el.cloneNode(true);
          for (const chrome of srcEl.querySelectorAll(
            "button, [role='button'], .ds-button, [role='toolbar'], .code-block-header, .code-block__header, .md-code-block-banner, [class*='ActionBar-'], [data-testid*='copy' i]"
          )) {
            chrome.remove();
          }
        } else if (!srcEl) {
          srcEl = el;
        }
        let lang = ctx.forcedLang || srcEl.getAttribute("data-language") || el.getAttribute("data-language") || "";
        for (const cTok of `${getClassStr(srcEl)} ${getClassStr(el)}`.split(/\s+/)) {
          const lm = cTok.match(/^(?:language|lang)-(.+)$/i);
          if (lm) { lang = lm[1]; break; }
        }
        lang = String(lang || "").trim();
        if (!lang && el.parentElement) {
          let probe = el;
          for (let up = 0; up < 2 && probe && probe.parentElement && !lang; up++) {
            const parent = probe.parentElement;
            if (parent.querySelector && parent.querySelector("p, h1, h2, h3, ul, ol, table")) break;
            const sibs = Array.from(parent.children || []);
            const idx = sibs.indexOf(probe);
            for (let si = idx - 1; si >= 0 && !lang; si--) {
              const sib = sibs[si];
              if (!sib || (sib.querySelector && sib.querySelector("pre"))) continue;
              const candLang = cleanCodeHeaderLang(readHeaderNonBtnText(sib));
              if (candLang) {
                lang = candLang;
              }
            }
            probe = parent;
          }
        }
        const code = (srcEl.textContent || "").replace(/\u00a0/g, " ").replace(/\n$/, "");
        lang = inferFenceLang(lang, code);
        const fence = "`".repeat(Math.max(3, maxBacktickRun(code) + 1));
        return `\n${fence}${lang}\n${code}\n${fence}\n\n`;
      }

      if (tag === "code" && !ctx.inPre) {
        const parentCls = el.parentElement ? getClassStr(el.parentElement) : "";
        const isBlockCode =
          cls.includes("CodeContent-") ||
          cls.includes("whitespace-pre") ||
          /\bblock\b/.test(cls) ||
          /InlineCodePane-|chatgpt-code-scrollport|Source-/.test(parentCls) ||
          (!ctx.inline && (el.textContent || "").includes("\n"));
        if (isBlockCode) {
          const code = (el.querySelector && el.querySelector("br")
            ? walk(el, { ...ctx, inPre: true })
            : (el.textContent || "")
          ).replace(/\u00a0/g, " ").replace(/\n+$/, "");
          let bLang = ctx.forcedLang || el.getAttribute("data-language") || "";
          if (!bLang && el.closest) {
            const surface = el.closest("[class*='CodeSurface-'], .md-code-block");
            const hdr = surface
              ? surface.querySelector("[class*='ActionBar-'] .truncate, [class*='ActionBar-'] .flex-1, .md-code-block-banner .d813de27, .md-code-block-banner, [class*='ActionBar-']")
              : null;
            if (hdr) bLang = cleanCodeHeaderLang(readHeaderNonBtnText(hdr));
          }
          bLang = inferFenceLang(bLang, code);
          const fence = "`".repeat(Math.max(3, maxBacktickRun(code) + 1));
          return `\n${fence}${bLang}\n${code}\n${fence}\n\n`;
        }
        const inline = (el.textContent || "").replace(/\s+/g, " ");
        const ticks = "`".repeat(Math.max(1, maxBacktickRun(inline) + 1));
        return `${ticks}${inline}${ticks}`;
      }

      if (tag === "table") {
        const trs = Array.from(el.querySelectorAll("tr")).filter((tr) => !tr.closest || tr.closest("table") === el);
        const rows = [];
        let maxCols = 0;
        for (const tr of trs) {
          const cells = [];
          for (const c of tr.children || []) {
            const cTag = (c.tagName || "").toLowerCase();
            if (cTag === "th" || cTag === "td") {
              cells.push(walk(c, { ...ctx, inline: true }).replace(/\s+/g, " ").replace(/\|/g, "\\|").trim());
            }
          }
          if (cells.length) {
            maxCols = Math.max(maxCols, cells.length);
            rows.push(cells);
          }
        }
        if (!rows.length) return "";
        const norm = (r) => {
          const padded = r.slice();
          while (padded.length < maxCols) padded.push("");
          return `| ${padded.join(" | ")} |`;
        };
        const outLines = [norm(rows[0]), `| ${Array(maxCols).fill("---").join(" | ")} |`];
        for (let rIdx = 1; rIdx < rows.length; rIdx++) {
          outLines.push(norm(rows[rIdx]));
        }
        return `\n${outLines.join("\n")}\n\n`;
      }

      if (tag === "ol" || tag === "ul") {
        const depth = ctx.listDepth || 0;
        let out = depth === 0 ? "\n" : "";
        let idx = 1;
        for (const child of el.children || []) {
          if ((child.tagName || "").toLowerCase() === "li") {
            out += walk(child, { ...ctx, ordered: tag === "ol", index: idx++, listDepth: depth });
          } else {
            out += walk(child, ctx);
          }
        }
        return depth === 0 ? `${out}\n` : out;
      }

      if (tag === "li") {
        const depth = ctx.listDepth || 0;
        const indent = "  ".repeat(depth);
        const prefix = ctx.ordered ? `${ctx.index}. ` : "- ";
        const textParts = [];
        const nestedLists = [];
        for (const child of el.childNodes || []) {
          const cTag = child.nodeType === Node.ELEMENT_NODE ? (child.tagName || "").toLowerCase() : "";
          if (cTag === "ul" || cTag === "ol") {
            const sub = walk(child, { ...ctx, listDepth: depth + 1, ordered: false, index: 0 });
            if (sub.trim()) nestedLists.push(sub.replace(/^\n+|\n+$/g, ""));
          } else {
            textParts.push(walk(child, { ...ctx, listDepth: depth + 1 }));
          }
        }
        const mainText = textParts.join("").replace(/\n{2,}/g, "\n").trim();
        let line = mainText ? `${indent}${prefix}${mainText.replace(/\n/g, `\n${indent}  `)}\n` : `${indent}${prefix}\n`;
        if (nestedLists.length) {
          line += `${nestedLists.join("\n")}\n`;
        }
        return line;
      }

      const dComp = (el.getAttribute("data-d-component") || "").toLowerCase();
      const nextCtx = (tag === "p" || tag === "a" || /^h[1-6]$/.test(tag) || dComp === "title")
        ? { ...ctx, inline: true }
        : ctx;
      let inner = "";
      for (const child of el.childNodes || []) {
        inner += walk(child, tag === "pre" ? { ...ctx, inPre: true } : nextCtx);
      }

      if (ctx.inPre) return inner;
      if (/^h[1-6]$/.test(tag)) {
        const lvl = Number(tag[1]);
        return `\n${"#".repeat(lvl)} ${inner.trim()}\n\n`;
      }
      if (tag === "p") {
        const t = inner.trim();
        return t ? `${t}\n\n` : "";
      }
      if (dComp === "title") {
        const t = inner.trim();
        return t ? `\n### ${t}\n\n` : "";
      }
      if (tag === "strong" || tag === "b") return inner ? `**${inner}**` : "";
      if (tag === "em" || tag === "i") return inner ? `*${inner}*` : "";
      if (tag === "a") {
        const href = el.getAttribute("href") || "";
        const label = inner.trim() || href;
        return href ? `[${label}](${href})` : label;
      }
      if (tag === "blockquote") {
        const lines = inner.trim().split("\n").map(l => `> ${l}`);
        return lines.length ? `\n${lines.join("\n")}\n\n` : "";
      }
      if (tag === "tr") {
        const cells = [];
        for (const c of el.children || []) {
          cells.push(walk(c, { ...ctx, inline: true }).replace(/\s+/g, " ").trim());
        }
        return cells.length ? `| ${cells.join(" | ")} |\n` : "";
      }
      if (!ctx.inline && (tag === "div" || tag === "section" || tag === "article" || tag === "aside" || tag === "header" || tag === "footer")) {
        return inner && !inner.endsWith("\n") ? `${inner}\n` : inner;
      }
      return inner;
    }
    return walk(root).replace(/\n{3,}/g, "\n\n").trim();
  }

  function sanitizeThinkingText(raw) {
    const out = [];
    const seen = new Set();
    const noiseRe = /^(?:running|done|loading(?:\.{3}|…)?|stopped|\d+(?:\.\d+)?s|(?:thought|reasoned|worked)\s+(?:for|about)\s+(?:\d+(?:\.\d+)?\s*(?:s|sec|seconds?)|a\s+(?:couple|few)\s+of\s+seconds)(?:\s*·\s*\d+\s*steps?)?(?:\s+running)?|thought process|thinking(?:\.{3}|…|\s+process)?|deepthink)$/i;
    for (const ln of String(raw || "").split("\n")) {
      const isListItem = /^\s*(?:[-*]|\d+\.)\s+/.test(ln);
      const s = isListItem ? ln.replace(/[ \t]+/g, " ").trimEnd() : ln.replace(/\s+/g, " ").trim();
      const k = s.trim();
      if (!k || noiseRe.test(k)) continue;
      const key = k.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(s);
    }
    return out.join("\n").trim();
  }

  async function attachComposerFile(file, initialEditor, findEditor, provider) {
    if (provider === "DeepSeek") {
      return { ok: true, attached: 0, expected: 1, verified: true, method: "not_supported", error: "" };
    }
    // Provider previews may live in a sibling of the text input, with either
    // an opaque image URL or a filename rendered as plain text.
    const editor = () => findEditor() || initialEditor;
    const root = () => {
      const ed = editor();
      if (!ed) return typeof document !== "undefined" ? (document.body || document) : null;
      let cur = ed;
      const docBody = typeof document !== "undefined" ? document.body : null;
      const docEl = typeof document !== "undefined" ? document.documentElement : null;
      while (cur && cur !== docBody && cur !== docEl) {
        if (
          cur.tagName === "FORM" ||
          cur.tagName === "FIELDSET" ||
          cur.getAttribute?.("data-testid")?.includes?.("composer") ||
          cur.className?.includes?.("composer") ||
          cur.querySelector?.("button[data-testid='send-button'], #composer-submit-button, .send-button, [aria-label*='Send' i], [data-testid*='composer']")
        ) {
          return cur;
        }
        cur = cur.parentElement;
      }
      return ed.closest?.("main, [role='main']") || (typeof document !== "undefined" ? (document.body || document) : null);
    };
    const outsideComposer = "nav, aside, [data-testid='user-message'], [data-testid='assistant-message'], .ds-message, article";
    const previews = () => {
      const r = root();
      if (!r || typeof r.querySelectorAll !== "function") return [];
      return [...r.querySelectorAll(
        "img, canvas, [data-testid*='file'], [data-testid*='attachment'], [data-testid*='image'], " +
        "[class*='attachment-preview'], [class*='file-thumbnail'], [class*='preview-item'], " +
        "[aria-label*='User attachment' i], [aria-label*='screenshot' i], [aria-label$='.png' i], " +
        "button[aria-label*='remove' i], button[aria-label*='delete' i], button[aria-label*='cancel' i]"
      )].filter(el => !el.closest(outsideComposer));
    };
    const countNames = () => {
      const scope = root();
      if (!scope || typeof document === "undefined" || !document.createTreeWalker) return 0;
      const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
      let count = 0;
      while (walker.nextNode()) {
        const n = walker.currentNode;
        if (n.nodeValue && n.nodeValue.includes(file.name) && !n.parentElement?.closest(outsideComposer + ", script, style")) count++;
      }
      return count;
    };
    const before = previews().length;
    const beforeNames = countNames();
    const transfer = new DataTransfer();
    transfer.items.add(file);
    const inputs = typeof document !== "undefined" && document.querySelectorAll
      ? [...document.querySelectorAll("input[type=file]")].filter(input =>
          !input.disabled && !input.webkitdirectory && !input.closest(outsideComposer)
        )
      : [];
    const extension = "." + file.name.split(".").pop().toLowerCase();
    const compatible = input => (input.accept || "").toLowerCase().split(",").map(s => s.trim()).some(
      type => type === file.type || type === "image/*" || type === extension || type === "*/*"
    );
    const input = inputs.find(compatible) || inputs.find(el => !el.accept || !el.accept.trim());
    let method = input ? "file input" : "paste";
    if (input) {
      try {
        input.value = "";
        const desc = typeof HTMLInputElement !== "undefined"
          ? Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "files")
          : null;
        if (desc && typeof desc.set === "function") {
          desc.set.call(input, transfer.files);
        } else {
          input.files = transfer.files;
        }
        input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
        input.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      } catch { /* fall through to paste */ }
    }

    // Also dispatch paste event to editor as fallback
    const target = editor();
    if (target && typeof ClipboardEvent === "function") {
      try {
        target.focus();
        target.dispatchEvent(new ClipboardEvent("paste", {
          clipboardData: transfer, bubbles: true, cancelable: true, composed: true
        }));
      } catch { /* ignore */ }
    }

    const deadline = Date.now() + 20000;
    let readySince = 0;
    while (Date.now() < deadline) {
      const previewSeen = previews().length > before || countNames() > beforeNames;
      const busy = previewSeen && [...(root()?.querySelectorAll?.(
        "[role=progressbar], [aria-busy=true], [data-testid*='uploading'], [class*='upload-progress']"
      ) || [])].some(el => !el.closest(outsideComposer) && isVisible(el));
      if (previewSeen && !busy) {
        if (readySince && Date.now() - readySince >= 350) {
          return { ok: true, attached: 1, expected: 1, verified: true, method };
        }
        readySince ||= Date.now();
      } else {
        readySince = 0;
      }
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    return { ok: false, attached: 0, expected: 1, verified: false, method,
      error: provider + " did not confirm the screenshot after " + method };
  }

  async function attachImagesToEditor(images, editorEl, findLiveEditor = () => editorEl, provider = "") {
    if (!Array.isArray(images) || images.length === 0) {
      return { ok: true, attached: 0, expected: 0, verified: true, method: "none", error: "" };
    }
    if (typeof DataTransfer === "undefined") {
      return { ok: false, attached: 0, expected: images.length, verified: false, error: "Image transfer unavailable" };
    }
    if (images.length > 1) {
      let attached = 0;
      for (const image of images) {
        const result = await attachImagesToEditor([image], findLiveEditor() || editorEl, findLiveEditor, provider);
        if (!result.ok) return { ...result, attached, expected: images.length };
        attached++;
      }
      return { ok: true, attached, expected: images.length, verified: true, method: "sequential" };
    }
    const sleepMs = ms => new Promise(r => setTimeout(r, ms));
    const files = [];
    const names = [];
    for (let i = 0; i < images.length; i++) {
      const item = images[i];
      let mime = "image/png";
      let b64 = "";
      let fname = `screenshot_${i + 1}.png`;
      if (item && typeof item === "object") {
        b64 = String(item.data || "");
        mime = String(item.mime_type || item.mimeType || "image/png");
        fname = String(item.filename || fname);
      } else {
        const dataUrl = String(item || "");
        const m = dataUrl.match(/^data:([^;]+);base64,(.+)$/);
        if (m) {
          mime = m[1] || "image/png";
          b64 = m[2];
          const ext = mime.split("/")[1] || "png";
          fname = `image_${i + 1}.${ext}`;
        }
      }
      if (!b64) continue;
      try {
        const bin = atob(b64);
        const arr = new Uint8Array(bin.length);
        for (let j = 0; j < bin.length; j++) arr[j] = bin.charCodeAt(j);
        files.push(new File([arr], fname, { type: mime }));
        names.push(fname);
      } catch { /* skip malformed image */ }
    }
    if (!files.length) {
      return { ok: false, attached: 0, expected: images.length, verified: false, method: "decode", error: "No valid image bytes to attach" };
    }

    if (provider === "DeepSeek") {
      return { ok: true, attached: 0, expected: images.length, verified: true, method: "not_supported", error: "" };
    }
    if (provider === "Claude") {
      return attachComposerFile(files[0], editorEl, findLiveEditor, provider);
    }

    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);

    const outsideComposer = "nav, aside, [data-testid='user-message'], [data-testid='assistant-message'], .ds-message, article, [data-message-author-role]";
    const currentRoot = () => {
      const editor = findLiveEditor() || editorEl;
      if (!editor) return typeof document !== "undefined" ? (document.body || document) : null;
      let cur = editor;
      let best = null;
      const docBody = typeof document !== "undefined" ? document.body : null;
      const docEl = typeof document !== "undefined" ? document.documentElement : null;
      while (cur && cur !== docBody && cur !== docEl) {
        if (
          cur.tagName === "FORM" ||
          cur.tagName === "FIELDSET" ||
          cur.getAttribute?.("aria-label")?.toLowerCase()?.includes("dropzone") ||
          cur.getAttribute?.("data-slot")?.includes?.("composer") ||
          cur.getAttribute?.("data-testid")?.includes?.("composer") ||
          cur.className?.includes?.("composer") ||
          cur.querySelector?.("button[data-testid='send-button'], #composer-submit-button, .send-button, [aria-label*='Send' i], [aria-label*='Submit chat' i], [data-testid*='composer']")
        ) {
          best = cur;
          break;
        }
        cur = cur.parentElement;
      }
      return best || editor.closest?.("main, [role='main']") || editor.parentElement?.parentElement || (typeof document !== "undefined" ? (document.body || document) : null);
    };

    const root = currentRoot();
    const attachSel =
      'img, canvas, [data-testid*="attachment"], [data-testid*="file"], [data-testid*="image"], ' +
      '[data-testid*="upload"], [class*="thumbnail"], [class*="attachment"], [class*="file-preview"], ' +
      '[class*="file-item"], [class*="preview-item"], [aria-label*="User attachment" i], button[aria-label*="Remove" i], button[aria-label*="Delete" i], ' +
      'button[aria-label*="Cancel" i], [aria-label*="Remove file" i], [aria-label*="Remove image" i], ' +
      '[aria-label*="Delete" i], div[style*="background-image"], div[style*="url("]';
    const spinnerSel =
      'mat-progress-spinner, mat-spinner, mat-progress-bar, ' +
      '[role="progressbar"], [aria-busy="true"], [data-testid*="uploading" i], [data-testid*="loading" i], ' +
      '[class*="uploading" i], [class*="loading" i], [class*="spinner" i], svg[class*="animate-spin" i]';

    const countAttachments = (scope) => {
      try {
        const rootNode = scope || currentRoot() || (typeof document !== "undefined" ? document : null);
        if (!rootNode || typeof rootNode.querySelectorAll !== "function") return 0;
        const els = rootNode.querySelectorAll(attachSel);
        let count = 0;
        for (const el of els) {
          if (typeof el.closest === "function" && el.closest(outsideComposer)) continue;
          if (el.tagName === "IMG") {
            const alt = String(el.getAttribute?.("alt") || "").toLowerCase();
            const src = String(el.getAttribute?.("src") || "").toLowerCase();
            if (alt.includes("avatar") || src.includes("avatar") || src.includes("favicon")) continue;
          }
          count++;
        }
        return count;
      } catch { return 0; }
    };

    const hasUploadSpinner = () => {
      try {
        const rootNode = currentRoot() || (typeof document !== "undefined" ? document : null);
        if (!rootNode || typeof rootNode.querySelectorAll !== "function") return false;
        const els = rootNode.querySelectorAll(spinnerSel);
        for (const el of els) {
          if (typeof el.closest === "function" && el.closest(outsideComposer)) continue;
          if (el && (el.offsetParent !== null || el.getAttribute?.("aria-busy") === "true")) return true;
        }
      } catch { /* ignore */ }
      return false;
    };

    const beforeRoot = countAttachments(root);
    const beforeDoc = countAttachments(typeof document !== "undefined" ? (document.body || document) : null);
    const beforeText = String(root?.textContent || "");

    const hasAttachedSignal = () => {
      const liveRoot = currentRoot();
      if (countAttachments(liveRoot) > beforeRoot) return true;
      if (countAttachments(typeof document !== "undefined" ? (document.body || document) : null) > beforeDoc) return true;
      const txt = String(liveRoot?.textContent || "");
      return names.some(n => n && txt.includes(n) && !beforeText.includes(n));
    };

    const waitForAttachmentOrSpinner = async (polls, stepMs) => {
      for (let i = 0; i < polls; i++) {
        await sleepMs(stepMs);
        if (hasAttachedSignal()) return true;
        if (hasUploadSpinner()) return true;
      }
      return false;
    };

    const tried = [];
    let attachedMethod = "";

    const fileInput =
      (root && root.querySelector && root.querySelector("input[type='file']")) ||
      (typeof document !== "undefined" && document.querySelector ? document.querySelector("input[type='file']") : null);

    if (fileInput && !fileInput.disabled && !(typeof fileInput.closest === "function" && fileInput.closest(outsideComposer))) {
      tried.push("fileInput");
      try {
        const desc = typeof HTMLInputElement !== "undefined"
          ? Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "files")
          : null;
        if (desc && typeof desc.set === "function") {
          desc.set.call(fileInput, dt.files);
        } else {
          fileInput.files = dt.files;
        }
        fileInput.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
        fileInput.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      } catch { /* fall through */ }
      const accepted = await waitForAttachmentOrSpinner(25, 200);
      if (accepted) {
        attachedMethod = "fileInput";
      }
    }

    editorEl = findLiveEditor() || editorEl;
    if (!attachedMethod && editorEl && typeof ClipboardEvent === "function") {
      tried.push("paste");
      try {
        editorEl.focus();
        const ev = new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true, composed: true });
        editorEl.dispatchEvent(ev);
      } catch { /* fall through */ }
      const accepted = await waitForAttachmentOrSpinner(20, 200);
      if (accepted) {
        attachedMethod = "paste";
      }
    }

    if (!attachedMethod && typeof DragEvent === "function" && (editorEl || root)) {
      tried.push("drop");
      const dropTarget = findLiveEditor() || editorEl || root;
      try {
        for (const evType of ["dragenter", "dragover", "drop"]) {
          dropTarget.dispatchEvent(new DragEvent(evType, {
            bubbles: true,
            cancelable: true,
            composed: true,
            dataTransfer: dt,
          }));
        }
      } catch { /* fall through */ }
      const accepted = await waitForAttachmentOrSpinner(15, 200);
      if (accepted) {
        attachedMethod = "drop";
      }
    }

    if (attachedMethod || hasAttachedSignal() || countAttachments(typeof document !== "undefined" ? (document.body || document) : null) > beforeDoc) {
      const deadline = Date.now() + 20000;
      await sleepMs(300);
      while (Date.now() < deadline && hasUploadSpinner()) {
        await sleepMs(200);
      }
      await sleepMs(300);
      if (hasUploadSpinner()) {
        return { ok: false, attached: 0, expected: files.length, verified: false, error: "Upload did not settle" };
      }
      return {
        ok: true,
        attached: files.length,
        expected: files.length,
        verified: true,
        method: attachedMethod || "detected",
        error: "",
      };
    }
    return {
      ok: false,
      attached: 0,
      expected: files.length,
      verified: false,
      method: tried.join(","),
      error: `Could not verify image upload in composer (tried: ${tried.join(", ") || "none"})`,
    };
  }

  function setEditablePromptText(editorEl, promptText) {
    const cleanText = String(promptText || "").replace(/\r\n/g, "\n");
    const tag = (editorEl.tagName || "").toUpperCase();
    if (tag === "TEXTAREA" || tag === "INPUT") {
      editorEl.focus();
      let execDone = false;
      try {
        if (typeof editorEl.select === "function" && typeof document !== "undefined" && typeof document.execCommand === "function") {
          editorEl.select();
          execDone = Boolean(document.execCommand("insertText", false, cleanText));
        }
      } catch { /* ignore */ }
      if (!execDone || String(editorEl.value || "") !== cleanText) {
        const tracker = editorEl._valueTracker;
        const prevSentinel = cleanText === "__wsa_prev__" ? "__wsa_other__" : "__wsa_prev__";
        if (tracker && typeof tracker.setValue === "function") {
          tracker.setValue(prevSentinel);
        }
        const proto = tag === "TEXTAREA"
          ? (typeof HTMLTextAreaElement !== "undefined" ? HTMLTextAreaElement.prototype : null)
          : (typeof HTMLInputElement !== "undefined" ? HTMLInputElement.prototype : null);
        const desc = proto ? Object.getOwnPropertyDescriptor(proto, "value") : null;
        if (desc && typeof desc.set === "function") {
          desc.set.call(editorEl, cleanText);
        } else {
          editorEl.value = cleanText;
        }
      }
      if (String(editorEl.value || "") !== cleanText) {
        editorEl.value = cleanText;
      }
      if (typeof InputEvent === "function") {
        try {
          editorEl.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertText", data: cleanText }));
        } catch { /* ignore */ }
        editorEl.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: cleanText }));
      } else {
        editorEl.dispatchEvent(new Event("input", { bubbles: true }));
      }
      editorEl.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }

    editorEl.focus();
    const toolPills = typeof editorEl.querySelectorAll === "function"
      ? Array.from(editorEl.querySelectorAll(
          "[data-layout='selection-pill'], [data-tone='composer-reference'], [data-ecosystem-mention], span[class*='Mention-']"
        ))
      : [];
    const hasToolPill = toolPills.length > 0;
    const textToInsert = hasToolPill && cleanText ? ` ${cleanText}` : cleanText;
    const selectEditableRange = () => {
      try {
        const sel = typeof window !== "undefined" && window.getSelection ? window.getSelection() : null;
        if (sel && typeof document.createRange === "function") {
          const range = document.createRange();
          if (hasToolPill) {
            const lastPill = toolPills[toolPills.length - 1];
            range.setStartAfter(lastPill);
            range.setEnd(editorEl, editorEl.childNodes.length);
          } else {
            range.selectNodeContents(editorEl);
          }
          sel.removeAllRanges();
          sel.addRange(range);
        }
      } catch { /* ignore */ }
    };
    const isLongText = textToInsert.length > 1200 || textToInsert.split("\n").length > 15;
    selectEditableRange();
    let inserted = false;
    const expNorm = normalizeCompareText(cleanText);
    const probePrefix = expNorm.slice(0, 24).trim();
    const probeTail = expNorm.slice(-24).trim();
    const hasExpectedText = () => {
      const curRaw = String(editorEl.innerText || editorEl.textContent || "").trim();
      const curNorm = normalizeCompareText(curRaw);
      if (!expNorm) return Boolean(curNorm) || cleanText.length === 0;
      if (!curNorm.includes(probePrefix)) return false;
      if (probeTail && !curNorm.includes(probeTail)) return false;
      if (!hasToolPill && expNorm.length > 64 && Math.abs(curNorm.length - expNorm.length) > Math.max(80, Math.floor(expNorm.length * 0.15))) {
        return false;
      }
      return true;
    };
    if (isLongText && typeof ClipboardEvent === "function" && typeof DataTransfer === "function") {
      try {
        const dt = new DataTransfer();
        dt.setData("text/plain", textToInsert);
        editorEl.dispatchEvent(new ClipboardEvent("paste", {
          bubbles: true,
          cancelable: true,
          clipboardData: dt,
        }));
        if (hasExpectedText()) {
          inserted = true;
        }
      } catch { /* ignore */ }
    }
    if (!inserted) {
      try {
        inserted = Boolean(document.execCommand && document.execCommand("insertText", false, textToInsert));
      } catch { /* ignore */ }
    }
    if (!inserted || !hasExpectedText()) {
      try {
        if (typeof ClipboardEvent === "function" && typeof DataTransfer === "function") {
          selectEditableRange();
          const dt = new DataTransfer();
          dt.setData("text/plain", textToInsert);
          editorEl.dispatchEvent(new ClipboardEvent("paste", {
            bubbles: true,
            cancelable: true,
            clipboardData: dt,
          }));
          if (hasExpectedText()) {
            inserted = true;
          }
        }
      } catch { /* ignore */ }
    }
    if (!inserted || !hasExpectedText()) {
      if (hasToolPill) {
        const lastPill = toolPills[toolPills.length - 1];
        const hostBlock = (typeof lastPill.closest === "function" && lastPill.closest("p, div")) || editorEl;
        while (hostBlock.nextSibling && hostBlock.parentNode === editorEl) {
          editorEl.removeChild(hostBlock.nextSibling);
        }
        while (lastPill.nextSibling) {
          hostBlock.removeChild(lastPill.nextSibling);
        }
        hostBlock.appendChild(document.createTextNode(textToInsert));
      } else if (typeof document.createElement === "function" && typeof editorEl.replaceChildren === "function") {
        const lines = cleanText.split("\n");
        const frag = typeof document.createDocumentFragment === "function" ? document.createDocumentFragment() : null;
        if (lines.length > 80) {
          for (let i = 0; i < lines.length; i += 20) {
            const chunk = lines.slice(i, i + 20).join("\n");
            const p = document.createElement("p");
            p.textContent = chunk;
            if (frag) frag.appendChild(p); else editorEl.appendChild(p);
          }
          if (frag) editorEl.replaceChildren(frag);
        } else {
          const pNodes = lines.map((line) => {
            const p = document.createElement("p");
            if (line) p.textContent = line;
            else p.appendChild(document.createElement("br"));
            return p;
          });
          editorEl.replaceChildren(...pNodes);
        }
      } else {
        editorEl.textContent = cleanText;
      }
      if (typeof InputEvent === "function") {
        try {
          editorEl.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertText", data: textToInsert }));
        } catch { /* ignore */ }
        editorEl.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: textToInsert }));
      } else {
        editorEl.dispatchEvent(new Event("input", { bubbles: true }));
      }
      editorEl.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  function placeCaretAtEditorEnd(editorEl) {
    try {
      const sel = typeof window !== "undefined" && window.getSelection ? window.getSelection() : null;
      if (!sel || typeof document.createRange !== "function") return;
      const range = document.createRange();
      const children = editorEl.childNodes;
      if (children && children.length > 0) {
        const last = children[children.length - 1];
        if (last && last.nodeType === 1) {
          range.selectNodeContents(last);
        } else {
          range.selectNodeContents(editorEl);
        }
      } else {
        range.selectNodeContents(editorEl);
      }
      if (typeof range.collapse === "function") range.collapse(false);
      sel.removeAllRanges();
      sel.addRange(range);
    } catch { /* ignore */ }
  }

  function readEditorPromptText(editorEl) {
    if (!editorEl || editorEl.isConnected === false) return "";
    const tag = (editorEl.tagName || "").toUpperCase();
    if (tag === "TEXTAREA" || tag === "INPUT") {
      return String(editorEl.value || "").trim();
    }
    return String(editorEl.innerText || editorEl.textContent || "").trim();
  }

  function isPromptStillInEditor(editorEl, promptText) {
    const clean = normalizeCompareText(promptText);
    if (!clean) return false;
    const cur = normalizeCompareText(readEditorPromptText(editorEl));
    if (!cur) return false;
    const head = clean.slice(0, 24).trim();
    const tail = clean.slice(-24).trim();
    if (head && cur.includes(head)) return true;
    if (tail && cur.length >= tail.length && cur.includes(tail)) return true;
    return false;
  }

  function triggerSendSubmission(rt, editorEl, allowFormSubmitFallback = false) {
    const liveEd = (rt && typeof rt.findEditor === "function" ? rt.findEditor() : null) || editorEl;
    if (!liveEd || liveEd.isConnected === false) return "missing_editor";
    let sendBtn = rt && typeof rt.findSendButton === "function" ? rt.findSendButton(liveEd) : null;
    if (!sendBtn) {
      if (typeof liveEd.focus === "function") {
        try { liveEd.focus(); } catch { /* ignore */ }
      }
      if (typeof Event === "function" && typeof liveEd.dispatchEvent === "function") {
        try { liveEd.dispatchEvent(new Event("input", { bubbles: true })); } catch { /* ignore */ }
      }
      sendBtn = rt && typeof rt.findSendButton === "function" ? rt.findSendButton(liveEd) : null;
    }
    if (sendBtn) {
      if (typeof sendBtn.scrollIntoView === "function") {
        try { sendBtn.scrollIntoView({ block: "nearest" }); } catch { /* ignore */ }
      }
      if (typeof Event === "function" && typeof sendBtn.dispatchEvent === "function") {
        for (const evType of ["pointerdown", "mousedown", "pointerup", "mouseup"]) {
          try {
            const Ctor = evType.startsWith("pointer") && typeof PointerEvent === "function"
              ? PointerEvent
              : (typeof MouseEvent === "function" ? MouseEvent : Event);
            sendBtn.dispatchEvent(new Ctor(evType, { bubbles: true, cancelable: true, composed: true }));
          } catch { /* ignore */ }
        }
      }
      try { sendBtn.click(); } catch { /* ignore */ }
      return "button";
    }
    if (typeof liveEd.focus === "function") {
      try { liveEd.focus(); } catch { /* ignore */ }
    }
    placeCaretAtEditorEnd(liveEd);
    if (allowFormSubmitFallback && typeof liveEd.closest === "function") {
      const form = liveEd.closest("form");
      if (form && typeof form.requestSubmit === "function") {
        try {
          form.requestSubmit();
          return "requestSubmit";
        } catch { /* fall through to Enter */ }
      }
    }
    if (typeof KeyboardEvent === "function" && typeof liveEd.dispatchEvent === "function") {
      for (const kEv of ["keydown", "keyup"]) {
        liveEd.dispatchEvent(new KeyboardEvent(kEv, {
          key: "Enter",
          code: "Enter",
          keyCode: 13,
          which: 13,
          bubbles: true,
          cancelable: true,
          composed: true,
        }));
      }
    }
    return "enter";
  }

  function detectPromptTooLongMessage(text, isBanner = false) {
    const raw = String(text || "").trim();
    if (!raw) return "";
    const re = /(?:the\s+message\s+you\s+submitted\s+was\s+too\s+long|message\s+(?:you\s+submitted\s+)?(?:is|was)\s+too\s+long|please\s+edit\s+it\s+and\s+resubmit|prompt\s+(?:is\s+)?too\s+long|input\s+(?:is\s+)?too\s+long|exceeds?\s+(?:the\s+)?max(?:imum)?\s+(?:message\s+)?length|character\s+limit\s+exceeded|too\s+many\s+characters|maximum\s+context\s+(?:length|limit|window)\s+exceeded|context\s+(?:window|limit)\s+exceeded)/i;
    if (isBanner) {
      const parts = raw.split(/\s*\|\s*|\r?\n+|(?<=[.!?])\s+/).map(p => p.replace(/\s+/g, " ").trim()).filter(Boolean);
      for (const seg of parts) {
        if (seg.length <= 460 && re.test(seg)) return seg;
      }
      const sb = raw.replace(/\s+/g, " ").trim();
      if (sb.length <= 1500 && re.test(sb)) return sb.slice(0, 320);
      return "";
    }
    const s = raw.replace(/\s+/g, " ").trim();
    return (s && s.length <= 460 && re.test(s)) ? s : "";
  }

  function detectLimitMessage(text, isBanner = false) {
    if (detectPromptTooLongMessage(text, isBanner)) return "";
    const raw = String(text || "").trim();
    if (!raw) return "";
    const softPctRe = /\b(?:over\s+|nearly\s+|almost\s+|approaching\s+|used\s+)?(?:[1-9]?\d)\s*%\s+of\s+(?:your\s+)?(?:\S+\s+){0,3}(?:limit|limi|quota|usage)\b/i;
    const hardBlockRe = /\b(?:reached|exceeded|hit|exhausted|will\s+reset|resets?\s+(?:at|in|after)|0\s+messages|out\s+of\s+(?:free\s+)?messages|upgrade\s+to\s+pro\s+to\s+(?:continue|chat|send)|cannot\s+send|try\s+again\s+(?:later|at|in)|until\s+\d)\b/i;
    const isSoftOnly = (s) => softPctRe.test(s) && !hardBlockRe.test(s);
    const re = /(?:you(?:'|’)?ve|you\s+have)\s+(?:reached|hit|exceeded)(?:\s+\S+){0,6}\s+(?:cap|limit)|(?:rate|usage|message|hourly|daily(?:\s+(?:chat|message|free))?|weekly|monthly|\d+\s*[- ]?\s*hour|plan|free|session|reasoning|thinking|deepthink|search|credit)\s+limit\b(?:\s+(?:has\s+been\s+)?(?:reached|exceeded|hit|will\s+reset|resets))?|\blimit\s+(?:has\s+been\s+)?(?:reached|exceeded|resets|will\s+reset)|(?:out\s+of\s+(?:free\s+)?messages|(?:0|zero|no)\s+messages\s+(?:left|remaining)|(?:out\s+of|insufficient|no)\s+(?:venice\s+)?credits|upgrade\s+to\s+pro\s+to\s+(?:continue|chat|send)|too\s+many\s+(?:concurrent\s+)?requests|resource[_\s-]*exhausted|quota\s+exceeded|(?:server|service|system|model)\s+is\s+(?:currently\s+)?busy|high\s+(?:demand|traffic)|http\s*(?:error\s*)?429\b|responses\s+will\s+use\s+(?:another|a\s+different|the\s+mini|gemini\s+auto|auto|gemini\s+flash-lite|flash-lite|flash\s+lite|gemini\s+flash|flash)\b|\bswitched\s+to\s+(?:gemini\s+)?(?:auto|flash-lite|flash\s+lite|flash)\b|\busing\s+(?:gemini\s+)?(?:auto|flash-lite|flash\s+lite|flash)\s+(?:until|because|due\s+to|after)\b|\b(?:you(?:'|’)?ve|you\s+have)\s+reached\s+(?:your\s+|the\s+)?(?:gemini\s+)?(?:pro|advanced)?\s*limit\b|\b(?:gemini\s+)?(?:pro|advanced)\s+(?:limit|quota)\s+(?:reached|exceeded|hit)\b|\breached\s+(?:your|the)?\s*(?:daily|hourly|usage|message)?\s*limit\s+for\s+gemini\b)/i;
    if (isBanner) {
      const parts = raw.split(/\s*\|\s*|\r?\n+|(?<=[.!?])\s+/).map(p => p.replace(/\s+/g, " ").trim()).filter(Boolean);
      for (const seg of parts) {
        if (seg.length <= 460 && !isSoftOnly(seg) && re.test(seg)) return seg;
      }
      const sb = raw.replace(/\s+/g, " ").trim();
      if (sb.length <= 1500 && !isSoftOnly(sb) && re.test(sb)) return sb.slice(0, 320);
      return "";
    }
    const s = raw.replace(/\s+/g, " ").trim();
    return (s && s.length <= 460 && !isSoftOnly(s) && re.test(s)) ? s : "";
  }

  async function startTurn(rt, reqId, prompt, images = [], opts = {}) {
    const sleepMs = ms => new Promise(r => setTimeout(r, ms));
    const provLabel = opts.label || opts.defaultModel || "Web AI";
    let editor = rt.findEditor();
    for (let i = 0; !editor && i < 80; i++) {
      if (i >= 6 && typeof rt.readErrorBanner === "function") {
        const earlyBanner = rt.readErrorBanner();
        const earlyTooLong = detectPromptTooLongMessage(earlyBanner, true);
        if (earlyTooLong) {
          return { ok: false, error: `[PROMPT_TOO_LONG] ${provLabel}: ${earlyTooLong}` };
        }
        const earlyLimit = detectLimitMessage(earlyBanner, true);
        if (earlyLimit) {
          return { ok: false, error: `[LIMIT_REACHED] ${provLabel}: ${earlyLimit}` };
        }
      }
      await sleepMs(250);
      editor = rt.findEditor();
    }
    if (!editor) {
      const missingBanner = typeof rt.readErrorBanner === "function" ? rt.readErrorBanner() : "";
      const missingTooLong = detectPromptTooLongMessage(missingBanner, true);
      if (missingTooLong) {
        return { ok: false, error: `[PROMPT_TOO_LONG] ${provLabel}: ${missingTooLong}` };
      }
      const missingLimit = detectLimitMessage(missingBanner, true);
      if (missingLimit) {
        return { ok: false, error: `[LIMIT_REACHED] ${provLabel}: ${missingLimit}` };
      }
      return {
        ok: false,
        error: opts.editorError || "AI prompt editor not found after 20s.",
      };
    }

    const monoNow = () => (typeof performance !== "undefined" && typeof performance.now === "function") ? performance.now() : Date.now();
    const tTurnStart = monoNow();
    let imagesAttached = null;
    let uploadMs = null;

    if (Array.isArray(images) && images.length > 0 && attachImagesToEditor) {
      const tAttachStart = monoNow();
      const attachRes = await attachImagesToEditor(images, editor, rt.findEditor, provLabel);
      if (attachRes && attachRes.verified === false) {
        const errBanner = typeof rt.readErrorBanner === "function" ? rt.readErrorBanner() : "";
        const limitHit = typeof rt.detectLimitMessage === "function" ? rt.detectLimitMessage(errBanner, true) : "";
        if (limitHit) {
          return { ok: false, error: `[LIMIT_REACHED] ${provLabel}: ${limitHit}` };
        }
        return {
          ok: false,
          error: `[IMAGE_ATTACH_FAILED] ${provLabel}: ${attachRes.error || "Image upload could not be verified before sending."}`,
        };
      }
      imagesAttached = Boolean(attachRes && attachRes.verified === true && (attachRes.attached > 0 || attachRes.method !== "not_supported") && attachRes.method !== "not_supported");
      if (imagesAttached) {
        uploadMs = Math.max(0, Math.round(monoNow() - tAttachStart));
      }
      editor = rt.findEditor() || editor;
    }

    const beforeTurns = rt.readAllTurns();
    const beforeLast = beforeTurns.length ? beforeTurns[beforeTurns.length - 1] : { text: "", thinking: "" };
    const beforeBanner = typeof rt.readErrorBanner === "function" ? rt.readErrorBanner() : "";
    const modelBadge = rt.readModelBadge();

    setEditablePromptText(editor, prompt);
    await sleepMs(160);

    const maxBtnWait = imagesAttached ? 40 : 20;
    let sendBtn = rt.findSendButton(rt.findEditor() || editor);
    for (let i = 0; !sendBtn && i < maxBtnWait; i++) {
      if (i === 8 || (imagesAttached && i === 24)) {
        const liveEd = rt.findEditor() || editor;
        if (liveEd) setEditablePromptText(liveEd, prompt);
      } else if ((i === 4 || i === 12 || i === 20) && typeof Event === "function") {
        const liveEd = rt.findEditor() || editor;
        if (liveEd && typeof liveEd.dispatchEvent === "function") {
          try { liveEd.dispatchEvent(new Event("input", { bubbles: true, composed: true })); } catch { /* ignore */ }
          try { liveEd.dispatchEvent(new Event("change", { bubbles: true, composed: true })); } catch { /* ignore */ }
        }
      }
      await sleepMs(150);
      sendBtn = rt.findSendButton(rt.findEditor() || editor);
    }

    const hasSubmitEffect = () => {
      if (rt.hasStopButton()) return true;
      if (rt.readAllTurns().length > beforeTurns.length) return true;
      const liveEd = rt.findEditor() || editor;
      if (!isPromptStillInEditor(liveEd, prompt)) return true;
      if (typeof rt.readErrorBanner === "function" && rt.readErrorBanner() !== beforeBanner) return true;
      return false;
    };

    triggerSendSubmission(rt, editor, false);
    await sleepMs(180);
    if (!hasSubmitEffect()) {
      await sleepMs(260);
      if (!hasSubmitEffect()) {
        triggerSendSubmission(rt, editor, true);
        await sleepMs(180);
      }
    }

    const now = Date.now();
    const afterEd = rt.findEditor() || editor;
    rt.turns[reqId] = {
      startedAt: now,
      lastChangeAt: now,
      lastRetryAt: now,
      lastText: "",
      lastThinking: "",
      prompt: String(prompt || ""),
      submitRetries: 0,
      beforeTurnCount: beforeTurns.length,
      beforeTurnKeys: beforeTurns.map(item => item.turnKey).filter(Boolean),
      beforeLastText: beforeLast.text || "",
      beforeLastThinking: beforeLast.thinking || "",
      beforeBanner,
      sawActivity: false,
      sawStop: false,
      sawEditorCleared: Boolean(!afterEd || !isPromptStillInEditor(afterEd, prompt)),
      modelBadge,
    };
    return {
      ok: true,
      model: modelBadge || opts.defaultModel || "Web AI",
      images_attached: imagesAttached,
      upload_ms: uploadMs,
      submit_ms: Math.max(0, Math.round(monoNow() - tTurnStart)),
    };
  }

  function autoDismissWaitPrompts() {
    try {
      const candidates = document.querySelectorAll(
        "button, [role='button'], .mat-mdc-button-base, [role='dialog'] button, mat-dialog-container button, .modal button"
      );
      for (const b of candidates) {
        const txt = (b.innerText || b.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
        if (txt === "wait" || txt === "keep waiting" || txt === "wait for page" || txt === "continue waiting") {
          b.click();
          break;
        }
      }
    } catch { /* ignore */ }
  }

  function pollTurn(rt, reqId, opts = {}) {
    autoDismissWaitPrompts();
    let turn = rt.turns[reqId];
    if (!turn) {
      if (typeof rt.hasStopButton === "function" && rt.hasStopButton()) {
        const nowRecover = Date.now();
        turn = rt.turns[reqId] = {
          startedAt: nowRecover,
          lastChangeAt: nowRecover,
          lastRetryAt: nowRecover,
          lastText: "",
          lastThinking: "",
          prompt: "",
          submitRetries: 0,
          beforeTurnCount: 0,
          beforeTurnKeys: [],
          beforeLastText: "",
          beforeLastThinking: "",
          beforeBanner: "",
          sawActivity: true,
          sawStop: true,
          sawEditorCleared: true,
          modelBadge: typeof rt.readModelBadge === "function" ? rt.readModelBadge() : "",
        };
      } else {
        return { missing: true };
      }
    }

    const label = opts.label || "Web AI";
    const defaultModel = opts.defaultModel || label;
    const now = Date.now();
    const pageUrl = typeof window !== "undefined" && window.location ? window.location.href : (typeof location !== "undefined" ? location.href : "");
    const allTurns = rt.readAllTurns();
    const stopVisible = rt.hasStopButton();
    const liveEd = typeof rt.findEditor === "function" ? rt.findEditor() : null;
    if (stopVisible || !liveEd || !isPromptStillInEditor(liveEd, turn.prompt)) {
      turn.sawEditorCleared = true;
    }
    if (stopVisible) {
      turn.sawStop = true;
      turn.sawActivity = true;
    }

    let latest = null;
    if (allTurns.length > turn.beforeTurnCount) {
      const candidate = allTurns[allTurns.length - 1];
      if (
        !turn.sawActivity &&
        !turn.sawStop &&
        !stopVisible &&
        !turn.sawEditorCleared &&
        candidate &&
        !candidate.isStreaming &&
        candidate.hasActionBar &&
        liveEd &&
        isPromptStillInEditor(liveEd, turn.prompt)
      ) {
        // Rebase hydrated historical turn that finished mounting while our prompt still sits unsent in composer.
        turn.beforeTurnCount = allTurns.length;
        turn.beforeTurnKeys = allTurns.map(item => item.turnKey).filter(Boolean);
        turn.beforeLastText = candidate.text || "";
        turn.beforeLastThinking = candidate.thinking || "";
      } else {
        latest = candidate;
        turn.sawActivity = true;
      }
    } else if (allTurns.length > 0 && (
      turn.sawStop || (turn.sawEditorCleared &&
        allTurns[allTurns.length - 1].turnKey &&
        Array.isArray(turn.beforeTurnKeys) && turn.beforeTurnKeys.length > 0 &&
        !turn.beforeTurnKeys.includes(allTurns[allTurns.length - 1].turnKey))
    )) {
      const candidate = allTurns[allTurns.length - 1];
      if (candidate.text !== turn.beforeLastText || candidate.thinking !== turn.beforeLastThinking || candidate.isStreaming ||
          (candidate.turnKey && Array.isArray(turn.beforeTurnKeys) && !turn.beforeTurnKeys.includes(candidate.turnKey))) {
        latest = candidate;
        turn.sawActivity = true;
      }
    }

    const isStillStreaming = Boolean(stopVisible || latest?.isStreaming);
    const curText = latest?.text || "";
    const curThinking = latest?.thinking || "";
    if (curText && curText !== turn.lastText) {
      turn.lastText = curText;
      turn.lastChangeAt = now;
      turn.sawActivity = true;
    }
    if (
      curThinking &&
      curThinking !== turn.lastThinking &&
      (curThinking.length >= turn.lastThinking.length || curThinking.split("\n").length >= turn.lastThinking.split("\n").length)
    ) {
      turn.lastThinking = curThinking;
      turn.lastChangeAt = now;
      turn.sawActivity = true;
    }

    const quietMs = now - turn.lastChangeAt;
    if (!isStillStreaming) {
      const pageBannerText = typeof rt.readErrorBanner === "function" ? rt.readErrorBanner() : "";
      const turnTooLong = curText ? detectPromptTooLongMessage(curText) : "";
      const pageTooLong = pageBannerText ? detectPromptTooLongMessage(pageBannerText, true) : "";
      const matchedTooLong = turnTooLong || (
        pageTooLong &&
        (!turn.lastText || turn.lastText === curText) &&
        (pageBannerText !== (turn.beforeBanner || "") || (now - turn.startedAt) >= 1200)
          ? pageTooLong
          : ""
      );
      if (matchedTooLong && !turn.lastText.includes("```")) {
        delete rt.turns[reqId];
        return {
          done: false,
          error: `[PROMPT_TOO_LONG] ${label}: ${matchedTooLong}`,
          text: "",
          thinking: "",
          model: rt.readModelBadge() || turn.modelBadge || defaultModel,
          url: pageUrl,
        };
      }
      const turnLimitErr = curText ? detectLimitMessage(curText) : "";
      const pageBannerErr = pageBannerText ? detectLimitMessage(pageBannerText, true) : "";
      const matchedLimit = turnLimitErr || (!turn.lastText ? pageBannerErr : (turn.lastText === curText ? pageBannerErr : ""));
      if (matchedLimit && !turn.lastText.includes("```")) {
        delete rt.turns[reqId];
        return {
          done: false,
          error: `[LIMIT_REACHED] ${label}: ${matchedLimit}`,
          text: "",
          thinking: "",
          model: rt.readModelBadge() || turn.modelBadge || defaultModel,
          url: pageUrl,
        };
      }
    }

    const isPromptInEd = Boolean(turn.prompt && liveEd && isPromptStillInEditor(liveEd, turn.prompt));
    const promptUnsent = Boolean(
      !turn.sawActivity &&
      !isStillStreaming &&
      allTurns.length <= turn.beforeTurnCount &&
      isPromptInEd
    );
    if (promptUnsent && (turn.submitRetries || 0) < 4 && (now - (turn.lastRetryAt || turn.startedAt)) >= 1800) {
      const sendMissing = typeof rt.findSendButton === "function" && !rt.findSendButton(liveEd);
      if (sendMissing && isPromptInEd && (turn.submitRetries || 0) < 2) {
        setEditablePromptText(liveEd, turn.prompt);
      }
      triggerSendSubmission(rt, liveEd, (turn.submitRetries || 0) >= 1);
      turn.submitRetries = (turn.submitRetries || 0) + 1;
      turn.lastRetryAt = now;
    }

    const stoppedByUser = Boolean(
      !turn.lastText &&
      !isStillStreaming &&
      (latest?.stoppedByUser || (opts.stoppedRe && opts.stoppedRe.test(`${curThinking}\n${curText}\n${turn.lastThinking}`)))
    );
    const emptyStalledAfterActivity = Boolean(turn.sawActivity && !turn.lastText && !isStillStreaming && quietMs >= 10000);
    const stuckUnsentStall = Boolean(promptUnsent && (turn.submitRetries || 0) >= 3 && (now - turn.startedAt) >= 14000);
    const neverStartedStall = Boolean(!turn.sawActivity && !isStillStreaming && (now - turn.startedAt) >= 45000);
    if (stoppedByUser || emptyStalledAfterActivity || stuckUnsentStall || neverStartedStall) {
      const fallbackBanner = typeof rt.readErrorBanner === "function" ? rt.readErrorBanner() : "";
      const tooLongMsg = detectPromptTooLongMessage(fallbackBanner, true);
      const limitMsg = tooLongMsg ? "" : detectLimitMessage(fallbackBanner, true);
      delete rt.turns[reqId];
      return {
        done: false,
        error: tooLongMsg
          ? `[PROMPT_TOO_LONG] ${label}: ${tooLongMsg}`
          : (limitMsg
            ? `[LIMIT_REACHED] ${label}: ${limitMsg}`
            : (stoppedByUser
              ? `${label} response was stopped in browser`
              : (stuckUnsentStall
                ? `${label} prompt remained unsubmitted in composer (Send button/form did not trigger response)`
                : `${label} tab did not start or produce a text response`))),
        text: turn.lastText,
        thinking: stoppedByUser ? "" : turn.lastThinking,
        model: rt.readModelBadge() || turn.modelBadge || defaultModel,
        url: pageUrl,
      };
    }

    const done = Boolean(
      turn.sawActivity &&
      turn.lastText &&
      !isStillStreaming &&
      ((latest?.hasActionBar && quietMs >= 450) || quietMs >= 1400)
    );
    if (done) {
      delete rt.turns[reqId];
    }

    return {
      done,
      text: turn.lastText,
      thinking: turn.lastThinking,
      streaming: isStillStreaming,
      model: rt.readModelBadge() || turn.modelBadge || defaultModel,
      url: pageUrl,
    };
  }

  globalThis.__wsaDomUtils = {
    version: 3,
    rev: 10,
    getClassStr,
    isVisible,
    normalizeCompareText,
    nodeToMarkdown,
    sanitizeThinkingText,
    attachImagesToEditor,
    setEditablePromptText,
    readEditorPromptText,
    isPromptStillInEditor,
    triggerSendSubmission,
    detectPromptTooLongMessage,
    detectLimitMessage,
    startTurn,
    pollTurn,
  };
  return true;
}

function _chatgptInstallPageRuntimeFn() {
  if (globalThis.__wsaChatgptRuntime && globalThis.__wsaChatgptRuntime.version === 3 && globalThis.__wsaChatgptRuntime.rev === 8) {
    return true;
  }
  const u = globalThis.__wsaDomUtils;
  if (!u || u.version !== 3) return false;
  const { getClassStr, isVisible, nodeToMarkdown, sanitizeThinkingText } = u;

  function findEditor() {
    const selectors = [
      "div.ProseMirror[contenteditable='true']#prompt-textarea",
      "#prompt-textarea[contenteditable='true']",
      "div.ProseMirror[contenteditable='true']",
      "[aria-label*='Ask ChatGPT' i][contenteditable='true']",
      "[contenteditable='true'][role='textbox']",
      "#prompt-textarea",
    ];
    for (const sel of selectors) {
      for (const el of document.querySelectorAll(sel)) {
        if (!isVisible(el) || el.disabled) continue;
        const tag = (el.tagName || "").toUpperCase();
        if (tag !== "TEXTAREA" && tag !== "INPUT") {
          if (el.isContentEditable === false || el.getAttribute?.("contenteditable") === "false") continue;
        }
        return el;
      }
    }
    return null;
  }

  function findSendButton(editorEl) {
    const scope = editorEl && typeof editorEl.closest === "function"
      ? editorEl.closest("form, [class*='composer' i], [data-testid*='composer' i]")
      : null;
    const roots = scope ? [scope, document] : [document];
    const selectors = [
      "button[data-testid='send-button']",
      "[role='button'][data-testid='send-button']",
      "[data-testid='send-button']",
      "#composer-submit-button",
      "button#composer-submit-button",
      "button[aria-label='Send']",
      "button[aria-label*='Send prompt' i]",
      "button[aria-label*='Send message' i]",
      "button[aria-label*='Send' i]",
      "[role='button'][aria-label*='Send' i]",
      "button[type='submit']",
      "button.bg-composer-primary",
      "form button.bg-composer-primary",
    ];
    for (let rIdx = 0; rIdx < roots.length; rIdx++) {
      const root = roots[rIdx];
      for (const sel of selectors) {
        if ((sel === "button[type='submit']" || sel === "button.bg-composer-primary") && (!scope || rIdx > 0)) continue;
        for (const b of root.querySelectorAll(sel)) {
          if (!isVisible(b) || b.disabled || b.getAttribute("aria-disabled") === "true") continue;
          const meta = `${b.getAttribute("data-testid") || ""} ${b.id || ""} ${b.getAttribute("aria-label") || ""} ${b.getAttribute("title") || ""} ${b.innerText || ""}`.toLowerCase();
          const testId = (b.getAttribute("data-testid") || "").toLowerCase();
          const isExplicit = testId === "send-button" || b.id === "composer-submit-button" || meta.includes("send message") || meta.includes("send prompt") || meta.trim() === "send";
          if (isExplicit) {
            if (/\b(?:voice|dictate|stop|speech|audio|talk|mic|microphone)\b/i.test(meta)) continue;
            return b;
          }
          if (/\b(?:voice|dictate|stop|speech|audio|talk|mic|microphone|attach|upload|add files?|think|reason|search|model|menu|more|feedback|regenerate|retry|expand|collapse|full\s*screen)\b/i.test(meta)) continue;
          return b;
        }
      }
    }
    return null;
  }

  function hasStopButton() {
    for (const b of document.querySelectorAll("button[data-testid='stop-button'], button[aria-label='Stop'], button[aria-label*='Stop' i]")) {
      if (isVisible(b) && !b.disabled) return true;
    }
    for (const sh of document.querySelectorAll("[role='region'][aria-label='Conversation'] [class*='cadencedShimmer-'], [data-content-search-turn-key] [class*='cadencedShimmer-']")) {
      if (isVisible(sh)) return true;
    }
    return Boolean(document.querySelector("[data-is-streaming='true'], .result-streaming"));
  }

  function readModelBadge() {
    let badge = "";
    for (const sel of [
      "[data-testid='model-switcher-dropdown-button']",
      "button[aria-label*='Model selector' i]",
      "button[data-testid*='model-switcher']",
    ]) {
      const btn = document.querySelector(sel);
      if (!btn || !isVisible(btn)) continue;
      const txt = (btn.innerText || btn.textContent || btn.getAttribute("aria-label") || "")
        .replace(/^Model selector,?\s*current model is\s*/i, "")
        .replace(/\s+/g, " ")
        .trim();
      if (txt && txt.length <= 60) {
        badge = txt;
        break;
      }
    }
    if (!badge) {
      for (const b of document.querySelectorAll("header button, main header button, [role='banner'] button")) {
        if (!isVisible(b)) continue;
        const t = (b.innerText || b.textContent || "").replace(/\s+/g, " ").trim();
        if (t && t.length <= 48 && /\b(?:ChatGPT|GPT-[456]|o[134]|Instant|Thinking)\b/i.test(t)) {
          badge = t;
          break;
        }
      }
    }
    if (!badge) {
      const profileBtn = document.querySelector("button[aria-label*='Open profile menu' i], [data-testid='accounts-profile-button']");
      const pTxt = (profileBtn?.innerText || profileBtn?.textContent || "").replace(/\s+/g, " ").trim();
      const tierMatch = pTxt.match(/\b(Go|Plus|Pro|Team|Enterprise|Free)\b/i);
      badge = tierMatch ? `ChatGPT (${tierMatch[1]})` : "ChatGPT";
    }
    let thinkActive = Boolean(
      document.querySelector(
        "button[aria-label*='Thinking Selected' i], [aria-label='Thinking Selected'], button[aria-pressed='true'][aria-label*='Think' i]"
      )
    );
    if (!thinkActive) {
      for (const b of document.querySelectorAll("form button, button")) {
        if (!isVisible(b)) continue;
        const lbl = (b.getAttribute("aria-label") || b.innerText || b.textContent || "").replace(/\s+/g, " ").trim();
        if (!/^Think(?:ing)?$/i.test(lbl)) continue;
        const pressed = b.getAttribute("aria-pressed") === "true" || b.getAttribute("data-state") === "checked";
        const cls = getClassStr(b);
        if (pressed || /accentSubtle|accent/i.test(cls)) {
          thinkActive = true;
          break;
        }
      }
    }
    let webSearchActive = false;
    const ed = findEditor();
    if (ed) {
      for (const pill of ed.querySelectorAll("[data-layout='selection-pill'], [data-tone='composer-reference'], [data-ecosystem-mention], span[class*='Mention-'], span[contenteditable='false']")) {
        const pTxt = (pill.innerText || pill.textContent || "").replace(/\s+/g, " ").trim();
        if (/\bWeb\s*search\b/i.test(pTxt)) {
          webSearchActive = true;
          break;
        }
      }
    }
    if (thinkActive && badge && !/\bthink(?:ing)?\b/i.test(badge)) {
      badge = `${badge} · Think`;
    } else if (webSearchActive && badge && !/\bweb\s*search\b/i.test(badge)) {
      badge = `${badge} · Web search`;
    }
    return badge;
  }

  function isThinkingNode(el) {
    if (!el || el.nodeType !== 1) return false;
    const tid = (el.getAttribute("data-testid") || "").toLowerCase();
    const cls = getClassStr(el).toLowerCase();
    if (cls.includes("cadencedshimmer-")) return true;
    if (tid.includes("thought") || tid.includes("reasoning") || cls.includes("thought") || cls.includes("reasoning")) {
      if (!el.querySelector("[class*='DilResponseRoot-'], [class*='MarkdownRoot-']")) {
        return true;
      }
    }
    if (el.matches("details, section, div") && !el.querySelector("[class*='DilResponseRoot-'], [class*='MarkdownRoot-'], .turn-action-controls")) {
      const btn = el.querySelector(":scope > button, :scope > summary, :scope > div > button");
      if (btn) {
        const lbl = `${btn.getAttribute("aria-label") || ""} ${btn.textContent || ""}`.replace(/\s+/g, " ").trim().toLowerCase();
        if (/^(?:thought\s+for|worked\s+for|reasoned\s+(?:for|about)|thinking(?:\.{3})?$|analyzed|searching\s+the\s+web)/.test(lbl)) {
          return true;
        }
      }
    }
    return false;
  }

  function isNonContentChromeNode(el) {
    if (isThinkingNode(el)) return true;
    const cls = getClassStr(el);
    const clsLow = cls.toLowerCase();
    if (clsLow.includes("turn-action-controls") || clsLow.split(/\s+/).includes("sr-only")) return true;
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (role === "status" || role === "toolbar" || el.getAttribute("aria-live")) return true;
    if ((el.getAttribute("aria-hidden") || "").toLowerCase() === "true") return true;
    if (el.getAttribute("data-d-inline-text") !== null && (el.getAttribute("aria-haspopup") === "dialog" || /Trigger-/i.test(cls))) {
      return true;
    }
    if (/Mention-/i.test(cls) || el.getAttribute("data-layout") === "selection-pill") {
      return true;
    }
    return false;
  }

  function hasHeadingMatch(el, re) {
    for (const h of el.querySelectorAll("h1, h2, h3, h4, h5, h6, .sr-only")) {
      if (re.test(h.textContent || "")) return true;
    }
    return false;
  }

  function isUserTurnNode(el) {
    if (!el || !(el instanceof Element)) return false;
    if (el.getAttribute("data-message-author-role") === "user") return true;
    if (hasHeadingMatch(el, /^\s*you\s+said\s*:?\s*$/i)) return true;
    if (el.matches && el.matches(".bg-user-message, [class*='user-message']")) return true;
    const hasUserBubble = Boolean(
      el.querySelector("[data-message-author-role='user'], .bg-user-message, button[aria-label='Edit message']")
    );
    const hasAsstRenderer = Boolean(
      el.querySelector(
        "[data-message-author-role='assistant'], [class*='DilResponseRoot-'], [class*='DilRenderer-'], [class*='actionRow-'].turn-action-controls, button[aria-label='Good response'], button[aria-label='Rate response']"
      ) || hasHeadingMatch(el, /chatgpt\s+said/i)
    );
    return hasUserBubble && !hasAsstRenderer;
  }

  function readTurnContainers() {
    const roleExplicit = Array.from(document.querySelectorAll("[data-message-author-role='assistant']")).filter((el) => !isUserTurnNode(el));
    if (roleExplicit.length) {
      return roleExplicit.filter((el) => !roleExplicit.some(other => other !== el && typeof other.contains === "function" && other.contains(el)));
    }
    const candidates = Array.from(document.querySelectorAll(
      "[data-content-search-turn-key], article[data-testid^='conversation-turn-']"
    )).filter((el) => {
      if (isUserTurnNode(el)) return false;
      const hasAsst =
        hasHeadingMatch(el, /chatgpt\s+said/i) ||
        el.getAttribute("data-message-author-role") === "assistant" ||
        Boolean(el.querySelector("[data-message-author-role='assistant'], [class*='DilResponseRoot-'], [class*='DilRenderer-'], [class*='actionRow-'].turn-action-controls, button[aria-label='Good response'], button[aria-label='Rate response']"));
      if (hasAsst) return true;
      return Boolean(el.querySelector("[data-testid*='thought' i], [data-testid*='reasoning' i], [class*='cadencedShimmer-']"));
    });
    if (candidates.length) {
      return candidates.filter((el) => !candidates.some(other => other !== el && typeof other.contains === "function" && other.contains(el)));
    }
    const srTurns = [];
    for (const h of document.querySelectorAll("h4.sr-only, h4")) {
      if (!/chatgpt\s+said/i.test(h.textContent || "")) continue;
      const box = h.closest("[data-content-search-turn-key], article, section, div");
      if (box && !isUserTurnNode(box) && !srTurns.includes(box)) srTurns.push(box);
    }
    return srTurns;
  }

  function extractTurnContent(turnEl) {
    if (!turnEl || isUserTurnNode(turnEl)) return { text: "", thinking: "", isStreaming: false, hasActionBar: false, stoppedByUser: false };
    const isStreaming =
      turnEl.getAttribute("data-is-streaming") === "true" ||
      Boolean(turnEl.querySelector("[data-is-streaming='true'], .result-streaming, [class*='cadencedShimmer-']"));

    const thinkNodes = [];
    for (const node of turnEl.querySelectorAll(
      "[data-testid*='thought' i], [data-testid*='reasoning' i], [class*='thought' i], [class*='reasoning' i], [class*='cadencedShimmer-'], [role='status'], details, button"
    )) {
      if (node.tagName === "BUTTON" || node.getAttribute("role") === "status") {
        const lbl = `${node.getAttribute("aria-label") || ""} ${node.textContent || ""}`.replace(/\s+/g, " ").trim();
        if (/^(?:thought\s+for|worked\s+for|reasoned\s+(?:for|about)|thinking|analyzed|searching\s+the\s+web)/i.test(lbl)) {
          const wrap = node.parentElement && node.parentElement !== turnEl && !node.parentElement.querySelector("[class*='DilResponseRoot-'], .markdown, .prose")
            ? node.parentElement
            : node;
          thinkNodes.push(wrap);
        }
      } else if (isThinkingNode(node)) {
        thinkNodes.push(node);
      }
    }
    const dedupThink = thinkNodes.filter((n, idx) => thinkNodes.indexOf(n) === idx && !thinkNodes.some(o => o !== n && typeof o.contains === "function" && o.contains(n)));
    const thinkChunks = dedupThink.map(n => sanitizeThinkingText(n.innerText || n.textContent || "")).filter(Boolean);
    const thinking = sanitizeThinkingText(thinkChunks.join("\n"));

    const hasAsstRenderer = Boolean(
      turnEl.querySelector("[class*='DilResponseRoot-'], [class*='DilRenderer-'], [class*='actionRow-'].turn-action-controls")
    );
    const answerRoots = Array.from(turnEl.querySelectorAll(
      "[class*='DilResponseRoot-'], [class*='DilRenderer-'], [class*='MarkdownRoot-'], .markdown, .prose"
    )).filter((mr) => {
      if (isThinkingNode(mr) || dedupThink.some(tn => typeof tn.contains === "function" && tn.contains(mr))) {
        return false;
      }
      if (mr.closest("[data-message-author-role='user'], .bg-user-message, [class*='user-message']")) {
        return false;
      }
      if (!hasAsstRenderer && mr.closest("[class*='bubble-']")) {
        return false;
      }
      return true;
    });

    let text = "";
    if (answerRoots.length) {
      const topAnswers = answerRoots.filter((mr) => !answerRoots.some(o => o !== mr && typeof o.contains === "function" && o.contains(mr)));
      text = topAnswers.map(mr => nodeToMarkdown(mr, isNonContentChromeNode)).filter(Boolean).join("\n\n").trim();
    } else if (!turnEl.querySelector("[data-message-author-role='user'], .bg-user-message") || hasAsstRenderer) {
      const fb = nodeToMarkdown(turnEl, isNonContentChromeNode);
      if (fb && fb !== thinking) text = fb;
    }

    const searchScope = turnEl.closest("[data-content-search-turn-key], article[data-testid^='conversation-turn-']") || turnEl.parentElement || turnEl;
    let hasActionBar = Boolean(searchScope.querySelector(".turn-action-controls button"));
    if (!hasActionBar) {
      for (const btn of searchScope.querySelectorAll(
        "button[aria-label='Rate response'], button[aria-label='Good response'], button[aria-label='More actions'], button[data-testid='copy-turn-action-button'], button[aria-label='Copy']"
      )) {
        if (btn.closest("[class*='DilResponseRoot-'], .markdown, .prose, pre")) continue;
        hasActionBar = true;
        break;
      }
    }

    const stoppedByUser = Boolean(!text && /\b(?:stopped generating|you stopped)\b/i.test(String(turnEl.innerText || turnEl.textContent || "")));
    return { text, thinking, isStreaming, hasActionBar, stoppedByUser };
  }

  function readAllTurns() {
    return readTurnContainers().map(turnEl => {
      const container = turnEl.closest("[data-content-search-turn-key], article[data-testid^='conversation-turn-']") || turnEl;
      const turnKey = turnEl.getAttribute("data-message-id") || container.getAttribute("data-content-search-turn-key") || container.getAttribute("data-testid") || "";
      return { ...extractTurnContent(turnEl), turnKey };
    });
  }

  function readErrorBanner() {
    const msgs = [];
    for (const el of document.querySelectorAll("[role='alert'], [role='status'], .text-token-text-error, [data-testid*='limit' i], [data-testid*='error' i], [data-testid*='toast' i], [class*='error' i], [class*='toast' i]")) {
      if (!isVisible(el)) continue;
      const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      if (t && t.length <= 1500 && !msgs.includes(t)) msgs.push(t);
    }
    return msgs.join(" | ");
  }

  globalThis.__wsaChatgptRuntime = {
    version: 3,
    rev: 8,
    turns: Object.create(null),
    findEditor,
    findSendButton,
    hasStopButton,
    readModelBadge,
    readAllTurns,
    readErrorBanner,
  };
  return true;
}

async function _chatgptStartTurnFn(reqId, prompt, images = []) {
  const u = globalThis.__wsaDomUtils;
  const rt = globalThis.__wsaChatgptRuntime;
  if (!u || u.version !== 3 || !rt || rt.version !== 3) {
    return { ok: false, error: "ChatGPT page runtime missing" };
  }
  return await u.startTurn(rt, reqId, prompt, images, {
    label: "ChatGPT",
    editorError: "ChatGPT prompt editor not found after 20s. Make sure chatgpt.com is open and signed in.",
    defaultModel: "ChatGPT Web",
  });
}

function _chatgptPollTurnFn(reqId) {
  const u = globalThis.__wsaDomUtils;
  const rt = globalThis.__wsaChatgptRuntime;
  if (!u || u.version !== 3 || !rt || rt.version !== 3) return { needRuntime: true };
  return u.pollTurn(rt, reqId, {
    label: "ChatGPT",
    defaultModel: "ChatGPT Web",
    stoppedRe: /\b(?:stopped generating|you stopped)\b/i,
  });
}

function _deepseekInstallPageRuntimeFn() {
  if (globalThis.__wsaDeepseekRuntime && globalThis.__wsaDeepseekRuntime.version === 3 && globalThis.__wsaDeepseekRuntime.rev === 7) {
    return true;
  }
  const u = globalThis.__wsaDomUtils;
  if (!u || u.version !== 3) return false;
  const { getClassStr, isVisible, nodeToMarkdown, sanitizeThinkingText } = u;

  function findEditor() {
    const selectors = [
      "textarea.ds-scroll-area",
      "textarea[aria-label*='DeepSeek' i]",
      "textarea[placeholder*='DeepSeek' i]",
      "textarea#chat-input",
      "textarea[placeholder*='Ask' i]",
      "textarea",
      "div.ProseMirror[contenteditable='true']",
      "[contenteditable='true'][role='textbox']",
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && isVisible(el)) return el;
    }
    return null;
  }

  function isButtonDisabled(b) {
    if (!b) return true;
    const cls = getClassStr(b);
    return Boolean(b.disabled || b.getAttribute("aria-disabled") === "true" || cls.includes("ds-button--disabled"));
  }

  function findSendButton(editorEl) {
    const scope = editorEl?.closest?.("form, ._77cefa5, [class*='chat-input' i], [class*='composer' i], [class*='input' i]") || editorEl?.parentElement?.parentElement || document;
    const roots = scope !== document ? [scope, document] : [document];
    const primarySelectors = [
      "div[role='button'].ds-button--primary.ds-button--circle",
      "button.ds-button--primary.ds-button--circle",
      "div[role='button']._52c986b",
      "button[data-testid='send-button']",
      "[role='button'][data-testid='send-button']",
      "button[aria-label*='Send' i]",
      "div[role='button'][aria-label*='Send' i]",
      "button[type='submit']",
    ];
    for (let rIdx = 0; rIdx < roots.length; rIdx++) {
      const root = roots[rIdx];
      for (const sel of primarySelectors) {
        if (sel === "button[type='submit']" && (scope === document || rIdx > 0)) continue;
        if (rIdx > 0 && !sel.includes("send-button") && !sel.includes("Send")) continue;
        for (const b of root.querySelectorAll(sel)) {
          if (!isVisible(b) || isButtonDisabled(b)) continue;
          const meta = `${b.getAttribute("data-testid") || ""} ${b.getAttribute("aria-label") || ""} ${b.getAttribute("title") || ""} ${b.innerText || ""}`.toLowerCase();
          const cls = getClassStr(b);
          const isExplicit = cls.includes("ds-button--primary") || (b.getAttribute("data-testid") || "").toLowerCase().includes("send-button") || meta.includes("send");
          if (isExplicit) {
            if (/\b(?:stop|voice|mic)\b/i.test(meta)) continue;
            return b;
          }
          if (/\b(?:deep\s*think|r1|search|upload|attach|file|voice|mic|stop|feedback|regenerate|retry|like|dislike|copy|share|edit|expand|collapse|full\s*screen)\b/i.test(meta)) continue;
          return b;
        }
      }
    }
    const candidates = Array.from(scope.querySelectorAll("button, div[role='button'], .ds-icon-button")).filter((b) => {
      if (!isVisible(b) || isButtonDisabled(b)) return false;
      const cls = getClassStr(b);
      if (cls.includes("ds-toggle-button") || cls.includes("f02f0e25")) return false;
      const lbl = `${b.getAttribute("aria-label") || ""} ${b.getAttribute("title") || ""} ${b.innerText || ""}`.trim();
      if (/deep\s*think|\br1\b|search|upload|attach|file|voice|mic|stop|feedback|regenerate|retry|like|dislike|copy|share|edit|new\s*chat|expand|collapse|full\s*screen/i.test(lbl)) return false;
      if (b.querySelector("rect") && !b.querySelector("path")) return false;
      return true;
    });
    return candidates.length ? candidates[candidates.length - 1] : null;
  }

  function hasStopButton() {
    for (const b of document.querySelectorAll("button[aria-label*='Stop' i], [role='button'][aria-label*='Stop' i], [data-testid*='stop' i]")) {
      if (isVisible(b) && !isButtonDisabled(b)) return true;
    }
    const ed = findEditor();
    const editorEmpty = Boolean(ed && !String(ed.value !== undefined ? ed.value : (ed.textContent || "")).trim());
    const scope = ed?.closest("form, ._77cefa5, [class*='chat-input' i], [class*='composer' i], [class*='input' i]") || document;
    for (const b of scope.querySelectorAll(".ds-button--primary.ds-button--circle, ._52c986b, .ds-icon-button, button, div[role='button']")) {
      if (!isVisible(b) || isButtonDisabled(b)) continue;
      const cls = getClassStr(b);
      if (cls.includes("ds-toggle-button") || cls.includes("f02f0e25")) continue;
      const lbl = `${b.getAttribute("aria-label") || ""} ${b.innerText || ""}`;
      if (/deep\s*think|\br1\b|search|upload|attach/i.test(lbl)) continue;
      const svg = b.querySelector("svg");
      if (svg && svg.querySelector("rect") && !svg.querySelector("path")) {
        return true;
      }
      if ((cls.includes("ds-button--primary") || cls.includes("_52c986b")) && editorEmpty) {
        return true;
      }
    }
    for (const hdr of document.querySelectorAll("._74c0879 ._245c867, ._74c0879 ._5255ff8, ._74c0879 ._60aa7fb")) {
      const txt = (hdr.innerText || hdr.textContent || "").replace(/\s+/g, " ").trim();
      if (/^(?:thinking|searching|reading)\b/i.test(txt)) return true;
    }
    return Boolean(document.querySelector("[data-is-streaming='true']"));
  }

  function readModelBadge() {
    let deepThinkActive = false;
    let searchActive = false;
    for (const b of document.querySelectorAll(".ds-toggle-button, button, [role='button']")) {
      if (!isVisible(b)) continue;
      const lbl = (b.getAttribute("aria-label") || b.innerText || b.textContent || "").replace(/\s+/g, " ").trim();
      if (!lbl || lbl.length > 48) continue;
      const isThinkBtn = /\bDeepThink\b|Deep\s*Think|\bR1\b/i.test(lbl);
      const isSearchBtn = !isThinkBtn && /\b(?:Smart\s+)?Search\b/i.test(lbl);
      if (!isThinkBtn && !isSearchBtn) continue;
      const pressed = b.getAttribute("aria-pressed") === "true"
        || b.getAttribute("data-selected") === "true"
        || b.getAttribute("data-state") === "checked";
      const cls = getClassStr(b);
      let activeColor = false;
      try {
        const st = getComputedStyle(b);
        const styles = `${st.backgroundColor || ""} ${st.borderColor || ""} ${st.color || ""}`;
        if (/40,\s*49,\s*66|103,\s*158,\s*254|72,\s*104,\s*178|4d6bfe|77,\s*107,\s*254|59,\s*130,\s*246|96,\s*165,\s*250/i.test(styles)) {
          activeColor = true;
        }
      } catch { /* ignore */ }
      const isSelected = pressed
        || activeColor
        || /(?:ds-toggle-button--selected|--selected|--active|\bselected\b|\bactive\b|\bchecked\b)/i.test(cls);
      if (!isSelected) continue;
      if (isThinkBtn) deepThinkActive = true;
      if (isSearchBtn) searchActive = true;
    }
    if (deepThinkActive && searchActive) return "DeepSeek-R1 · DeepThink + Search";
    if (deepThinkActive) return "DeepSeek-R1 · DeepThink";
    if (searchActive) return "DeepSeek-V3 · Search";
    return "DeepSeek-V3";
  }

  function isThinkingNode(el) {
    if (!el || el.nodeType !== 1) return false;
    const cls = getClassStr(el).toLowerCase();
    if (cls.includes("ds-assistant-message-main-content")) return false;
    const tid = (el.getAttribute("data-testid") || "").toLowerCase();
    return cls.includes("ds-think-content") || cls.includes("think-content") || tid.includes("think");
  }

  function isNonContentChromeNode(el) {
    if (isThinkingNode(el)) return true;
    const cls = getClassStr(el);
    const clsLow = cls.toLowerCase();
    if (clsLow.includes("ds-assistant-message-main-content") || (el.querySelector && el.querySelector(".ds-assistant-message-main-content"))) {
      return false;
    }
    if (
      (el.tagName || "").toLowerCase() === "span" &&
      el.parentElement &&
      getClassStr(el.parentElement).toLowerCase().includes("ds-markdown-cite") &&
      /^[-–—]$/.test((el.textContent || "").trim())
    ) {
      return true;
    }
    if (
      clsLow.includes("_74c0879") ||
      clsLow.includes("_245c867") ||
      clsLow.includes("_60aa7fb") ||
      clsLow.includes("e4c3fd02") ||
      clsLow.includes("f93f59e4") ||
      clsLow.includes("ds-button") ||
      clsLow.includes("ds-icon-button")
    ) {
      return true;
    }
    if (clsLow.includes("ds-flex") && el.querySelector(".ds-icon-button") && !el.querySelector(".ds-markdown")) return true;
    if (clsLow.split(/\s+/).includes("sr-only")) return true;
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (role === "status" || role === "toolbar" || el.getAttribute("aria-live")) return true;
    return false;
  }

  function readTurnContainers() {
    const msgs = Array.from(document.querySelectorAll(".ds-message"));
    if (msgs.length) {
      const assistantMsgs = msgs.filter((el) => {
        if (msgs.some(o => o !== el && typeof o.contains === "function" && o.contains(el))) return false;
        const cls = getClassStr(el);
        const hasMainAnswer = Boolean(el.querySelector(".ds-assistant-message-main-content"));
        if (!hasMainAnswer && (cls.includes("d29f3d7d") || el.querySelector(".fbb737a4, .ds-collapsible-text"))) {
          return false;
        }
        if (
          hasMainAnswer ||
          el.querySelector(".ds-markdown, .ds-think-content, [class*='think-content' i], ._74c0879, .f93f59e4")
        ) {
          return true;
        }
        if (el.querySelector("[aria-label*='Edit' i], [data-testid*='user' i]")) return false;
        return Boolean(el.getAttribute("data-message-author-role") === "assistant");
      });
      if (assistantMsgs.length) return assistantMsgs;
    }
    const mainMds = Array.from(document.querySelectorAll(".ds-markdown.ds-assistant-message-main-content"));
    const fallbackMds = mainMds.length
      ? mainMds
      : Array.from(document.querySelectorAll(".ds-markdown")).filter(
          (mr) => !mr.closest(".ds-think-content, [class*='think-content' i], ._74c0879, .e4c3fd02")
        );
    return fallbackMds.map((mr) => mr.closest(".ds-message") || mr.parentElement || mr).filter((el, idx, arr) => arr.indexOf(el) === idx);
  }

  function extractTurnContent(turnEl) {
    if (!turnEl) return { text: "", thinking: "", isStreaming: false, hasActionBar: false, stoppedByUser: false };
    const isStreaming = turnEl.getAttribute("data-is-streaming") === "true" || Boolean(turnEl.querySelector("[data-is-streaming='true']"));

    const thinkNodes = Array.from(turnEl.querySelectorAll(".ds-think-content, [class*='think-content' i], [data-testid*='think' i]"));
    const topThink = thinkNodes.filter((n) => !thinkNodes.some(o => o !== n && typeof o.contains === "function" && o.contains(n)));
    let thinking = sanitizeThinkingText(
      topThink.map(n => nodeToMarkdown(n) || n.innerText || n.textContent || "").filter(Boolean).join("\n")
    );

    const primaryAnswerRoots = Array.from(turnEl.querySelectorAll(".ds-markdown.ds-assistant-message-main-content"));
    const mdRoots = primaryAnswerRoots.length
      ? primaryAnswerRoots
      : Array.from(turnEl.querySelectorAll(".ds-markdown")).filter(
          (mr) => !isThinkingNode(mr) && !mr.closest(".ds-think-content, [class*='think-content' i], ._74c0879, .e4c3fd02")
        );

    let text = "";
    if (mdRoots.length) {
      const topMd = mdRoots.filter((mr) => !mdRoots.some(o => o !== mr && o.contains(mr)));
      text = topMd.map(mr => nodeToMarkdown(mr, isNonContentChromeNode)).filter(Boolean).join("\n\n").trim();
    } else {
      const fb = nodeToMarkdown(turnEl, isNonContentChromeNode);
      if (fb && fb !== thinking) text = fb;
    }

    if (!thinking && !text) {
      const liveHdr = turnEl.querySelector("._74c0879 ._60aa7fb, ._74c0879 ._245c867");
      const hdrTxt = liveHdr ? (liveHdr.innerText || liveHdr.textContent || "").replace(/\s+/g, " ").trim() : "";
      if (/^(?:thinking|searching|.+web pages)/i.test(hdrTxt)) {
        thinking = hdrTxt;
      }
    }

    let hasActionBar = false;
    let sawContinueBtn = false;
    const actionScopes = [turnEl];
    let sib = turnEl.nextElementSibling;
    for (let hops = 0; sib && hops < 3 && !getClassStr(sib).includes("ds-message"); hops++) {
      actionScopes.push(sib);
      sib = sib.nextElementSibling;
    }
    for (const scope of actionScopes) {
      for (const btn of scope.querySelectorAll(
        ".ds-button--iconLabelTertiary, .ds-icon-button, button[aria-label='Read aloud'], button[aria-label*='Copy' i], button[aria-label*='Regenerate' i], [role='button'], button"
      )) {
        if (btn.closest(".ds-markdown, .ds-think-content, ._74c0879, .f93f59e4, pre")) continue;
        const bCls = getClassStr(btn);
        const bLbl = (btn.getAttribute("aria-label") || btn.innerText || btn.textContent || "").replace(/\s+/g, " ").trim();
        if (/^Continue$/i.test(bLbl)) {
          sawContinueBtn = true;
        }
        if (
          bCls.includes("ds-button--iconLabelTertiary") ||
          bCls.includes("ds-icon-button") ||
          bCls.includes("db183363") ||
          /^(?:Read aloud|Copy|Regenerate|Like|Dislike|Share|Continue)$/i.test(bLbl)
        ) {
          hasActionBar = true;
        }
      }
      if (hasActionBar && sawContinueBtn) break;
    }

    const stoppedByUser = Boolean(
      !text &&
      (sawContinueBtn || /\b(?:stopped generating|generation stopped)\b/i.test(String(turnEl.innerText || turnEl.textContent || "")))
    );
    return { text, thinking, isStreaming, hasActionBar, stoppedByUser };
  }

  function readAllTurns() {
    return readTurnContainers().map(extractTurnContent);
  }

  function readErrorBanner() {
    const msgs = [];
    for (const el of document.querySelectorAll("[role='alert'], .ds-toast, [class*='error' i], [class*='toast' i]")) {
      if (!isVisible(el)) continue;
      const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      if (t && t.length <= 1500) msgs.push(t);
    }
    return msgs.join(" | ");
  }

  globalThis.__wsaDeepseekRuntime = {
    version: 3,
    rev: 7,
    turns: Object.create(null),
    findEditor,
    findSendButton,
    hasStopButton,
    readModelBadge,
    readAllTurns,
    readErrorBanner,
  };
  return true;
}

async function _deepseekStartTurnFn(reqId, prompt, images = []) {
  const u = globalThis.__wsaDomUtils;
  const rt = globalThis.__wsaDeepseekRuntime;
  if (!u || u.version !== 3 || !rt || rt.version !== 3) {
    return { ok: false, error: "DeepSeek page runtime missing" };
  }
  return await u.startTurn(rt, reqId, prompt, images, {
    label: "DeepSeek",
    editorError: "DeepSeek prompt editor not found after 20s. Make sure chat.deepseek.com is open and signed in.",
    defaultModel: "DeepSeek Web",
  });
}

function _deepseekPollTurnFn(reqId) {
  const u = globalThis.__wsaDomUtils;
  const rt = globalThis.__wsaDeepseekRuntime;
  if (!u || u.version !== 3 || !rt || rt.version !== 3) return { needRuntime: true };
  return u.pollTurn(rt, reqId, {
    label: "DeepSeek",
    defaultModel: "DeepSeek Web",
    stoppedRe: /\b(?:stopped generating|generation stopped)\b/i,
  });
}

function _veniceInstallPageRuntimeFn() {
  if (globalThis.__wsaVeniceRuntime && globalThis.__wsaVeniceRuntime.version === 1 && globalThis.__wsaVeniceRuntime.rev === 7) {
    return true;
  }
  const u = globalThis.__wsaDomUtils;
  if (!u || u.version !== 3) return false;
  const { getClassStr, isVisible, nodeToMarkdown, sanitizeThinkingText } = u;
  const MODEL_KW_RE = /\b(?:DeepSeek|Kimi|GLM|Claude|Grok|Llama|Qwen|Mistral|Gemini|Venice\s*Auto|Auto)\b/i;

  function getBtnLabel(b) {
    if (!b) return "";
    return (b.getAttribute?.("aria-label") || b.innerText || b.textContent || b.getAttribute?.("title") || "").replace(/\s+/g, " ").trim();
  }

  function isButtonDisabled(b) {
    if (!b) return true;
    return Boolean(b.disabled || b.getAttribute?.("aria-disabled") === "true" || (typeof b.hasAttribute === "function" && b.hasAttribute("data-disabled")));
  }

  function findEditor() {
    const selectors = [
      "textarea[data-slot='composer-field']",
      "[aria-label='Mind chat attachment dropzone'] textarea",
      "textarea[aria-label='Chat message input']",
      "textarea[aria-label*='Ask a question' i]",
      "textarea[aria-label*='Ask anything' i]",
      "textarea.chakra-textarea",
      "textarea",
      "[contenteditable='true'][role='textbox']",
    ];
    for (const sel of selectors) {
      for (const el of document.querySelectorAll(sel)) {
        if (el.closest?.("[role='dialog']")) continue;
        if (isVisible(el) && !el.disabled && el.getAttribute?.("aria-disabled") !== "true") return el;
      }
    }
    return null;
  }

  function findSendButton(editorEl) {
    const scope = editorEl?.closest?.("form, [aria-label='Mind chat attachment dropzone'], [class*='composer' i]") || editorEl?.parentElement?.parentElement || document;
    const roots = scope !== document ? [scope, document] : [document];
    const primarySelectors = [
      "button[aria-label='Send message']",
      "[role='button'][aria-label='Send message']",
      "button[aria-label*='Send message' i]",
      "[role='button'][aria-label*='Send message' i]",
      "button[aria-label='Submit chat']",
      "[role='button'][aria-label='Submit chat']",
      "button[aria-label*='Submit chat' i]",
      "[role='button'][aria-label*='Submit chat' i]",
      "button[data-testid='send-button']",
      "[role='button'][data-testid='send-button']",
      "button[type='submit']",
    ];
    const rejectRe = /\b(?:dictate|voice|mic|microphone|attach|file|upload|stop|settings|actions|model|new\s*chat|get\s*pro|more\s*models)\b/i;
    for (let rIdx = 0; rIdx < roots.length; rIdx++) {
      const root = roots[rIdx];
      for (const sel of primarySelectors) {
        if (sel === "button[type='submit']" && (scope === document || rIdx > 0)) continue;
        for (const b of root.querySelectorAll(sel)) {
          if (b.closest?.("[role='dialog']")) continue;
          if (!isVisible(b) || isButtonDisabled(b)) continue;
          const lbl = getBtnLabel(b);
          const isExplicit = /^(?:send(?:\s*message)?|submit(?:\s*chat)?)$/i.test(lbl) || (b.getAttribute("data-testid") || "").toLowerCase() === "send-button";
          if (isExplicit) {
            if (/\b(?:dictate|voice|mic|stop)\b/i.test(lbl)) continue;
            return b;
          }
          if (rejectRe.test(lbl)) continue;
          return b;
        }
      }
    }
    for (const b of scope.querySelectorAll("button, [role='button']")) {
      if (b.closest?.("[role='dialog']")) continue;
      if (!isVisible(b) || isButtonDisabled(b)) continue;
      const lbl = getBtnLabel(b);
      if (/^(?:send(?:\s*message)?|submit(?:\s*chat)?)$/i.test(lbl)) return b;
    }
    return null;
  }

  function hasStopButton() {
    for (const b of document.querySelectorAll(
      "button[aria-label='Stop generation'], button[aria-label*='Stop generation' i], button[aria-label='stop' i], [data-testid*='stop' i]"
    )) {
      if (b.closest?.("[role='dialog']")) continue;
      if (isVisible(b) && !isButtonDisabled(b)) return true;
    }
    for (const b of document.querySelectorAll("form button, [aria-label='Mind chat attachment dropzone'] button, button")) {
      if (b.closest?.("[role='dialog']")) continue;
      if (!isVisible(b) || isButtonDisabled(b)) continue;
      const lbl = getBtnLabel(b);
      if (/^stop(?:\s+generation)?$/i.test(lbl)) return true;
    }
    return Boolean(document.querySelector("[data-is-streaming='true']"));
  }

  function readModelBadge() {
    for (const sel of [
      "button[aria-haspopup='menu']",
      "button[aria-label*='model' i]",
      "button.chakra-button",
    ]) {
      for (const b of document.querySelectorAll(sel)) {
        if (!isVisible(b)) continue;
        const txt = getBtnLabel(b);
        if (!txt || txt.length > 64) continue;
        if (/^(?:more models|new chat|settings|actions|get pro(?:\s+access)?|dictate|send message|submit chat|stop(?:\s+generation)?)$/i.test(txt)) continue;
        if (MODEL_KW_RE.test(txt)) return txt;
      }
    }
    for (const pEl of document.querySelectorAll("main p.chakra-text")) {
      if (!isVisible(pEl) || pEl.closest?.(".prose")) continue;
      const txt = (pEl.innerText || pEl.textContent || "").replace(/\s+/g, " ").trim();
      if (txt && txt.length <= 64 && MODEL_KW_RE.test(txt)) return txt;
    }
    const locPath = (typeof location !== "undefined" && location?.pathname) || (typeof window !== "undefined" && window.location?.pathname) || "";
    return /\/chat\/agent/i.test(locPath) ? "DeepSeek V4.1 Flash Uncensored" : "Venice Web";
  }

  function isTurnActionGroup(el) {
    if (!el || el.nodeType !== 1) return false;
    if ((el.getAttribute("role") || "").toLowerCase() !== "group") return false;
    if (el.querySelector(".prose, pre, table, ul, ol")) return false;
    for (const b of el.querySelectorAll("button, [role='button']")) {
      const lbl = getBtnLabel(b);
      if (/^(?:Copy|Regenerate|Edit|Delete|Edit Response|Venice Voice|Create Image|Create Video|Encrypt and share this conversation|Actions|Copy code|download as file)$/i.test(lbl)) {
        return true;
      }
    }
    return false;
  }

  function isCodeHeaderNode(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.querySelector("pre, p, h1, h2, h3, ul, ol, table")) return false;
    const cls = getClassStr(el);
    if (cls.includes("css-1tozq6k")) return true;
    for (const b of el.querySelectorAll("button, [role='button']")) {
      const lbl = getBtnLabel(b);
      if (/^(?:copy\s*code|download\s+as\s+file|copied)$/i.test(lbl)) return true;
    }
    return false;
  }

  function isThinkingNode(el) {
    if (!el || el.nodeType !== 1) return false;
    if (hasAssistantActionBar(el)) return false;
    const cls = getClassStr(el);
    const clsLow = cls.toLowerCase();
    const tid = (el.getAttribute("data-testid") || "").toLowerCase();
    if (
      cls.includes("css-ev1bas") ||
      cls.includes("css-1w2ukei") ||
      cls.includes("css-1gtoqpr") ||
      cls.includes("css-8ozojj") ||
      cls.includes("css-1frcpea") ||
      cls.includes("css-gild96") ||
      cls.includes("css-934klp") ||
      clsLow.includes("thought") ||
      clsLow.includes("reasoning") ||
      tid.includes("think") ||
      tid.includes("reasoning")
    ) {
      return true;
    }
    if (el.querySelector("pre")) return false;
    if (!el.querySelector(".prose, pre, ul, ol, table, [role='group']")) {
      const txt = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      if (/^(?:worked\s+for\s+\d+(?:\.\d+)?\s*(?:s|sec|seconds?)(?:\s*·\s*\d+\s*steps?)?|thought\s+for\s+\d+(?:\.\d+)?\s*(?:s|sec|seconds?)|thought\s+process|thinking(?:\.\.\.|…)?|loading(?:\.\.\.|…)?|done)$/i.test(txt)) {
        return true;
      }
    }
    return false;
  }

  function hasAssistantActionBar(scopeEl) {
    if (!scopeEl || !scopeEl.querySelectorAll) return false;
    for (const grp of scopeEl.querySelectorAll("[role='group']")) {
      if (grp.closest?.(".prose, pre")) continue;
      for (const b of grp.querySelectorAll("button, [role='button']")) {
        const lbl = getBtnLabel(b);
        if (/^(?:Regenerate|Delete|Venice Voice|Create Image|Create Video|Encrypt and share this conversation)$/i.test(lbl)) {
          return true;
        }
      }
    }
    return false;
  }

  function countAssistantActionBars(scopeEl) {
    if (!scopeEl || !scopeEl.querySelectorAll) return 0;
    let count = 0;
    for (const grp of scopeEl.querySelectorAll("[role='group']")) {
      if (grp.closest?.(".prose, pre")) continue;
      const isAsstGrp = Array.from(grp.querySelectorAll("button, [role='button']")).some((b) =>
        /^(?:Regenerate|Delete|Venice Voice|Create Image|Create Video|Encrypt and share this conversation)$/i.test(getBtnLabel(b))
      );
      if (isAsstGrp) count++;
    }
    return count;
  }

  function hasUserTurnMarker(el) {
    if (!el || el.nodeType !== 1) return false;
    const cls = getClassStr(el);
    if (cls.includes("fancy-card") || cls.includes("css-1dio6h5")) return true;
    if (el.querySelector?.(".fancy-card, .css-zdl3ww, .css-1dio6h5")) return true;
    for (const grp of el.querySelectorAll?.("[role='group']") || []) {
      if (grp.closest?.(".prose, pre")) continue;
      const labels = Array.from(grp.querySelectorAll("button, [role='button']")).map(getBtnLabel);
      if (labels.some((l) => /^Edit Response$/i.test(l))) return true;
      const hasEdit = labels.some((l) => /^Edit$/i.test(l));
      const hasCopy = labels.some((l) => /^Copy$/i.test(l));
      const hasAsstOnly = labels.some((l) => /^(?:Regenerate|Delete|Venice Voice|Create Image|Create Video)$/i.test(l));
      if (hasEdit && hasCopy && !hasAsstOnly) return true;
    }
    return false;
  }

  function isNonContentChromeNode(el) {
    if (!el || el.nodeType !== 1) return false;
    if (isThinkingNode(el) || isCodeHeaderNode(el) || isTurnActionGroup(el)) return true;
    const cls = getClassStr(el);
    const clsLow = cls.toLowerCase();
    if (clsLow.split(/\s+/).includes("sr-only")) return true;
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (role === "progressbar" || role === "status" || role === "toolbar" || el.getAttribute("aria-live")) return true;
    if (!el.closest?.(".prose")) {
      if (cls.includes("css-1vyjab")) return true;
      if (!el.querySelector(".prose, pre, ul, ol, table") && !hasAssistantActionBar(el)) {
        const txt = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
        if (/^\d+(?:\.\d+)?s$/i.test(txt)) return true;
      }
    }
    return false;
  }

  function readTurnContainers() {
    const convRoot = document.querySelector(".minds-chat-scroll-root") || document.querySelector("main") || document.body;
    if (!convRoot) return [];
    const anchors = [];
    const insideIgnoredChrome = (el) => Boolean(el.closest("form, header, nav, aside, [role='dialog']"));
    for (const grp of convRoot.querySelectorAll("[role='group']")) {
      if (insideIgnoredChrome(grp) || grp.closest(".prose, pre")) continue;
      const isAsstGrp = Array.from(grp.querySelectorAll("button, [role='button']")).some((b) =>
        /^(?:Regenerate|Delete|Venice Voice|Create Image|Create Video)$/i.test(getBtnLabel(b))
      );
      if (isAsstGrp) anchors.push(grp);
    }
    for (const tk of convRoot.querySelectorAll(".css-934klp, .css-ev1bas, .css-gild96, [data-testid*='think' i], [data-testid*='reasoning' i]")) {
      if (!insideIgnoredChrome(tk)) anchors.push(tk);
    }
    for (const mr of convRoot.querySelectorAll(".prose")) {
      if (insideIgnoredChrome(mr)) continue;
      if (mr.closest(".css-ev1bas, .css-8ozojj, .css-gild96, [data-testid*='think' i], [data-testid*='reasoning' i]")) continue;
      if (mr.closest(".fancy-card, .css-zdl3ww, .css-1dio6h5")) continue;
      let userCheck = mr.parentElement;
      let ownedByUser = false;
      for (let hops = 0; hops < 4 && userCheck && userCheck !== convRoot; hops++) {
        if (hasAssistantActionBar(userCheck) || userCheck.querySelector(".css-934klp, .css-ev1bas, .css-gild96")) break;
        if (hasUserTurnMarker(userCheck)) {
          ownedByUser = true;
          break;
        }
        userCheck = userCheck.parentElement;
      }
      if (!ownedByUser) anchors.push(mr);
    }

    const containers = [];
    for (const a of anchors) {
      let cur = a;
      while (cur.parentElement && cur.parentElement !== convRoot && cur.parentElement !== document.body) {
        const up = cur.parentElement;
        if (up.querySelector("form, nav, header, aside")) break;
        if (hasUserTurnMarker(up)) break;
        if (countAssistantActionBars(up) > 1) break;
        const topProse = Array.from(up.querySelectorAll(".prose")).filter(
          (p) => !p.closest(".css-ev1bas, .css-8ozojj, .css-gild96, [data-testid*='think' i], [data-testid*='reasoning' i]")
        );
        if (topProse.length > 1 && topProse.some((p) => p.parentElement !== topProse[0].parentElement)) break;
        cur = up;
      }
      if (!hasUserTurnMarker(cur) && !containers.includes(cur)) {
        containers.push(cur);
      }
    }
    const topContainers = containers.filter((el) => !containers.some((o) => o !== el && typeof o.contains === "function" && o.contains(el)));
    topContainers.sort((a, b) => {
      if (a === b) return 0;
      if (typeof a.compareDocumentPosition === "function") {
        const pos = a.compareDocumentPosition(b);
        if (pos & 4) return -1;
        if (pos & 2) return 1;
      }
      return 0;
    });
    return topContainers;
  }

  function isThinkChromeOnly(el) {
    if (!el || el.nodeType !== 1) return false;
    const cls = getClassStr(el);
    if (cls.includes("css-1gtoqpr") || cls.includes("css-1frcpea") || cls.includes("css-934klp")) {
      return true;
    }
    if (!el.querySelector(".prose, .css-8ozojj, .css-gild96, pre, ul, ol")) {
      const txt = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      if (/^(?:worked\s+for\s+\d+(?:\.\d+)?\s*(?:s|sec|seconds?)(?:\s*·\s*\d+\s*steps?)?|thought\s+for\s+\d+(?:\.\d+)?\s*(?:s|sec|seconds?)|thought\s+process|thinking(?:\.\.\.|…)?|loading(?:\.\.\.|…)?|done)$/i.test(txt)) {
        return true;
      }
    }
    return false;
  }

  function extractTurnContent(turnEl) {
    if (!turnEl || hasUserTurnMarker(turnEl)) {
      return { text: "", thinking: "", isStreaming: false, hasActionBar: false, stoppedByUser: false };
    }
    const hasActionBar = hasAssistantActionBar(turnEl);
    const thinkNodes = Array.from(turnEl.querySelectorAll(
      ".css-ev1bas, .css-8ozojj, .css-gild96, [data-testid*='think' i], [data-testid*='reasoning' i], [class*='thought' i], [class*='reasoning' i], .css-934klp"
    )).filter((n) => !hasAssistantActionBar(n) && !isThinkChromeOnly(n));
    const topThink = thinkNodes.filter((n) => !thinkNodes.some((o) => o !== n && typeof o.contains === "function" && o.contains(n)));
    const thinking = sanitizeThinkingText(
      topThink.map((n) => nodeToMarkdown(n, isThinkChromeOnly) || n.innerText || n.textContent || "").filter(Boolean).join("\n")
    );

    const proseRoots = Array.from(turnEl.querySelectorAll(".prose")).filter(
      (mr) => !isThinkingNode(mr) && !mr.closest(".css-ev1bas, .css-8ozojj, .css-gild96, [data-testid*='think' i], [data-testid*='reasoning' i], .fancy-card, .css-1dio6h5")
    );
    const topProse = proseRoots.filter((mr) => !proseRoots.some((o) => o !== mr && typeof o.contains === "function" && o.contains(mr)));
    let text = "";
    if (topProse.length) {
      text = topProse.map((mr) => nodeToMarkdown(mr, isNonContentChromeNode)).filter(Boolean).join("\n\n").trim();
    } else {
      const fb = nodeToMarkdown(turnEl, isNonContentChromeNode);
      if (fb && fb !== thinking) text = fb;
    }

    let isStreaming = turnEl.getAttribute("data-is-streaming") === "true";
    if (!isStreaming && !hasActionBar) {
      for (const el of turnEl.querySelectorAll("p, span, div")) {
        if (el.closest(".prose, pre")) continue;
        const txt = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
        if (/^(?:loading(?:\.\.\.|…)?|thinking(?:\.\.\.|…)?)$/i.test(txt)) {
          isStreaming = true;
          break;
        }
      }
    }

    const stoppedByUser = Boolean(
      !text &&
      /\b(?:stopped generating|generation stopped|response stopped)\b/i.test(String(turnEl.innerText || turnEl.textContent || ""))
    );
    return { text, thinking, isStreaming, hasActionBar, stoppedByUser };
  }

  function readAllTurns() {
    return readTurnContainers().map(extractTurnContent);
  }

  function readErrorBanner() {
    const msgs = [];
    for (const el of document.querySelectorAll(
      "[role='alert'], .chakra-alert, [id^='chakra-toast'] [role='status'], [role='region'][aria-live='polite'] [role='status'], [data-testid*='error' i], [data-testid*='limit' i], [data-testid*='toast' i]"
    )) {
      if (!isVisible(el) || el.matches("[role='progressbar'], button")) continue;
      const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      if (!t || /^get pro access$/i.test(t) || t.length > 1500) continue;
      if (!msgs.includes(t)) msgs.push(t);
    }
    return msgs.join(" | ");
  }

  globalThis.__wsaVeniceRuntime = {
    version: 1,
    rev: 7,
    turns: Object.create(null),
    findEditor,
    findSendButton,
    hasStopButton,
    readModelBadge,
    readAllTurns,
    readErrorBanner,
  };
  return true;
}

async function _veniceStartTurnFn(reqId, prompt, images = []) {
  const u = globalThis.__wsaDomUtils;
  const rt = globalThis.__wsaVeniceRuntime;
  if (!u || u.version !== 3 || !rt || rt.version !== 1) {
    return { ok: false, error: "Venice page runtime missing" };
  }
  return await u.startTurn(rt, reqId, prompt, images, {
    label: "Venice",
    editorError: "Venice prompt editor not found after 20s. Make sure venice.ai is open and signed in.",
    defaultModel: "DeepSeek V4.1 Flash Uncensored",
  });
}

function _venicePollTurnFn(reqId) {
  const u = globalThis.__wsaDomUtils;
  const rt = globalThis.__wsaVeniceRuntime;
  if (!u || u.version !== 3 || !rt || rt.version !== 1) return { needRuntime: true };
  return u.pollTurn(rt, reqId, {
    label: "Venice",
    defaultModel: "DeepSeek V4.1 Flash Uncensored",
    stoppedRe: /\b(?:stopped generating|generation stopped|response stopped)\b/i,
  });
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    EXPORT_ORDER,
    PROVIDER_KEYS,
    PROVIDERS,
    normalizeProviderKey,
    detectTabProvider,
    extractGeminiConvId,
    extractClaudeConvId,
    extractChatgptConvId,
    extractDeepseekConvId,
    extractVeniceConvId,
    extractConvId,
    parseAuthUserFromUrl,
    pickBestTabForProvider,
    pickGeminiTab,
    getRelaySessions,
    saveRelaySession,
    clearRelaySession,
    selectEffectiveChatUrl,
    evaluateTabRecoveryAction,
    computeTailDomRange,
    sanitizeInspectUrl,
    isProtectedInspectUrl,
    normalizeBrowserHint,
    _installWsaDomUtilsFn,
    _domInstallPageRuntimeFn,
    _domStartTurnFn,
    _domPollTurnFn,
    _claudeInstallPageRuntimeFn,
    _claudeStartTurnFn,
    _claudePollTurnFn,
    _chatgptInstallPageRuntimeFn,
    _chatgptStartTurnFn,
    _chatgptPollTurnFn,
    _deepseekInstallPageRuntimeFn,
    _deepseekStartTurnFn,
    _deepseekPollTurnFn,
    _veniceInstallPageRuntimeFn,
    _veniceStartTurnFn,
    _venicePollTurnFn,
    _pageCollectBrowserDiagnosticsFn,
    _pageInteractDomFn,
  };
}
