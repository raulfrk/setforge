# Command reference

The authoritative list is always `setforge --help` (and `setforge <command>
--help`). This page covers the commands you reach for day to day, the
subcommand groups, and the confirmation behavior of mutating runs.

All deploy/compare/sync commands require `--profile=<name>`; profiles live in
your config repo's `setforge.yaml`.

## Global options

Apply to every command (`setforge [OPTIONS] COMMAND`):

- `--source PATH` — config source directory (overrides `SETFORGE_SOURCE` and
  `local.yaml`).
- `--code-bin` / `--claude-bin` / `--gitleaks-bin` / `--patch-bin` — override
  external binary paths.
- `-v` / `--verbose` (`-v` → INFO, `-vv` → DEBUG with secret redaction).
- `-q` / `--quiet` suppresses success output on the structured read-only
  commands listed below; errors remain on stderr.
- `-o` / `--format [human|json]` selects human output or a versioned JSON
  envelope on those same commands.
- `--version` — print the installed version and exit.

Structured output is supported by `compare`, `status`, `inspect`, `profile
show`, `transitions list`, `ownership list`, `ownership history`, `stage
--list`, and `config show --effective`. Every other command rejects `--quiet`
and `--format=json` before doing work. `--quiet` and `--format=json` are
mutually exclusive.

## Daily workflow

```bash
setforge fetch                          # clone/fetch + checkout the git source
setforge compare  --profile=<profile>   # show drift between live and tracked/
setforge sync     --profile=<profile>   # capture live -> tracked + record a transition
setforge install  --profile=<profile>   # deploy tracked/ -> live
setforge revert   --profile=<profile>   # undo the most recent install/sync
setforge status   --profile=<profile>   # one-screen status summary (read-only)
setforge validate --profile=<profile>   # config-shape check (no live target paths)
```

`validate` requires exactly one of `--profile=<name>` or `--all` (both, or
neither, exits 2). `install` and `status` require `--profile`.

`sync` is `capture`'s transition-recording sibling: "I tweaked something live,
now save it and record a transition I can revert later." Both write captured
content into your config repo's `tracked/`; `git diff` + commit + push from
inside the config repo to lock it in. `capture` is the lower-level piece
`sync` composes (the capture pipeline without the transition record).

## Top-level command inventory

This table is intentionally complete and is checked against `setforge --help`.

<!-- setforge-doc-command-inventory:start -->
| Command | Purpose |
|---|---|
| `install` | Deploy tracked files and reconcile provisioned state. |
| `compare` | Report tracked/live drift. |
| `cleanup-orphans` | Review transition-attributed or explicitly scanned file orphans. |
| `cleanup` | Review undeclared provisioned binaries recorded by receipts. |
| `capture` | Capture live tracked-file content. |
| `sync` | Capture files and reconcile extension declarations. |
| `revert` | Undo or redo recorded transitions. |
| `recover` | Inspect or recover an interrupted write-ahead operation. |
| `validate` | Validate config shape without comparing live paths. |
| `fetch` | Update a configured git source. |
| `lock` | Resolve exact package pins into `setforge.lock`. |
| `init` | Bootstrap host-local configuration or a config repo. |
| `upgrade` | Upgrade the installed SetForge engine. |
| `migrate` | Preview or apply schema migrations. |
| `status` | Summarize profile state. |
| `stage` | Classify and stage selected plain-file changes. |
| `inspect` | Inspect reconcile base/live/merge state. |
| `transitions` | Inspect transition history. |
| `ownership` | Inspect claims and explicitly release, reverse, or recover authority. |
| `ext` | Manage VSCode extension package declarations. |
| `plugin` | Manage Claude or Codex plugin declarations (`--product`). |
| `marketplace` | Manage Claude or Codex marketplaces (`--product`). |
| `profile` | Inspect raw and effective profiles. |
| `snapshot` | Create, list, and restore directory snapshots. |
| `completion` | Install shell completions. |
| `config` | Read or edit tracked and host-local configuration. |
| `project` | Inject, synchronize, and remove reusable files in a project worktree. |
<!-- setforge-doc-command-inventory:end -->

## Subcommand groups

setforge ships ten subcommand groups for narrow inspections and edits. Run
`setforge <group> --help` for each:

