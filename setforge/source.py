"""Config-source discovery layer for setforge.

The engine reads its declarative config (``setforge.yaml`` + ``tracked/``)
from a *source* — a directory containing both. Sources are typed as a
discriminated union of ``PathSource`` (a plain directory path on disk)
and ``GitSource`` (a clone destination derived from a git URL; the actual
clone/fetch logic lives in :mod:`setforge.git_ops`, landing in a
follow-up bead).

Discovery walks four precedence layers, first non-empty wins entirely
(mirrors :func:`setforge.binaries.resolve_binary`):

1. CLI flag — ``--source PATH`` (paths only; git URLs require fields
   that don't fit a single CLI flag, so they live in ``local.yaml``).
2. Env var — ``SETFORGE_SOURCE=PATH`` (paths only).
3. Host-local config — ``~/.config/setforge/local.yaml`` top-level
   ``source:`` block (PathSource OR GitSource).
4. Fallback — CWD if it contains ``setforge.yaml``.

Multi-source / stacked sources are explicitly OUT OF SCOPE per the
parent spec (setforge-2ba). The Pydantic schema's ``source:`` key is
singular; a list-shaped value raises a :class:`pydantic.ValidationError`
at load time.
"""

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ruamel.yaml ships py.typed without resolvable annotations; no stub pkg on PyPI.
from ruamel.yaml import YAML  # type: ignore[import-not-found]
from ruamel.yaml.error import YAMLError  # type: ignore[import-not-found]

from setforge import git_ops
from setforge.errors import (
    ConfigError,
    DirtySourceCheckout,
    NoSourceConfigured,
    SourceNotCloned,
)

_STRICT = ConfigDict(extra="forbid")

CLI_FLAG: Final[str] = "--source"
ENV_VAR: Final[str] = "SETFORGE_SOURCE"
LOCAL_CONFIG_PATH: Final[Path] = Path.home() / ".config" / "setforge" / "local.yaml"
DEFAULT_CLONE_ROOT: Final[Path] = (
    Path.home() / ".local" / "share" / "setforge" / "sources"
)
CONFIG_FILENAME: Final[str] = "setforge.yaml"


class SourceKind(StrEnum):
    """Discriminator for the :data:`Source` tagged union.

    Mirrors :class:`setforge.config.MarketplaceSourceKind` (the
    project's established pattern for Pydantic discriminator values).
    """

    PATH = "path"
    GIT = "git"


class PathSource(BaseModel):
    """Source backed by a directory already on disk.

    The directory must contain ``setforge.yaml`` at its root (validated
    lazily by :func:`validate_source_dir`, not at model construction).
    """

    model_config = _STRICT

    kind: Literal[SourceKind.PATH] = SourceKind.PATH
    path: Path
    name: str | None = None

    @property
    def display_name(self) -> str:
        """Return ``name`` if set, otherwise the directory basename."""
        return self.name or self.path.expanduser().name


class GitSource(BaseModel):
    """Source backed by a git repository to be cloned to ``clone_dest``.

    Cloning + checkout is handled by :mod:`setforge.git_ops` (a follow-up
    bead). This module only resolves the *expected on-disk location*:
    ``clone_dest`` if set, otherwise ``DEFAULT_CLONE_ROOT / <name>``.
    """

    model_config = _STRICT

    kind: Literal[SourceKind.GIT] = SourceKind.GIT
    url: str
    ref: str = "main"
    name: str | None = None
    clone_dest: Path | None = None

    @property
    def display_name(self) -> str:
        """Return ``name`` if set, otherwise the URL basename minus ``.git``."""
        if self.name:
            return self.name
        tail = self.url.rstrip("/").rsplit("/", 1)[-1]
        return tail.removesuffix(".git")

    @property
    def resolved_clone_dest(self) -> Path:
        """Return the on-disk location where this source's clone lives."""
        if self.clone_dest is not None:
            return self.clone_dest.expanduser()
        return DEFAULT_CLONE_ROOT / self.display_name


Source = Annotated[PathSource | GitSource, Field(discriminator="kind")]


class _LocalSourceConfig(BaseModel):
    """Just the ``source:`` block of ``~/.config/setforge/local.yaml``.

    Loaded separately from :class:`setforge.binaries.HostLocalConfig` so
    the source-discovery layer and the binary-override layer can each
    parse the file independently without coupling.
    """

    model_config = _STRICT

    source: Source | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_list_shaped_source(cls, data: object) -> object:
        """Reject ``source:`` as a list (multi-source out of scope).

        Pydantic's discriminated-union validation would error on a list
        value, but the message is opaque ("Input should be a valid
        dictionary"). Surface a clear message here so the user knows
        WHY a list shape is rejected.
        """
        if isinstance(data, Mapping) and isinstance(data.get("source"), list):
            raise ValueError(
                "`source:` must be a single mapping (path-kind or git-kind), "
                "not a list. Multi-source / stacked sources is out of scope "
                "for setforge; see parent bead setforge-2ba."
            )
        return data


