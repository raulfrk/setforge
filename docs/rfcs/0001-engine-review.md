<!-- NOTE: bead IDs (deoq.*) are intentional in this doc — they're pointers to the
     implementation work, kept by author decision. Do NOT scrub them as "leaks". -->
# Engine review — RFC 0001 work to date (deoq.7)

**Status: APPROVED 2026-06-21** (reviewed via the Atelier served-annotation surface — dogfooding it, per `deoq.7`). Deep multi-agent audit (6 parallel auditors) of all engine work merged toward the overhaul, against [RFC 0001](0001-setforge-overhaul.md).

## Scope

Two beads shipped engine code from the RFC and are the subject of this review:

- **F1 — `docs/RULES.md`** (`deoq.3.1`)
- **E1 — the Hypothesis test harness** `tests/harness/` (`deoq.2.1`)

Everything else in the RFC (the reconciliation/provisioning cores A/B, the UX framework D, the rest of the spine F2a/F5/F7, E2) is **designed but not yet built**. The RFC design doc itself is out of scope here.

## Verdict

**RFC design-complete, build just-started.** The engine *primitives* that exist are mature and well-tested (`atomicio`, the two-phase crash-safe `transitions`, type-aware `structural_merge`, plugin-reconcile that already honors the RFC by never writing `enabledPlugins`, refuse-before-write install, orphan-overreach defenses). But the overhaul's defining 2.0 surface is unbuilt, and much merged code is the *legacy* model the RFC sets out to replace (and shares vocabulary with it — "3-way", "base", "conflict" — so a naive grep over-reports progress).

## What we built from the RFC, and why

### F1 — `docs/RULES.md` (deoq.3.1)
The single source-of-truth rule index (INV-1..10, UX/SAFE/PY/PROV/SELF), each rule with a stable ID + a **DETERMINISTIC/ADVISORY** tag + an *Enforced-by* mechanism column; plus a Part-2 testing rubric (tier guide, invariant-ownership map, coverage/mutation bars).

**Why this shape:** one place humans + LLM review agents + lints all reference (RFC §5 wires it into session-flow). The DETERMINISTIC-vs-ADVISORY split is load-bearing: a DETERMINISTIC rule must have a blocking mechanism (lint/gate/test); ADVISORY only surfaces in the review fan / human gate — to avoid false-positive fatigue. Every rule names its enforcer.

**Maturity caveat:** the document is complete, but 5 cited lints (`wizard-letter-ban`, `theme-hardcode-ban`, `theme-256-completeness`, `shell=True`-ban, `legacy-API-ban`) don't exist yet — they are the next bead (F2a / `deoq.3.2`). So RULES.md currently *documents* enforcement that F2a will make real.

### E1 — the Hypothesis harness (deoq.2.1)
`strategies.py` (input generators), `invariants.py` (the `@invariant` helper + `InvariantStateMachine`), `model.py` (`StubReconcileModel` — a thin reconcile seam), `conftest.py` (fixtures incl. `mock_cli_subprocess`), `test_harness_meta.py` (meta-tests).

**Why this shape:**
- **Built before the engine, on purpose** (spine-first, RFC §4/§13): invariants are authored per component and "guard from day 1", so the machine must exist before A/B land.
- **A stub model, not the real CLI, by design:** the real `base/`+`local/`+`index/` store + line-level 3-way don't exist yet. The stub mirrors their verb surface (install/sync/revert/migrate) + store layout, so swap-in later is mechanical (replace each `_engine_*` body); the public verbs/accessors are the stable seam invariants assert against.
- **One decorator, two run modes:** `@invariant()` is both a harness invariant and Hypothesis's native one, so a method runs inside the stateful machine and standalone — the meta-tests use the standalone path to *prove* it actually fails on a broken invariant (defends "coverage ≠ assertion").
- **`mock_cli_subprocess` refuses unregistered shell-outs** — an unexpected subprocess fails loudly, never escapes to the host.

