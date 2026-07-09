#!/usr/bin/env python3
"""E2E audit-verdict-manifest gate for the Docker suite.

A STANDALONE script (NOT a pytest test — pytest is skippable via markers
and ``addopts``, which would silently disarm the contract; same reasoning
as :mod:`scripts.check_schema_gates` and :mod:`scripts.check_policy_lints`).

It keeps ``tests/docker/e2e_verdicts.toml`` — the audit-and-prune verdict
manifest — in lock-step with the REAL collected e2e test set, so a test
can never be added/renamed/removed without an accompanying verdict row.
Five gates:

1. set-equality — every collected node id has a manifest row AND every
   manifest row names a still-collected id (checked in BOTH directions,
   never a length compare, which would mask a dup+gap).
2. uniqueness — no node id appears twice in the manifest.
3. superseded-by-liveness — every ``delete``/``merge`` row's
   ``superseded_by`` ids still exist in the collected set (no
   mutual-deletion pair where the sibling that "covers" a pruned test is
   itself pruned).
4. verb-smoke-coverage — every setforge verb has >=1 ``smoke: true``
   manifest row, cross-checked against the REAL ``-m 'e2e_docker and
   smoke'`` collection so a typo'd ``@pytest.mark.smok`` (a silent no-op)
   cannot satisfy the gate on the manifest side alone.
5. fail-closed — a nonzero collect exit OR an empty collected set is a
   hard failure (never a vacuous pass; the default ``addopts`` excludes
   ``e2e_docker`` via ``-m 'not e2e_docker'``, so collection MUST pass an
   explicit ``-m e2e_docker`` or it silently sees zero tests).

Exit ``0`` clean / ``1`` on any violation (offending ids printed to
stderr).

Invocation::

    uv run python scripts/check_e2e_manifest.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "tests" / "docker" / "e2e_verdicts.toml"

# A collected node id is a non-blank, whitespace-free path prefix followed by
# ``::`` (``path::[Class::]func[...]``). Anchoring on this shape (rather than a
# bare ``"::" in line`` substring test) means a stray warning/banner/summary
# line that merely CONTAINS ``::`` — e.g. a deprecation notice citing a
# dotted-and-colon'd symbol — cannot be mis-parsed as a node id.
_NODE_ID_RE = re.compile(r"^\S+::")

VALID_VERDICTS: frozenset[str] = frozenset(
    {"keep", "change", "merge", "delete", "should-be-integration"}
)
PRUNE_VERDICTS: frozenset[str] = frozenset({"delete", "merge"})

# Every setforge verb (+ the two cross-cutting UX surfaces) that MUST retain
# at least one smoke-tagged golden-path e2e test. Keyed to the ``verbs`` list
# each manifest row declares, so the smoke-coverage gate is verb-aware rather
# than counting a bare total.
REQUIRED_SMOKE_VERBS: frozenset[str] = frozenset(
    {
        "install",
        "sync",
        "compare",
        "revert",
        "validate",
        "init",
        "migrate",
        "upgrade",
        "secrets",
        "reconcile",
    }
)


def _collect_node_ids(*, extra_marker_expr: str = "e2e_docker") -> list[str]:
    """Return the REAL collected node-id list for a marker expression.

    Runs ``pytest --collect-only -q`` with an EXPLICIT ``-m`` (overriding the
    default ``addopts`` ``-m 'not e2e_docker'`` exclude — without it collection
    sees zero e2e tests and the whole gate passes vacuously) and ``--no-cov``
    (the coverage plugin otherwise appends a term-missing table that pollutes
    the node-id parse) and ``-p no:cacheprovider`` (suppresses the cache
    banner). Only lines matching :data:`_NODE_ID_RE` (``^\\S+::``) are kept, so
    a warning/banner/summary line that merely contains ``::`` cannot be
    mis-parsed as a node id.

    FAIL-CLOSED: a nonzero pytest exit raises, surfaced by the caller as a hard
    gate failure — never a silent empty list.
    """
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pytest",
            "--collect-only",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            "-m",
            extra_marker_expr,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pytest --collect-only -m {extra_marker_expr!r} exited "
            f"{proc.returncode} (fail-closed):\n{proc.stdout}\n{proc.stderr}"
        )
    return [
        stripped
        for line in proc.stdout.splitlines()
        if _NODE_ID_RE.match(stripped := line.strip())
    ]


def _load_manifest() -> dict[str, dict[str, object]]:
    """Parse the verdict manifest; return its ``[tests]`` node-id → row table."""
    with MANIFEST_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    tests = data.get("tests")
    if not isinstance(tests, dict):
        raise RuntimeError(
            f"{MANIFEST_PATH} has no [tests] table — the manifest is empty or malformed"
        )
    return tests


def gate_collect_nonempty(collected: list[str]) -> list[str]:
    """Fail-closed: an empty collected set is never a valid clean pass."""
    if len(collected) == 0:
        return [
            "fail-closed: pytest collected ZERO e2e_docker tests — the marker "
            "expression or default addopts exclude is masking the suite; refusing "
            "to pass vacuously"
        ]
    return []


def gate_row_shape(manifest: dict[str, dict[str, object]]) -> list[str]:
    """Every row must carry a valid ``verdict``, a bool ``smoke``, a ``signal``,
    and a ``verbs`` list; prune verdicts additionally require ``superseded_by``."""
    out: list[str] = []
    for node_id, row in manifest.items():
        verdict = row.get("verdict")
        if verdict not in VALID_VERDICTS:
            out.append(
                f"row-shape: {node_id!r} has verdict {verdict!r}, "
                f"not one of {sorted(VALID_VERDICTS)}"
            )
        if not isinstance(row.get("smoke"), bool):
            out.append(f"row-shape: {node_id!r} 'smoke' must be a bool")
        if not isinstance(row.get("signal"), str) or not row.get("signal"):
            out.append(f"row-shape: {node_id!r} 'signal' must be a non-empty string")
        if not isinstance(row.get("verbs"), list) or not row.get("verbs"):
            out.append(f"row-shape: {node_id!r} 'verbs' must be a non-empty list")
        if verdict in PRUNE_VERDICTS:
            sup = row.get("superseded_by")
            if not isinstance(sup, list) or not sup:
                out.append(
                    f"row-shape: {node_id!r} verdict {verdict!r} requires a non-empty "
                    f"'superseded_by' list naming the sibling(s) that assert the "
                    f"same behavior"
                )
    return out


def gate_set_equality(
    collected: list[str], manifest: dict[str, dict[str, object]]
) -> list[str]:
    """Collected and manifest node-id sets must match in BOTH directions."""
    collected_set = set(collected)
    manifest_set = set(manifest)
    out: list[str] = []
    missing_rows = sorted(collected_set - manifest_set)
    for node_id in missing_rows:
        out.append(
            f"set-equality: collected id has NO manifest row (add a verdict): {node_id}"
        )
    stale_rows = sorted(manifest_set - collected_set)
    for node_id in stale_rows:
        out.append(
            f"set-equality: manifest row names a NON-collected id "
            f"(rename/remove it): {node_id}"
        )
    return out


def gate_uniqueness(collected: list[str]) -> list[str]:
    """Node ids must be unique (a dup+gap would net to the same COUNT)."""
    seen: set[str] = set()
    dups: set[str] = set()
    for node_id in collected:
        if node_id in seen:
            dups.add(node_id)
        seen.add(node_id)
    return [f"uniqueness: node id collected more than once: {d}" for d in sorted(dups)]


def gate_superseded_by_live(
    collected: list[str], manifest: dict[str, dict[str, object]]
) -> list[str]:
    """Every prune row's ``superseded_by`` id must still be collected.

    Guards the mutual-deletion trap: a ``delete``/``merge`` row may only cite a
    sibling that is ITSELF still present (and thus still asserting the shared
    behavior). A superseded_by pointing at a since-removed id is a hard failure.
    """
    collected_set = set(collected)
    out: list[str] = []
    for node_id, row in manifest.items():
        if row.get("verdict") not in PRUNE_VERDICTS:
            continue
        superseded = row.get("superseded_by", [])
        if not isinstance(superseded, list):
            continue
        for sup in superseded:
            if sup not in collected_set:
                out.append(
                    f"superseded-by: {node_id!r} is marked "
                    f"{row.get('verdict')!r} but its superseded_by target "
                    f"{sup!r} is NOT in the collected set (mutual-deletion / stale "
                    f"reference)"
                )
    return out


def gate_verb_smoke_coverage(
    smoke_collected: list[str], manifest: dict[str, dict[str, object]]
) -> list[str]:
    """Every required verb must have >=1 REAL smoke-collected golden-path row.

    Cross-checks the manifest's ``smoke = true`` rows against the id set that
    pytest ACTUALLY collects under ``-m 'e2e_docker and smoke'`` — so a
    manifest row claiming ``smoke = true`` whose test lacks a real (or has a
    typo'd) ``@pytest.mark.smoke`` cannot satisfy the gate, and vice versa.
    """
    smoke_set = set(smoke_collected)
    out: list[str] = []

    manifest_smoke_ids = {
        node_id for node_id, row in manifest.items() if row.get("smoke") is True
    }
    # The manifest's smoke flag and the real @pytest.mark.smoke collection must
    # agree exactly — either drift means a mislabeled row or a typo'd marker.
    claimed_but_not_marked = sorted(manifest_smoke_ids - smoke_set)
    for node_id in claimed_but_not_marked:
        out.append(
            f"verb-smoke: manifest marks {node_id!r} smoke=true but it is NOT "
            f"collected under -m 'e2e_docker and smoke' (missing/typo'd "
            f"@pytest.mark.smoke?)"
        )
    marked_but_not_claimed = sorted(smoke_set - manifest_smoke_ids)
    for node_id in marked_but_not_claimed:
        out.append(
            f"verb-smoke: {node_id!r} carries @pytest.mark.smoke but the manifest "
            f"row is not smoke=true (out-of-sync)"
        )

    # Every required verb must be covered by at least one REAL smoke id.
    covered_verbs: set[str] = set()
    for node_id in smoke_set & manifest_smoke_ids:
        verbs = manifest[node_id].get("verbs", [])
        if isinstance(verbs, list):
            covered_verbs.update(str(v) for v in verbs)
    for verb in sorted(REQUIRED_SMOKE_VERBS - covered_verbs):
        out.append(
            f"verb-smoke: verb {verb!r} has NO smoke-collected golden-path test "
            f"(add @pytest.mark.smoke to one and set smoke=true on its row)"
        )
    return out


def run_all_gates() -> list[str]:
    """Run every gate against the live tree; return aggregated violations."""
    try:
        collected = _collect_node_ids(extra_marker_expr="e2e_docker")
    except RuntimeError as err:
        return [str(err)]

    violations = gate_collect_nonempty(collected)
    if violations:
        # No point running set gates against a vacuous collection.
        return violations

    try:
        manifest = _load_manifest()
    except (OSError, RuntimeError, tomllib.TOMLDecodeError) as err:
        return [f"manifest-load: {err}"]

    violations.extend(gate_row_shape(manifest))
    violations.extend(gate_uniqueness(collected))
    violations.extend(gate_set_equality(collected, manifest))
    violations.extend(gate_superseded_by_live(collected, manifest))

    try:
        smoke_collected = _collect_node_ids(extra_marker_expr="e2e_docker and smoke")
    except RuntimeError as err:
        return [*violations, f"smoke-collect: {err}"]
    violations.extend(gate_verb_smoke_coverage(smoke_collected, manifest))
    return violations


def main() -> int:
    """Run every gate; print violations and return a process exit code."""
    violations = run_all_gates()
    if violations:
        print("E2E verdict-manifest gate FAILED:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1
    print(
        "E2E verdict-manifest gate passed: set-equality, uniqueness, "
        "superseded-by-liveness, verb-smoke-coverage, fail-closed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
