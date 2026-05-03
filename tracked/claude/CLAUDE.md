<!--
This file is global. It applies to every project on this VM and is appended on top of Claude Code's
system prompt, which already covers: concision, no speculative error handling, no unnecessary comments,
no backwards-compat shims, confirm-before-destructive. Do not restate those.

Pruning rule (Anthropic): for each line, ask "would removing this cause Claude to make a mistake?"
If no, cut it. CLAUDE.md is advisory, not enforced — for hard rules, write a PreToolUse hook.
-->

@header.md

## Communication style

Optimize for me learning, not for task throughput.

- For any decision with multiple possible approaches, present it with appropriate trade-offs and let me pick. If multiple approaches are not clear, run adversarial agents to produce a list of the top 4 possible approaches with strengths and tradeoffs. I will review, ask questions and make a decision.
- You can make changes to code yourself but it needs to be done after entering plan mode first. EnterPlanMode should be invoked every time there is either a multi-step process you are about to begin, you are about to produce code changes of significant scale (not just one or two lines), you are about to suggest design/architectural paths/decisions. This includes the cases where you are in auto mode. Any plan you produce should have code fragments that outline critical functionality that the user can judge. Ideally the user would implement themselves the features, but in some cases Claude would do a faster job while still preserving decent quality. For those scenarios the user should always be asked to review.
- The user may interact with you by adding `TODO(claude) - ` within the code, these are often comments or suggestions as to what Claude should do. Always try to find these and take those suggestions in. These should not be taken as instructions to fully write the code yourself, instead they should be interpreted as hints or research starting point. The user should be asked to write most of the significant code with `TODO(human) - `
- QUESTION(human) can be used by claude to ask user to answer specific questions about intent/architecture or how the code works. This is intended to ensure that the human is fully aware of how the implementation is structured and how it is meant to function.
- QUESTION(claude) can be used by human to ask claude specific questions about segments of code, the expectation here is that claude will thoroughly analyze the question as well as the context around around it and provide the human an answer. Once the answer is provided, the human should be asked if they are satisfied with the answer and whether that question can be removed.
- You are absolutely allowed and encouraged to facilitate the development process by creating the scaffolding/stubs for code and especially tests. These are exempt from the plan-mode first rule mentioned above.
- For design/architectural decisions, load-bearing decisions, specific implementation details, functionality and UX decisions always challenge me by running adversarial agents and producing a series of brief questions about why it was done a certain way, what are the general standards we want to define in the project/codebase and is this in line with them? Propose other viable options where appropriate, but ensure you do thorough research prior to that to ensure that your questions and challenging acts are based on facts.
- Try to always verify claims no matter what, otherwise there is a high risk you might be misguiding the user.
- For long-running tasks provide checkpoints where you inform the user about the progress and provide brief insights of the various phases that were done and that are next.

## Code style

- Comments and documentation: Comments should be short and provide meaningful information as to why something was done if that cannot be immediately understood from the code. If a comment does not provide any meaningful value to the user (e.g. why a certain decision was made?), then it should not be added. Avoid narrating history or how a certain component was decided or implemented. Avoid describing non-actions ("intentionally not pinned") and especially avoid restating what the code already does.
- Additionally, you are allowed to use section dividers in code, but those should be used sparingly and only to facilitate navigation throughout the code.

## Beads (cross-project task tracking)

You have access to **bd** (Beads) — an issue tracker. The beads home is at `~/.beads/` (set via `BEADS_DIR`), but **each project gets its own database** inside it (`~/.beads/embeddeddolt/<project>/`), auto-created on first write, with the issue prefix derived from the repo name (e.g. `dotfiles-jxt`). Issues do **not** cross databases unless you set up `bd repo add` (multi-repo hydration) or `bd federation`. `bd prime` runs automatically at session start and before compaction.

**Worktrees:** All git worktrees of the same repo share the parent's beads database via git common-directory discovery — no manual `--db` redirect needed. **Always create worktrees under `~/projects/worktrees/`** — never inside the repo. The default `bd worktree create <name>` form nests at `./<name>` and writes a `.gitignore` entry, which we don't want; pass an explicit path instead (e.g. `bd worktree create ~/projects/worktrees/dotfiles-feature-x`). `git worktree add ~/projects/worktrees/<name>` also works and is auto-discovered by beads. `bd worktree list` shows the redirect state per worktree.

**Rules:**
- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists.
- Use `bd remember` for persistent cross-session knowledge — do NOT use MEMORY.md files.
- Run `bd prime` for the full command reference and current context.

**Quick reference:**
- `bd ready` — find available work
- `bd q "<title>"` — quick capture, outputs only the new ID
- `bd show <id>` — view issue details
- `bd update <id> --claim` — claim work atomically
- `bd close <id>` — complete an issue
- `bd remember "<insight>"` — save a learning or preference

**Session close:**
- File issues for any incomplete work.
- Update or close in-progress items.
- Never push to git remotes automatically — the user pushes when ready.

@superpowers-prefs.md

@additional-content.md
