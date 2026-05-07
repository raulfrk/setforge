# my-setup

Dotfiles + VSCode extensions, dotdrop-managed. About to gain a Python CLI that replaces the Makefile orchestration (branch `python-rewrite-design`).

## The meta-twist: live vs tracked

`tracked/claude/*` is the source of truth for `~/.claude/*`. Edits to `~/.claude/CLAUDE.md` are ephemeral — only edits to `tracked/claude/CLAUDE.md` survive `make install`. When I say "edit CLAUDE.md," confirm which one I mean unless context makes it obvious. Before any edit, run `diff -q ~/.claude/CLAUDE.md tracked/claude/CLAUDE.md` — drift means there are unsaved live edits to capture via `make sync` first.

## Profiles — always pass PROFILE=

Daily driver: `vm-headless`. Five profiles total — see [README.md](README.md). Never run a `make` target without `PROFILE=`.

## Workflow verbs

- `make compare PROFILE=<name>` — read-only drift check (live vs tracked).
- `make sync PROFILE=<name>` — capture live edits + extensions into tracked/. Always `git diff` after to review.
- `make install PROFILE=<name>` — deploy tracked → live + reinstall extensions.

## The four-tool stack

Beads + Superpowers configured by this repo. Repomix + worktrunk installed externally; `make install` does NOT bootstrap them.

## Adding tracked files and extensions

- Dotfile: `uvx dotdrop --cfg ~/my-setup/config.yaml import -p <profile> <live-path>`, then edit `config.yaml` if needed.
- Extension: install in VSCode, then `make sync PROFILE=...`. Never hand-edit `vscode-extensions/<profile>.txt`.

## Host-local, never-tracked

`~/.claude/additional-content.md` is intentionally untracked per host. `make install` creates a stub if missing. Never commit its content.

## Python rewrite (branch: python-rewrite-design)

Goal: replace the Makefile orchestration with a Python CLI. Scope (thin shim vs wider replacement of dotdrop) is being decided in a separate session — defer Python-tooling-specific decisions (ruff/mypy strictness, src layout, entry point, test layout) until that session resolves. The Makefile is the legacy orchestrator; do not add features to both.

## Don't-do list

- Don't push to git remotes automatically — I push when ready.
- Don't add features to the Makefile and the Python rewrite simultaneously.
- Don't auto-create `vscode-extensions/<profile>.txt` content; capture via `make sync`.
