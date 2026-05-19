"""End-to-end tests for the ``setforge transitions`` sub-app.

Drives the real CLI via Typer's CliRunner against fixture transition
directories under a tmp ``SETFORGE_STATE_DIR``. Read-only — no install
or sync invocation needed.
"""

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Strip ANSI color/style escapes so substring asserts ignore Rich
    formatting. The polished ``transitions list/show`` output uses Rich,
    which emits ``\\x1b[1mheader\\x1b[0m`` even with ``highlight=False``
    (the bold-on-headers behavior of ``rich.table.Table`` is opt-in via
    ``show_header=True``)."""
    return _ANSI_RE.sub("", text)


def _stub(
    root: Path,
    *,
    dirname: str,
    profile: str,
    command: str = "install",
    timestamp: str = "2026-05-07T12:00:00+00:00",
    paths: list[str] | None = None,
    extensions_added: list[str] | None = None,
    extensions_removed: list[str] | None = None,
    patch_text: str | None = None,
) -> Path:
    """Materialize one transition directory and return its path. Mirrors
    the helper in test_transitions.py — kept independent here so the CLI
    tests don't import test-internal helpers across files."""
    target = root / dirname
    target.mkdir(parents=True, exist_ok=True)
    meta: dict[str, str | list[str]] = {
        "command": command,
        "profile": profile,
        "timestamp": timestamp,
        "host": "h",
        "version": "0.1.0",
    }
    if paths is not None:
        meta["paths"] = paths
    (target / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if extensions_added is not None or extensions_removed is not None:
        (target / "extensions.json").write_text(
            json.dumps(
                {
                    "added": extensions_added or [],
                    "removed": extensions_removed or [],
                }
            ),
            encoding="utf-8",
        )
    if patch_text is not None:
        (target / "changes.patch").write_text(patch_text, encoding="utf-8")
    return target


def test_list_empty_history_prints_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "ghost"))

    result = CliRunner().invoke(app, ["transitions", "list"])

    assert result.exit_code == 0, result.output
    assert "(no transitions)" in result.output


def test_list_renders_columns_newest_first_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``transitions list`` defaults to newest-first per mockup H, with
    columns id / type / age / files / plugins / ext."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    _stub(
        root,
        dirname="20260507T090000000000Z-install-vmh",
        profile="vmh",
        timestamp="2026-05-07T09:00:00+00:00",
    )
    _stub(
        root,
        dirname="20260507T170000000000Z-sync-vmh",
        profile="vmh",
        command="sync",
        timestamp="2026-05-07T17:00:00+00:00",
    )

    result = CliRunner().invoke(app, ["transitions", "list"])

    assert result.exit_code == 0, result.output
    clean = _strip_ansi(result.output)
    lines = clean.splitlines()
    header_line = next(line for line in lines if line.lstrip().startswith("id "))
    # Mockup-H columns: id / type / age / files / plugins / ext.
    assert "type" in header_line
    assert "age" in header_line
    assert "files" in header_line
    assert "plugins" in header_line
    assert "ext" in header_line
    install_idx = next(i for i, line in enumerate(lines) if "install-vmh" in line)
    sync_idx = next(i for i, line in enumerate(lines) if "sync-vmh" in line)
    # Newest-first default: sync (17:00) before install (09:00).
    assert sync_idx < install_idx
    # Footer suggestions are rendered.
    assert "to view details" in clean
    assert "to revert to BEFORE" in clean


def test_list_oldest_first_flips_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--oldest-first`` reverses the default newest-first ordering."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    _stub(root, dirname="20260507T090000000000Z-install-vmh", profile="vmh")
    _stub(
        root,
        dirname="20260507T170000000000Z-sync-vmh",
        profile="vmh",
        command="sync",
    )

    result = CliRunner().invoke(app, ["transitions", "list", "--oldest-first"])

    assert result.exit_code == 0, result.output
    lines = _strip_ansi(result.output).splitlines()
    install_idx = next(i for i, line in enumerate(lines) if "install-vmh" in line)
    sync_idx = next(i for i, line in enumerate(lines) if "sync-vmh" in line)
    assert install_idx < sync_idx


def test_list_profile_filter_repeatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    _stub(root, dirname="20260507T090000000000Z-install-vmh", profile="vmh")
    _stub(root, dirname="20260507T100000000000Z-install-ws", profile="ws")
    _stub(root, dirname="20260507T110000000000Z-install-other", profile="other")

    result = CliRunner().invoke(
        app, ["transitions", "list", "--profile=vmh", "--profile=ws"]
    )

    assert result.exit_code == 0, result.output
    assert "vmh" in result.output
    assert "ws" in result.output
    assert "other" not in result.output


