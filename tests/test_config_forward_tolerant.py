"""``load_config`` forward-tolerance.

Cross-major refusal, same-major tolerate-and-warn, unknown-key warnings,
and clean errors on malformed ``schema_version`` — all asserted at the
``load_config`` boundary (the pre-validate gate + raw-key diff), never as
a raw Pydantic traceback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.config import load_config
from setforge.errors import ConfigError

_AT = (
    "version: 1\nschema_version: {ver}\n"
    "tracked_files: {{}}\nprofiles:\n  default: {{}}\n"
)


def _write(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def test_cross_major_newer_refuses_clean(tmp_path: Path) -> None:
    """A major-3 config on the major-2 engine refuses cleanly."""
    cfg = _write(tmp_path, _AT.format(ver='"3.0"'))
    with pytest.raises(ConfigError, match="upgrade setforge"):
        load_config(cfg)


def test_same_major_newer_minor_loads(tmp_path: Path) -> None:
    """A 2.9 config on a 2.0 engine tolerates (loads) — no refusal."""
    cfg = _write(tmp_path, _AT.format(ver='"2.9"'))
    config = load_config(cfg)
    assert config.schema_version == "2.9"


def test_malformed_schema_version_clean_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _AT.format(ver='"1.2.3"'))
    with pytest.raises(ConfigError, match="malformed schema_version"):
        load_config(cfg)


def test_unknown_key_warns_and_still_loads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A same-version typo is ignored (loads) but surfaced as a warning."""
    body = (
        'version: 1\nschema_version: "1.1"\n'
        "tracked_files: {}\nprofiles:\n  default: {}\nstray_typo: 1\n"
    )
    cfg = _write(tmp_path, body)
    config = load_config(cfg)
    assert config.version == 1
    assert "stray_typo" in capsys.readouterr().err


def test_nested_unknown_key_warned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = (
        'version: 1\nschema_version: "1.1"\n'
        "tracked_files:\n  a:\n    src: x\n    dst: y\n    tipo: true\n"
        "profiles:\n  default: {}\n"
    )
    cfg = _write(tmp_path, body)
    load_config(cfg)
    assert "tracked_files.a.tipo" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# minimum_version floor — hard-refuse below the operator-declared schema floor
# ---------------------------------------------------------------------------

_WITH_FLOOR = (
    "version: 1\nschema_version: {ver}\nminimum_version: {floor}\n"
    "tracked_files: {{}}\nprofiles:\n  default: {{}}\n"
)


def test_minimum_version_field_accepted(tmp_path: Path) -> None:
    """The optional field is part of the schema (not flagged as unknown)."""
    cfg = _write(tmp_path, _WITH_FLOOR.format(ver='"1.2"', floor='"1.2"'))
    config = load_config(cfg)
    assert config.minimum_version == "1.2"


def test_minimum_version_above_engine_same_major_refuses(tmp_path: Path) -> None:
    """Floor 2.5 on a 2.0 engine refuses — inside the same-major-tolerant window."""
    cfg = _write(tmp_path, _WITH_FLOOR.format(ver='"2.0"', floor='"2.5"'))
    with pytest.raises(ConfigError, match="minimum_version") as exc:
        load_config(cfg)
    assert "upgrade setforge" in str(exc.value)


def test_minimum_version_equal_engine_loads(tmp_path: Path) -> None:
    """Engine exactly AT the floor proceeds (boundary: strict below refuses)."""
    cfg = _write(tmp_path, _WITH_FLOOR.format(ver='"1.2"', floor='"1.2"'))
    assert load_config(cfg).minimum_version == "1.2"


def test_minimum_version_below_engine_loads(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _WITH_FLOOR.format(ver='"1.2"', floor='"1.1"'))
    assert load_config(cfg).minimum_version == "1.1"


def test_minimum_version_absent_loads(tmp_path: Path) -> None:
    """No floor declared ⇒ None ⇒ permit (no parse of None)."""
    cfg = _write(tmp_path, _AT.format(ver='"1.2"'))
    assert load_config(cfg).minimum_version is None


def test_minimum_version_malformed_clean_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _WITH_FLOOR.format(ver='"1.2"', floor='"1.2.3"'))
    with pytest.raises(ConfigError, match="malformed schema_version"):
        load_config(cfg)


def test_minimum_version_refuses_before_unknown_key_validation(tmp_path: Path) -> None:
    """Floor read from RAW data refuses even when a stray unknown key is present.

    Proves the gate runs pre-validation off the raw mapping — not off a
    validated Config attribute that the forward-tolerant strip would eat.
    """
    body = (
        'version: 1\nschema_version: "2.0"\nminimum_version: "2.5"\n'
        "tracked_files: {}\nprofiles:\n  default: {}\nstray_typo: 1\n"
    )
    cfg = _write(tmp_path, body)
    with pytest.raises(ConfigError, match="minimum_version"):
        load_config(cfg)
