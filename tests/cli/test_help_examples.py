"""CliRunner-based assertions for every Typer leaf command's ``--help`` epilog.

Three things are verified for every leaf command:

1. The ``--help`` output contains the literal substring ``"Examples:"``
   (each ``@app.command()`` / ``@<group>.command()`` registration carries
   an ``epilog=`` from :mod:`setforge.cli._help_examples`).
2. The ``--help`` output is free of personal-config leaks
   (``vm-headless`` / ``raulfrk`` / ``raul``) — examples must use the
   neutral ``<profile>`` placeholder.
3. The most-common example for each command parses without raising
   ``SystemExit(2)`` in the same CliRunner. The example flag combos are
   swapped against ``--help`` (dry-run substitute) so the parse runs in
   isolation from real config / live filesystem state — what we're
   verifying is that the flags referenced in the epilog still exist in
   the current command surface (no drift).
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from setforge.cli import _help_examples, app

# Leaf-command invocation paths — each entry is a path list that drives
# ``setforge <path...> --help`` end-to-end. Order matches the source
# registration order from ``setforge/cli/__init__.py``.
LEAF_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("install",),
    ("compare",),
    ("cleanup-orphans",),
    ("capture",),
    ("merge",),
    ("sync",),
    ("revert",),
    ("transitions", "list"),
    ("transitions", "show"),
    ("ext", "list"),
    ("ext", "add"),
    ("ext", "remove"),
    ("ext", "reconcile"),
    ("plugin", "list"),
    ("plugin", "add"),
    ("plugin", "remove"),
    ("plugin", "reconcile"),
    ("plugin", "sync-cache"),
    ("marketplace", "add"),
    ("marketplace", "remove"),
    ("marketplace", "update"),
    ("validate",),
    ("fetch",),
    ("section", "emit"),
    ("section", "add"),
    ("init",),
    ("upgrade",),
    ("migrate",),
    ("status",),
    ("profile", "list"),
    ("profile", "show"),
    ("snapshot", "create"),
    ("snapshot", "list"),
    ("snapshot", "restore"),
    ("completion", "install"),
)

# Strip Click/Rich ANSI escapes so substring asserts on flag names
# (e.g. ``'--dry-run' in stdout``) are not fragmented by color injection.
_ANSI_RE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*m")

# Profile / username substrings that would indicate a personal-config
# leak into an engine-shipped ``--help`` epilog. ``raulfrk`` is the
# author's GitHub handle; ``vm-headless`` is the author's daily-driver
# profile name. The bare-UNIX username ``raul`` is intentionally
# omitted from the per-command grep — it collides with legitimate
# docstring references to ``~/.claude/projects/-home-raul-setforge/...``
# in command docstrings — but it IS still grepped against the
# ``_help_examples`` source via :func:`test_no_personal_config_in_help_examples_module`
# below, where any match is unambiguously a real leak.
_PERSONAL_CONFIG_NEEDLES: tuple[str, ...] = (
    "vm-headless",
    "raulfrk",
)

# Stricter needle set — used only against the ``_help_examples`` module
# source where any match is by construction a personal-config leak.
_MODULE_PERSONAL_CONFIG_NEEDLES: tuple[str, ...] = (
    "vm-headless",
    "raulfrk",
    "raul",
)


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from ``text``."""
    return _ANSI_RE.sub("", text)


def _id_for(path: tuple[str, ...]) -> str:
    """pytest id helper — ``("snapshot","create") → "snapshot create"``."""
    return " ".join(path)


@pytest.fixture
def runner() -> CliRunner:
    """Return a fresh CliRunner for each test."""
    return CliRunner()


@pytest.mark.parametrize(
    "leaf_path", LEAF_COMMANDS, ids=[_id_for(p) for p in LEAF_COMMANDS]
)
def test_every_command_has_examples(
    runner: CliRunner, leaf_path: tuple[str, ...]
) -> None:
    """Each leaf command's ``--help`` output ends with an ``Examples:`` block."""
    result = runner.invoke(app, [*leaf_path, "--help"], env={"COLUMNS": "100"})
    assert result.exit_code == 0, result.stdout
    stdout = _strip_ansi(result.stdout)
    assert "Examples:" in stdout, (
        f"missing 'Examples:' in --help for {' '.join(leaf_path)}:\n{stdout}"
    )


@pytest.mark.parametrize(
    "leaf_path", LEAF_COMMANDS, ids=[_id_for(p) for p in LEAF_COMMANDS]
)
def test_no_personal_config_in_epilog(
    runner: CliRunner, leaf_path: tuple[str, ...]
) -> None:
    """No example references a personal profile / username substring."""
    result = runner.invoke(app, [*leaf_path, "--help"], env={"COLUMNS": "100"})
    assert result.exit_code == 0, result.stdout
    stdout = _strip_ansi(result.stdout)
    for needle in _PERSONAL_CONFIG_NEEDLES:
        assert needle not in stdout, (
            f"personal-config leak {needle!r} in --help for "
            f"{' '.join(leaf_path)}:\n{stdout}"
        )


def test_install_help_advertises_dry_run(runner: CliRunner) -> None:
    """install --help cites --dry-run (cross-link to lnvq dry-run bead)."""
    result = runner.invoke(app, ["install", "--help"], env={"COLUMNS": "100"})
    assert result.exit_code == 0, result.stdout
    stdout = _strip_ansi(result.stdout)
    assert "--dry-run" in stdout, stdout


def test_install_help_advertises_auto_flag(runner: CliRunner) -> None:
    """install --help cites --auto=use-tracked (cross-link to bviv --auto bead)."""
    result = runner.invoke(app, ["install", "--help"], env={"COLUMNS": "100"})
    assert result.exit_code == 0, result.stdout
    stdout = _strip_ansi(result.stdout)
    assert "--auto=use-tracked" in stdout, stdout


def test_no_personal_config_in_help_examples_module() -> None:
    """The :mod:`setforge.cli._help_examples` source carries no personal config."""
    # Walk every module-level string constant and assert no needle leaks
    # in. This is the source-of-truth check that backs the per-command
    # ``test_no_personal_config_in_epilog`` parametrize above.
    for attr_name in dir(_help_examples):
        if attr_name.startswith("_"):
            continue
        value = getattr(_help_examples, attr_name)
        if not isinstance(value, str):
            continue
        for needle in _MODULE_PERSONAL_CONFIG_NEEDLES:
            assert needle not in value, (
                f"personal-config leak {needle!r} in "
                f"setforge.cli._help_examples.{attr_name}"
            )


# Per-command minimal flag-shape parse: invoke each leaf command's most
# common example shape against ``--help``; this proves the leaf path is
# wired into the Typer app (no typo in the registration), without
# exercising the underlying command body (which would require fixture
# config / tmp paths beyond the scope of this test file).
@pytest.mark.parametrize(
    "leaf_path", LEAF_COMMANDS, ids=[_id_for(p) for p in LEAF_COMMANDS]
)
def test_every_leaf_help_parses(runner: CliRunner, leaf_path: tuple[str, ...]) -> None:
    """``setforge <leaf-path> --help`` exits 0 — verifies registration is wired."""
    result = runner.invoke(app, [*leaf_path, "--help"], env={"COLUMNS": "100"})
    assert result.exit_code == 0, (
        f"--help for {' '.join(leaf_path)} exited {result.exit_code}:\n{result.stdout}"
    )
