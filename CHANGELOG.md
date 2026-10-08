# Changelog

All notable changes to setforge are tracked here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- `install` now asks whether to manage or transfer existing files and packages
  after it has worked out the full plan and while it holds the lock, instead of
  before. The questions, `--yes`, the refusal without a terminal and the exit
  codes are the same; declining still changes nothing. Another `setforge`
  command started meanwhile waits until the question is answered. Problems the
  plan finds (a missing tracked source, a managed-tree conflict, an interrupted
  operation that needs `setforge recover`) are now reported before the
  ownership question or refusal. `--reconcile-user-sections` shows its conflict
  screens before the question, but a file whose ownership blocks the install is
  still refused first, and the screens open only when the question can be
  answered: with `--yes`, or with a terminal on standard input. Without either,
  `install` refuses a pending adoption as before and, when nothing needs
  adopting, leaves conflicts unresolved the way a piped `install` does instead
  of opening a screen it cannot read keys for. `install` no longer checks
  packages twice. A
  config file edited while `install` is starting or waiting for an answer is
  refused with "install configuration changed while loading; retry" or
  "install inputs changed after planning", replacing the "changed after
  confirmation" messages.
- With `claude.install_mode: local-clone`, a marketplace whose cache directory
  already holds a clone of a different repo (two repos with the same name, such
  as `alice/tools` and `bob/tools`) no longer opens the keep/update/both/abort
  prompt. `install`, `plugin reconcile` and `plugin sync-cache` now report an
  error that names the marketplace, the directory and both repos and says what
  to run, and they do not touch that directory. `install` stops before
  deploying anything; `plugin reconcile` reports that marketplace as failed,
  carries on with the others and exits 1; `plugin sync-cache` stops at that
  marketplace and exits 1. The "keep the existing clone" step shows the repo
  as `owner/repo`. A cache created by the old "both" choice keeps working.
  `plugin reconcile --yes` is still accepted but no longer does anything.
- `upgrade` no longer tries to show release notes or guess at schema changes
  (it could not find the changelog in an installed copy anyway). It prints the
  changelog link instead and offers "Upgrade" or "Upgrade + `migrate --check`".
  `--no-prompt` runs the second one, as it already did for installed copies.
  The yanked and pre-release warnings and the `migrate --check` after the
  upgrade are unchanged.
- `upgrade --check` and `--to` now look up the version list with a single plain
  request to PyPI instead of a custom client. `--to` no longer makes a second
  request, a version PyPI does not list is reported as such, and the small
  cache file the old client kept in the cache directory is no longer used (it
  can be deleted). Timeouts, certificate checks, the clear error and exit code 1
  when PyPI is unreachable or answers with an error (nothing is changed) and the
  way versions are compared are unchanged. A redirect to another host is now
  refused instead of followed.
- An interrupted operation that still needs `setforge recover` now blocks every
  command that changes SetForge's state, a managed file, a package, an
  extension or plugin, or a config repository, not only the ones touching the
  same profile, config repository or packages. This includes `setforge config
  add` and `config remove`: they refuse and name the `setforge recover` command
  to run first. Read-only commands such as `compare`, `status` and `validate`
  keep working. So does `setforge completion install`, the one command that
  writes without being blocked: it only writes the completion script and the
  shell rc file.
- An interrupted `setforge ownership release` or `ownership revert` is now
  undone instead of completed. Run the `setforge recover
  --profile=ownership-<owner-id> --apply` command that the next mutating
  command prints: it restores the claim and its history exactly as they were,
  and you then repeat the release or revert. Until then every mutating command
  refuses, as for any other interrupted operation.
- `setforge ownership recover` no longer handles new interruptions. It still
  completes a release or revert that SetForge 1.4.0 or earlier left unfinished,
  and `ownership release` and `ownership revert` refuse while such a record
  exists, naming `setforge ownership recover --apply`. With nothing of that kind
  to complete it exits 0, changes nothing and prints the `setforge recover`
  command to use. Scripts that call `ownership recover --apply` after a crash
  should call `setforge recover` instead.
