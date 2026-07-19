"""Wave-6 tests: the cutover apply orchestration (end-to-end, dormant migration).

The migration isn't registered yet (that lands with the field removal), so these
drive ``DispositionRetireMigration().apply`` directly. They pin: the seed +
schema stamp + legacy-store deletion happen; an already-unified fid is preserved
(never re-seeded); a re-run is idempotent; a fresh/empty profile is a clean
no-op; and the one committed transition captures the legacy manifests so revert
can restore them.
"""

from __future__ import annotations

from pathlib import Path

from setforge import reconcile, scalar_base_store, transitions
from setforge.migrations import MigrationRoots, detect_current_schema
from setforge.migrations._disposition_retire import DispositionRetireMigration
from setforge.reconcile import file_id
from setforge.reconcile.types import HunkClass
from setforge.transitions import SnapshotStore

_CFG = """schema_version: "2.1"
tracked_files:
  conf:
    src: conf/conf.yaml
    dst: ~/.config/app/conf.yaml
    disposition: forked
profiles:
  default:
    tracked_files: [conf]
"""

_EMPTY_CFG = """schema_version: "2.1"
tracked_files: {}
profiles:
  default:
    tracked_files: []
"""

_BASE = b"editor:\n  fontSize: 12\n"
_LIVE = b"editor:\n  fontSize: 18\n"


def _setup(tmp_path: Path, *, cfg: str = _CFG, deploy: bool = True) -> MigrationRoots:
    repo = tmp_path / "repo"
    (repo / "tracked" / "conf").mkdir(parents=True)
    (repo / "setforge.yaml").write_text(cfg, encoding="utf-8")
    (repo / "tracked" / "conf" / "conf.yaml").write_bytes(_BASE)
    if deploy:
        live = Path("~/.config/app").expanduser()
        live.mkdir(parents=True, exist_ok=True)
        (live / "conf.yaml").write_bytes(_LIVE)
    return MigrationRoots(
        cfg_path=repo / "setforge.yaml", repo_root=repo, home=Path.home()
    )


def test_apply_seeds_unified_stamps_and_deletes_legacy(tmp_path) -> None:
    roots = _setup(tmp_path)
    fid = file_id("conf")
    scalar_base_store.set_bases("default", "conf", {"editor.fontSize": 12})
    assert scalar_base_store.manifest_path("default", "conf").exists()

    DispositionRetireMigration().apply(roots=roots)

    assert detect_current_schema(roots.cfg_path) == "3.0"
    # Base seeded from TRACKED bytes (DL1), local from live bytes.
    assert reconcile.read_base("default", fid) == _BASE
    assert reconcile.read_local("default", fid) == _LIVE
    # forked -> every divergent key-unit is LOCAL.
    entry = reconcile.read_index("default").files["conf"]
    assert entry.hunks
    assert all(h["cls"] == HunkClass.LOCAL.value for h in entry.hunks)
    # Legacy scalar-base manifest is gone.
    assert not scalar_base_store.manifest_path("default", "conf").exists()


def test_apply_strips_disposition_spans_keys_from_config(tmp_path) -> None:
    """A migrated setforge.yaml is a clean 3.0 doc: the retired disposition/spans
    tracked-file keys are removed so ``validate`` accepts it (the 3.0 model only
    warn-and-ignores them, and ``validate`` rejects unknown keys)."""
    from setforge.config import load_config

    roots = _setup(tmp_path)
    DispositionRetireMigration().apply(roots=roots)

    text = roots.cfg_path.read_text(encoding="utf-8")
    assert "disposition" not in text
    assert "spans" not in text
    # Loads clean under the strict 3.0 model (no unknown-key warning to reject).
    load_config(roots.cfg_path)


def test_apply_is_idempotent(tmp_path) -> None:
    roots = _setup(tmp_path)
    DispositionRetireMigration().apply(roots=roots)
    index_after_first = reconcile.read_index("default")
    # A second run must not raise and must leave the index byte-identical.
    DispositionRetireMigration().apply(roots=roots)
    assert reconcile.read_index("default") == index_after_first


