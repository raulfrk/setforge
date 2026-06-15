# Changelog

All notable changes to setforge are tracked here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-06-15

The host-reproducibility and schema-versioning release. v0.3.0 makes a
setup reproducible on a fresh host (MCP servers, cargo binaries,
shareable section templates), adds a per-host override layer backed by
stored-base 3-way merge, and introduces a versioned config schema with
bidirectional migrations.

### Added
- **Config schema versioning + bidirectional compatibility.** Every
  `setforge.yaml` carries a `schema_version`; the engine ships up + down
  migrations per bump, an expand→contract policy for breaking changes
  (see [`COMPATIBILITY.md`](COMPATIBILITY.md)), and a CI schema-compat
  matrix. `setforge migrate --check`/`--apply` reports and runs the
  needed chain; `setforge upgrade` surfaces a version bump's schema
  impact.
- **Per-host override layer.** A `local.yaml` overlay carries host-local
  `mode` / `dst` / `symlink_target` field overrides and markerless
  host-local sections; a disposition model (`shared` / `forked` /
  `pinned`) governs how `sync`/`install` reconcile each file, with
  in-file pinned regions excluded from capture. An `override` CLI
  (`list` / `fork` / `pin` / `show`) drives it.
- **Stored-base 3-way merge.** A per-host stored-base byte store anchors
  a real 3-way merge: a markdown engine (merge3 + histogram), a
  structural engine for JSON / YAML / JSONC, and a hunk-level conflict
  wizard. Whole-subtree structural pins preserve the live node's
  comments.
- **MCP servers + cargo binaries in `setforge.yaml`.** A new
  `mcp_servers:` registry (plus a per-profile list) registers servers
  via `claude mcp add` on install — converge-declared (hand-registered
  servers are left untouched), revertible via a recorded delta. A
  `cargo_binaries:` list installs crates during install (skip-if-present;
  a missing cargo toolchain warns and continues).
- **Shareable host-local section templates.** A `section_templates:`
  registry plus per-profile `section_slots:` seed-once a template body
  into an empty/missing host-local section; a populated section is never
  overwritten, so library edits do not clobber a host that has adopted
  the section.
- **`init` scaffolds the config repo** (`setforge.yaml` + `tracked/`),
  not just `local.yaml`; `install` transparently migrates a legacy
  stealth layout and warns.

### Changed
- **schema_version bumped 1.0 → 2.0.** The unified-span contract
  retires the legacy host-local-section markers in favor of `spans`
  OVERLAY entries; `install` migrates live configs forward (and `revert`
  restores them), with an optional `minimum_version` floor for
  operator-attested contraction.
- **`install` is now two-pass** — `--strict-spans` refuses before any
  file write, so a span-resolution failure can no longer leave a
  partial, unrevertable install.
- **`validate` flags orphan overlay entries** — a `local.yaml` overlay
  id unknown to `setforge.yaml` fails with a did-you-mean, an
  off-profile id is noted — and `compare` lists skipped orphan overlays
  (human output and `--json`).
- **Consolidated every atomic-write site** onto `setforge/atomicio.py`.
- **`compare` now classifies every drifted file** with a per-file drift
  class — `expected`, `stale` (live still equals the stored base while
  tracked advanced; the next install fast-forwards it), `unexpected`, or
  `conflicted` (a forked-scalar conflict: the stored base differs from
  both live and tracked at the same scalar path, so the next interactive
  install would prompt) — fixing the report that listed a genuinely
  drifted file with zero drift counts. The summary table's dead
  `expected drift` / `unexpected drift` count columns are replaced by
  `File | Disposition | Class | Why`; conflicted rows render each
  conflict as `path: base → tracked | live` (tracked = upstream, live =
  yours) in the Why column. `compare --check` now passes on stale-only
  drift but fails on conflicted drift (`--check --strict` still fails on
  any drift). The `--json` entry schema gains `drift_class`, `reason`,
  `span_only_drift`, and `forked_scalar_conflicts` (the same pre-rendered
  conflict lines) and drops the always-empty `expected_drift_keys` /
  `unexpected_drift_keys` arrays. Engine output schema only — the
  config schema is untouched.
- **Capture no longer bakes host-local span values into the repo** — a
  structural span path with no value in tracked is now dropped from the
  `sync`/`capture` writeback (previously the live value flowed through
  into the shared config repo), with a per-path warning on stderr:
  `span path P absent in tracked — host value not captured`.
