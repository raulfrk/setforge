"""Tests for the A0 fetch-upstream wiring in ``setforge install``.

``install`` fetches the git config source (via :func:`source.fetch_source`)
BEFORE the pre-deploy git-check, so the pulled content is what gets
reconciled. ``--no-fetch`` skips the pull entirely for offline / CI runs.
A :class:`PathSource` (the legacy ``--config`` shape these tests use) is a
no-op inside ``fetch_source``, so the wiring is asserted via a spy on the
call rather than real git ops.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import git_ops, operations
from setforge import source as source_mod
from setforge.cli import app
from setforge.errors import SetforgeError, SourceNotCloned
from setforge.source import GitSource, PathSource

_PROFILE = "test-fetch"


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.setforge_fetch/doc.md\n"
        "    disposition: shared\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - doc\n",
        encoding="utf-8",
    )
    (repo / "tracked").mkdir(parents=True, exist_ok=True)
    (repo / "tracked" / "doc.md").write_text("# doc\n", encoding="utf-8")
    return config


def _write_local_package_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.0'\n"
        "version: 1\n"
        "tracked_files: {}\n"
        "packages:\n"
        "  helper:\n"
        "    type: local\n"
        "    path: helper\n"
        "    binary: helper\n"
        "    install: ~/.local/bin\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    packages: [helper]\n",
        encoding="utf-8",
    )
    tracked = repo / "tracked"
    tracked.mkdir()
    (tracked / "helper").write_bytes(b"#!/bin/sh\nexit 0\n")
    return config


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _install(config: Path | None, *extra: str) -> Result:
    args = [
        "install",
        f"--profile={_PROFILE}",
        "--no-secrets-scan",
        "--no-git-check",
        "--yes",
        *extra,
    ]
    if config is not None:
        args.append(f"--config={config}")
    return CliRunner().invoke(app, args)


def test_install_without_package_delta_does_not_begin_irreversible_checkpoint(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(repo)
    begun: list[str] = []
    real_begin = operations.begin_checkpoint

    def record_begin(
        journal: operations.OperationJournal, **kwargs: Any
    ) -> operations.OperationJournal:
        begun.append(kwargs["name"])
        return real_begin(journal, **kwargs)

    monkeypatch.setattr(operations, "begin_checkpoint", record_begin)

    result = _install(config, "--no-fetch", "--no-transition")

    assert result.exit_code == 0, result.output
    assert "packages" not in begun
    assert "secrets-and-bootstrap" not in begun


def test_install_package_failure_retains_uncertain_manual_recovery(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_local_package_config(repo)
    package_effect = repo / "package-effect"

    def fail_after_effect(*_args: object, **_kwargs: object) -> list[object]:
        package_effect.write_text("possibly installed", encoding="utf-8")
        raise RuntimeError("package manager interrupted")

    monkeypatch.setattr("setforge.cli.install.reconcile_packages", fail_after_effect)

    result = _install(config, "--no-fetch", "--no-transition")

    assert result.exit_code == 1
    assert package_effect.exists(), (result.output, result.exception)
    assert package_effect.read_text(encoding="utf-8") == "possibly installed"
    journal = operations.load(_PROFILE)
    assert journal.phase is operations.OperationPhase.MANUAL
    package_checkpoint = next(
        item for item in journal.checkpoints if item.name == "packages"
    )
    assert not package_checkpoint.completed
    operations.complete(journal)


def test_install_report_adapters_emit_drift_without_writes_or_transition(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_claude,
) -> None:
    config = repo / "setforge.yaml"
    config.write_text(
        """version: 1
tracked_files: {}
marketplaces:
  m1:
    source: github
    repo: owner/m1
claude_plugins:
  plug:
    marketplace: m1
packages:
  plug:
    type: plugin
    plugin: plug
  pub.ext:
    type: extension
    extension: pub.ext
profiles:
  test-fetch:
    packages: [plug, pub.ext]
    reconcile:
      plugins:
        policy: report
      extensions:
        policy: report
