<!--
This file is global. It applies to every project on this VM and is appended on top of Claude Code's
system prompt, which already covers: concision, no speculative error handling, no unnecessary comments,
no backwards-compat shims, confirm-before-destructive. Do not restate those.

Pruning rule (Anthropic): for each line, ask "would removing this cause Claude to make a mistake?"
If no, cut it. CLAUDE.md is advisory, not enforced — for hard rules, write a PreToolUse hook.
-->

@header.md

## Communication

<!-- my-setup:user-section start communication -->
- **[CRITICAL] Ground every decision-ask in concrete context before asking.** Before presenting design questions, options, plans, specs, or any choice that depends on the current state of code: surface WHAT each thing is (file:line refs, current code shapes), WHY it's a problem (the specific smell, bug, or constraint at stake), THEN options as concrete code shapes — never abstract A/B/C without grounding. Applies to brainstorming, plan mode, exploratory questions, and inline trade-offs alike. *Highest-priority communication rule; overrides terseness preferences when they conflict.*
- Goals (in order): high-quality output, high productivity, and learning where it's cheap. Surface trade-offs on multi-option decisions and let me pick — that's where learning happens cheaply without slowing throughput.
- Plan mode whenever you need user input — design choice, ambiguity, multi-option decision. Mechanical edits with no input skip it.
- Every plan mode response opens with a **TL;DR** — one or two sentences naming the proposed approach and the key trade-off — before any sectioned plan body.
- Capture all load-bearing details into the bd issue's `design` / `acceptance` / `notes` fields before code — interfaces, edge cases, error model, data shapes, scope boundaries, what's explicitly out of scope. Plan mode and Superpowers brainstorming are the *means*; the bd issue is the *durable artifact*. Once the issue is sharpened, implement at human quality without per-decision gating — the issue carries the quality bar.
- `TODO(human)` reserved for moments where I explicitly want to make a call myself during implementation (rare). `TODO(claude)` = my hint to you in code (research starting point). `QUESTION(human)` / `QUESTION(claude)` = inline bidirectional clarifications when something surfaces only during implementation.
- For adversarial review of a load-bearing decision, use the `challenge` skill (`/challenge` or "challenge this"). Otherwise, present 2 options inline with trade-offs.
<!-- my-setup:user-section end communication -->

## Workflow

<!-- my-setup:user-section start workflow -->
- Beads owns WHAT (issue = the contract: acceptance criteria, dependencies, scope, completion record). Superpowers owns HOW (the multi-phase flow applied within an issue). See `superpowers-prefs.md` for the canonical phase flow and hard-gate posture.
- Default phase flow on non-trivial work: **brainstorm → spec → plan → implement → review fan → address-findings+merge → post-merge review**. The **spec** is written verbatim into plan mode for user approval; the **plan** is a normal response. For parallel work that produces multiple specs, run them sequentially through plan mode or batch them into one plan-mode session — never skip plan-mode review of a spec. Escape hatch is narrow — single-file mechanical edits only.
- After implementation, invoke the appropriate `reviewing-X` skill for the artifact: `reviewing-python-code` for Python source / pyproject / CI workflow / pre-commit-config; `reviewing-claude-md` for tracked/claude/ docs / skills / agents. Each fires a 4-aspect parallel review fan (spec / form / substance / specifics) before merge, and again post-merge against merged HEAD. Mixed-artifact diffs invoke both.
- bd issue = the contract. Read with `bd show <id>` at session start. The plan-mode spec session sharpens it; once accepted, code review is verification against the spec, not discovery.
- Default: one issue per session. When no issue is named, `bd ready` picks the next unblocked leaf.
- Out-of-scope findings during review → new `bd create` issue with a dep link, NEVER inline-fix into the current change. This keeps diffs small.
- Tier self-review by blast radius. Leaf / throwaway → spot-check. Public API, concurrency, data pipelines, auth/security → line-by-line.
- Verification means more than "tools passed." Confirm: spec satisfied, nothing introduced outside the spec, tests assert the right thing.
- When the contract isn't obvious, write tests first and treat them as the reviewable spec — names, asserts, what's NOT asserted, edge cases.
- Keep changes small. A 200-line diff understood beats 1500 skimmed. Refuse scope creep within a change.
- Worktree primitive: `wt switch --create <slug>` (worktrunk), not raw `git worktree add`. Worktrees land under `~/projects/worktrees/<slug>` per wt's configured location. See the `wt-reference` skill for the command surface.
- Canonical parallel-work loop:
  1. `bd ready` — pick the next unblocked issue.
  2. `bd show <id>` — load the contract.
  3. `wt switch --create <slug>` — new worktree + branch (slug should include the bd id, e.g. `dotfiles-g20-py-rewrite`).
  4. `bd update <id> --claim` — mark in_progress immediately, before any code or research work.
  5. Run the phase flow inside the worktree: brainstorm → spec (plan-mode review) → plan → implement → review fan (`reviewing-X` skill) → address-findings+merge → post-merge cross-cutting review.
  6. `wt merge --no-squash` — merge the branch into target.
  7. `bd close <id>` — close the issue.
  8. `wt remove` — delete worktree (auto-deletes the merged branch).
