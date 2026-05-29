"""Exit-code contract tests for ``ext reconcile`` / ``plugin reconcile``.

These pin the live-mode (ADDITIVE/PRUNE) behavior: a reconcile run that
records action failures in ``report.failed`` must exit non-zero (and print a
FAILED line to stderr), while a live run with zero failures exits 0. The
read-only path (REPORT policy / ``--dry-run``) must still exit 1 on any drift
— a regression guard for the read-only semantics.

The reconcile entrypoint is monkeypatched to return a forced
:class:`ReconcileReport`, so the tests exercise the CLI's exit-code chain in
isolation from the real subprocess-backed reconcile. Config resolution and
loading are stubbed via ``monkeypatch`` (auto-reverted) so no real
``setforge.yaml`` is read; any config path passed is a no-op stub.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.config import ReconcilePolicy


def _stub_ext_config_layer(
    monkeypatch: pytest.MonkeyPatch, *, policy: ReconcilePolicy
) -> None:
    """Bypass source/config resolution for ``ext reconcile``.

    ``resolved.extensions`` carries the reconcile policy (used to compute
    ``is_read_only``) and is otherwise opaque (the real reconcile is patched
    per-test).
    """
    import setforge.cli.ext as ext_mod

    extensions = SimpleNamespace(reconcile=policy)
    resolved = SimpleNamespace(extensions=extensions)
    monkeypatch.setattr(ext_mod, "_resolve_config_arg", lambda c: c)
    monkeypatch.setattr(ext_mod, "load_config", lambda c: SimpleNamespace())
    monkeypatch.setattr(ext_mod, "resolve_profile", lambda cfg, profile: resolved)


def _stub_plugin_config_layer(
    monkeypatch: pytest.MonkeyPatch, *, policy: ReconcilePolicy
) -> None:
    """Bypass source/config resolution for ``plugin reconcile``.

    ``resolved.plugins_reconcile`` carries the policy used to compute
    ``is_read_only``.
    """
    import setforge.cli.plugins as plugins_mod

    resolved = SimpleNamespace(plugins_reconcile=policy)
    monkeypatch.setattr(plugins_mod, "_resolve_config_arg", lambda c: c)
    monkeypatch.setattr(plugins_mod, "load_config", lambda c: SimpleNamespace())
    monkeypatch.setattr(plugins_mod, "resolve_profile", lambda cfg, profile: resolved)


# ---------------------------------------------------------------------------
# ext reconcile
# ---------------------------------------------------------------------------


def test_ext_reconcile_live_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live (ADDITIVE) reconcile with a recorded action failure must exit
    non-zero and print a FAILED line to stderr — the bare exit-0 fall-through
    on live-mode failures is the regression being guarded."""
    import setforge.cli.ext as ext_mod
    from setforge.vscode_extensions import ReconcileReport

    _stub_ext_config_layer(monkeypatch, policy=ReconcilePolicy.ADDITIVE)
    report = ReconcileReport(
        policy=ReconcilePolicy.ADDITIVE,
        to_install=["pub.ext"],
        to_uninstall=[],
        dry_run=False,
        failed=[("pub.ext", "code --install-extension exited 1")],
    )
    monkeypatch.setattr(ext_mod.vscode_extensions, "reconcile", lambda *a, **k: report)

    result = CliRunner().invoke(app, ["ext", "reconcile", "--profile=x"])
    assert result.exit_code != 0
    assert "FAILED" in result.output
    assert "pub.ext" in result.output


