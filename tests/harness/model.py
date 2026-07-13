from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from setforge import reconcile_apply, transitions
from setforge.config import Config, resolve_profile
from setforge.locking import profile_lock
from setforge.migrations import (
    MigrationRoots,
    detect_current_schema,
    find_migration_path,
)
from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt
from setforge.reconcile import (
    ABSENT,
    read_base,
    read_index,
    read_local,
    reconstruct,
    record,
    verify,
)
from setforge.reconcile.types import Absent, FileId, file_id
from setforge.reconcile_apply import ReconcileKind


@dataclass(slots=True)
class _Snapshot:
    verb: str
    profile: str
    config: Config | None
    store: tuple[transitions.StateSnapshotEntry, ...]
    config_bytes: str | None


@dataclass(slots=True)
class StubReconcileModel:
    root: Path
    state_dir: Path
    profile: str = "default"
    config: Config | None = None
    live: dict[str, str] = field(default_factory=dict)
    _transitions: list[_Snapshot] = field(default_factory=list)

    # -- construction --------------------------------------------------

    @classmethod
    def create(cls, root: Path) -> StubReconcileModel:
        state_dir = root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        return cls(root=root, state_dir=state_dir)

    def set_config(self, config: Config) -> None:
        self.config = config
        self.profile = next(iter(config.profiles))
        tracked = set(resolve_profile(config, self.profile).tracked_files)
        self.live = {
            fid: body for fid, body in self._live_from_store().items() if fid in tracked
        }
        self._write_config_yaml()

    # -- verbs (the @rule targets) -------------------------------------

    def install(self) -> None:
        self._snapshot("install")
        if self.config is None:
            return
        resolved = resolve_profile(self.config, self.profile)
        with profile_lock(self.profile):
            for fid_str in resolved.tracked_files:
                self._install_one(fid_str)

    def _install_one(self, fid_str: str) -> None:
        assert self.config is not None
        fid = file_id(fid_str)
        tracked = self._tracked_body(fid_str).encode("utf-8")
        live: bytes | Absent = (
            self.live[fid_str].encode("utf-8") if fid_str in self.live else ABSENT
        )
        outcome = reconcile_apply.reconcile_plain_file(
            self.profile, fid, live=live, tracked=tracked
        )
        if outcome.kind is ReconcileKind.WRITE:
            content = outcome.content
            assert isinstance(content, bytes)
            assert outcome.new_base is not None
            self.live[fid_str] = content.decode("utf-8", errors="surrogateescape")
            record(self.profile, fid, base=outcome.new_base, local=content)
        elif outcome.kind is ReconcileKind.REMOVE:
            assert outcome.new_base is not None
            self.live.pop(fid_str, None)
            record(self.profile, fid, base=outcome.new_base, local=ABSENT)

    def sync(self) -> None:
        self._snapshot("sync")
        with profile_lock(self.profile):
            for fid_str, body in self.live.items():
                fid = file_id(fid_str)
                base = read_base(self.profile, fid)
                if base is None:
                    continue
                record(self.profile, fid, base=base, local=body.encode("utf-8"))
            verify(self.profile)

    def migrate(self) -> None:
        self._snapshot("migrate")
        if self.config is None:
            return
        roots = MigrationRoots(
            cfg_path=self._config_path(), repo_root=self.root, home=self.root
        )
        current = detect_current_schema(self._config_path())
        chain = find_migration_path(from_v=current, to_v=self.config.schema_version)
        for step in chain:
            step.apply(roots=roots)

    def revert(self) -> None:
        if not self._transitions:
            return
        snap = self._transitions.pop()
        self.profile = snap.profile
        self.config = snap.config
        with profile_lock(self.profile):
            transitions.restore_state_snapshots(snap.store)
        # Derived from the restored store, not snap.live, so a store leg the
        # restore didn't re-materialize can't hide as a false-green INV-1.
        self.live = self._live_from_store()
        if snap.config_bytes is not None:
            self._config_path().write_text(snap.config_bytes, encoding="utf-8")

    # -- observable state (the invariants read these) ------------------

    def store_index(self) -> dict[str, dict[str, str]]:
        index = read_index(self.profile)
        return {
            fid: {"present": str(entry.present), "local_hash": entry.local_hash or ""}
            for fid, entry in index.files.items()
        }

    def indexed_file_ids(self) -> set[str]:
        return set(read_index(self.profile).files)

    def live_bodies(self) -> dict[str, str]:
        return dict(self.live)

    def base_plus_local(self, file_id_str: str) -> bytes | None:
        result = reconstruct(self.profile, file_id(file_id_str))
        if result is ABSENT or result is None:
            return None if result is None else b""
        return result

    def recorded_local(self, file_id_str: str) -> bytes | None | Absent:
        return read_local(self.profile, file_id(file_id_str))

    def transition_count(self) -> int:
        return len(self._transitions)

    def schema_version(self) -> str:
        return detect_current_schema(self._config_path())

    def verify_store(self) -> None:
        verify(self.profile)

    # -- internals -----------------------------------------------------

    def _live_from_store(self) -> dict[str, str]:
        live: dict[str, str] = {}
        for fid_str in self.indexed_file_ids():
            body = reconstruct(self.profile, file_id(fid_str))
            if isinstance(body, bytes):
                live[fid_str] = body.decode("utf-8", errors="surrogateescape")
        return live

    def _config_path(self) -> Path:
        return self.root / "setforge.yaml"

    def _write_config_yaml(self) -> None:
        assert self.config is not None
        yaml = yaml_rt()
        data = yaml.load(self.config.model_dump_json())
        data["schema_version"] = "1.0"
        atomic_write_yaml(self._config_path(), data)

    def _tracked_body(self, fid_str: str) -> str:
        assert self.config is not None
        tracked_file = self.config.tracked_files[fid_str]
        return f"# tracked body for {fid_str}\nsrc={tracked_file.src}\n"

    def _capture_store(self) -> tuple[transitions.StateSnapshotEntry, ...]:
        entries: list[transitions.StateSnapshotEntry] = []
        keys: set[str] = set(self.live) | self.indexed_file_ids()
        if self.config is not None:
            keys |= set(resolve_profile(self.config, self.profile).tracked_files)
        for key in sorted(keys):
            entries.extend(transitions.reconcile_file_snapshots(self.profile, key))
            entries.append(
                transitions.snapshot_store_state(
                    transitions.SnapshotStore.BASE, self.profile, key
                )
            )
        entries.append(
            transitions.snapshot_store_state(
                transitions.SnapshotStore.INDEX, self.profile, ""
            )
        )
        return tuple(entries)

    def _snapshot(self, verb: str) -> None:
        cfg_path = self._config_path()
        config_bytes = (
            cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else None
        )
        self._transitions.append(
            _Snapshot(
                verb=verb,
                profile=self.profile,
                config=self.config,
                store=self._capture_store(),
                config_bytes=config_bytes,
            )
        )


__all__ = ["StubReconcileModel"]


# Keeps FileId referenced so a linter does not flag it as an unused import.
_TYPES: tuple[object, ...] = (FileId,)
