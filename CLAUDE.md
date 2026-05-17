# setforge

Tracked-file + VSCode-extension + Claude-plugin orchestration CLI. The engine repo (this one) ships the `setforge` tool; the user's personal config (a `my_setup.yaml` + `tracked/` tree) lives in a SEPARATE config repo per the setforge-2ba.4 split. The config repo is discovered via the source-layer (CLI `--source` > `SETFORGE_SOURCE` env > `~/.config/setforge/local.yaml` `source:` block > CWD fallback). The author's personal config now lives at `raulfrk/setforge-config` (private).

## The meta-twist: live vs tracked

For setforge users (including the author): the user's config repo's `tracked/claude/*` is the source of truth for `~/.claude/*`. Edits to `~/.claude/CLAUDE.md` are ephemeral — only edits to the CONFIG REPO's `tracked/claude/CLAUDE.md` survive `setforge install`. When working on the author's config: edit `~/.local/share/setforge/sources/setforge-config/tracked/claude/CLAUDE.md` (or wherever the config repo is cloned), not the live file. Before any edit, run `diff -q ~/.claude/CLAUDE.md <config-repo>/tracked/claude/CLAUDE.md` — drift means there are unsaved live edits to capture via `setforge sync` first.

User-section markers in tracked CLAUDE.md (HTML comments around section bodies) make those regions per-host: edits to live `~/.claude/CLAUDE.md` between markers survive a re-install. The marker syntax requires a `host-local|shared` semantics keyword on both start and end markers:

```
<!-- setforge:user-section start host-local NAME -->
... live edits to this body always survive re-install (host-specific) ...
<!-- setforge:user-section end host-local NAME -->

<!-- setforge:user-section start shared NAME -->
... live edits survive too, but tracked-side updates surface in the
    `install --reconcile-user-sections` wizard (rules that should
    propagate across hosts) ...
<!-- setforge:user-section end shared NAME -->
```

End markers may carry an optional `hash=<sha256-hex>` segment that records the body's baseline hash; `install` rewrites it on every run so the three-way reconciler can tell pending-tracked drift from live edits.

## Profiles — always pass --profile=

Profiles are defined in the USER's config repo's `my_setup.yaml`. The author's daily-driver profile is `vm-headless`. Never run a `setforge` command without `--profile=`.

## Workflow verbs

