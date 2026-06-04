"""Tests for the shallow scalar-overlay branch of copy_atomic.

These exercise :func:`setforge.deploy.copy_atomic` with ``scalar_bases`` set,
asserting it routes the SHALLOW ``preserve_user_keys`` overlay through the
stored-base 3-way driver (:mod:`setforge.scalar_overlay`) while keeping the
legacy tracked-structured contract for non-preserved keys, new tracked keys
and deep overlay. The ``scalar_bases=None`` regression case confirms the
legacy blind overlay is byte-for-byte unchanged.
"""

from pathlib import Path

from setforge.deploy import copy_atomic
from setforge.section_wizard import ReconcileAuto


def test_yaml_scalar_base_upstream_propagates_and_rebaselines(
    tmp_path: Path,
) -> None:
    # base for `a` == 1 (== live); tracked moved `a` to 2 → upstream
    # propagates. `b` is non-preserved and must stay tracked-structured.
    src = tmp_path / "src.yaml"
    src.write_text("a: 2\nb: TRACKED\n")
    dst = tmp_path / "dst.yaml"
    dst.write_text("a: 1\nb: LIVE\n")

    result = copy_atomic(
        src,
        dst,
        preserve_user_keys=["a"],
        scalar_bases={"a": 1},
    )

    text = dst.read_text()
    assert "a: 2" in text  # upstream change propagated via 3-way
    assert "b: TRACKED" in text  # non-preserved key keeps tracked
    assert result.new_scalar_bases == {"a": 2}
    assert result.scalar_conflicts == []


def test_yaml_scalar_base_user_edit_preserved(tmp_path: Path) -> None:
    # base == tracked == 1; live edited `a` to 9 → user edit preserved.
    src = tmp_path / "src.yaml"
    src.write_text("a: 1\n")
    dst = tmp_path / "dst.yaml"
    dst.write_text("a: 9\n")

    result = copy_atomic(src, dst, preserve_user_keys=["a"], scalar_bases={"a": 1})

    assert "a: 9" in dst.read_text()
    assert result.new_scalar_bases == {"a": 9}
    assert result.scalar_conflicts == []


def test_jsonc_scalar_base_upstream_propagates(tmp_path: Path) -> None:
    src = tmp_path / "src.json"
    src.write_text('{\n  "a": 2,\n  "b": "TRACKED"\n}\n')
    dst = tmp_path / "dst.json"
    dst.write_text('{\n  "a": 1,\n  "b": "LIVE"\n}\n')

    result = copy_atomic(src, dst, preserve_user_keys=["a"], scalar_bases={"a": 1})

    text = dst.read_text()
    assert '"a": 2' in text
    assert '"b": "TRACKED"' in text
    assert result.new_scalar_bases == {"a": 2}
    assert result.scalar_conflicts == []


def test_scalar_conflict_bare_keeps_live_omits_path_from_bases(
    tmp_path: Path,
) -> None:
    # base=1, live=7, tracked=8 → all differ → bare conflict keeps live and
    # the path is omitted from new_scalar_bases (defer) but reported as a
    # conflict.
    src = tmp_path / "src.yaml"
    src.write_text("a: 8\n")
    dst = tmp_path / "dst.yaml"
    dst.write_text("a: 7\n")

    result = copy_atomic(
        src,
        dst,
        preserve_user_keys=["a"],
        scalar_bases={"a": 1},
        merge_auto=None,
    )

    assert "a: 7" in dst.read_text()  # live kept
    assert result.scalar_conflicts == ["a"]
    assert result.new_scalar_bases is not None
    assert "a" not in result.new_scalar_bases  # base NOT advanced


def test_scalar_conflict_use_tracked_takes_tracked_and_advances(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src.yaml"
    src.write_text("a: 8\n")
    dst = tmp_path / "dst.yaml"
    dst.write_text("a: 7\n")

    result = copy_atomic(
        src,
        dst,
        preserve_user_keys=["a"],
        scalar_bases={"a": 1},
        merge_auto=ReconcileAuto.USE_TRACKED,
    )

    assert "a: 8" in dst.read_text()  # tracked taken
    assert result.scalar_conflicts == ["a"]
    assert result.new_scalar_bases == {"a": 8}


def test_scalar_base_absent_first_run_matches_blind_overlay(
    tmp_path: Path,
) -> None:
    # base ABSENT (no stored base for `a`) → first-run fallback keeps live and
    # seeds the base. Output must equal the legacy blind overlay (live wins).
    src = tmp_path / "src.yaml"
    src.write_text("a: 2\nb: TRACKED\n")
    dst = tmp_path / "dst.yaml"
    dst.write_text("a: 1\nb: LIVE\n")

    result = copy_atomic(
        src,
        dst,
        preserve_user_keys=["a"],
        scalar_bases={},  # no base for `a`
    )

    text = dst.read_text()
    assert "a: 1" in text  # blind live-wins (first run)
    assert "b: TRACKED" in text
    assert result.new_scalar_bases == {"a": 1}  # seeded


def test_scalar_bases_none_identical_to_legacy_blind_overlay(
    tmp_path: Path,
) -> None:
    # Regression: scalar_bases=None → the legacy blind overlay runs verbatim
    # and the scalar fields stay inert. Byte-compare against an explicit
    # legacy deploy of the same inputs.
    src = tmp_path / "src.yaml"
    src.write_text("a: 2\nb: TRACKED\nc: NEWKEY\n")
    dst_legacy = tmp_path / "legacy.yaml"
    dst_legacy.write_text("a: 1\nb: LIVE\n")
    dst_none = tmp_path / "none.yaml"
    dst_none.write_text("a: 1\nb: LIVE\n")

    legacy = copy_atomic(src, dst_legacy, preserve_user_keys=["a"])
    result = copy_atomic(src, dst_none, preserve_user_keys=["a"], scalar_bases=None)

    assert dst_none.read_text() == dst_legacy.read_text()
    assert result.new_scalar_bases is None
    assert result.scalar_conflicts == []
    assert legacy.new_scalar_bases is None


def test_jsonc_scalar_conflict_bare_keeps_live(tmp_path: Path) -> None:
    src = tmp_path / "src.json"
    src.write_text('{\n  "a": 8\n}\n')
    dst = tmp_path / "dst.json"
    dst.write_text('{\n  "a": 7\n}\n')

    result = copy_atomic(
        src,
        dst,
        preserve_user_keys=["a"],
        scalar_bases={"a": 1},
        merge_auto=None,
    )

    assert '"a": 7' in dst.read_text()
    assert result.scalar_conflicts == ["a"]
    assert result.new_scalar_bases is not None
    assert "a" not in result.new_scalar_bases
