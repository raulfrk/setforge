"""Audit-fix regression: init's source: block must survive a YAML round-trip.

``_build_source_block`` renders a literal ``source:`` mapping that is later
re-read by the safe-YAML loader. An unquoted scalar containing an inline
inline-comment sequence (`` #``), a leading YAML indicator, or a ``: ``
would be silently truncated or mis-parsed — pointing the resolved source at
the WRONG location with no error. The fix emits the scalars via
``json.dumps`` (a YAML-safe double-quoted scalar); this asserts the
round-trip is now lossless.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.cli.init import SourceChoice, SourceSpec, _build_source_block
from setforge.source import (
    GitSource,
    PathSource,
    SourceKind,
    _load_local_source_config,
)


def _round_trip(tmp_path: Path, block: str) -> object:
    """Write the rendered block as local.yaml and re-parse its source."""
    cfg = tmp_path / "local.yaml"
    # _build_source_block prefixes a blank line; the snippet is valid YAML
    # on its own, so write it directly as the document body.
    cfg.write_text(block, encoding="utf-8")
    return _load_local_source_config(cfg).source


@pytest.mark.parametrize(
    "raw_path",
    [
        "/home/user/cfg #1",  # inline-comment sequence — was truncated
        "/home/user/cfg with spaces",
        "/home/user/a: b",  # mapping-indicator sequence
        "/home/user/#leading-hash",
    ],
)
def test_build_source_block_path_round_trips(tmp_path: Path, raw_path: str) -> None:
    spec = SourceSpec(choice=SourceChoice.PATH, path=Path(raw_path))
    resolved = _round_trip(tmp_path, _build_source_block(spec))
    assert isinstance(resolved, PathSource)
    assert resolved.kind is SourceKind.PATH
    assert resolved.path == Path(raw_path)


@pytest.mark.parametrize(
    "raw_url",
    [
        "git@example.com:org/repo.git #mirror",  # inline-comment sequence
        "https://example.com/repo.git",
    ],
)
def test_build_source_block_git_url_round_trips(tmp_path: Path, raw_url: str) -> None:
    spec = SourceSpec(choice=SourceChoice.GIT, url=raw_url, ref="main")
    resolved = _round_trip(tmp_path, _build_source_block(spec))
    assert isinstance(resolved, GitSource)
    assert resolved.kind is SourceKind.GIT
    assert resolved.url == raw_url
    assert resolved.ref == "main"


def test_build_source_block_git_ref_with_hash_round_trips(
    tmp_path: Path,
) -> None:
    spec = SourceSpec(
        choice=SourceChoice.GIT,
        url="https://example.com/repo.git",
        ref="feature #99",
    )
    resolved = _round_trip(tmp_path, _build_source_block(spec))
    assert isinstance(resolved, GitSource)
    assert resolved.ref == "feature #99"
