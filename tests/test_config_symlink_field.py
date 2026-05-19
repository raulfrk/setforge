"""Tests for the ``TrackedFile.symlink`` Pydantic field (setforge-m483).

The schema must:

- Accept ``symlink: None`` (the default — non-symlink tracked_files
  keep their pre-bump behavior).
- Accept any raw user string for the symlink target and preserve it
  verbatim (no :func:`Path.expanduser`, no :func:`Path.resolve`)
  — cross-host portability invariant. ``os.readlink`` on a deployed
  link must return exactly what the user wrote.
- Refuse a self-loop where the expanded symlink target equals the
  expanded ``dst`` (config-time guard).
- Refuse self-loops regardless of textual form: ``~/x`` vs
  ``$HOME/x`` resolve to the same path under expanduser only for
  ``~`` — but the literal expansion equality check still catches
  the common case ``dst: ~/x; symlink: ~/x``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from setforge.config import TrackedFile


def _make(**overrides: object) -> TrackedFile:
    base: dict[str, object] = {"src": Path("x"), "dst": "y"}
    base.update(overrides)
    return TrackedFile.model_validate(base)


def test_symlink_default_none() -> None:
    """Omitting ``symlink:`` leaves the field as ``None`` (non-symlink default)."""
    tf = _make()
    assert tf.symlink is None


def test_symlink_accepts_tilde_string() -> None:
    """``~/foo`` is preserved verbatim; no eager expanduser at load."""
    tf = _make(dst="~/.config/setforge/link", symlink="~/.config/setforge/target")
    assert tf.symlink == "~/.config/setforge/target"


def test_symlink_accepts_absolute_string() -> None:
    """Absolute paths pass through unchanged."""
    tf = _make(dst="/etc/foo", symlink="/var/lib/foo/target")
    assert tf.symlink == "/var/lib/foo/target"


def test_symlink_raw_string_not_expanded() -> None:
    """Field stores the raw string — explicit ``expanduser`` check.

    Regression guard for the spec's anti-pattern #3: applying
    :func:`Path.expanduser` to a user-declared symlink target before
    :func:`os.symlink` bakes ``/home/<user>/`` into the on-disk link
    and destroys cross-host portability. The schema must keep the
    raw string.
    """
    tf = _make(dst="~/a", symlink="~/b/c")
    # Path.expanduser on the raw value WOULD return /home/<user>/b/c —
    # but the schema must return the unexpanded form.
    assert tf.symlink == "~/b/c"
    assert "~" in tf.symlink


def test_symlink_self_loop_rejected_tilde() -> None:
    """``dst == symlink`` (both ``~/foo``) — refused with self-loop error."""
    with pytest.raises(ValidationError) as exc_info:
        _make(dst="~/.config/foo", symlink="~/.config/foo")
    assert "self-loop" in str(exc_info.value).lower()


def test_symlink_self_loop_rejected_absolute() -> None:
    """``dst == symlink`` (both absolute) — refused."""
    with pytest.raises(ValidationError) as exc_info:
        _make(dst="/tmp/x", symlink="/tmp/x")
    assert "self-loop" in str(exc_info.value).lower()


def test_symlink_distinct_target_accepted() -> None:
    """Different ``dst`` and ``symlink`` — accepted."""
    tf = _make(dst="~/a", symlink="~/b")
    assert tf.symlink == "~/b"


def test_symlink_no_self_loop_message_mentions_both_paths() -> None:
    """Self-loop error message includes both the symlink and dst values."""
    with pytest.raises(ValidationError) as exc_info:
        _make(dst="~/loop", symlink="~/loop")
    msg = str(exc_info.value)
    assert "~/loop" in msg
