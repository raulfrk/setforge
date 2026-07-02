"""Wave-4 tests: the corrupt-base pre-flight abort (D4).

A single malformed structured base makes _validate_bases raise (listing the
offender); an all-valid set returns cleanly. Validation is pure — no mutation —
so "0 files changed on abort" holds by construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.errors import ConfigError
from setforge.migrations._disposition_retire import _FidLegacy, _validate_bases
from setforge.reconcile import file_id


def _rec(name: str, dst: str, tracked: bytes, *, is_structured: bool) -> _FidLegacy:
    return _FidLegacy(
        profile="default",
        name=name,
        fid=file_id(name),
        src=Path(f"/repo/tracked/{name}"),
        dst=Path(dst),
        disposition="forked",
        spans=(),
        tracked_bytes=tracked,
        live_bytes=tracked,
        dst_exists=True,
        is_structured=is_structured,
    )


def test_all_valid_bases_pass() -> None:
    records = [
        _rec("conf", "conf.yaml", b"editor:\n  fontSize: 12\n", is_structured=True),
        _rec("notes", "NOTES.md", b"# not structured\n", is_structured=False),
    ]
    _validate_bases(records)  # no raise


def test_one_corrupt_structured_base_aborts_and_names_it() -> None:
    records = [
        _rec("good", "good.yaml", b"a: 1\n", is_structured=True),
        _rec("bad", "bad.yaml", b"a: [unterminated\n", is_structured=True),
    ]
    with pytest.raises(ConfigError, match=r"default/bad"):
        _validate_bases(records)


def test_markdown_base_is_not_validated() -> None:
    # A non-structured file with arbitrary bytes never trips the structured gate.
    records = [_rec("notes", "NOTES.md", b"\x00\xff not yaml\n", is_structured=False)]
    _validate_bases(records)  # no raise
