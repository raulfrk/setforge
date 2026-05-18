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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from setforge import claude_plugins as claude_plugins_mod
from setforge import transitions, vscode_extensions
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
)


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


def _reconcile_extensions(
    resolved: ResolvedProfile,
) -> transitions.ExtensionDelta | None:
    """Reconcile VSCode extensions for ``resolved`` and emit progress lines.

    Returns the :class:`ExtensionDelta` describing what landed on disk, or
    ``None`` when the underlying tool is missing (warn-and-skip). Pure
    data in/out so :func:`install` can stay a thin orchestrator.
    """
    try:
        report = vscode_extensions.reconcile(resolved.extensions)
    except ExtensionToolMissing as exc:
        typer.secho(
            f"warning: skipping extension reconcile — {exc}",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return None
    failed_ids = {ext_id for ext_id, _ in report.failed}
    for ext_id in report.to_install:
        if ext_id not in failed_ids:
            typer.echo(f"installed  {ext_id}")
    for ext_id in report.to_uninstall:
        if ext_id not in failed_ids:
            typer.echo(f"uninstalled  {ext_id}")
    for ext_id, err in report.failed:
        typer.secho(f"FAILED  {ext_id} — {err}", err=True, fg=typer.colors.YELLOW)
    if not report:
        typer.echo("extensions: nothing to reconcile")
    return transitions.ExtensionDelta(
        added=[i for i in report.to_install if i not in failed_ids],
        removed=[i for i in report.to_uninstall if i not in failed_ids],
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


def _reconcile_plugins(
    cfg: Config,
    resolved: ResolvedProfile,
) -> transitions.PluginDelta | None:
    """Reconcile Claude plugins and compute the install-time :class:`PluginDelta`.

    Snapshots disk state pre/post so the delta reflects ground truth:
    install-then-enable-fail plugins still appear in
    :attr:`PluginDelta.installed` and survive revert. Returns ``None``
    when the ``claude`` binary is absent (warn-and-skip).
    """
    try:
        pre_plugins = claude_plugins_mod.list_installed()
        pre_marketplaces = claude_plugins_mod.list_marketplaces()
    except PluginToolMissing as exc:
        _warn_skip_reconcile(exc)
        return None
    try:
        plugin_report = claude_plugins_mod.reconcile(cfg, resolved)
    except PluginToolMissing as exc:
        _warn_skip_reconcile(exc)
        return None
    _emit_plugin_report(plugin_report)
    post_plugins = claude_plugins_mod.list_installed()
    post_marketplaces = claude_plugins_mod.list_marketplaces()
    return _compute_plugin_delta(
        pre_plugins, pre_marketplaces, post_plugins, post_marketplaces
    )


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
