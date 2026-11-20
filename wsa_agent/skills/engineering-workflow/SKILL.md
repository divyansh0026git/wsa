---
name: engineering-workflow
description: Master Software Engineering Discipline: Karpathy simplicity, Red-Green-Refactor TDD, strict pre-completion evidence verification, and git safety guardrails.
category: engineering
license: MIT (adapted for WSA)
---

# Engineering Workflow — Master Software Engineering Discipline & Guardrails

Combines Andrej Karpathy's clean coding principles, test-driven development (TDD), evidence-first verification gates, and git destruction safeguards.

---

## 1. Karpathy 4-Pillar Clean Coding Discipline

1. **Architect & Inspect First (System, Backend & File Architecture Blueprint)**:
   - Before coding software, backends, or websites, outline a file-by-file architecture blueprint (modules, schemas/types, contracts, failure/concurrency boundaries, OWASP security).
   - Understand the current architecture before proposing changes. Read existing implementations first.
   - Form an explicit mental model of data flow, dependencies, and side effects.
2. **Simplicity First**:
   - The best code is no code; the second best is simple code. Avoid premature abstractions, speculative interfaces, or excessive design patterns.
   - Favor clear, linear code that is straightforward to read and debug over clever one-liners.
3. **Surgical, Minimal Changes**:
   - Touch only the lines required to achieve the goal. Never reformat or rewrite adjacent files.
   - Avoid cascading refactors unless explicitly requested. Preserve existing conventions and code style.
4. **Goal-Driven Verification**:
   - Every change must have a verifiable outcome. Define how success will be measured before typing the fix.

---

## 2. Test-Driven Development (TDD: Red-Green-Refactor)

* **Phase 1 (Red)**: Write a focused unit or integration test reproducing the bug or asserting the new requirement. Run the test and verify that it fails for the *expected* reason.
* **Phase 2 (Green)**: Write the minimal amount of code necessary to make the failing test pass. Do not over-engineer or add unneeded features.
* **Phase 3 (Refactor)**: Clean up duplication, improve naming, and refine code structure while ensuring all tests continue to pass green.

---

## 3. Verification Before Completion Gate

* **Never Claim Completion Without Fresh Evidence**:
  - Do not assume code works because it looks correct.
  - Run the build, test suite, or linter AFTER the final edit has been written to disk.
  - Inspect the real process exit code (`$LASTEXITCODE -eq 0`) and read the actual test runner output (e.g., `X passed in Y seconds`).
* **Check Edge Cases & Regressions**:
  - Verify that existing test cases still pass alongside the new test.
  - If a test fails, do not ignore or disable it; diagnose and fix the root cause.

---

## 4. Grill & Git Guardrails

* **Pre-Build Clarity ("Grill-Me")**:
  - When a requirement is ambiguous or has multiple viable architectural paths, consult the user early with clear trade-offs rather than making assumptions.
* **Destructive Git Guardrails**:
  - **NEVER** run `git push --force` or `git push -f` on any shared or main branch.
  - **NEVER** run `git reset --hard` without verifying uncommitted user work is stashed or backed up.
  - Avoid destructive git actions that cannot be undone. Keep clean, atomic commit histories.
