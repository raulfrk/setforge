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

Two top-level keys are required: `tracked_files` and `profiles`. `setforge
validate` rejects unknown top-level keys; the normal load path (install / sync)
instead warns and strips them, so a config from a newer same-major engine still
loads. Everything else has a default:

| Key | Required | Default | Purpose |
|---|---|---|---|
| `tracked_files` | yes | — | Map of stable id → tracked-file definition. |
| `profiles` | yes | — | Map of profile name → profile definition. |
| `version` | no | `1` | Engine-owned config format; only integer `1` is supported. Other explicit values refuse cleanly with upgrade guidance. |
| `schema_version` | no | `"1.0"` | Migration schema version; author new configs as `"6.5"`. |
| `minimum_version` | no | — | Lowest schema-aware engine the operator permits. |
| `marketplaces` | no | `{}` | Claude plugin marketplaces. |
| `claude_plugins` | no | `{}` | Top-level Claude plugin defaults. |
| `codex` | no | omitted | Typed Codex resources; requires `schema_version` and `minimum_version` 6.4 or newer. |
| `mcp_servers` | no | `{}` | Named Claude MCP server commands. |
| `section_templates` | no | `{}` | Reusable host-local section bodies. |
| `packages` | no | `{}` | Named package declarations. |
| `bundles` | no | `{}` | Ordered, dependency-aware package/file groups. |
| `project_profiles` | no | `{}` | Portable files that SetForge can later apply inside project repositories. |

## Project profiles

`project_profiles` describes portable, project-relative files independently of
the host profiles above. SetForge can resolve and validate these declarations,
then inject them reversibly into an existing project directory:

```console
setforge project inject application /path/to/worktree --dry-run
setforge project inject application /path/to/worktree --yes
setforge project sync /path/to/worktree --dry-run
setforge project remove application /path/to/worktree --yes
```

Injection creates missing files, retains byte-and-mode-identical untracked
files, and snapshots differing untracked files so `remove` can restore their
exact bytes and mode. In a Git worktree, an already-tracked destination opens
the standard per-hunk resolver; accepted profile hunks remain in the live file
but a required private Git filter keeps only those hunks out of `git diff`.
Unrelated edits in the same file remain visible. Non-interactive tracked-file
resolution requires `--auto=keep-live` or `--auto=use-profile`. Both
commands support `--dry-run`; live non-interactive use requires `--yes`.

After profile sources or membership change, `project sync <path>` reconciles
every injection recorded for the exact target atomically—even when those
profiles came from different config repositories. Existing members use the
stored upstream ancestor, current project bytes, and current profile bytes for
a three-way merge. Independent project edits survive automatically; overlapping
regions use the same Ours/Theirs/Edit/Claude-merge/Skip wizard as ordinary
reconciliation. `--auto=keep-live` and `--auto=use-profile` are the explicit
non-interactive alternatives.
An executable-mode conflict also fails closed unless one of those explicit
automatic policies chooses the live or profile mode.
When one side of a conflict is an absent file, the wizard records whether Ours
or Theirs was selected so deletion remains distinct from choosing an
intentionally empty file.

Older injection records did not retain ancestor bytes. Their first sync exposes
each differing line hunk as a conflict instead of guessing its author. A
successful resolution upgrades only that record to the current private schema
and establishes the profile bytes as the ancestor for later true three-way
syncs. Dry runs, unresolved conflicts, cancellation, and faults do not migrate
records or partially update another profile at the target.

New profile members inherit their injection's visibility. A new member whose
destination already contains differing local bytes or mode is a conflict, not
an implicit replacement. Removed members
reconcile toward the exact pre-injection state and release only their ownership
and visibility claims. Existing members keep their visibility throughout sync;
hidden files therefore remain absent from normal `git status`.

