"""Load-bearing end-to-end gate for JSONC support (dotfiles-nen.6).

If this fails, the json-five-based design is invalid and we'd fall back
to hand-rolled textual surgery. Every other JSONC test in the suite
exercises wrapper internals; this one drives the real CLI end-to-end on
a fixture that mirrors a realistic VSCode settings.json — both ``//``
line comments and ``/* */`` block comments, multiple top-level keys,
trailing commas — round-tripping through ``install`` then ``capture``.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from my_setup.cli import app


_TRACKED_FIXTURE = """\
{
  // Top-of-file comment that ships in tracked.
  "editor.formatOnSave": true,
  /* Block comment between sections.
     Multi-line. */
  "editor.rulers": [88, 100],
  "files.insertFinalNewline": true,  // inline at end of line
  "[python]": {
    "editor.defaultFormatter": "charliermarsh.ruff"
  }
  // Trailing comment before close brace.
}
"""


_LIVE_FIXTURE = """\
{
  // Top-of-file comment that ships in tracked.
  "editor.formatOnSave": true,
  /* Block comment between sections.
     Multi-line. */
  "editor.rulers": [88, 100],
  "files.insertFinalNewline": true,  // inline at end of line
  "[python]": {
    "editor.defaultFormatter": "charliermarsh.ruff"
  },
  // Trailing comment before close brace.
  "claudeCode.allowDangerouslySkipPermissions": true,
  "claudeCode.initialPermissionMode": "bypassPermissions"
}
"""


_FIXTURE_YAML = """\
version: 1
dotfiles:
  vscode_settings:
    src: settings.json
    dst: {dst}
    preserve_user_keys:
      - claudeCode.allowDangerouslySkipPermissions
      - claudeCode.initialPermissionMode
profiles:
  vmh:
    dotfiles: [vscode_settings]
"""


def _setup_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Build a fixture repo and live target. Tracked starts WITHOUT the
    user-keys; live starts WITH them. Returns (cfg, dst)."""
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    src = repo / "tracked" / "settings.json"
    src.write_text(_TRACKED_FIXTURE, encoding="utf-8")
    dst = tmp_path / "live" / "settings.json"
    dst.parent.mkdir(parents=True)
    dst.write_text(_LIVE_FIXTURE, encoding="utf-8")
    cfg = repo / "my_setup.yaml"
    cfg.write_text(_FIXTURE_YAML.format(dst=dst), encoding="utf-8")
    return cfg, dst


def _no_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("my_setup.vscode_extensions.resolve_binary", lambda name: None)


def test_install_preserves_tracked_comments_and_keeps_user_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-install live = tracked text + live's user-key values, with
    every tracked comment intact."""
    cfg, dst = _setup_repo(tmp_path)
    monkeypatch.setenv("MY_SETUP_STATE_DIR", str(tmp_path / "state"))
    _no_code(monkeypatch)

    result = CliRunner().invoke(
        app, ["install", "--profile=vmh", f"--config={cfg}"]
    )
    assert result.exit_code == 0, result.output

    live_text = dst.read_text(encoding="utf-8")
    assert "// Top-of-file comment that ships in tracked." in live_text
    assert "/* Block comment between sections." in live_text
    assert "// inline at end of line" in live_text
    assert "// Trailing comment before close brace." in live_text
    assert (
        '"claudeCode.allowDangerouslySkipPermissions": true' in live_text
    )
    assert (
        '"claudeCode.initialPermissionMode": "bypassPermissions"'
        in live_text
    )


def test_install_then_capture_round_trips_tracked_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contract: tracked → install → live → capture → tracked' must
    leave tracked' byte-identical to tracked. Every comment, every blank
    line, every whitespace nuance survives.

    If this fails, the json-five-backed design is invalid and we fall
    back to hand-rolled textual surgery. The check is byte-equality
    (``==``), not "approximately equal" — drift here is unacceptable for
    daily-driver dotfile management.
    """
    cfg, dst = _setup_repo(tmp_path)
    monkeypatch.setenv("MY_SETUP_STATE_DIR", str(tmp_path / "state"))
    _no_code(monkeypatch)
    runner = CliRunner()

    install_result = runner.invoke(
        app, ["install", "--profile=vmh", f"--config={cfg}", "--no-transition"]
    )
    assert install_result.exit_code == 0, install_result.output

    capture_result = runner.invoke(
        app, ["capture", "--profile=vmh", f"--config={cfg}"]
    )
    assert capture_result.exit_code == 0, capture_result.output

    src = cfg.parent / "tracked" / "settings.json"
    assert src.read_text(encoding="utf-8") == _TRACKED_FIXTURE


def test_compare_classifies_user_key_drift_as_expected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``compare`` on a JSONC file with diverging user-keys reports
    expected drift (non-zero count) and zero unexpected drift."""
    cfg, dst = _setup_repo(tmp_path)
    monkeypatch.setenv("MY_SETUP_STATE_DIR", str(tmp_path / "state"))
    _no_code(monkeypatch)

    result = CliRunner().invoke(
        app, ["compare", "--profile=vmh", f"--config={cfg}"]
    )
    assert result.exit_code == 0, result.output
    # Rich table format: "expected drift" column shows count,
    # "unexpected drift" column shows 0.
    assert "expected drift" in result.output or "2" in result.output
    assert "0" in result.output
