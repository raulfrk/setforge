"""setforge — tracked-file + VSCode-extension + Claude-plugin orchestration CLI.

``__version__`` is sourced from the installed distribution metadata so a
single bump in ``pyproject.toml`` is the source of truth. Falls back to
``"0.0.0+local"`` when running uninstalled from a source tree without
metadata (rare; only happens in a fresh clone before ``uv sync``).
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("setforge")
except PackageNotFoundError:
    __version__ = "0.0.0+local"