- Project injection records written before SetForge 1.3.0 are converted to
  the current format the first time `project inject`, `sync`, `visibility` or
  `remove` runs against their directory without `--dry-run`; the command says
  which profiles it converted. Only the record is rewritten. `project list` and
  `--dry-run` no longer read such a record: they report it as an error naming
  `setforge project sync <path>`, or `setforge project remove <profile> <path>`
  when its directory is gone or was replaced. A record in the oldest format, from before
  1.2.0, behaves differently in two ways: its first sync now merges three ways
  instead of asking about every differing line, and a record whose file and
  profile source have both changed since injection is refused and needs one
  `project sync` with SetForge 1.3 or 1.4 first. A file overlaid on tracked
  content by a build between 1.2.0 and 1.3.0 stays hidden from Git instead of
  being exposed by its first sync. SetForge 1.3.0 and later keep reading a
  converted record.
- `setforge project inject` of a profile that is already injected in that
  directory now always stops with "already injected; use `setforge project
  sync <path>`" and exit status 1, and also names `project visibility` and
  `project remove` for a different Git visibility or config file, which sync
  does not apply. An exact repeat used to exit 0 with "no
  changes", and could re-add a missing private exclude entry; `project sync`
  does that. The message no longer says which of the profile, the visibility
  flag or a file differs.
- `setforge validate` suggests a "Did you mean" name in more cases, including
  shortened names such as `plugins` for `claude_plugins`.
- `setforge sync --yes` now captures all live drift on its own; it used to stop
  with "--yes requires --auto". `--auto=use-live --yes` still works and does
  the same thing.
- `setforge cleanup` no longer records a transition when it deletes a package.
  That record held nothing to undo, yet it became the newest one, so the next
  `setforge revert` reverted nothing and exited 0. `revert` now goes to your
  last real install or sync. Records written by earlier versions stay listed
  and behave as before; `revert --to-before=<id>` still goes past them.

### Fixed

- Answering "no" to `setforge migrate --apply` now really changes nothing. When
  the migration included the step that retires user-section markers (schema 2.0
  to 2.1), merely showing the preview stripped the markers from the deployed
  file (for example `~/.claude/CLAUDE.md`) before you had answered, and
  declining did not put them back. For the same reason a migration that failed
  and rolled back left that file without its markers; it is now restored too.
  The preview now shows the change to that file correctly, and a confirmed
  migration writes the same result as before.
- `lock --update <package>` now records the profile you ran it for on that
  package's existing entry in `setforge.lock`. It used to leave the entry
  listing only the profiles it had before, so once the profile that originally
  locked the package stopped using it and re-ran `lock`, the entry was removed
  even though the updated profile still needed it.
- `plugin reconcile --dry-run` (and a plugin policy of `report`) now reports a
  marketplace cache directory that holds a different repo, with the same
  `FAILED` line a real run prints, without cloning or changing anything. It used
  to list only the plugin actions, so the error showed up only on the real run.
  The exit code is 1, as before.
- `setforge stage NAME` now stages only the tracked file whose ID is `NAME`.
  It used to also walk any other tracked file whose live file was called `NAME`,
  and asked you to classify that file's changes too, or refused it as one-way
  output when a generated file's live file was called `NAME`. The live file name
  or path is still used when no tracked file has that ID. A live file name that
  several tracked files share is now refused with the matching IDs and paths
  listed, instead of staging all of them; pass the ID or the full path. A live
  path given as `~/...` or relative to the current directory now works too.
- In the Claude-assisted merge, a draft you edit by hand is now checked like
  Claude's own drafts: an empty edit or one that still contains conflict markers
  is refused with a message, and you stay on the review screen to edit again or
  go back. It used to be accepted and written into the file as typed.
- A host-local section that `install` seeded from a `section_templates` entry and
  that you later deleted now stays deleted. It used to come back on the next
  install that also brought a change to the same tracked file.
- `setforge recover` after an interrupted install now also restores a file that
  install was updating through a symlink at its destination, along with that
  file's `.bak` copy; the symlink itself is left alone.
- The first install of a managed tree whose destination holds nothing but
  SetForge's own state, cache or data directory (for example `~/.local/state`,
  `~/.local` or `~/.cache` on a new machine) now deploys the tree. It used to
  ask to adopt the directory SetForge had just created and deploy nothing until
  a second install. A destination holding anything else is still adopted only
  with consent.
- `setforge recover --apply` after an interrupted install no longer stops with
  "no unfinished operation" when the managed tree's inventory, written by a
  version before 1.4.0, still lists SetForge's own files.
- `setforge project remove` previews a file that injection created as
  `delete:` instead of `restore create:`, and says `leave absent:` for one that
  is already gone.
- `setforge stage` now lists and stages a YAML or JSON file whose copy recorded at
  the last install is not valid YAML or JSON. It used to be left out silently, so
  its local changes could not be marked shared or local. Such a file is staged by
  text hunk, like a plain file, and `sync` and `compare` follow suit; a file that
  has already been staged keeps being staged the same way.
- A profile name containing a path separator, `..` or a control character is
  now refused when SetForge saves, reads or prunes its merge baselines, as it
  already was for the rest of its per-host state. Such a name could previously
  reach files outside the profile's own directory. A profile directory that is
  itself a symlink is now refused too.
- A `setforge migrate` whose automatic rollback cannot finish can now be undone
  with `setforge recover`; before, recover stopped with "refusing to remove
  non-empty recovery directory". `migrate` now says the rollback did not
  complete and prints the `setforge recover` command to run, instead of
  claiming it rolled back.
- `setforge migrate` now stops before changing anything when its state
  directory is not writable ("transition state dir not writable"), instead of
  migrating the config and then failing.
- `setforge completion install` now writes the completion script from the
  SetForge you are running. It used to start whichever `setforge` came first on
  your `PATH` and, if that failed, fall back to a bundled copy with a warning.
  The script it writes is unchanged.
- A misspelled variable in a `template: true` destination (for example
  `{{ home_typo }}/x.txt`) now stops `install`, `install --dry-run`, `sync`,
  `compare`, `status` and the other commands that locate your files with an
  error naming the variable, before anything is written. `validate` already
  rejected it, but these commands used to treat the unknown variable as empty
  and use the shortened path (`~/x.txt`). Destinations that `validate` accepts
  resolve exactly as before.
- `setforge config add --local marketplaces.add NAME` now writes the marketplace
  under `marketplaces: add:` in `local.yaml`, beside any existing `add` and
  `remove` entries, and refuses a name that is already there or that begins
  with `-`. It used to write `NAME` directly under `marketplaces:`, which
  `validate`, `profile show`, `compare` and `install` then rejected as an
  unknown key until you fixed `local.yaml` by hand. A `local.yaml` already
  written that way still has to be corrected by hand: move the entry under
  `add:`.
- `setforge plugin add` now checks that `--profile` exists before it changes
  anything, for Claude and for Codex. A mistyped profile used to leave the new
  marketplace and plugin in `setforge.yaml` and register the marketplace with
  Claude before the command failed with "profile not found" (Codex called its
  tool and then undid it, and reported just the profile name). It now fails
  first with "profile not found: <name>" and changes nothing. For Claude, it
  also refuses up front when `packages` already holds a different package under
  the plugin's name (it used to bind the profile to that package and report
  success), naming the package and leaving the config and Claude untouched.
- `setforge plugin add --no-install` for Claude now only edits `setforge.yaml`,
  as its help says. It used to still register a new marketplace with Claude
  (and, with `claude.install_mode: local-clone`, clone it into the local cache).
  The marketplace, plugin and profile entries are still written to the config.
- `setforge inspect` on a tracked file whose destination you deleted now shows the
  live file as missing, in the header and as an empty live pane in the JSON
  output. It used to show the copy recorded at the last install as if it were
  still on disk, so it looked like the next `install` would restore the file.
- `setforge inspect` no longer reports a conflict for a YAML or JSON file when
  you changed one key and the tracked copy changed a different key. It now shows
  the combined file that `install` writes, with comments kept, instead of
  conflict markers for edits that `install` merges without asking. A real
  conflict, where both sides changed the same key, is still shown.

### Removed

- The `setforge capture` command is gone; use `setforge sync`, which does the
  same capture and can be undone with `setforge revert`. Unlike `capture`,
  `sync` also brings the profile's extension list in `setforge.yaml` in line
  with the extensions installed on the host, and records a transition.
  Two things `capture` did not need, `sync` does: `code --list-extensions`
  must not fail or time out (if it does, `sync` stops before writing anything;
  a missing `code` command only skips the extension step with a warning), and
  the SetForge state directory must be writable, to record the transition.
- `setforge install --retry-failed` is gone. Run `setforge install` again
  instead: it retries the plugins and extensions that failed last time and
  leaves everything already in place alone. Install also stops writing
  `reconcile_outcomes.json` into its undo records; files already there are
  left untouched and ignored.
- The "yes + open editor before applying" choice of the `setforge revert`
  prompt is gone. It opened a text copy of the plan the prompt had already
  shown and did not read it back. The other choices are unchanged.
- `setforge snapshot restore --non-interactive` is no longer listed in `--help`
  or the docs; it is still accepted and means the same as `--yes`.
- The hidden `--no-transition` option of `setforge install` and `setforge sync`
  is gone. Every install or sync that changes something records a transition,
  so it can be undone with `setforge revert`.
- `setforge install` no longer accepts `--auto-accept-tracked` or
  `--auto-accept-live`. Both did the same thing: let install reset a file's
  permission bits to its declared `mode:`. Install now asks for that in its
  confirmation prompt, listing each file and the mode change. In a script, pass
  `--yes`: install prints each reset and applies it, where `install --yes`
  alone used to stop with "permission-mode drift". Without `--yes` and without
  a terminal, install still stops and changes nothing.

## [1.4.0] - 2026-10-05

### Fixed

- `setforge recover` after an interrupted install that was updating a file now
  also removes the `.bak` copy that install had written, so the tree returns to
  its exact earlier state; a `.bak` that existed before is restored as it was.
- The first install of a managed tree that contains SetForge's own state
  directory no longer stops with "inputs changed after confirmation; retry".
- Home directories reached through a symlink, such as NFS or automounted homes,
  work across install, sync, revert, snapshots, orphan cleanup and project
  commands. Symlinks present when an operation is planned are followed; a
  directory swapped or re-pointed afterwards is still refused, and SetForge's
  own ownership state never follows one.
- Recovery and project injection no longer depend on the filesystem device
  number, so they survive an NFS remount or reboot. Managed trees install,
  update and prune on filesystems that reject atomic exchange renames.
- A refused or failed revert, a failed migration on a fresh host and a failed
  project sync no longer leave an operation that `recover` cannot clear. Corrupt
  journals and lock or filesystem errors produce one clear line.
- YAML and JSON files are byte-exact when there is nothing to merge: a second
  install is a no-op, comment-only and formatting-only tracked changes are
  deployed, and host edits are left alone when the tracked file is unchanged.
  Merges keep untouched lines, anchors, merge keys, BOM and line endings. Files
  the key-aware engine cannot parse are merged line by line instead of
  aborting the install, and top-level JSON arrays merge element edits made at
  different positions.
- Sync shares only the classified values of a YAML file, never host comment
  text, and refuses to promote a JSON or YAML file that no longer parses.
- Install, compare, sync and revert handle CRLF and non-UTF-8 files with exact
  bytes. A line-ending-only difference is reported as drift.
- Revert restores exact bytes, works through symlinked destinations, refuses a
  missing or empty patch instead of reporting success, and leaves no `.orig`
  files. A user-scope MCP change can be reverted from any directory.
- A deployed file deleted on the host stays absent and no longer fails install;
  `--auto=use-tracked` restores it.
- Project removal keeps local edits that a sync merged, restores a directory
  whose Git directory changed, and can drop records of moved or deleted
  projects. Re-injecting after a removal works when the profile changed.
- Configuration edits keep the file's indentation, line endings and comments.
  `validate` marks the offending line, names missing fields and rejects nested
  destinations. The `local.yaml` template written by `init` validates.
- `status` counts missing files and shows an unfinished operation. Piped output
  is no longer wrapped at 80 columns. `inspect` rejects ambiguous names.
- Mutation-test workers are capped in memory, so a runaway mutant can no longer
  exhaust the host running the nightly gate.
- `install`, `compare` and `sync` no longer refuse a Markdown file that merely
  documents the retired user-section markers.
- A failed `stage` rolls back while still holding its locks. A checkpoint over
  a path with no saved pre-image is refused instead of silently left out of
  recovery, and an operation journal stays loadable after a parent of its
  config directory becomes a symlink.
- `cleanup-orphans` never offers SetForge's own state, journals, cache or
  snapshots for deletion when they sit under a managed root.
- `cleanup` and `cleanup-orphans` write `local.yaml` atomically and keep its
  comments and layout.
- A `type: local` package no longer overwrites a file it did not install at
  its destination. A plain (non-archive) file identical to the source is
  adopted.
- `project sync` merges YAML and JSON members exactly as `install` does, so
  untouched lines keep their bytes and a JSON file with a duplicate key gives a
  conflict instead of a crash. `project remove` no longer recreates a file Git
  removed and works after a re-clone; injecting into a repository without
  `.git/info` works; a stale record whose old path is now a symlink can be
  dropped.
- `install --dry-run` reports the same drift-gate count the real run enforces.
- A corrupt adapter record in a transition gives a clean error from `revert`,
  `transitions show` and `transitions list`.
- A transition's recorded end time is no longer earlier than its start.
- `install --dry-run` prints a `would-be refusal` block for permission-mode
  drift and for a symlink destination occupied by a regular file, which the
  real install refuses.
- `sync` and `capture` refuse a staged file whose stored draft is not UTF-8
  while planning, in every mode including `--auto=keep-tracked`, instead of
  failing with a decode error after the prompt.

### Changed

- `install --auto=use-tracked` requires `--yes` without a terminal before it
  replaces a live file, as documented.
- `cleanup-orphans` previews never refuse; applying still requires releasing an
  active ownership claim first and now prints the exact command.
- `snapshot create --keep` prunes per profile. `migrate` without an action flag
  exits 2. Extension ids must have the `publisher.name` form.
- `--auto=keep-live` and `--auto=use-tracked` resolve held managed-tree entries.
- A file or tree claimed by another configuration is treated as not yet
  authorised by `compare`, `status`, `sync` and `capture`, as `install` and
  `stage` already did: staged drift shows as unexpected and `sync` asks for
  `stage` first.
- `install`, `install --dry-run` and `compare` no longer print the blocks that
  described the retired host-local injection, `transitions show` no longer
  prints `overlay:`, and `project inject` no longer prints the worktree
  auto-carry line.
- The unused `diff-match-patch` dependency is removed.
- `revert` restores the recorded copy of every changed file instead of
  reversing a text patch, so it no longer needs the GNU `patch` program.
  If a recorded file was edited after the transition, `revert` refuses that
  file by name and changes nothing, even when the edit is on other lines; a
  `touch` or `git checkout` that leaves the bytes equal no longer blocks it.
  A `--to-before` chain checks every step before the first write. `show` and
  the preview now list every recorded path, including tree entries, symlinks,
  claim files, orphan deletions and empty files.
  `revert` also removes the directories the command created, deepest first,
  and keeps any that gained other entries or existed before.
- Transitions recorded by 1.3.9 or earlier 1.4 development builds in the old
  patch format cannot be reverted by this version: `revert` refuses them with a
  message naming the version that recorded them and the paths to restore by
  hand. They still appear in `transitions list` and `show`.
- `--patch-bin` is removed (scripts passing it now fail with exit 2);
  `SETFORGE_PATCH_BIN` and a `patch:` key in `local.yaml` are accepted and
  ignored.
- Project overlay state written by this release is not readable by 1.3.9;
  state written by earlier releases still reads.
- Operation journals no longer record the command line. `project remove`
  previews `leave absent` for a file Git removed.

## [1.3.9] - 2026-09-28

### Added

- Targeted retirement through `install --file` and `cleanup --package` selectors,
  with exact ownership checks and preservation of unrelated managed resources.

### Fixed

- Package cleanup matches complete GitHub release receipts, including the
  selected artifact and platform, before retiring an owned installation.
- Structured reconciliation preserves signed JSON values and literal bracket
  keys, including existing YAML classifications and staged drafts.
- Claude MCP reconciliation reads current native inventories across user,
  project and local scopes, honors config overrides, and preserves exact command
  arguments. Ambiguous or unrepresentable registrations refuse changes.
- MCP updates record both replacement and prior endpoints for undo and redo.
  Recovery checks recorded destinations and supports interrupted reverse chains;
  failed reversals report failure and compensate earlier effects.
- Missing Claude still warns and skips MCP reconciliation before probing Git.

### Changed

- Automatic MCP reversal requires recorded native context. Older records without
  this context refuse automatic reversal rather than guessing a destination.
- Older symlink transitions without recorded link preimages refuse ambiguous
  automatic reversal before changing files.
- Expanded public CLI, project-sync, structured-merge and native MCP regression
  coverage, including isolated recovery, mutation and installed-wheel checks.

## [1.3.8] - 2026-09-28

### Fixed

- Managed-tree installation preserves inventory order independence, ownership,
  directory modes and rollback state, including recovery after process failure.
- Snapshot restoration and cleanup preserve current resource membership and
  ownership. Orphan detection protects other profiles' native configuration and
  skips unreadable transition history without losing genuine orphan candidates.
- Configuration authoring validates effective profiles before writing. Migration
  chains preserve supported intent and refuse unsupported lossy reversals.
- Project injection, visibility, sync and removal preserve Git and filesystem
  state across ownership conflicts, membership changes and partial failures.
- Provisioning receipts, shared package lifecycles and external CLI responses
  retain verified identities and report failures accurately.
- Status honors the explicitly selected configuration repository.

### Changed

- Expanded public CLI, mixed-resource lifecycle, crash recovery and migration
  regression coverage. Completed the functional audit with container, installed
  wheel and mutation verification.

## [1.3.7] - 2026-09-27

### Fixed

- Orphan cleanup protects native Codex configuration used by other configured
  profiles, including settings retained in reconciliation state. Native setting
  retirement no longer exposes the entire config file to unrelated cleanup.
- Unrecorded-path scanning protects declared native config even without
  transition history, and recognizes preserved dangling and directory symlinks
  in managed trees while still finding genuinely unrecorded neighbors.
- Managed-tree snapshots capture and restore preserved symlinks, respect tree
  exclusions, and use the same policy for current restore authorization.

## [1.3.6] - 2026-09-27

### Fixed

- Compare and orphan cleanup no longer classify active native Codex configuration
  or managed skill directories as leftovers. Protection includes tree roots,
  empty nested directories, preserved symlinks, and active containing directories
  while keeping genuinely retired tracked files eligible for cleanup.
- Orphan cleanup retains the fully resolved profile, including native Codex
  instructions and skills, when rechecking candidates before deletion.

## [1.3.5] - 2026-09-26

### Fixed

- Codex plugin operations accept current native JSON receipts and register
  declared marketplaces when native registration is missing. Marketplace cache
  aliases and SHA-256 Git pins remain consistent across resolution and install.
- Plugin, MCP, Git, and executable boundaries reject option-shaped identifiers,
  malformed inventories, and failed process launches with actionable errors.
  Executable overrides remain valid when child processes change directories.
- Orphan cleanup, project sync, and project removal honor active ownership
  claims. Historical ignore IDs survive declaration removal, while unknown IDs
  are rejected and managed-tree inventory paths use canonical identities.
- Shared direct and bundled packages follow dependency order without duplicate
  actions. Soft prerequisite failures block dependent capabilities, Python pins
  use canonical identities, and relocking prunes obsolete profile memberships.
- Ambiguous lock updates require qualified selectors, colliding Go executable
  destinations are rejected, and release installers use the selected artifact's
  checksum. Pinned prerelease metadata and provisioned-package receipts are
  handled consistently.
- Capture confirmation includes Codex and extension writes, missing staged files
  fail during preview, and inherited extension selections must be representable
  before capture changes configuration. Extension mutations are case-insensitive.
- Meaningful Markdown trailing whitespace requires confirmation, and structured
  staging validates reconstruction before saving classifications. Existing shared
  Markdown confirmations may require reconfirmation.
- Deselected or moved Codex configuration fragments retire managed keys at old
  destinations while preserving unrelated content. Unknown overlay removals and
  normalized plugin add/remove conflicts are rejected.
- Backup refresh is atomic, declared symlink snapshots include their managed
  payloads, and absent-state restoration flushes affected directories.
- Configuration mutations validate profile selection and YAML octal modes;
  migration rejects unsupported future schemas and reports invalid UTF-8 cleanly.
- Generated templates allow only supported expressions, generated identifiers and
  path literals are validated, and local relocation anchors remain Markdown-only.
- Compare checks retain missing-tree and mode drift despite staged content;
  status reports branches behind their remote, marketplace provenance is accurate,
  and the inspect JSON help example is directly executable.
- Release preflight recognizes the current Workbox CI jobs and continues to
  reject missing required workflow jobs.

## [1.3.4] - 2026-09-19

### Fixed

- Cleanup refuses destructive orphan and provisioned-package actions when
  `local.yaml` is unreadable, non-UTF-8, or carries wrongly shaped ignore
  lists, instead of silently dropping the user's protections.
- Local package reconciliation tolerates coexisting legacy and typed receipts,
  prefers the typed record, and re-provisions missing destinations.
- `setforge upgrade` validates PEP 440 targets, checks the explicitly selected
  manifest after upgrading, and compares canonical versions without turning
  host-configuration diagnostics into a failed upgrade.
- Project target verification converts missing or timed-out Git invocations
  into actionable SetForge errors instead of Python tracebacks.

## [1.3.3] - 2026-09-18

### Fixed

- `setforge upgrade` reports a successful upgrade as success again: the
  post-upgrade check now understands the version format `uv tool list` actually
  prints.

## [1.3.2] - 2026-09-18

### Fixed

- GitHub release packages whose installed binary is removed outside setforge
  are re-provisioned on the next run instead of being reported as already
  present.

## [1.3.1] - 2026-09-07

### Fixed

- Stage, inspect, and install now expose consistent current staging
  classifications during clean, no-op reconciliation.

## [1.3.0] - 2026-09-04

### Added

- Tracked project overlays preserve injected hunks privately, with reversible
  removal and conflict-aware synchronization.
- Per-file project visibility lets injected destinations move between private
  and tracked Git behavior.

### Fixed

- Source distributions include only intended project paths, excluding private
  checkout state and unrelated local files.
- Install status distinguishes deployed state from install provenance, and
  dry runs predict transition creation from actual planned changes.
- Explicit configuration paths take precedence over source discovery; install
  checks remain attached to the selected configuration.
- Configuration validation rejects unresolved references, duplicate destinations,
  invalid effective Codex resources, and duplicate lock identities.
- Reconciliation preserves absence and empty values, compares structured values
  semantically, and handles unsupported shapes and nested YAML safely.
- Migration sources resolve under the tracked root, profile migrations preserve
  user intent, and schema version lookup accepts canonical equivalent versions.
- Project injection preserves ownership and identity across worktrees; cleanup
  retains protected content and managed-tree planning preserves user choices.
- Symlink deployment and comparison agree on target semantics, strict comparison
  detects missing destinations, and Git sources verify identity and freshness.
- Codex plugin installation accepts its operation-specific success response;
  external CLI parsing and source filesystem errors produce precise diagnostics.

## [1.2.0] - 2026-08-28

The project-profile release. SetForge can now describe a project once, inject it
into selected Git checkouts and worktrees, and atomically reconcile every
recorded Claude and Codex injection for one named target while preserving
explicit membership, visibility, and host-local choices.

### Added

- **Project profiles across repositories and worktrees.** Typed project
  declarations resolve repository identities, profile membership, and target
  visibility without conflating linked worktrees or unrelated clones.
- **Explicit project injection and removal.** Operators can add or remove a
  project profile from selected Claude and Codex targets while retaining
  unrelated configuration and refusing ambiguous legacy state.
- **Atomic target-wide project sync.** One command plans and applies the full
  target set with deterministic multi-config locking, exact stale-plan checks,
  transactional rollback, restart recovery, and hunk-level conflict resolution.
- **Private Git visibility controls.** Exact project paths can be published to a
  repository's private `.git/info/exclude`, shared by its linked worktrees, so
  hidden paths stay out of normal Git status while no hide metadata is committed.

### Changed

- **Configuration failures are stricter and clearer.** Non-mapping roots and
  unsupported file-format versions now refuse with focused diagnostics instead
  of failing later or accepting an unsafe shape.
- **Nightly verification is more reliable.** Quality-gate fixtures isolate
  parallel workers and keep mutation/static-analysis execution hermetic.

## [1.1.0] - 2026-08-25

The safe-adoption and Codex-integration release. SetForge can now discover,
adopt, transfer, release, and recover authority over existing resources while
keeping discovery read-only and destructive actions explicit. Its resource
model now covers files and regions, generated resources, directory trees,
typed application capabilities, packages with provenance, and
platform-specific release assets.

### Added

- **Durable resource ownership and safe adoption.** Origin-neutral claims,
  collision-safe checkout identities, explicit adoption, inline transfer,
  release tombstones, history, revert, and recovery let users bring existing
  resources under management without discovery silently granting authority.
- **Broader resource capabilities.** Typed application graphs coordinate
  dependencies across ecosystems; generated resources resolve host inputs at
  deploy time; managed directory trees support inventory and orphan policy;
  package reconciliation records provenance; and GitHub release packages can
  select checksum-verified assets per platform.
- **Full Codex configuration lifecycle.** SetForge validates and reconciles
  Codex configuration, filesystem resources, MCP servers, plugins and
  marketplaces, and durable lifecycle state, with a published parity contract
  covering the supported Claude and Codex surfaces.

### Changed

- **Install and reconciliation are recoverable by construction.** Operations
  use one immutable plan, durable restart recovery, reversible store pruning,
  transactional snapshot restore, stable unit identities, and explicit staged
  participation. Read-only commands remain mutation-free and shared units
  require reconfirmation before writes.
- **CLI output and profile resolution fail closed.** Human and structured
  output modes share one effective-profile path, interactive capture retains
  its diagnostics, and malformed global output combinations no longer fall
  through ambiguously.
- **Faster, more representative CI.** Local pytest has a fast lane; Docker
  smoke and full suites have non-overlapping boundaries, isolated network
  canaries, prebuilt images, current completion fixtures, and restart-recovery
  coverage.

### Fixed

- **Installed wheels now include every runtime dependency.** `pathspec`, used
  by managed-tree exclusion matching, and `click`, used by CLI error handling,
  are declared directly instead of arriving only through development or
  transitive dependencies.
- **Local package destinations are repaired when missing.** A valid receipt no
  longer suppresses reinstall when the recorded destination has disappeared.
- **Legacy and interrupted reconciliation state remains safe.** Legacy
  identities are preserved, structured deletions are correctly subsumed,
  reconcile drafts route by unit kind, Cargo lock pins are enforced, and
  orphan restart recovery runs in CI.

## [1.0.0] - 2026-08-08

### Fixed

- **`cleanup-orphans` no longer over-reaches.** Orphan detection now
  scopes candidates to currently-managed destination roots — an ancestor
  directory of a tracked file's destination, excluding generic shared
  roots such as `~/.config` and `/tmp`. The config source manifest, files
  left by a retired profile, and stray `/tmp` scratch are no longer
  offered for deletion, while a file removed from a still-managed tree
  still surfaces. The dry-run note now reports an `unmanaged` skip count.

### Removed
- **The inert `install --strict-spans` flag and the `setforge section`
  command** are gone. Both were dead remnants of the retired overlay-spans
  subsystem: `--strict-spans` gated a pinned-orphan refusal that can never
  fire (the span-orphan set has been empty since the disposition/spans
  cutover), and `section detect` produced overlay-span metadata the deploy
  path no longer reads. Their removal retires the last callers of six
  now-deleted legacy modules.

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
[Unreleased]: https://github.com/raulfrk/setforge/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/raulfrk/setforge/compare/v1.3.9...v1.4.0
[1.3.9]: https://github.com/raulfrk/setforge/compare/v1.3.8...v1.3.9
[1.3.8]: https://github.com/raulfrk/setforge/compare/v1.3.7...v1.3.8
[1.3.7]: https://github.com/raulfrk/setforge/compare/v1.3.6...v1.3.7
[1.3.6]: https://github.com/raulfrk/setforge/compare/v1.3.5...v1.3.6
[1.3.5]: https://github.com/raulfrk/setforge/compare/v1.3.4...v1.3.5
[1.3.4]: https://github.com/raulfrk/setforge/compare/v1.3.3...v1.3.4
[1.3.3]: https://github.com/raulfrk/setforge/compare/v1.3.2...v1.3.3
[1.3.2]: https://github.com/raulfrk/setforge/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/raulfrk/setforge/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/raulfrk/setforge/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/raulfrk/setforge/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/raulfrk/setforge/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/raulfrk/setforge/compare/v0.2.2...v1.0.0
[0.2.2]: https://github.com/raulfrk/setforge/compare/v0.2.0...v0.2.2
[0.2.0]: https://github.com/raulfrk/setforge/releases/tag/v0.2.0
