---
name: dream-loop
description: Visual quality critic loop for 3D, shader, and showcase web scenes. Locks a concrete visual target first, renders live browser screenshots, critiques lighting/composition/materials against the target with fresh eyes, and iterates until visual craft passes.
source: https://github.com/achimala/dream-loop
category: 3d-motion
license: MIT (adapted for WSA)
---

# Dream Loop — Visual Quality Loop for 3D & Interactive Craft

Inspired by `achimala/dream-loop`. Never code 3D scenes, WebGL shaders, or hero visual compositions blind.

## Core Principle

Code alone cannot verify lighting, silhouette, framing, depth separation, or material realism. Every visual pass MUST be judged against a concrete target aesthetic via live rendered screenshots (`visual_inspect` / `browser_inspect`).

## The 4-Phase Dream Loop

1. **Lock the Target Aesthetic First**:
   - Write a 6-line **Visual Target Spec**: hero subject & silhouette (`igloo.inc` cinema VFX standard), camera rail & angle, 3-point + volumetric rim light, shader/PBR response, hex palette, and negative space.
   - If reference media exists, inspect it with `inspect_image` first to lock proportions and lighting ratios.
2. **Build the Scene Cleanly**:
   - Implement camera framing, `PMREMGenerator` environment lighting, volumetric 3D shaders/particles/`EffectComposer` VFX, and hero composition cleanly.
   - Ensure the hero reads unmistakably in `1440x900` with zero canvas clipping, z-fighting, or flat surfaces.
3. **Fresh-Context Visual Critique**:
   - Capture live screenshots via `visual_inspect` (`1440x900` hero + interactive state) and critique as Art Director:
     - **Silhouette & VFX Depth**: Volumetric layers, shader depth, and framing breathing room?
     - **Lighting & Materials**: Key, rim, bloom, and believable shader/PBR response?
     - **Spatial HUD**: Razor-minimal aerospace HUD (1px hairline ticks, monospace kinetic telemetry, scrim safe-zones) — zero rounded SaaS card/pill clutter.
     - **Runtime Hygiene**: Zero WebGL warnings or console errors.
4. **Iterate Until Visual Sign-Off**:
   - Fix lighting/exposure -> shader VFX/scale -> material detail -> HUD contrast, then re-capture.

## Do Not

- Never finish 3D scenes without passing `visual_inspect` across hero and interactive states.
- Ban "Saturn-toy" (`sphere+Torus+Cone`), `FogExp2` breaks (match `scene.background`, `FogExp2` & `--bg`), and SaaS card/pill clutter.
- Never bleed unbacked text over 3D: keep safe-zones, 1px hairline reticles, or `backdrop-filter: blur(16px)` (`alpha>=0.78`); mute Web Audio by default.
