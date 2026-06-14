"""Regression tests: duplicate user-section names must fail loudly.

Audit finding ``dup_section_names``: two sections sharing one name
(e.g. hand-authored or migrated tracked CLAUDE.md with two
``<!-- setforge:user-section start shared A -->`` regions) used to
collapse silently in the dict-keyed primitives — only the last body
survived, ``merge_sections`` spliced it into BOTH regions (first
region's distinct content permanently lost), and ``set_marker_hashes``
stamped one hash onto both end markers (corrupting the first region's
``hash=`` segment). The core parse/merge/hash primitives now raise
:class:`MarkerError` on the second pair instead of collapsing.
"""

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app
from setforge.errors import MarkerError, SetforgeError
from setforge.sections import (
    detect_duplicate_section_names,
    extract_marker_hashes,
    extract_sections,
    hash_sections,
    merge_sections,
    section_semantics,
    set_marker_hashes,
)

_HASH_64 = "0" * 64

_TWO_SHARED_A = (
    "before\n"
    "<!-- setforge:user-section start shared A -->\n"
    "LIVE1\n"
    f"<!-- setforge:user-section end shared A hash={_HASH_64} -->\n"
    "middle\n"
    "<!-- setforge:user-section start shared A -->\n"
    "LIVE2\n"
    f"<!-- setforge:user-section end shared A hash={_HASH_64} -->\n"
    "after\n"
)


def test_extract_sections_rejects_duplicate_name() -> None:
    with pytest.raises(MarkerError, match=r"duplicate user-section name 'A'"):
        extract_sections(_TWO_SHARED_A)


def test_extract_sections_rejects_duplicate_name_allow_legacy() -> None:
    # Legacy tolerance covers missing-keyword / missing-hash, NOT duplicate
    # names — a duplicate is structural corruption, not a pre-hash artifact.
    with pytest.raises(MarkerError, match=r"duplicate user-section name 'A'"):
        extract_sections(_TWO_SHARED_A, allow_legacy=True)


def test_merge_sections_rejects_duplicate_name() -> None:
    # The data-loss path: this used to emit LIVE2 into BOTH regions.
    with pytest.raises(MarkerError, match=r"duplicate user-section name 'A'"):
        merge_sections(_TWO_SHARED_A, {"A": "LIVE2\n"})


def test_set_marker_hashes_rejects_duplicate_name() -> None:
    # The hash-corruption path: this used to stamp one hash onto both ends.
    with pytest.raises(MarkerError, match=r"duplicate user-section name 'A'"):
        set_marker_hashes(_TWO_SHARED_A, {"A": _HASH_64})


def test_hash_sections_rejects_duplicate_name() -> None:
    with pytest.raises(MarkerError, match=r"duplicate user-section name 'A'"):
        hash_sections(_TWO_SHARED_A)


def test_extract_marker_hashes_rejects_duplicate_name() -> None:
    with pytest.raises(MarkerError, match=r"duplicate user-section name 'A'"):
        extract_marker_hashes(_TWO_SHARED_A)


def test_section_semantics_rejects_duplicate_name() -> None:
    with pytest.raises(MarkerError, match=r"duplicate user-section name 'A'"):
        section_semantics(_TWO_SHARED_A)


def test_distinct_names_still_parse() -> None:
    text = (
        "<!-- setforge:user-section start shared A -->\n"
        "BODY_A\n"
        f"<!-- setforge:user-section end shared A hash={_HASH_64} -->\n"
        "<!-- setforge:user-section start shared B -->\n"
        "BODY_B\n"
        f"<!-- setforge:user-section end shared B hash={_HASH_64} -->\n"
    )
    assert extract_sections(text) == {"A": "BODY_A\n", "B": "BODY_B\n"}


def test_repeated_unnamed_sections_do_not_collide() -> None:
    # Unnamed sections are keyed positionally ("0", "1", ...) so two of them
    # are NOT duplicates — the guard keys on the section key, not the marker
    # text, so positional keys stay distinct.
    text = (
        "<!-- setforge:user-section start shared -->\n"
        "B0\n"
        f"<!-- setforge:user-section end shared hash={_HASH_64} -->\n"
        "<!-- setforge:user-section start shared -->\n"
        "B1\n"
        f"<!-- setforge:user-section end shared hash={_HASH_64} -->\n"
    )
    assert extract_sections(text) == {"0": "B0\n", "1": "B1\n"}


def test_detect_duplicate_section_names_finds_repeat() -> None:
    assert detect_duplicate_section_names(_TWO_SHARED_A) == "A"


