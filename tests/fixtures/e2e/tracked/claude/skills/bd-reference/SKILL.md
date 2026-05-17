---
name: bd-reference
description: Beads task-tracking command reference. Invoke at the first sign of bd involvement in a session (other than `bd prime`, which the SessionStart hook fires). Triggers include: any `bd` command (create/update/show/list/ready/close/note/comment/dep/search/recall), claiming an issue when work begins, looking up flag syntax, deciding which persistence layer (memory/note/comment/structured field) to use, lifecycle verbs (defer/supersede/stale/orphans), quality flags (--validate/--acceptance/--design/--notes), or handoff patterns between sessions or agents. If the next action involves creating, updating, claiming, closing, or querying a bd issue, invoke this skill first.
---

# Beads command reference

The beads home is at `~/.beads/` (set via `BEADS_DIR`); each project gets its own database inside it (`~/.beads/embeddeddolt/<project>/`), auto-created on first write, with the issue prefix derived from the repo name. Issues do NOT cross databases unless `bd repo add` (multi-repo hydration) or `bd federation` is configured. Memories are cross-project (separate storage from issues).

All git worktrees of the same repo share the parent's beads database via git common-directory discovery — no manual `--db` redirect. `bd worktree list` shows the redirect state per worktree. Default `bd worktree create <name>` form nests at `./<name>` and writes a `.gitignore` entry; prefer `wt switch --create <slug>` which lands the worktree at `~/projects/worktrees/<slug>` (the configured location for this VM; see `tracked/wt/config.toml`). `git worktree add ~/projects/worktrees/<slug>` also works and is auto-discovered.

## Starting work on an issue

The moment you begin substantive work on a bd issue — reading code for
the fix, sketching a design, writing tests, anything more than a quick
look — claim it:

```
bd update <id> --claim
```

`--claim` is atomic: assignee = you AND status = `in_progress`.
Idempotent if already yours. **Run this before code work, not after.**
It tells anyone watching the queue the issue is being touched, and it
produces a clean started-at timestamp.

If you stop without finishing, leave a handoff trail with
`bd comment <id> "stopped because X, next step Y"` and let the
assignment stand; the comment thread is the handoff signal.

## Persistence taxonomy

| Layer | Command | Scope | Use for |
|---|---|---|---|
| Memory | `bd remember "<insight>" [--key <name>]` | Cross-project, persistent | Generalizable lessons, preferences, gotchas. Surfaced via `bd memories` / `bd recall`. |
| Issue notes | `bd note <id> "<text>"` | One issue, free-form body field | Context that belongs to the issue: research, dead-ends, decisions made while working it. Appended, not overwritten. |
| Comments | `bd comment <id> "<text>"` | One issue, timestamped thread | Conversation, status updates, **handoff messages** between sessions/agents. View with `bd comments <id>`. |
| Description / design / acceptance | `bd update <id> --description/--design/--acceptance "..."` | One issue, structured fields | The intent, criteria, and design decisions of the issue itself. Edit when intent changes. |

**Memory:** `bd remember "<text>" [--key <name>]` (same `--key` updates in place), `bd memories [search]`, `bd recall <key>`, `bd forget <key>`.

**Multi-line text:** `bd note` / `bd comment` / `bd update --description` all accept `--stdin` or `--file <path>` — use these instead of long quoted CLI args. Note: `--design` uses `--design-file <path>` (not `--file`); `--acceptance` has no file variant — use shell expansion (`--acceptance "$(cat /tmp/path)"`).

**Spec vs bd contract:** The spec file archived at `~/.claude/projects/{cwd-slug}/specs/...` is a snapshot of what was agreed at brainstorm time. The bd issue's `--design` / `--acceptance` / `--notes` is the **durable contract** — update it when scope changes; treat the spec file as a historical record, not a living document. *(empirical observation I from dotfiles-23k.)*

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
- `--acceptance "..."` — acceptance criteria (checked by `--validate`). Prefer **concrete commands that exit 0** over abstract counts. "lint count drops to zero in non-deferred categories" is hard to verify; "`uv run ruff check` exits 0 in CI" is binary. *(empirical observation B from dotfiles-23k: abstract count acceptance passed spec review but CI still broke on first push because the criterion didn't specify command-level success.)*
- `--design "..."` — design notes.
- `--notes "..."` (initial) or `bd note <id>` later — supplementary context.
- `--parent <id>` — file as hierarchical child.
- `--dry-run` — preview without writing.

## Sizing follow-up beads

When measure-then-decide work surfaces follow-up beads with concrete counts (e.g., "fix N sites of category X"), measure **post-auto-fix**, not pre-fix. Tooling like `ruff check --fix --unsafe-fixes` can materially change the count before you file. *(empirical observation C from dotfiles-23k: cxj originally sized ANN201 follow-up at 34 sites; auto-fix collapsed it to 2. RUF059 was 38 → 0. Three of seven planned bead categories materially changed.)*

## Lifecycle

- `bd defer <id> --until="date"` — hide from `bd ready` until date (use for "not now", not for completed work).
- `bd undefer <id>` / `bd reopen <id>` — restore deferred / reopen closed.
- `bd supersede <id> --with=<new-id>` — mark replaced by a newer issue.
- `bd stale` / `bd orphans` — find untouched issues / broken-dependency issues.