def _load_local_source_config(path: Path) -> _LocalSourceConfig:
    """Parse the ``source:`` block from ``local.yaml``.

    Returns an empty :class:`_LocalSourceConfig` when the file is absent
    or carries no ``source:`` key. Raises :class:`ConfigError` on YAML
    parse failure or non-mapping top level. Pydantic validation errors
    propagate unchanged (with the field-level message).
    """
    if not path.exists():
        return _LocalSourceConfig()
    yaml = YAML(typ="safe")
    try:
        data = yaml.load(path.read_text(encoding="utf-8"))
    except YAMLError as exc:
        raise ConfigError(f"malformed YAML in {path}: {exc}") from exc
    if data is None:
        return _LocalSourceConfig()
    if not isinstance(data, Mapping):
        raise ConfigError(f"top-level of {path} must be a mapping")
    # Extract only the source: key; ignore other blocks (binaries:, claude:)
    # which belong to other loaders.
    if "source" not in data:
        return _LocalSourceConfig()
    return _LocalSourceConfig.model_validate({"source": data["source"]})


_cli_source: Path | None = None


def set_cli_source(value: Path | None) -> None:
    """Capture the ``--source`` flag value from the Typer callback.

    Stored at module scope so commands can call :func:`get_resolved_source`
    without re-threading the flag through every signature. Mirrors the
    pattern in :func:`setforge.binaries.set_cli_overrides`.
    """
    global _cli_source
    _cli_source = value


def get_resolved_source() -> Source:
    """Resolve the current source using module-state CLI flag + live env.

    Convenience wrapper around :func:`resolve_source` for use inside
    Typer command bodies that don't carry the flag through their own
    signature. Reads ``os.environ`` and ``Path.cwd()`` live.
    """
    return resolve_source(
        cli_path=_cli_source,
        env=os.environ,
        local_config_path=LOCAL_CONFIG_PATH,
        cwd=Path.cwd(),
    )


def resolve_source(
    *,
    cli_path: Path | None,
    env: Mapping[str, str],
    local_config_path: Path = LOCAL_CONFIG_PATH,
    cwd: Path | None = None,
) -> Source:
    """Walk the 4-layer precedence chain and return the resolved source.

    Layers (first non-empty wins entirely):

    1. ``cli_path`` (from ``--source PATH`` on the command line).
    2. ``env[ENV_VAR]`` (``SETFORGE_SOURCE=PATH``).
    3. ``local_config_path`` ``source:`` block (path OR git source).
    4. ``cwd / "setforge.yaml"`` exists (back-compat for run-from-repo).

    Raises :class:`NoSourceConfigured` when no layer produces a source,
    listing all four layers in the message so the user knows where to
    configure.
    """
    if cli_path is not None:
        return PathSource(path=cli_path)
    env_value = env.get(ENV_VAR)
    if env_value:
        return PathSource(path=Path(env_value))
    local = _load_local_source_config(local_config_path)
    if local.source is not None:
        return local.source
    cwd_resolved = cwd or Path.cwd()
    cwd_yaml = cwd_resolved / CONFIG_FILENAME
    if cwd_yaml.exists():
        return PathSource(path=cwd_resolved)
    raise NoSourceConfigured(
        "no config source configured. Layers checked in order:\n"
        f"  1. CLI flag {CLI_FLAG} PATH (not provided)\n"
        f"  2. env {ENV_VAR}=PATH (unset or empty)\n"
        f"  3. {local_config_path} `source:` block (absent or missing key)\n"
        f"  4. CWD fallback {cwd_yaml} (file not found)"
    )


def resolve_source_dir(source: Source) -> Path:
    """Return the on-disk directory where ``source``'s contents live.

    For :class:`PathSource`: returns ``path`` expanded.
    For :class:`GitSource`: returns ``clone_dest`` (or its default);
    raises :class:`SourceNotCloned` if the directory does not exist on
    disk (the user must run ``setforge fetch`` first).
    """
    if isinstance(source, PathSource):
        return source.path.expanduser()
    resolved = source.resolved_clone_dest
    if not resolved.exists():
        raise SourceNotCloned(
            f"git source {source.display_name!r} not cloned at {resolved}. "
            f"Run `setforge fetch` to clone."
        )
    return resolved


