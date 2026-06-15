# Configuration

setforge is a tool; the config it deploys is your data. The two live in
separate repos:

- **Engine repo** (`raulfrk/setforge`): ships the `setforge` CLI, the
  source-discovery layer, and the git-management subsystem. No user-specific
  config.
- **Config repo** (yours): holds `setforge.yaml` plus a `tracked/` tree of the
  files you want managed.

This page covers how setforge finds your config repo, the shape of
`setforge.yaml`, and how per-host preservation works.

## Source discovery

setforge locates your config repo via a 4-layer precedence — the first
non-empty layer wins:

1. **CLI flag** `--source PATH` (paths only).
2. **Env var** `SETFORGE_SOURCE=PATH` (paths only).
3. **Host-local config** `~/.config/setforge/local.yaml`, `source:` block
   (path **or** git).
4. **Fallback**: the current directory, if it contains a `setforge.yaml`.

A per-command `--config PATH` set explicitly **overrides the whole source
layer** — discovery only fires when `--config` is left at its default and the
CWD has no `setforge.yaml`. Use `setforge init` to write the `local.yaml`
`source:` block rather than authoring it by hand (see the
[Quickstart](../README.md#quickstart)).

### `local.yaml` source blocks

Point setforge at a config repo already on disk:

```yaml
# ~/.config/setforge/local.yaml
source:
  kind: path
  path: ~/your-config
```

Or let setforge clone and manage a git source:

```yaml
# ~/.config/setforge/local.yaml
source:
  kind: git
  url: git@github.com:you/your-config.git
  ref: main
```

For a git source, `ref` defaults to `main`; `name` and `clone_dest` are
optional (`clone_dest` defaults to `~/.local/share/setforge/sources/<name>`).
Run `setforge fetch` to clone-if-missing, fetch, and check out the ref; a dirty
`tracked/` aborts the checkout with an actionable error.

## `setforge.yaml`

Two top-level keys are required: `tracked_files` and `profiles`. Unknown
top-level keys are rejected. Everything else has a default:

| Key | Required | Default | Purpose |
|---|---|---|---|
| `tracked_files` | yes | — | Map of stable id → tracked-file definition. |
| `profiles` | yes | — | Map of profile name → profile definition. |
| `version` | no | `1` | Config format version. |
| `schema_version` | no | `"1.0"` | Migration schema version (`setforge migrate`). |
| `marketplaces` | no | `{}` | Claude plugin marketplaces. |
| `claude_plugins` | no | `{}` | Top-level Claude plugin defaults. |

### Tracked files

A `tracked_files` entry requires only `src` and `dst`:

```yaml
tracked_files:
  example:
    src: example.txt            # relative to <config-repo>/tracked/
    dst: ~/.config/example.txt  # live destination
```

Optional per-entry keys:

- `template` — render the source through Jinja2 before deploying.
- `mode` — file mode, written as a **YAML-1.2 octal literal** (`0o755`, not
  `0755` or `755`). Omit to preserve the source file's mode.
- `symlink` — deploy as a symlink instead of copying.
- `disposition` — file-level reconciliation policy (`shared` / `forked` /
  `pinned`): opt into the stored-base 3-way merge instead of a verbatim deploy
  (see below).
- `spans` — sub-file pinned/forked regions (the schema-2.0 model that
  superseded the legacy `preserve_*` family). See below.

`src` must exist on disk under `<config-repo>/tracked/` — `setforge validate`
checks this.

### Profiles

A profile selects which tracked files, extensions, and plugins to deploy. Every
profile field is optional (an empty profile is valid shape):

```yaml
profiles:
  default:
    tracked_files:
      - example
    extensions:
      include:
        - ms-python.python
    extends: []          # inherit from another profile
```

Inspect resolved profiles with `setforge profile list` / `setforge profile
show`.

## Per-host preservation

Some live state is host-specific and must survive a re-`install`. setforge
offers two mechanisms.

### Markdown: user-section markers

Wrap a region of a tracked markdown *source* file in HTML-comment markers to
**declare** it a user section. The marker pair is the authoring syntax in the
source — as of schema 2.0 it is **not** what gets deployed (see below). Both
markers need a `host-local` or `shared` semantics keyword:

```markdown
<!-- setforge:user-section start host-local NAME -->
... per-machine body; stored in local.yaml, never shared ...
<!-- setforge:user-section end host-local NAME -->

<!-- setforge:user-section start shared NAME -->
... shared body; tracked-side updates reconcile via
    `install --reconcile-user-sections` ...
<!-- setforge:user-section end shared NAME -->
```

**The deployed file is markerless.** Under the schema-2.0 unified-span contract,
`install` strips the tracked-authored markers and resolves each section through
the span model rather than letting markers survive in the live file:

- **host-local** → the per-host body is injected *markerless* into the live file
  and kept as an OVERLAY span in `local.yaml` (nothing host-specific reaches the
  config repo).
- **shared** → the region becomes a stored-base 3-way merge (`disposition:
  shared`); the end marker's `hash=<sha256-hex>` segment lets the reconciler
  distinguish pending-tracked drift from live edits.

Legacy `version: 1` configs that relied on marker *survival* in the live file
are migrated to this span model by `setforge migrate` (and transparently on
`install`). The project-root [CLAUDE.md](../CLAUDE.md) documents the full marker
grammar; adding marker pairs is automated by `setforge section` — see
[commands.md](commands.md).

### YAML / JSON: preserved keys → spans

Schema 2.0 replaced the per-key `preserve_user_keys` / `preserve_user_keys_deep`
flags with the unified **span** model. To keep host-local values out of the
config repo while still deploying a tracked file, give the file
`disposition: forked` and declare PINNED `spans` at the structural paths you
want preserved (a deep span pins a whole subtree). The `setforge override` CLI
(`fork` / `pin` / `list` / `show`) manages these; legacy
`preserve_user_keys[_deep]` configs are auto-translated to PINNED spans by
`setforge migrate`.

## Host-local, never-tracked files

`~/.claude/additional-content.md` is intentionally per-host. `setforge install`
creates it as an empty file if missing; the engine never tracks its content.

## Adding a tracked file or extension

All of this happens in **your** config repo, not the engine repo:

1. Add an entry under `tracked_files:` in `setforge.yaml` and reference its id
   from the relevant profile's `tracked_files:` list. (Extensions: add the
   extension id to the profile's `extensions.include:` list.)
2. Place the source under `<config-repo>/tracked/<src>`, matching the entry's
   `src:` path.
3. Commit and push your config repo.
4. On each machine: `setforge fetch` (git sources) or `git pull` (path
   sources), then `setforge install --profile=<profile>`.
