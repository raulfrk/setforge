"""dg2a Q10 Option B — secrets-scan findings surface in the confirm panel.

The confirm panel renders gitleaks findings as a non-blocking warning
in the RISKS section; default=No still applies so a fat-finger Enter
aborts cleanly. These tests inject a forged
:class:`SecretsScanResult` with one finding and assert the rendered
panel carries the warning text + the rule_id + the findings count.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from setforge.secrets import SecretFinding, SecretsScanResult
from setforge.section_promote import (
    PromotePlan,
    _render_panel,
    build_promote_plan,
)
from setforge.source import (
    AnchorAfterHeading,
    AnchorKind,
    HostLocalSectionName,
)


def _finding(rule_id: str = "generic-api-key") -> SecretFinding:
    return SecretFinding(
        rule_id=rule_id,
        file_path=Path("/tmp/body.md"),
        line_number=3,
        snippet="sk-XXX-leaked",
        snippet_hash="a" * 64,
        secret_kind=rule_id,
    )


def _plan_with_secrets(findings: tuple[SecretFinding, ...]) -> PromotePlan:
    return PromotePlan(
        section_name=HostLocalSectionName("work-overrides"),
        local_yaml_path=Path("/tmp/local.yaml"),
        tracked_path=Path("/tmp/tracked.md"),
        live_path=Path("/tmp/live.md"),
        body="line1\nline2\nline3 sk-XXX-leaked\nline4\n",
        anchor=AnchorAfterHeading(kind=AnchorKind.AFTER_HEADING, value="Workflow"),
        revert_command="setforge revert --profile=demo",
        secrets=SecretsScanResult(findings=findings, files_scanned=1),
    )


def test_panel_renders_secrets_warning_when_findings_present() -> None:
    """When the secrets scan returned findings, the panel renders a RISKS warning."""
    plan = _plan_with_secrets((_finding(),))
    console = Console(record=True, width=120)
    _render_panel(plan, console=console)
    out = console.export_text()
    assert "GITLEAKS FOUND CANDIDATE SECRETS" in out
    assert "generic-api-key" in out
    # Default-no friction text still surfaces.
    assert "To undo" in out


def test_panel_renders_clean_when_no_findings() -> None:
    """When the secrets scan returned no findings, the panel says 'clean'."""
    plan = _plan_with_secrets(())
    console = Console(record=True, width=120)
    _render_panel(plan, console=console)
    out = console.export_text()
    assert "clean (gitleaks 0 findings)" in out
    assert "GITLEAKS FOUND CANDIDATE SECRETS" not in out


def test_build_promote_plan_routes_through_injected_scanner() -> None:
    """build_promote_plan calls the injected scanner exactly once on the body."""
    seen_bodies: list[str] = []

    def stub(body: str) -> SecretsScanResult:
        seen_bodies.append(body)
        return SecretsScanResult(findings=(_finding(),), files_scanned=1)

    plan = build_promote_plan(
        section_name=HostLocalSectionName("work-overrides"),
        local_yaml_path=Path("/tmp/local.yaml"),
        tracked_path=Path("/tmp/tracked.md"),
        live_path=Path("/tmp/live.md"),
        body="body bytes\n",
        anchor=AnchorAfterHeading(kind=AnchorKind.AFTER_HEADING, value="Workflow"),
        profile="demo",
        secrets_scanner=stub,
    )
    assert seen_bodies == ["body bytes\n"]
    assert len(plan.secrets.findings) == 1
    assert plan.secrets.findings[0].rule_id == "generic-api-key"
