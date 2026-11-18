---
name: verification-before-completion
description: Strict evidence-first completion gate. Forbids claiming a bug is fixed, tests pass, or a feature works without executing a fresh verification command AFTER the last edit and reading its real output and exit code.
source: https://github.com/obra/superpowers
category: debugging-qa
license: MIT (adapted for WSA)
---

# Verification Before Completion

Inspired by `obra/superpowers` (`verification-before-completion`).

## The Iron Law

```
NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE AFTER THE LAST EDIT
```

If you edited a single character after running a test or check, your prior pass result is STALE and invalid.

## The 5-Step Gate Function

BEFORE stating "fixed", "passes", "working", "complete", or "ready":
1. **IDENTIFY**: Which exact command, test suite, linter, build, or live browser check actually proves the claim?
2. **RUN**: Execute the FULL fresh check AFTER your final edit (never rely on an earlier run or a partial subset when a wider regression is possible).
3. **READ**: Inspect the COMPLETE stdout/stderr, actual pass/fail/skip counts, and exit code — exit code 0 alone is insufficient if 0 tests ran or warnings hid a broken import/render.
4. **VERIFY**: Does the observed output directly match every acceptance criterion in the user's request? For web UI/3D work, verify live rendering & interactions (`visual_inspect` / `browser_inspect`), not just static HTML parsing.
5. **REPORT HONESTLY**: Cite exact commands, observed counts, and file paths. If any check could not run or failed, explicitly state what failed or remained unverified.

## Red Flags (STOP Immediately)

- Using hedging phrases: "should work now", "probably fixed", "looks good to me", "seems fine".
- Expressing confidence or summarizing success BEFORE running the post-edit verification command.
- Claiming "end-to-end verified" when only mocked unit tests were run.
- Ignoring failing tests, console errors, or broken layout checks as "unrelated" without proving they were pre-existing.
