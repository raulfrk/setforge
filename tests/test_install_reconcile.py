"""Integration: a PLAIN tracked file installs through the 3-way engine.

A0 routes a plain tracked file (no disposition, no spans, no host-local
overlay) through ``reconcile_apply.reconcile_plain_file`` instead of a
verbatim copy, so a local edit is merged against the recorded base rather
than silently overwritten. These tests drive the real ``install`` CLI
against a sandboxed ``$HOME`` + ``$SETFORGE_STATE_DIR`` and pin the
per-case behavior + the base-store side effect.
"""

from __future__ import annotations

import json
import stat
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store, locking, reconcile
from setforge.cli import app
from setforge.cli.stage import Decision, _apply, collect_stages, walk
from setforge.config import load_config, resolve_profile
from setforge.reconcile import store as reconcile_store
from setforge.reconcile.types import HunkClass
from setforge.transitions import transitions_root
from tests.verb_calls import capture_profile, preview_capture_profile

_PROFILE = "test-recon"


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  note:\n"
        "    src: note.md\n"
        "    dst: ~/.setforge_recon/note.md\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - note\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, body: str) -> None:
    tracked = repo / "tracked"
    tracked.mkdir(parents=True, exist_ok=True)
    (tracked / "note.md").write_text(body, encoding="utf-8")


def _live() -> Path:
    return Path.home() / ".setforge_recon" / "note.md"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _install(config: Path, *extra: str) -> Result:
    return CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--no-secrets-scan",
            "--no-git-check",
            "--yes",
            *extra,
        ],
    )


def _base() -> bytes | None:
    return reconcile.read_base(_PROFILE, reconcile.file_id("note"))


def _base_mtime_ns() -> int:
    """Mtime of the recorded reconcile base — bumps iff the base is rewritten."""
    return (
        base_store.base_path(_PROFILE, str(reconcile.file_id("note")))
        .stat()
        .st_mtime_ns
    )


def _transition_dirs() -> set[str]:
    root = transitions_root()
    return {p.name for p in root.iterdir() if p.is_dir()} if root.exists() else set()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _status(repo: Path, config: Path) -> Result:
    return CliRunner().invoke(
        app,
        [
            "--source",
            str(repo),
            "status",
            f"--profile={_PROFILE}",
            f"--config={config}",
        ],
    )


def test_first_install_creates_and_records_base(repo: Path) -> None:
    _write_tracked(repo, "v1\n")
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    assert _live().read_text(encoding="utf-8") == "v1\n"
    assert _base() == b"v1\n"


def test_upstream_change_fast_forwards_live(repo: Path) -> None:
    config = _write_config(repo)
    _write_tracked(repo, "v1\n")
    assert _install(config).exit_code == 0
    _write_tracked(repo, "v2\n")
    assert _install(config).exit_code == 0
    assert _live().read_text(encoding="utf-8") == "v2\n"
    assert _base() == b"v2\n"


def test_local_edit_preserved_when_upstream_unchanged(repo: Path) -> None:
    # The F1/F2 fix: a re-install does NOT clobber a local edit with the
    # tracked source when upstream did not change.
    config = _write_config(repo)
    _write_tracked(repo, "v1\n")
    assert _install(config).exit_code == 0
    _live().write_text("locally edited\n", encoding="utf-8")
    assert _install(config).exit_code == 0
    assert _live().read_text(encoding="utf-8") == "locally edited\n"


