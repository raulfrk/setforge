from __future__ import annotations

import copy
import json
import stat
import subprocess
import uuid
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from setforge import (
    binaries,
    codex_lifecycle,
    compare,
    locking,
    operations,
    snapshots,
    transitions,
)
from setforge.cli import _install_helpers, app
from setforge.cli._helpers import ProfileContext
from setforge.compare import CompareReport, CompareStatus, FileCompare
from setforge.config import (
    Config,
    Profile,
    ResolvedProfile,
    TrackedFile,
    load_config,
    resolve_effective_profile,
)
from setforge.errors import RevertFailed, SetforgeError
from setforge.file_ownership import file_resource_id
from setforge.ownership import OwnershipStore, resolve_owner_common_dir
from setforge.provision.receipt import default_receipt_root
from setforge.reconcile import store as reconcile_store
from setforge.reconcile.types import FileId, file_id
from tests.conftest import redirect_local_config_path


def _mixed_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    order: tuple[str, ...],
    *,
    native: bool = False,
) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    local = tmp_path / "local.yaml"
    redirect_local_config_path(monkeypatch, local)
    scanner = tmp_path / "gitleaks"
    scanner.write_text("#!/bin/sh\nexit 0\n")
    scanner.chmod(0o755)
    monkeypatch.setenv("SETFORGE_GITLEAKS_BIN", str(scanner))
    repo = tmp_path / "repo"
    tracked = repo / "tracked"
    (tracked / "tree").mkdir(parents=True)
    (tracked / "tree/item").write_text("tree\n")
    for name in ("one", "two"):
        (tracked / name).write_text(f"{name}\n")
    codex: dict[str, object] | None = None
    selected: dict[str, list[str]] | None = None
    if native:
        (tracked / "model.toml").write_text('model = "fixture"\n')
        (tracked / "guide.md").write_text("instructions\n")
        (tracked / "skill").mkdir()
        (tracked / "skill/SKILL.md").write_text(
            "---\nname: smoke\ndescription: Fixture\n---\nSkill\n"
        )
        codex = {
            "config": {"model": {"source": "model.toml"}},
            "instructions": {"guide": {"source": "guide.md"}},
            "skills": {"smoke": {"source": "skill"}},
        }
        selected = {"config": ["model"], "instructions": ["guide"], "skills": ["smoke"]}
    config = repo / "setforge.yaml"
    YAML().dump(
        {
            "schema_version": "6.5",
            "minimum_version": "6.4",
            "tracked_files": {
                "one": {"src": "one", "dst": str(home / "live/one")},
                "two": {"src": "two", "dst": str(home / "live/two")},
                "tree": {
                    "src": "tree",
                    "dst": str(home / "live/tree"),
                    "tree": {},
                },
            },
            "codex": codex,
            "profiles": {"p": {"tracked_files": list(order), "codex": selected}},
        },
        config,
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    cfg = load_config(config)
    resolved = resolve_effective_profile(cfg, "p", repo).resolved
    targets = [
        OwnershipStore().root,
        operations.journals_root(),
        transitions.state_root(),
        snapshots.snapshots_root(),
        locking._user_global_locks_dir(),
        resolve_owner_common_dir(repo),
        local,
    ]
    targets.extend(
        compare.resolve_dst(cfg.tracked_files[name]) for name in resolved.tracked_files
    )
    targets.extend(
        codex_lifecycle.config_destinations(cfg, resolved, repo, profile="p")
    )
    assert all(path.resolve().is_relative_to(tmp_path.resolve()) for path in targets)
    assert binaries.resolve_binary("gitleaks") == scanner
    return config, home / "live"


def _set_first_phase(
    config: Path, path: Path, phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert path.resolve().is_relative_to(config.parent.parent.resolve())
    if phase == "bootstrap":
        document = YAML().load(config.read_text())
        document["profiles"]["p"]["bootstrap"] = [str(path)]
        YAML().dump(document, config)
    else:
        assert phase == "allowlist"
        install = import_module("setforge.cli.install")
        monkeypatch.setattr(
            install,
            "_plan_secret_findings",
            lambda *_args, **_kwargs: install.SecretPlan(
                hashes=("1" * 64,), allowlist_path=path
            ),
        )


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("existing_root", [False, True])
@pytest.mark.parametrize("phase", ["bootstrap", "allowlist"])
def test_early_install_effects_share_managed_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native: bool,
    existing_root: bool,
    phase: str,
) -> None:
    config, live = _mixed_config(
        tmp_path, monkeypatch, ("one", "tree", "two"), native=native
    )
    root = live.parent / ".codex" if native else live
    if existing_root:
        root.mkdir(mode=0o750)
    early_file = root / "host-local"
    _set_first_phase(config, early_file, phase, monkeypatch)
    args = [
        "install",
        "--profile=p",
        f"--config={config}",
        "--yes",
        "--no-fetch",
        "--no-git-check",
    ]
    runner = CliRunner()
    result = runner.invoke(app, args)

    assert result.exit_code == 0, (result.output, result.exception)
    assert (live / "one").read_bytes() == b"one\n"
    assert (live / "two").read_bytes() == b"two\n"
    assert (live / "tree/item").read_bytes() == b"tree\n"
    if phase == "bootstrap":
        assert early_file.read_bytes() == b""
    else:
        assert "1" * 64 in early_file.read_text()
    if existing_root:
        assert root.stat().st_mode & 0o777 == 0o750
    elif native:
        assert root.stat().st_mode & 0o777 == 0o700
    if native:
        assert (root / "config.toml").read_text() == 'model = "fixture"\n'
        assert (root / "AGENTS.md").read_bytes() == b"instructions\n"
        assert (root / "skills/smoke/SKILL.md").read_bytes() == (
            config.parent / "tracked/skill/SKILL.md"
        ).read_bytes()
    before = (live / "one").stat()
    repeated = runner.invoke(app, args)
    assert repeated.exit_code == 0, (repeated.output, repeated.exception)
    after = (live / "one").stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert operations.active("p") is None


@pytest.mark.parametrize("phase", ["bootstrap", "allowlist"])
@pytest.mark.parametrize("after_effect", [False, True])
def test_early_root_failure_restores_only_prepared_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, after_effect: bool
) -> None:
    config, live = _mixed_config(
        tmp_path, monkeypatch, ("tree", "one", "two"), native=True
    )
    native_root = live.parent / ".codex"
    early_file = native_root / "host-local"
    _set_first_phase(config, early_file, phase, monkeypatch)
    install = import_module("setforge.cli.install")
    owner = install.deploy if phase == "bootstrap" else install
    attribute = "bootstrap_local" if phase == "bootstrap" else "_apply_secret_plan"
    effect = getattr(owner, attribute)

    def fail(*args: object, **kwargs: object) -> None:
        assert native_root.is_dir()
        assert live.is_dir()
        if after_effect:
            effect(*args, **kwargs)
            assert early_file.exists()
        raise OSError("injected first-phase failure")

    monkeypatch.setattr(owner, attribute, fail)
    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--yes",
            "--no-fetch",
            "--no-git-check",
        ],
    )

    assert result.exit_code != 0
    assert "injected first-phase failure" in str(result.exception)
    assert not native_root.exists()
    assert not live.exists()
    assert not OwnershipStore().list_claims()
    assert operations.active("p") is None


