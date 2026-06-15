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
- `-v` / `--verbose` (`-v` → INFO, `-vv` → DEBUG with secret redaction);
  `-q` / `--quiet` (errors only).
- `-o` / `--format [human|json]` — `json` emits a versioned envelope.
- `--version` — print the installed version and exit.

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

setforge ships ten subcommand groups for narrow inspections and edits. Run
`setforge <group> --help` for each:

| Group | Subcommands | Purpose |
|---|---|---|
| `section` | `add`, `emit` | Manage user-section markers in tracked markdown. |
| `override` | `fork`, `pin`, `unpin`, `unfork`, `reset`, `list`, `show` | Tracked-file disposition + sub-file span overrides. |
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
tracked-file orphans).

### Managing user-section markers

`setforge section` automates adding `<!-- setforge:user-section ... -->` marker
pairs to tracked markdown:

```bash
# Interactive: arrow-key picker for semantics + TUI anchor picker + confirm.
setforge section add --profile=<profile>

# Scripted: every flag set, --yes bypasses the final confirm.
setforge section add --profile=<profile> \
    --tracked-file=<key-from-setforge.yaml> \
    --semantics=shared \
    --name=my-notes \
    --anchor-line=42 \
    --body-source=empty \
    --yes

# Print a paste-ready marker pair for files setforge cannot edit
# (anything not .md or .markdown).
setforge section emit shared my-notes
```

`section add` only edits `.md` / `.markdown`; other suffixes print a hint to
use `section emit` and paste the pair manually. The end marker is stamped with
the body's sha256 hash on write so the pair passes strict parsing immediately.

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