| Group | Subcommands | Purpose |
|---|---|---|
| `plugin` | `list`, `add`, `remove`, `reconcile`, `sync-cache` | Claude plugin packages by default; pass `--product codex` for Codex. Cache sync remains Claude-specific. |
| `marketplace` | `add`, `remove`, `update` | Claude marketplaces by default; pass `--product codex` for Codex sources. |
| `ext` | `list`, `add`, `remove`, `reconcile` | VSCode extension packages selected by a profile. |
| `transitions` | `list`, `show` | Inspect install/sync/stage/revert history. |
| `ownership` | `list`, `release`, `history`, `revert`, `recover` | Inspect durable claims and explicitly change their authority without changing resource bytes. |
| `profile` | `list`, `show` | Inspect profile definitions and resolved overlays. |
| `config` | `show`, `add`, `remove` | Granular CRUD over `setforge.yaml` / `local.yaml`. |
| `snapshot` | `create`, `list`, `restore` | Directory-copy snapshots. |
| `completion` | `install` | Install shell completion scripts. |
| `project` | `inject`, `list`, `visibility`, `sync`, `remove` | Inspect, materialize, change per-file Git visibility, synchronize, and remove project profiles in Git worktrees or plain directories. |

### Project profile synchronization

`setforge project list` inventories every recorded project injection, grouped
by target and profile, and reports each destination's actual state as `hidden`,
`tracked`, `tracked-overlay`, or `not-applicable`. Stale, corrupt, drifted, or
otherwise inconsistent records stay visible as errors and make the command
exit nonzero.

`setforge project visibility <path> <file>` changes one normalized,
target-relative destination. `--tracked` exposes an injected hunk as an
ordinary Git diff (or makes an injected file ordinary untracked/tracked
content); `--hidden` restores SetForge's private filter or exclude claim.
SetForge never stages content. Plain-directory targets report the operation as
not applicable and remain unchanged.

```console
$ setforge project list
$ setforge project visibility /path/to/worktree AGENTS.md --tracked --dry-run
$ setforge project visibility /path/to/worktree AGENTS.md --hidden --yes
```

`setforge project sync <path>` discovers every recorded profile injection for
that exact Git worktree and plans them as one transaction. `--dry-run` prints
updates, membership additions/removals, legacy records, and conflict counts
without changing files or private state. A live run preserves independent local
edits with three-way reconciliation and opens the existing per-region wizard for
overlapping edits when stdin is a TTY.

For non-interactive use, pass both `--yes` and either `--auto=keep-live` or
`--auto=use-profile` when conflicts are possible. Without an explicit automatic
policy, unresolved non-TTY conflicts fail without mutation. A cancellation or
deferred region also leaves the whole target batch unchanged.
Membership additions that collide with differing local files follow the same
conflict policy. For conflicts involving an absent file, the wizard records
whether Ours or Theirs was selected so deletion remains distinct from choosing
an intentionally empty file.

```console
$ setforge project sync /path/to/worktree --dry-run
$ setforge project sync /path/to/worktree --auto=keep-live --yes
```

### Ownership authority

`ownership list` is global and read-only: it does not load a config file or
create a checkout identity. Claim IDs are full 64-character lowercase hashes.
Release and owner history resolve `--config` normally, then read the existing
Git checkout owner ID; they never create one.

```console
$ setforge ownership list
$ setforge ownership release <claim-id> --config=setforge.yaml --yes
$ setforge ownership history --config=setforge.yaml
$ setforge ownership history <transition-id> --config=setforge.yaml
$ setforge ownership revert <transition-id> --config=setforge.yaml --yes
$ setforge ownership recover --config=setforge.yaml
$ setforge ownership recover --config=setforge.yaml --apply --yes
```

Release removes SetForge's management authority while retaining the live
resource, its tombstone, provenance, and immutable owner-scoped history. A
normal clone has a different owner namespace; linked Git worktrees share one.
Revert succeeds only while the recorded post-state is still current and, when
it would restore authority, the current declaration, resource identity, and
live fingerprint still match. Interrupted publication remains visible through
`ownership recover`; `--apply` completes only unambiguous pending work.

