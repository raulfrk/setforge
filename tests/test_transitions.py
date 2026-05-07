"""Tests for the transitions module."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from my_setup.transitions import (
    now_utc,
    state_root,
    transition_dirname,
    transitions_root,
)


def test_state_root_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_SETUP_STATE_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/test")))
    assert state_root() == Path("/home/test/.local/state/my-setup")


def test_state_root_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_SETUP_STATE_DIR", str(tmp_path))
    assert state_root() == tmp_path
    assert transitions_root() == tmp_path / "transitions"


def test_transition_dirname_format() -> None:
    ts = datetime(2026, 5, 7, 12, 30, 45, tzinfo=timezone.utc)
    assert transition_dirname(ts, "install", "vm-headless") == (
        "20260507T123045Z-install-vm-headless"
    )


def test_transition_dirname_sort_matches_time() -> None:
    """Lexicographic sort across dirnames must match chronological sort."""
    earlier = datetime(2026, 5, 7, 9, 0, 0, tzinfo=timezone.utc)
    later = datetime(2026, 5, 7, 17, 0, 0, tzinfo=timezone.utc)
    a = transition_dirname(earlier, "install", "vm-headless")
    b = transition_dirname(later, "install", "vm-headless")
    assert sorted([b, a]) == [a, b]


def test_now_utc_is_aware() -> None:
    ts = now_utc()
    assert ts.tzinfo is timezone.utc
