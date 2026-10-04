"""The ordered registry of concrete schema migrations.

Importing this module imports every migration step, and through them the
engine modules the steps drive. Only code that enumerates or walks the
chain (``setforge migrate``, the schema gates) imports it; everything else
takes the version helpers and the :class:`Migration` Protocol from
:mod:`setforge.migrations`, which stays free of the steps.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from ruamel.yaml.comments import CommentedMap

from setforge import migrations as _framework
from setforge.errors import ConfigError
from setforge.migrations import (
    FeatureGate,
    Migration,
    RestampMigration,
    VersionStampMigration,
    parse_schema_version,
)
from setforge.migrations._contract_2_0 import Contract20Migration
from setforge.migrations._disposition_retire import DispositionRetireMigration
from setforge.migrations._marker_retire import MarkerRetireMigration
from setforge.migrations._profile_fields_retire import ProfileFieldsRetireMigration
from setforge.migrations._span_surface_retire import SpanSurfaceRetireMigration
from setforge.migrations._span_types_retire import SpanTypesRetireMigration
from setforge.migrations._yaml_ops import _has_tracked_file_field

__all__ = [
    "MIGRATIONS",
    "find_migration_path",
    "known_versions",
]


def _uses_platform_assets(data: CommentedMap) -> bool:
    packages = data.get("packages")
    if isinstance(packages, Mapping) and any(
        isinstance(package, Mapping) and "assets" in package
        for package in packages.values()
    ):
        return True
    bundles = data.get("bundles")
    if not isinstance(bundles, Mapping):
        return False
    for bundle in bundles.values():
        if not isinstance(bundle, Mapping):
            continue
        components = bundle.get("components")
        if not isinstance(components, Sequence) or isinstance(components, (str, bytes)):
            continue
        if any(
            isinstance(component, Mapping)
            and isinstance(component.get("github_release"), Mapping)
            and "assets" in component["github_release"]
            for component in components
        ):
            return True
    return False


def _uses_codex(data: CommentedMap) -> bool:
    profiles = data.get("profiles")
    profile_uses_codex = isinstance(profiles, dict) and any(
        isinstance(profile, dict) and "codex" in profile
        for profile in profiles.values()
    )
    return "codex" in data or profile_uses_codex


def _uses_scoped_codex_mcp(data: CommentedMap) -> bool:
    codex = data.get("codex")
    servers = codex.get("mcp_servers") if isinstance(codex, dict) else None
    return isinstance(servers, dict) and any(
        isinstance(server, dict) and ("scope" in server or "project" in server)
        for server in servers.values()
    )


MIGRATIONS: Final[tuple[Migration, ...]] = (
    VersionStampMigration(),
    RestampMigration(from_version="1.1", to_version="1.2"),
    Contract20Migration(),
    MarkerRetireMigration(),
    DispositionRetireMigration(),
    SpanSurfaceRetireMigration(),
    SpanTypesRetireMigration(),
    ProfileFieldsRetireMigration(),
    RestampMigration(
        from_version="6.0",
        to_version="6.1",
        gate=FeatureGate(
            enable_description="enable typed generated tracked-file resources",
            disable_description="disable generated resources when none are declared",
            in_use=lambda data: _has_tracked_file_field(data, "generated"),
            refusal=(
                "cannot downgrade schema 6.1 while generated tracked-file "
                "intent is declared; remove it before downgrading"
            ),
        ),
    ),
    RestampMigration(
        from_version="6.1",
        to_version="6.2",
        gate=FeatureGate(
            enable_description="enable managed directory trees",
            disable_description="disable managed trees when none are declared",
            in_use=lambda data: _has_tracked_file_field(data, "tree"),
            refusal=(
                "cannot downgrade schema 6.2 while managed tree intent is declared; "
                "remove it before downgrading"
            ),
        ),
    ),
    RestampMigration(
        from_version="6.2",
        to_version="6.3",
        gate=FeatureGate(
            enable_description="enable platform-qualified release assets",
            disable_description=(
                "disable platform release assets when none are declared"
            ),
            in_use=_uses_platform_assets,
            refusal=(
                "cannot downgrade schema 6.3 while platform release assets are "
                "declared; remove them before downgrading"
            ),
        ),
    ),
    RestampMigration(
        from_version="6.3",
        to_version="6.4",
        gate=FeatureGate(
            enable_description="enable product-aware Codex declarations",
            disable_description="disable the Codex contract when unused",
            in_use=_uses_codex,
            refusal=(
                "cannot downgrade schema 6.4 while Codex declarations are "
                "present; remove them before downgrading"
            ),
        ),
    ),
    RestampMigration(
        from_version="6.4",
        to_version="6.5",
        gate=FeatureGate(
            enable_description="enable project-scoped Codex MCP declarations",
            disable_description="disable project-scoped Codex MCP declarations",
            in_use=_uses_scoped_codex_mcp,
            refusal=(
                "cannot downgrade schema 6.5 while scoped Codex MCP "
                "declarations are present"
            ),
        ),
    ),
)
"""Ordered registry of available FORWARD migrations.

