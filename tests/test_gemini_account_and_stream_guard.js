const assert = require("assert");
const bg = require("../wsa-browser-bridge/background.js");

console.log("--> Testing hasIncompleteStreamText...");
// 1. Clean prose
assert.strictEqual(bg.hasIncompleteStreamText("Here is the answer."), false);
assert.strictEqual(bg.hasIncompleteStreamText("All 5 tests passed."), false);

// 2. Unclosed code fence (odd number of ```)
assert.strictEqual(bg.hasIncompleteStreamText("Here is code:\n```json\n{\"foo\": 1"), true);
assert.strictEqual(bg.hasIncompleteStreamText("```typescript\nconst x = 1;\n"), true);

// 3. Balanced code fences (even number of ```)
assert.strictEqual(bg.hasIncompleteStreamText("Here is code:\n```json\n{\"foo\": 1}\n```"), false);
assert.strictEqual(bg.hasIncompleteStreamText("```ts\nconst a = 1;\n```\nDone!"), false);

// 4. Incomplete tool calls (e.g. from user screenshot)
assert.strictEqual(
  bg.hasIncompleteStreamText('{"name": "apply_patch", "arguments": {"edits": [{"path": "src/shaders/cinematicPass.'),
  true
);
assert.strictEqual(bg.hasIncompleteStreamText('{"name": "'), true);
assert.strictEqual(bg.hasIncompleteStreamText('{"name": "read_file",'), true);
assert.strictEqual(bg.hasIncompleteStreamText('{"name"'), true);
assert.strictEqual(bg.hasIncompleteStreamText('{"'), true);
assert.strictEqual(bg.hasIncompleteStreamText('[{"'), true);
assert.strictEqual(bg.hasIncompleteStreamText('Code snippet\n'), true);
assert.strictEqual(bg.hasIncompleteStreamText('Code snippet\n{"'), true);
assert.strictEqual(bg.hasIncompleteStreamText('{"name": "read_file", "arguments": {"path": "main.ts"'), true);
assert.strictEqual(
  bg.hasIncompleteStreamText('```tool_call\n{"name": "read_file", "arguments": {"path": "a.ts"}}\n```\n\n```tool_call\n{"name": "read_file",\n```'),
  true
);
assert.strictEqual(
  bg.hasIncompleteStreamText('```tool_call\n{"name": "read_file", "arguments": {"path": "a.ts"}}\n```\n\n```\n{"\n```'),
  true
);

// 5. Complete tool call
assert.strictEqual(
  bg.hasIncompleteStreamText('{"name": "apply_patch", "arguments": {"edits": [{"path": "src/shaders/cinematicPass.ts"}]}}'),
  false
);
assert.strictEqual(
  bg.hasIncompleteStreamText('```tool_call\n{"name": "read_file", "arguments": {"path": "a.ts"}}\n```\n\n```tool_call\n{"name": "read_file", "arguments": {"path": "b.ts"}}\n```'),
  false
);

console.log("   [PASS] hasIncompleteStreamText passed.");

console.log("--> Testing pickBestTabForProvider with account switching...");
const mockTabs = [
  { id: 101, url: "https://gemini.google.com/app", active: false },
  { id: 102, url: "https://gemini.google.com/u/1/app/conv-1", active: false },
  { id: 103, url: "https://gemini.google.com/u/4/app/908339786d330325", active: true },
  { id: 104, url: "https://gemini.google.com/u/7/app/conv-7", active: false },
];

// Expected authUser "4" (devcormac73@gmail.com)
const chosen4 = bg.pickBestTabForProvider(mockTabs, "gemini", null, "", "4");
assert.strictEqual(chosen4.id, 103, "Should select tab 103 for auth user 4");

// Expected authUser "1"
const chosen1 = bg.pickBestTabForProvider(mockTabs, "gemini", null, "", "1");
assert.strictEqual(chosen1.id, 102, "Should select tab 102 for auth user 1");

// Expected authUser "0"
const chosen0 = bg.pickBestTabForProvider(mockTabs, "gemini", null, "", "0");
assert.strictEqual(chosen0.id, 101, "Should select tab 101 for default auth user 0");

// With savedSession convId on authUser "4"
const chosenConv = bg.pickBestTabForProvider(mockTabs, "gemini", { convId: "908339786d330325" }, "", "4");
assert.strictEqual(chosenConv.id, 103, "Should match convId on auth user 4");