def test_bootstrap_does_not_create_unrelated_blocked_file_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.provision.protocol import Outcome, ProvisionOutcome

    config, live = _mixed_config(
        tmp_path, monkeypatch, ("tree", "one", "two"), native=True
    )
    _set_first_phase(config, live / "host-local", "bootstrap", monkeypatch)
    document = YAML().load(config.read_text())
    document["bundles"] = {
        "app": {
            "components": [
                {"id": "tool", "cargo": {"crate": "fixture-prerequisite"}},
                {
                    "id": "launcher",
                    "depends_on": ["tool"],
                    "file": {"src": "one", "dst": str(live / "launcher")},
                },
            ]
        }
    }
    document["profiles"]["p"]["bundles"] = ["app"]
    YAML().dump(document, config)
    monkeypatch.setattr(
        "setforge.provision.cargo.CargoProvisioner.probe", lambda _: set()
    )
    monkeypatch.setattr(
        "setforge.provision.cargo.CargoProvisioner.apply_one",
        lambda _self, item: ProvisionOutcome(
            item=item, outcome=Outcome.SOFT, detail="fixture missing toolchain"
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--yes",
            "--no-fetch",
            "--no-git-check",
        ],
    )

    assert result.exit_code == 0, (result.output, result.exception)
    assert "file=blocked" in result.output
    assert {path.name for path in live.iterdir()} == {"host-local"}
    assert (live / "host-local").read_bytes() == b""
    assert not (live.parent / ".codex").exists()
    assert not OwnershipStore().list_claims()
    assert operations.active("p") is None


def test_local_package_and_managed_tree_share_new_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live = _mixed_config(tmp_path, monkeypatch, ("tree", "one", "two"))
    source = config.parent / "tracked/tool"
    source.write_bytes(b"#!/bin/sh\necho fixture\n")
    binary = live / "bin/audit-tool"
    document = YAML().load(config.read_text())
    document["packages"] = {
        "tool": {
            "type": "local",
            "path": "tool",
            "binary": "audit-tool",
            "install": str(binary.parent),
            "extract": False,
        }
    }
    document["profiles"]["p"]["packages"] = ["tool"]
    YAML().dump(document, config)
    assert all(
        path.resolve().is_relative_to(tmp_path.resolve())
        for path in (source, binary, default_receipt_root())
    )
    args = [
        "--source",
        str(config.parent),
        "install",
        "--profile=p",
        f"--config={config}",
        "--yes",
        "--no-fetch",
        "--no-git-check",
    ]
    runner = CliRunner()

    first = runner.invoke(app, args)
    assert first.exit_code == 0, (first.output, first.exception)
    assert binary.read_bytes() == b"#!/bin/sh\necho fixture\n"
    assert binary.stat().st_mode & 0o111
    assert (live / "tree/item").read_bytes() == b"tree\n"
    assert tuple(default_receipt_root().glob("*.json"))
    assert {claim.resource_id.kind for claim in OwnershipStore().list_claims()} >= {
        "package",
        "file",
    }
    assert operations.active("p") is None

    before = (binary.stat().st_ino, (live / "tree/item").stat().st_ino)
    repeated = runner.invoke(app, args)
    assert repeated.exit_code == 0, (repeated.output, repeated.exception)
    assert (binary.stat().st_ino, (live / "tree/item").stat().st_ino) == before
    assert operations.active("p") is None


@pytest.mark.parametrize("native", [False, True])
def test_install_recovery_preserves_file_under_replaced_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool
) -> None:
    config, live = _mixed_config(
        tmp_path, monkeypatch, ("one", "tree", "two"), native=native
    )
    selected = live.parent / ".codex" if native else live
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / ("AGENTS.md" if native else "one")
    sentinel.write_bytes(b"foreign bytes\n")
    inode = sentinel.stat().st_ino
    assert selected.resolve().is_relative_to(tmp_path.resolve())
    assert sentinel.resolve().is_relative_to(tmp_path.resolve())

    def replace_before_flat(*_args: object, **_kwargs: object) -> None:
        assert selected.is_dir()
        selected.rename(tmp_path / "moved-managed-root")
        selected.symlink_to(outside, target_is_directory=True)
        raise OSError("injected before flat writes")

    monkeypatch.setattr(
        _install_helpers, "_apply_tracked_file_plan", replace_before_flat
    )
    runner = CliRunner()
    applied = runner.invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--yes",
            "--no-fetch",
            "--no-git-check",
        ],
    )
    assert applied.exit_code != 0
    assert "injected before flat writes" in str(applied.exception)
    assert sentinel.read_bytes() == b"foreign bytes\n"
    assert sentinel.stat().st_ino == inode
    assert selected.is_symlink()
    assert operations.active("p") is not None

    refused = runner.invoke(app, ["recover", "--profile=p", "--apply", "--yes"])
    assert refused.exit_code != 0
    assert sentinel.read_bytes() == b"foreign bytes\n"
    assert sentinel.stat().st_ino == inode
    assert selected.is_symlink()
    assert operations.active("p") is not None

    selected.unlink()
    (tmp_path / "moved-managed-root").rename(selected)
    restored = runner.invoke(app, ["recover", "--profile=p", "--apply", "--yes"])
    assert restored.exit_code == 0, (restored.output, restored.exception)
    assert not selected.exists()
    assert sentinel.read_bytes() == b"foreign bytes\n"
    assert sentinel.stat().st_ino == inode
    assert operations.active("p") is None


