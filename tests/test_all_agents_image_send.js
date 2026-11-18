/**
 * Comprehensive verification of image attachment and submission across all 5 web agents:
 * 1. Gemini Web: image attachment, spinner wait, send button click.
 * 2. ChatGPT Web: image attachment outside input, preview detection, send button click.
 * 3. Claude Web: image attachment via fileInput/paste, preview shelf detection, send button click.
 * 4. Venice Web: image attachment into dropzone, preview detection, submit button click.
 * 5. DeepSeek Web: text-only model gracefully handles prompt with image, enables circle send button.
 */

const assert = require("assert");
const fs = require("fs");
const path = require("path");

class MockFile {
  constructor(parts, name, opts = {}) {
    this.name = name;
    this.type = opts.type || "image/png";
    this.size = parts.reduce((acc, p) => acc + (p.length || 0), 0);
  }
}

class MockDataTransfer {
  constructor() {
    this.files = [];
    this.items = {
      add: (f) => this.files.push(f),
    };
  }
}

globalThis.File = MockFile;
globalThis.DataTransfer = MockDataTransfer;
if (typeof Event === "undefined") {
  globalThis.Event = class Event {
    constructor(type, opts = {}) {
      this.type = type;
      this.bubbles = Boolean(opts.bubbles);
      this.cancelable = Boolean(opts.cancelable);
      this.composed = Boolean(opts.composed);
    }
  };
}
if (typeof ClipboardEvent === "undefined") {
  globalThis.ClipboardEvent = class ClipboardEvent extends globalThis.Event {
    constructor(type, opts = {}) {
      super(type, opts);
      this.clipboardData = opts.clipboardData || new MockDataTransfer();
    }
  };
}
if (typeof DragEvent === "undefined") {
  globalThis.DragEvent = class DragEvent extends globalThis.Event {
    constructor(type, opts = {}) {
      super(type, opts);
      this.dataTransfer = opts.dataTransfer || new MockDataTransfer();
    }
  };
}
if (typeof InputEvent === "undefined") {
  globalThis.InputEvent = class InputEvent extends globalThis.Event {
    constructor(type, opts = {}) {
      super(type, opts);
      this.inputType = opts.inputType || "insertText";
      this.data = opts.data || "";
    }
  };
}

