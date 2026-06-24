#!/usr/bin/env python3
"""Draft a config-schema-entry skeleton (model + validator + round-trip test).

A GENERATIVE SCAFFOLD: it writes a DRAFT pydantic ``BaseModel`` carrying one
new field, a ``field_validator`` stub for it, and a round-trip unit-test
skeleton — modeled on the strict models in ``setforge/config.py``. The draft
is a STARTING POINT for human review: it is written to a scratch location, not
spliced into the live schema (which is frozen by an additive-only manifest
gate), and the scaffold never auto-commits and never overwrites an existing
file.

Safety contract (the scaffold takes UNTRUSTED CLI input):

* ``--key`` is validated against a strict bare-identifier regex; anything with
  ``..``, a path separator, a dot, whitespace, or other non-identifier
  characters is rejected before it reaches the filesystem or a code template.
* ``--type`` is restricted to a small allowlist of scalar Python types — an
  arbitrary expression (``os.system``, ``Evil()``, ``list[int]``) is refused,
  so nothing executable is ever interpolated into the drafted source.
* The output path is derived from the (validated) key, resolved, and confirmed
  to stay under the intended destination dir — a traversal escape is refused.
* The file is written with ``exist_ok=False`` semantics (never clobbers).
* User input is interpolated as DATA into a fixed Python template via
  ``str.format`` over pre-validated tokens — never a template-from-string
  engine.

Invocation::

    uv run python scripts/author_config_entry.py --key retry_count --type int
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Default destination root for drafted config-entry skeletons.
_DEST_SUBDIR = ("tests", "config_entries")

# A bare Python identifier (single segment, no dot): the shape a config field
# name must take. Rejects ``..``, dots, path separators, whitespace, metachars.
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Allowlist of scalar field types the scaffold will draft. Restricting to a
# fixed set means no arbitrary expression is ever rendered into source.
_ALLOWED_TYPES: dict[str, str] = {
    "int": "0",
    "str": '""',
    "bool": "False",
    "float": "0.0",
}

_TEMPLATE = '''\
"""DRAFT config-schema entry for {key} ({pytype}).

Generated scaffold — review, then splice the field + validator into the real
{config_model} model in setforge/config.py and update the frozen field
manifest in the SAME commit (the additive-only schema gate requires it). This
file is a starting point, not finished schema code.
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator


class Draft{classname}(BaseModel):
    """Standalone draft of the new {key} field for review.

    Mirrors the strict-model shape used in setforge/config.py (extra keys
    forbidden). Move the field + validator onto the real model when ready.
    """

    model_config = {{"extra": "forbid"}}

    {key}: {pytype} = {default}

    @field_validator("{key}")
    @classmethod
    def _validate_{key}(cls, v: {pytype}) -> {pytype}:
        # TODO: enforce the real {key} constraints here.
        return v


def test_{key}_round_trips() -> None:
    """The {key} field survives a model_dump -> model_validate round trip."""
    original = Draft{classname}({key}={default})
    restored = Draft{classname}.model_validate(original.model_dump())
    assert restored == original
'''


def _validate_key(key: str) -> str:
    """Reject anything that is not a bare Python identifier."""
    if not _KEY_RE.match(key):
        raise ValueError(
            f"invalid --key {key!r}: must be a bare identifier (e.g. retry_count); "
            f"no dots, '..', path separators, or spaces"
        )
    return key


def _validate_type(pytype: str) -> str:
    """Reject any type outside the scalar allowlist."""
    if pytype not in _ALLOWED_TYPES:
        allowed = ", ".join(sorted(_ALLOWED_TYPES))
        raise ValueError(f"invalid --type {pytype!r}: must be one of {{{allowed}}}")
    return pytype


def _classname(key: str) -> str:
    """CamelCase the validated key for the draft model name."""
    return "".join(part.capitalize() for part in key.split("_") if part) or "Entry"


def _dest_dir(dest_root: Path) -> Path:
    return dest_root.joinpath(*_DEST_SUBDIR)


def draft(key: str, pytype: str, dest_root: Path = REPO_ROOT) -> Path:
    """Draft a config-entry skeleton for ``key`` / ``pytype``; return its path.

    Validates both inputs, confines the output under ``dest_root`` / the
    destination subdir, refuses to overwrite, and writes a fixed template with
    the validated tokens interpolated as data. Raises ``ValueError`` on bad
    input and ``FileExistsError`` if the target already exists.
    """
    key = _validate_key(key)
    pytype = _validate_type(pytype)

    dest_dir = _dest_dir(dest_root).resolve()
    out = (dest_dir / f"draft_config_{key}.py").resolve()
    if dest_dir != out.parent:
        raise ValueError(f"refusing path escape: {out} is outside {dest_dir}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    content = _TEMPLATE.format(
        key=key,
        pytype=pytype,
        default=_ALLOWED_TYPES[pytype],
        classname=_classname(key),
        config_model="Config",
    )
    with open(out, "x", encoding="utf-8") as fh:
        fh.write(content)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="author-config-entry",
        description="Draft a config-schema entry + validator + round-trip test.",
    )
    parser.add_argument(
        "--key", required=True, help="New config field name (bare identifier)."
    )
    parser.add_argument(
        "--type",
        required=True,
        dest="pytype",
        help=f"Field type, one of: {', '.join(sorted(_ALLOWED_TYPES))}.",
    )
    args = parser.parse_args(argv)
    try:
        out = draft(key=args.key, pytype=args.pytype)
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