def test_stale_toml_anchor_has_identical_cli_diagnostics_without_mutation(
    repo: Path,
) -> None:
    from setforge.cli.stage import Decision, _apply, collect_stages, walk
    from setforge.config import load_config, resolve_effective_profile
    from setforge.file_ownership import FileAction
    from setforge.locking import profile_lock
    from setforge.reconcile import hunks as hunks_mod
    from setforge.reconcile import store
    from setforge.reconcile.merge import merge
    from setforge.reconcile.types import HunkClass, file_id

    base = (
        b'title = "demo"\nalpha = 1\nbeta = 2\ngamma = 3\n'
        b"delta = 4\nepsilon = 5\nzeta = 6\n"
    )
    local = base.replace(b"beta = 2", b"beta = 20")
    upstream = base.replace(b"epsilon = 5", b"inserted = true\nepsilon = 5")
    tracked = repo / "tracked" / "settings.toml"
    tracked.parent.mkdir(parents=True)
    tracked.write_bytes(base)
    live = Path.home() / ".setforge_recon" / "settings.toml"
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: settings.toml\n"
        "    dst: ~/.setforge_recon/settings.toml\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files: [settings]\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-b", "main")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=SetForge Tests",
        "-c",
        "user.email=setforge@example.invalid",
        "commit",
        "-m",
        "stale anchor fixture",
    )
    assert _install(config).exit_code == 0
    live.write_bytes(local)

    cfg = load_config(config)
    resolved = resolve_effective_profile(cfg, _PROFILE, repo).resolved
    (old_stage,) = collect_stages(cfg, resolved, repo, _PROFILE)
    assert old_stage.ownership is not None
    assert old_stage.ownership.action is FileAction.REVIEW
    (old_unit,) = old_stage.hunks
    _apply(
        _PROFILE,
        old_stage,
        walk(old_stage.hunks, lambda _hunk, _i, _n: Decision(HunkClass.LOCAL)),
    )
    stored = store.read_index(_PROFILE).files["settings"].hunks
    assert stored == hunks_mod.serialize([replace(old_unit, cls=HunkClass.LOCAL)])

    tracked.write_bytes(upstream)
    merged = merge(base, local, upstream)
    assert merged.clean
    merged_bytes = merged.merged()
    assert isinstance(merged_bytes, bytes)
    assert b"beta = 20" in merged_bytes
    assert b"inserted = true" in merged_bytes
    live.write_bytes(merged_bytes)
    with profile_lock(_PROFILE):
        store.record(
            _PROFILE,
            file_id("settings"),
            base=upstream,
            local=merged_bytes,
            staged=True,
            hunks=stored,
        )

    (fresh_stage,) = collect_stages(cfg, resolved, repo, _PROFILE)
    (fresh_unit,) = fresh_stage.hunks
    assert fresh_unit.unit_id != old_unit.unit_id
    assert fresh_unit.cls is HunkClass.PENDING

    fid = file_id("settings")
    index_path = store._index_path(_PROFILE)

    def snapshot() -> tuple[object, ...]:
        return (
            tracked.read_bytes(),
            live.read_bytes(),
            store.read_base(_PROFILE, fid),
            store.read_local(_PROFILE, fid),
            index_path.read_bytes(),
            store.read_drafts(_PROFILE, fid),
            stat.S_IMODE(tracked.stat().st_mode),
            stat.S_IMODE(live.stat().st_mode),
        )

    before = snapshot()
    runner = CliRunner()
    common = [f"--profile={_PROFILE}", f"--config={config}"]
    stage_result = runner.invoke(app, ["--format=json", "stage", "--list", *common])
    inspect_result = runner.invoke(
        app, ["--format=json", "inspect", "settings", *common]
    )
    inspect_human = runner.invoke(app, ["inspect", "settings", *common])
    dry_run = runner.invoke(
        app,
        [
            "install",
            *common,
            "--dry-run",
            "--locked",
            "--no-fetch",
            "--no-git-check",
            "--no-secrets-scan",
        ],
    )
    for result in (stage_result, inspect_result, inspect_human, dry_run):
        assert result.exit_code == 0, result.output

    (stage_row,) = json.loads(stage_result.stdout)["data"]
    inspect_data = json.loads(inspect_result.stdout)["data"]
    assert stage_row["pending"] == inspect_data["staging"]["pending"] == 1
    assert "merge clean" in " ".join(inspect_human.stdout.split())
    assert "unexpected drift in 0 file(s)" in dry_run.stdout
    assert f"WOULD noop      {live}" in dry_run.stdout
    assert "settings: 0 shared-promotable" in dry_run.stdout
    assert "0 local  1 pending" in dry_run.stdout
    assert snapshot() == before

    real_install = _install(config)
    assert real_install.exit_code == 0, real_install.output
    assert "=== pre-install staging classifications ===" in real_install.stdout
    assert "0 local  1 pending" in real_install.stdout
    assert "run `setforge stage settings` to classify" in real_install.stdout
    assert snapshot() == before


