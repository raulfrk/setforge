"""Wave-2 tests: the cutover's read-only legacy-state reader.

Pins that ``_build_legacy_records`` enumerates every (profile, tracked-file)
with its EFFECTIVE disposition + spans (shared config folded with the
``local.yaml`` host-local overlay), the tracked (base-seed) + live (local) byte
pair, and the structured/markdown discriminator — and that a missing/undeployed
dst is a flagged empty record, never a crash.
"""

from __future__ import annotations

from pathlib import Path

from setforge.config import Disposition
from setforge.migrations import MigrationRoots
from setforge.migrations._disposition_retire import _build_legacy_records

_CFG = """schema_version: "2.1"
tracked_files:
  notes:
    src: notes/NOTES.md
    dst: ~/.config/app/NOTES.md
    disposition: shared
  conf:
    src: conf/conf.yaml
    dst: ~/.config/app/conf.yaml
    disposition: forked
profiles:
  default:
    tracked_files: [notes, conf]
"""


def _setup(tmp_path: Path, *, cfg: str = _CFG, deploy: bool = True) -> MigrationRoots:
    repo = tmp_path / "repo"
    (repo / "tracked" / "notes").mkdir(parents=True)
    (repo / "tracked" / "conf").mkdir(parents=True)
    (repo / "setforge.yaml").write_text(cfg, encoding="utf-8")
    (repo / "tracked" / "notes" / "NOTES.md").write_text("# tracked notes\n")
    (repo / "tracked" / "conf" / "conf.yaml").write_text("editor:\n  fontSize: 12\n")
    if deploy:
        live = Path("~/.config/app").expanduser()
        live.mkdir(parents=True, exist_ok=True)
        (live / "NOTES.md").write_text("# live notes\n")
        (live / "conf.yaml").write_text("editor:\n  fontSize: 18\n")
    return MigrationRoots(
        cfg_path=repo / "setforge.yaml", repo_root=repo, home=Path.home()
    )


def _by_name(records):
    return {r.name: r for r in records}


def test_reader_enumerates_disposition_and_bytes(tmp_path) -> None:
    records = _build_legacy_records(_setup(tmp_path))
    by = _by_name(records)
    assert set(by) == {"notes", "conf"}

    notes = by["notes"]
    assert notes.profile == "default"
    assert notes.disposition is Disposition.SHARED
    assert notes.is_structured is False
    assert notes.tracked_bytes == b"# tracked notes\n"
    assert notes.live_bytes == b"# live notes\n"
    assert notes.dst_exists is True

    conf = by["conf"]
    assert conf.disposition is Disposition.FORKED
    assert conf.is_structured is True
    assert conf.tracked_bytes == b"editor:\n  fontSize: 12\n"
    assert conf.live_bytes == b"editor:\n  fontSize: 18\n"


def test_reader_folds_host_local_disposition_override(tmp_path) -> None:
    roots = _setup(tmp_path)
    local_yaml = Path("~/.config/setforge/local.yaml").expanduser()
    local_yaml.parent.mkdir(parents=True, exist_ok=True)
    # Host overrides conf's shared disposition (forked) -> pinned.
    local_yaml.write_text(
        "tracked_files:\n  conf:\n    disposition: pinned\n", encoding="utf-8"
    )
    by = _by_name(_build_legacy_records(roots))
    assert by["conf"].disposition is Disposition.PINNED
    # An un-overridden file keeps its shared-config disposition.
    assert by["notes"].disposition is Disposition.SHARED


def test_reader_flags_undeployed_dst_without_crash(tmp_path) -> None:
    roots = _setup(tmp_path, deploy=False)
    by = _by_name(_build_legacy_records(roots))
    for rec in by.values():
        assert rec.dst_exists is False
        assert rec.live_bytes == b""
    # Tracked src still read (base seed source is present in the repo).
    assert by["notes"].tracked_bytes == b"# tracked notes\n"
