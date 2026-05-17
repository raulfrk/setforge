# Global preferences for Claude Code on this VM

## Environment

- Debian 12 VM, headless. Accessed only via Remote-SSH from a separate workstation.
- Shell: zsh. No local desktop VSCode here; `~/.vscode-server/` is the only VSCode footprint.
- Treat GUI workflows as happening on the workstation, not on this VM.

## Hard rails (also apply in auto mode)

- Never `git push --force` or `--force-with-lease` to `main`, `master`, or `release/*` without explicit confirmation in the current session.
- Never modify `/etc/`, `/usr/`, `/opt/`, or other system paths without confirmation.
- Never `rm -rf` outside the active project's working tree.
- Never read, copy, or modify `~/.ssh/`, `~/.gnupg/`, `~/.aws/`.
- System-package installs (`apt`, `dpkg -i`, `snap install`) require confirmation. User-scope installs (`pip install --user`, `npm i` inside a project, `uv add`) are fine.

## Tool preferences (defaults when alternatives exist)

- Code search: `rg`, not `grep`. File discovery: `fd`, not `find`.
- Python: `uv` for venv / install / run. Avoid raw `pip`, `poetry`, `virtualenv`.
- GitHub: `gh` for any API interaction. Don't hand-roll `curl` against api.github.com.
- JSON: `jq` for inspection and transforms.
