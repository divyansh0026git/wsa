---
name: systematic-debugging
description: Master Debugging & Browser QA: 4-phase root-cause investigation protocol, live Chrome DevTools inspection (console, network, DOM, memory), and performance profiling.
category: debugging-qa
license: MIT (adapted for WSA)
---

# Systematic Debugging & Browser QA — Master Root-Cause Protocol

Combines the 4-phase disciplined root-cause investigation protocol with real-time browser DevTools telemetry (console, network, DOM, and memory).

---

## The Iron Law of Debugging

```
NO FIXES WITHOUT ROOT-CAUSE INVESTIGATION FIRST
```

Symptom fixes are failure. If you haven't completed Phase 1 (Root Cause Investigation), you cannot propose or implement fixes.

---

## 1. The 4-Phase Root Cause Protocol

### Phase 1: Root Cause Investigation
1. **Read Error Messages & Stack Traces Carefully**:
   - Don't skim past errors or warnings. They often state the exact failing condition and line.
2. **Reproduce Minimally & Consistently**:
   - Create a minimal reproduction script or command. If intermittent, isolate variables until reproducible.
3. **Trace Data Flow & Dependencies**:
   - Trace backwards from the failure site to where corrupt or unexpected state originated.
4. **Form a Specific, Testable Hypothesis**:
   - State clearly: *"Function X receives Y instead of Z because condition W evaluates incorrectly."*

### Phase 2: Pattern Analysis & Isolation
1. **Find Working vs. Failing Boundary**:
   - What works? What fails? What changed between the working state and the failing state?
2. **Check Recent Commits & Diffs**:
   - Inspect git log/diff for recent regressions.

### Phase 3: Surgical Hypothesis Testing & Fix
1. **Test the Hypothesis with Minimal Instrumentation**:
   - Add targeted logging or assertions to verify the hypothesis before changing application logic.
2. **Implement Minimal, Surgical Fix**:
   - Fix the root cause, not the symptom. Avoid unnecessary refactoring.

### Phase 4: Verification & Regression Gate
1. **Verify the Fix Neutralizes the Failure**:
   - Run the reproduction case and assert it now succeeds.
2. **Run Full Regression Suite**:
   - Run adjacent unit, integration, or e2e tests to ensure zero collateral regressions.

---

## 2. Live Browser DevTools QA (Frontend & WebGL)

End blind frontend debugging by inspecting real browser runtime telemetry (`visual_inspect`, `browser_inspect`, `browser_interact`):

1. **Console & Exceptions**:
   - Check for uncaught JS errors, unhandled promise rejections, React/Vue hydration mismatches, and WebGL shader compile errors.
2. **Network & Assets**:
   - Check for failed 4xx/5xx API requests, CORS rejections, missing fonts/textures/images (`naturalWidth === 0`).
3. **Layout & Geometry**:
   - Inspect horizontal overflows (`scrollWidth > innerWidth`), overlapping elements, collapsed 0x0 canvases, and z-index occlusion.
4. **Memory & GPU Leaks**:
   - Hunt uncanceled `requestAnimationFrame`, unremoved event listeners, undisposed Three.js geometries/materials/textures, and zombie GSAP triggers.
5. **Core Web Vitals**:
   - Identify main-thread blocking long tasks, LCP asset bottlenecks, and layout shifts (CLS).
