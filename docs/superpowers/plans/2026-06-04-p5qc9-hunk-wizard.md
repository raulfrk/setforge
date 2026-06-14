# Interactive hunk conflict wizard — Implementation Plan

**Goal:** When an install-time disposition merge produces genuine conflicts and
the session is interactive, prompt the user per conflict
(`[k]eep-yours / [t]ake-upstream / [e]dit / [s]kip`) instead of applying the
non-interactive default; clean merges never reach the wizard.

**Design:** `disposition_merge` stays pure by accepting an OPTIONAL injectable
per-conflict resolver. `resolver=None` ⇒ today's exact non-interactive auto
logic (unchanged). An interactive resolver (the new `conflict_wizard`) does the
prompt/editor I/O. The install loop passes the wizard resolver only when
interactive (tty + the existing reconcile gate). `--auto` keeps working
non-interactively and short-circuits the wizard.

**Reused:** `wizard.read_one_choice`, `_editor.run_editor`,
`markdown_merge.{merge_markdown_segments, resolve_segments, LineConflict}`,
`structural_merge.{PathConflict, set_at_path, merge_structural}`,
`disposition_merge.resolve_file`.

Spec: `~/.claude/plans/eager-bouncing-sunrise.md` (hunk-wizard section + pitfalls).

## Task 1: injectable per-conflict resolver in disposition_merge
File: `setforge/disposition_merge.py`; Test: `tests/test_disposition_merge_resolver.py`.
- Add:
  ```python
  class ConflictChoice(StrEnum): KEEP_OURS="keep_ours"; TAKE_THEIRS="take_theirs"; EDIT="edit"; SKIP="skip"
  @dataclass(frozen=True, slots=True)
  class ConflictResolution:
      choice: ConflictChoice
      edited_lines: list[str] | None = None   # line-based EDIT payload (terminators kept)
      edited_value: object | None = None      # structural EDIT payload (plain scalar/list)
  ConflictResolver = Callable[[LineConflict | PathConflict], ConflictResolution]
  ```
- Add `resolver: ConflictResolver | None = None` kwarg to `resolve_file`, `_resolve_structural`, `_resolve_line_based`. When `resolver is None`, behavior is byte-identical to today (auto logic). When set AND there are conflicts, drive each conflict through `resolver`:
  - line-based: build the `choose` fn from per-conflict resolutions — KEEP_OURS→`c.ours`, TAKE_THEIRS→`c.theirs`, EDIT→`res.edited_lines`, SKIP→`c.ours`. Track whether ANY conflict was SKIP.
  - structural: per PathConflict — KEEP_OURS→noop (ours in model), TAKE_THEIRS→`set_at_path(model, pc.path, pc.theirs)`, EDIT→`set_at_path(model, pc.path, res.edited_value)`, SKIP→noop. Track SKIP.
  - `advance_base = (no conflicts) or (resolver applied AND no SKIP)`. ANY skip ⇒ defer (False) so the file re-detects next run. (PINNED / base-absent paths unchanged.)
- Tests: resolver returning each choice for both line-based + structural; mixed (one resolved one skipped) ⇒ advance_base False, resolved conflict still applied; EDIT payload applied; resolver=None unchanged (regression).

## Task 2: the conflict wizard
File: `setforge/conflict_wizard.py`; Test: `tests/test_conflict_wizard.py`.
- `make_wizard_resolver(console/...) -> ConflictResolver` (or a class) that, per conflict:
  - Render the conflict: for `LineConflict` show ours vs theirs line blocks; for `PathConflict` show `pc.path` + ours/theirs values.
  - Prompt via `wizard.read_one_choice("  Choice (k/t/e/s): ", {"k","t","e","s"})`.
  - `k`→ConflictResolution(KEEP_OURS); `t`→TAKE_THEIRS; `s`→SKIP.
  - `e`→ open `_editor.run_editor` on a tmpfile: line-based seed with ours' lines (or a 3-pane base/ours/theirs view — keep simple: seed ours, let user edit) → read back → `edited_lines`; structural seed with the serialized ours value (YAML/JSON snippet) → parse back → `edited_value` (clean parse-error handling → re-prompt). Return EDIT.
- Reuse `read_one_choice`'s non-tty fallback so tests can feed input. Tests: feed scripted choices (k/t/s) + an edit; assert the returned ConflictResolution; parse-error-then-retry for structural edit.

## Task 3: install wiring (interactive gate)
Files: `setforge/deploy.py` (thread a `resolver` into `copy_atomic` → `resolve_file`), `setforge/cli/_install_helpers.py` / `setforge/cli/install.py` (build the wizard resolver when interactive + reconcile-gated, else None). Test: `tests/test_install_wizard.py`.
- `copy_atomic` gains `conflict_resolver: ConflictResolver | None = None`, passed into the disposition `resolve_file` call.
- Install builds the resolver only when: stdout is a tty AND the existing interactive reconcile condition holds (mirror how section reconcile decides interactive vs `--auto`). When `--auto` is set, resolver stays None (auto wins, no prompt). Non-tty/non-interactive ⇒ None (today's behavior).
- Tests: drive install with a scripted resolver (inject a fake resolver to avoid real tty) over a conflicting disposition file → live reflects the per-conflict choices; skip ⇒ base not advanced; `--auto` set ⇒ resolver NOT invoked.

## Task 4: e2e (pyte TUI)
File: `tests/docker/test_e2e_docker_conflict_wizard.py`.
- Use the `pyte_pty_session` fixture (per CLAUDE.md, for full-screen prompt panels; `docker exec -it`, arrows `\x1b[A/B`, Enter `\r`). Drive an install that hits a conflict on a `disposition: shared` file; send `k` / `t` / `s` keystrokes; assert the live file reflects the choice and (for skip) re-running compare still shows drift. Model on `tests/docker/pyte_session.py` API + an existing interactive e2e (e.g. `test_e2e_docker_section_reconcile.py`).

## Verification
```sh
uv run pytest tests/ -q --no-cov
pre-commit run --all-files
uv run pytest tests/docker/test_e2e_docker_conflict_wizard.py -m e2e_docker --no-cov
```
