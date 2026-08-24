# setforge

[![CI](https://github.com/raulfrk/setforge/actions/workflows/ci.yml/badge.svg)](https://github.com/raulfrk/setforge/actions/workflows/ci.yml)

One CLI to deploy your dotfiles and provision packages, VSCode extensions, and
Claude Code plugins from a declarative config repo you own — idempotent,
drift-aware, and revertible.

## What is setforge?

setforge keeps a machine's personal config in sync with a single source of
truth. You describe what should be on a host — tracked files (dotfiles,
rules, hooks), VSCode extensions, and Claude plugins — in a `setforge.yaml`
that lives in **your own config repo**. `setforge install` makes the machine
match it; `setforge compare` shows what drifted; `setforge revert` undoes the
last change.

The tool (this repo) and your config are deliberately separate: the engine
ships no personal data, and your config repo carries no engine code. That
split is what lets one published tool drive many different people's setups.

## Codex parity and compatibility

Setforge supports mixed Claude and Codex profiles. Codex configuration,
layered `AGENTS.md` instructions, standalone skills, plugins and marketplaces,
and STDIO/HTTP MCP servers use the same profile lifecycle and transition model
as existing resources.

| Surface | Claude | Codex | Runtime requirement |
| --- | --- | --- | --- |
| Configuration and instructions | native JSON/Markdown | native TOML/`AGENTS.md` | filesystem access |
| Standalone skills | managed directories | managed directories | filesystem access |
| Plugins and marketplaces | `claude` CLI | `codex` CLI | non-interactive JSON commands described below |
| MCP servers | Claude registry | native `config.toml` | host-local environment values when needed |
| File lifecycle and snapshots | supported | supported | Setforge schema 6.4+; Codex MCP needs 6.5 |
| Plugin install/compare/revert | supported | supported | compatible native CLI |

Codex CLI compatibility is capability-based rather than tied to a guessed
version number. Plugin automation requires `plugin list --available --json`,
`plugin add/remove --json`, and `plugin marketplace
list/add/remove/upgrade --json`. When those commands are unavailable, file and
MCP management still works and compare/status reports actionable plugin-state
drift. Credential values, OAuth state, and bearer tokens remain host-local.

See [configuration](docs/configuration.md#codex-profile),
[commands](docs/commands.md#codex-lifecycle), and the
[tutorial](docs/tutorial.md#codex-migration-and-limitations) for a complete
profile, migration steps, and limitations.

## How it works

- **Engine repo** (`raulfrk/setforge`, this one): the `setforge` CLI plus the
  source-discovery and git-management layers. No user config.
- **Config repo** (yours): a `setforge.yaml` declaring `tracked_files`,
  top-level registries, and profiles, alongside a `tracked/` tree holding the
  source files.

setforge finds your config repo through a 4-layer precedence — first match
wins: `--source` flag → `SETFORGE_SOURCE` env → `~/.config/setforge/local.yaml`
→ a `setforge.yaml` in the current directory. The full precedence rules and
`local.yaml` shapes live in [docs/configuration.md](docs/configuration.md).

## Quickstart

> **PyPI is coming soon.** A `v*.*.*` tag push publishes setforge to PyPI
> (`uv tool install setforge`), but the package isn't there yet — install from
> source for now.

**1. Prerequisites**

- [`uv`](https://github.com/astral-sh/uv) and `git`.
- Optional: `code` on PATH for VSCode extension reconcile (auto-injected inside
  a VSCode terminal, including Remote-SSH); `claude` on PATH for Claude plugin
  reconcile.

**2. Install the engine from source**

```bash
git clone https://github.com/raulfrk/setforge ~/setforge && cd ~/setforge
uv sync --extra dev
```

Run it with `uv run setforge …` from the repo (`uv sync` installs the package,
so `setforge --version` reports the real version). After `uv sync`, bare
`setforge` and `uv run setforge` are interchangeable; examples below use both.

**3. Create a minimal config repo**

setforge needs a config repo of your own. The smallest one that works:

```
your-config/
├── setforge.yaml
└── tracked/
    └── example.txt
```

<!-- setforge-doc-example: readme-minimal-schema6 -->
```yaml
# your-config/setforge.yaml
schema_version: "6.2"
tracked_files:
  example:
    src: example.txt            # lives at tracked/example.txt
    dst: ~/.config/example.txt  # where it deploys on the host
profiles:
  default:
    tracked_files:
      - example
```

Put any content in `tracked/example.txt`, then `git init` the directory.
See [docs/configuration.md](docs/configuration.md) for the full schema
(packages, bundles, plugins, MCP servers, templates, and per-host
preservation).

(Configs without a `schema_version` — or with an older one — still load and are
migrated forward to the current `6.2` by `setforge migrate`. The unrelated
engine-owned `version:` file-format field defaults to `1` and you don't set it.)

**4. Wire setforge to your config**

```bash
setforge init --path-source=~/your-config
# or, for a git-hosted config repo (record the source, then clone it):
# setforge init --git-source=git@github.com:you/your-config.git --git-ref=main
# setforge fetch
```

`setforge init` writes `~/.config/setforge/local.yaml` with the `source:` block
for you — no hand-editing. For a git source, `init --git-source` records it and
`setforge fetch` then clones/updates it and checks out the pinned ref.

**5. Deploy**

```bash
uv run setforge install --profile=default
```

This deploys tracked files and reconciles the profile's packages, bundles,
VSCode extensions, Claude plugins, and MCP servers.

## Daily workflow

Core commands (all deploy/compare/sync commands require `--profile=<name>`):

```bash
uv run setforge compare  --profile=<profile>   # show drift between live and tracked/
uv run setforge sync     --profile=<profile>   # capture live edits into tracked/ + record a transition
uv run setforge install  --profile=<profile>   # deploy tracked/ -> live
uv run setforge revert   --profile=<profile>   # undo the most recent install/sync
uv run setforge status   --profile=<profile>   # one-screen status summary (read-only)
uv run setforge validate --profile=<profile>   # config-shape check (no live target paths)
```

`validate` takes exactly one of `--profile=<name>` or `--all`. For the full
command surface, run `setforge --help` or see
[docs/commands.md](docs/commands.md).

## Command overview

Beyond the daily commands above, setforge's full surface groups as:

- **Lifecycle:** install · compare · capture · sync · revert · status · validate
- **Config and packages:** init · fetch · migrate · upgrade · lock
- **Inspection and recovery:** stage · inspect · recover · transitions · ownership · profile
- **Cleanup:** cleanup (provisioning receipts) · cleanup-orphans (tracked-file
  transition history or an explicit bounded scan)
- **Subcommand groups:** ext · plugin · marketplace · transitions · ownership ·
  profile · snapshot · completion · config

New to setforge, or want to see what the main commands' output looks like? The
**[guided tutorial](docs/tutorial.md)** walks the lifecycle with worked examples
and terminal mockups. The complete inventory and flags live in
[docs/commands.md](docs/commands.md).

## Concepts & deep reference

- **Guided tutorial** — the full lifecycle and main commands with examples and
  output mockups: [docs/tutorial.md](docs/tutorial.md).
- **Configuration & the config repo** — source discovery, the `setforge.yaml`
  schema, per-host preservation: [docs/configuration.md](docs/configuration.md).
- **Command reference & subcommand groups** — every command, the nine
  subcommand groups, and `--auto=*` confirmation:
  [docs/commands.md](docs/commands.md).
- **Cutting a release** — CI gates and the tag-push flow:
  [docs/releasing.md](docs/releasing.md).
- **Upgrading from my-setup v0.x** — the rename + repo-split migration:
  [docs/migrating-from-my-setup.md](docs/migrating-from-my-setup.md).

## The four-tool stack

The Claude Code workflow setforge is built around relies on four tools:

| Tool | Role | Configured by setforge |
|---|---|---|
| Beads | Task tracking | Yes (deploys `bd` skill + config into Claude) |
| Superpowers | Development methodology | Yes (deploys `superpowers-prefs.md`) |
| Repomix | Repo packaging | No — install separately |
| worktrunk | Worktree management for parallel agents | No — install separately |

`setforge install` configures Beads and Superpowers; install Repomix and
worktrunk yourself.

## Development

Install the pre-commit hooks once per fresh clone or worktree — otherwise
commits sail past local gates (gitleaks, ruff, ruff-format) and only fail on
CI:

```bash
uv run pre-commit install
```

CI runs on every push/PR to `main`; see [docs/releasing.md](docs/releasing.md).
