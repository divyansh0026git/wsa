---
name: context7-docs
description: Up-to-date framework and library documentation grounding workflow. Prevents stale training-data API hallucinations by checking installed package versions and fetching current official documentation before coding.
source: https://github.com/upstash/context7
category: engineering
license: MIT (adapted for WSA)
---

# Context7 Docs — Live, Version-Accurate Documentation Grounding

Inspired by `upstash/context7`. Stop writing broken code from stale training-data APIs when working with fast-moving frameworks and SDKs.

## Ground-Before-Code Protocol

1. **Check the Installed Version First**:
   - Inspect `package.json` / `package-lock.json`, `pyproject.toml` / `requirements.txt`, or importmaps/CDN version tags in the workspace BEFORE assuming which major version is installed.
   - Major versions with frequent breaking changes require explicit verification: Next.js App Router (14/15/16), React 19 (`useActionState`, ref-as-prop), Tailwind CSS v4 (`@import "tailwindcss"`, `@theme`), Three.js r150+ (`SRGBColorSpace`), GSAP 3.12+, Pydantic v2 (`model_validate`, `model_dump`), FastAPI, SQLAlchemy 2.0, Drizzle, Prisma, Supabase, Stripe, Clerk, Playwright.
2. **Inspect Installed Types/Signatures Locally First (Fastest & 100% Exact)**:
   - For installed Python packages: run a targeted `run_python` introspection (`import inspect, pkg; print(pkg.__version__, inspect.signature(...))`).
   - For installed Node packages: read `node_modules/<pkg>/package.json` or `.d.ts` type definitions for the exact method signature.
3. **Fetch Live Official Docs When Needed (`web_fetch`)**:
   - When introducing a new library feature, upgrading versions, or hitting an unfamiliar API error, call `web_fetch` against official documentation URLs (keep `max_chars` bounded, e.g., `6000–12000` chars) to verify exact current syntax, flags, and migration notes.
   - Cap documentation lookups to targeted queries (1–3 focused fetches max per task) so prompt context stays lean.
4. **Never Guess Deprecated APIs**:
   - If a compiler, typechecker, or runtime error says an option/export doesn't exist, check the real installed version/docs immediately instead of guessing alternative parameter names.
