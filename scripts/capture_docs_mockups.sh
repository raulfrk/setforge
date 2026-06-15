#!/usr/bin/env bash
# Capture real `setforge` terminal output for the docs tutorial mockups, in a
# fully isolated throwaway environment so NO real file is ever touched.
#
# Why this exists: `dst` paths expand the live $HOME (deploy.py uses
# Path.expanduser), and source discovery falls through to ~/.config/setforge/
# local.yaml and the CWD. So an un-isolated capture run would deploy tracked
# files over the operator's real ~/.config / ~/.claude. This wrapper pins three
# independent guards: a throwaway HOME, an explicit --source, and cleared
# SETFORGE_*/XDG_* env. Only PLAIN-STDOUT, read-safe surfaces are captured here;
# interactive prompt_toolkit TUIs are hand-authored from their renderers.
#
# Usage: scripts/capture_docs_mockups.sh
# Output: prints each captured surface delimited by `### <name>` markers.
set -euo pipefail

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
H="$TMP/home"                 # throwaway HOME — every ~ expansion lands here
SRC="$TMP/src"               # synthetic config source (pinned via --source)
mkdir -p "$H" "$SRC/tracked"

# Run setforge isolated: throwaway HOME, pinned --source, no inherited setforge
# env, deterministic width. PATH is kept so `uv` works (env -i would break it).
sf() {
  # NO_COLOR keeps box-drawing glyphs but drops SGR color; the sed strip
  # removes any residual ANSI so the captured text drops cleanly into markdown.
  env -u SETFORGE_SOURCE -u SETFORGE_CODE_BIN -u SETFORGE_CLAUDE_BIN \
    -u SETFORGE_GITLEAKS_BIN -u SETFORGE_PATCH_BIN -u SETFORGE_GITHUB_TOKEN \
    -u XDG_CONFIG_HOME -u XDG_DATA_HOME -u XDG_STATE_HOME \
    HOME="$H" COLUMNS=120 TERM=dumb NO_COLOR=1 \
    uv run setforge --source "$SRC" "$@" 2>&1 | sed -r 's/\x1b\[[0-9;:]*m//g'
}
mark() { printf '\n### %s\n' "$1"; }

# --- synthetic config: two tracked files, no plugins/extensions (so no claude/
# --- code binary is needed); a markdown file carries a shared user-section.
cat > "$SRC/setforge.yaml" <<'YAML'
schema_version: "2.0"
tracked_files:
  gitconfig:
    src: gitconfig
    dst: ~/.config/sample/gitconfig
  notes:
    src: notes.md
    dst: ~/.config/sample/notes.md
profiles:
  default:
    tracked_files: [gitconfig, notes]
YAML
cat > "$SRC/tracked/gitconfig" <<'CFG'
[user]
    name = Example User
    email = you@example.com
[init]
    defaultBranch = main
CFG
cat > "$SRC/tracked/notes.md" <<'MD'
# Notes

Shared, tracked content.
MD

# --- read-only surfaces (no state needed) ---------------------------------
mark "validate --all";            sf validate --all || true
mark "profile list";              sf profile list || true
mark "profile show default";      sf profile show default || true
mark "config show --effective";   sf config show --effective --profile=default || true
mark "migrate --check";           sf migrate --check || true
mark "status (pre-install)";      sf status --profile=default || true

# --- install (mutating, but only inside throwaway HOME) -------------------
mark "install --dry-run";         sf install --profile=default --dry-run --no-secrets-scan || true
mark "install (real, sandboxed)"; sf install --profile=default --yes --no-secrets-scan || true
mark "status (post-install)";     sf status --profile=default || true
mark "transitions list";          sf transitions list || true
mark "compare (clean)";           sf compare --profile=default || true

# --- introduce drift in the LIVE file, then compare ----------------------
printf '\n[core]\n    editor = vim\n' >> "$H/.config/sample/gitconfig"
mark "compare (drift)";           sf compare --profile=default || true
mark "compare --full-diff (drift)"; sf compare --profile=default --full-diff || true

# --- revert (sandboxed) --------------------------------------------------
mark "revert --to-before help";   sf revert --help || true

echo "" ; echo "### DONE (sandbox $TMP cleaned on exit)"
