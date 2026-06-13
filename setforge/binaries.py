"""Binary path resolution with host-local override layers.

Production code never calls :func:`shutil.which` directly. Instead it
calls :func:`resolve_binary`, which walks four layers in order of
precedence:

1. CLI flags (``--code-bin``, ``--claude-bin``, ``--gitleaks-bin``,
   ``--patch-bin``) — stored in module-level state by
   :func:`set_cli_overrides`, which the Typer ``@app.callback()``
   invokes once at startup.
2. Environment variables ``SETFORGE_CODE_BIN`` / ``SETFORGE_CLAUDE_BIN``
   / ``SETFORGE_GITLEAKS_BIN`` / ``SETFORGE_PATCH_BIN``.
3. Host-local config file ``~/.config/setforge/local.yaml`` with shape
   ``binaries: {code: /p, claude: /p, gitleaks: /p, patch: /p}``.
4. ``shutil.which(name)`` (current behavior).

The CLI layer is set once at process start; env and config layers are
read lazily on each lookup so tests can monkey-patch the environment or
``LOCAL_CONFIG_PATH`` between calls without touching module state.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from setforge.config import ClaudeInstallMode
from setforge.errors import BinaryOverrideInvalid, ConfigError

LOCAL_CONFIG_PATH: Final[Path] = Path.home() / ".config" / "setforge" / "local.yaml"
SUPPORTED_BINARIES: Final[tuple[str, ...]] = (
    "claude",
    "code",
    "gitleaks",
    "patch",
    "cargo",
)
_ENV_VAR_PREFIX: Final[str] = "SETFORGE_"
_ENV_VAR_SUFFIX: Final[str] = "_BIN"

_STUB_TEMPLATE: Final[str] = """\
# setforge host-local config — never tracked in git.
#
# Override binary paths here when the defaults on PATH are wrong or absent.
# Uncomment and edit:
#
# binaries:
#   code: /custom/path/to/code
#   claude: /opt/claude/bin/claude
#   gitleaks: /usr/local/bin/gitleaks
#   patch: /usr/local/bin/gpatch
#
# Claude-specific host-local knobs. Uncomment to opt into offline-capable
# install via locally-cloned marketplaces:
#
# claude:
#   install_mode: regular        # or "local-clone"
#   # Future knobs (not yet implemented):
#   # claude_log_level: info
#   # cache_max_age_days: 30
#
# ---------------------------------------------------------------------------
# Per-host overlay classes (commented examples — uncomment + edit to use).
# These mirror the overlay surfaces resolved at install/sync time on top of
# the profile from the config repo's setforge.yaml. Schema for each block
# is enforced by the relevant loader; see the spec for full semantics.
# ---------------------------------------------------------------------------
#
# Per-host plugin overrides (claude_plugins). Uncomment + edit:
# plugins:
#   include:
#     - secure-code-review@work-internal
#   exclude: []
#
# Per-host extension overrides. Uncomment + edit:
# extensions:
#   include:
#     - work-only-extension
#   exclude: []
#
# Marketplaces (claude). Uncomment + edit:
# marketplaces:
#   work-internal: github:my-employer/claude-plugins-internal
#
# Host-local user-sections (overrides marker namespace on a per-tracked-file
# basis):
# host_local_sections:
#   claude_clauded_md:
#     - per-host-section-name
#
# Preserve user keys (deep-merge surface; per tracked file id):
# preserve_user_keys:
#   vscode_serv_settings:
#     - claudeCode.allowDangerouslySkipPermissions
#
# Per-host tracked file overrides (rarely needed):
# tracked_files:
#   claude_clauded_md:
#     dst: /custom/path/to/CLAUDE.md
"""


@dataclass(frozen=True, slots=True)
class ClaudeLocalConfig:
    """Host-local Claude-specific knobs from ``local.yaml``'s ``claude:`` block.

    Today carries a single field, ``install_mode``, that selects between
    the network-fetched marketplace flow (``REGULAR``, default — current
    behavior) and the locally-cloned mirror flow (``LOCAL_CLONE``).
    Additional host-local knobs (log level, cache age) belong here when
    they land in future beads.
    """

    install_mode: ClaudeInstallMode = ClaudeInstallMode.REGULAR


@dataclass(frozen=True, slots=True)
class HostLocalConfig:
    """In-memory shape of ``~/.config/setforge/local.yaml``.

    Consolidates today's ad-hoc dict loaders into a typed value object.
    ``binaries`` mirrors the legacy ``binaries:`` mapping; ``claude``
    carries the nested ``claude:`` block. Both default to "no overrides"
    so a missing ``local.yaml`` (or one without the relevant section)
    yields :class:`HostLocalConfig()` with semantically-empty fields —
    today's behavior is unchanged.
    """

    binaries: Mapping[str, str] = field(default_factory=dict)
    claude: ClaudeLocalConfig = field(default_factory=ClaudeLocalConfig)


_cli_overrides: dict[str, str] = {}


def load_host_local_config() -> HostLocalConfig:
    """Parse ``LOCAL_CONFIG_PATH`` into a :class:`HostLocalConfig`.

    Returns :class:`HostLocalConfig()` defaults if the file is absent,
    empty, or has neither a ``binaries:`` nor a ``claude:`` block.
    Raises :class:`ConfigError` on YAML parse failure, on a non-mapping
    top level, or when an expected block has the wrong shape (e.g.
    ``binaries:`` as a string, or ``claude.install_mode`` not one of
    the :class:`ClaudeInstallMode` members).
    """
    if not LOCAL_CONFIG_PATH.exists():
        return HostLocalConfig()
    yaml = YAML(typ="safe")
    try:
        data = yaml.load(LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
    except YAMLError as exc:
        raise ConfigError(f"malformed YAML in {LOCAL_CONFIG_PATH}: {exc}") from exc
    if data is None:
        return HostLocalConfig()
    if not isinstance(data, dict):
        raise ConfigError(f"top-level of {LOCAL_CONFIG_PATH} must be a mapping")
    return HostLocalConfig(
        binaries=_parse_binaries_block(data.get("binaries")),
        claude=_parse_claude_block(data.get("claude")),
    )


def _parse_binaries_block(raw: object) -> dict[str, str]:
    """Validate and coerce the raw ``binaries:`` block to ``dict[str, str]``.

    ``None`` (absent) → ``{}``. Non-mapping → :class:`ConfigError`. Keys
    and values are coerced to ``str`` for downstream uniformity (matches
    the legacy loader's behavior).
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"'binaries:' in {LOCAL_CONFIG_PATH} must be a mapping")
    return {str(k): str(v) for k, v in raw.items()}


