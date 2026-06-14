"""Docker E2E: standalone ``plugin reconcile`` exit-code / dry-run / no-op contract.

Closes the e2e-coverage-gap flagged by the audit: ``setforge plugin
reconcile`` is a user-facing command distinct from the reconcile phase
embedded in ``install``, with its own exit-code contract:

- exit 1 when policy is REPORT or ``--dry-run`` AND drift remains;
- exit 1 when a live run has failed actions;
- otherwise print ``plugins: nothing to reconcile`` and exit 0.

The sibling ``ext reconcile`` command is fully e2e-covered
(``test_e2e_docker_auditfix_ext_e2e.py``), but ``plugin reconcile`` was
only exercised implicitly through ``install``/dry-run and unit tests —
so a regression in the standalone command's exit code or no-op messaging
would slip past the canonical e2e gate. These tests mirror the
ext-reconcile cases.

Read-only modes (REPORT policy / ``--dry-run``) compute drift WITHOUT
shelling to ``claude plugin install``, so the drift cases need no real
marketplace fetch — the github source can point at a local bare repo and
is never actually installed. The no-op case declares no plugins at all.

Self-contained: each test writes its own config repo under
``_SRC_REPO`` and points the source layer at it via ``local.yaml``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

_SRC_REPO = "/tmp/cfg-plugin-reconcile"
_HOME_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"
_PROFILE = "base"
_BARE_REPO = "/tmp/mp-reconcile-origin.git"

# A profile with NO declared plugins → reconcile is a clean no-op.
_NO_PLUGINS_YAML = """\
version: 1
schema_version: '1.0'
tracked_files:
  foo:
    src: foo.md
    dst: /tmp/out/foo.md
profiles:
  base:
    tracked_files:
      - foo
"""

# A profile that declares one plugin under a (report) reconcile policy. The
# marketplace `repo` is a local bare repo so config validation passes; the
# plugin is never actually installed, so a read-only reconcile sees drift.
_REPORT_DRIFT_YAML = f"""\
version: 1
schema_version: '1.0'
tracked_files:
  foo:
    src: foo.md
    dst: /tmp/out/foo.md
marketplaces:
  fixture-mp:
    source: github
    repo: {_BARE_REPO}
claude_plugins:
  some-plugin:
    marketplace: fixture-mp
profiles:
  base:
    plugins_reconcile: report
    tracked_files:
      - foo
    claude_plugins:
      - some-plugin
"""

# Same drift, but default (additive) policy — combined with ``--dry-run``
# the run is still read-only and must exit 1 on drift without installing.
_ADDITIVE_DRIFT_YAML = f"""\
version: 1
schema_version: '1.0'
tracked_files:
  foo:
    src: foo.md
    dst: /tmp/out/foo.md
marketplaces:
  fixture-mp:
    source: github
    repo: {_BARE_REPO}
claude_plugins:
  some-plugin:
    marketplace: fixture-mp
profiles:
  base:
    tracked_files:
      - foo
    claude_plugins:
      - some-plugin
"""


def _point_source_at(c: ContainerHandle) -> None:
    """Point local.yaml's source layer at ``_SRC_REPO`` (regular install mode)."""
    c.write_text(_HOME_LOCAL_YAML, f"source:\n  kind: path\n  path: {_SRC_REPO}\n")


def _write_config(c: ContainerHandle, *, body: str) -> None:
    """Write a minimal config repo (setforge.yaml + a tracked file)."""
    c.write_text(f"{_SRC_REPO}/setforge.yaml", body)
    c.write_text(f"{_SRC_REPO}/tracked/foo.md", "# foo\n")


def _make_bare_marketplace_repo(c: ContainerHandle) -> None:
    """Create a local bare git repo at ``_BARE_REPO`` to stand in for a github mp.

    Only needed so the github ``MarketplaceSource.repo`` resolves to a real
    path during config load; the plugin is never installed in these read-only
    reconcile cases.
    """
    seed = "/tmp/mp-reconcile-seed"
    script = (
        f"set -e; "
        f"rm -rf {seed} {_BARE_REPO}; "
        f"git init -q {seed}; "
        f"cd {seed}; "
        f"git config user.email t@e.x; git config user.name t; "
        f"echo manifest > marketplace.json; "
        f"git add -A; git commit -q -m seed; "
        f"git clone -q --bare {seed} {_BARE_REPO}"
    )
    c.exec(["sh", "-c", script], check=True)


def _reconcile(c: ContainerHandle, *args: str):
    return c.exec(
        [
            "uv",
            "run",
            "setforge",
            "plugin",
            "reconcile",
            *args,
            f"--profile={_PROFILE}",
        ],
        check=False,
    )


@pytest.mark.xdist_group("docker_daemon")
def test_plugin_reconcile_nothing_to_reconcile_exits_zero(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A profile with no declared plugins → 'nothing to reconcile' + exit 0."""
    c = docker_container()
    _write_config(c, body=_NO_PLUGINS_YAML)
    _point_source_at(c)

    res = _reconcile(c)
    combined = res.stdout + res.stderr
    assert res.returncode == 0, combined
    assert "plugins: nothing to reconcile" in res.stdout, combined


@pytest.mark.xdist_group("docker_daemon")
def test_plugin_reconcile_dry_run_exits_1_on_drift(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``plugin reconcile --dry-run`` exits 1 on drift without invoking claude.

    A declared-but-uninstalled plugin under the default (additive) policy:
    ``--dry-run`` forces read-only behavior, so the run prints the
    ``would install`` verb and exits 1, never calling ``claude plugin
    install``.
    """
    c = docker_container()
    _make_bare_marketplace_repo(c)
    _write_config(c, body=_ADDITIVE_DRIFT_YAML)
    _point_source_at(c)

    res = _reconcile(c, "--dry-run")
    combined = res.stdout + res.stderr
    assert res.returncode == 1, combined
    assert "would install" in res.stdout, combined
    # Dry-run is read-only: no past-tense "installed" action line.
    assert "nothing to reconcile" not in res.stdout, combined


@pytest.mark.xdist_group("docker_daemon")
def test_plugin_reconcile_report_policy_exits_1_on_drift(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Standalone ``plugin reconcile`` under REPORT policy exits 1 on drift.

    REPORT is read-only: it computes the diff, runs no ``claude``
    invocation, and exits non-zero when drift remains so CI can gate on it.
    """
    c = docker_container()
    _make_bare_marketplace_repo(c)
    _write_config(c, body=_REPORT_DRIFT_YAML)
    _point_source_at(c)

    res = _reconcile(c)
    combined = res.stdout + res.stderr
    assert res.returncode == 1, combined
    assert "would install" in res.stdout, combined
