"""Forward-tolerance for unknown ENUM VALUES on existing fields.

COMPATIBILITY.md permits a newer minor to add an enum member to an existing
field within a major (additive-first). An older same-major engine must not
crash on one: it either reverts the offending field to its default and warns
(when the field is safely defaultable), or refuses cleanly via ``ConfigError``
("upgrade setforge") — never a raw Pydantic ``ValidationError`` traceback.

Regression for the audit finding: ``_validate_tolerant`` only stripped
``extra_forbidden`` (unknown *fields*); an unknown enum *value* produced a
Pydantic ``enum`` error and the guard re-raised the raw ``ValidationError``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from setforge.config import load_config
from setforge.errors import ConfigError


def _write(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def test_unknown_disposition_value_reverts_to_default_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A future ``disposition: layered`` on a known field loads, not crashes.

    ``disposition`` is Optional with a ``None`` default, so the offending
    key is stripped (reverting to the default) and a stderr warning naming
    the ignored value is emitted — no raw ``ValidationError`` escapes.
    """
    body = (
        'version: 1\nschema_version: "2.99"\n'
        "tracked_files:\n  a:\n    src: x\n    dst: y\n    disposition: layered\n"
        "profiles:\n  default: {}\n"
    )
    cfg = _write(tmp_path, body)

    config = load_config(cfg)  # must NOT raise

    assert config.tracked_files["a"].disposition is None
    err = capsys.readouterr().err
    assert "tracked_files.a.disposition" in err
    assert "warning:" in err


def test_unknown_span_kind_value_refuses_clean_not_traceback(
    tmp_path: Path,
) -> None:
    """An unknown enum value nested in a list entry refuses cleanly.

    A span ``kind`` lives inside a list, where this reader cannot safely
    default the offending entry. Rather than leaking a raw Pydantic
    traceback, ``load_config`` raises a clean ``ConfigError`` pointing the
    user at an upgrade.
    """
    body = (
        'version: 1\nschema_version: "2.99"\n'
        "tracked_files:\n  a:\n    src: x\n    dst: y\n    disposition: pinned\n"
        "    spans:\n      - anchor: '## H'\n        kind: futurekind\n"
        "profiles:\n  default: {}\n"
    )
    cfg = _write(tmp_path, body)

    with pytest.raises(ConfigError, match="upgrade setforge"):
        load_config(cfg)


def test_no_raw_validation_error_escapes_for_unknown_enum(
    tmp_path: Path,
) -> None:
    """Belt-and-suspenders: the span case never leaks a bare ValidationError."""
    body = (
        'version: 1\nschema_version: "2.99"\n'
        "tracked_files:\n  a:\n    src: x\n    dst: y\n    disposition: pinned\n"
        "    spans:\n      - anchor: '## H'\n        kind: futurekind\n"
        "profiles:\n  default: {}\n"
    )
    cfg = _write(tmp_path, body)

    with pytest.raises(ConfigError):
        load_config(cfg)
    # A raw ValidationError would NOT be a ConfigError, so the assertion above
    # already guards the regression; assert the exception type explicitly too.
    try:
        load_config(cfg)
    except ValidationError:  # pragma: no cover - regression guard
        pytest.fail("raw ValidationError leaked for unknown enum value")
    except ConfigError:
        pass


def test_unknown_scope_value_reverts_to_default_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A defaulted mapping-keyed enum (``mcp_servers.<id>.scope``) reverts.

    ``scope`` defaults to ``user``; an unknown value strips to that default
    and warns rather than crashing.
    """
    body = (
        'version: 1\nschema_version: "2.99"\n'
        "tracked_files: {}\n"
        "mcp_servers:\n  s1:\n    command: ['x']\n    scope: futurescope\n"
        "profiles:\n  default: {}\n"
    )
    cfg = _write(tmp_path, body)

    config = load_config(cfg)  # must NOT raise

    assert config.mcp_servers["s1"].scope.value == "user"
    assert "mcp_servers.s1.scope" in capsys.readouterr().err


def test_genuine_validation_error_still_propagates(tmp_path: Path) -> None:
    """A real validation failure (missing required ``src``) is NOT swallowed."""
    body = (
        'version: 1\nschema_version: "2.0"\n'
        "tracked_files:\n  a:\n    dst: y\n"
        "profiles:\n  default: {}\n"
    )
    cfg = _write(tmp_path, body)

    with pytest.raises(ValidationError):
        load_config(cfg)