def test_install_recovery_preserves_file_under_replaced_nested_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live = _mixed_config(tmp_path, monkeypatch, ("tree",))
    tracked = config.parent / "tracked/tree/folder"
    tracked.mkdir()
    (tracked / "item").write_bytes(b"nested managed bytes\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "item"
    sentinel.write_bytes(b"foreign nested bytes\n")
    inode = sentinel.stat().st_ino
    folder = live / "tree/folder"
    assert all(
        path.resolve().is_relative_to(tmp_path.resolve())
        for path in (tracked, folder, sentinel)
    )
    install = import_module("setforge.cli.install")
    original = install.apply_tree

    def replace_nested(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)
        assert (folder / "item").read_bytes() == b"nested managed bytes\n"
        folder.rename(tmp_path / "moved-folder")
        folder.symlink_to(outside, target_is_directory=True)
        raise OSError("injected after nested tree effect")

    monkeypatch.setattr(install, "apply_tree", replace_nested)
    runner = CliRunner()
    applied = runner.invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--yes",
            "--no-fetch",
            "--no-git-check",
        ],
    )
    assert applied.exit_code != 0
    assert "injected after nested tree effect" in str(applied.exception)
    assert folder.is_symlink()
    assert sentinel.read_bytes() == b"foreign nested bytes\n"
    assert sentinel.stat().st_ino == inode
    assert operations.active("p") is not None

    refused = runner.invoke(app, ["recover", "--profile=p", "--apply", "--yes"])
    assert refused.exit_code != 0
    assert folder.is_symlink()
    assert sentinel.read_bytes() == b"foreign nested bytes\n"
    assert sentinel.stat().st_ino == inode
    assert operations.active("p") is not None

    folder.unlink()
    (tmp_path / "moved-folder").rename(folder)
    restored = runner.invoke(app, ["recover", "--profile=p", "--apply", "--yes"])
    assert restored.exit_code == 0, (restored.output, restored.exception)
    assert not live.exists()
    assert sentinel.read_bytes() == b"foreign nested bytes\n"
    assert sentinel.stat().st_ino == inode
    assert operations.active("p") is None


@pytest.mark.parametrize(
    "order", [("tree", "one", "two"), ("one", "tree", "two"), ("one", "two", "tree")]
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_mixed_file_tree_inventory_accepts_profile_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    order: tuple[str, ...],
    dry_run: bool,
) -> None:
    config, live = _mixed_config(tmp_path, monkeypatch, order)
    args = [
        "install",
        "--profile=p",
        f"--config={config}",
        "--yes",
        "--no-fetch",
        "--no-git-check",
    ]
    if dry_run:
        args.append("--dry-run")

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, (result.output, result.exception)
    if dry_run:
        assert not live.exists()
    else:
        assert (live / "one").read_bytes() == b"one\n"
        assert (live / "two").read_bytes() == b"two\n"
        assert (live / "tree/item").read_bytes() == b"tree\n"


def test_unchanged_install_with_managed_tree_records_no_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live = _mixed_config(tmp_path, monkeypatch, ("tree", "one", "two"))
    args = [
        "install",
        "--profile=p",
        f"--config={config}",
        "--yes",
        "--no-fetch",
        "--no-git-check",
    ]
    runner = CliRunner()
    initial = runner.invoke(app, args)
    assert initial.exit_code == 0, (initial.output, initial.exception)
    (config.parent / "tracked/one").write_text("one updated\n")
    updated = runner.invoke(app, args)
    assert updated.exit_code == 0, (updated.output, updated.exception)
    recorded = transitions.load_latest("p")
    assert recorded is not None

    repeated = runner.invoke(app, args)

    assert repeated.exit_code == 0, (repeated.output, repeated.exception)
    assert "transition:" not in repeated.output
    assert transitions.load_latest("p") == recorded
    reverted = runner.invoke(
        app, ["revert", "--profile=p", f"--config={config}", "--yes"]
    )
    assert reverted.exit_code == 0, (reverted.output, reverted.exception)
    assert (live / "one").read_bytes() == b"one\n"
    assert (live / "tree/item").read_bytes() == b"tree\n"


def _install_then_hold_tree_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], Path, Path]:
    config, live = _mixed_config(tmp_path, monkeypatch, ("tree", "one", "two"))
    document = YAML().load(config.read_text())
    document["tracked_files"]["tree"]["tree"] = {"orphans": "remove-owned"}
    YAML().dump(document, config)
    tracked = config.parent / "tracked"
    (tracked / "tree/kept").write_text("kept\n")
    args = [
        "install",
        "--profile=p",
        f"--config={config}",
        "--yes",
        "--no-fetch",
        "--no-git-check",
    ]
    initial = CliRunner().invoke(app, args)
    assert initial.exit_code == 0, (initial.output, initial.exception)
    (tracked / "tree/item").unlink()
    (live / "tree/item").write_text("edited live\n")
    (tracked / "one").write_text("one updated\n")
    return args, config, live


def test_held_tree_entry_error_names_path_reason_and_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _config, live = _install_then_hold_tree_entry(tmp_path, monkeypatch)

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 1
    message = str(result.exception)
    assert isinstance(result.exception, SetforgeError)
    assert f"tree: {live / 'tree/item'}" in message
    assert "changed live since the last install" in message
    assert "--auto=keep-live" in message
    assert "--auto=use-tracked" in message
    assert (live / "tree/item").read_bytes() == b"edited live\n"
    assert (live / "one").read_bytes() == b"one\n"


def test_auto_keep_live_leaves_held_tree_entry_and_installs_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _config, live = _install_then_hold_tree_entry(tmp_path, monkeypatch)
    runner = CliRunner()

    kept = runner.invoke(app, [*args, "--auto=keep-live"])
    assert kept.exit_code == 0, (kept.output, kept.exception)
    assert (live / "tree/item").read_bytes() == b"edited live\n"
    assert (live / "one").read_bytes() == b"one updated\n"

    repeated = runner.invoke(app, args)
    assert repeated.exit_code == 0, (repeated.output, repeated.exception)
    assert (live / "tree/item").read_bytes() == b"edited live\n"
    assert operations.active("p") is None


def test_auto_keep_live_leaves_file_where_tracked_has_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, config, live = _install_then_hold_tree_entry(tmp_path, monkeypatch)
    tracked = config.parent / "tracked"
    (tracked / "tree/item").mkdir()
    (tracked / "tree/item/child").write_text("child\n")
    runner = CliRunner()

    refused = runner.invoke(app, [*args, "--auto=use-tracked"])
    assert refused.exit_code == 1
    assert "tracked directory conflicts with live file" in str(refused.exception)
    assert (live / "one").read_bytes() == b"one\n"

    kept = runner.invoke(app, [*args, "--auto=keep-live"])
    assert kept.exit_code == 0, (kept.output, kept.exception)
    assert (live / "tree/item").read_bytes() == b"edited live\n"
    assert (live / "one").read_bytes() == b"one updated\n"
    assert operations.active("p") is None


def test_auto_use_tracked_applies_held_tree_entry_reversibly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, config, live = _install_then_hold_tree_entry(tmp_path, monkeypatch)
    runner = CliRunner()

    applied = runner.invoke(app, [*args, "--auto=use-tracked"])
    assert applied.exit_code == 0, (applied.output, applied.exception)
    assert not (live / "tree/item").exists()
    assert (live / "tree/kept").read_bytes() == b"kept\n"
    assert (live / "one").read_bytes() == b"one updated\n"

    reverted = runner.invoke(
        app, ["revert", "--profile=p", f"--config={config}", "--yes"]
    )
    assert reverted.exit_code == 0, (reverted.output, reverted.exception)
    assert (live / "tree/item").read_bytes() == b"edited live\n"
    assert operations.active("p") is None


