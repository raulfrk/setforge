---
name: wt-reference
description: worktrunk (binary `wt`) command reference and Beads integration patterns. Use when working with git worktrees, parallel agent workflows, the canonical bd-issue → wt-worktree loop, the `wt switch / list / remove / merge / step / hook / config` command surface, or hooks that fire on worktree lifecycle events.
---

# worktrunk (`wt`) command reference

worktrunk manages git worktrees for parallel agent workflows. Default location: `~/projects/worktrees/<slug>` — same convention bd uses for auto-discovery via git common-directory.

## Commands

- `wt switch --create <slug>` — create a worktree + branch from current HEAD; switch to it.
- `wt switch <slug>` — switch to an existing worktree (auto-cd if shell integration is set up via `wt config shell install`).
- `wt list` — list all worktrees and their status (clean / dirty / merged).
- `wt remove [<slug>]` — remove the current worktree (or named one); auto-deletes the branch if merged.
- `wt merge [<branch>]` — merge the current worktree's branch into target (default = main).
- `wt step <name>` — run an individual operation (used when scripting partial flows).
- `wt hook <name>` — run configured hooks (pre/post for switch/merge/remove).
- `wt config` — manage user and project configs (locations, hooks, aliases). `wt config shell install` writes a PATH-guarded eval into `~/.zshrc` / `~/.bashrc` enabling auto-cd on `wt switch`.

## Beads integration

- One bd issue = one worktree. Slug should match or include the bd issue ID (`wt switch --create dotfiles-g20-py-rewrite`).
- After `wt switch --create`, `bd update <id> --claim` locks the issue. Combo gives: isolated tree + claimed issue + atomic ownership.
- bd auto-discovers the worktree's database via git common-directory — no `--db` redirect.
- After `wt merge`: `bd close <id>`, then `wt remove` to clean both layers.

## Anti-patterns

- Don't use raw `git worktree add` when `wt` is available — bypasses configured location, hooks, and merge tracking.
- Don't create worktrees inside the repo — always use wt's `~/projects/worktrees/` location.
- Don't `wt merge` without verifying tests pass and the bd issue's acceptance criteria are met.
- Don't `wt remove` an unmerged worktree without explicit user confirmation — destructive.
- Don't run multiple agents in the same worktree — the whole point is isolation.
