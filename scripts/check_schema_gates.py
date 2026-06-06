#!/usr/bin/env python3
"""Non-bypassable CI schema gates for the setforge config contract.

A STANDALONE script (NOT a pytest test — pytest is skippable via markers
and ``addopts``, which would silently disarm the contract). It imports
the LIVE engine modules and runs three gates that protect the
forward-tolerant / additive-first guarantees in ``COMPATIBILITY.md``:

1. migration-coverage — ``current_expected_schema_version`` must have a
   bridging chain in :data:`MIGRATIONS` (so an upgrade can actually reach
   the version this build expects).
2. field-removal — the live Pydantic schema must not drop or retype a
   frozen field within the major version (reuses
   :func:`additivity_violations`).
3. reverse-required — every registered migration must carry a correctly
   swapped ``reverse`` (reuses :func:`_validate_registry`, which ACCESSES
   ``.reverse``; a ``runtime_checkable`` ``isinstance`` would NOT enforce
   property presence).

Each gate is a small importable function returning a list of
human-readable violation strings (empty == clean). :func:`main` is a thin
wrapper that prints every violation and ``sys.exit(1)`` on ANY, ``0`` when
all gates are clean.

Invocation::

    uv run python scripts/check_schema_gates.py

Wired as its own CI step (see ``.github/workflows/ci.yml``) with no
``continue-on-error``, so a violation is a hard CI failure.

Known seed-gate limitation: a brand-new Pydantic model absent from
:data:`setforge.schema_manifest._MODELS` is invisible to the field-removal
gate (the manifest can only compare models it enumerates). Fixing that
is out of scope here; it is deferred to a contract bead.
"""

from __future__ import annotations

import sys

from setforge.errors import ConfigError
from setforge.migrations import (
    _DEFAULT_SCHEMA_VERSION,
    _validate_registry,
    current_expected_schema_version,
    find_migration_path,
    parse_schema_version,
)
from setforge.schema_manifest import (
    FROZEN_FIELD_MANIFEST,
    additivity_violations,
    live_field_manifest,
)


def gate_migration_coverage(
    *,
    baseline: str = _DEFAULT_SCHEMA_VERSION,
    expected: str = current_expected_schema_version,
) -> list[str]:
    """Fail unless ``expected`` is reachable from ``baseline`` via the registry.

    Resolves the chain with :func:`find_migration_path`, which reads the
    module-level :data:`setforge.migrations.MIGRATIONS`; a unit test that
    wants a synthetic registry monkeypatches that global (mirroring
    ``tests/test_migrations.py``), so the live invocation here always uses
    the real registry.

    ``find_migration_path`` returns ``()`` for BOTH "no bridge needed
    (versions equal)" AND "unreachable". The two cases are
    indistinguishable from the empty tuple alone, so the equal-version
    case is short-circuited FIRST: when ``parse_schema_version(baseline)
    == parse_schema_version(expected)`` the empty path is VALID and the
    gate passes. Only then is a non-empty path required, AND its last
    step's ``to_version`` must land on ``expected`` specifically (a chain
    that bridges to some *other* version is not coverage).

    All comparison goes through :func:`parse_schema_version` →
    ``(int, int)`` so 1.10 sorts above 1.9 (a string compare gets this
    wrong). A malformed version raises :class:`ConfigError` (never a bare
    ``ValueError`` / ``IndexError``), surfaced to the caller.

    ``baseline`` / ``expected`` are injectable for unit testing; the
    defaults pull the live build's baseline and expected version.
    """
    if parse_schema_version(baseline) == parse_schema_version(expected):
        # No bump pending — the empty path is the correct, valid answer.
        return []

    path = find_migration_path(from_v=baseline, to_v=expected)
    if not path:
        return [
            f"migration-coverage: no chain in MIGRATIONS bridges baseline "
            f"{baseline!r} → expected {expected!r}; append the migration that "
            f"reaches {expected!r} (or revert the current_expected_schema_version "
            f"bump)."
        ]
    if parse_schema_version(path[-1].to_version) != parse_schema_version(expected):
        return [
            f"migration-coverage: the chain from baseline {baseline!r} ends at "
            f"{path[-1].to_version!r}, not the expected {expected!r}; the "
            f"registry does not reach the version this build expects."
        ]
    return []


def gate_field_removal(
    *,
    frozen: dict[str, dict[str, str]] | None = None,
    live: dict[str, dict[str, str]] | None = None,
) -> list[str]:
    """Fail on any additive-only violation between frozen and live schema.

    Reuses :func:`additivity_violations` verbatim (blanket-forbid: a
    marker-aware allowance is explicitly out of scope, deferred to a
    contract bead). ``frozen`` defaults to :data:`FROZEN_FIELD_MANIFEST` and
    ``live`` to the live Pydantic models; both are injectable (via the
    ``None`` sentinel, not a mutable default) so a test can simulate a
    removal on a deep-copied manifest.

    Note: this gate compares only models the manifest enumerates; a
    brand-new model absent from ``_MODELS`` is invisible to it (the
    seed-gate limitation documented in the module docstring).
    """
    if frozen is None:
        frozen = FROZEN_FIELD_MANIFEST
    if live is None:
        live = live_field_manifest()
    return additivity_violations(frozen, live)


def gate_reverse_required() -> list[str]:
    """Fail if any registered migration lacks a correctly-swapped reverse.

    Reuses :func:`_validate_registry`, which ACCESSES ``.reverse`` and
    checks the from/to swap — a ``runtime_checkable`` ``isinstance`` does
    NOT enforce property presence, so ``isinstance`` is deliberately not
    used. ``_validate_registry`` raises :class:`ConfigError` on a bad
    reverse; it is caught here and surfaced as a gate violation rather
    than a traceback.
    """
    try:
        _validate_registry()
    except ConfigError as err:
        return [f"reverse-required: {err}"]
    return []


def run_all_gates() -> list[str]:
    """Run all three gates against the live tree; return aggregated violations.

    A malformed live ``schema_version`` raises :class:`ConfigError` out of
    the coverage gate; that is itself a contract failure, so it is caught
    and reported as a violation rather than crashing the runner.
    """
    violations: list[str] = []
    try:
        violations.extend(gate_migration_coverage())
    except ConfigError as err:
        violations.append(f"migration-coverage: {err}")
    violations.extend(gate_field_removal())
    violations.extend(gate_reverse_required())
    return violations


def main() -> int:
    """Run every gate; print violations and return a process exit code."""
    violations = run_all_gates()
    if violations:
        print("CI schema gates FAILED:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1
    print(
        "CI schema gates passed: migration-coverage, field-removal, reverse-required."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
