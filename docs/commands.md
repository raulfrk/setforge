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
show`, `transitions list`, `stage --list`, and `config show --effective`.
Every other command rejects `--quiet` and `--format=json` before doing work.
`--quiet` and `--format=json` are mutually exclusive.

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

## Subcommand groups

setforge ships eight subcommand groups for narrow inspections and edits. Run
`setforge <group> --help` for each:

| Group | Subcommands | Purpose |
|---|---|---|
| `plugin` | `list`, `add`, `remove`, `reconcile`, `sync-cache` | Claude plugins in a profile's `claude_plugins:` block. |
| `marketplace` | `add`, `remove`, `update` | Claude plugin marketplaces (upstream plugin sources). |
| `ext` | `list`, `add`, `remove`, `reconcile` | VSCode extensions in a profile's `extensions:` block. |
| `transitions` | `list`, `show` | Inspect install/sync/revert history. |
| `profile` | `list`, `show` | Inspect profile definitions and resolved overlays. |
| `config` | `show`, `add`, `remove` | Granular CRUD over `setforge.yaml` / `local.yaml`. |
| `snapshot` | `create`, `list`, `restore` | Directory-copy snapshots. |
| `completion` | `install` | Install shell completion scripts. |

Other top-level commands: `init` (bootstrap config dirs + `local.yaml`),
`upgrade` (PyPI check + release notes + `uv` upgrade), `migrate` (schema
migrations against `setforge.yaml`), `cleanup-orphans` (review/remove
tracked-file orphans), and `recover` (inspect or restore an interrupted
write-ahead operation; manual remediation records require explicit
`--acknowledge-manual --yes`).

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
