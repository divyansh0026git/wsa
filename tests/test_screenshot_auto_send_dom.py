"""
Verification test suite for screenshot/image auto-send DOM across all 5 web agents:
Gemini, Claude, ChatGPT, DeepSeek, and Venice.

Ensures that when screenshots or images are uploaded/pasted into the composer:
1. The Send button is accurately detected and not disqualified by media/attachment keywords.
2. Active upload spinners (including Angular Material mat-progress-spinner/mat-spinner) are detected.
3. Full pointerdown -> mousedown -> pointerup -> mouseup -> click event sequences are dispatched.
4. Any stuck media/screenshot in the composer triggers automated submission recovery during polling.
5. Attachment picker buttons (e.g. 'Attach file', 'Upload image') are cleanly rejected.
"""

import json
import subprocess
from pathlib import Path


def _get_bg_path():
    return (Path(__file__).resolve().parent.parent / "wsa-browser-bridge" / "background.js").as_posix()


def test_gemini_screenshot_auto_send_and_spinner_detection():
    """Verify Gemini detects send button with attached image, detects mat-spinners, and retries stuck media."""
    bg_path = _get_bg_path()
    js = f"""
const assert = require("assert");

class MockNode {{
  constructor(tag, attrs = {{}}, text = "") {{
    this.tagName = String(tag || "div").toUpperCase();
    this.attributes = {{ ...attrs }};
    this.className = attrs.class || attrs.className || "";
    this.classList = {{
      _classes: new Set(this.className.split(/\\s+/).filter(Boolean)),
      contains: (c) => this.classList._classes.has(c),
      add: (c) => this.classList._classes.add(c),
    }};
    this.innerText = text;
    this.textContent = text;
    this.childNodes = [];
    this.parentElement = null;
    this.listeners = {{}};
    this.dispatchedEvents = [];
    this.style = {{}};
  }}
  getAttribute(n) {{ return this.attributes[n] !== undefined ? this.attributes[n] : null; }}
  setAttribute(n, v) {{ this.attributes[n] = String(v); }}
  hasAttribute(n) {{ return this.attributes[n] !== undefined; }}
  appendChild(c) {{ c.parentElement = this; this.childNodes.push(c); return c; }}
  closest(sel) {{
    let cur = this;
    while (cur) {{
      if (cur._matches(sel)) return cur;
      cur = cur.parentElement;
    }}
    return null;
  }}
  _matches(sel) {{
    const s = sel.toLowerCase();
    if (s.includes("form") && this.tagName === "FORM") return true;
    if (s.includes("rich-textarea") && this.tagName === "RICH-TEXTAREA") return true;
    return false;
  }}
  querySelectorAll(sel) {{
    const res = [];
    const walk = (n) => {{
      for (const c of n.childNodes) {{
        if (c._matchesSel(sel)) res.push(c);
        walk(c);
      }}
    }};
    walk(this);
    return res;
  }}
  querySelector(sel) {{
    const all = this.querySelectorAll(sel);
    return all.length ? all[0] : null;
  }}
  _matchesSel(sel) {{
    const s = sel.toLowerCase();
    const tag = this.tagName.toLowerCase();
    if (s.includes("stop")) {{
      return tag === "button" && (this.getAttribute("aria-label") || "").toLowerCase().includes("stop");
    }}
    if (s.includes("img") && tag === "img") return true;
    if (s.includes("mat-progress-spinner") && tag === "mat-progress-spinner") return true;
    if (s.includes("textbox") && this.getAttribute("role") === "textbox") return true;
    if (s.includes("send-button") && this.classList.contains("send-button")) return true;
    if (s.includes("button") && (tag === "button" || this.getAttribute("role") === "button")) return true;
    return false;
  }}
  addEventListener(t, fn) {{
    if (!this.listeners[t]) this.listeners[t] = [];
    this.listeners[t].push(fn);
  }}
  dispatchEvent(e) {{
    this.dispatchedEvents.push(e.type);
    for (const h of (this.listeners[e.type] || [])) h(e);
    return true;
  }}
  click() {{ this.dispatchEvent({{ type: "click" }}); }}
  getBoundingClientRect() {{ return {{ left: 10, top: 10, width: 50, height: 50 }}; }}
}}

const root = new MockNode("div");
const form = new MockNode("form", {{ class: "composer-form" }});
const rich = new MockNode("rich-textarea");
const editor = new MockNode("div", {{ role: "textbox", contenteditable: "true" }}, "Describe this UI screenshot");
const attachPickerBtn = new MockNode("button", {{ "aria-label": "Add image to prompt" }});
const sendBtn = new MockNode("button", {{
  class: "send-button mat-mdc-button-base",
  "data-test-id": "send-button",
  "aria-label": "Send prompt (with 1 image attached)",
}});

rich.appendChild(editor);
form.appendChild(attachPickerBtn);
form.appendChild(rich);
form.appendChild(sendBtn);
root.appendChild(form);

globalThis.document = root;
globalThis.window = {{ location: {{ href: "https://gemini.google.com/app" }} }};
globalThis.Element = MockNode;

const bg = require({json.dumps(bg_path)});
bg._domInstallPageRuntimeFn();
const rt = globalThis.__wsaRuntime;
assert.ok(rt, "Gemini runtime must be installed");

// 1. Send candidate must find the send button even when aria-label mentions 'image'
const found = rt.findEnabledSendButton(editor);
assert.strictEqual(found, sendBtn, "Must find send-button even with 'image' in aria-label");
assert.notStrictEqual(found, attachPickerBtn, "Must not choose Add image picker button");

// 2. hasActiveUploadSpinner detects Angular Material progress spinner
const spinner = new MockNode("mat-progress-spinner", {{ role: "progressbar" }});
rich.appendChild(spinner);
assert.strictEqual(rt.hasActiveUploadSpinner(form), true, "Must detect mat-progress-spinner");
rich.childNodes = rich.childNodes.filter(c => c !== spinner);
assert.strictEqual(rt.hasActiveUploadSpinner(form), false, "Must clear when spinner removed");

// 3. triggerDomSubmit dispatches full pointer/mouse event sequence on sendBtn
sendBtn.dispatchedEvents = [];
rt.triggerDomSubmit(editor, false);
assert.ok(sendBtn.dispatchedEvents.includes("pointerdown"), "Must dispatch pointerdown");
assert.ok(sendBtn.dispatchedEvents.includes("mousedown"), "Must dispatch mousedown");
assert.ok(sendBtn.dispatchedEvents.includes("mouseup"), "Must dispatch mouseup");
assert.ok(sendBtn.dispatchedEvents.includes("click"), "Must dispatch click");

// 4. Polling automatically retries submission when stuck media preview is in composer
const imgPreview = new MockNode("img", {{ src: "blob:gemini-shot-123" }});
form.appendChild(imgPreview);
assert.ok(rt.countComposerMediaPreviews(form) > 0, "Composer must have media preview");

globalThis.__wsaActiveJob = {{
  reqId: "gm-shot-1",
  startedAt: Date.now() - 3000,
  prompt: "Inspect screenshot",
  submitRetries: 0,
  lastRetryAt: Date.now() - 3000,
  beforeRelayObj: {{}},
  beforeDomCount: 0,
  beforeModelCount: 0,
  beforeActionCount: 0,
  lastText: "",
  lastThinking: "",
  thinkingLines: [],
  sawNewTurn: false,
}};

sendBtn.dispatchedEvents = [];
rt.pollTurn = bg._domPollTurnFn;
const pollRes = bg._domPollTurnFn("gm-shot-1");
assert.strictEqual(pollRes.done, false, "Poll turn still active");
assert.strictEqual(globalThis.__wsaActiveJob.submitRetries, 1, "Must have retried submit for stuck image");
assert.ok(sendBtn.dispatchedEvents.includes("click"), "Send button must be clicked during retry");

console.log("GEMINI_SCREENSHOT_AUTO_SEND_OK");
"""
    proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_claude_screenshot_auto_send_and_retry():
    """Verify Claude detects send button with attached image, dispatches pointer sequence, and retries stuck media."""
    bg_path = _get_bg_path()
    js = f"""
const assert = require("assert");

class MockNode {{
  constructor(tag, attrs = {{}}, text = "") {{
    this.tagName = String(tag || "div").toUpperCase();
    this.attributes = {{ ...attrs }};
    this.className = attrs.class || attrs.className || "";
    this.classList = {{
      _classes: new Set(this.className.split(/\\s+/).filter(Boolean)),
      contains: (c) => this.classList._classes.has(c),
      add: (c) => this.classList._classes.add(c),
    }};
    this.innerText = text;
    this.textContent = text;
    this.childNodes = [];
    this.parentElement = null;
    this.listeners = {{}};
    this.dispatchedEvents = [];
    this.style = {{}};
  }}
  getAttribute(n) {{ return this.attributes[n] !== undefined ? this.attributes[n] : null; }}
  setAttribute(n, v) {{ this.attributes[n] = String(v); }}
  hasAttribute(n) {{ return this.attributes[n] !== undefined; }}
  appendChild(c) {{ c.parentElement = this; this.childNodes.push(c); return c; }}
  closest(sel) {{
    let cur = this;
    while (cur) {{
      if (cur._matches(sel)) return cur;
      cur = cur.parentElement;
    }}
    return null;
  }}
  _matches(sel) {{
    const s = sel.toLowerCase();
    if (s.includes("form") && this.tagName === "FORM") return true;
    if (s.includes("composer") && this.className.includes("composer")) return true;
    return false;
  }}
  querySelectorAll(sel) {{
    const res = [];
    const walk = (n) => {{
      for (const c of n.childNodes) {{
        if (c._matchesSel(sel)) res.push(c);
        walk(c);
      }}
    }};
    walk(this);
    return res;
  }}
  querySelector(sel) {{
    const all = this.querySelectorAll(sel);
    return all.length ? all[0] : null;
  }}
  _matchesSel(sel) {{
    const s = sel.toLowerCase();
    const tag = this.tagName.toLowerCase();
    if (s.includes("stop")) {{
      return tag === "button" && (this.getAttribute("aria-label") || "").toLowerCase().includes("stop");
    }}
    if (s.includes("img") && tag === "img") return true;
    if (s.includes("chat-input") && (this.getAttribute("data-testid") || "").includes("chat-input")) return true;
    if (s.includes("send-button") && (this.getAttribute("data-testid") || "").includes("send-button")) return true;
    if (s.includes("button") && (tag === "button" || this.getAttribute("role") === "button")) return true;
    return false;
  }}
  addEventListener(t, fn) {{
    if (!this.listeners[t]) this.listeners[t] = [];
    this.listeners[t].push(fn);
  }}
  dispatchEvent(e) {{
    this.dispatchedEvents.push(e.type);
    for (const h of (this.listeners[e.type] || [])) h(e);
    return true;
  }}
  click() {{ this.dispatchEvent({{ type: "click" }}); }}
  getBoundingClientRect() {{ return {{ left: 10, top: 10, width: 50, height: 50 }}; }}
}}

const clRoot = new MockNode("div");
const clForm = new MockNode("form", {{ class: "composer" }});
const clEd = new MockNode("div", {{ "data-testid": "chat-input", contenteditable: "true", role: "textbox" }}, "Review UI");
const clAttach = new MockNode("button", {{ "aria-label": "Attach file or image" }});
const clSend = new MockNode("button", {{ "data-testid": "send-button", "aria-label": "Send Message (with image)" }});
const clImg = new MockNode("img", {{ src: "blob:claude-shot" }});

clForm.appendChild(clAttach);
clForm.appendChild(clEd);
clForm.appendChild(clSend);
clForm.appendChild(clImg);
clRoot.appendChild(clForm);

globalThis.document = clRoot;
globalThis.window = {{ location: {{ href: "https://claude.ai/chat/c1" }} }};
globalThis.location = globalThis.window.location;
globalThis.Element = MockNode;

const bg = require({json.dumps(bg_path)});
bg._installWsaDomUtilsFn();
bg._claudeInstallPageRuntimeFn();
const clRt = globalThis.__wsaClaudeRuntime;
assert.ok(clRt, "Claude runtime must be installed");

// 1. Send button found even if aria mentions image
const found = clRt.findSendButton(clEd);
assert.strictEqual(found, clSend, "Must find send button with attachment meta");
assert.notStrictEqual(found, clAttach, "Must not choose attach button");

// 2. triggerClaudeSubmit dispatches pointerdown, mousedown, mouseup, click
clSend.dispatchedEvents = [];
clRt.triggerSubmit(clEd, false);
assert.ok(clSend.dispatchedEvents.includes("pointerdown"), "Must dispatch pointerdown");
assert.ok(clSend.dispatchedEvents.includes("click"), "Must dispatch click");

// 3. Claude polling retries when attachment preview is present
clRt.turns["cl-shot-1"] = {{
  startedAt: Date.now() - 3000,
  lastChangeAt: Date.now() - 3000,
  lastRetryAt: Date.now() - 3000,
  lastText: "",
  lastThinking: "",
  prompt: "Review UI",
  submitRetries: 0,
  beforeTurnCount: 0,
  sawActivity: false,
  sawStop: false,
}};

clSend.dispatchedEvents = [];
const clPoll = bg._claudePollTurnFn("cl-shot-1");
assert.strictEqual(clPoll.done, false, "Claude poll turn still active");
assert.strictEqual(clRt.turns["cl-shot-1"].submitRetries, 1, "Must retry submit for stuck Claude attachment");
assert.ok(clSend.dispatchedEvents.includes("click"), "Send button must click on retry");

console.log("CLAUDE_SCREENSHOT_AUTO_SEND_OK");
"""
    proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_chatgpt_deepseek_venice_screenshot_auto_send():
    """Verify ChatGPT, DeepSeek, and Venice detect send buttons with media and retry stuck composer images."""
    bg_path = _get_bg_path()
    js = f"""
const assert = require("assert");

class MockNode {{
  constructor(tag, attrs = {{}}, text = "") {{
    this.tagName = String(tag || "div").toUpperCase();
    this.attributes = {{ ...attrs }};
    this.className = attrs.class || attrs.className || "";
    this.classList = {{
      _classes: new Set(this.className.split(/\\s+/).filter(Boolean)),
      contains: (c) => this.classList._classes.has(c),
      add: (c) => this.classList._classes.add(c),
    }};
    this.innerText = text;
    this.textContent = text;
    this.childNodes = [];
    this.parentElement = null;
    this.listeners = {{}};
    this.dispatchedEvents = [];
    this.style = {{}};
  }}
  getAttribute(n) {{ return this.attributes[n] !== undefined ? this.attributes[n] : null; }}
  setAttribute(n, v) {{ this.attributes[n] = String(v); }}
  hasAttribute(n) {{ return this.attributes[n] !== undefined; }}
  appendChild(c) {{ c.parentElement = this; this.childNodes.push(c); return c; }}
  closest(sel) {{
    let cur = this;
    while (cur) {{
      if (cur._matches(sel)) return cur;
      cur = cur.parentElement;
    }}
    return null;
  }}
  _matches(sel) {{
    const s = sel.toLowerCase();
    if (s.includes("form") && this.tagName === "FORM") return true;
    if (s.includes("composer") && this.className.includes("composer")) return true;
    return false;
  }}
  querySelectorAll(sel) {{
    const res = [];
    const walk = (n) => {{
      for (const c of n.childNodes) {{
        if (c._matchesSel(sel)) res.push(c);
        walk(c);
      }}
    }};
    walk(this);
    return res;
  }}
  querySelector(sel) {{
    const all = this.querySelectorAll(sel);
    return all.length ? all[0] : null;
  }}
  _matchesSel(sel) {{
    const s = sel.toLowerCase();
    const tag = this.tagName.toLowerCase();
    if (s.includes("stop")) {{
      return tag === "button" && (this.getAttribute("aria-label") || "").toLowerCase().includes("stop");
    }}
    if (s.includes("img") && tag === "img") return true;
    if (s.includes("composer-submit-button") && this.getAttribute("id") === "composer-submit-button") return true;
    if (s.includes("ds-button") && this.classList.contains("ds-button")) return true;
    if (s.includes("submit chat") && (this.getAttribute("aria-label") || "").toLowerCase().includes("submit chat")) return true;
    if (s.includes("button") && (tag === "button" || this.getAttribute("role") === "button")) return true;
    return false;
  }}
  addEventListener(t, fn) {{
    if (!this.listeners[t]) this.listeners[t] = [];
    this.listeners[t].push(fn);
  }}
  dispatchEvent(e) {{
    this.dispatchedEvents.push(e.type);
    for (const h of (this.listeners[e.type] || [])) h(e);
    return true;
  }}
  click() {{ this.dispatchEvent({{ type: "click" }}); }}
  getBoundingClientRect() {{ return {{ left: 10, top: 10, width: 50, height: 50 }}; }}
}}

const bg = require({json.dumps(bg_path)});

// 1. ChatGPT
{{
  const cgRoot = new MockNode("div");
  const cgForm = new MockNode("form", {{ class: "composer" }});
  const cgEd = new MockNode("div", {{ id: "prompt-textarea", contenteditable: "true" }}, "Chat prompt");
  const cgAttach = new MockNode("button", {{ "aria-label": "Upload files" }});
  const cgSend = new MockNode("button", {{ id: "composer-submit-button", "aria-label": "Send prompt with attached file" }});
  cgForm.appendChild(cgAttach);
  cgForm.appendChild(cgEd);
  cgForm.appendChild(cgSend);
  cgRoot.appendChild(cgForm);
  globalThis.document = cgRoot;
  globalThis.Element = MockNode;
  bg._installWsaDomUtilsFn();
  bg._chatgptInstallPageRuntimeFn();
  const cgRt = globalThis.__wsaChatgptRuntime;
  const found = cgRt.findSendButton(cgEd);
  assert.strictEqual(found, cgSend, "ChatGPT must find send button with attachment label");
  assert.notStrictEqual(found, cgAttach, "ChatGPT must not select upload files button");
}}

// 2. DeepSeek
{{
  const dsRoot = new MockNode("div");
  const dsForm = new MockNode("form", {{ class: "composer" }});
  const dsTa = new MockNode("textarea", {{ id: "chat-input" }});
  const dsUpload = new MockNode("button", {{ "aria-label": "Upload file" }});
  const dsSend = new MockNode("div", {{ role: "button", class: "ds-button ds-button--primary ds-button--circle", "aria-label": "Send with file" }});
  dsForm.appendChild(dsUpload);
  dsForm.appendChild(dsTa);
  dsForm.appendChild(dsSend);
  dsRoot.appendChild(dsForm);
  globalThis.document = dsRoot;
  bg._deepseekInstallPageRuntimeFn();
  const dsRt = globalThis.__wsaDeepseekRuntime;
  const found = dsRt.findSendButton(dsTa);
  assert.strictEqual(found, dsSend, "DeepSeek must find ds-button--primary even with file label");
}}

// 3. Venice
{{
  const vnRoot = new MockNode("div");
  const vnForm = new MockNode("form", {{ class: "composer" }});
  const vnTa = new MockNode("textarea", {{ "aria-label": "Chat message input" }});
  const vnAttach = new MockNode("button", {{ "aria-label": "Attach file or image" }});
  const vnSubmit = new MockNode("button", {{ "aria-label": "Submit chat (1 image)" }});
  vnForm.appendChild(vnAttach);
  vnForm.appendChild(vnTa);
  vnForm.appendChild(vnSubmit);
  vnRoot.appendChild(vnForm);
  globalThis.document = vnRoot;
  bg._veniceInstallPageRuntimeFn();
  const vnRt = globalThis.__wsaVeniceRuntime;
  const found = vnRt.findSendButton(vnTa);
  assert.strictEqual(found, vnSubmit, "Venice must find Submit chat button even with image in label");
  assert.notStrictEqual(found, vnAttach, "Venice must not select attach button");
}}

// 4. Generic pollTurn retry test (used by ChatGPT, DeepSeek, Venice)
{{
  const genRoot = new MockNode("div");
  const genForm = new MockNode("form", {{ class: "composer" }});
  const genEd = new MockNode("div", {{ id: "prompt-textarea", contenteditable: "true" }}, "Chat prompt");
  const genSend = new MockNode("button", {{ id: "composer-submit-button" }});
  const genImg = new MockNode("img", {{ src: "blob:generic-shot" }});
  genForm.appendChild(genEd);
  genForm.appendChild(genSend);
  genForm.appendChild(genImg);
  genRoot.appendChild(genForm);
  globalThis.document = genRoot;

  const u = globalThis.__wsaDomUtils;
  const dummyRt = {{
    findEditor: () => genEd,
    findSendButton: () => genSend,
    readAllTurns: () => [],
    readErrorBanner: () => "",
    readModelBadge: () => "ChatGPT",
    hasStopButton: () => false,
    getTurnCount: () => 0,
    readTurnText: () => "",
    isPromptStillInEditor: () => false,
    triggerSubmit: (ed, isRetry) => u.triggerSendSubmission(dummyRt, ed, isRetry),
    turns: {{
      "t-gen-1": {{
        startedAt: Date.now() - 3000,
        lastChangeAt: Date.now() - 3000,
        lastRetryAt: Date.now() - 3000,
        lastText: "",
        lastThinking: "",
        prompt: "Chat prompt",
        submitRetries: 0,
        beforeTurnCount: 0,
        sawActivity: false,
      }},
    }},
  }};

  genSend.dispatchedEvents = [];
  u.pollTurn(dummyRt, "t-gen-1", {{ label: "ChatGPT", defaultModel: "ChatGPT" }});
  assert.strictEqual(dummyRt.turns["t-gen-1"].submitRetries, 1, "Generic pollTurn must retry for stuck media");
  assert.ok(genSend.dispatchedEvents.includes("pointerdown"), "Pointerdown dispatched on retry");
  assert.ok(genSend.dispatchedEvents.includes("click"), "Send button must click on retry");
}}

console.log("ALL_GENERIC_PROVIDERS_SCREENSHOT_AUTO_SEND_OK");
"""
    proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
