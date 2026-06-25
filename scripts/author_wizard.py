#!/usr/bin/env python3
"""Draft a button-bar wizard skeleton (module + paired INV test).

A GENERATIVE SCAFFOLD: it writes a DRAFT wizard module that drives
:func:`setforge.ui.widgets.button_bar`, plus a paired INV test asserting the
two invariants every wizard must hold — (a) it is UX-1 clean (no legacy
``read_one_choice`` letter menu) and (b) it returns a button value or
:data:`~setforge.ui.widgets.CANCEL` on Esc. The draft is a STARTING POINT for
human review: it is written to a scratch location, never auto-committed, and
never overwrites an existing file.

Safety contract (the scaffold takes UNTRUSTED CLI input) — mirrors
:mod:`scripts.author_config_entry`:

* ``--name`` is validated against a strict bare-identifier regex FIRST;
  anything with ``..``, a path separator, a dot, whitespace, or braces is
  rejected before it reaches the filesystem or a code template (the brace ban
  closes the ``str.format`` injection vector).
* The output paths are derived from the validated name, resolved, and confirmed
  to stay under the destination dir (``dest_dir == out.parent``) — a traversal
  escape is refused.
* Files are written with ``"x"`` mode (never clobber).
* User input is interpolated as DATA into fixed module-constant templates via
  ``str.format`` over the pre-validated token — never a template-from-string
  engine, never a user-built format string.

Invocation::

    uv run python scripts/author_wizard.py --name demo
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Default destination root for drafted wizards.
_DEST_SUBDIR = ("tests", "wizard_drafts")

# A bare Python identifier (single segment, no dot): rejects ``..``, dots, path
# separators, whitespace, braces, and shell metacharacters.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_TEMPLATE_MODULE = '''\
"""DRAFT button-bar wizard for {name}.

Generated scaffold — review, replace the placeholder buttons + body with the
real choices, then wire it into the surface. This is a starting point, not
finished wizard code. It drives the shared button bar and returns the chosen
value or CANCEL on Esc.
"""

from __future__ import annotations

from setforge.ui.widgets import CANCEL, Button, button_bar


def run_{name}_wizard() -> str:
    """Run the {name} wizard; return the chosen value, or "cancel" on Esc."""
    buttons: list[Button[str]] = [
        Button("Proceed", "proceed"),
        Button("Skip", "skip"),
    ]
    result = button_bar(
        buttons,
        title="{classname}",
        body="Replace this body with the real prompt for {name}.",
    )
    if result is CANCEL:
        return "cancel"
    return result
'''

_TEMPLATE_TEST = '''\
"""DRAFT INV test for the {name} wizard.

Generated scaffold — asserts the two invariants every button-bar wizard must
hold:

(a) UX-1 clean — the drafted module uses no legacy ``read_one_choice`` letter
    menu (it is checked through the real policy lint over a setforge-scoped
    path so the UX-1 rule actually fires).
(b) Esc -> CANCEL — driving the wizard with a bare Esc keypress under a piped
    input resolves to the cancel outcome, never a stray button value.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

sys.path.insert(0, "{repo_root}")

from scripts.check_policy_lints import check_source  # noqa: E402

_MODULE_PATH = Path("{module_path}")


def _load_wizard():
    spec = importlib.util.spec_from_file_location("drafted_{name}_wizard", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_{name}_wizard_is_ux1_clean() -> None:
    source = _MODULE_PATH.read_text(encoding="utf-8")
    # Lint under a setforge-scoped path so the UX-1 rule is in scope.
    violations = check_source(source, "setforge/{name}_wizard.py")
    assert not [v for v in violations if v.rule_id == "UX-1"], violations


def test_{name}_wizard_esc_cancels() -> None:
    module = _load_wizard()
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\\x1b")  # bare Esc
        with create_app_session(input=pipe, output=DummyOutput()):
            result = module.run_{name}_wizard()
    assert result == "cancel"
'''


def _validate_name(name: str) -> str:
    """Reject anything that is not a bare Python identifier."""
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid --name {name!r}: must be a bare identifier (e.g. demo); "
            f"no dots, '..', path separators, braces, or spaces"
        )
    return name


def _classname(name: str) -> str:
    """CamelCase the validated name for the draft wizard title/class label."""
    return "".join(part.capitalize() for part in name.split("_") if part) or "Wizard"


def _dest_dir(dest_root: Path) -> Path:
    return dest_root.joinpath(*_DEST_SUBDIR)


def draft(name: str, dest_root: Path = REPO_ROOT) -> tuple[Path, Path]:
    """Draft a wizard module + paired INV test for ``name``; return both paths.

    Validates the name, confines both outputs under ``dest_root`` / the
    destination subdir, refuses to overwrite, and writes the fixed templates
    with the validated token interpolated as data. Raises ``ValueError`` on bad
    input and ``FileExistsError`` if either target already exists.
    """
    name = _validate_name(name)

    dest_dir = _dest_dir(dest_root).resolve()
    module_path = (dest_dir / f"{name}_wizard.py").resolve()
    test_path = (dest_dir / f"test_{name}_wizard.py").resolve()
    # Confinement: both resolved targets MUST stay under dest_dir.
    for out in (module_path, test_path):
        if dest_dir != out.parent:
            raise ValueError(f"refusing path escape: {out} is outside {dest_dir}")

    dest_dir.mkdir(parents=True, exist_ok=True)

    classname = _classname(name)
    module_src = _TEMPLATE_MODULE.format(name=name, classname=classname)
    test_src = _TEMPLATE_TEST.format(
        name=name,
        repo_root=str(REPO_ROOT),
        module_path=str(module_path),
    )

    # 'x' mode raises FileExistsError on clobber. Write the module first; if the
    # test write fails, the module is the only artifact left (re-running is a
    # clean FileExistsError, never a silent partial overwrite).
    with open(module_path, "x", encoding="utf-8") as fh:
        fh.write(module_src)
    with open(test_path, "x", encoding="utf-8") as fh:
        fh.write(test_src)
    return module_path, test_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="author-wizard",
        description="Draft a button-bar wizard module + paired INV test.",
    )
    parser.add_argument(
        "--name", required=True, help="New wizard name (bare identifier)."
    )
    parser.add_argument(
        "--dest-root",
        default=None,
        help="Destination root (defaults to the repo root); drafts land under "
        "its tests/wizard_drafts/ subdir.",
    )
    args = parser.parse_args(argv)
    dest_root = Path(args.dest_root).resolve() if args.dest_root else REPO_ROOT
    try:
        module_path, test_path = draft(name=args.name, dest_root=dest_root)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileExistsError:
        print(
            "error: refusing to overwrite existing file; remove it first",
            file=sys.stderr,
        )
        return 1
    print(module_path)
    print(test_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