def test_first_install_creates_tree_beside_absent_state_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live = _mixed_config(tmp_path, monkeypatch, ("tree", "one", "two"))
    shared_parent = live.parent / ".local"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(shared_parent / "state/setforge"))
    destination = shared_parent / "share/tree"
    document = YAML().load(config.read_text())
    document["tracked_files"]["tree"]["dst"] = str(destination)
    YAML().dump(document, config)
    assert not shared_parent.exists()

    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--yes",
            "--no-fetch",
            "--no-git-check",
        ],
    )

    assert result.exit_code == 0, (result.output, result.exception)
    assert (destination / "item").read_bytes() == b"tree\n"
    assert (live / "one").read_bytes() == b"one\n"
    assert operations.active("p") is None


def test_managed_tree_lifecycle_when_filesystem_rejects_rename_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rename_flags_rejected: list[int]
) -> None:
    config, live = _mixed_config(tmp_path, monkeypatch, ("tree", "one", "two"))
    document = YAML().load(config.read_text())
    document["tracked_files"]["tree"]["tree"] = {"orphans": "remove-owned"}
    YAML().dump(document, config)
    source = config.parent / "tracked/tree"
    (source / "sub").mkdir()
    (source / "sub/extra").write_text("extra\n")
    args = [
        "install",
        "--profile=p",
        f"--config={config}",
        "--yes",
        "--no-fetch",
        "--no-git-check",
    ]
    runner = CliRunner()

    created = runner.invoke(app, args)
    assert created.exit_code == 0, (created.output, created.exception)
    assert (live / "tree/item").read_bytes() == b"tree\n"
    assert (live / "tree/sub/extra").read_bytes() == b"extra\n"

    (source / "item").write_text("updated\n")
    (source / "sub/extra").unlink()
    (source / "sub").rmdir()
    changed = runner.invoke(app, args)
    assert changed.exit_code == 0, (changed.output, changed.exception)
    assert (live / "tree/item").read_bytes() == b"updated\n"
    assert not (live / "tree/sub").exists()

    reverted = runner.invoke(
        app, ["revert", "--profile=p", f"--config={config}", "--yes"]
    )
    assert reverted.exit_code == 0, (reverted.output, reverted.exception)
    assert (live / "tree/item").read_bytes() == b"tree\n"
    assert (live / "tree/sub/extra").read_bytes() == b"extra\n"
    assert rename_flags_rejected
    assert sorted(path.name for path in (live / "tree").iterdir()) == ["item", "sub"]
    assert operations.active("p") is None


@pytest.mark.parametrize("duplicate", [False, True], ids=["missing", "duplicate"])
def test_mixed_inventory_still_rejects_changed_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, duplicate: bool
) -> None:
    config, live = _mixed_config(tmp_path, monkeypatch, ("one", "two", "tree"))
    original = compare.compare_profile

    def changed(*args: object, **kwargs: object) -> CompareReport:
        report = original(*args, **kwargs)  # type: ignore[arg-type]
        if duplicate:
            report.entries.append(report.entries[0])
        else:
            report.entries.pop()
        return report

    monkeypatch.setattr(compare, "compare_profile", changed)
    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--dry-run",
            "--no-fetch",
            "--no-git-check",
        ],
    )

    assert result.exit_code != 0
    assert "inventory changed during planning" in str(result.exception)
    assert not live.exists()


def test_native_resources_share_a_new_locked_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live = _mixed_config(
        tmp_path, monkeypatch, ("one", "tree", "two"), native=True
    )
    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--yes",
            "--no-fetch",
            "--no-git-check",
        ],
    )

    assert result.exit_code == 0, (result.output, result.exception)
    native = live.parent / ".codex"
    assert native.stat().st_mode & 0o777 == 0o700
    assert (native / "config.toml").read_text() == 'model = "fixture"\n'
    assert (native / "AGENTS.md").read_text() == "instructions\n"
    assert (native / "skills/smoke/SKILL.md").read_bytes() == (
        config.parent / "tracked/skill/SKILL.md"
    ).read_bytes()
    assert (live / "tree/item").read_bytes() == b"tree\n"


@pytest.mark.parametrize("native", [False, True])
def test_mixed_root_preparation_rolls_back_before_flat_file_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool
) -> None:
    config, live = _mixed_config(
        tmp_path, monkeypatch, ("one", "tree", "two"), native=native
    )

    def fail(*args: object, **kwargs: object) -> None:
        assert live.is_dir()
        if native:
            assert (live.parent / ".codex").is_dir()
        raise OSError("injected before flat effects")

    monkeypatch.setattr(_install_helpers, "_apply_tracked_file_plan", fail)
    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--yes",
            "--no-fetch",
            "--no-git-check",
        ],
    )

    assert result.exit_code != 0
    assert "injected before flat effects" in str(result.exception)
    assert not live.exists()
    assert not (live.parent / ".codex").exists()
    assert not OwnershipStore().list_claims()
    assert operations.active("p") is None


def test_mixed_install_still_refuses_replaced_parent_before_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live = _mixed_config(tmp_path, monkeypatch, ("one", "two", "tree"))
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "one"
    sentinel.write_bytes(b"foreign\n")
    inode = sentinel.stat().st_ino
    install = import_module("setforge.cli.install")
    original = install._assert_plan_inputs_unchanged

    def replace_parent(plan: object) -> None:
        live.symlink_to(outside, target_is_directory=True)
        assert live.resolve().is_relative_to(tmp_path.resolve())
        original(plan)

    monkeypatch.setattr(install, "_assert_plan_inputs_unchanged", replace_parent)
    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--yes",
            "--no-fetch",
            "--no-git-check",
        ],
    )

    assert result.exit_code != 0
    assert sentinel.read_bytes() == b"foreign\n"
    assert sentinel.stat().st_ino == inode
    assert operations.active("p") is None


def test_dry_run_refuses_mismatched_regular_deploy_plan(tmp_path: Path) -> None:
    source = tmp_path / "tracked" / "note.md"
    source.parent.mkdir()
    source.write_text("tracked\n", encoding="utf-8")
    tracked = TrackedFile(src=Path("note.md"), dst=str(tmp_path / "live.md"))
    cfg = Config(
        tracked_files={"note": tracked},
        profiles={"p": Profile(tracked_files=["note"])},
    )
    ctx = ProfileContext(
        cfg=cfg,
        resolved=ResolvedProfile(tracked_files=["note"]),
        repo_root=tmp_path,
        profile="p",
    )
    report = CompareReport(
        entries=[FileCompare("note", CompareStatus.MISSING, "")],
        has_unexpected_drift=False,
    )

    with pytest.raises(SetforgeError, match="immutable deploy plan does not match"):
        _install_helpers._dry_run_pipeline(
            ctx=ctx,
            drift_report=report,
            deploys=(),
        )


