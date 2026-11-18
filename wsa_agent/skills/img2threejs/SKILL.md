---
name: img2threejs
description: Procedural, animation-ready 3D hero asset builder for Three.js. Reconstructs reference photos or physical product subjects into clean modular THREE.Group factories across 8 vision-verified sculpting passes instead of cheap primitive shapes.
source: https://github.com/img2threejs/img2threejs
category: 3d-motion
license: MIT (adapted for WSA)
---

# Img2ThreeJS — 8-Pass Procedural Hero 3D Reconstruction

Inspired by `img2threejs/img2threejs`. Build hero 3D products and showcase objects as clean, reusable, animation-ready `THREE.Group` factory functions — never crude stacked boxes or external broken GLB URLs.

## Architecture Rule

- Export a clean factory (e.g., `createHeroAsset(options)` returning `{ group, parts, update(t, pointer), dispose() }`).
- Name every sub-part (`parts.chassis`, `parts.lens`, `parts.dial`, `parts.lid`) so scroll timelines and hover interactions can animate sub-assemblies independently without re-traversing the scene.
- Use procedural geometry (`LatheGeometry`, `ExtrudeGeometry` with `bevelEnabled: true`, `TubeGeometry`, `CatmullRomCurve3`, `RoundedBoxGeometry` / chamfered profiles, `InstancedMesh` for repeated rivets/fins/perforations) instead of raw sharp-edged cubes.

## The 8 Vision-Verified Sculpting Passes

1. **Pass 1 — Reference Analysis & Dimension Ratios**: Lock real-world width/height/depth proportions and center-of-mass before coding vertices.
2. **Pass 2 — Primary Silhouette Blocking**: Build the core body volume using beveled extrusions or lathed curves so the silhouette is unmistakable even in flat shading.
3. **Pass 3 — Structural Sub-Assemblies**: Add functional parts (stands, housings, rims, drivers, ports, straps, screens, seams) anchored to exact parent coordinates with zero floating gaps or unintended intersections.
4. **Pass 4 — Chamfers, Grooves & Micro-Relief**: Add recessed seams, speaker grilles, knurled rings, trim bezels, and rounded edges so specular highlights catch every contour.
5. **Pass 5 — Physically Based Materials & Procedural Canvas Textures**:
   - Build procedural high-res `CanvasTexture` maps (brushed aluminum anisotropy grain, woven acoustic mesh, matte ceramic micro-noise, crisp embossed typography/labels) instead of flat solid colors.
   - Tune `metalness`, `roughness`, `clearcoat`, `clearcoatRoughness`, `ior`, and `transmission` deliberately per material.
6. **Pass 6 — Studio Lighting & Grounding**: Pair the asset with a soft PMREM environment map, directional key light with soft PCF shadows, subtle rim light to separate dark materials from dark backdrops, and a contact shadow plane.
7. **Pass 7 — Animation Hooks & Interactive States**: Expose smooth damped idle float/rotation, pointer parallax, and exploded-view / variant-color / camera-preset transitions via `update(dt, state)`.
8. **Pass 8 — Multi-Angle Live Visual Inspection**: Render via `visual_inspect` (three-quarter hero, front, side/detail) to verify proportions, highlights, seam alignment, and zero clipping; refine until realistic.

## Guardrails

- Never invent broken external `.glb`/`.hdr` CDN URLs that fail offline or with CORS; generate procedural geometries and studio `RoomEnvironment` / canvas envMaps locally so assets render 100% reliably.
- Dispose every procedural geometry, material, and `CanvasTexture` inside `dispose()`.