def test_divergent_live_without_base_seeds_and_keeps_live(repo: Path) -> None:
    # First install over a pre-existing, divergent live file (no base yet)
    # SEEDS the merge base from upstream non-interactively while KEEPING the
    # local file — never silently overwritten with the tracked source. The
    # recorded base means the next install reconciles instead of re-seeding.
    _write_tracked(repo, "tracked\n")
    config = _write_config(repo)
    live = _live()
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("pre-existing local\n", encoding="utf-8")
    result = _install(config)
    assert result.exit_code == 0
    assert live.read_text(encoding="utf-8") == "pre-existing local\n"
    # The base is seeded from upstream so the edit is now a tracked local
    # change atop it (the next install reconciles rather than re-prompts).
    assert _base() == b"tracked\n"
    assert "seeded the merge base" in result.output


def test_clean_reinstall_is_idempotent(repo: Path) -> None:
    # INV-4 (install∘install == install): a second identical install must not
    # diverge. Beyond live content that means NO store churn — the recorded
    # merge base is not rewritten. The stronger INV-4 clause (a true no-op
    # writes NO transition dir at all) is pinned by the companion strict-xfail
    # test below, NOT blessed here as an empty-but-present transition.
    config = _write_config(repo)
    _write_tracked(repo, "stable\n")
    assert _install(config).exit_code == 0
    before = _live().read_text(encoding="utf-8")
    base_mtime_before = _base_mtime_ns()
    live_mtime_before = _live().stat().st_mtime_ns
    # Sleep so a spurious rewrite of live/base would land a DIFFERENT mtime,
    # making the stability asserts below actually bite.
    time.sleep(0.01)
    assert _install(config).exit_code == 0
    # Live content + the file itself untouched (byte-identical, same mtime).
    assert _live().read_text(encoding="utf-8") == before == "stable\n"
    assert _live().stat().st_mtime_ns == live_mtime_before
    # No store churn: the recorded merge base is not re-written.
    assert _base_mtime_ns() == base_mtime_before


def test_idempotent_reinstall_writes_no_transition(repo: Path) -> None:
    # INV-4 (no-op-ness): no-new-upstream + no-local-edits must write NO new
    # transition dir. Asserted strictly here instead of being relaxed to
    # "exactly one empty transition" — see the xfail reason above.
    config = _write_config(repo)
    _write_tracked(repo, "stable\n")
    assert _install(config).exit_code == 0
    transitions_before = _transition_dirs()
    assert _install(config).exit_code == 0
    assert _transition_dirs() == transitions_before


def test_converged_dry_run_previews_no_transition_and_mutates_nothing(
    repo: Path,
) -> None:
    config = _write_config(repo)
    _write_tracked(repo, "stable\n")
    assert _install(config).exit_code == 0
    transitions_before = _transition_dirs()
    base_mtime_before = _base_mtime_ns()
    live_mtime_before = _live().stat().st_mtime_ns

    result = _install(config, "--dry-run")

    assert result.exit_code == 0, result.output
    assert "=== would-be transition record ===" in result.output
    assert "no transition would be created" in result.output
    assert "WOULD record" not in result.output
    assert _transition_dirs() == transitions_before
    assert _base_mtime_ns() == base_mtime_before
    assert _live().stat().st_mtime_ns == live_mtime_before


