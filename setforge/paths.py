"""Where setforge keeps its own files, and the Jinja2 dst-path context.

Every root is a function evaluated at call time from ``$HOME`` (and
``SETFORGE_STATE_DIR`` for the state root), never a module constant: a
process whose home is redirected — every test — resolves under the
redirected home without any per-module patching.

The ``vscode_user_dir`` template variable matches the dotdrop dynvariable
of the same name (``$HOME/.config/Code/User`` on Linux, ``$HOME/Library/
Application Support/Code/User`` on macOS) so existing dst paths in the
migrated YAML config continue to resolve identically.
"""

import os
import sys
from pathlib import Path

import platformdirs

STATE_DIR_ENV = "SETFORGE_STATE_DIR"


def config_root() -> Path:
    """Return ``~/.config/setforge``."""
    return Path.home() / ".config" / "setforge"


def local_config_path() -> Path:
    """Return the host-local overlay file, ``~/.config/setforge/local.yaml``."""
    return config_root() / "local.yaml"


def state_root() -> Path:
    """Resolve the setforge state dir.

    Honors the ``SETFORGE_STATE_DIR`` env var (used by tests and by
    operators relocating state). Falls back to ``~/.local/state/setforge``.
    """
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "setforge"


def cache_root() -> Path:
    """Return ``~/.cache/setforge``; not auto-created (writers ensure it)."""
    return Path.home() / ".cache" / "setforge"


def journals_root() -> Path:
    """Return the user-global active-operation namespace.

    Recovery reservations protect user-global adapters and config repositories,
    so an operator-selected transition state root must not hide them.
    """
    return cache_root() / "operations"


def data_root() -> Path:
    """Return ``~/.local/share/setforge``."""
    return Path.home() / ".local" / "share" / "setforge"


def snapshots_root() -> Path:
    """Return the XDG-data root where snapshots live."""
    return data_root() / "snapshots"


def vscode_user_dir() -> Path:
    """Return the VSCode application config directory (parent of ``User/``).

    Note: this is the OS-level Code config root, NOT the ``User/`` directory
    where ``settings.json`` lives. Callers (or :func:`template_context`)
    are responsible for appending ``/User``.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Code"
    return Path(platformdirs.user_config_path("Code"))


def template_context() -> dict[str, str]:
    """Return the variable bindings exposed to Jinja2 dst-path templates."""
    return {
        "vscode_user_dir": str(vscode_user_dir() / "User"),
        "home": str(Path.home()),
    }
