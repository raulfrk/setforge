# setforge

Tracked-file + VSCode-extension + Claude-plugin orchestration CLI for personal config (dotfiles + extensions + Claude plugins). Single Python CLI (`setforge`) driven by a `setforge.yaml` declarative config that lives in a SEPARATE config repo (you bring your own).

## Stack

The Claude Code workflow setforge is built around relies on four tools:

| Tool | Role | Configured by setforge |
|---|---|---|
| Beads | Task tracking | Yes (deploys `~/.claude/beads/` and `bd` skill into Claude config) |
| Superpowers | Development methodology | Yes (deploys `superpowers-prefs.md`) |
| Repomix | Repo packaging | No — install separately |
| worktrunk | Worktree management for parallel agents | No — install separately |

`setforge install` doesn't install these tools themselves; install them yourself.

## Prerequisites

- `git`
- [`uv`](https://github.com/astral-sh/uv)
- `code` on PATH if you want VSCode extension reconcile (auto-injected inside a VSCode terminal, including Remote-SSH sessions). Optional.
- `claude` CLI on PATH if you want Claude plugin reconcile. Optional.

## Architecture: engine + config repos

setforge is a TOOL; the config it deploys is YOUR data. Post-setforge-2ba.4, the two live in separate repos:

- **Engine repo (this one)**: ships the `setforge` CLI + the source-discovery layer + git-management subsystem. No user-specific config.
- **Config repo (your repo)**: holds `setforge.yaml` + `tracked/<paths>` for the dotfiles you want managed. The author's personal config repo is `raulfrk/setforge-config` (private).

setforge discovers your config repo via a 4-layer precedence (first non-empty wins):

1. CLI flag: `--source PATH` (paths only).
2. Env var: `SETFORGE_SOURCE=PATH` (paths only).
3. Host-local config file `~/.config/setforge/local.yaml` `source:` block (path OR git).
4. Fallback: CWD if it contains `setforge.yaml`.

## Install on a new machine

Two pieces: install the engine, then point it at your config.

### 1. Install the engine

```bash
git clone https://github.com/raulfrk/my-setup ~/setforge && cd ~/setforge
uv sync --extra dev
```

(The engine repo's GitHub name remains `raulfrk/my-setup` until/unless the user renames it; the engine itself is `setforge`.)

### 2. Configure a source

Either clone a config repo manually and point setforge at it:

```bash
git clone git@github.com:raulfrk/setforge-config.git ~/setforge-config

mkdir -p ~/.config/setforge
cat > ~/.config/setforge/local.yaml <<'EOF'
source:
  kind: path
  path: ~/setforge-config
EOF
```

Or let setforge clone + manage a git source:

```bash
mkdir -p ~/.config/setforge
cat > ~/.config/setforge/local.yaml <<'EOF'
source:
  kind: git
  url: git@github.com:raulfrk/setforge-config.git
  ref: main
EOF

uv run setforge fetch
```

### 3. Install

```bash
uv run setforge install --profile=<profile>
```

`setforge install` deploys tracked files to their live destinations and reconciles VSCode extensions and Claude plugins.

## Development setup

The repo's [.pre-commit-config.yaml](.pre-commit-config.yaml) declares hooks (gitleaks, ruff, ruff-format) that only fire after `pre-commit install` registers the git hook in `.git/hooks/`. Run this once per fresh clone or worktree — otherwise commits sail past local quality gates and only fail on CI:

```bash
uv run pre-commit install
```

## Daily workflow

All commands require `--profile=<name>`. Profiles live in YOUR config repo's `setforge.yaml`.

```bash
uv run setforge fetch                              # clone/fetch + checkout the configured git source
uv run setforge compare --profile=<profile>       # show drift between live and tracked/
uv run setforge sync    --profile=<profile>       # capture live edits into tracked/
uv run setforge install --profile=<profile>       # deploy tracked/ -> live
uv run setforge revert  --profile=<profile>       # undo the most recent install/sync
uv run setforge validate --profile=<profile>     # config-shape check (no live target paths needed)
uv run setforge --help                            # list all commands
```

`sync` is the alias for `capture` — "I tweaked something live, now save it." Setforge writes the captured content into your config repo's `<source-dir>/tracked/`; `git diff` + commit + push from inside the config repo to lock in.

When tracked declares `preserve_user_keys_deep` or carries top-level non-preserve drift between tracked and live, `sync` fires the merge wizard interactively (symmetric with `install`'s drift gate). For non-interactive contexts (CI, scripted runs):

- `--auto=use-live` reproduces today's silent-absorb behavior — every drift item is absorbed into tracked.
- `--auto=keep-tracked` is the safer alternative — every drift item is rejected, tracked stays as-is.
- Without TTY and without `--auto`, `sync` exits 1 with `CaptureRequiresInteractive`.

`revert` undoes the most recent `install` or `sync` for the named profile by replaying its transition record in reverse — file diffs via `patch -R`, plus uninstalling extensions that were installed (and reinstalling extensions that were uninstalled). Drift on any touched file aborts cleanly with no partial revert. A second `revert` acts as redo. Transition records are written to `~/.local/state/setforge/transitions/` and kept indefinitely; if that directory grows large, you can `rm -rf` it.

## User-section preservation

Markdown tracked files can opt into per-host preservation. Wrap any region in HTML-comment markers and the live content survives subsequent `install` runs. Markers require a `host-local` or `shared` semantics keyword on both start and end:

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

YAML tracked files can declare `preserve_user_keys: list[str]` per tracked file in your config repo's `setforge.yaml`. Live values at those JSONPath-lite paths overlay tracked content on every deploy and are stripped from tracked on every capture.

## Host-local files

Edit `~/.claude/additional-content.md` directly on each host for machine-specific Claude Code rules. `setforge install` creates it as an empty file if missing; the engine never tracks its content.

## Add a new tracked file or extension

Both happen in YOUR config repo, not in this engine repo:

1. Edit `<config-repo>/setforge.yaml` to add an entry under `tracked_files:` and reference it from the relevant profile's `tracked_files:` list. (Extensions: add the extension ID to the profile's `extensions.include:` list.)
2. Place tracked-file source files under `<config-repo>/tracked/<src>` (matching each entry's `src:` path).
3. Commit + push to your config repo.
4. On every machine: `uv run setforge fetch` (for git sources) or `git pull` (for path sources), then `uv run setforge install --profile=<profile>`.

## CI

Push/PR to `main` runs [.github/workflows/ci.yml](.github/workflows/ci.yml): unit tests (`uv run pytest`), config validation against the e2e test fixture (`uv run setforge validate --config=tests/fixtures/e2e/setforge.test.yaml --all`), and gitleaks.

The engine repo no longer carries a `setforge.yaml` at root (it lives in your config repo post-setforge-2ba.4); CI validates against the e2e test fixture instead.

## Upgrading from my-setup v0.x to setforge

setforge is the post-rename + post-split form of the older `my-setup` tool. If you have an existing my-setup checkout, the migration is:

1. **Rename**: the Python package, CLI binary, env vars, XDG dirs, and bd issue prefix all changed (my_setup → setforge, MY_SETUP_ → SETFORGE_, ~/.local/state/my-setup/ → ~/.local/state/setforge/, etc.). Migrate XDG state:

   ```bash
   mv ~/.config/my-setup ~/.config/setforge      # if it exists
   mv ~/.local/state/my-setup ~/.local/state/setforge   # if it exists
   ```

2. **User-section markers in deployed live files**: the marker namespace changed from `my-setup:user-section` to `setforge:user-section`. Run this on every host, per markered live file:

   ```bash
   sed -i 's/my-setup:user-section/setforge:user-section/g' ~/.claude/CLAUDE.md
   # repeat for any other live file you've installed with markers
   ```

   `setforge install` detects pre-rename markers and refuses to clobber section bodies, pointing you at this sed command — but running it preemptively is safer.

3. **Repo split**: your old monorepo had engine + config together. Post-2ba.4, separate them:

   - **Option A** (clean): clone the new engine repo afresh, then create / clone a config repo containing your `my_setup.yaml` + `tracked/` (you can use `git filter-repo --path tracked/ --path my_setup.yaml` to extract them from your old monorepo with full history).
   - **Option B** (migrated): if you're `raulfrk` (the author), your config now lives at `git@github.com:raulfrk/setforge-config.git`.

4. **Rename your config file**: setforge now expects `setforge.yaml` (was `my_setup.yaml`). In your config repo:

   ```bash
   git mv my_setup.yaml setforge.yaml
   git commit -m "Rename my_setup.yaml to setforge.yaml"
   ```

   If you forget, `setforge` refuses to run with a `ConfigError` pointing at this exact command.

5. **Configure the source layer** so setforge finds your config repo (see "Architecture: engine + config repos" above).

6. **Run**: `setforge install --profile=<your-profile>` should be a no-op on a host that was already on the latest my-setup state.
