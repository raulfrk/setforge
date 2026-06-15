# setforge

[![CI](https://github.com/raulfrk/setforge/actions/workflows/ci.yml/badge.svg)](https://github.com/raulfrk/setforge/actions/workflows/ci.yml)

One CLI to deploy your dotfiles, VSCode extensions, and Claude Code plugins
from a declarative config repo you own — idempotent, drift-aware, and
revertible.

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

## How it works

- **Engine repo** (`raulfrk/setforge`, this one): the `setforge` CLI plus the
  source-discovery and git-management layers. No user config.
- **Config repo** (yours): a `setforge.yaml` declaring `tracked_files`,
  `profiles`, extensions, and plugins, alongside a `tracked/` tree holding the
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

```yaml
# your-config/setforge.yaml
schema_version: "2.0"
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
(templates, file modes, extensions, plugins, per-host preservation).

(Existing configs written with the legacy `version: 1` key still load and are
migrated forward by `setforge migrate` — `schema_version: "2.0"` is the current
shape for new repos.)

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

This deploys your tracked files to their live destinations and reconciles
VSCode extensions and Claude plugins.

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

- **Lifecycle:** install · compare · sync · capture · revert · status · validate
- **Config repo:** init · fetch · migrate · upgrade
- **Cleanup:** cleanup-orphans
- **Subcommand groups:** override · plugin · marketplace · ext · section ·
  snapshot · profile · transitions · config · completion

New to setforge, or want to see what each command's output looks like? The
**[guided tutorial](docs/tutorial.md)** walks the whole lifecycle and documents
every command with worked examples and terminal mockups. Exhaustive flags live
in [docs/commands.md](docs/commands.md).

## Concepts & deep reference

- **Guided tutorial** — the full lifecycle plus every command with examples and
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
