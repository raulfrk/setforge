"""Integration tests for store-state restore on ``setforge revert``.

Drive real ``install`` / ``revert`` CLI invocations (recording
transitions) against a temp config repo with a sandboxed ``$HOME`` +
``$SETFORGE_STATE_DIR`` and assert the revert side of the snapshot
mechanism:

- the headline recovery promise: a base-absent first install clobbers a
  structural span edit and seeds base + sidecar; revert restores live AND
  deletes the seeded stores, so a re-install repeats the first run
  verbatim (deploy-tracked-verbatim) instead of 3-way-merging against a
  stranded base — which would silently keep the live value and diverge
  from the first run
- pre-snapshot transitions (no ``state_snapshots/`` dir) revert exactly
  as before — stores untouched, no crash
- revert→revert acts as redo: store state round-trips both ways
- an empty (zero-byte) store entry restores as empty, never as deleted
- ``--to-before`` walks the chain newest-first, landing on the oldest
  requested pre-state
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store, spans_store, transitions
from setforge.cli import app

_PROFILE = "test-snapshots"
_MD_ID = "doc"
_YAML_ID = "settings"

_DOC = """\
# Title

## Forked

Forked body original.

## Shared

Shared body original.
"""

# Live markdown with edits confined to the FORKED span. A forked span
# merges upstream with NO post-merge override, so the base-absent
# deploy-tracked-verbatim path clobbers this edit — the clobber shape the
# recovery promise must round-trip.
_DOC_FORK_EDITED = _DOC.replace("Forked body original.", "MY FORKED EDIT.")

_YAML_DOC = "editor:\n  fontSize: 12\n  tabSize: 4\nshared:\n  theme: dark\n"

# Live YAML with the pinned dotted-path span edited. The base-absent
# structural path seeds the base FROM live (the auto-on-install
# migration) — a seeded entry revert must DELETE.
_YAML_SPAN_EDITED = _YAML_DOC.replace("fontSize: 12", "fontSize: 20")


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.setforge_snap/doc.md\n"
        "    disposition: shared\n"
        "    spans:\n"
        '      - anchor: "## Forked"\n'
        "        kind: forked\n"
        "        semantics: shared\n"
        "  settings:\n"
        "    src: doc.yaml\n"
        "    dst: ~/.setforge_snap/doc.yaml\n"
        "    disposition: shared\n"
        "    spans:\n"
        '      - anchor: "editor.fontSize"\n'
        "        kind: pinned\n"
        "        semantics: shared\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - doc\n"
        "      - settings\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, md_body: str, yaml_body: str = _YAML_DOC) -> None:
    tracked = repo / "tracked"
    tracked.mkdir(parents=True, exist_ok=True)
    (tracked / "doc.md").write_text(md_body, encoding="utf-8")
    (tracked / "doc.yaml").write_text(yaml_body, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _live_md() -> Path:
    return Path.home() / ".setforge_snap" / "doc.md"


def _live_yaml() -> Path:
    return Path.home() / ".setforge_snap" / "doc.yaml"


def _install(config: Path) -> Result:
    """Run a transition-RECORDING install (no --no-transition)."""
    args = [
        "install",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-secrets-scan",
        "--no-git-check",
        "--yes",
    ]
    return CliRunner().invoke(app, args)


def _revert(config: Path, *, extra: list[str] | None = None) -> Result:
    args = ["revert", f"--profile={_PROFILE}", f"--config={config}", "--yes"]
    if extra:
        args.extend(extra)
    return CliRunner().invoke(app, args)


def _latest_dirname() -> str:
    latest = transitions.load_latest(_PROFILE)
    assert latest is not None
    return latest.name


# ---------------------------------------------------------------------------
# the headline recovery promise
# ---------------------------------------------------------------------------


def test_revert_deletes_seeded_stores_and_reinstall_repeats_first_run(
    repo: Path,
) -> None:
    """seed-install → revert deletes the stores → re-install verbatim.

    Both live files carry span edits with NO stored base. The first
    install clobbers the FORKED markdown span edit (forked spans get no
    post-merge override, so the base-absent path deploys tracked
    verbatim over it) and seeds base + sidecar state for both files;
    the structural YAML file additionally seeds its base FROM live (the
    auto-on-install migration). Revert must restore live AND delete
    every seeded store entry, so the re-install repeats the first run
    verbatim: it clobbers AGAIN. A stranded markdown base (the bug this
    pins) would route the re-install through the 3-way merge instead —
    live differs from base, tracked does not, so the merge would KEEP
    the live edit and silently diverge from the first run.
    """
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    live_yaml = _live_yaml()
    live_yaml.parent.mkdir(parents=True, exist_ok=True)
    live_yaml.write_text(_YAML_SPAN_EDITED, encoding="utf-8")
    _live_md().write_text(_DOC_FORK_EDITED, encoding="utf-8")

    assert _install(config).exit_code == 0
    # The base-absent path deployed tracked VERBATIM over the forked edit.
    assert _live_md().read_text(encoding="utf-8") == _DOC
    yaml_after_first = live_yaml.read_bytes()
    yaml_base_after_first = base_store.read_base(_PROFILE, _YAML_ID)
    md_base_after_first = base_store.read_base(_PROFILE, _MD_ID)
    sidecar_after_first = spans_store.get_states(_PROFILE, _MD_ID)
    assert yaml_base_after_first is not None  # migration-seeded from live
    assert md_base_after_first is not None
    assert sidecar_after_first  # seeded

    result = _revert(config)
    assert result.exit_code == 0, result.output
    # Live is back to the span-edited pre-install content...
    assert _live_md().read_text(encoding="utf-8") == _DOC_FORK_EDITED
    assert live_yaml.read_text(encoding="utf-8") == _YAML_SPAN_EDITED
    # ...and every seeded store entry is DELETED, not stranded.
    assert base_store.read_base(_PROFILE, _MD_ID) is None
    assert base_store.read_base(_PROFILE, _YAML_ID) is None
    assert spans_store.get_states(_PROFILE, _MD_ID) == {}
    assert not spans_store.manifest_path(_PROFILE, _MD_ID).exists()

    # Re-install repeats the first run verbatim: the forked edit is
    # clobbered AGAIN (base-absent path, not a live-preserving 3-way
    # against a stale ancestor), and the stores re-seed identically.
    assert _install(config).exit_code == 0
    assert _live_md().read_text(encoding="utf-8") == _DOC
    assert live_yaml.read_bytes() == yaml_after_first
    assert base_store.read_base(_PROFILE, _MD_ID) == md_base_after_first
    assert base_store.read_base(_PROFILE, _YAML_ID) == yaml_base_after_first
    assert spans_store.get_states(_PROFILE, _MD_ID) == sidecar_after_first


# ---------------------------------------------------------------------------
# back-compat: pre-snapshot transitions
# ---------------------------------------------------------------------------


def test_pre_snapshot_transition_reverts_cleanly_with_stores_untouched(
    repo: Path,
) -> None:
    """A transition without state_snapshots/ (recorded before the bump)
    reverts exactly as before: live restored, stores untouched, exit 0."""
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install(config).exit_code == 0

    latest = transitions.load_latest(_PROFILE)
    assert latest is not None
    shutil.rmtree(latest / "state_snapshots")  # simulate an old record

    base_before_revert = base_store.read_base(_PROFILE, _MD_ID)
    assert base_before_revert is not None

    result = _revert(config)
    assert result.exit_code == 0, result.output
    assert not _live_md().exists()  # live restored (created by install)
    # Stores untouched — the seeded base survives, exactly as a
    # pre-snapshot revert behaved.
    assert base_store.read_base(_PROFILE, _MD_ID) == base_before_revert


# ---------------------------------------------------------------------------
# revert → revert (redo) round-trips the store state
# ---------------------------------------------------------------------------


def test_revert_then_redo_round_trips_store_state(repo: Path) -> None:
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    base_v1 = base_store.read_base(_PROFILE, _MD_ID)
    live_v1 = _live_md().read_bytes()

    _write_tracked(repo, _DOC.replace("Shared body original.", "Shared body V2."))
    assert _install(config).exit_code == 0
    base_v2 = base_store.read_base(_PROFILE, _MD_ID)
    live_v2 = _live_md().read_bytes()
    assert base_v2 != base_v1

    assert _revert(config).exit_code == 0
    assert base_store.read_base(_PROFILE, _MD_ID) == base_v1
    assert _live_md().read_bytes() == live_v1

    # Second revert = redo: the reverse transition snapshotted the
    # pre-revert store state, so the redo round-trips back to v2.
    assert _revert(config).exit_code == 0
    assert base_store.read_base(_PROFILE, _MD_ID) == base_v2
    assert _live_md().read_bytes() == live_v2


# ---------------------------------------------------------------------------
# absent vs empty through a real install + revert
# ---------------------------------------------------------------------------


def test_empty_store_entry_restores_as_empty_not_deleted(repo: Path) -> None:
    """A zero-byte store entry pre-install must come back as zero bytes —
    deleting it instead would collapse absent and empty."""
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install(config).exit_code == 0

    # Force the empty-but-present pre-state for the next install. Live and
    # tracked agree, so the 3-way merge resolves trivially and the base
    # advances off the empty state.
    base_store.write_base(_PROFILE, _MD_ID, b"")

    assert _install(config).exit_code == 0
    advanced = base_store.read_base(_PROFILE, _MD_ID)
    assert advanced is not None
    assert advanced != b""

    assert _revert(config).exit_code == 0
    restored = base_store.read_base(_PROFILE, _MD_ID)
    assert restored is not None  # NOT deleted...
    assert restored == b""  # ...rewritten to exactly zero bytes


# ---------------------------------------------------------------------------
# --to-before chain restores newest-first
# ---------------------------------------------------------------------------


def test_to_before_chain_restores_store_state_newest_first(repo: Path) -> None:
    """Reverting to BEFORE install #2 walks #3 then #2, landing live AND
    the stores back at the post-install-#1 state."""
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    base_v1 = base_store.read_base(_PROFILE, _MD_ID)
    live_v1 = _live_md().read_bytes()

    _write_tracked(repo, _DOC.replace("Shared body original.", "Shared body V2."))
    assert _install(config).exit_code == 0
    second_dirname = _latest_dirname()

    _write_tracked(repo, _DOC.replace("Shared body original.", "Shared body V3."))
    assert _install(config).exit_code == 0
    assert base_store.read_base(_PROFILE, _MD_ID) != base_v1

    result = _revert(config, extra=[f"--to-before={second_dirname}"])
    assert result.exit_code == 0, result.output
    assert base_store.read_base(_PROFILE, _MD_ID) == base_v1
    assert _live_md().read_bytes() == live_v1