console.log("   [PASS] pickBestTabForProvider multi-login routing passed.");

console.log("--> Testing evaluateTurnCompletionState incomplete stream hold...");
// Incomplete tool call: should NOT complete after 1500ms or 2300ms
const t0 = 1000000;
const incompleteTurnState = {
  text: '{"name": "apply_patch", "arguments": {"edits": [{"path": "src/shaders/cinematicPass.',
  stopVisible: false,
  reactBusy: false,
  hasTurnActions: true,
  sawNewTurn: true,
  now: t0 + 1500,
  lastTextChangeAt: t0,
  lastThinkChangeAt: t0,
  lastBusyAt: t0,
};

// We can test rt's evaluateTurnCompletionState by invoking _domInstallPageRuntimeFn in a mock window
const vm = require("vm");
const sandbox = {
  globalThis: {},
  document: {
    querySelector: () => null,
    querySelectorAll: () => [],
  },
  window: {},
  location: { href: "https://gemini.google.com/u/4/app" },
};
sandbox.globalThis = sandbox;
vm.runInNewContext("(" + bg._domInstallPageRuntimeFn.toString() + ")()", sandbox);
const rt = sandbox.__wsaRuntime;

assert.strictEqual(
  rt.evaluateTurnCompletionState(incompleteTurnState),
  false,
  "Incomplete JSON should hold turn open at 1500ms"
);

incompleteTurnState.now = t0 + 2500;
assert.strictEqual(
  rt.evaluateTurnCompletionState(incompleteTurnState),
  false,
  "Incomplete JSON should hold turn open at 2500ms even with hasTurnActions=true"
);

// Tool calls require 3000ms stability to prevent premature cutoff between code blocks
const completeTurnState = {
  text: '{"name": "apply_patch", "arguments": {"edits": [{"path": "src/shaders/cinematicPass.ts"}]}}',
  stopVisible: false,
  reactBusy: false,
  geminiBusy: false,
  hasTurnActions: true,
  sawNewTurn: true,
  now: t0 + 1500,
  lastTextChangeAt: t0,
  lastThinkChangeAt: t0,
  lastBusyAt: t0,
};
assert.strictEqual(
  rt.evaluateTurnCompletionState(completeTurnState),
  false,
  "Tool call should hold turn open at 1500ms to allow multi-block rendering"
);

completeTurnState.now = t0 + 3100;
assert.strictEqual(
  rt.evaluateTurnCompletionState(completeTurnState),
  true,
  "Completed JSON should complete normally once 3000ms stability threshold is reached"
);

// Plain prose without tool calls completes at 1500ms
const proseTurnState = {
  text: "Here is the summary of changes.",
  stopVisible: false,
  reactBusy: false,
  geminiBusy: false,
  hasTurnActions: true,
  sawNewTurn: true,
  now: t0 + 1500,
  lastTextChangeAt: t0,
  lastThinkChangeAt: t0,
  lastBusyAt: t0,
};
assert.strictEqual(
  rt.evaluateTurnCompletionState(proseTurnState),
  true,
  "Plain prose should complete at 1400ms threshold"
);

// geminiBusy holds turn open even if text is stable
proseTurnState.geminiBusy = true;
assert.strictEqual(
  rt.evaluateTurnCompletionState(proseTurnState),
  false,
  "geminiBusy must hold turn open"
);

// Fallback after 8s of sustained silence on incomplete JSON
incompleteTurnState.now = t0 + 8500;
assert.strictEqual(
  rt.evaluateTurnCompletionState(incompleteTurnState),
  true,
  "Incomplete JSON should fallback complete after 8s of sustained idle"
);

console.log("   [PASS] evaluateTurnCompletionState streaming protection passed.");

console.log("--> Testing in-flight streaming tool call recognition in rt.isToolCallPayload...");
assert.strictEqual(
  rt.isToolCallPayload('{"name": "apply_patch", "arguments": {"edits": [{"path": "src/shaders/cinematicPass.'),
  true,
  "In-flight streaming tool call should be recognized by isToolCallPayload"
);
assert.strictEqual(
  rt.isToolCallPayload('{"name": "read_file",'),
  true,
  "Partial tool call header should be recognized by isToolCallPayload"
);
assert.strictEqual(
  rt.isToolCallPayload('{"'),
  true,
  "Early opening brace-quote should be recognized by isToolCallPayload"
);
assert.strictEqual(
  rt.isToolCallPayload('Just regular prose without tool call'),
  false,
  "Prose should return false from isToolCallPayload"
);
console.log("   [PASS] isToolCallPayload streaming recognition passed.");

