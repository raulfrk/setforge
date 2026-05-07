# my-setup

Dotfiles + VSCode extensions, driven by a single Python CLI (`my-setup`) and a typed `my_setup.yaml`.

## The meta-twist: live vs tracked

`tracked/claude/*` is the source of truth for `~/.claude/*`. Edits to `~/.claude/CLAUDE.md` are ephemeral — only edits to `tracked/claude/CLAUDE.md` survive `my-setup install`. When I say "edit CLAUDE.md," confirm which one I mean unless context makes it obvious. Before any edit, run `diff -q ~/.claude/CLAUDE.md tracked/claude/CLAUDE.md` — drift means there are unsaved live edits to capture via `my-setup sync` first.

User-section markers in tracked CLAUDE.md (HTML comments around section bodies) make those regions per-host: edits to live `~/.claude/CLAUDE.md` between markers survive a re-install.

## Profiles — always pass --profile=

Daily driver: `vm-headless`. Five profiles total — see [README.md](README.md). Never run a `my-setup` command without `--profile=`.

## Workflow verbs

- `uv run my-setup compare --profile=<name>` — read-only drift check (live vs tracked).
- `uv run my-setup sync --profile=<name>` — capture live edits into tracked/. Always `git diff` after to review.
- `uv run my-setup install --profile=<name>` — deploy tracked → live.

## The four-tool stack

Beads + Superpowers configured by this repo. Repomix + worktrunk installed externally; `my-setup install` does NOT bootstrap them.

## Adding tracked files and extensions

- Dotfile: edit `my_setup.yaml` to add an entry under `dotfiles:` and reference it from the relevant profile, then place the source file under `tracked/<src>`.
- Extension: add the extension ID to the profile's `extensions.include:` list in `my_setup.yaml`. (Pillar 2 will add an `ext` subcommand that edits this YAML in place.)

## Host-local, never-tracked

`~/.claude/additional-content.md` is intentionally untracked per host. `my-setup install` creates a stub if missing. Never commit its content.

## Don't-do list

- Don't push to git remotes automatically — I push when ready.
- Don't auto-edit `my_setup.yaml`'s extension lists — those land via the dedicated `ext` subcommand once Pillar 2 ships.
