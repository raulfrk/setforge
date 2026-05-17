---
name: wt-reference
description: worktrunk (binary `wt`) command reference and Beads integration patterns. Use when working with git worktrees, parallel agent workflows, the canonical bd-issue → wt-worktree loop, the `wt switch / list / remove / merge / step / hook / config` command surface, or hooks that fire on worktree lifecycle events.
---

# worktrunk (`wt`) command reference

worktrunk manages git worktrees for parallel agent workflows. Default location: `~/projects/worktrees/<slug>` per wt's `worktree-path` template (see `tracked/wt/config.toml`) — bd auto-discovers via git common-directory regardless of location.

*Footnote on `<slug>`: throughout this doc, `<slug>` is the user-facing placeholder for the worktree-path suffix. wt's actual template variable is `{{ branch | sanitize }}` — the sanitized branch name. In practice the two collapse because `wt switch --create <slug>` creates a branch literally named `<slug>`, so `<slug>` == `branch` at worktree-creation time. They diverge only if you rename the branch after creation (e.g. `git branch -m`); the worktree path keeps the original sanitized branch name while the branch itself has the new name.*

## Commands

- `wt switch --create <slug>` — create a worktree + branch from current HEAD; switch to it.
- `wt switch <slug>` — switch to an existing worktree (auto-cd if shell integration is set up via `wt config shell install`).
- `wt list` — list all worktrees and their status (clean / dirty / merged).
- `wt remove [<slug>]` — remove the current worktree (or named one); auto-deletes the branch if merged.
- `wt merge [<branch>]` — merge the current worktree's branch into target (default = main).
  - **Deployed default on this VM**: `--no-squash --ff-only` (set by `~/.config/worktrunk/config.toml`'s `[merge]` block — `squash = false`, `ff = true` — deployed by `setforge install`). Bare `wt merge` runs this mode: preserves the branch's commits on target, fast-forward only. This is what satisfies observation F (`Never squash review-fix commits into the implementation commit`) operationally, not just in intent. See `tracked/claude/superpowers-prefs.md` Phase 6.
  - **Upstream wt default**: `--squash` (collapse the branch into one commit on target). Reachable on this VM only by explicitly overriding the deployed config; use only when the branch's history is genuinely throwaway (e.g., a noisy WIP history collapsed for a leaf feature).
  - `wt merge --no-squash` — explicit form of the deployed default. Identical to bare `wt merge` on this VM; spell it out when documenting flows so the intent (preserve separate commits) survives a config change.
- `wt step <name>` — run an individual operation (used when scripting partial flows).
- `wt hook <name>` — run configured hooks (pre/post for switch/merge/remove).
- `wt config` — manage user and project configs (locations, hooks, aliases). `wt config shell install` writes a PATH-guarded eval into `~/.zshrc` / `~/.bashrc` enabling auto-cd on `wt switch`.

## Beads integration

- One bd issue = one worktree. Slug should match or include the bd issue ID (`wt switch --create setforge-g20-py-rewrite`).
- After `wt switch --create`, `bd update <id> --claim` locks the issue. Combo gives: isolated tree + claimed issue + atomic ownership.
- bd auto-discovers the worktree's database via git common-directory — no `--db` redirect.
- After `wt merge`: `bd close <id>`, then `wt remove` to clean both layers.

## Sibling-from-parent rebase pattern

When N sibling worktrees branch from a common parent (typical multi-bead batch shape) and the parent receives a review-fix commit during Phase 6 of the canonical flow (see `superpowers-prefs.md`), each sibling must rebase onto the updated parent before `wt merge --no-squash`:

```
# In each sibling worktree:
git fetch
git rebase <parent-branch>
# Resolve any conflicts surfaced by the rebase
wt merge --no-squash
```

`--no-squash` is mandatory here: each sibling typically carries its own implementation + review-fix commits, and squashing them at merge time would erase the review-fix audit trail (observation F / Phase 6).

**Conflict-free condition:** the parent's review-fix file footprint does NOT overlap with any sibling's file footprint. When they overlap, expect manual conflict resolution during the rebase.

**Convention when planning multi-worktree batches:** size sibling worktrees so their file footprints don't overlap with the parent's review-fix surface. If overlap is unavoidable, document the rebase plan up front.

*(empirical observation G from setforge-23k: the cxj/2rs/d6g/g4h May 2026 batch's rebases were conflict-free because no sibling touched the parent's review-fix files — `pyproject.toml`, `tests/test_capture_wizard.py` — but future batches with overlap will produce conflicts.)*

## Parallel dispatch via pre-prepared worktrees

When dispatching N subagents in parallel for a bd-issue-per-worktree batch, the orchestrator pre-creates each worktree before dispatch and the subagents operate inside user-named paths (NOT inside `.claude/worktrees/agent-*`). This sidesteps two failure modes of the `Agent` tool's `isolation: worktree` parameter — stale-base auto-worktrees AND sandbox-locked agent-path subtrees.

```
# Orchestrator side (in main worktree)
wt switch --create setforge-<id>-<slug>
# → worktree at <wt-path>; wt prints the path it created.
# The "Cannot change directory — shell requires restart" warning is
# benign for orchestration; the worktree IS created.

# Dispatch (Agent tool, NO isolation parameter).
# Subagent prompt template:
#   cd <wt-path>
#   uv sync --all-extras       # fresh worktree's .venv is pristine
#   bd update setforge-<id> --claim
#   ... implementation ...
```

For multi-bead waves: run `wt switch --create` N times serially (avoids git index lock races), then dispatch the N subagents in a single message of parallel Agent calls.

*(empirical 2026-05-12; see bd setforge-7gf)*

## Anti-patterns

- Don't use raw `git worktree add` when `wt` is available — bypasses configured location, hooks, and merge tracking.
- Don't create worktrees inside the repo — always use wt's configured location at `~/projects/worktrees/<slug>` (per `tracked/wt/config.toml`).
- Don't `wt merge` without verifying tests pass and the bd issue's acceptance criteria are met.
- Don't squash review-fix commits — use `wt merge --no-squash` when merging a branch with separate implementation + review-fix commits (observation F / Phase 6).
- Don't `wt remove` an unmerged worktree without explicit user confirmation — destructive.
- Don't run multiple agents in the same worktree — the whole point is isolation.
- Don't use `Agent` tool's `isolation: worktree` parameter for parallel dispatch in this build — auto-worktrees branch from a stale base AND their sandboxes deny git ops + file edits (empirical 2026-05-12; see bd setforge-7gf).