<!-- my-setup:user-section end workflow -->

## Python

<!-- my-setup:user-section start python -->
- Target the project's declared Python version; assume 3.11+ unless config says otherwise.
- Annotate every public function, method, and module-level constant. Use `X | None`, never `Optional`; `X | Y`, never `Union` (PEP 604).
- Import `Iterable`, `Sequence`, `Mapping`, `Callable` from `collections.abc`, never `typing` (PEP 585). Reserve `typing` for `Protocol`, `TypedDict`, `Literal`, `cast`, `override`, `Self`, `Never`.
- On 3.12+: `class Foo[T]:` over module-level `TypeVar`; `type Alias = ...` over `TypeAlias` (PEP 695).
- Prefer `@dataclass(slots=True, frozen=True)` for value objects. Reach for `attrs.frozen` only when you need per-field validators, converters, or `cattrs` un/structuring. Pydantic (or `msgspec.Struct` when hot) at trust boundaries — HTTP, config, external JSON.
- `enum.StrEnum` / `IntEnum` for closed sets — never bare module-level magic strings.
- `pathlib.Path` and `/`; never `os.path.join` or string concatenation for paths.
- `match`/`case` when destructuring shape or dispatching on type+attributes; `if`/`elif` for plain values.
- `typing.Protocol` for structural interfaces; `abc.ABC` only when runtime instantiation guards are needed.
- F-strings only. `with` for resources. `contextlib.suppress(Exc)` over empty `except Exc: pass`.
- Functions: aim under ~40 lines, nesting depth ≤ 3. Exceeded → extract a helper.
- Use `uv` (env, install, run); never raw pip/poetry/virtualenv.
- Subprocess: list args, `check=True`, `text=True`, `timeout=`, `shutil.which()` for binaries; never `shell=True` with non-literal args. Use `import subprocess` and call `subprocess.run(...)` — never `from subprocess import run` — when test monkeypatching of the call site depends on the module-attribute access path.
- `@typing.override` on overrides; `@typing.final` to lock subclass/override surface.
- Docstrings (PEP 257): required on public modules, classes, and functions; one imperative sentence is enough unless behavior, raises, or invariants need calling out.
<!-- my-setup:user-section end python -->

## Commits

<!-- my-setup:user-section start commits -->
- Subject: imperative mood, capitalized, no period, target 50 chars, hard cap 72. ("Fix X" not "Fixed X".)
- Body required only when the diff is not self-evident: state the problem and the user-visible consequence, not diff narration. Skip body for renames, formatting, trivial fixes.
- Wrap body at 72; one blank line between subject and body.
- One logical change per commit — if the subject needs "and", split it.
- Never squash review-fix commits into the implementation commit. They document what the review fan caught; preserving them as separate commits keeps the audit trail meaningful. Operationally: use `wt merge --no-squash` for worktree merges, or `git merge --ff-only` for plain git.
- No issue refs in the subject; footers (`Refs: #123`) go after a blank line at the end.
- Use Conventional Commits (`feat:`, `fix:`) only when the repo has a changelog generator or commitlint wired up. Otherwise it's noise.
<!-- my-setup:user-section end commits -->

## Beads (task tracking)

- **Invoke the `bd-reference` skill via the Skill tool the first time bd is involved in a session** — before any `bd` command other than `bd prime` (which the SessionStart hook handles). It carries flag syntax, lifecycle verbs (defer / supersede / stale / orphans), quality flags (`--validate` / `--acceptance` / `--design` / `--notes`), and handoff patterns. Do not guess bd flags from memory; flags drift between releases. Once loaded in a session, it stays loaded — no need to re-invoke.
- Use `bd` for ALL task tracking — never TodoWrite, never markdown TODO lists.
- `bd prime` gives live workflow context (auto-fired by SessionStart hook). `bd <cmd> --help` verifies a specific flag set on the installed binary when in doubt.
- Persistence layers (pick the right one): **memory** (`bd remember`, cross-project), **issue notes** (`bd note <id>`, body field), **comments** (`bd comment <id>`, timestamped thread for handoffs), **structured fields** (`bd update --description/--design/--acceptance`).
- Never use `bd edit` — it opens `$EDITOR` and blocks the agent. Use `bd update --notes/--design/...` or `bd note` / `bd comment`.
- Worktrees: always create under `~/projects/worktrees/`. `bd` auto-discovers via git common-directory.
- Session close: `bd close <id1> <id2> ...` for completed; `bd comment <id> "next: ..."` for paused; file new issues for incomplete work surfaced this session. Never push to git remotes — I push when ready.

@superpowers-prefs.md

@additional-content.md
