"""Sync-wizard ``[p]`` auto-promote: host-local section → shared.

When ``setforge sync``'s per-section drift prompt encounters a host-local
section declared in ``~/.config/setforge/local.yaml``
``host_local_sections``, the user can press ``[p]`` to promote the section to
``shared``. The promote performs four atomic mutations:

1. Drop the ``host_local_sections.<name>`` entry from ``local.yaml``.
2. Insert a NEW ``shared`` marker pair into the tracked-file at the
   anchor previously declared by ``local.yaml`` (body copied from live).
3. (Subsumed in step 2 — the body is spliced as part of the new pair.)
4. Rewrite the live-side marker keywords from ``host-local`` to
   ``shared`` (body bytes preserved exactly).

Atomicity is enforced via :class:`setforge.wizard.Snapshot`: a snapshot
of the three mutated files (local.yaml, tracked-file, live-file) is
taken before the first write; on any exception, every file is restored.

The pre-promote confirm UI mirrors the auto-confirm ``_render_panel`` shape —
one rich ``Panel`` carrying file mutations + body preview + secrets-scan
results + RISKS — followed by a ``radiolist_dialog`` whose default is
``no, abort`` so a fat-finger ``Enter`` aborts cleanly.

Secrets policy (Q10 Option B): the body is scanned BEFORE the live-side
rewrite, findings are surfaced in the RISKS section, and the user may
still pick ``yes`` — the default=no provides the friction; the panel
provides the warning surface.
"""

from __future__ import annotations

import io
import re
import sys
import tempfile
from collections.abc import Callable, Mapping, MutableMapping, MutableSequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from ruamel.yaml import YAML

from setforge.errors import SetforgeError
from setforge.host_local_inject import inject_host_local_section
from setforge.secrets import SecretsScanResult, run_pre_deploy_scan
from setforge.section_reconcile import _atomic_write_text, maintain_marker_hashes
from setforge.source import Anchor, HostLocalSectionName
from setforge.wizard import Snapshot

__all__ = [
    "PromotePlan",
    "PromoteSecretsScanner",
    "build_promote_plan",
    "confirm_promote_to_shared",
    "execute_promote_to_shared",
    "offer_promote",
    "rewrite_live_markers_to_shared",
    "scan_body_for_secrets",
]

# Module-level lazy import hook for prompt_toolkit.shortcuts.radiolist_dialog.
# Mirrors the pattern in :mod:`setforge.cli._confirm` so cold-start paths
# (validate / compare / help) do not pay the ~140ms prompt_toolkit import
# cost. Tests monkeypatch this attribute path directly.


def __getattr__(name: str) -> Any:  # noqa: ANN401 — PEP 562 module hook
    if name == "radiolist_dialog":
        from prompt_toolkit.shortcuts import radiolist_dialog

        return radiolist_dialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# A secrets scanner that takes a body string and returns a SecretsScanResult.
# Default scanner is :func:`scan_body_for_secrets` (writes body to a temp
# dir + routes through :func:`setforge.secrets.run_pre_deploy_scan`); test
# code injects no-op / forced-finding scanners directly via this parameter.
type PromoteSecretsScanner = Callable[[str], SecretsScanResult]


@dataclass(slots=True, frozen=True)
class PromotePlan:
    """Inventory of the four mutations a single host-local → shared promote performs.

    Captured BEFORE the live-side rewrite so the body bytes reflect the
    pre-promote state. The confirm panel renders this; the executor
    consumes it as the source of truth for the four file writes.
    """

    section_name: HostLocalSectionName
    local_yaml_path: Path
    tracked_path: Path
    live_path: Path
    body: str
    anchor: Anchor
    revert_command: str
    secrets: SecretsScanResult


