"""Tests for symlink-aware deploy.

Contract for :func:`setforge.deploy.deploy_symlinked_file`:

- Writes tracked content to the declared target path, resolving a relative
  target from ``dst.parent``.
- Creates a symbolic link at ``dst`` pointing at the *raw* user
  string (``tracked_file.symlink``), NOT the expanded path —
  cross-host portability invariant. ``os.readlink(dst)`` returns
  the unexpanded user string verbatim.
- Refuses (``SetforgeError``) when a regular file pre-exists at
  ``dst`` (anti-pattern check 4: guard before ``os.symlink``).
- Updates a pre-existing symlink at ``dst`` atomically via
  ``tmp + os.replace`` (no unlink/symlink gap).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge import deploy
from setforge.config import TrackedFile
from setforge.errors import SetforgeError
from tests.verb_calls import deploy_symlinked_file


def _make(src: Path, dst: Path, *, symlink: str) -> TrackedFile:
    return TrackedFile.model_validate(
        {"src": str(src), "dst": str(dst), "symlink": symlink}
    )


def test_deploy_symlink_creates_both(tmp_path: Path) -> None:
    """Deploy lands a symlink at dst AND writes content to target."""
    src = tmp_path / "tracked-source"
    src.write_text("payload\n")
    target = tmp_path / "real-target"
    dst = tmp_path / "link"
    tf = _make(src, dst, symlink=str(target))

    result = deploy_symlinked_file(src, dst, tf)

    assert dst.is_symlink()
    assert str(dst.readlink()) == str(target)
    assert target.is_file()
    assert target.read_text() == "payload\n"
    assert result.dst == dst


def test_deploy_symlink_consumes_frozen_source_snapshot(tmp_path: Path) -> None:
    """Planned bytes and mode win over a checkout edit before pass 2."""
    src = tmp_path / "tracked-source"
    src.write_text("planned\n")
    target = tmp_path / "real-target"
    dst = tmp_path / "link"
    tf = _make(src, dst, symlink=str(target))
    frozen_mode = src.stat().st_mode & 0o777
    src.write_text("raced\n")

    deploy.deploy_symlinked_file(
        dst,
        tf,
        source_content=b"planned\n",
        source_mode=frozen_mode,
    )

    assert target.read_text() == "planned\n"
    assert target.stat().st_mode & 0o777 == frozen_mode


def test_deploy_symlink_preserves_raw_string_in_readlink(tmp_path: Path) -> None:
    """The raw user string survives ``os.symlink`` verbatim — no expansion.

    Anti-pattern check 3: applying :func:`Path.expanduser` to a
    user-declared symlink target before :func:`os.symlink` bakes the
    current host's ``$HOME`` into the link metadata, destroying
    cross-host portability. The on-disk link metadata
    (:func:`os.readlink`) must be the EXACT string passed in.
    """
    src = tmp_path / "src"
    src.write_text("x\n")
    dst = tmp_path / "link"
    raw_target = str(tmp_path / "preserved-as-passed")
    tf = _make(src, dst, symlink=raw_target)

    deploy_symlinked_file(src, dst, tf)

    assert str(dst.readlink()) == raw_target


def test_deploy_relative_symlink_target_from_link_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src"
    src.write_text("payload\n")
    dst = tmp_path / "links" / "link"
    raw_target = "targets/payload"
    tf = _make(src, dst, symlink=raw_target)
    unrelated_cwd = tmp_path / "elsewhere"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    deploy_symlinked_file(src, dst, tf)

    assert dst.is_symlink()
    assert str(dst.readlink()) == raw_target
    assert (dst.parent / raw_target).read_text() == "payload\n"
    assert not (unrelated_cwd / raw_target).exists()


def test_deploy_symlink_refuses_regular_file_at_dst(tmp_path: Path) -> None:
    """Pre-existing regular file at dst — refuses without clobbering."""
    src = tmp_path / "src"
    src.write_text("payload\n")
    target = tmp_path / "target"
    dst = tmp_path / "link"
    dst.write_text("user-content\n")  # regular file, not a symlink
    tf = _make(src, dst, symlink=str(target))

    with pytest.raises(SetforgeError) as exc_info:
        deploy_symlinked_file(src, dst, tf)
    assert "regular file" in str(exc_info.value)
    # User content NOT clobbered.
    assert dst.read_text() == "user-content\n"
    assert not dst.is_symlink()


def test_deploy_symlink_replaces_pre_existing_link(tmp_path: Path) -> None:
    """Pre-existing symlink at dst is replaced atomically (tmp + replace)."""
    src = tmp_path / "src"
    src.write_text("new-payload\n")
    old_target = tmp_path / "old-target"
    old_target.write_text("old\n")
    new_target = tmp_path / "new-target"
    dst = tmp_path / "link"
    dst.symlink_to(str(old_target))
    tf = _make(src, dst, symlink=str(new_target))

    deploy_symlinked_file(src, dst, tf)

    assert dst.is_symlink()
    assert str(dst.readlink()) == str(new_target)
    assert new_target.read_text() == "new-payload\n"


def test_deploy_symlink_noop_on_equal_target(tmp_path: Path) -> None:
    """Re-deploy of an already-correct symlink short-circuits to NOOP.

    Pre-fix: every re-install re-symlinked + ``os.replace``-d the link,
    showing ``UPDATED`` in install output and burning a syscall pair
    even when the link already pointed at the declared target. Fix:
    fast-path returns :attr:`DeployAction.NOOP` when
    ``os.readlink(dst) == raw_target``.
    """
    src = tmp_path / "src"
    src.write_text("payload\n")
    target = tmp_path / "target"
    dst = tmp_path / "link"
    raw_target = str(target)
    tf = _make(src, dst, symlink=raw_target)

    first = deploy_symlinked_file(src, dst, tf)
    assert first.action is deploy.DeployAction.CREATED

    # Second deploy: link is already correct; expect NOOP.
    second = deploy_symlinked_file(src, dst, tf)
    assert second.action is deploy.DeployAction.NOOP
    assert dst.is_symlink()
    assert str(dst.readlink()) == raw_target


def test_deploy_symlink_no_tmp_leftover(tmp_path: Path) -> None:
    """No staging tmp entry survives the deploy.

    The staging link uses a UNIQUE name (not a fixed one), so assert on the
    deploy result + final link and that no sibling lingers, rather than
    probing a hard-coded transient name.
    """
    src = tmp_path / "src"
    src.write_text("x\n")
    target = tmp_path / "target"
    dst = tmp_path / "link"
    tf = _make(src, dst, symlink=str(target))

    deploy_symlinked_file(src, dst, tf)

    assert dst.is_symlink()
    assert str(dst.readlink()) == str(target)
    assert sorted(path.name for path in dst.parent.iterdir()) == [
        "link",
        "src",
        "target",
    ]


def test_deploy_symlink_survives_stale_tmp_collision(tmp_path: Path) -> None:
    """A stale entry occupying the staging path cannot wedge the deploy.

    A crashed run can leave a directory (or foreign file) at the would-be
    fixed staging name. With a fixed staging name, ``unlink`` raises an
    un-suppressed ``IsADirectoryError`` (or ``symlink_to`` raises
    ``FileExistsError``), wedging the swap. The unique staging name
    sidesteps any such collision, so the deploy completes.
    """
    src = tmp_path / "src"
    src.write_text("payload\n")
    target = tmp_path / "target"
    dst = tmp_path / "link"
    tf = _make(src, dst, symlink=str(target))

    # Stale DIRECTORY squatting the old fixed staging name.
    stale = dst.parent / f".{dst.name}.setforge-symlink-tmp"
    stale.mkdir()

    result = deploy_symlinked_file(src, dst, tf)

    assert result.action is deploy.DeployAction.CREATED
    assert dst.is_symlink()
    assert str(dst.readlink()) == str(target)
    assert target.read_text() == "payload\n"
    # The stale collision is left untouched (not our entry to clean up).
    assert stale.is_dir()
