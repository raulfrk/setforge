"""Tests for the ``FileComponent`` Pydantic model (bundle ``file`` source).

``FileComponent`` carries the real :class:`TrackedFile` fields
(``src``/``dst``/``mode``/``template``/``symlink``) and MUST reuse
:class:`TrackedFile`'s mode validator — so setuid/setgid bits and
out-of-range values are refused with the same policy, and ``mode: None``
is accepted (source-mode fallback).
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from setforge.config import FileComponent


def _make(**overrides: object) -> FileComponent:
    base: dict[str, object] = {"src": Path("launcher"), "dst": "~/.local/bin/x"}
    base.update(overrides)
    return FileComponent.model_validate(base)


def test_file_component_accepts_good_fields() -> None:
    fc = _make(mode=0o755, template=True, symlink=None)
    assert fc.src == Path("launcher")
    assert fc.dst == "~/.local/bin/x"
    assert fc.mode == 0o755
    assert fc.template is True
    assert fc.symlink is None


def test_file_component_defaults() -> None:
    fc = _make()
    assert fc.mode is None
    assert fc.template is False
    assert fc.symlink is None


def test_file_component_accepts_mode_none() -> None:
    fc = _make(mode=None)
    assert fc.mode is None


def test_file_component_rejects_setuid() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _make(mode=0o4755)
    assert "setuid" in str(exc_info.value) or "setgid" in str(exc_info.value)


def test_file_component_rejects_setgid() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _make(mode=0o2755)
    assert "setuid" in str(exc_info.value) or "setgid" in str(exc_info.value)


def test_file_component_rejects_out_of_range() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _make(mode=0o10000)
    assert "out of range" in str(exc_info.value)


def test_file_component_is_strict_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        _make(unknown_field="boom")


def test_file_component_has_no_disposition_field() -> None:
    """The retired ``disposition``/``share`` field must NOT exist."""
    assert "disposition" not in FileComponent.model_fields
    assert "share" not in FileComponent.model_fields


def test_file_component_rejects_control_char_in_src() -> None:
    """Control chars in ``src`` are refused at model load (matches TrackedFile).

    Without this the char slips past FileComponent and only fails later
    inside the synthetic-TrackedFile construction as a raw ValidationError,
    escaping the clean domain-gate path.
    """
    with pytest.raises(ValidationError) as exc_info:
        _make(src=Path("laun\tcher"))
    assert "forbidden control character" in str(exc_info.value)


def test_file_component_rejects_control_char_in_dst() -> None:
    """Control chars in ``dst`` are refused at model load (matches TrackedFile)."""
    with pytest.raises(ValidationError) as exc_info:
        _make(dst="~/.local/\nbin/x")
    assert "forbidden control character" in str(exc_info.value)
