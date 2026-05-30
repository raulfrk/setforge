"""Tests for the ``TrackedFile.mode`` Pydantic field.

The validator must:

- Reject bare strings — covers ``mode: "0755"`` and ``mode: 0755``
  (the latter parses as the string ``"0755"`` under YAML 1.2).
- Reject ``bool`` (Python's ``isinstance(True, int)`` is True;
  ``mode: true`` should NOT silently mean ``0o1``).
- Reject out-of-range integers.
- Reject setuid (``0o4000``) and setgid (``0o2000``).
- Accept the canonical YAML-1.2 octal literal ``0o755`` (= 493).
- Accept ``None`` (the default — preserves source mode at deploy time).
- Accept the sticky bit (``0o1000``) — only setuid/setgid are refused.

The YAML round-trip case is covered separately by
``test_mode_yaml_round_trip_preserves_0o_format`` which loads the
ruamel.yaml document, runs it through a write back, and asserts the
serialized text still contains ``0o755`` (not ``493`` and not ``0755``).
"""

import io
from pathlib import Path

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from setforge.config import TrackedFile


def _make_tracked_file(**overrides: object) -> TrackedFile:
    base: dict[str, object] = {"src": Path("x"), "dst": "y"}
    base.update(overrides)
    return TrackedFile.model_validate(base)


def test_mode_accepts_0o755() -> None:
    """The canonical YAML-1.2 octal literal lands as ``0o755`` (= 493)."""
    tf = _make_tracked_file(mode=0o755)
    assert tf.mode == 0o755


def test_mode_accepts_none_default() -> None:
    """Omitting ``mode`` leaves the field as ``None`` (source-mode fallback)."""
    tf = _make_tracked_file()
    assert tf.mode is None


def test_mode_accepts_sticky_bit() -> None:
    """The sticky bit (``0o1000``) is permitted; only setuid/setgid are refused."""
    tf = _make_tracked_file(mode=0o1755)
    assert tf.mode == 0o1755


def test_mode_rejects_quoted_0755() -> None:
    """Quoted string ``"0755"`` — fails with helpful error mentioning 0o755."""
    with pytest.raises(ValidationError) as exc_info:
        _make_tracked_file(mode="0755")
    assert "0o755" in str(exc_info.value)


def test_mode_rejects_unquoted_0755_yaml() -> None:
    """Unquoted YAML-1.1-style ``0755`` parses as :class:`ScalarInt(755)` under
    YAML 1.2 — NOT as octal — and is the load-time footgun setforge guards
    against. The validator must reject with a message pointing at ``0o755``.
    """
    from ruamel.yaml.scalarint import OctalInt, ScalarInt

    yaml = YAML(typ="rt")
    doc = yaml.load("mode: 0755\n")
    # Sanity: ruamel really does produce a ScalarInt-not-OctalInt here.
    assert isinstance(doc["mode"], ScalarInt)
    assert not isinstance(doc["mode"], OctalInt)
    with pytest.raises(ValidationError) as exc_info:
        _make_tracked_file(mode=doc["mode"])
    msg = str(exc_info.value)
    assert "0o755" in msg, msg


def test_mode_rejects_decimal_int_as_string() -> None:
    """``mode: "493"`` is also a string and must be refused (no implicit cast)."""
    with pytest.raises(ValidationError):
        _make_tracked_file(mode="493")


def test_mode_rejects_bool_true() -> None:
    """``isinstance(True, int)`` is True; the validator must reject bool
    explicitly so ``mode: true`` doesn't silently mean ``0o1``.
    """
    with pytest.raises(ValidationError) as exc_info:
        _make_tracked_file(mode=True)
    assert "0o755" in str(exc_info.value) or "octal" in str(exc_info.value).lower()


def test_mode_rejects_negative() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _make_tracked_file(mode=-1)
    assert "out of range" in str(exc_info.value)


def test_mode_rejects_above_max() -> None:
    """``0o7777`` is the upper bound; ``0o10000`` is over-range."""
    with pytest.raises(ValidationError) as exc_info:
        _make_tracked_file(mode=0o10000)
    assert "out of range" in str(exc_info.value)


def test_mode_rejects_setuid() -> None:
    """Setuid (``0o4000``) bit is refused for security."""
    with pytest.raises(ValidationError) as exc_info:
        _make_tracked_file(mode=0o4755)
    assert "setuid" in str(exc_info.value) or "setgid" in str(exc_info.value)


def test_mode_rejects_setgid() -> None:
    """Setgid (``0o2000``) bit is refused for security."""
    with pytest.raises(ValidationError) as exc_info:
        _make_tracked_file(mode=0o2755)
    assert "setuid" in str(exc_info.value) or "setgid" in str(exc_info.value)


def test_mode_yaml_round_trip_preserves_0o_format(tmp_path: Path) -> None:
    """Load ``mode: 0o755`` from YAML, write it back, assert formatting survives.

    Exercises the natural ruamel.yaml round-trip behaviour: the value
    loads as an :class:`OctalInt`, which serializes back as ``0o755``
    rather than the decimal ``493`` or the YAML-1.1-style ``0755``.
    Models the way ``setforge sync`` writeback (wizard +
    ``_action_save_as_preserved``) preserves the user's chosen
    formatting across mutations of OTHER keys in the document.
    """
    cfg_text = (
        "tracked_files:\n"
        "  foo:\n"
        "    src: x\n"
        "    dst: y\n"
        "    mode: 0o755\n"
        "profiles: {}\n"
    )
    cfg_path = tmp_path / "setforge.yaml"
    cfg_path.write_text(cfg_text, encoding="utf-8")

    yaml = YAML(typ="rt")
    with cfg_path.open("r", encoding="utf-8") as fh:
        doc = yaml.load(fh)

    # Simulate a wizard write that touches an UNRELATED key (the
    # natural setforge sync mutation surface — preserve_user_keys),
    # leaving mode untouched. The round-trip must keep `0o755`.
    doc["tracked_files"]["foo"]["preserve_user_keys"] = ["k1"]

    buf = io.StringIO()
    yaml.dump(doc, buf)
    out = buf.getvalue()

    assert "mode: 0o755" in out, out
    assert "mode: 493" not in out
    assert "mode: 0755\n" not in out
