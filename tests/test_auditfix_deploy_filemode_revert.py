"""Integration: install-applied file-mode changes are reversed by revert.

Finding (Important / revert-completeness): ``setforge install`` actively
chmods live files (a content-NOOP mode-only fixup and a content UPDATE both
apply the tracked mode), but transitions snapshotted only file CONTENT (a
difflib patch). After ``setforge revert`` the patch reverse restored the
prior bytes while the file KEPT the install-applied mode — so a 0600 secret
retracked to 0644 stayed 0644 after revert (revert was not a faithful inverse
on the mode axis).

The transition records each changed file's pre-install image, mode
included, and ``revert`` restores it. These tests drive real ``install`` /
``revert`` CLI invocations against a sandboxed ``$HOME`` +
``$SETFORGE_STATE_DIR`` and pin:

- the headline case: a content-NOOP mode-only install (0644 → 0600) writes a
  transition and ``revert`` restores live to 0644;
- redo symmetry: a second ``revert`` (redo) re-applies the install mode.
"""

from __future__ import annotations

import stat
from pathlib import Path

from click.testing import Result
from typer.testing import CliRunner

from setforge import transitions
from setforge.cli import app

_PROFILE = "test-filemode"
_BODY = "secret-token-do-not-widen\n"


def _write_config(repo: Path, *, mode: str) -> Path:
    """Write a one-tracked-file config whose dst carries an explicit ``mode``."""
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  secret:\n"
        "    src: secret.txt\n"
        "    dst: ~/.setforge_mode/secret.txt\n"
        f"    mode: {mode}\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - secret\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, body: str = _BODY) -> None:
    tracked = repo / "tracked"
    tracked.mkdir(parents=True, exist_ok=True)
    (tracked / "secret.txt").write_text(body, encoding="utf-8")


def _live() -> Path:
    return Path.home() / ".setforge_mode" / "secret.txt"


def _live_mode() -> int:
    return stat.S_IMODE(_live().stat().st_mode)


def _install(config: Path) -> Result:
    args = [
        "install",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-secrets-scan",
        "--no-git-check",
        # The content-NOOP + mode-only path is a permission-mode drift the
        # bare-install gate rejects; --auto-accept-tracked resolves it.
        "--auto-accept-tracked",
        "--yes",
    ]
    return CliRunner().invoke(app, args)


def _revert(config: Path) -> Result:
    args = ["revert", f"--profile={_PROFILE}", f"--config={config}", "--yes"]
    return CliRunner().invoke(app, args)


def _seed_live(body: str, mode: int) -> None:
    live = _live()
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(body, encoding="utf-8")
    live.chmod(mode)


def test_mode_only_install_is_reverted_to_prior_mode(repo: Path) -> None:
    """Content-NOOP 0644 → 0600 install; revert restores live to 0644.

    The regression-critical case: the content patch is EMPTY (live already
    has the tracked bytes), so the install's only mutation is the chmod. The
    transition must record the pre-install mode so revert can undo it.
    """
    _write_tracked(repo)
    config = _write_config(repo, mode="0o600")
    # Live pre-exists with IDENTICAL content at the wider mode, so install
    # is a content-NOOP + mode-only tighten.
    _seed_live(_BODY, 0o644)

    assert _install(config).exit_code == 0
    assert _live_mode() == 0o600  # install tightened perms
    assert _live().read_text(encoding="utf-8") == _BODY  # content untouched

    # The transition recorded the pre-install mode for the chmod-ed path.
    latest = transitions.load_latest(_PROFILE)
    assert latest is not None
    recorded = {
        item.path: (item.pre.mode, item.post.mode)
        for item in transitions.load_filesystem_deltas(latest)
    }
    assert recorded[_live()] == (0o644, 0o600)

    result = _revert(config)
    assert result.exit_code == 0, result.output
    # The mode axis is a faithful inverse now: live is back to 0644.
    assert _live_mode() == 0o644
    assert _live().read_text(encoding="utf-8") == _BODY


def test_revert_then_redo_round_trips_the_mode(repo: Path) -> None:
    """revert → revert (redo) restores the install-applied mode.

    The reverse transition recaptures the install mode (0600) BEFORE
    restoring 0644, so a second revert re-applies 0600 — mode redo symmetry,
    mirroring the store-state recapture.
    """
    _write_tracked(repo)
    config = _write_config(repo, mode="0o600")
    _seed_live(_BODY, 0o644)

    assert _install(config).exit_code == 0
    assert _live_mode() == 0o600

    assert _revert(config).exit_code == 0
    assert _live_mode() == 0o644

    # Redo: the reverse transition's image carries the install mode.
    assert _revert(config).exit_code == 0
    assert _live_mode() == 0o600


def test_content_and_mode_install_reverts_both_axes(repo: Path) -> None:
    """A content UPDATE that also tightens perms reverts content AND mode.

    The reconcile engine merges a plain file against its recorded base
    rather than blindly overwriting live, so the content UPDATE is staged as
    a clean fast-forward: a first install (live == tracked) records the base,
    then an upstream edit + a mode tighten produce a single UPDATE whose
    revert must undo both axes.
    """
    _write_tracked(repo, "live-body\n")
    config = _write_config(repo, mode="0o644")
    _seed_live("live-body\n", 0o644)
    # First install: live already matches tracked, so content is a NOOP and
    # the mode is unchanged — its purpose is to record the merge base.
    assert _install(config).exit_code == 0
    assert _live().read_text(encoding="utf-8") == "live-body\n"

    # Upstream edits the content AND the config tightens the mode: a clean
    # fast-forward UPDATE (live == base, tracked changed) that also chmods.
    _write_tracked(repo, "tracked-body\n")
    config = _write_config(repo, mode="0o600")
    assert _install(config).exit_code == 0
    assert _live().read_text(encoding="utf-8") == "tracked-body\n"
    assert _live_mode() == 0o600

    # Revert undoes the second install on BOTH axes.
    assert _revert(config).exit_code == 0
    assert _live().read_text(encoding="utf-8") == "live-body\n"
    assert _live_mode() == 0o644


def test_revert_preview_surfaces_mode_restore(repo: Path) -> None:
    """The revert confirm preview lists the per-file mode restore note."""
    from setforge.cli.revert import _build_revert_plan

    _write_tracked(repo)
    config = _write_config(repo, mode="0o600")
    _seed_live(_BODY, 0o644)
    assert _install(config).exit_code == 0

    latest = transitions.load_latest(_PROFILE)
    assert latest is not None
    plan = _build_revert_plan(transitions.load_record(latest), _PROFILE)
    notes = [fm.mode_restore for fm in plan.file_mutations if fm.path == _live()]
    assert notes == ["mode → 0o644"]
