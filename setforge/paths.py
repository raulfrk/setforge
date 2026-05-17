"""OS-conditional path resolution and Jinja2 template context.

The ``vscode_user_dir`` template variable matches the dotdrop dynvariable
of the same name (``$HOME/.config/Code/User`` on Linux, ``$HOME/Library/
Application Support/Code/User`` on macOS) so existing dst paths in the
migrated YAML config continue to resolve identically.
"""

import sys
from pathlib import Path

import platformdirs


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
