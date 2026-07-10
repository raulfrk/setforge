# B1 Provisioner Protocol — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]` tracking. Full design + pitfall checklist live in the approved spec `~/.claude/plans/synchronous-giggling-squid.md` (read it before Task 1).

**Goal:** Build the uniform provisioner protocol + central reconcile driver + error model + a copy-me reference provisioner — Epic B's foundation, proven by an in-memory reference (real cargo/python/go/release stay B2–B5).

**Architecture:** New `setforge/provision/` package. Plan/apply split (pure `plan()` → typed delta; driver applies only when policy≠REPORT). ABC + frozen `Identity` value-object. Marker-based `ReceiptStore` for list-less ecosystems, reusing `setforge/atomicio.py`. No live `setforge install` wiring, no changes to `reconcile/` or plugin/extension code.

**Tech Stack:** Python 3.13, StrEnum, frozen dataclasses, pydantic BaseModel, pytest, mypy.

---

## Conventions (all tasks)
- Match repo style: `StrEnum`, `@dataclass(frozen=True)`, PEP 604/695 types, pathlib, Google-style docstrings, comment-density ≤2%.
- TDD: write the failing test first, assert it fails against pre-change state (quote the red), then implement.
- Commit per task with a `feat:`/`test:`/`docs:` subject. NO bd references in code/commits.
- Reuse: `setforge/atomicio.py` (atomic writes), `setforge/errors.py:SetforgeError` (base), existing `ReconcilePolicy` (`setforge/config.py`).

## File structure
| File | Responsibility |
|---|---|
| `setforge/provision/protocol.py` | `Provisioner(ABC)`, `Identity`, `DesiredState`, `Outcome`, `ProvisionItem/Outcome/Delta`, `ReconcileResult` |
| `setforge/provision/registry.py` | `@register("type")` + `build(item)` |
| `setforge/provision/receipt.py` | `ReceiptStore` — atomic per-item marker |
| `setforge/provision/driver.py` | `reconcile(...)`, `exit_code(...)` |
| `setforge/provision/reference.py` | `InMemoryProvisioner` (proof + scaffold) |
| `setforge/provision/__init__.py` | package exports |
| `setforge/errors.py` | + `ProvisionItemFailed` (additive) |
| `docs/RULES.md` | + INV-7 definition (additive) |
| `tests/unit/test_provision_*.py` | acceptance + property tests |

---

### Task 1: Protocol surface + error model
**Files:** Create `setforge/provision/protocol.py`; Modify `setforge/errors.py`; Test `tests/unit/test_provision_protocol.py`

- [ ] Write failing tests: `Identity` is frozen; `a==b ⇒ hash(a)==hash(b)` for two `Identity(key=k, display=d1)` / `(key=k, display=d2)` (match keys on `.key` only); `Outcome`/`DesiredState` StrEnum values; `ProvisionDelta().is_empty()` True, non-empty False; `ProvisionItemFailed` carries `item_id/error_summary/full_stderr/kind` and is a `SetforgeError`.
- [ ] Run → fails (module/class not defined).
- [ ] Implement `protocol.py` per spec §2 (Outcome, DesiredState, frozen Identity with `key`+`display`, ProvisionItem with validated `config: BaseModel`, ProvisionOutcome, ProvisionDelta, `ReconcileResult`, `Provisioner(ABC)` with `probe/plan/apply_one/uninstall_one`). Add `ProvisionItemFailed(SetforgeError)` to `errors.py`.
- [ ] Run → pass. `uv run mypy setforge/provision/protocol.py`. Commit.

### Task 2: Registry
**Files:** Create `setforge/provision/registry.py`; Test `tests/unit/test_provision_registry.py`
- [ ] Failing tests: `@register("x")` on a `Provisioner` subclass makes `build(ProvisionItem(type="x", …))` return an instance; a second `@register("x")` (dup) raises; `build` on an unknown type raises a clear `SetforgeError`. Assert registering does NOT require editing `registry.py` itself (the decorator is the only touch point).
- [ ] Run → fail. Implement decorator + module-level registry dict + `build`. Run → pass. mypy. Commit.

### Task 3: Install-receipt store
**Files:** Create `setforge/provision/receipt.py`; Test `tests/unit/test_provision_receipt.py`
- [ ] Failing tests (use `tmp_path`): write a receipt for an `Identity`+version+checksum, read it back; write is atomic (no partial file visible — assert via `atomicio`); a second write replaces; `probe`-style `installed()` returns the set of recorded identities; the receipt dir is DISTINCT from any `setforge.lock` path; writing item A then simulating a crash (don't write B) ⇒ next `installed()` still contains A (per-item durability).
- [ ] Run → fail. Implement `ReceiptStore` reusing `setforge/atomicio.py` (`atomic_write_text` + dir fsync) — do NOT hand-roll tmp+rename. Per-item write API (caller invokes after each success). Run → pass. mypy. Commit.

### Task 4: Reconcile driver
**Files:** Create `setforge/provision/driver.py`; Test `tests/unit/test_provision_driver.py`
- [ ] Failing tests using a tiny fake `Provisioner`: (a) `policy=REPORT` ⇒ `apply_one` never called (spy), result `.reported` True, zero receipt writes; (b) a fake whose `apply_one` raises `ProvisionItemFailed(kind=HARD)` for one item ⇒ that item recorded HARD, loop continues to others, `exit_code()==1`; (c) all-SKIP/empty delta ⇒ `exit_code()==0` and zero writes; (d) a `plan()` that tries to mutate is a spec violation — assert the driver itself performs no writes before the REPORT gate; (e) mixed SOFT+HARD ⇒ exit 1 (terminal `any(HARD)`), SOFT alone ⇒ 0.
- [ ] Run → fail. Implement `reconcile()` (probe → plan → single REPORT early-return gate → per-item apply loop with `except ProvisionItemFailed` containment → per-item receipt-after-success) and `exit_code()` (terminal `any(o.outcome is HARD)`). Run → pass. mypy. Commit.

### Task 5: Reference provisioner (proof + scaffold)
**Files:** Create `setforge/provision/reference.py`; Test `tests/unit/test_provision_reference.py`
- [ ] Failing tests — the end-to-end acceptance via `InMemoryProvisioner`: HARD⇒exit1; SOFT⇒exit0; REPORT⇒zero writes; 2nd identical run⇒`delta.is_empty()` + zero writes (INV-7); a marker-backed variant proves the receipt path (install → receipt written after success → re-run skips).
- [ ] Run → fail. Implement `InMemoryProvisioner` (registered `@register("reference")`), heavily commented as the copy-me scaffold, with a marker-backed mode. Run → pass. mypy. Commit.

### Task 6: INV-7 doc + package exports
**Files:** Modify `docs/RULES.md`; Create `setforge/provision/__init__.py`
- [ ] Add INV-7 to `docs/RULES.md` per spec §7 (exit-gated + idempotent; re-run empty delta ⇒ no writes; partial failure ⇒ nonzero exit; REPORT ⇒ zero writes). Find the existing INV-* list and match its format.
- [ ] `__init__.py` exports the public surface. `rg -q "INV-7" docs/RULES.md` passes. Commit.

### Final verification (orchestrator, before review)
- [ ] `uv run pytest tests/unit/test_provision_*.py -v` → all pass.
- [ ] `uv run pytest -q` → full suite green.
- [ ] `uv run mypy setforge/provision/` → clean.
- [ ] `pre-commit run --files setforge/provision/*.py setforge/errors.py docs/RULES.md` → clean.
