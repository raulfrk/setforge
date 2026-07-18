"""Negative guard for PROV-5's user-scope-by-construction premise.

PROV-5 (docs/RULES.md) requires that any system-scope (apt) provisioner
soft-fails rather than demanding sudo or hanging. Today there is *no*
system-scope provisioner: every module under ``setforge/provision/`` installs
into ``$HOME``/config and never shells out to a system package manager. The
e2e "soft-fail path" gate is therefore vacuous, so this test enforces the
premise mechanically: it asserts that no provisioner invokes ``apt``,
``apt-get``, ``dpkg``, or ``sudo``.

The detector is AST-based, not a substring scan, so it cannot false-positive on
``adapt``/``chapter`` (which merely contain the letters "apt") nor on a banned
word sitting in a comment or an unrelated string. It inspects, per module:

* the argv of ``subprocess.run``/``Popen``/``call``/``check_call``/
  ``check_output`` calls — every string literal in the command list/tuple;
* the literal argument of ``shutil.which(...)`` and ``resolve_binary(...)``,
  the two indirection layers the provisioners use to name a binary;
* module-level ``*_BIN_NAME = "..."`` string constants, which those
  indirection calls dereference (argv[0] is frequently a ``Name`` bound to one
  of these rather than an inline literal).

Because the check reads real Python token structure, a comment like
``# never call apt`` or a docstring mentioning ``dpkg`` is invisible to it, and
a banned command is caught the moment it becomes a real subprocess argument.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import setforge

# Binaries a user-scope provisioner must never invoke. Exact-token match only
# (never substring), so "adapt"/"chapter" cannot trip the guard.
_BANNED_BINARIES: frozenset[str] = frozenset({"apt", "apt-get", "dpkg", "sudo"})

# subprocess entry points whose first positional arg is the command argv.
_SUBPROCESS_FUNCS: frozenset[str] = frozenset(
    {"run", "Popen", "call", "check_call", "check_output"}
)

# Indirection calls that name a binary by its (string-literal) first argument.
_BINARY_NAMING_FUNCS: frozenset[str] = frozenset({"which", "resolve_binary"})

_PROVISION_DIR: Path = Path(setforge.__file__).resolve().parent / "provision"


def _provision_modules() -> list[Path]:
    return sorted(_PROVISION_DIR.rglob("*.py"))


def _string_literal(node: ast.expr | None) -> str | None:
    """Return ``node``'s value if it is a string constant, else ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _called_func_name(call: ast.Call) -> str | None:
    """Return the (possibly-qualified tail) function name of ``call``.

    ``subprocess.run(...)`` -> ``"run"``; ``resolve_binary(...)`` ->
    ``"resolve_binary"``; ``shutil.which(...)`` -> ``"which"``.
    """
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _bin_name_constants(tree: ast.Module) -> dict[str, str]:
    """Map module-level ``*_BIN_NAME`` names to their string-literal value.

    These constants are the layer of indirection provisioners use for the
    binary name (e.g. ``_CARGO_BIN_NAME = "cargo"``), later dereferenced by
    ``resolve_binary``/``shutil.which`` or placed at argv[0]. Collecting them
    lets the detector see the effective binary even when argv[0] is a ``Name``.
    """
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        value = _string_literal(node.value)
        if value is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.endswith("_BIN_NAME"):
                constants[target.id] = value
    return constants


def _command_tokens(tree: ast.Module) -> set[str]:
    """Collect every effective command/binary token referenced in ``tree``.

    Returns the union of: string literals inside subprocess argv lists/tuples;
    literal or ``*_BIN_NAME``-resolved argv[0] names; and literal/constant
    arguments to ``shutil.which``/``resolve_binary``.
    """
    bin_constants = _bin_name_constants(tree)
    tokens: set[str] = set()

    def _resolve_name(node: ast.expr) -> str | None:
        # A ``Name`` argv[0] may point at a module-level ``*_BIN_NAME`` const.
        if isinstance(node, ast.Name):
            return bin_constants.get(node.id)
        return _string_literal(node)

    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        name = _called_func_name(call)
        if name is None:
            continue

        if name in _SUBPROCESS_FUNCS and call.args:
            argv = call.args[0]
            if isinstance(argv, ast.List | ast.Tuple):
                for i, element in enumerate(argv.elts):
                    literal = _string_literal(element)
                    if literal is not None:
                        tokens.add(literal)
                    elif i == 0:
                        # argv[0] as a Name -> try the *_BIN_NAME indirection.
                        resolved = _resolve_name(element)
                        if resolved is not None:
                            tokens.add(resolved)

        if name in _BINARY_NAMING_FUNCS and call.args:
            resolved = _resolve_name(call.args[0])
            if resolved is not None:
                tokens.add(resolved)

    return tokens


@pytest.mark.parametrize("module_path", _provision_modules(), ids=lambda p: p.name)
def test_no_provisioner_invokes_system_package_manager(module_path: Path) -> None:
    """No provisioner shells out to apt/apt-get/dpkg/sudo (PROV-5 premise)."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    offenders = _command_tokens(tree) & _BANNED_BINARIES
    assert not offenders, (
        f"{module_path.relative_to(_PROVISION_DIR.parent.parent)} invokes "
        f"system-scope binaries {sorted(offenders)}; PROV-5 requires "
        f"user-scope-by-construction (no apt/apt-get/dpkg/sudo)"
    )


def test_provision_dir_is_populated() -> None:
    """Guard the guard: the scan must actually cover provisioner modules."""
    modules = _provision_modules()
    assert modules, f"no provisioner modules found under {_PROVISION_DIR}"


# --- detector precision proofs (inline samples, no real file is mutated) -----

_CRAFTED_OFFENDER = """
import subprocess

def apply() -> None:
    subprocess.run(["sudo", "apt", "install", "-y", "ripgrep"], check=True)
"""

_ADAPT_LOOKALIKE = '''
"""This module would adapt a chapter of the manual; aptitude aside."""
import subprocess

_UV_BIN_NAME = "uv"

def apply() -> None:
    # We must never call apt / dpkg / sudo here (this comment is invisible).
    subprocess.run([_UV_BIN_NAME, "tool", "install", "adapter"], check=True)
'''

_DPKG_BIN_NAME_CONST = """
import shutil

_PKG_BIN_NAME = "dpkg"

def probe() -> None:
    shutil.which(_PKG_BIN_NAME)
"""


def test_detector_flags_crafted_sudo_apt_call() -> None:
    """A real ``subprocess.run(["sudo","apt",...])`` must be flagged."""
    tree = ast.parse(_CRAFTED_OFFENDER)
    assert _command_tokens(tree) & _BANNED_BINARIES == {"sudo", "apt"}


def test_detector_ignores_apt_lookalikes_and_comments() -> None:
    """ "adapt"/"chapter"/"aptitude" and banned words in comments/docstrings
    must NOT be flagged; only real subprocess/binary tokens count."""
    tree = ast.parse(_ADAPT_LOOKALIKE)
    assert _command_tokens(tree) & _BANNED_BINARIES == set()


def test_detector_flags_banned_bin_name_constant_via_which() -> None:
    """A ``*_BIN_NAME = "dpkg"`` dereferenced through ``shutil.which`` is
    flagged even though "dpkg" never appears in an argv literal."""
    tree = ast.parse(_DPKG_BIN_NAME_CONST)
    assert _command_tokens(tree) & _BANNED_BINARIES == {"dpkg"}
