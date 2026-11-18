---
name: semgrep-guardrails
description: While-you-code OWASP Top 10 and LLM Top 10 security guardrails. Prevents XSS, injection, SSRF, broken auth, insecure cookies/CORS/CSP, secret exposure, and prompt injection as code is written.
source: https://github.com/semgrep/skills
category: security
license: LGPL-2.1 / MIT docs (adapted for WSA)
---

# Semgrep Guardrails — While-You-Code OWASP & LLM Security Rules

Inspired by `semgrep/skills`. Prevent vulnerabilities at authoring time rather than patching them after deployment.

## OWASP Web & Frontend Coding Rules

1. **Zero Unsafe DOM Sinks (XSS Prevention)**:
   - Use `element.textContent` and `document.createElement()` (never `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`) when inserting user input, URL params, API data, or localStorage values.
   - In React/Vue/Svelte, avoid `dangerouslySetInnerHTML` / `v-html` / `{@html}` unless passed through a strict HTML sanitizer (e.g., DOMPurify).
   - Validate dynamic URLs before assigning to `href`/`src` — allow ONLY `https:`, `http:`, `mailto:`, or relative paths (block `javascript:`, `data:text/html`, `vbscript:`).
   - Add `rel="noopener noreferrer"` on every `<a target="_blank">` link.
2. **Injection & Command Safety**:
   - SQL/DB: ALWAYS use parameterized queries / prepared statements / ORM bindings — zero string interpolation in queries.
   - Shell/Subprocess: ALWAYS pass argv lists (`shell=False`), never concatenate untrusted input into shell commands. Never use `eval()`, `new Function()`, or Python `pickle.loads()` / `yaml.load()` (use `yaml.safe_load`) on untrusted input.
   - Filesystem: Canonicalize resolved paths and assert `resolved.is_relative_to(base_dir)` before reading/writing user-supplied filenames.
3. **Auth, Sessions, State & Client Trust**:
   - NEVER trust client-submitted prices, totals, roles, permissions, or `isAdmin` flags on the backend — always recompute totals and enforce RBAC/ownership server-side.
   - Cookies carrying sessions/tokens MUST use `HttpOnly`, `Secure` (in HTTPS), and `SameSite=Lax` (or `Strict`). Never store long-lived auth secrets in `localStorage` when cookies are available.
   - Hash passwords strictly with `argon2` / `bcrypt` / `scrypt` (never MD5/SHA1/SHA256) and compare tokens with constant-time `hmac.compare_digest` / `crypto.timingSafeEqual`.
4. **Network, SSRF, CORS & Headers**:
   - Never fetch arbitrary user-supplied URLs from a backend without protocol allowlisting (`http/https`) and blocking loopback/link-local/RFC1918 private IP ranges (`127.0.0.0/8`, `169.254.169.254`, `10.0.0.0/8`, `::1`).
   - Never pair `Access-Control-Allow-Origin: *` with `Access-Control-Allow-Credentials: true`.
   - Include sane defense-in-depth headers on web apps/servers: `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `X-Frame-Options: DENY` (or CSP `frame-ancestors`), and CSP meta/header avoiding unsafe inline scripts where feasible.
5. **Secrets & Error Hygiene**:
   - Read secrets strictly from environment variables (`os.environ` / `process.env`); never commit API keys, private keys, JWT secrets, or `.env` files.
   - Return generic user-safe error messages in HTTP responses; keep detailed tracebacks server-side.
6. **LLM / AI App Guardrails (OWASP LLM Top 10)**:
   - Treat ALL external web/file/user content retrieved into an LLM prompt as untrusted data — never execute shell commands, SQL, or HTML directly from unvalidated LLM output without allowlists and encoding.
