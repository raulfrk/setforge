from __future__ import annotations

import subprocess
import sys
from importlib import import_module
from pathlib import Path

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
from setforge.errors import SetforgeError
from setforge.ownership import OwnershipStore, resolve_owner_common_dir
from setforge.provision.receipt import default_receipt_root


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
    for name, module in tuple(sys.modules.items()):
        if name.startswith("setforge") and module is not None:
            for attribute in ("LOCAL_CONFIG_PATH", "_LOCAL_CONFIG_PATH"):
                if attribute in vars(module):
                    monkeypatch.setattr(module, attribute, local)
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
            host_local_sections_map={},
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
