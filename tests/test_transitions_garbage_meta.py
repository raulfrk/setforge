"""A transition whose meta.json is not UTF-8 is handled like other corrupt metadata."""

from pathlib import Path

import pytest

from setforge.errors import InvalidTransitionRecord
from setforge.transitions import TransitionDir, list_transitions, load_meta


def test_garbage_meta_is_a_clean_invalid_record(tmp_path: Path) -> None:
    transition = TransitionDir(tmp_path / "transition")
    transition.mkdir()
    (transition / "meta.json").write_bytes(b"\xff\xfe\x00zz")

    with pytest.raises(InvalidTransitionRecord):
        load_meta(transition)


def test_garbage_meta_is_skipped_when_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    broken = tmp_path / "transitions" / "20260101T000000000000Z-install-p"
    broken.mkdir(parents=True)
    (broken / "meta.json").write_bytes(b"\xff\xfe\x00zz")

    assert list_transitions() == []
