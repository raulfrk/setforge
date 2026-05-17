"""CliRunner ring for the ``my-setup`` CLI (tracked_files-nen.9 inner ring).

Drives the real Typer surface against ``tests/fixtures/e2e/my_setup.test.yaml``,
sandboxing the live tree under ``tmp_path`` via ``$HOME`` redirection and
mocking ``subprocess.run`` at the ``code`` / ``claude`` seams (extension
+ plugin reconcile). Runs in default ``pytest`` — fast, no Docker.

One test class per top-level CLI command (``install``, ``sync``,
``compare``, ``revert``, ``validate``) to keep the matrix legible.

The Docker ring (``tests/test_e2e_docker.py``) exercises the same
fixtures against real ``claude`` + ``code`` binaries.

tracked_files-181 (this file) extends nen.9 with ``fake_claude`` + ``fake_code``
in-memory driver fixtures so the inner ring also exercises the
extension + plugin reconcile legs (not just the warn-and-skip path).
``FakeClaude`` lives in ``tests.test_claude_plugins`` (its primary
consumer); the ``fake_claude`` fixture is re-exported via
``tests/conftest.py`` so this module can request it as a test
parameter. ``FakeCode`` is defined inline below since
``test_cli_e2e.py`` is its only consumer today.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app

# Snapshot the real ``subprocess.run`` at import time so the ``fake_code``
# fixture can forward non-code, non-claude invocations (e.g. the ``patch``
# subprocess fired by ``transitions.apply_patch_reverse`` during revert)
# through to the real implementation, even when both fakes have monkey-
# patched ``subprocess.run`` away.
_REAL_SUBPROCESS_RUN = subprocess.run

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


# ---------------------------------------------------------------------------
# FakeCode — in-memory ``code`` driver for the extension reconcile leg.
#
# Mirrors the FakeClaude pattern from tests/test_claude_plugins.py but is
# scoped to ``setforge.vscode_extensions``. ``vscode_extensions`` invokes
# ``subprocess.run`` with separate argv tokens (``[code, "--install-extension",
# id]``) and has no ``lru_cache``'d resolver, so the fixture only needs to
# monkeypatch ``resolve_binary`` + ``subprocess.run`` — no cache to clear.
# ---------------------------------------------------------------------------


class FakeCode:
    """In-memory simulation of the ``code`` CLI extension surface.

    Tracks installed extension IDs in ``self._installed`` and records
    every invocation in ``self.calls`` so tests can assert both the
    end-state and the exact subprocess sequence.

    Recognized commands (matched on ``args[1:]``):
    - ``--list-extensions`` → newline-joined sorted installed set.
    - ``--install-extension <id>`` → adds ``<id>`` to the installed set.
    - ``--uninstall-extension <id>`` → removes ``<id>`` (no-op if absent).

    ``FakeCode.run`` only handles invocations whose argv[1] starts with
    ``--`` (the ``code`` flag style). Other invocations (e.g. ``claude
    plugin list``) are forwarded to ``_delegate`` — set by the
    ``fake_code`` fixture to the previously-installed ``subprocess.run``
    so a co-resident ``fake_claude`` continues to handle its own calls.
    This is required because monkeypatching ``setforge.foo.subprocess.run``
    actually patches the global ``subprocess.run`` attribute (both
    modules share the same ``subprocess`` module object), so the second
    fixture would otherwise clobber the first.
    """

    def __init__(self, *, installed: set[str] | None = None) -> None:
        self._installed: set[str] = set(installed or ())
        self.calls: list[list[str]] = []
        # Set by the fake_code fixture so non-code, non-claude invocations
        # forward to the real subprocess.run (so transitions' patch /
        # git calls still work). ``_delegate`` is the prior
        # ``subprocess.run`` binding captured at fixture-setup time and
        # is used only for ``claude`` argv.
        self._delegate: Callable[..., subprocess.CompletedProcess[str]] | None = None
        self._real_run: Callable[..., subprocess.CompletedProcess[str]] | None = None

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        # Dispatch on the binary path basename: this fixture only owns
        # invocations of the ``code`` binary. ``claude`` argv goes to
        # the prior subprocess.run binding (the co-resident FakeClaude
        # when fake_claude ran first); any other binary invocation
        # (``patch`` / ``git`` etc. from the transitions layer) passes
        # through to the real :func:`subprocess.run` via
        # ``_real_run``.
        if not args or Path(args[0]).name != "code":
            basename = Path(args[0]).name if args else ""
            if basename == "claude" and self._delegate is not None:
                return self._delegate(args, **kwargs)
            if self._real_run is not None:
                return self._real_run(args, **kwargs)
            raise AssertionError(f"unexpected code invocation: {args!r}")

        self.calls.append(list(args))
        cmd = args[1:]
        if cmd == ["--list-extensions"]:
            body = "\n".join(sorted(self._installed))
            stdout = body + ("\n" if body else "")
            return subprocess.CompletedProcess(args, 0, stdout, "")
        if len(cmd) == 2 and cmd[0] == "--install-extension":
            self._installed.add(cmd[1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if len(cmd) == 2 and cmd[0] == "--uninstall-extension":
            self._installed.discard(cmd[1])
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected code invocation: {args!r}")

    # Convenience query helpers parallel to FakeClaude's install_args / etc.
    def install_args(self) -> list[str]:
        return [
            c[2] for c in self.calls if len(c) >= 3 and c[1] == "--install-extension"
        ]

    def uninstall_args(self) -> list[str]:
        return [
            c[2] for c in self.calls if len(c) >= 3 and c[1] == "--uninstall-extension"
        ]

    def installed_set(self) -> set[str]:
        return set(self._installed)


@pytest.fixture
def fake_code(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeCode]:
    """Return a factory that wires :class:`FakeCode` into ``vscode_extensions``.

    Parallel to ``fake_claude``: monkeypatches both
    ``setforge.vscode_extensions.resolve_binary`` (so ``_ensure_code``
    returns a non-None path) and ``subprocess.run`` (so reconcile
    invokes the fake driver instead of the real ``code`` binary).

    Captures the prior ``subprocess.run`` binding (whatever was active
    before this factory ran — possibly ``fake_claude.run``) and stores
    it on the FakeCode instance so non-code argv shapes forward through
    instead of raising. Letting both fakes coexist requires this because
    both ``setforge.vscode_extensions.subprocess`` and
    ``setforge.claude_plugins.subprocess`` resolve to the same module
    object — the second monkeypatch would otherwise clobber the first.

    The fixture-order precondition (``fake_claude`` must be requested
    before ``fake_code`` in the test signature so the delegate snapshot
    captures ``FakeClaude.run``) is ENFORCED at factory-call time via
    an assertion below — not merely documented.
    """

    def factory(*, installed: set[str] | None = None) -> FakeCode:
        if subprocess.run is _REAL_SUBPROCESS_RUN:
            raise AssertionError(
                "fake_code requires fake_claude to be requested first; "
                "request both fixtures in this order so the delegate "
                "snapshot captures FakeClaude.run."
            )
        fake = FakeCode(installed=installed)
        # Capture whatever subprocess.run is bound to right now BEFORE
        # we overwrite it (will be the FakeClaude.run when fake_claude
        # ran first, otherwise the real subprocess.run). Used only for
        # ``claude`` argv; everything else (e.g. ``patch`` from
        # transitions) flows through ``_REAL_SUBPROCESS_RUN``.
        fake._delegate = subprocess.run
        fake._real_run = _REAL_SUBPROCESS_RUN
        monkeypatch.setattr(
            "setforge.vscode_extensions.resolve_binary",
            lambda name: Path("/usr/local/bin/code") if name == "code" else None,
        )
        monkeypatch.setattr("setforge.vscode_extensions.subprocess.run", fake.run)
        return fake

    return factory


def _invoke(args: list[str]) -> Result:
    """Convenience wrapper — returns the typer.testing.Result via CliRunner."""
    return CliRunner().invoke(app, args)


# ---------------------------------------------------------------------------
# install — exercises one variant per major tracked_file mechanism
# ---------------------------------------------------------------------------


class TestInstall:
    """``my-setup install`` against fixture profiles.

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
        """No pre-existing live file → live equals tracked with hashes maintained.

        Post-9by, install always rewrites end-marker ``hash=<...>``
        segments so the embedded hash matches the body actually written;
        the post-install live byte-matches ``maintain_marker_hashes``
        applied to tracked, not the raw tracked bytes.
        """
        from setforge.section_reconcile import maintain_marker_hashes

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
        assert live.read_text() == maintain_marker_hashes(tracked.read_text())

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
        """3-level extends chain: all three entries land; bootstrap stubs created."""
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

        root = sandboxed_home / ".my_setup_e2e" / "comprehensive"
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
        fk = fake_code()

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
        assert fk.install_args() == ["editorconfig.editorconfig"]
        assert fk.installed_set() == {"editorconfig.editorconfig"}

        # TrackedFile leg still completed.
        root = sandboxed_home / ".my_setup_e2e" / "comprehensive"
        assert (root / "notes.md").exists()


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
        fk = fake_code(installed={"editorconfig.editorconfig", "ms-python.python"})

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
        synced = _invoke(
            ["sync", "--profile=test-comprehensive", f"--config={fixture_repo}"]
        )
        assert synced.exit_code == 0, synced.output
        list_calls_after = sum(1 for c in fk.calls if c[1:] == ["--list-extensions"])
        assert list_calls_after > list_calls_before


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
        fk = fake_code()

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
        fk = fake_code()  # starts empty — install will add editorconfig.

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
            ]
        )
        assert reverted.exit_code == 0, reverted.output
        # The install delta recorded `added: [editorconfig.editorconfig]`;
        # revert inverts that into an uninstall call.
        assert fk.uninstall_args() == ["editorconfig.editorconfig"]
        assert fk.installed_set() == set()


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