def test_managed_tree_dry_run_renders_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    source = repo / "tracked" / "tools"
    source.mkdir(parents=True)
    (source / "tool.txt").write_text("tracked\n", encoding="utf-8")
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.5'\n"
        "minimum_version: '6.5'\n"
        "tracked_files:\n"
        "  tools:\n"
        "    src: tools\n"
        "    dst: ~/.tools\n"
        "    tree: {}\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [tools]\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--dry-run",
            "--no-fetch",
            "--no-git-check",
            "--no-secrets-scan",
            "--no-transition",
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"WOULD install   {home / '.tools'}" in result.output
    assert result.output.rstrip().endswith(
        "=== rerun without --dry-run to apply for real ==="
    )


@pytest.mark.parametrize("fail_after_write", [False, True])
def test_selected_file_preserves_other_resources_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_after_write: bool
) -> None:
    config, live = _mixed_config(
        tmp_path, monkeypatch, ("one", "tree", "two"), native=True
    )
    tracked = config.parent / "tracked"
    initial = b"heading\nRTK instructions\nfooter\n"
    (tracked / "one").write_bytes(initial)
    args = [
        "install",
        "--profile=p",
        f"--config={config}",
        "--yes",
        "--no-fetch",
        "--no-git-check",
    ]
    runner = CliRunner()
    installed = runner.invoke(app, args)
    assert installed.exit_code == 0, (installed.output, installed.exception)
    (live / "one").write_bytes(initial + b"host-only line\n")
    (tracked / "one").write_bytes(b"heading\nfooter\n")
    (tracked / "two").write_bytes(b"do not deploy\n")
    (tracked / "model.toml").write_text('model = "do-not-deploy"\n')
    document = YAML().load(config.read_text())
    document["packages"] = {"unrelated": {"type": "cargo", "crate": "unrelated"}}
    document["mcp_servers"] = {"unrelated": {"command": ["never-invoke"]}}
    document["profiles"]["p"]["packages"] = ["unrelated"]
    document["profiles"]["p"]["mcp_servers"] = ["unrelated"]
    YAML().dump(document, config)
    install = import_module("setforge.cli.install")

    def unexpected_adapter(*_args: object, **_kwargs: object) -> None:
        pytest.fail("file-only installation reached an unselected adapter")

    monkeypatch.setattr(install, "_plan_owned_provisioning", unexpected_adapter)
    monkeypatch.setattr(install, "plan_mcp_servers", unexpected_adapter)
    monkeypatch.setattr(
        install.codex_resources_mod, "plan_config_resources", unexpected_adapter
    )
    monkeypatch.setattr(
        install.codex_resources_mod, "config_target_roots", unexpected_adapter
    )
    controls = [
        live / "two",
        live / "tree/item",
        live.parent / ".codex/config.toml",
        live.parent / ".codex/AGENTS.md",
    ]
    before_controls = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_ino)
        for path in controls
    }
    ownership = OwnershipStore()
    selected_resource = file_resource_id(live / "one")
    before_claims = {claim.resource_id: claim for claim in ownership.list_claims()}
    before_index = copy.deepcopy(reconcile_store.read_index("p").files)
    before_store = {
        fid: (
            reconcile_store.read_base("p", file_id(fid)),
            reconcile_store.read_local("p", file_id(fid)),
        )
        for fid in before_index
    }
    selected_args = [*args, "--file=one", "--file=one", "--locked"]
    preview = runner.invoke(app, [*selected_args, "--dry-run"])
    assert preview.exit_code == 0, (preview.output, preview.exception)
    assert (live / "one").read_bytes() == initial + b"host-only line\n"
    injected: list[bytes] = []
    if fail_after_write:
        original_apply = _install_helpers._apply_tracked_file_plan

        def fail(
            profile: str,
            pending: tuple[_install_helpers._PendingDeploy, ...],
            *,
            preserved_ids: frozenset[FileId] = frozenset(),
        ) -> _install_helpers.DeployOutcome:
            original_apply(profile, pending, preserved_ids=preserved_ids)
            content = (live / "one").read_bytes()
            assert content == b"heading\nfooter\nhost-only line\n"
            injected.append(content)
            raise SetforgeError("injected after selected file write")

        monkeypatch.setattr(_install_helpers, "_apply_tracked_file_plan", fail)
    result = runner.invoke(app, selected_args)
    if fail_after_write:
        assert injected == [b"heading\nfooter\nhost-only line\n"]
        assert str(result.exception) == "injected after selected file write"
        assert (live / "one").read_bytes() == initial + b"host-only line\n"
        assert ownership.read(selected_resource) == before_claims[selected_resource]
        assert reconcile_store.read_index("p").files == before_index
        assert operations.active("p") is None
    else:
        assert result.exit_code == 0, (result.output, result.exception)
        assert (live / "one").read_bytes() == b"heading\nfooter\nhost-only line\n"
        repeated = runner.invoke(app, selected_args)
        assert repeated.exit_code == 0, (repeated.output, repeated.exception)
        reverted = runner.invoke(
            app, ["revert", "--profile=p", f"--config={config}", "--yes"]
        )
        assert reverted.exit_code == 0, (reverted.output, reverted.exception)
        assert (live / "one").read_bytes() == initial + b"host-only line\n"
    after_index = reconcile_store.read_index("p").files
    assert {key: row for key, row in after_index.items() if key != "one"} == {
        key: row for key, row in before_index.items() if key != "one"
    }
    for fid, expected in before_store.items():
        if fail_after_write or fid != "one":
            assert (
                reconcile_store.read_base("p", file_id(fid)),
                reconcile_store.read_local("p", file_id(fid)),
            ) == expected
    assert {
        claim.resource_id: claim
        for claim in ownership.list_claims()
        if claim.resource_id != selected_resource
    } == {
        resource: claim
        for resource, claim in before_claims.items()
        if resource != selected_resource
    }
    assert before_controls == {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_ino)
        for path in controls
    }


