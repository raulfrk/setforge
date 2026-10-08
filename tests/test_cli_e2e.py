"""CliRunner ring for the ``setforge`` CLI (inner ring).

Drives the real Typer surface against ``tests/fixtures/e2e/setforge.test.yaml``,
sandboxing the live tree under ``tmp_path`` via ``$HOME`` redirection and
mocking ``subprocess.run`` at the ``code`` / ``claude`` seams (extension
+ plugin reconcile). Runs in default ``pytest`` — fast, no Docker.

One test class per top-level CLI command (``install``, ``sync``,
``compare``, ``revert``, ``validate``) to keep the matrix legible.

The Docker ring (``tests/test_e2e_docker.py``) exercises the same
fixtures against real ``claude`` + ``code`` binaries.

This file extends the inner ring with ``fake_claude`` + ``fake_code``
in-memory driver fixtures so the inner ring also exercises the
extension + plugin reconcile legs (not just the warn-and-skip path).
``FakeClaude`` lives in ``tests.test_claude_plugins`` (its primary
consumer); the ``fake_claude`` fixture is re-exported via
``tests/conftest.py`` so this module can request it as a test
parameter. ``fake_code`` is the shared fixture from
``tests/shared_fixtures.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import paths
from setforge.cli import app
from setforge.transitions import transitions_root

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "e2e"
_FIXTURE_YAML = _FIXTURE_DIR / "setforge.test.yaml"
_FIXTURE_TRACKED = _FIXTURE_DIR / "tracked"


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """Copy the fixture repo into ``tmp_path`` so tests can mutate freely.

    Returns the path to the copied ``setforge.test.yaml``. The
    accompanying ``tracked/`` tree sits beside it (yaml's parent = repo
    root for ``resolve_src``). The shared fixture is kept at the current schema,
    so the copy is accepted unchanged by both tolerant runtime loading and the
    strict ``validate`` path.
    """
    target = tmp_path / "repo"
    target.mkdir()
    shutil.copy2(_FIXTURE_YAML, target / "setforge.test.yaml")
    shutil.copytree(_FIXTURE_TRACKED, target / "tracked")
    return target / "setforge.test.yaml"


@pytest.fixture
def sandboxed_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``$HOME`` to a tmp dir so dst ``~/.setforge_e2e/...`` is sandboxed."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    return home


@pytest.fixture
def no_code_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``code`` CLI absent so extension reconcile is warn-and-skipped."""
    monkeypatch.setattr(
        "setforge.vscode_extensions.resolve_binary",
        lambda name: None,
    )