SetForge applies either hidden or tracked visibility (`--git-hidden` or
`--git-tracked`) while injecting. Hidden files receive exact, root-anchored
claims in the repository's private `.git/info/exclude`; they do not appear in
normal `git status`, and no hide metadata is committed. Tracked files remain
ordinary untracked Git content until you stage them—SetForge never stages them.
Removal releases only that injection's private claims and preserves both user
exclude text and claims still used by sibling linked worktrees.

Linked worktrees share the repository's `info/exclude`, `info/attributes`, and
local filter configuration. Compatible hidden
claims for the same relative path coexist, but hidden and tracked intent for the
same path cannot differ between siblings; SetForge refuses that conflict before
mutation. Tracked-file attribute claims are reference-counted, and the generic
filter passes content through unchanged when the current worktree/path has no
active overlay. In a non-Git target, injection, sync, and removal still work;
Git visibility is reported as not applicable and SetForge does not initialize
Git. Project metadata is stored in SetForge's private state directory, not in
the target directory.

Injection, synchronization, and the future optional worktree auto-carry hook have independent
lifecycles: `project remove` never changes that hook, and disabling the hook
will preserve existing injections.

```yaml
project_profiles:
  base:
    default_visibility: tracked
    files:
      instructions:
        src: AGENTS.md
        dst: AGENTS.md
      guide:
        src: guide.md
        dst: docs/guide.md
  application:
    extends: base
    files:
      instructions:
        src: AGENTS.md
        dst: ./AGENTS.md
```

Each source lives under `project/<declaring-profile>/` in the config repo. In
the example, `base.guide` resolves from `project/base/guide.md`, while the
replacement `application.instructions` resolves from
`project/application/AGENTS.md`. Inherited files retain their original source
profile.

Destinations are normalized relative paths. Empty paths, absolute paths,
parent traversal, and anything below `.git/` are rejected. Two files in one
profile cannot normalize to the same destination. During inheritance, a child
file targeting the same normalized destination replaces its parent in place;
child-only destinations append in declaration order.

`default_visibility` is `hidden` or `tracked`. It inherits from the parent and
defaults to `hidden` only after the full chain is resolved. `setforge validate`
also requires every resolved source to be a regular file confined to its
declaring profile's source directory, including after resolving symlinks.

<a id="codex-profile"></a>
## Complete mixed Codex profile

Codex resources require `schema_version: '6.4'` or newer. Codex MCP servers
require 6.5. This example keeps Claude instructions and a complete
Codex selection in one profile:

```yaml
schema_version: '6.5'
minimum_version: '6.5'
version: 1
tracked_files:
  claude-instructions:
    src: claude/CLAUDE.md
    dst: ~/.claude/CLAUDE.md
codex:
  config:
    defaults: {source: codex/config.toml}
  instructions:
    shared: {source: codex/AGENTS.md}
  skills:
    review: {source: codex/skills/review}
  marketplaces:
    team: {source: github, repo: example/codex-plugins}
  plugins:
    reviewer: {marketplace: team}
  mcp_servers:
    notes:
      transport: stdio
      command: uvx
      args: [notes-mcp]
      env_vars: [notes_token]
profiles:
  workstation:
    tracked_files: [claude-instructions]
    codex:
      config: [defaults]
      instructions: [shared]
      skills: [review]
      plugins: [reviewer]
      mcp_servers: [notes]
      reconcile: {policy: additive}
```

Map portable environment names to real host variables in
`~/.config/setforge/local.yaml`; never put values in the shared repository:

```yaml
codex:
  environment_vars:
    notes_token: NOTES_TOKEN
```

Setforge owns only selected TOML leaves and declared filesystem resources.
Unmanaged TOML keys, comments, OAuth state, credential material, and unrelated
instructions or skills are preserved.

The current schema keeps deployable registries at the top level. Profiles
select `tracked_files`, `packages`, `bundles`, `mcp_servers`, and typed `codex`
resources, then configure reconciliation behavior. The pre-6.0 profile fields `extensions`,
`claude_plugins`, `cargo_binaries`, and `plugins_reconcile` are migration input,
not schema-6 authoring syntax. The historical `5.0 -> 6.0` migration folds
those selections into package declarations before removing the old fields.

