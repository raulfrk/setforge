"""Plugin + extension reconcile and reverse helpers shared by
install / revert / plugins subcommand modules.

No ``app`` import and no ``@app.command()`` decorator registrations.
Helpers do drive subprocesses (``claude`` / ``code``), write stderr via
``typer.secho``, and ``_write_reverse_transition`` persists a transition
record. The split keeps the subcommand modules free of reconcile state
machinery; the dispatch table here (``_REVERSE_PLUGIN_DISPATCH``) covers
four of the five plugin-side inverse ops — ``marketplaces_removed`` is
handled separately by :func:`_apply_marketplace_re_add` because it
needs marketplace-source re-construction that the per-plugin dispatch
shape doesn't fit.
"""

import json
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import typer

from setforge import claude_plugins as claude_plugins_mod
from setforge import transitions, vscode_extensions
from setforge.cli._confirm import FailureAction, prompt_failure_action
from setforge.config import (
    Config,
    MarketplaceSource,
    MarketplaceSourceKind,
    ResolvedProfile,
)
from setforge.errors import (
    ExtensionInstallFailed,
    ExtensionToolMissing,
    PluginToolMissing,
    ReconcileAborted,
)
from setforge.transitions import ReconcileKind, ReconcileStatus


def _parse_marketplace_from(from_: str) -> MarketplaceSource:
    """Parse ``--from=github:owner/repo`` or ``--from=path:/dir`` into a source.

    Raises :class:`typer.Exit(1)` with a user-visible error if the prefix
    is neither ``github:`` nor ``path:``. Shared by ``plugin add`` and
    ``marketplace add`` so the parser stays in one place.
    """
    if from_.startswith("github:"):
        repo = from_[len("github:") :]
        return MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo=repo)
    if from_.startswith("path:"):
        local_path = Path(from_[len("path:") :]).expanduser()
        return MarketplaceSource(source=MarketplaceSourceKind.PATH, path=local_path)
    typer.secho(
        f"error: unrecognised --from format {from_!r};"
        " use github:owner/repo or path:/dir",
        err=True,
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=1)


@dataclass(slots=True, frozen=True)
class ReconcileAttempt:
    """One per-item reconcile attempt recorded as we walk the work list.

    ``source`` is a human-tag describing where the item came from
    (``"from profile"`` or ``"from local.yaml"``); today we always set
    ``"from profile"`` since the per-source provenance split is
    setforge-7dav scope. ``full_stderr`` carries the full captured
    subprocess trace for the DIAGNOSE branch of
    :func:`prompt_failure_action`.
    """

    item_id: str
    started_at: datetime
    ended_at: datetime
    success: bool
    error_summary: str | None
    full_stderr: str | None
    source: str


def _stderr_full_from_failed(error_summary: str) -> str:
    """Return ``error_summary`` as the full trace stand-in.

    Today ``claude_plugins.reconcile`` / ``vscode_extensions.reconcile``
    surface only the tail of stderr in their failure list; this helper
    exists so a future change that captures the full trace can swap one
    callsite without touching the prompt path.
    """
    return error_summary


def _append_extension_success_outcomes(
    outcomes: list[transitions.ReconcileOutcome],
    ext_ids: Iterable[str],
    *,
    verb: str,
) -> None:
    """Append ``ReconcileStatus.OK`` outcomes for each extension id and echo progress.

    ``verb`` is the past-tense action word printed on the progress line
    (``"installed"`` or ``"uninstalled"``) — matches the prior inline
    loop's output verbatim so tests asserting against stdout do not
    shift.
    """
    for ext_id in ext_ids:
        outcomes.append(
            transitions.ReconcileOutcome(
                item_id=ext_id,
                kind=ReconcileKind.EXTENSION,
                status=ReconcileStatus.OK,
                error_summary=None,
            )
        )
        typer.echo(f"{verb}  {ext_id}")


def _walk_extension_failures(
    *,
    report: "vscode_extensions.ReconcileReport",
    retry_failed_ids: frozenset[str],
    yes: bool,
    outcomes: list[transitions.ReconcileOutcome],
    final_added: list[str],
    final_removed: list[str],
) -> None:
    """Walk ``report.failed`` and surface the per-item failure prompt.

    Mutates ``outcomes`` (append per-item :class:`ReconcileOutcome`) and
    ``final_added`` / ``final_removed`` (append on RETRY-success only).
    Skips ids not in ``retry_failed_ids`` when that set is non-empty;
    full-pass behavior is restored when it is empty. The is_install
    branch picks the correct inverse for the RETRY path inside
    :func:`_handle_extension_failure`.
    """
    for ext_id, err in report.failed:
        if retry_failed_ids and ext_id not in retry_failed_ids:
            continue
        # The originating op was either install (in to_install) or
        # uninstall (in to_uninstall). Pick the right inverse for RETRY.
        is_install = ext_id in report.to_install
        outcome, retry_ok = _handle_extension_failure(
            ext_id=ext_id,
            error_summary=err,
            is_install=is_install,
            yes=yes,
            successful_added=tuple(final_added),
            successful_removed=tuple(final_removed),
        )
        outcomes.append(outcome)
        if retry_ok:
            if is_install:
                final_added.append(ext_id)
            else:
                final_removed.append(ext_id)


