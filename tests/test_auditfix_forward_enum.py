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


def _write(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


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
