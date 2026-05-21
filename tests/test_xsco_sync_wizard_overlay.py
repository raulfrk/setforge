"""Sync-wizard overlay regression tests (setforge-xsco round-2).

Regression coverage for the Round-2 IMPORTANT finding that
``setforge merge`` (the standalone wizard) and ``setforge sync
--auto=use-live`` (the non-interactive confirm panel) historically
called :func:`setforge.compare.compare_profile` WITHOUT threading the
local.yaml ``host_local_sections`` overlay, so already-injected
host-local sections surfaced as DRIFT in the wizard's display and
confirm-panel counts — a false positive even though the
``f1ba8c0`` capture filter prevented the write-side leak.

These tests assert the post-fix shape: both sync.py call sites pass
``host_local_sections=`` to ``compare_profile`` and the wizard's drift
display excludes injected host-local sections.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import compare as compare_mod
from setforge.cli import app
from setforge.config import load_config, resolve_profile
from setforge.deploy import copy_atomic
from setforge.source import AnchorAfterHeading, HostLocalSection


@pytest.fixture
def overlay_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Build a one-tracked_file profile + local.yaml overlay.

    Deploys the host-local section to ``dst`` so the next compare/sync
    invocation sees a live file that already received its injection — the
    state in which the wizard's drift display historically false-positived.
    """
    src = tmp_path / "tracked" / "section.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Title\n\n## Workflow\n\nbody\n", encoding="utf-8")
    dst = tmp_path / "live" / "section.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "setforge.yaml"
    cfg_path.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  xsco_md:\n"
        "    src: section.md\n"
        f"    dst: {dst}\n"
        "    preserve_user_sections: true\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [xsco_md]\n",
        encoding="utf-8",
    )

    # Deploy the host-local section so dst is in the post-install state.
    host_local = {
        "work-overrides": HostLocalSection(
            anchor=AnchorAfterHeading(value="Workflow"),
            body="WORK OVERRIDES",
        )
    }
    copy_atomic(src, dst, preserve_user_sections=True, host_local_sections=host_local)

    # Stub load_local_host_local_sections to return our test overlay so the
    # CLI's _load_validated_host_local_sections (which calls this loader
    # with no explicit path) picks up the test fixture. Monkeypatching the
    # LOCAL_CONFIG_PATH module-level constant does not help because the
    # default-arg binding captures it at function-def time.
    overlay = {
        "xsco_md": {
            "work-overrides": HostLocalSection(
                anchor=AnchorAfterHeading(value="Workflow"), body="WORK OVERRIDES"
            )
        }
    }
    monkeypatch.setattr(
        "setforge.cli._install_helpers.load_local_host_local_sections",
        lambda: overlay,
    )
    # Stub state-dir / transition writes so the CLI invocation is read-only.
    monkeypatch.setattr("setforge.transitions.ensure_state_dir_writable", lambda: None)

    return {"cfg": cfg_path, "src": src, "dst": dst}


def test_sync_wizard_excludes_injected_host_local_from_drift_display(
    overlay_fixture: dict[str, Path],
) -> None:
    """Post-install live file with a host-local injection: no unexpected drift.

    Asserts the contract sync.py (merge wizard + --auto=use-live confirm)
    relies on: ``compare_profile`` threaded with ``host_local_sections=``
    masks injected sections so the wizard's drift display does not
    surface them.
    """
    from setforge.cli._install_helpers import _load_validated_host_local_sections

    cfg = load_config(overlay_fixture["cfg"])
    repo_root = overlay_fixture["cfg"].parent
    resolved = resolve_profile(cfg, "p")
    host_local_sections_map = _load_validated_host_local_sections(
        cfg, resolved, repo_root
    )
    # Sanity: overlay was loaded.
    assert "xsco_md" in host_local_sections_map
    assert "work-overrides" in host_local_sections_map["xsco_md"]

    # Overlay-aware compare: no drift.
    report_with = compare_mod.compare_profile(
        cfg, "p", repo_root, host_local_sections=host_local_sections_map
    )
    assert not report_with.has_unexpected_drift
    for entry in report_with.entries:
        # No diff body for the injected file.
        assert "work-overrides" not in entry.diff

    # Sanity-check the other arm: WITHOUT the overlay (the pre-fix path),
    # the same setup surfaces the injected section as drift. Confirms the
    # mask is overlay-driven, not unconditional.
    report_without = compare_mod.compare_profile(cfg, "p", repo_root)
    drifted = [e for e in report_without.entries if e.diff]
    assert drifted, "pre-fix path should still surface drift without overlay"
    assert any("work-overrides" in e.diff for e in drifted)


def test_merge_command_reports_no_unexpected_drift_when_overlay_threaded(
    overlay_fixture: dict[str, Path],
) -> None:
    """End-to-end via the CLI: ``setforge merge`` exits 0 with the no-drift message.

    Pre-fix (without the overlay threaded at sync.py:161): merge would
    surface the injected host-local section as unexpected drift and
    enter the wizard. Post-fix: merge exits 0 with "no unexpected
    drift; nothing to do."
    """
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "merge",
            "--profile=p",
            f"--config={overlay_fixture['cfg']}",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "no unexpected drift" in result.output
