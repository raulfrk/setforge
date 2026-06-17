"""Unit tests for :mod:`setforge.cli.section` — section add + emit."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from setforge.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# --- prompt_toolkit dialog test doubles (pytest monkeypatch style) ---


class _FakeDialog:
    """Stand-in for prompt_toolkit's ``Dialog`` return object.

    ``radiolist_dialog`` / ``input_dialog`` / ``yes_no_dialog`` each
    return a Dialog whose ``.run()`` yields the user's choice. Tests
    configure ``.run()`` to return a fixed value or walk an iterator
    of values (``side_effect``).
    """

    def __init__(
        self,
        *,
        return_value: object = None,
        side_effect: list[object] | None = None,
    ) -> None:
        self._return_value = return_value
        self._side_effect: Iterator[object] | None = (
            iter(side_effect) if side_effect is not None else None
        )
        self.run_calls = 0

    def run(self) -> object:
        self.run_calls += 1
        if self._side_effect is not None:
            return next(self._side_effect)
        return self._return_value


class _DialogRecorder:
    """Callable replacement for a prompt_toolkit dialog factory.

    Each call returns the same ``_FakeDialog`` so test setup can stage
    a sequence of ``.run()`` returns via ``side_effect`` and still
    introspect the dialog's invocation count.
    """

    def __init__(self, fake: _FakeDialog) -> None:
        self.fake = fake
        self.call_count = 0

    def __call__(self, *args: Any, **kwargs: Any) -> _FakeDialog:
        self.call_count += 1
        return self.fake


def _patch_section_dialogs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    radiolist_returns: list[object] | object = None,
    input_returns: list[object] | object = None,
    yes_no_returns: list[object] | object = None,
    pick_anchor_return: object = None,
) -> dict[str, _DialogRecorder]:
    """Install fakes for the four prompt_toolkit boundaries used by section add.

    Returns a dict keyed by dialog name so tests can introspect
    ``call_count`` / ``run_calls``. ``pick_anchor_line`` is a plain
    function (not a Dialog factory), so it gets a separate lambda
    patch and is not recorded here.
    """
    recorders: dict[str, _DialogRecorder] = {}
    for key, target, value in (
        ("radiolist", "setforge.cli.section.radiolist_dialog", radiolist_returns),
        ("input", "setforge.cli.section.input_dialog", input_returns),
        ("yes_no", "setforge.cli.section.yes_no_dialog", yes_no_returns),
    ):
        fake = (
            _FakeDialog(side_effect=value)
            if isinstance(value, list)
            else _FakeDialog(return_value=value)
        )
        rec = _DialogRecorder(fake)
        monkeypatch.setattr(target, rec)
        recorders[key] = rec
    monkeypatch.setattr(
        "setforge.cli.section.pick_anchor_line",
        lambda *_a, **_kw: pick_anchor_return,
    )
    monkeypatch.setattr("typer.prompt", lambda *_a, **_kw: "1")
    return recorders


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


def test_section_add_scripted_host_local_rejected(
    runner: CliRunner, minimal_config: tuple[Path, Path]
) -> None:
    """`section add` no longer authors host-local sections (markerless redesign).

    Host-local content is authored by editing the live file and running
    `section detect`; `section add` writing a host-local marker into tracked
    was the leak this redesign removes. The command must refuse and point at
    `section detect`, and must NOT write any marker into the tracked file.
    """
    yaml_path, tracked = minimal_config
    before = tracked.read_text()
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
    assert result.exit_code != 0
    assert "section detect" in result.output
    # No marker was written into the tracked file.
    assert tracked.read_text() == before
    assert "host-local bar" not in tracked.read_text()


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
    _patch_section_dialogs(
        monkeypatch,
        radiolist_returns=["empty"],
        input_returns="foo",
        yes_no_returns=True,
        pick_anchor_return=2,
    )
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
    _patch_section_dialogs(
        monkeypatch,
        radiolist_returns="shared",
        input_returns="foo",
        pick_anchor_return=None,
    )
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
    _patch_section_dialogs(
        monkeypatch,
        radiolist_returns=["empty"],
        input_returns="foo",
        yes_no_returns=False,
        pick_anchor_return=2,
    )
    result = runner.invoke(
        app, ["section", "add", "--profile=testp", f"--config={yaml_path}"]
    )
    assert result.exit_code == 0
    assert "shared foo" not in tracked.read_text()


def test_section_add_interactive_aborts_on_body_source_cancel(
    runner: CliRunner,
    minimal_config: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the body-source dialog aborts cleanly with no write.

    `section add` no longer prompts for semantics (it is always shared since
    the host-local path moved to `section detect`), so the body-source dialog
    is the first radiolist; cancelling it (``None``) must abort with exit 0 and
    leave the tracked file untouched.
    """
    yaml_path, tracked = minimal_config
    _force_tty(monkeypatch)
    _patch_section_dialogs(
        monkeypatch,
        radiolist_returns=None,
        input_returns="foo",
        pick_anchor_return=2,
    )
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
    recs = _patch_section_dialogs(
        monkeypatch,
        radiolist_returns=["empty"],
        input_returns="foo",
        yes_no_returns=True,
        pick_anchor_return=2,
    )
    result = runner.invoke(
        app,
        ["section", "add", "--profile=testp", f"--config={yaml_path}", "--yes"],
    )
    assert result.exit_code == 0
    assert "shared foo" in tracked.read_text()
    # --yes short-circuits the final confirm dialog; yes_no_dialog must
    # never have been called.
    assert recs["yes_no"].fake.run_calls == 0
    assert recs["yes_no"].call_count == 0


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
    recs = _patch_section_dialogs(
        monkeypatch,
        radiolist_returns=["empty"],
        # First call returns an invalid name; second call returns a valid one.
        input_returns=["BadName", "good-name"],
        yes_no_returns=True,
        pick_anchor_return=2,
    )
    result = runner.invoke(
        app, ["section", "add", "--profile=testp", f"--config={yaml_path}"]
    )
    assert result.exit_code == 0
    assert "shared good-name" in tracked.read_text()
    # input_dialog().run() was called twice — once with the invalid name,
    # once with the valid one (proving the re-prompt loop fired).
    assert recs["input"].fake.run_calls == 2