@pytest.fixture
def no_claude_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``claude`` CLI absent so plugin reconcile is warn-and-skipped.

    Clears the module-level lru_cache on ``_get_claude_bin`` first so a
    prior test (e.g. ``test_claude_plugins.py``) that cached a fake path
    doesn't short-circuit our monkeypatched resolver.
    """
    from setforge import claude_plugins as cp

    cp._get_claude_bin.cache_clear()
    monkeypatch.setattr(
        "setforge.claude_plugins.resolve_binary",
        lambda name: None,
    )


def _invoke(args: list[str]) -> Result:
    """Convenience wrapper — returns the typer.testing.Result via CliRunner."""
    return CliRunner().invoke(app, args)


def _transition_dirs() -> set[str]:
    root = transitions_root()
    return {p.name for p in root.iterdir() if p.is_dir()} if root.exists() else set()


# ---------------------------------------------------------------------------
# install — exercises one variant per major tracked_file mechanism
# ---------------------------------------------------------------------------


class TestInstall:
    """``setforge install`` against fixture profiles.

    Mocks ``code`` and ``claude`` as absent so the tracked_file leg is the
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
        live = sandboxed_home / ".setforge_e2e" / "minimal" / "text.txt"
        assert live.exists()
        assert live.read_text() == "hello from test-minimal\n"

    def test_text_sections_no_live_writes_tracked(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        """No pre-existing live file → live is a byte-for-byte copy of tracked.

        With user-section markers retired (schema 2.1), deploy no longer
        rewrites end-marker ``hash=<...>`` segments — it byte-copies the
        tracked source, so a first install of a marker-bearing tracked file
        lands the tracked bytes verbatim.
        """
        result = _invoke(
            [
                "install",
                "--profile=test-text-sections",
                f"--config={fixture_repo}",
            ]
        )
        assert result.exit_code == 0, result.output
        live = sandboxed_home / ".setforge_e2e" / "sections" / "marked.md"
        tracked = fixture_repo.parent / "tracked" / "sections" / "marked.md"
        assert live.read_text() == tracked.read_text()
        assert "setforge:user-section" not in live.read_text()

    def test_json_byte_copy(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        result = _invoke(["install", "--profile=test-json", f"--config={fixture_repo}"])
        assert result.exit_code == 0, result.output
        live = sandboxed_home / ".setforge_e2e" / "json" / "settings.json"
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
        live = sandboxed_home / ".setforge_e2e" / "jsonc" / "shallow.json"
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
        live = sandboxed_home / ".setforge_e2e" / "yaml" / "shallow.yaml"
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
        root = sandboxed_home / ".setforge_e2e" / "directory"
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
        """3-level extends chain: all three entries land; bootstrap stubs created."""
        result = _invoke(
            [
                "install",
                "--profile=test-chain-child",
                f"--config={fixture_repo}",
            ]
        )
        assert result.exit_code == 0, result.output

        root = sandboxed_home / ".setforge_e2e" / "chain"
        assert (root / "grand.txt").read_text() == "grand-content\n"
        assert (root / "base.txt").read_text() == "base-content\n"
        assert (root / "child.txt").read_text() == "child-content\n"

        # Bootstrap stubs created (parent-first order); they're empty files.
        assert (root / "bootstrap-grand.txt").exists()
        assert (root / "bootstrap-base.txt").exists()
        assert (root / "bootstrap-child.txt").exists()

    @pytest.mark.parametrize("edit_live", [True, False], ids=["live-edit", "no-drift"])
    @pytest.mark.parametrize("with_yes", [True, False], ids=["yes", "no-yes"])
    @pytest.mark.parametrize(
        "flag",
        [
            "--auto=use-tracked",
            "--auto=keep-live",
        ],
    )
    def test_auto_flags_leave_a_live_only_edit_alone_without_a_prompt(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
        flag: str,
        with_yes: bool,
        edit_live: bool,
    ) -> None:
        """A live-only edit (or no drift) is a clean no-op under every
        confirmation flag: exit 0 on a non-TTY with or without ``--yes``,
        the live content survives, and no revert hint is printed."""
        args = ["install", "--profile=test-jsonc-shallow", f"--config={fixture_repo}"]
        assert _invoke(args).exit_code == 0
        live = sandboxed_home / ".setforge_e2e" / "jsonc" / "shallow.json"
        if edit_live:
            live.write_text('{"unexpected_new_key": 1}\n')
        expected = live.read_text()

        result = _invoke([*args, flag, *(["--yes"] if with_yes else [])])

        assert result.exit_code == 0, result.output
        assert live.read_text() == expected
        assert "noop" in result.output
        assert "revert with" not in result.output

    def test_comprehensive_tracked_files_only(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        """Comprehensive profile: with ``code``+``claude`` mocked absent, the
        tracked_file leg still completes for all four formats + bootstrap.

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

        root = sandboxed_home / ".setforge_e2e" / "comprehensive"
        assert "comprehensive notes" in (root / "notes.md").read_text()
        assert json.loads((root / "data.json").read_text()) == {
            "key": "comprehensive-value"
        }
        assert "comprehensive-tracked" in (root / "preserve-settings.json").read_text()
        assert "comprehensive-tracked-yaml" in (root / "config.yaml").read_text()
        assert (root / "bootstrap-stub.txt").exists()

    def test_comprehensive_reconciles_plugins_and_extensions(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        fake_claude,
        fake_code,
    ) -> None:
        """test-comprehensive with both fake drivers wired in.

        Asserts the reconcile legs run (not warn-and-skipped):
        - Plugin install lands ``superpowers@claude-plugins-official``;
          ``FakeClaude``'s internal ``_plugins`` is the in-memory analog
          of what real ``claude`` writes to ``installed_plugins.json``
          (production matches: install adds to that JSON, enable flips
          ``enabled: true``).
        - Marketplace was registered before install.
        - Extension reconcile installs ``editorconfig.editorconfig``;
          the post-install ``code --list-extensions`` set matches the
          profile's declared include list.
        """
        fc = fake_claude()
        fk = fake_code(real_binaries=("git",))

        result = _invoke(
            [
                "install",
                "--profile=test-comprehensive",
                f"--config={fixture_repo}",
            ]
        )
        assert result.exit_code == 0, result.output

        # Plugin reconcile leg: marketplace-add then install then enable.
        # ``claude plugin marketplace add`` receives the source URL
        # (``owner/repo`` for GitHub), not the marketplace name.
        assert "anthropics/claude-plugins-official" in fc.mp_add_args()
        assert fc.install_args() == ["superpowers@claude-plugins-official"]
        assert fc.enable_args() == ["superpowers@claude-plugins-official"]
        # ``FakeClaude.installed_state()`` is the in-memory analog of
        # ``installed_plugins.json``; after install + enable it should
        # reflect the declared, enabled set.
        assert fc.installed_state() == {
            "superpowers@claude-plugins-official": {
                "id": "superpowers@claude-plugins-official",
                "enabled": True,
                "scope": "user",
            }
        }

        # Extension reconcile leg: declared include = {editorconfig.editorconfig}.
        assert fk.install_args == ["editorconfig.editorconfig"]
        assert fk.installed_set() == {"editorconfig.editorconfig"}

        # TrackedFile leg still completed.
        root = sandboxed_home / ".setforge_e2e" / "comprehensive"
        assert (root / "notes.md").exists()


# ---------------------------------------------------------------------------
# sync — captures live → tracked
# ---------------------------------------------------------------------------


class TestSync:
    """``setforge sync`` against fixture profiles."""

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

    def test_sync_auto_use_live_absorbs_minimal_drift(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        """Explicit use-live confirmation absorbs ordinary plain-text drift."""
        _invoke(["install", "--profile=test-minimal", f"--config={fixture_repo}"])
        live = sandboxed_home / ".setforge_e2e" / "minimal" / "text.txt"
        live.write_text("updated locally\n")

        synced = _invoke(
            [
                "sync",
                "--profile=test-minimal",
                f"--config={fixture_repo}",
                "--auto=use-live",
                "--yes",
            ]
        )
        assert synced.exit_code == 0, synced.output
        assert "revert with: setforge revert --profile=test-minimal" in synced.output
        tracked = fixture_repo.parent / "tracked" / "minimal" / "text.txt"
        assert "updated locally" in tracked.read_text()

    def test_sync_captures_extensions_via_fake_code(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        fake_claude,
        fake_code,
    ) -> None:
        """``sync`` calls ``capture_extensions`` → ``list_installed``.

        Pre-seed FakeCode with an extra extension the user "installed by
        hand" and confirm sync surveys the live installed set via the
        fake (i.e., reconcile leg is exercised, not warn-and-skipped).
        """
        # Pre-seed with a user-added extension plus the declared one.
        fake_claude()
        fk = fake_code(
            installed={"editorconfig.editorconfig", "ms-python.python"},
            real_binaries=("git",),
        )

        # Install lands the declared set without uninstalling the extra
        # (ADDITIVE policy by default).
        installed = _invoke(
            [
                "install",
                "--profile=test-comprehensive",
                f"--config={fixture_repo}",
            ]
        )
        assert installed.exit_code == 0, installed.output
        assert fk.installed_set() == {"editorconfig.editorconfig", "ms-python.python"}

        # Sync surveys current state via `code --list-extensions`.
        # Track call count before sync so we can assert sync triggered a list.
        list_calls_before = sum(1 for c in fk.calls if c[1:] == ["--list-extensions"])
        before = fixture_repo.read_bytes()
        refused = _invoke(
            ["sync", "--profile=test-comprehensive", f"--config={fixture_repo}"]
        )
        assert refused.exit_code == 1
        assert "--yes to capture it" in refused.output
        assert fixture_repo.read_bytes() == before
        synced = _invoke(
            [
                "sync",
                "--profile=test-comprehensive",
                f"--config={fixture_repo}",
                "--auto=use-live",
                "--yes",
            ]
        )
        assert synced.exit_code == 0, synced.output
        list_calls_after = sum(1 for c in fk.calls if c[1:] == ["--list-extensions"])
        assert list_calls_after > list_calls_before


# ---------------------------------------------------------------------------
# compare — read-only drift report
# ---------------------------------------------------------------------------


class TestCompare:
    """``setforge compare`` against fixture profiles."""

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
        live = sandboxed_home / ".setforge_e2e" / "minimal" / "text.txt"
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

    def test_compare_after_reconcile_install_no_subprocess_shells(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        fake_claude,
        fake_code,
    ) -> None:
        """End-to-end on test-comprehensive: install (with reconcile legs
        firing through fake drivers), then compare.

        Locks the invariant that ``compare`` does NOT invoke either
        reconcile leg — no additional ``claude`` or ``code`` subprocess
        calls land on the fake drivers after the install loop has
        completed. (Drift on ``preserve_user_keys`` placeholders is
        expected on the comprehensive profile, so this test does not
        gate on ``--check`` exit code.)
        """
        fc = fake_claude()
        fk = fake_code(real_binaries=("git",))

        installed = _invoke(
            [
                "install",
                "--profile=test-comprehensive",
                f"--config={fixture_repo}",
            ]
        )
        assert installed.exit_code == 0, installed.output
        claude_calls_after_install = len(fc.calls)
        code_calls_after_install = len(fk.calls)

        result = _invoke(
            [
                "compare",
                "--profile=test-comprehensive",
                f"--config={fixture_repo}",
            ]
        )
        assert result.exit_code == 0, result.output
        # compare doesn't shell out to claude or code.
        assert len(fc.calls) == claude_calls_after_install
        assert len(fk.calls) == code_calls_after_install

    def test_compare_renders_local_overlay_provenance(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        """A host-local overlay shows up in ``compare`` with the
        ``[from local.yaml]`` tag on adds, the U+2212 remove tag on removes,
        and a footer carrying the per-axis counts."""
        paths.local_config_path().write_text(
            "plugins:\n"
            "  add:\n"
            "    - some-extra@claude-plugins-official\n"
            "  remove:\n"
            "    - superpowers\n"
            "extensions:\n"
            "  add:\n"
            "    - ms-toolsai.jupyter\n"
            "  remove:\n"
            "    - editorconfig.editorconfig\n"
            "marketplaces:\n"
            "  add:\n"
            "    work-internal:\n"
            "      source: github\n"
            "      repo: work-corp/claude-plugins\n",
            encoding="utf-8",
        )

        result = _invoke(
            ["compare", "--profile=test-comprehensive", f"--config={fixture_repo}"]
        )

        assert result.exit_code == 0, result.output
        minus = chr(0x2212)
        remove_tag = f"[{minus} removed via local.yaml]"
        assert "some-extra@claude-plugins-official [from local.yaml]" in result.output
        assert "ms-toolsai.jupyter [from local.yaml]" in result.output
        assert f"{minus} superpowers {remove_tag}" in result.output
        assert f"{minus} editorconfig.editorconfig {remove_tag}" in result.output
        assert (
            f"[Host overlay summary: plugins 1+/1{minus}; "
            f"extensions 1+/1{minus}; marketplaces 1+/0{minus} via local.yaml]"
        ) in result.output


# ---------------------------------------------------------------------------
# revert — undoes most recent install/sync
# ---------------------------------------------------------------------------


class TestRevert:
    """``setforge revert`` against fixture profiles."""

    def test_install_then_revert_restores_state(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
    ) -> None:
        live = sandboxed_home / ".setforge_e2e" / "minimal" / "text.txt"
        assert not live.exists()

        installed = _invoke(
            ["install", "--profile=test-minimal", f"--config={fixture_repo}"]
        )
        assert installed.exit_code == 0, installed.output
        assert live.exists()

        reverted = _invoke(
            ["revert", "--profile=test-minimal", f"--config={fixture_repo}", "--yes"]
        )
        assert reverted.exit_code == 0, reverted.output
        # Revert removes the file (it was created from absence on install).
        assert not live.exists()
        # ... and records its own reverse transition.
        assert any(
            "revert-test-minimal" in entry.name
            for entry in transitions_root().iterdir()
        )

    def test_revert_uninstalls_extension_via_fake_code(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        fake_claude,
        fake_code,
    ) -> None:
        """Install with fake drivers seeds an extension delta into the
        transition; revert applies the inverse via ``uninstall_one``.

        Confirms the revert leg actually drives ``code --uninstall-extension``
        through ``FakeCode`` (not warn-and-skipped).
        """
        fake_claude()
        fk = fake_code(
            real_binaries=("git",)
        )  # starts empty — install will add editorconfig.

        installed = _invoke(
            [
                "install",
                "--profile=test-comprehensive",
                f"--config={fixture_repo}",
            ]
        )
        assert installed.exit_code == 0, installed.output
        assert fk.installed_set() == {"editorconfig.editorconfig"}

        reverted = _invoke(
            [
                "revert",
                "--profile=test-comprehensive",
                f"--config={fixture_repo}",
                "--yes",
            ]
        )
        assert reverted.exit_code == 0, reverted.output
        # The install delta recorded `added: [editorconfig.editorconfig]`;
        # revert inverts that into an uninstall call.
        assert fk.uninstall_args == ["editorconfig.editorconfig"]
        assert fk.installed_set() == set()

    def test_failed_extension_leaves_a_transition_and_a_rerun_repairs_it(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        fake_claude,
        fake_code,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An install whose only event is a skipped extension still records
        a transition, and a plain re-run installs the extension."""
        fake_claude()
        fk = fake_code(installed={"editorconfig.editorconfig"}, real_binaries=("git",))
        args = ["install", "--profile=test-comprehensive", f"--config={fixture_repo}"]
        assert _invoke([*args, "--yes"]).exit_code == 0
        fk.installed.clear()

        def refuse_install(
            argv: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            if argv[1:2] == ["--install-extension"]:
                raise subprocess.CalledProcessError(1, argv, "", "refused")
            return fk.run(argv, **kwargs)

        before = _transition_dirs()
        with monkeypatch.context() as patch:
            patch.setattr("setforge.vscode_extensions.subprocess.run", refuse_install)
            failed = _invoke([*args, "--yes"])
        assert failed.exit_code == 0, failed.output
        assert "1 skipped: editorconfig.editorconfig" in failed.output
        (recorded,) = _transition_dirs() - before
        assert not (transitions_root() / recorded / "reconcile_outcomes.json").exists()

        repaired = _invoke([*args, "--yes"])
        assert repaired.exit_code == 0, repaired.output
        assert fk.installed_set() == {"editorconfig.editorconfig"}

    @pytest.mark.parametrize(
        ("profile", "deployed"),
        [
            pytest.param(
                "test-prose-reviewers",
                [
                    (".claude/agents/python-prose-reviewer.md", "claude/agents"),
                    (".claude/agents/claude-md-prose-reviewer.md", "claude/agents"),
                    (".claude/agents/markdown-prose-reviewer.md", "claude/agents"),
                    (
                        ".claude/skills/reviewing-markdown/SKILL.md",
                        "claude/skills/reviewing-markdown",
                    ),
                ],
                id="prose-reviewers",
            ),
            pytest.param(
                "test-workflows",
                [(".claude/workflows/example-impl.js", "claude/workflows")],
                id="workflows",
            ),
        ],
    )
    def test_new_artifact_install_compare_revert_lifecycle(
        self,
        fixture_repo: Path,
        sandboxed_home: Path,
        no_code_bin: None,
        no_claude_bin: None,
        profile: str,
        deployed: list[tuple[str, str]],
    ) -> None:
        """Net-new ``~/.claude`` artifacts (agents, a skill, a workflow) deploy
        byte-identical into parent directories that did not exist, compare
        clean, survive a ``--auto=keep-tracked`` sync untouched, and are
        removed again by ``revert``."""
        tracked_root = fixture_repo.parent / "tracked"
        pairs = [
            (
                sandboxed_home / live_rel,
                tracked_root / tracked_dir / Path(live_rel).name,
            )
            for live_rel, tracked_dir in deployed
        ]
        tracked_before = {tracked: tracked.read_bytes() for _, tracked in pairs}
        args = [f"--profile={profile}", f"--config={fixture_repo}"]

        installed = _invoke(["install", *args])
        assert installed.exit_code == 0, installed.output
        for live, tracked in pairs:
            assert live.read_bytes() == tracked_before[tracked], live

        compared = _invoke(["compare", *args, "--check"])
        assert compared.exit_code == 0, compared.output

        transitions_before = _transition_dirs()
        synced = _invoke(["sync", *args, "--auto=keep-tracked", "--yes"])
        assert synced.exit_code == 0, synced.output
        for tracked, before in tracked_before.items():
            assert tracked.read_bytes() == before, tracked
        assert _transition_dirs() == transitions_before

        reverted = _invoke(["revert", *args, "--yes"])
        assert reverted.exit_code == 0, reverted.output
        for live, _ in pairs:
            assert not live.exists(), live


# ---------------------------------------------------------------------------
# validate — config-shape check, no filesystem comparison
# ---------------------------------------------------------------------------


class TestValidate:
    """``setforge validate`` against the fixture YAML.

    The fixture YAML is itself validated in CI by ``setforge validate
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

    @pytest.mark.parametrize(
        ("local_body", "phrases"),
        [
            pytest.param(
                "plugins:\n  add:\n    - superpowers\n  remove:\n    - superpowers\n",
                ("in both add and remove", "'superpowers'"),
                id="add-remove-collision",
            ),
            pytest.param(
                "plugins:\n  remove:\n    - never-was-there\n",
                ("not in profile-resolved set", "'never-was-there'"),
                id="remove-not-in-profile",
            ),
            pytest.param(
                "plugins:\n  add:\n    - leaked-tool@undefined-mp\n",
                ("'leaked-tool'", "'undefined-mp'", "Available marketplaces"),
                id="undefined-marketplace",
            ),
        ],
    )
    def test_validate_rejects_invalid_local_overlay(
        self, fixture_repo: Path, local_body: str, phrases: tuple[str, ...]
    ) -> None:
        paths.local_config_path().write_text(local_body, encoding="utf-8")

        result = _invoke(
            [
                "validate",
                "--profile=test-comprehensive",
                f"--config={fixture_repo}",
            ]
        )

        assert result.exit_code != 0, result.output
        for phrase in phrases:
            assert phrase in result.output, result.output


# ---------------------------------------------------------------------------
# --verbose/-v flag + SETFORGE_LOG_LEVEL env var
# ---------------------------------------------------------------------------


class TestVerbosity:
    """``-v`` / ``--verbose`` and ``SETFORGE_LOG_LEVEL`` wire the root logger.

    Precedence: flag > env > WARNING default. Garbage env values fall back
    to WARNING silently. The root ``_root`` callback calls
    ``logging.basicConfig(force=True, ...)`` so each invocation
    re-initializes the handlers cleanly across tests.
    """

    def test_root_v_flag_enables_info_stderr(self, fixture_repo: Path) -> None:
        """-v sets INFO level (count=True)."""
        result = _invoke(["-v", "validate", "--all", f"--config={fixture_repo}"])
        assert result.exit_code == 0, result.output
        # -v is INFO; the DEBUG message is filtered out.
        assert "setforge.cli DEBUG: logging configured at level" not in result.stderr

    def test_root_vv_flag_enables_debug_stderr(self, fixture_repo: Path) -> None:
        """-vv sets DEBUG level (count=True)."""
        result = _invoke(["-vv", "validate", "--all", f"--config={fixture_repo}"])
        assert result.exit_code == 0, result.output
        assert "setforge.cli DEBUG: logging configured at level" in result.stderr

    def test_env_var_enables_debug_when_flag_absent(
        self,
        fixture_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SETFORGE_LOG_LEVEL", "DEBUG")
        result = _invoke(["validate", "--all", f"--config={fixture_repo}"])
        assert result.exit_code == 0, result.output
        assert "setforge.cli DEBUG: logging configured at level" in result.stderr

    def test_garbage_env_var_falls_back_to_warning(
        self,
        fixture_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SETFORGE_LOG_LEVEL", "not-a-level")
        result = _invoke(["validate", "--all", f"--config={fixture_repo}"])
        assert result.exit_code == 0, result.output
        assert "setforge.cli DEBUG: logging configured at level" not in result.stderr

    def test_flag_overrides_env(
        self,
        fixture_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """-vv overrides SETFORGE_LOG_LEVEL=WARNING (flag > env > default)."""
        monkeypatch.setenv("SETFORGE_LOG_LEVEL", "WARNING")
        result = _invoke(["-vv", "validate", "--all", f"--config={fixture_repo}"])
        assert result.exit_code == 0, result.output
        assert "setforge.cli DEBUG: logging configured at level" in result.stderr

    def test_garbage_setforge_log_level_emits_stderr_warning(
        self,
        fixture_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SETFORGE_LOG_LEVEL", "DEBGU")
        result = _invoke(["validate", "--all", f"--config={fixture_repo}"])
        assert result.exit_code == 0, result.output
        assert "unknown SETFORGE_LOG_LEVEL='DEBGU'" in result.stderr
        assert "defaulting to WARNING" in result.stderr

    def test_garbage_setforge_log_level_with_non_level_module_attr_warns(
        self,
        fixture_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``SETFORGE_LOG_LEVEL=BASIC_FORMAT`` resolves to a ``logging`` str attr.

        A bare ``getattr(logging, env_value.upper(), None) is None`` check
        accepts ``logging.BASIC_FORMAT`` (a non-None string) and then
        ``basicConfig(level=<str>)`` interprets the format string as a
        level name and crashes opaquely. The ``isinstance(resolved, int)``
        guard surfaces the same friendly stderr warning the typo path emits.
        """
        monkeypatch.setenv("SETFORGE_LOG_LEVEL", "BASIC_FORMAT")
        result = _invoke(["validate", "--all", f"--config={fixture_repo}"])
        assert result.exit_code == 0, result.output
        assert "unknown SETFORGE_LOG_LEVEL='BASIC_FORMAT'" in result.stderr
        assert "defaulting to WARNING" in result.stderr


class TestSourceLayerMigrationError:
    """Verify legacy ``my_setup.yaml`` triggers a CLI-surfaced migration hint.

    The unit test in :class:`tests.test_source.TestValidateSourceDir`
    pins the function-level contract; these tests pin the CLI
    integration (the source-layer resolution fires from ``--source``,
    propagates the ConfigError up the Typer call chain, and surfaces
    the actionable migration recipe to the caller).
    """

    def test_compare_with_legacy_my_setup_yaml_surfaces_migration_hint(
        self,
        tmp_path: Path,
    ) -> None:
        """``compare --source=<legacy-dir>`` raises ConfigError with ``git mv`` hint.

        The autouse ``_isolated_local_config`` fixture in
        ``tests/conftest.py`` already resets module-level
        ``setforge.source._cli_source`` per test.
        """
        from setforge.errors import ConfigError

        src_dir = tmp_path / "legacy-src"
        src_dir.mkdir()
        (src_dir / "my_setup.yaml").write_text("version: 1\nprofiles: {}\n")
        result = _invoke(["--source", str(src_dir), "compare", "--profile=anything"])
        assert result.exit_code != 0, result.output
        assert isinstance(result.exception, ConfigError), result.exception
        msg = str(result.exception)
        assert "legacy 'my_setup.yaml'" in msg, msg
        assert "git mv my_setup.yaml setforge.yaml" in msg, msg
