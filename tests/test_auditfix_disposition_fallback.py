"""Regression tests: structural span pins survive the MergeTypeMismatch fallback.

Audit finding ``disposition_fallback`` (Critical): when a YAML/JSONC file routes
through the structural engine but a shape clash anywhere in the file raises
:class:`~setforge.errors.MergeTypeMismatch`, ``resolve_file`` fell back to the
raw line-based merge WITHOUT the structural spans — silently dropping every
PINNED span (its deterministic live-wins guarantee) and emitting NO orphan
warning. Under ``--auto=use-tracked`` the line merge then overwrote the user's
pinned host-local value with the upstream value, with zero warning end-to-end.

The contract (Invariants I1 / I6) is preserve-or-warn for every pin: a pin is
never both un-honored AND un-warned. These tests lock that down across all auto
modes — before the fix they red-flag the silent drop.
"""

import io
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from setforge.config import Disposition
from setforge.disposition_merge import (
    FileResolution,
    StructuralSpanOrphanReason,
    resolve_file,
)
from setforge.markdown_merge import LineConflict
from setforge.section_wizard import ReconcileAuto
from setforge.spans import SpanEntry
from setforge.structural_merge import get_at_path

# A file whose ``clash`` key is a MAP on live/base but a SCALAR on tracked —
# the shape clash that forces merge_structural to raise MergeTypeMismatch, so
# resolve_file falls back to the line-based path. The PINNED span ``shared.token``
# diverges on both live (MYHOST) and tracked (upstream).
_BASE = "clash:\n  x: 1\nshared:\n  token: base\n"
_LIVE = "clash:\n  x: 2\nshared:\n  token: MYHOST\n"
_TRACKED = "clash: scalar\nshared:\n  token: upstream\n"


def _pin(anchor: str) -> SpanEntry:
    return SpanEntry.model_validate({"anchor": anchor, "kind": "pinned"})


def _yaml_value_at(text: str, path: str) -> object:
    return get_at_path(YAML(typ="rt").load(io.StringIO(text)), path)


@pytest.mark.parametrize(
    "auto",
    [ReconcileAuto.USE_TRACKED, ReconcileAuto.KEEP_LIVE, None],
)
def test_shape_mismatch_fallback_preserves_pinned_span(
    auto: ReconcileAuto | None,
) -> None:
    """The pinned live value survives the shape-clash line-based fallback.

    The core regression: under --auto=use-tracked the old code took tracked's
    ``upstream`` at the pinned line. In every auto mode the pin must hold the
    live value (MYHOST), and since the pin was honored there is NO orphan.
    """
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=_BASE,
        live=_LIVE,
        tracked=_TRACKED,
        auto=auto,
        structural_spans=[_pin("shared.token")],
    )
    assert isinstance(res, FileResolution)
    # Pin honored: live value wins regardless of auto mode (live-wins is the
    # whole point of a pin). Pre-fix, --auto=use-tracked yielded "upstream".
    assert _yaml_value_at(res.text, "shared.token") == "MYHOST"
    # Honored, so no orphan; and the file still merged as text (no raise).
    assert res.structural_span_orphans == []
    assert all(isinstance(c, LineConflict) for c in res.conflicts)


def test_shape_mismatch_fallback_warns_when_pin_cannot_be_reasserted() -> None:
    """A pin gone from live on the fallback path is PRESERVED + warned, not silent.

    When the pinned path no longer exists in live, it cannot be re-imposed —
    but the contract still forbids a silent drop: a STRUCTURAL_FALLBACK orphan
    is emitted so deploy warns the user (preserve-OR-warn, never both absent).
    """
    live_without_pin = "clash:\n  x: 2\n"  # shared.token deleted locally
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=_BASE,
        live=live_without_pin,
        tracked=_TRACKED,
        auto=ReconcileAuto.USE_TRACKED,
        structural_spans=[_pin("shared.token")],
    )
    assert [(o.anchor, o.reason) for o in res.structural_span_orphans] == [
        ("shared.token", StructuralSpanOrphanReason.STRUCTURAL_FALLBACK)
    ]


def test_shape_mismatch_fallback_never_both_absent() -> None:
    """The invariant directly: the pin is honored in text OR named in an orphan.

    A pin is never both un-honored (live value missing from the result) AND
    un-warned (no orphan). This is the exact failure mode the finding cites:
    pre-fix, with --auto=use-tracked, the live value was gone from res.text AND
    structural_span_orphans was empty.
    """
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=_BASE,
        live=_LIVE,
        tracked=_TRACKED,
        auto=ReconcileAuto.USE_TRACKED,
        structural_spans=[_pin("shared.token")],
    )
    honored = _yaml_value_at(res.text, "shared.token") == "MYHOST"
    warned = any(o.anchor == "shared.token" for o in res.structural_span_orphans)
    assert honored or warned


def test_shape_mismatch_fallback_jsonc_preserves_pinned_span() -> None:
    """Same guarantee on the JSONC structural format, not just YAML."""
    base = '{\n  "clash": {"x": 1},\n  "shared": {"token": "base"}\n}\n'
    live = '{\n  "clash": {"x": 2},\n  "shared": {"token": "MYHOST"}\n}\n'
    tracked = '{\n  "clash": "scalar",\n  "shared": {"token": "upstream"}\n}\n'
    res = resolve_file(
        Disposition.SHARED,
        Path("c.json"),
        base=base,
        live=live,
        tracked=tracked,
        auto=ReconcileAuto.USE_TRACKED,
        structural_spans=[_pin("shared.token")],
    )
    honored = '"MYHOST"' in res.text or '"token": "MYHOST"' in res.text
    warned = any(o.anchor == "shared.token" for o in res.structural_span_orphans)
    assert honored or warned
