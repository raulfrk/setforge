# Mutation-testing baseline — merge/reconcile/store core

The nightly mutation gate (run by the home CI coordinator for the nightly
`full` trigger in `.github/workflows/nightly.yml`) requires a mutation score
strictly above 80% over this whole core: `scripts/mutmut_diff_gate.py --full`
over the results of a full `mutmut run`. Changes to core files are separately
gated by `scripts/mutmut_diff_gate.py`, which blocks unkilled mutants whose
functions overlap changed lines; run it by hand before merging a core change.
Survivors remain an assertion-gap backlog; they do not imply a 100% nightly
requirement.

## Headline

| metric | value |
|---|---|
| **Mutation score** | **91.32%** (full nightly gate on commit 2724c5d3, 2026-10-05) |
| 🙁 survived | 325 (11 allowlisted) |
| 🫥 skipped (no covering test in scope) | 10 |
| ⏰ timeout | 2 |

The figures are the complete baseline for the expanded scope below; the gate
log did not report the killed and generated counts. The previous baseline
(80.48%, 1101 killed ÷ 1368 scored, before the scope grew) is superseded.

Score = killed ÷ (killed + survived); skipped/timeout/suspicious are excluded
from the denominator (mutmut's standard reporting).

## Provenance

| field | value |
|---|---|
| measured | 2026-08-25 |
| base commit | `8e0577b` |
| tool | `mutmut 3.6.0` |
| mutated (`source_paths` + `only_mutate`) | the **8** core files below |
| test scope (`pytest_add_cli_args_test_selection`) | 9 focused per-module unit files **+ 2 hermetic integration suites** |
| observed runtime | about 10.8 minutes (2.29 mutations/second) |

## Scope & why the score is a conservative lower bound

Mutation is restricted to the merge/reconcile/store **core** (RFC §6), the
modules where "coverage ≠ assertion" historically let bugs through. The current
9 `only_mutate` files:

`scalar_merge · structural_merge · base_store · base_store_format ·
scalar_base_store · project_sync · project_record · reconcile_apply ·
reconcile/merge`

`project_record`, `reconcile_apply` and `reconcile/merge` joined the scope on
2026-10-04, when the retired `markdown_merge` and `yaml_merge` modules were
deleted. The first complete run on that scope (nightly gate, 2026-10-05) scored 91.32%.
Two allowlist entries (`project_sync.x_apply_sync__mutmut_433` and `_436`) are
stale: the gate only warns about them. Renumbering the allowlist and killing the
cheap survivors in `project_sync`, `reconcile_apply`, `structural_merge` and
`reconcile/merge` need a per-mutant survivor list from a full run.

The mutmut run executes a **sandbox-clean test selection** (see
`pyproject.toml [tool.mutmut]`), because mutmut runs the suite from a copied
`mutants/` sandbox and any test that reads uncopied repo files
(CHANGELOG/docs/migrations) fails there and aborts the run under mutmut's `-x`.

G4 adds `project_sync` and `tests/test_project_sync.py` to the enforced mutation
scope. G4's
pre-commit evidence is selective over its changed planner, compatibility,
conflict-policy, and transaction functions.

G6 also includes `tests/test_project_visibility.py` in the sandbox-clean test
selection so mutations in shared sync/visibility state handling are exercised
through the per-file CLI journeys.

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
any of the 8 `only_mutate` core files, so it has no kill power over core mutants
and would add only latency. It also drives the verbs **without** the
`integration_subprocess` guard (unmocked real subprocess), which is fragile in
the copied sandbox. Hermetic on paths, but no-kill-power dead weight — left out.

## Survivors by module

| module | survivors |
|---|---|
| structural_merge | 193 |
| yaml_merge | 4 |
| scalar_base_store | 27 |
| markdown_merge | 14 |
| base_store | 13 |
| scalar_merge | 10 |
| base_store_format | 6 |
| **total** | **267** |

`structural_merge` remains the dominant assertion-gap backlog. The five timeout
outcomes also occur there and are excluded from the score denominator.

## Regenerate / inspect

Run by hand (`|| true` is mandatory because survivors make `mutmut run` exit
nonzero):

```sh
uv run mutmut run || true        # full run (regenerates the mutants/ sandbox)
uv run mutmut results --all true # list every outcome, including killed
uv run python scripts/mutmut_diff_gate.py --full
uv run mutmut show <mutant-id>   # exact diff of one mutant, e.g. setforge.yaml_merge.x__deep_merge_dicts__mutmut_6
uv run mutmut browse             # interactive TUI over survivors
```

`mutants/` and `.mutmut-cache` are gitignored (run state; a stale copy serves
false results — `rm -rf mutants/ .mutmut-cache` for a clean re-baseline).
