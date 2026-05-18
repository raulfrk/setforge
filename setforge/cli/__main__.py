"""Module-execution shim — supports ``python -m setforge.cli``.

The pyproject entry point ``setforge = "setforge.cli:main"`` is the
canonical invocation path. This shim preserves ``python -m setforge.cli``
for users who prefer the explicit module form (``__init__.py`` runs
under ``__name__ == "setforge.cli"`` regardless of invocation, so an
``if __name__ == "__main__":`` block there never fires).
"""

from setforge.cli import main

if __name__ == "__main__":
    main()