def validate_source_dir(source: Source) -> Path:
    """Verify the source's directory contains ``setforge.yaml``; return its path.

    Raises :class:`SourceNotCloned` if a :class:`GitSource`'s clone is
    absent; raises :class:`ConfigError` if the directory exists but does
    not contain ``setforge.yaml`` at its root. When a legacy
    ``my_setup.yaml`` is present, the error message surfaces a ``git mv``
    migration recipe.
    """
    source_dir = resolve_source_dir(source)
    config_path = source_dir / CONFIG_FILENAME
    if config_path.exists():
        return config_path
    # Friendly migration error for the my_setup.yaml -> setforge.yaml
    # rename (setforge-2ba.1). Mirrors the legacy-namespace detector
    # pattern in setforge.sections.detect_legacy_namespace_markers.
    legacy_path = source_dir / "my_setup.yaml"
    if legacy_path.exists():
        raise ConfigError(
            f"source {source.display_name!r} at {source_dir} contains a "
            f"legacy 'my_setup.yaml'. setforge expects '{CONFIG_FILENAME}'. "
            f"Rename: (cd {source_dir} && git mv my_setup.yaml "
            f"{CONFIG_FILENAME})"
        )
    raise ConfigError(
        f"source {source.display_name!r} at {source_dir} does not contain "
        f"{CONFIG_FILENAME}"
    )


def check_source_clean(source: Source) -> None:
    """Pre-write gate: raise :class:`DirtySourceCheckout` on dirty source.

    Scopes the porcelain check to ``tracked/`` (the engine's only write
    surface). Non-git PathSource dirs skip the check (the user isn't
    using git here; nothing to protect against). GitSource always runs
    the check; if its clone is missing, :class:`SourceNotCloned` from
    :func:`resolve_source_dir` propagates.
    """
    source_dir = resolve_source_dir(source)
    if not git_ops.is_git_repo(source_dir):
        return
    porcelain = git_ops.status_porcelain(source_dir, path="tracked")
    if not porcelain:
        return
    file_count = len([line for line in porcelain.splitlines() if line.strip()])
    raise DirtySourceCheckout(
        f"{source_dir}/tracked/ has uncommitted changes "
        f"({file_count} file{'s' if file_count != 1 else ''}). "
        "Commit or stash before retrying."
    )


def fetch_source(source: Source) -> str:
    """Clone-on-missing + fetch + ref-checkout the given git source.

    Returns a one-line human-readable status message describing the
    operation performed. :class:`PathSource` returns
    ``"source is a path; nothing to fetch"`` immediately (no git ops).
    GitSource: (1) compute clone_dest, clone if missing; (2) fetch
    origin; (3) verify ``tracked/`` is clean; (4) check out the
    pinned ref. Errors propagate (:class:`GitOpError`,
    :class:`DirtySourceCheckout`).
    """
    if isinstance(source, PathSource):
        return "source is a path; nothing to fetch"
    clone_dest = source.resolved_clone_dest
    cloned = False
    if not clone_dest.exists():
        git_ops.git_clone(source.url, clone_dest)
        cloned = True
    git_ops.git_fetch(clone_dest)
    porcelain = git_ops.status_porcelain(clone_dest, path="tracked")
    if porcelain:
        file_count = len([line for line in porcelain.splitlines() if line.strip()])
        raise DirtySourceCheckout(
            f"{clone_dest}/tracked/ has uncommitted changes "
            f"({file_count} file{'s' if file_count != 1 else ''}). "
            "Commit or stash before fetching."
        )
    git_ops.git_checkout(clone_dest, source.ref)
    action = "cloned and checked out" if cloned else "fetched and checked out"
    return f"{action} {source.ref} at {clone_dest}"


def format_post_write_hint(source: Source, file_count: int) -> str:
    """Build the post-sync/capture hint message pointing at the source dir.

    Three shapes (decided by source kind + git upstream presence):

    * PathSource without ``.git/``: bare file-count message, no git hint.
    * Git repo without upstream: ``cd ... && git diff && git commit``.
    * Git repo with upstream: ``... && git push`` appended.
    """
    try:
        source_dir = resolve_source_dir(source)
    except SourceNotCloned:
        return f"→ wrote {file_count} files to <source> (not on disk?)"
    plural = "s" if file_count != 1 else ""
    base = f"→ wrote {file_count} file{plural} to {source_dir}/tracked/"
    if not git_ops.is_git_repo(source_dir):
        return base
    upstream = git_ops.rev_parse_upstream(source_dir)
    if upstream is not None:
        return f"{base}; cd {source_dir} && git diff && git commit && git push"
    return f"{base}; cd {source_dir} && git diff && git commit"


__all__ = [
    "CLI_FLAG",
    "CONFIG_FILENAME",
    "DEFAULT_CLONE_ROOT",
    "ENV_VAR",
    "LOCAL_CONFIG_PATH",
    "GitSource",
    "PathSource",
    "Source",
    "SourceKind",
    "check_source_clean",
    "fetch_source",
    "format_post_write_hint",
    "get_resolved_source",
    "resolve_source",
    "resolve_source_dir",
    "set_cli_source",
    "validate_source_dir",
]
