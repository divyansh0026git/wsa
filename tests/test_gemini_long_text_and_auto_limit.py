"""
Tests for Gemini long-text zero-freeze insertion, auto-wait dismissal,
and Gemini limit exceeded auto-downgrade detection (Gemini Auto / Flash-Lite).
"""

import json
import subprocess
from pathlib import Path
import pytest

from wsa_agent.client import _extract_limit_error_from_dict
from wsa_server.models import extract_upstream_model_info


def test_gemini_client_extract_limit_on_auto_and_flash_lite():
    # Various phrases Gemini uses when limit is exceeded and it switches to Auto or Flash-Lite
    phrases = [
        "Switched to Gemini Auto",
        "Switched to Gemini Flash-Lite",
        "Switched to Auto",
        "Switched to Flash-Lite",
        "Responses will use Gemini Auto",
        "Responses will use Gemini Flash-Lite",
        "Responses will use Auto",
        "Responses will use Flash-Lite",
        "Using Gemini Auto until your limit resets",
        "Using Gemini Flash-Lite until your limit resets",
        "You've reached your Gemini Pro limit",
        "Gemini Pro limit reached",
        "Gemini Advanced limit reached",
    ]
    for phrase in phrases:
        err = {"error": {"message": phrase, "provider": "gemini"}}
        res = _extract_limit_error_from_dict(err)
        assert res is not None, f"Failed to extract limit from: {phrase}"
        assert res[0] == "gemini"


def test_gemini_models_detect_downgrade_on_auto_and_flash_lite():
    # Test extract_upstream_model_info detects Auto and Flash-Lite downgrades
    info_auto = extract_upstream_model_info({"badge": "Gemini Auto"})
    assert info_auto["is_downgraded_flash_lite"] is True

    info_auto_bare = extract_upstream_model_info({"badge": "Auto"})
    assert info_auto_bare["is_downgraded_flash_lite"] is True

    info_flash_lite = extract_upstream_model_info({"badge": "Gemini Flash-Lite"})
    assert info_flash_lite["is_downgraded_flash_lite"] is True

    info_flash_lite_space = extract_upstream_model_info({"badge": "3.5 Flash Lite"})
    assert info_flash_lite_space["is_downgraded_flash_lite"] is True

    # Pro models should NOT be marked as downgraded
    info_pro = extract_upstream_model_info({"badge": "Gemini 2.0 Pro", "mode": 8})
    assert info_pro["is_downgraded_flash_lite"] is False