When `install` or `stage` finds an active claim owned by a different config
checkout, it offers an explicit inline transfer. Accepting with the prompt (or
`--yes` for `install`) revalidates the exact owner, generation, declaration,
resource identity, and live fingerprint under mutation locks, then changes only
the claim—not the file bytes or installed package. The transfer appears in the
normal profile transition history and `setforge revert --profile=PROFILE`
reverses it only when invoked by the current recipient while that exact
post-transfer state is still current. In `stage`, transfer and “adopt locally”
are separate invocations: transfer the claim first, then rerun stage to change
live content. Released, drifted, stale, corrupt, or non-Git cases fail closed.

<a id="codex-lifecycle"></a>
## Codex lifecycle

The ordinary profile lifecycle needs no product-specific flag:

```console
setforge validate --profile=workstation
setforge lock --profile=workstation
setforge install --profile=workstation
setforge compare --check --profile=workstation
setforge status --profile=workstation
setforge sync --profile=workstation
setforge revert --profile=workstation --yes
```

These commands reconcile Claude and Codex selections as one profile operation,
with product-qualified diagnostics and reversible transition records. Use
`--product codex` only for direct `plugin` and `marketplace` declaration
commands. `compare` and `status` stay read-only; a missing or incompatible
Codex plugin CLI is reported as drift rather than silently ignored.
`cleanup` remains the package-receipt cleanup flow; Codex plugin pruning is
selected by `profiles.<name>.codex.reconcile.policy: prune` and applied by
install or direct plugin reconciliation.

`cleanup` and `cleanup-orphans` are deliberately different. `cleanup` compares
package provisioner receipts with the effective package/bundle declaration and
reviews undeclared binaries. `cleanup-orphans` concerns filesystem paths: its
default mode uses tracked-file transition attribution, while `--scan` opts into
bounded discovery of unrecorded leaves.

Mutating commands share one lock order: a user-global mutation gate, then
user-global package/adapter resources, the canonical config repository, and
finally profile state. The gate covers the interval before a write-ahead journal
can be published, including migrations that later lock multiple real profiles.
An interrupted
install/sync/revert/migration leaves a durable per-profile journal in the
user-global recovery registry. Conflicting mutations refuse across profiles
and across `SETFORGE_STATE_DIR` overrides until automatic recovery succeeds or
the operator runs `setforge recover --profile=<name> --apply --yes` from the
recorded transition-state root. A begun package checkpoint is intentionally
reported as uncertain/manual even if it did not reach its completion marker.

## Package locks and Cargo

`setforge lock --profile=<profile>` resolves all lockable entries selected by
the effective profile and writes the shared `setforge.lock`; `--update=<key>`
re-resolves one key while retaining the rest. `setforge install --locked`
requires matching pins for every lockable item and does not re-resolve them.

For Cargo, a pin is an exact semantic version plus a `sha256:` checksum from
the exact crate/version row in the crates.io sparse index. Install compares
that row with the committed lock before either skipping an exact installed
crate or invoking `cargo install` to mutate. A malformed pin, checksum
mismatch, unavailable row, or unavailable index is **HARD** and invokes no
mutating install for that item; the read-only `cargo install --list` inventory
probe may already have run during planning.

The invoked command uses `cargo install --version <exact> --locked`; Cargo's
`--locked` selects the archive's packaged Cargo lockfile. Cargo handles its
registry archive download and checksum verification; SetForge independently
validates the sparse-index checksum but does not hash the downloaded archive.
Neither SetForge `--locked` nor `--no-fetch` is a Cargo offline mode: the former
still needs the sparse-index comparison and the latter only disables the
config-source git fetch.

<!-- setforge-doc-flags: lock -->
| `lock` flags documented here | Meaning |
|---|---|
| `--profile` | Effective profile to resolve. |
| `--update` | Re-resolve one lock key. |
| `--config` | Select a manifest path. |

<!-- setforge-doc-flags: install -->
| `install` lock-related flags documented here | Meaning |
|---|---|
| `--locked` | Require complete matching lock coverage. |
| `--no-fetch` | Skip only the config-source git fetch. |

## Filesystem orphan cleanup

Legacy mode (`cleanup-orphans` without `--scan`) reviews paths attributed to
removed tracked-file entries by transition history. It defaults to dry-run;
`--apply` opens the delete/delete-and-transition wizard, `--apply --yes`
chooses the reversible transition branch, and `--ignore=<tracked-id>` adds a
host-local exclusion without scanning.

