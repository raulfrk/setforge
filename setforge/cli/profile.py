"""``setforge profile`` subgroup — read-only profile introspection.

``profile list`` enumerates profiles in ``setforge.yaml`` with each
``extends:`` chain; ``profile show <name>`` resolves the profile and
renders every effective list (tracked_files / claude_plugins /
marketplaces / host_local_sections / bootstrap / extensions /
preserve_user_keys) with per-entry provenance tags answering "where
did this item come from?".

Read-only: no live mutation, no subprocess, no network.
:class:`SetforgeError` propagates to ``main()`` for ``exit 1``.

``local.yaml`` overlay surfaces (plugin / marketplaces / extensions
overrides, host_local_sections, preserve_user_keys overlay diff) land
with bd setforge-lgvp; until then the affected blocks print
:data:`_OVERLAY_PENDING_NOTE` instead of forging false provenance.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from setforge.cli import _CONFIG_OPTION, _resolve_config_arg, app
from setforge.cli._helpers import ProfileContext
from setforge.config import (
    Config,
    Profile,
    load_config,
    resolve_chain,
    resolve_profile,
)
from setforge.errors import SetforgeError

# Provenance placeholder for surfaces whose overlay machinery has not
# shipped yet. Cited bd id is the contract; do NOT replace with a TODO
# comment.
_OVERLAY_PENDING_NOTE: str = "(overlay surface lands when bd setforge-lgvp ships)"


def _build_console() -> Console:
    """Build the Console used by both subcommands.

    ``markup=False`` so square-bracket provenance tags like
    ``[from profile base]`` are emitted verbatim; with markup parsing
    enabled Rich treats the brackets as style tags and silently
    strips them.
    """
    return Console(markup=False, highlight=False)


profile_app: typer.Typer = typer.Typer(
    help="Inspect profile definitions and resolved overlays.",
    no_args_is_help=True,
)
app.add_typer(profile_app, name="profile")


@profile_app.command("list")
def profile_list(config: Path = _CONFIG_OPTION) -> None:
    """List every profile in ``setforge.yaml`` with its ``extends:`` chain.

    Profile order matches the source ``profiles:`` mapping order
    (insertion order from the YAML loader). Each row shows the profile
    name and, when ``extends:`` is set, the full chain root-first
    (``extends grand-parent -> parent``).
    """
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    console = _build_console()
    console.print(f"=== profiles defined in {config} ===")
    if not cfg.profiles:
        console.print("(no profiles defined)")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("name", no_wrap=True)
    table.add_column("extends")
    for name in cfg.profiles:
        chain = _format_extends_chain(cfg, name)
        table.add_row(name, chain)
    console.print(table)


@profile_app.command("show")
def profile_show(
    name: str = typer.Argument(..., help="Profile name from setforge.yaml."),
    config: Path = _CONFIG_OPTION,
) -> None:
    """Render the fully-resolved profile with per-entry provenance tags.

    Raises :class:`SetforgeError` (caught by the outer ``main()`` wrapper
    and printed as a red error before ``exit 1``) when ``name`` is not
    defined in ``setforge.yaml``.
    """
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    if name not in cfg.profiles:
        defined = ", ".join(cfg.profiles) if cfg.profiles else "(none)"
        raise SetforgeError(
            f"profile {name!r} not defined in {config}; defined profiles: {defined}"
        )
    resolved = resolve_profile(cfg, name)
    repo_root = config.resolve().parent
    ctx = ProfileContext(cfg=cfg, resolved=resolved, repo_root=repo_root, profile=name)
    console = _build_console()
    header = _format_show_header(cfg, name)
    console.print(header)
    _render_tracked_files(ctx, console)
    _render_plugins(ctx, console)
    _render_marketplaces(ctx, console)
    _render_host_local_sections(ctx, console)
    _render_bootstrap(ctx, console)
    _render_extensions(ctx, console)
    _render_preserve_user_keys(ctx, console)


# ---------------------------------------------------------------------------
# Helpers — extends chain + provenance tagging
# ---------------------------------------------------------------------------


def _format_extends_chain(cfg: Config, name: str) -> str:
    """Render the ``extends:`` chain for one profile, root-first.

    Returns an empty string when the profile has no parent. ``A -> B``
    means ``B`` extends ``A``. Walks the chain via
    :func:`setforge.config.resolve_chain` so cycle detection stays in
    one place.
    """
    chain = resolve_chain(cfg, name)
    if len(chain) <= 1:
        return ""
    parents = [_chain_label(cfg, profile) for profile in chain[:-1]]
    return " -> ".join(parents)


def _chain_label(cfg: Config, profile: Profile) -> str:
    """Return the YAML key that maps to ``profile`` in ``cfg.profiles``.

    :func:`resolve_chain` returns :class:`Profile` instances; the
    rendered chain needs the name keys. Falls back to ``?`` when no
    match is found (defensive — resolve_chain only yields profiles
    that are in ``cfg.profiles``, so the fallback is unreachable in
    practice but keeps the type signature total).
    """
    for key, candidate in cfg.profiles.items():
        if candidate is profile:
            return key
    return "?"


def _format_show_header(cfg: Config, name: str) -> str:
    """Build the ``=== profile <name> (extends ...) ===`` banner."""
    chain = _format_extends_chain(cfg, name)
    if chain:
        return f"=== profile {name} (extends {chain}) ==="
    return f"=== profile {name} ==="


def _tag_provenance[T](
    item: T,
    *,
    chain_resolved_by_name: list[tuple[str, set[T]]],
    overlay_add: frozenset[T] = frozenset(),
    overlay_remove: frozenset[T] = frozenset(),
    leaf_name: str,
) -> str:
    """Return the provenance tag for one resolved-list item.

    Resolution order:

    1. If ``item`` appears in ``overlay_remove`` it was removed via
       ``local.yaml`` overlay — tag ``[removed via local.yaml]``.
    2. If ``item`` appears in ``overlay_add`` it was added via
       ``local.yaml`` overlay — tag ``[from local.yaml]``.
    3. If ``item`` first appears in the chain at ancestor ``X`` —
       tag ``[from profile X]``. ``chain_resolved_by_name`` is
       walked root-first; the first hit wins.
    4. Otherwise (introduced by the leaf profile) — tag
       ``[from profile <leaf_name>]``.

    ``overlay_add`` and ``overlay_remove`` are typed as
    :class:`frozenset` so callers build the set ONCE above their
    per-item loop instead of paying ``O(items * overlay)`` to
    re-materialize on every call. The default ``frozenset()`` is safe
    as a mutable-default avatar (frozensets are immutable).
    """
    if item in overlay_remove:
        return "[removed via local.yaml]"
    if item in overlay_add:
        return "[from local.yaml]"
    for ancestor_name, ancestor_items in chain_resolved_by_name:
        if item in ancestor_items:
            return f"[from profile {ancestor_name}]"
    return f"[from profile {leaf_name}]"


def _chain_resolved_by_name_field(
    cfg: Config, name: str, *, field: str
) -> list[tuple[str, set[str]]]:
    """Pre-compute resolved-up-to-ancestor item sets for one list field.

    ``field`` selects one of the flattened list-shaped attributes on
    :class:`ResolvedProfile` (``tracked_files``, ``claude_plugins``,
    ``bootstrap``). For every ancestor X in the extends chain
    (root → just-before-leaf), runs :func:`resolve_profile` on X and
    records its resolved items as a set, paired with the ancestor
    name. Returns ``[(ancestor_name, items_set), ...]`` ordered
    root-first so :func:`_tag_provenance` finds the earliest-introducing
    ancestor.
    """
    chain = resolve_chain(cfg, name)
    out: list[tuple[str, set[str]]] = []
    for ancestor_profile in chain[:-1]:
        ancestor_name = _chain_label(cfg, ancestor_profile)
        ancestor_resolved = resolve_profile(cfg, ancestor_name)
        items = getattr(ancestor_resolved, field)
        out.append((ancestor_name, {str(x) for x in items}))
    return out


# ---------------------------------------------------------------------------
# Section renderers — one per logical block in the mockup
# ---------------------------------------------------------------------------


def _render_tracked_files(ctx: ProfileContext, console: Console) -> None:
    """Render the ``tracked_files`` table with provenance tags."""
    items = ctx.resolved.tracked_files
    console.print(f"tracked_files ({len(items)} effective):")
    if not items:
        console.print("  (none)")
        return
    chain_by_name = _chain_resolved_by_name_field(
        ctx.cfg, ctx.profile, field="tracked_files"
    )
    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True)
    table.add_column()
    for tf_name in items:
        tag = _tag_provenance(
            tf_name,
            chain_resolved_by_name=chain_by_name,
            leaf_name=ctx.profile,
        )
        table.add_row(tf_name, tag)
    console.print(table)


def _render_plugins(ctx: ProfileContext, console: Console) -> None:
    """Render the resolved ``claude_plugins`` list with provenance."""
    items = ctx.resolved.claude_plugins
    console.print(f"claude_plugins ({len(items)} effective):")
    if not items:
        console.print("  (none)")
        return
    chain_by_name = _chain_resolved_by_name_field(
        ctx.cfg, ctx.profile, field="claude_plugins"
    )
    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True)
    table.add_column()
    for plugin_name in items:
        tag = _tag_provenance(
            plugin_name,
            chain_resolved_by_name=chain_by_name,
            leaf_name=ctx.profile,
        )
        table.add_row(plugin_name, tag)
    console.print(table)


def _render_marketplaces(ctx: ProfileContext, console: Console) -> None:
    """Render the global ``marketplaces:`` mapping.

    Marketplaces live on :class:`Config`, not on :class:`Profile`, so
    every profile in the file sees the same registry. Provenance per
    entry is therefore the config file itself; the rendering shows the
    count and lists the (name, source-kind) pairs without per-entry
    profile tags. ``local.yaml`` marketplace overrides land via bd
    setforge-lgvp.
    """
    items = ctx.cfg.marketplaces
    console.print(f"marketplaces ({len(items)} effective):")
    if not items:
        console.print("  (none)")
        return
    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column()
    for mp_name, source in items.items():
        target = source.repo if source.repo is not None else str(source.path)
        table.add_row(mp_name, source.source.value, target)
    console.print(table)
    console.print(f"  {_OVERLAY_PENDING_NOTE}")


def _render_host_local_sections(ctx: ProfileContext, console: Console) -> None:
    """Placeholder section for the upcoming host_local_sections overlay.

    The ``host_local_sections:`` block in ``~/.config/setforge/local.yaml``
    is sketched as a commented stub in :mod:`setforge.binaries` but has
    no loader yet; the data shape lands with bd setforge-lgvp. Until
    then this renderer prints the section title with the pending-overlay
    note so the user knows the surface is acknowledged but empty.
    """
    del ctx  # No data to read until bd setforge-lgvp ships the loader.
    console.print("host_local_sections (0 effective):")
    console.print(f"  {_OVERLAY_PENDING_NOTE}")


def _render_bootstrap(ctx: ProfileContext, console: Console) -> None:
    """Render the resolved ``bootstrap`` paths with provenance tags."""
    items = ctx.resolved.bootstrap
    console.print(f"bootstrap ({len(items)} effective):")
    if not items:
        console.print("  (none)")
        return
    chain_by_name = _chain_resolved_by_name_field(
        ctx.cfg, ctx.profile, field="bootstrap"
    )
    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True)
    table.add_column()
    for path in items:
        path_str = str(path)
        tag = _tag_provenance(
            path_str,
            chain_resolved_by_name=chain_by_name,
            leaf_name=ctx.profile,
        )
        table.add_row(path_str, tag)
    console.print(table)


def _render_extensions(ctx: ProfileContext, console: Console) -> None:
    """Render the resolved ``extensions.include`` list with provenance."""
    include = ctx.resolved.extensions.include
    exclude = ctx.resolved.extensions.exclude
    console.print(f"extensions.include ({len(include)} effective):")
    if include:
        chain_by_name = _extensions_chain_by_name(ctx.cfg, ctx.profile)
        table = Table.grid(padding=(0, 2))
        table.add_column(no_wrap=True)
        table.add_column()
        for ext_id in include:
            tag = _tag_provenance(
                ext_id,
                chain_resolved_by_name=chain_by_name,
                leaf_name=ctx.profile,
            )
            table.add_row(ext_id, tag)
        console.print(table)
    else:
        console.print("  (none)")
    if exclude:
        console.print(f"extensions.exclude ({len(exclude)}):")
        for ext_id in exclude:
            console.print(f"  {ext_id}")


def _extensions_chain_by_name(cfg: Config, name: str) -> list[tuple[str, set[str]]]:
    """Like :func:`_chain_resolved_by_name_field` but for extensions.include.

    Extensions are nested inside ``ResolvedProfile.extensions`` rather
    than living on the top-level resolved object, so the generic
    ``getattr(..., field)`` shape doesn't apply directly.
    """
    chain = resolve_chain(cfg, name)
    out: list[tuple[str, set[str]]] = []
    for ancestor_profile in chain[:-1]:
        ancestor_name = _chain_label(cfg, ancestor_profile)
        ancestor_resolved = resolve_profile(cfg, ancestor_name)
        out.append(
            (
                ancestor_name,
                {str(x) for x in ancestor_resolved.extensions.include},
            )
        )
    return out


def _render_preserve_user_keys(ctx: ProfileContext, console: Console) -> None:
    """Render preserve_user_keys per tracked file in the resolved profile.

    Collects every tracked_file referenced by the resolved profile that
    declares a non-empty ``preserve_user_keys`` (or
    ``preserve_user_keys_deep``) list, and prints one block per file
    with the key paths inline. The ``local.yaml`` overlay diff for these
    keys is out of scope for this bead; the bd setforge-lgvp citation
    line communicates that to the reader.
    """
    rows: list[tuple[str, list[str]]] = []
    for tf_name in ctx.resolved.tracked_files:
        tf = ctx.cfg.tracked_files.get(tf_name)
        if tf is None:
            continue
        keys = list(tf.preserve_user_keys) + list(tf.preserve_user_keys_deep)
        if keys:
            rows.append((tf_name, keys))
    console.print(f"preserve_user_keys: {len(rows)} files")
    for tf_name, keys in rows:
        joined = ", ".join(keys)
        console.print(f"  {tf_name}: {len(keys)} keys ({joined})")
    console.print(f"  {_OVERLAY_PENDING_NOTE}")
