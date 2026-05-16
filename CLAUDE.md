# my-setup

Dotfiles + VSCode extensions, driven by a single Python CLI (`my-setup`) and a typed `my_setup.yaml`.

## The meta-twist: live vs tracked

`tracked/claude/*` is the source of truth for `~/.claude/*`. Edits to `~/.claude/CLAUDE.md` are ephemeral — only edits to `tracked/claude/CLAUDE.md` survive `my-setup install`. When I say "edit CLAUDE.md," confirm which one I mean unless context makes it obvious. Before any edit, run `diff -q ~/.claude/CLAUDE.md tracked/claude/CLAUDE.md` — drift means there are unsaved live edits to capture via `my-setup sync` first.

User-section markers in tracked CLAUDE.md (HTML comments around section bodies) make those regions per-host: edits to live `~/.claude/CLAUDE.md` between markers survive a re-install. The marker syntax requires a `host-local|shared` semantics keyword on both start and end markers:

```
<!-- my-setup:user-section start host-local NAME -->
... live edits to this body always survive re-install (host-specific) ...
<!-- my-setup:user-section end host-local NAME -->

<!-- my-setup:user-section start shared NAME -->
... live edits survive too, but tracked-side updates surface in the
    `install --reconcile-user-sections` wizard (rules that should
    propagate across hosts) ...
<!-- my-setup:user-section end shared NAME -->
```

End markers may carry an optional `hash=<sha256-hex>` segment that records the body's baseline hash; `install` rewrites it on every run so the three-way reconciler can tell pending-tracked drift from live edits.

## Profiles — always pass --profile=

Daily driver: `vm-headless`. Five profiles total — see [README.md](README.md). Never run a `my-setup` command without `--profile=`.

## Workflow verbs

- `uv run my-setup compare --profile=<name>` — read-only drift check (live vs tracked).
- `uv run my-setup sync --profile=<name>` — capture live edits into tracked/. Always `git diff` after to review. Drift on `preserve_user_keys_deep` sub-keys or top-level non-preserve keys triggers the merge wizard interactively; for non-interactive use pass `--auto=use-live` (silent-absorb, today's behavior) or `--auto=keep-tracked` (refuse to absorb).
- `uv run my-setup install --profile=<name>` — deploy tracked → live. Drift inside `shared` user-section markers triggers the reconcile wizard interactively when `--reconcile-user-sections` is passed; for non-interactive use pass `--auto=use-tracked` (deploy tracked-side updates over the live body) or `--auto=keep-live` (silence the warning, keep live). `--reconcile-user-sections` and `--auto=` are mutually exclusive (exit 2). Bare `install` warns once per shared-drifted file and keeps live. `host-local` sections are always preserved-live regardless of flags.
- `uv run my-setup revert --profile=<name>` — undo the most recent install/sync (file diffs via `patch -R` + extension reverse). Drift refuses cleanly; second invocation acts as redo. Transitions live at `~/.local/state/my-setup/transitions/` (kept indefinitely; pruning is a future bead).
- `uv run my-setup validate --profile=<name>` — config-shape check (schema + profile chain + Jinja2 + tracked srcs + claude_plugins references). No filesystem comparison; works offline. CI runs `validate --all`.

## Docker e2e tests

A 25-test end-to-end suite at `tests/docker/test_e2e_docker.py` exercises `install`/`sync`/`compare`/`revert`/`validate` against a fresh Debian 12 container with real `claude` and `code` binaries. It is the strongest behavior-preservation gate in this project.

- **Invocation:** `uv run pytest tests/docker/ -m e2e_docker -v` (unchanged; xdist auto-activates)
- **Parallel execution:** `-m e2e_docker` auto-activates pytest-xdist with `-n auto`. Override with `-n 0` for serial-mode debugging or `-n N` for a specific worker count. Runtime drops from ~8-10 min to ~3-4 min on a 4-core machine.
- **Runtime:** ~3-4 min on a 4-core machine (xdist parallel); ~8-10 min serial (`-n 0`).
- **When to run:** required on every Phase 7 (post-merge cross-cutting
  review). See `## Final checks (post-merge)` below.
- **Prerequisite:** `docker` on PATH; the suite skips when docker is missing
  (see `tests/docker/conftest.py`).

The suite is gated by `pytest -m e2e_docker` AND excluded from the default `pytest` run via `pyproject.toml`'s `addopts = "-m 'not e2e_docker'"`, so plain `uv run pytest` will not run it.

## Final checks (post-merge)

After merging a non-trivial branch into `main`, both of these must exit 0:

```sh
pre-commit run --all-files
uv run pytest tests/docker/ -m e2e_docker -v
```

`pre-commit` catches tool-version skew that per-worktree reviewers cannot see
— most importantly the ruff version mismatch the cxj batch only hit on first
push to main. The Docker e2e suite catches integration-emergent install /
sync / revert / plugin / extension behavior regressions that unit tests
cannot exercise.

This is the canonical Phase 7 (post-merge cross-cutting review) gate for
this project. See `tracked/claude/superpowers-prefs.md` Phase 7.

### Failure handling

A Docker e2e failure on Phase 7 is CRITICAL: a behavior the suite asserted
is now broken on `main`.

Default action:

1. `git revert <merge-commit>` — restore main to a known-good state.
2. `git checkout <feature-branch>`.
3. Reproduce locally, fix, push, re-PR, re-merge.
4. Re-run Phase 7 on the new merge.

Inline-fix on main (skip the revert) ONLY when both hold:
- the diff is one file and obviously the cause, AND
- the fix is narrowly scoped (one or two lines).

Filing a new bead is appropriate for follow-up work but does NOT replace
the revert-or-fix step. A red main is not OK. Either path (revert-and-re-PR
OR inline-fix) must Re-run Phase 7 to confirm main is green.

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