def test_toml_comment_staged_local_keeps_compare_and_dry_run_usable(repo: Path) -> None:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  note:\n"
        "    src: note.toml\n"
        "    dst: ~/.setforge_recon/note.toml\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - note\n",
        encoding="utf-8",
    )
    tracked = repo / "tracked" / "note.toml"
    tracked.parent.mkdir(parents=True)
    base = b'model = "default"\n'
    tracked.write_bytes(base)
    assert _install(config).exit_code == 0

    live = Path.home() / ".setforge_recon" / "note.toml"
    local = base + b"# Auto-injected by opencodex (undo: ocx restore)\n"
    live.write_bytes(local)
    cfg = load_config(config)
    (stage,) = collect_stages(cfg, resolve_profile(cfg, _PROFILE), repo, _PROFILE)
    _apply(
        _PROFILE,
        stage,
        walk(stage.hunks, lambda _h, _i, _n: Decision(HunkClass.LOCAL)),
    )

    (row,) = reconcile_store.read_index(_PROFILE).files["note"].hunks
    assert row["cls"] == HunkClass.LOCAL.value
    assert "reloc_anchor" not in row

    compare = CliRunner().invoke(
        app, ["compare", f"--profile={_PROFILE}", f"--config={config}", "--check"]
    )
    assert compare.exit_code == 0, compare.output
    assert "expected" in compare.output

    dry_run = _install(config, "--dry-run")
    assert dry_run.exit_code == 0, dry_run.output
    assert f"WOULD noop      {live}" in dry_run.output
    assert live.read_bytes() == local
    assert tracked.read_bytes() == base

    (preview,) = preview_capture_profile(
        cfg, _PROFILE, repo, resolved=resolve_profile(cfg, _PROFILE)
    )
    assert preview.store_update is False
    capture_profile(cfg, _PROFILE, repo)
    (row,) = reconcile_store.read_index(_PROFILE).files["note"].hunks
    assert "reloc_anchor" not in row
    assert tracked.read_bytes() == base

    # Hosts affected before this fix already have the mistaken marker in state.
    with locking.profile_lock(_PROFILE):
        index = reconcile_store.read_index(_PROFILE)
        index.files["note"].hunks[0]["reloc_anchor"] = (
            "# Auto-injected by opencodex (undo: ocx restore)"
        )
        reconcile_store.write_index(_PROFILE, index)
    compare = CliRunner().invoke(
        app, ["compare", f"--profile={_PROFILE}", f"--config={config}", "--check"]
    )
    assert compare.exit_code == 0, compare.output
    assert "expected" in compare.output
    dry_run = _install(config, "--dry-run")
    assert dry_run.exit_code == 0, dry_run.output
    assert f"WOULD noop      {live}" in dry_run.output

    (stage,) = collect_stages(cfg, resolve_profile(cfg, _PROFILE), repo, _PROFILE)
    _apply(
        _PROFILE,
        stage,
        walk(stage.hunks, lambda _h, _i, _n: Decision(HunkClass.LOCAL)),
    )
    (row,) = reconcile_store.read_index(_PROFILE).files["note"].hunks
    assert "reloc_anchor" not in row


def test_noop_install_reports_committed_dirty_deployment_as_current(repo: Path) -> None:
    """Transition provenance may be older even though deployed bytes match HEAD."""
    config = _write_config(repo)
    _write_tracked(repo, "v1\n")
    _git(repo, "init", "-b", "main")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=SetForge Tests",
        "-c",
        "user.email=setforge@example.invalid",
        "commit",
        "-m",
        "initial",
    )

    _write_tracked(repo, "v2\n")
    assert _install(config).exit_code == 0
    transitions_after_dirty_install = _transition_dirs()
    _git(repo, "add", "tracked/note.md")
    _git(
        repo,
        "-c",
        "user.name=SetForge Tests",
        "-c",
        "user.email=setforge@example.invalid",
        "commit",
        "-m",
        "track deployed bytes",
    )

    assert _install(config).exit_code == 0
    assert _transition_dirs() == transitions_after_dirty_install
    result = _status(repo, config)

    assert result.exit_code == 0, result.output
    assert "1 commit ahead of last-installed state" in result.output
    assert "(deployed current)" in result.output
    assert "(install lag)" not in result.output


def _setup_conflict(repo: Path) -> Path:
    """Install v1, then diverge BOTH live and tracked → a real conflict."""
    config = _write_config(repo)
    _write_tracked(repo, "l1\nl2\nl3\n")
    assert _install(config).exit_code == 0
    _live().write_text("l1\nLOCAL\nl3\n", encoding="utf-8")
    _write_tracked(repo, "l1\nUPSTREAM\nl3\n")
    return config


def test_noninteractive_conflict_exits_nonzero_and_keeps_live(repo: Path) -> None:
    config = _setup_conflict(repo)
    result = _install(config)
    assert result.exit_code != 0, result.output
    assert "deferred with unresolved conflicts" in result.output
    assert _live().read_text(encoding="utf-8") == "l1\nLOCAL\nl3\n"


def test_auto_use_tracked_resolves_conflict_to_upstream(repo: Path) -> None:
    config = _setup_conflict(repo)
    result = _install(config, "--auto=use-tracked")
    assert result.exit_code == 0, result.output
    assert _live().read_text(encoding="utf-8") == "l1\nUPSTREAM\nl3\n"
    assert _base() == b"l1\nUPSTREAM\nl3\n"


def test_auto_keep_live_resolves_conflict_to_local(repo: Path) -> None:
    config = _setup_conflict(repo)
    result = _install(config, "--auto=keep-live")
    assert result.exit_code == 0, result.output
    # ours for the conflicting line, clean lines pass through; base advances.
    assert _live().read_text(encoding="utf-8") == "l1\nLOCAL\nl3\n"
    assert _base() == b"l1\nUPSTREAM\nl3\n"


