# setforge tutorial — everything it does, and how

This is the guided tour of setforge: a narrative walkthrough of the full
lifecycle followed by a reference for **every** command, each with an example,
a realistic terminal mockup, and a note on when to reach for it.

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
  - [Config repo: init, fetch, migrate, upgrade](#config-repo-commands)
  - [cleanup-orphans](#cleanup-orphans)
  - [User sections (host-local vs shared) + the reconcile wizard](#user-sections--the-reconcile-wizard)
  - [Overrides: fork / pin + the conflict wizard](#overrides--the-conflict-wizard)
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
- **Profiles.** A profile is a named subset of tracked files / plugins /
  extensions, with optional inheritance (`extends:`). Every command that
  deploys or compares takes `--profile`.
- **Schema.** `setforge.yaml` carries `schema_version: "2.0"`. An optional
  `minimum_version:` floor refuses to run an engine older than your config
  needs. Older `version: 1` configs still load and are migrated forward by
  `setforge migrate`.
- **Live vs tracked.** *Tracked* is the content in your config repo. *Live* is
  what's deployed on the host. `install` pushes tracked → live; `sync`/`capture`
  pull live → tracked; `compare` reports the difference.
- **User sections.** A marked region inside a tracked file can be **host-local**
  (per-machine, kept in `local.yaml`, never shared) or **shared** (travels in
  the config repo). Drift in a shared section is resolved by the reconcile
  wizard at install time.

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
setforge 0.2.2
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

```yaml
# ~/projects/dotfiles/setforge.yaml
schema_version: "2.0"
tracked_files:
  gitconfig:
    src: gitconfig            # lives at tracked/gitconfig
    dst: ~/.config/sample/gitconfig
  notes:
    src: notes.md
    dst: ~/.config/sample/notes.md
profiles:
  default:
    tracked_files: [gitconfig, notes]
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
  claude_plugins: 0
=== would-be drift gate ===
unexpected drift in 0 file(s)
=== would-be deploy ===
  WOULD install   ~/.config/sample/gitconfig
  WOULD install   ~/.config/sample/notes.md
=== would-be transition record ===
  WOULD record  ~/.local/state/setforge/transitions/20260615T083757Z-install-default
=== rerun without --dry-run to apply for real ===
```

Then apply. Interactively, setforge confirms before mutating and shows the
revert command up front:

```
setforge install — profile=default
Proceed with the deploy above?
  ▸ abort — no changes
    proceed
    proceed (skip secrets scan)
    proceed + open editor
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
    Proceed (allowlist this finding; persisted host-local)
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
drift:          0 unexpected, 0 user-section drift, 0 expected (preserve_user_keys)
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
`conflicted` (both sides moved). `compare --check` exits non-zero when any drift
exists, which makes it a clean CI gate.

**When to use:** before install/sync, in CI, or any time you want to know
whether live and tracked agree.

### 6. `sync` — capture live → tracked

`compare` showed a live edit you want to **keep**. `sync` pulls live changes
back into your tracked files (and reconciles extensions), the inverse of
`install`:

```console
$ setforge sync --profile=default --yes
```

`sync` records its own transition, so it too is revertable. (`capture` is the
narrower form — tracked files only, no extension reconcile.) Commit the updated
`tracked/` in your config repo afterward.

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
  setforge install --profile=default      # re-applies the original deploy

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

Every command setforge ships, grouped by purpose. Lifecycle commands and the
two wizards get full mockups; routine CRUD subcommands get an example, their
one-line output, and a "when to use". Flags are summarized — see
**[commands.md](commands.md)** for the exhaustive list.

<a id="lifecycle-commands"></a>
### Lifecycle: install · compare · sync · capture · revert · status · validate

These are covered in depth in [Part A](#part-a--guided-walkthrough); this is the
quick index.

- **`install --profile=P`** — deploy tracked → live. Key flags: `--dry-run`,
  `--yes`, `--auto={use-tracked,keep-live}`, `--reconcile-user-sections`,
  `--no-secrets-scan`, `--strict-spans`, `--retry-failed`. *When:* first setup,
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
### Config repo: init · fetch · migrate · upgrade

- **`init`** — bootstrap host dirs + `local.yaml`, report env health, optionally
  wire the source. Flags: `--config-repo` (scaffold a new repo), `--git-source`
  + `--git-ref`, `--path-source`, `--check`, `--force`, `--no-prompt`. *When:*
  once per host. → [walkthrough](#2-init--bootstrap-the-host)
- **`fetch`** — clone/update the configured git source and check out its pinned
  ref. *When:* after `init --git-source`, or to pull the latest config.

  ```console
  $ setforge fetch
  ```

- **`migrate`** — run schema migrations against the active `setforge.yaml`.
  `--check` previews, `--apply` writes, `--pin`/`--to` target a version,
  `--finalize` strips migration markers. *When:* after a schema bump (e.g. a
  `version: 1` config), or on upgrade.

  ```console
  $ setforge migrate --check
  ```
  ```
  === schema migration check ===
  your setforge.yaml:  ~/projects/dotfiles/setforge.yaml
    declared schema:   2.0
  installed setforge expects schema:   2.0
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
### cleanup-orphans

Find and remove tracked-file **orphans** — live files left behind after their
entry was removed from a profile. Dry-run by default; `--apply` to act,
`--yes` to skip the prompt, `--ignore` to record an orphan as intentional.

```console
$ setforge cleanup-orphans --profile=default
```

*When:* after you delete a `tracked_files` entry and want the stale live file
cleaned up.

<a id="user-sections--the-reconcile-wizard"></a>
### User sections (host-local vs shared) + the reconcile wizard

A **user section** is a marker-delimited region inside a tracked file. It is
either **host-local** (intent lives in `local.yaml`, per-machine, never shared)
or **shared** (intent lives in the tracked `setforge.yaml` and travels across
hosts). This lets one tracked file carry both shared content and per-machine
content.

Emit a marker pair to paste into a tracked file, or insert it in place:

```console
$ setforge section emit host-local mymachine
<!-- setforge:user-section start host-local mymachine -->

<!-- setforge:user-section end host-local mymachine hash=01ba4719c80b6fe9… -->

$ setforge section add --profile=default --tracked-file notes \
      --semantics shared --name mysection --anchor-line 1
```

When a **shared** section has drifted between live and tracked, `install`
(with `--reconcile-user-sections`) opens the reconcile wizard — one prompt per
drifted section:

```
───────────────────────────────────────────────────────────
 section notes.md (shared) pending tracked update
───────────────────────────────────────────────────────────
--- live/notes.md
+++ tracked/notes.md
@@ -42,8 +42,9 @@
   timeout: 30
-  retries: 5
+  retries: 3

  [k] keep live          preserve the current live body
  [t] take tracked       overwrite live with the tracked body
  [e] edit               open $EDITOR with the live body as seed
  [s] skip               keep live, ask again next install
  [q] quit-keep-rest     keep live for this and all remaining

   Choice (k/t/e/s/q): _
```

*(reconcile wizard rendered from `setforge/section_wizard.py`)*

A worked example of the host-local vs shared model and `preserve_user_keys` is
in **[configuration.md](configuration.md)**.

<a id="overrides--the-conflict-wizard"></a>
### Overrides: fork / pin + the conflict wizard

An **override** changes a tracked file's *disposition* — how setforge reconciles
it:

- **fork** — three-way merge tracked changes into the live file (live edits are
  preserved; upstream changes still flow in). The file is never blindly
  overwritten.
- **pin** — live always wins; the file is never merged or clobbered.

```console
$ setforge override fork notes        # merge upstream, keep live edits
$ setforge override pin gitconfig     # freeze live, ignore tracked changes
$ setforge override list
$ setforge override show notes        # spans + annotations for one file
$ setforge override unfork notes      # drop a FORKED override
$ setforge override unpin gitconfig   # drop a PINNED override
$ setforge override reset             # clear ALL override state
```

When a forked file's merge hits a real conflict (both sides changed the same
place), install opens the conflict wizard — one prompt per conflict:

```
───────────────────────────────────────────────────────────
 line conflict
───────────────────────────────────────────────────────────
 ours (live):
   editor = vim
 theirs (tracked):
   editor = nano

  [k] keep ours (live)      preserve the live side
  [t] take theirs (tracked) overwrite with the tracked side
  [e] edit                  open $EDITOR seeded with ours
  [s] skip                  keep live, ask again next install

  Choice (k/t/e/s): _
```

*(conflict wizard rendered from `setforge/conflict_wizard.py`)*

*When:* `fork` a file you hand-edit per machine but still want upstream updates
for; `pin` a file you've fully taken over locally.

<a id="plugins-marketplaces-extensions"></a>
### Plugins, marketplaces, extensions

setforge can also reconcile **Claude plugins** (+ their **marketplaces**) and
**VSCode extensions** declared in `setforge.yaml`. Each `list` shows declared
(YAML) vs installed (queried from the `claude` / `code` CLI).

**Claude plugins:**

```console
$ setforge plugin list                                  # declared vs installed
$ setforge plugin add myplugin@mymarket --from github:owner/repo   # register + declare + install
$ setforge plugin remove myplugin                       # drop from the profile
$ setforge plugin reconcile                             # apply declared state
$ setforge plugin sync-cache                            # clone/refresh marketplace caches
```

**Marketplaces:**

```console
$ setforge marketplace add mymarket --from github:owner/repo
$ setforge marketplace remove mymarket
$ setforge marketplace update                # `claude plugin marketplace update`
```

**VSCode extensions:**

```console
$ setforge ext list
$ setforge ext add ms-python.python
$ setforge ext remove ms-python.python
$ setforge ext reconcile
```

*When:* to keep your Claude plugin set and VSCode extension set declarative and
in sync across hosts. (Plugin reconcile needs the `claude` CLI; extension
reconcile needs `code` on PATH.)

<a id="snapshots"></a>
### Snapshots

A snapshot is a directory-copy of a profile's live state you can restore later —
a coarse, whole-tree safety net distinct from per-transition revert.

```console
$ setforge snapshot create before-experiment --profile=default
$ setforge snapshot list
$ setforge snapshot restore before-experiment   # overlay a snapshot onto live (additive)
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
claude_plugins (0 effective):
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
$ setforge config add --tracked profiles.default.tracked_files notes
$ setforge config remove --tracked profiles.default.tracked_files notes
```
`config show` requires one of `--local` / `--tracked` / `--effective` (and
`--effective` requires `--profile`). *When:* scripted edits, or inspecting the
resolved config without opening the file.

<a id="completion--global-options"></a>
### Completion + global options

```console
$ setforge completion install        # install shell completion (zsh/bash/fish)
```

**Global options** (before the command) apply everywhere:

- `--source PATH` — override config-source discovery.
- `--code-bin` / `--claude-bin` / `--gitleaks-bin` / `--patch-bin` — override a
  tool binary path.
- `-v` / `-vv` — INFO / DEBUG logging (DEBUG redacts secrets).
- `-q` / `--quiet` — suppress non-error output (cron/CI).
- `-o` / `--format [human|json]` — human (default) or a versioned JSON envelope.
- `--version` — print the version and exit.

---

*Flags here are summarized. For the complete option set of any command, see
**[commands.md](commands.md)**; for the `setforge.yaml` schema, see
**[configuration.md](configuration.md)**.*
