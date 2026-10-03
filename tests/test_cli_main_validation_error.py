"""Call ``main()`` directly — ``CliRunner`` swallows exceptions first."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from setforge.cli import _config_path_from_argv, main

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


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # Spaced forms.
        (["setforge", "compare", "-c", "wanted.yaml"], "wanted.yaml"),
        (["setforge", "compare", "--config", "wanted.yaml"], "wanted.yaml"),
        # Attached long form (`=` stripped).
        (["setforge", "compare", "--config=wanted.yaml"], "wanted.yaml"),
        # Attached short form — the char(s) after `-c` are the value verbatim,
        # so a leading `=` is part of the path (Click does NOT strip it).
        (["setforge", "compare", "-cwanted.yaml"], "wanted.yaml"),
        (["setforge", "compare", "-c=wanted.yaml"], "=wanted.yaml"),
        # Stacked short flags: `-c` last in the cluster, spaced value.
        (["setforge", "compare", "-vc", "wanted.yaml"], "wanted.yaml"),
        # Stacked short flags: `-c` last in the cluster, attached value.
        (["setforge", "compare", "-vcwanted.yaml"], "wanted.yaml"),
        # A `-c…`-shaped token that is the VALUE of a preceding value-option
        # must not be scraped as `--config` (Click binds it to `-p`).
        (["setforge", "compare", "-p", "-cnot-a-config"], "setforge.yaml"),
        # No config flag at all → default.
        (["setforge", "compare", "--profile", "demo"], "setforge.yaml"),
    ],
)
def test_config_path_from_argv_matches_click_short_flag_parsing(
    argv: list[str],
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", argv)
    assert _config_path_from_argv() == Path(expected)


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


def test_main_json_domain_error_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Supported JSON commands render caught domain errors as one envelope."""
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text("version: 1\ntracked_files: {}\nprofiles: {}\n", encoding="utf-8")
    code = _run_main(
        [
            "setforge",
            "--format=json",
            "compare",
            "--profile=missing",
            f"--config={cfg}",
        ],
        monkeypatch,
    )
    captured = capsys.readouterr()
    assert "error:" not in captured.err
    envelope = json.loads(captured.out)
    assert envelope == {
        "schema_version": 1,
        "command": "error",
        "data": None,
        "errors": ["profile not found: missing"],
    }
    assert code == 1


def test_main_human_domain_error_stays_on_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Extracting the JSON branch preserves the established human diagnostic."""
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text("version: 1\ntracked_files: {}\nprofiles: {}\n", encoding="utf-8")
    code = _run_main(
        ["setforge", "compare", "--profile=missing", f"--config={cfg}"],
        monkeypatch,
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error: profile not found: missing" in captured.err
    assert code == 1


def _raise_from_app(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> int:
    def _fail() -> None:
        raise error

    monkeypatch.setattr("setforge.cli.app", _fail)
    return _run_main(["setforge", "install"], monkeypatch)


def test_main_renders_escaped_os_error_as_reason_and_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = PermissionError(13, "Permission denied", "/live/.note.txt.tmp")
    error.add_note("automatic recovery failed; the journal was retained: boom")

    code = _raise_from_app(monkeypatch, error)

    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        "error: Permission denied: /live/.note.txt.tmp",
        "automatic recovery failed; the journal was retained: boom",
    ]
    assert "Traceback" not in captured.out + captured.err
    assert code == 1


def test_main_names_the_exception_when_an_os_error_has_no_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _raise_from_app(monkeypatch, OSError(116, "Stale file handle"))

    assert capsys.readouterr().err == "error: OSError: [Errno 116] Stale file handle\n"
    assert code == 1


def test_main_keeps_os_error_traceback_for_debug_logging(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = OSError(5, "Input/output error", "/live/note.txt")

    with caplog.at_level(logging.DEBUG, logger="setforge.cli"):
        _raise_from_app(monkeypatch, error)

    assert [record.exc_info[1] for record in caplog.records if record.exc_info] == [
        error
    ]


def test_main_still_lets_programming_errors_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail() -> None:
        raise AttributeError("bug")

    monkeypatch.setattr("setforge.cli.app", _fail)
    monkeypatch.setattr(sys, "argv", ["setforge", "install"])

    with pytest.raises(AttributeError, match="bug"):
        main()
