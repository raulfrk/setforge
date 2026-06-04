"""Integration tests for the stored-base lifecycle in the install loop.

Drive the real ``setforge install`` CLI against a temp config repo with a
sandboxed ``$HOME`` + ``$SETFORGE_STATE_DIR`` and assert on the per-host
stored base (:mod:`setforge.base_store`) it seeds, advances, defers, and
prunes for ``disposition``-bearing tracked files.

The cases mirror Task 8's acceptance grid:

1. First install of a ``shared`` file seeds base == tracked bytes.
2. A second no-edit install is idempotent (base unchanged, no warning).
3. Non-overlapping live + tracked edits clean-merge into live; base advances.
4. Same-region edits under bare install keep live, warn, and DO NOT advance
   the base (so the conflict re-surfaces).
5. The same conflict under ``--auto=use-tracked`` takes tracked and advances.
6. A ``pinned`` file is never overwritten and never gets a base.
7. Dropping a ``shared`` file from the profile prunes its base.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store
from setforge.cli import app

_PROFILE = "test-disposition"
_FILE_ID = "shared_text"


def _write_config(
    repo: Path, *, disposition: str = "shared", include: bool = True
) -> Path:
    """Write a setforge.yaml; return its path.

    The profile always carries an inert ``anchor`` tracked file so it stays
    a valid non-empty list. ``disposition`` sets the ``shared_text`` file's
    policy. ``include=False`` drops ``shared_text`` from the profile (for
    the prune case) while keeping its tracked source on disk.
    """
    shared_line = "      - shared_text\n" if include else ""
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  shared_text:\n"
        "    src: text/note.txt\n"
        "    dst: ~/.setforge_disp/note.txt\n"
        f"    disposition: {disposition}\n"
        "  anchor:\n"
        "    src: text/anchor.txt\n"
        "    dst: ~/.setforge_disp/anchor.txt\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        f"{shared_line}"
        "      - anchor\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, body: str) -> None:
    """Write the tracked source bodies for ``shared_text`` and ``anchor``."""
    src = repo / "tracked" / "text" / "note.txt"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(body, encoding="utf-8")
    (src.parent / "anchor.txt").write_text("anchor\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp config repo with sandboxed ``$HOME`` + ``$SETFORGE_STATE_DIR``.

    The live dst (``~/.setforge_disp/note.txt``) lands under the sandbox
    home; the stored base lands under ``$SETFORGE_STATE_DIR/base/...``.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _live_path() -> Path:
    """Resolve the sandboxed live destination path."""
    return Path.home() / ".setforge_disp" / "note.txt"


def _install(config: Path, *, extra: list[str] | None = None) -> Result:
    """Run ``setforge install`` against ``config``; return the CliRunner result."""
    args = [
        "install",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-transition",
        "--no-secrets-scan",
        "--no-git-check",
        "--yes",
    ]
    if extra:
        args.extend(extra)
    return CliRunner().invoke(app, args)


def test_first_install_seeds_base(repo: Path) -> None:
    """First install of a shared file: live == tracked, base seeded == tracked."""
    tracked = "line1\nline2\n"
    _write_tracked(repo, tracked)
    config = _write_config(repo)

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert _live_path().read_text(encoding="utf-8") == tracked
    assert base_store.read_base(_PROFILE, _FILE_ID) == tracked.encode("utf-8")


def test_idempotent_second_install(repo: Path) -> None:
    """Re-install with no edits: base unchanged, no conflict warning."""
    tracked = "alpha\nbeta\n"
    _write_tracked(repo, tracked)
    config = _write_config(repo)

    assert _install(config).exit_code == 0
    base_after_first = base_store.read_base(_PROFILE, _FILE_ID)

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert base_store.read_base(_PROFILE, _FILE_ID) == base_after_first
    assert "conflict" not in result.output.lower()


def test_clean_merge_advances_base(repo: Path) -> None:
    """Non-overlapping live + tracked edits clean-merge; base advances to merge."""
    _write_tracked(repo, "header\nbody\nfooter\n")
    config = _write_config(repo)
    assert _install(config).exit_code == 0

    # Live edits the LAST line; tracked edits the FIRST line — disjoint hunks.
    _live_path().write_text("header\nbody\nfooter-EDITED\n", encoding="utf-8")
    _write_tracked(repo, "header-EDITED\nbody\nfooter\n")

    result = _install(config)
    assert result.exit_code == 0, result.output
    merged = _live_path().read_text(encoding="utf-8")
    assert "header-EDITED" in merged
    assert "footer-EDITED" in merged
    # Base advanced to the merged content (== current live).
    assert base_store.read_base(_PROFILE, _FILE_ID) == merged.encode("utf-8")


def test_conflict_bare_keeps_live_and_defers_base(repo: Path) -> None:
    """Same-region edits, bare install: live kept, warned, base NOT advanced."""
    _write_tracked(repo, "one\ntwo\nthree\n")
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    base_before = base_store.read_base(_PROFILE, _FILE_ID)

    # Both sides edit the SAME middle line → conflict.
    _live_path().write_text("one\ntwo-LIVE\nthree\n", encoding="utf-8")
    _write_tracked(repo, "one\ntwo-TRACKED\nthree\n")

    result = _install(config)
    assert result.exit_code == 0, result.output
    # Live kept its own edit.
    assert "two-LIVE" in _live_path().read_text(encoding="utf-8")
    # Conflict warning emitted (stderr is folded into output by CliRunner).
    assert "conflict" in result.output.lower()
    # Base NOT advanced — still the previous base so the next install
    # re-detects the divergence.
    assert base_store.read_base(_PROFILE, _FILE_ID) == base_before


def test_conflict_use_tracked_takes_tracked_and_advances(repo: Path) -> None:
    """Same conflict under --auto=use-tracked: live takes tracked, base advances."""
    _write_tracked(repo, "one\ntwo\nthree\n")
    config = _write_config(repo)
    assert _install(config).exit_code == 0

    _live_path().write_text("one\ntwo-LIVE\nthree\n", encoding="utf-8")
    _write_tracked(repo, "one\ntwo-TRACKED\nthree\n")

    result = _install(config, extra=["--auto=use-tracked"])
    assert result.exit_code == 0, result.output
    live = _live_path().read_text(encoding="utf-8")
    assert "two-TRACKED" in live
    assert "two-LIVE" not in live
    assert base_store.read_base(_PROFILE, _FILE_ID) == live.encode("utf-8")


def test_pinned_never_overwritten_no_base(repo: Path) -> None:
    """A pinned file: live wins, tracked never overwrites, no base written."""
    _write_tracked(repo, "tracked-body\n")
    config = _write_config(repo, disposition="pinned")
    # Pre-seed a live file the install must NOT clobber.
    live = _live_path()
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("LIVE-WINS\n", encoding="utf-8")

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert live.read_text(encoding="utf-8") == "LIVE-WINS\n"
    # pinned never re-baselines → no stored base.
    assert base_store.read_base(_PROFILE, _FILE_ID) is None


def test_prune_removes_dropped_file_base(repo: Path) -> None:
    """Removing a shared file from the profile prunes its base on next install."""
    _write_tracked(repo, "keepme\n")
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    assert base_store.read_base(_PROFILE, _FILE_ID) is not None

    # Re-write the config WITHOUT the tracked file in the profile.
    config = _write_config(repo, include=False)
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert base_store.read_base(_PROFILE, _FILE_ID) is None
