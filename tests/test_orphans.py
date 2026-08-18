"""Unit tests for tracked-file orphan detection + cleanup-orphans subcommand.

Covers :func:`setforge.compare.detect_orphans` and the CLI surface in
:mod:`setforge.cli.orphans`. Docker-end-to-end coverage lives in
``tests/docker/test_e2e_docker_orphans.py``.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from ruamel.yaml import YAML
from typer.testing import CliRunner

from setforge import compare as compare_mod
from setforge import transitions
from setforge.cli import app
from setforge.cli import orphans as orphans_mod
from setforge.compare import (
    OrphanDetection,
    OrphanEntry,
    detect_orphans,
    load_ignored_orphans,
)
from setforge.config import Config, Profile, TrackedFile, resolve_profile
from setforge.errors import ConfigError, OrphanCleanupRequiresInteractive

_ANSI_RE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi_and_newlines(text: str) -> str:
    """Strip ANSI escapes AND collapse newlines; CliRunner's Rich console
    wraps paths and counts across both, fragmenting substring asserts."""
    return _ANSI_RE.sub("", text).replace("\n", "")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect ``SETFORGE_STATE_DIR`` to a per-test tmp tree."""
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def _write_config_file(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _make_config_with(tracked: dict[str, TrackedFile]) -> Config:
    """Build a Config with ``tracked`` and a single profile referencing all of them."""
    return Config(
        tracked_files=tracked,
        profiles={"p": Profile(tracked_files=list(tracked))},
    )


def _write_meta_record(transitions_root: Path, dirname: str, paths: list[str]) -> Path:
    """Write a minimal ``meta.json`` record exposing ``paths`` for detection."""
    target = transitions_root / dirname
    target.mkdir(parents=True, exist_ok=True)
    payload = {
        "command": "install",
        "profile": "p",
        "timestamp": "2026-05-19T12:00:00+00:00",
        "host": "test-host",
        "version": "0.2.0",
        "paths": paths,
    }
    (target / "meta.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    return target


# ---------------------------------------------------------------------------
# detect_orphans()
# ---------------------------------------------------------------------------


def test_detect_orphans_finds_removed_from_yaml(tmp_path: Path) -> None:
    """A path in meta.json's `paths` that's no longer in resolved
    tracked_files surfaces as an orphan."""
    transitions_dir = tmp_path / "transitions"
    live_orphan = tmp_path / "live" / "orphan.txt"
    live_kept = tmp_path / "live" / "kept.txt"
    # The orphan must EXIST on disk to survive the existence gate.
    live_orphan.parent.mkdir(parents=True, exist_ok=True)
    live_orphan.write_text("orphan body\n", encoding="utf-8")
    _write_meta_record(
        transitions_dir,
        "20260518T120000000000Z-install-p",
        [str(live_orphan), str(live_kept)],
    )

    config = _make_config_with(
        {"kept": TrackedFile(src=Path("kept.txt"), dst=str(live_kept))}
    )

    detection = detect_orphans(
        resolve_profile_wrap(config, "p"),
        config,
        transitions_dir,
        tmp_path,
    )
    assert detection.orphans == [OrphanEntry(path=live_orphan)]


def test_detect_orphans_empty_when_transitions_missing(tmp_path: Path) -> None:
    transitions_dir = tmp_path / "no-transitions"
    config = _make_config_with(
        {"x": TrackedFile(src=Path("x"), dst=str(tmp_path / "x"))}
    )
    assert (
        detect_orphans(
            resolve_profile_wrap(config, "p"), config, transitions_dir, tmp_path
        ).orphans
        == []
    )


def test_detect_orphans_ignores_corrupt_meta(tmp_path: Path) -> None:
    transitions_dir = tmp_path / "transitions"
    target = transitions_dir / "20260518T120000000000Z-install-p"
    target.mkdir(parents=True)
    (target / "meta.json").write_text("{not json", encoding="utf-8")
    # Second, valid record with one orphan.
    live_orphan = tmp_path / "live" / "orphan.txt"
    live_orphan.parent.mkdir(parents=True, exist_ok=True)
    live_orphan.write_text("body\n", encoding="utf-8")
    _write_meta_record(
        transitions_dir,
        "20260518T130000000000Z-install-p",
        [str(live_orphan)],
    )
    # A tracked_file establishes ``tmp_path/live`` as a managed dst root so
    # the orphan (a sibling under it) survives the managed-scope guard.
    config = _make_config_with(
        {
            "kept": TrackedFile(
                src=Path("kept.txt"), dst=str(tmp_path / "live" / "kept.txt")
            )
        }
    )
    detection = detect_orphans(
        resolve_profile_wrap(config, "p"), config, transitions_dir, tmp_path
    )
    assert detection.orphans == [OrphanEntry(path=live_orphan)]


def test_detect_orphans_respects_ignore_list(tmp_path: Path) -> None:
    """Tracked_file IDs in ``ignored`` are excluded from orphan output
    via their resolved destination."""
    transitions_dir = tmp_path / "transitions"
    live_ignored = tmp_path / "live" / "ignored.txt"
    _write_meta_record(
        transitions_dir,
        "20260518T120000000000Z-install-p",
        [str(live_ignored)],
    )
    # 'ignored_id' is in the config but NOT in the profile's tracked_files
    # → it would be an orphan by default; the ignored frozenset must
    # suppress it.
    config = Config(
        tracked_files={
            "ignored_id": TrackedFile(src=Path("ignored.txt"), dst=str(live_ignored)),
        },
        profiles={"p": Profile(tracked_files=[])},
    )
    detection = detect_orphans(
        resolve_profile_wrap(config, "p"),
        config,
        transitions_dir,
        tmp_path,
        ignored=frozenset({"ignored_id"}),
    )
    assert detection.orphans == []


def test_detect_orphans_excludes_tracked_source(tmp_path: Path) -> None:
    """A path under the config-repo `tracked/` tree (a tracked SOURCE)
    is never an orphan — even when it exists on disk and is absent from
    the active dst set. Guards against a meta.json that recorded src
    paths scheduling the config source of truth for deletion."""
    repo_root = tmp_path / "config-repo"
    transitions_dir = tmp_path / "transitions"
    src_file = repo_root / "tracked" / "claude" / "CLAUDE.md"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("# source of truth\n", encoding="utf-8")
    live_orphan = tmp_path / "live" / "orphan.txt"
    live_orphan.parent.mkdir(parents=True, exist_ok=True)
    live_orphan.write_text("orphan\n", encoding="utf-8")
    _write_meta_record(
        transitions_dir,
        "20260518T120000000000Z-install-p",
        [str(src_file), str(live_orphan)],
    )
    # A tracked_file establishes ``tmp_path/live`` as a managed dst root so
    # the orphan survives managed-scope; the src under ``tracked/`` is still
    # excluded by the source guard.
    config = _make_config_with(
        {
            "kept": TrackedFile(
                src=Path("kept.txt"), dst=str(tmp_path / "live" / "kept.txt")
            )
        }
    )
    detection = detect_orphans(
        resolve_profile_wrap(config, "p"), config, transitions_dir, repo_root
    )
    assert detection.orphans == [OrphanEntry(path=live_orphan)]
    assert detection.skipped_source == 1


def test_detect_orphans_refuses_dotdot_escape_src(tmp_path: Path) -> None:
    """A tracked_file `src` that escapes `tracked/` via `..` is now
    refused outright by `resolve_src`'s containment guard — an
    out-of-tree src bypasses the gitleaks sweep, so it never reaches
    the orphan set at all."""
    repo_root = tmp_path / "repo"
    transitions_dir = tmp_path / "transitions"
    # `src: ../outside/x.txt` resolves (lexically) to repo_root/outside/x.txt.
    escaped = repo_root / "outside" / "x.txt"
    escaped.parent.mkdir(parents=True, exist_ok=True)
    escaped.write_text("src\n", encoding="utf-8")
    _write_meta_record(
        transitions_dir,
        "20260518T120000000000Z-install-p",
        [str(repo_root / "tracked" / ".." / "outside" / "x.txt")],
    )
    config = _make_config_with(
        {
            "x": TrackedFile(
                src=Path("../outside/x.txt"), dst=str(tmp_path / "live" / "x")
            )
        }
    )
    with pytest.raises(ConfigError, match="outside"):
        detect_orphans(
            resolve_profile_wrap(config, "p"), config, transitions_dir, repo_root
        )


def test_detect_orphans_expands_directory_tracked_file_children(tmp_path: Path) -> None:
    """A directory tracked_file's deployed CHILD dst is excluded: the
    dst set expands directories via `expand_tracked_file`, so children
    are not flagged as orphans (regression guard for the third
    false-positive class)."""
    repo_root = tmp_path / "repo"
    transitions_dir = tmp_path / "transitions"
    src_dir = repo_root / "tracked" / "skills"
    (src_dir / "a").mkdir(parents=True, exist_ok=True)
    (src_dir / "a" / "SKILL.md").write_text("skill\n", encoding="utf-8")
    dst_dir = tmp_path / "live" / "skills"
    child_dst = dst_dir / "a" / "SKILL.md"
    child_dst.parent.mkdir(parents=True, exist_ok=True)
    child_dst.write_text("skill\n", encoding="utf-8")
    _write_meta_record(
        transitions_dir,
        "20260518T120000000000Z-install-p",
        [str(child_dst)],
    )
    config = _make_config_with(
        {"skills": TrackedFile(src=Path("skills"), dst=str(dst_dir))}
    )
    detection = detect_orphans(
        resolve_profile_wrap(config, "p"), config, transitions_dir, repo_root
    )
    assert detection.orphans == []


def test_detect_orphans_retains_dangling_symlink_drops_absent(tmp_path: Path) -> None:
    """lexists gate (matches the apply path's lstat): a dangling symlink
    is a real, deletable dir entry → RETAINED; a fully-absent path is
    dropped and tallied in `skipped_absent`."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    transitions_dir = tmp_path / "transitions"
    dangling = tmp_path / "live" / "dangling_link"
    dangling.parent.mkdir(parents=True, exist_ok=True)
    dangling.symlink_to(tmp_path / "nonexistent_target")
    absent = tmp_path / "live" / "gone.txt"
    _write_meta_record(
        transitions_dir,
        "20260518T120000000000Z-install-p",
        [str(dangling), str(absent)],
    )
    # A tracked_file establishes ``tmp_path/live`` as a managed dst root so
    # both candidates survive managed-scope (then split by the existence gate).
    config = _make_config_with(
        {
            "kept": TrackedFile(
                src=Path("kept.txt"), dst=str(tmp_path / "live" / "kept.txt")
            )
        }
    )
    detection = detect_orphans(
        resolve_profile_wrap(config, "p"), config, transitions_dir, repo_root
    )
    assert detection.orphans == [OrphanEntry(path=dangling)]
    assert detection.skipped_absent == 1
    assert detection.skipped_source == 0
    assert detection.skipped_unmanaged == 0


def resolve_profile_wrap(config: Config, name: str) -> Any:
    """Wrap ``resolve_profile`` so the helper above stays single-line."""
    return resolve_profile(config, name)


# ---------------------------------------------------------------------------
# detect_orphans() — managed-scope guard
# ---------------------------------------------------------------------------


@pytest.fixture
def managed_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Treat ``tmp_path`` as a generic root so the managed-dst-root climb
    stops there.

    Without this, ``tmp_path`` (a shared ancestor of every fixture dst)
    would itself be a managed root, making every sibling subtree under it
    count as managed and defeating the exclusion assertions. In production
    the climb stops at the real ``$HOME``/``~/.config``/``/tmp`` etc.; the
    fixture reproduces that boundary at the test root.
    """
    monkeypatch.setattr(
        compare_mod,
        "GENERIC_DST_ROOTS",
        compare_mod.GENERIC_DST_ROOTS | {compare_mod._norm(tmp_path)},
    )
    return tmp_path


def test_detect_orphans_skips_unmanaged_path(
    tmp_path: Path, managed_boundary: Path
) -> None:
    """A previously-deployed path outside every managed dst root (e.g. a
    config left by a retired profile) is dropped as unmanaged, even when
    it still exists on disk."""
    transitions_dir = tmp_path / "transitions"
    config = _make_config_with(
        {
            "keep": TrackedFile(
                src=Path("keep"), dst=str(tmp_path / "managed" / "keep.txt")
            )
        }
    )
    unmanaged = tmp_path / "retired" / "worktrunk.toml"
    unmanaged.parent.mkdir(parents=True, exist_ok=True)
    unmanaged.write_text("live config\n", encoding="utf-8")
    _write_meta_record(
        transitions_dir, "20260518T120000000000Z-install-p", [str(unmanaged)]
    )
    detection = detect_orphans(
        resolve_profile_wrap(config, "p"), config, transitions_dir, tmp_path
    )
    assert detection.orphans == []
    assert detection.skipped_unmanaged == 1


def test_detect_orphans_excludes_host_local_config(
    tmp_path: Path, managed_boundary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A setforge-written host-local file (LOCAL_CONFIG_PATH) is NEVER an
    orphan, even when it sits under a managed dst root and was recorded in a
    transition. Regression for the over-reach where cleanup-orphans wanted to
    delete ~/.config/setforge/local.yaml (data loss)."""
    transitions_dir = tmp_path / "transitions"
    managed = tmp_path / "cfgdir"
    # A tracked file deploys INTO cfgdir, making it a managed root — mirrors
    # claude-canary.sh making ~/.config/setforge a managed root.
    config = _make_config_with(
        {"canary": TrackedFile(src=Path("canary"), dst=str(managed / "canary.sh"))}
    )
    local_yaml = managed / "local.yaml"
    local_yaml.parent.mkdir(parents=True, exist_ok=True)
    local_yaml.write_text("tracked_files: {}\n", encoding="utf-8")
    # HOST_LOCAL_FILES is computed from the real LOCAL_CONFIG_PATH at import;
    # point it at this fixture (same pattern as managed_boundary/GENERIC_DST_ROOTS).
    monkeypatch.setattr(
        compare_mod, "HOST_LOCAL_FILES", frozenset({compare_mod._norm(local_yaml)})
    )
    _write_meta_record(
        transitions_dir, "20260518T120000000000Z-install-p", [str(local_yaml)]
    )
    detection = detect_orphans(
        resolve_profile_wrap(config, "p"), config, transitions_dir, tmp_path
    )
    assert detection.orphans == []
    assert detection.skipped_host_local == 1


def test_detect_orphans_excludes_bootstrap_stubs(
    tmp_path: Path, managed_boundary: Path
) -> None:
    """A config-driven bootstrap stub (e.g. ~/.claude/header.md) is NEVER an
    orphan even under a managed root + recorded in a transition. Derived from
    every profile's bootstrap list (not a hardcoded literal) — the header.md
    sibling of the local.yaml data-loss class."""
    transitions_dir = tmp_path / "transitions"
    managed = tmp_path / "claude"
    header = managed / "header.md"
    header.parent.mkdir(parents=True, exist_ok=True)
    header.write_text("# host header\n", encoding="utf-8")
    config = Config(
        tracked_files={
            "canary": TrackedFile(src=Path("canary"), dst=str(managed / "canary.sh"))
        },
        profiles={"p": Profile(tracked_files=["canary"], bootstrap=[header])},
    )
    _write_meta_record(
        transitions_dir, "20260518T120000000000Z-install-p", [str(header)]
    )
    detection = detect_orphans(
        resolve_profile_wrap(config, "p"), config, transitions_dir, tmp_path
    )
    assert detection.orphans == []
    assert detection.skipped_host_local == 1


def test_detect_orphans_skips_source_manifest(
    tmp_path: Path, managed_boundary: Path
) -> None:
    """The config source manifest (setforge.yaml), recorded by a migrate
    transition, is never an orphan — it lives outside the managed dst
    roots (and is excluded by scope, not just absence)."""
    transitions_dir = tmp_path / "transitions"
    config = _make_config_with(
        {
            "keep": TrackedFile(
                src=Path("keep"), dst=str(tmp_path / "managed" / "keep.txt")
            )
        }
    )
    manifest = tmp_path / "config-repo" / "setforge.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("schema_version: '2.0'\n", encoding="utf-8")
    _write_meta_record(
        transitions_dir, "20260518T120000000000Z-migrate-migrate", [str(manifest)]
    )
    detection = detect_orphans(
        resolve_profile_wrap(config, "p"), config, transitions_dir, tmp_path
    )
    assert detection.orphans == []
    assert detection.skipped_unmanaged == 1


def test_detect_orphans_keeps_removed_managed_subtree(
    tmp_path: Path, managed_boundary: Path
) -> None:
    """A file removed from a still-managed tree surfaces: it shares a
    managed ancestor (the deploy dir) with surviving tracked dsts even
    though nothing tracked remains in its own subdir."""
    transitions_dir = tmp_path / "transitions"
    skills = tmp_path / "managed" / "skills"
    config = _make_config_with(
        {"keep": TrackedFile(src=Path("keep"), dst=str(skills / "keep" / "SKILL.md"))}
    )
    gone = skills / "gone" / "SKILL.md"
    gone.parent.mkdir(parents=True, exist_ok=True)
    gone.write_text("retired skill\n", encoding="utf-8")
    _write_meta_record(transitions_dir, "20260518T120000000000Z-install-p", [str(gone)])
    detection = detect_orphans(
        resolve_profile_wrap(config, "p"), config, transitions_dir, tmp_path
    )
    assert detection.orphans == [OrphanEntry(path=gone)]
    assert detection.skipped_unmanaged == 0


def test_detect_orphans_managed_root_prefix_not_substring(
    tmp_path: Path, managed_boundary: Path
) -> None:
    """Managed-scope containment is component-wise (``is_relative_to``), so a
    sibling whose name is a string prefix of a managed dir does NOT match
    it: ``…/managed-evil`` is not inside ``…/managed``."""
    transitions_dir = tmp_path / "transitions"
    config = _make_config_with(
        {
            "keep": TrackedFile(
                src=Path("keep"), dst=str(tmp_path / "managed" / "keep.txt")
            )
        }
    )
    sibling = tmp_path / "managed-evil" / "x.txt"
    sibling.parent.mkdir(parents=True, exist_ok=True)
    sibling.write_text("not managed\n", encoding="utf-8")
    _write_meta_record(
        transitions_dir, "20260518T120000000000Z-install-p", [str(sibling)]
    )
    detection = detect_orphans(
        resolve_profile_wrap(config, "p"), config, transitions_dir, tmp_path
    )
    assert detection.orphans == []
    assert detection.skipped_unmanaged == 1


def test_detect_orphans_overreach_regression(
    tmp_path: Path, managed_boundary: Path
) -> None:
    """End-to-end over-reach regression: a ledger mixing the source
    manifest, a retired-profile config, /tmp scratch, AND one genuinely
    removed managed dst surfaces ONLY the removed managed dst. The junk all
    EXISTS on disk (except the /tmp path) to prove exclusion is by scope,
    not by absence. None of the junk is under ``tracked/`` or a tracked
    src, so the source guard passes it through and the managed-scope guard
    is what excludes it (hence ``skipped_unmanaged == 3``); guard order is
    source → managed → existence."""
    transitions_dir = tmp_path / "transitions"
    managed_dir = tmp_path / "managed"
    managed_dir.mkdir(parents=True, exist_ok=True)
    config = _make_config_with(
        {"keep": TrackedFile(src=Path("keep"), dst=str(managed_dir / "keep.txt"))}
    )
    real_orphan = managed_dir / "orphan.txt"
    real_orphan.write_text("orphan\n", encoding="utf-8")
    manifest = tmp_path / "config-repo" / "setforge.yaml"
    retired = tmp_path / "retired" / "worktrunk.toml"
    for junk in (manifest, retired):
        junk.parent.mkdir(parents=True, exist_ok=True)
        junk.write_text("junk\n", encoding="utf-8")
    scratch = Path("/tmp/mig2/setforge.yaml")  # generic /tmp root, never created
    _write_meta_record(
        transitions_dir,
        "20260518T120000000000Z-install-p",
        [str(real_orphan), str(manifest), str(retired), str(scratch)],
    )
    detection = detect_orphans(
        resolve_profile_wrap(config, "p"), config, transitions_dir, tmp_path
    )
    assert detection.orphans == [OrphanEntry(path=real_orphan)]
    assert detection.skipped_unmanaged == 3
    assert detection.skipped_absent == 0


def test_rmdir_empty_parents_keeps_dir_with_sibling(tmp_path: Path) -> None:
    """``_rmdir_empty_parents`` removes a dir emptied by the deletion but
    keeps one still holding an unrelated sibling (Decision 5: keep the
    directory unless the orphan was its only content)."""
    emptied = tmp_path / "emptied"
    emptied.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "user_file.txt").write_text("keep me\n", encoding="utf-8")
    orphans_mod._rmdir_empty_parents([emptied, shared], Console())
    assert not emptied.exists()
    assert shared.exists()
    assert (shared / "user_file.txt").exists()


# ---------------------------------------------------------------------------
# load_ignored_orphans()
# ---------------------------------------------------------------------------


def test_load_ignored_orphans_missing_returns_empty() -> None:
    """No local.yaml on disk → empty frozenset (no surprise crash)."""
    assert load_ignored_orphans() == frozenset()


def test_load_ignored_orphans_parses_list(tmp_path: Path) -> None:
    """A `orphan_ignore:` block round-trips into a frozenset."""
    cfg_path = compare_mod.LOCAL_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("orphan_ignore:\n  - foo\n  - bar\n", encoding="utf-8")
    assert load_ignored_orphans() == frozenset({"foo", "bar"})


def test_load_ignored_orphans_corrupt_yaml_returns_empty(tmp_path: Path) -> None:
    """Best-effort: malformed YAML must NOT crash compare."""
    cfg_path = compare_mod.LOCAL_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("not: [valid: yaml\n", encoding="utf-8")
    assert load_ignored_orphans() == frozenset()


# ---------------------------------------------------------------------------
# CLI: dry-run default
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write_minimal_yaml(tmp_path: Path) -> Path:
    """Build a minimal setforge.yaml with one tracked_file."""
    cfg = tmp_path / "setforge.yaml"
    body = (
        "version: 1\n"
        "tracked_files:\n"
        "  kept:\n"
        "    src: kept.txt\n"
        f"    dst: {tmp_path / 'live' / 'kept.txt'}\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [kept]\n"
    )
    _write_config_file(cfg, body)
    return cfg


def _write_retargeted_active_path(
    tmp_path: Path,
    transitions_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    """Create an active destination that exists only through local.yaml."""
    from setforge import source as source_mod

    cfg = _write_minimal_yaml(tmp_path)
    tracked = tmp_path / "tracked"
    tracked.mkdir()
    (tracked / "kept.txt").write_text("shared\n", encoding="utf-8")
    effective = tmp_path / "live" / "effective.txt"
    effective.parent.mkdir(parents=True, exist_ok=True)
    effective.write_text("active\n", encoding="utf-8")
    local_config = tmp_path / "local.yaml"
    local_config.write_text(
        f"tracked_files:\n  kept:\n    dst: {effective}\n", encoding="utf-8"
    )
    monkeypatch.setattr(source_mod, "LOCAL_CONFIG_PATH", local_config)
    monkeypatch.setattr(compare_mod, "LOCAL_CONFIG_PATH", local_config)
    _write_meta_record(
        transitions_root,
        "20260518T120000000000Z-install-p",
        [str(effective)],
    )
    return cfg, effective


def test_apply_default_is_dry_run(
    runner: CliRunner, tmp_path: Path, isolated_state_dir: Path
) -> None:
    """Without `--apply`, the subcommand must not delete anything."""
    cfg = _write_minimal_yaml(tmp_path)
    live_orphan = tmp_path / "live" / "orphan.txt"
    live_orphan.parent.mkdir(parents=True, exist_ok=True)
    live_orphan.write_text("orphan body\n", encoding="utf-8")

    _write_meta_record(
        isolated_state_dir / "transitions",
        "20260518T120000000000Z-install-p",
        [str(live_orphan)],
    )

    result = runner.invoke(
        app, ["cleanup-orphans", "--profile", "p", "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.output
    assert live_orphan.exists()  # CRITICAL: not deleted under dry-run.
    # Human command output rides stderr (stdout stays machine-readable).
    plain = _strip_ansi_and_newlines(result.stderr)
    assert "WOULD delete" in plain
    assert live_orphan.name in plain


def test_dry_run_keeps_host_retargeted_active_destination(
    runner: CliRunner,
    tmp_path: Path,
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The effective local destination is active, never an orphan candidate."""
    cfg, effective = _write_retargeted_active_path(
        tmp_path, isolated_state_dir / "transitions", monkeypatch
    )

    result = runner.invoke(
        app, ["cleanup-orphans", "--profile", "p", "--config", str(cfg)]
    )

    assert result.exit_code == 0, result.output
    assert "=== no orphans ===" in _strip_ansi_and_newlines(result.stderr)
    assert effective.read_text(encoding="utf-8") == "active\n"


def test_apply_keeps_host_retargeted_active_destination(
    runner: CliRunner,
    tmp_path: Path,
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even the confirmed mutation path cannot delete the effective target."""
    cfg, effective = _write_retargeted_active_path(
        tmp_path, isolated_state_dir / "transitions", monkeypatch
    )

    result = runner.invoke(
        app,
        [
            "cleanup-orphans",
            "--profile",
            "p",
            "--config",
            str(cfg),
            "--apply",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "=== no orphans ===" in _strip_ansi_and_newlines(result.stderr)
    assert effective.read_text(encoding="utf-8") == "active\n"


def test_dry_run_prints_skip_note(
    runner: CliRunner, tmp_path: Path, isolated_state_dir: Path
) -> None:
    """When the guards filter candidates, the dry-run prints a one-line
    skip note with absent / tracked-source counts."""
    cfg = _write_minimal_yaml(tmp_path)
    live_orphan = tmp_path / "live" / "orphan.txt"
    live_orphan.parent.mkdir(parents=True, exist_ok=True)
    live_orphan.write_text("orphan\n", encoding="utf-8")
    absent = tmp_path / "live" / "gone.txt"  # recorded but never created.
    _write_meta_record(
        isolated_state_dir / "transitions",
        "20260518T120000000000Z-install-p",
        [str(live_orphan), str(absent)],
    )

    result = runner.invoke(
        app, ["cleanup-orphans", "--profile", "p", "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.output
    plain = _strip_ansi_and_newlines(result.stderr)
    assert "WOULD delete" in plain
    assert "skipped 1 previously-touched path(s)" in plain
    assert "1 no longer on disk" in plain
    assert "0 tracked source" in plain
    assert "0 unmanaged" in plain


def test_dry_run_no_skip_note_when_clean(
    runner: CliRunner, tmp_path: Path, isolated_state_dir: Path
) -> None:
    """No skip note when nothing was filtered — the note is suppressed."""
    cfg = _write_minimal_yaml(tmp_path)
    live_orphan = tmp_path / "live" / "orphan.txt"
    live_orphan.parent.mkdir(parents=True, exist_ok=True)
    live_orphan.write_text("orphan\n", encoding="utf-8")
    _write_meta_record(
        isolated_state_dir / "transitions",
        "20260518T120000000000Z-install-p",
        [str(live_orphan)],
    )

    result = runner.invoke(
        app, ["cleanup-orphans", "--profile", "p", "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.output
    plain = _strip_ansi_and_newlines(result.stderr)
    assert "WOULD delete" in plain
    assert "skipped" not in plain


def test_apply_non_tty_raises(
    runner: CliRunner,
    tmp_path: Path,
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--apply` + non-TTY + no `--yes` raises OrphanCleanupRequiresInteractive."""
    cfg = _write_minimal_yaml(tmp_path)
    live_orphan = tmp_path / "live" / "orphan.txt"
    live_orphan.parent.mkdir(parents=True, exist_ok=True)
    live_orphan.write_text("orphan body\n", encoding="utf-8")
    _write_meta_record(
        isolated_state_dir / "transitions",
        "20260518T120000000000Z-install-p",
        [str(live_orphan)],
    )

    # CliRunner stdin is always non-TTY; no monkeypatching needed.
    # CliRunner invokes the Typer `app` directly (NOT `main()`), so the
    # SetforgeError surfaces on `result.exception`, not the stderr-formatted
    # exit code that `main()`'s top-level handler produces.
    result = runner.invoke(
        app, ["cleanup-orphans", "--profile", "p", "--config", str(cfg), "--apply"]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, OrphanCleanupRequiresInteractive)
    assert "requires --yes when stdin is not a TTY" in str(result.exception)
    assert live_orphan.exists()  # mutate-gate: no deletion on raise.


def test_apply_yes_writes_transition_first(
    tmp_path: Path,
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--apply --yes` → `_write_orphan_transition` must fire BEFORE any unlink.

    Probes the transition-first invariant directly on
    :func:`_execute_cleanup_locked` so the test doesn't depend on
    prompt_toolkit being available or on CliRunner stdin behavior.
    """
    live_orphan = tmp_path / "live" / "orphan.txt"
    live_orphan.parent.mkdir(parents=True, exist_ok=True)
    live_orphan.write_text("body\n", encoding="utf-8")
    orphan_entry = OrphanEntry(path=live_orphan)

    order: list[str] = []
    real_write = transitions.write_transition

    def _spy_write_transition(*args: object, **kwargs: object) -> Path:
        order.append("write_transition")
        return real_write(*args, **kwargs)  # type: ignore[arg-type]

    real_unlink = Path.unlink

    def _spy_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == live_orphan:
            order.append("unlink")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(
        "setforge.cli.orphans.transitions.write_transition", _spy_write_transition
    )
    monkeypatch.setattr(Path, "unlink", _spy_unlink)

    orphans_mod._execute_cleanup_locked(
        "p",
        [orphan_entry],
        orphans_mod.ApplyChoice.DELETE_AND_TRANSITION,
        Console(),
    )
    assert order == ["write_transition", "unlink"], order
    assert not live_orphan.exists()


def test_apply_yes_holds_profile_lock(
    tmp_path: Path,
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The apply path must enter profile_lock before its refreshed unlink.

    Every other mutating verb (install/sync/revert) serializes its live
    mutation under profile_lock; orphan cleanup deletes files + writes a
    transition and must too. Fails against the old unlocked behavior: no
    "enter" event precedes the "unlink".
    """
    import contextlib

    from setforge import locking

    live_orphan = tmp_path / "live" / "orphan.txt"
    live_orphan.parent.mkdir(parents=True, exist_ok=True)
    live_orphan.write_text("body\n", encoding="utf-8")
    orphan_entry = OrphanEntry(path=live_orphan)

    events: list[str] = []
    real_lock = locking.profile_lock

    @contextlib.contextmanager
    def _recording_lock(profile: str, timeout: float | None = None):
        events.append("enter")
        with real_lock(profile, timeout=timeout):
            try:
                yield
            finally:
                events.append("exit")

    real_unlink = Path.unlink

    def _spy_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == live_orphan:
            events.append("unlink")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr("setforge.cli.orphans.profile_lock", _recording_lock)
    monkeypatch.setattr(Path, "unlink", _spy_unlink)
    detection = OrphanDetection(orphans=[orphan_entry])
    monkeypatch.setattr(
        orphans_mod,
        "_detect_orphans_live",
        lambda profile, config_path: (_make_config_with({}), detection),
    )

    orphans_mod._apply_orphan_cleanup(
        "p", tmp_path / "setforge.yaml", yes=True, console=Console()
    )

    assert "enter" in events, "cleanup never acquired the profile lock"
    assert "unlink" in events, "cleanup never unlinked the orphan"
    assert events.index("enter") < events.index("unlink"), (
        f"lock must be held before mutating; order: {events}"
    )
    assert events[-1] == "exit", f"lock must be released last; order: {events}"


def test_apply_redetects_after_prompt_inside_profile_lock(
    tmp_path: Path,
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path made active while cleanup waits is retained by locked re-scan."""
    from setforge import source as source_mod

    cfg = _write_minimal_yaml(tmp_path)
    tracked = tmp_path / "tracked"
    tracked.mkdir()
    (tracked / "kept.txt").write_text("shared\n", encoding="utf-8")
    candidate = tmp_path / "live" / "newly-active.txt"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("installed while waiting\n", encoding="utf-8")
    _write_meta_record(
        isolated_state_dir / "transitions",
        "20260518T120000000000Z-install-p",
        [str(candidate)],
    )
    local_config = tmp_path / "local.yaml"
    monkeypatch.setattr(source_mod, "LOCAL_CONFIG_PATH", local_config)
    monkeypatch.setattr(compare_mod, "LOCAL_CONFIG_PATH", local_config)
    prompt_called = False

    def _activate_during_prompt(*, yes: bool) -> orphans_mod.ApplyChoice:
        nonlocal prompt_called
        prompt_called = True
        assert yes is False
        local_config.write_text(
            f"tracked_files:\n  kept:\n    dst: {candidate}\n", encoding="utf-8"
        )
        return orphans_mod.ApplyChoice.DELETE_AND_TRANSITION

    monkeypatch.setattr(orphans_mod, "_pick_cleanup_branch", _activate_during_prompt)

    orphans_mod._apply_orphan_cleanup("p", cfg, yes=False, console=Console())

    assert prompt_called, "cleanup never reached the prompt activation boundary"
    assert local_config.exists(), "prompt callback never activated the candidate"
    assert candidate.read_text(encoding="utf-8") == "installed while waiting\n"
    assert not list((isolated_state_dir / "transitions").glob("*cleanup-orphans*"))


def test_apply_never_deletes_orphan_discovered_after_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The locked scan may contract approval, but cannot expand it."""
    approved = OrphanEntry(path=tmp_path / "approved")
    newly_discovered = OrphanEntry(path=tmp_path / "not-shown-to-user")
    detections = iter(
        [
            (_make_config_with({}), OrphanDetection(orphans=[approved])),
            (
                _make_config_with({}),
                OrphanDetection(orphans=[approved, newly_discovered]),
            ),
        ]
    )
    executed: list[OrphanEntry] = []

    monkeypatch.setattr(
        orphans_mod, "_detect_orphans_live", lambda *_a: next(detections)
    )
    monkeypatch.setattr(
        orphans_mod,
        "_pick_cleanup_branch",
        lambda *, yes: orphans_mod.ApplyChoice.DELETE_ONLY,
    )
    monkeypatch.setattr(
        orphans_mod,
        "_execute_cleanup_locked",
        lambda _profile, orphans, _choice, _console: executed.extend(orphans),
    )

    orphans_mod._apply_orphan_cleanup(
        "p", tmp_path / "setforge.yaml", yes=False, console=Console()
    )

    assert executed == [approved]


def test_apply_default_branch_uses_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--apply --yes` short-circuits to DELETE_AND_TRANSITION (safe default)."""
    assert (
        orphans_mod._pick_cleanup_branch(yes=True)
        is orphans_mod.ApplyChoice.DELETE_AND_TRANSITION
    )


def test_apply_non_tty_resolver_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct mutate-gate probe: non-TTY + no-yes → raise."""

    class _FakeStdin:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr("sys.stdin", _FakeStdin)
    with pytest.raises(OrphanCleanupRequiresInteractive):
        orphans_mod._pick_cleanup_branch(yes=False)


class _TtyStdin:
    @staticmethod
    def isatty() -> bool:
        return True


def test_apply_tty_button_bar_returns_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", _TtyStdin)
    monkeypatch.setattr(
        orphans_mod,
        "button_bar",
        lambda *_a, **_kw: orphans_mod.ApplyChoice.DELETE_ONLY,
    )
    assert (
        orphans_mod._pick_cleanup_branch(yes=False)
        is orphans_mod.ApplyChoice.DELETE_ONLY
    )


def test_apply_tty_button_bar_cancel_is_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    from setforge.ui.widgets import CANCEL

    monkeypatch.setattr("sys.stdin", _TtyStdin)
    monkeypatch.setattr(orphans_mod, "button_bar", lambda *_a, **_kw: CANCEL)
    assert orphans_mod._pick_cleanup_branch(yes=False) is orphans_mod.ApplyChoice.ABORT


# ---------------------------------------------------------------------------
# CLI: --ignore writes local.yaml only
# ---------------------------------------------------------------------------


def test_ignore_writes_local_yaml_not_tracked(
    runner: CliRunner, tmp_path: Path
) -> None:
    """`--ignore <id>` mutates ~/.config/setforge/local.yaml ONLY; the
    tracked setforge.yaml is untouched."""
    cfg = _write_minimal_yaml(tmp_path)
    cfg_bytes_before = cfg.read_bytes()

    result = runner.invoke(
        app,
        [
            "cleanup-orphans",
            "--profile",
            "p",
            "--config",
            str(cfg),
            "--ignore",
            "some_old_id",
        ],
    )
    assert result.exit_code == 0, result.output
    # Tracked setforge.yaml: untouched, byte-for-byte.
    assert cfg.read_bytes() == cfg_bytes_before
    # Host-local local.yaml (redirected by conftest's autouse fixture
    # to tmp_path/local.yaml) now contains the ignore entry.
    yaml = YAML(typ="safe")
    payload = yaml.load(compare_mod.LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert payload == {"orphan_ignore": ["some_old_id"]}


def test_ignore_is_idempotent(runner: CliRunner, tmp_path: Path) -> None:
    """Re-adding an existing id leaves the list shape unchanged."""
    cfg = _write_minimal_yaml(tmp_path)
    for _ in range(2):
        result = runner.invoke(
            app,
            [
                "cleanup-orphans",
                "--profile",
                "p",
                "--config",
                str(cfg),
                "--ignore",
                "id_a",
            ],
        )
        assert result.exit_code == 0, result.output
    yaml = YAML(typ="safe")
    payload = yaml.load(compare_mod.LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
    # ruamel rt-loaded list is a CommentedSeq under the hood; compare contents.
    assert payload == {"orphan_ignore": ["id_a"]}


# ---------------------------------------------------------------------------
# Symlink safety
# ---------------------------------------------------------------------------


def test_symlink_orphan_uses_lstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symlink orphan: unlink removes the link only, never the target."""
    target = tmp_path / "real_data" / "important.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("DO NOT DELETE\n", encoding="utf-8")

    link = tmp_path / "live" / "orphan_link"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)

    assert link.is_symlink()
    orphan_entry = OrphanEntry(path=link)

    orphans_mod._unlink_orphan_path(orphan_entry.path, Console())

    # The link is gone; the target survives.
    assert not link.exists()
    assert not link.is_symlink()
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "DO NOT DELETE\n"


def test_unlink_missing_path_warns_does_not_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A vanished orphan logs a warning, NOT a crash (no missing_ok=True
    swallow — we want the race surfaced explicitly)."""

    ghost = tmp_path / "vanished.txt"
    assert not ghost.exists()
    orphans_mod._unlink_orphan_path(ghost, Console())
    captured = capsys.readouterr()
    assert "vanished before delete" in captured.out


# ---------------------------------------------------------------------------
# Anti-pattern checks: source-code structural assertions
# ---------------------------------------------------------------------------


def _orphans_module_ast() -> ast.Module:
    """Parsed AST of :mod:`setforge.cli.orphans` for structural assertions."""
    src = Path(orphans_mod.__file__).read_text(encoding="utf-8")
    return ast.parse(src)


def test_no_unlink_missing_ok_in_orphans_module() -> None:
    """`unlink(missing_ok=True)` swallows the "user re-added the file"
    race; must not appear in any AST-level Call in the cleanup module.
    (Source-text grep would false-positive on the prohibition's own
    docstring.)"""
    tree = _orphans_module_ast()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        attr = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if attr != "unlink":
            continue
        for kw in node.keywords:
            if kw.arg == "missing_ok":
                raise AssertionError(
                    f"unlink(missing_ok=...) detected at line {node.lineno}"
                )


def test_no_shutil_rmtree_or_removedirs() -> None:
    """`shutil.rmtree` (recursive) and `os.removedirs` (walks up) are
    both forbidden — single-level Path.rmdir() only. AST-walk for
    matching attribute call shapes."""
    tree = _orphans_module_ast()
    forbidden = {"rmtree", "removedirs"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        attr = getattr(node.func, "attr", None)
        if attr in forbidden:
            raise AssertionError(
                f"forbidden recursive-delete call {attr!r} at line {node.lineno}"
            )


def test_no_resolve_in_orphan_unlink_helpers() -> None:
    """Calling `.resolve()` on a symlink before `.unlink()` torches the
    pointed-to file. None of the per-orphan helpers
    (`_unlink_orphan_path`, `_rmdir_empty_parents`, `_execute_cleanup_locked`,
    `_write_orphan_transition`, `_read_orphan_content`,
    `_lstat_safe`, `_orphan_path_identity`) may call `.resolve()`. The
    `_detect_orphans_live` helper is allowed to call
    `config_path.resolve()` for source-dir normalization (Typer config
    path, not an orphan path)."""
    tree = _orphans_module_ast()
    helper_names = {
        "_unlink_orphan_path",
        "_rmdir_empty_parents",
        "_execute_cleanup_locked",
        "_write_orphan_transition",
        "_read_orphan_content",
        "_lstat_safe",
        "_orphan_path_identity",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in helper_names:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and getattr(inner.func, "attr", None) == "resolve"
            ):
                raise AssertionError(
                    f".resolve() forbidden in {node.name} at line {inner.lineno}"
                )


def test_apply_path_calls_detect_orphans() -> None:
    """The `--apply` code path MUST re-compute orphans live (via
    `_detect_orphans_live`, which dispatches to `compare_profile`
    AND `detect_orphans`), NOT cache from a prior `compare` call.

    Mirrors the SPEC 2 robust acceptance command — the FIRST function
    whose name contains "apply" (case-insensitive) must transitively
    reach `detect_orphans`.
    """
    tree = _orphans_module_ast()
    apply_fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and "apply" in n.name.lower()
    )
    # Direct calls inside apply_fn.
    direct_calls = {
        getattr(c.func, "attr", None) or getattr(c.func, "id", None)
        for c in ast.walk(apply_fn)
        if isinstance(c, ast.Call)
    }
    # Transitive call set: include the bodies of any helper called by
    # apply_fn that is also defined in this module (e.g.
    # `_detect_orphans_live`).
    transitive_names = {n for n in direct_calls if isinstance(n, str)}
    transitive_calls: set[str] = set(transitive_names)
    helper_fns = {
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name in transitive_names
    }
    for helper in helper_fns:
        for c in ast.walk(helper):
            if isinstance(c, ast.Call):
                attr = getattr(c.func, "attr", None) or getattr(c.func, "id", None)
                if attr is not None:
                    transitive_calls.add(attr)
    assert "detect_orphans" in transitive_calls or "compare_profile" in transitive_calls


# ---------------------------------------------------------------------------
# Compare output includes Orphans block
# ---------------------------------------------------------------------------


def test_compare_renders_orphans_block(
    runner: CliRunner, tmp_path: Path, isolated_state_dir: Path
) -> None:
    """`setforge compare` surfaces orphans as a separate `Orphans (N):` block."""
    cfg = _write_minimal_yaml(tmp_path)
    live_orphan = tmp_path / "live" / "orphan.txt"
    live_orphan.parent.mkdir(parents=True, exist_ok=True)
    live_orphan.write_text("body\n", encoding="utf-8")
    _write_meta_record(
        isolated_state_dir / "transitions",
        "20260518T120000000000Z-install-p",
        [str(live_orphan)],
    )
    # Also create the kept tracked source so compare's missing-src guard
    # doesn't trip.
    (tmp_path / "kept.txt").write_text("k", encoding="utf-8")

    result = runner.invoke(app, ["compare", "--profile", "p", "--config", str(cfg)])
    assert result.exit_code == 0, result.stdout
    # Rich wraps fragments in ANSI escapes AND newlines for narrow
    # terminal width; strip both before substring assertions.
    plain = _strip_ansi_and_newlines(result.stdout)
    assert "Orphans (1):" in plain
    assert live_orphan.name in plain
