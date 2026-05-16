"""Tests for the atomic deploy primitive."""

import os
import stat
from pathlib import Path
from typing import Any, NoReturn

import pytest

from my_setup.deploy import (
    DeployAction,
    DeployResult,
    bootstrap_local,
    copy_atomic,
)
from my_setup.errors import MergeTypeMismatch


def test_fresh_deploy_creates_dst(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("hello\n")
    dst = tmp_path / "out" / "dst"
    result = copy_atomic(src, dst)
    assert isinstance(result, DeployResult)
    assert result.action is DeployAction.CREATED
    assert result.backup_path is None
    assert dst.read_text() == "hello\n"


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


def test_markdown_user_section_preserved(tmp_path: Path) -> None:
    src = tmp_path / "src.md"
    src.write_text(
        "header\n"
        "<!-- my-setup:user-section start host-local -->\n"
        "<!-- my-setup:user-section end host-local -->\n"
        "footer\n"
    )
    dst = tmp_path / "dst.md"
    dst.write_text(
        "old header\n"
        "<!-- my-setup:user-section start host-local -->\n"
        "USER CONTENT\n"
        "<!-- my-setup:user-section end host-local -->\n"
        "old footer\n"
    )
    copy_atomic(src, dst, preserve_user_sections=True)
    final = dst.read_text()
    assert "header\n" in final
    assert "USER CONTENT\n" in final
    assert "footer\n" in final


def test_yaml_user_keys_preserved(tmp_path: Path) -> None:
    src = tmp_path / "src.yaml"
    src.write_text("a: 1\nb: 2\nc: 3\n")
    dst = tmp_path / "dst.yaml"
    dst.write_text("a: 10\nb: 20\nc: 30\n")
    copy_atomic(src, dst, preserve_user_keys=["a", "c"])
    text = dst.read_text()
    assert "a: 10" in text
    assert "b: 2" in text
    assert "c: 30" in text


def test_yaml_user_keys_type_mismatch_raises(tmp_path: Path) -> None:
    src = tmp_path / "src.yaml"
    src.write_text("a: scalar\n")
    dst = tmp_path / "dst.yaml"
    dst.write_text("a:\n  - 1\n  - 2\n")
    with pytest.raises(MergeTypeMismatch):
        copy_atomic(src, dst, preserve_user_keys=["a"])


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
    from my_setup.errors import MissingTrackedFile

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
    from my_setup.config import Config, Dotfile, Profile, resolve_profile

    repo = tmp_path / "repo"
    live = tmp_path / "live"
    for name in present:
        (repo / "tracked" / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / "tracked" / name).write_text("data\n")
    cfg = Config(
        dotfiles={
            name: Dotfile(src=Path(name), dst=str(live / name))
            for name in (*present, *missing)
        },
        profiles={"p": Profile(dotfiles=[*present, *missing])},
    )
    return repo, live, cfg, resolve_profile(cfg, "p")


def test_validate_srcs_exist_passes_when_all_present(tmp_path: Path) -> None:
    from my_setup.deploy import validate_srcs_exist

    repo, _, cfg, resolved = _build_profile(tmp_path, ["a", "b"], [])
    validate_srcs_exist(cfg, resolved, repo)


def test_validate_srcs_exist_raises_with_single_missing(tmp_path: Path) -> None:
    from my_setup.deploy import validate_srcs_exist
    from my_setup.errors import MissingTrackedFile

    repo, _, cfg, resolved = _build_profile(tmp_path, ["a"], ["ghost"])
    with pytest.raises(MissingTrackedFile, match="ghost"):
        validate_srcs_exist(cfg, resolved, repo)


def test_validate_srcs_exist_lists_all_missing(tmp_path: Path) -> None:
    from my_setup.deploy import validate_srcs_exist
    from my_setup.errors import MissingTrackedFile

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
    leave any dotfile half-applied to live.
    """
    from my_setup.deploy import validate_srcs_exist
    from my_setup.errors import MissingTrackedFile

    repo, live, cfg, resolved = _build_profile(tmp_path, ["a"], ["ghost"])
    with pytest.raises(MissingTrackedFile):
        validate_srcs_exist(cfg, resolved, repo)
    assert not (live / "a").exists()
    assert not (live / "ghost").exists()