def _reconcile_extensions(
    resolved: ResolvedProfile,
    *,
    retry_failed_ids: frozenset[str] = frozenset(),
    yes: bool = False,
) -> tuple[transitions.ExtensionDelta | None, tuple[transitions.ReconcileOutcome, ...]]:
    """Reconcile VSCode extensions with per-item skip / retry / abort UX.

    On per-extension failure, surfaces
    :func:`prompt_failure_action`. SKIP records a
    :class:`~transitions.ReconcileOutcome` with
    :attr:`ReconcileStatus.SKIPPED` and
    continues. RETRY re-invokes :func:`vscode_extensions.install_one` /
    :func:`vscode_extensions.uninstall_one` once for the same id; on
    second-attempt success the outcome is ``"retried_ok"``, on second
    failure it's ``"skipped"``. ABORT raises
    :class:`ReconcileAborted` after recording ``"aborted"`` outcomes for
    every successfully-reconciled extension so the caller can roll back.
    DIAGNOSE prints the captured stderr and re-prompts (handled inside
    :func:`prompt_failure_action`).

    Returns the :class:`ExtensionDelta` describing what landed on disk
    (or ``None`` when the underlying ``code`` binary is missing —
    warn-and-skip) plus the per-item outcomes tuple.

    ``retry_failed_ids`` filters the work list to only those ids: items
    not in the set are silently skipped from the reconcile pass when
    the set is non-empty. Today's full-pass behavior is restored when
    the set is empty (the default). ``yes=True`` short-circuits the
    prompt to its default :attr:`FailureAction.SKIP`.
    """
    try:
        report = vscode_extensions.reconcile(resolved.extensions)
    except ExtensionToolMissing as exc:
        typer.secho(
            f"warning: skipping extension reconcile — {exc}",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return None, ()

    initial_failed: dict[str, str] = dict(report.failed)
    successful_install = [i for i in report.to_install if i not in initial_failed]
    successful_uninstall = [i for i in report.to_uninstall if i not in initial_failed]

    outcomes: list[transitions.ReconcileOutcome] = []
    _append_extension_success_outcomes(outcomes, successful_install, verb="installed")
    _append_extension_success_outcomes(
        outcomes, successful_uninstall, verb="uninstalled"
    )

    final_added = list(successful_install)
    final_removed = list(successful_uninstall)

    _walk_extension_failures(
        report=report,
        retry_failed_ids=retry_failed_ids,
        yes=yes,
        outcomes=outcomes,
        final_added=final_added,
        final_removed=final_removed,
    )

    if not report:
        typer.echo("extensions: nothing to reconcile")
    delta = transitions.ExtensionDelta(
        added=final_added,
        removed=final_removed,
    )
    return delta, tuple(outcomes)


def _retry_extension_op(ext_id: str, *, is_install: bool) -> str | None:
    """Re-attempt one per-extension op; return error string on failure, None on success.

    Mirrors :func:`_retry_plugin_op`'s signature shape so the RETRY
    branch in :func:`_handle_extension_failure` has the same
    ``retry_err is None`` predicate as :func:`_handle_plugin_failure`.
    """
    try:
        if is_install:
            vscode_extensions.install_one(ext_id)
        else:
            vscode_extensions.uninstall_one(ext_id)
    except (ExtensionInstallFailed, ExtensionToolMissing) as exc:
        return str(exc)
    return None


def _handle_extension_failure(
    *,
    ext_id: str,
    error_summary: str,
    is_install: bool,
    yes: bool,
    successful_added: tuple[str, ...],
    successful_removed: tuple[str, ...],
) -> tuple[transitions.ReconcileOutcome, bool]:
    """Surface the failure prompt for one extension; return (outcome, retry_ok).

    ``retry_ok`` is ``True`` only when the user picked RETRY and the
    second attempt succeeded — the caller uses that signal to fold the
    item into the successful-added / -removed lists for the
    :class:`ExtensionDelta`.

    On ABORT, calls :func:`_abort_reverse_reconcile_extensions` to roll
    back items landed so far this install, then raises
    :class:`ReconcileAborted`.
    """
    typer.secho(f"FAILED  {ext_id} — {error_summary}", err=True, fg=typer.colors.YELLOW)
    action = prompt_failure_action(
        message=f"failed: {ext_id}\n{error_summary}",
        full_stderr=_stderr_full_from_failed(error_summary),
        yes=yes,
    )
    if action is FailureAction.SKIP:
        typer.echo(f"skipped   {ext_id}")
        return (
            transitions.ReconcileOutcome(
                item_id=ext_id,
                kind=ReconcileKind.EXTENSION,
                status=ReconcileStatus.SKIPPED,
                error_summary=error_summary,
            ),
            False,
        )
    if action is FailureAction.RETRY:
        retry_err = _retry_extension_op(ext_id, is_install=is_install)
        if retry_err is not None:
            typer.secho(
                f"FAILED  retry {ext_id} — {retry_err}",
                err=True,
                fg=typer.colors.YELLOW,
            )
            return (
                transitions.ReconcileOutcome(
                    item_id=ext_id,
                    kind=ReconcileKind.EXTENSION,
                    status=ReconcileStatus.SKIPPED,
                    error_summary=retry_err,
                ),
                False,
            )
        typer.echo(f"retried   {ext_id}")
        return (
            transitions.ReconcileOutcome(
                item_id=ext_id,
                kind=ReconcileKind.EXTENSION,
                status=ReconcileStatus.RETRIED_OK,
                error_summary=None,
            ),
            True,
        )
    # ABORT
    _abort_reverse_reconcile_extensions(
        successful_added=successful_added,
        successful_removed=successful_removed,
    )
    raise ReconcileAborted(
        f"install aborted during extension reconcile (failed item: {ext_id!r})"
    )


def _abort_reverse_reconcile_extensions(
    *,
    successful_added: tuple[str, ...],
    successful_removed: tuple[str, ...],
) -> None:
    """Reverse extensions landed so far this install (ABORT path).

    Best-effort: a per-item failure during the reverse pass is logged
    via :func:`typer.secho` but does not abort the rollback — mirrors
    :func:`_reverse_extensions`'s warn-and-continue posture so the user
    sees every conflicting id rather than only the first.
    """
    for ext_id in successful_added:
        try:
            vscode_extensions.uninstall_one(ext_id)
        except (ExtensionInstallFailed, ExtensionToolMissing) as exc:
            typer.secho(
                f"warning: rollback uninstall of {ext_id} failed — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )
    for ext_id in successful_removed:
        try:
            vscode_extensions.install_one(ext_id)
        except (ExtensionInstallFailed, ExtensionToolMissing) as exc:
            typer.secho(
                f"warning: rollback install of {ext_id} failed — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )


def _emit_plugin_report(
    plugin_report: claude_plugins_mod.ReconcileReport,
) -> None:
    """Render per-plugin install/enable/disable progress lines from a
    reconcile report.

    Skips plugins listed in ``plugin_report.failed`` so a successful
    install line never appears for a plugin whose subsequent step
    failed. Failed plugins surface as a separate ``FAILED plugin`` line
    written to stderr. Emits a single ``plugins: nothing to reconcile``
    line when the report is empty.
    """
    failed_plugin_ids = {pid for pid, _ in plugin_report.failed}
    for name, mp in plugin_report.to_install:
        pid = f"{name}@{mp}"
        if pid not in failed_plugin_ids:
            typer.echo(f"plugin installed  {pid}")
    for pid in plugin_report.to_enable:
        if pid not in failed_plugin_ids:
            typer.echo(f"plugin enabled    {pid}")
    for pid in plugin_report.to_disable:
        if pid not in failed_plugin_ids:
            typer.echo(f"plugin disabled   {pid}")
    for pid, err in plugin_report.failed:
        typer.secho(f"FAILED plugin  {pid} — {err}", err=True, fg=typer.colors.YELLOW)
    if not plugin_report:
        typer.echo("plugins: nothing to reconcile")


def _warn_skip_reconcile(exc: PluginToolMissing) -> None:
    """Yellow stderr warning when the ``claude`` binary is absent."""
    typer.secho(
        f"warning: skipping claude plugin reconcile — {exc}",
        err=True,
        fg=typer.colors.YELLOW,
    )


def _emit_reconcile_summary(
    plugin_outcomes: tuple[transitions.ReconcileOutcome, ...],
    ext_outcomes: tuple[transitions.ReconcileOutcome, ...],
) -> None:
    """Render the post-reconcile ``summary:`` block per acceptance #6.

    Emits one line per kind (``plugin`` / ``extension``) of the shape::

        N <kind>s reconciled (M required retry, K skipped: <ids>)

    where ``N`` counts items that landed (``ok`` + ``retried_ok``), ``M``
    counts ``retried_ok`` only, and ``K`` counts ``skipped`` with the
    comma-separated ids appended for ``--retry-failed`` discoverability.
    The parenthetical drops to ``""`` when both ``M`` and ``K`` are
    zero, keeping happy-path output compact. ``aborted`` outcomes are
    excluded from ``N``: they represent rollback bookkeeping for items
    the user explicitly abandoned, not items reconciled this pass.

    An empty outcome tuple suppresses the corresponding line (no
    misleading ``0 <kind>s reconciled`` row); when BOTH tuples are
    empty the function emits nothing at all so a tracked-files-only
    install does not surface a bare ``summary:`` header.
    """
    plugin_line = _format_summary_line(plugin_outcomes, "plugin")
    ext_line = _format_summary_line(ext_outcomes, "extension")
    if plugin_line is None and ext_line is None:
        return
    typer.echo("summary:")
    if plugin_line is not None:
        typer.echo(f"  {plugin_line}")
    if ext_line is not None:
        typer.echo(f"  {ext_line}")


def _format_summary_line(
    outcomes: tuple[transitions.ReconcileOutcome, ...],
    kind_label: str,
) -> str | None:
    """Format one ``N <kind>s reconciled (...)`` line, or return ``None``
    when ``outcomes`` is empty so the caller can suppress the row.

    Pluralization is naive — every kind label gets a trailing ``s`` to
    match the spec mockup (``plugins reconciled`` / ``extensions
    reconciled``); both supported labels already end in a consonant so
    the mockup phrasing falls out without special-casing.
    """
    if not outcomes:
        return None
    ok = sum(1 for o in outcomes if o.status is ReconcileStatus.OK)
    retried = sum(1 for o in outcomes if o.status is ReconcileStatus.RETRIED_OK)
    skipped_ids = [o.item_id for o in outcomes if o.status is ReconcileStatus.SKIPPED]
    reconciled = ok + retried
    parts: list[str] = []
    if retried:
        parts.append(f"{retried} required retry")
    if skipped_ids:
        parts.append(f"{len(skipped_ids)} skipped: {', '.join(skipped_ids)}")
    suffix = f" ({', '.join(parts)})" if parts else ""
    return f"{reconciled} {kind_label}s reconciled{suffix}"


def _append_plugin_success_outcomes(
    outcomes: list[transitions.ReconcileOutcome],
    delta: transitions.PluginDelta,
) -> None:
    """Append ``ReconcileStatus.OK`` outcomes for each landed plugin / marketplace.

    Iterates the four delta fields whose entries became ``"ok"`` outcomes
    in the original inline form (``installed`` / ``enabled`` /
    ``disabled`` / ``marketplaces_added``). ``marketplaces_removed`` is
    excluded — today's ``_compute_plugin_delta`` always returns ``()``
    for that field on install, so there is nothing to record there.
    """
    field_pairs: tuple[tuple[str, Sequence[str]], ...] = (
        ("installed", delta.installed),
        ("enabled", delta.enabled),
        ("disabled", delta.disabled),
        ("marketplaces_added", delta.marketplaces_added),
    )
    for _field_name, items in field_pairs:
        for pid in items:
            outcomes.append(
                transitions.ReconcileOutcome(
                    item_id=pid,
                    kind=ReconcileKind.PLUGIN,
                    status=ReconcileStatus.OK,
                    error_summary=None,
                )
            )


def _reconcile_plugins(
    cfg: Config,
    resolved: ResolvedProfile,
    *,
    retry_failed_ids: frozenset[str] = frozenset(),
    yes: bool = False,
) -> tuple[transitions.PluginDelta | None, tuple[transitions.ReconcileOutcome, ...]]:
    """Reconcile Claude plugins with per-item skip / retry / abort UX.

    On per-plugin failure, surfaces :func:`prompt_failure_action`. SKIP
    records a :class:`~transitions.ReconcileOutcome` with
    :attr:`ReconcileStatus.SKIPPED` and continues. RETRY re-invokes the
    originating per-item op (``plugin_install`` / ``plugin_enable`` /
    ``plugin_disable`` / ``marketplace_add``) once; success → outcome
    ``"retried_ok"``, failure → outcome ``"skipped"``. ABORT triggers
    reverse-reconcile of items landed in THIS install via the existing
    :data:`_REVERSE_PLUGIN_DISPATCH` dispatch table, then raises
    :class:`ReconcileAborted`. DIAGNOSE prints captured stderr and
    re-prompts inside :func:`prompt_failure_action`.

    Returns the :class:`PluginDelta` describing what landed on disk (or
    ``None`` when the ``claude`` binary is missing — warn-and-skip)
    plus the per-item outcomes tuple.

    ``retry_failed_ids`` filters the work list to only those ids: today
    we cannot pre-filter ``claude_plugins.reconcile`` (it's batch-only),
    so the filter applies AFTER first reconcile: only failed-ids that
    appear in the set surface the prompt; others are silently dropped
    from the outcomes. When the set is empty (the default), all
    failures surface. ``yes=True`` short-circuits to the default
    :attr:`FailureAction.SKIP`.
    """
    try:
        pre_plugins = claude_plugins_mod.list_installed()
        pre_marketplaces = claude_plugins_mod.list_marketplaces()
    except PluginToolMissing as exc:
        _warn_skip_reconcile(exc)
        return None, ()
    try:
        plugin_report = claude_plugins_mod.reconcile(cfg, resolved)
    except PluginToolMissing as exc:
        _warn_skip_reconcile(exc)
        return None, ()
    _emit_plugin_report(plugin_report)
    post_plugins = claude_plugins_mod.list_installed()
    post_marketplaces = claude_plugins_mod.list_marketplaces()
    delta_first = _compute_plugin_delta(
        pre_plugins, pre_marketplaces, post_plugins, post_marketplaces
    )

    outcomes: list[transitions.ReconcileOutcome] = []
    _append_plugin_success_outcomes(outcomes, delta_first)

    retried_delta_pieces = _PluginRetriedPieces()
    for failed_id, err in plugin_report.failed:
        if retry_failed_ids and failed_id not in retry_failed_ids:
            continue
        op_kind = _classify_plugin_failure(plugin_report, failed_id)
        outcome = _handle_plugin_failure(
            cfg=cfg,
            failed_id=failed_id,
            error_summary=err,
            op_kind=op_kind,
            yes=yes,
            delta_so_far=delta_first,
            retried=retried_delta_pieces,
        )
        outcomes.append(outcome)

    final_delta = _merge_retried_plugin_delta(delta_first, retried_delta_pieces)
    return final_delta, tuple(outcomes)


@dataclass(slots=True)
class _PluginRetriedPieces:
    """Mutable accumulator for plugin retries that succeeded second time.

    Folded into the final :class:`PluginDelta` so a retried-OK plugin
    install lands in the delta exactly like a first-attempt success
    — ``revert`` can roll it back via the same path.
    """

    installed: list[str] = field(default_factory=list)
    enabled: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    marketplaces_added: list[str] = field(default_factory=list)


def _merge_retried_plugin_delta(
    delta_first: transitions.PluginDelta,
    retried: _PluginRetriedPieces,
) -> transitions.PluginDelta:
    """Fold retry-success ids into a fresh :class:`PluginDelta`.

    Preserves the immutability of ``delta_first`` (frozen dataclass)
    and the tuple-of-str invariant on each field — the retry path
    cannot append into the original's tuples in place.
    """
    return transitions.PluginDelta(
        installed=tuple(list(delta_first.installed) + retried.installed),
        enabled=tuple(list(delta_first.enabled) + retried.enabled),
        disabled=tuple(list(delta_first.disabled) + retried.disabled),
        marketplaces_added=tuple(
            list(delta_first.marketplaces_added) + retried.marketplaces_added
        ),
        marketplaces_removed=delta_first.marketplaces_removed,
    )


def _classify_plugin_failure(
    report: "claude_plugins_mod.ReconcileReport", failed_id: str
) -> str:
    """Map a failed-id back to its originating op for the RETRY branch.

    Returns one of ``"install"``, ``"enable"``, ``"disable"``,
    ``"marketplace_add"``, or ``"unknown"``. The to_install field on
    :class:`~claude_plugins.ReconcileReport` is a list of
    ``(name, marketplace)`` tuples; we reassemble the ``name@marketplace``
    pid before comparison. ``"unknown"`` reaches the RETRY branch only
    when ``claude_plugins.reconcile`` introduces a new failure category
    without updating this classifier — surfaces as a SKIP-equivalent
    (the retry is a no-op).
    """
    install_pids = {f"{n}@{m}" for n, m in report.to_install}
    if failed_id in install_pids:
        return "install"
    if failed_id in set(report.to_enable):
        return "enable"
    if failed_id in set(report.to_disable):
        return "disable"
    if failed_id in set(report.marketplaces_added):
        return "marketplace_add"
    return "unknown"


def _retry_plugin_op(
    cfg: Config,
    failed_id: str,
    op_kind: str,
) -> str | None:
    """Re-attempt one per-plugin op; return error string on failure, None on success.

    Dispatched by ``op_kind`` from :func:`_classify_plugin_failure`.
    ``marketplace_add`` looks up the source from ``cfg.marketplaces``;
    other kinds parse ``name@marketplace`` from ``failed_id``.
    ``"unknown"`` returns a placeholder error string so the outcome
    records ``"skipped"`` rather than a misleading ``"retried_ok"``.
    """
    try:
        if op_kind == "install":
            name, mp = failed_id.split("@", 1)
            claude_plugins_mod.plugin_install(name, mp)
        elif op_kind == "enable":
            claude_plugins_mod.plugin_enable(failed_id)
        elif op_kind == "disable":
            claude_plugins_mod.plugin_disable(failed_id)
        elif op_kind == "marketplace_add":
            source = cfg.marketplaces.get(failed_id)
            if source is None:
                return f"marketplace {failed_id!r} not in cfg.marketplaces"
            claude_plugins_mod.marketplace_add(failed_id, source)
        else:
            return f"unknown op kind {op_kind!r} for retry"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return claude_plugins_mod.stderr_of(exc)
    except PluginToolMissing as exc:
        return str(exc)
    return None


# Maps a retried op_kind from _classify_plugin_failure to the
# _PluginRetriedPieces field that accumulates its second-attempt
# successes. Unknown op kinds (the classifier's "unknown" fallback)
# are deliberately absent — _handle_plugin_failure treats a missing
# entry as a SKIP-equivalent rather than a retried_ok with no delta
# bookkeeping.
_RETRY_PIECE_FIELD: Final[Mapping[str, str]] = {
    "install": "installed",
    "enable": "enabled",
    "disable": "disabled",
    "marketplace_add": "marketplaces_added",
}


def _handle_plugin_failure(
    *,
    cfg: Config,
    failed_id: str,
    error_summary: str,
    op_kind: str,
    yes: bool,
    delta_so_far: transitions.PluginDelta,
    retried: _PluginRetriedPieces,
) -> transitions.ReconcileOutcome:
    """Surface the failure prompt for one plugin; return the outcome.

    On RETRY, mutates ``retried`` to record the second-attempt success
    so the caller can fold it into the final :class:`PluginDelta`. On
    ABORT, calls :func:`_abort_reverse_reconcile_plugins` with the
    plugins/marketplaces landed so far this install, then raises
    :class:`ReconcileAborted`.
    """
    action = prompt_failure_action(
        message=f"failed: {failed_id}\n{error_summary}",
        full_stderr=_stderr_full_from_failed(error_summary),
        yes=yes,
    )
    if action is FailureAction.SKIP:
        return transitions.ReconcileOutcome(
            item_id=failed_id,
            kind=ReconcileKind.PLUGIN,
            status=ReconcileStatus.SKIPPED,
            error_summary=error_summary,
        )
    if action is FailureAction.RETRY:
        retry_err = _retry_plugin_op(cfg, failed_id, op_kind)
        if retry_err is None:
            # Record the retry success in the appropriate piece so the
            # final delta reflects ground truth. ``op_kind == "unknown"``
            # is a no-op append (matches the prior elif chain) — today
            # that branch is dead because ``_retry_plugin_op`` already
            # returns a non-None error string for unknown kinds, so this
            # ``is None`` arm is only reached for the four mapped kinds.
            piece_field = _RETRY_PIECE_FIELD.get(op_kind)
            if piece_field is not None:
                getattr(retried, piece_field).append(failed_id)
            return transitions.ReconcileOutcome(
                item_id=failed_id,
                kind=ReconcileKind.PLUGIN,
                status=ReconcileStatus.RETRIED_OK,
                error_summary=None,
            )
        return transitions.ReconcileOutcome(
            item_id=failed_id,
            kind=ReconcileKind.PLUGIN,
            status=ReconcileStatus.SKIPPED,
            error_summary=retry_err,
        )
    # ABORT
    _abort_reverse_reconcile_plugins(delta_so_far)
    raise ReconcileAborted(
        f"install aborted during plugin reconcile (failed item: {failed_id!r})"
    )


def _abort_reverse_reconcile_plugins(delta: transitions.PluginDelta) -> None:
    """Reverse plugin/marketplace items landed so far this install (ABORT path).

    Reuses :func:`_reverse_plugins` to avoid duplicating the four
    uniform-inverse-op dispatch table — that function already runs the
    correct inverse for each delta field with per-item warn-and-continue
    semantics so the rollback completes even if a single inverse op
    fails. The reverse delta and failure list are discarded; the
    caller's :class:`ReconcileAborted` carries the abort reason and
    SetforgeError handler surfaces it.
    """
    _reverse_plugins(delta)


def _compute_plugin_delta(
    pre_plugins: Mapping[str, dict[str, Any]],
    pre_marketplaces: Mapping[str, dict[str, Any]],
    post_plugins: Mapping[str, dict[str, Any]],
    post_marketplaces: Mapping[str, dict[str, Any]],
) -> transitions.PluginDelta:
    """Diff pre/post claude-plugin disk state into a :class:`PluginDelta`.

    Disk state is ground truth: captures plugins that landed on disk
    regardless of whether their subsequent enable step succeeded. The
    old ``failed_plugin_ids`` filter collapsed install-failures and
    enable-failures into one set and excluded both — leaving
    install-then-enable-fail plugins orphaned at revert time.
    ``marketplaces_removed`` is always empty for install today
    (reconcile never auto-evicts marketplaces).
    """
    installed_pids = tuple(sorted(set(post_plugins) - set(pre_plugins)))
    common = set(pre_plugins) & set(post_plugins)
    enabled_pids = tuple(
        sorted(
            pid
            for pid in common
            if not pre_plugins[pid].get("enabled", True)
            and post_plugins[pid].get("enabled", True)
        )
    )
    disabled_pids = tuple(
        sorted(
            pid
            for pid in common
            if pre_plugins[pid].get("enabled", True)
            and not post_plugins[pid].get("enabled", True)
        )
    )
    added_mps = tuple(sorted(set(post_marketplaces) - set(pre_marketplaces)))
    return transitions.PluginDelta(
        installed=installed_pids,
        enabled=enabled_pids,
        disabled=disabled_pids,
        marketplaces_added=added_mps,
        marketplaces_removed=(),
    )


def _reverse_extensions(
    delta: transitions.ExtensionDelta,
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Apply the inverse of an extensions.json delta.

    Returns ``(reverse_added, reverse_removed, failed)``. Per-extension
    failures are caught (warn-and-continue) so the reverse transition
    still gets written; ``failed`` records ``(ext_id, error_msg)`` for
    logging by the caller.
    """
    reverse_added: list[str] = []
    reverse_removed: list[str] = []
    failed: list[tuple[str, str]] = []
    for ext_id in delta.added:
        try:
            vscode_extensions.uninstall_one(ext_id)
            reverse_removed.append(ext_id)
        except ExtensionToolMissing as exc:
            typer.secho(
                f"warning: skipping uninstall of {ext_id} — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )
        except ExtensionInstallFailed as exc:
            failed.append((ext_id, str(exc)))
            typer.secho(
                f"FAILED  uninstall {ext_id} — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )
    for ext_id in delta.removed:
        try:
            vscode_extensions.install_one(ext_id)
            reverse_added.append(ext_id)
        except ExtensionToolMissing as exc:
            typer.secho(
                f"warning: skipping install of {ext_id} — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )
        except ExtensionInstallFailed as exc:
            failed.append((ext_id, str(exc)))
            typer.secho(
                f"FAILED  install {ext_id} — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )
    return reverse_added, reverse_removed, failed


def _apply_inverse(
    items: Iterable[str],
    inverse_fn: Callable[[str], None],
    verb: str,
    success_list: list[str],
    failed: list[tuple[str, str]],
) -> None:
    """Apply ``inverse_fn`` to each item; record success and failure.

    Shared body for the four uniform inverse loops in
    :func:`_reverse_plugins` (uninstall / disable / enable /
    marketplace remove). ``verb`` is the operation name used in
    warning + failure log lines. ``success_list`` is appended to on
    success; ``failed`` is appended ``(item, error_msg)`` on
    subprocess errors. :class:`PluginToolMissing` is treated as a
    skip with a yellow warning (mirrors the extensions warn-and-skip
    path).
    """
    for item in items:
        try:
            inverse_fn(item)
            success_list.append(item)
        except PluginToolMissing as exc:
            typer.secho(
                f"warning: skipping {verb} of {item} — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            msg = str(exc)
            failed.append((item, msg))
            typer.secho(
                f"FAILED plugin {verb} {item} — {msg}",
                err=True,
                fg=typer.colors.YELLOW,
            )


def _apply_marketplace_re_add(
    items: Iterable[tuple[str, dict[str, str]]],
    success_list: list[tuple[str, dict[str, str]]],
    failed: list[tuple[str, str]],
) -> None:
    """Apply the inverse of ``marketplaces_removed``: re-register each marketplace.

    Distinct from :func:`_apply_inverse` because each entry is a
    ``(name, source_payload)`` pair, the payload must be validated
    through :class:`MarketplaceSource`, and the success record is a
    ``(name, dict)`` tuple — not a bare string. The pair shape is
    guaranteed by :attr:`transitions.PluginDelta.marketplaces_removed`,
    so no runtime shape check is needed.
    """
    for entry in items:
        name, source_payload = entry
        try:
            # source_payload is the JSON-round-tripped form (all str values
            # per PluginDelta.marketplaces_removed contract); pydantic
            # coerces them back into MarketplaceSourceKind / Path through
            # model_validate (accepts Any, runs full validation — avoids
            # the `# type: ignore[arg-type]` that the **-splat
            # construction would otherwise need).
            source = MarketplaceSource.model_validate(source_payload)
            claude_plugins_mod.marketplace_add(name, source)
            success_list.append((name, dict(source_payload)))
        except PluginToolMissing as exc:
            typer.secho(
                f"warning: skipping marketplace add of {name} — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            ValueError,
        ) as exc:
            msg = str(exc)
            failed.append((name, msg))
            typer.secho(
                f"FAILED plugin marketplace add {name} — {msg}",
                err=True,
                fg=typer.colors.YELLOW,
            )


@dataclass(frozen=True, slots=True)
class _PluginReverseOp:
    """One uniform inverse op on a :class:`PluginDelta`.

    Bundles the delta field name (which doubles as the
    :class:`PluginDelta` kwarg used to assemble the reverse delta), the
    inverse function to call per item, and the verb shown in
    warning / failure log lines. Replaces an earlier 3-tuple +
    separate stringly-typed accumulator dict whose two key sets had
    to be kept in lockstep — a typo on either side would surface as a
    runtime ``KeyError``. With this dataclass the field name lives in
    exactly one place per op.
    """

    delta_field: str
    inverse_fn: Callable[[str], None]
    verb: str


# Dispatch table for the four uniform inverse ops on a PluginDelta. The
# fifth field — marketplaces_removed — has a (name, dict) shape and is
# handled outside this table by :func:`_apply_marketplace_re_add`.
_REVERSE_PLUGIN_DISPATCH: tuple[_PluginReverseOp, ...] = (
    _PluginReverseOp("installed", claude_plugins_mod.plugin_uninstall, "uninstall"),
    _PluginReverseOp("enabled", claude_plugins_mod.plugin_disable, "disable"),
    _PluginReverseOp("disabled", claude_plugins_mod.plugin_enable, "enable"),
    _PluginReverseOp(
        "marketplaces_added",
        claude_plugins_mod.marketplace_remove,
        "marketplace remove",
    ),
)


def _reverse_plugins(
    delta: transitions.PluginDelta,
) -> tuple[transitions.PluginDelta, list[tuple[str, str]]]:
    """Apply the inverse of a plugins.json delta.

    Returns ``(reverse_delta, failed)``. ``reverse_delta`` reflects only
    the inverse operations that succeeded (mirrors
    :func:`_reverse_extensions`'s exclusion of failed ops, so a
    revert-of-revert never re-applies a no-op). Per-op
    :class:`PluginToolMissing` surfaces as a warn-and-skip; subprocess
    failures are caught and recorded so the loop continues.

    Four ops share the ``Iterable[str]`` shape and are driven by
    :data:`_REVERSE_PLUGIN_DISPATCH`; ``marketplaces_removed`` is
    handled separately because its entries round-trip through
    :class:`MarketplaceSource`.
    """
    # Accumulator dict keyed by the same field names the dispatch ops
    # carry, so the reverse-delta construction below pulls from the
    # same source of truth as the loop. A typo in ``_PluginReverseOp``'s
    # ``delta_field`` would surface immediately at the getattr call.
    accumulators: dict[str, list[str]] = {
        op.delta_field: [] for op in _REVERSE_PLUGIN_DISPATCH
    }
    reverse_mps_removed: list[tuple[str, dict[str, str]]] = []
    failed: list[tuple[str, str]] = []
    for op in _REVERSE_PLUGIN_DISPATCH:
        items: Iterable[str] = getattr(delta, op.delta_field)
        _apply_inverse(
            items, op.inverse_fn, op.verb, accumulators[op.delta_field], failed
        )
    _apply_marketplace_re_add(delta.marketplaces_removed, reverse_mps_removed, failed)
    reverse_delta = transitions.PluginDelta(
        marketplaces_removed=tuple(reverse_mps_removed),
        **{field: tuple(items) for field, items in accumulators.items()},
    )
    return reverse_delta, failed


def _write_reverse_transition(
    transition: Path,
    profile: str,
    touched_paths: Sequence[Path],
    file_pre: Mapping[Path, str | None],
) -> Path:
    """Reverse plugin/extension deltas from ``transition`` and write the redo record."""
    ext_file = transition / "extensions.json"
    reverse_added: list[str] = []
    reverse_removed: list[str] = []
    if ext_file.exists():
        ext_raw = json.loads(ext_file.read_text())
        ext_delta = transitions.extension_delta_from_json(ext_raw)
        reverse_added, reverse_removed, _ = _reverse_extensions(ext_delta)

    plugin_file = transition / "plugins.json"
    reverse_plugin_delta: transitions.PluginDelta | None = None
    if plugin_file.exists():
        plugin_raw = json.loads(plugin_file.read_text(encoding="utf-8"))
        plugin_payload = transitions.plugin_delta_from_json(plugin_raw)
        reverse_plugin_delta, _ = _reverse_plugins(plugin_payload)
        if reverse_plugin_delta.is_empty():
            reverse_plugin_delta = None

    file_post = transitions.snapshot_paths(touched_paths)
    reverse_meta = transitions.make_meta(transitions.TransitionCommand.REVERT, profile)
    reverse_delta: transitions.ExtensionDelta | None = None
    if reverse_added or reverse_removed:
        reverse_delta = transitions.ExtensionDelta(
            added=reverse_added, removed=reverse_removed
        )
    return transitions.write_transition(
        reverse_meta,
        file_pre,
        file_post,
        reverse_delta,
        plugin_delta=reverse_plugin_delta,
    )