Holds the version-stamp chain 1.0 → 1.1 (:class:`VersionStampMigration`)
→ 1.2 (:class:`RestampMigration`) → 2.0 (:class:`Contract20Migration`, the
breaking preserve_* contraction) → 2.1 (:class:`MarkerRetireMigration`)
→ 3.0 (:class:`DispositionRetireMigration`) → 4.0
(:class:`SpanSurfaceRetireMigration`) → 5.0 (:class:`SpanTypesRetireMigration`)
→ 6.0 (:class:`ProfileFieldsRetireMigration`) → 6.1 → 6.2 → 6.3 → 6.4 → 6.5
(each a gated :class:`RestampMigration`). Future migrations are
appended in ``from_version`` order so :func:`find_migration_path` can
walk the chain forward. Each migration's reverse is attached to its
forward instance, never added here — that would make the forward walk
cycle (see :class:`VersionStampMigration`).
"""


def known_versions() -> frozenset[str]:
    """Every schema version the current registry can resolve to.

    The build's :data:`current_expected_schema_version` plus every
    ``from_version`` / ``to_version`` in :data:`MIGRATIONS`. The
    ``migrate --to`` CLI validates a user-supplied target against this set
    so an unknown version errors cleanly instead of falling through a
    string-range "reachable" check. (``migrate --pin`` validates against an
    equivalent set it builds inline.)
    """
    versions = {_framework.current_expected_schema_version}
    for m in MIGRATIONS:
        versions.add(m.from_version)
        versions.add(m.to_version)
    return frozenset(versions)


def find_migration_path(*, from_v: str, to_v: str) -> tuple[Migration, ...]:
    """Find a chain from ``from_v`` to ``to_v`` — walking forward OR backward.

    Direction is decided **semantically** (:func:`parse_schema_version`
    → ``(int, int)``), never by string sort, so the 1.9 ↔ 1.10 boundary
    is correct.

    - ``to_v == from_v`` → ``()`` (nothing to do).
    - ``to_v`` newer → forward chain via :data:`MIGRATIONS` (each step
      picks the migration whose ``from_version`` matches the cursor).
    - ``to_v`` older → reverse chain: at each step pick the forward
      migration whose ``to_version`` matches the cursor and append its
      ``.reverse`` (the registry itself stays forward-only).

    Returns ``()`` when no chain bridges the two versions. The walk is
    bounded by ``len(MIGRATIONS) + 1`` in BOTH directions, so an
    unreachable target terminates with ``()`` instead of looping.

    Raises :class:`ConfigError` (never a bare ``ValueError`` /
    ``IndexError``) when either version is not a valid ``MAJOR.MINOR``
    token.
    """
    from_t = parse_schema_version(from_v)
    to_t = parse_schema_version(to_v)
    if from_t == to_t:
        return ()
    chain: list[Migration] = []
    cursor = from_t
    bound = len(MIGRATIONS) + 1
    forward = to_t > from_t
    for _ in range(bound):
        if cursor == to_t:
            return tuple(chain)
        if forward:
            match = next(
                (
                    m
                    for m in MIGRATIONS
                    if parse_schema_version(m.from_version) == cursor
                ),
                None,
            )
            if match is None:
                return ()
            chain.append(match)
            cursor = parse_schema_version(match.to_version)
        else:
            match = next(
                (m for m in MIGRATIONS if parse_schema_version(m.to_version) == cursor),
                None,
            )
            if match is None:
                return ()
            chain.append(match.reverse)
            cursor = parse_schema_version(match.from_version)
    return ()


def _validate_registry() -> None:
    """Assert every registered migration carries a correctly-swapped ``reverse``.

    ``@runtime_checkable`` Protocols verify attribute *names* at
    isinstance time but do NOT check property presence or behavior, so a
    migration appended to :data:`MIGRATIONS` without a ``reverse`` (or
    with a mis-swapped one) would crash only at downgrade time, deep in
    the reverse walk. This import-time guard turns that latent failure
    into a loud one at module load.
    """
    for m in MIGRATIONS:
        rev = m.reverse
        if rev.from_version != m.to_version or rev.to_version != m.from_version:
            raise ConfigError(
                f"migration {type(m).__name__} has a mis-swapped reverse: "
                f"forward {m.from_version}->{m.to_version}, "
                f"reverse {rev.from_version}->{rev.to_version}"
            )


_validate_registry()
