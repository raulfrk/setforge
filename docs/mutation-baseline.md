# Mutation-testing baseline — merge/reconcile/store core

> **ADVISORY — NOT ENFORCED.** This is a recorded baseline, not a gate. Nothing
> in CI or pre-commit fails on this score. A `>80%` mutation-score gate (CI
> diff-mode on PR + full nightly) is wired separately once the test
> audit-and-prune lands. Treat the survivor list below as a backlog of
> assertion gaps to investigate, not a pass/fail signal.

## Headline

| metric | value |
|---|---|
| **Mutation score** | **≈79.0%** (1914 killed ÷ 2423 tested) |
| Mutants generated | 2517 |
| 🎉 killed | 1914 |
| 🙁 survived | 509 |
| 🫥 skipped (no covering test in scope) | 94 |
| ⏰ timeout / 🤔 suspicious | 0 / 0 |

Score = killed ÷ (killed + survived); skipped/timeout/suspicious are excluded
from the denominator (mutmut's standard reporting).

## Provenance

| field | value |
|---|---|
| measured | 2026-06-24 |
| branch | `setforge-deoq.2.3-e3e4` (base commit `3fafc26`) |
| tool | `mutmut==3.6.0` |
| mutated (`source_paths` + `only_mutate`) | the 10 core files below |
| test scope (`pytest_add_cli_args_test_selection`) | the 15 focused per-module unit files |

## Scope & why the score is a conservative lower bound

Mutation is restricted to the merge/reconcile/store **core** (RFC §6), the
modules where "coverage ≠ assertion" historically let bugs through:

`disposition_merge · markdown_merge · scalar_merge · structural_merge ·
yaml_merge · section_reconcile · base_store · base_store_format ·
scalar_base_store · spans_store`

The mutmut run executes only the **focused per-module unit tests** (see
`pyproject.toml [tool.mutmut]`), because mutmut runs the suite from a copied
`mutants/` sandbox and the broader suite's repo-file-dependent tests
(CHANGELOG/docs/migrations) fail there and abort the run. Consequently
**integration tests that also exercise the core (install / capture / auditfix)
are out of the mutmut test scope**, so a number of the 509 survivors are in
fact killed by tests not included here. The true score is therefore **≥ 79%**;
this baseline is a deliberate lower bound, and the per-module survivor counts
are upper bounds on the real gaps.

## Survivors by module

| module | survivors |
|---|---|
| structural_merge | 182 |
| disposition_merge | 137 |
| yaml_merge | 56 |
| section_reconcile | 34 |
| spans_store | 31 |
| scalar_base_store | 27 |
| markdown_merge | 13 |
| base_store | 13 |
| scalar_merge | 10 |
| base_store_format | 6 |
| **total** | **509** |

The two biggest (`structural_merge`, `disposition_merge`) are also the largest
modules and the ones most exercised by integration tests excluded from this
run — expect their real gap to shrink most under the full suite.

## Regenerate / inspect

Non-gating; run by hand (the `|| true` is mandatory — `mutmut run` exits
nonzero when mutants survive):

```sh
uv run mutmut run || true        # full run (regenerates the mutants/ sandbox)
uv run mutmut results            # list killed/survived per mutant
uv run mutmut show <mutant-id>   # exact diff of one mutant, e.g. setforge.yaml_merge.x__deep_merge_dicts__mutmut_6
uv run mutmut browse             # interactive TUI over survivors
```

`mutants/` and `.mutmut-cache` are gitignored (run state; a stale copy serves
false results — `rm -rf mutants/ .mutmut-cache` for a clean re-baseline).
