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

    source: Source | None = None
    binaries: dict[str, str] = Field(default_factory=dict)
    # ``claude:`` is currently a free-form mapping (install_mode field
    # only, hand-validated by setforge.binaries._parse_claude_block).
    # Validate as a mapping; the hand-validator's deeper schema is
    # exercised by its own load path.
    claude: dict[str, object] = Field(default_factory=dict)
