"""Unit tests for the marker-retire migration's frozen strip + baseline helpers.

``strip_markers`` and ``baseline_stripped`` are the byte-shaping primitives the
migration's ``apply`` composes: ``strip_markers(live)`` becomes the recorded
``local`` content, and ``baseline_stripped(live, tracked)`` reconstructs the
3-way merge ``base`` from the embedded ``hash=`` baselines. Their correctness is
data-loss-critical (a wrong base silently overwrites a live edit, MP-4), so they
are pinned directly here in addition to the end-to-end ``apply`` tests.
"""

from __future__ import annotations

import hashlib

import pytest

from setforge.errors import MarkerError
from setforge.migrations._marker_retire import baseline_stripped, strip_markers


def _h(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _shared(name: str, body: str, *, hash_: str | None = None) -> str:
    end = f" hash={hash_}" if hash_ is not None else ""
    return (
        f"<!-- setforge:user-section start shared {name} -->\n"
        f"{body}"
        f"<!-- setforge:user-section end shared {name}{end} -->\n"
    )


def _host_local(name: str, body: str, *, hash_: str | None = None) -> str:
    end = f" hash={hash_}" if hash_ is not None else ""
    return (
        f"<!-- setforge:user-section start host-local {name} -->\n"
        f"{body}"
        f"<!-- setforge:user-section end host-local {name}{end} -->\n"
    )


# --- strip_markers ----------------------------------------------------------


def test_strip_keeps_shared_body_drops_its_markers() -> None:
    text = "head\n" + _shared("rules", "Use rg.\n") + "tail\n"
    assert strip_markers(text) == "head\nUse rg.\ntail\n"


def test_strip_drops_host_local_body_and_markers() -> None:
    text = "head\n" + _host_local("paths", "workdir: /home/raul\n") + "tail\n"
    assert strip_markers(text) == "head\ntail\n"


def test_strip_mixed_keeps_shared_drops_host_local() -> None:
    text = (
        "intro\n"
        + _shared("rules", "shared body\n")
        + "mid\n"
        + _host_local("paths", "host body\n")
        + "tail\n"
    )
    assert strip_markers(text) == "intro\nshared body\nmid\ntail\n"


def test_strip_no_markers_is_identity() -> None:
    text = "just\nplain\ntext\n"
    assert strip_markers(text) == text


def test_strip_empty_shared_body_round_trips() -> None:
    # MP-7: an empty-body section must not be dropped by a truthiness check.
    text = "a\n" + _shared("blank", "") + "b\n"
    assert strip_markers(text) == "a\nb\n"


def test_strip_refuses_malformed_marker() -> None:
    text = "<!-- setforge:user-section start rules -->\nx\n"  # missing keyword
    with pytest.raises(MarkerError):
        strip_markers(text)


# --- baseline_stripped ------------------------------------------------------


def test_baseline_no_drift_equals_strip() -> None:
    body = "shared body\n"
    live = "h\n" + _shared("r", body, hash_=_h(body)) + "t\n"
    tracked = live
    assert baseline_stripped(live, tracked) == strip_markers(live)


def test_baseline_pending_tracked_uses_live_body() -> None:
    # Live pristine (matches its embedded hash), tracked moved => baseline is the
    # live body, so the later 3-way sees only tracked changed (update applies).
    base_body = "old shared\n"
    live = "h\n" + _shared("r", base_body, hash_=_h(base_body)) + "t\n"
    tracked = "h\n" + _shared("r", "new shared\n", hash_=_h(base_body)) + "t\n"
    assert baseline_stripped(live, tracked) == "h\nold shared\nt\n"


def test_baseline_live_edited_uses_tracked_body() -> None:
    # Live moved, tracked pristine => baseline is the TRACKED body. Seeding the
    # live body here would make ours==base and silently lose the live edit (MP-4).
    base_body = "baseline\n"
    live = "h\n" + _shared("r", "my live edit\n", hash_=_h(base_body)) + "t\n"
    tracked = "h\n" + _shared("r", base_body, hash_=_h(base_body)) + "t\n"
    assert baseline_stripped(live, tracked) == "h\nbaseline\nt\n"


def test_baseline_drops_host_local_like_strip() -> None:
    body = "shared\n"
    live = _shared("r", body, hash_=_h(body)) + _host_local("p", "host\n")
    tracked = _shared("r", body, hash_=_h(body))
    assert baseline_stripped(live, tracked) == "shared\n"
