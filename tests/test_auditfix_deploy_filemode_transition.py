"""Audit fix: install-applied file-mode changes must be reversible by revert.

Finding (Important / revert-completeness): a `setforge install` actively
changes a live file's permission bits — a content-NOOP mode-only fixup calls
``os.chmod`` and a content UPDATE fchmods to the tracked/source mode — but the
transition record snapshots only file CONTENT (a difflib patch). After a
``setforge revert`` the patch reverse restores the prior bytes while the file
keeps the install-applied mode, so revert is NOT a faithful inverse on the
mode axis (e.g. a 0600 secret retracked to 0644 stays 0644 after revert).

The faithful end-to-end fix (snapshot the pre-install mode into the transition
and chmod each reverted path back) spans ``transitions.py`` /
``cli/install.py`` / ``cli/_install_helpers.py`` / ``cli/revert.py`` — outside
this task's editable scope. The deploy-side enabler IS in scope: deploy must
SURFACE the pre-install mode it overwrote so the transition writer can record
it. These tests pin that ``DeployResult.prior_mode`` is populated exactly when
the deploy changes a pre-existing file's mode (the data revert needs) and is
``None`` otherwise. They fail on the pre-fix ``DeployResult`` (no such field /
always inert).
"""

import stat
from pathlib import Path

import setforge.deploy as deploy_mod
from setforge.deploy import copy_atomic


def test_noop_mode_only_fixup_records_prior_mode(tmp_path: Path) -> None:
    """Content-NOOP + mode-only chmod surfaces the overwritten mode.

    This is the regression-critical case: the content patch is EMPTY, so
    ``prior_mode`` is the only reversible record of the install's mode change.
    Pre-fix the result carried nothing and revert could never undo the chmod.
    """
    src = tmp_path / "src"
    src.write_text("same\n")
    src.chmod(0o644)
    dst = tmp_path / "dst"
    dst.write_text("same\n")
    dst.chmod(0o600)

    result = copy_atomic(src, dst, mode=0o644)

    assert result.action is deploy_mod.DeployAction.UPDATED
    assert stat.S_IMODE(dst.stat().st_mode) == 0o644
    # The mode revert needs: what the live file was BEFORE install chmod-ed it.
    assert result.prior_mode == 0o600


def test_content_update_with_mode_change_records_prior_mode(tmp_path: Path) -> None:
    """A content UPDATE that also tightens perms surfaces the prior mode."""
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
    assert result.prior_mode == 0o644


def test_content_update_mode_unchanged_records_no_prior_mode(tmp_path: Path) -> None:
    """A content UPDATE whose mode already matched leaves ``prior_mode`` None.

    Nothing to revert on the mode axis — the transition must not record a
    spurious chmod target.
    """
    src = tmp_path / "src"
    src.write_text("new\n")
    src.chmod(0o644)
    dst = tmp_path / "dst"
    dst.write_text("old\n")
    dst.chmod(0o644)

    result = copy_atomic(src, dst, mode=0o644)

    assert result.action is deploy_mod.DeployAction.UPDATED
    assert dst.read_text() == "new\n"
    assert result.prior_mode is None


def test_content_update_mode_none_matches_source_records_no_prior_mode(
    tmp_path: Path,
) -> None:
    """``mode=None`` falls back to the source mode; matching live → no record."""
    src = tmp_path / "src"
    src.write_text("new\n")
    src.chmod(0o640)
    dst = tmp_path / "dst"
    dst.write_text("old\n")
    dst.chmod(0o640)

    result = copy_atomic(src, dst)

    assert result.action is deploy_mod.DeployAction.UPDATED
    assert result.prior_mode is None


def test_content_update_mode_none_differs_from_source_records_prior_mode(
    tmp_path: Path,
) -> None:
    """``mode=None`` resolves to the SOURCE mode; a differing live mode is recorded."""
    src = tmp_path / "src"
    src.write_text("new\n")
    src.chmod(0o644)
    dst = tmp_path / "dst"
    dst.write_text("old\n")
    dst.chmod(0o600)

    result = copy_atomic(src, dst)

    assert result.action is deploy_mod.DeployAction.UPDATED
    assert stat.S_IMODE(dst.stat().st_mode) == 0o644
    assert result.prior_mode == 0o600


def test_fresh_create_records_no_prior_mode(tmp_path: Path) -> None:
    """A CREATE has no pre-existing mode to overwrite → ``prior_mode`` None."""
    src = tmp_path / "src"
    src.write_text("data\n")
    src.chmod(0o600)
    dst = tmp_path / "dst"

    result = copy_atomic(src, dst, mode=0o755)

    assert result.action is deploy_mod.DeployAction.CREATED
    assert result.prior_mode is None


def test_true_noop_records_no_prior_mode(tmp_path: Path) -> None:
    """Identical content AND matching mode is a true NOOP → ``prior_mode`` None."""
    src = tmp_path / "src"
    src.write_text("same\n")
    dst = tmp_path / "dst"
    dst.write_text("same\n")
    dst.chmod(0o644)

    result = copy_atomic(src, dst, mode=0o644)

    assert result.action is deploy_mod.DeployAction.NOOP
    assert result.prior_mode is None
