"""Call ``main()`` directly — ``CliRunner`` swallows exceptions first."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from setforge.cli import main

_BAD_CONFIG = """\
version: 1
schema_version: "1.0"
tracked_files: []
profiles:
  demo:
    tracked_files: []
"""


@pytest.fixture
def bad_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(_BAD_CONFIG)
    return cfg


def _run_main(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as excinfo:
        main()
    code = excinfo.value.code
    return code if isinstance(code, int) else 1


def test_main_renders_validation_error_politely(
    bad_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run_main(
        ["setforge", "compare", "--profile", "demo", "-c", str(bad_config)],
        monkeypatch,
    )
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "SCHEMA VALIDATION ERROR" in combined
    assert "Traceback (most recent call last)" not in combined
    assert "pydantic_core" not in combined
    assert "ValidationError" not in combined
    assert code == 1


def test_main_validation_error_attached_short_flag_anchors_real_path(
    bad_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run_main(
        ["setforge", "compare", "--profile", "demo", f"-c{bad_config}"],
        monkeypatch,
    )
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "SCHEMA VALIDATION ERROR" in combined
    assert f"({bad_config.name}:" in combined
    assert "(setforge.yaml:1)" not in combined
    assert code == 1


def test_main_json_validation_error_envelope(
    bad_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = [
        "setforge",
        "-o",
        "json",
        "compare",
        "--profile",
        "demo",
        "-c",
        str(bad_config),
    ]
    code = _run_main(argv, monkeypatch)
    captured = capsys.readouterr()
    assert "Traceback (most recent call last)" not in captured.err
    assert "pydantic_core" not in captured.err
    envelope = json.loads(captured.out)
    assert envelope["schema_version"] == 1
    assert envelope["errors"]
    assert any("SCHEMA VALIDATION ERROR" in e for e in envelope["errors"])
    assert code == 1
