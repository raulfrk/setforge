"""Regression tests for the revert mutate-before-record atomicity finding.

Audit finding ``revert_order``: ``_apply_revert`` reversed the content
patch and restored stores BEFORE running the symlink-unlink pass, which
refuses (raises :class:`SetforgeError`) when the user retargeted a
deployed link or replaced it with a regular file. A refusal raised after
the content patch was already reversed left a PARTIAL, un-redoable revert
(content reverted, link untouched, no reverse transition written).

Fix: ``_apply_revert`` validates the recorded link images against the live
tree BEFORE any mutation — symmetric to ``apply_patch_reverse``'s own
``--dry-run`` gate — so revert refuses cleanly with zero mutation.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from tests.test_cli_revert import _no_code, _state_root

_SYMLINK_DEPLOY_YAML = """\
version: 1
tracked_files:
  greeting:
    src: greeting.md
    dst: {content_dst}
  hook:
    src: hook.sh
    dst: {link_dst}
    symlink: {link_target}
profiles:
  vmh:
    tracked_files: [greeting, hook]
"""


def _setup_content_plus_symlink_repo(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path]:
    """Build a profile with a content-deployed file AND a symlink-deployed
    tracked_file. Returns ``(cfg, content_dst, link_dst, link_target)``."""
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    (repo / "tracked" / "greeting.md").write_text("hello\n", encoding="utf-8")
    (repo / "tracked" / "hook.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    content_dst = tmp_path / "live" / "greeting.md"
    link_dst = tmp_path / "live" / "hook.sh"
    link_target = tmp_path / "live" / "hook-target.sh"
    cfg = repo / "setforge.yaml"
    cfg.write_text(
        _SYMLINK_DEPLOY_YAML.format(
            content_dst=content_dst,
            link_dst=link_dst,
            link_target=link_target,
        ),
        encoding="utf-8",
    )
    return cfg, content_dst, link_dst, link_target


def test_apply_revert_refuses_with_zero_mutation_on_retargeted_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end of the finding: install a content file + a symlink, then
    the user retargets the link. ``_apply_revert`` must REFUSE with zero
    mutation — the content patch is NOT reversed, and NO reverse transition
    is written (so a documented redo stays possible).

    Pre-fix, ``_apply_revert`` reversed the content patch and only THEN hit
    the symlink refusal, leaving the content file reverted with no reverse
    transition: a partial, un-redoable state.
    """
    state = _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)
    cfg, content_dst, link_dst, _link_target = _setup_content_plus_symlink_repo(
        tmp_path
    )

    install = CliRunner().invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert install.exit_code == 0, install.output
    assert content_dst.read_text(encoding="utf-8") == "hello\n"
    assert link_dst.is_symlink()

    transitions_dir = state / "transitions"
    before = {p.name for p in transitions_dir.iterdir()}

    # User retargets the deployed link to a different path — the documented
    # symlink-revert refusal condition.
    link_dst.unlink()
    link_dst.symlink_to(tmp_path / "live" / "user-chosen-target.sh")

    result = CliRunner().invoke(
        app, ["revert", "--profile=vmh", f"--config={cfg}", "--yes"]
    )
    assert result.exit_code != 0
    assert "filesystem path changed since transition" in str(result.exception)
    assert link_dst.readlink() == tmp_path / "live" / "user-chosen-target.sh"

    # Zero mutation on the content axis: the patch was NOT reversed (would
    # have removed the install-created file or restored /dev/null).
    assert content_dst.exists()
    assert content_dst.read_text(encoding="utf-8") == "hello\n"

    # No reverse transition was written — the dir set is unchanged, so a
    # later redo is still possible once the user resolves the link drift.
    after = {p.name for p in transitions_dir.iterdir()}
    assert after == before


def test_apply_revert_succeeds_when_link_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: when the link is NOT user-mutated, ``_apply_revert`` runs
    fully — content patch reversed, link unlinked, reverse transition
    written — confirming the pre-flight does not block the happy path.
    """
    state = _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)
    cfg, content_dst, link_dst, _link_target = _setup_content_plus_symlink_repo(
        tmp_path
    )

    install = CliRunner().invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert install.exit_code == 0, install.output
    assert link_dst.is_symlink()

    transitions_dir = state / "transitions"
    before = {p.name for p in transitions_dir.iterdir()}

    result = CliRunner().invoke(
        app, ["revert", "--profile=vmh", f"--config={cfg}", "--yes"]
    )
    assert result.exit_code == 0, (result.output, result.exception)

    # Content reverted (install created the file; revert removes it) and the
    # link is gone.
    assert not content_dst.exists()
    assert not link_dst.exists()

    # A reverse transition WAS written (redo record).
    after = {p.name for p in transitions_dir.iterdir()}
    new = after - before
    assert len(new) == 1
    reverse = transitions_dir / next(iter(new))
    assert (reverse / "meta.json").exists()
    json.loads((reverse / "meta.json").read_text(encoding="utf-8"))