# ---------------------------------------------------------------------------
# --verbose/-v flag + MY_SETUP_LOG_LEVEL env var (tracked_files-58x)
# ---------------------------------------------------------------------------


class TestVerbosity:
    """``-v`` / ``--verbose`` and ``MY_SETUP_LOG_LEVEL`` wire the root logger.

    Precedence: flag > env > WARNING default. Garbage env values fall back
    to WARNING silently. The root ``_root`` callback calls
    ``logging.basicConfig(force=True, ...)`` so each invocation
    re-initializes the handlers cleanly across tests.
    """

    def test_root_v_flag_enables_debug_stderr(self, fixture_repo: Path) -> None:
        result = _invoke(["-v", "validate", "--all", f"--config={fixture_repo}"])
        assert result.exit_code == 0, result.output
        assert "setforge.cli DEBUG: logging configured at level" in result.stderr

    def test_env_var_enables_debug_when_flag_absent(
        self,
        fixture_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MY_SETUP_LOG_LEVEL", "DEBUG")
        result = _invoke(["validate", "--all", f"--config={fixture_repo}"])
        assert result.exit_code == 0, result.output
        assert "setforge.cli DEBUG: logging configured at level" in result.stderr

    def test_garbage_env_var_falls_back_to_warning(
        self,
        fixture_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MY_SETUP_LOG_LEVEL", "not-a-level")
        result = _invoke(["validate", "--all", f"--config={fixture_repo}"])
        assert result.exit_code == 0, result.output
        assert "setforge.cli DEBUG: logging configured at level" not in result.stderr

    def test_flag_overrides_env(
        self,
        fixture_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MY_SETUP_LOG_LEVEL", "WARNING")
        result = _invoke(["-v", "validate", "--all", f"--config={fixture_repo}"])
        assert result.exit_code == 0, result.output
        assert "setforge.cli DEBUG: logging configured at level" in result.stderr

    def test_garbage_my_setup_log_level_emits_stderr_warning(
        self,
        fixture_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MY_SETUP_LOG_LEVEL", "DEBGU")
        result = _invoke(["validate", "--all", f"--config={fixture_repo}"])
        assert result.exit_code == 0, result.output
        assert "unknown MY_SETUP_LOG_LEVEL='DEBGU'" in result.stderr
        assert "defaulting to WARNING" in result.stderr

    def test_garbage_my_setup_log_level_with_non_level_module_attr_warns(
        self,
        fixture_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``MY_SETUP_LOG_LEVEL=BASIC_FORMAT`` resolves to a ``logging`` str attr.

        A bare ``getattr(logging, env_value.upper(), None) is None`` check
        accepts ``logging.BASIC_FORMAT`` (a non-None string) and then
        ``basicConfig(level=<str>)`` interprets the format string as a
        level name and crashes opaquely. The ``isinstance(resolved, int)``
        guard surfaces the same friendly stderr warning the typo path emits.
        """
        monkeypatch.setenv("MY_SETUP_LOG_LEVEL", "BASIC_FORMAT")
        result = _invoke(["validate", "--all", f"--config={fixture_repo}"])
        assert result.exit_code == 0, result.output
        assert "unknown MY_SETUP_LOG_LEVEL='BASIC_FORMAT'" in result.stderr
        assert "defaulting to WARNING" in result.stderr
