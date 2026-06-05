"""``load_config`` forward-tolerance (p5qc.14.2).

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
    cfg = _write(tmp_path, _AT.format(ver='"2.0"'))
    with pytest.raises(ConfigError, match="upgrade setforge"):
        load_config(cfg)


def test_same_major_newer_minor_loads(tmp_path: Path) -> None:
    """A 1.9 config on a 1.1 engine tolerates (loads) — no refusal."""
    cfg = _write(tmp_path, _AT.format(ver='"1.9"'))
    config = load_config(cfg)
    assert config.schema_version == "1.9"


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