Codex MCP declarations live under `codex.mcp_servers`, separately from the
legacy Claude registry. They use `transport: stdio` with `command`, optional
`args`, `cwd`, and `env_vars`, or `transport: http` with `url`, optional
`bearer_token_env_var`, and `env_http_headers`. Both transports support
`scope`/`project`, `enabled`, `required`, startup/tool timeouts, enabled and
disabled tool lists, a default approval mode, and per-tool approval modes.
Environment fields contain portable logical names only. Map each selected name
to a host environment-variable name under `codex.environment_vars` in
`local.yaml`; OAuth sessions and all credential values remain host-owned.
The MCP scope/project contract requires `schema_version` and
`minimum_version` 6.5 or newer.

### Complete schema-6 shape

This compact example exercises every top-level registry and every schema-6
profile selection field. Registry entries may be split or expanded to suit a
real config.

<!-- setforge-doc-example: configuration-full-schema6 -->
```yaml
schema_version: "6.2"
minimum_version: "6.2"
tracked_files:
  shell:
    src: shell/zshrc
    dst: ~/.zshrc
marketplaces:
  team:
    source: github
    repo: example/claude-plugins
claude_plugins:
  review:
    marketplace: team
mcp_servers:
  notes:
    command: [uvx, notes-mcp]
    scope: user
section_templates:
  workstation_notes:
    src: workstation-notes.md
packages:
  rg:
    type: cargo
    crate: ripgrep
  formatter:
    type: python
    package: ruff
  review_plugin:
    type: plugin
    plugin: review
  python_extension:
    type: extension
    extension: ms-python.python
bundles:
  developer:
    components:
      - id: search
        package: rg
      - id: formatting
        package: formatter
        depends_on: [search]
profiles:
  default:
    tracked_files: [shell]
    packages: [review_plugin, python_extension]
    bundles: [developer]
    mcp_servers: [notes]
    reconcile:
      plugins:
        policy: additive
      extensions:
        exclude: [vendor.unwanted]
        policy: prune
    section_slots:
      workstation: workstation_notes
```

### Tracked files

A `tracked_files` entry requires only `src` and `dst`:

```yaml
tracked_files:
  example:
    src: example.txt            # relative to <config-repo>/tracked/
    dst: ~/.config/example.txt  # live destination
```

Optional per-entry keys:

- `template` — render Jinja2 expressions in `dst` (it does not render content).
- `generated` — render the tracked source as one-way Jinja2 output from an
  explicit map of typed host inputs. Generated output cannot be staged or
  captured back into `tracked/`.
- `mode` — file mode, written as a **YAML-1.2 octal literal** (`0o755`, not
  `0755` or `755`). Omit to preserve the source file's mode.
- `symlink` — deploy as a symlink instead of copying.

`src` must exist on disk under `<config-repo>/tracked/` — `setforge validate`
checks this.

Generated resources keep portable intent in the repository while resolving
host facts only in the frozen install plan:

```yaml
schema_version: "6.2"
minimum_version: "6.2"
tracked_files:
  code-settings:
    src: code-settings.json.j2
    dst: ~/.config/Code/User/settings.json
    generated:
      inputs:
        home: home
        code_dir: vscode-user-dir
```

The source template reads those declared values as `{{ host.home }}` and
`{{ host.code_dir }}`. The closed resolver set is `home` and
`vscode-user-dir`; arbitrary environment variables, commands, secrets, and
network lookups are not available. Rendering uses strict undefined values, so
misspelled or undeclared input names fail before any install write. Templates
are deliberately expression-only: function and method calls, filters, and
tests are rejected so rendering stays deterministic and confined to declared
values.

