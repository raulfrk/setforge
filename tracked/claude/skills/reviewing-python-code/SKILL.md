---
name: reviewing-python-code
description: 5-aspect parallel review fan for Python code changes (Python source / pyproject / pre-commit / CI workflow edits). Dispatches `python-spec-reviewer`, `python-form-reviewer`, `python-substance-reviewer`, `python-specifics-reviewer`, and `python-prose-reviewer` in parallel via a single message of Agent tool calls. Consolidates verdicts worst-of-five. Use after Python implementation (Phase 5), and again post-merge (Phase 7).
---

# Reviewing Python Code

This skill orchestrates a 5-aspect parallel review fan for Python code changes. Each aspect is a dedicated specialized agent under `~/.claude/agents/`; this skill's job is to dispatch all five in parallel via the `Agent` tool **in a single message**, then consolidate the five reports.

## When to invoke

- **Phase 5** (review fan) after Phase 4 (Implement) completes and before Phase 6 (Address findings + merge), per the 7-phase flow in `superpowers-prefs.md`.
- **Phase 7** (post-merge cross-cutting review) against the merged HEAD on the target branch.

Artifacts in scope:
- Python source (`*.py`).
- `pyproject.toml`, `uv.lock`.
- `.pre-commit-config.yaml`.
- `.github/workflows/*.yml`.

For mixed-artifact PRs (Python + CLAUDE.md), invoke BOTH `reviewing-python-code` AND `reviewing-claude-md` in parallel via separate Skill invocations.

## Dispatch inputs

Compute (or accept as input):

| Input | Default |
|---|---|
| `BASE_SHA` | `git merge-base HEAD origin/main` |
| `HEAD_SHA` | `git rev-parse HEAD` |
| `spec_path` | `~/.claude/projects/{cwd-slug}/specs/<YYYY-MM-DD>-<topic>.md` or `(none)` |
| `bd_id` | derive from current branch / worktree slug, or explicit user input |
| `changed_files` | `git diff --name-only $BASE_SHA..$HEAD_SHA` |

## Parallel dispatch

Send **a single message** containing 5 `Agent` tool calls, one per reviewer:

1. `subagent_type: python-spec-reviewer` — spec/contract conformance.
2. `subagent_type: python-form-reviewer` — ruff/mypy/PEP cleanliness.
3. `subagent_type: python-substance-reviewer` — design, error model, security.
4. `subagent_type: python-specifics-reviewer` — CLAUDE.md Python rules + my-setup conventions.
5. `subagent_type: python-prose-reviewer` — docstring prose quality + factual correctness vs. function body.

Use the same prompt template across all five:

```
You are reviewing the diff in BASE_SHA..HEAD_SHA.
BASE_SHA: <sha>
HEAD_SHA: <sha>
spec_path: <path or "(none)">
bd_id: <bd-id>
changed_files:
- <file>
- <file>
...

Apply your aspect-specific checklist (see your system prompt body).
Return structured findings + DoD checklist + verdict line.
```

## Consolidation

After all 5 agents return, produce a single report:

```
# Python review fan report (BASE_SHA..HEAD_SHA)

## python-spec-reviewer (red)
<sub-report verbatim>

## python-form-reviewer (blue)
<sub-report verbatim>

## python-substance-reviewer (green)
<sub-report verbatim>

## python-specifics-reviewer (purple)
<sub-report verbatim>

## python-prose-reviewer (yellow)
<sub-report verbatim>

## Overall
- python-spec: <verdict>
- python-form: <verdict>
- python-substance: <verdict>
- python-specifics: <verdict>
- python-prose: <verdict>

Overall verdict: <worst-of-five — BLOCK > CONCERNS > PASS>
```

## After the report

- CRITICAL findings → fix immediately.
- IMPORTANT findings → fix before merge. Per the Commits rule in `~/.claude/CLAUDE.md`, review-fix changes commit as their OWN commits — never squashed into the implementation commit.
- MINOR findings → file new bd issues with dep links unless trivially fixable inline.

## Definition of done for the orchestrator

- [ ] All 5 reviewer dispatches issued in a single message (true parallel).
- [ ] All 5 agents returned a `Verdict: ...` line.
- [ ] Consolidated report produced with all 5 sub-reports verbatim.
- [ ] Worst-of-five overall verdict computed and stated explicitly.
