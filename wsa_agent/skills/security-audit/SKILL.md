---
name: security-audit
description: Structured 6-phase full-repo security auditor with adversarial candidate validation to eliminate false positives and surgically remediate real vulnerabilities across web frontends, APIs, auth, storage, and build configs.
source: https://github.com/cloudflare/security-audit-skill
category: security
license: Apache-2.0 (adapted for WSA)
---

# Security Audit — 6-Phase Adversarial Vulnerability Audit & Remediation

Inspired by `cloudflare/security-audit-skill`. Audit every generated or modified website, API, and web application systematically — hunting real exploit paths and proving every finding adversarially so fixes target true risk, not noise.

## The 6 Structured Phases

1. **Phase 1 — Attack Surface & Trust-Boundary Recon**:
   - Map all entrypoints: HTML forms, URL query/hash params (`location.search`, `location.hash`), `postMessage` listeners, API endpoints, WebSockets, file uploads, cookies/localStorage, auth/session middleware, and external fetches.
   - Identify trust boundaries: untrusted user/URL input -> client DOM -> API -> database/filesystem/third-party calls.
2. **Phase 2 — Coverage-Led Vulnerability Hunting**:
   - Systematically scan every mapped entrypoint against high-impact web & app classes:
     - **DOM & Stored/Reflected XSS**: `innerHTML`, `outerHTML`, `document.write`, `insertAdjacentHTML`, `dangerouslySetInnerHTML`, unvalidated `href="javascript:..."`, template interpolation of user/URL data.
     - **Broken Access Control / Auth / IDOR**: Missing auth/role checks on state-changing routes, client-only auth gates, trusting client-sent user IDs/prices/roles, weak/predictable session tokens, missing logout invalidation.
     - **Injection & Path Traversal**: String-concatenated SQL/NoSQL/shell commands, `eval`/`new Function`, unsanitized `path.join` allowing `../` escape, SSRF on user-supplied URLs.
     - **CSRF, CORS & Browser Hardening**: Wildcard `Access-Control-Allow-Origin: *` with credentials, missing CSRF token / `SameSite` cookie flags, missing `Content-Security-Policy` / `X-Content-Type-Options: nosniff` / `Referrer-Policy` / `rel="noopener noreferrer"` on `target="_blank"`.
     - **Secrets & Information Leakage**: Hardcoded API keys/tokens/passwords in client JS or repo, verbose stack traces leaked in HTTP responses, sensitive data stored in `localStorage` or logged to console.
3. **Phase 3 — Adversarial Candidate Validation (Disprove First)**:
   - For each candidate finding, play devil's advocate: trace exact attacker-controlled input -> sink path, check existing sanitizers/framework auto-escaping/guards, and DISCARD theoretical or unreachable false positives.
   - Only promote findings with a concrete, reproducible exploit scenario and exact `file:line`.
4. **Phase 4 — Severity Triage**:
   - Rank confirmed findings: `CRITICAL` (RCE, auth bypass, SQLi, stored XSS, secret leak) -> `HIGH` (reflected/DOM XSS, IDOR, CSRF on mutating route, SSRF, path traversal) -> `MEDIUM` (missing security headers/CSP, weak input validation, open redirect, `noopener` missing).
5. **Phase 5 — Surgical Remediation**:
   - Fix every confirmed `CRITICAL`, `HIGH`, and practical `MEDIUM` issue in-place with `patch_file`: replace unsafe `innerHTML` with `textContent` / DOM node APIs or strict allowlisted sanitization, parameterize queries, validate/canonicalize URLs and paths, enforce server-side auth/totals, tighten headers & `rel="noopener noreferrer"`.
   - Preserve all intended UI craft and functionality — never break working features while hardening.
6. **Phase 6 — Independent Re-Verification**:
   - Re-run tests, syntax checks, and `visual_inspect` / runtime checks AFTER security patches, and report confirmed findings fixed + residual risks honestly.
