"""The reconcile seam the state machine drives (RFC 0001 §6, E2).

:class:`StubReconcileModel` drives setforge's REAL reconcile engine over a
``self.root``-isolated store — the base/local/index store, the line-level
3-way merge, and the schema-version migration registry, per the RFC §9.3
layout:

    <state_dir>/base/<profile>/<file-id>    3-way merge base (verbatim bytes)
    <state_dir>/local/<profile>/<file-id>   recorded keep-local content
    <state_dir>/index/<profile>.json        classification document
    <state_dir>/locks/<profile>.lock        the per-profile write lock

E1 shipped this as a hand-rolled stub whose ``_engine_*`` bodies faked the
store; E2 replaced each body with a call into the real engine
(:mod:`setforge.reconcile_apply`, :mod:`setforge.reconcile.store`,
:mod:`setforge.migrations`, :mod:`setforge.transitions`). The public verb
methods (:meth:`install` / :meth:`sync` / :meth:`revert` / :meth:`migrate`)
and the observable accessors (:meth:`store_index` / :meth:`live_bodies` /
:meth:`base_plus_local` / :meth:`schema_version`) are the stable contract the
invariants assert against, so the swap did not touch the state machine.

The engine is subprocess-free on this path: the merge is pure, the store does
fsync-backed atomic writes, and the migration registry mutates a
``setforge.yaml`` on disk via ruamel — no ``claude`` / ``code`` / ``gitleaks``
shell-out is reached. The name ``StubReconcileModel`` is retained so the
scaffold's meta-tests and ``scripts/author_invariants.py`` keep importing it.
"""

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
    """One transition's pre-state, captured immediately BEFORE a verb ran.

    ``store`` is the real per-file store-leg capture (local content, its
    absence marker, the drafts manifest, the base, and the profile index) plus
    the ``setforge.yaml`` bytes; restoring it is the real
    :func:`transitions.restore_state_snapshots` reverse, so
    ``revert ∘ verb == identity`` on the observable store (INV-3, INV-5).
    ``profile`` + ``config`` are the active profile / config at capture time,
    restored so a revert that crosses a ``reconfigure`` profile switch reads the
    store back under the SAME profile. Live is NOT captured: ``revert`` derives
    it from the RESTORED store (see :meth:`StubReconcileModel._live_from_store`),
    which is the byte-exact reality and keeps live ⊆ store-reconstructable.
    """

    verb: str
    profile: str
    config: Config | None
    store: tuple[transitions.StateSnapshotEntry, ...]
    config_bytes: str | None


