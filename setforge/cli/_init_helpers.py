"""Pure-logic helpers for ``setforge init`` — environment probe + bootstrap shapes.

The probe wraps :func:`setforge.binaries.resolve_binary` so init's
capability claims match the install/sync runtime exactly (acceptance
criterion 11). No side effects in :func:`probe_environment` —
:func:`_mkdir_with_retry` is the only mutating helper here and is only
called from :mod:`setforge.cli.init` after the user confirms.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from setforge.binaries import LOCAL_CONFIG_PATH, resolve_binary

__all__ = [
    "BinaryProbe",
    "CapabilityProbe",
    "CapabilityState",
    "DirProbe",
    "EnvProbe",
    "backup_suffix_now",
    "config_dir_path",
    "host_local_dir_path",
    "is_initialized",
    "probe_environment",
]

# Multi-line fix hints rendered verbatim in the CLI report; keeping
# them as module-level constants lets tests assert exact substrings
# without re-deriving the prose.
_UV_FIX: str = (
    "install uv: curl -LsSf https://astral.sh/uv/install.sh | sh\n"
    "             (or: pip install --user uv)"
)
_CLAUDE_FIX: str = (
    "install Claude CLI / add to PATH / set binaries.claude in local.yaml\n"
    "             → then rerun `setforge init` (no --force needed)"
)
_CODE_FIX: str = (
    "install VSCode + 'code' CLI / set binaries.code in local.yaml\n"
    "             → then rerun `setforge init`"
)

# Optional binaries surface in capability rows; their absence yields
# disabled-with-reason rather than blocking init.
_BINARY_SPECS: tuple[tuple[str, bool, str], ...] = (
    ("uv", True, _UV_FIX),
    ("claude", False, _CLAUDE_FIX),
    ("code", False, _CODE_FIX),
)

# Sentinel substring stamped on local.yaml's template. Used by
# :func:`is_initialized` so reinit detection survives users editing
# the file (as long as the header or a ``binaries:`` block remains).
_SENTINEL: str = "# setforge host-local config"


class CapabilityState(StrEnum):
    """Closed set of capability states reported by ``setforge init``."""

    ENABLED = "enabled"
    DISABLED = "disabled"


@dataclass(slots=True, frozen=True)
class BinaryProbe:
    """One probed binary — ``uv`` (required) or ``claude`` / ``code`` (optional).

    ``resolved_path`` is ``None`` when no precedence layer in
    :func:`setforge.binaries.resolve_binary` returned a path; the
    ``fix_hint`` is rendered verbatim in the init report for missing
    optional binaries (acceptance criterion 4).
    """

    name: str
    required: bool
    resolved_path: Path | None
    fix_hint: str


@dataclass(slots=True, frozen=True)
class DirProbe:
    """One config directory or file the bootstrap may create."""

    path: Path
    exists: bool
    will_create: bool


@dataclass(slots=True, frozen=True)
class CapabilityProbe:
    """One capability the user gets after init — enabled iff its binary resolves.

    ``newly_enabled`` is ``True`` only on a reinit where the previous
    probe showed the binary missing AND the current probe resolved it
    (mockup J scenario 2 — `★ NEWLY ENABLED`).
    """

    label: str
    state: CapabilityState
    reason: str
    newly_enabled: bool


@dataclass(slots=True, frozen=True)
class EnvProbe:
    """Snapshot of binaries, directories, and capabilities at probe time."""

    binaries: tuple[BinaryProbe, ...]
    dirs: tuple[DirProbe, ...]
    capabilities: tuple[CapabilityProbe, ...]


def config_dir_path() -> Path:
    """Return ``~/.config/setforge/`` per the canonical layout."""
    return Path.home() / ".config" / "setforge"


def host_local_dir_path() -> Path:
    """Return ``~/.local/share/setforge/host-local/`` per the canonical layout."""
    return Path.home() / ".local" / "share" / "setforge" / "host-local"


def _probe_binaries() -> tuple[BinaryProbe, ...]:
    """Resolve every supported binary through the 4-layer precedence chain."""
    out: list[BinaryProbe] = []
    for name, required, fix in _BINARY_SPECS:
        resolved = resolve_binary(name) if name != "uv" else _resolve_uv()
        out.append(
            BinaryProbe(
                name=name,
                required=required,
                resolved_path=resolved,
                fix_hint=fix,
            )
        )
    return tuple(out)


def _resolve_uv() -> Path | None:
    """Resolve ``uv`` via ``shutil.which`` only.

    ``uv`` is not in :data:`setforge.binaries.SUPPORTED_BINARIES`
    (CLI/env/config overrides apply to ``code``/``claude``/``patch``
    only). The init probe reports its presence on PATH; users who
    relocate ``uv`` already need a PATH fix.
    """
    import shutil

    which = shutil.which("uv")
    return Path(which) if which is not None else None


def _probe_dirs() -> tuple[DirProbe, ...]:
    """Probe the three init-created paths; ``will_create`` is True iff absent."""
    cfg_dir = config_dir_path()
    local_yaml = LOCAL_CONFIG_PATH
    host_local = host_local_dir_path()
    return (
        DirProbe(
            path=cfg_dir,
            exists=cfg_dir.exists(),
            will_create=not cfg_dir.exists(),
        ),
        DirProbe(
            path=local_yaml,
            exists=local_yaml.exists(),
            will_create=not local_yaml.exists(),
        ),
        DirProbe(
            path=host_local,
            exists=host_local.exists(),
            will_create=not host_local.exists(),
        ),
    )


def _binary_resolved(binaries: tuple[BinaryProbe, ...], name: str) -> bool:
    """Return True iff the named binary resolved to a path."""
    for probe in binaries:
        if probe.name == name:
            return probe.resolved_path is not None
    return False


def _capability_for(
    *,
    label: str,
    binary_name: str,
    binaries: tuple[BinaryProbe, ...],
    prev_state: EnvProbe | None,
) -> CapabilityProbe:
    """Build one capability row, including the ★ newly-enabled flag."""
    resolved = _binary_resolved(binaries, binary_name)
    state = CapabilityState.ENABLED if resolved else CapabilityState.DISABLED
    reason = "" if resolved else f"({binary_name} binary missing)"
    newly_enabled = False
    if resolved and prev_state is not None:
        prev_resolved = _binary_resolved(prev_state.binaries, binary_name)
        newly_enabled = not prev_resolved
    return CapabilityProbe(
        label=label,
        state=state,
        reason=reason,
        newly_enabled=newly_enabled,
    )


def _probe_capabilities(
    binaries: tuple[BinaryProbe, ...],
    *,
    prev_state: EnvProbe | None,
) -> tuple[CapabilityProbe, ...]:
    """Build the capability table — tracked-file deploy + plugin + extension rows."""
    return (
        CapabilityProbe(
            label="tracked-file deploy + sync",
            state=CapabilityState.ENABLED,
            reason="",
            newly_enabled=False,
        ),
        _capability_for(
            label="claude_plugins reconcile",
            binary_name="claude",
            binaries=binaries,
            prev_state=prev_state,
        ),
        _capability_for(
            label="vscode_extensions reconcile",
            binary_name="code",
            binaries=binaries,
            prev_state=prev_state,
        ),
    )


def probe_environment(*, prev_state: EnvProbe | None = None) -> EnvProbe:
    """Resolve uv/claude/code + check three init dirs + build capability table.

    Idempotent — no side effects. Capability detection wraps
    :func:`setforge.binaries.resolve_binary` verbatim so init's claim
    matches install/sync runtime behavior bit-for-bit (acceptance
    criterion 11). When ``prev_state`` is supplied, capabilities that
    flipped from disabled→enabled carry ``newly_enabled=True`` for the
    reinit ★ marker (mockup J scenario 2).
    """
    binaries = _probe_binaries()
    dirs = _probe_dirs()
    capabilities = _probe_capabilities(binaries, prev_state=prev_state)
    return EnvProbe(binaries=binaries, dirs=dirs, capabilities=capabilities)


def is_initialized(probe: EnvProbe) -> bool:
    """Return True iff a setforge-managed init has fully landed.

    Two signals must both hold (content-aware per research brief §7):

    1. ``local.yaml`` is present AND carries the stub sentinel OR an
       uncommented ``binaries:`` block (an empty leftover does NOT
       count — that is an aborted init, not an initialized state).
    2. ``~/.local/share/setforge/host-local/`` exists — the second
       half of the bootstrap contract. Required because the root
       Typer callback writes the local.yaml stub on every invocation
       via :func:`setforge.binaries.ensure_local_config_stub`; the
       host-local dir is the distinguishing signal between
       "just-arrived stub" and "init already ran".

    Reads through ``LOCAL_CONFIG_PATH`` directly (probe carries
    existence only, not content) so the same helper works against a
    monkeypatched path.
    """
    if not LOCAL_CONFIG_PATH.exists():
        return False
    if not host_local_dir_path().exists():
        return False
    try:
        text = LOCAL_CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return False
    if _SENTINEL in text:
        return True
    # A user-edited file with an uncommented ``binaries:`` mapping
    # still counts — the sentinel is just one detection avenue.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("binaries:") and not stripped.startswith("#"):
            return True
    return False


def _mkdir_with_retry(path: Path) -> None:
    """``mkdir(parents=True, exist_ok=True)`` with one retry on TOCTOU race.

    Wraps the canonical idempotent-mkdir per research brief §7 and
    CPython issue #142916 — the syscall can spuriously raise
    :class:`FileExistsError` when another process deletes the path
    between syscall and post-check. A single 10 ms retry covers the
    realistic window. The ``(attempt, is_last)`` paired iter
    flattens the retry/raise dispatch (nesting depth 2 instead of 4).
    """
    for _attempt, is_last in ((1, False), (2, True)):
        try:
            path.mkdir(parents=True, exist_ok=True)
            return
        except FileExistsError:
            if is_last:
                raise
            time.sleep(0.01)


def backup_suffix_now() -> str:
    """Return ``<UTC-ISO8601>`` for ``<file>.bak.<suffix>`` backup naming.

    Format: ``YYYYMMDDTHHMMSSZ`` (no microseconds, no separators
    inside the timestamp). Matches the convention rustup / brew /
    pyenv use for ``--force``-style backups (research brief §7).
    """
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
