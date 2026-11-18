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
   - Before writing scene/shader code, write a concise 6-line **Visual Target Spec**: hero subject & silhouette, camera focal length & angle, 3-point + rim/environment lighting mood, exact material response (roughness/metalness/subsurface/glass), color palette (hex), and negative space reserved for typography.
   - If the user provided a reference screenshot/image, inspect it with `inspect_image` first and lock proportions, camera elevation, and lighting ratios directly from it.
2. **Build the Scene Cleanly**:
   - Implement camera framing, environment lighting (`PMREMGenerator` / studio HDRI procedural room), grounded contact shadows, and deliberate hero composition in one clean pass.
   - Ensure the hero reads unmistakably within the first viewport (`1440x900`) with zero canvas clipping, zero z-fighting, and zero flat unlit surfaces.
3. **Fresh-Context Visual Critique**:
   - Render the live app and capture screenshots via `visual_inspect` (desktop `1440x900` + key interaction states or camera presets).
   - Critique strictly as an independent Art Director (forget how hard the code was to write):
     - **Silhouette & Framing**: Is the hero centered/balanced with intentional breathing room, or cramped/cropped?
     - **Lighting & Depth**: Are key light, fill, rim highlight, and contact shadow visibly separating foreground from background?
     - **Materials**: Do metallic, glass, matte, and emissive materials read believably instead of plastic/flat?
     - **UI Integration**: Does typography sit on calm space with crisp contrast (WCAG AA+) and zero overlap with 3D focal points?
     - **Runtime Hygiene**: Zero WebGL warnings, zero console errors, smooth frame pacing.
4. **Iterate Until Visual Sign-Off**:
   - Fix the highest-impact visual defects first (lighting/exposure/tone mapping -> silhouette/scale -> material roughness/detail -> UI typography contrast).
   - Re-capture after edits. Stop only when the live render matches the Visual Target Spec cleanly.

## Do Not

- Do NOT declare a 3D scene "done" from code inspection alone without a passing `visual_inspect` / `browser_inspect` render.
- Do NOT use raw unlit primitives (`MeshBasicMaterial` boxes/spheres) as a hero centerpiece unless the brief explicitly calls for geometric minimalism.
- Do NOT obscure the hero canvas with heavy blurred modals, neon glows, or cluttered floating telemetry overlays.
