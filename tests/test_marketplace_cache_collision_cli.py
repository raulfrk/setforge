"""CLI behaviour when a marketplace cache directory holds a different repo.

Under ``claude.install_mode: local-clone`` two repos with the same final name
(``alice/tools`` and ``bob/tools``) share one cache directory. SetForge never
prompts, replaces or reuses that directory on its own: the command fails with an
error that names the marketplace, the directory, both repos and the manual
steps, and the colliding cache directory is left as it was.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from tests.conftest import FakeGit, _local_clone_yaml
from tests.shared_helpers import write_setforge_yaml

_CONFIG = """\
version: 1
tracked_files:
  d:
    src: x
    dst: ~/.setforge-test/y
marketplaces:
  bobs:
    source: github
    repo: bob/tools
claude_plugins:
  helper:
    marketplace: bobs
packages:
  helper:
    type: plugin
    plugin: helper
profiles:
  myprofile:
    tracked_files: [d]
    packages: [helper]
"""


@pytest.fixture
def colliding_cache(
    fake_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FakeGit, Path]:
    """``alice/tools`` already cloned where ``bob/tools`` would be cached."""
    _local_clone_yaml(tmp_path, monkeypatch)
    fake = fake_git(known_repos={"alice/tools", "bob/tools"})
    cache_dir = tmp_path / "marketplaces" / "tools"
    cache_dir.mkdir(parents=True)
    (cache_dir / ".git").mkdir()
    (cache_dir / "marker.txt").write_text("alice's clone", encoding="utf-8")
    fake.cloned[cache_dir] = "alice/tools"
    return fake, cache_dir


def _assert_error_tells_the_user_what_to_do(output: str, cache_dir: Path) -> None:
    assert "marketplace 'bobs'" in output
    assert str(cache_dir) in output
    assert "'alice/tools'" in output
    assert "'bob/tools'" in output
    assert "This cache directory was not touched" in output
    assert "set this marketplace's repo in setforge.yaml to 'alice/tools'" in output
    assert f"rm -rf {cache_dir}" in output


def _assert_nothing_changed(fake: FakeGit, cache_dir: Path) -> None:
    assert (cache_dir / "marker.txt").read_text(encoding="utf-8") == "alice's clone"
    assert fake.cloned == {cache_dir: "alice/tools"}
    assert fake.clone_count() == 0
    assert not any("fetch" in call or "reset" in call for call in fake.calls)


def test_sync_cache_refuses_a_colliding_cache_dir(
    colliding_cache, tmp_path: Path
) -> None:
    fake, cache_dir = colliding_cache
    cfg = write_setforge_yaml(tmp_path, _CONFIG)

    result = CliRunner().invoke(
        app, ["plugin", "sync-cache", "--profile=myprofile", f"--config={cfg}"]
    )

    assert result.exit_code == 1, result.output
    _assert_error_tells_the_user_what_to_do(result.output, cache_dir)
    _assert_nothing_changed(fake, cache_dir)


def test_plugin_reconcile_refuses_a_colliding_cache_dir(
    colliding_cache, fake_claude, tmp_path: Path
) -> None:
    fake, cache_dir = colliding_cache
    claude = fake_claude(marketplaces=[])
    cfg = write_setforge_yaml(tmp_path, _CONFIG)

    result = CliRunner().invoke(
        app, ["plugin", "reconcile", "--profile=myprofile", f"--config={cfg}"]
    )

    assert result.exit_code == 1, result.output
    _assert_error_tells_the_user_what_to_do(result.output, cache_dir)
    _assert_nothing_changed(fake, cache_dir)
    assert claude.mp_add_args() == []


def test_plugin_reconcile_yes_flag_does_not_resolve_the_collision(
    colliding_cache, fake_claude, tmp_path: Path
) -> None:
    """``--yes`` is still accepted but never picks a way out for the user."""
    fake, cache_dir = colliding_cache
    claude = fake_claude(marketplaces=[])
    cfg = write_setforge_yaml(tmp_path, _CONFIG)

    result = CliRunner().invoke(
        app,
        ["plugin", "reconcile", "--profile=myprofile", f"--config={cfg}", "--yes"],
    )

    assert result.exit_code == 1, result.output
    _assert_error_tells_the_user_what_to_do(result.output, cache_dir)
    _assert_nothing_changed(fake, cache_dir)
    assert claude.mp_add_args() == []


def test_plugin_reconcile_dry_run_reports_the_same_collision_as_a_live_run(
    colliding_cache, fake_claude, tmp_path: Path
) -> None:
    """``--dry-run`` predicts the live failure and changes nothing."""
    fake, cache_dir = colliding_cache
    claude = fake_claude(marketplaces=[])
    cfg = write_setforge_yaml(tmp_path, _CONFIG)
    args = ["plugin", "reconcile", "--profile=myprofile", f"--config={cfg}"]

    dry = CliRunner().invoke(app, [*args, "--dry-run"])

    assert dry.exit_code == 1, dry.output
    assert "would add marketplace" not in dry.output
    _assert_error_tells_the_user_what_to_do(dry.output, cache_dir)
    _assert_nothing_changed(fake, cache_dir)
    assert claude.mp_add_args() == []
    assert claude.install_args() == []
    assert claude.enable_args() == []

    live = CliRunner().invoke(app, args)

    assert live.exit_code == dry.exit_code, live.output
    failed_line = "FAILED  bobs"
    assert (
        dry.output[dry.output.index(failed_line) :]
        == (live.output[live.output.index(failed_line) :])
    )
