"""Unit tests for :mod:`setforge.cli.section` — section add + emit."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from setforge.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# --- section emit ---


def test_section_emit_shared(runner: CliRunner) -> None:
    result = runner.invoke(app, ["section", "emit", "shared", "foo"])
    assert result.exit_code == 0
    # Body between markers is a single "\n"; its sha256 is the stamped hash.
    import hashlib

    expected_hash = hashlib.sha256(b"\n").hexdigest()
    assert result.stdout == (
        "<!-- setforge:user-section start shared foo -->\n"
        "\n"
        f"<!-- setforge:user-section end shared foo hash={expected_hash} -->\n"
    )


def test_section_emit_host_local(runner: CliRunner) -> None:
    result = runner.invoke(app, ["section", "emit", "host-local", "bar"])
    assert result.exit_code == 0
    assert "<!-- setforge:user-section start host-local bar -->" in result.stdout
    assert "<!-- setforge:user-section end host-local bar hash=" in result.stdout


def test_section_emit_uses_setforge_namespace_not_legacy(runner: CliRunner) -> None:
    result = runner.invoke(app, ["section", "emit", "shared", "foo"])
    assert "setforge:user-section" in result.stdout
    assert "my-setup:user-section" not in result.stdout


@pytest.mark.parametrize("bad_name", ["Foo", "foo bar", "1foo", "foo!", "_foo"])
def test_section_emit_rejects_invalid_name(runner: CliRunner, bad_name: str) -> None:
    result = runner.invoke(app, ["section", "emit", "shared", bad_name])
    assert result.exit_code == 2


def test_section_emit_rejects_too_long_name(runner: CliRunner) -> None:
    too_long = "a" * 64
    result = runner.invoke(app, ["section", "emit", "shared", too_long])
    assert result.exit_code == 2


def test_section_emit_accepts_max_length_name(runner: CliRunner) -> None:
    max_len = "a" * 63
    result = runner.invoke(app, ["section", "emit", "shared", max_len])
    assert result.exit_code == 0


def test_section_emit_rejects_invalid_semantics(runner: CliRunner) -> None:
    result = runner.invoke(app, ["section", "emit", "weird", "foo"])
    assert result.exit_code == 2


def test_section_emit_rejects_empty_name(runner: CliRunner) -> None:
    result = runner.invoke(app, ["section", "emit", "shared", ""])
    assert result.exit_code == 2


def test_section_emit_no_extra_blanks(runner: CliRunner) -> None:
    result = runner.invoke(app, ["section", "emit", "shared", "foo"])
    assert result.stdout.count("\n") == 3


# --- section add (scripted path) ---


def _write_minimal_config(tmp_path: Path, *, suffix: str = ".md") -> tuple[Path, Path]:
    """Build a minimal setforge.yaml + a single tracked file. Return (yaml, tracked).

    Mirrors the canonical layout: ``setforge.yaml`` at repo root, tracked
    sources under ``<repo>/tracked/`` (resolved by :func:`resolve_src`).
    """
    tracked = tmp_path / "tracked" / f"doc{suffix}"
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("line 1\nline 2\nline 3\nline 4\nline 5\n")
    yaml_path = tmp_path / "setforge.yaml"
    yaml_path.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        f"    src: doc{suffix}\n"
        f"    dst: ~/.local/doc{suffix}\n"
        "profiles:\n"
        "  testp:\n"
        "    tracked_files: [doc]\n"
    )
    return yaml_path, tracked


@pytest.fixture
def minimal_config(tmp_path: Path) -> tuple[Path, Path]:
    """Standard md-suffix minimal config for happy-path scripted tests."""
    return _write_minimal_config(tmp_path)


def test_section_add_scripted_shared_empty_body(
    runner: CliRunner, minimal_config: tuple[Path, Path]
) -> None:
    yaml_path, tracked = minimal_config
    result = runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=shared",
            "--name=foo",
            "--anchor-line=2",
            "--body-source=empty",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    text = tracked.read_text()
    assert "<!-- setforge:user-section start shared foo -->" in text
    assert "<!-- setforge:user-section end shared foo hash=" in text
    lines = text.splitlines()
    assert lines[1] == "line 2"
    assert lines[2].startswith("<!-- setforge:user-section start shared foo")


def test_section_add_scripted_host_local_empty_body(
    runner: CliRunner, minimal_config: tuple[Path, Path]
) -> None:
    yaml_path, tracked = minimal_config
    result = runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=host-local",
            "--name=bar",
            "--anchor-line=3",
            "--body-source=empty",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    assert "host-local bar" in tracked.read_text()


@pytest.mark.parametrize("anchor", [1, 5])
def test_section_add_scripted_at_boundary_lines(
    runner: CliRunner, minimal_config: tuple[Path, Path], anchor: int
) -> None:
    yaml_path, _ = minimal_config
    result = runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=shared",
            "--name=foo",
            f"--anchor-line={anchor}",
            "--body-source=empty",
            "--yes",
        ],
    )
    assert result.exit_code == 0


def test_section_add_scripted_appends_second_pair_with_different_name(
    runner: CliRunner, minimal_config: tuple[Path, Path]
) -> None:
    yaml_path, tracked = minimal_config
    runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=shared",
            "--name=first",
            "--anchor-line=2",
            "--body-source=empty",
            "--yes",
        ],
    )
    result = runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=shared",
            "--name=second",
            "--anchor-line=2",
            "--body-source=empty",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    text = tracked.read_text()
    assert "shared first" in text
    assert "shared second" in text


@pytest.mark.parametrize("bad_name", ["Foo", "foo bar", "1foo", "foo!"])
def test_section_add_rejects_invalid_name(
    runner: CliRunner, minimal_config: tuple[Path, Path], bad_name: str
) -> None:
    yaml_path, _ = minimal_config
    result = runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=shared",
            f"--name={bad_name}",
            "--anchor-line=2",
            "--body-source=empty",
            "--yes",
        ],
    )
    assert result.exit_code == 2


def test_section_add_rejects_duplicate_name(
    runner: CliRunner, minimal_config: tuple[Path, Path]
) -> None:
    yaml_path, _ = minimal_config
    runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=shared",
            "--name=foo",
            "--anchor-line=2",
            "--body-source=empty",
            "--yes",
        ],
    )
    result = runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=shared",
            "--name=foo",
            "--anchor-line=3",
            "--body-source=empty",
            "--yes",
        ],
    )
    assert result.exit_code == 2


@pytest.mark.parametrize("suffix", [".json", ".yaml", ".txt", ".jsonc"])
def test_section_add_rejects_non_markdown(
    runner: CliRunner, tmp_path: Path, suffix: str
) -> None:
    yaml_path, _ = _write_minimal_config(tmp_path, suffix=suffix)
    result = runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=shared",
            "--name=foo",
            "--anchor-line=1",
            "--body-source=empty",
            "--yes",
        ],
    )
    assert result.exit_code == 2


@pytest.mark.parametrize("anchor", [0, -1, 100])
def test_section_add_rejects_invalid_anchor_line(
    runner: CliRunner, minimal_config: tuple[Path, Path], anchor: int
) -> None:
    yaml_path, _ = minimal_config
    result = runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=shared",
            "--name=foo",
            f"--anchor-line={anchor}",
            "--body-source=empty",
            "--yes",
        ],
    )
    assert result.exit_code == 2


def test_section_add_rejects_unknown_tracked_file(
    runner: CliRunner, minimal_config: tuple[Path, Path]
) -> None:
    yaml_path, _ = minimal_config
    result = runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=does-not-exist",
            "--semantics=shared",
            "--name=foo",
            "--anchor-line=1",
            "--body-source=empty",
            "--yes",
        ],
    )
    assert result.exit_code == 2


def test_section_add_rejects_unknown_semantics(
    runner: CliRunner, minimal_config: tuple[Path, Path]
) -> None:
    yaml_path, _ = minimal_config
    result = runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=weird",
            "--name=foo",
            "--anchor-line=1",
            "--body-source=empty",
            "--yes",
        ],
    )
    assert result.exit_code == 2


def test_section_add_rejects_unknown_body_source(
    runner: CliRunner, minimal_config: tuple[Path, Path]
) -> None:
    yaml_path, _ = minimal_config
    result = runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=shared",
            "--name=foo",
            "--anchor-line=1",
            "--body-source=weird",
            "--yes",
        ],
    )
    assert result.exit_code == 2


def test_section_add_rejects_body_file_with_empty_source(
    runner: CliRunner, minimal_config: tuple[Path, Path], tmp_path: Path
) -> None:
    yaml_path, _ = minimal_config
    body_path = tmp_path / "body.md"
    body_path.write_text("body content\n")
    result = runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=shared",
            "--name=foo",
            "--anchor-line=1",
            "--body-source=empty",
            f"--body-file={body_path}",
            "--yes",
        ],
    )
    assert result.exit_code == 2


def test_section_add_rejects_missing_body_file_when_source_is_file(
    runner: CliRunner, minimal_config: tuple[Path, Path]
) -> None:
    yaml_path, _ = minimal_config
    result = runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=shared",
            "--name=foo",
            "--anchor-line=1",
            "--body-source=file",
            "--yes",
        ],
    )
    assert result.exit_code == 2


def test_section_add_scripted_with_file_body_source(
    runner: CliRunner, minimal_config: tuple[Path, Path], tmp_path: Path
) -> None:
    yaml_path, tracked = minimal_config
    body_path = tmp_path / "body.md"
    body_path.write_text("user customization\n")
    result = runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=shared",
            "--name=foo",
            "--anchor-line=1",
            "--body-source=file",
            f"--body-file={body_path}",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    assert "user customization" in tracked.read_text()


def test_section_add_scripted_editor_body_source(
    runner: CliRunner,
    minimal_config: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_path, tracked = minimal_config

    def fake_editor(target: Path) -> None:
        target.write_text("edited body\n")

    monkeypatch.setattr("setforge.cli.section.run_editor", fake_editor)
    result = runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=shared",
            "--name=foo",
            "--anchor-line=1",
            "--body-source=editor",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    assert "edited body" in tracked.read_text()


def test_section_add_marker_pair_round_trips_through_extract_sections(
    runner: CliRunner, minimal_config: tuple[Path, Path]
) -> None:
    yaml_path, tracked = minimal_config
    runner.invoke(
        app,
        [
            "section",
            "add",
            "--profile=testp",
            f"--config={yaml_path}",
            "--tracked-file=doc",
            "--semantics=shared",
            "--name=rt",
            "--anchor-line=2",
            "--body-source=empty",
            "--yes",
        ],
    )
    from setforge.sections import extract_sections

    sections = extract_sections(tracked.read_text(), allow_legacy=True)
    assert "rt" in sections


# --- section add (interactive path; mock prompt_toolkit boundary) ---


def _force_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("setforge.cli.section._stdin_is_tty", lambda: True)


def test_section_add_interactive_walks_all_prompts(
    runner: CliRunner,
    minimal_config: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_path, tracked = minimal_config
    _force_tty(monkeypatch)
    with (
        patch("setforge.cli.section.radiolist_dialog") as rd,
        patch("setforge.cli.section.input_dialog") as ip,
        patch("setforge.cli.section.yes_no_dialog") as yn,
        patch("setforge.cli.section.pick_anchor_line", return_value=2),
        patch("typer.prompt", return_value="1"),
    ):
        rd.return_value.run.side_effect = ["shared", "empty"]
        ip.return_value.run.return_value = "foo"
        yn.return_value.run.return_value = True
        result = runner.invoke(
            app, ["section", "add", "--profile=testp", f"--config={yaml_path}"]
        )
    assert result.exit_code == 0
    assert "shared foo" in tracked.read_text()


def test_section_add_interactive_aborts_on_anchor_cancel(
    runner: CliRunner,
    minimal_config: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_path, tracked = minimal_config
    _force_tty(monkeypatch)
    with (
        patch("setforge.cli.section.radiolist_dialog") as rd,
        patch("setforge.cli.section.input_dialog") as ip,
        patch("setforge.cli.section.pick_anchor_line", return_value=None),
        patch("typer.prompt", return_value="1"),
    ):
        rd.return_value.run.return_value = "shared"
        ip.return_value.run.return_value = "foo"
        result = runner.invoke(
            app, ["section", "add", "--profile=testp", f"--config={yaml_path}"]
        )
    assert result.exit_code == 0
    assert "shared foo" not in tracked.read_text()


def test_section_add_interactive_aborts_on_confirm_no(
    runner: CliRunner,
    minimal_config: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_path, tracked = minimal_config
    _force_tty(monkeypatch)
    with (
        patch("setforge.cli.section.radiolist_dialog") as rd,
        patch("setforge.cli.section.input_dialog") as ip,
        patch("setforge.cli.section.yes_no_dialog") as yn,
        patch("setforge.cli.section.pick_anchor_line", return_value=2),
        patch("typer.prompt", return_value="1"),
    ):
        rd.return_value.run.side_effect = ["shared", "empty"]
        ip.return_value.run.return_value = "foo"
        yn.return_value.run.return_value = False
        result = runner.invoke(
            app, ["section", "add", "--profile=testp", f"--config={yaml_path}"]
        )
    assert result.exit_code == 0
    assert "shared foo" not in tracked.read_text()


def test_section_add_interactive_aborts_on_semantics_cancel(
    runner: CliRunner,
    minimal_config: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_path, tracked = minimal_config
    _force_tty(monkeypatch)
    with (
        patch("setforge.cli.section.radiolist_dialog") as rd,
        patch("typer.prompt", return_value="1"),
    ):
        rd.return_value.run.return_value = None
        result = runner.invoke(
            app, ["section", "add", "--profile=testp", f"--config={yaml_path}"]
        )
    assert result.exit_code == 0
    assert "shared" not in tracked.read_text()


def test_section_add_interactive_with_yes_skips_final_confirm(
    runner: CliRunner,
    minimal_config: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_path, tracked = minimal_config
    _force_tty(monkeypatch)
    with (
        patch("setforge.cli.section.radiolist_dialog") as rd,
        patch("setforge.cli.section.input_dialog") as ip,
        patch("setforge.cli.section.yes_no_dialog") as yn,
        patch("setforge.cli.section.pick_anchor_line", return_value=2),
        patch("typer.prompt", return_value="1"),
    ):
        rd.return_value.run.side_effect = ["shared", "empty"]
        ip.return_value.run.return_value = "foo"
        result = runner.invoke(
            app,
            ["section", "add", "--profile=testp", f"--config={yaml_path}", "--yes"],
        )
    assert result.exit_code == 0
    assert "shared foo" in tracked.read_text()
    yn.return_value.run.assert_not_called()


def test_section_add_interactive_non_tty_falls_back_to_error(
    runner: CliRunner,
    minimal_config: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_path, _ = minimal_config
    monkeypatch.setattr("setforge.cli.section._stdin_is_tty", lambda: False)
    result = runner.invoke(
        app, ["section", "add", "--profile=testp", f"--config={yaml_path}"]
    )
    assert result.exit_code == 2


def test_section_add_interactive_validates_user_name_input(
    runner: CliRunner,
    minimal_config: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid name from input_dialog re-prompts; second (valid) try wins."""
    yaml_path, tracked = minimal_config
    _force_tty(monkeypatch)
    with (
        patch("setforge.cli.section.radiolist_dialog") as rd,
        patch("setforge.cli.section.input_dialog") as ip,
        patch("setforge.cli.section.yes_no_dialog") as yn,
        patch("setforge.cli.section.pick_anchor_line", return_value=2),
        patch("typer.prompt", return_value="1"),
    ):
        rd.return_value.run.side_effect = ["shared", "empty"]
        # First call returns an invalid name; second call returns a valid one.
        ip.return_value.run.side_effect = ["BadName", "good-name"]
        yn.return_value.run.return_value = True
        result = runner.invoke(
            app, ["section", "add", "--profile=testp", f"--config={yaml_path}"]
        )
    assert result.exit_code == 0
    assert "shared good-name" in tracked.read_text()
    # input_dialog().run() was called twice — once with the invalid name,
    # once with the valid one (proving the re-prompt loop fired).
    assert ip.return_value.run.call_count == 2
