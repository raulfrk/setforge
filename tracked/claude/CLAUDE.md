<!--
This file is global. It applies to every project on this VM and is appended on top of Claude Code's
system prompt, which already covers: concision, no speculative error handling, no unnecessary comments,
no backwards-compat shims, confirm-before-destructive. Do not restate those.

Pruning rule (Anthropic): for each line, ask "would removing this cause Claude to make a mistake?"
If no, cut it. CLAUDE.md is advisory, not enforced — for hard rules, write a PreToolUse hook.
-->

@header.md

## Communication

- Goals (in order): high-quality output, high productivity, and learning where it's cheap. Surface trade-offs on multi-option decisions and let me pick — that's where learning happens cheaply without slowing throughput.
- Plan mode whenever you need user input — design choice, ambiguity, multi-option decision. Mechanical edits with no input skip it.
- Capture all load-bearing details into the bd issue's `design` / `acceptance` / `notes` fields before code — interfaces, edge cases, error model, data shapes, scope boundaries, what's explicitly out of scope. Plan mode and Superpowers brainstorming are the *means*; the bd issue is the *durable artifact*. Once the issue is sharpened, implement at human quality without per-decision gating — the issue carries the quality bar.
- `TODO(human)` reserved for moments where I explicitly want to make a call myself during implementation (rare). `TODO(claude)` = my hint to you in code (research starting point). `QUESTION(human)` / `QUESTION(claude)` = inline bidirectional clarifications when something surfaces only during implementation.
- For adversarial review of a load-bearing decision, use the `challenge` skill (`/challenge` or "challenge this"). Otherwise, present 2 options inline with trade-offs.

## Workflow

- Beads owns WHAT (issue = the contract: acceptance criteria, dependencies, scope, completion record). Superpowers owns HOW (brainstorm → plan → implement → verify, applied within an issue).
- bd issue = the contract. Read with `bd show <id>` at session start. The plan-mode session sharpens it; once accepted, code review is verification against the plan, not discovery.
- Default: one issue per session. When no issue is named, `bd ready` picks the next unblocked leaf.
- Out-of-scope findings during review → new `bd create` issue with a dep link, NEVER inline-fix into the current change. This keeps diffs small.
- Tier self-review by blast radius. Leaf / throwaway → spot-check. Public API, concurrency, data pipelines, auth/security → line-by-line.
- Verification means more than "tools passed." Confirm: plan satisfied, nothing introduced outside the plan, tests assert the right thing.
- When the contract isn't obvious, write tests first and treat them as the reviewable spec — names, asserts, what's NOT asserted, edge cases.
- Keep changes small. A 200-line diff understood beats 1500 skimmed. Refuse scope creep within a change.

## Python

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
- Subprocess: list args, `check=True`, `text=True`, `timeout=`, `shutil.which()` for binaries; never `shell=True` with non-literal args.
- `@typing.override` on overrides; `@typing.final` to lock subclass/override surface.
- Docstrings (PEP 257): required on public modules, classes, and functions; one imperative sentence is enough unless behavior, raises, or invariants need calling out.

## Commits

- Subject: imperative mood, capitalized, no period, target 50 chars, hard cap 72. ("Fix X" not "Fixed X".)
- Body required only when the diff is not self-evident: state the problem and the user-visible consequence, not diff narration. Skip body for renames, formatting, trivial fixes.
- Wrap body at 72; one blank line between subject and body.
- One logical change per commit — if the subject needs "and", split it.
- No issue refs in the subject; footers (`Refs: #123`) go after a blank line at the end.
- Use Conventional Commits (`feat:`, `fix:`) only when the repo has a changelog generator or commitlint wired up. Otherwise it's noise.

## Beads (task tracking)

- Use `bd` for ALL task tracking — never TodoWrite, never markdown TODO lists.
- Run `bd prime` for live workflow context. Run `bd <cmd> --help` to verify flags. Full taxonomy and command surface lives in the `bd-reference` skill.
- Persistence layers (pick the right one): **memory** (`bd remember`, cross-project), **issue notes** (`bd note <id>`, body field), **comments** (`bd comment <id>`, timestamped thread for handoffs), **structured fields** (`bd update --description/--design/--acceptance`).
- Never use `bd edit` — it opens `$EDITOR` and blocks the agent. Use `bd update --notes/--design/...` or `bd note` / `bd comment`.
- Worktrees: always create under `~/projects/worktrees/`. `bd` auto-discovers via git common-directory.
- Session close: `bd close <id1> <id2> ...` for completed; `bd comment <id> "next: ..."` for paused; file new issues for incomplete work surfaced this session. Never push to git remotes — I push when ready.

@superpowers-prefs.md

@additional-content.md
