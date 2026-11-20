---
name: context-docs
description: Live, version-accurate framework and library documentation grounding. Prevents API hallucinations by inspecting installed versions and fetching official documentation before coding.
category: docs
license: MIT (adapted for WSA)
---

# Context Docs — Live, Version-Accurate Documentation Grounding

Inspired by `upstash/context7`. Prevents broken code caused by stale training-data APIs when working with fast-moving modern frameworks, libraries, and SDKs.

---

## The Ground-Before-Code Protocol

1. **Check the Installed Version First**:
   - Inspect `package.json`, `package-lock.json`, `pyproject.toml`, or `requirements.txt` before assuming API signatures.
   - Watch for breaking changes in modern frameworks:
     - Next.js (App Router vs. Pages router, Server Components, cache semantics)
     - React 19 (`useActionState`, ref as prop, Server Actions)
     - Tailwind CSS v4 (`@import "tailwindcss"`, CSS-first configuration)
     - Three.js r150+ (`SRGBColorSpace`, ACESFilmicToneMapping, compileAsync, TSL)
     - GSAP 3.12+ (`gsap.context()` cleanup)
     - Pydantic v2 (`model_validate`, `model_dump`)
     - FastAPI, Supabase, Playwright
2. **Inspect Installed Types & Signatures Locally First**:
   - In Node.js / TypeScript: read `node_modules/<pkg>/package.json` or `.d.ts` declaration files for exact type definitions and exports.
   - In Python: use `run_python` or inspect package `__version__` and function signatures directly.
3. **Targeted Live Doc Fetching**:
   - When using new library features or resolving API breaking changes, fetch current official documentation using `web_fetch`.
   - Keep fetches targeted (bounded to 6000–12000 chars) to maintain lean prompt context.
4. **Never Guess Hallucinated Parameters**:
   - If a function or export is reported missing, check the installed version and official docs immediately rather than trying random guesses.
