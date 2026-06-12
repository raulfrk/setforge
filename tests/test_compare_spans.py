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


def _tracked_file(spans: list[dict[str, object]]) -> TrackedFile:
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


def test_span_only_drift_true_for_overlay_body(tmp_path: Path) -> None:
    # A markerless OVERLAY body present in live but absent from tracked is
    # expected span-confined drift, not spurious DRIFTED.
    from setforge.overlay_inject import canonical_body, inject_body_at_anchor
    from setforge.source import AnchorAfterHeading

    src = tmp_path / "doc.md"
    src.write_text(_DOC, encoding="utf-8")
    dst = tmp_path / "live.md"
    live = inject_body_at_anchor(
        _DOC, AnchorAfterHeading(value="Shared"), canonical_body("HOST LOCAL ONLY")
    )
    dst.write_text(live, encoding="utf-8")
    tf = _tracked_file(
        [
            {
                "anchor": "## Shared",
                "kind": "overlay",
                "overlay": {
                    "anchor": {"kind": "after-heading", "value": "Shared"},
                    "body": "HOST LOCAL ONLY",
                },
            }
        ]
    )
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


_YAML_DOC = "editor:\n  fontSize: 12\n  tabSize: 4\nshared:\n  theme: dark\n"


def _tracked_yaml(spans: list[dict[str, str]]) -> TrackedFile:
    return TrackedFile.model_validate(
        {
            "src": "doc.yaml",
            "dst": "~/.x/doc.yaml",
            "disposition": "shared",
            "spans": spans,
        }
    )


def test_span_only_drift_true_when_structural_drift_inside_span(
    tmp_path: Path,
) -> None:
    # A structural (yaml dotted-path) file whose ONLY live divergence is at the
    # pinned path classifies as span-only — the compare gate must dispatch the
    # structural exclusion path, not hard-bail because the file isn't markdown.
    src = tmp_path / "doc.yaml"
    src.write_text(_YAML_DOC, encoding="utf-8")
    dst = tmp_path / "live.yaml"
    dst.write_text(_YAML_DOC.replace("fontSize: 12", "fontSize: 20"), encoding="utf-8")
    tf = _tracked_yaml([{"anchor": "editor.fontSize", "kind": "pinned"}])
    assert _span_only_drift(src, dst, tf) is True


def test_span_only_drift_false_when_structural_drift_outside_span(
    tmp_path: Path,
) -> None:
    src = tmp_path / "doc.yaml"
    src.write_text(_YAML_DOC, encoding="utf-8")
    dst = tmp_path / "live.yaml"
    # Drift at a NON-pinned path -> not span-only, needs attention.
    dst.write_text(
        _YAML_DOC.replace("theme: dark", "theme: solarized"), encoding="utf-8"
    )
    tf = _tracked_yaml([{"anchor": "editor.fontSize", "kind": "pinned"}])
    assert _span_only_drift(src, dst, tf) is False


def test_compare_span_only_true_when_live_adds_key_tracked_lacks(
    tmp_path: Path,
) -> None:
    # Live HAS a value at the span path; tracked LACKS it. Capture now DROPS
    # the path (host value never bakes into tracked), so excluded == tracked
    # and the drift classifies as span-only — expected host divergence, not
    # unsynced shared drift. Pins the intentional classification flip from
    # the old leave-live-as-is behavior (which reported False here).
    tracked = "editor:\n  tabSize: 4\nshared:\n  theme: dark\n"
    src = tmp_path / "doc.yaml"
    src.write_text(tracked, encoding="utf-8")
    dst = tmp_path / "live.yaml"
    dst.write_text(
        "editor:\n  fontSize: 20\n  tabSize: 4\nshared:\n  theme: dark\n",
        encoding="utf-8",
    )
    tf = _tracked_yaml([{"anchor": "editor.fontSize", "kind": "pinned"}])
    assert _span_only_drift(src, dst, tf) is True


def test_file_compare_span_drift_is_expected() -> None:
    entry = FileCompare(
        name="doc",
        status=CompareStatus.DRIFTED,
        diff="-x\n+y\n",
        disposition=Disposition.SHARED,
        span_only_drift=True,
    )
    assert entry.drift_is_expected is True


def test_file_compare_shared_non_span_drift_not_expected() -> None:
    entry = FileCompare(
        name="doc",
        status=CompareStatus.DRIFTED,
        diff="-x\n+y\n",
        disposition=Disposition.SHARED,
        span_only_drift=False,
    )
    assert entry.drift_is_expected is False
