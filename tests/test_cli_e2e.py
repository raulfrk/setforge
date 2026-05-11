"""CliRunner ring for the ``my-setup`` CLI (dotfiles-nen.9 inner ring).

Drives the real Typer surface against ``tests/fixtures/e2e/my_setup.test.yaml``,
sandboxing the live tree under ``tmp_path`` via ``$HOME`` redirection and
mocking ``subprocess.run`` at the ``code`` / ``claude`` seams (extension
+ plugin reconcile). Runs in default ``pytest`` — fast, no Docker.

One test class per top-level CLI command (``install``, ``sync``,
``compare``, ``revert``, ``validate``) to keep the matrix legible.

The Docker ring (``tests/test_e2e_docker.py``) exercises the same
fixtures against real ``claude`` + ``code`` binaries.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from my_setup.cli import app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "e2e"
_FIXTURE_YAML = _FIXTURE_DIR / "my_setup.test.yaml"
_FIXTURE_TRACKED = _FIXTURE_DIR / "tracked"


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """Copy the fixture repo into ``tmp_path`` so tests can mutate freely.

    Returns the path to the copied ``my_setup.test.yaml``. The
    accompanying ``tracked/`` tree sits beside it (yaml's parent = repo
    root for ``resolve_src``).
    """
    target = tmp_path / "repo"
    target.mkdir()
    shutil.copy2(_FIXTURE_YAML, target / "my_setup.test.yaml")
    shutil.copytree(_FIXTURE_TRACKED, target / "tracked")
    return target / "my_setup.test.yaml"


@pytest.fixture
def sandboxed_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``$HOME`` to a tmp dir so dst ``~/.my_setup_e2e/...`` is sandboxed."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MY_SETUP_STATE_DIR", str(tmp_path / "state"))
    return home


@pytest.fixture
def no_code_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``code`` CLI absent so extension reconcile is warn-and-skipped."""
    monkeypatch.setattr(
        "my_setup.vscode_extensions.resolve_binary",
        lambda name: None,
    )