def test_show_resolves_unique_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    _stub(
        root,
        dirname="20260507T120000000000Z-install-vmh",
        profile="vmh",
        paths=["/tmp/test-show-modified.txt"],
        # Sentinel patch with one modified file.
        patch_text=(
            "--- tmp/test-show-modified.txt\n"
            "+++ tmp/test-show-modified.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
    )

    result = CliRunner().invoke(app, ["transitions", "show", "20260507T1200"])

    assert result.exit_code == 0, result.output
    clean = _strip_ansi(result.output)
    # Mockup-H header line and per-field labels.
    assert "transition 20260507T120000000000Z-install-vmh" in clean
    assert "type:" in clean
    assert "profile:" in clean
    assert "start:" in clean
    assert "files mutated" in clean
    assert "/tmp/test-show-modified.txt" in clean
    # Reverse-this-transition footer per mockup H.
    assert "reverse this transition" in clean
    assert "--to-before=20260507T120000000000Z-install-vmh" in clean


def test_show_ambiguous_prefix_lists_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    _stub(root, dirname="20260507T120000000000Z-install-vmh", profile="vmh")
    _stub(
        root,
        dirname="20260507T130000000000Z-sync-vmh",
        profile="vmh",
        command="sync",
    )

    from setforge.errors import SetforgeError

    result = CliRunner().invoke(app, ["transitions", "show", "20260507T1"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SetforgeError)
    msg = str(result.exception)
    assert "matches 2 transitions" in msg
    assert "20260507T120000000000Z-install-vmh" in msg
    assert "20260507T130000000000Z-sync-vmh" in msg


def test_show_zero_match_prefix_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    _stub(root, dirname="20260507T120000000000Z-install-vmh", profile="vmh")

    from setforge.errors import SetforgeError

    result = CliRunner().invoke(app, ["transitions", "show", "19990101"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SetforgeError)


def test_show_omits_files_section_when_no_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extension-only transitions have no changes.patch; the FILES block
    is suppressed entirely (no empty section)."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    _stub(
        root,
        dirname="20260507T120000000000Z-install-vmh",
        profile="vmh",
        extensions_added=["x.y"],
    )

    result = CliRunner().invoke(app, ["transitions", "show", "20260507T1200"])

    assert result.exit_code == 0, result.output
    clean = _strip_ansi(result.output)
    assert "files mutated" not in clean
    assert "extensions:" in clean
    assert "x.y" in clean


def test_show_omits_extensions_section_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    _stub(
        root,
        dirname="20260507T120000000000Z-install-vmh",
        profile="vmh",
        paths=["/tmp/test-show-no-exts.txt"],
        patch_text=(
            "--- /dev/null\n+++ tmp/test-show-no-exts.txt\n@@ -0,0 +1 @@\n+hello\n"
        ),
    )

    result = CliRunner().invoke(app, ["transitions", "show", "20260507T1200"])

    assert result.exit_code == 0, result.output
    clean = _strip_ansi(result.output)
    assert "files mutated" in clean
    # mockup uses + marker for created files (vs M for modified, - for deleted).
    assert "+  /tmp/test-show-no-exts.txt" in clean
    assert "extensions:" not in clean
    assert "EXTENSIONS" not in clean


# ---------------------------------------------------------------------------
# sqcw mockup-H polish tests (compact age, polished show layout)
# ---------------------------------------------------------------------------


def test_list_human_age_format_h_d_m_seconds() -> None:
    """``_compact_age`` produces the mockup-H narrow column form."""
    from datetime import UTC, datetime, timedelta

    from setforge.cli.revert import _compact_age

    now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    # Seconds-old: <1m ago.
    assert _compact_age(now - timedelta(seconds=10), now) == "<1m ago"
    # Minutes (< 60).
    assert _compact_age(now - timedelta(minutes=5), now) == "5m ago"
    # Hours (< 24).
    assert _compact_age(now - timedelta(hours=2), now) == "2h ago"
    # Days.
    assert _compact_age(now - timedelta(days=3), now) == "3d ago"


def test_show_renders_command_and_profile_per_mockup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``transitions show`` emits ``type:`` and ``profile:`` per mockup H,
    along with the reverse-this-transition footer."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    _stub(
        root,
        dirname="20260518T203015000000Z-install-vm-headless",
        profile="vm-headless",
        timestamp="2026-05-18T20:30:15+00:00",
        paths=["/tmp/test-show-polish.txt"],
        patch_text=(
            "--- tmp/test-show-polish.txt\n"
            "+++ tmp/test-show-polish.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
    )

    result = CliRunner().invoke(app, ["transitions", "show", "20260518T2030"])

    assert result.exit_code == 0, result.output
    clean = _strip_ansi(result.output)
    assert "type:    install" in clean
    assert "profile: vm-headless" in clean
    assert "start:" in clean
    # diff stats per file per mockup.
    assert "diff:" in clean
    assert "--to-before=20260518T203015000000Z-install-vm-headless" in clean