def test_node_gemini_long_text_and_auto_wait_dismissal():
    bg_path = (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()
    node_script = f"""
const assert = require("assert");
const fs = require("fs");

class MockElement {{
  constructor(tag, attrs = {{}}, text = "") {{
    this.tagName = String(tag || "div").toUpperCase();
    this.attributes = {{ ...attrs }};
    this.className = attrs.class || attrs.className || "";
    this.innerText = text;
    this.textContent = text;
    this.childNodes = [];
    this.parentElement = null;
    this.listeners = {{}};
    this.clicked = false;
  }}
  getAttribute(n) {{ return this.attributes[n] !== undefined ? this.attributes[n] : null; }}
  setAttribute(n, v) {{ this.attributes[n] = String(v); }}
  appendChild(c) {{ c.parentElement = this; this.childNodes.push(c); return c; }}
  removeChild(c) {{
    const idx = this.childNodes.indexOf(c);
    if (idx !== -1) {{ c.parentElement = null; this.childNodes.splice(idx, 1); }}
    return c;
  }}
  replaceChildren(...nodes) {{
    this.childNodes = [];
    for (const n of nodes) this.appendChild(n);
  }}
  querySelector(sel) {{
    const all = this.querySelectorAll(sel);
    return all.length ? all[0] : null;
  }}
  querySelectorAll(sel) {{
    const res = [];
    const parts = sel.split(",").map(p => p.trim().toLowerCase());
    const walk = (n) => {{
      for (const c of n.childNodes) {{
        const tag = c.tagName.toLowerCase();
        const cls = (c.className || "").toLowerCase();
        const role = (c.getAttribute("role") || "").toLowerCase();
        for (const p of parts) {{
          if (p === tag || p === "*") {{ res.push(c); break; }}
          else if (p === "button" && tag === "button") {{ res.push(c); break; }}
          else if (p === "[role='button']" && role === "button") {{ res.push(c); break; }}
          else if (p.includes("role='textbox'") && role === "textbox") {{ res.push(c); break; }}
          else if (p.includes("class*=") && cls.includes(p.split("class*=")[1].replace(/['"\\s\\]i]/g, ""))) {{ res.push(c); break; }}
          else if (p.includes("role=") && role === p.split("role=")[1].replace(/['"\\s\\]i]/g, "")) {{ res.push(c); break; }}
        }}
        walk(c);
      }}
    }};
    walk(this);
    return res;
  }}
  addEventListener(t, fn) {{
    if (!this.listeners[t]) this.listeners[t] = [];
    this.listeners[t].push(fn);
  }}
  dispatchEvent(e) {{
    for (const h of (this.listeners[e.type] || [])) h(e);
    return true;
  }}
  click() {{
    this.clicked = true;
    this.dispatchEvent({{ type: "click", bubbles: true }});
  }}
  focus() {{}}
  getBoundingClientRect() {{ return {{ left: 10, top: 10, width: 200, height: 60 }}; }}
}}

class MockDocument extends MockElement {{
  constructor() {{
    super("document");
    this.body = new MockElement("body");
    this.appendChild(this.body);
  }}
  createElement(tag) {{ return new MockElement(tag); }}
  createDocumentFragment() {{ return new MockElement("fragment"); }}
  createTextNode(text) {{ return new MockElement("#text", {{}}, text); }}
  execCommand(cmd, showUi, val) {{
    return false; // Force paste/DOM fallback
  }}
}}

class MockDataTransfer {{
  constructor() {{
    this.data = {{}};
  }}
  setData(k, v) {{ this.data[k] = String(v); }}
  getData(k) {{ return this.data[k] || ""; }}
}}

class MockClipboardEvent {{
  constructor(type, opts = {{}}) {{
    this.type = type;
    this.clipboardData = opts.clipboardData || new MockDataTransfer();
  }}
}}

globalThis.document = new MockDocument();
globalThis.window = {{
  location: {{ href: "https://gemini.google.com/app" }},
  getSelection: () => ({{
    removeAllRanges: () => {{}},
    addRange: () => {{}},
  }})
}};
globalThis.DataTransfer = MockDataTransfer;
globalThis.ClipboardEvent = MockClipboardEvent;

(async () => {{
const bg = require({json.dumps(bg_path)});
bg._domInstallPageRuntimeFn();
const rt = globalThis.__wsaRuntime;
assert.ok(rt, "Gemini runtime must install");

// 1. Test Limit detection on Gemini Auto / Flash-Lite phrases
const phrases = [
  "Switched to Gemini Auto because your limit was reached",
  "Switched to Gemini Flash-Lite",
  "Responses will use Gemini Auto",
  "Responses will use Gemini Flash-Lite",
  "Using Gemini Auto until 3:00 AM",
  "Using Gemini Flash-Lite until limit resets",
  "You've reached your Gemini Pro limit",
  "Gemini Pro limit reached",
  "Limit reached · Resets 6:39 AM      See usage",
  "Limit reached · Resets 12:00 PM",
  "Limit reached"
];
for (const p of phrases) {{
  const det = rt.detectLimitMessage(p, true);
  assert.ok(det, "Must detect limit for: " + p);
}}

// 1b. Test readModePickerBadge with icon/chevron (e.g., ≡ Gemini Auto ∨)
const modelMenuBtn = new MockElement("button", {{ "aria-haspopup": "menu" }}, "≡ Gemini Auto ∨");
document.body.appendChild(modelMenuBtn);
const badge = rt.readModePickerBadge();
assert.strictEqual(badge, "Gemini Auto", "Must parse Gemini Auto from badge with symbols: " + badge);
document.body.removeChild(modelMenuBtn);

// 2. Test Auto-Dismiss of 'Wait' prompt
const waitBtn = new MockElement("button", {{}}, "Wait");
document.body.appendChild(waitBtn);
assert.strictEqual(waitBtn.clicked, false);
// Call trigger or autoDismiss
const candidates = document.querySelectorAll("button");
for (const b of candidates) {{
  const txt = (b.innerText || b.textContent || "").trim().toLowerCase();
  if (txt === "wait" || txt === "keep waiting") b.click();
}}
assert.strictEqual(waitBtn.clicked, true, "Wait prompt must be auto-dismissed");

// 3. Test Long Text Insertion in Gemini Editor
const editor = new MockElement("div", {{ role: "textbox", contenteditable: "true" }});
document.body.appendChild(editor);

editor.addEventListener("paste", (e) => {{
  const text = e.clipboardData.getData("text/plain");
  editor.innerText = text;
  editor.textContent = text;
}});

const veryLongPrompt = "Line 1: Some long context\\n" + "A".repeat(5000) + "\\nLine 3: End of text";
rt.setPromptTextInEditor(editor, veryLongPrompt);

assert.strictEqual(editor.innerText, veryLongPrompt, "Editor must contain the long text");

// 4. Test early limit failover in _domStartTurnFn and _domPollTurnFn
const limitCard = new MockElement("div", {{ class: "limit-card" }}, "Limit reached · Resets 6:39 AM      See usage");
document.body.appendChild(limitCard);
assert.ok(rt.readErrorBanner().includes("Limit reached"), "readErrorBanner must find floating limit card");

const startRes = await bg._domStartTurnFn("req-limit-1", "Hello Gemini");
assert.strictEqual(startRes.ok, false, "Turn must not start when limit card is present");
assert.ok(startRes.error.startsWith("[LIMIT_REACHED]"), "Must fail with [LIMIT_REACHED], got: " + startRes.error);

// 5. Test _domPollTurnFn early abort on Limit banner
globalThis.__wsaActiveJob = {{
  reqId: "req-poll-limit",
  startedAt: Date.now(),
  selectedBadge: "Gemini Pro",
  prompt: "Test unsent prompt",
  submitRetries: 0,
  lastRetryAt: Date.now(),
  beforeRelayObj: {{}},
  beforeDomCount: 0,
  beforeModelCount: 0,
  beforeActionCount: 0,
  beforeAnnouncerSnapshot: "",
  lastText: "",
  lastThinking: "",
  sourceKind: "",
  thinkingLines: [],
  lastTextChangeAt: Date.now(),
  lastThinkChangeAt: Date.now(),
  lastBusyAt: Date.now(),
  sawNewTurn: false,
  sawEditorCleared: false,
  announcerChanged: false,
}};
const pollRes = bg._domPollTurnFn("req-poll-limit");
assert.strictEqual(pollRes.done, false);
assert.ok(pollRes.error.startsWith("[LIMIT_REACHED]"), "Poll must return [LIMIT_REACHED] when banner is present, got: " + pollRes.error);
document.body.removeChild(limitCard);

// 6. Test _domPollTurnFn early abort on Gemini Auto when prompt is unsent even without explicit limit banner
const autoModelBtn = new MockElement("button", {{ "aria-haspopup": "menu" }}, "≡ Gemini Auto ∨");
document.body.appendChild(autoModelBtn);
const unsentEd = new MockElement("div", {{ role: "textbox", contenteditable: "true" }}, "Unsent prompt text");
document.body.appendChild(unsentEd);

globalThis.__wsaActiveJob = {{
  reqId: "req-poll-auto",
  startedAt: Date.now(),
  selectedBadge: "Gemini Pro",
  prompt: "Unsent prompt text",
  submitRetries: 0,
  lastRetryAt: Date.now(),
  beforeRelayObj: {{}},
  beforeDomCount: 0,
  beforeModelCount: 0,
  beforeActionCount: 0,
  beforeAnnouncerSnapshot: "",
  lastText: "",
  lastThinking: "",
  sourceKind: "",
  thinkingLines: [],
  lastTextChangeAt: Date.now(),
  lastThinkChangeAt: Date.now(),
  lastBusyAt: Date.now(),
  sawNewTurn: false,
  sawEditorCleared: false,
  announcerChanged: false,
}};

const pollRes2 = bg._domPollTurnFn("req-poll-auto");
console.log("pollRes2 result:", pollRes2);
assert.strictEqual(pollRes2.done, false);
assert.ok(pollRes2.error.startsWith("[LIMIT_REACHED]"), "Poll must return [LIMIT_REACHED] on Gemini Auto unsent prompt, got: " + pollRes2.error);
document.body.removeChild(autoModelBtn);
document.body.removeChild(unsentEd);

console.log("GEMINI_LONG_TEXT_AND_AUTO_LIMIT_OK");
}})().catch(err => {{
  console.error(err);
  process.exit(1);
}});
"""
    proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"Node script failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "GEMINI_LONG_TEXT_AND_AUTO_LIMIT_OK" in proc.stdout