Explicit `--scan` searches only bounded roots inferred from managed
destinations across all effective profiles. It excludes tracked sources,
host-local files, ignored/attributed destinations, the config repo, and control
state; never follows symlinks; and does not descend into a directory whose
filesystem device differs from the managed root's device. This device-boundary
check does not detect a same-device bind mount. Only regular files and symlinks
are offered. Apply is TTY-only, asks separately for every path, defaults to
keeping it, and rejects both `--yes` and `--ignore`. A locked reload and rescan
may contract the approved set but never expands it, and deletion never prunes
parent directories. A managed root with a symlinked or non-directory parent is
refused rather than traversed.

Reversible deletion stores typed absent/file/symlink images, including
arbitrary bytes or link target, mode, and nanosecond mtime. Crash recovery
refuses to overwrite a replacement or traverse a changed/symlinked parent; the
journal remains active and conflicting mutations remain blocked until the
operator moves the replacement aside and retries recovery.

<!-- setforge-doc-flags: cleanup-orphans -->
| `cleanup-orphans` flags documented here | Meaning |
|---|---|
| `--profile` | Profile whose transition-history-attributed mode is reviewed. |
| `--config` | Select a manifest path. |
| `--apply` | Mutate; absence means dry-run. |
| `--yes` | Legacy mode only: choose reversible cleanup non-interactively. |
| `--ignore` | Legacy mode only: record one tracked id as host-local ignored. |
| `--scan` | Opt into bounded unrecorded-leaf discovery. |

### Managing user-section markers

User-section markers are **hand-authored** — there is no `section` subcommand.
Open the tracked source and add the `<!-- setforge:user-section ... -->` pair
yourself:

```markdown
<!-- setforge:user-section start host-local my-notes -->
... per-machine body; kept live, never shared ...
<!-- setforge:user-section end host-local my-notes -->
```

Rules the parser enforces (see `setforge/user_section_markers.py`):

- The `host-local` / `shared` keyword is **required** on both the start and end
  marker, and the two must match.
- The `NAME` is **optional** but must match between start and end when present;
  unnamed sections are keyed by their position in the file. No nesting.
- The end marker also carries a `hash=<sha256-hex>` segment (64 lowercase hex
  chars). **You never compute it by hand** — omit it (or leave any placeholder)
  when you first author the pair, and `setforge install` stamps and rewrites the
  real body hash on every run.

Pick the semantics keyword by where the body should live:

- **host-local** — per-machine content. The body is injected *markerless* into
  the live file and kept as an overlay in `local.yaml`; it never reaches the
  config repo and is always preserved live.
- **shared** — content that travels in the config repo. Tracked-side updates
  reconcile against live edits through the `install --reconcile-user-sections`
  wizard.

Markers work in any tracked text file, not just markdown. To *seed* an empty
host-local section's body from a reusable template, see the optional
`section_templates` / `section_slots` helper in
[configuration.md](configuration.md#seeding-host-local-bodies-optional).

## Mutating `--auto=*` confirmation

When a tracked_file carries drift, `sync` resolves it; pass `--auto=` for
non-interactive contexts:

- `--auto=use-live` — absorb every drift item into tracked (today's
  silent-absorb behavior).
- `--auto=keep-tracked` — reject every drift item; tracked stays as-is (safer).
- Without a TTY and without `--auto`, `sync` exits 1 with
  `CaptureRequiresInteractive`.

When `install` or `sync` runs with a **mutating** `--auto*` flag
(`--auto=use-tracked`, `--auto=use-live`, `--auto-accept-tracked`,
`--auto-accept-live`), setforge shows a risks panel describing what changes in
which direction, plus the exact `setforge revert` command to undo, then prompts
arrow-key yes/no (default **No**). For CI/scripts, pass `--yes` (`-y`) to bypass
the prompt; without `--yes` in a non-TTY context the command exits 1.

## Revert

`revert` undoes the most recent `install` or `sync` for the named profile by
replaying its transition record in reverse — file diffs via `patch -R`, plus
uninstalling extensions that were installed (and reinstalling ones that were
uninstalled). Drift on any touched file aborts cleanly with no partial revert.
A second `revert` acts as redo. Transition records live under
`~/.local/state/setforge/transitions/` and are kept indefinitely; if that
directory grows large you can remove it.
