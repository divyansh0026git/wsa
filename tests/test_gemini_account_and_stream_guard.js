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
assert.strictEqual(bg.hasIncompleteStreamText('{"name": "read_file", "arguments": {"path": "main.ts"'), true);

// 5. Complete tool call
assert.strictEqual(
  bg.hasIncompleteStreamText('{"name": "apply_patch", "arguments": {"edits": [{"path": "src/shaders/cinematicPass.ts"}]}}'),
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

// Once JSON completes, it can complete normally
const completeTurnState = {
  text: '{"name": "apply_patch", "arguments": {"edits": [{"path": "src/shaders/cinematicPass.ts"}]}}',
  stopVisible: false,
  reactBusy: false,
  hasTurnActions: true,
  sawNewTurn: true,
  now: t0 + 1500,
  lastTextChangeAt: t0,
  lastThinkChangeAt: t0,
  lastBusyAt: t0,
};
assert.strictEqual(
  rt.evaluateTurnCompletionState(completeTurnState),
  true,
  "Completed JSON should complete normally once idle threshold is reached"
);

// Fallback after 8s of sustained silence on incomplete JSON
incompleteTurnState.now = t0 + 8500;
assert.strictEqual(
  rt.evaluateTurnCompletionState(incompleteTurnState),
  true,
  "Incomplete JSON should fallback complete after 8s of sustained idle"
);

console.log("   [PASS] evaluateTurnCompletionState streaming protection passed.");
console.log("\nALL GEMINI ACCOUNT SWITCHING & STREAM GUARD TESTS PASSED!");