@pytest.mark.parametrize(
    "case", ["unknown", "nonmember", "unowned", "foreign", "released", "retry"]
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_file_selection_refuses_without_resource_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, dry_run: bool
) -> None:
    config, live = _mixed_config(tmp_path, monkeypatch, ("one", "tree", "two"))
    yaml = YAML()
    if case == "unowned":
        document = yaml.load(config.read_text())
        document["profiles"]["p"]["tracked_files"] = ["tree", "two"]
        yaml.dump(document, config)
    runner = CliRunner()
    args = [
        "install",
        "--profile=p",
        f"--config={config}",
        "--yes",
        "--no-fetch",
        "--no-git-check",
    ]
    installed = runner.invoke(app, args)
    assert installed.exit_code == 0, (installed.output, installed.exception)
    ownership = OwnershipStore()
    resource = file_resource_id(live / "one")
    if case in {"foreign", "released"}:
        claim = ownership.read(resource)
        assert claim is not None
        with locking.mutation_locks(resources=True):
            if case == "foreign":
                ownership.transfer_locked(
                    resource,
                    expected_owner=claim.owner_id,
                    new_owner=uuid.uuid4(),
                    expected_generation=claim.generation,
                    declaration_refs=claim.declaration_refs,
                )
            else:
                ownership.release_locked(
                    resource,
                    expected_owner=claim.owner_id,
                    expected_generation=claim.generation,
                )
    document = yaml.load(config.read_text())
    if case == "nonmember":
        document["profiles"]["p"]["tracked_files"] = ["tree", "two"]
    elif case == "unowned":
        document["profiles"]["p"]["tracked_files"] = ["one", "tree", "two"]
        (live / "one").write_bytes(b"unowned content\n")
    yaml.dump(document, config)
    (config.parent / "tracked/one").write_bytes(b"new source\n")
    before_paths = {
        path: path.read_bytes() for path in live.rglob("*") if path.is_file()
    }
    before_claims = ownership.list_claims()
    before_index = copy.deepcopy(reconcile_store.read_index("p"))
    selected = "missing" if case == "unknown" else "one"
    extra = [f"--file={selected}"]
    if case == "retry":
        extra.append("--retry-failed")
    if dry_run:
        extra.append("--dry-run")
    result = runner.invoke(app, [*args, *extra])

    assert result.exit_code == 1, (result.output, result.exception)
    assert isinstance(result.exception, SetforgeError)
    assert {
        path: path.read_bytes() for path in live.rglob("*") if path.is_file()
    } == before_paths
    assert ownership.list_claims() == before_claims
    assert reconcile_store.read_index("p") == before_index
    assert operations.active("p") is None


@pytest.mark.parametrize("dry_run", [False, True])
def test_unmanaged_tree_selection_refuses_before_its_held_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    config, live = _mixed_config(tmp_path, monkeypatch, ("one", "tree", "two"))
    runner = CliRunner()
    args = [
        "install",
        "--profile=p",
        f"--config={config}",
        "--yes",
        "--no-fetch",
        "--no-git-check",
    ]
    installed = runner.invoke(app, args)
    assert installed.exit_code == 0, (installed.output, installed.exception)
    ownership = OwnershipStore()
    claim = ownership.read(file_resource_id(live / "tree"))
    assert claim is not None
    with locking.mutation_locks(resources=True):
        ownership.release_locked(
            claim.resource_id,
            expected_owner=claim.owner_id,
            expected_generation=claim.generation,
        )
    (live / "tree/item").unlink()
    (live / "tree/item").mkdir()
    mode = ["--dry-run"] if dry_run else []

    whole_profile = runner.invoke(app, [*args, "--dry-run"])
    selected = runner.invoke(app, [*args, "--file=tree", *mode])

    assert "managed tree conflicts require review" in str(whole_profile.exception)
    assert selected.exit_code == 1, (selected.output, selected.exception)
    assert "file-only install requires already-managed files" in str(selected.exception)
    assert (live / "tree/item").is_dir()


def test_selected_directory_does_not_prune_named_sibling_or_retired_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live = _mixed_config(tmp_path, monkeypatch, ("one", "tree", "two"))
    tracked = config.parent / "tracked"
    (tracked / "one").unlink()
    (tracked / "one").mkdir()
    (tracked / "one/current").write_bytes(b"current baseline\n")
    (tracked / "one/retired").write_bytes(b"retained baseline\n")
    yaml = YAML()
    document = yaml.load(config.read_text())
    document["tracked_files"]["one"]["dst"] = str(live / "directory")
    document["tracked_files"]["one/sibling"] = {
        "src": "two",
        "dst": str(live / "named-sibling"),
    }
    document["profiles"]["p"]["tracked_files"].append("one/sibling")
    yaml.dump(document, config)
    args = [
        "install",
        "--profile=p",
        f"--config={config}",
        "--yes",
        "--no-fetch",
        "--no-git-check",
    ]
    runner = CliRunner()
    initial = runner.invoke(app, args)
    assert initial.exit_code == 0, (initial.output, initial.exception)
    before = copy.deepcopy(reconcile_store.read_index("p").files)
    assert {"one/sibling", "one/retired"}.issubset(before)
    controls = {
        fid: (
            reconcile_store.read_base("p", file_id(fid)),
            reconcile_store.read_local("p", file_id(fid)),
        )
        for fid in ("one/sibling", "one/retired")
    }
    (tracked / "one/current").write_bytes(b"selected update\n")
    (tracked / "one/retired").unlink()
    (tracked / "two").write_bytes(b"unselected source update\n")
    selected = runner.invoke(app, [*args, "--file=one"])

    assert selected.exit_code == 0, (selected.output, selected.exception)
    assert (live / "directory/current").read_bytes() == b"selected update\n"
    assert (live / "named-sibling").read_bytes() == b"two\n"
    assert (live / "directory/retired").read_bytes() == b"retained baseline\n"
    after = reconcile_store.read_index("p").files
    for fid, expected in controls.items():
        assert after[fid] == before[fid]
        assert (
            reconcile_store.read_base("p", file_id(fid)),
            reconcile_store.read_local("p", file_id(fid)),
        ) == expected


def test_file_only_store_scope_rejects_overlapping_declarations(tmp_path: Path) -> None:
    cfg = Config(
        tracked_files={
            "directory": TrackedFile(
                src=Path("directory"), dst=str(tmp_path / "directory")
            ),
            "directory/item": TrackedFile(
                src=Path("other"), dst=str(tmp_path / "other")
            ),
        },
        profiles={"p": Profile(tracked_files=["directory", "directory/item"])},
    )
    ctx = ProfileContext(
        cfg=cfg,
        resolved=ResolvedProfile(tracked_files=["directory", "directory/item"]),
        repo_root=tmp_path,
        profile="p",
        file_selection=frozenset({"directory"}),
    )
    install = import_module("setforge.cli.install")
    with pytest.raises(SetforgeError, match="overlapping tracked-file identities"):
        install._preserved_file_store_ids(ctx, frozenset({"directory/item"}))