class MockElement {
  constructor(tag, attrs = {}, text = "") {
    this.tagName = String(tag || "div").toUpperCase();
    this.attributes = { ...attrs };
    this.className = attrs.class || attrs.className || "";
    this.classList = {
      _classes: new Set(this.className.split(/\s+/).filter(Boolean)),
      contains: (c) => this.classList._classes.has(c),
      add: (c) => this.classList._classes.add(c),
      remove: (c) => this.classList._classes.delete(c),
    };
    this.innerText = text;
    this.textContent = text;
    this.value = attrs.value || "";
    this.disabled = Boolean(attrs.disabled);
    this.childNodes = [];
    this.parentElement = null;
    this.listeners = {};
    this.dispatchedEvents = [];
    this.style = {};
    this.isConnected = true;
    this.offsetParent = {};
  }
  get isContentEditable() {
    return this.getAttribute("contenteditable") === "true";
  }
  getAttribute(n) { return this.attributes[n] !== undefined ? this.attributes[n] : null; }
  setAttribute(n, v) { this.attributes[n] = String(v); }
  hasAttribute(n) { return this.attributes[n] !== undefined; }
  removeAttribute(n) { delete this.attributes[n]; }
  appendChild(c) { c.parentElement = this; this.childNodes.push(c); return c; }
  removeChild(c) {
    const idx = this.childNodes.indexOf(c);
    if (idx !== -1) { c.parentElement = null; this.childNodes.splice(idx, 1); }
    return c;
  }
  closest(sel) {
    let cur = this;
    while (cur) {
      if (cur._matchesSel && cur._matchesSel(sel)) return cur;
      cur = cur.parentElement;
    }
    return null;
  }
  querySelectorAll(sel) {
    const res = [];
    const walk = (n) => {
      for (const c of n.childNodes) {
        if (c._matchesSel(sel)) res.push(c);
        walk(c);
      }
    };
    walk(this);
    return res;
  }
  querySelector(sel) {
    const all = this.querySelectorAll(sel);
    return all.length ? all[0] : null;
  }
  _matchesSel(sel) {
    if (!sel) return false;
    if (sel.includes(",")) {
      return sel.split(",").some(part => this._matchesSel(part.trim()));
    }
    const clean = sel.trim().replace(/\s+i\]/g, "]");
    const tagMatch = clean.match(/^[a-zA-Z0-9_-]+/);
    if (tagMatch) {
      if (this.tagName.toLowerCase() !== tagMatch[0].toLowerCase()) return false;
    }
    const idMatch = clean.match(/#([a-zA-Z0-9_-]+)/);
    if (idMatch) {
      if (this.getAttribute("id") !== idMatch[1]) return false;
    }
    const classMatches = clean.matchAll(/\.([a-zA-Z0-9_-]+)/g);
    for (const cm of classMatches) {
      if (!this.classList.contains(cm[1])) return false;
    }
    const attrMatches = clean.matchAll(/\[([a-zA-Z0-9_-]+)(?:([*^$]?=)(?:'([^']*)'|"([^"]*)"|([^\]]*)))?\]/g);
    for (const am of attrMatches) {
      const attrName = am[1];
      const op = am[2];
      const expectedVal = am[3] !== undefined ? am[3] : (am[4] !== undefined ? am[4] : am[5]);
      const actualVal = this.getAttribute(attrName);
      if (actualVal === null || actualVal === undefined) return false;
      if (op === "=") {
        if (actualVal.toLowerCase() !== expectedVal.toLowerCase()) return false;
      } else if (op === "*=") {
        if (!actualVal.toLowerCase().includes(expectedVal.toLowerCase())) return false;
      } else if (op === "^=") {
        if (!actualVal.toLowerCase().startsWith(expectedVal.toLowerCase())) return false;
      } else if (op === "$=") {
        if (!actualVal.toLowerCase().endsWith(expectedVal.toLowerCase())) return false;
      }
    }
    return true;
  }
  addEventListener(t, fn) {
    if (!this.listeners[t]) this.listeners[t] = [];
    this.listeners[t].push(fn);
  }
  dispatchEvent(e) {
    this.dispatchedEvents.push(e.type);
    for (const h of (this.listeners[e.type] || [])) h(e);
    return true;
  }
  click() { this.dispatchEvent({ type: "click", bubbles: true }); }
  focus() { this.dispatchEvent({ type: "focus", bubbles: true }); }
  select() {}
  getBoundingClientRect() { return { left: 10, top: 10, width: 60, height: 40 }; }
}

const redPngBase64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
const sampleImages = [{
  data: redPngBase64,
  mime_type: "image/png",
  filename: "test_ui_shot.png"
}];

const bg = require("../wsa-browser-bridge/background.js");

async function testGemini() {
  console.log("--> Testing Gemini Web Image Send...");
  const root = new MockElement("div");
  const form = new MockElement("form", { class: "composer-form" });
  const rich = new MockElement("rich-textarea");
  const editor = new MockElement("div", { role: "textbox", contenteditable: "true" });
  const fileInput = new MockElement("input", { type: "file", accept: "image/*" });
  const sendBtn = new MockElement("button", { class: "send-button mat-mdc-button-base", "aria-label": "Send message" });

  rich.appendChild(editor);
  form.appendChild(fileInput);
  form.appendChild(rich);
  form.appendChild(sendBtn);
  root.appendChild(form);

  globalThis.document = root;
  globalThis.window = { location: { href: "https://gemini.google.com/app" } };
  globalThis.Element = MockElement;

  fileInput.addEventListener("change", () => {
    // When file change is dispatched, Gemini mounts image thumbnail
    const thumb = new MockElement("img", { src: "blob:gemini-preview-1" });
    rich.appendChild(thumb);
  });

  bg._domInstallPageRuntimeFn();
  const rt = globalThis.__wsaRuntime;
  assert.ok(rt, "Gemini runtime must install");

  // Attach image to Gemini
  const res = await rt.attachImagesToPage(sampleImages, editor);
  assert.strictEqual(res.verified, true, "Gemini image upload must verify");
  assert.strictEqual(res.ok, true);

  // Verify send button detection & submit
  sendBtn.dispatchedEvents = [];
  rt.triggerDomSubmit(editor, false);
  assert.ok(sendBtn.dispatchedEvents.includes("click"), "Gemini send button must be clicked");
  console.log("   [PASS] Gemini Web verified.");
}

async function testChatGPT() {
  console.log("--> Testing ChatGPT Web Image Send...");
  const root = new MockElement("div");
  const form = new MockElement("form", { class: "composer-parent" });
  const composerBox = new MockElement("div", { "data-testid": "composer-background" });
  const editor = new MockElement("div", { id: "prompt-textarea", role: "textbox", contenteditable: "true" });
  const fileInput = new MockElement("input", { type: "file", accept: "image/*" });
  const sendBtn = new MockElement("button", { "data-testid": "send-button", "aria-label": "Send prompt" });

  form.appendChild(composerBox);
  composerBox.appendChild(fileInput);
  composerBox.appendChild(editor);
  composerBox.appendChild(sendBtn);
  root.appendChild(form);

  globalThis.document = root;
  globalThis.window = { location: { href: "https://chatgpt.com/" } };
  globalThis.Element = MockElement;

  fileInput.addEventListener("change", () => {
    // Attachment preview shelf mounts above editor in composerBox
    const preview = new MockElement("div", { "data-testid": "attachment-thumbnail" });
    const img = new MockElement("img", { src: "blob:chatgpt-card-1" });
    const removeBtn = new MockElement("button", { "aria-label": "Remove file" });
    preview.appendChild(img);
    preview.appendChild(removeBtn);
    composerBox.appendChild(preview);
  });

  bg._installWsaDomUtilsFn();
  bg._chatgptInstallPageRuntimeFn();
  const rt = globalThis.__wsaChatgptRuntime;
  const u = globalThis.__wsaDomUtils;
  assert.ok(rt, "ChatGPT runtime must install");

  // Start turn with image
  sendBtn.dispatchedEvents = [];
  const startRes = await bg._chatgptStartTurnFn("cg-1", "Inspect UI", sampleImages);
  console.log("ChatGPT startRes:", startRes);
  assert.strictEqual(startRes.ok, true, "ChatGPT start turn must succeed with image");
  assert.strictEqual(startRes.images_attached, true, "ChatGPT must attach image");
  assert.ok(sendBtn.dispatchedEvents.includes("click"), "ChatGPT send button must be clicked");
  console.log("   [PASS] ChatGPT Web verified.");
}

async function testClaude() {
  console.log("--> Testing Claude Web Image Send...");
  const root = new MockElement("div");
  const fieldset = new MockElement("fieldset", { class: "composer-box" });
  const editor = new MockElement("div", { class: "ProseMirror", contenteditable: "true", "aria-label": "Write your prompt to Claude" });
  const fileInput = new MockElement("input", { type: "file", accept: "image/*" });
  const sendBtn = new MockElement("button", { "aria-label": "Send message" });

  fieldset.appendChild(fileInput);
  fieldset.appendChild(editor);
  fieldset.appendChild(sendBtn);
  root.appendChild(fieldset);

  globalThis.document = root;
  globalThis.window = { location: { href: "https://claude.ai/chat/new" } };
  globalThis.Element = MockElement;

  fileInput.addEventListener("change", () => {
    // Claude preview card mounts in fieldset
    const card = new MockElement("div", { class: "attachment-preview", "aria-label": "test_ui_shot.png" });
    const rm = new MockElement("button", { "aria-label": "Remove file" });
    card.appendChild(rm);
    fieldset.appendChild(card);
  });

  bg._installWsaDomUtilsFn();
  bg._claudeInstallPageRuntimeFn();
  const rt = globalThis.__wsaClaudeRuntime;
  assert.ok(rt, "Claude runtime must install");

  sendBtn.dispatchedEvents = [];
  const startRes = await bg._claudeStartTurnFn("cl-1", "Inspect design", sampleImages);
  assert.strictEqual(startRes.ok, true, "Claude start turn must succeed with image");
  assert.strictEqual(startRes.images_attached, true, "Claude must attach image");
  assert.ok(sendBtn.dispatchedEvents.includes("click"), "Claude send button must be clicked");
  console.log("   [PASS] Claude Web verified.");
}

async function testVenice() {
  console.log("--> Testing Venice Web Image Send...");
  const root = new MockElement("div");
  const form = new MockElement("form");
  const dropzone = new MockElement("div", { "aria-label": "Mind chat attachment dropzone" });
  const editor = new MockElement("textarea", { "data-slot": "composer-field" });
  const fileInput = new MockElement("input", { type: "file", accept: "image/*" });
  const submitBtn = new MockElement("button", { "aria-label": "Submit chat" });

  dropzone.appendChild(fileInput);
  dropzone.appendChild(editor);
  dropzone.appendChild(submitBtn);
  form.appendChild(dropzone);
  root.appendChild(form);

  globalThis.document = root;
  globalThis.window = { location: { href: "https://venice.ai/chat" } };
  globalThis.Element = MockElement;

  fileInput.addEventListener("change", () => {
    // Venice mounts preview card
    const preview = new MockElement("div", { class: "attachment-preview" });
    const img = new MockElement("img", { src: "blob:venice-image-1" });
    preview.appendChild(img);
    dropzone.appendChild(preview);
  });

  bg._installWsaDomUtilsFn();
  bg._veniceInstallPageRuntimeFn();
  const rt = globalThis.__wsaVeniceRuntime;
  assert.ok(rt, "Venice runtime must install");

  submitBtn.dispatchedEvents = [];
  const startRes = await bg._veniceStartTurnFn("vn-1", "Explain this design", sampleImages);
  assert.strictEqual(startRes.ok, true, "Venice start turn must succeed with image");
  assert.strictEqual(startRes.images_attached, true, "Venice must attach image");
  assert.ok(submitBtn.dispatchedEvents.includes("click"), "Venice submit button must be clicked");
  console.log("   [PASS] Venice Web verified.");
}

async function testDeepSeek() {
  console.log("--> Testing DeepSeek Web Graceful Text-Only Bypass...");
  const root = new MockElement("div");
  const form = new MockElement("div", { class: "chat-input" });
  const editor = new MockElement("textarea", { id: "chat-input" });
  const sendBtn = new MockElement("button", { class: "ds-button--primary", "aria-label": "Send" });

  form.appendChild(editor);
  form.appendChild(sendBtn);
  root.appendChild(form);

  globalThis.document = root;
  globalThis.window = { location: { href: "https://chat.deepseek.com/" } };
  globalThis.Element = MockElement;

  bg._installWsaDomUtilsFn();
  bg._deepseekInstallPageRuntimeFn();
  const rt = globalThis.__wsaDeepseekRuntime;
  assert.ok(rt, "DeepSeek runtime must install");

  // DeepSeek is text-only: it must NOT fail with [IMAGE_ATTACH_FAILED]
  sendBtn.dispatchedEvents = [];
  const startRes = await bg._deepseekStartTurnFn("ds-1", "Solve this problem", sampleImages);
  assert.strictEqual(startRes.ok, true, "DeepSeek must succeed without crashing on images");
  assert.strictEqual(startRes.images_attached, false, "DeepSeek marks images as not attached (text fallback)");
  assert.ok(sendBtn.dispatchedEvents.includes("click"), "DeepSeek send button must be clicked");
  console.log("   [PASS] DeepSeek Web verified.");
}

async function main() {
  console.log("=================================================");
  console.log("VERIFYING IMAGE SEND TO ALL 5 WEB AGENTS IN WSA");
  console.log("=================================================");
  await testGemini();
  await testChatGPT();
  await testClaude();
  await testVenice();
  await testDeepSeek();
  console.log("=================================================");
  console.log("ALL 5 WEB AGENTS VERIFIED SUCCESSFULLY!");
  console.log("=================================================");
}

main().catch(err => {
  console.error("FATAL ERROR IN MULTI-AGENT VERIFICATION:", err);
  process.exit(1);
});
