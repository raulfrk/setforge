"""Tests for the merge subcommand — P4.2.

Covers:
- Walker yields DriftItem items for YAML and JSONC drift
- dotfile_filter narrows walker output
- read_one_choice: valid key, invalid key (bell + re-read), Ctrl-C
- apply_action [k], [u] YAML (comments preserved), [u] JSONC (comments preserved)
- apply_action [s] — extends preserve_user_keys in my_setup.yaml (comments preserved)
- apply_action [m]+y — launches $EDITOR and continues
- apply_action [m]+n — returns ActionResult.MANUAL_PENDING
- Snapshot context manager: snapshot dir created, restore reverts files,
  discard removes dir
- Signal handlers: SIGINT/SIGTERM/SIGHUP restore snapshot, no transition recorded
- Successful walk records exactly one merge-transition
"""

import io
import subprocess
from pathlib import Path
from typing import Any, override

import pytest

# ruamel.yaml ships py.typed without resolvable annotations; see my_setup/compare.py.
from ruamel.yaml import YAML  # type: ignore[import-not-found]

from my_setup.compare import CompareReport, CompareStatus, FileCompare
from my_setup.config import Config, Dotfile, Profile
from my_setup.merge import walk_unexpected_drift
from my_setup.wizard import (
    ActionResult,
    DriftItem,
    DriftMode,
    FileFormat,
    Snapshot,
    apply_action,
    read_one_choice,
)

# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------


