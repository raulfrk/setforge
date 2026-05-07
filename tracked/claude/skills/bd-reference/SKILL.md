---
name: bd-reference
description: Beads task-tracking command reference and workflow taxonomy. Use when working with bd commands, dependency graphs, lifecycle (defer/supersede/stale/orphans), quality flags (--validate/--acceptance/--design/--notes), handoff patterns between sessions or agents, multi-project hydration, or when specific bd command syntax is needed.
---

# Beads command reference

The beads home is at `~/.beads/` (set via `BEADS_DIR`); each project gets its own database inside it (`~/.beads/embeddeddolt/<project>/`), auto-created on first write, with the issue prefix derived from the repo name. Issues do NOT cross databases unless `bd repo add` (multi-repo hydration) or `bd federation` is configured. Memories are cross-project (separate storage from issues).

All git worktrees of the same repo share the parent's beads database via git common-directory discovery — no manual `--db` redirect. `bd worktree list` shows the redirect state per worktree. Default `bd worktree create <name>` form nests at `./<name>` and writes a `.gitignore` entry; pass an explicit path under `~/projects/worktrees/` instead. `git worktree add ~/projects/worktrees/<name>` also works and is auto-discovered.

## Persistence taxonomy

| Layer | Command | Scope | Use for |
|---|---|---|---|
| Memory | `bd remember "<insight>" [--key <name>]` | Cross-project, persistent | Generalizable lessons, preferences, gotchas. Surfaced via `bd memories` / `bd recall`. |
| Issue notes | `bd note <id> "<text>"` | One issue, free-form body field | Context that belongs to the issue: research, dead-ends, decisions made while working it. Appended, not overwritten. |
| Comments | `bd comment <id> "<text>"` | One issue, timestamped thread | Conversation, status updates, **handoff messages** between sessions/agents. View with `bd comments <id>`. |
| Description / design / acceptance | `bd update <id> --description/--design/--acceptance "..."` | One issue, structured fields | The intent, criteria, and design decisions of the issue itself. Edit when intent changes. |

**Memory:** `bd remember "<text>" [--key <name>]` (same `--key` updates in place), `bd memories [search]`, `bd recall <key>`, `bd forget <key>`.

**Multi-line text:** `bd note` / `bd comment` / `bd update --description` all accept `--stdin` or `--file <path>` — use these instead of long quoted CLI args.

## Handoffs (between sessions, agents, or to a human)

- `bd update <id> --claim` — atomically set assignee=you + status=in_progress. Idempotent if already yours.
- `bd assign <id> <name>` — hand to someone (or `""` to unassign).
- `bd list --assignee <name>` — find work owned by an actor; `--no-assignee` finds unowned.
- Before handing off: `bd comment <id> "stopped because X, next step is Y"` — leaves a timestamped, threaded handoff message. Prefer comments over notes for handoffs.
- `bd human <id>` — flag for a human decision. `bd human list/respond/dismiss/stats`. Use when a load-bearing call needs the user.

## Quick reference

- `bd ready [--explain]` — find available work; `--explain` shows why each is ready/blocked.
- `bd q "<title>"` — quick capture, prints only the new ID.
- `bd show <id>` — full view (description, design, acceptance, notes, children, deps).
- `bd children <parent-id>` — list everything under a parent (includes closed by default).
- `bd close <id1> <id2> ...` — complete one or more issues; `--reason "..."` and `--suggest-next` available.
- `bd dep add <issue> <depends-on>` — add dependency; `bd blocked` lists blocked issues.
- `bd search <query>` — text search across issues.
- `bd doctor` / `bd preflight` — health check / pre-PR checklist.

## Quality flags on `create` / `update`

- `--validate` — fail if required sections are missing for the type.
- `--acceptance "..."` — acceptance criteria (checked by `--validate`).
- `--design "..."` — design notes.
- `--notes "..."` (initial) or `bd note <id>` later — supplementary context.
- `--parent <id>` — file as hierarchical child.
- `--dry-run` — preview without writing.

## Lifecycle

- `bd defer <id> --until="date"` — hide from `bd ready` until date (use for "not now", not for completed work).
- `bd undefer <id>` / `bd reopen <id>` — restore deferred / reopen closed.
- `bd supersede <id> --with=<new-id>` — mark replaced by a newer issue.
- `bd stale` / `bd orphans` — find untouched issues / broken-dependency issues.
