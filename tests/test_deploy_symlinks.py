"""Tests for symlink-aware deploy (setforge-m483).

Contract for :func:`setforge.deploy.deploy_symlinked_file`:

- Writes tracked content to ``Path(tracked_file.symlink).expanduser()``
  (the *target* path).
- Creates a symbolic link at ``dst`` pointing at the *raw* user
  string (``tracked_file.symlink``), NOT the expanded path —
  cross-host portability invariant. ``os.readlink(dst)`` returns
  the unexpanded user string verbatim.
- Refuses (``SetforgeError``) when a regular file pre-exists at
  ``dst`` (anti-pattern check 4: guard before ``os.symlink``).
- Updates a pre-existing symlink at ``dst`` atomically via
  ``tmp + os.replace`` (no unlink/symlink gap).

The corresponding revert helper
(:func:`setforge.cli._install_helpers.revert_symlink_deployment`) contract:

- Refuses to unlink when the user retargeted the symlink (target
  drift since deploy).
- Refuses to unlink when a regular file is present at dst (user
  replaced the link with their own content).
- Otherwise unlinks via ``Path.unlink(missing_ok=False)``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from setforge import deploy
from setforge.cli._install_helpers import revert_symlink_deployment
from setforge.config import TrackedFile
from setforge.errors import SetforgeError


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

    result = deploy.deploy_symlinked_file(src, dst, tf)

    assert dst.is_symlink()
    assert os.readlink(dst) == str(target)
    assert target.is_file()
    assert target.read_text() == "payload\n"
    assert result.dst == dst


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

    deploy.deploy_symlinked_file(src, dst, tf)

    assert os.readlink(dst) == raw_target


def test_deploy_symlink_refuses_regular_file_at_dst(tmp_path: Path) -> None:
    """Pre-existing regular file at dst — refuses without clobbering."""
    src = tmp_path / "src"
    src.write_text("payload\n")
    target = tmp_path / "target"
    dst = tmp_path / "link"
    dst.write_text("user-content\n")  # regular file, not a symlink
    tf = _make(src, dst, symlink=str(target))

    with pytest.raises(SetforgeError) as exc_info:
        deploy.deploy_symlinked_file(src, dst, tf)
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
    os.symlink(str(old_target), dst)
    tf = _make(src, dst, symlink=str(new_target))

    deploy.deploy_symlinked_file(src, dst, tf)

    assert dst.is_symlink()
    assert os.readlink(dst) == str(new_target)
    assert new_target.read_text() == "new-payload\n"


def test_deploy_symlink_no_tmp_leftover(tmp_path: Path) -> None:
    """The staging tmp file at ``.<name>.setforge-symlink-tmp`` is removed."""
    src = tmp_path / "src"
    src.write_text("x\n")
    target = tmp_path / "target"
    dst = tmp_path / "link"
    tf = _make(src, dst, symlink=str(target))

    deploy.deploy_symlinked_file(src, dst, tf)

    tmp_link = dst.parent / f".{dst.name}.setforge-symlink-tmp"
    assert not tmp_link.exists()
    assert not tmp_link.is_symlink()


def test_revert_refuses_changed_symlink(tmp_path: Path) -> None:
    """``revert_symlink_deployment`` refuses to unlink when target drifts.

    User retargeted the symlink since deploy; revert MUST refuse
    rather than blindly unlinking — the link may now carry meaning
    setforge isn't responsible for.
    """
    dst = tmp_path / "link"
    user_retarget = "/tmp/user-retarget"
    os.symlink(user_retarget, dst)

    expected = "/tmp/setforge-original-target"
    with pytest.raises(SetforgeError) as exc_info:
        revert_symlink_deployment(dst, expected)
    msg = str(exc_info.value)
    assert "target changed" in msg
    assert expected in msg
    assert user_retarget in msg
    # Link NOT removed.
    assert dst.is_symlink()


def test_revert_unlinks_matching_symlink(tmp_path: Path) -> None:
    """``revert_symlink_deployment`` unlinks the link when target matches."""
    dst = tmp_path / "link"
    expected = "/tmp/setforge-target"
    os.symlink(expected, dst)
    assert dst.is_symlink()

    removed = revert_symlink_deployment(dst, expected)

    assert removed is True
    assert not dst.is_symlink()
    assert not dst.exists()


def test_revert_returns_false_when_link_absent(tmp_path: Path) -> None:
    """``revert_symlink_deployment`` is idempotent: no link, no error."""
    dst = tmp_path / "link"
    assert not dst.exists()

    removed = revert_symlink_deployment(dst, "/tmp/anything")
    assert removed is False


def test_revert_refuses_regular_file_at_dst(tmp_path: Path) -> None:
    """``revert_symlink_deployment`` refuses on a regular file at dst."""
    dst = tmp_path / "link"
    dst.write_text("user-data\n")  # regular file, not a symlink

    with pytest.raises(SetforgeError) as exc_info:
        revert_symlink_deployment(dst, "/tmp/expected")
    assert "regular file" in str(exc_info.value)
    # User data not touched.
    assert dst.read_text() == "user-data\n"
