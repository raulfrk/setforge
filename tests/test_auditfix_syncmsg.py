"""Regression: sync's Ctrl-C handler must actually restore the snapshot.

The sync/capture KeyboardInterrupt path used to print "cancelled (Ctrl-C);
files restored from snapshot" while ``capture_profile`` performed NO
snapshot or restore — so an interrupt after capture had already written a
tracked src (and advanced a stored base) left those writes in place and the
message was false. Worse, sync recorded its transition only AFTER capture
returned, so an interrupted sync left partially-captured tracked files AND
re-baselined stores with NO transition to revert.

The fix restores the pre-capture file + store snapshots that ``sync``
already takes, then reports the truth; plain ``capture`` (which takes no
snapshot) reports the partial-write truth instead.

* ``test_sync_ctrl_c_restores_tracked_and_base`` — drives install →
  live-edit → ``sync`` with ``capture_profile`` patched to mutate a tracked
  src + advance the base then raise ``KeyboardInterrupt``; asserts BOTH are
  restored byte-exact and the message is now true. Fails pre-fix (nothing
  restored).
* ``test_capture_ctrl_c_message_not_false_restore`` — asserts plain
  ``capture``'s Ctrl-C message does NOT claim files were restored.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store
from setforge.cli import app

_PROFILE = "test-syncmsg"
_MD_ID = "doc"

_DOC = """\
# Title

Original body.
"""

_DOC_LIVE_EDIT = _DOC.replace("Original body.", "MY LIVE EDIT.")


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.setforge_syncmsg/doc.md\n"
        "    disposition: shared\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - doc\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, md_body: str) -> None:
    tracked = repo / "tracked"
    tracked.mkdir(parents=True, exist_ok=True)
    (tracked / "doc.md").write_text(md_body, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _tracked_src(repo: Path) -> Path:
    return repo / "tracked" / "doc.md"


def _live_md() -> Path:
    return Path.home() / ".setforge_syncmsg" / "doc.md"


def _install(config: Path) -> Result:
    args = [
        "install",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-secrets-scan",
        "--no-git-check",
        "--yes",
    ]
    return CliRunner().invoke(app, args)


def _sync(config: Path) -> Result:
    args = [
        "sync",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--auto=use-live",
        "--yes",
    ]
    return CliRunner().invoke(app, args)


def test_sync_ctrl_c_restores_tracked_and_base(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted sync restores the tracked src AND the byte base.

    ``capture_profile`` is patched to commit a partial write (mutate the
    tracked src + advance the base) then raise ``KeyboardInterrupt``,
    reproducing the real "wrote some files, then Ctrl-C" hazard. Before the
    fix the sync handler printed "files restored from snapshot" but did NOT
    restore — both the tracked src and the base stayed mutated.
    """
    _write_tracked(repo, _DOC)
    config = _write_config(repo)

    assert _install(config).exit_code == 0
    pre_sync_tracked = _tracked_src(repo).read_bytes()
    pre_sync_base = base_store.read_base(_PROFILE, _MD_ID)
    assert pre_sync_base is not None

    _live_md().write_text(_DOC_LIVE_EDIT, encoding="utf-8")

    def _partial_then_interrupt(*_args: object, **_kwargs: object) -> list[object]:
        # Simulate capture committing one write + a base advance with no
        # internal rollback, then the user hitting Ctrl-C.
        _tracked_src(repo).write_text(_DOC_LIVE_EDIT, encoding="utf-8")
        base_store.write_base(_PROFILE, _MD_ID, b"ADVANCED-BASE-BYTES\n")
        raise KeyboardInterrupt

    # Patch the symbol as sync.py resolves it (capture_mod.capture_profile).
    monkeypatch.setattr(
        "setforge.cli.sync.capture_mod.capture_profile", _partial_then_interrupt
    )

    result = _sync(config)
    assert result.exit_code == 130, result.output
    # The message is only true now that the restore actually runs.
    assert "files restored from snapshot" in result.output

    # The load-bearing assertions: both halves restored to pre-sync state.
    assert _tracked_src(repo).read_bytes() == pre_sync_tracked
    assert base_store.read_base(_PROFILE, _MD_ID) == pre_sync_base


def test_capture_ctrl_c_message_not_false_restore(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain ``capture``'s Ctrl-C message must not claim a restore.

    ``capture`` takes no snapshot, so the old "files restored from
    snapshot" wording was always false on this path.
    """
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    _live_md().write_text(_DOC_LIVE_EDIT, encoding="utf-8")

    def _interrupt(*_args: object, **_kwargs: object) -> list[object]:
        raise KeyboardInterrupt

    monkeypatch.setattr("setforge.cli.sync.capture_mod.capture_profile", _interrupt)

    args = [
        "capture",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--auto=use-live",
    ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 130, result.output
    assert "restored from snapshot" not in result.output
    assert "partially written" in result.output