def scan_body_for_secrets(body: str) -> SecretsScanResult:
    """Default scanner: write ``body`` to a tempdir + run gitleaks on it.

    :func:`setforge.secrets.run_pre_deploy_scan` operates on a directory;
    promote's input is a single body string. We materialise the body to
    a ``.md`` file in an isolated tempdir so gitleaks scans only the
    promote target, then route through the same allowlist + soft-binary
    contract every other call site uses.
    """
    with tempfile.TemporaryDirectory(prefix="setforge-promote-") as tmp:
        body_file = Path(tmp) / "body.md"
        body_file.write_text(body, encoding="utf-8")
        return run_pre_deploy_scan(tracked_root=Path(tmp))


def _format_secrets_summary(secrets: SecretsScanResult) -> str:
    """Render the secrets-scan row for the confirm panel."""
    if not secrets.findings:
        return "[green]Secrets scan:[/green] clean (gitleaks 0 findings)."
    lines = [
        f"[bold red]Secrets scan: "
        f"{len(secrets.findings)} finding"
        f"{'s' if len(secrets.findings) != 1 else ''} (gitleaks)[/bold red]"
    ]
    for finding in secrets.findings:
        lines.append(f"  - {finding.rule_id} at line {finding.line_number}")
    return "\n".join(lines)


def _format_risks(secrets: SecretsScanResult, revert_command: str) -> str:
    """Render the RISKS bullet list for the confirm panel."""
    risks: list[str] = []
    if secrets.findings:
        risks.append(
            "[bold red]GITLEAKS FOUND CANDIDATE SECRETS in the body content."
            "[/bold red]\n"
            "    Review BOTH lines before promoting; secrets in the tracked\n"
            "    repo are visible to everyone with read access."
        )
    risks.append(
        "Content becomes visible in the tracked config repo "
        "(visible to anyone with read access)."
    )
    risks.append(f"To undo: [cyan]{revert_command}[/cyan]")
    risks.append("Transition kind: PROMOTE (4-mutation atomic record)")
    return "[bold red]RISKS:[/bold red]\n" + "\n".join(f"  - {risk}" for risk in risks)


def _truncate_body_preview(body: str, *, max_lines: int = 6) -> str:
    """Return the first ``max_lines`` lines of ``body`` for panel preview."""
    lines = body.splitlines()
    if len(lines) <= max_lines:
        return body if body.endswith("\n") else body + "\n"
    head = "\n".join(lines[:max_lines])
    return f"{head}\n  ... {len(lines) - max_lines} more line(s) ..."


def _render_panel(plan: PromotePlan, *, console: Console) -> None:
    """Print the all-in-one confirm panel for ``plan`` to ``console``."""
    body_preview = _truncate_body_preview(plan.body)
    file_lines = [
        f"  [yellow]-[/yellow] {plan.local_yaml_path}      "
        f"(drop host_local_sections.{plan.section_name})",
        f"  [yellow]+[/yellow] {plan.tracked_path}     "
        "(insert shared marker pair + body)",
        f"  [yellow]~[/yellow] {plan.live_path}     "
        "(markers: host-local -> shared, body unchanged)",
    ]
    body = (
        f'[bold]Promote section "{plan.section_name}" to shared[/bold]\n\n'
        f"[bold]Files to mutate (3):[/bold]\n" + "\n".join(file_lines) + "\n\n"
        f"[bold]Body preview ({len(plan.body.splitlines())} line(s)):[/bold]\n"
        f"{body_preview}\n\n"
        f"{_format_secrets_summary(plan.secrets)}\n\n"
        f"{_format_risks(plan.secrets, plan.revert_command)}"
    )
    console.print(Panel(body, expand=False))


