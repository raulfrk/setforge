"""Focused unit tests for bounded unrecorded managed-tree scanning."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest

from setforge import compare as compare_mod
from setforge import orphan_scan
from setforge.config import (
    BundleComponent,
    BundleSpec,
    Config,
    FileComponent,
    Profile,
    TrackedFile,
)
from setforge.errors import SetforgeError


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    tracked = repo / "tracked"
    tracked.mkdir(parents=True)
    config_path = repo / "setforge.yaml"
    config_path.write_text("schema_version: '6.0'\n", encoding="utf-8")
    return repo, config_path


def _config(repo: Path, live: Path) -> Config:
    (repo / "tracked" / "kept.txt").write_text("tracked\n", encoding="utf-8")
    return Config(
        tracked_files={
            "kept": TrackedFile(
                src=Path("kept.txt"), dst=str(live / "tool" / "kept.txt")
            )
        },
        profiles={"p": Profile(tracked_files=["kept"])},
    )


def _scan(
    config: Config,
    repo: Path,
    config_path: Path,
    transitions_dir: Path,
) -> orphan_scan.ScanResult:
    return orphan_scan.scan_unrecorded_managed_tree(
        config,
        repo,
        config_path=config_path,
        transitions_dir=transitions_dir,
    )


def test_scan_finds_only_unrecorded_leaf_under_bounded_root(tmp_path: Path) -> None:
    repo, config_path = _repo(tmp_path)
    live = Path.home() / ".managed"
    tool = live / "tool"
    tool.mkdir(parents=True)
    kept = tool / "kept.txt"
    kept.write_text("deployed\n", encoding="utf-8")
    stray = tool / "old.txt"
    stray.write_bytes(b"\x00old\xff")

    result = _scan(_config(repo, live), repo, config_path, tmp_path / "state")

    assert [entry.path for entry in result.entries] == [stray]
    assert result.entries[0].kind is orphan_scan.ScanEntryKind.REGULAR


def test_double_slash_destination_cannot_broaden_root_or_surface_control(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    live = Path.home() / ".double-slash-managed"
    tool = live / "tool"
    tool.mkdir(parents=True)
    kept = tool / "kept.txt"
    kept.write_text("deployed", encoding="utf-8")
    stray = tool / "stray.txt"
    stray.write_text("candidate", encoding="utf-8")
    control = repo / "control-stray.txt"
    control.write_text("never scan", encoding="utf-8")
    config = _config(repo, live)
    config.tracked_files["kept"].dst = f"/{kept}"
    transitions_dir = tmp_path / "state"

    inventory = orphan_scan._managed_inventory(
        config,
        repo,
        config_path=config_path,
        transitions_dir=transitions_dir,
    )
    result = _scan(config, repo, config_path, transitions_dir)

    assert inventory.roots == (live,)
    assert [entry.path for entry in result.entries] == [stray]
    assert control not in {entry.path for entry in result.entries}


def test_scan_excludes_transition_attributed_and_sibling_profile_paths(
    tmp_path: Path,
) -> None:
    repo, config_path = _repo(tmp_path)
    tracked = repo / "tracked"
    (tracked / "one.txt").write_text("one", encoding="utf-8")
    (tracked / "two.txt").write_text("two", encoding="utf-8")
    live = Path.home() / ".managed" / "tool"
    live.mkdir(parents=True)
    first = live / "one.txt"
    sibling = live / "two.txt"
    ledger = live / "old.txt"
    unknown = live / "unknown.txt"
    for path in (first, sibling, ledger, unknown):
        path.write_text(path.name, encoding="utf-8")
    transitions_dir = tmp_path / "state" / "transition"
    record = transitions_dir / "record"
    record.mkdir(parents=True)
    (record / "meta.json").write_text(
        json.dumps({"paths": [str(ledger)]}), encoding="utf-8"
    )
    config = Config(
        tracked_files={
            "one": TrackedFile(src=Path("one.txt"), dst=str(first)),
            "two": TrackedFile(src=Path("two.txt"), dst=str(sibling)),
        },
        profiles={
            "p": Profile(tracked_files=["one"]),
            "other": Profile(tracked_files=["two"]),
        },
    )

    result = _scan(config, repo, config_path, transitions_dir)

    assert [entry.path for entry in result.entries] == [unknown]


def test_scan_excludes_host_ignored_inactive_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, config_path = _repo(tmp_path)
    tracked = repo / "tracked"
    (tracked / "ignored.txt").write_text("ignored", encoding="utf-8")
    live = Path.home() / ".managed" / "tool"
    live.mkdir(parents=True)
    ignored = live / "ignored.txt"
    ignored.write_text("keep", encoding="utf-8")
    unknown = live / "unknown.txt"
    unknown.write_text("candidate", encoding="utf-8")
    local_config = tmp_path / "local.yaml"
    local_config.write_text("orphan_ignore: [ignored]\n", encoding="utf-8")
    monkeypatch.setattr(compare_mod, "LOCAL_CONFIG_PATH", local_config)
    config = _config(repo, Path.home() / ".managed")
    config.tracked_files["ignored"] = TrackedFile(
        src=Path("ignored.txt"), dst=str(ignored)
    )

    result = _scan(config, repo, config_path, tmp_path / "state")

    assert [entry.path for entry in result.entries] == [unknown]


def test_scan_surfaces_symlink_without_following_target(tmp_path: Path) -> None:
    repo, config_path = _repo(tmp_path)
    live = Path.home() / ".managed"
    tool = live / "tool"
    tool.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    secret = external / "secret.txt"
    secret.write_text("keep", encoding="utf-8")
    link = tool / "old-link"
    link.symlink_to(external, target_is_directory=True)

    result = _scan(_config(repo, live), repo, config_path, tmp_path / "state")

    assert [entry.path for entry in result.entries] == [link]
    assert result.entries[0].kind is orphan_scan.ScanEntryKind.SYMLINK
    assert result.entries[0].link_target == str(external)
    assert secret.read_text(encoding="utf-8") == "keep"


def test_scan_excludes_host_local_and_source_control_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, config_path = _repo(tmp_path)
    live = Path.home() / ".managed"
    tool = live / "tool"
    tool.mkdir(parents=True)
    local = tool / "local.yaml"
    local.write_text("host: local", encoding="utf-8")
    monkeypatch.setattr(compare_mod, "LOCAL_CONFIG_PATH", local)
    unknown = tool / "unknown"
    unknown.write_text("candidate", encoding="utf-8")

    result = _scan(_config(repo, live), repo, config_path, tmp_path / "state")

    assert [entry.path for entry in result.entries] == [unknown]


def test_scan_refuses_symlinked_managed_root(tmp_path: Path) -> None:
    repo, config_path = _repo(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    live = Path.home() / ".managed"
    live.symlink_to(external, target_is_directory=True)
    (external / "tool").mkdir()

    with pytest.raises(SetforgeError, match="refusing managed scan through"):
        _scan(_config(repo, live), repo, config_path, tmp_path / "state")


def test_scan_skips_fifo_and_never_returns_directories(tmp_path: Path) -> None:
    repo, config_path = _repo(tmp_path)
    live = Path.home() / ".managed"
    tool = live / "tool"
    nested = tool / "nested"
    nested.mkdir(parents=True)
    fifo = tool / "pipe"
    os.mkfifo(fifo)
    candidate = nested / "candidate"
    candidate.write_text("candidate", encoding="utf-8")

    result = _scan(_config(repo, live), repo, config_path, tmp_path / "state")

    assert [entry.path for entry in result.entries] == [candidate]
    assert result.skipped_unsupported == 1


def test_scan_counts_cross_device_directory_as_mount_skip(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed"
    child = root / "mounted"
    child.mkdir(parents=True)
    ancestry = orphan_scan._safe_root_ancestry(root)
    assert ancestry is not None
    real = child.lstat()

    class _MountStat:
        st_dev = real.st_dev + 1
        st_ino = real.st_ino
        st_mode = real.st_mode
        st_size = real.st_size
        st_mtime_ns = real.st_mtime_ns
        st_ctime_ns = real.st_ctime_ns

    class _MountEntry:
        path = str(child)

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            assert not follow_symlinks
            return cast(os.stat_result, _MountStat())

    outcome = orphan_scan._inspect_child(
        cast(os.DirEntry[str], _MountEntry()),
        root_device=ancestry[-1][1].device,
        parent_identities=ancestry,
        attributed=frozenset(),
        excluded_roots=(),
    )

    assert outcome is orphan_scan._WalkSkip.MOUNT


def test_scan_attributes_host_override_and_bundle_destination_across_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge import source

    repo, config_path = _repo(tmp_path)
    (repo / "tracked" / "bundle.txt").write_text("bundle", encoding="utf-8")
    base = Path.home() / ".managed" / "base"
    override = Path.home() / ".managed" / "override"
    bundle = Path.home() / ".managed" / "bundle"
    for directory in (base, override, bundle):
        directory.mkdir(parents=True)
    (override / "kept.txt").write_text("managed", encoding="utf-8")
    override_stray = override / "stray"
    override_stray.write_text("candidate", encoding="utf-8")
    (bundle / "kept.txt").write_text("managed", encoding="utf-8")
    bundle_stray = bundle / "stray"
    bundle_stray.write_text("candidate", encoding="utf-8")
    local = tmp_path / "local.yaml"
    local.write_text(
        f"tracked_files:\n  kept:\n    dst: {override / 'kept.txt'}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(source, "LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr(compare_mod, "LOCAL_CONFIG_PATH", local)
    config = Config(
        tracked_files={
            "kept": TrackedFile(src=Path("kept.txt"), dst=str(base / "kept.txt"))
        },
        bundles={
            "tools": BundleSpec(
                components=[
                    BundleComponent(
                        id="kept",
                        file=FileComponent(
                            src=Path("bundle.txt"), dst=str(bundle / "kept.txt")
                        ),
                    )
                ]
            )
        },
        profiles={
            "p": Profile(tracked_files=["kept"]),
            "other": Profile(bundles=["tools"]),
        },
    )

    result = _scan(config, repo, config_path, tmp_path / "state")

    assert [entry.path for entry in result.entries] == [bundle_stray, override_stray]


def test_descriptor_unlink_rejects_replaced_candidate(tmp_path: Path) -> None:
    repo, config_path = _repo(tmp_path)
    live = Path.home() / ".managed"
    tool = live / "tool"
    tool.mkdir(parents=True)
    candidate = tool / "candidate"
    candidate.write_text("approved", encoding="utf-8")
    result = _scan(_config(repo, live), repo, config_path, tmp_path / "state")
    approved = result.entries[0]
    candidate.unlink()
    candidate.write_text("replacement", encoding="utf-8")

    with pytest.raises(SetforgeError, match="scan candidate changed"):
        orphan_scan.unlink_approved_entry(approved)

    assert candidate.read_text(encoding="utf-8") == "replacement"


def test_descriptor_unlink_rejects_symlink_swapped_ancestor(tmp_path: Path) -> None:
    repo, config_path = _repo(tmp_path)
    live = Path.home() / ".managed"
    tool = live / "tool"
    tool.mkdir(parents=True)
    candidate = tool / "candidate"
    candidate.write_text("approved", encoding="utf-8")
    approved = _scan(
        _config(repo, live), repo, config_path, tmp_path / "state"
    ).entries[0]
    moved = live.with_name(".managed-moved")
    live.rename(moved)
    live.symlink_to(moved, target_is_directory=True)

    with pytest.raises(SetforgeError, match="scan candidate parent changed"):
        orphan_scan.unlink_approved_entry(approved)

    assert (moved / "tool" / "candidate").read_text(encoding="utf-8") == "approved"


def test_descriptor_unlink_removes_link_not_target(tmp_path: Path) -> None:
    repo, config_path = _repo(tmp_path)
    live = Path.home() / ".managed"
    tool = live / "tool"
    tool.mkdir(parents=True)
    target = tmp_path / "important"
    target.write_text("keep", encoding="utf-8")
    link = tool / "candidate"
    link.symlink_to(target)
    approved = _scan(
        _config(repo, live), repo, config_path, tmp_path / "state"
    ).entries[0]

    orphan_scan.unlink_approved_entry(approved)

    assert not link.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep"
