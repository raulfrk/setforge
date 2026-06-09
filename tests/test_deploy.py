"""Tests for the atomic deploy primitive."""

import os
import stat
from pathlib import Path
from typing import Any, NoReturn

import pytest

from setforge.config import Config, Profile, TrackedFile, resolve_profile
from setforge.deploy import (
    DeployAction,
    DeployResult,
    bootstrap_local,
    copy_atomic,
    validate_srcs_exist,
)
from setforge.errors import MissingTrackedFile


def test_fresh_deploy_creates_dst(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("hello\n")
    dst = tmp_path / "out" / "dst"
    result = copy_atomic(src, dst)
    assert isinstance(result, DeployResult)
    assert result.action is DeployAction.CREATED
    assert result.backup_path is None
    assert dst.read_text() == "hello\n"


def test_deploy_to_nested_workflows_dir_creates_parents(tmp_path: Path) -> None:
    """Deploying a workflows-category file creates the ~/.claude/workflows parent."""
    src = tmp_path / "session-workflow-impl.js"
    src.write_text("export const meta = {}\n")
    dst = tmp_path / ".claude" / "workflows" / "session-workflow-impl.js"
    result = copy_atomic(src, dst)
    assert result.action is DeployAction.CREATED
    assert result.backup_path is None
    assert dst.parent.is_dir()
    assert dst.read_text() == "export const meta = {}\n"


def test_redeploy_with_backup(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("new\n")
    dst = tmp_path / "dst"
    dst.write_text("old\n")
    result = copy_atomic(src, dst)
    assert result.action is DeployAction.UPDATED
    assert result.backup_path == Path(str(dst) + ".bak")
    assert result.backup_path.read_text() == "old\n"
    assert dst.read_text() == "new\n"


def test_redeploy_overwrites_existing_backup(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("v3\n")
    dst = tmp_path / "dst"
    dst.write_text("v2\n")
    bak = Path(str(dst) + ".bak")
    bak.write_text("v1\n")
    result = copy_atomic(src, dst)
    assert result.action is DeployAction.UPDATED
    assert bak.read_text() == "v2\n"
    assert dst.read_text() == "v3\n"


def test_redeploy_without_backup(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("new\n")
    dst = tmp_path / "dst"
    dst.write_text("old\n")
    result = copy_atomic(src, dst, backup=False)
    assert result.backup_path is None
    assert not Path(str(dst) + ".bak").exists()


def test_identical_content_is_noop(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("same\n")
    dst = tmp_path / "dst"
    dst.write_text("same\n")
    result = copy_atomic(src, dst)
    assert result.action is DeployAction.NOOP
    assert result.backup_path is None
    assert not Path(str(dst) + ".bak").exists()


def test_backup_update_never_loses_dst(tmp_path: Path) -> None:
    """An UPDATE that takes a backup leaves dst present with new content
    and the backup carrying old content — no window where dst is absent."""
    src = tmp_path / "src"
    src.write_text("new\n")
    dst = tmp_path / "dst"
    dst.write_text("old\n")

    result = copy_atomic(src, dst)

    assert dst.exists()
    assert dst.read_text() == "new\n"
    assert result.backup_path == Path(str(dst) + ".bak")
    assert result.backup_path.read_text() == "old\n"


def test_backup_does_not_follow_preexisting_bak_symlink(tmp_path: Path) -> None:
    """A pre-existing ``.bak`` symlink must be replaced, not written
    through — copy2 follows symlinks, so without an unlink the backup
    would clobber the link's target instead of snapshotting dst."""
    src = tmp_path / "src"
    src.write_text("new\n")
    dst = tmp_path / "dst"
    dst.write_text("old\n")
    victim = tmp_path / "victim"
    victim.write_text("KEEP\n")
    bak = Path(str(dst) + ".bak")
    bak.symlink_to(victim)

    result = copy_atomic(src, dst)

    assert victim.read_text() == "KEEP\n"  # target untouched
    assert not bak.is_symlink()  # link replaced by a regular file
    assert bak.read_text() == "old\n"  # backup snapshots the old content
    assert result.backup_path == bak
    assert dst.read_text() == "new\n"


def test_atomic_write_has_no_exdev_branch() -> None:
    """The cross-filesystem rescue branch (and its unlink-before-replace
    window) is gone: backup is now an unconditional copy + os.replace."""
    import setforge.deploy as deploy_mod

    src = Path(deploy_mod.__file__).read_text(encoding="utf-8")
    assert "errno.EXDEV" not in src
    assert "import errno" not in src


def test_mode_preserved(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("data\n")
    src.chmod(0o644)
    dst = tmp_path / "dst"
    copy_atomic(src, dst)
    assert stat.S_IMODE(dst.stat().st_mode) == 0o644


def test_dst_parent_created(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("x\n")
    dst = tmp_path / "deeply" / "nested" / "dst"
    copy_atomic(src, dst)
    assert dst.read_text() == "x\n"


def test_tmp_cleaned_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src"
    src.write_text("data\n")
    dst = tmp_path / "dst"

    def _boom(*args: Any, **kwargs: Any) -> NoReturn:
        raise OSError("simulated")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError, match="simulated"):
        copy_atomic(src, dst)
    leftover = list(tmp_path.glob(".dst.*.tmp"))
    assert leftover == []


def test_missing_src_raises(tmp_path: Path) -> None:
    with pytest.raises(MissingTrackedFile):
        copy_atomic(tmp_path / "ghost", tmp_path / "dst")


def test_bootstrap_local_creates_missing(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "file.md"
    bootstrap_local([target])
    assert target.exists()
    assert target.read_text() == ""


def test_bootstrap_local_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "file.md"
    target.write_text("existing\n")
    bootstrap_local([target])
    assert target.read_text() == "existing\n"


def _build_profile(tmp_path: Path, present: list[str], missing: list[str]):
    repo = tmp_path / "repo"
    live = tmp_path / "live"
    for name in present:
        (repo / "tracked" / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / "tracked" / name).write_text("data\n")
    cfg = Config(
        tracked_files={
            name: TrackedFile(src=Path(name), dst=str(live / name))
            for name in (*present, *missing)
        },
        profiles={"p": Profile(tracked_files=[*present, *missing])},
    )
    return repo, live, cfg, resolve_profile(cfg, "p")


def test_validate_srcs_exist_passes_when_all_present(tmp_path: Path) -> None:
    repo, _, cfg, resolved = _build_profile(tmp_path, ["a", "b"], [])
    validate_srcs_exist(cfg, resolved, repo)


def test_validate_srcs_exist_raises_with_single_missing(tmp_path: Path) -> None:
    repo, _, cfg, resolved = _build_profile(tmp_path, ["a"], ["ghost"])
    with pytest.raises(MissingTrackedFile, match="ghost"):
        validate_srcs_exist(cfg, resolved, repo)


def test_validate_srcs_exist_lists_all_missing(tmp_path: Path) -> None:
    repo, _, cfg, resolved = _build_profile(
        tmp_path, ["ok"], ["miss1", "miss2", "miss3"]
    )
    with pytest.raises(MissingTrackedFile) as exc_info:
        validate_srcs_exist(cfg, resolved, repo)
    msg = str(exc_info.value)
    assert "miss1" in msg
    assert "miss2" in msg
    assert "miss3" in msg


def test_validate_srcs_exist_failure_leaves_live_untouched(tmp_path: Path) -> None:
    """Pre-flight runs before any deploy, so a missing src must not
    leave any tracked_file half-applied to live.
    """
    repo, live, cfg, resolved = _build_profile(tmp_path, ["a"], ["ghost"])
    with pytest.raises(MissingTrackedFile):
        validate_srcs_exist(cfg, resolved, repo)
    assert not (live / "a").exists()
    assert not (live / "ghost").exists()
