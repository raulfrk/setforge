"""End-to-end tests for the ``setforge transitions`` sub-app.

Drives the real CLI via Typer's CliRunner against fixture transition
directories under a tmp ``SETFORGE_STATE_DIR``. Read-only — no install
or sync invocation needed.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app


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


def test_list_renders_columns_and_chronological_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    lines = result.output.splitlines()
    header_line = next(
        line for line in lines if "TIMESTAMP" in line and "DIRECTORY" in line
    )
    # PLUGINS column surfaces TransitionListing.plugin_count alongside
    # FILES/EXTS so plugin-only transitions aren't silently invisible
    # in the list view (the field was added by nen.13 but only wired
    # into the renderer in xj8).
    assert "FILES" in header_line
    assert "EXTS" in header_line
    assert "PLUGINS" in header_line
    install_idx = next(
        i for i, line in enumerate(lines) if "install" in line and "vmh" in line
    )
    sync_idx = next(
        i for i, line in enumerate(lines) if "sync" in line and "vmh" in line
    )
    assert install_idx < sync_idx


def test_list_reverse_flips_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    result = CliRunner().invoke(app, ["transitions", "list", "--reverse"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    install_idx = next(
        i for i, line in enumerate(lines) if "install" in line and "vmh" in line
    )
    sync_idx = next(
        i for i, line in enumerate(lines) if "sync" in line and "vmh" in line
    )
    assert sync_idx < install_idx


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
    assert "DIRECTORY" in result.output
    assert "20260507T120000000000Z-install-vmh" in result.output
    assert "FILES" in result.output
    assert "modified" in result.output
    assert "/tmp/test-show-modified.txt" in result.output


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

    from setforge.errors import MySetupError

    result = CliRunner().invoke(app, ["transitions", "show", "20260507T1"])

    assert result.exit_code == 1
    assert isinstance(result.exception, MySetupError)
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

    from setforge.errors import MySetupError

    result = CliRunner().invoke(app, ["transitions", "show", "19990101"])

    assert result.exit_code == 1
    assert isinstance(result.exception, MySetupError)


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
    assert "FILES" not in result.output
    assert "EXTENSIONS" in result.output
    assert "x.y" in result.output


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
    assert "FILES" in result.output
    assert "created" in result.output
    assert "EXTENSIONS" not in result.output