@pytest.fixture
def no_claude_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``claude`` CLI absent so plugin reconcile is warn-and-skipped.

    Clears the module-level lru_cache on ``_get_claude_bin`` first so a
    prior test (e.g. ``test_claude_plugins.py``) that cached a fake path
    doesn't short-circuit our monkeypatched resolver.
    """
    from my_setup import claude_plugins as cp

    cp._get_claude_bin.cache_clear()
    monkeypatch.setattr(
        "my_setup.claude_plugins.resolve_binary",
        lambda name: None,
    )


def _invoke(args: list[str]) -> subprocess.CompletedProcess[str]:  # type: ignore[type-arg]
    """Convenience wrapper — returns the typer.testing.Result via CliRunner."""
    return CliRunner().invoke(app, args)


# ---------------------------------------------------------------------------
# install — exercises one variant per major dotfile mechanism
# ---------------------------------------------------------------------------


class TestInstall:
    """``my-setup install`` against fixture profiles.

    Mocks ``code`` and ``claude`` as absent so the dotfile leg is the
    only side-effect under test. The Docker ring picks up the
    extension + plugin legs against real binaries.
    """

    def test_minimal_byte_copy(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        result = _invoke(
            ["install", "--profile=test-minimal", f"--config={fixture_repo}"]
        )
        assert result.exit_code == 0, result.output
        live = sandboxed_home / ".my_setup_e2e" / "minimal" / "text.txt"
        assert live.exists()
        assert live.read_text() == "hello from test-minimal\n"

    def test_text_sections_no_live_writes_tracked(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        """No pre-existing live file → live equals tracked verbatim."""
        result = _invoke(
            [
                "install",
                "--profile=test-text-sections",
                f"--config={fixture_repo}",
            ]
        )
        assert result.exit_code == 0, result.output
        live = sandboxed_home / ".my_setup_e2e" / "sections" / "marked.md"
        tracked = fixture_repo.parent / "tracked" / "sections" / "marked.md"
        assert live.read_text() == tracked.read_text()

    def test_json_byte_copy(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        result = _invoke(["install", "--profile=test-json", f"--config={fixture_repo}"])
        assert result.exit_code == 0, result.output
        live = sandboxed_home / ".my_setup_e2e" / "json" / "settings.json"
        payload = json.loads(live.read_text())
        assert payload == {
            "settingA": "tracked-value-A",
            "settingB": 42,
            "settingC": ["alpha", "beta"],
        }

    def test_jsonc_shallow_no_live(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        """JSONC without a pre-seeded live: byte-copy semantics + comments survive."""
        result = _invoke(
            [
                "install",
                "--profile=test-jsonc-shallow",
                f"--config={fixture_repo}",
            ]
        )
        assert result.exit_code == 0, result.output
        live = sandboxed_home / ".my_setup_e2e" / "jsonc" / "shallow.json"
        content = live.read_text()
        assert "// tracked side comment" in content
        assert "tracked-placeholder-A" in content
        assert "tracked-placeholder-B" in content

    def test_yaml_shallow_no_live(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        result = _invoke(
            [
                "install",
                "--profile=test-yaml-shallow",
                f"--config={fixture_repo}",
            ]
        )
        assert result.exit_code == 0, result.output
        live = sandboxed_home / ".my_setup_e2e" / "yaml" / "shallow.yaml"
        content = live.read_text()
        assert "trackedKey: tracked-value" in content
        # Comment from the tracked side should survive a round-trip.
        assert "YAML shallow-preserve fixture" in content

    def test_directory_recursive(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        result = _invoke(
            ["install", "--profile=test-directory", f"--config={fixture_repo}"]
        )
        assert result.exit_code == 0, result.output
        root = sandboxed_home / ".my_setup_e2e" / "directory"
        assert (root / "file-a.txt").read_text() == "file-a content\n"
        assert (root / "file-b.txt").read_text() == "file-b content\n"
        assert (root / "nested" / "file-c.txt").read_text() == (
            "file-c content (nested)\n"
        )

    def test_chain_resolution_and_bootstrap(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        """3-level extends chain: all three dotfiles land; bootstrap stubs created."""
        result = _invoke(
            [
                "install",
                "--profile=test-chain-child",
                f"--config={fixture_repo}",
            ]
        )
        assert result.exit_code == 0, result.output

        root = sandboxed_home / ".my_setup_e2e" / "chain"
        assert (root / "grand.txt").read_text() == "grand-content\n"
        assert (root / "base.txt").read_text() == "base-content\n"
        assert (root / "child.txt").read_text() == "child-content\n"

        # Bootstrap stubs created (parent-first order); they're empty files.
        assert (root / "bootstrap-grand.txt").exists()
        assert (root / "bootstrap-base.txt").exists()
        assert (root / "bootstrap-child.txt").exists()

    def test_comprehensive_dotfiles_only(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        """Comprehensive profile: with ``code``+``claude`` mocked absent, the
        dotfile leg still completes for all four formats + bootstrap.

        The plugin / extension legs are exercised by the Docker ring.
        """
        result = _invoke(
            [
                "install",
                "--profile=test-comprehensive",
                f"--config={fixture_repo}",
            ]
        )
        assert result.exit_code == 0, result.output

        root = sandboxed_home / ".my_setup_e2e" / "comprehensive"
        assert "comprehensive notes" in (root / "notes.md").read_text()
        assert json.loads((root / "data.json").read_text()) == {
            "key": "comprehensive-value"
        }
        assert "comprehensive-tracked" in (root / "preserve-settings.json").read_text()
        assert "comprehensive-tracked-yaml" in (root / "config.yaml").read_text()
        assert (root / "bootstrap-stub.txt").exists()


# ---------------------------------------------------------------------------
# sync — captures live → tracked
# ---------------------------------------------------------------------------


class TestSync:
    """``my-setup sync`` against fixture profiles."""

    def test_sync_no_drift_noop(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        """install then sync — tracked unchanged."""
        installed = _invoke(
            ["install", "--profile=test-minimal", f"--config={fixture_repo}"]
        )
        assert installed.exit_code == 0, installed.output

        tracked = fixture_repo.parent / "tracked" / "minimal" / "text.txt"
        before = tracked.read_bytes()
        synced = _invoke(["sync", "--profile=test-minimal", f"--config={fixture_repo}"])
        assert synced.exit_code == 0, synced.output
        assert tracked.read_bytes() == before

    def test_sync_absorbs_minimal_drift_silently(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        """Plain-text drift outside preserve_user_* surfaces is silently absorbed."""
        _invoke(["install", "--profile=test-minimal", f"--config={fixture_repo}"])
        live = sandboxed_home / ".my_setup_e2e" / "minimal" / "text.txt"
        live.write_text("updated locally\n")

        synced = _invoke(["sync", "--profile=test-minimal", f"--config={fixture_repo}"])
        assert synced.exit_code == 0, synced.output
        tracked = fixture_repo.parent / "tracked" / "minimal" / "text.txt"
        assert "updated locally" in tracked.read_text()


# ---------------------------------------------------------------------------
# compare — read-only drift report
# ---------------------------------------------------------------------------


class TestCompare:
    """``my-setup compare`` against fixture profiles."""

    def test_compare_clean_after_install_exits_zero_with_check(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        _invoke(["install", "--profile=test-minimal", f"--config={fixture_repo}"])
        result = _invoke(
            [
                "compare",
                "--profile=test-minimal",
                f"--config={fixture_repo}",
                "--check",
            ]
        )
        assert result.exit_code == 0, result.output

    def test_compare_strict_reports_drift_exits_nonzero(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        _invoke(["install", "--profile=test-minimal", f"--config={fixture_repo}"])
        live = sandboxed_home / ".my_setup_e2e" / "minimal" / "text.txt"
        live.write_text("mutated\n")

        result = _invoke(
            [
                "compare",
                "--profile=test-minimal",
                f"--config={fixture_repo}",
                "--check",
                "--strict",
            ]
        )
        assert result.exit_code == 1, result.output


# ---------------------------------------------------------------------------
# revert — undoes most recent install/sync
# ---------------------------------------------------------------------------


class TestRevert:
    """``my-setup revert`` against fixture profiles."""

    def test_install_then_revert_restores_state(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        live = sandboxed_home / ".my_setup_e2e" / "minimal" / "text.txt"
        assert not live.exists()

        installed = _invoke(
            ["install", "--profile=test-minimal", f"--config={fixture_repo}"]
        )
        assert installed.exit_code == 0, installed.output
        assert live.exists()

        reverted = _invoke(
            ["revert", "--profile=test-minimal", f"--config={fixture_repo}"]
        )
        assert reverted.exit_code == 0, reverted.output
        # Revert removes the file (it was created from absence on install).
        assert not live.exists()


# ---------------------------------------------------------------------------
# validate — config-shape check, no filesystem comparison
# ---------------------------------------------------------------------------


class TestValidate:
    """``my-setup validate`` against the fixture YAML.

    The fixture YAML is itself validated in CI by ``my-setup validate
    --all`` (acceptance bullet). This class pins per-profile validate
    semantics end-to-end through the CliRunner.
    """

    def test_validate_all_clean_exits_zero(self, fixture_repo: Path) -> None:
        result = _invoke(["validate", "--all", f"--config={fixture_repo}"])
        assert result.exit_code == 0, result.output
        assert "ok" in result.output

    def test_validate_per_profile_minimal(self, fixture_repo: Path) -> None:
        result = _invoke(
            [
                "validate",
                "--profile=test-minimal",
                f"--config={fixture_repo}",
            ]
        )
        assert result.exit_code == 0, result.output

    def test_validate_per_profile_chain_child(self, fixture_repo: Path) -> None:
        result = _invoke(
            [
                "validate",
                "--profile=test-chain-child",
                f"--config={fixture_repo}",
            ]
        )
        assert result.exit_code == 0, result.output

    def test_validate_per_profile_comprehensive(self, fixture_repo: Path) -> None:
        result = _invoke(
            [
                "validate",
                "--profile=test-comprehensive",
                f"--config={fixture_repo}",
            ]
        )
        assert result.exit_code == 0, result.output
