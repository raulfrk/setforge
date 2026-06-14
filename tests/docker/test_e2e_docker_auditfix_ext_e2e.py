"""Docker E2E: ``ext add`` / ``ext remove`` / standalone ``ext reconcile``.

Pre-audit, only ``ext list`` had any e2e coverage
(``test_e2e_docker_source_layer.py``). The mutating ext commands are
exactly the integration-emergent surface the Docker suite exists to guard:

- ``ext add`` rewrites ``setforge.yaml`` (``extensions.include`` via
  ruamel round-trip) AND shells out to the real ``code
  --install-extension``.
- ``ext remove`` rewrites the include / exclude lists.
- standalone ``ext reconcile`` (non-dry-run) actually installs /
  uninstalls via the real ``code`` binary and carries distinct exit-code
  logic: read-only modes (REPORT policy or ``--dry-run``) exit 1 on any
  remaining drift; a live run exits 1 only on failed actions.

The e2e image ships a real ``code`` binary (pinned VSCode from the
Microsoft apt repo), so these tests exercise the true YAML round-trip +
real-binary-invocation + exit-code paths a CliRunner unit test cannot.

Self-contained: each test writes its own config repo under ``/tmp/cfg``
and passes it via ``--config`` so the ``ext add`` / ``ext remove``
mutations never touch the shared baked-in fixture tree.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

_CFG_REPO = "/tmp/cfg"
_CFG_YAML = f"{_CFG_REPO}/setforge.yaml"

# A small, real marketplace extension that installs quickly via
# `code --install-extension`. Used as the add/reconcile target so the
# real binary path is exercised end-to-end.
_EXT_ID = "editorconfig.editorconfig"


def _write_config(c: ContainerHandle, *, reconcile: str | None = None) -> None:
    """Write a minimal config repo at ``_CFG_REPO`` with a ``base`` profile.

    When ``reconcile`` is given, the profile's ``extensions`` block
    declares that ``reconcile:`` policy (e.g. ``report``) so the standalone
    ``ext reconcile`` exit-code branches can be exercised.
    """
    ext_block = "    extensions:\n      include: []\n"
    if reconcile is not None:
        ext_block = (
            "    extensions:\n"
            f"      reconcile: {reconcile}\n"
            "      include:\n"
            f"        - {_EXT_ID}\n"
        )
    c.write_text(
        _CFG_YAML,
        "version: 1\nschema_version: '1.0'\n"
        "tracked_files: {}\n"
        "profiles:\n  base:\n" + ext_block,
    )


def _ext(
    c: ContainerHandle, *args: str, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run ``setforge ext <args> --profile=base --config=<cfg>``."""
    return c.exec(
        [
            "uv",
            "run",
            "setforge",
            "ext",
            *args,
            "--profile=base",
            f"--config={_CFG_YAML}",
        ],
        check=check,
    )


def _list_extensions(c: ContainerHandle) -> str:
    """Return ``code --list-extensions`` stdout (lowercased for matching)."""
    res = c.exec(["code", "--list-extensions"], check=False)
    return (res.stdout + res.stderr).lower()


