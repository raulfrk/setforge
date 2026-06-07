"""Capture-side leak gate for markerless OVERLAY spans.

The capture half of the canonical leak gate: a live file carrying an
injected host-local body is captured back to tracked, and the body NEVER
reaches the tracked src nor the re-baselined stored base. Overlay capture
owns its excise (exact recorded bytes), never the leaky
exclude_spans_for_capture pass-through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge import base_store, spans_store
from setforge.capture import CaptureAction, _capture_disposition_file
from setforge.config import Disposition
from setforge.overlay_inject import (
    OverlayAmbiguousError,
    canonical_body,
    inject_body_at_anchor,
)
from setforge.source import AnchorAfterHeading
from setforge.spans import OverlaySpanPayload, SpanEntry, SpanKind
from setforge.spans_store import SpanState

_PROFILE = "p"
_SUB_NAME = "notes/CLAUDE.md"
_TRACKED = "# Title\n\n## Notes\n\nshared body\n"


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))


def _overlay(body: str = "HOST LOCAL ONLY") -> SpanEntry:
    return SpanEntry(
        anchor="## Notes",
        kind=SpanKind.OVERLAY,
        overlay=OverlaySpanPayload(anchor=AnchorAfterHeading(value="Notes"), body=body),
    )


def _seed_state(body: str) -> None:
    st = SpanState(
        anchor="## Notes",
        fingerprint="a" * 64,
        prefix=[],
        suffix=[],
        position_hint_start_line=3,
        position_hint_n_lines=1,
        heading_level=2,
        last_deployed_body=canonical_body(body),
    )
    spans_store.set_states(_PROFILE, _SUB_NAME, {"## Notes": st})


def test_capture_excises_body_from_tracked_and_base(tmp_path: Path) -> None:
    src = tmp_path / "src.md"
    src.write_text(_TRACKED)
    dst = tmp_path / "dst.md"
    # Live carries the injected body (what deploy produced).
    body = canonical_body("HOST LOCAL ONLY")
    dst.write_text(
        inject_body_at_anchor(_TRACKED, AnchorAfterHeading(value="Notes"), body)
    )
    _seed_state("HOST LOCAL ONLY")

    result = _capture_disposition_file(
        _SUB_NAME,
        src,
        dst,
        disposition=Disposition.SHARED,
        profile=_PROFILE,
        spans=[_overlay()],
    )
    assert result.action in (CaptureAction.UPDATED, CaptureAction.NOOP)
    # LEAK GATE: tracked src is body-free.
    assert "HOST LOCAL ONLY" not in src.read_text()
    assert src.read_text() == _TRACKED
    # LEAK GATE: stored base is body-free.
    stored_base = base_store.read_base(_PROFILE, _SUB_NAME)
    assert stored_base is not None
    assert b"HOST LOCAL ONLY" not in stored_base


def test_capture_hand_edited_body_auto_use_tracked_discards(tmp_path: Path) -> None:
    # Live carries a HAND-EDITED body (no exact needle hit). --auto=keep-tracked
    # maps to discard: the located body is excised, tracked stays body-free,
    # local.yaml is NOT written.
    from setforge.capture import CaptureAuto

    src = tmp_path / "src.md"
    src.write_text(_TRACKED)
    dst = tmp_path / "dst.md"
    dst.write_text(
        inject_body_at_anchor(
            _TRACKED, AnchorAfterHeading(value="Notes"), canonical_body("EDITED LIVE")
        )
    )
    _seed_state("ORIGINAL BODY")
    local = tmp_path / "local.yaml"
    local.write_text("tracked_files: {}\n", encoding="utf-8")

    _capture_disposition_file(
        _SUB_NAME,
        src,
        dst,
        disposition=Disposition.SHARED,
        profile=_PROFILE,
        spans=[_overlay("ORIGINAL BODY")],
        tracked_file_id="notes",
        auto=CaptureAuto.KEEP_TRACKED,
        interactive=False,
        local_config_path=local,
    )
    assert "EDITED LIVE" not in src.read_text()
    assert src.read_text() == _TRACKED
    assert "EDITED LIVE" not in local.read_text()


def test_capture_hand_edited_body_auto_keep_writes_local(tmp_path: Path) -> None:
    # --auto=use-live keeps the edit: writes it into local.yaml, tracked clean.
    from setforge.capture import CaptureAuto

    src = tmp_path / "src.md"
    src.write_text(_TRACKED)
    dst = tmp_path / "dst.md"
    dst.write_text(
        inject_body_at_anchor(
            _TRACKED, AnchorAfterHeading(value="Notes"), canonical_body("EDITED LIVE")
        )
    )
    _seed_state("ORIGINAL BODY")
    local = tmp_path / "local.yaml"
    local.write_text(
        "tracked_files:\n"
        "  notes:\n"
        "    spans:\n"
        "      - anchor: '## Notes'\n"
        "        kind: overlay\n"
        "        overlay:\n"
        "          anchor:\n"
        "            kind: after-heading\n"
        "            value: Notes\n"
        "          body: ORIGINAL BODY\n",
        encoding="utf-8",
    )

    _capture_disposition_file(
        _SUB_NAME,
        src,
        dst,
        disposition=Disposition.SHARED,
        profile=_PROFILE,
        spans=[_overlay("ORIGINAL BODY")],
        tracked_file_id="notes",
        auto=CaptureAuto.USE_LIVE,
        interactive=False,
        local_config_path=local,
    )
    assert "EDITED LIVE" not in src.read_text()
    assert src.read_text() == _TRACKED
    # The edit landed in local.yaml.
    assert "EDITED LIVE" in local.read_text()


def test_capture_hand_edited_body_no_auto_non_interactive_raises(
    tmp_path: Path,
) -> None:
    from setforge.errors import CaptureRequiresInteractive

    src = tmp_path / "src.md"
    src.write_text(_TRACKED)
    dst = tmp_path / "dst.md"
    dst.write_text(
        inject_body_at_anchor(
            _TRACKED, AnchorAfterHeading(value="Notes"), canonical_body("EDITED LIVE")
        )
    )
    _seed_state("ORIGINAL BODY")
    with pytest.raises(CaptureRequiresInteractive):
        _capture_disposition_file(
            _SUB_NAME,
            src,
            dst,
            disposition=Disposition.SHARED,
            profile=_PROFILE,
            spans=[_overlay("ORIGINAL BODY")],
            tracked_file_id="notes",
            auto=None,
            interactive=False,
            local_config_path=tmp_path / "local.yaml",
        )


def _seed_unlocatable_state(body: str) -> None:
    # A sidecar that records a deployed body but whose position hint makes
    # the body region unlocatable (n_lines <= 0 → _locate_body_region_bounds
    # returns None). detect_overlay_body_edit then returns None even though a
    # body WAS deployed here.
    st = SpanState(
        anchor="## Notes",
        fingerprint="a" * 64,
        prefix=[],
        suffix=[],
        position_hint_start_line=3,
        position_hint_n_lines=0,
        heading_level=2,
        last_deployed_body=canonical_body(body),
    )
    spans_store.set_states(_PROFILE, _SUB_NAME, {"## Notes": st})


def test_capture_deployed_but_unlocatable_body_fails_closed(tmp_path: Path) -> None:
    # LEAK GATE (fail-closed): a host-local body WAS deployed here
    # (last_deployed_body set), but it is now neither present verbatim nor
    # fuzzy-locatable. Capturing would leak the hand-edited body into tracked,
    # so the gate REFUSES before any tracked write.
    from setforge.errors import OverlayBodyUnlocatable

    src = tmp_path / "src.md"
    src.write_text(_TRACKED)
    dst = tmp_path / "dst.md"
    # Live carries a hand-edited body that no needle matches and the hint
    # cannot bound. The edited body must remain in tracked-IF-it-leaked.
    dst.write_text(
        "# Title\n\n## Notes\n\nSECRET HOST EDIT\n\nshared body\n", encoding="utf-8"
    )
    _seed_unlocatable_state("ORIGINAL BODY")

    with pytest.raises(OverlayBodyUnlocatable):
        _capture_disposition_file(
            _SUB_NAME,
            src,
            dst,
            disposition=Disposition.SHARED,
            profile=_PROFILE,
            spans=[_overlay("ORIGINAL BODY")],
            tracked_file_id="notes",
            auto=None,
            interactive=False,
            local_config_path=tmp_path / "local.yaml",
        )
    # The refuse happens BEFORE any tracked / base write: tracked is untouched
    # and the host-local edit never leaked.
    assert src.read_text() == _TRACKED
    assert "SECRET HOST EDIT" not in src.read_text()
    assert base_store.read_base(_PROFILE, _SUB_NAME) is None


def test_capture_first_deploy_absent_body_continues_cleanly(tmp_path: Path) -> None:
    # The genuine first-deploy / never-deployed case: NO sidecar body exists
    # (stored is None for this anchor). An unlocatable miss must NOT refuse —
    # there is nothing to leak; capture proceeds and writes tracked normally.
    src = tmp_path / "src.md"
    src.write_text(_TRACKED)
    dst = tmp_path / "dst.md"
    # Live has no injected body at all (clean shared content only).
    dst.write_text(_TRACKED, encoding="utf-8")
    # No _seed_state call → span_states is empty → stored is None.

    result = _capture_disposition_file(
        _SUB_NAME,
        src,
        dst,
        disposition=Disposition.SHARED,
        profile=_PROFILE,
        spans=[_overlay("ORIGINAL BODY")],
        tracked_file_id="notes",
        auto=None,
        interactive=False,
        local_config_path=tmp_path / "local.yaml",
    )
    assert result.action in (CaptureAction.UPDATED, CaptureAction.NOOP)
    assert src.read_text() == _TRACKED


def test_capture_ambiguous_body_refuses(tmp_path: Path) -> None:
    body = canonical_body("DUP")
    src = tmp_path / "src.md"
    src.write_text(_TRACKED)
    dst = tmp_path / "dst.md"
    dst.write_text(f"x\n{body}y\n{body}z\n")
    _seed_state("DUP")
    with pytest.raises(OverlayAmbiguousError):
        _capture_disposition_file(
            _SUB_NAME,
            src,
            dst,
            disposition=Disposition.SHARED,
            profile=_PROFILE,
            spans=[_overlay("DUP")],
        )