Managed directory trees use schema 6.2 and remain one-way tracked-to-live
resources. The first install over an existing directory adopts its current
inventory without changing bytes; later installs manage entries under the
declared policy:

```yaml
schema_version: "6.2"
minimum_version: "6.2"
tracked_files:
  tool-home:
    src: tool-home
    dst: ~/.local/share/example
    tree:
      exclude: ["cache/**"]
      symlinks: refuse       # or preserve
      orphans: keep          # or remove-owned
```

Excludes use gitignore-style matching. `remove-owned` removes only entries
recorded in the previous owned inventory and only while their content remains
unchanged; unowned or drifted entries are preserved or held for review. Tree
roots cannot overlap another tracked destination. `capture` and `stage` refuse
managed trees; edit the tracked source tree instead.

Basenames matching `.NAME.setforge-create`, `.NAME.setforge-update`, or
`.NAME.setforge-remove` are reserved for journaled atomic publication and are
rejected in both tracked sources and live managed trees.

### Profiles

A profile selects tracked files, packages, bundles, and MCP servers. Every
profile field is optional (an empty profile is valid shape):

```yaml
profiles:
  default:
    tracked_files:
      - example
    packages:
      - python_extension
    bundles:
      - developer
    reconcile:
      extensions:
        exclude: [vendor.unwanted]
        policy: additive
    # extends: base      # optional — inherit from one parent profile (by name)
```

Inspect resolved profiles with `setforge profile list` / `setforge profile
show`.

### Package locks and Cargo verification

`setforge lock --profile=<profile>` resolves lockable package declarations and
writes the exact version and integrity value to the shared, committed
`setforge.lock`. For a Cargo package, the lock entry has this TOML shape:

```toml
version = 2

[[package]]
type = "cargo"
key = "ripgrep"
version = "14.1.1"
checksum = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
profiles = ["default"]
```

At install time, a pinned Cargo crate is considered present only when
`cargo install --list` reports the exact version from the registry (not a path
or git source) and the lock checksum equals the exact crate/version row fetched
from the crates.io sparse index. A malformed pin, unavailable index row, or
checksum mismatch is a **HARD** outcome and no mutating `cargo install`
invocation is attempted for that item. SetForge rechecks the index before
treating an exact installed crate as satisfied or invoking `cargo install` to
mutate, so a frozen plan cannot silently use a changed row. Inventory planning
may still run the read-only `cargo install --list` probe.

`cargo install --version <exact> --locked` then asks Cargo to use the crate
archive's packaged `Cargo.lock`; Cargo performs its own registry download and
archive checksum handling. SetForge does not download or hash the `.crate`
archive itself. `setforge install --locked` means every lockable declaration
must have a matching `setforge.lock` entry and disables re-resolution; it does
not make Cargo offline. Cargo pins still need the crates.io sparse-index check,
and a missing crate/tool download can still need the network. `--no-fetch`
only suppresses the config-repository git fetch.

### GitHub release assets by platform

The original `github_release` shape remains valid and means that one asset is
universal across every supported host:

```yaml
schema_version: "6.3"
minimum_version: "6.3"
packages:
  tool:
    type: github_release
    repo: example/tool
    tag: v1.2.3
    asset: tool
    checksum: sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    binary: tool
    install: ~/.local/bin
    extract: false
```

For releases with different files per platform, replace `asset` with `assets`:

```yaml
schema_version: "6.3"
minimum_version: "6.3"
packages:
  tool:
    type: github_release
    repo: example/tool
    tag: v1.2.3
    assets:
      - {asset: tool-linux-amd64, os: linux, arch: x86_64}
      - {asset: tool-linux-arm64, os: linux, arch: aarch64}
      - {asset: tool-macos, os: macos}
      - {asset: tool-portable}
    binary: tool
    install: ~/.local/bin
    extract: false
```