def _parse_claude_block(raw: object) -> ClaudeLocalConfig:
    """Validate and coerce the raw ``claude:`` block to :class:`ClaudeLocalConfig`.

    ``None`` (absent) → defaults (``install_mode=REGULAR``). Non-mapping
    or an ``install_mode`` outside :class:`ClaudeInstallMode` members
    raises :class:`ConfigError` with a message that points at the file.
    """
    if raw is None:
        return ClaudeLocalConfig()
    if not isinstance(raw, dict):
        raise ConfigError(f"'claude:' in {LOCAL_CONFIG_PATH} must be a mapping")
    install_mode_raw = raw.get("install_mode")
    if install_mode_raw is None:
        return ClaudeLocalConfig()
    try:
        install_mode = ClaudeInstallMode(install_mode_raw)
    except ValueError as exc:
        valid = ", ".join(repr(m.value) for m in ClaudeInstallMode)
        raise ConfigError(
            f"'claude.install_mode' in {LOCAL_CONFIG_PATH} must be one of "
            f"{valid}; got {install_mode_raw!r}"
        ) from exc
    return ClaudeLocalConfig(install_mode=install_mode)


def _load_local_config() -> dict[str, str]:
    """Return the ``binaries:`` mapping from :func:`load_host_local_config`.

    Thin shim over :func:`load_host_local_config` preserved for callers
    that only need binary overrides. Returns a plain ``dict[str, str]``
    so callers can ``.get(name)`` on it (the dataclass exposes a
    ``Mapping`` for type-safety; the dict round-trip is harmless).
    """
    return dict(load_host_local_config().binaries)


def _env_overrides() -> dict[str, str]:
    """Return env-var overrides for the supported binaries.

    Reads ``SETFORGE_<NAME>_BIN`` for each name in
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
    gitleaks: str | None = None,
    patch: str | None = None,
) -> None:
    """Record CLI-flag overrides; called once by the Typer app callback.

    Re-invocation replaces the prior set (which matters in tests, not
    production where the callback fires once per process). ``None``
    values are dropped, so unset flags don't shadow lower precedence
    layers.
    """
    _cli_overrides.clear()
    for name, value in (
        ("code", code),
        ("claude", claude),
        ("gitleaks", gitleaks),
        ("patch", patch),
    ):
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


def stderr_of(exc: BaseException) -> str:
    """Best-effort extraction of stderr from a subprocess exception.

    Returns the stripped stderr if the exception carries one (typically
    ``CalledProcessError`` or ``TimeoutExpired`` raised from a
    ``subprocess.run(capture_output=True, ...)`` call), otherwise the
    exception's ``str()`` form.
    """
    return (getattr(exc, "stderr", None) or "").strip() or str(exc)


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

    TOCTOU-safe under concurrent invocation. The previous shape used
    ``if LOCAL_CONFIG_PATH.exists(): return`` followed by
    ``write_text(...)`` — two parallel processes that ran the
    ``exists()`` check between each other's writes would both proceed
    to the write, racing on the file content. Under
    ``pytest -n auto`` this surfaced as a unit-suite-race symptom.
    The atomic ``open("x")`` mode raises
    ``FileExistsError`` for any other process that won the race; we
    swallow it (the file's existence is the invariant, not which
    process wrote it).

    Opt-out via the ``SETFORGE_SKIP_LOCAL_STUB=1`` environment
    variable. Useful in headless / read-only-home contexts where
    creating the stub is undesirable (e.g. CI containers that mount
    ``$HOME`` read-only).
    """
    if os.environ.get("SETFORGE_SKIP_LOCAL_STUB") == "1":
        return
    LOCAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with LOCAL_CONFIG_PATH.open("x", encoding="utf-8") as fh:
            fh.write(_STUB_TEMPLATE)
    except FileExistsError:
        # Another process (or this test run's earlier invocation) created it.
        # The file's existence is the invariant; we're done.
        pass
