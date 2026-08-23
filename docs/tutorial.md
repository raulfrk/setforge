# setforge tutorial — the main workflows, end to end

This is the guided tour of setforge: a narrative walkthrough of the full
lifecycle followed by a practical command guide with examples, realistic
terminal mockups, and links to the complete inventory.

- New here? Read **[Part A — Guided walkthrough](#part-a--guided-walkthrough)** top to bottom.
- Looking for one command? Jump to **[Part B — Command reference](#part-b--command-reference)**.
- Exhaustive flag lists live in **[docs/commands.md](commands.md)**; the full
  `setforge.yaml` schema lives in **[docs/configuration.md](configuration.md)**.
  This page links to them rather than repeating them.

> Terminal output below is captured from real runs (plain-stdout surfaces) or
> drawn faithfully from the rendering code (interactive prompts). Paths are
> shown with a placeholder home (`~`) and host (`myhost`).

## Contents

- [Concepts in 60 seconds](#concepts-in-60-seconds)
- [Part A — Guided walkthrough](#part-a--guided-walkthrough)
  - [1. Install the engine](#1-install-the-engine)
  - [2. `init` — bootstrap the host](#2-init--bootstrap-the-host)
  - [3. Author `setforge.yaml` + `tracked/`](#3-author-setforgeyaml--tracked)
  - [4. `install` — deploy tracked → live](#4-install--deploy-tracked--live)
  - [5. `compare` — see drift](#5-compare--see-drift)
  - [6. `sync` — capture live → tracked](#6-sync--capture-live--tracked)
  - [7. `revert` — undo the last transition](#7-revert--undo-the-last-transition)
- [Part B — Command reference](#part-b--command-reference)
  - [Lifecycle: install, compare, sync, capture, revert, status, validate](#lifecycle-commands)
  - [Config repo and locks: init, fetch, lock, migrate, upgrade](#config-repo-commands)
  - [cleanup and cleanup-orphans](#cleanup-orphans)
  - [User sections (host-local vs shared) + the reconcile wizard](#user-sections--the-reconcile-wizard)
  - [Plugins, marketplaces, extensions](#plugins-marketplaces-extensions)
  - [Snapshots](#snapshots)
  - [Profiles, transitions, config](#profiles-transitions-config)
  - [Completion + global options](#completion--global-options)

---

## Concepts in 60 seconds

- **Two repos.** The **engine** (this Python package, `setforge`) is the tool.
  Your **config repo** is the source of truth for what gets deployed: a
  `setforge.yaml` manifest plus a `tracked/` directory of file content. The
  engine never ships your config; your config never ships the engine.
- **Source discovery.** The engine finds your config repo by walking four
  layers, first match wins: `--source PATH` → `SETFORGE_SOURCE` env →
  `~/.config/setforge/local.yaml` `source:` block → a `setforge.yaml` in the
  current directory. (Git sources live in `local.yaml`; the flag/env take
  paths only.)
- **Profiles.** A profile is a named subset of tracked files, packages,
  bundles, and MCP servers, with optional inheritance (`extends:`). Reconcile
  policy for plugin/extension package types also lives on the profile.
- **Schema.** New `setforge.yaml` files carry `schema_version: "6.5"`. An optional
  `minimum_version:` floor refuses to run an engine older than your config
  needs. Older `version: 1` configs still load and are migrated forward by
  `setforge migrate`.
- **Live vs tracked.** *Tracked* is the content in your config repo. *Live* is
  what's deployed on the host. `install` pushes tracked → live; `sync`/`capture`
  pull live → tracked; `compare` reports the difference.
- **User sections.** A region you mark in a tracked *source* file as
  **host-local** (per-machine, never shared) or **shared** (travels in the
  config repo). The markers are the source-side declaration only — they never
  survive into the deployed file: host-local bodies deploy *markerless* and are
  preserved across re-installs, and shared-section drift is reconciled by a
  stored-base 3-way merge (the reconcile wizard).

For the precise schema and every field, see **[configuration.md](configuration.md)**.

---

## Part A — Guided walkthrough

One continuous story: stand up a config repo, deploy it, drift it, and roll it
back. Each step shows the real command and what you'll see.

### 1. Install the engine

> **PyPI is coming soon.** A `v*.*.*` tag push will publish setforge to PyPI
> (`uv tool install setforge`); until then, install from source.

```console
$ git clone https://github.com/raulfrk/setforge && cd setforge
$ uv sync
$ uv run setforge --version
0.3.0
```

`uv sync` installs the package into the project venv, so `uv run setforge`
reports the real version. (Examples below write `setforge` for brevity; prefix
with `uv run` when running from a source checkout.)

### 2. `init` — bootstrap the host

`setforge init` creates the host-local config dir + `local.yaml`, reports
environment health, and (optionally) wires up your config repo as the source.

```console
$ setforge init
```

```
=== setforge init ===

checking environment...
  ✓ uv binary on PATH
  ✓ claude binary on PATH
  ⚠ code binary not on PATH
        impact: VSCode extension install/management DISABLED at runtime.
        fix: install VSCode + 'code' CLI / set binaries.code in local.yaml

checking config directories...
  ✗ ~/.config/setforge does not exist
  ✗ ~/.config/setforge/local.yaml does not exist

=== capabilities ===
  ✓ tracked-file deploy + sync
  ✓ claude_plugins reconcile
  ✗ vscode_extensions reconcile        DISABLED (code binary missing)

configure your config-repo source?
  ▸ skip (default)   — configure later (edit local.yaml's source: block)
    git URL          — clone a remote config repo now
    local path       — point to a local config-repo directory now

=== init complete ===
  next: edit local.yaml source: block, then setforge install --profile=<name> --dry-run
```

*(interactive prompt rendered from `setforge/cli/init.py` /
`setforge/cli/_init_helpers.py`)*

**Wiring a remote config repo.** Recording the source and cloning it are two
steps, in order:

```console
$ setforge init --git-source=https://github.com/you/dotfiles --git-ref=main
$ setforge fetch
```

`init --git-source` writes the `source:` block into `local.yaml`; `fetch` then
clones/updates it and checks out the pinned ref. Use `--config-repo` instead to
**scaffold a brand-new** config repo (a starter `setforge.yaml` + `tracked/`).

**When to use:** once per host, before your first `install`.

### 3. Author `setforge.yaml` + `tracked/`

A minimal config repo is a manifest plus the file content it points at:

```text
~/projects/dotfiles/
├── setforge.yaml
└── tracked/
    ├── gitconfig
    └── notes.md
```

<!-- setforge-doc-example: tutorial-schema6 -->
```yaml
# ~/projects/dotfiles/setforge.yaml
schema_version: "6.5"
tracked_files:
  gitconfig:
    src: gitconfig            # lives at tracked/gitconfig
    dst: ~/.config/sample/gitconfig
  notes:
    src: notes.md
    dst: ~/.config/sample/notes.md
packages:
  rg:
    type: cargo
    crate: ripgrep
bundles:
  command_line:
    components:
      - id: search
        package: rg
profiles:
  default:
    tracked_files: [gitconfig, notes]
    bundles: [command_line]
    reconcile:
      plugins: {policy: additive}
      extensions: {exclude: [], policy: additive}
```

`src` resolves under `tracked/`; `dst` is where the file deploys (it expands
`~`). Confirm the manifest is well-formed before deploying:

```console
$ setforge validate --all
ok
```

**When to use:** whenever you add or change what setforge manages.

### 4. `install` — deploy tracked → live

Dry-run first to see exactly what would happen — nothing is written:

```console
$ setforge install --profile=default --dry-run
```

```
=== DRY-RUN MODE — NOTHING WILL BE MUTATED ===
=== resolving profile + host overlay ===
profile default
  tracked_files:  2
  extensions:     0 declared (0 excluded)
  plugins:        0
  mcp_servers:    0
=== would-be drift gate ===
unexpected drift in 0 file(s)
=== would-be secrets gate ===
  scanned 2 file(s); 0 finding(s) require a decision
=== would-be deploy ===
  WOULD install   ~/.config/sample/gitconfig
  WOULD install   ~/.config/sample/notes.md
=== would-be host-local section inject ===
  no host-local sections to inject
=== would-be plugin reconcile ===
  nothing declared
=== would-be extension reconcile ===
  nothing declared
=== would-be MCP server reconcile ===
  nothing to reconcile
=== would-be package provision ===
  nothing to provision
=== would-be transition record ===
  WOULD record  ~/.local/state/setforge/transitions/20260615T083757Z-install-default
=== rerun without --dry-run to apply for real ===
```

Then apply. When setforge needs to confirm a mutation (for example an `--auto`
reconcile), it prints the change plus its risks and the revert command, then
asks:

```
Proceed with the mutation above?
  ▸ No  — abort, no mutations
    Yes — apply the changes
```

*(confirm prompt rendered from `setforge/cli/_confirm.py`)*

Before writing, setforge runs a **pre-deploy secrets scan** over your tracked
content. If it finds something, you decide per-finding:

```
⚠ POTENTIAL SECRET DETECTED
  rule:     aws-access-key
  file:     ~/projects/dotfiles/tracked/gitconfig:7
  snippet:  AKIA…EXAMPLE

How would you like to proceed?
  ▸ Abort install — review and remove the secret
    Proceed (allowlist this snippet hash; persisted host-local)
    Proceed (silence one-shot — do NOT add to allowlist)
```

*(secrets prompt rendered from `setforge/cli/_secrets_confirm.py`)*

Non-interactively (`--yes`), the deploy just runs:

```console
$ setforge install --profile=default --yes
 created  ~/.config/sample/gitconfig
 created  ~/.config/sample/notes.md
plugins: nothing to reconcile
mcp servers: nothing to reconcile
transition: ~/.local/state/setforge/transitions/20260615T083640Z-install-default
↩  revert with: setforge revert --profile=default
```

Every install records a **transition** so it can be reverted. Check the result:

```console
$ setforge status --profile=default
```

```
=== setforge status — default on myhost ===
config-repo:    ~/projects/dotfiles @ (no HEAD)
last install:   2s ago (transition 20260615T083640Z-install-default)
drift:          0 drifted
overlay:        (no overlays in local.yaml)
capabilities:   ✓ tracked-file deploy + sync  ✓ claude_plugins reconcile  ✗ vscode_extensions reconcile (code binary missing)
=== ready: run install if any drift surfaces or after fetch ===
```

**When to use:** to push your config onto a host — first setup, after a `fetch`,
or any time tracked content changes.

### 5. `compare` — see drift

Edit a live file by hand (say you tweak `~/.config/sample/gitconfig` directly),
then ask setforge what diverged:

```console
$ setforge compare --profile=default
```

```
                Drift Summary
┏━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━┓
┃ File      ┃ Disposition ┃ Class      ┃ Why ┃
┡━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━┩
│ gitconfig │             │ unexpected │     │
└───────────┴─────────────┴────────────┴─────┘
UNCHANGED: 1 files
```

`--full-diff` appends the actual hunks:

```console
$ setforge compare --profile=default --full-diff
```

```
--- ~/.config/sample/gitconfig
+++ ~/projects/dotfiles/tracked/gitconfig
@@ -3,6 +3,3 @@
     email = you@example.com
 [init]
     defaultBranch = main
-
-[core]
-    editor = vim
```

The **Class** column tells you what kind of drift each file has — `unexpected`
(live changed off-book), `stale` (tracked changed, live not yet updated),
`conflicted` (both sides moved). `compare --check` exits non-zero on
*unexpected* drift — a clean CI gate; add `--strict` to also fail on stale or
expected drift.

**When to use:** before install/sync, in CI, or any time you want to know
whether live and tracked agree.

### 6. `sync` — capture live → tracked

`compare` showed a live edit you want to **keep**. `sync` pulls live changes
back into your tracked files (and reconciles extensions), the inverse of
`install`:

```console
$ setforge sync --profile=default --auto=use-live --yes
```

`sync` records its own transition, so it too is revertable. (`capture` is the
narrower form — tracked files only, no extension reconcile.) Commit the updated
`tracked/` in your config repo afterward.

For a Git-backed configuration, SetForge also records which checkout owns each
tracked destination. A pre-existing file is adopted only after confirmation
(`--yes` in automation), without replacing its bytes. `stage` then decides
publication per hunk or structured key: SHARED units may flow back to tracked,
while LOCAL and PENDING units remain host-only. The container claim and those
unit choices are separate—a SHARED classification cannot claim a file, and
adopting a mixed file does not make its LOCAL hunks portable. `capture` and
`compare` fail closed when a staged Git-backed file has no current container
claim.

**When to use:** when the host is the source of truth for a change and you want
it back in the repo.

### 7. `revert` — undo the last transition

Made a mistake? Roll back the most recent install/sync:

```console
$ setforge revert --profile=default
```

Interactively, setforge shows the full plan — what it will reverse, the risks,
and how to redo — before touching anything:

```
=== resolving most-recent transition ===
transition: 20260615T083640Z-install-default
  type:    install
  files affected (2):
    M  ~/.config/sample/gitconfig
    M  ~/.config/sample/notes.md

=== what 'revert' will do ===
  Reverse the 2 file mutation(s) using stored patch-reverse data.

=== RISKS ===
  - revert uses patch-reverse, not whole-file overwrite, and refuses cleanly
    if any reverse-hunk collides with a live edit.

=== REDO (after revert lands) ===
  setforge revert acts as an inverse op. To REDO this install — run:
  setforge revert --profile=default
  again. Second invocation re-applies the original mutations.

setforge revert (install)
  ▸ no, abort (default — safe)
    yes, revert
    yes + open editor before applying
```

*(revert wizard rendered from `setforge/cli/_revert_confirm.py`)*

`--to-before=<id>` reverts a named transition **and every newer one**; `--yes`
skips the prompt for CI. A revert records its own reverse transition, so running
it again acts as a redo.

**When to use:** to undo a deploy or sync that went wrong.

---

## Part B — Command reference

The main commands, grouped by purpose. Lifecycle commands and the two wizards
get full mockups; routine CRUD subcommands get a compact example. Flags and the
closed-world top-level inventory live in **[commands.md](commands.md)**.

<a id="lifecycle-commands"></a>
### Lifecycle: install · compare · sync · capture · revert · status · validate

These are covered in depth in [Part A](#part-a--guided-walkthrough); this is the
quick index.

- **`install --profile=P`** — deploy tracked → live. Key flags: `--dry-run`,
  `--yes`, `--auto={use-tracked,keep-live}`, `--reconcile-user-sections`,
  `--no-secrets-scan`, `--retry-failed`. *When:* first setup,
  after `fetch`, or after tracked content changes. → [walkthrough](#4-install--deploy-tracked--live)
- **`compare --profile=P`** — report drift (the Drift Summary table). Key flags:
  `--full-diff`, `--check` (non-zero exit on drift), `--strict`. *When:* before
  install/sync, or as a CI gate. → [walkthrough](#5-compare--see-drift)
- **`sync --profile=P`** — capture live → tracked (files + extensions). `--auto`,
  `--yes`. *When:* push host-side changes back to the repo. → [walkthrough](#6-sync--capture-live--tracked)
- **`capture --profile=P`** — narrower `sync`: tracked files only, no extension
  reconcile. `--auto={use-live,keep-tracked}`. *When:* you want only file
  content captured.

  ```console
  $ setforge capture --profile=default --auto=use-live
  ```

- **`revert --profile=P`** — undo the most recent transition (or, with
  `--to-before=<id>`, that transition and every newer one). `--yes`. *When:* a
  deploy/sync went wrong. → [walkthrough](#7-revert--undo-the-last-transition)
- **`status --profile=P`** — one-screen health summary (config-repo HEAD, last
  install, drift counts, capabilities). Read-only. *When:* a quick "where do I
  stand". → [example output](#4-install--deploy-tracked--live)
- **`validate`** — config-shape validation only; no filesystem comparison.
  Exactly one of `--profile=P` or `--all` is required. *When:* after editing
  `setforge.yaml`, in CI.

  ```console
  $ setforge validate --all
  ok
  ```

<a id="config-repo-commands"></a>
### Config repo and locks: init · fetch · lock · migrate · upgrade

- **`init`** — bootstrap host dirs + `local.yaml`, report env health, optionally
  wire the source. Flags: `--config-repo` (scaffold a new repo), `--git-source`
  + `--git-ref`, `--path-source`, `--check`, `--force`, `--no-prompt`. *When:*
  once per host. → [walkthrough](#2-init--bootstrap-the-host)
- **`fetch`** — clone/update the configured git source and check out its pinned
  ref. *When:* after `init --git-source`, or to pull the latest config.

  ```console
  $ setforge fetch
  ```

- **`lock --profile=P`** — resolve exact package pins into the shared,
  committed `setforge.lock`; `--update=<key>` refreshes one pin. Cargo locks
  carry an exact version and crates.io sparse-index checksum. Install rechecks
  that exact row before skipping or invoking `cargo install` to mutate;
  mismatch or an unavailable index is HARD. The read-only `cargo install
  --list` inventory probe may already have run. `install --locked` requires
  complete lock coverage but is not a Cargo offline mode. *When:* after changing
  a lockable package declaration.

  GitHub release packages may keep the legacy universal `asset: name` form or
  declare an `assets:` list under schema 6.3. A variant can select `os`, `arch`,
  both, or neither. Exact OS+architecture wins over OS-only, then arch-only,
  then universal; an equal-rank tie or no match fails before download. Lock v2
  records every variant and checksum rather than the host that ran `lock`, so
  the committed lock stays portable across Linux/macOS and x86_64/aarch64.

  ```console
  $ setforge lock --profile=default
  $ setforge install --profile=default --locked
  ```

- **`migrate`** — run schema migrations against the active `setforge.yaml`.
  `--check` previews, `--apply` writes, `--pin`/`--to` target a version,
  `--finalize` strips vestigial host-local user-section markers from tracked
  sources (gated on a `minimum_version` floor). *When:* after a `schema_version`
  bump, or on upgrade.

  ```console
  $ setforge migrate --check
  ```
  ```
  === schema migration check ===
  your setforge.yaml:  ~/projects/dotfiles/setforge.yaml
    declared schema:   6.3
  installed setforge expects schema:   6.3
  === no migrations available ===
  ```

- **`upgrade`** — check PyPI for a newer setforge, show release notes + schema
  impact, and upgrade the `uv` tool wrapper. `--check`, `--to`, `--prerelease`,
  `--no-prompt`. *When:* to move to a new engine release.

  ```
  setforge upgrade 0.2.2 → 0.3.0
  release notes: ## [0.3.0] …
  === schema impact ===
  ⚠ SCHEMA CHANGE: after upgrade, run `setforge migrate --check`
  setforge upgrade
    ▸ Abort — no changes
      Upgrade
      Upgrade + run `setforge migrate --check`
  ```
  *(upgrade prompt rendered from `setforge/cli/upgrade.py`)*

<a id="cleanup-orphans"></a>
### cleanup and cleanup-orphans

`setforge cleanup --profile=default` reviews undeclared provisioned packages.
Current typed receipts and live package-manager inventories are eligible only
when they match this checkout's durable ownership claim. Legacy receipts and
drifted or foreign claims are shown as unowned and cannot be deleted. A package
that already exists on the first `install` is adopted only after confirmation
(`--yes` in automation); that adoption records metadata and does not reinstall
the package. When its evidence is one unambiguous legacy receipt, adoption also
migrates that receipt to the provider-qualified format in the same reversible
metadata transaction. Later upgrades and cleanup are allowed only through the
same current claim. Cleanup is separate from filesystem orphan handling.

Without `--scan`, this is the legacy, transition-history-attributed mode: it
finds live files attributed to removed `tracked_files` entries. It is a dry-run
unless `--apply` is present. The apply wizard can delete only or delete and
write a reversible transition; `--apply --yes` chooses the reversible branch.
`--ignore=<tracked-id>` records a host-local exclusion and returns without
scanning.

```console
$ setforge cleanup-orphans --profile=default
$ setforge cleanup-orphans --profile=default --apply --yes
```

Explicit `--scan` instead searches for unrecorded leaves beneath bounded roots
derived from directory and individual-file destinations across every effective
profile:

```console
$ setforge cleanup-orphans --profile=default --scan          # dry-run
$ setforge cleanup-orphans --profile=default --scan --apply  # TTY only
```

Scan mode never starts at generic roots such as `$HOME`, `~/.config`, or `/`,
and excludes the config repo, tracked sources, host-local files, ignored and
transition-attributed paths, and SetForge control/state trees. It never follows
symlinks and does not descend into a directory whose filesystem device differs
from the managed root's device. That is a device-boundary check, not general
mount detection: a same-device bind mount is not distinguishable by this check.
Only regular files and symlinks are candidates; directories and unsupported
leaf types are retained. A managed root reached through a
symlinked/non-directory parent is refused rather than traversed.

Applying a scan asks separately for every file, defaults each answer to keep,
and rejects both blanket `--yes` and `--ignore`. Under the mutation lock,
SetForge reloads all effective profiles and rescans: approved paths that are no
longer identical candidates are retained, newly appearing candidates are not
added, and only the surviving contraction is removed. Parent directories are
never pruned.

The reversible branch records typed before/after images, preserving arbitrary
file bytes or a symlink target plus mode and nanosecond mtime for undo/redo. An
interrupted cleanup is recovered from its write-ahead journal. If recovery
finds a replacement at a deleted path or a changed/symlinked parent, it keeps
that user data, retains the recovery record, and blocks conflicting mutations;
move the replacement aside, retry `setforge recover --profile=default --apply
--yes`.

*When:* use legacy mode after removing a tracked entry; opt into `--scan` only
to review otherwise unrecorded leaves inside already managed trees.

<a id="user-sections--the-reconcile-wizard"></a>
### User sections (host-local vs shared) + the reconcile wizard

A **user section** is a region you mark in a tracked *source* markdown file with
HTML-comment markers. The marker pair is the **authoring syntax** — it is *not*
what ends up in the deployed file. A section is either **host-local**
(per-machine, never shared) or **shared** (travels in the config repo). This lets
one tracked file carry both shared and per-machine content.

**The deployed file is markerless.** `install` strips the tracked-authored
markers so they never survive in the live file, and reconciles each section by
its semantics:

- **host-local** → the per-host body is injected *markerless* into the live file
  and preserved across re-installs (nothing host-specific in the config repo, no
  markers in the live file).
- **shared** → the region is reconciled as a stored-base 3-way merge; tracked-side
  updates reconcile against live edits via the wizard.

Legacy configs that relied on marker *survival* are migrated forward by
`setforge migrate` (and on `install`).

Declare a section by hand-authoring the marker pair in the tracked source (there
is no `section` subcommand). The `host-local` / `shared` keyword is required on
both markers:

```markdown
<!-- setforge:user-section start host-local mymachine -->
... per-machine body ...
<!-- setforge:user-section end host-local mymachine -->
```

Leave the end marker's `hash=<sha256-hex>` segment off (or drop in any
placeholder) — `setforge install` computes and rewrites the real body hash on
every run, so you never calculate it yourself. Name the section (optional) to
key it stably; unnamed sections are keyed by position. Sections cannot nest.

When a **shared** section has drifted between live and tracked, `install`
(with `--reconcile-user-sections`) opens the reconcile wizard — one full-screen
prompt per conflicting region. Each region shows the two sides framed as a
git-style diff, with a **navigable button bar** below it (arrow keys move the
focus, Enter picks):

```
┌─ ~/.claude/notes.md — region 1 of 1 ────────────────────┐
│ <<<<<<< OURS (this host)                                │
│   retries: 5                                            │
│ =======                                                 │
│   retries: 3                                            │
│ >>>>>>> THEIRS (upstream)                               │
│                                                         │
│   [ Ours ]  Theirs   Edit   Claude-merge   Skip         │
└─────────────────────────────────────────────────────────┘
```

The options:

- **Ours** — keep the live side of the region.
- **Theirs** — take the tracked/upstream side.
- **Edit** — open `$EDITOR` seeded with the git-style markers to hand-merge
  (shown only when both sides are valid UTF-8).
- **Claude-merge** — hand the region to Claude for a merge (shown only when
  all three sides are valid UTF-8).
- **Skip** — keep live for this region and re-surface it on the next install
  (the file is not re-baselined).

A worked example of the host-local vs shared model is in
**[configuration.md](configuration.md)**.

<a id="plugins-marketplaces-extensions"></a>
### Plugins, marketplaces, extensions

setforge can also reconcile **Claude plugins** (+ their **marketplaces**) and
**VSCode extensions** declared in `setforge.yaml`. Each `list` shows declared
(YAML) vs installed (queried from the `claude` / `code` CLI).

**Claude plugins:**

```console
$ setforge plugin list --profile=default                # declared vs installed
$ setforge plugin add myplugin@mymarket --from github:owner/repo --profile=default   # register + install
$ setforge plugin remove myplugin --profile=default     # drop from the profile
$ setforge plugin reconcile --profile=default           # apply declared state
$ setforge plugin sync-cache --profile=default          # clone/refresh marketplace caches
```

**Marketplaces:**

```console
$ setforge marketplace add mymarket --from github:owner/repo
$ setforge marketplace remove mymarket
$ setforge marketplace update mymarket       # claude plugin marketplace update (per-marketplace)
```

**Codex plugins:** declare sources and plugins beneath the top-level `codex`
block and select them from the profile's `codex.plugins` list:

```yaml
schema_version: '6.5'
minimum_version: '6.5'
codex:
  marketplaces:
    team:
      source: github
      repo: example/codex-plugins
  plugins:
    review:
      marketplace: team
profiles:
  default:
    codex:
      plugins: [review]
      reconcile: {policy: additive}
```

Use the same commands with an explicit product:

```console
$ setforge plugin list --product codex --profile default
$ setforge plugin reconcile --product codex --profile default --dry-run
$ setforge plugin add review@team --product codex --from github:example/codex-plugins --profile default
$ setforge marketplace update team --product codex
```

SetForge invokes the native `codex plugin` JSON interface and never writes
Codex's opaque plugin state. Authentication remains host-local. Current Codex
CLI releases do not expose plugin enable/disable commands; `--disable` with
`--product codex` therefore fails explicitly. Removal and reconciliation are
supported, and install/revert records Codex effects separately from Claude.

**Codex MCP servers:** declare STDIO or HTTP servers in the same top-level
`codex` registry and select them from `profiles.<name>.codex.mcp_servers`:

```yaml
codex:
  mcp_servers:
    notes:
      transport: stdio
      command: uvx
      args: [notes-mcp]
      env_vars: [notes_token]
      enabled_tools: [search, read]
      disabled_tools: [delete]
      tool_timeout_sec: 30
    team_api:
      transport: http
      url: https://mcp.example.com/api
      bearer_token_env_var: team_token
      env_http_headers: {X-Tenant: tenant_id}
      default_tools_approval_mode: writes
profiles:
  default:
    codex:
      mcp_servers: [notes, team_api]
```

The portable names are mapped to real host variable names in
`~/.config/setforge/local.yaml`; values never enter SetForge configuration or
state:

```yaml
codex:
  environment_vars:
    notes_token: NOTES_TOKEN
    team_token: TEAM_MCP_TOKEN
    tenant_id: TEAM_TENANT_ID
```

SetForge reconciles the corresponding native `config.toml` leaves, preserving
unmanaged servers, comments, OAuth login state, and credential material.
Project-scoped servers additionally use `scope: project`, a portable
`project: app` locator, and `codex.project_paths.app` in `local.yaml`; the
project must already be trusted by Codex.

**VSCode extensions:**

```console
$ setforge ext list --profile=default
$ setforge ext add ms-python.python --profile=default
$ setforge ext remove ms-python.python --profile=default
$ setforge ext reconcile --profile=default
```

*When:* to keep your Claude plugin set and VSCode extension set declarative and
in sync across hosts. (Plugin reconcile needs the `claude` CLI; extension
reconcile needs `code` on PATH.)

<a id="snapshots"></a>
### Snapshots

A snapshot is a directory-copy of a profile's live state you can restore later —
a coarse, whole-tree safety net distinct from per-transition revert. Restore is
profile-scoped: SetForge refuses snapshots from another profile or destinations
that the requested effective profile no longer manages. Destination parents
must be real directories rather than symlinks, preventing restore or recovery
from being redirected outside the journaled path tree. A restore is journaled,
so a partial failure rolls file type, bytes, mode, and mtime back automatically;
an interrupted process reserves its profile, source repository, and captured
host-local config namespaces until it is finished with `setforge recover`.

```console
$ setforge snapshot create before-experiment --profile=default
$ setforge snapshot list
$ setforge snapshot restore before-experiment --profile=default   # overlay onto live (additive)
```

*When:* before a risky manual change you want a guaranteed way back from.

<a id="profiles-transitions-config"></a>
### Profiles, transitions, config

**Profiles** — inspect what a profile resolves to:

```console
$ setforge profile list
```
```
=== profiles defined in ~/projects/dotfiles/setforge.yaml ===
┏━━━━━━━━━┳━━━━━━━━━┓
┃ name    ┃ extends ┃
┡━━━━━━━━━╇━━━━━━━━━┩
│ default │         │
└─────────┴─────────┘
```
```console
$ setforge profile show default        # fully-resolved profile + provenance
```
```
=== profile default ===
tracked_files (2 effective):
gitconfig  [from profile default]
notes      [from profile default]
packages (0 effective):
  (none)
```

**Transitions** — the install/sync/revert audit log:

```console
$ setforge transitions list
```
```
=== transitions (all profiles) ===
id                                       type     age      files  plugins  ext
20260615T083640Z-install-default         install  <1m ago      2        0    0
=== to view details ===
  setforge transitions show 20260615T083640Z-install-default
```
```console
$ setforge transitions show <id>        # full audit-detail panel
```

**Config** — granular CRUD over `setforge.yaml` / `local.yaml`:

```console
$ setforge config show --effective --profile=default   # resolved view
$ setforge config show --tracked tracked_files          # a dotted-path slice
$ setforge config add --tracked profiles.default.tracked_files notes --profile=default
$ setforge config remove --tracked profiles.default.tracked_files notes --profile=default
```
`config show` requires one of `--local` / `--tracked` / `--effective` (and
`--effective` requires `--profile`). *When:* scripted edits, or inspecting the
resolved config without opening the file.

<a id="completion--global-options"></a>
### Completion + global options

```console
$ setforge completion install zsh    # install shell completion (or bash / fish)
```

**Global options** (before the command) apply everywhere:

- `--source PATH` — override config-source discovery.
- `--code-bin` / `--claude-bin` / `--gitleaks-bin` / `--patch-bin` — override a
  tool binary path.
- `-v` / `-vv` — INFO / DEBUG logging (DEBUG redacts secrets).
- `-q` / `--quiet` — suppress success output on structured read-only commands;
  errors remain on stderr.
- `-o` / `--format [human|json]` — human (default) or a versioned JSON envelope
  on those commands.
- `--version` — print the version and exit.

The structured read-only set is `compare`, `status`, `inspect`, `profile show`,
`transitions list`, `stage --list`, and `config show --effective`. Other
commands reject `--quiet` and `--format=json` before doing work, and the two
non-default modes cannot be combined.

---

*Flags here are summarized. For the complete option set of any command, see
**[commands.md](commands.md)**; for the `setforge.yaml` schema, see
**[configuration.md](configuration.md)**.*