""",
        encoding="utf-8",
    )
    extension_writes: list[str] = []
    monkeypatch.setattr(
        "setforge.vscode_extensions.resolve_binary", lambda _name: Path("/fake/code")
    )
    monkeypatch.setattr("setforge.vscode_extensions.list_installed", lambda: set())
    monkeypatch.setattr(
        "setforge.vscode_extensions.install_one",
        lambda ext_id, **_kwargs: extension_writes.append(ext_id),
    )
    monkeypatch.setattr(
        "setforge.vscode_extensions.uninstall_one",
        lambda ext_id: extension_writes.append(ext_id),
    )
    claude = fake_claude(marketplaces=[])
    import setforge.cli.install as install_mod

    real_plan_plugins = install_mod.claude_plugins_mod.plan_reconcile
    planned_auto: list[bool] = []

    def plan_plugins(*args: Any, auto: bool = False, **kwargs: Any) -> Any:
        planned_auto.append(auto)
        return real_plan_plugins(*args, auto=auto, **kwargs)

    monkeypatch.setattr(install_mod.claude_plugins_mod, "plan_reconcile", plan_plugins)

    def transition_tripwire(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("REPORT-only install must not write a transition")

    monkeypatch.setattr(
        "setforge.cli.install._write_install_transition", transition_tripwire
    )

    result = _install(config, "--no-fetch")

    assert result.exit_code == 0, result.output
    assert "would install" in result.output
    assert planned_auto == [True]
    assert extension_writes == []
    mutating = {"install", "enable", "disable", "add"}
    assert not any(mutating.intersection(call) for call in claude.calls)


class TestFetchWiring:
    def test_explicit_config_git_check_does_not_rediscover_source(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import setforge.cli.install as install_mod

        selected_config = _write_config(repo)
        other_repo = repo.parent / "other-repo"
        other_repo.mkdir()
        _write_config(other_repo)
        discoveries: list[Path] = []

        def rediscover() -> PathSource:
            discoveries.append(other_repo)
            return PathSource(path=other_repo)

        checked_sources: list[object] = []
        monkeypatch.setattr("setforge.cli._git_check.get_resolved_source", rediscover)
        monkeypatch.setattr(
            install_mod,
            "run_git_check_or_raise",
            lambda *, source, **_kwargs: checked_sources.append(source),
        )

        result = _install(selected_config, "--dry-run", "--no-fetch")

        assert result.exit_code == 0, result.output
        assert discoveries == []
        assert checked_sources == [PathSource(path=repo)]

    def test_real_fetch_runs_inside_global_install_lock(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import setforge.cli.install as install_mod

        held = False

        @contextmanager
        def recording_lock(**scopes: object) -> Iterator[None]:
            nonlocal held
            assert scopes["resources"] is True
            held = True
            try:
                yield
            finally:
                held = False

        def guarded_fetch(_source: object) -> str:
            assert held, "fetch escaped the cross-profile install resource lock"
            return "ok"

        monkeypatch.setattr(install_mod, "mutation_locks", recording_lock)
        monkeypatch.setattr(source_mod, "fetch_source", guarded_fetch)

        result = _install(_write_config(repo))

        assert result.exit_code == 0, result.output

    def test_live_fingerprint_refuses_same_bytes_symlink_retarget(
        self, repo: Path
    ) -> None:
        import setforge.cli.install as install_mod

        target_a = repo / "target-a"
        target_b = repo / "target-b"
        target_a.write_text("same\n", encoding="utf-8")
        target_b.write_text("same\n", encoding="utf-8")
        link = repo / "live-link"
        link.symlink_to(target_a)
        planned = install_mod._snapshot_live_paths({link})
        link.unlink()
        link.symlink_to(target_b)

        with pytest.raises(SetforgeError, match="topology changed"):
            install_mod._assert_live_paths_unchanged(planned)

    def test_live_fingerprint_refuses_declared_link_replaced_by_file(
        self, repo: Path
    ) -> None:
        import setforge.cli.install as install_mod

        target = repo / "target"
        target.write_text("same\n", encoding="utf-8")
        link = repo / "declared-link"
        link.symlink_to(target)
        planned = install_mod._snapshot_live_paths({link, target})
        link.unlink()
        link.write_text("same\n", encoding="utf-8")

        with pytest.raises(SetforgeError, match="topology changed"):
            install_mod._assert_live_paths_unchanged(planned)

    def test_live_fingerprint_normalizes_mid_snapshot_oserror(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import setforge.cli.install as install_mod

        target = repo / "target"
        target.write_text("content\n", encoding="utf-8")
        link = repo / "link"
        link.symlink_to(target)
        real_readlink = Path.readlink

        def raced_readlink(path: Path) -> Path:
            if path == link:
                raise FileNotFoundError("removed after lstat")
            return real_readlink(path)

        monkeypatch.setattr(Path, "readlink", raced_readlink)

        with pytest.raises(SetforgeError, match="changed while snapshotting"):
            install_mod._snapshot_live_paths({link})

    def test_install_fetches_by_default(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []

        def _spy(src: object) -> str:
            calls.append(src)
            return "ok"

        monkeypatch.setattr(source_mod, "fetch_source", _spy)
        config = _write_config(repo)
        result = _install(config)
        assert result.exit_code == 0, result.output
        assert len(calls) == 1

    def test_fetched_config_is_loaded_after_fetch(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _write_config(repo)

        def _update_source(_src: object) -> str:
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "~/.setforge_fetch/doc.md", "~/.setforge_fetch/fetched.md"
                ),
                encoding="utf-8",
            )
            return "updated"

        monkeypatch.setattr(source_mod, "fetch_source", _update_source)

        result = _install(config)

        assert result.exit_code == 0, result.output
        assert (Path.home() / ".setforge_fetch" / "fetched.md").read_text(
            encoding="utf-8"
        ) == "# doc\n"
        assert not (Path.home() / ".setforge_fetch" / "doc.md").exists()

    def test_config_symlink_is_canonicalized_before_fetch(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_a = _write_config(repo)
        repo_b = repo.parent / "repo-b"
        repo_b.mkdir()
        config_b = _write_config(repo_b)
        config_b.write_text(
            config_b.read_text(encoding="utf-8").replace(
                "~/.setforge_fetch/doc.md", "~/.setforge_fetch/wrong.md"
            ),
            encoding="utf-8",
        )
        alias = repo.parent / "active-setforge.yaml"
        alias.symlink_to(config_a)

        def retarget_alias(_source: object) -> str:
            alias.unlink()
            alias.symlink_to(config_b)
            return "updated"

        monkeypatch.setattr(source_mod, "fetch_source", retarget_alias)

        result = _install(alias)

        assert result.exit_code == 0, result.output
        assert (Path.home() / ".setforge_fetch" / "doc.md").exists()
        assert not (Path.home() / ".setforge_fetch" / "wrong.md").exists()

    def test_config_change_while_loading_refuses_before_deploy(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import setforge.cli.install as install_mod

        config = _write_config(repo)
        real_load = install_mod.load_config

        def load_then_change(path: Path):
            parsed = real_load(path)
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "~/.setforge_fetch/doc.md", "~/.setforge_fetch/raced.md"
                ),
                encoding="utf-8",
            )
            return parsed

        monkeypatch.setattr(install_mod, "load_config", load_then_change)

        result = _install(config, "--no-fetch")

        assert result.exit_code != 0
        assert result.exception is not None
        assert "configuration changed while loading" in str(result.exception)
        assert not (Path.home() / ".setforge_fetch").exists()

    def test_config_change_before_planning_refuses_before_writes(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import setforge.cli.install as install_mod

        config = _write_config(repo)
        real_build = install_mod._build_install_plan

        def mutate_then_build(*args: Any, **kwargs: Any):
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "~/.setforge_fetch/doc.md", "~/.setforge_fetch/raced.md"
                ),
                encoding="utf-8",
            )
            return real_build(*args, **kwargs)

        monkeypatch.setattr(install_mod, "_build_install_plan", mutate_then_build)
        monkeypatch.setattr(
            install_mod,
            "_write_install_transition",
            lambda *_args, **_kwargs: pytest.fail("transition write crossed refusal"),
        )

        result = _install(config, "--no-fetch")

        assert result.exit_code != 0
        assert result.exception is not None
        assert "configuration changed before planning" in str(result.exception)
        assert not (Path.home() / ".setforge_fetch").exists()

    def test_source_change_during_planning_refuses_before_writes(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import setforge.cli.install as install_mod

        config = _write_config(repo)
        source = repo / "tracked" / "doc.md"
        real_plan = install_mod.plan_provisioning

        def plan_then_mutate(*args: Any, **kwargs: Any):
            planned = real_plan(*args, **kwargs)
            source.write_text("# changed during planning\n", encoding="utf-8")
            return planned

        monkeypatch.setattr(install_mod, "plan_provisioning", plan_then_mutate)
        monkeypatch.setattr(
            install_mod,
            "_write_install_transition",
            lambda *_args, **_kwargs: pytest.fail("transition write crossed refusal"),
        )

        result = _install(config, "--no-fetch")

        assert result.exit_code != 0
        assert result.exception is not None
        assert "inputs changed during planning" in str(result.exception)
        assert not (Path.home() / ".setforge_fetch").exists()

    def test_live_symlink_retarget_during_planning_refuses_before_writes(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import setforge.cli.install as install_mod

        config = _write_config(repo)
        live = Path.home() / ".setforge_fetch" / "doc.md"
        live.parent.mkdir(parents=True)
        target_a = live.parent / "target-a"
        target_b = live.parent / "target-b"
        target_a.write_text("same\n", encoding="utf-8")
        target_b.write_text("same\n", encoding="utf-8")
        live.symlink_to(target_a)
        real_plan = install_mod.plan_provisioning

        def plan_then_retarget(*args: Any, **kwargs: Any):
            planned = real_plan(*args, **kwargs)
            live.unlink()
            live.symlink_to(target_b)
            return planned

        monkeypatch.setattr(install_mod, "plan_provisioning", plan_then_retarget)
        monkeypatch.setattr(
            install_mod,
            "_write_install_transition",
            lambda *_args, **_kwargs: pytest.fail("transition write crossed refusal"),
        )

        result = _install(config, "--no-fetch")

        assert result.exit_code != 0
        assert result.exception is not None
        assert "topology changed" in str(result.exception)
        assert live.is_symlink()
        assert live.readlink() == target_b

    def test_directory_entry_added_during_planning_refuses_before_writes(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import setforge.cli.install as install_mod

        config = _write_config(repo)
        config.write_text(
            config.read_text(encoding="utf-8").replace("src: doc.md", "src: docs"),
            encoding="utf-8",
        )
        docs = repo / "tracked" / "docs"
        docs.mkdir()
        (docs / "existing.md").write_text("existing\n", encoding="utf-8")
        real_plan = install_mod.install_helpers_mod._plan_tracked_files

        def add_then_plan(*args: Any, **kwargs: Any):
            (docs / "added.md").write_text("added\n", encoding="utf-8")
            return real_plan(*args, **kwargs)

        monkeypatch.setattr(
            install_mod.install_helpers_mod, "_plan_tracked_files", add_then_plan
        )
        monkeypatch.setattr(
            install_mod,
            "_write_install_transition",
            lambda *_args, **_kwargs: pytest.fail("transition write crossed refusal"),
        )

        result = _install(config, "--no-fetch")

        assert result.exit_code != 0
        assert result.exception is not None
        assert "tracked file inventory changed" in str(result.exception)
        assert not (Path.home() / ".setforge_fetch").exists()

    def test_transient_directory_entry_during_compare_refuses_before_writes(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import setforge.cli.install as install_mod

        config = _write_config(repo)
        config.write_text(
            config.read_text(encoding="utf-8").replace("src: doc.md", "src: docs"),
            encoding="utf-8",
        )
        docs = repo / "tracked" / "docs"
        docs.mkdir()
        (docs / "existing.md").write_text("existing\n", encoding="utf-8")
        transient = docs / "transient.md"
        real_compare = install_mod.compare_mod.compare_profile

        def compare_with_transient(*args: Any, **kwargs: Any):
            transient.write_text("transient\n", encoding="utf-8")
            try:
                return real_compare(*args, **kwargs)
            finally:
                transient.unlink()

        monkeypatch.setattr(
            install_mod.compare_mod, "compare_profile", compare_with_transient
        )

        result = _install(config, "--no-fetch")

        assert result.exit_code != 0
        assert result.exception is not None
        assert "tracked file inventory changed" in str(result.exception)
        assert not (Path.home() / ".setforge_fetch").exists()

    def test_no_fetch_skips_the_pull(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []

        def _spy(src: object) -> str:
            calls.append(src)
            return "ok"

        monkeypatch.setattr(source_mod, "fetch_source", _spy)
        config = _write_config(repo)
        result = _install(config, "--no-fetch")
        assert result.exit_code == 0, result.output
        assert calls == []

    def test_git_source_fetch_message_echoed(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When the source is a real GitSource, the fetch status line surfaces.
        git_src = GitSource(url="https://example.invalid/cfg.git", ref="main")
        monkeypatch.setattr(
            "setforge.cli.install.resolve_source_for_git_check",
            lambda _repo_root: git_src,
        )
        config = _write_config(repo)
        monkeypatch.setattr(
            "setforge.cli.install._resolve_config_arg", lambda _config: config
        )
        monkeypatch.setattr(
            source_mod, "fetch_source", lambda _src: "fetched and checked out main"
        )
        # git-check also resolves a source; keep it from touching the network.
        monkeypatch.setattr(
            "setforge.cli.install.run_git_check_or_raise",
            lambda **_kw: None,
        )
        result = _install(None)
        assert result.exit_code == 0, result.output
        assert "fetched and checked out main" in result.output

    def test_git_source_dry_run_announces_without_fetching(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        git_src = GitSource(url="https://example.invalid/cfg.git", ref="main")
        monkeypatch.setattr(
            "setforge.cli.install.resolve_source_for_git_check",
            lambda _repo_root: git_src,
        )
        config = _write_config(repo)
        monkeypatch.setattr(
            "setforge.cli.install._resolve_config_arg", lambda _config: config
        )
        calls: list[object] = []

        def _unexpected_fetch(source: object) -> str:
            calls.append(source)
            return "fetched"

        monkeypatch.setattr(source_mod, "fetch_source", _unexpected_fetch)
        monkeypatch.setattr(
            "setforge.cli.install.run_git_check_or_raise", lambda **_kwargs: None
        )
        result = _install(None, "--dry-run")

        assert result.exit_code == 0, result.output
        assert "WOULD fetch upstream config source" in result.output
        assert calls == []

    def test_path_source_fetch_is_silent(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A PathSource fetch no-ops; its status line must NOT clutter output.
        monkeypatch.setattr(
            "setforge.cli.install.resolve_source_for_git_check",
            lambda repo_root: PathSource(path=repo_root),
        )
        config = _write_config(repo)
        result = _install(config)
        assert result.exit_code == 0, result.output
        assert "nothing to fetch" not in result.output

    def test_no_fetch_runs_no_git_operation(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []

        def _spy(*args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            raise AssertionError("no git operation must run under --no-fetch")

        monkeypatch.setattr(git_ops, "_run_git", _spy)
        config = _write_config(repo)
        result = _install(config, "--no-fetch")
        assert result.exit_code == 0, result.output
        assert calls == []

    def test_no_fetch_missing_git_clone_raises_source_not_cloned(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import setforge.cli.install as install_mod

        git_src = GitSource(url="https://example.invalid/cfg.git", ref="main")
        monkeypatch.setattr(
            "setforge.cli.install.resolve_source_for_git_check",
            lambda _repo_root: git_src,
        )
        config = _write_config(repo)
        monkeypatch.setattr(install_mod, "_resolve_config_arg", lambda _config: config)
        result = CliRunner().invoke(
            app,
            [
                "install",
                f"--profile={_PROFILE}",
                "--no-secrets-scan",
                "--yes",
                "--no-fetch",
            ],
        )
        assert result.exit_code != 0
        assert isinstance(result.exception, SourceNotCloned)
