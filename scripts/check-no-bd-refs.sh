#!/usr/bin/env bash
# Block leaked Beads / bd task-tracker references from entering shipping
# artifacts. This is the deterministic HARD GATE half of bd-leak enforcement;
# the advisory, high-recall half is the `bd-leak-reviewer` agent run in every
# review fan (see the `reviewing-bd-leaks` skill).
#
# Precision contract: a hard gate must NEVER false-block, or it gets disabled.
# So this script matches ONLY unambiguous tracker tokens:
#   - `bd <subcommand>` command lines
#   - `.beads/` database paths
#   - the `~/handoff` tracker repo path
# It deliberately does NOT match bare issue IDs (`setforge-<id>`): that shape is
# indistinguishable by regex from the repo/branch/worktree names that
# legitimately appear in shipping docs (`setforge-config`, `setforge-p5qc-audit`).
# Issue-ID and fuzzy detection ("bd", "beads", epic-child shorthand) require
# judgment and are handled by the bd-leak-reviewer agent, not this gate.
#
# Usage:
#   check-no-bd-refs.sh <file> [<file> ...]   # scan staged file CONTENT (pre-commit)
#   check-no-bd-refs.sh --commit-msg <file>   # scan a commit message (commit-msg)
# Exits 1 (with file:line: match) on any hit; 0 otherwise.

set -euo pipefail

# Structured, high-precision patterns (extended regex). The bd-command verb list
# tracks the documented surface; new verbs are caught by the agent's fuzzy pass.
readonly PATTERN='(\bbd[[:space:]]+(create|q|ready|show|list|update|close|note|comment|dep|blocked|search|recall|remember|forget|memories|defer|undefer|reopen|supersede|stale|orphans|assign|human|doctor|preflight|prime|children|init|migrate|upgrade)\b)|(\.beads(/|\b))|(~/handoff\b)'

# Paths exempt from scanning: the private orchestration layer legitimately
# references bd, and the detector's own files carry the patterns by necessity.
is_exempt() {
  case "$1" in
    */CLAUDE.md|CLAUDE.md) return 0 ;;
    */.claude/skills/*|*/.claude/agents/*|.claude/skills/*|.claude/agents/*) return 0 ;;
    *tracked/claude/*) return 0 ;;
    # Ignore-files reference `.beads/` to EXCLUDE it from the image / index —
    # that is the invisibility mechanism, not a leak; removing it would expose
    # the tracker DB.
    .dockerignore|*/.dockerignore|.gitignore|*/.gitignore) return 0 ;;
    # The detector's own source + its test carry the patterns by necessity.
    */check-no-bd-refs.sh|check-no-bd-refs.sh|*test_check_no_bd_refs.py) return 0 ;;
    *) return 1 ;;
  esac
}

rc=0

if [ "${1:-}" = "--commit-msg" ]; then
  msg_file="${2:?--commit-msg requires a path}"
  # Strip comment lines (pre-commit / git scissors) before scanning.
  if matches=$(grep -nE "$PATTERN" "$msg_file" | grep -vE '^[0-9]+:[[:space:]]*#'); then
    echo "bd-leak: tracker reference in commit message:" >&2
    echo "$matches" | sed 's/^/  commit-msg:/' >&2
    rc=1
  fi
else
  for f in "$@"; do
    [ -f "$f" ] || continue
    is_exempt "$f" && continue
    if matches=$(grep -nE "$PATTERN" "$f"); then
      echo "bd-leak: tracker reference in $f:" >&2
      echo "$matches" | sed "s|^|  $f:|" >&2
      rc=1
    fi
  done
fi

if [ "$rc" -ne 0 ]; then
  echo "" >&2
  echo "Beads must stay invisible in shipping artifacts. Remove the reference(s) above" >&2
  echo "(or, if this file is part of the private layer, add it to is_exempt)." >&2
fi
exit "$rc"
