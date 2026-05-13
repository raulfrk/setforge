"""Interactive wizard for the marketplace cache-collision branch.

When LOCAL_CLONE mode encounters a cache directory whose existing
``git remote get-url origin`` does not match the YAML-declared
``MarketplaceSource.repo`` (URL drift), today's behavior is to
silently ``shutil.rmtree`` the cache and re-clone. That hides
genuine config divergence — a hostile or stale YAML change can wipe
work cached on the host without warning.

This module replaces the silent path with a four-option prompt:

- ``[k]eep existing``  — use the existing cache content as-is for this
  invocation. The new ``source.repo`` is NOT applied.
- ``[u]pdate``         — re-clone with the new content (drop existing
  cache). This is today's silent behavior, now opt-in.
- ``[b]oth``           — keep the existing cache AND create a new
  cache under a name the user supplies, for this invocation only.
  Name is validated against the same path-traversal guard as
  :func:`my_setup.claude_plugins._safe_cache_dir`.
- ``[a]bort``          — raise :class:`typer.Abort`.

Non-interactive entry (``auto=True`` or non-TTY stdin) refuses to
silently auto-pick: it raises :class:`MarketplaceCacheMiss` with a
remediation message instructing the user to fix the YAML or
``rm -rf`` the colliding cache directory manually. This is the
spec-locked safe-default — never silently destroy user state.

The collision-resolution choice lives only for the current
invocation; the YAML stays unchanged. Persistent renames belong in
the YAML edit surface, not in the wizard.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import typer

from my_setup.errors import MarketplaceCacheMiss

#: Default name-input prompt for the ``[b]oth`` branch. Module-level so
#: tests can monkeypatch it cleanly without altering the function
#: signature.
_NAME_PROMPT: str = (
    "Name for new cache subdir "
    "(must not match existing names; alphanumeric + hyphens + underscores): "
)


class CollisionAction(StrEnum):
    """Outcomes a wizard call can produce.

    ``KEEP`` — caller reuses the existing cache_dir as-is.
    ``UPDATE`` — caller drops the existing cache_dir and re-clones with
    the new source.
    ``BOTH`` — caller creates a new cache_dir at ``new_cache_dir``
    (set on :class:`CollisionResolution`) for the new source; the
    existing cache_dir stays untouched.
    ``ABORT`` — never returned; :func:`resolve_collision` raises
    :class:`typer.Abort` directly so the caller doesn't have to.
    """

    KEEP = "keep"
    UPDATE = "update"
    BOTH = "both"
    ABORT = "abort"


@dataclass(frozen=True, slots=True)
class CollisionResolution:
    """Result of a wizard call.

    ``action`` says which branch was chosen. ``new_cache_dir`` is set
    only when ``action`` is :data:`CollisionAction.BOTH` — it points
    at the (not-yet-created) directory the caller will clone into.
    """

    action: CollisionAction
    new_cache_dir: Path | None = None


def _is_valid_subdir_name(name: str) -> bool:
    """Return ``True`` if ``name`` is safe to use as a cache subdir.

    Mirrors :func:`my_setup.claude_plugins._safe_cache_dir`'s rejection
    rules without the resolved-path check (which the caller still does).
    Accepts alphanumeric + ``-`` + ``_`` only — no dots, no separators,
    no empty strings.
    """
    if not name:
        return False
    if name in (".", ".."):
        return False
    return all(c.isalnum() or c in ("-", "_") for c in name)


def _render_prompt(
    mp_name: str,
    cache_dir: Path,
    existing_origin: str,
    new_repo: str,
) -> str:
    """Format the prompt text for the four-option header."""
    subdir = cache_dir.name
    return (
        f"Cache collision detected for marketplace '{mp_name}':\n"
        f"  cache dir:     {cache_dir}/\n"
        f"  existing repo: {existing_origin}\n"
        f"  new source:    {new_repo}\n"
        f"\n"
        f"  [k]eep existing  use existing cache content as-is for "
        f"'{mp_name}'\n"
        f"  [u]pdate         re-clone with new content (drop existing "
        f"cache)\n"
        f"  [b]oth           keep existing AND create a new cache for "
        f"'{mp_name}'\n"
        f"                   under a name you choose (this invocation only;\n"
        f"                   YAML is not modified)\n"
        f"  [a]bort          stop install\n"
        f"\n"
        f"existing cache subdir: {subdir!r}\n"
        f"Choose [k/u/b/a]: "
    )


def resolve_collision(
    *,
    mp_name: str,
    cache_dir: Path,
    cache_root: Path,
    existing_origin: str,
    new_repo: str,
    auto: bool = False,
    prompt_fn: Callable[[str], str] | None = None,
    name_prompt_fn: Callable[[str], str] | None = None,
    stdin_is_tty: Callable[[], bool] | None = None,
) -> CollisionResolution:
    """Resolve a marketplace cache URL-drift collision interactively.

    Parameters
    ----------
    mp_name:
        Marketplace name (the YAML-side key) — for error / prompt text.
    cache_dir:
        The existing cache directory whose origin URL drifted.
    cache_root:
        Parent of all marketplace caches. Used to validate that any
        ``[b]oth`` rename stays inside the root (via the same guard
        rules as :func:`_safe_cache_dir`).
    existing_origin:
        The remote URL ``git remote get-url origin`` reported.
    new_repo:
        The new ``source.repo`` from YAML.
    auto:
        When ``True`` (e.g. ``--auto`` flag), bypass the prompt and
        raise :class:`MarketplaceCacheMiss` — the spec-locked safe
        default. Never silently auto-pick: silent auto-update would
        wipe user state; silent auto-keep would mask divergence.
    prompt_fn:
        Override the action prompter for tests. Default uses
        :func:`typer.prompt`.
    name_prompt_fn:
        Override the rename prompter for tests. Default uses
        :func:`typer.prompt` with :data:`_NAME_PROMPT`.
    stdin_is_tty:
        Override TTY detection for tests. Default checks
        ``sys.stdin.isatty()``.

    Returns
    -------
    CollisionResolution
        Carries the chosen action and (for ``BOTH``) the new cache_dir.

    Raises
    ------
    MarketplaceCacheMiss
        When ``auto`` is ``True``, or stdin is not a TTY, or the user
        repeatedly supplies an invalid rename. The message names the
        marketplace and lists the two manual remediations.
    typer.Abort
        When the user selects ``[a]bort``.
    """
    if stdin_is_tty is None:
        stdin_is_tty = sys.stdin.isatty
    if auto or not stdin_is_tty():
        raise MarketplaceCacheMiss(
            f"marketplace {mp_name!r}: cache collision (existing origin "
            f"{existing_origin!r} != new {new_repo!r}) and no TTY / "
            f"--auto set. Either fix the YAML to match the existing "
            f"cache, or `rm -rf {cache_dir}` to drop it manually."
        )

    def _default_prompt(msg: str) -> str:
        return typer.prompt(msg, prompt_suffix="")

    if prompt_fn is None:
        prompt_fn = _default_prompt
    if name_prompt_fn is None:
        name_prompt_fn = _default_prompt

    header = _render_prompt(mp_name, cache_dir, existing_origin, new_repo)
    while True:
        choice = prompt_fn(header).strip().lower()
        if choice in ("k", "keep"):
            return CollisionResolution(action=CollisionAction.KEEP)
        if choice in ("u", "update"):
            return CollisionResolution(action=CollisionAction.UPDATE)
        if choice in ("b", "both"):
            return _resolve_both(
                mp_name=mp_name,
                cache_root=cache_root,
                name_prompt_fn=name_prompt_fn,
            )
        if choice in ("a", "abort"):
            raise typer.Abort()
        # Anything else: re-prompt with a hint.
        header = "Enter k, u, b, or a: "


def _resolve_both(
    *,
    mp_name: str,
    cache_root: Path,
    name_prompt_fn: Callable[[str], str],
) -> CollisionResolution:
    """Prompt for a new cache subdir name with retries.

    Caps retries at 3 (validation failures) before bailing to
    :class:`MarketplaceCacheMiss` — prevents an infinite loop when the
    user can't type a valid name. The check matches
    :func:`my_setup.claude_plugins._safe_cache_dir` rejection rules:
    rejects empty, ``.``, ``..``, anything containing ``/`` or ``\\``,
    and anything outside ``[A-Za-z0-9_-]``. Also rejects a name that
    already resolves to an existing entry under ``cache_root`` (any
    file or directory) — silently overwriting would defeat the
    purpose of ``[b]oth``.
    """
    cache_root_resolved = cache_root.resolve()
    for _ in range(3):
        candidate = name_prompt_fn(_NAME_PROMPT).strip()
        if not _is_valid_subdir_name(candidate):
            typer.echo(
                f"invalid name {candidate!r}: must be non-empty and "
                f"contain only [A-Za-z0-9_-].",
                err=True,
            )
            continue
        new_dir = cache_root / candidate
        if new_dir.resolve().parent != cache_root_resolved:
            typer.echo(
                f"invalid name {candidate!r}: resolved path escapes cache_root.",
                err=True,
            )
            continue
        if new_dir.exists():
            typer.echo(
                f"name {candidate!r} already exists at {new_dir}; "
                f"pick a different name.",
                err=True,
            )
            continue
        return CollisionResolution(
            action=CollisionAction.BOTH,
            new_cache_dir=new_dir,
        )
    raise MarketplaceCacheMiss(
        f"marketplace {mp_name!r}: too many invalid cache-rename inputs; "
        f"aborting. Re-run and supply a name in [A-Za-z0-9_-]+ that does "
        f"not collide with an existing cache subdir."
    )
