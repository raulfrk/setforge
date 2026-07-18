"""Shared $HOME-containment check + warn for deploy destinations.

A resolved deploy ``dst`` that lands outside ``$HOME`` is a self-trust
warning, not a refusal: setforge deploys anyway and tells the user how to
silence the warning. Both the plain-``tracked_files`` path
(:func:`setforge.compare.warn_if_dst_outside_home`) and the bundle
``file``-component gate (:func:`setforge.config._gate_component_paths`)
funnel through here so the check and the warn message stay identical.

This module lives outside ``compare`` / ``config`` on purpose: ``config``
cannot import ``compare`` (``compare`` imports ``config`` — a cycle), so the
shared helper needs a neutral home that depends on neither. It imports only
the stdlib and ``typer``.

Callers pass an already-``.resolve()``-d path; ``.resolve()`` (not a string
prefix) is what collapses ``../`` and symlinked parents, catching a
``~/../etc`` or symlink-escape a ``startswith()`` would miss.
"""

from __future__ import annotations

from pathlib import Path

import typer


def is_outside_home(resolved: Path, home: Path) -> bool:
    """True when the resolved dst is neither ``home`` itself nor under it.

    Both operands MUST already be ``.resolve()``-d by the caller so the
    parent walk sees a canonical path (``../`` collapsed, symlinks followed).
    """
    return resolved != home and home not in resolved.parents


def warn_outside_home_dst(
    *,
    label: str,
    raw_dst: str,
    resolved: Path,
    home: Path,
    silence_hint: str,
) -> None:
    """Emit the shared out-of-$HOME warning to stderr (yellow).

    ``label`` names the offending entity (e.g. ``tracked_file`` or
    ``bundle file component 'x.y'``); ``silence_hint`` names the opt-out
    (e.g. ``set allow_outside_home: true on this tracked_file``).
    """
    typer.secho(
        f"warning: {label} dst {raw_dst!r} resolves to {resolved} — outside "
        f"$HOME ({home}); deploying anyway ({silence_hint} to silence).",
        err=True,
        fg=typer.colors.YELLOW,
    )