- `uv run setforge compare --profile=<name>` — read-only drift check (live vs tracked).
- `uv run setforge sync --profile=<name>` — capture live edits into tracked/. Always `git diff` after to review. Drift on `preserve_user_keys_deep` sub-keys or top-level non-preserve keys triggers the merge wizard interactively; for non-interactive use pass `--auto=use-live` (silent-absorb, today's behavior) or `--auto=keep-tracked` (refuse to absorb).
- `uv run setforge install --profile=<name>` — deploy tracked → live. Drift inside `shared` user-section markers triggers the reconcile wizard interactively when `--reconcile-user-sections` is passed; for non-interactive use pass `--auto=use-tracked` (deploy tracked-side updates over the live body) or `--auto=keep-live` (silence the warning, keep live). `--reconcile-user-sections` and `--auto=` are mutually exclusive (exit 2). Bare `install` warns once per shared-drifted file and keeps live. `host-local` sections are always preserved-live regardless of flags.
- `uv run setforge revert --profile=<name>` — undo the most recent install/sync (file diffs via `patch -R` + extension reverse). Drift refuses cleanly; second invocation acts as redo. Transitions live at `~/.local/state/setforge/transitions/` (kept indefinitely; pruning is a future bead).
- `uv run setforge validate --profile=<name>` — config-shape check (schema + profile chain + Jinja2 + tracked srcs + claude_plugins references). No filesystem comparison; works offline. CI runs `validate --all`.

## Docker e2e tests

A 49-test end-to-end suite at `tests/docker/test_e2e_docker.py` exercises `install`/`sync`/`compare`/`revert`/`validate` against a fresh Debian 12 container with real `claude` and `code` binaries — the canonical behavior-preservation gate for this project.

- **Invocation:** `uv run pytest tests/docker/ -m e2e_docker -v` (unchanged; xdist auto-activates)
- **Parallel execution:** `-m e2e_docker` auto-activates pytest-xdist with `-n auto`. Override with `-n 0` for serial-mode debugging or `-n N` for a specific worker count. Runtime drops from ~8-10 min to ~3-4 min on a 4+ core machine.
- **Runtime:** ~3-4 min on a 4+ core machine (xdist parallel); ~8-10 min serial (`-n 0`).
- **When to run:** required whenever Phase 7 fires (post-merge cross-cutting
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

`pre-commit` catches tool-version skew (e.g. the ruff mismatch the cxj batch
only hit on first push to main); the Docker e2e suite catches
integration-emergent install / sync / revert / plugin / extension regressions
that unit tests cannot exercise.

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

Filing a new bead covers follow-up work but does NOT replace
the revert-or-fix step. A red main is not OK. Either path (revert-and-re-PR
OR inline-fix) must re-run Phase 7 to confirm main is green.

**Routine post-merge review-fan findings (no broken gates).** The strict
1-file/1-2-line rule above is for ACTUAL test failures or pre-commit fails
on main. When all Phase 7 gates stay green and the cross-cutting review
fan surfaces quality nits (prose tweaks, missed annotations, docstring
drift, small refactors), follow the **Decision-I default**: fix INLINE on
main as SEPARATE review-fix commits (one logical change per commit per the
Commits rule); reserve `bd create` ONLY for LARGE follow-ups:

- (a) introduces a new design question requiring its own brainstorm + spec, OR
- (b) cross-cutting across 3+ files outside the bead's scope, OR
- (c) the implementer/reviewer is uncertain whether it's safe to fix inline.

After ANY inline-fix on main (failure-handling OR Decision-I), re-run the
Phase 7 gates per `feedback_phase7_rerun_after_inline_fix` memory.

## wt post-merge hook

After `wt merge`, the project's wt config (`.config/wt.toml`) runs
`uv sync --extra dev` automatically. Per `wt hook --help`, the
post-merge hook runs in the TARGET branch worktree (typically main) —
not the merging worktree, which `wt merge` removes by default. This
keeps main's venv in sync when a merged branch adds a new dep (e.g.
the setforge-rsw / nen.9 + pexpect class). Unrelated sibling worktrees
still need manual `uv sync` recovery — see setforge-b6d for scope.

## The four-tool stack

Beads + Superpowers configured by this repo. Repomix + worktrunk installed externally; `setforge install` does NOT bootstrap them.

## Adding tracked files and extensions

Both happen in the USER's config repo (the one configured via the source layer), not in this engine repo:

- Tracked file: edit `<config-repo>/my_setup.yaml` to add an entry under `tracked_files:`, reference it from the relevant profile, place the source file under `<config-repo>/tracked/<src>`.
- Extension: add the extension ID to the profile's `extensions.include:` list in `<config-repo>/my_setup.yaml`.

After editing the config repo, commit + push there. On the next `setforge install --profile=X` the engine reads the updated config via the source layer.

## Host-local, never-tracked

`~/.claude/additional-content.md` is intentionally untracked per host. `setforge install` creates a stub if missing. Never commit its content.

`~/.vscode-server/data/Machine/settings.json` may carry host-local keys (e.g. `claudeCode.allowDangerouslySkipPermissions`) that are intentionally not in tracked. The profile's `preserve_user_keys` overlays those keys from live to tracked on `install`, and `capture` strips them from tracked, so they stay host-local without manual intervention. Comments in the JSONC settings file are preserved end-to-end.

## Don't-do list

- Don't push to git remotes automatically — I push when ready.
- Don't auto-edit `my_setup.yaml`'s extension lists — those land via the dedicated `ext` subcommand once Pillar 2 ships.
