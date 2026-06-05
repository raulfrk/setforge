"""Additive-only schema gate (p5qc.14.2).

Enforces the invariant that makes forward-tolerant reading safe: within a
major version, schema fields are only ever ADDED — never removed,
renamed, or retyped. The live models must match the frozen manifest
exactly; a removal/retype requires a major bump, an addition requires
recording in the manifest.
"""

from __future__ import annotations

import copy

from setforge.schema_manifest import (
    FROZEN_FIELD_MANIFEST,
    additivity_violations,
    live_field_manifest,
)


def test_live_schema_matches_frozen_manifest() -> None:
    """The shipped schema matches the frozen manifest exactly.

    If this fails: an ADDITION must be recorded in FROZEN_FIELD_MANIFEST;
    a REMOVAL/RETYPE requires a major schema_version bump.
    """
    violations = additivity_violations(FROZEN_FIELD_MANIFEST, live_field_manifest())
    assert violations == [], "schema drift:\n" + "\n".join(violations)


def test_simulated_same_major_removal_fails() -> None:
    live = copy.deepcopy(FROZEN_FIELD_MANIFEST)
    del live["Config"]["schema_version"]
    violations = additivity_violations(FROZEN_FIELD_MANIFEST, live)
    assert any("Config.schema_version: field removed" in v for v in violations)


def test_simulated_retype_fails() -> None:
    live = copy.deepcopy(FROZEN_FIELD_MANIFEST)
    live["Config"]["version"] = "<class 'str'>"  # was int
    violations = additivity_violations(FROZEN_FIELD_MANIFEST, live)
    assert any("Config.version: type changed" in v for v in violations)


def test_simulated_addition_flagged_until_recorded() -> None:
    live = copy.deepcopy(FROZEN_FIELD_MANIFEST)
    live["Config"]["new_field"] = "<class 'int'>"
    violations = additivity_violations(FROZEN_FIELD_MANIFEST, live)
    assert any("Config.new_field: field added" in v for v in violations)