def _revert(config: Path) -> Result:
    return CliRunner().invoke(
        app, ["revert", f"--profile={_PROFILE}", f"--config={config}", "--yes"]
    )


def test_revert_restores_base_so_reinstall_recreates(repo: Path) -> None:
    # Revert must restore the reconcile base, not just the live file: else the
    # next install sees a stale base (theirs == base) and treats the
    # reverted-away file as a deletion instead of re-deploying it.
    config = _write_config(repo)
    _write_tracked(repo, "v1\n")
    assert _install(config).exit_code == 0
    assert _base() == b"v1\n"

    assert _revert(config).exit_code == 0, "revert should succeed"
    assert not _live().exists(), "revert removes the created live file"
    assert _base() is None, "revert restores the pre-install (absent) base"

    # Reinstall re-creates the file (a fresh first install again).
    assert _install(config).exit_code == 0
    assert _live().read_text(encoding="utf-8") == "v1\n"
    assert _base() == b"v1\n"


def test_clean_deletion_is_honored_not_resurrected(repo: Path) -> None:
    config = _write_config(repo)
    _write_tracked(repo, "v1\n")
    assert _install(config).exit_code == 0
    assert _live().exists()

    _live().unlink()
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert not _live().exists(), "honored deletion must not resurrect the file"
    assert "kept absent" in result.output
    assert _base() == b"v1\n"


def test_second_install_after_deletion_is_a_real_noop(repo: Path) -> None:
    config = _write_config(repo)
    _write_tracked(repo, "v1\n")
    assert _install(config).exit_code == 0
    _live().unlink()
    assert _install(config).exit_code == 0
    transitions_before = _transition_dirs()
    assert _install(config).exit_code == 0
    assert not _live().exists()
    assert _transition_dirs() == transitions_before, "steady-state must not churn"


def test_delete_modify_routes_to_deferred(repo: Path) -> None:
    config = _write_config(repo)
    _write_tracked(repo, "v1\n")
    assert _install(config).exit_code == 0
    _live().unlink()
    _write_tracked(repo, "v2\n")
    result = _install(config)
    assert result.exit_code != 0, result.output
    assert "conflict" in result.output.lower()
    assert _base() == b"v1\n", "delete/modify must NOT advance the base"


def test_revert_restores_deletion_and_no_resurrect(repo: Path) -> None:
    config = _write_config(repo)
    _write_tracked(repo, "v1\n")
    assert _install(config).exit_code == 0
    _live().unlink()
    assert _install(config).exit_code == 0
    assert not _live().exists()

    assert _revert(config).exit_code == 0, "revert of a honored deletion"
    assert _base() == b"v1\n", "revert restores the pre-delete base bytes"
    assert not _live().exists(), "the live file stayed absent (nothing to undo)"

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert not _live().exists()
    assert _base() == b"v1\n"


def test_claude_merge_wired_only_when_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D6: the plain-file wizard gets a real claude-merge fn ONLY interactively;
    # non-interactively it stays the unavailable stub (never auto-invoked).
    from setforge.cli import _install_helpers as ih
    from setforge.config import TrackedFile
    from setforge.reconcile.wizard import claude_merge_unavailable
    from setforge.reconcile_apply import ReconcileKind, ReconcileOutcome

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    src = tmp_path / "note.md"
    src.write_text("x\n", encoding="utf-8")
    dst = tmp_path / "live" / "note.md"
    tf = TrackedFile.model_validate({"src": "note.md", "dst": str(dst)})

    captured: dict[str, object] = {}
    sentinel = object()
    monkeypatch.setattr(
        "setforge.reconcile.claude_merge.make_claude_merge_fn",
        lambda *, display_path: sentinel,
    )

    def _fake_rpf(_profile: str, _fid: object, **kw: object) -> ReconcileOutcome:
        captured["cm"] = kw["claude_merge"]
        return ReconcileOutcome(ReconcileKind.NOOP)

    monkeypatch.setattr(ih.reconcile_apply, "reconcile_plain_file", _fake_rpf)

    ih._resolve_plain_reconcile(
        "p", "note", src, dst, tf, interactive=True, section_auto=None
    )
    assert captured["cm"] is sentinel
    ih._resolve_plain_reconcile(
        "p", "note", src, dst, tf, interactive=False, section_auto=None
    )
    assert captured["cm"] is claude_merge_unavailable
