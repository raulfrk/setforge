# dotfiles

Personal config for Claude Code + VSCode, managed with [dotdrop](https://github.com/deadc0de6/dotdrop) (config files) and a thin [Makefile](Makefile) wrapper (extensions + orchestration).

## Prerequisites

- `git`
- [`uv`](https://github.com/astral-sh/uv) (provides `uvx`, used to run `dotdrop` without installing it)
- `make`
- `code` on PATH if you want VSCode extension capture/restore (auto-injected inside a VSCode terminal, including Remote-SSH sessions)

## Install on a new machine

```bash
git clone https://github.com/raulfrk/dotfiles ~/dotfiles && cd ~/dotfiles && make install PROFILE=<profile>
```

`make install` deploys tracked dotfiles to their live destinations and reinstalls VSCode extensions from `vscode-extensions/<profile>.txt` (skipped automatically if `code` is unavailable).

## Profiles

| Profile | Includes | Used on |
|---|---|---|
| `shared-base` | `CLAUDE.md` | (inherited by both below) |
| `vm-headless` | shared-base + VSCode Machine settings | Remote-SSH VMs |
| `workstation` | shared-base (+ User settings, once assigned) | desktop |

## Daily workflow

All targets require `PROFILE=<name>`.

```bash
make compare PROFILE=vm-headless     # show drift between live and tracked/
make sync    PROFILE=vm-headless     # capture extensions + pull live edits into tracked/
make install PROFILE=vm-headless     # deploy tracked/ -> live + reinstall extensions
make help                            # list all targets
```

`make sync` (alias for `make update`) is the "I tweaked something live, now save it" command. After it, `git diff` to review and `git commit` to lock in.

## Add a new tracked dotfile

dotdrop's bookkeeping is still the canonical way to register new entries:

```bash
uvx dotdrop --cfg ~/dotfiles/config.yaml import -p <profile> <live-path>
```

Then edit `config.yaml` if you want to override the auto-generated key, `src` path, or profile assignment.

## Add VSCode extensions

Extensions are not dotfiles — they're captured as a name list per profile under `vscode-extensions/`. The Makefile handles capture and restore:

```bash
make sync    PROFILE=<profile>       # captures whatever is currently installed
make install PROFILE=<profile>       # reinstalls everything in the list
```
