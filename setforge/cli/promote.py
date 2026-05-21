"""``setforge promote-section`` CLI surface for the dg2a auto-promote feature.

Promotes a single host-local user-section (declared in
``~/.config/setforge/local.yaml`` ``host_local_sections``, setforge-xsco)
to a ``shared`` section in the tracked-file. The four-mutation atomic
procedure lives in :mod:`setforge.section_promote`; this module wires
the CLI inputs, the source-cleanliness pre-gate, the snapshot base
directory, and the transition record + revert hint that ``setforge
revert`` uses to roll the promote back.

Per spec dg2a:

* ``[p]`` keystroke gating is intent-only — execution requires the
  arrow-key ``radiolist_dialog`` confirm (default=No) rendered by
  :func:`setforge.section_promote.confirm_promote_to_shared`.
* The confirm panel runs the gitleaks pre-deploy scan on the captured
  body BEFORE the live-side rewrite; findings surface in the panel's
  RISKS block but do NOT block the user's choice (Q10 Option B).
* A single transition record (``TransitionCommand.PROMOTE``) covers
  all three mutated files (local.yaml, tracked-file, live-file) so
  ``setforge revert`` rolls the whole promote back via the standard
  ``patch -R`` reverse-diff machinery.
"""

from __future__ import annotations

import sys
from datetime import UTC
from pathlib import Path

import typer

from setforge import transitions
from setforge._redact import redact_argv
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.compare import resolve_dst, resolve_src
from setforge.config import load_config, resolve_profile
from setforge.errors import SetforgeError
from setforge.section_promote import (
    build_promote_plan,
    confirm_promote_to_shared,
    execute_promote_to_shared,
    offer_promote,
)
from setforge.sections import extract_sections
from setforge.source import (
    LOCAL_CONFIG_PATH,
    HostLocalSectionName,
    check_source_clean,
    load_local_host_local_sections,
)


@app.command("promote-section")
def promote_section(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    tracked_file: str = typer.Option(
        ...,
        "--tracked-file",
        help="tracked_file id whose host_local_sections entry is being promoted.",
    ),
    section: str = typer.Option(
        ...,
        "--section",
        help="Section name (must appear in local.yaml host_local_sections).",
    ),
    no_transition: bool = typer.Option(
        False,
        "--no-transition",
        hidden=True,
        help="Skip writing a transition record (testing / debugging).",
    ),
) -> None:
    """Promote one host-local section to shared (interactive, atomic).

    Drives the dg2a auto-promote flow for a single tracked_file +
    section pair. Requires the section to be declared in
    ``~/.config/setforge/local.yaml`` ``host_local_sections`` for
    ``tracked_file`` (anti-smell 10 — host-local-via-tracked-marker
    sections are NOT promotable through this surface).

    Refuses on a dirty source tree (anti-smell 13) so the promote does
    not race with uncommitted edits in the tracked repo. Renders the
    pre-promote confirm panel; on user-confirmed ``yes``, applies the
    three-file atomic mutation and records a ``TransitionCommand.PROMOTE``
    transition that ``setforge revert --profile=<x>`` can roll back.
    """
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)
    if tracked_file not in resolved.tracked_files:
        raise SetforgeError(f"tracked_file {tracked_file!r} not in profile {profile!r}")

    overlays = load_local_host_local_sections()
    if not offer_promote(
        section_name=section,
        host_local_sections=overlays,
        tracked_file_id=tracked_file,
    ):
        raise SetforgeError(
            f"section {section!r} is not declared in local.yaml "
            f"host_local_sections.{tracked_file}; only local.yaml-declared "
            "host-local sections are promotable"
        )

    section_name = HostLocalSectionName(section)
    section_overlay = overlays[tracked_file][section_name]

    tracked_def = cfg.tracked_files[tracked_file]
    tracked_path = resolve_src(tracked_def, repo_root)
    live_path = resolve_dst(tracked_def)

    # Source-cleanliness pre-gate: refuse if the tracked tree is dirty.
    if cfg.source is not None:
        check_source_clean(cfg.source)

    # Capture body from the LIVE file BEFORE the live-side rewrite
    # (anti-smell 4): the user's edits to the host-local section are
    # what gets promoted to shared, not the local.yaml-declared body.
    live_text = live_path.read_text(encoding="utf-8")
    live_sections = extract_sections(live_text, allow_legacy=True)
    if section not in live_sections:
        raise SetforgeError(
            f"section {section!r} not present in live file {live_path}; "
            "host-local injection must have run at least once via "
            "`setforge install` before this section can be promoted"
        )
    body = live_sections[section]

    plan = build_promote_plan(
        section_name=section_name,
        local_yaml_path=LOCAL_CONFIG_PATH,
        tracked_path=tracked_path,
        live_path=live_path,
        body=body,
        anchor=section_overlay.anchor,
        profile=profile,
    )

    if not confirm_promote_to_shared(plan):
        raise typer.Exit(0)

    if not no_transition:
        transitions.ensure_state_dir_writable()

    snapshot_paths = [
        plan.tracked_path,
        plan.live_path,
        plan.local_yaml_path,
    ]
    file_pre = transitions.snapshot_paths(snapshot_paths)

    snapshot_base = transitions.state_root() / "snapshots"
    snapshot_base.mkdir(parents=True, exist_ok=True)

    execute_promote_to_shared(
        plan, tracked_file_id=tracked_file, snapshot_base=snapshot_base
    )

    file_post = transitions.snapshot_paths(snapshot_paths)

    if not no_transition:
        target = transitions.write_transition(
            transitions.make_meta(
                transitions.TransitionCommand.PROMOTE,
                profile,
                end_timestamp=transitions.now_utc().astimezone(UTC).isoformat(),
                command_line=redact_argv(sys.argv[1:]),
            ),
            file_pre,
            file_post,
            None,
        )
        typer.echo(f"transition: {target}")
        typer.echo(f"revert with: setforge revert --profile={profile}")