def test_already_unified_fid_is_preserved_not_clobbered(tmp_path) -> None:
    roots = _setup(tmp_path)
    fid = file_id("conf")
    custom: list[dict[str, object]] = [
        {
            "kind": "key",
            "cls": HunkClass.SHARED.value,
            "label": "editor.fontSize",
            "path": "editor.fontSize",
            "value_hash": "sha256:deadbeef",
        }
    ]
    from setforge import locking

    with locking.profile_lock("default"):
        reconcile.record(
            "default", fid, base=b"seeded\n", local=b"seeded-live\n", hunks=custom
        )

    DispositionRetireMigration().apply(roots=roots)

    # The pre-existing seed + its staged hunks survive (never re-seeded).
    assert reconcile.read_base("default", fid) == b"seeded\n"
    assert reconcile.read_index("default").files["conf"].hunks == custom
    assert detect_current_schema(roots.cfg_path) == "3.0"


def test_fresh_empty_profile_is_a_clean_no_op_stamp(tmp_path) -> None:
    roots = _setup(tmp_path, cfg=_EMPTY_CFG, deploy=False)
    DispositionRetireMigration().apply(roots=roots)
    assert detect_current_schema(roots.cfg_path) == "3.0"


def test_apply_writes_one_transition_capturing_the_legacy_manifest(tmp_path) -> None:
    roots = _setup(tmp_path)
    scalar_base_store.set_bases("default", "conf", {"editor.fontSize": 12})
    pre_bytes = scalar_base_store.manifest_path("default", "conf").read_bytes()

    DispositionRetireMigration().apply(roots=roots)

    # Exactly one migrate transition, and it captured the scalar-base manifest's
    # pre-cutover bytes so `setforge revert` can restore it.
    latest = transitions.list_transitions()[-1]
    snaps = transitions.load_state_snapshots(latest.directory)
    assert snaps is not None
    scalar = [
        e for e in snaps if e.store is SnapshotStore.SCALAR_BASE and e.key == "conf"
    ]
    assert scalar
    assert scalar[0].payload == pre_bytes


def test_threaded_pre_chain_text_patch_excludes_store_legs(
    tmp_path, monkeypatch
) -> None:
    """The text patch never overlaps a state_snapshot leg (INV-5).

    When the driver threads a pre-chain image, only the user-facing config
    stamp rides the text patch — every store leg is reversed by the binary
    ``state_snapshots`` alone. A store leg in BOTH would let the snapshot
    (a pre-cutover image) win on revert and strand it at the intermediate
    schema, so the transition's text-patch pre-image must be config-only.
    """
    from typing import Any

    from setforge.migrations import _disposition_retire
    from setforge.transitions import TransitionDir

    roots_pre = _setup(tmp_path)
    scalar_base_store.set_bases("default", "conf", {"editor.fontSize": 12})

    # A pre-chain image that carries BOTH the config stamp AND a store leg,
    # exactly as the driver's ``snapshot_paths(affected)`` would (the cutover's
    # own ``affected_paths`` enumerates that leg).
    leg = scalar_base_store.manifest_path("default", "conf")
    pre_chain = {
        roots_pre.cfg_path: 'schema_version: "2.1"\n',
        leg: leg.read_text(encoding="utf-8"),
    }
    roots = MigrationRoots(
        cfg_path=roots_pre.cfg_path,
        repo_root=roots_pre.repo_root,
        home=roots_pre.home,
        pre_chain_snapshot=pre_chain,
    )

    captured: dict[str, list[Path]] = {}
    real = _disposition_retire._write_cutover_transition

    def _spy(**kwargs: Any) -> TransitionDir:
        captured["file_pre"] = list(kwargs["file_pre"])
        return real(**kwargs)

    monkeypatch.setattr(_disposition_retire, "_write_cutover_transition", _spy)
    DispositionRetireMigration().apply(roots=roots)

    assert roots.cfg_path in captured["file_pre"]
    assert leg not in captured["file_pre"], (
        "store leg leaked into the text patch's pre-image — it overlaps its "
        "state_snapshot"
    )