def confirm_promote_to_shared(
    plan: PromotePlan,
    *,
    console: Console | None = None,
) -> bool:
    """Render the confirm panel + arrow-key dialog; return user's choice.

    Returns ``True`` only when the user explicitly selects ``yes``;
    ``False`` on default-no (Enter without arrows), explicit no, or
    Esc. Non-TTY callers are rejected — promote is an interactive-only
    flow per the spec (the auto-confirm ``yes=`` short-circuit does NOT apply).
    """
    if not sys.stdin.isatty():
        raise SetforgeError(
            "setforge sync [p] promote requires an interactive TTY "
            "(non-TTY callers cannot confirm the multi-file mutation)"
        )
    if console is None:
        console = Console()
    _render_panel(plan, console=console)
    # Resolve through the module-level __getattr__ so tests can
    # monkeypatch setforge.section_promote.radiolist_dialog without
    # paying the prompt_toolkit import cost on cold-start paths.
    from setforge import section_promote as _self  # local alias for monkeypatch

    app = _self.radiolist_dialog(
        title=f"setforge sync — promote {plan.section_name}",
        text="Promote this host-local section to shared?",
        values=[
            (False, "No  - abort, no mutations"),
            (True, "Yes - apply the 4-mutation atomic promote"),
        ],
        default=False,
    )
    _bind_escape_to_abort(app)
    choice = app.run()
    if choice is None or choice is False:
        console.print("[red]aborted[/red] - no mutations applied")
        return False
    console.print("[green]proceeding[/green] - applying promote")
    return True


