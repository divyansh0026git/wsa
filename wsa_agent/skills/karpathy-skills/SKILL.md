---
name: karpathy-skills
description: Daily clean coding discipline inspired by Andrej Karpathy's LLM coding guidelines. Eliminates guessing, speculative over-abstraction, over-editing unrelated code, and drift from verifiable goals.
source: https://github.com/multica-ai/andrej-karpathy-skills
category: engineering
license: MIT (adapted for WSA)
---

# Karpathy Skills — 4-Pillar Daily Engineering Discipline

Inspired by `multica-ai/andrej-karpathy-skills`. Counters the four most common AI coding failure modes: silent assumptions, premature bloat, collateral edits, and unverified drift.

## The 4 Core Principles

1. **Think Before Coding (Don't Assume — Surface Tradeoffs)**:
   - State your assumptions explicitly before mutating code. If a requirement has multiple valid interpretations with meaningful consequences, clarify or pick the cleanest reversible default and call it out directly.
   - Push back gently if a simpler architecture solves the real goal faster and cleaner.
2. **Simplicity First (Zero Speculative Bloat)**:
   - Write the minimum clean, idiomatic code that fully solves the active task — no unused factory layers, no single-use wrapper classes, no speculative configurability for hypothetical future requirements, no dead error branches for impossible states.
   - If 200 lines can be expressed with equal clarity in 50 lines, choose the 50 lines.
3. **Surgical Changes (Touch Only What You Own)**:
   - Every modified line must trace directly to the user's request.
   - Do NOT reformat untouched files, rename working variables, reorder unrelated imports, or "clean up" adjacent code outside the task scope.
   - Remove ONLY imports/variables/helpers that YOUR OWN changes made unused; mention pre-existing dead code instead of silently deleting it.
4. **Goal-Driven Execution (Verifiable Milestones)**:
   - Translate vague goals into concrete, testable acceptance criteria before editing:
     - "Fix bug" -> write/run reproducing check -> patch root cause -> prove check passes.
     - "Add feature" -> define edge cases & assertions -> implement -> prove all pass.
     - "Refactor" -> verify tests pass before AND after with zero behavioral regressions.
   - For multi-step tasks, state a crisp numbered checklist (`step -> exact verification check`) and verify every item honestly.