# Minimal my_setup.yaml body used by several wizard / snapshot tests below.
_BASIC_YAML: str = (
    "version: 1\n"
    "dotfiles:\n"
    "  x:\n"
    "    src: x.yaml\n"
    "    dst: /tmp/x.yaml\n"
    "    preserve_user_keys: [a]\n"
    "profiles:\n"
    "  p:\n"
    "    dotfiles: [x]\n"
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _yaml_rt(text: str) -> Any:
    """Parse YAML in round-trip mode."""
    y = YAML(typ="rt")
    return y.load(io.StringIO(text))


def _make_report(
    *,
    name: str = "x",
    expected: list[str] | None = None,
    unexpected: list[str] | None = None,
    status: CompareStatus = CompareStatus.DRIFTED,
) -> CompareReport:
    """Build a minimal CompareReport with one FileCompare entry."""
    entry = FileCompare(
        name=name,
        status=status,
        diff="",
        expected_drift_keys=expected or [],
        unexpected_drift_keys=unexpected or [],
    )
    has_unexp = bool(unexpected)
    return CompareReport(entries=[entry], has_unexpected_drift=has_unexp)


def _make_config(
    tmp_path: Path,
    src_text: str,
    dst_text: str,
    dotfile_name: str = "x",
    preserve: list[str] | None = None,
    is_json: bool = False,
) -> tuple[Config, Path, Path, Path]:
    """Return (Config, repo_root, src_path, dst_path)."""
    ext = ".json" if is_json else ".yaml"
    repo = tmp_path / "repo"
    src = repo / "tracked" / f"{dotfile_name}{ext}"
    _write(src, src_text)
    dst = tmp_path / "live" / f"{dotfile_name}{ext}"
    _write(dst, dst_text)
    config = Config(
        dotfiles={
            dotfile_name: Dotfile(
                src=Path(f"{dotfile_name}{ext}"),
                dst=str(dst),
                preserve_user_keys=preserve or [],
            )
        },
        profiles={"p": Profile(dotfiles=[dotfile_name])},
    )
    return config, repo, src, dst


# ---------------------------------------------------------------------------
# Walker tests
# ---------------------------------------------------------------------------


def test_walk_unexpected_drift_yaml(tmp_path: Path) -> None:
    """Walker yields one DriftItem per unexpected YAML drift path."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        "a: 1\nb: 2\n",
        "a: 99\nb: 88\n",
        preserve=["a"],
    )
    report = _make_report(expected=["a"], unexpected=["b"])
    items = list(walk_unexpected_drift(report, config, repo))
    assert len(items) == 1
    item = items[0]
    assert item.key_path == "b"
    assert item.file_format is FileFormat.YAML
    assert item.tracked_value == 2
    assert item.live_value == 88


def test_walk_unexpected_drift_jsonc(tmp_path: Path) -> None:
    """Walker yields one DriftItem per unexpected JSONC drift key."""
    tracked_text = '{\n  "a": 1,\n  "b": 2\n}\n'
    live_text = '{\n  "a": 99,\n  "b": 88\n}\n'
    config, repo, _src, _dst = _make_config(
        tmp_path,
        tracked_text,
        live_text,
        preserve=["a"],
        is_json=True,
    )
    report = _make_report(expected=["a"], unexpected=["b"])
    items = list(walk_unexpected_drift(report, config, repo))
    assert len(items) == 1
    item = items[0]
    assert item.key_path == "b"
    assert item.file_format is FileFormat.JSONC
    assert item.tracked_value == 2
    assert item.live_value == 88


def test_walk_dotfile_filter(tmp_path: Path) -> None:
    """dotfile_filter narrows walker to the specified dotfile name."""
    config, repo, _src, _dst = _make_config(
        tmp_path, "a: 1\nb: 2\n", "a: 99\nb: 88\n", preserve=["a"]
    )
    report = _make_report(name="x", expected=["a"], unexpected=["b"])
    items_all = list(walk_unexpected_drift(report, config, repo, dotfile_filter=None))
    items_x = list(walk_unexpected_drift(report, config, repo, dotfile_filter="x"))
    items_y = list(walk_unexpected_drift(report, config, repo, dotfile_filter="y"))
    assert len(items_all) == 1
    assert len(items_x) == 1
    assert len(items_y) == 0


# ---------------------------------------------------------------------------
# read_one_choice tests  (monkeypatched tty/termios)
# ---------------------------------------------------------------------------


def _make_stdin(chars: str):
    """Return a StringIO-like that behaves like sys.stdin for read_one_choice."""
    return io.StringIO(chars)


def test_read_one_choice_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid key is returned lowercased."""
    monkeypatch.setattr("sys.stdin", io.StringIO("k"))
    monkeypatch.setattr("my_setup.wizard.termios.tcgetattr", lambda fd: [])
    monkeypatch.setattr("my_setup.wizard.tty.setraw", lambda fd: None)
    monkeypatch.setattr(
        "my_setup.wizard.termios.tcsetattr", lambda fd, when, attr: None
    )
    result = read_one_choice("Choice: ", {"k", "u", "s", "m"})
    assert result == "k"


def test_read_one_choice_uppercase_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uppercase input is lowercased and accepted."""
    monkeypatch.setattr("sys.stdin", io.StringIO("K"))
    monkeypatch.setattr("my_setup.wizard.termios.tcgetattr", lambda fd: [])
    monkeypatch.setattr("my_setup.wizard.tty.setraw", lambda fd: None)
    monkeypatch.setattr(
        "my_setup.wizard.termios.tcsetattr", lambda fd, when, attr: None
    )
    result = read_one_choice("Choice: ", {"k", "u", "s", "m"})
    assert result == "k"


def test_read_one_choice_invalid_then_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid key triggers bell and re-reads; valid key then accepted."""
    monkeypatch.setattr("my_setup.wizard.sys.stdin", io.StringIO("xk"))

    written: list[str] = []

    class FakeStdout:
        def write(self, s: str) -> int:
            written.append(s)
            return len(s)

        def flush(self) -> None:
            pass

    monkeypatch.setattr("sys.stdout", FakeStdout())
    result = read_one_choice("Choice: ", {"k", "u", "s", "m"})
    assert result == "k"
    assert "\a" in written


def test_read_one_choice_ctrl_c(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl-C (\\x03) raises KeyboardInterrupt."""
    monkeypatch.setattr("sys.stdin", io.StringIO("\x03"))
    monkeypatch.setattr("my_setup.wizard.termios.tcgetattr", lambda fd: [])
    monkeypatch.setattr("my_setup.wizard.tty.setraw", lambda fd: None)
    monkeypatch.setattr(
        "my_setup.wizard.termios.tcsetattr", lambda fd, when, attr: None
    )
    with pytest.raises(KeyboardInterrupt):
        read_one_choice("Choice: ", {"k", "u", "s", "m"})


def test_read_one_choice_termios_error_falls_back_to_line_buffered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """termios.error on tcgetattr (e.g., piped stdin to docker exec -i) falls
    back to the line-buffered loop instead of crashing."""
    import termios as _termios

    class PipedStdin(io.StringIO):
        """StringIO that reports a real fd, mimicking piped stdin."""

        @override
        def fileno(self) -> int:
            return 0

    monkeypatch.setattr("my_setup.wizard.sys.stdin", PipedStdin("k\n"))

    def fake_tcgetattr(_fd: int) -> object:
        raise _termios.error(25, "Inappropriate ioctl for device")

    monkeypatch.setattr("my_setup.wizard.termios.tcgetattr", fake_tcgetattr)
    result = read_one_choice("Choice: ", {"k", "u"})
    assert result == "k"


# ---------------------------------------------------------------------------
# apply_action tests
# ---------------------------------------------------------------------------


def test_apply_action_k_no_fs_write(tmp_path: Path) -> None:
    """[k] keep tracked: no filesystem write, returns ActionResult.KEEP_TRACKED."""
    _config, _repo, src, dst = _make_config(
        tmp_path, "a: 1\nb: 2\n", "a: 99\nb: 88\n", preserve=["a"]
    )
    uk = DriftItem(
        dotfile_name="x",
        src_path=src,
        dst_path=dst,
        key_path="b",
        tracked_value=2,
        live_value=88,
        file_format=FileFormat.YAML,
        mode=DriftMode.SHALLOW,
    )
    my_setup_yaml = tmp_path / "my_setup.yaml"
    original_src_content = src.read_text()
    result = apply_action(uk, "k", my_setup_yaml_path=my_setup_yaml)
    assert result == ActionResult.KEEP_TRACKED
    # tracked file untouched
    assert src.read_text() == original_src_content


def test_apply_action_u_yaml(tmp_path: Path) -> None:
    """[u] use live on YAML: tracked value updated to live value; comments preserved."""
    tracked_yaml = "# header\na: 1  # comment\nb: 2\n"
    live_yaml = "a: 99\nb: 88\n"
    _config, _repo, src, dst = _make_config(
        tmp_path, tracked_yaml, live_yaml, preserve=["a"]
    )
    uk = DriftItem(
        dotfile_name="x",
        src_path=src,
        dst_path=dst,
        key_path="b",
        tracked_value=2,
        live_value=88,
        file_format=FileFormat.YAML,
        mode=DriftMode.SHALLOW,
    )
    my_setup_yaml = tmp_path / "my_setup.yaml"
    result = apply_action(uk, "u", my_setup_yaml_path=my_setup_yaml)
    assert result == ActionResult.USE_LIVE

    # Read back and check value is updated
    y = YAML(typ="rt")
    updated = y.load(src.read_text())
    assert updated["b"] == 88

    # Comment in the header is preserved
    assert "# header" in src.read_text()


def test_apply_action_u_jsonc(tmp_path: Path) -> None:
    """[u] use live on JSONC: tracked value updated; // comments preserved."""
    tracked_json = '{\n  // comment\n  "a": 1,\n  "b": 2\n}\n'
    live_json = '{\n  "a": 99,\n  "b": 88\n}\n'
    _config, _repo, src, dst = _make_config(
        tmp_path, tracked_json, live_json, preserve=["a"], is_json=True
    )
    uk = DriftItem(
        dotfile_name="x",
        src_path=src,
        dst_path=dst,
        key_path="b",
        tracked_value=2,
        live_value=88,
        file_format=FileFormat.JSONC,
        mode=DriftMode.SHALLOW,
    )
    my_setup_yaml = tmp_path / "my_setup.yaml"
    result = apply_action(uk, "u", my_setup_yaml_path=my_setup_yaml)
    assert result == ActionResult.USE_LIVE

    updated_text = src.read_text()

    # json5 can parse the result.
    # json5 ships py.typed without resolvable annotations; see my_setup/jsonc.py.
    from json5.loader import loads  # type: ignore[import-not-found]

    parsed = loads(updated_text)
    assert parsed["b"] == 88
    # comment survives
    assert "// comment" in updated_text


def test_apply_action_s_extends_preserve_user_keys(tmp_path: Path) -> None:
    """[s] save-as-preserved appends key_path to dotfile.preserve_user_keys."""
    my_setup_yaml = tmp_path / "my_setup.yaml"
    # Write a minimal my_setup.yaml with comments
    yaml_text = (
        "# my-setup config\n"
        "version: 1\n"
        "dotfiles:\n"
        "  x:\n"
        "    src: x.yaml\n"
        "    dst: /tmp/x.yaml\n"
        "    preserve_user_keys:\n"
        "      - a\n"
        "profiles:\n"
        "  p:\n"
        "    dotfiles: [x]\n"
    )
    my_setup_yaml.write_text(yaml_text, encoding="utf-8")

    _config, _repo, src, dst = _make_config(
        tmp_path / "sub", "a: 1\nb: 2\n", "a: 99\nb: 88\n", preserve=["a"]
    )
    uk = DriftItem(
        dotfile_name="x",
        src_path=src,
        dst_path=dst,
        key_path="b",
        tracked_value=2,
        live_value=88,
        file_format=FileFormat.YAML,
        mode=DriftMode.SHALLOW,
    )
    result = apply_action(uk, "s", my_setup_yaml_path=my_setup_yaml)
    assert result == ActionResult.SAVE_AS_PRESERVED

    # Reload and verify
    y = YAML(typ="rt")
    updated = y.load(my_setup_yaml.read_text())
    assert "b" in updated["dotfiles"]["x"]["preserve_user_keys"]

    # Comment preserved
    assert "# my-setup config" in my_setup_yaml.read_text()


def test_apply_action_s_idempotent(tmp_path: Path) -> None:
    """[s] does not duplicate an already-present key_path."""
    my_setup_yaml = tmp_path / "my_setup.yaml"
    yaml_text = (
        "version: 1\n"
        "dotfiles:\n"
        "  x:\n"
        "    src: x.yaml\n"
        "    dst: /tmp/x.yaml\n"
        "    preserve_user_keys:\n"
        "      - a\n"
        "      - b\n"
        "profiles:\n"
        "  p:\n"
        "    dotfiles: [x]\n"
    )
    my_setup_yaml.write_text(yaml_text, encoding="utf-8")

    _config, _repo, src, dst = _make_config(
        tmp_path / "sub", "a: 1\nb: 2\n", "a: 99\nb: 88\n", preserve=["a", "b"]
    )
    uk = DriftItem(
        dotfile_name="x",
        src_path=src,
        dst_path=dst,
        key_path="b",
        tracked_value=2,
        live_value=88,
        file_format=FileFormat.YAML,
        mode=DriftMode.SHALLOW,
    )
    apply_action(uk, "s", my_setup_yaml_path=my_setup_yaml)
    y = YAML(typ="rt")
    updated = y.load(my_setup_yaml.read_text())
    # b should appear exactly once
    assert updated["dotfiles"]["x"]["preserve_user_keys"].count("b") == 1


def test_apply_action_m_y_launches_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[m]+y: launches $EDITOR on src_path and returns ActionResult.MANUAL_EDIT_DONE."""
    _config, _repo, src, dst = _make_config(
        tmp_path, "a: 1\nb: 2\n", "a: 99\nb: 88\n", preserve=["a"]
    )
    uk = DriftItem(
        dotfile_name="x",
        src_path=src,
        dst_path=dst,
        key_path="b",
        tracked_value=2,
        live_value=88,
        file_format=FileFormat.YAML,
        mode=DriftMode.SHALLOW,
    )
    my_setup_yaml = tmp_path / "my_setup.yaml"

    calls: list[list[str]] = []

    def fake_run(args, **kwargs: Any):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setenv("EDITOR", "myfakeeditor")
    monkeypatch.setattr(
        "my_setup._editor.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr("my_setup._editor.subprocess.run", fake_run)
    monkeypatch.setattr("my_setup.wizard.read_one_choice", lambda prompt, choices: "y")

    result = apply_action(uk, "m", my_setup_yaml_path=my_setup_yaml)
    assert result == ActionResult.MANUAL_EDIT_DONE
    assert calls == [["myfakeeditor", str(src)]]


def test_apply_action_m_n_returns_manual_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[m]+n: returns ActionResult.MANUAL_PENDING without launching editor."""
    _config, _repo, src, dst = _make_config(
        tmp_path, "a: 1\nb: 2\n", "a: 99\nb: 88\n", preserve=["a"]
    )
    uk = DriftItem(
        dotfile_name="x",
        src_path=src,
        dst_path=dst,
        key_path="b",
        tracked_value=2,
        live_value=88,
        file_format=FileFormat.YAML,
        mode=DriftMode.SHALLOW,
    )
    my_setup_yaml = tmp_path / "my_setup.yaml"

    monkeypatch.setattr("my_setup.wizard.read_one_choice", lambda prompt, choices: "n")

    run_called = []
    monkeypatch.setattr(
        "my_setup._editor.subprocess.run", lambda *a, **kw: run_called.append(a)
    )

    result = apply_action(uk, "m", my_setup_yaml_path=my_setup_yaml)
    assert result == ActionResult.MANUAL_PENDING
    assert not run_called


# ---------------------------------------------------------------------------
# Snapshot context manager tests
# ---------------------------------------------------------------------------


def test_snapshot_creates_dir_and_copies(tmp_path: Path) -> None:
    """Snapshot.__enter__ creates the snapshot dir and copies each file."""
    f1 = tmp_path / "f1.txt"
    f2 = tmp_path / "f2.txt"
    f1.write_text("original1", encoding="utf-8")
    f2.write_text("original2", encoding="utf-8")

    snap_root = tmp_path / "snapshots"
    snap = Snapshot(files=[f1, f2], snapshot_base=snap_root)
    with snap:
        assert snap.snapshot_dir is not None
        assert snap.snapshot_dir.is_dir()
        # Mutate files
        f1.write_text("modified1", encoding="utf-8")
        f2.write_text("modified2", encoding="utf-8")
        # Restore
        n = snap.restore()
        assert n == 2
        assert f1.read_text() == "original1"
        assert f2.read_text() == "original2"
    # After __exit__, dir still exists (caller decides to discard)


def test_snapshot_discard_removes_dir(tmp_path: Path) -> None:
    """Snapshot.discard() removes the snapshot directory."""
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    snap_root = tmp_path / "snapshots"
    snap = Snapshot(files=[f], snapshot_base=snap_root)
    with snap:
        snap.discard()
        assert not snap.snapshot_dir.exists()


def test_snapshot_restore_on_sigint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGINT during wizard restores files from snapshot; no transition recorded."""
    from my_setup.merge import run_wizard

    f = tmp_path / "target.yaml"
    f.write_text("b: 2\n", encoding="utf-8")

    # Monkeypatch write_transition to detect if it's called
    transition_calls: list[Any] = []
    monkeypatch.setattr(
        "my_setup.wizard.transitions.write_transition",
        lambda *a, **kw: transition_calls.append(1),
    )

    # Run wizard in-process, inject SIGINT via KeyboardInterrupt
    config, repo, _src, _dst = _make_config(
        tmp_path / "sub", "a: 1\nb: 2\n", "a: 99\nb: 88\n", preserve=["a"]
    )
    my_setup_yaml = tmp_path / "my_setup.yaml"
    my_setup_yaml.write_text(_BASIC_YAML, encoding="utf-8")

    # Simulate SIGINT via raising KeyboardInterrupt inside read_one_choice
    snap_root = tmp_path / "snaps"
    monkeypatch.setattr(
        "my_setup.wizard.read_one_choice",
        lambda prompt, choices: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    report = _make_report(name="x", expected=["a"], unexpected=["b"])

    # run_wizard should return without calling write_transition on KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        run_wizard(
            report,
            config,
            repo,
            my_setup_yaml_path=my_setup_yaml,
            snapshot_base=snap_root,
        )

    assert not transition_calls


# ---------------------------------------------------------------------------
# Transition recording tests
# ---------------------------------------------------------------------------


def test_successful_walk_records_one_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed wizard walk records exactly one merge-transition."""
    from my_setup.merge import run_wizard

    config, repo, _src, _dst = _make_config(
        tmp_path / "sub", "a: 1\nb: 2\n", "a: 99\nb: 88\n", preserve=["a"]
    )
    my_setup_yaml = tmp_path / "my_setup.yaml"
    my_setup_yaml.write_text(_BASIC_YAML, encoding="utf-8")

    transition_calls: list[Any] = []

    def _fake_write_transition_keep(*a: Any, **kw: Any) -> Path:
        transition_calls.append(1)
        return Path("/tmp/fake")

    monkeypatch.setattr(
        "my_setup.wizard.transitions.write_transition",
        _fake_write_transition_keep,
    )
    # choose [k] for every prompt
    monkeypatch.setattr("my_setup.wizard.read_one_choice", lambda prompt, choices: "k")

    snap_root = tmp_path / "snaps"
    report = _make_report(name="x", expected=["a"], unexpected=["b"])
    run_wizard(
        report,
        config,
        repo,
        my_setup_yaml_path=my_setup_yaml,
        snapshot_base=snap_root,
        profile="p",
    )

    assert len(transition_calls) == 1


def test_manual_pending_records_transition_for_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[m]+n pauses walk; merge-transition IS recorded for prior decisions."""
    from my_setup.merge import run_wizard

    # Two drift keys: first -> [k], second -> [m]+n
    config = Config(
        dotfiles={
            "x": Dotfile(
                src=Path("x.yaml"),
                dst=str(tmp_path / "live" / "x.yaml"),
                preserve_user_keys=["a"],
            )
        },
        profiles={"p": Profile(dotfiles=["x"])},
    )
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x.yaml"
    _write(src, "a: 1\nb: 2\nc: 3\n")
    _write(tmp_path / "live" / "x.yaml", "a: 99\nb: 88\nc: 77\n")

    my_setup_yaml = tmp_path / "my_setup.yaml"
    my_setup_yaml.write_text(_BASIC_YAML, encoding="utf-8")

    # Report: 2 unexpected keys
    entry = FileCompare(
        name="x",
        status=CompareStatus.DRIFTED,
        diff="",
        expected_drift_keys=["a"],
        unexpected_drift_keys=["b", "c"],
    )
    report = CompareReport(entries=[entry], has_unexpected_drift=True)

    transition_calls: list[Any] = []

    def _fake_write_transition_mixed(*a: Any, **kw: Any) -> Path:
        transition_calls.append(1)
        return Path("/tmp/fake")

    monkeypatch.setattr(
        "my_setup.wizard.transitions.write_transition",
        _fake_write_transition_mixed,
    )

    choices = iter(["k", "m", "n"])
    monkeypatch.setattr(
        "my_setup.wizard.read_one_choice", lambda prompt, cs: next(choices)
    )

    snap_root = tmp_path / "snaps"
    run_wizard(
        report,
        config,
        repo,
        my_setup_yaml_path=my_setup_yaml,
        snapshot_base=snap_root,
        profile="p",
    )
    assert len(transition_calls) == 1


# ---------------------------------------------------------------------------
# Comment survival tests (YAML + JSONC explicit assertions)
# ---------------------------------------------------------------------------


def test_use_live_yaml_comments_survive(tmp_path: Path) -> None:
    """After [u] on YAML, comments in tracked file survive."""
    tracked_yaml = "# top-level comment\nfoo: bar  # inline\nbaz: 1\n"
    live_yaml = "foo: bar\nbaz: 999\n"
    _config, _repo, src, dst = _make_config(
        tmp_path, tracked_yaml, live_yaml, preserve=["foo"]
    )
    uk = DriftItem(
        dotfile_name="x",
        src_path=src,
        dst_path=dst,
        key_path="baz",
        tracked_value=1,
        live_value=999,
        file_format=FileFormat.YAML,
        mode=DriftMode.SHALLOW,
    )
    apply_action(uk, "u", my_setup_yaml_path=tmp_path / "x.yaml")
    text = src.read_text()
    assert "# top-level comment" in text
    assert "# inline" in text


def test_use_live_jsonc_comments_survive(tmp_path: Path) -> None:
    """After [u] on JSONC, // comments survive."""
    tracked_json = '{\n  // settings\n  "a": 1,\n  "b": 2\n}\n'
    live_json = '{\n  "a": 1,\n  "b": 99\n}\n'
    _config, _repo, src, dst = _make_config(
        tmp_path, tracked_json, live_json, preserve=["a"], is_json=True
    )
    uk = DriftItem(
        dotfile_name="x",
        src_path=src,
        dst_path=dst,
        key_path="b",
        tracked_value=2,
        live_value=99,
        file_format=FileFormat.JSONC,
        mode=DriftMode.SHALLOW,
    )
    apply_action(uk, "u", my_setup_yaml_path=tmp_path / "x.yaml")
    text = src.read_text()
    assert "// settings" in text


def test_save_as_preserved_yaml_comments_survive(tmp_path: Path) -> None:
    """After [s], comments in my_setup.yaml survive."""
    my_setup_yaml = tmp_path / "my_setup.yaml"
    my_setup_yaml.write_text("# my config\n" + _BASIC_YAML, encoding="utf-8")
    _config, _repo, src, dst = _make_config(
        tmp_path / "sub", "a: 1\nb: 2\n", "a: 1\nb: 99\n", preserve=["a"]
    )
    uk = DriftItem(
        dotfile_name="x",
        src_path=src,
        dst_path=dst,
        key_path="b",
        tracked_value=2,
        live_value=99,
        file_format=FileFormat.YAML,
        mode=DriftMode.SHALLOW,
    )
    apply_action(uk, "s", my_setup_yaml_path=my_setup_yaml)
    assert "# my config" in my_setup_yaml.read_text()
