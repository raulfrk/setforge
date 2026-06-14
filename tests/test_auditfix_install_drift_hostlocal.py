"""Regression: install's pre-deploy drift report must be overlay-aware.

The standalone ``setforge compare`` threads the validated
``host_local_sections`` overlay into :func:`compare_profile` so a live file
that already received its injected host-local sections does NOT surface as
spurious drift. The ``install`` pre-deploy drift gate did NOT — it called
``compare_profile`` with no ``host_local_sections=`` (effective ``None``), so
for any tracked file using legacy ``host_local_sections`` marker injection the
drift report (which feeds the section-reconcile gate) over-reported drift.

The fix threads the already-loaded, validated overlay
(``_load_validated_host_local_sections`` at install.py) into the drift-gate
``compare_profile`` call. This test pins that wiring: it stubs the loader to
return a sentinel overlay and asserts the drift gate forwards EXACTLY that
sentinel — failing on the old overlay-blind call.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

import setforge.cli._install_helpers as install_helpers_mod
import setforge.cli.install as install_mod
from setforge.cli import app
from setforge.compare import CompareReport
from setforge.config import Config
from setforge.source import AnchorAfterHeading, HostLocalSection, HostLocalSectionName

HostLocalOverlay = Mapping[str, dict[HostLocalSectionName, HostLocalSection]]

_PROFILE = "drift-test"

_DOC = """\
# Title

## Notes

upstream notes body
"""

_SENTINEL_OVERLAY: dict[str, dict[HostLocalSectionName, HostLocalSection]] = {
    "doc": {
        HostLocalSectionName("preexisting"): HostLocalSection(
            anchor=AnchorAfterHeading(value="Notes"),
            body="PRE-EXISTING HOST BODY\n",
        )
    }
}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    (target / "tracked").mkdir(parents=True)
    (target / "tracked" / "doc.md").write_text(_DOC, encoding="utf-8")
    return target


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.setforge_seed/doc.md\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - doc\n",
        encoding="utf-8",
    )
    return config


def _invoke(config: Path, *extra: str) -> Result:
    return CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--no-git-check",
            "--yes",
            "--no-secrets-scan",
            "--no-transition",
            *extra,
        ],
    )


def test_install_drift_gate_threads_host_local_overlay(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-deploy drift gate must forward the validated host-local overlay.

    Old behavior: ``compare_profile(cfg, profile, repo_root)`` with no
    ``host_local_sections=`` (effective ``None``) — the injected markers
    surface as spurious drift. New behavior: the validated map is threaded
    through, matching ``setforge compare``.
    """
    config = _write_config(repo)

    # Stub the validated-overlay loader so the install path observes a known
    # non-empty overlay regardless of host local.yaml state.
    monkeypatch.setattr(
        install_mod,
        "_load_validated_host_local_sections",
        lambda *_a, **_kw: _SENTINEL_OVERLAY,
    )

    captured: dict[str, object] = {}
    real_compare_profile = install_mod.compare_mod.compare_profile

    def _spy(
        config: Config,
        profile_name: str,
        repo_root: Path,
        *,
        host_local_sections: HostLocalOverlay | None = None,
    ) -> CompareReport:
        # Record only the FIRST call (the install drift gate at install.py).
        captured.setdefault("host_local_sections", host_local_sections)
        return real_compare_profile(
            config,
            profile_name,
            repo_root,
            host_local_sections=host_local_sections,
        )

    monkeypatch.setattr(install_mod.compare_mod, "compare_profile", _spy)

    result = _invoke(config)
    assert result.exit_code == 0, result.output

    overlay = captured.get("host_local_sections")
    # Old (buggy) code passed nothing -> None. The fix forwards the exact
    # validated map the loader returned.
    assert overlay is not None, (
        "install drift gate called compare_profile WITHOUT the host-local "
        "overlay (overlay-blind) — would over-report injected-marker drift"
    )
    assert overlay is _SENTINEL_OVERLAY, (
        f"drift gate must forward the validated overlay verbatim, got: {overlay!r}"
    )


def test_dry_run_pipeline_threads_host_local_overlay(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dry-run / fresh-host welcome preview must be overlay-aware too.

    ``setforge install --dry-run`` (and, via ``_welcome_dry_run`` ->
    ``_dry_run_pipeline``, the fresh-host welcome preview) route their drift
    compare through :func:`_dry_run_pipeline`. That call previously omitted
    ``host_local_sections=``, so the preview the user consents against
    over-reported injected-marker drift. The fix loads the same validated
    overlay inside ``_dry_run_pipeline`` and threads it through verbatim.
    """
    config = _write_config(repo)

    # Stub the validated-overlay loader IN THE DRY-RUN MODULE so the pipeline
    # observes a known non-empty overlay regardless of host local.yaml state.
    monkeypatch.setattr(
        install_helpers_mod,
        "_load_validated_host_local_sections",
        lambda *_a, **_kw: _SENTINEL_OVERLAY,
    )

    captured: dict[str, object] = {}
    real_compare_profile = install_helpers_mod.compare_mod.compare_profile

    def _spy(
        config: Config,
        profile_name: str,
        repo_root: Path,
        *,
        host_local_sections: HostLocalOverlay | None = None,
    ) -> CompareReport:
        captured.setdefault("host_local_sections", host_local_sections)
        return real_compare_profile(
            config,
            profile_name,
            repo_root,
            host_local_sections=host_local_sections,
        )

    monkeypatch.setattr(install_helpers_mod.compare_mod, "compare_profile", _spy)

    result = _invoke(config, "--dry-run")
    assert result.exit_code == 0, result.output

    overlay = captured.get("host_local_sections")
    assert overlay is not None, (
        "dry-run pipeline called compare_profile WITHOUT the host-local "
        "overlay (overlay-blind) — preview would over-report injected-marker "
        "drift the user consents against"
    )
    assert overlay is _SENTINEL_OVERLAY, (
        "dry-run pipeline must forward the validated overlay verbatim, got: "
        f"{overlay!r}"
    )
