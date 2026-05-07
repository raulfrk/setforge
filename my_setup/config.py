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

from my_setup.errors import ConfigError


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


class Config(BaseModel):
    version: int = 1
    dotfiles: dict[str, Dotfile]
    marketplaces: dict[str, MarketplaceSource] = {}
    claude_plugins: dict[str, ClaudePluginRef] = {}
    profiles: dict[str, Profile]


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
