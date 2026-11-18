---
name: gsap-skills
description: Official GSAP motion & scroll-storytelling rules covering smooth scroll-scrubbed timelines, ScrollTrigger pinned chapters, GPU-only transforms, reduced-motion fallbacks, and leak-free gsap.context() / gsap.matchMedia() cleanup.
source: https://github.com/greensock/gsap-skills
category: 3d-motion
license: MIT (adapted for WSA)
---

# GSAP Skills — Scroll Storytelling & Leak-Free Motion

Inspired by `greensock/gsap-skills`. Craft smooth, orchestrated, Igloo.inc-grade scroll choreography without jank, layout thrash, or zombie ScrollTriggers.

## Core Rules

1. **Register Plugins Explicitly**: Always register plugins once at module startup (`gsap.registerPlugin(ScrollTrigger)`), guarding SSR (`typeof window !== "undefined"`).
2. **Scoped Lifecycle Cleanup (`gsap.context` & `gsap.matchMedia`)**:
   - Wrap ALL component/page animations inside `const ctx = gsap.context(() => { ... }, containerRef)` and ALWAYS call `ctx.revert()` on unmount/teardown.
   - Use `gsap.matchMedia()` for responsive breakpoints AND accessibility:
     ```js
     const mm = gsap.matchMedia();
     mm.add("(prefers-reduced-motion: no-preference)", () => { /* scroll/entrance motion */ });
     mm.add("(prefers-reduced-motion: reduce)", () => { /* instant final state, zero parallax */ });
     ```
3. **GPU-Composited Properties Only**:
   - Animate ONLY `x`, `y`, `xPercent`, `yPercent`, `scale`, `rotation`, `opacity`, and `clipPath` (sparingly).
   - NEVER animate `top`, `left`, `width`, `height`, `margin`, or `padding` in scroll-scrubbed timelines — they force per-frame layout recalculations.
4. **ScrollTrigger Pinned Chapters & Scrub Discipline**:
   - Use numeric scrub damping (`scrub: 0.6` to `1.2`) for smooth tactile inertia synced with Three.js camera/model progress (`onUpdate: (self) => { sceneState.progress = self.progress; }`).
   - Set `invalidateOnRefresh: true` on dynamic-size tweens and call `ScrollTrigger.refresh()` after web fonts or async images finish loading so pin markers never drift.
   - Avoid pinning tiny or overflow-hidden ancestors (`overflow: hidden` breaks `position: sticky`/pin layout; prefer `overflow-x: clip`).
5. **Choreographed Restraint**:
   - Prefer ONE coherent page-load sequence + purposeful chapter transitions over bouncing/fading every individual card.
   - Never hide above-the-fold hero content at `opacity: 0` if JS fails or before hydration — use `gsap.from` carefully with `immediateRender: false` on below-the-fold ScrollTriggers or CSS fallback visibility.
6. **Three.js + GSAP Sync**:
   - Drive Three.js progress from a single plain state object (`const rig = { progress: 0, camX: 0, camY: 1.2, rotY: 0 }`) tweened by GSAP timelines; read `rig` inside the single `requestAnimationFrame` loop (never start competing RAF loops).

## Checklist

- [ ] `gsap.registerPlugin(ScrollTrigger)` called cleanly once.
- [ ] Every timeline/ScrollTrigger wrapped in `gsap.context()` / `gsap.matchMedia()` with teardown `.revert()`.
- [ ] `prefers-reduced-motion: reduce` disables parallax/scrub and leaves all content immediately readable.
- [ ] Zero layout-thrashing properties (`top`/`left`/`width`/`height`) animated on scroll.
- [ ] `ScrollTrigger.refresh()` triggered after font/media load; zero stuck pins or blank sections.
