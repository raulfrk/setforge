# Command reference

The authoritative list is always `setforge --help` (and `setforge <command>
--help`). This page covers the commands you reach for day to day, the
subcommand groups, and the confirmation behavior of mutating runs.

All deploy/compare/sync commands require `--profile=<name>`; profiles live in
your config repo's `setforge.yaml`.

## Global options

Apply to every command (`setforge [OPTIONS] COMMAND`). They must come
**before** the subcommand: `setforge -o json compare` works, while
`setforge compare -o json` fails with "No such option".

- `--source PATH` — config source directory (overrides `SETFORGE_SOURCE` and
  `local.yaml`).
- `--code-bin` / `--claude-bin` / `--gitleaks-bin` — override external binary
  paths.
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

`sync` means "I tweaked something live, now save it and record a transition I
can revert later." It writes captured content into your config repo's
`tracked/`; `git diff` + commit + push from inside the config repo to lock it
in.

## Top-level command inventory

This table is intentionally complete and is checked against `setforge --help`.

<!-- setforge-doc-command-inventory:start -->
| Command | Purpose |
|---|---|
| `install` | Deploy tracked files and reconcile provisioned state. |
| `compare` | Report tracked/live drift. |
| `cleanup-orphans` | Review transition-attributed or explicitly scanned file orphans. |
| `cleanup` | Review undeclared provisioned binaries recorded by receipts. |
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
`tracked`, `tracked-overlay`, `not-applicable`, or `deleted-locally` (a local
deletion that a sync kept). Stale, corrupt, drifted, or
otherwise inconsistent records stay visible as errors and make the command
exit nonzero. A record whose project directory was moved, deleted, or replaced
names the command that drops it: `setforge project remove <profile> <path>`
releases that record's ownership claims and private Git entries without
touching project files. Dropping discards the pre-injection contents saved in
the record, so the preview warns that the injection cannot be removed normally
afterwards. The same command clears claims left behind when the
record itself was lost. A stale record never blocks injecting other projects.
A directory that is still the same one but gained, lost, or changed its Git
directory is not stale: `project remove` restores it normally.

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
an intentionally empty file. A current member that is missing from the project
is reported as `missing locally`, never `unchanged`: sync keeps the deletion,
and `--auto=use-profile` restores the file with the profile mode. Sync also
restores a hidden file's private exclude claim when it was removed by hand.

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
Claims held by a project injection are refused here: `setforge project remove
<profile> <path>` releases them together with the injection record.

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

For targeted package retirement, remove its declaration from the selected
profile, then preview its exact provider-qualified identity:

```sh
setforge cleanup --profile=work --package=github_release:owner/tool
setforge cleanup --profile=work --package=github_release:owner/tool --apply
```

Repeat `--package` to review several identities. A selector uses the provider's
identity, such as `cargo:ripgrep`, rather than the manifest's package alias.
Unknown, still-declared and ignored selections fail before removal. Apply keeps
the interactive per-item wizard and requires matching current ownership and
package evidence. It leaves other packages and lock pins untouched. Package
removal records no transition, so `revert` does not undo it and still targets
your last install or sync; reinstall is a separate action.

To update an already-managed instructions file belonging to another existing
profile, edit its current tracked source and preview only that declared file:

```sh
setforge install --profile=legacy --file=agents --dry-run
setforge install --profile=legacy --file=agents
```

Repeat `--file` for several tracked-file IDs in that profile. File-only install
uses its existing rendering and reconciliation context and retains unselected
files and native integrations. It does not bootstrap directories, provision
packages or reconcile plugins, extensions or MCP registrations. Every selected
container must already be managed by the current checkout; this mode does not
adopt or transfer resources.
Package lock coverage is not checked in file-only mode and the lock is not
refreshed.

New installs record symlink topology for exact undo and redo. Older transitions
without a link preimage refuse an ambiguous symlink undo before changing files;
use a reviewed file-only install to reconcile the desired source instead.

Removing a declaration and running an ordinary `install` does not uninstall
packages or rewrite resources outside the selected profile. Explicit retirement
uses the scoped workflows above; history-only file edits are not supported.

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

Native Codex config containers selected or retained in reconciliation state
for any configured effective profile are protected in both modes, even when
cleanup targets a different profile. Retiring native settings belongs to
native key reconciliation; orphan cleanup does not delete the whole container.

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

### Retired user-section markers

The hand-authored `<!-- setforge:user-section ... -->` marker pairs are
**retired** (schema 2.0 → 2.1). They are no longer parsed on install or sync:
a marker left in a tracked source is deployed verbatim and `sync` copies the
text between the markers into the shared source. `setforge validate` fails when a
tracked source still contains one, and `setforge migrate` strips them (host-local
bodies move to the markerless `local.yaml` overlay).

To keep part of a file host-only, use `setforge stage`: it classifies each text
hunk or YAML key as SHARED (may flow back to the config repo) or LOCAL (stays on
this host). A `.json` file is staged as a single whole-document unit.
A YAML or JSON file whose copy recorded at the last install cannot be parsed
is staged by text hunk instead, and keeps that mode once you have staged it. A
file already staged by key whose recorded copy later stops parsing is still
skipped by `stage`.

## Mutating `--auto=*` confirmation

When a tracked_file carries drift, `sync` resolves it; for non-interactive
contexts pass one of:

- `--yes` — absorb every drift item into tracked. `--auto=use-live --yes` is
  the same and stays accepted.
- `--auto=keep-tracked` — reject every drift item; tracked stays as-is (safer).
- Without a TTY and without `--yes` or `--auto=keep-tracked`, `sync` exits 1
  and writes nothing.

When `install` runs with the **mutating** `--auto=use-tracked`, or `sync` has
drift to capture, setforge shows a risks panel
describing what changes in which direction, plus the exact `setforge revert`
command to undo, then prompts arrow-key yes/no (default **No**). For
CI/scripts, pass `--yes` (`-y`) to bypass the prompt; without `--yes` in a
non-TTY context the command exits 1.

`install` asks the same way when a file with a declared `mode:` has different
permission bits on the host: the panel lists each file and the reset to the
declared mode (the live mode cannot be kept). To keep a deliberately different
mode, change the file's `mode:` in `setforge.yaml`, or, for this host only, set
`tracked_files.<id>.mode` (for example `0o600`) in
`~/.config/setforge/local.yaml`. With `--yes` the reset is printed and applied;
without `--yes` in a non-TTY context `install` exits 1 and changes nothing.

## Revert

`revert` undoes the most recent `install` or `sync` for the named profile by
replaying its transition record in reverse — every changed file, symlink and
directory is restored from the image recorded before the command (bytes, mode,
link target), plus uninstalling extensions that were installed (and
reinstalling ones that were uninstalled). Each file must still hold exactly what
the command left; timestamps are ignored. A file edited since — even on a line
the command did not touch — refuses the whole revert, naming the file, before
anything is written; save or undo that edit first. Directories the command
created are removed once the files in them are; a directory that still holds
anything else (your own files, or the `.bak` copies `install` keeps) is left in
place with its contents, and a directory that existed before the command is
never removed, even when empty (releases that used GNU `patch` removed empty
parent directories, including pre-existing ones). A second `revert` acts as
redo. Transition records live under `~/.local/state/setforge/transitions/` and
are kept indefinitely; if that directory grows large you can remove it.

Records written by earlier versions kept file changes as a text patch
(`changes.patch`). This version cannot revert those and refuses them with a
message naming the version that recorded them and the files involved;
`transitions list` and `transitions show` still list them. Revert such a record
with the version that wrote it, or restore the listed files by hand.
