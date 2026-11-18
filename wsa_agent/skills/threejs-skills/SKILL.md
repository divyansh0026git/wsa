---
name: threejs-skills
description: Modular Three.js r150+ guardrails for WebGL/WebGPU setup, physically correct lighting, PBR materials, custom GLSL shaders, post-processing, raycasting, resize handling, and strict GPU resource disposal.
source: https://github.com/CloudAI-X/threejs-skills
category: 3d-motion
license: MIT (adapted for WSA)
---

# Three.js Skills — Modern 3D, Shaders & Performance Guardrails

Inspired by `CloudAI-X/threejs-skills`. Prevents outdated Three.js APIs, washed-out colors, broken shaders, blank canvases, and GPU memory leaks.

## Modern Three.js r150+ API Rules (No Deprecated APIs)

- **Color Management**: Use `renderer.outputColorSpace = THREE.SRGBColorSpace` (NEVER legacy `outputEncoding` / `sRGBEncoding`) and set `texture.colorSpace = THREE.SRGBColorSpace` on color/albedo maps (keep normal/roughness/metalness maps linear).
- **Tone Mapping & Exposure**: Set `renderer.toneMapping = THREE.ACESFilmicToneMapping` and calibrate `renderer.toneMappingExposure` (`0.95`–`1.25`) so highlights roll off smoothly without blowing out whites.
- **Physical Lighting**: Modern Three.js uses physically based intensities — pair `RoomEnvironment` / `PMREMGenerator` environment map (`scene.environment`) with directional key light (`intensity: 2.0–3.5`, `castShadow: true`, tuned shadow camera frustum + `shadow.bias = -0.0004`, `shadow.normalBias = 0.02`) and subtle hemisphere/rim light.
- **Pixel Ratio & Sizing**: Always clamp `renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))` and size from the container's `ResizeObserver` (`clientWidth` / `clientHeight`) with fallback non-zero dimensions so the canvas never collapses to 0x0.

## Custom GLSL Shaders & Post-Processing

- Always check `uTime`, `uResolution`, `uPointer`, and `uProgress` uniforms are initialized with matching GLSL types (`float`, `vec2`, `vec3`, `THREE.Color`).
- Keep fragment shaders branch-light (`mix`/`smoothstep`/`step` over heavy `if` branches), use `precision highp float;` when needed, and respect `THREE.ShaderMaterial` tone mapping/colorspace (`#include <tonemapping_fragment>`, `#include <colorspace_fragment>`).
- Avoid expensive full-screen post-processing chains unless justified; if using Bloom, set a strict luminance threshold (`threshold >= 0.85`, subtle `strength: 0.25–0.45`) so UI and hero edges stay sharp.

## Performance, Interaction & Disposal

- Use `THREE.InstancedMesh` when rendering >20 repeated meshes; share geometries and materials across instances.
- Pause or throttle the `requestAnimationFrame` loop when `document.hidden` is true or canvas is scrolled off-screen (`IntersectionObserver`), and clamp frame `delta = Math.min(clock.getDelta(), 0.05)` so tab switches never cause physics/animation jumps.
- Implement clean WebGL fallback (`try { new THREE.WebGLRenderer(...) } catch { renderFallbackPoster() }`) and handle `webglcontextlost` gracefully.
- On teardown: cancel RAF, disconnect `ResizeObserver`, remove event listeners, traverse scene to `.dispose()` every geometry, material, texture, and `PMREMGenerator` render target, and call `renderer.dispose()`.