- **Compare classifies live-added span keys as expected** — when live
  adds a key inside a span that tracked lacks, the drift now counts as
  span-only (expected host divergence) instead of unexpected shared
  drift. Intentional flip following the capture-side drop above: with
  the path excluded from capture, the divergence is exactly the kind a
  span pin declares host-local. (Exception: on a SHARED file with no
  stored base yet, compare classifies the same drift `unexpected` with
  a clobber warning — the first install would overwrite it.)

### Fixed
- **`revert` now removes overlay-declared symlinks.** A symlink declared
  only in `local.yaml` (via `symlink_target:`) was skipped by the revert
  unlink pass, leaving a dangling link; revert now folds the host-local
  overlay before the unlink pass, matching install/compare/sync.
- **Full revert state restoration.** Revert restores seeded byte-bases,
  scalar-base manifests, and span sidecars from per-transition state
  snapshots, not just file content — so a revert leaves no orphaned
  per-host base state behind.
- **Install-time upstream rename/delete classifier** for span paths,
  with a did-you-mean suggestion when a tracked anchor disappears
  upstream.

## [0.2.2] - 2026-06-02

Patch release: Docker e2e test-suite hardening. No user-facing behavior
changes — the engine surfaces are byte-for-byte identical to 0.2.1. The
release banks the verification work before the v0.3.0 feature cycle.

### Changed
- **Tightened the Docker e2e assertion surface** — audited the full
  end-to-end suite and rewrote 24 weak-but-passing assertions across 10
  test files so each pins the specific gate, content, or ordering it
  intends, rather than a bare return code or a substring that could
  match anywhere. Several were reframed to assert an impossible state's
  *absence*, positive dry-run output, or post-revert target removal.
- **Rewrote the two `config_cli` git-check e2e tests** to genuinely trip
  the git-clean gate — seed a tracked git repo, dirty a committed file,
  and drive the pre-deploy abort dialog. The e2e image excludes `.git`,
  so the prior setup silently exercised source-validation instead of the
  git-dirty path; the test names now match the behavior.

## [0.2.1] - 2026-06-01

Documentation and CI-maintenance release. Folded into the v0.2.2 tag —
never released to PyPI separately.

### Changed
- **Restructured the README as a landing page**, splitting the detailed
  command reference into `docs/`. Added an install version note and
  tightened the task-tracker-invisibility guidance.

### Fixed
- **Docker e2e CI reliability** — write container files via `tee` so the
  in-container tester owns them, make `docker cp` staging files
  world-readable, and pass `--no-cov` to the CI e2e pytest invocation
  (avoids the pytest-cov + xdist controller crash).

## [0.2.0] - 2026-05-31

The rename release. setforge is the renamed, re-architected successor
to the prior `my-setup` tool. v0.2.0 is the first release under the new
name. Major restructuring across the engine, with no breaking changes
to the YAML config surface beyond the file rename.

### Changed
- **Renamed the engine** from `my-setup` to `setforge`. The Python
  package is now `setforge`, the CLI entry point is `setforge`, env
  vars use the `SETFORGE_` prefix, and the XDG state directory moved
  from `~/.local/state/my-setup/` to `~/.local/state/setforge/`. See
  the README's "Upgrading from my-setup v0.x" section for the
  migration recipe.
- **Renamed the config file** from `my_setup.yaml` to `setforge.yaml`.
  Engine surfaces a migration error pointing at the new filename when
  the old name is detected.
- **Renamed identifiers**: `Dotfile` class → `TrackedFile`, the
  `dotfiles:` YAML key → `tracked_files:`, `Profile.dotfiles` →
  `Profile.tracked_files`. The "dotfile" term is no longer used in
  any user-facing surface.
- **Split the engine from user config**. The engine repo no longer
  carries `setforge.yaml` or `tracked/`; both live in a separate
  user-owned config repo discovered via the source layer (CLI
  `--source` > `SETFORGE_SOURCE` env > `~/.config/setforge/local.yaml`
  > CWD fallback).
- **Split `cli.py` into a `setforge.cli` subpackage** (2,119 lines
  → 11 per-area files). Public API unchanged; `setforge --help`
  output is bit-for-bit identical to the pre-split snapshot.