@pytest.mark.parametrize("kind", ["generated", "symlink", "tree"])
def test_file_selection_keeps_supported_resource_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    config, live = _mixed_config(tmp_path, monkeypatch, ("one", "tree", "two"))
    tracked = config.parent / "tracked"
    yaml = YAML()
    document = yaml.load(config.read_text())
    selected = "tree" if kind == "tree" else "one"
    if kind == "generated":
        document["tracked_files"]["one"]["generated"] = {"inputs": {"home": "home"}}
        (tracked / "one").write_text("first={{ host.home }}\n")
    elif kind == "symlink":
        document["tracked_files"]["one"]["symlink"] = str(live / "payload")
    yaml.dump(document, config)
    runner = CliRunner()
    args = [
        "install",
        "--profile=p",
        f"--config={config}",
        "--yes",
        "--no-fetch",
        "--no-git-check",
    ]
    initial = runner.invoke(app, args)
    assert initial.exit_code == 0, (initial.output, initial.exception)
    target = live / "tree/item" if kind == "tree" else live / "one"
    before = target.read_bytes()
    if kind == "generated":
        (tracked / "one").write_text("second={{ host.home }}\n")
        expected = f"second={live.parent}\n".encode()
    else:
        (tracked / ("tree/item" if kind == "tree" else "one")).write_bytes(
            b"selected update\n"
        )
        expected = b"selected update\n"
    (tracked / "two").write_bytes(b"unselected change\n")
    applied = runner.invoke(app, [*args, f"--file={selected}"])
    assert applied.exit_code == 0, (applied.output, applied.exception)
    assert target.read_bytes() == expected
    assert (live / "two").read_bytes() == b"two\n"
    if kind == "symlink":
        assert (live / "one").is_symlink()
        assert (live / "payload").read_bytes() == expected
    update_transition = transitions.load_latest("p")
    assert update_transition is not None
    repeated = runner.invoke(app, [*args, f"--file={selected}"])
    assert repeated.exit_code == 0, (repeated.output, repeated.exception)
    assert target.read_bytes() == expected
    reverted = runner.invoke(
        app,
        [
            "revert",
            "--profile=p",
            f"--config={config}",
            f"--to-before={update_transition.name}",
            "--yes",
        ],
    )
    assert reverted.exit_code == 0, (reverted.output, reverted.exception)
    assert target.read_bytes() == before
    assert (live / "two").read_bytes() == b"two\n"


def test_file_selection_retains_profile_section_template_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live = _mixed_config(tmp_path, monkeypatch, ("one", "tree", "two"))
    tracked = config.parent / "tracked"
    (tracked / "guide.md").write_bytes(b"# Guide\nshared baseline\n")
    templates = config.parent / "templates"
    templates.mkdir()
    (templates / "notes.md").write_bytes(b"## Local notes\nlocal template\n")
    yaml = YAML()
    document = yaml.load(config.read_text())
    document["tracked_files"]["one"]["src"] = "guide.md"
    document["tracked_files"]["one"]["dst"] = str(live / "guide.md")
    document["section_templates"] = {"notes": {"src": "notes.md"}}
    document["profiles"]["p"]["section_slots"] = {"Local notes": "notes"}
    yaml.dump(document, config)
    runner = CliRunner()
    args = [
        "install",
        "--profile=p",
        f"--config={config}",
        "--yes",
        "--no-fetch",
        "--no-git-check",
    ]
    initial = runner.invoke(app, args)
    assert initial.exit_code == 0, (initial.output, initial.exception)
    target = live / "guide.md"
    assert b"## Local notes\nlocal template\n" in target.read_bytes()
    (templates / "later.md").write_bytes(b"## Later notes\nnew profile slot\n")
    document["section_templates"]["later"] = {"src": "later.md"}
    document["profiles"]["p"]["section_slots"]["Later notes"] = "later"
    yaml.dump(document, config)
    selected = runner.invoke(app, [*args, "--file=one"])

    assert selected.exit_code == 0, (selected.output, selected.exception)
    assert (
        target.read_bytes()
        == b"# Guide\nshared baseline\n## Local notes\nlocal template\n"
        b"## Later notes\nnew profile slot\n"
    )
    assert (live / "two").read_bytes() == b"two\n"
    assert "Local notes" in load_config(config).profiles["p"].section_slots


@pytest.mark.parametrize("inject_failure", [False, True])
def test_selected_symlink_reverse_retains_unselected_link_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inject_failure: bool
) -> None:
    config, live = _mixed_config(tmp_path, monkeypatch, ("one", "tree", "two"))
    yaml = YAML()
    document = yaml.load(config.read_text())
    for name in ("one", "two"):
        document["tracked_files"][name]["symlink"] = str(live / f"payload-{name}")
    yaml.dump(document, config)
    runner = CliRunner()
    args = [
        "install",
        "--profile=p",
        f"--config={config}",
        "--yes",
        "--no-fetch",
        "--no-git-check",
    ]
    initial = runner.invoke(app, args)
    assert initial.exit_code == 0, (initial.output, initial.exception)
    unselected = live / "two"
    before = (
        str(unselected.readlink()),
        unselected.lstat().st_ino,
        unselected.read_bytes(),
    )
    (config.parent / "tracked/one").write_bytes(b"selected update\n")
    update = runner.invoke(app, [*args, "--file=one"])
    assert update.exit_code == 0, (update.output, update.exception)
    injected: list[bool] = []
    if inject_failure:
        original = operations.apply_filesystem_deltas_reverse_anchored

        def fail(
            deltas: tuple[transitions.FilesystemDelta, ...],
            guards: tuple[operations.PathGuard, ...],
        ) -> None:
            original(deltas, guards)
            assert (live / "one").read_bytes() == b"one\n"
            injected.append(True)
            raise SetforgeError("injected after selected reverse effect")

        monkeypatch.setattr(
            operations, "apply_filesystem_deltas_reverse_anchored", fail
        )
    reversed_result = runner.invoke(
        app, ["revert", "--profile=p", f"--config={config}", "--yes"]
    )
    if inject_failure:
        assert injected == [True], (reversed_result.output, reversed_result.exception)
        assert reversed_result.exit_code != 0
        assert "injected after selected reverse effect" in str(
            reversed_result.exception
        )
    else:
        assert reversed_result.exit_code == 0, (
            reversed_result.output,
            reversed_result.exception,
        )
        assert (live / "one").is_symlink()
        assert (live / "one").read_bytes() == b"one\n"
        redone = runner.invoke(
            app, ["revert", "--profile=p", f"--config={config}", "--yes"]
        )
        assert redone.exit_code == 0, (redone.output, redone.exception)
        assert (live / "one").read_bytes() == b"selected update\n"
    assert (
        str(unselected.readlink()),
        unselected.lstat().st_ino,
        unselected.read_bytes(),
    ) == before