def _bind_escape_to_abort(app: object) -> None:
    """Add an explicit Escape binding that exits the dialog with ``None``.

    prompt_toolkit's :func:`radiolist_dialog` ships no ESC keybinding by
    default — exit is only via the OK / Cancel buttons. The spec
    requires Escape to abort the confirm dialog cleanly,
    so we layer a per-Application ``escape`` binding on top of the
    factory-returned :class:`prompt_toolkit.application.Application` that
    calls :meth:`Application.exit` with no result (which surfaces as
    ``None`` to the ``.run()`` caller, matching the Cancel-button path).

    The ``app`` parameter is typed as :class:`object` so this helper
    stays usable from cold-start paths that have not paid the
    prompt_toolkit import cost yet — the actual runtime shape is
    :class:`prompt_toolkit.application.Application`, but typing it that
    way would force an eager prompt_toolkit import at module load.
    Imports are deferred to the function body for the same reason.
    """
    from prompt_toolkit.key_binding import (
        KeyBindings,
        KeyPressEvent,
        merge_key_bindings,
    )

    bindings = KeyBindings()

    @bindings.add("escape", eager=True)
    def _(event: KeyPressEvent) -> None:
        event.app.exit()

    # Merge the new bindings on top of whatever the factory installed.
    # Application.key_bindings is the active set; replacing via
    # merge_key_bindings preserves the factory's tab / s-tab focus moves.
    app.key_bindings = merge_key_bindings(  # type: ignore[attr-defined]
        [app.key_bindings, bindings]  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# Live-side marker rewrite
# ---------------------------------------------------------------------------

# Matches start/end markers for a specific host-local section.
# Tolerates leading whitespace + an optional ``hash=<...>`` segment on
# the end marker. Used by :func:`rewrite_live_markers_to_shared` to flip
# the semantics keyword in-place while preserving body bytes exactly.
_HOST_LOCAL_START_TEMPLATE = (
    r"^(\s*<!--\s*setforge:user-section\s+start\s+)host-local(\s+{name}\s*-->\s*)$"
)
_HOST_LOCAL_END_TEMPLATE = (
    r"^(\s*<!--\s*setforge:user-section\s+end\s+)host-local"
    r"(\s+{name}(?:\s+hash=\S+)?\s*-->\s*)$"
)


def rewrite_live_markers_to_shared(text: str, name: HostLocalSectionName) -> str:
    """Replace ``host-local`` -> ``shared`` on the start + end markers for ``name``.

    Body bytes between the markers stay byte-identical (anti-smell 4).
    Raises :class:`SetforgeError` when zero or more than one start/end
    pair matches — the caller's PromotePlan asserts exactly-one-pair
    before reaching here, so a mismatch means live drift since plan
    capture.

    The end marker may carry a ``hash=`` segment; the segment is
    preserved verbatim (the post-rewrite text gets re-stamped via
    :func:`setforge.section_reconcile.maintain_marker_hashes` so a stale
    hash value is harmless).
    """
    start_re = re.compile(_HOST_LOCAL_START_TEMPLATE.format(name=re.escape(name)))
    end_re = re.compile(_HOST_LOCAL_END_TEMPLATE.format(name=re.escape(name)))
    new_lines: list[str] = []
    start_hits = 0
    end_hits = 0
    for line in text.splitlines(keepends=True):
        # Strip the trailing newline for regex; keep it for output assembly.
        stripped = line[:-1] if line.endswith("\n") else line
        trailing = line[len(stripped) :]
        match_start = start_re.match(stripped)
        match_end = end_re.match(stripped)
        if match_start is not None:
            start_hits += 1
            new_lines.append(
                f"{match_start.group(1)}shared{match_start.group(2)}{trailing}"
            )
        elif match_end is not None:
            end_hits += 1
            new_lines.append(
                f"{match_end.group(1)}shared{match_end.group(2)}{trailing}"
            )
        else:
            new_lines.append(line)
    if start_hits != 1 or end_hits != 1:
        raise SetforgeError(
            f"rewrite_live_markers_to_shared: expected exactly one "
            f"host-local marker pair for section {name!r}; "
            f"found {start_hits} start / {end_hits} end markers"
        )
    return "".join(new_lines)


# ---------------------------------------------------------------------------
# local.yaml mutation: drop host_local_sections.<name>
# ---------------------------------------------------------------------------


def _drop_legacy_host_local_section(
    tracked_file_node: MutableMapping[object, object],
    section_name: HostLocalSectionName,
) -> bool:
    """Drop a legacy ``host_local_sections.<name>`` entry; return whether found.

    Prunes the parent ``host_local_sections`` block when the drop empties
    it. Returns ``False`` (no mutation) when the legacy block or the named
    entry is absent — the caller falls through to the migrated-span path.
    """
    hl_node = tracked_file_node.get("host_local_sections")
    if not isinstance(hl_node, MutableMapping) or section_name not in hl_node:
        return False
    del hl_node[section_name]
    if len(hl_node) == 0:
        del tracked_file_node["host_local_sections"]
    return True


def _drop_overlay_span(
    tracked_file_node: MutableMapping[object, object],
    section_name: HostLocalSectionName,
) -> bool:
    """Drop a migrated OVERLAY ``spans`` entry by identity; return whether found.

    The migration (:mod:`setforge.overlay_migration`) retires each
    ``host_local_sections.<name>`` into a ``spans`` entry whose top-level
    ``anchor`` IS that section name with ``kind=overlay``. Promote on an
    already-migrated host must drop THAT representation. Prunes the
    ``spans`` sequence when the drop empties it. Returns ``False`` (no
    mutation) when no matching overlay span exists.
    """
    spans_node = tracked_file_node.get("spans")
    if not isinstance(spans_node, MutableSequence):
        return False
    for idx, span in enumerate(spans_node):
        if (
            isinstance(span, MutableMapping)
            and span.get("kind") == "overlay"
            and span.get("anchor") == section_name
        ):
            del spans_node[idx]
            if len(spans_node) == 0:
                del tracked_file_node["spans"]
            return True
    return False


def _drop_host_local_section_entry(
    local_yaml_path: Path,
    *,
    tracked_file_id: str,
    section_name: HostLocalSectionName,
) -> None:
    """Drop a host-local section from ``local.yaml`` (legacy block OR overlay span).

    Uses ruamel.yaml round-trip mode so the rest of the file
    (comments, key order, formatting) is preserved. The section may live
    in either representation: the legacy ``host_local_sections.<name>``
    block (pre-migration hosts) or the migrated ``spans`` OVERLAY entry
    whose identity ``anchor`` is the section name (post-migration hosts,
    see :mod:`setforge.overlay_migration`). Both are checked; dropping the
    last child of either container prunes that container, and when the
    parent overlay block becomes empty the tracked_file entry is dropped.
    ``tracked_files`` itself stays (other tracked_files are independent).

    Raises :class:`SetforgeError` if neither representation carries the
    entry — the caller's PromotePlan asserts presence; a missing entry
    means drift since plan capture.
    """
    yaml = YAML(typ="rt")
    with local_yaml_path.open("r", encoding="utf-8") as fh:
        doc = yaml.load(fh)
    if not isinstance(doc, MutableMapping):
        raise SetforgeError(
            f"local.yaml at {local_yaml_path} is not a mapping; cannot drop "
            f"host_local_sections.{section_name}"
        )
    tracked_files_node = doc.get("tracked_files")
    if not isinstance(tracked_files_node, MutableMapping):
        raise SetforgeError(
            f"local.yaml missing tracked_files block; cannot drop "
            f"host_local_sections.{section_name}"
        )
    tracked_file_node = tracked_files_node.get(tracked_file_id)
    if not isinstance(tracked_file_node, MutableMapping):
        raise SetforgeError(
            f"local.yaml has no tracked_files.{tracked_file_id} block; cannot drop "
            f"host_local_sections.{section_name}"
        )
    dropped = _drop_legacy_host_local_section(
        tracked_file_node, section_name
    ) or _drop_overlay_span(tracked_file_node, section_name)
    if not dropped:
        raise SetforgeError(
            f"local.yaml has no host_local_sections.{section_name} entry "
            f"(legacy block or migrated overlay span) under "
            f"tracked_files.{tracked_file_id}; nothing to drop"
        )
    if len(tracked_file_node) == 0:
        del tracked_files_node[tracked_file_id]
    buf = io.StringIO()
    yaml.dump(doc, buf)
    _atomic_write_text(local_yaml_path, buf.getvalue())


# ---------------------------------------------------------------------------
# Atomic executor
# ---------------------------------------------------------------------------


def _write_tracked_with_shared_section(plan: PromotePlan) -> None:
    """Step 1: splice a new shared marker pair + body into tracked-file.

    Reads ``plan.tracked_path``, threads it through
    :func:`inject_host_local_section` (which emits a host-local marker
    pair),
    :func:`rewrite_live_markers_to_shared` (flip the just-inserted pair
    to ``shared``), and :func:`maintain_marker_hashes` (stamp the
    end-marker hash). The single :func:`_atomic_write_text` call lands
    everything as one atomic move (anti-smell 2).
    """
    tracked_text = plan.tracked_path.read_text(encoding="utf-8")
    new_tracked = inject_host_local_section(
        tracked_text, plan.section_name, plan.anchor, plan.body
    )
    new_tracked = rewrite_live_markers_to_shared(new_tracked, plan.section_name)
    new_tracked = maintain_marker_hashes(new_tracked)
    _atomic_write_text(plan.tracked_path, new_tracked)


def _rewrite_live_to_shared(plan: PromotePlan) -> None:
    """Step 2: rewrite the live-side markers host-local → shared.

    Body bytes between the markers are preserved exactly
    (anti-smell 4 — :func:`rewrite_live_markers_to_shared` enforces).
    The post-rewrite text is re-stamped via
    :func:`maintain_marker_hashes` so the end-marker hash matches the
    new (now ``shared``) marker keyword set.
    """
    live_text = plan.live_path.read_text(encoding="utf-8")
    new_live = rewrite_live_markers_to_shared(live_text, plan.section_name)
    new_live = maintain_marker_hashes(new_live)
    _atomic_write_text(plan.live_path, new_live)


def execute_promote_to_shared(
    plan: PromotePlan,
    *,
    tracked_file_id: str,
    snapshot_base: Path,
) -> None:
    """Apply the 4 mutations atomically. Roll back on any partial failure.

    Mutations, in order:

    1. ``tracked_path``: splice a new ``shared`` marker pair + body at
       ``plan.anchor`` (post-splice text routed through
       :func:`maintain_marker_hashes` for the hash invariant).
    2. ``live_path``: rewrite the host-local marker keywords to
       ``shared`` (body bytes preserved exactly).
    3. ``local_yaml_path``: drop the ``host_local_sections.<name>``
       entry under ``tracked_files.<id>``.

    Three writes total — the spec's "4 mutations" counts the body
    insertion as a separate step from the marker-pair insertion, but
    the executor splices them together in one atomic
    :func:`_atomic_write_text` call per file (anti-smell 2).

    :class:`Snapshot` covers all three files; on any exception during
    the writes, every file is restored from snapshot before the
    exception re-raises (anti-smell 1). Caller is responsible for the
    transition record + ``check_source_clean`` pre-gate.

    Step 3 calls :func:`_drop_host_local_section_entry` via bare-name
    module-level lookup so rollback tests monkeypatching that name on
    the module attribute hit the call site as written.
    """
    files = [plan.tracked_path, plan.live_path, plan.local_yaml_path]
    snap = Snapshot(files=files, snapshot_base=snapshot_base)
    with snap:
        try:
            _write_tracked_with_shared_section(plan)
            _rewrite_live_to_shared(plan)
            _drop_host_local_section_entry(
                plan.local_yaml_path,
                tracked_file_id=tracked_file_id,
                section_name=plan.section_name,
            )
            snap.discard()
        except BaseException:
            snap.restore()
            raise


# ---------------------------------------------------------------------------
# PromotePlan factory (called by the wizard once the user picks [p])
# ---------------------------------------------------------------------------


def build_promote_plan(
    *,
    section_name: HostLocalSectionName,
    local_yaml_path: Path,
    tracked_path: Path,
    live_path: Path,
    body: str,
    anchor: Anchor,
    profile: str,
    secrets_scanner: PromoteSecretsScanner | None = None,
) -> PromotePlan:
    """Build a :class:`PromotePlan` from wizard context.

    ``body`` is captured from the LIVE file BEFORE the live-side rewrite
    so the secrets scan sees the pre-rewrite content (anti-smell 4 +
    Q10 Option B). Runs the scanner inline so the panel can render the
    findings; pass a stub scanner in tests to inject controlled results.

    ``secrets_scanner`` defaults to :func:`scan_body_for_secrets` (the
    real gitleaks-via-tempdir path). Tests pass a callable returning a
    forged :class:`SecretsScanResult` to exercise the
    finding-displayed-in-panel path without invoking gitleaks.
    """
    scanner = secrets_scanner if secrets_scanner is not None else scan_body_for_secrets
    secrets = scanner(body)
    return PromotePlan(
        section_name=section_name,
        local_yaml_path=local_yaml_path,
        tracked_path=tracked_path,
        live_path=live_path,
        body=body,
        anchor=anchor,
        revert_command=f"setforge revert --profile={profile}",
        secrets=secrets,
    )


def offer_promote(
    *,
    section_name: str,
    host_local_sections: Mapping[str, Mapping[HostLocalSectionName, object]],
    tracked_file_id: str,
) -> bool:
    """Gate predicate: is ``[p]`` offered for ``section_name``?

    Per anti-smell 10: promote is offered ONLY for sections present in
    ``local.yaml.host_local_sections[tracked_file_id]``. host-local
    sections sourced from tracked-side markers (no local.yaml overlay)
    are NOT promotable — their content already lives in the tracked
    repo, and the host-local keyword is a per-file decision rather
    than a per-host one.
    """
    overlay = host_local_sections.get(tracked_file_id)
    if overlay is None:
        return False
    # Cast-free membership: HostLocalSectionName is a NewType wrapping
    # str at runtime, so plain `in` against the str-keyed view works.
    return section_name in {str(k) for k in overlay}
