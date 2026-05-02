# Private journal (private-journal-mcp)

The journal captures durable insights and decisions for retrieval across
sessions. Use it as a working bookend, not as ambient narration.

## When to journal

Journal at clear bookends, not at every step:

- After completing a non-trivial task (a feature lands, a tricky bug
  is solved, a design decision is locked).
- When a recurring pattern or anti-pattern shows up worth remembering
  next time.
- When the user explicitly says "journal this" / "save this for later".

Skip routine work (renames, formatting, single-line fixes) and skip the
"insight-after-every-tool-call" failure mode.

## What to capture, by section

- `technical_insights` — novel patterns, failed approaches, what made
  the difference, anti-patterns to avoid next time.
- `project_notes` — project-specific decisions and the *why* behind
  them (not the *what* — that lives in the code).
- `user_context` — preferences, working patterns, things the user
  values that aren't already in CLAUDE.md.
- `reflections` — meta-observations about the collaboration itself.
- Skip `world_knowledge` and `observations` unless the entry genuinely
  belongs there.

## Search before acting on related work

Before starting a task that resembles past work, call `search_journal`
with a natural-language query (e.g. "retry strategy decisions",
"flaky-test debugging sessions"). Don't repeat past mistakes; don't
re-derive what the journal already settled. Use `list_recent_entries`
for chronological browsing when semantic search isn't the right shape.

## Promotion to the wiki

The journal is for raw thinking. The llm-wiki is for distilled durable
knowledge. Periodically (weekly, or when a topic recurs):

- Review recent entries via `list_recent_entries`.
- For insights that appear 3+ times or solve recurring problems, ask
  the user: "promote this to the wiki?"
- Don't auto-promote — curation is manual.

## Privacy

The journal is local-only and never leaves the machine. Safe for
candid observations about the work, the user's preferences, and
partial reasoning. Treat it as a private notebook, not a public
artifact.
