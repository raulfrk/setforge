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

from pathlib import Path
from typing import Final

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
