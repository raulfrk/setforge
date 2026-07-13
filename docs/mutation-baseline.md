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

> **STALE headline — the 2026-06-24 numbers above predate two changes:** (1)
> the legacy disposition/sections/spans subsystem was retired, dropping
> `only_mutate` from 10 files to 7 (see below); (2) the test selection was
> broadened with two hermetic integration suites (this change). The count-drop
> re-baseline is **DEFERRED to the merge gate** — see "Regenerate" below. The
> per-module survivor table further down is likewise pre-retirement and no
> longer authoritative.

## Provenance

| field | value |
|---|---|
| measured | 2026-06-24 (headline; **stale** — see note above) |
| base commit | `3fafc26` |
| tool | `mutmut` (see the pinned dev extra) |
| mutated (`source_paths` + `only_mutate`) | the **7** core files below |
| test scope (`pytest_add_cli_args_test_selection`) | 9 focused per-module unit files **+ 2 hermetic integration suites** |

## Scope & why the score is a conservative lower bound

Mutation is restricted to the merge/reconcile/store **core** (RFC §6), the
modules where "coverage ≠ assertion" historically let bugs through. The current
7 `only_mutate` files:

`markdown_merge · scalar_merge · structural_merge · yaml_merge · base_store ·
base_store_format · scalar_base_store`

The mutmut run executes a **sandbox-clean test selection** (see
`pyproject.toml [tool.mutmut]`), because mutmut runs the suite from a copied
`mutants/` sandbox and any test that reads uncopied repo files
(CHANGELOG/docs/migrations) fails there and aborts the run under mutmut's `-x`.

### Broadened selection (this change)

The core is reached at runtime through `deploy.py` / `reconcile/` /
`transitions.py` / `stage.py`, which the focused per-module unit tests exercise
directly but the real **install / sync / compare / revert / stage** verbs also
drive end-to-end. Previously those integration paths were out of the mutmut
scope, so some survivors were **FALSE survivors** — killed only by integration
tests not in the set. The selection now adds the two sandbox-clean integration
suites so those mutants are scored killed:

- **`tests/integration/test_verbs.py`** — per-verb install/sync/compare/revert/
  stage/plugin/ext/etc.
- **`tests/integration/test_smoke.py`** — install against a real-git source.

Both are hermetic: the `integration_env` fixture (in the copied
`tests/integration/conftest.py`) synthesizes the config repo under `tmp_path`,
hardens git (devnull global/system config, no network protocols), and mocks
`claude`/`code`/`gitleaks` behind the `integration_subprocess` no-leak guard —
no `Path(__file__)`/`REPO_ROOT`/repo-file reads, no root-conftest dependency
(the root `conftest.py` is not copied into the sandbox and holds no fixtures).

Broadening alone regressed the `structural_merge.is_structural` predicate: the
integration suites *execute* it but never assert its bool, so its 6 mutants
flipped `no-test` → survived. Targeted unit tests in
`tests/test_structural_merge.py` now pin the predicate's True/False on each
branch (`.json`/`.yaml`/`.yml` → True, non-structural → False), killing all 6.

**Excluded — `tests/integration/test_lockflow.py`:** it exercises the lock /
provision path (`setforge.provision.*`, `lockfile`), which does **not** import
any of the 7 `only_mutate` core files, so it has no kill power over core mutants
and would add only latency. It also drives the verbs **without** the
`integration_subprocess` guard (unmocked real subprocess), which is fragile in
the copied sandbox. Hermetic on paths, but no-kill-power dead weight — left out.

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
