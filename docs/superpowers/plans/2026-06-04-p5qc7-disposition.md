# Disposition model + file-level merge wiring + lock — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans / TDD. Steps use `- [ ]`.

**Goal:** Add an opt-in per-file `disposition` (shared/forked/pinned) that wires the
stored-base 3-way merge into install/sync/compare, with a profile lock and the
internal host-local→pinned rename.

**Architecture:** New `Disposition` StrEnum + `TrackedFile.disposition` field
(opt-in; `None` ⇒ today's exact 2-way path). The file-level 3-way merge attaches
in `deploy._compute_content` gated on `disposition is not None`; capture gates in
`capture.py`; compare reports in `compare.py`. Base advances on resolve only
(live-write then base-write). A profile flock serializes install/sync/compare.

**Tech stack:** Pydantic v2, ruamel.yaml, json-five, merge3, `base_store`,
`markdown_merge`, `structural_merge`, `fcntl`.

Spec: `~/.claude/plans/eager-bouncing-sunrise.md` (read its "Bugs and code smells
to avoid" before each task).

---

## File structure

- `setforge/config.py` — `Disposition` enum, `TrackedFile.disposition`,
  mutual-exclusion validator; re-validate after local.yaml overlay.
- `setforge/locking.py` (NEW) — profile-scoped flock context manager.
- `setforge/markdown_merge.py` — additive `merge_markdown_segments` (ordered
  segments).
- `setforge/structural_merge.py` — additive `set_at_path` (scalar+list,
  comment-preserving) + `apply_path_conflict` resolution helper.
- `setforge/disposition_merge.py` (NEW) — the non-interactive merge driver:
  given (disposition, base, live, tracked, suffix, auto) → resolved text +
  conflict list + whether base should advance.
- `setforge/deploy.py` — wire driver into `_compute_content` / `copy_atomic`;
  thread `disposition`, `base`, `profile`, `file_id`.
- `setforge/cli/install.py` + `_install_helpers.py` — acquire lock; pass
  disposition; re-baseline (write_base) after live-write; prune after loop.
- `setforge/capture.py` — gate capture by disposition; re-baseline shared.
- `setforge/compare.py` — `FileCompare.disposition` + drift class; renderers +
  `--json`.
- `setforge/cli/sync.py`, `setforge/cli/compare.py` — acquire lock.
- Tests under `tests/`.

---

## Task 1: Disposition enum + field + mutual-exclusion validator

**Files:** Modify `setforge/config.py`; Test `tests/test_config_disposition.py`.

- [ ] **Step 1 — failing tests.**
```python
# tests/test_config_disposition.py
import pytest
from pydantic import ValidationError
from setforge.config import Disposition, TrackedFile

def _tf(**kw):
    return TrackedFile(src="a.md", dst="~/a.md", **kw)

def test_disposition_defaults_none():
    assert _tf().disposition is None

def test_disposition_accepts_each_value():
    assert _tf(disposition="shared").disposition is Disposition.SHARED
    assert _tf(disposition="forked").disposition is Disposition.FORKED
    assert _tf(disposition="pinned").disposition is Disposition.PINNED

@pytest.mark.parametrize("bad", ["Shared", "PINNED", "shared ", "fork", "host-local"])
def test_disposition_rejects_bad_value(bad):
    with pytest.raises(ValidationError):
        _tf(disposition=bad)

@pytest.mark.parametrize("legacy", [
    {"preserve_user_sections": True},
    {"preserve_user_keys": ["a"]},
    {"preserve_user_keys_deep": ["a"]},
])
def test_disposition_mutually_exclusive_with_legacy(legacy):
    with pytest.raises(ValidationError, match="disposition"):
        _tf(disposition="shared", **legacy)

def test_no_disposition_allows_legacy():
    assert _tf(preserve_user_keys=["a"]).disposition is None
```
- [ ] **Step 2 — run, expect fail** (`Disposition` undefined).
  `uv run pytest tests/test_config_disposition.py -x`
- [ ] **Step 3 — implement.** Add near the other StrEnums (after `SectionMode`):
```python
class Disposition(StrEnum):
    """How a tracked file is reconciled under the stored-base 3-way model.

    ``shared`` 3-way merges and captures live edits back to tracked;
    ``forked`` 3-way merges but never captures back; ``pinned`` is never
    merged or captured (the live copy is authoritative — today's
    "host-local", renamed). ``None`` on a tracked file keeps the legacy
    2-way preserve behavior unchanged.
    """

    SHARED = "shared"
    FORKED = "forked"
    PINNED = "pinned"
```
Add the field on `TrackedFile` (after `symlink`):
```python
    disposition: Disposition | None = None
    """File-level reconciliation policy (opt-in). ``None`` ⇒ legacy 2-way
    preserve path, unchanged. Mutually exclusive with the legacy
    ``preserve_*`` family (see :meth:`_disposition_excludes_legacy_preserve`)."""
```
Add the validator (after `_no_preserve_path_overlap`):
```python
    @model_validator(mode="after")
    def _disposition_excludes_legacy_preserve(self) -> Self:
        """A file uses EITHER the new disposition model OR legacy preserve_*.

        The two are different reconciliation models (whole-file 3-way vs
        per-key/section live-preserve); allowing both on one file would make
        the deploy path ambiguous. Reject the combination at load.
        """
        if self.disposition is None:
            return self
        offenders = []
        if self.preserve_user_sections:
            offenders.append("preserve_user_sections")
        if self.preserve_user_keys:  # computed view, excludes REMOVED_VIA_LOCAL
            offenders.append("preserve_user_keys")
        if self.preserve_user_keys_deep:
            offenders.append("preserve_user_keys_deep")
        if self.preserve_user_sections_mode is not SectionMode.KEEP_DEFAULTS:
            offenders.append("preserve_user_sections_mode")
        if offenders:
            raise ValueError(
                f"disposition: {self.disposition.value!r} is mutually exclusive "
                f"with legacy preserve field(s): {sorted(offenders)}. A file uses "
                f"either the disposition model or preserve_*, not both."
            )
        return self
```
- [ ] **Step 4 — run, expect pass.**
- [ ] **Step 5 — commit.** `Add disposition field with legacy-preserve exclusion`

## Task 2: local.yaml disposition override (re-validated)

**Files:** Modify `setforge/config.py` (`apply_host_local_tracked_file_overrides`,
and the local-overlay tracked-file override schema); Test
`tests/test_config_disposition.py` (extend).

- [ ] **Step 1 — read** `apply_host_local_tracked_file_overrides` (config.py:669)
  + the `_LocalTrackedFileOverlay`/local overlay schema to find where mode/dst/
  symlink overrides rebuild the TrackedFile. Confirm the rebuild path uses
  `model_validate` (re-runs validators) vs `model_copy` (bypasses).
- [ ] **Step 2 — failing test.** local.yaml sets `disposition: forked` on a file;
  assert resolved tracked_file.disposition is FORKED. Second test: a
  disposition file whose local.yaml override adds a preserve key ⇒ ValidationError.
- [ ] **Step 3 — run, expect fail.**
- [ ] **Step 4 — implement.** Add `disposition` to the local override field set;
  rebuild via `model_validate(model_dump() | overrides)` so the mutual-exclusion
  validator re-fires on the merged model.
- [ ] **Step 5 — run, expect pass. Commit.** `Allow per-host disposition override in local.yaml`

## Task 3: profile lock

**Files:** Create `setforge/locking.py`; Test `tests/test_locking.py`; wire into
`cli/install.py`, `cli/sync.py`, `cli/compare.py`.

- [ ] **Step 1 — failing test.** `profile_lock(profile)` context manager: holding
  it, a second non-blocking acquire raises/returns busy; releasing frees it.
  Lockfile lives under `state_root()/locks/<profile>.lock`.
- [ ] **Step 2 — run, expect fail.**
- [ ] **Step 3 — implement** with `fcntl.flock(LOCK_EX)` on a fd under
  `state_root()`; capture `state_root()` once. Clean `SetforgeError` on contention
  for non-blocking callers (or block — pick blocking with a clear message; decide
  in step 1 test).
- [ ] **Step 4 — run, expect pass.**
- [ ] **Step 5 — acquire** the lock at the top of install/sync/compare command
  bodies (`with profile_lock(profile):`). Commit. `Add profile-scoped lock for state-mutating commands`

## Task 4: markdown segment view

**Files:** Modify `setforge/markdown_merge.py`; Test `tests/test_markdown_merge_segments.py`.

- [ ] **Step 1 — failing tests.** Define `CleanSegment(lines)` / reuse
  `LineConflict`; `merge_markdown_segments(base, ours, theirs) -> list[Segment]`.
  - clean input ⇒ `"".join(concat of all clean segment texts)` (+ ours terminator)
    == `merge_markdown(...).merged_text` byte-for-byte.
  - conflict at index 0 and at last position ⇒ adjacent clean block appears once.
  - both-add-same (`same` tag) ⇒ one clean segment, no conflict.
- [ ] **Step 2 — run, expect fail.**
- [ ] **Step 3 — implement.** Refactor the `merge_groups` walk to build an ordered
  `list[Segment]` (Segment = `CleanSegment` | `LineConflict`); cover all 5 tags
  with `case _: raise`. Keep `merge_markdown` returning the existing shape
  (derive from segments). Restore the ours terminator on the final rebuild.
- [ ] **Step 4 — run, expect pass. Commit.** `Add ordered-segment view to markdown 3-way merge`

## Task 5: structural set-value-at-path + path-conflict apply

**Files:** Modify `setforge/structural_merge.py`; Test `tests/test_structural_set_at_path.py`.

- [ ] **Step 1 — failing tests.** `set_at_path(model, dotted_path, value)` for
  ruamel + json-five + plain: sets a scalar; sets a list; replaces an existing
  leaf preserving sibling comments + the leaf's `wsc_before`; json-five appends
  via `.keys`/`.values` lockstep (never `key_value_pairs`); round-trip byte-stable
  on untouched regions.
- [ ] **Step 2 — run, expect fail.**
- [ ] **Step 3 — implement** `set_at_path` reusing the backend abstraction
  (`_make_backend`/`_apply_take` patterns already in the module).
- [ ] **Step 4 — run, expect pass. Commit.** `Add comment-preserving set-value-at-path for structural merge`

## Task 6: non-interactive disposition merge driver

**Files:** Create `setforge/disposition_merge.py`; Test `tests/test_disposition_merge.py`.

- [ ] **Step 1 — failing tests.** `resolve_file(disposition, suffix, base, live,
  tracked, auto) -> FileResolution{text, conflicts, advance_base}`:
  - pinned ⇒ text == live, no merge, advance_base False, base untouched.
  - shared/forked clean merge ⇒ merged text, advance_base True.
  - conflict + auto=keep-live ⇒ ours at conflicts, advance_base True.
  - conflict + auto=use-tracked ⇒ theirs at conflicts, advance_base True.
  - conflict + auto=None ⇒ ours at conflicts, advance_base **False** (defer),
    conflicts non-empty (caller warns).
  - base absent (None) ⇒ caller signalled to 2-way-fallback (driver returns a
    BASE_ABSENT marker so deploy does the legacy render then seed).
- [ ] **Step 2 — run, expect fail.**
- [ ] **Step 3 — implement** dispatching markdown (segments) vs structural
  (PathConflict apply) by suffix; reuse `jsonc`/`ruamel` load+dump for structural
  serialize (preserve_quotes). Use `is`/`_scalar_eq` discipline.
- [ ] **Step 4 — run, expect pass. Commit.** `Add non-interactive disposition merge driver`

## Task 7: wire driver into deploy

**Files:** Modify `setforge/deploy.py` (`copy_atomic`, `_compute_content`),
threading `disposition`, `base_text`, and a re-baseline callback or returned
`new_base`; Test `tests/test_deploy_disposition.py`.

- [ ] **Step 1 — failing tests.** `copy_atomic(..., disposition=SHARED,
  base_text=...)`: clean merge writes merged content; base-absent writes 2-way
  fallback; pinned leaves live untouched; the result reports the new base bytes to
  seed. No-disposition path unchanged (regression test: identical bytes to today).
- [ ] **Step 2 — run, expect fail.**
- [ ] **Step 3 — implement.** In `_compute_content`, when `disposition is not
  None`, branch to `disposition_merge.resolve_file` instead of the
  preserve-keys/sections path; return the resolved text + surface the new-base /
  advance signal up through `DeployResult` (add fields). Keep live-write inside
  `_atomic_write`; the base write is the caller's job (Task 8), so `copy_atomic`
  returns enough info (resolved bytes + advance flag) for the install loop.
- [ ] **Step 4 — run, expect pass. Commit.** `Wire file-level 3-way merge into deploy by disposition`

## Task 8: install loop — base seed/advance + prune + lock

**Files:** Modify `setforge/cli/install.py`, `setforge/cli/_install_helpers.py`;
Test `tests/test_install_disposition.py` (integration via temp profile +
`SETFORGE_STATE_DIR`).

- [ ] **Step 1 — failing tests.** install a shared file: base seeded =
  deployed tracked bytes after live-write; install twice no edits ⇒ second run
  zero drift; edit live + tracked different lines ⇒ clean merge, base advances;
  same line ⇒ keep-live + warn + base NOT advanced (re-detects). prune removes
  base for a file dropped from the profile.
- [ ] **Step 2 — run, expect fail.**
- [ ] **Step 3 — implement.** Read base via `base_store.read_base(profile,
  file_id)` before deploy; pass to `copy_atomic`; after a successful live-write
  and when `advance_base`, `base_store.write_base(profile, file_id,
  resolved_bytes)` (live FIRST, base SECOND, loud on failure). After the loop,
  `base_store.prune(profile, live_file_ids)`. Wrap the command in `profile_lock`.
- [ ] **Step 4 — run, expect pass. Commit.** `Seed/advance/prune stored base in install loop`

## Task 9: capture gating

**Files:** Modify `setforge/capture.py`; Test `tests/test_capture_disposition.py`.

- [ ] **Step 1 — failing tests.** shared ⇒ live captured to tracked + base
  re-baselined; forked ⇒ capture skipped (tracked unchanged); pinned ⇒ skipped.
- [ ] **Step 2 — run, expect fail.**
- [ ] **Step 3 — implement.** Gate `capture_tracked_file` / `capture_profile` loop
  on disposition; re-baseline shared after capture. Acquire lock in `cli/sync.py`.
- [ ] **Step 4 — run, expect pass. Commit.** `Gate sync capture by disposition`

## Task 10: compare disposition + drift class

**Files:** Modify `setforge/compare.py`, `setforge/cli/compare.py`; Test
`tests/test_compare_disposition.py`.

- [ ] **Step 1 — failing tests.** `FileCompare.disposition` populated; shared drift
  classed unexpected, forked/pinned classed expected; `compare --json` includes
  `disposition` per file.
- [ ] **Step 2 — run, expect fail.**
- [ ] **Step 3 — implement.** Add `disposition` to `FileCompare`; thread through
  `_compare_one`; adjust drift classification + text/JSON renderers. Acquire lock
  in `cli/compare.py`.
- [ ] **Step 4 — run, expect pass. Commit.** `Report disposition and disposition-aware drift in compare`

## Task 11: host-local→pinned internal rename

**Files:** internal identifiers only (no user-facing field rename); Test: existing
suite stays green.
- [ ] **Step 1** — grep internal `host_local` code-symbol uses that name the
  *disposition* concept (NOT the local.yaml layer, NOT marker semantics, NOT the
  `host_local_sections` config field — those stay). Rename only true disposition
  references to `pinned`.
- [ ] **Step 2 — run full suite + pre-commit, expect green. Commit.** `Rename host-local disposition concept to pinned (internal)`

---

## Self-review notes
- Every spec decision (1–10) maps to a task: enum/field/validator (T1), local
  override (T2), lock (T3), segments (T4), set-at-path (T5), driver (T6), deploy
  wiring (T7), base lifecycle (T8), capture gating (T9), compare (T10), rename
  (T11).
- Re-baseline timing (advance on resolve only) lives in T6 (`advance_base` flag)
  + T8 (honoring it). Live-then-base ordering in T8.
- Pitfall checklist (spec) is the review-fan target for Phase 5.

## Verification
```sh
uv run pytest tests/ -q
pre-commit run --all-files
# Phase 7 (post-merge): uv run pytest tests/docker/ -m e2e_docker -v --no-cov
```
