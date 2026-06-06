"""Developer / CI utility scripts (not part of the shipped wheel).

Marks ``scripts/`` as a regular package so its modules resolve under a
single, unambiguous import name (``scripts.<module>``) for both pytest
imports and mypy — avoiding mypy's "source file found twice under
different module names" ambiguity when a script is both imported as a
package member and type-checked by explicit path. The wheel ships only
``setforge`` (see ``[tool.hatch.build.targets.wheel]``), so this package
never reaches the distribution.
"""
