# setforge

Personal config for Claude Code + VSCode, managed by a single Python CLI (`setforge`) driven by [my_setup.yaml](my_setup.yaml).

## Stack

The Claude Code workflow this repo configures relies on four tools:

| Tool | Role | Configured by this repo |
|---|---|---|
| Beads | Task tracking | Yes |
| Superpowers | Development methodology | Yes |
| Repomix | Repo packaging | No — install separately |
| worktrunk | Worktree management for parallel agents | No — install separately |

`setforge install` does not install these tools; install them yourself.

## Prerequisites

- `git`
- [`uv`](https://github.com/astral-sh/uv)
- `code` on PATH if you want VSCode extension reconcile (auto-injected inside a VSCode terminal, including Remote-SSH sessions). Optional.
- `claude` CLI on PATH if you want Claude plugin reconcile (P3). Optional.

## Install on a new machine

```bash
git clone https://github.com/raulfrk/setforge ~/setforge && cd ~/setforge
uv run setforge install --profile=<profile>
```

`setforge install` deploys tracked tracked files to their live destinations and (P2/P3) reconciles VSCode extensions and Claude plugins.

## Development setup

The repo's [.pre-commit-config.yaml](.pre-commit-config.yaml) declares hooks (gitleaks, ruff, ruff-format) that only fire after `pre-commit install` registers the git hook in `.git/hooks/`. Run this once per fresh clone or worktree — otherwise commits sail past local quality gates and only fail on CI:

```bash
uv run pre-commit install
```

## Profiles

| Profile | Includes | Use on |
|---|---|---|
| `shared-base` | Claude config + skills | inherited, not used directly |
| `vm-headless` | shared-base + VSCode Machine settings | Remote-SSH VM, minimal Claude context |
| `vm-headless-full` | vm-headless + `header.md` + `additional-content.md` stub | Remote-SSH VM, full Claude context |
| `vm-headless-vscode` | VSCode Machine settings only | hosts with VSCode but no Claude Code |
| `workstation` | shared-base + VSCode User settings (OS-detected path) | desktop (macOS or Linux) |

`vm-headless` is the daily-driver; `vm-headless-full` is the explicit form that includes the shared header content and the host-local stub.

## Daily workflow

All commands require `--profile=<name>`.

```bash
uv run setforge compare --profile=vm-headless     # show drift between live and tracked/
uv run setforge sync    --profile=vm-headless     # capture live edits into tracked/
uv run setforge install --profile=vm-headless     # deploy tracked/ -> live
uv run setforge revert  --profile=vm-headless     # undo the most recent install/sync
uv run setforge validate --profile=vm-headless    # config-shape check (no live target paths needed)
uv run setforge --help                             # list all commands
```

`sync` is the alias for `capture` — "I tweaked something live, now save it." After it, `git diff` to review and `git commit` to lock in.

When tracked declares `preserve_user_keys_deep` or carries top-level non-preserve drift between tracked and live, `sync` fires the merge wizard interactively (symmetric with `install`'s drift gate). Each diverged sub-key / top-level key surfaces a `[k]eep tracked / [u]se live / [s]ave-as-preserved / [m]anual edit` prompt; the wizard mutates tracked in place per choice. Tracked-only top-level keys are preserved through `sync` (behavior change vs pre-`nen.23`, where they were silently lost).

For non-interactive contexts (CI, scripted runs):

- `--auto=use-live` reproduces today's silent-absorb behavior — every drift item is absorbed into tracked.
- `--auto=keep-tracked` is the safer alternative — every drift item is rejected, tracked stays as-is.
- Without TTY and without `--auto`, `sync` exits 1 with `CaptureRequiresInteractive`. Migration: scripted runs of `setforge sync` need to add `--auto=use-live` (compatibility) or `--auto=keep-tracked` (stricter) once a profile starts declaring `preserve_user_keys_deep` or accumulating top-level non-preserve drift.

`revert` undoes the most recent `install` or `sync` for the named profile by replaying its transition record in reverse — file diffs via `patch -R`, plus uninstalling extensions that were installed (and reinstalling extensions that were uninstalled). Drift on any touched file aborts cleanly with no partial revert. A second `revert` acts as redo. Transition records are written to `~/.local/state/setforge/transitions/` and kept indefinitely; if that directory grows large, you can `rm -rf` it (a future bead, `setforge-nen`-tracked, will add automatic pruning).

## User-section preservation

Markdown tracked files can opt into per-host preservation. Wrap any region in HTML-comment markers and the live content survives subsequent `install` runs. Markers require a `host-local` or `shared` semantics keyword on both start and end (untagged markers raise `MarkerError`):

```markdown
<!-- setforge:user-section start host-local NAME -->
... live edits to this body always survive re-install (host-specific) ...
<!-- setforge:user-section end host-local NAME -->

<!-- setforge:user-section start shared NAME -->
... live edits survive, and tracked-side updates surface via
    `install --reconcile-user-sections` ...
<!-- setforge:user-section end shared NAME -->
```

See the project-root [CLAUDE.md](CLAUDE.md) marker-syntax section for the full grammar (including the `hash=<sha256-hex>` segment install rewrites on every run).

YAML tracked files can declare `preserve_user_keys: list[str]` per tracked file in `my_setup.yaml`. Live values at those JSONPath-lite paths overlay tracked content on every deploy and are stripped from tracked on every capture.

## Host-local files

Edit `~/.claude/additional-content.md` directly on each host for machine-specific Claude Code rules. `setforge install` creates it as an empty file if missing; the repo never tracks its content.

## Add a new tracked tracked file

1. Edit `my_setup.yaml` to add an entry under `tracked_files:` and reference it from the relevant profile's `tracked_files:` list.
2. Place the file under `tracked/<src>` (matching the entry's `src:` path).
3. Run `uv run setforge install --profile=<profile>` to deploy.

## Add VSCode extensions

Extensions are typed under each profile's `extensions.include:` list in `my_setup.yaml`. Pillar 2 will land an `ext` subcommand that edits the YAML in place.

## CI

Push/PR to `main` runs [.github/workflows/ci.yml](.github/workflows/ci.yml): unit tests (`uv run pytest`), config validation (`uv run setforge validate --all`), and gitleaks.
