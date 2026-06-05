"""Tests for span-aware expected-drift classification in compare (Stage 9).

Invariant I13: a SHARED file whose only divergence lives inside a
pinned/forked span reports as EXPECTED drift, so a deliberate sub-span
override does not render identically to unsynced shared drift.
"""

from pathlib import Path

from setforge.compare import CompareStatus, FileCompare, _span_only_drift
from setforge.config import Disposition, TrackedFile

_DOC = """\
# Title

## Pinned

Pinned body original.

## Shared

Shared body original.
"""


def _tracked_file(spans: list[dict[str, str]]) -> TrackedFile:
    return TrackedFile.model_validate(
        {
            "src": "doc.md",
            "dst": "~/.x/doc.md",
            "disposition": "shared",
            "spans": spans,
        }
    )


def test_span_only_drift_true_when_drift_inside_span(tmp_path: Path) -> None:
    src = tmp_path / "doc.md"
    src.write_text(_DOC, encoding="utf-8")
    dst = tmp_path / "live.md"
    dst.write_text(
        _DOC.replace("Pinned body original.", "Pinned body LIVE."), encoding="utf-8"
    )
    tf = _tracked_file([{"anchor": "## Pinned", "kind": "pinned"}])
    assert _span_only_drift(src, dst, tf) is True


def test_span_only_drift_false_when_drift_outside_span(tmp_path: Path) -> None:
    src = tmp_path / "doc.md"
    src.write_text(_DOC, encoding="utf-8")
    dst = tmp_path / "live.md"
    # Drift in the SHARED (non-span) region -> not span-only.
    dst.write_text(
        _DOC.replace("Shared body original.", "Shared body LIVE."), encoding="utf-8"
    )
    tf = _tracked_file([{"anchor": "## Pinned", "kind": "pinned"}])
    assert _span_only_drift(src, dst, tf) is False


def test_span_only_drift_false_without_spans(tmp_path: Path) -> None:
    src = tmp_path / "doc.md"
    src.write_text(_DOC, encoding="utf-8")
    dst = tmp_path / "live.md"
    dst.write_text(_DOC.replace("Pinned body original.", "x"), encoding="utf-8")
    tf = TrackedFile.model_validate(
        {"src": "doc.md", "dst": "~/.x/doc.md", "disposition": "shared"}
    )
    assert _span_only_drift(src, dst, tf) is False


def test_file_compare_span_drift_is_expected() -> None:
    entry = FileCompare(
        name="doc",
        status=CompareStatus.DRIFTED,
        diff="-x\n+y\n",
        expected_drift_keys=[],
        unexpected_drift_keys=[],
        disposition=Disposition.SHARED,
        span_only_drift=True,
    )
    assert entry.drift_is_expected is True


def test_file_compare_shared_non_span_drift_not_expected() -> None:
    entry = FileCompare(
        name="doc",
        status=CompareStatus.DRIFTED,
        diff="-x\n+y\n",
        expected_drift_keys=[],
        unexpected_drift_keys=[],
        disposition=Disposition.SHARED,
        span_only_drift=False,
    )
    assert entry.drift_is_expected is False