**Maturity caveat:** the machinery is sound + self-verifying, but the shipped invariants are placeholders — the real INV-1..10 catalog is the next testing bead (E2 / `deoq.2.2`). So E1 proves the scaffold runs, not yet any setforge property; INV-1/INV-2 (the silent-data-loss killers) have the longest path to becoming real.

## Findings

### CRITICAL
1. **Silent overwrite still possible** — `cli/_install_helpers.py` `_check_unexpected_drift` rejects only `mode_drift`, not diff-only content drift, so a `disposition: None` file with edited live content is deployed verbatim over the user's edits with no conflict prompt. The exact F1/F2 data-loss class the overhaul exists to kill — open for plain files until A2 (`deoq.4.3`) lands.
2. **`local.yaml` writes are not atomic** — `cli/override.py` `_dump_local_data` uses a plain `open("w")` + dump; a torn write loses all host-local intent. Fix: route through `atomicio.atomic_write_text`.
3. **RULES.md cites 5 lints that don't exist** — documents enforcement that isn't there (false confidence). Ship them (F2a / `deoq.3.2`) or re-tag the rules "planned".
4. **SAFE-2 (legacy-API-ban) is un-armable as written** — it bans the currently-shipping legacy APIs; needs a "new-code-only / post-migration" scope.

### IMPORTANT
- **Deploy-then-record gap** (`transitions.py`): a crash between live deploy and the transition commit leaves changes with no revert record.
- **Revert TOCTOU** (`cli/revert.py`): selection (`load_latest`) happens before the profile lock.
- **Multi-step `--to-before` revert is fail-fast, not resumable** — contradicts RFC §11's interrupt-safe promise.
- **Overlay re-capture mis-slices** (`cli/_detect_helpers.py` `_extract_live_body`) with >1 overlay / net line delta → silent corruption; `body_file` overlay re-capture corrupts the payload.
- **Duplicate JSON keys silently collapse to first-match** (`structural_merge.py`); no recursion-depth/size bound on merges.
- **No uniform provisioner protocol** — cargo soft-fails, MCP hard-gates, plugins separate; cargo idempotency probe re-installs on probe failure (INV-7 churn). B1 (`deoq.5.1`) fixes this.
- **Coverage gate contradiction** — RULES.md says 85% unit-only; `pyproject.toml` enforces 83% whole-suite; no unit/integration markers exist.
- **Harness invariants are placeholders** (E1 scaffold); INV-1/INV-2 asserted by nothing yet.

### Divergences
- `conflict_wizard.py` is a `[k]/[t]/[e]/[s]` letter menu — violates SETTLED §8.1 "no letter menus". Replace, don't extend.
- Land the new engine under fresh module names so progress-tracking can't false-positive on shared vocabulary.

## Roadmap — done → next (spine-first)

```
DONE        E1 harness (deoq.2.1) · F1 RULES.md (deoq.3.1)
SPINE next  F2a policy/AST lints (deoq.3.2, ready) → F5 enforce-tests (deoq.3.5) → F7 self-improve (deoq.3.7)
UX          D theme + button widget (deoq.1) — shared dep for every A/B wizard
ENGINE A    A1 store base/local/index (deoq.4.2) → A2 line-level 3-way merge (deoq.4.3)   [closes CRITICAL #1]
PROVISION   B1 provisioner protocol + uniform reconcile (deoq.5.1)
INVARIANTS  E2 INV-1..10 stateful machine (deoq.2.2) — author INV-1/INV-2 the moment A1/A2 land
```

**Suggested order:** `3.2 → 3.5 → 3.7 → D(deoq.1) → 4.2 → 4.3 → 5.1 → 2.2`.

**Quick wins (independent):** make `local.yaml` atomic (CRITICAL #2); arm the 5 RULES.md lints (deoq.3.2); add a `disposition:None` content-drift warning so CRITICAL #1 at least surfaces before A2; reconcile the coverage-gate number/scope (85 vs 83, add unit/integration markers).
