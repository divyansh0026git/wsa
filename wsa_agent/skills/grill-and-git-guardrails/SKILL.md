---
name: grill-and-git-guardrails
description: Pre-build requirement clarification discipline (grill-me) paired with strict destructive-git safety guardrails blocking force pushes, hard resets, and accidental history loss.
source: https://github.com/mattpocock/skills
category: engineering
license: MIT (adapted for WSA)
---

# Grill-Me & Git Guardrails — Pre-Build Clarity + Repository Safety

Inspired by `mattpocock/skills` (`grill-me` + `git-guardrails-claude-code`).

## Part 1: Pre-Build Clarity (`grill-me`)

Use when the user asks to plan/grill/interview on a spec, or when a greenfield request has critical unstated decisions:
1. **Walk the Dependency Tree**: Identify the load-bearing unknowns first (target users, core workflow, data/auth persistence, visual direction, edge/error states, non-goals) rather than trivial naming details.
2. **Ground in the Repo First**: Answer every question you CAN resolve by inspecting existing workspace files before asking the user anything.
3. **Crisp Targeted Questions**: Ask 1–3 focused, concrete questions with a recommended default option for each so decisions converge rapidly instead of overwhelming the user.
4. **Lock a Crisp Contract**: Once requirements are aligned, summarize the locked scope, acceptance checks, and out-of-scope boundaries in 5–8 bullets before writing code.

## Part 2: Git Guardrails (Zero Destructive Accidents)

NEVER execute destructive git operations (via `git` tool OR `run_command` shell) unless the user explicitly requested that exact command and acknowledged data loss:
- **BLOCKED by default**:
  - `git push --force` / `git push -f` / `git push --force-with-lease` to shared or `main`/`master` branches
  - `git reset --hard` (destroys uncommitted working-tree changes)
  - `git clean -fd` / `git clean -fdx` (permanently deletes untracked files)
  - `git checkout -- .` / `git restore .` (wipes unstaged edits across the repo)
  - `git branch -D` (force-deletes unmerged branches)
  - `git rebase` on pushed/shared history or `git commit --amend` on already-pushed commits
- **Safe Alternatives**:
  - Prefer `git status`, `git diff`, `git log`, `git stash push -m "..."`, new commits over amending pushed history, and WSA's reversible `rollback` / `/undo` checkpoints.
  - Always check `git status` and `git diff --stat` before committing so secrets (`.env`, auth tokens, cookie files) are never staged.
