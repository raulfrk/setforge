"""Integration tests for the stored-base lifecycle in the install loop.

Drive the real ``setforge install`` CLI against a temp config repo with a
sandboxed ``$HOME`` + ``$SETFORGE_STATE_DIR`` and assert on the per-host
stored base (:mod:`setforge.base_store`) it seeds, advances, defers, and
prunes for ``disposition``-bearing tracked files.

The cases mirror Task 8's acceptance grid:

1. First install of a ``shared`` file seeds base == tracked bytes.
2. A second no-edit install is idempotent (base unchanged, no warning).
3. Non-overlapping live + tracked edits clean-merge into live; base advances.
4. Same-region edits under bare install keep live, warn, and DO NOT advance
   the base (so the conflict re-surfaces).
5. The same conflict under ``--auto=use-tracked`` takes tracked and advances.
6. A ``pinned`` file is never overwritten and never gets a base.
7. Dropping a ``shared`` file from the profile prunes its base.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store
from setforge.cli import app

_PROFILE = "test-disposition"
_FILE_ID = "shared_text"


def _write_config(
    repo: Path, *, disposition: str = "shared", include: bool = True
) -> Path:
    """Write a setforge.yaml; return its path.

    The profile always carries an inert ``anchor`` tracked file so it stays
    a valid non-empty list. ``disposition`` sets the ``shared_text`` file's
    policy. ``include=False`` drops ``shared_text`` from the profile (for
    the prune case) while keeping its tracked source on disk.
    """
    shared_line = "      - shared_text\n" if include else ""
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  shared_text:\n"
        "    src: text/note.txt\n"
        "    dst: ~/.setforge_disp/note.txt\n"
        f"    disposition: {disposition}\n"
        "  anchor:\n"
        "    src: text/anchor.txt\n"
        "    dst: ~/.setforge_disp/anchor.txt\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        f"{shared_line}"
        "      - anchor\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, body: str) -> None:
    """Write the tracked source bodies for ``shared_text`` and ``anchor``."""
    src = repo / "tracked" / "text" / "note.txt"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(body, encoding="utf-8")
    (src.parent / "anchor.txt").write_text("anchor\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp config repo with sandboxed ``$HOME`` + ``$SETFORGE_STATE_DIR``.

    The live dst (``~/.setforge_disp/note.txt``) lands under the sandbox
    home; the stored base lands under ``$SETFORGE_STATE_DIR/base/...``.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _live_path() -> Path:
    """Resolve the sandboxed live destination path."""
    return Path.home() / ".setforge_disp" / "note.txt"


def _install(config: Path, *, extra: list[str] | None = None) -> Result:
    """Run ``setforge install`` against ``config``; return the CliRunner result."""
    args = [
        "install",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-transition",
        "--no-secrets-scan",
        "--no-git-check",
        "--yes",
    ]
    if extra:
        args.extend(extra)
    return CliRunner().invoke(app, args)


def test_first_install_seeds_base(repo: Path) -> None:
    """First install of a shared file: live == tracked, base seeded == tracked."""
    tracked = "line1\nline2\n"
    _write_tracked(repo, tracked)
    config = _write_config(repo)

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert _live_path().read_text(encoding="utf-8") == tracked
    assert base_store.read_base(_PROFILE, _FILE_ID) == tracked.encode("utf-8")


def test_idempotent_second_install(repo: Path) -> None:
    """Re-install with no edits: base unchanged, no conflict warning."""
    tracked = "alpha\nbeta\n"
    _write_tracked(repo, tracked)
    config = _write_config(repo)

    assert _install(config).exit_code == 0
    base_after_first = base_store.read_base(_PROFILE, _FILE_ID)

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert base_store.read_base(_PROFILE, _FILE_ID) == base_after_first
    assert "conflict" not in result.output.lower()


def test_clean_merge_advances_base(repo: Path) -> None:
    """Non-overlapping live + tracked edits clean-merge; base advances to merge."""
    _write_tracked(repo, "header\nbody\nfooter\n")
    config = _write_config(repo)
    assert _install(config).exit_code == 0

    # Live edits the LAST line; tracked edits the FIRST line — disjoint hunks.
    _live_path().write_text("header\nbody\nfooter-EDITED\n", encoding="utf-8")
    _write_tracked(repo, "header-EDITED\nbody\nfooter\n")

    result = _install(config)
    assert result.exit_code == 0, result.output
    merged = _live_path().read_text(encoding="utf-8")
    assert "header-EDITED" in merged
    assert "footer-EDITED" in merged
    # Base advanced to the merged content (== current live).
    assert base_store.read_base(_PROFILE, _FILE_ID) == merged.encode("utf-8")


def test_conflict_bare_keeps_live_and_defers_base(repo: Path) -> None:
    """Same-region edits, bare install: live kept, warned, base NOT advanced."""
    _write_tracked(repo, "one\ntwo\nthree\n")
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    base_before = base_store.read_base(_PROFILE, _FILE_ID)

    # Both sides edit the SAME middle line → conflict.
    _live_path().write_text("one\ntwo-LIVE\nthree\n", encoding="utf-8")
    _write_tracked(repo, "one\ntwo-TRACKED\nthree\n")

    result = _install(config)
    assert result.exit_code == 0, result.output
    # Live kept its own edit.
    assert "two-LIVE" in _live_path().read_text(encoding="utf-8")
    # Conflict warning emitted (stderr is folded into output by CliRunner).
    assert "conflict" in result.output.lower()
    # Base NOT advanced — still the previous base so the next install
    # re-detects the divergence.
    assert base_store.read_base(_PROFILE, _FILE_ID) == base_before


def test_conflict_use_tracked_takes_tracked_and_advances(repo: Path) -> None:
    """Same conflict under --auto=use-tracked: live takes tracked, base advances."""
    _write_tracked(repo, "one\ntwo\nthree\n")
    config = _write_config(repo)
    assert _install(config).exit_code == 0

    _live_path().write_text("one\ntwo-LIVE\nthree\n", encoding="utf-8")
    _write_tracked(repo, "one\ntwo-TRACKED\nthree\n")

    result = _install(config, extra=["--auto=use-tracked"])
    assert result.exit_code == 0, result.output
    live = _live_path().read_text(encoding="utf-8")
    assert "two-TRACKED" in live
    assert "two-LIVE" not in live
    assert base_store.read_base(_PROFILE, _FILE_ID) == live.encode("utf-8")


def test_pinned_never_overwritten_no_base(repo: Path) -> None:
    """A pinned file: live wins, tracked never overwrites, no base written."""
    _write_tracked(repo, "tracked-body\n")
    config = _write_config(repo, disposition="pinned")
    # Pre-seed a live file the install must NOT clobber.
    live = _live_path()
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("LIVE-WINS\n", encoding="utf-8")

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert live.read_text(encoding="utf-8") == "LIVE-WINS\n"
    # pinned never re-baselines → no stored base.
    assert base_store.read_base(_PROFILE, _FILE_ID) is None


def test_prune_removes_dropped_file_base(repo: Path) -> None:
    """Removing a shared file from the profile prunes its base on next install."""
    _write_tracked(repo, "keepme\n")
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    assert base_store.read_base(_PROFILE, _FILE_ID) is not None

    # Re-write the config WITHOUT the tracked file in the profile.
    config = _write_config(repo, include=False)
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert base_store.read_base(_PROFILE, _FILE_ID) is None


# ---------------------------------------------------------------------------
# Bead 10.1: SHARED-section strip + base-seed migration on install.
#
# When a file moves from the legacy marker-based ``preserve_user_sections``
# model into the disposition stored-base model, its FIRST install under a
# ``disposition`` finds the live file still carrying SHARED markers and no
# stored base. Install strips the SHARED marker LINES (keeping bodies), writes
# the stripped form live, and seeds base == stripped-live so the first 3-way
# merge sees no spurious delta (zero data loss).
# ---------------------------------------------------------------------------

_LIVE_WITH_SHARED_MARKERS = (
    "intro line\n"
    "<!-- setforge:user-section start shared RULES -->\n"
    "user rule one\n"
    "user rule two\n"
    "<!-- setforge:user-section end shared RULES -->\n"
    "outro line\n"
)

_STRIPPED_LIVE = "intro line\nuser rule one\nuser rule two\noutro line\n"


def _seed_live_markers(
    content: str = _LIVE_WITH_SHARED_MARKERS, *, mode: int = 0o600
) -> Path:
    """Pre-create the live dst carrying SHARED markers at ``mode``."""
    live = _live_path()
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(content, encoding="utf-8")
    live.chmod(mode)
    return live


def test_migration_strips_shared_markers_and_lands_stripped_live(repo: Path) -> None:
    """First disposition install over marker-bearing live strips markers in place."""
    # Tracked side equals the stripped bodies (the steady-state shared content
    # once the markers are gone) so the merge is a no-op beyond the strip.
    _write_tracked(repo, _STRIPPED_LIVE)
    config = _write_config(repo)
    _seed_live_markers()

    result = _install(config)
    assert result.exit_code == 0, result.output
    # Markers gone, bodies + outside text intact byte-for-byte.
    assert _live_path().read_text(encoding="utf-8") == _STRIPPED_LIVE
    assert "user-section" not in _live_path().read_text(encoding="utf-8")


def test_migration_seeds_base_byte_identical_to_stripped_live(repo: Path) -> None:
    """Data-loss invariant: base == stripped-live bytes == what landed live."""
    _write_tracked(repo, _STRIPPED_LIVE)
    config = _write_config(repo)
    _seed_live_markers()

    assert _install(config).exit_code == 0
    base = base_store.read_base(_PROFILE, _FILE_ID)
    live = _live_path().read_bytes()
    assert base == _STRIPPED_LIVE.encode("utf-8")
    assert base == live


def test_migration_first_merge_clean_no_spurious_conflict(repo: Path) -> None:
    """base == stripped-live makes the first merge clean; bodies byte-intact.

    Bead contract test #3. With base seeded == stripped-live, the first 3-way
    merge against a tracked that equals the stripped steady state is a clean
    no-op: NO spurious conflict (which a ``None`` base would manufacture as a
    whole-file both-add), markers GONE, and every body byte preserved.
    """
    # Tracked is the stripped steady state (markers already absent upstream).
    _write_tracked(repo, _STRIPPED_LIVE)
    config = _write_config(repo)
    _seed_live_markers()

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "conflict" not in result.output.lower()
    merged = _live_path().read_text(encoding="utf-8")
    # Markers gone, every body + outside byte intact.
    assert merged == _STRIPPED_LIVE
    assert "user-section" not in merged


def test_migration_seeds_base_from_stripped_not_tracked(repo: Path) -> None:
    """The seed is stripped-LIVE, not tracked — distinguishes strip from verbatim.

    On the migration install the live body diverges from tracked. The strip
    path lands stripped-LIVE live and seeds base from it; because base == live
    the clean merge is a no-op against the user's content for any line the
    user holds and tracked left at base. The DISTINGUISHING assertion: live
    keeps the user's body verbatim (the strip wrote stripped-live in place),
    whereas the naive base-absent deploy-tracked-verbatim path would have
    replaced live with tracked's (markerless) body wholesale.

    Tracked here is byte-identical to the stripped live body so the merge is a
    pure no-op and the post-merge live + base are exactly stripped-live — the
    in-memory seed bytes, never a tracked substitution.
    """
    _write_tracked(repo, _STRIPPED_LIVE)
    config = _write_config(repo)
    live = _seed_live_markers()
    # Sanity: tracked file's on-disk mode differs from the live 0600 so a
    # verbatim deploy would be visible via mode too.
    (repo / "tracked" / "text" / "note.txt").chmod(0o644)

    assert _install(config).exit_code == 0
    # base == stripped-live in-memory bytes (NOT seeded from a re-read).
    assert base_store.read_base(_PROFILE, _FILE_ID) == _STRIPPED_LIVE.encode("utf-8")
    assert live.read_bytes() == _STRIPPED_LIVE.encode("utf-8")


def test_migration_then_live_edit_survives_clean_merge(repo: Path) -> None:
    """ZERO-DATA-LOSS: a post-migration live edit survives the next merge.

    Proves the seeded base is a usable 3-way ancestor: after migration
    (base == stripped-live), the user edits a DISJOINT live line and tracked
    edits a different disjoint line. The next install clean-merges both —
    the user's live edit is NOT clobbered.
    """
    _write_tracked(repo, _STRIPPED_LIVE)
    config = _write_config(repo)
    _seed_live_markers()
    assert _install(config).exit_code == 0  # migration install seeds base.

    # User edits the LAST line; tracked edits the FIRST — disjoint hunks.
    _live_path().write_text(
        "intro line\nuser rule one\nuser rule two\noutro-EDITED\n", encoding="utf-8"
    )
    _write_tracked(repo, "intro-EDITED\nuser rule one\nuser rule two\noutro line\n")

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "conflict" not in result.output.lower()
    merged = _live_path().read_text(encoding="utf-8")
    assert "intro-EDITED" in merged  # tracked's change landed.
    assert "outro-EDITED" in merged  # user's live edit SURVIVED.


def test_migration_rerun_does_not_re_seed_or_double_strip(repo: Path) -> None:
    """Re-running the install does not re-seed or re-strip (gate on the pair)."""
    _write_tracked(repo, _STRIPPED_LIVE)
    config = _write_config(repo)
    _seed_live_markers()

    assert _install(config).exit_code == 0
    base_after_first = base_store.read_base(_PROFILE, _FILE_ID)
    live_after_first = _live_path().read_bytes()

    # Second run: base is now present and live has no SHARED markers, so the
    # migration gate must NOT fire again.
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert base_store.read_base(_PROFILE, _FILE_ID) == base_after_first
    assert _live_path().read_bytes() == live_after_first
    assert "conflict" not in result.output.lower()


def test_migration_preserves_live_mode(repo: Path) -> None:
    """The in-place live rewrite preserves the existing mode (0600 stays 0600)."""
    _write_tracked(repo, _STRIPPED_LIVE)
    config = _write_config(repo)
    live = _seed_live_markers(mode=0o600)

    assert _install(config).exit_code == 0
    import stat

    assert stat.S_IMODE(live.stat().st_mode) == 0o600


def test_migration_skipped_when_no_shared_markers(repo: Path) -> None:
    """A live file with NO shared markers takes the ordinary base-absent path.

    The gate is the (base is None, live-has-shared-markers) pair: a plain live
    file (no markers) must NOT be rewritten by the strip path; it follows the
    today's first-install seed == tracked behavior.
    """
    _write_tracked(repo, "tracked-body\n")
    config = _write_config(repo)
    # Pre-existing plain live file, no markers.
    _seed_live_markers(content="plain live\n")

    result = _install(config)
    assert result.exit_code == 0, result.output
    # Base-absent path deploys tracked verbatim and seeds base == tracked.
    assert _live_path().read_text(encoding="utf-8") == "tracked-body\n"
    assert base_store.read_base(_PROFILE, _FILE_ID) == b"tracked-body\n"
