"""Tests for the clobber slot (slot 2) of the per-file drift classifier.

Span-only drift normally classifies EXPECTED — intentional host
divergence. But when the byte base is ABSENT, the next install takes the
base-absent deploy-tracked-verbatim path, which does NOT honor every span
override (forked spans get no post-merge re-assert), so the live span
edits are at clobber risk. Covers:

- span-only drift + base absent + disposition != PINNED → UNEXPECTED
  with the run-sync-first reason
- the same drift WITH a stored base stays EXPECTED
- a PINNED disposition is excluded (install never overwrites live)
- ``compare --check`` exits 1 on the clobber class
- a torn base-store read degrades deterministically (no crash)
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import base_store
from setforge.cli import app
from setforge.compare import CompareStatus, DriftClass, compare_profile
from setforge.config import Config, Profile, TrackedFile

_DOC = """\
# Title

## Pinned

Pinned body original.

## Shared

Shared body original.
"""

_DOC_SPAN_EDITED = _DOC.replace("Pinned body original.", "Pinned body LIVE.")

_CLOBBER_REASON_MARK = "no stored base"


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _span_file(tmp_path: Path, *, disposition: str = "shared") -> tuple[Config, Path]:
    """One markdown tracked_file with a pinned span; live drifts ONLY
    inside the span region. Returns (config, repo_root)."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "doc.md", _DOC)
    dst = tmp_path / "live" / "doc.md"
    _write(dst, _DOC_SPAN_EDITED)
    config = Config(
        tracked_files={
            "doc": TrackedFile.model_validate(
                {
                    "src": "doc.md",
                    "dst": str(dst),
                    "disposition": disposition,
                    "spans": [{"anchor": "## Pinned", "kind": "pinned"}],
                }
            )
        },
        profiles={"p": Profile(tracked_files=["doc"])},
    )
    return config, repo


def test_clobber_classifies_unexpected_with_reason(tmp_path: Path) -> None:
    """Span-only drift with NO stored base → UNEXPECTED + the sync-first
    reason; the report flags unexpected drift."""
    config, repo = _span_file(tmp_path)

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.status is CompareStatus.DRIFTED
    assert entry.span_only_drift is True
    assert entry.drift_class is DriftClass.UNEXPECTED
    assert entry.reason is not None
    assert _CLOBBER_REASON_MARK in entry.reason
    assert "run sync first" in entry.reason
    assert report.has_unexpected_drift is True


def test_span_drift_with_stored_base_stays_expected(tmp_path: Path) -> None:
    """The SAME span-only drift WITH a base present is the ordinary
    expected shape — the 3-way merge protects the live region."""
    config, repo = _span_file(tmp_path)
    base_store.write_base("p", "doc", _DOC.encode("utf-8"))

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.drift_class is DriftClass.EXPECTED
    assert entry.reason is None
    assert report.has_unexpected_drift is False


def test_pinned_disposition_excluded_from_clobber(tmp_path: Path) -> None:
    """disposition: pinned never overwrites live, so a base-absent span
    drift on it stays EXPECTED."""
    config, repo = _span_file(tmp_path, disposition="pinned")

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.drift_class is DriftClass.EXPECTED
    assert report.has_unexpected_drift is False


def test_torn_base_read_degrades_without_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A base-store read error is NOT base-absence: the probe degrades to
    not-clobber and the entry classifies via the later slots."""
    from setforge.errors import BaseStoreError

    config, repo = _span_file(tmp_path)

    def _raise(profile: str, file_id: str) -> bytes | None:
        raise BaseStoreError("torn read")

    monkeypatch.setattr("setforge.compare.base_store.read_base", _raise)

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    # Falls through to slot 4 (span-only drift is expected).
    assert entry.drift_class is DriftClass.EXPECTED


def _write_cli_config(tmp_path: Path) -> Path:
    """Write the span-file scenario as a real setforge.yaml; returns its path."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "doc.md", _DOC)
    dst = tmp_path / "live" / "doc.md"
    _write(dst, _DOC_SPAN_EDITED)
    cfg_path = repo / "setforge.yaml"
    cfg_path.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        f"    dst: {dst}\n"
        "    disposition: shared\n"
        "    spans:\n"
        '      - anchor: "## Pinned"\n'
        "        kind: pinned\n"
        "        semantics: shared\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [doc]\n",
        encoding="utf-8",
    )
    return cfg_path


def test_check_exits_1_on_clobber(tmp_path: Path) -> None:
    """compare --check fails on the clobber class (it IS unexpected)."""
    cfg_path = _write_cli_config(tmp_path)

    result = CliRunner().invoke(
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--check"]
    )
    assert result.exit_code == 1, result.output


def test_check_exits_0_once_base_stored(tmp_path: Path) -> None:
    """The clobber flag clears as soon as a base exists (post-sync state)."""
    cfg_path = _write_cli_config(tmp_path)
    base_store.write_base("p", "doc", _DOC.encode("utf-8"))

    result = CliRunner().invoke(
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--check"]
    )
    assert result.exit_code == 0, result.output
