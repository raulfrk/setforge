"""Binary path resolution with host-local override layers.

Production code never calls :func:`shutil.which` directly. Instead it
calls :func:`resolve_binary`, which walks four layers in order of
precedence:

1. CLI flags (``--code-bin``, ``--claude-bin``, ``--patch-bin``) —
   stored in module-level state by :func:`set_cli_overrides`, which the
   Typer ``@app.callback()`` invokes once at startup.
2. Environment variables ``MY_SETUP_CODE_BIN`` / ``CLAUDE_BIN`` /
   ``PATCH_BIN``.
3. Host-local config file ``~/.config/my-setup/local.yaml`` with shape
   ``binaries: {code: /p, claude: /p, patch: /p}``.
4. ``shutil.which(name)`` (current behavior).

The CLI layer is set once at process start; env and config layers are
read lazily on each lookup so tests can monkey-patch the environment or
``LOCAL_CONFIG_PATH`` between calls without touching module state.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from my_setup.errors import BinaryOverrideInvalid, ConfigError

LOCAL_CONFIG_PATH: Final[Path] = (
    Path.home() / ".config" / "my-setup" / "local.yaml"
)
SUPPORTED_BINARIES: Final[tuple[str, ...]] = ("code", "claude", "patch")
_ENV_VAR_PREFIX: Final[str] = "MY_SETUP_"
_ENV_VAR_SUFFIX: Final[str] = "_BIN"

_STUB_TEMPLATE: Final[str] = """\
# my-setup host-local config — never tracked in git.
#
# Override binary paths here when the defaults on PATH are wrong or absent.
# Uncomment and edit:
#
# binaries:
#   code: /custom/path/to/code
#   claude: /opt/claude/bin/claude
#   patch: /usr/local/bin/gpatch
"""

_cli_overrides: dict[str, str] = {}


def _load_local_config() -> dict[str, str]:
    """Return the ``binaries:`` dict from ``LOCAL_CONFIG_PATH``.

    Returns ``{}`` if the file is absent, empty, or has no ``binaries:``
    key. Raises :class:`ConfigError` on YAML parse failure or when
    ``binaries:`` is present but not a mapping. Values are coerced to
    ``str`` for downstream uniformity.
    """
    if not LOCAL_CONFIG_PATH.exists():
        return {}
    yaml = YAML(typ="safe")
    try:
        data = yaml.load(LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
    except YAMLError as exc:
        raise ConfigError(
            f"malformed YAML in {LOCAL_CONFIG_PATH}: {exc}"
        ) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"top-level of {LOCAL_CONFIG_PATH} must be a mapping"
        )
    binaries = data.get("binaries")
    if binaries is None:
        return {}
    if not isinstance(binaries, dict):
        raise ConfigError(
            f"'binaries:' in {LOCAL_CONFIG_PATH} must be a mapping"
        )
    return {str(k): str(v) for k, v in binaries.items()}


def _env_overrides() -> dict[str, str]:
    """Return env-var overrides for the supported binaries.

    Reads ``MY_SETUP_<NAME>_BIN`` for each name in
    :data:`SUPPORTED_BINARIES`. Empty-string values are treated as
    unset (avoids surprising the user when a wrapper sets the var
    blank).
    """
    out: dict[str, str] = {}
    for name in SUPPORTED_BINARIES:
        var = f"{_ENV_VAR_PREFIX}{name.upper()}{_ENV_VAR_SUFFIX}"
        value = os.environ.get(var)
        if value:
            out[name] = value
    return out


def set_cli_overrides(
    *,
    code: str | None = None,
    claude: str | None = None,
    patch: str | None = None,
) -> None:
    """Record CLI-flag overrides; called once by the Typer app callback.

    Re-invocation replaces the prior set (which matters in tests, not
    production where the callback fires once per process). ``None``
    values are dropped, so unset flags don't shadow lower precedence
    layers.
    """
    _cli_overrides.clear()
    for name, value in (("code", code), ("claude", claude), ("patch", patch)):
        if value is not None:
            _cli_overrides[name] = value


def _validate(name: str, raw_path: str, layer: str) -> Path:
    """Confirm an override path exists and is executable.

    Raises :class:`BinaryOverrideInvalid` with structured fields when
    the path is missing or not executable. Returns the resolved
    :class:`Path` otherwise.
    """
    p = Path(raw_path)
    if not p.exists():
        raise BinaryOverrideInvalid(
            layer=layer, binary=name, path=raw_path, reason="not found"
        )
    if not os.access(p, os.X_OK):
        raise BinaryOverrideInvalid(
            layer=layer,
            binary=name,
            path=raw_path,
            reason="not executable",
        )
    return p


def resolve_binary(name: str) -> Path | None:
    """Resolve ``name`` through the precedence chain.

    Order: CLI override → env var → config file → ``shutil.which``.
    Returns an absolute :class:`Path` for a hit at any layer, or
    ``None`` when no layer resolves the name.

    Raises :class:`BinaryOverrideInvalid` if a layer above ``which``
    produced a path that fails :func:`_validate`. (We do not silently
    fall through a broken override; an invalid override is a user
    error worth surfacing.)
    """
    if (raw := _cli_overrides.get(name)) is not None:
        return _validate(name, raw, layer="cli")
    if (raw := _env_overrides().get(name)) is not None:
        return _validate(name, raw, layer="env")
    if (raw := _load_local_config().get(name)) is not None:
        return _validate(name, raw, layer="config")
    which = shutil.which(name)
    return Path(which) if which else None


def ensure_local_config_stub() -> None:
    """Create ``LOCAL_CONFIG_PATH`` with a commented stub if absent.

    Idempotent: a pre-existing file (regardless of content) is never
    touched. Creates parent directories as needed. Called from the
    Typer ``@app.callback()`` so a fresh install gets the discoverable
    file on first invocation of any subcommand.
    """
    if LOCAL_CONFIG_PATH.exists():
        return
    LOCAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_CONFIG_PATH.write_text(_STUB_TEMPLATE, encoding="utf-8")