console.log("--> Testing triggerDomSubmit guard against clicking Stop button or busy generation...");
const mockStopDoc = {
  querySelector: (sel) => {
    if (sel.includes("stop")) return { disabled: false, getAttribute: () => null };
    return null;
  },
  querySelectorAll: (sel) => {
    if (sel.includes("stop")) return [{ disabled: false, getAttribute: () => null }];
    return [];
  },
};
const stopSandbox = {
  globalThis: {},
  document: mockStopDoc,
  window: {},
  location: { href: "https://gemini.google.com/u/4/app" },
};
stopSandbox.globalThis = stopSandbox;
vm.runInNewContext("(" + bg._domInstallPageRuntimeFn.toString() + ")()", stopSandbox);
const stopRt = stopSandbox.__wsaRuntime;

assert.strictEqual(
  stopRt.isStopButtonVisible(),
  true,
  "Mock stop button should be detected as visible"
);
assert.strictEqual(
  stopRt.detectGeminiBusy(),
  true,
  "detectGeminiBusy should be true when stop button is visible"
);
assert.strictEqual(
  stopRt.findEnabledSendButton(),
  null,
  "findEnabledSendButton must return null when Stop button is visible"
);
assert.strictEqual(
  stopRt.triggerDomSubmit({ isConnected: true }),
  "busy",
  "triggerDomSubmit must return 'busy' when stop button is visible"
);
console.log("   [PASS] triggerDomSubmit & findEnabledSendButton stop protection passed.");

console.log("--> Testing detectGeminiBusy with analyzing status & spinner...");
const mockAnalyzingDoc = {
  querySelector: (sel) => {
    if (sel.includes("spinner") || sel.includes("progressbar")) return { offsetWidth: 20, offsetHeight: 20 };
    return null;
  },
  querySelectorAll: (sel) => {
    if (sel.includes("stop")) return [];
    if (sel.includes("spinner") || sel.includes("progressbar")) return [{ offsetWidth: 20, offsetHeight: 20 }];
    if (sel.includes("thoughts-status-message") || sel.includes("bC5vW") || sel.includes("thought-view")) {
      return [{
        offsetWidth: 200,
        offsetHeight: 24,
        innerText: "... Analyzing visual details",
        textContent: "... Analyzing visual details",
        closest: () => null,
        matches: () => true,
        classList: { contains: () => false },
      }];
    }
    return [];
  },
};
const analyzingSandbox = {
  globalThis: {},
  document: mockAnalyzingDoc,
  window: {},
  location: { href: "https://gemini.google.com/u/4/app" },
};
analyzingSandbox.globalThis = analyzingSandbox;
vm.runInNewContext("(" + bg._domInstallPageRuntimeFn.toString() + ")()", analyzingSandbox);
const analyzingRt = analyzingSandbox.__wsaRuntime;

assert.strictEqual(
  analyzingRt.detectGeminiBusy(),
  true,
  "detectGeminiBusy should return true when analyzing status & spinner are present"
);
assert.strictEqual(
  analyzingRt.findEnabledSendButton(),
  null,
  "findEnabledSendButton must return null when detectGeminiBusy is true"
);

// Test collectThinkingText extracting '... Analyzing visual details'
const mockJob = {
  beforeAnnouncerSnapshot: "",
  thinkingLines: [],
  lastThinking: "",
  beforeModelCount: 0,
};
const extractedThink = analyzingRt.collectThinkingText(mockJob, "", "", true);
assert.strictEqual(
  extractedThink,
  "Analyzing visual details",
  "Should extract clean thinking text from '... Analyzing visual details'"
);
console.log("   [PASS] detectGeminiBusy & thinking extraction passed.");

