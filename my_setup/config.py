"""Typed configuration schema for my-setup.

Pydantic models validate ``my_setup.yaml`` and provide the in-memory
contract used by every subcommand. YAML is loaded via ruamel.yaml in
round-trip mode so comments and key order survive subsequent capture
writes that re-serialize the document.
"""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, model_validator
from ruamel.yaml import YAML

from my_setup.errors import ConfigError, ProfileNotFound


class ReconcilePolicy(StrEnum):
    ADDITIVE = "additive"
    PRUNE = "prune"
    REPORT = "report"


class MarketplaceSourceKind(StrEnum):
    GITHUB = "github"
    PATH = "path"


class Dotfile(BaseModel):
    src: Path
    dst: str
    template: bool = False
    preserve_user_sections: bool = False
    preserve_user_keys: list[str] = []


class MarketplaceSource(BaseModel):
    source: MarketplaceSourceKind
    repo: str | None = None
    path: Path | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "MarketplaceSource":
        if (self.repo is None) == (self.path is None):
            raise ValueError("MarketplaceSource: exactly one of repo/path required")
        return self


class ClaudePluginRef(BaseModel):
    marketplace: str


class Extensions(BaseModel):
    include: list[str] = []
    exclude: list[str] = []
    reconcile: ReconcilePolicy = ReconcilePolicy.ADDITIVE


class Profile(BaseModel):
    extends: str | None = None
    dotfiles: list[str] = []
    extensions: Extensions = Extensions()
    claude_plugins: list[str] = []
    plugins_reconcile: ReconcilePolicy = ReconcilePolicy.ADDITIVE
    bootstrap: list[Path] = []


class ResolvedProfile(BaseModel):
    """A profile with its ``extends:`` chain fully resolved.

    All list fields are flattened (parent entries first, child entries
    appended, duplicates dropped while preserving first occurrence).
    Scalar fields take the deepest explicit value in the chain.
    """

    extends: None = None
    dotfiles: list[str] = []
    extensions: Extensions = Extensions()
    claude_plugins: list[str] = []
    plugins_reconcile: ReconcilePolicy = ReconcilePolicy.ADDITIVE
    bootstrap: list[Path] = []


class Config(BaseModel):
    version: int = 1
    dotfiles: dict[str, Dotfile]
    marketplaces: dict[str, MarketplaceSource] = {}
    claude_plugins: dict[str, ClaudePluginRef] = {}
    profiles: dict[str, Profile]


def _merge_list[T](parent: list[T], child: list[T]) -> list[T]:
    """Concatenate parent + child, preserving first-occurrence order."""
    seen: set[T] = set()
    merged: list[T] = []
    for item in (*parent, *child):
        if item in seen:
            continue
        seen.add(item)
        merged.append(item)
    return merged


def _merge_extensions(parent: Extensions, child: Extensions) -> Extensions:
    """Merge two Extensions blocks. Lists concatenate; ``reconcile``
    overrides only when explicitly set in child (per ``model_fields_set``)."""
    merged_include = _merge_list(parent.include, child.include)
    merged_exclude = _merge_list(parent.exclude, child.exclude)
    reconcile = (
        child.reconcile
        if "reconcile" in child.model_fields_set
        else parent.reconcile
    )
    return Extensions(
        include=merged_include,
        exclude=merged_exclude,
        reconcile=reconcile,
    )


def _resolve_chain(config: Config, name: str) -> list[Profile]:
    """Walk ``extends:`` from leaf to root, return profiles root-first."""
    chain: list[Profile] = []
    visited: list[str] = []
    current: str | None = name
    while current is not None:
        if current in visited:
            visited.append(current)
            raise ConfigError(f"profile cycle: {' → '.join(visited)}")
        if current not in config.profiles:
            raise ProfileNotFound(f"profile not found: {current}")
        visited.append(current)
        chain.append(config.profiles[current])
        current = config.profiles[current].extends
    chain.reverse()
    return chain


def resolve_profile(config: Config, name: str) -> ResolvedProfile:
    """Walk the ``extends:`` chain and produce a fully-merged profile.

    - List fields (``dotfiles``, ``claude_plugins``, ``bootstrap``,
      ``extensions.include``, ``extensions.exclude``) are concatenated
      parent-first and deduplicated, preserving first occurrence.
    - Scalar fields (``plugins_reconcile``, ``extensions.reconcile``)
      are overridden by the child only when explicitly set in that
      child's ``model_fields_set``; otherwise they inherit.
    - A cycle in ``extends:`` raises :class:`ConfigError` with every
      profile name in the cycle.
    """
    if name not in config.profiles:
        raise ProfileNotFound(f"profile not found: {name}")
    chain = _resolve_chain(config, name)

    resolved = ResolvedProfile()
    for profile in chain:
        fields_set = profile.model_fields_set
        resolved = ResolvedProfile(
            dotfiles=_merge_list(resolved.dotfiles, profile.dotfiles),
            claude_plugins=_merge_list(resolved.claude_plugins, profile.claude_plugins),
            bootstrap=_merge_list(resolved.bootstrap, profile.bootstrap),
            extensions=_merge_extensions(resolved.extensions, profile.extensions),
            plugins_reconcile=(
                profile.plugins_reconcile
                if "plugins_reconcile" in fields_set
                else resolved.plugins_reconcile
            ),
        )
    return resolved


def load_config(path: Path) -> Config:
    """Parse ``my_setup.yaml`` from disk and validate against the schema.

    Raises :class:`ConfigError` on file-not-found or YAML parse errors.
    Pydantic validation errors are propagated unchanged so the caller
    sees the full field-level message.
    """
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    yaml = YAML(typ="rt")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    if data is None:
        raise ConfigError(f"config file is empty: {path}")
    return Config.model_validate(data)
