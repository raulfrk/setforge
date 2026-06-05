"""Tests for the spans derived-state sidecar (Layer 2).

Mirrors :mod:`tests.test_scalar_base_store`: per-``(profile, file-id)``
JSON manifest under ``<state_root>/spans/<profile>/<file-id>.json``,
atomic write, traversal guard, round-trip of the per-span record
(fingerprint + prefix/suffix context + advisory position hint + heading
level).
"""

import json
from pathlib import Path

import pytest

from setforge import spans_store
from setforge.errors import BaseStoreError
from setforge.spans_store import SpanState

_PROFILE = "debian-vm"
_FILE_ID = "claude/CLAUDE.md"


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))


def _state(anchor: str = "## Foo") -> SpanState:
    return SpanState(
        anchor=anchor,
        fingerprint="a" * 64,
        prefix=["before1", "before2"],
        suffix=["after1"],
        position_hint_start_line=10,
        position_hint_n_lines=4,
        heading_level=2,
    )


def test_get_states_missing_returns_empty() -> None:
    assert spans_store.get_states(_PROFILE, _FILE_ID) == {}


def test_set_and_get_round_trip() -> None:
    st = _state()
    spans_store.set_states(_PROFILE, _FILE_ID, {st.anchor: st})
    loaded = spans_store.get_states(_PROFILE, _FILE_ID)
    assert loaded == {"## Foo": st}


def test_set_states_merges_untouched() -> None:
    a = _state("## A")
    b = _state("## B")
    spans_store.set_states(_PROFILE, _FILE_ID, {a.anchor: a})
    spans_store.set_states(_PROFILE, _FILE_ID, {b.anchor: b})
    loaded = spans_store.get_states(_PROFILE, _FILE_ID)
    assert set(loaded) == {"## A", "## B"}


def test_prune_drops_unlisted() -> None:
    a = _state("## A")
    b = _state("## B")
    spans_store.set_states(_PROFILE, _FILE_ID, {a.anchor: a, b.anchor: b})
    spans_store.prune(_PROFILE, _FILE_ID, {"## A"})
    loaded = spans_store.get_states(_PROFILE, _FILE_ID)
    assert set(loaded) == {"## A"}


def test_prune_missing_manifest_is_noop() -> None:
    spans_store.prune(_PROFILE, _FILE_ID, {"## A"})
    assert spans_store.get_states(_PROFILE, _FILE_ID) == {}


def test_traversal_guard_rejects_dotdot() -> None:
    with pytest.raises(BaseStoreError):
        spans_store.get_states(_PROFILE, "../escape")


def test_traversal_guard_rejects_absolute() -> None:
    with pytest.raises(BaseStoreError):
        spans_store.set_states(_PROFILE, "/etc/passwd", {})


def test_corrupt_manifest_raises() -> None:
    target = Path(spans_store.spans_root()) / _PROFILE / "claude" / "CLAUDE.md.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(BaseStoreError):
        spans_store.get_states(_PROFILE, _FILE_ID)


def test_manifest_is_human_readable_json() -> None:
    st = _state()
    spans_store.set_states(_PROFILE, _FILE_ID, {st.anchor: st})
    target = Path(spans_store.spans_root()) / _PROFILE / "claude" / "CLAUDE.md.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["## Foo"]["fingerprint"] == "a" * 64
    assert payload["## Foo"]["heading_level"] == 2