console.log("--> Testing Turn 2+ historical thoughts isolation, lm-input-redesign selectors, ql-clipboard skip & fast 25K insertion...");
(async () => {
  // Minimal flexible MockDom to verify real Gemini Angular + Quill 1.x behavior
  class DomNode {
    constructor(tag, attrs = {}, cls = "", text = "") {
      this.nodeType = 1;
      this.tagName = String(tag || "div").toUpperCase();
      this.attrs = Object.assign({}, attrs);
      this.className = cls || "";
      this._text = String(text || "");
      this.children = [];
      this.childNodes = [];
      this.parentElement = null;
      this.parentNode = null;
      this.disabled = false;
      this.listeners = {};
      this.dataset = {};
      this._clickHandler = null;
      const self = this;
      this.classList = {
        contains: (c) => self.className.split(/\s+/).filter(Boolean).includes(c),
        add: (c) => {
          const set = new Set(self.className.split(/\s+/).filter(Boolean));
          set.add(c);
          self.className = [...set].join(" ");
        },
        remove: (c) => {
          const set = new Set(self.className.split(/\s+/).filter(Boolean));
          set.delete(c);
          self.className = [...set].join(" ");
        },
      };
    }
    get textContent() {
      if (this.children.length) return this.children.map(c => c.textContent || "").join("\n");
      return this._text;
    }
    set textContent(v) {
      this.children = [];
      this._text = String(v ?? "");
      this.childNodes = this._text ? [{ nodeType: 3, textContent: this._text, nodeValue: this._text }] : [];
    }
    get innerText() { return this.textContent; }
    set innerText(v) { this.textContent = v; }
    appendChild(ch) {
      if (!ch) return ch;
      if (ch.nodeType === 11 && Array.isArray(ch.children)) {
        for (const sub of [...ch.children]) this.appendChild(sub);
        return ch;
      }
      if (typeof ch === "object") { ch.parentElement = this; ch.parentNode = this; }
      this.children.push(ch);
      this.childNodes.push(ch);
      return ch;
    }
    replaceChildren(...nodes) {
      this.children = [];
      this.childNodes = [];
      for (const n of nodes) this.appendChild(n);
    }
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); }
    getBoundingClientRect() {
      if (this.classList.contains("ql-clipboard")) return { width: 1, height: 1, top: 0, left: -100000 };
      return { width: 420, height: 48, top: 10, left: 10 };
    }
    getClientRects() { return [this.getBoundingClientRect()]; }
    get offsetParent() { return this.parentElement || {}; }
    focus() {}
    scrollIntoView() {}
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
    dispatchEvent(e) {
      for (const fn of (this.listeners[e.type] || [])) fn(e);
      return true;
    }
    click() {
      if (typeof this._clickHandler === "function") this._clickHandler();
      this.dispatchEvent({ type: "click" });
    }
    get nextElementSibling() {
      const sibs = this.parentElement?.children || [];
      const i = sibs.indexOf(this);
      return (i >= 0 && i + 1 < sibs.length) ? sibs[i + 1] : null;
    }
    closest(sel) {
      let cur = this;
      while (cur) {
        if (cur.matches(sel)) return cur;
        cur = cur.parentElement;
      }
      return null;
    }
    _matchesPart(part) {
      const p = part.trim();
      if (!p) return false;
      const chunks = p.split(/\s+/);
      if (chunks.length > 1) {
        if (!this._matchesSimple(chunks[chunks.length - 1])) return false;
        let cur = this.parentElement;
        for (let ci = chunks.length - 2; ci >= 0; ci--) {
          while (cur && !cur._matchesSimple(chunks[ci])) cur = cur.parentElement;
          if (!cur) return false;
          cur = cur.parentElement;
        }
        return true;
      }
      return this._matchesSimple(p);
    }
    _matchesSimple(s) {
      if (s === this.tagName.toLowerCase()) return true;
      if (s.includes("gem-icon-button.send-button") && this.tagName === "GEM-ICON-BUTTON" && this.classList.contains("send-button")) return true;
      if (s === "gem-icon-button.submit" && this.tagName === "GEM-ICON-BUTTON" && this.classList.contains("submit")) return true;
      if (s.includes(".ql-editor") && this.classList.contains("ql-editor")) {
        if (s.includes("[contenteditable='true']")) return this.attrs.contenteditable === "true";
        if (s.includes("[role='textbox']")) return this.attrs.role === "textbox";
        return true;
      }
      if (s === "button[aria-label='Send message' i]" && this.tagName === "BUTTON" && /^send message$/i.test(this.attrs["aria-label"] || "")) return true;
      if (s === "[role='textbox'][contenteditable='true']" && this.attrs.role === "textbox" && this.attrs.contenteditable === "true") return true;
      if (s === "message-actions button" && this.tagName === "BUTTON" && Boolean(this.parentElement?.tagName === "MESSAGE-ACTIONS")) return true;
      if (s.startsWith(".") && this.classList.contains(s.slice(1).split("[")[0])) return true;
      if (s === "[data-test-id='bard-mode-menu-button']" && this.attrs["data-test-id"] === "bard-mode-menu-button") return true;
      return false;
    }
    matches(sel) {
      return String(sel).split(",").some(p => this._matchesPart(p));
    }
    querySelectorAll(sel) {
      const out = [];
      const walk = (n) => {
        for (const ch of (n.children || [])) {
          if (ch.nodeType === 1) {
            if (ch.matches(sel)) out.push(ch);
            walk(ch);
          }
        }
      };
      walk(this);
      return out;
    }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  }

  const root = new DomNode("div");
  const docEl = new DomNode("html");
  const modeBtn = new DomNode("button", { "data-test-id": "bard-mode-menu-button", "aria-label": "Open mode picker, currently Neon High" }, "", "Neon High");
  root.appendChild(modeBtn);

  // Turn 1 (historical, completed): model-response containing model-thoughts + message-content + message-actions + stale live announcer
  const mr1 = new DomNode("model-response");
  const mt1 = new DomNode("model-thoughts");
  const mtDesc = new DomNode("div", {}, "thought-description", "Analyzing visual details from Turn 1");
  mt1.appendChild(mtDesc);
  mr1.appendChild(mt1);
  mr1.appendChild(new DomNode("message-content", {}, "", "Turn 1 answer"));
  const ma1 = new DomNode("message-actions");
  ma1.appendChild(new DomNode("button", { "aria-label": "Copy response" }));
  mr1.appendChild(ma1);
  root.appendChild(new DomNode("user-query", {}, "", "First user query"));
  root.appendChild(mr1);

  const staleAnn = new DomNode("div", {}, "cdk-live-announcer-element", "Thinking...");
  root.appendChild(staleAnn);

  // Composer: rich-textarea with hidden .ql-clipboard BEFORE .ql-editor, and <gem-icon-button class="send-button has-input submit"><button aria-label="Send message">
  const form = new DomNode("form", {}, "input-area");
  const rich = new DomNode("rich-textarea", {}, "text-input-field_textarea ql-container");
  const qlClip = new DomNode("div", { contenteditable: "true" }, "ql-clipboard");
  const qlEd = new DomNode("div", { role: "textbox", contenteditable: "true" }, "ql-editor ql-blank textarea");
  rich.appendChild(qlClip);
  rich.appendChild(qlEd);
  form.appendChild(rich);

  const sendContainer = new DomNode("div", {}, "send-button-container");
  const gemHost = new DomNode("gem-icon-button", {}, "send-button has-input submit");
  // Deliberately omit aria-label/send classes on inner button to verify gem-icon-button.send-button host detection too,
  // while keeping aria-label on gem-icon-button or localized label on inner button
  const innerSendBtn = new DomNode("button", { "aria-label": "Send message" }, "mdc-icon-button mat-mdc-icon-button");
  let sendClicks = 0;
  innerSendBtn._clickHandler = () => { sendClicks++; };
  gemHost.appendChild(innerSendBtn);
  sendContainer.appendChild(gemHost);
  form.appendChild(sendContainer);
  root.appendChild(form);

  let execCalls = 0;
  let replaceCalls = 0;
  const origReplace = qlEd.replaceChildren.bind(qlEd);
  qlEd.replaceChildren = (...a) => { replaceCalls++; return origReplace(...a); };

  const sb2 = {
    Element: DomNode,
    Node: { ELEMENT_NODE: 1, TEXT_NODE: 3 },
    document: {
      documentElement: docEl,
      body: root,
      createElement: (t) => new DomNode(t),
      createTextNode: (t) => ({ nodeType: 3, textContent: t, nodeValue: t }),
      createDocumentFragment: () => new DomNode("#fragment"),
      execCommand: () => { execCalls++; return true; },
      querySelectorAll: (s) => root.querySelectorAll(s),
      querySelector: (s) => root.querySelector(s),
    },
    InputEvent: function(t, o) { this.type = t; Object.assign(this, o || {}); },
    Event: function(t, o) { this.type = t; Object.assign(this, o || {}); },
    PointerEvent: function(t, o) { this.type = t; Object.assign(this, o || {}); },
    MouseEvent: function(t, o) { this.type = t; Object.assign(this, o || {}); },
    KeyboardEvent: function(t, o) { this.type = t; Object.assign(this, o || {}); },
    getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
    Date,
    setTimeout: (fn) => { fn(); return 1; },
    clearTimeout: () => {},
    location: { href: "https://gemini.google.com/u/4/app/conv-turn2", pathname: "/u/4/app/conv-turn2" },
    console: { log: () => {}, warn: () => {}, error: () => {} },
  };
  sb2.window = sb2;
  sb2.globalThis = sb2;
  vm.runInNewContext("(" + bg._domInstallPageRuntimeFn.toString() + ")()", sb2);
  const rt2 = sb2.__wsaRuntime;

  assert.strictEqual(rt2.rev, 10, "Gemini runtime rev must be 10");
  assert.strictEqual(rt2.readModePickerBadge(), "Neon High", "Should parse Neon High badge");
  assert.strictEqual(rt2.findEditor(), qlEd, "findEditor must skip .ql-clipboard and pick .ql-editor");
  assert.strictEqual(rt2.detectGeminiBusy(), false, "Historical completed turn + stale announcer must NOT trigger detectGeminiBusy()");
  assert.strictEqual(rt2.findEnabledSendButton(qlEd), innerSendBtn, "Must find inner <button> inside gem-icon-button.send-button");

  // Long multi-line prompt (~25K chars, 400 lines)
  const longLines = [];
  for (let li = 0; li < 400; li++) {
    longLines.push(`Line ${li}: ` + "X".repeat(55));
  }
  const prompt25k = longLines.join("\n");
  const tStart = Date.now();
  rt2.setPromptTextInEditor(qlEd, prompt25k);
  const elapsedMs = Date.now() - tStart;
  assert.strictEqual(execCalls, 0, "Long text must NEVER call slow document.execCommand('insertText')");
  assert.strictEqual(replaceCalls, 1, "Long text must use single atomic replaceChildren(frag)");
  assert.strictEqual(elapsedMs < 200, true, "25K insertion must complete quickly without freezing");
  assert.strictEqual(qlEd.classList.contains("ql-blank"), false, "Must remove .ql-blank");
  assert.strictEqual(rt2.isPromptStillInEditor(qlEd, prompt25k), true, "Full 25K prompt must match in editor");
  assert.ok(docEl.dataset.wsaLastPromptHead.includes("Line 0:"), "Must record wsaLastPromptHead dataset");

  // If clicking Send fails to clear editor or mount user-query, submitPromptToPage must NOT be fooled by Turn 1's historical model-thoughts
  const ctx2 = rt2.inspectReactContext(qlEd);
  const subOk = await rt2.submitPromptToPage(prompt25k, qlEd, ctx2, 1);
  assert.strictEqual(subOk, false, "Historical model-thoughts must NOT trick hasSubmitEffect() into returning true when prompt is still unsent");
  assert.ok(sendClicks >= 1, "Must still have clicked Send");

  // Clear via clearEditorContents
  rt2.clearEditorContents(qlEd);
  assert.strictEqual(rt2.isPromptStillInEditor(qlEd, prompt25k), false, "clearEditorContents must remove stranded prompt");
  assert.strictEqual(qlEd.classList.contains("ql-blank"), true, "clearEditorContents must restore .ql-blank");

  // Verify lingering announcer text ("Thinking...") does NOT lock detectGeminiBusy()=true after a new model-response completes with message-actions
  sb2.__wsaActiveJob = {
    reqId: "req-ann-done",
    beforeModelCount: 1,
    beforeAnnouncerSnapshot: "",
    announcerChanged: true,
    beforeLastModelEl: mr1,
  };
  const mr2 = new DomNode("model-response");
  mr2.appendChild(new DomNode("message-content", {}, "", "Turn 2 completed answer"));
  const ma2 = new DomNode("message-actions");
  ma2.appendChild(new DomNode("button", { "aria-label": "Copy response" }));
  mr2.appendChild(ma2);
  root.appendChild(mr2);
  assert.strictEqual(rt2.detectGeminiBusy(), false, "Lingering announcer text must NOT keep detectGeminiBusy()=true once fresh model-response has message-actions");
  delete sb2.__wsaActiveJob;

  console.log("   [PASS] Turn 2+ historical isolation, lm-input-redesign, ql-clipboard skip & fast 25K passed.");
  console.log("\nALL GEMINI ACCOUNT SWITCHING & STREAM GUARD TESTS PASSED!");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