def test_ext_add_writes_yaml_and_installs(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``ext add`` appends to ``extensions.include`` AND installs via ``code``."""
    c = docker_container()
    _write_config(c)

    res = _ext(c, "add", _EXT_ID)
    combined = res.stdout + res.stderr
    assert res.returncode == 0, combined
    # The id is now declared in the profile's include list.
    assert _EXT_ID in c.read_text(_CFG_YAML), combined
    assert f"added to base.extensions.include: {_EXT_ID}" in res.stdout, combined
    # The real `code` binary actually installed it.
    assert _EXT_ID.lower() in _list_extensions(c), combined


def test_ext_add_no_install_skips_code_invocation(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``ext add --no-install`` edits YAML but never shells to ``code``."""
    c = docker_container()
    _write_config(c)

    res = _ext(c, "add", _EXT_ID, "--no-install")
    combined = res.stdout + res.stderr
    assert res.returncode == 0, combined
    assert _EXT_ID in c.read_text(_CFG_YAML), combined
    # No install line emitted, and the extension is NOT installed.
    assert "installed" not in res.stdout, combined
    assert _EXT_ID.lower() not in _list_extensions(c), combined


def test_ext_remove_drops_from_include(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``ext remove`` deletes the id from the YAML include list."""
    c = docker_container()
    _write_config(c)

    # Add to YAML only (skip the slow real install — we only assert YAML).
    add = _ext(c, "add", _EXT_ID, "--no-install")
    assert add.returncode == 0, add.stdout + add.stderr
    assert _EXT_ID in c.read_text(_CFG_YAML)

    rm = _ext(c, "remove", _EXT_ID)
    combined = rm.stdout + rm.stderr
    assert rm.returncode == 0, combined
    assert f"updated base.extensions.include: {_EXT_ID}" in rm.stdout, combined
    # The include list no longer mentions the id.
    assert _EXT_ID not in c.read_text(_CFG_YAML), combined


def test_ext_remove_no_change_when_absent(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``ext remove`` on an undeclared id is a clean no-op (exit 0)."""
    c = docker_container()
    _write_config(c)

    rm = _ext(c, "remove", _EXT_ID)
    combined = rm.stdout + rm.stderr
    assert rm.returncode == 0, combined
    assert "no change" in rm.stdout, combined


def test_ext_reconcile_live_applies_and_installs(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Standalone live ``ext reconcile`` installs declared-but-missing ids.

    A default (ADDITIVE) policy with one declared, uninstalled extension
    must install it via the real ``code`` binary and exit 0.
    """
    c = docker_container()
    # ADDITIVE is the default reconcile policy; declare the ext in include.
    _write_config(c, reconcile="additive")
    assert _EXT_ID.lower() not in _list_extensions(c)

    res = _ext(c, "reconcile")
    combined = res.stdout + res.stderr
    assert res.returncode == 0, combined
    # Live run prints the bare verb (not "would install") and applies it.
    assert "install" in res.stdout, combined
    assert "would install" not in res.stdout, combined
    assert _EXT_ID.lower() in _list_extensions(c), combined

    # Second reconcile is a clean no-op once the ext is present.
    again = _ext(c, "reconcile")
    again_out = again.stdout + again.stderr
    assert again.returncode == 0, again_out
    assert "nothing to reconcile" in again.stdout, again_out


def test_ext_reconcile_report_policy_exits_1_on_drift(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Standalone ``ext reconcile`` under REPORT policy exits 1 on drift.

    REPORT is read-only: it computes the diff, runs no ``code``
    invocation, and exits non-zero when drift remains so CI can gate on it.
    """
    c = docker_container()
    _write_config(c, reconcile="report")
    assert _EXT_ID.lower() not in _list_extensions(c)

    res = _ext(c, "reconcile")
    combined = res.stdout + res.stderr
    # Read-only drift → exit 1, "would install" verb, NOT installed.
    assert res.returncode == 1, combined
    assert "would install" in res.stdout, combined
    assert _EXT_ID.lower() not in _list_extensions(c), combined


def test_ext_reconcile_dry_run_exits_1_on_drift(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``ext reconcile --dry-run`` exits 1 on drift without invoking ``code``."""
    c = docker_container()
    # ADDITIVE policy, but --dry-run forces read-only behavior.
    _write_config(c, reconcile="additive")
    assert _EXT_ID.lower() not in _list_extensions(c)

    res = _ext(c, "reconcile", "--dry-run")
    combined = res.stdout + res.stderr
    assert res.returncode == 1, combined
    assert "would install" in res.stdout, combined
    # Dry-run must not actually install.
    assert _EXT_ID.lower() not in _list_extensions(c), combined
