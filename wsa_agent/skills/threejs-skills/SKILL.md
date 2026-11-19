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
- **Atmospheric Continuity (`FogExp2`)**: Always match `scene.background`, `scene.fog = new THREE.FogExp2(bgHex, density)`, terrain edge fade, and CSS `--bg` — never leave a harsh black-sky/bright-floor split behind UI copy.
- **Pixel Ratio & Sizing**: Always clamp `renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))` and size from the container's `ResizeObserver` (`clientWidth` / `clientHeight`) with fallback non-zero dimensions so the canvas never collapses to 0x0.

## Custom GLSL Shaders & Post-Processing

- Always check `uTime`, `uResolution`, `uPointer`, and `uProgress` uniforms are initialized with matching GLSL types (`float`, `vec2`, `vec3`, `THREE.Color`).
- Keep fragment shaders branch-light (`mix`/`smoothstep`/`step` over heavy `if` branches), use `precision highp float;` when needed, and respect `THREE.ShaderMaterial` tone mapping/colorspace (`#include <tonemapping_fragment>`, `#include <colorspace_fragment>`).
- **Cinema VFX & Post-Processing (`igloo.inc` Standard)**: For hero/sci-fi 3D builds, wire an `EffectComposer` cinema pipeline: selective `UnrealBloomPass` (`threshold: 0.78–0.88`, crisp emissive corona/highlights without blowing out UI text) + a custom full-screen `ShaderPass` combining subtle radial chromatic aberration (pulsing smoothly on warp camera jumps), fine ACES film grain + optical vignette, and anamorphic solar glare.
- **Volumetric GPU & Particle VFX**: Render a layered 3D fBm/curl-noise volumetric nebula/atmosphere shader dome (never a flat solid void with static dots), animated 3D plasma/energy geometry (`TubeGeometry` ribbons/prominence arcs with additive flow shaders), GPU `THREE.Points` solar-wind/warp-streak particle fields, instanced micro-debris/ice crystals, and smooth mouse-parallax + inertia choreography.

## Performance, Interaction & Disposal

- Use `THREE.InstancedMesh` when rendering >20 repeated meshes; share geometries and materials across instances.
- Pause or throttle the `requestAnimationFrame` loop when `document.hidden` is true or canvas is scrolled off-screen (`IntersectionObserver`), and clamp frame `delta = Math.min(clock.getDelta(), 0.05)` so tab switches never cause physics/animation jumps.
- Implement clean WebGL fallback (`try { new THREE.WebGLRenderer(...) } catch { renderFallbackPoster() }`) and handle `webglcontextlost` gracefully.
- On teardown: cancel RAF, disconnect `ResizeObserver`, remove event listeners, traverse scene to `.dispose()` every geometry, material, texture, and `PMREMGenerator` render target, and call `renderer.dispose()`.