def test_ext_reconcile_live_zero_failures_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live (ADDITIVE) reconcile that performed actions with no failures must
    still exit 0 — the success path must not be broken by guarding on report
    truthiness."""
    import setforge.cli.ext as ext_mod
    from setforge.vscode_extensions import ReconcileReport

    _stub_ext_config_layer(monkeypatch, policy=ReconcilePolicy.ADDITIVE)
    report = ReconcileReport(
        policy=ReconcilePolicy.ADDITIVE,
        to_install=["pub.ext"],
        to_uninstall=[],
        dry_run=False,
        failed=[],
    )
    monkeypatch.setattr(ext_mod.vscode_extensions, "reconcile", lambda *a, **k: report)

    result = CliRunner().invoke(app, ["ext", "reconcile", "--profile=x"])
    assert result.exit_code == 0, result.output
    assert "FAILED" not in result.output


def test_ext_reconcile_readonly_drift_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only (REPORT policy) reconcile with drift must still exit 1 —
    regression guard for the read-only branch the live-failure fix sits
    after."""
    import setforge.cli.ext as ext_mod
    from setforge.vscode_extensions import ReconcileReport

    _stub_ext_config_layer(monkeypatch, policy=ReconcilePolicy.REPORT)
    report = ReconcileReport(
        policy=ReconcilePolicy.REPORT,
        to_install=["pub.ext"],
        to_uninstall=[],
        dry_run=False,
        failed=[],
    )
    monkeypatch.setattr(ext_mod.vscode_extensions, "reconcile", lambda *a, **k: report)

    result = CliRunner().invoke(app, ["ext", "reconcile", "--profile=x"])
    assert result.exit_code == 1, result.output


# ---------------------------------------------------------------------------
# plugin reconcile
# ---------------------------------------------------------------------------


def test_plugin_reconcile_live_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live (ADDITIVE) plugin reconcile with a recorded action failure must
    exit non-zero and print a FAILED line to stderr."""
    import setforge.cli.plugins as plugins_mod
    from setforge.claude_plugins import ReconcileReport

    _stub_plugin_config_layer(monkeypatch, policy=ReconcilePolicy.ADDITIVE)
    report = ReconcileReport(
        to_install=[("plug", "mp")],
        to_enable=[],
        to_disable=[],
        marketplaces_added=[],
        dry_run=False,
        failed=[("plug@mp", "claude plugin install exited 1")],
    )
    monkeypatch.setattr(
        plugins_mod.claude_plugins_mod, "reconcile", lambda *a, **k: report
    )

    result = CliRunner().invoke(app, ["plugin", "reconcile", "--profile=x"])
    assert result.exit_code != 0
    assert "FAILED" in result.output
    assert "plug@mp" in result.output


def test_plugin_reconcile_live_zero_failures_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live (ADDITIVE) plugin reconcile with no failures must still exit 0."""
    import setforge.cli.plugins as plugins_mod
    from setforge.claude_plugins import ReconcileReport

    _stub_plugin_config_layer(monkeypatch, policy=ReconcilePolicy.ADDITIVE)
    report = ReconcileReport(
        to_install=[("plug", "mp")],
        to_enable=[],
        to_disable=[],
        marketplaces_added=[],
        dry_run=False,
        failed=[],
    )
    monkeypatch.setattr(
        plugins_mod.claude_plugins_mod, "reconcile", lambda *a, **k: report
    )

    result = CliRunner().invoke(app, ["plugin", "reconcile", "--profile=x"])
    assert result.exit_code == 0, result.output
    assert "FAILED" not in result.output


def test_plugin_reconcile_readonly_drift_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only (REPORT policy) plugin reconcile with drift must still exit
    1 — regression guard for the read-only branch."""
    import setforge.cli.plugins as plugins_mod
    from setforge.claude_plugins import ReconcileReport

    _stub_plugin_config_layer(monkeypatch, policy=ReconcilePolicy.REPORT)
    report = ReconcileReport(
        to_install=[("plug", "mp")],
        to_enable=[],
        to_disable=[],
        marketplaces_added=[],
        dry_run=False,
        failed=[],
    )
    monkeypatch.setattr(
        plugins_mod.claude_plugins_mod, "reconcile", lambda *a, **k: report
    )

    result = CliRunner().invoke(app, ["plugin", "reconcile", "--profile=x"])
    assert result.exit_code == 1, result.output
