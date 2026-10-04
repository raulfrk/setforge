"""Deploy applies the tracked mode and classifies each write.

A content-NOOP mode-only fixup chmods the live file and a content UPDATE
fchmods to the tracked/source mode. The transition records the mode the
deploy replaced in the file's pre image, so ``revert`` restores it (see
``tests/test_auditfix_deploy_filemode_revert.py``).
"""

import stat
from pathlib import Path

import setforge.deploy as deploy_mod
from tests.verb_calls import copy_atomic


def test_noop_mode_only_fixup_applies_the_mode(tmp_path: Path) -> None:
    """Content-NOOP + mode-only chmod is an UPDATE that applies the mode."""
    src = tmp_path / "src"
    src.write_text("same\n")
    src.chmod(0o644)
    dst = tmp_path / "dst"
    dst.write_text("same\n")
    dst.chmod(0o600)

    result = copy_atomic(src, dst, mode=0o644)

    assert result.action is deploy_mod.DeployAction.UPDATED
    assert stat.S_IMODE(dst.stat().st_mode) == 0o644


def test_content_update_with_mode_change_applies_the_mode(tmp_path: Path) -> None:
    """A content UPDATE that also tightens perms applies the new mode."""
    src = tmp_path / "src"
    src.write_text("new\n")
    src.chmod(0o644)
    dst = tmp_path / "dst"
    dst.write_text("old\n")
    dst.chmod(0o644)

    result = copy_atomic(src, dst, mode=0o600)

    assert result.action is deploy_mod.DeployAction.UPDATED
    assert dst.read_text() == "new\n"
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600


def test_content_update_mode_unchanged_is_an_update(tmp_path: Path) -> None:
    """A content UPDATE whose mode already matched is a plain UPDATE."""
    src = tmp_path / "src"
    src.write_text("new\n")
    src.chmod(0o644)
    dst = tmp_path / "dst"
    dst.write_text("old\n")
    dst.chmod(0o644)

    result = copy_atomic(src, dst, mode=0o644)

    assert result.action is deploy_mod.DeployAction.UPDATED
    assert dst.read_text() == "new\n"


def test_content_update_mode_none_matches_source_is_an_update(
    tmp_path: Path,
) -> None:
    """``mode=None`` falls back to the source mode, which live already has."""
    src = tmp_path / "src"
    src.write_text("new\n")
    src.chmod(0o640)
    dst = tmp_path / "dst"
    dst.write_text("old\n")
    dst.chmod(0o640)

    result = copy_atomic(src, dst)

    assert result.action is deploy_mod.DeployAction.UPDATED


def test_content_update_mode_none_differs_from_source_applies_source_mode(
    tmp_path: Path,
) -> None:
    """``mode=None`` resolves to the SOURCE mode, replacing a differing live mode."""
    src = tmp_path / "src"
    src.write_text("new\n")
    src.chmod(0o644)
    dst = tmp_path / "dst"
    dst.write_text("old\n")
    dst.chmod(0o600)

    result = copy_atomic(src, dst)

    assert result.action is deploy_mod.DeployAction.UPDATED
    assert stat.S_IMODE(dst.stat().st_mode) == 0o644


def test_fresh_create_is_a_create(tmp_path: Path) -> None:
    """A destination that did not exist is a CREATE."""
    src = tmp_path / "src"
    src.write_text("data\n")
    src.chmod(0o600)
    dst = tmp_path / "dst"

    result = copy_atomic(src, dst, mode=0o755)

    assert result.action is deploy_mod.DeployAction.CREATED


def test_true_noop_is_a_noop(tmp_path: Path) -> None:
    """Identical content AND matching mode is a true NOOP."""
    src = tmp_path / "src"
    src.write_text("same\n")
    dst = tmp_path / "dst"
    dst.write_text("same\n")
    dst.chmod(0o644)

    result = copy_atomic(src, dst, mode=0o644)

    assert result.action is deploy_mod.DeployAction.NOOP