@pytest.mark.parametrize("updated", [False, True])
@pytest.mark.parametrize("attributed", [False, True])
def test_legacy_symlink_inverse_refuses_without_preimage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, updated: bool, attributed: bool
) -> None:
    config, live = _mixed_config(tmp_path, monkeypatch, ("one", "tree", "two"))
    yaml = YAML()
    document = yaml.load(config.read_text())
    for name in ("one", "two"):
        document["tracked_files"][name]["symlink"] = str(live / f"payload-{name}")
    yaml.dump(document, config)
    runner = CliRunner()
    args = [
        "install",
        "--profile=p",
        f"--config={config}",
        "--yes",
        "--no-fetch",
        "--no-git-check",
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, (result.output, result.exception)
    if updated:
        (config.parent / "tracked/one").write_bytes(b"changed\n")
        result = runner.invoke(app, [*args, "--file=one"])
        assert result.exit_code == 0, (result.output, result.exception)
    transition = transitions.load_latest("p")
    assert transition is not None
    # Released records have payload paths and optional attribution, but no
    # filesystem preimage for flat links (verified against released install).
    fs = transition / "filesystem_deltas.json"
    payload = json.loads(fs.read_text())
    links = {str(live / name) for name in ("one", "two")}
    payload["entries"] = [e for e in payload["entries"] if e["path"] not in links]
    fs.write_text(json.dumps(payload))
    meta_path = transition / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["paths"] = [p for p in meta["paths"] if p not in links]
    if not attributed:
        meta.pop("tracked_file_destinations", None)
    meta_path.write_text(json.dumps(meta))
    before = tuple(
        (str(p.readlink()), p.lstat().st_ino, p.read_bytes())
        for p in (live / "one", live / "two")
    )
    state = {
        p.relative_to(tmp_path / "state"): p.read_bytes()
        for p in (tmp_path / "state").rglob("*")
        if p.is_file()
    }
    reverse = runner.invoke(
        app, ["revert", "--profile=p", f"--config={config}", "--yes"]
    )
    assert reverse.exit_code != 0
    assert "legacy transition lacks symlink preimage" in str(reverse.exception)
    revert_module = import_module("setforge.cli.revert")
    with pytest.raises(SetforgeError, match="legacy transition lacks symlink preimage"):
        revert_module._apply_revert(transitions.load_record(transition), "p", config)
    assert (
        tuple(
            (str(p.readlink()), p.lstat().st_ino, p.read_bytes())
            for p in (live / "one", live / "two")
        )
        == before
    )
    assert {
        p.relative_to(tmp_path / "state"): p.read_bytes()
        for p in (tmp_path / "state").rglob("*")
        if p.is_file()
    } == state


def _tree_with_restricted_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, list[str]]:
    config, live = _mixed_config(tmp_path, monkeypatch, ("tree", "one", "two"))
    source = config.parent / "tracked/tree"
    (source / "sub").mkdir()
    (source / "sub").chmod(0o755)
    (source / "sub/extra").write_text("extra\n")
    install = [
        "install",
        "--profile=p",
        f"--config={config}",
        "--yes",
        "--no-fetch",
        "--no-git-check",
    ]
    return config, live, source, install


def test_refused_revert_chain_rolls_back_a_directory_mode_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live, source, install = _tree_with_restricted_subdirectory(
        tmp_path, monkeypatch
    )
    runner = CliRunner()
    assert runner.invoke(app, install).exit_code == 0
    (config.parent / "tracked/one").write_text("one v2\n")
    assert runner.invoke(app, install).exit_code == 0
    target = transitions.load_latest("p")
    assert target is not None
    (source / "sub").chmod(0o700)
    (source / "sub/extra").write_text("extra v2\n")
    assert runner.invoke(app, install).exit_code == 0
    real_apply = operations.apply_filesystem_deltas_reverse_anchored
    applied: list[object] = []

    def fail_second_step(*args: Any, **kwargs: Any) -> None:
        applied.append(args)
        if len(applied) == 2:
            raise RevertFailed("second step failed")
        real_apply(*args, **kwargs)

    monkeypatch.setattr(
        operations, "apply_filesystem_deltas_reverse_anchored", fail_second_step
    )

    failed = runner.invoke(
        app,
        [
            "revert",
            "--profile=p",
            f"--config={config}",
            "--yes",
            f"--to-before={target.name}",
        ],
    )

    assert failed.exit_code != 0
    assert not getattr(failed.exception, "__notes__", ())
    assert "rolled back 1 already reverted step(s)" in failed.output
    assert operations.active("p") is None
    assert stat.S_IMODE((live / "tree/sub").stat().st_mode) == 0o700
    assert (live / "tree/sub/extra").read_text() == "extra v2\n"
    assert (live / "one").read_text() == "one v2\n"
    monkeypatch.setattr(
        operations, "apply_filesystem_deltas_reverse_anchored", real_apply
    )
    assert runner.invoke(app, install).exit_code == 0


def test_revert_chain_with_an_edited_older_file_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live, source, install = _tree_with_restricted_subdirectory(
        tmp_path, monkeypatch
    )
    runner = CliRunner()
    assert runner.invoke(app, install).exit_code == 0
    (config.parent / "tracked/one").write_text("one v2\n")
    assert runner.invoke(app, install).exit_code == 0
    target = transitions.load_latest("p")
    assert target is not None
    (source / "sub").chmod(0o700)
    (source / "sub/extra").write_text("extra v2\n")
    assert runner.invoke(app, install).exit_code == 0
    (live / "one").write_text("manually edited\n")
    records = sorted(transitions.transitions_root().iterdir())

    failed = runner.invoke(
        app,
        [
            "revert",
            "--profile=p",
            f"--config={config}",
            "--yes",
            f"--to-before={target.name}",
        ],
    )

    assert failed.exit_code != 0
    assert "no live changes made" in str(failed.exception)
    assert f"filesystem path changed since transition: {live / 'one'}" in str(
        failed.exception
    )
    assert operations.active("p") is None
    assert stat.S_IMODE((live / "tree/sub").stat().st_mode) == 0o700
    assert (live / "tree/sub/extra").read_text() == "extra v2\n"
    assert (live / "one").read_text() == "manually edited\n"
    assert sorted(transitions.transitions_root().iterdir()) == records


@pytest.mark.parametrize("change_child", [True, False])
def test_failed_revert_rolls_back_a_directory_mode_it_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change_child: bool
) -> None:
    config, live, source, install = _tree_with_restricted_subdirectory(
        tmp_path, monkeypatch
    )
    runner = CliRunner()
    assert runner.invoke(app, install).exit_code == 0
    (source / "sub").chmod(0o700)
    if change_child:
        (source / "sub/extra").write_text("extra v2\n")
    assert runner.invoke(app, install).exit_code == 0
    revert_module = import_module("setforge.cli.revert")

    def crash(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated failure after revert effects")

    monkeypatch.setattr(revert_module, "_write_reverse_transition", crash)

    failed = runner.invoke(
        app, ["revert", "--profile=p", f"--config={config}", "--yes"]
    )

    assert isinstance(failed.exception, RuntimeError)
    assert not getattr(failed.exception, "__notes__", ())
    assert operations.active("p") is None
    assert stat.S_IMODE((live / "tree/sub").stat().st_mode) == 0o700
    assert (live / "tree/sub/extra").read_text() == (
        "extra v2\n" if change_child else "extra\n"
    )
