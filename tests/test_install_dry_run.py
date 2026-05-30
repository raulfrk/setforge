"""Unit tests for ``setforge install --dry-run``.

Eight tests, one per acceptance criterion from SPEC 4:

- ``test_no_write_transition`` — dry-run never calls
  :func:`transitions.write_transition`.
- ``test_no_stamp_baseline`` — dry-run never calls
  :func:`section_reconcile.stamp_tracked_baseline`.
- ``test_no_allowlist_mutation`` — dry-run never calls
  :func:`secrets.append_to_allowlist` (the secrets scan path is
  unreachable under dry-run).
- ``test_no_bootstrap_local`` — dry-run never calls
  :func:`deploy.bootstrap_local`.
- ``test_no_ensure_state_dir`` — dry-run never calls
  :func:`transitions.ensure_state_dir_writable`.
- ``test_auto_use_tracked_no_confirm_under_dry_run`` /
  ``test_auto_use_live_no_confirm_under_dry_run`` — dry-run
  short-circuits BEFORE both ``confirm_auto_operation`` call sites
  (legacy unexpected-drift confirm + section-reconcile confirm).
- ``test_no_git_fetch_under_dry_run`` — dry-run does not invoke
  ``git fetch`` (the source-layer git check runs BEFORE the dry-run
  branch but is itself read-only; this anchors that subprocess.run
  is never called with ``["git", "fetch", ...]`` argv).

Each test monkeypatches the mutating leaf with a tripwire that records
calls; the assertion is on the tripwire's call count, not on the
absence of side effects (mock-then-assert pattern). Fixtures sandbox
``$HOME`` to a tmp dir + redirect ``SETFORGE_STATE_DIR`` so a leaked
mutation surfaces as a tmp-dir artifact, not a real one.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "e2e"
_FIXTURE_YAML = _FIXTURE_DIR / "setforge.test.yaml"
_FIXTURE_TRACKED = _FIXTURE_DIR / "tracked"


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """Copy the e2e fixture repo into ``tmp_path`` so tests can mutate freely.

    Returns the path to the copied ``setforge.test.yaml``. The
    accompanying ``tracked/`` tree sits beside it so ``resolve_src``
    finds the tracked files at ``<config_parent>/tracked/<src>``.
    """
    target = tmp_path / "repo"
    target.mkdir()
    shutil.copy2(_FIXTURE_YAML, target / "setforge.test.yaml")
    shutil.copytree(_FIXTURE_TRACKED, target / "tracked")
    return target / "setforge.test.yaml"


@pytest.fixture
def sandboxed_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``$HOME`` + ``SETFORGE_STATE_DIR`` to tmp so dst paths are sandboxed.

    Any leaked mutation lands under ``tmp_path`` rather than the real
    home directory — the per-test cleanup still applies, but the
    sandbox makes leaks immediately visible via tmp_path inspection.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    return home


@pytest.fixture
def no_external_bins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out ``code`` and ``claude`` binary resolution so reconcilers no-op.

    The dry-run pipeline catches :class:`ExtensionToolMissing` and
    :class:`PluginToolMissing` from the read-only reconcile calls and
    emits a ``skipped (... tool unavailable: ...)`` line. Stubbing both
    binaries to absent keeps the unit tests hermetic against the host's
    installed tooling.
    """
    monkeypatch.setattr(
        "setforge.vscode_extensions.resolve_binary",
        lambda name: None,
    )
    from setforge import claude_plugins as cp

    cp._get_claude_bin.cache_clear()
    monkeypatch.setattr(
        "setforge.claude_plugins.resolve_binary",
        lambda name: None,
    )


