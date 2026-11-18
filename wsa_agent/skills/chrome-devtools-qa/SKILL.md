---
name: chrome-devtools-qa
description: Live browser inspection, console/network stack-trace debugging, layout/CLS verification, memory leak detection, and frontend performance QA using real browser DevTools telemetry.
source: https://github.com/ChromeDevTools/chrome-devtools-mcp
category: debugging-qa
license: Apache-2.0 (adapted for WSA)
---

# Chrome DevTools QA — Live Browser Inspection & Performance Debugging

Inspired by `ChromeDevTools/chrome-devtools-mcp`. End blind frontend debugging by inspecting real browser runtime telemetry (console errors, network failures, DOM/layout metrics, visual screenshots, and performance bottlenecks).

## Core Workflow (Observe -> Diagnose -> Fix -> Re-Verify)

1. **Launch / Attach & Capture Real Telemetry**:
   - For local web apps, start the dev/static server (`start_background` + `wait_for(kind="http", ...)`, ensuring `Cache-Control: no-store` so stale assets never mask fixes), then run `visual_inspect` (desktop `1440x900` + explicit mobile viewports if responsive bugs are involved) or `browser_inspect` / `browser_interact` on an open app tab.
   - Always inspect ALL 4 live signals:
     - **Console & Exceptions**: Uncaught JS errors, unhandled promise rejections, React/Vue hydration mismatches, WebGL shader compile logs.
     - **Network & Assets**: Failed 4xx/5xx API requests, CORS blocks, missing CSS/JS/font/image/icon assets (`naturalWidth === 0`).
     - **Layout & Geometry**: Horizontal overflow (`scrollWidth > innerWidth`), clipped/overlapping text, collapsed 0x0 canvases, z-index occlusion, off-screen modals/drawers.
     - **Interactive State**: Click real controls, submit forms, switch tabs/routes, resize viewports, and confirm state transitions actually work in DOM.
2. **Performance & Optimization Audit** (when app feels slow, laggy, janky, or freezes):
   - Hunt main-thread blockers: forced synchronous reflows (`offsetHeight`/`getBoundingClientRect` interleaved with DOM writes), unthrottled `scroll`/`mousemove`/`resize` listeners, redundant full-list re-renders, unvirtualized giant DOM trees.
   - Hunt memory & GPU leaks: uncanceled `requestAnimationFrame` / `setInterval`, unremoved event listeners, undisposed Three.js geometries/materials/textures, zombie GSAP ScrollTriggers, detached DOM nodes.
   - Check Core Web Vitals fundamentals: LCP hero image/script blocking, CLS from unsized media/webfonts, INP from heavy click/input handlers, oversized uncompressed images or unsplit bundles.
3. **Closed-Loop Verification**:
   - Apply surgical fixes with `patch_file`, re-run `visual_inspect` / `browser_interact`, and confirm 0 console errors, 0 failed network requests, clean pixels, and smooth responsiveness before closing.

## Guardrails

- Never guess why a browser UI is broken when you can inspect the live rendered page.
- Stop managed background servers cleanly (`stop_background`) when QA is finished unless the user asked to keep the preview running.
