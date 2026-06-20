"""The thin reconcile seam the state machine drives (RFC 0001 §6, E1).

:class:`StubReconcileModel` mimics setforge's install / sync / revert /
migrate transitions over a tmp_path-isolated store, modeled on the RFC
§9.3 layout:

    <state_dir>/base/<profile>/<file-id>    last-installed upstream snapshot
    <state_dir>/local/<profile>/<file-id>   recorded keep-local content
    <state_dir>/index/<profile>.json        classification (local/shared/pending)
    <state_dir>/transitions/                snapshot stack for revert

Why a stub and not the real CLI? Per RFC §4 / §13, the real Epic-A
reconcile engine (the ``base/`` + ``local/`` + ``index/`` store and the
line-level 3-way merge) does NOT exist yet — task E1 (this harness) lands
BEFORE task E2 (the INV catalog) and the Epic-A engine. The stub gives
the state machine a runnable, meta-testable target TODAY whose verb
surface, store layout, and observable state match what the real engine
will expose, so swapping it in later is mechanical.

EXTENSION POINT (E2 / Epic-A): replace each ``_engine_*`` method body
with a call into the real reconcile engine. The public verb methods
(:meth:`install` / :meth:`sync` / :meth:`revert` / :meth:`migrate`), the
store accessors (:meth:`store_index` / :meth:`live_bodies` /
:meth:`base_plus_local`), and the transition stack stay as the stable
contract the invariants assert against.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from setforge.config import Config, resolve_profile

# The store subdirectories, named to match RFC §9.3 verbatim so the real
# engine and the stub agree on layout.
_BASE = "base"
_LOCAL = "local"
_INDEX = "index"
_TRANSITIONS = "transitions"


@dataclass(slots=True)
class _Snapshot:
    """One transition's pre-state, for revert.

    Holds the full live-body + store-index maps and the schema version
    captured immediately BEFORE a verb mutated them, plus the verb label.
    Revert pops the top snapshot and restores it — the stub analog of the
    real transition record's reverse-patch. Capturing ``schema_version``
    is what makes ``migrate`` reversible (INV-5): ``revert`` restores it
    alongside ``live`` + ``index``.
    """

    verb: str
    live: dict[str, str]
    index: dict[str, dict[str, str]]
    schema_version: str


@dataclass(slots=True)
class StubReconcileModel:
    """In-memory + on-disk model of the reconcile state.

    ``live`` maps file-id → the current live body (what is "deployed").
    ``index`` maps file-id → ``{"class": local|shared|pending}`` — the
    §9.3 classification. ``_transitions`` is the revert stack. The store
    is persisted to disk under :attr:`state_dir` so a future engine swap
    and the fs-isolation fixtures both have a real on-disk surface to
    assert against.
    """

    root: Path
    state_dir: Path
    profile: str = "default"
    config: Config | None = None
    live: dict[str, str] = field(default_factory=dict)
    index: dict[str, dict[str, str]] = field(default_factory=dict)
    _transitions: list[_Snapshot] = field(default_factory=list)
    _schema_version: str = "1.0"

    # -- construction --------------------------------------------------

    @classmethod
    def create(cls, root: Path) -> StubReconcileModel:
        """Build a model rooted at ``root`` with a fresh state dir.

        The signature ``(Path) -> model`` is the ``model_factory``
        contract :class:`tests.harness.invariants.InvariantStateMachine`
        expects, so a subclass wires it with
        ``model_factory = staticmethod(StubReconcileModel.create)``.
        """
        state_dir = root / "state"
        for sub in (_BASE, _LOCAL, _INDEX, _TRANSITIONS):
            (state_dir / sub).mkdir(parents=True, exist_ok=True)
        return cls(root=root, state_dir=state_dir)

    def set_config(self, config: Config) -> None:
        """Adopt a (generated) config and align the active profile.

        Picks the first profile in the config as the active one — the
        scaffold reconciles a single profile at a time, matching the CLI's
        ``--profile=`` contract. Does not itself reconcile; a following
        ``install`` verb does.
        """
        self.config = config
        self.profile = next(iter(config.profiles))

    # -- verbs (the @rule targets) -------------------------------------

    def install(self) -> None:
        """Deploy tracked → live and record the §9.3 store.

        EXTENSION POINT (E2 / A2): the real engine runs the line-level
        3-way merge here and fires the conflict wizard on a true
        conflict. The stub deploys verbatim (no merge) — enough to give
        the state machine a non-trivial, idempotent transition to drive.
        """
        self._snapshot("install")
        self._engine_install()
        self._persist()

    def sync(self) -> None:
        """Capture live edits back into the store classification.

        EXTENSION POINT (E2 / A5): the real engine captures newly-``share``d
        hunks into ``tracked/``. The stub re-classifies every known file as
        ``shared`` if it has a base, else leaves it ``pending``.
        """
        self._snapshot("sync")
        self._engine_sync()
        self._persist()

    def migrate(self) -> None:
        """Bump the schema version, snapshotting the pre-state.

        EXTENSION POINT (E2 / C-epic): the real engine runs the
        config-reconcile + package-key reshape mapping. The stub only
        toggles the recorded ``schema_version`` so ``revert`` has
        something reversible to undo (INV-5: migrate reversible).
        """
        self._snapshot("migrate")
        self._engine_migrate()
        self._persist()

    def revert(self) -> None:
        """Undo the most recent verb by restoring its pre-state snapshot.

        The stub silently no-ops where the real revert refuses cleanly
        (nothing to undo) when the transition stack is empty. EXTENSION
        POINT (E2 / A1+C4): the real engine reverses the on-disk patch +
        the extension/plugin deltas; the stub restores the captured maps
        and schema version (so ``revert ∘ migrate == identity``, INV-5).
        """
        if not self._transitions:
            return
        snap = self._transitions.pop()
        self.live = dict(snap.live)
        self.index = dict(snap.index)
        self._schema_version = snap.schema_version
        self._persist()

    # -- engine seams (replace bodies in E2) ---------------------------

    def _engine_install(self) -> None:
        if self.config is None:
            return
        resolved = resolve_profile(self.config, self.profile)
        for fid in resolved.tracked_files:
            tracked_file = self.config.tracked_files[fid]
            body = f"# tracked body for {fid}\nsrc={tracked_file.src}\n"
            self.live[fid] = body
            self._write_store(_BASE, fid, body)
            self.index.setdefault(fid, {"class": "pending"})

    def _engine_sync(self) -> None:
        for fid in list(self.index):
            base = self._read_store(_BASE, fid)
            if base is not None:
                self.index[fid] = {"class": "shared"}
                self._write_store(_LOCAL, fid, self.live.get(fid, ""))

    def _engine_migrate(self) -> None:
        self._schema_version = "2.0" if self._schema_version == "1.0" else "1.0"

    # -- observable state (the invariants read these) ------------------

    def store_index(self) -> dict[str, dict[str, str]]:
        """The current §9.3 classification index (a copy)."""
        return {fid: dict(entry) for fid, entry in self.index.items()}

    def live_bodies(self) -> dict[str, str]:
        """The current live file bodies (a copy)."""
        return dict(self.live)

    def base_plus_local(self, file_id: str) -> str | None:
        """Reconstruct ``base + recorded-local`` for a file (INV-2 hook).

        The stub returns the recorded local body when present, else the
        base — the trivial reconstruction. A real INV-2 compares this
        against the live body. Returns ``None`` for an unknown file.
        """
        local = self._read_store(_LOCAL, file_id)
        if local is not None:
            return local
        return self._read_store(_BASE, file_id)

    def transition_count(self) -> int:
        """Number of revertible transitions currently on the stack."""
        return len(self._transitions)

    def schema_version(self) -> str:
        """The recorded schema version (toggled by :meth:`migrate`)."""
        return self._schema_version

    # -- internals -----------------------------------------------------

    def _snapshot(self, verb: str) -> None:
        self._transitions.append(
            _Snapshot(
                verb=verb,
                live=dict(self.live),
                index={fid: dict(entry) for fid, entry in self.index.items()},
                schema_version=self._schema_version,
            )
        )

    def _store_path(self, sub: str, file_id: str) -> Path:
        if sub == _INDEX:
            return self.state_dir / _INDEX / f"{self.profile}.json"
        return self.state_dir / sub / self.profile / file_id

    def _write_store(self, sub: str, file_id: str, body: str) -> None:
        path = self._store_path(sub, file_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def _read_store(self, sub: str, file_id: str) -> str | None:
        path = self._store_path(sub, file_id)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def _persist(self) -> None:
        """Flush the index to ``index/<profile>.json`` (on-disk truth)."""
        index_path = self.state_dir / _INDEX / f"{self.profile}.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(self.index, indent=2), encoding="utf-8")
