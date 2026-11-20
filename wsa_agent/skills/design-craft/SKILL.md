---
name: design-craft
description: Master Design Director & 3D Cinematic Craft: anti-slop typography, responsive layout, GSAP choreography, and award-winning WebGL/Three.js shaders (Igloo.inc & NASA Eyes on the Solar System standard).
category: design
license: MIT (adapted for WSA)
---

# Design Craft — Master UI, Anti-Slop, GSAP Motion & 3D Cinematic Architecture

Combines visual design direction, anti-slop polish, GSAP scroll choreography, and award-winning WebGL/Three.js architecture (Igloo.inc Site of the Year 2024 & NASA Eyes on the Solar System standard).

---

## 1. UI & Visual Identity (Anti-Slop Baseline)

### The Obsidian & Crisp Ink Palette
* **Canvas Void**: Default to Obsidian-Black (`#090A0D` or `#030407` for 3D cosmic scenes).
* **Surfaces & Cards**: Deep layered darks (`#111318` card surface, `#161920` elevated hover, `#1C202A` interactive controls). Use `backdrop-filter: blur(12px)` for frosted glass overlays.
* **Hairlines & Dividers**: Subtle `1px solid rgba(255, 255, 255, 0.08)` or `rgba(255, 255, 255, 0.05)`. Never thick borders.
* **Typography & Ink**: High-contrast Crisp White (`#F5F6F8`) for primary headings, Slate Muted (`#8E95A5`) for secondary metadata, and `#5B6275` for muted labels.
* **Accent & CTA**: Pure crisp white button with obsidian text (`background: #F5F6F8; color: #090A0D; font-weight: 600;`), or single tactile signature color. One bright element per viewport.

### Anti-Slop Strict Rules
* **No AI Slop Tells**: Eliminate generic purple/violet/cyan radial neon glows, vibrating gradient text, floating emoji badges ("✨ New AI feature"). No emoji left in the interface.
* **One Bright Element**: Give each view exactly one dominant focal point / one bright element — a hero metric, primary action, or key visualization.
* **Credit**: unslop by Damir Sokolovsky (@heydamir), CC BY 4.0.
* **No Unstyled Form Controls**: Custom style all `<input type="range">`, `<select>`, scrollbars (`scrollbar-width: thin`), and checkbox controls.
* **Typography Hierarchy**: Distinctive editorial or technical font pairing (e.g. `Inter` / `Geist` + `JetBrains Mono` / `IBMPlexMono` or high-end serif header). Use clamp for fluid scales: `clamp(2rem, 5vw, 4.5rem)`.
* **Accessibility (WCAG 2.1 AA)**: Minimum 4.5:1 text contrast, visible `:focus-visible` outlines, semantic landmarks (`<main>`, `<nav>`, `<aside>`), explicit touch targets ($\ge 44 \times 44\text{px}$).

---

## 2. GSAP Scroll Choreography & Fluid Motion

* **GPU-Only Transforms**: Animate only `transform` (`x`, `y`, `xPercent`, `yPercent`, `scale`, `rotation`) and `opacity`. Never animate `top`, `left`, `width`, `height`, or `margin`.
* **Context & Cleanup**: Always wrap animations in `gsap.context((self) => { ... }, containerRef)` and call `ctx.revert()` on component unmount to prevent memory leaks and ghost triggers.
* **Pinned Scrubbed Chapters**: Pin sections cleanly using `ScrollTrigger.create({ trigger, start: "top top", end: "+=150%", pin: true, scrub: 0.8 })`.
* **Prefers-Reduced-Motion**: Always check `window.matchMedia('(prefers-reduced-motion: reduce)')`. If enabled, skip camera warps and scroll pins, displaying clean static sections.

---

## 3. Igloo.inc-Class 3D Cinematic & Solar System Architecture

Replaces conventional HTML/CSS document layouts with a unified WebGL scene driven by scroll-linked camera choreography, procedural 3D assets, compressed GPU textures, and custom shaders.

