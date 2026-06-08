"""Validate-shape model for ``~/.config/setforge/local.yaml``.

Houses :class:`_LocalConfig`, the loose Pydantic shape used by both
:mod:`setforge.cli.validate` (for the schema-error reporting path) and
:mod:`setforge.cli.config` (for the in-memory candidate-doc validation
gate in ``setforge config add`` / ``remove``).

This module sits OUTSIDE the ``setforge.cli`` package because the model
itself is reusable schema state — not CLI orchestration. The previous
home in :mod:`setforge.cli.validate` forced :mod:`setforge.cli.config`
to import cross-CLI from a sibling subcommand module just to reach the
schema, which obscured the dependency direction (model = data, not
CLI). Live re-export from :mod:`setforge.cli.validate` preserves
backward compatibility for any external callers that learned the old
import path.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from setforge.source import Source

__all__ = ["LocalConfig"]


class LocalConfig(BaseModel):
    """Top-level shape of ``~/.config/setforge/local.yaml`` for validate.

    Mirrors the runtime loaders in :mod:`setforge.source` and
    :mod:`setforge.binaries` but unifies them into a single Pydantic
    model with ``extra="forbid"`` so unknown top-level keys surface as
    schema errors (per the mockup D anti-smell discipline). The runtime
    loaders intentionally keep their per-block split; this model is
    validate-only and not consumed by other subcommands.
    """

    model_config = ConfigDict(extra="forbid")

    # local.yaml carries its OWN schema_version, independent of
    # setforge.yaml's (it has a separate baseline and migration chain —
    # see :mod:`setforge.migrations._local_yaml`). Defaults to the local
    # baseline so a pre-versioning local.yaml validates as 1.0.
    schema_version: str = "1.0"
    source: Source | None = None
    binaries: dict[str, str] = Field(default_factory=dict)
    # ``claude:`` is currently a free-form mapping (install_mode field
    # only, hand-validated by setforge.binaries._parse_claude_block).
    # Validate as a mapping; the hand-validator's deeper schema is
    # exercised by its own load path.
    claude: dict[str, object] = Field(default_factory=dict)
    # ``tracked_files:`` carries per-tracked_file host-local overlay
    # (preserve_user_keys / host_local_sections). The nested-shape
    # validation runs through :func:`_check_host_local_sections` and
    # :func:`apply_preserve_user_keys_overlay`; this layer only asserts
    # the key is allowed at the top level.
    tracked_files: dict[str, object] = Field(default_factory=dict)
    # SPEC 2 per-host plugin / extension / marketplace
    # overlay blocks. Free-form mapping shapes at this layer; the
    # strict per-block schemas (PluginOverlay / ExtensionOverlay /
    # MarketplaceOverlay) live in :mod:`setforge.source` and are
    # exercised by :func:`apply_local_overlay` at install + validate.
    plugins: dict[str, object] = Field(default_factory=dict)
    extensions: dict[str, object] = Field(default_factory=dict)
    marketplaces: dict[str, object] = Field(default_factory=dict)
    # ``orphan_ignore:`` is a list of tracked_file ids the user has
    # flagged "keep orphan" via ``cleanup-orphans --ignore``. Free-form
    # list-of-strings at this layer; the runtime loader in
    # :mod:`setforge.compare.load_ignored_orphans` does the deeper
    # validation.
    orphan_ignore: list[str] = Field(default_factory=list)
