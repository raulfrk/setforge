"""Mixed-resource CLI sequences checked against explicit filesystem intent."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest
from click.testing import Result
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from ruamel.yaml import YAML
from typer.testing import CliRunner

from setforge import (
    binaries,
    codex_lifecycle,
    compare,
    locking,
    operations,
    orphan_scan,
    paths,
    snapshots,
    source,
    transitions,
)
from setforge.cli import app
from setforge.config import load_config, resolve_effective_profile
from setforge.file_ownership import file_resource_id
from setforge.ownership import (
    ClaimLifecycle,
    OwnershipStore,
    read_owner_id,
    resolve_owner_common_dir,
)
from setforge.reconcile.types import HunkClass

from ..conftest import redirect_local_config_path

type Action = Literal[
    "install", "observe", "publish", "snapshot", "prune", "switch", "retire", "change"
]

_ACTIONS: tuple[Action, ...] = (
    "install",
    "observe",
    "publish",
    "snapshot",
    "prune",
    "switch",
    "retire",
    "change",
)


def _contents(paths: Sequence[Path]) -> dict[Path, tuple[int, bytes | str]]:
    return {
        path: (
            path.lstat().st_mode,
            str(path.readlink()) if path.is_symlink() else path.read_bytes(),
        )
        for path in paths
    }


def _identities(paths: Sequence[Path]) -> dict[Path, tuple[int, int, int]]:
    return {
        path: (info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
        for path in paths
        for info in (path.lstat(),)
    }


def _run_sequence(root: Path, actions: Sequence[Action], marker: str) -> None:
    home = root / "home"
    home.mkdir(parents=True)
    repo = root / "repo"
    tracked = repo / "tracked"
    tracked.mkdir(parents=True)
    stage = importlib.import_module("setforge.cli.stage")
    with pytest.MonkeyPatch.context() as patch:
        for key in tuple(os.environ):
            if key.startswith("GIT_"):
                patch.delenv(key)
        environment = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "SETFORGE_STATE_DIR": str(root / "state"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_STATE_HOME": str(home / ".local/state"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "file",
        }
        for key, value in environment.items():
            patch.setenv(key, value)
        local = home / ".config/setforge/local.yaml"
        redirect_local_config_path(patch, local)
        patch.setattr(source, "_cli_source", None)
        patch.setattr(compare, "GENERIC_DST_ROOTS", compare.GENERIC_DST_ROOTS | {home})
        scanner = root / "gitleaks"
        scanner.write_text("#!/bin/sh\nexit 0\n")
        scanner.chmod(0o755)
        patch.setenv("SETFORGE_GITLEAKS_BIN", str(scanner))

        expected_note = f"baseline: {marker}\n".encode()
        (tracked / "note.txt").write_bytes(expected_note)
        template = b"home={{ host.home }}\n"
        (tracked / "generated.txt").write_bytes(template)
        (tracked / "retired.txt").write_bytes(b"retired baseline\n")
        (tracked / "model.toml").write_text('model = "fixture"\n')
        (tracked / "guide.md").write_bytes(b"instructions\n")
        tree = tracked / "tree"
        (tree / "folder").mkdir(parents=True)
        (tree / "empty").mkdir()
        (tree / "folder/data.txt").write_bytes(b"tree data\n")
        (tree / "dangling").symlink_to("missing")
        (tree / "directory-link").symlink_to("folder", target_is_directory=True)
        live = home / ".managed"
        codex = home / ".codex"
        codex.mkdir()
        (codex / "config.toml").write_text('personal = "keep"\n')
        document = {
            "schema_version": "6.5",
            "minimum_version": "6.4",
            "tracked_files": {
                "note": {"src": "note.txt", "dst": str(live / "note.txt")},
                "generated": {
                    "src": "generated.txt",
                    "dst": str(live / "generated.txt"),
                    "generated": {"inputs": {"home": "home"}},
                },
                "tree": {
                    "src": "tree",
                    "dst": str(live / "tree"),
                    "tree": {"symlinks": "preserve"},
                },
                "retired": {"src": "retired.txt", "dst": str(live / "retired.txt")},
            },
            "codex": {
                "config": {"model": {"source": "model.toml"}},
                "instructions": {"guide": {"source": "guide.md"}},
            },
            "profiles": {
                "base": {"tracked_files": ["note", "generated", "tree", "retired"]},
                "p": {
                    "extends": "base",
                    "codex": {"config": ["model"], "instructions": ["guide"]},
                },
                "q": {"extends": "base"},
                "publisher": {"tracked_files": ["note"]},
            },
        }
        config_path = repo / "setforge.yaml"
        YAML().dump(document, config_path)
        assert repo.resolve().is_relative_to(root.resolve())
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            check=True,
        )
        runtime_paths = [
            OwnershipStore().root,
            operations.journals_root(),
            transitions.state_root(),
            snapshots.snapshots_root(),
            locking._user_global_locks_dir(),
            resolve_owner_common_dir(repo),
        ]
        for profile in ("base", "p", "q", "publisher"):
            cfg = load_config(config_path)
            resolved = resolve_effective_profile(cfg, profile, repo).resolved
            for name in resolved.tracked_files:
                runtime_paths.extend(
                    (
                        compare.resolve_src(cfg.tracked_files[name], repo),
                        compare.resolve_dst(cfg.tracked_files[name]),
                    )
                )
            runtime_paths.extend(
                codex_lifecycle.config_destinations(
                    cfg, resolved, repo, profile=profile
                )
            )
        runtime_paths.append(paths.local_config_path())
        assert all(
            path.resolve().is_relative_to(root.resolve()) for path in runtime_paths
        )
        assert binaries.resolve_binary("gitleaks") == scanner

        runner = CliRunner()

        def cli(*args: str, profile: str | None = "p") -> Result:
            argv = [*args, f"--config={config_path}"]
            if profile is not None:
                argv.append(f"--profile={profile}")
            result = runner.invoke(app, argv)
            assert result.exit_code == 0, (argv, result.output, result.exception)
            return result

        def install(profile: str = "p") -> None:
            cli("install", "--yes", "--no-fetch", "--no-git-check", profile=profile)

        def classify(kind: HunkClass) -> None:
            with pytest.MonkeyPatch.context() as ui:
                ui.setattr(
                    stage,
                    "sys",
                    SimpleNamespace(
                        **{**vars(sys), "stdin": SimpleNamespace(isatty=lambda: True)}
                    ),
                )
                ui.setattr(
                    stage,
                    "_interactive_choice",
                    lambda _: lambda h, i, n: stage.Decision(kind),
                )
                cli("stage", "note", profile="publisher")

        install()
        install("publisher")
        owner = read_owner_id(repo)
        note = live / "note.txt"
        stray = live / "unrecorded"
        stray.write_bytes(b"unrecorded neighbor\n")
        extra = live / "live-only"
        extra_created = False
        retired = True
        current = "p"

        def leaves() -> list[Path]:
            result = [
                note,
                live / "generated.txt",
                live / "tree/folder/data.txt",
                live / "tree/dangling",
                live / "tree/directory-link",
                codex / "config.toml",
                codex / "AGENTS.md",
            ]
            if retired:
                result.append(live / "retired.txt")
            return result

        def verify() -> None:
            assert note.read_bytes() == expected_note
            assert (tracked / "note.txt").read_bytes() == expected_note
            assert (tracked / "generated.txt").read_bytes() == template
            assert (live / "generated.txt").read_text() == f"home={home}\n"
            assert (live / "tree/folder/data.txt").read_bytes() == b"tree data\n"
            assert (live / "tree/empty").is_dir()
            assert (live / "tree/dangling").readlink() == Path("missing")
            assert (live / "tree/directory-link").readlink() == Path("folder")
            assert tomllib.loads((codex / "config.toml").read_text()) == {
                "personal": "keep",
                "model": "fixture",
            }
            assert (codex / "AGENTS.md").read_bytes() == b"instructions\n"
            assert stray.read_bytes() == b"unrecorded neighbor\n"
            if extra_created:
                assert extra.read_bytes() == b"preserve this addition\n"
            if not retired:
                assert not (live / "retired.txt").exists()
            claims = [
                claim
                for claim in OwnershipStore().list_claims()
                if claim.lifecycle is ClaimLifecycle.CLAIMED
            ]
            expected_claims = {
                note,
                live / "generated.txt",
                live / "tree",
                codex / "AGENTS.md",
            }
            if retired:
                expected_claims.add(live / "retired.txt")
            assert {Path(claim.locator) for claim in claims} == expected_claims
            assert all(claim.owner_id == owner for claim in claims)
            assert all(
                operations.active(name) is None for name in ("p", "q", "publisher")
            )
            scan = orphan_scan.scan_unrecorded_managed_tree(
                load_config(config_path),
                repo,
                config_path=config_path,
                transitions_dir=transitions.transitions_root(),
            )
            candidates = {entry.path for entry in scan.entries}
            assert not set(leaves()) & candidates
            assert stray in candidates
            report = cli("--format=json", "compare", profile=current)
            assert json.loads(report.stdout)["data"]["orphans"] == []

        verify()
        for index, action in enumerate(actions):
            if action == "install":
                before_install = _identities(leaves())
                install(current)
                assert _identities(leaves()) == before_install
            elif action == "observe":
                before_observe = _contents(leaves()), _identities(leaves())
                cli("--format=json", "status", profile=current)
                cli("--format=json", "stage", "--list", profile="publisher")
                cli("--format=json", "inspect", "note", profile="publisher")
                assert (_contents(leaves()), _identities(leaves())) == before_observe
            elif action == "publish":
                install("publisher")
                updated = expected_note + f"host {index}: {marker}\n".encode()
                note.write_bytes(updated)
                classify(HunkClass.LOCAL)
                cli("sync", "--auto=use-live", "--yes", profile="publisher")
                assert (tracked / "note.txt").read_bytes() == expected_note
                assert note.read_bytes() == updated
                classify(HunkClass.SHARED)
                cli("sync", "--auto=use-live", "--yes", profile="publisher")
                expected_note = updated
            elif action == "snapshot":
                before = _contents(leaves())
                saved, changed = f"saved-{index}", f"changed-{index}"
                cli("snapshot", "create", saved)
                note.write_bytes(f"drift\r\n{marker}\r\n".encode())
                (live / "tree/dangling").unlink()
                (live / "tree/dangling").symlink_to("other")
                (live / "tree/directory-link").unlink()
                (live / "tree/directory-link").symlink_to("other-directory")
                extra.write_bytes(b"preserve this addition\n")
                extra_created = True
                cli("snapshot", "create", changed)
                drift = _contents(leaves())
                cli("snapshot", "restore", saved, "--yes")
                assert _contents(leaves()) == before
                cli("snapshot", "restore", changed, "--yes")
                assert _contents(leaves()) == drift
                cli("snapshot", "restore", saved, "--yes")
            elif action == "prune":
                history = transitions.transitions_root()
                assert history.resolve().is_relative_to(root.resolve())
                if history.exists():
                    shutil.rmtree(history)
            elif action == "switch":
                current = "q" if current == "p" else "p"
                install(current)
            elif action == "change":
                expected_note += f"upstream {index}: {marker}\n".encode()
                (tracked / "note.txt").write_bytes(expected_note)
                # Another profile may have published since this profile's base.
                # This action explicitly chooses the newly edited source.
                cli(
                    "install",
                    "--yes",
                    "--no-fetch",
                    "--no-git-check",
                    "--auto=use-tracked",
                    profile=current,
                )
            else:
                assert action == "retire"
                if retired:
                    (tracked / "retired.txt").write_bytes(b"fresh retirement history\n")
                    install()
                    updated_config = YAML().load(config_path.read_text())
                    updated_config["profiles"]["base"]["tracked_files"].remove(
                        "retired"
                    )
                    del updated_config["tracked_files"]["retired"]
                    YAML().dump(updated_config, config_path)
                    refused = runner.invoke(
                        app,
                        [
                            "cleanup-orphans",
                            "--apply",
                            "--yes",
                            "--profile=p",
                            f"--config={config_path}",
                        ],
                    )
                    assert refused.exit_code != 0
                    retired_path = live / "retired.txt"
                    assert retired_path.read_bytes() == b"fresh retirement history\n"
                    store = OwnershipStore()
                    resource = file_resource_id(retired_path)
                    cli(
                        "ownership",
                        "release",
                        store.claim_id(resource),
                        "--yes",
                        profile=None,
                    )
                    cli("cleanup-orphans", "--apply", "--yes")
                    assert not retired_path.exists()
                    cli("revert", "--yes")
                    assert retired_path.read_bytes() == b"fresh retirement history\n"
                    cli("cleanup-orphans", "--apply", "--yes")
                    retired = False
                else:
                    cli("cleanup-orphans", "--apply", "--yes", profile=current)
            verify()


def test_mixed_resource_lifecycle_sequence_is_isolated(tmp_path: Path) -> None:
    for name in ("first", "second"):
        _run_sequence(tmp_path / name, _ACTIONS, "café λ")


@pytest.mark.slow
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    actions=st.lists(st.sampled_from(_ACTIONS), min_size=1, max_size=8),
    marker=st.text(alphabet="ab01 caféλ", min_size=1, max_size=12),
)
@example(actions=["publish", "change"], marker="fixture")
def test_generated_resource_lifecycle_sequences(
    tmp_path: Path, actions: list[Action], marker: str
) -> None:
    with tempfile.TemporaryDirectory(prefix="sequence-", dir=tmp_path) as temporary:
        _run_sequence(Path(temporary), actions, marker)