### A. The 3-Layer Viewport Stack
* **Architecture**: `z0` (Backdrop Void) $\to$ `z1` (100vw WebGL Canvas) $\to$ `z2` (Non-blocking DOM HUD).
1. **`z0` — Backdrop Void**: CSS `--bg: #030407` matched verbatim to WebGL clear color (`renderer.setClearColor(0x030407, 1)`) and `scene.fog = new THREE.FogExp2(0x030407, 0.0018)` (Saturn & celestial continuum). Zero white flash, zero seam between sky and HUD.
2. **`z1` — Real-Time Canvas**: `position: fixed; inset: 0; z-index: 1; pointer-events: auto;`. The entire website *is* the canvas.
3. **`z2` — Isolated DOM HUD**: `position: fixed; inset: 0; z-index: 2; pointer-events: none;`. All interactive children (`button`, `a`, sliders, drawer) set `pointer-events: auto;`.

### B. Shaders & Modern Three.js r150+ Setup
* **Color Space & Tone Mapping**:
  ```javascript
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  ```
* **TSL / ShaderMaterial Contract**:
  - Custom shaders MUST declare uniform contracts: `uTime` (float), `uResolution` (vec2), `uPointer` (vec2), `uProgress` (float).
  - Include `#include <tonemapping_fragment>` and `#include <colorspace_fragment>` in fragment shaders.
* **SwiftShader Headless Compatibility**:
  - **NEVER** use default `MeshPhysicalMaterial` transmission (`transmission: 1.0`) — it renders as opaque flat gray in SwiftShader headless Chromium during QA/CI.
  - Use custom additive Fresnel shaders or unlit custom blending with depth testing enabled.
* **Signature Material Polish**: Establish one cohesive tactile shader identity (e.g. tactile ice, solar corona, or celestial refraction) across the experience.

### C. Performance Budget (`PERF_BUDGET`) & Geometry Quotas
* **Quotas**:
  - Desktop: Max 60 draw calls, max 150,000 triangles, 60 FPS target.
  - Mobile: Max 30 draw calls, max 60,000 triangles, DPR capped at 1.5.
* **Instancing**: Asteroid belts, planetary rings, and micro-debris fields MUST use `THREE.InstancedMesh` with matrix and color instance buffers.
* **Texture & Mesh Compression**: Stream geometry using Draco or Meshopt; use KTX2 / Basis Universal GPU textures for surface maps and volumetric particle lookup.
* **One-Way Performance Latching**: If frame render delta exceeds 33ms (dropping below 30 FPS) for 3 consecutive seconds, latch performance down: disable bloom pass $\to$ drop DPR to 1.0 $\to$ halve particle count.

### D. Pipeline Warmup (`compileAsync`)
* Shaders must never compile on user interaction (causing frame freezes).
* Behind the initial loading screen, always run:
  ```javascript
  await renderer.compileAsync(scene, camera);
  ```
  Gate the removal of the loader until the first rendered frame succeeds.

### E. Step-Clamped Damped Camera Choreography
* Never snap camera positions directly.
* Use step-clamped damped spline interpolation:
  ```javascript
  const delta = Math.min(clock.getDelta(), 0.05);
  camera.position.lerp(targetPosition, 1.0 - Math.exp(-dampFactor * delta));
  ```
* Add subtle mouse dwell micro-parallax on camera orientation (`factor = 0.03`).

### F. SDF Typography & Web Audio
* **WebGL SDF Typography**: Critical 3D coordinates, speed telemetry, and celestial labels rendered via WebGL SDF textures to avoid DOM layout reflows during scrub animations.
* **Interactive Spatial Audio**: Use Web Audio API (`AudioContext`) with compressed `.ogg` stems for ambient drone pads, chime waypoints, and warp acceleration feedback. Always unlock `AudioContext` on first user gesture.

---

## 4. Pre-Ship Verification Checklist

- [ ] Canvas resizes cleanly via `ResizeObserver` without collapsing to 0x0.
- [ ] `compileAsync()` completes before loader dismiss.
- [ ] No default `transmission: 1.0` materials that break SwiftShader headless rendering.
- [ ] `scene.fog` hex matches CSS `--bg` hex exactly.
- [ ] All GSAP animations revert cleanly on teardown.
- [ ] Clean GPU resource disposal: `.dispose()` called on geometries, materials, textures, and renderer.
