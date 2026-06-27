"""Integration: a PLAIN tracked file installs through the 3-way engine.

A0 routes a plain tracked file (no disposition, no spans, no host-local
overlay) through ``reconcile_apply.reconcile_plain_file`` instead of a
verbatim copy, so a local edit is merged against the recorded base rather
than silently overwritten. These tests drive the real ``install`` CLI
against a sandboxed ``$HOME`` + ``$SETFORGE_STATE_DIR`` and pin the
per-case behavior + the base-store side effect.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import reconcile
from setforge.cli import app

_PROFILE = "test-recon"


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  note:\n"
        "    src: note.md\n"
        "    dst: ~/.setforge_recon/note.md\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - note\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, body: str) -> None:
    tracked = repo / "tracked"
    tracked.mkdir(parents=True, exist_ok=True)
    (tracked / "note.md").write_text(body, encoding="utf-8")


def _live() -> Path:
    return Path.home() / ".setforge_recon" / "note.md"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _install(config: Path, *extra: str) -> Result:
    return CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--no-secrets-scan",
            "--no-git-check",
            "--yes",
            *extra,
        ],
    )


def _base() -> bytes | None:
    return reconcile.read_base(_PROFILE, reconcile.file_id("note"))


def test_first_install_creates_and_records_base(repo: Path) -> None:
    _write_tracked(repo, "v1\n")
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    assert _live().read_text(encoding="utf-8") == "v1\n"
    assert _base() == b"v1\n"


def test_upstream_change_fast_forwards_live(repo: Path) -> None:
    config = _write_config(repo)
    _write_tracked(repo, "v1\n")
    assert _install(config).exit_code == 0
    _write_tracked(repo, "v2\n")
    assert _install(config).exit_code == 0
    assert _live().read_text(encoding="utf-8") == "v2\n"
    assert _base() == b"v2\n"


def test_local_edit_preserved_when_upstream_unchanged(repo: Path) -> None:
    # The F1/F2 fix: a re-install does NOT clobber a local edit with the
    # tracked source when upstream did not change.
    config = _write_config(repo)
    _write_tracked(repo, "v1\n")
    assert _install(config).exit_code == 0
    _live().write_text("locally edited\n", encoding="utf-8")
    assert _install(config).exit_code == 0
    assert _live().read_text(encoding="utf-8") == "locally edited\n"


def test_divergent_live_without_base_seeds_and_keeps_live(repo: Path) -> None:
    # First install over a pre-existing, divergent live file (no base yet)
    # SEEDS the merge base from upstream non-interactively while KEEPING the
    # local file — never silently overwritten with the tracked source. The
    # recorded base means the next install reconciles instead of re-seeding.
    _write_tracked(repo, "tracked\n")
    config = _write_config(repo)
    live = _live()
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("pre-existing local\n", encoding="utf-8")
    result = _install(config)
    assert result.exit_code == 0
    assert live.read_text(encoding="utf-8") == "pre-existing local\n"
    # The base is seeded from upstream so the edit is now a tracked local
    # change atop it (the next install reconciles rather than re-prompts).
    assert _base() == b"tracked\n"
    assert "seeded the merge base" in result.output


def test_clean_reinstall_is_idempotent(repo: Path) -> None:
    config = _write_config(repo)
    _write_tracked(repo, "stable\n")
    assert _install(config).exit_code == 0
    # A second identical install changes nothing on disk.
    before = _live().read_text(encoding="utf-8")
    assert _install(config).exit_code == 0
    assert _live().read_text(encoding="utf-8") == before == "stable\n"


def _setup_conflict(repo: Path) -> Path:
    """Install v1, then diverge BOTH live and tracked → a real conflict."""
    config = _write_config(repo)
    _write_tracked(repo, "l1\nl2\nl3\n")
    assert _install(config).exit_code == 0
    _live().write_text("l1\nLOCAL\nl3\n", encoding="utf-8")
    _write_tracked(repo, "l1\nUPSTREAM\nl3\n")
    return config


def test_noninteractive_conflict_exits_nonzero_and_keeps_live(repo: Path) -> None:
    config = _setup_conflict(repo)
    result = _install(config)
    assert result.exit_code != 0, result.output
    assert "deferred with unresolved conflicts" in result.output
    assert _live().read_text(encoding="utf-8") == "l1\nLOCAL\nl3\n"


def test_auto_use_tracked_resolves_conflict_to_upstream(repo: Path) -> None:
    config = _setup_conflict(repo)
    result = _install(config, "--auto=use-tracked")
    assert result.exit_code == 0, result.output
    assert _live().read_text(encoding="utf-8") == "l1\nUPSTREAM\nl3\n"
    assert _base() == b"l1\nUPSTREAM\nl3\n"


def test_auto_keep_live_resolves_conflict_to_local(repo: Path) -> None:
    config = _setup_conflict(repo)
    result = _install(config, "--auto=keep-live")
    assert result.exit_code == 0, result.output
    # ours for the conflicting line, clean lines pass through; base advances.
    assert _live().read_text(encoding="utf-8") == "l1\nLOCAL\nl3\n"
    assert _base() == b"l1\nUPSTREAM\nl3\n"


def _revert(config: Path) -> Result:
    return CliRunner().invoke(
        app, ["revert", f"--profile={_PROFILE}", f"--config={config}", "--yes"]
    )


def test_revert_restores_base_so_reinstall_recreates(repo: Path) -> None:
    # Revert must restore the reconcile base, not just the live file: else the
    # next install sees a stale base (theirs == base) and treats the
    # reverted-away file as a deletion instead of re-deploying it.
    config = _write_config(repo)
    _write_tracked(repo, "v1\n")
    assert _install(config).exit_code == 0
    assert _base() == b"v1\n"

    assert _revert(config).exit_code == 0, "revert should succeed"
    assert not _live().exists(), "revert removes the created live file"
    assert _base() is None, "revert restores the pre-install (absent) base"

    # Reinstall re-creates the file (a fresh first install again).
    assert _install(config).exit_code == 0
    assert _live().read_text(encoding="utf-8") == "v1\n"
    assert _base() == b"v1\n"