def test_detect_duplicate_section_names_none_when_distinct() -> None:
    text = (
        "<!-- setforge:user-section start shared A -->\n"
        "x\n"
        f"<!-- setforge:user-section end shared A hash={_HASH_64} -->\n"
        "<!-- setforge:user-section start host-local B -->\n"
        "y\n"
        f"<!-- setforge:user-section end host-local B hash={_HASH_64} -->\n"
    )
    assert detect_duplicate_section_names(text) is None


def test_detect_duplicate_section_names_ignores_unnamed() -> None:
    text = (
        "<!-- setforge:user-section start shared -->\n"
        "x\n"
        f"<!-- setforge:user-section end shared hash={_HASH_64} -->\n"
        "<!-- setforge:user-section start shared -->\n"
        "y\n"
        f"<!-- setforge:user-section end shared hash={_HASH_64} -->\n"
    )
    assert detect_duplicate_section_names(text) is None


# --- CLI wiring: the actionable error must surface before the raw MarkerError.
#
# detect_duplicate_section_names is wired into the install / compare / sync
# detector chain (setforge.cli._helpers._refuse_duplicate_section_names) so a
# tracked OR live file repeating a user-section name aborts with a clear
# "rename one of the two sections" message naming the duplicate — never the
# opaque "line N: duplicate user-section name 'A'" the strict parser would
# otherwise raise partway through deploy / capture.

_PROFILE = "dup-test"


def _write_repo(
    repo: Path, *, doc_body: str, dst: str = "~/.setforge_dup/doc.md"
) -> Path:
    (repo / "tracked").mkdir(parents=True, exist_ok=True)
    (repo / "tracked" / "doc.md").write_text(doc_body, encoding="utf-8")
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        f"    dst: {dst}\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - doc\n",
        encoding="utf-8",
    )
    return config


def _install(config: Path) -> Result:
    return CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--no-git-check",
            "--no-secrets-scan",
            "--yes",
            "--no-transition",
        ],
    )


def _compare(config: Path) -> Result:
    return CliRunner().invoke(
        app, ["compare", f"--profile={_PROFILE}", f"--config={config}"]
    )


def test_install_tracked_duplicate_surfaces_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _write_repo(tmp_path / "repo", doc_body=_TWO_SHARED_A)

    result = _install(config)

    # The duplicate-name gate raises a SetforgeError (surfaced on
    # result.exception under CliRunner; main() renders it to stderr in prod).
    assert result.exit_code != 0
    assert isinstance(result.exception, SetforgeError)
    message = str(result.exception)
    assert "duplicate user-section name 'A'" in message
    # The actionable rename guidance is what the user sees, NOT a bare
    # "line N: duplicate user-section name 'A'" parser trace.
    assert "Rename one of the two sections" in message
    assert "silently collapse" in message


def test_compare_tracked_duplicate_surfaces_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _write_repo(tmp_path / "repo", doc_body=_TWO_SHARED_A)

    result = _compare(config)

    assert result.exit_code != 0
    assert isinstance(result.exception, SetforgeError)
    message = str(result.exception)
    assert "duplicate user-section name 'A'" in message
    assert "Rename one of the two sections" in message


def test_compare_live_duplicate_surfaces_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tracked is clean; the duplicate lives on the deployed (live) side.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    clean = (
        "<!-- setforge:user-section start shared A -->\n"
        "BODY\n"
        f"<!-- setforge:user-section end shared A hash={_HASH_64} -->\n"
    )
    config = _write_repo(tmp_path / "repo", doc_body=clean)
    live = home / ".setforge_dup" / "doc.md"
    live.parent.mkdir(parents=True)
    live.write_text(_TWO_SHARED_A, encoding="utf-8")

    result = _compare(config)

    assert result.exit_code != 0
    assert isinstance(result.exception, SetforgeError)
    message = str(result.exception)
    assert "duplicate user-section name 'A'" in message
    assert str(live) in message


def test_install_distinct_names_not_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Distinct section names must NOT trip the duplicate gate (no false positive).
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    doc = (
        "<!-- setforge:user-section start shared A -->\n"
        "BODY_A\n"
        f"<!-- setforge:user-section end shared A hash={_HASH_64} -->\n"
        "<!-- setforge:user-section start shared B -->\n"
        "BODY_B\n"
        f"<!-- setforge:user-section end shared B hash={_HASH_64} -->\n"
    )
    config = _write_repo(tmp_path / "repo", doc_body=doc)

    result = _install(config)

    assert result.exit_code == 0, result.output
    assert "duplicate user-section name" not in result.output
