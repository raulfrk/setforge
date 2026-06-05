"""Frozen field manifest for the ``setforge.yaml`` schema — additive-only gate.

The additive-first guarantee (``COMPATIBILITY.md``) says a schema field is
never removed, renamed, or retyped *within a major version* — only added.
That invariant is what makes forward-tolerant reading (``extra="ignore"``)
safe: an older engine can ignore a newer minor's extra fields precisely
because the fields it *does* know never changed meaning.

The invariant cannot be checked at runtime (an old engine has no knowledge
of fields added after it shipped). It is a property of the engine's own
schema across releases, so it is enforced at TEST time
(``test_schema_additivity``): the live Pydantic models are compared against
:data:`FROZEN_FIELD_MANIFEST`. A same-major removal/retype fails the gate,
forcing a major ``schema_version`` bump (and a fresh manifest section). An
addition fails too, until the manifest is updated in the same commit — so
the manifest can never silently drift.

This is the minimal in-wave seed of the broader field-removal CI gate
(p5qc.14.9), which extends the same manifest with migration-coverage and
reverse-required enforcement.
"""

from __future__ import annotations

from pydantic import BaseModel

from setforge.config import (
    ClaudePluginRef,
    Config,
    Extensions,
    MarketplaceSource,
    Profile,
    ResolvedProfile,
    TrackedFile,
)

SCHEMA_MAJOR: int = 1
"""The major version :data:`FROZEN_FIELD_MANIFEST` describes."""

_MODELS: tuple[type[BaseModel], ...] = (
    Config,
    Profile,
    TrackedFile,
    Extensions,
    MarketplaceSource,
    ClaudePluginRef,
    ResolvedProfile,
)


def _field_fingerprint(model: type[BaseModel]) -> dict[str, str]:
    """Map each field name to a stable string of its declared annotation."""
    return {name: str(field.annotation) for name, field in model.model_fields.items()}


def live_field_manifest() -> dict[str, dict[str, str]]:
    """Field manifest computed from the live Pydantic models."""
    return {model.__name__: _field_fingerprint(model) for model in _MODELS}


# Regenerate after an intentional ADDITION with:
#   uv run python -c "from setforge.schema_manifest import live_field_manifest; \
#     import pprint; pprint.pp(live_field_manifest())"
# A REMOVAL or RETYPE within major 1 is forbidden — bump the major instead.
FROZEN_FIELD_MANIFEST: dict[str, dict[str, str]] = {
    "Config": {
        "version": "<class 'int'>",
        "schema_version": "<class 'str'>",
        "tracked_files": "dict[str, setforge.config.TrackedFile]",
        "marketplaces": "dict[str, setforge.config.MarketplaceSource]",
        "claude_plugins": "dict[str, setforge.config.ClaudePluginRef]",
        "profiles": "dict[str, setforge.config.Profile]",
    },
    "Profile": {
        "extends": "str | None",
        "tracked_files": "list[str]",
        "extensions": "<class 'setforge.config.Extensions'>",
        "claude_plugins": "list[str]",
        "plugins_reconcile": "<enum 'ReconcilePolicy'>",
        "bootstrap": "list[pathlib._local.Path]",
    },
    "TrackedFile": {
        "src": "<class 'pathlib._local.Path'>",
        "dst": "<class 'str'>",
        "template": "<class 'bool'>",
        "preserve_user_sections": "<class 'bool'>",
        "preserve_user_sections_mode": "<enum 'SectionMode'>",
        "preserve_user_keys_resolved": (
            "list[setforge.preserved_keys.ResolvedPreservedKey]"
        ),
        "preserve_user_keys_deep": "list[str]",
        "mode": "int | None",
        "symlink": "str | None",
        "disposition": "setforge.config.Disposition | None",
        "spans": "list[setforge.spans.SpanEntry]",
    },
    "Extensions": {
        "include": "list[str]",
        "exclude": "list[str]",
        "reconcile": "<enum 'ReconcilePolicy'>",
    },
    "MarketplaceSource": {
        "source": "<enum 'MarketplaceSourceKind'>",
        "repo": "str | None",
        "path": "pathlib._local.Path | None",
    },
    "ClaudePluginRef": {"marketplace": "<class 'str'>"},
    "ResolvedProfile": {
        "extends": "<class 'NoneType'>",
        "tracked_files": "list[str]",
        "extensions": "<class 'setforge.config.Extensions'>",
        "claude_plugins": "list[str]",
        "plugins_reconcile": "<enum 'ReconcilePolicy'>",
        "bootstrap": "list[pathlib._local.Path]",
    },
}


def additivity_violations(
    frozen: dict[str, dict[str, str]], live: dict[str, dict[str, str]]
) -> list[str]:
    """Return human-readable violations of the additive-only invariant.

    A removal, a model deletion, or a type change is a breaking same-major
    change → requires a major bump. An addition not yet recorded in the
    manifest is flagged so the manifest stays in sync. An empty list means
    the live schema matches the frozen manifest exactly.
    """
    violations: list[str] = []
    for model_name, frozen_fields in frozen.items():
        live_fields = live.get(model_name)
        if live_fields is None:
            violations.append(
                f"{model_name}: model removed within major {SCHEMA_MAJOR} "
                f"(requires a major schema_version bump)"
            )
            continue
        for field, ftype in frozen_fields.items():
            if field not in live_fields:
                violations.append(
                    f"{model_name}.{field}: field removed "
                    f"(requires a major schema_version bump)"
                )
            elif live_fields[field] != ftype:
                violations.append(
                    f"{model_name}.{field}: type changed "
                    f"{ftype!r} -> {live_fields[field]!r} "
                    f"(requires a major schema_version bump)"
                )
        for field in live_fields:
            if field not in frozen_fields:
                violations.append(
                    f"{model_name}.{field}: field added — "
                    f"record it in FROZEN_FIELD_MANIFEST"
                )
    return violations