### Added
- **`setforge fetch` subcommand** — clones / fetches the configured
  git source and checks out its pinned ref. Path-based sources are a
  no-op.
- **Source-layer discovery** — 4-tier `--source` > env >
  `~/.config/setforge/local.yaml` > CWD fallback. Schema enforces a
  single source per user; tagged-union `kind:` discriminator selects
  between `PathSource` and `GitSource`.
- **Git management subsystem** — `setforge fetch` orchestrates clone /
  fetch / dirty-gate / ref-checkout via `setforge.git_ops`. Dirty
  checkouts of `tracked/` abort with an actionable error; post-sync
  emits a hint pointing the user at the source dir for `git diff +
  commit + push`.
- **Legacy-marker namespace detection** — `compare` / `sync` / `merge`
  refuse to operate on files still carrying the pre-rename
  `my-setup:user-section` marker namespace, with a `sed` command
  prepared inline in the error message.
- **Frozen-tuple CLI registration-order regression test**
  (`tests/test_cli_registration_order.py`) — pins `setforge --help`
  listing order against accidental reorder during a future split or
  rename pass.
- **Per-command-area module docstrings** — each new `setforge/cli/*.py`
  file gets a module-level docstring describing its scope and any
  cross-file dependencies (e.g. the bottom-of-`__init__.py`
  side-effect import block).
- **`setforge --version` flag** — eager Typer callback that prints
  `setforge.__version__` (sourced from `importlib.metadata`) and exits
  before the root callback runs.
- **PyPI publish workflow** at `.github/workflows/publish-pypi.yml` —
  fires on `v*.*.*` tag push, runs `uv build` + `twine check`, uploads
  via `pypa/gh-action-pypi-publish` using the `PYPI_API_TOKEN` secret
  scoped through a `pypi` GitHub environment. `skip-existing: true`
  makes re-pushes of the same tag idempotent.
- **GitHub Release workflow** at `.github/workflows/release.yml` —
  fires on the same tags, creates a GitHub Release with auto-generated
  notes scoped to the commit range between the previous release tag
  and the current one (or full history on the first release).
- **`CHANGELOG.md`** in Keep-a-Changelog 1.1.0 format (this file).
- **`LICENSE` (MIT)** — first formal license file in the repo.
- **PyPI-ready `pyproject.toml` metadata** — `readme`, `license`,
  `authors`, `keywords`, full `classifiers` (intended audience, OS,
  license, topics), and a `[project.urls]` block (homepage, source,
  issues, changelog).

### Fixed
- **`hash=` in semantics position is now a `MarkerError`** instead of
  a silent non-marker fallthrough. The pre-fix
  `_raise_if_malformed_marker` early-returned on `hash=`-prefixed
  first tokens to preserve end-marker hash handling, but the strict
  grammar puts `hash=` in position 3 (after NAME), not position 1; a
  position-1 `hash=` is always malformed. The new error message
  flags the missing-semantics-keyword shape directly.
- **Type tightening across `setforge/`** — removed the project-wide
  ANN001 + ANN401 ruff ignores. The remaining ~27 `typing.Any`
  call sites at the ruamel.yaml + json-five untyped seam (concentrated
  in `setforge/yaml_merge.py` + `setforge/jsonc.py` + 4 sites in
  `setforge/capture_wizard.py`) are now per-file or per-site allowed
  with explanatory comments instead of a project-wide suppression.

### Removed
- **`my-setup` CLI binary** — replaced by `setforge`. `pyproject.toml`
  no longer exposes a `my-setup` entry point.
- **`my_setup.yaml` config-file recognition** — replaced by
  `setforge.yaml`. Loaders surface a migration error pointing at the
  new filename instead of silently accepting the old name.

## [0.1.0]

Earlier development series under the `my-setup` name (no formal release
tag). See the migration section of the README for the upgrade recipe.

<!-- 0.2.1 is documented for history but was never tagged (it folded
into the v0.2.2 tag), so it carries no compare ref. The 0.2.2 refs
resolve once the v0.2.2 tag lands on origin/main. -->
[Unreleased]: https://github.com/raulfrk/setforge/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/raulfrk/setforge/compare/v0.2.0...v0.2.2
[0.2.0]: https://github.com/raulfrk/setforge/releases/tag/v0.2.0