Selectors use canonical `linux` / `macos` and `x86_64` / `aarch64` values;
common aliases such as `darwin`, `amd64`, and `arm64` normalize to those
values. Selection precedence is exact OS+architecture, then OS-only, then
architecture-only, then the universal row. No match or more than one match at
the winning precedence is an error before download or mutation. `asset` and
`assets` are mutually exclusive, and each variant checksum is bound only to
that variant.

`setforge lock` writes every declared variant, its canonical selector, and its
checksum to lock format v2. The lock is portable: it contains no locking-host
field, so a lock committed on Linux can later select its declared macOS or ARM
artifact without contacting GitHub or silently resolving a different asset.
Legacy lock v1 files and scalar declarations remain readable as universal
assets.

## Per-host preservation

Some live state is host-specific and must survive a re-`install`. setforge
offers two mechanisms.

### Markdown: user-section markers

Wrap a region of a tracked markdown *source* file in HTML-comment markers to
**declare** it a user section. The marker pair is the authoring syntax in the
source; it is **not** what gets deployed (see below). Both
markers need a `host-local` or `shared` semantics keyword:

```markdown
<!-- setforge:user-section start host-local NAME -->
... per-machine body; kept live, never shared ...
<!-- setforge:user-section end host-local NAME -->

<!-- setforge:user-section start shared NAME -->
... shared body; tracked-side updates reconcile via
    `install --reconcile-user-sections` ...
<!-- setforge:user-section end shared NAME -->
```

**The deployed file is markerless.** `install` strips the tracked-authored
markers so they never reach the live file, and reconciles each section by its
semantics:

- **host-local** → the per-host body is injected *markerless* into the live file
  and preserved across re-installs (nothing host-specific reaches the config
  repo).
- **shared** → the region is reconciled as a stored-base 3-way merge against
  live edits; the reconcile wizard surfaces any conflict.

Configs predating the current schema (no `schema_version`, or an older one) that
relied on marker *survival* in the live file are migrated forward by
`setforge migrate` (and transparently on
`install`). The project-root [CLAUDE.md](../CLAUDE.md) documents the full marker
grammar. Marker pairs are **hand-authored** — there is no `section` subcommand;
open the tracked source and write the pair yourself. The `host-local` / `shared`
keyword is required on both markers and must match; the `NAME` is optional (and
must match between start and end when present). Leave off the end marker's
`hash=<sha256-hex>` segment — `install` computes and rewrites it on every run, so
you never calculate it by hand. See [commands.md](commands.md#managing-user-section-markers)
for the authoring walkthrough.

#### Seeding host-local bodies (optional)

For host-local sections you can seed an empty body from a reusable template
instead of typing it out on each machine. Register the body file under the
config's top-level `section_templates:` (a name → `src:` path relative to the
config repo's `templates/` directory), then map a host-local section NAME to it
in a profile's `section_slots:`. On `install`, an empty or missing host-local
section named there is seeded **once** from the template body; a section that
already has content is left untouched (the host owns it), so later template
edits do not propagate to a host that has already adopted the section. This is a
convenience layer on top of hand-authored markers — the marker pair is still
what declares the section.

## Host-local, never-tracked files

Some files are intentionally per-host and never tracked. List such a path in a
profile's `bootstrap:` block and `setforge install` creates it as an empty stub
if missing (e.g. `~/.claude/additional-content.md`) — the engine never tracks
its content.

## Adding a tracked file or extension package

All of this happens in **your** config repo, not the engine repo:

1. Add an entry under `tracked_files:` in `setforge.yaml` and reference its id
   from the relevant profile's `tracked_files:` list. For an extension, add a
   top-level `packages:` entry with `type: extension` and `extension: <id>`,
   then reference that package key from the profile's `packages:` list.
2. Place the source under `<config-repo>/tracked/<src>`, matching the entry's
   `src:` path.
3. Commit and push your config repo.
4. On each machine: `setforge fetch` (git sources) or `git pull` (path
   sources), then `setforge install --profile=<profile>`.