@dataclass(slots=True)
class StubReconcileModel:
    """On-disk model of the real reconcile state, rooted at :attr:`root`.

    ``live`` maps file-id → the current deployed body. The classification
    ``index`` and the ``base``/``local`` stores are the REAL on-disk stores
    under :attr:`state_dir` (``$SETFORGE_STATE_DIR``); the invariants read them
    back through the real store accessors, never a mirrored in-memory copy.
    """

    root: Path
    state_dir: Path
    profile: str = "default"
    config: Config | None = None
    live: dict[str, str] = field(default_factory=dict)
    _transitions: list[_Snapshot] = field(default_factory=list)

    # -- construction --------------------------------------------------

    @classmethod
    def create(cls, root: Path) -> StubReconcileModel:
        """Build a model rooted at ``root`` with a fresh state dir.

        The ``(Path) -> model`` signature is the ``model_factory`` contract
        :class:`tests.harness.invariants.InvariantStateMachine` expects.
        ``$SETFORGE_STATE_DIR`` is pointed at ``state_dir`` by the machine's
        setup so every real engine path lands inside ``root``.
        """
        state_dir = root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        return cls(root=root, state_dir=state_dir)

    def set_config(self, config: Config) -> None:
        """Adopt a (generated) config and align the active profile + on-disk YAML.

        Writes the config to ``root/setforge.yaml`` (the migration registry's
        real target) and picks the first profile — the CLI's single-profile
        ``--profile=`` contract. ``live`` is then RE-DERIVED from the store of
        the new active profile (:meth:`_live_from_store`), then narrowed to the
        profile's tracked-file set. Deriving — not carrying the old ``live`` — is
        what makes the profile-switch case correct: a fid the new profile happens
        to track by NAME but has never deployed under it has no store record, so
        it must NOT appear in live (carrying it would be a spurious "live file
        with no store record", a model artifact — the store is the truth for what
        is deployed under a profile). Does not reconcile; ``install`` does.
        """
        self.config = config
        self.profile = next(iter(config.profiles))
        tracked = set(resolve_profile(config, self.profile).tracked_files)
        self.live = {
            fid: body for fid, body in self._live_from_store().items() if fid in tracked
        }
        self._write_config_yaml()

    # -- verbs (the @rule targets) -------------------------------------

    def install(self) -> None:
        """Deploy tracked → live via the real 3-way reconcile + store record.

        Each tracked file runs :func:`reconcile_apply.reconcile_plain_file`
        (non-interactive: a genuine conflict DEFERS, writing nothing) and, on a
        WRITE / REMOVE, advances the real base/local/index store under the
        profile lock. An idempotent re-install is a store NOOP.
        """
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
        """Capture live back into the store, then verify the store invariants.

        Re-records each live file's bytes as recorded-local against its stored
        base (the capture path), then runs the real :func:`store.verify` — the
        engine's own INV-2 + INV-10 fail-closed check — over the whole profile.
        """
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
        """Run the real schema-migration chain on ``setforge.yaml``.

        Resolves the on-disk ``schema_version`` and, when it differs from the
        engine's expected version, applies the real
        :func:`find_migration_path` chain (pure ruamel YAML edits — no
        subprocess). A config already at the expected version is a no-op. The
        pre-migrate ``setforge.yaml`` bytes are snapshotted so ``revert`` is a
        byte-exact inverse (INV-5).
        """
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
        """Undo the most recent verb by restoring its pre-state snapshot.

        No-ops on an empty stack (the real revert refuses cleanly with nothing
        to undo). Restores the real store legs via
        :func:`transitions.restore_state_snapshots` and rewrites the
        pre-verb ``setforge.yaml`` bytes, so ``revert ∘ install`` and
        ``revert ∘ migrate`` restore the byte-exact prior store (INV-3, INV-5).
        """
        if not self._transitions:
            return
        snap = self._transitions.pop()
        # Restore the active profile + config FIRST so the store legs (captured
        # under the snapshot's profile) and the derived live map are read back
        # under the same profile — a revert may cross a reconfigure profile
        # switch, and the restored config's profile chain must match.
        self.profile = snap.profile
        self.config = snap.config
        with profile_lock(self.profile):
            transitions.restore_state_snapshots(snap.store)
        # Derive live from the RESTORED store, not the captured snap.live: on a
        # real system the deployed files after a revert are exactly what the
        # restored base/local store reconstructs. Trusting snap.live instead can
        # leave a fid in live whose store leg the restore did not re-materialize
        # (e.g. a leg absent at capture time, restored to absent) — a model
        # shadow-drift, not a real no-silent-loss failure. Deriving keeps the
        # model's live ⊆ store-reconstructable by construction.
        self.live = self._live_from_store()
        if snap.config_bytes is not None:
            self._config_path().write_text(snap.config_bytes, encoding="utf-8")

    # -- observable state (the invariants read these) ------------------

    def store_index(self) -> dict[str, dict[str, str]]:
        """The current §9.3 classification index as a plain-dict view.

        Reads the REAL ``index/<profile>.json`` back through the store codec
        and projects each entry to ``{"present", "local_hash"}`` — the stable
        shape the invariants assert against (the raw ``FileEntry`` carries a
        hunk list this storage layer leaves empty).
        """
        index = read_index(self.profile)
        return {
            fid: {"present": str(entry.present), "local_hash": entry.local_hash or ""}
            for fid, entry in index.files.items()
        }

    def indexed_file_ids(self) -> set[str]:
        """Every file-id with a live index entry (INV-10 hook)."""
        return set(read_index(self.profile).files)

    def live_bodies(self) -> dict[str, str]:
        """The current live file bodies (a copy)."""
        return dict(self.live)

    def base_plus_local(self, file_id_str: str) -> bytes | None:
        """Reconstruct ``base + recorded-local`` for a file (INV-2 hook).

        Delegates to the real :func:`store.reconstruct` (identity over the
        recorded-local bytes in this storage layer). Returns the verbatim bytes,
        ``None`` when nothing is recorded, or ``b""`` for the ABSENT sentinel so
        the caller compares byte payloads without importing the store's Absent.
        """
        result = reconstruct(self.profile, file_id(file_id_str))
        if result is ABSENT or result is None:
            return None if result is None else b""
        return result

    def recorded_local(self, file_id_str: str) -> bytes | None | Absent:
        """The raw recorded-local trichotomy for INV-2 (bytes / ABSENT / None)."""
        return read_local(self.profile, file_id(file_id_str))

    def transition_count(self) -> int:
        """Number of revertible transitions currently on the stack."""
        return len(self._transitions)

    def schema_version(self) -> str:
        """The on-disk ``setforge.yaml`` schema version (advanced by migrate)."""
        return detect_current_schema(self._config_path())

    def verify_store(self) -> None:
        """Run the engine's own fail-closed INV-2/INV-10 store check.

        Raises :class:`setforge.errors.InvariantViolation` on an orphan
        classification or a local-byte hash mismatch — the real invariant the
        store owns, surfaced to the harness as an assertion.
        """
        verify(self.profile)

    # -- internals -----------------------------------------------------

    def _live_from_store(self) -> dict[str, str]:
        """Rebuild the live-body map from the store's reconstructable files.

        A file is "deployed" (live) iff the store reconstructs a present,
        non-absent body for it under the active profile — the byte-exact analog
        of what a real revert leaves on disk. This is the single source of truth
        that keeps ``live ⊆ store-reconstructable`` (INV-1) after a revert.
        """
        live: dict[str, str] = {}
        for fid_str in self.indexed_file_ids():
            body = reconstruct(self.profile, file_id(fid_str))
            if isinstance(body, bytes):
                live[fid_str] = body.decode("utf-8", errors="surrogateescape")
        return live

    def _config_path(self) -> Path:
        return self.root / "setforge.yaml"

    def _write_config_yaml(self) -> None:
        """Write the config to ``setforge.yaml`` at the ``1.0`` on-disk baseline.

        The config's own ``schema_version`` field is the migrate TARGET (what a
        later ``migrate`` advances the on-disk stamp toward); the file itself is
        stamped at the pre-versioning ``1.0`` baseline so a target above ``1.0``
        leaves a real, reachable chain for the migration registry to run.
        """
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
        """Capture every store leg for the active profile's known file-ids + index."""
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


# Reference the module's typed re-exports so a linter does not flag the imports
# that exist for the invariant machines' type annotations.
_TYPES: tuple[object, ...] = (FileId,)
