"""Tests for the shared ``_check_yaml_octal_mode`` helper.

The bool/ScalarInt/OctalInt cascade is shared between
:func:`setforge.config.TrackedFile._validate_mode` and
:func:`setforge.source._LocalTrackedFileOverlay._validate_mode_octal_only`;
both field validators dispatch to the module-level helper with a
``source_label`` carrying the consumer-specific message prefix.

Two layers of coverage:

- Direct unit tests for the helper (accept/reject cascade + label
  threading).
- Byte-identical error-text locks for BOTH consumers — the exact
  strings each validator produced before the extraction, asserted as
  substrings of the raised :class:`pydantic.ValidationError`.
"""

import pytest
from pydantic import ValidationError
from ruamel.yaml.scalarint import OctalInt, ScalarInt

from setforge.config import TrackedFile, _check_yaml_octal_mode
from setforge.source import _LocalTrackedFileOverlay

# ---------------------------------------------------------------------------
# Direct helper tests
# ---------------------------------------------------------------------------


def test_helper_none_passthrough() -> None:
    """``None`` (mode omitted) passes through unchanged."""
    assert _check_yaml_octal_mode(None, "mode") is None


def test_helper_accepts_octalint() -> None:
    """OctalInt (the YAML-1.2 ``0o755`` shape) is accepted as plain int."""
    result = _check_yaml_octal_mode(OctalInt(0o755), "mode")
    assert result == 0o755
    assert type(result) is int


def test_helper_accepts_exact_int() -> None:
    """The exact ``int`` type (Python literal ``0o755`` == 493) is accepted."""
    assert _check_yaml_octal_mode(0o755, "mode") == 0o755


def test_helper_rejects_bool() -> None:
    """``isinstance(True, int)`` is True; the bool gate must fire first."""
    with pytest.raises(ValueError, match=r"not bool") as exc_info:
        _check_yaml_octal_mode(True, "mode")
    assert str(exc_info.value) == (
        "mode must be YAML-1.2 octal int literal (e.g. 0o755), not bool. Got: True"
    )


def test_helper_rejects_scalarint_with_octal_hint() -> None:
    """ScalarInt-not-OctalInt (the YAML-1.1 ``0755`` shape) gets the
    canonical "use 0o755" hint with both reinterpretations spelled out."""
    with pytest.raises(ValueError, match=r"YAML-1\.1-style") as exc_info:
        _check_yaml_octal_mode(ScalarInt(755), "mode")
    assert str(exc_info.value) == (
        "mode 755 appears to use YAML-1.1-style leading-zero "
        "octal (e.g. 0755) which YAML 1.2 silently parses as "
        "decimal. If you meant the permission bits commonly "
        "written as 'octal 755', use the YAML-1.2 literal 0o755. "
        "If you literally meant the integer 755, use 0o1363."
    )


def test_helper_rejects_str() -> None:
    """Strings (covers quoted ``"0755"``) are rejected with the
    catch-all other-types message."""
    with pytest.raises(ValueError, match=r"strings, floats") as exc_info:
        _check_yaml_octal_mode("0755", "mode")
    assert str(exc_info.value) == (
        "mode must be a YAML-1.2 octal int literal (e.g. 0o755); "
        "strings, floats, and other types are rejected. Got: '0755'"
    )


def test_helper_threads_source_label_through_every_branch() -> None:
    """The ``source_label`` parameter prefixes all three reject messages."""
    label = "_LocalTrackedFileOverlay: `mode`"
    for bad in (True, ScalarInt(755), "0755"):
        with pytest.raises(ValueError, match=r"0o755") as exc_info:
            _check_yaml_octal_mode(bad, label)
        assert str(exc_info.value).startswith(label + " ")


# ---------------------------------------------------------------------------
# Consumer error-text locks (byte-identical to pre-extraction strings)
# ---------------------------------------------------------------------------


def _make_tracked_file_error(mode: object) -> str:
    with pytest.raises(ValidationError) as exc_info:
        TrackedFile.model_validate({"src": "x", "dst": "y", "mode": mode})
    return str(exc_info.value)


def _make_overlay_error(mode: object) -> str:
    with pytest.raises(ValidationError) as exc_info:
        _LocalTrackedFileOverlay(mode=mode)  # type: ignore[arg-type]
    return str(exc_info.value)


def test_trackedfile_bool_error_text_unchanged() -> None:
    assert (
        "mode must be YAML-1.2 octal int literal (e.g. 0o755), not bool. Got: True"
    ) in _make_tracked_file_error(True)


def test_trackedfile_scalarint_error_text_unchanged() -> None:
    assert (
        "mode 755 appears to use YAML-1.1-style leading-zero "
        "octal (e.g. 0755) which YAML 1.2 silently parses as "
        "decimal. If you meant the permission bits commonly "
        "written as 'octal 755', use the YAML-1.2 literal 0o755. "
        "If you literally meant the integer 755, use 0o1363."
    ) in _make_tracked_file_error(ScalarInt(755))


def test_trackedfile_str_error_text_unchanged() -> None:
    assert (
        "mode must be a YAML-1.2 octal int literal (e.g. 0o755); "
        "strings, floats, and other types are rejected. Got: '0755'"
    ) in _make_tracked_file_error("0755")


def test_overlay_bool_error_text_unchanged() -> None:
    assert (
        "_LocalTrackedFileOverlay: `mode` must be YAML-1.2 octal "
        "int literal (e.g. 0o755), not bool. Got: True"
    ) in _make_overlay_error(True)


def test_overlay_scalarint_error_text_unchanged() -> None:
    assert (
        "_LocalTrackedFileOverlay: `mode` 755 appears to use "
        "YAML-1.1-style leading-zero octal (e.g. 0755) which YAML 1.2 "
        "silently parses as decimal. If you meant the permission bits "
        "commonly written as 'octal 755', use the YAML-1.2 literal 0o755. "
        "If you literally meant the integer 755, use 0o1363."
    ) in _make_overlay_error(ScalarInt(755))


def test_overlay_str_error_text_unchanged() -> None:
    assert (
        "_LocalTrackedFileOverlay: `mode` must be a YAML-1.2 octal "
        "int literal (e.g. 0o755); strings, floats, and other types "
        "are rejected. Got: '0755'"
    ) in _make_overlay_error("0755")
