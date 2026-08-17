# setforge

Tracked-file + VSCode-extension + Claude-plugin orchestration CLI. The engine repo (this one) ships the `setforge` tool; the user's personal config (a `setforge.yaml` + `tracked/` tree) lives in a SEPARATE config repo per the engine/config split. The config repo is discovered via the source-layer (CLI `--source` > `SETFORGE_SOURCE` env > `~/.config/setforge/local.yaml` `source:` block > CWD fallback). The author's personal config now lives at `raulfrk/setforge-config` (public).

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

Profiles are defined in the USER's config repo's `setforge.yaml`. The author's daily-driver profile is `debian-vm`. Never run a `setforge` command without `--profile=`.

## Workflow verbs

- `uv run setforge compare --profile=<name>` — read-only drift check (live vs tracked).
- `uv run setforge sync --profile=<name>` — capture live edits into tracked/. Always `git diff` after to review. Drift on any tracked_file (file-content or mode bits) triggers the merge wizard interactively; for non-interactive use pass `--auto=use-live` (silent-absorb, today's behavior) or `--auto=keep-tracked` (refuse to absorb).
- `uv run setforge install --profile=<name>` — deploy tracked → live. Drift inside `shared` user-section markers triggers the reconcile wizard interactively when `--reconcile-user-sections` is passed; for non-interactive use pass `--auto=use-tracked` (deploy tracked-side updates over the live body) or `--auto=keep-live` (silence the warning, keep live). `--reconcile-user-sections` and `--auto=` are mutually exclusive (exit 2). Bare `install` warns once per shared-drifted file and keeps live. `host-local` sections are always preserved-live regardless of flags.
- `uv run setforge revert --profile=<name>` — undo the most recent install/sync (file diffs via `patch -R` + extension reverse). Drift refuses cleanly; second invocation acts as redo. Transitions live at `~/.local/state/setforge/transitions/` (kept indefinitely; pruning is a future bead).
- `uv run setforge validate --profile=<name>` — config-shape check (schema + profile chain + Jinja2 + tracked srcs + claude_plugins references). No filesystem comparison; works offline. CI runs `validate --all`.

## Docker e2e tests

A 300+ test end-to-end suite at `tests/docker/` exercises `install`/`sync`/`compare`/`revert`/`validate`/`init`/`migrate`/`upgrade` and the new secrets-scan + reconcile-failure UX surfaces against a fresh Debian 12 container with real `claude`/`code`/`gitleaks` binaries — the canonical behavior-preservation gate for this project.

- **Invocation:** `uv run pytest tests/docker/ -m e2e_docker -v` (bare — xdist auto-activates with `-n 2`).
- **Image targets:** `-m "e2e_docker and smoke"` selects the lean `smoke` target (real Debian, VS Code, gitleaks, shells, Python, and SetForge; no Claude/Go/Rust). Every other positive `e2e_docker` selection uses the default `full` target with all pinned tools. Target names are part of the content-hashed image tag, so the two capability sets cannot alias.
- **Parallel execution:** auto-activated by the project-root `conftest.py:pytest_configure` hook when `-m e2e_docker` is selected and `-n` was not passed explicitly. Capped at **2 workers** — empirically validated as the stable maximum for this host's Docker daemon (~300 tests in ~42 min wall, zero `TimeoutExpired` flakes). Earlier attempts at `-n 4` saturated the daemon AND the host VM under sustained load; `-n auto` (= 6 on a 6-core host) produces ~20 transient timeouts per run. Override the cap with `-n N` on the CLI when running on a host with different daemon throughput; `-n 0` opts out of xdist for serial-mode debugging.
- **Image preparation:** the xdist controller builds a missing content-hashed image before workers start; worker fixtures inspect and reuse it. Serial `-n 0` runs retain fixture-side build fallback.
- **Build guardrails:** reference-host diagnostics are a no-cache smoke build at or below 420s and at least 25% below the recorded 559s full baseline, unchanged preparation below 15s, source-only rebuild below 90s, and full no-cache below 620s. These are reprofile thresholds, not timing assertions in CI: apt/npm/marketplace latency is external and would make a wall-clock test flaky. Build failures remain bounded by the helper timeout.
- **Measured split (2026-08-17):** final no-cache smoke build 410s (26.7% faster than the 559s baseline); source-only rebuild 14s; unchanged repeat 6s. The smoke image was 1.37 GB versus 2.58 GB for full. A no-cache full build during the same run was 551s and retained every pinned capability.
- **Runtime with `-n 2` (default):** ~42 min on a 6-core host (~300 tests).
- **Runtime serial (`-n 0`):** ~90+ min (proportional estimate; not re-measured).
- **When to run:** required whenever Phase 7 fires (post-merge cross-cutting review). See `## Final checks (post-merge)` below.
- **Prerequisite:** `docker` on PATH; the suite skips containers missing docker. `gitleaks` is baked into the e2e image (Dockerfile pins v8.21.2), so the secrets-scan cases need NO host `gitleaks` — they run gitleaks inside the container, and the absent-gitleaks warn-and-continue path is itself covered by `test_e2e_docker_install_no_gitleaks_warns_and_continues`. (Host `gitleaks` matters only for the repo's pre-commit hook, not this suite.)
- **Full-screen TUI tests:** prompt_toolkit's `radiolist_dialog` / `input_dialog` panels emit cursor-positioning ANSI that pexpect's line matcher cannot anchor on. Use the `pyte_pty_session` fixture in `tests/docker/conftest.py` for those — it feeds the PTY byte stream into a `pyte.HistoryScreen` and exposes `.display: list[str]` for line-by-line asserts. See `tests/docker/pyte_session.py` for the API (anti-smell items: `docker exec -it` is required, arrow keys are `\x1b[A/B/C/D`, Enter is `\r`). The plain-stdout `docker_pty_session` (raw pexpect) still suits non-TUI interactives.

The suite is gated by `pytest -m e2e_docker` and excluded from the default run. Process-level tests of these pytest/xdist hooks use `test_infra` and are also excluded from the fast default. `pyproject.toml` sets `addopts = "-m 'not e2e_docker and not test_infra' ..."`; CI runs the infrastructure lane separately with explicit branch coverage.

## Final checks (post-merge)

After merging a non-trivial branch into `main`, both of these must exit 0:

```sh
pre-commit run --all-files
uv run pytest tests/docker/ -m e2e_docker -v --no-cov
```

`pre-commit` catches tool-version skew (e.g. the ruff mismatch the cxj batch
only hit on first push to main); the Docker e2e suite catches
integration-emergent install / sync / revert / plugin / extension regressions
that unit tests cannot exercise.

When the merge touches the config schema or migrations, also confirm the
change honors the standing guarantees in [`COMPATIBILITY.md`](COMPATIBILITY.md)
(additive-first, expand → contract, up + down migration per `schema_version`
bump).

When an engine diff touches any of the mutmut core files (the
`[tool.mutmut].only_mutate` list — the merge/reconcile/store modules), run
`uv run python scripts/mutmut_diff_gate.py` before merge (Phase 6) as a local
Tier-1 check. It runs mutation testing scoped to only the changed core lines
(diff-scoped, so it stays fast) and blocks on any surviving mutant whose
function overlaps the change. It is deliberately NOT in pre-commit or the
always-on review fan (a mutmut run is too slow for either), so this is a
documented manual trigger; CI enforces it non-locally via the `pr-mutmut-diff`
PR job (diff-scoped) and the `mutmut-full` nightly job (whole core).

**Why `--no-cov` on the Docker e2e invocation:** pytest-cov's controller is
selected at master `pytest_configure` time (before xdist worker setup). With
`--cov` in default addopts and the conftest auto-xdist
hook setting `numprocesses=2`, pytest-cov picks the `Central` controller and
then crashes at `pytest_configure_node` when xdist tries to set up workers
with `AttributeError: 'Central' object has no attribute 'configure_node'`.
`[tool.coverage.run].parallel=true` doesn't fix it because controller
selection happens before our conftest's `tryfirst` hook can intervene.
Coverage is meaningful for unit tests, not for behavior/e2e tests anyway —
the explicit `--no-cov` opts out cleanly. See memory
`feedback_pytest_cov_xdist_parallel` for the full diagnostic.

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

## Review fan — project reviewers (enforce-tests)

This project ships its own Phase-5/7 review entry: the **`enforce-tests`**
skill (`.claude/skills/enforce-tests/`). When session-flow Phase 5 (post-impl
review) or Phase 7 (post-merge) fires on an **engine diff**, invoke
`enforce-tests` instead of running the global python fan bare — it is the
single orchestrator for setforge review.

What it dispatches, and the contract it owns:

- The two **project reviewers** — `test-quality-reviewer` and
  `design-invariant-reviewer` (`.claude/agents/`) — run **alongside** the global
  `reviewing-python-code` + `reviewing-bd-leaks` fans, which `enforce-tests`
  invokes **by skill name** (never a hard-coded aspect-agent roster, so a new
  upstream python aspect is picked up automatically).
- **`enforce-tests` owns the `reviewing-bd-leaks` scan** — it runs the leak fan
  exactly once. Session-flow's "always run the leak scan" rule is satisfied here;
  do not also dispatch `reviewing-bd-leaks` separately for an engine diff.
- It runs the **Tier-1 deterministic gates** first (`scripts/check_policy_lints.py`,
  `scripts/check_schema_gates.py`, `scripts/check-no-bd-refs.sh`), short-circuiting
  the fan on failure, and consolidates **one two-axis verdict** (BLOCKING:
  Tier-1 + bd-leak / ADVISORY: the LLM fan).
- All reviewers receive the **same once-computed range**, base pinned to
  `git merge-base HEAD main` — see the skill for the full input contract.

**Cross-host wiring.** The generic hook that makes session-flow Phase 5/7 honor
this manifest is one project-agnostic line in the global `session-flow` skill
(maintained in the config repo). This section is the engine half of that bridge;
the two reference each other.

## wt post-merge hook

After `wt merge`, the project's wt config (`.config/wt.toml`) runs
`uv sync --extra dev` automatically. Per `wt hook --help`, the
post-merge hook runs in the TARGET branch worktree (typically main) —
not the merging worktree, which `wt merge` removes by default. This
keeps main's venv in sync when a merged branch adds a new dep (e.g.
the pexpect class). Unrelated sibling worktrees
still need manual `uv sync` recovery.

## The four-tool stack

Beads + Superpowers configured by this repo. Repomix + worktrunk installed externally; `setforge install` does NOT bootstrap them.

## Adding tracked files and extensions

Both happen in the USER's config repo (the one configured via the source layer), not in this engine repo:

- Tracked file: edit `<config-repo>/setforge.yaml` to add an entry under `tracked_files:`, reference it from the relevant profile, place the source file under `<config-repo>/tracked/<src>`.
- Extension: add the extension ID to the profile's `extensions.include:` list in `<config-repo>/setforge.yaml`.

After editing the config repo, commit + push there. On the next `setforge install --profile=X` the engine reads the updated config via the source layer.

## Host-local, never-tracked

`~/.claude/additional-content.md` is intentionally untracked per host. `setforge install` creates a stub if missing. Never commit its content.

`~/.vscode-server/data/Machine/settings.json` may carry host-local keys (e.g. `claudeCode.allowDangerouslySkipPermissions`) that are intentionally not in tracked. Those keys stay host-local without manual intervention: an unclassified key defaults to host-only (`PENDING`) and is never promoted into tracked; only a key explicitly classified `SHARED` is promoted into tracked on `sync`, and `install` merges tracked → live at key granularity, honoring existing classifications rather than creating them. Comments in the JSONC settings file are preserved end-to-end (json5 AST round-trip).

## Don't-do list

- Don't push to git remotes automatically — I push when ready.
- Don't auto-edit `setforge.yaml`'s extension lists — those land via the dedicated `ext` subcommand once Pillar 2 ships.
