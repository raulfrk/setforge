"""Pre-flight the symlink-dst clobber refusal so install stays all-or-nothing.

Regression for the partial-install audit finding: a symlink tracked_file
ordered AFTER regular-file tracked_files whose ``dst`` already holds a regular
file used to let the earlier regular files deploy and THEN abort in pass 2 with
no transition recorded (an un-revertable partial install). The fix moves the
``deploy_symlinked_file`` dst-conflict refusal to a pass-1 refuse-before-write
gate (:func:`setforge.cli.install._refuse_on_symlink_dst_conflicts`), so the
install aborts before any write — nothing deployed, no transition landed.

Drives the real ``setforge install`` CLI against a temp config repo with a
sandboxed ``$HOME`` + ``$SETFORGE_STATE_DIR``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import transitions
from setforge.cli import app

_PROFILE = "test-preflight"

_DOC_A = "# A\n\nbody a\n"
_DOC_Z = "# Z\n\nbody z\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _live_dir() -> Path:
    return Path.home() / ".setforge_preflight"


def _install(config: Path, *, transition: bool = False) -> Result:
    args = [
        "install",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-secrets-scan",
        "--no-git-check",
        "--yes",
    ]
    if not transition:
        args.append("--no-transition")
    return CliRunner().invoke(app, args)


def _transition_count() -> int:
    root = transitions.transitions_root()
    if not root.exists():
        return 0
    return sum(1 for entry in root.iterdir() if entry.is_dir())


def _write_tracked(repo: Path, name: str, body: str) -> None:
    src = repo / "tracked" / f"{name}.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(body, encoding="utf-8")


def _write_config(repo: Path, *, link_target: Path) -> Path:
    """Regular-file ``a`` (deploys first) before symlink ``z`` (refused)."""
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  a:\n"
        "    src: a.md\n"
        "    dst: ~/.setforge_preflight/a.md\n"
        "  z:\n"
        "    src: z.md\n"
        "    dst: ~/.setforge_preflight/z-link\n"
        f"    symlink: {link_target}\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - a\n"
        "      - z\n",
        encoding="utf-8",
    )
    return config


def test_preflight_refuses_before_writing_earlier_regular_file(repo: Path) -> None:
    """A regular file at the symlink dst aborts install BEFORE deploying ``a``.

    Pre-fix the regular file ``a`` (earlier in profile order) was written by
    pass 2 before ``z``'s deploy raised, leaving a partial install with no
    transition. The pre-flight gate now refuses before any write: ``a`` is
    never created and zero transitions land.
    """
    _write_tracked(repo, "a", _DOC_A)
    _write_tracked(repo, "z", _DOC_Z)
    link_target = _live_dir() / "z-target"
    config = _write_config(repo, link_target=link_target)

    # A REGULAR FILE already sits where z's symlink dst would land.
    z_dst = _live_dir() / "z-link"
    z_dst.parent.mkdir(parents=True, exist_ok=True)
    z_dst.write_text("pre-existing live content\n", encoding="utf-8")

    a_live = _live_dir() / "a.md"
    assert not a_live.exists()

    result = _install(config, transition=True)

    assert result.exit_code != 0
    # The earlier regular file was NOT deployed — refused before any write.
    assert not a_live.exists()
    # No transition recorded for the refused install.
    assert _transition_count() == 0
    # The blocking dst is untouched (not clobbered).
    assert z_dst.read_text(encoding="utf-8") == "pre-existing live content\n"
    # The refusal names the offending dst (raised as SetforgeError; the
    # top-level handler — not exercised under CliRunner — renders it).
    assert "refusing to deploy symlink" in str(result.exception)


def test_preflight_refuses_when_directory_occupies_dst(repo: Path) -> None:
    """A directory at the symlink dst is refused with the directory wording."""
    _write_tracked(repo, "a", _DOC_A)
    _write_tracked(repo, "z", _DOC_Z)
    link_target = _live_dir() / "z-target"
    config = _write_config(repo, link_target=link_target)

    z_dst = _live_dir() / "z-link"
    z_dst.mkdir(parents=True)

    result = _install(config, transition=True)

    assert result.exit_code != 0
    assert not (_live_dir() / "a.md").exists()
    assert _transition_count() == 0
    assert "a directory is already present" in str(result.exception)


def test_preflight_allows_pre_existing_symlink_at_dst(repo: Path) -> None:
    """A pre-existing SYMLINK at the dst is replaced, not refused."""
    _write_tracked(repo, "a", _DOC_A)
    _write_tracked(repo, "z", _DOC_Z)
    link_target = _live_dir() / "z-target"
    config = _write_config(repo, link_target=link_target)

    z_dst = _live_dir() / "z-link"
    z_dst.parent.mkdir(parents=True, exist_ok=True)
    z_dst.symlink_to(_live_dir() / "some-old-target")

    result = _install(config, transition=True)

    assert result.exit_code == 0, result.output
    # Both files deployed: a as a regular file, z as a refreshed symlink.
    assert (_live_dir() / "a.md").read_text(encoding="utf-8") == _DOC_A
    assert z_dst.is_symlink()
    assert _transition_count() == 1