def _invoke_dry_run(
    fixture_repo: Path,
    *,
    profile: str = "test-minimal",
    extra: list[str] | None = None,
) -> None:
    """Run ``setforge install --dry-run`` against ``fixture_repo``; assert exit 0.

    Shared helper so every test reads the same intent: invoke dry-run
    on a fixture profile, assert the run completes cleanly, leave the
    tripwire's call-count assertion to the caller.
    """
    args = [
        "install",
        f"--profile={profile}",
        f"--config={fixture_repo}",
        "--dry-run",
        "--no-git-check",
    ]
    if extra:
        args.extend(extra)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Tripwire tests — each asserts a specific mutating leaf is unreachable.
# ---------------------------------------------------------------------------


def test_no_write_transition(
    fixture_repo: Path,
    sandboxed_home: Path,
    no_external_bins: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` never calls :func:`transitions.write_transition`."""
    calls: list[tuple[object, ...]] = []

    def tripwire(*args: object, **kwargs: object) -> Path:
        calls.append(args)
        raise AssertionError(
            f"write_transition called under --dry-run: args={args!r} kwargs={kwargs!r}"
        )

    monkeypatch.setattr("setforge.transitions.write_transition", tripwire)
    _invoke_dry_run(fixture_repo)
    assert calls == []


def test_no_stamp_baseline(
    fixture_repo: Path,
    sandboxed_home: Path,
    no_external_bins: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` never calls :func:`section_reconcile.stamp_tracked_baseline`."""
    calls: list[Path] = []

    def tripwire(path: Path) -> None:
        calls.append(path)
        raise AssertionError(
            f"stamp_tracked_baseline called under --dry-run: path={path}"
        )

    monkeypatch.setattr("setforge.section_reconcile.stamp_tracked_baseline", tripwire)
    _invoke_dry_run(fixture_repo, profile="test-text-sections")
    assert calls == []


def test_no_allowlist_mutation(
    fixture_repo: Path,
    sandboxed_home: Path,
    no_external_bins: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` never calls :func:`secrets.append_to_allowlist`.

    The secrets scan path itself (``run_pre_deploy_scan``) is
    unreachable under dry-run — the dry-run pipeline never invokes it.
    The tripwire here is a belt-and-suspenders check on the mutating
    sink that the scan path would otherwise hit.
    """
    calls: list[tuple[str, Path]] = []

    def tripwire(*, snippet_hash: str, allowlist_path: Path) -> None:
        calls.append((snippet_hash, allowlist_path))
        raise AssertionError(
            f"append_to_allowlist called under --dry-run: hash={snippet_hash}"
        )

    monkeypatch.setattr("setforge.secrets.append_to_allowlist", tripwire)
    _invoke_dry_run(fixture_repo)
    assert calls == []


def test_no_bootstrap_local(
    fixture_repo: Path,
    sandboxed_home: Path,
    no_external_bins: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` never calls :func:`deploy.bootstrap_local`."""
    calls: list[list[Path]] = []

    def tripwire(paths: list[Path]) -> None:
        calls.append(paths)
        raise AssertionError(f"bootstrap_local called under --dry-run: paths={paths!r}")

    monkeypatch.setattr("setforge.deploy.bootstrap_local", tripwire)
    # ``test-chain-child`` declares a bootstrap path; the real install
    # would call ``bootstrap_local`` for each ancestor's bootstrap
    # list. Under dry-run the tripwire MUST stay silent.
    _invoke_dry_run(fixture_repo, profile="test-chain-child")
    assert calls == []


def test_no_ensure_state_dir(
    fixture_repo: Path,
    sandboxed_home: Path,
    no_external_bins: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` never calls :func:`transitions.ensure_state_dir_writable`."""
    calls: list[None] = []

    def tripwire() -> None:
        calls.append(None)
        raise AssertionError("ensure_state_dir_writable called under --dry-run")

    monkeypatch.setattr("setforge.transitions.ensure_state_dir_writable", tripwire)
    _invoke_dry_run(fixture_repo)
    assert calls == []


def test_auto_use_tracked_no_confirm_under_dry_run(
    fixture_repo: Path,
    sandboxed_home: Path,
    no_external_bins: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--auto=use-tracked --dry-run`` short-circuits before the section confirm.

    Per spec anti-pattern #5: the auto-confirm ``confirm_auto_operation``
    wizard MUST NOT fire under ``--auto=*`` + ``--dry-run``. The two
    call sites in :mod:`setforge.cli._install_helpers` live inside
    ``_run_predeploy_gates``, which the dry-run pipeline does not
    invoke — the tripwire here anchors that invariant by patching the
    confirm function itself.
    """
    calls: list[tuple[object, ...]] = []

    def tripwire(*args: object, **kwargs: object) -> bool:
        calls.append(args)
        raise AssertionError(
            f"confirm_auto_operation called under --dry-run + --auto: kwargs={kwargs!r}"
        )

    monkeypatch.setattr(
        "setforge.cli._install_helpers.confirm_auto_operation", tripwire
    )
    _invoke_dry_run(
        fixture_repo,
        profile="test-reconcile-sections",
        extra=["--auto=use-tracked", "--yes"],
    )
    assert calls == []


def test_auto_use_live_no_confirm_under_dry_run(
    fixture_repo: Path,
    sandboxed_home: Path,
    no_external_bins: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--auto-accept-live --dry-run`` short-circuits before the legacy confirm.

    The legacy unexpected-drift confirm is the OTHER
    ``confirm_auto_operation`` call site (``_confirm_legacy_drift_or_exit``).
    Same tripwire shape as the section-reconcile variant; runs against
    a profile whose live tree has no unexpected drift so the confirm
    would short-circuit at the no-drift gate even without dry-run —
    but the dry-run path skips ``_run_predeploy_gates`` entirely.
    """
    calls: list[tuple[object, ...]] = []

    def tripwire(*args: object, **kwargs: object) -> bool:
        calls.append(args)
        raise AssertionError(
            f"confirm_auto_operation called under --dry-run + "
            f"--auto-accept-live: kwargs={kwargs!r}"
        )

    monkeypatch.setattr(
        "setforge.cli._install_helpers.confirm_auto_operation", tripwire
    )
    _invoke_dry_run(
        fixture_repo,
        profile="test-minimal",
        extra=["--auto-accept-live", "--yes"],
    )
    assert calls == []


@pytest.fixture
def capture_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> list[list[str]]:
    """Intercept ``subprocess.run`` from every transitions/secrets caller.

    Records the argv of every subprocess invocation so a test can
    assert that ``git fetch`` (or any other mutating subprocess) is
    never issued under ``--dry-run``. Wraps the real
    :func:`subprocess.run` so transitively-needed read calls still
    succeed (the source-layer git check itself runs a couple of
    ``git rev-parse`` / ``git status`` shape commands before the
    dry-run branch).
    """
    seen: list[list[str]] = []
    real_run = subprocess.run

    def spy(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args:
            argv = args[0]
            if isinstance(argv, list):
                seen.append([str(x) for x in argv])
        # The spy passes through to the real ``subprocess.run`` so
        # transitively-needed read calls (rev-parse / status) still
        # succeed; mypy can't narrow the ``object`` spread through
        # the overload set.
        return real_run(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr("setforge.transitions.subprocess.run", spy)
    monkeypatch.setattr("setforge.secrets.subprocess.run", spy)
    return seen


def test_no_git_fetch_under_dry_run(
    fixture_repo: Path,
    sandboxed_home: Path,
    no_external_bins: None,
    capture_subprocess: list[list[str]],
) -> None:
    """``--dry-run`` does not issue ``git fetch`` against the source layer.

    Per spec open-decision Q5 (default proposal: skip fetch — dry-run
    should be fully local). The source-layer git-status check that
    runs before the dry-run branch is itself read-only (rev-parse /
    status), so the absence of any ``git fetch`` argv in the captured
    subprocess argv list is the load-bearing invariant.
    """
    _invoke_dry_run(fixture_repo)
    fetches = [argv for argv in capture_subprocess if "fetch" in argv]
    assert fetches == [], f"unexpected git fetch under --dry-run: {fetches!r}"
