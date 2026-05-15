# my-setup

Dotfiles + VSCode extensions, driven by a single Python CLI (`my-setup`) and a typed `my_setup.yaml`.

## The meta-twist: live vs tracked

`tracked/claude/*` is the source of truth for `~/.claude/*`. Edits to `~/.claude/CLAUDE.md` are ephemeral — only edits to `tracked/claude/CLAUDE.md` survive `my-setup install`. When I say "edit CLAUDE.md," confirm which one I mean unless context makes it obvious. Before any edit, run `diff -q ~/.claude/CLAUDE.md tracked/claude/CLAUDE.md` — drift means there are unsaved live edits to capture via `my-setup sync` first.

User-section markers in tracked CLAUDE.md (HTML comments around section bodies) make those regions per-host: edits to live `~/.claude/CLAUDE.md` between markers survive a re-install.

## Profiles — always pass --profile=

Daily driver: `vm-headless`. Five profiles total — see [README.md](README.md). Never run a `my-setup` command without `--profile=`.

## Workflow verbs

- `uv run my-setup compare --profile=<name>` — read-only drift check (live vs tracked).
- `uv run my-setup sync --profile=<name>` — capture live edits into tracked/. Always `git diff` after to review. Drift on `preserve_user_keys_deep` sub-keys or top-level non-preserve keys triggers the merge wizard interactively; for non-interactive use pass `--auto=use-live` (silent-absorb, today's behavior) or `--auto=keep-tracked` (refuse to absorb).
- `uv run my-setup install --profile=<name>` — deploy tracked → live.
- `uv run my-setup revert --profile=<name>` — undo the most recent install/sync (file diffs via `patch -R` + extension reverse). Drift refuses cleanly; second invocation acts as redo. Transitions live at `~/.local/state/my-setup/transitions/` (kept indefinitely; pruning is a future bead).
- `uv run my-setup validate --profile=<name>` — config-shape check (schema + profile chain + Jinja2 + tracked srcs + claude_plugins references). No filesystem comparison; works offline. CI runs `validate --all`.

## Docker e2e tests

A 25-test end-to-end suite at `tests/docker/test_e2e_docker.py` exercises `install`/`sync`/`compare`/`revert`/`validate` against a fresh Debian 12 container with real `claude` and `code` binaries. It is the strongest behavior-preservation gate in this project.

- **Invocation:** `uv run pytest tests/docker/ -m e2e_docker -v`
- **Runtime:** ~5 min.
- **When to run:** required on every Phase 7 (post-merge cross-cutting
  review). See `## Final checks (post-merge)` below.
- **Prerequisite:** `docker` on PATH; the suite skips when docker is missing
  (see `tests/docker/conftest.py`).

The suite is gated by `pytest -m e2e_docker` AND excluded from the default `pytest` run via `pyproject.toml`'s `addopts = "-m 'not e2e_docker'"`, so plain `uv run pytest` will not run it.

## Final checks (post-merge)

After merging a non-trivial branch into `main`, run `pre-commit run --all-files` as the canonical post-merge verification. Catches issues that per-worktree reviewers cannot see — most importantly tool version skew between the pre-commit pinned versions and uv-resolved tooling (the cxj batch shipped a ruff version mismatch that only pre-commit caught on first push to main). This is the canonical Phase 7 (post-merge cross-cutting review) final-check command for this project. See `tracked/claude/superpowers-prefs.md` Phase 7.

## The four-tool stack

Beads + Superpowers configured by this repo. Repomix + worktrunk installed externally; `my-setup install` does NOT bootstrap them.

## Adding tracked files and extensions

- Dotfile: edit `my_setup.yaml` to add an entry under `dotfiles:` and reference it from the relevant profile, then place the source file under `tracked/<src>`.
- Extension: add the extension ID to the profile's `extensions.include:` list in `my_setup.yaml`. (Pillar 2 will add an `ext` subcommand that edits this YAML in place.)

## Host-local, never-tracked

`~/.claude/additional-content.md` is intentionally untracked per host. `my-setup install` creates a stub if missing. Never commit its content.

`~/.vscode-server/data/Machine/settings.json` may carry host-local keys (e.g. `claudeCode.allowDangerouslySkipPermissions`) that are intentionally not in tracked. The profile's `preserve_user_keys` overlays those keys from live to tracked on `install`, and `capture` strips them from tracked, so they stay host-local without manual intervention. Comments in the JSONC settings file are preserved end-to-end.

## Don't-do list

- Don't push to git remotes automatically — I push when ready.
- Don't auto-edit `my_setup.yaml`'s extension lists — those land via the dedicated `ext` subcommand once Pillar 2 ships.
