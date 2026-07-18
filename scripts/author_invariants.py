#!/usr/bin/env python3
"""Draft a stateful-invariant test skeleton for one component / INV pair.

A GENERATIVE SCAFFOLD: it writes a conformant ``InvariantStateMachine``
subclass with one ``@invariant()``-decorated method stub for the named
invariant, modeled on the harness at ``tests/harness/``. The drafted file is
a STARTING POINT for human review — the scaffold never auto-commits and never
overwrites an existing file.

Safety contract (the scaffold takes UNTRUSTED CLI input):

* ``--component`` / ``--inv`` are validated against a strict identifier
  regex; anything with ``..``, a path separator, whitespace, or other
  non-identifier characters is rejected before it reaches the filesystem or
  a code template.
* The output path is derived from the (validated) component, resolved, and
  confirmed to stay under the intended ``tests/`` destination dir — a
  traversal escape is refused.
* The file is written with ``exist_ok=False`` semantics (an existing file is
  never clobbered).
* User input is interpolated as DATA into a fixed Python template via
  ``str.format`` over pre-validated identifier tokens — never via a
  Jinja/template-from-string engine.

Invocation::

    uv run python scripts/author_invariants.py \\
        --component setforge.reconcile.merge --inv INV-2
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Default destination root for drafted invariant machines.
_DEST_SUBDIR = ("tests", "invariants")

# A dotted Python identifier: segments of [A-Za-z0-9_], dot-joined. Rejects
# ``..`` (empty segment), path separators, whitespace, and shell metacharacters.
_COMPONENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
# An invariant id like ``INV-2`` / ``INV-10``: letters, digits, dashes,
# underscores only. No path separators or whitespace.
_INV_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_TEMPLATE = '''\
"""DRAFT invariant state machine for {component} ({inv}).

Generated scaffold — review, fill in the rule bodies and the {inv} assertion,
then wire it into the suite. This is a starting point, not finished test code.
"""

from __future__ import annotations

from hypothesis.stateful import rule

from tests.harness.invariants import InvariantStateMachine, invariant
from tests.harness.model import StubReconcileModel


class {classname}(InvariantStateMachine):
    """State machine exercising {inv} for {component}."""

    model_factory = staticmethod(StubReconcileModel.create)

    @rule()
    def step(self) -> None:
        # TODO: drive the model verb(s) relevant to {inv}.
        self.model.install()

    @invariant()
    def {method}(self) -> None:
        # TODO: assert {inv} over self.model's observable state.
        assert self.model is not None


Test{classname} = {classname}.TestCase
'''


def _validate_component(component: str) -> str:
    """Reject anything that is not a dotted Python identifier."""
    if not _COMPONENT_RE.match(component):
        raise ValueError(
            f"invalid --component {component!r}: must be a dotted identifier "
            f"(e.g. setforge.reconcile.merge); no '..', path separators, or spaces"
        )
    return component


def _validate_inv(inv: str) -> str:
    """Reject an invariant id that is not a bare ``[A-Za-z0-9_-]+`` token."""
    if not _INV_RE.match(inv):
        raise ValueError(
            f"invalid --inv {inv!r}: must match [A-Za-z0-9_-]+ (e.g. INV-2)"
        )
    return inv


def _classname(component: str, inv: str) -> str:
    """Derive a CamelCase machine name from the validated tokens."""
    parts = [*component.split("."), inv.replace("-", "_")]
    return "".join(p.capitalize() for p in parts if p) + "Machine"


def _method_name(inv: str) -> str:
    """A snake_case invariant-method name from the validated INV id."""
    return "holds_" + inv.lower().replace("-", "_")


def _dest_dir(dest_root: Path) -> Path:
    """The destination directory under ``dest_root`` (created if missing)."""
    return dest_root.joinpath(*_DEST_SUBDIR)


def draft(component: str, inv: str, dest_root: Path = REPO_ROOT) -> Path:
    """Draft an invariant machine for ``component`` / ``inv``; return its path.

    Validates both inputs, confines the output under ``dest_root`` / the
    destination subdir, refuses to overwrite, and writes a fixed template
    with the validated tokens interpolated as data. Raises ``ValueError`` on
    bad input and ``FileExistsError`` if the target already exists.
    """
    component = _validate_component(component)
    inv = _validate_inv(inv)

    dest_dir = _dest_dir(dest_root).resolve()
    filename = f"test_{component.replace('.', '_')}_{inv.lower().replace('-', '_')}.py"
    out = (dest_dir / filename).resolve()
    # Confinement: the resolved target MUST stay under dest_dir.
    if dest_dir != out.parent:
        raise ValueError(f"refusing path escape: {out} is outside {dest_dir}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    content = _TEMPLATE.format(
        component=component,
        inv=inv,
        classname=_classname(component, inv),
        method=_method_name(inv),
    )
    # exist_ok=False semantics: 'x' mode raises FileExistsError on clobber.
    with out.open("x", encoding="utf-8") as fh:
        fh.write(content)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="author-invariants",
        description="Draft a Hypothesis invariant state machine for a component/INV.",
    )
    parser.add_argument(
        "--component",
        required=True,
        help="Dotted component name (e.g. setforge.reconcile.merge).",
    )
    parser.add_argument(
        "--inv",
        required=True,
        help="Invariant id to stub (e.g. INV-2).",
    )
    args = parser.parse_args(argv)
    try:
        out = draft(component=args.component, inv=args.inv)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileExistsError:
        print(
            "error: refusing to overwrite existing file; remove it first",
            file=sys.stderr,
        )
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
