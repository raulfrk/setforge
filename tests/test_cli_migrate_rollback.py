"""Rollback-completeness tests for ``setforge migrate --apply``.

Three gaps closed here:

* A ``KeyboardInterrupt`` raised mid-``apply()`` must trigger the same
  file rollback the ``Exception`` branch does AND re-raise the interrupt
  (never swallowed into a ``typer.Exit(1)`` — a user-cancel is not a
  migration error).
* A mid-chain failure AFTER a ``writes_own_transition`` cutover must leave
  the reconcile store + transition log consistent with the rolled-back
  files: no phantom/superseding cutover transition, store legs restored.
* The store snapshot must be captured at chain-START (before any cutover
  mutates) so a rollback never clobbers a legitimate prior install/sync
  store leg.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import transitions
from setforge.cli import app
from setforge.migrations import ManifestEntry, ManifestType, MigrationRoots

runner = CliRunner()

_AT_1_0 = "version: 1\ntracked_files: {}\nprofiles:\n  default: {}\n"


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the state dir (store + transition log) into the test tmp tree."""
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def _write_cfg(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


@dataclass(slots=True, frozen=True)
class _StampStep:
    """Forward step that stamps schema_version."""

    from_version: str = "1.0"
    to_version: str = "1.1"

    @property
    def reverse(self) -> _StampStep:
        return _StampStep(from_version=self.to_version, to_version=self.from_version)

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (
            ManifestEntry(
                type=ManifestType.ADD, description="stamp", affected_path=roots.cfg_path
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

        data = yaml_rt().load(roots.cfg_path.read_text())
        data["schema_version"] = self.to_version
        atomic_write_yaml(roots.cfg_path, data)


@dataclass(slots=True, frozen=True)
class _InterruptStep:
    """Second step that raises ``KeyboardInterrupt`` inside ``apply``."""

    from_version: str = "1.1"
    to_version: str = "1.2"

    @property
    def reverse(self) -> _InterruptStep:
        return _InterruptStep(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (ManifestEntry(type=ManifestType.NOTE, description="interrupt"),)

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        # Raise only during the REAL apply, not the shadow-tree preview pass
        # (which runs the chain to render the diff). The driver threads
        # pre_chain_snapshot only into the real apply; the preview's shadow
        # roots leave it None. Without this guard the interrupt fires in
        # preview and never reaches _execute_chain's loop.
        if roots.pre_chain_snapshot is None:
            return
        raise KeyboardInterrupt


@dataclass(slots=True, frozen=True)
class _StoreCutoverStep:
    """Cutover step: mutates a reconcile store leg AND commits its OWN durable
    transition inside ``apply`` (mimics DispositionRetire / SpanSurfaceRetire).

    Declares ``writes_own_transition`` so the driver treats it as a store
    cutover: captures the pre-chain store snapshot and skips its own text-only
    transition.
    """

    from_version: str = "1.1"
    to_version: str = "2.0"
    profile: str = "default"
    key: str = "cutover-key"
    writes_own_transition: bool = True

    @property
    def reverse(self) -> _StoreCutoverStep:
        return _StoreCutoverStep(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="store cutover",
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

        cfg_pre = roots.cfg_path.read_text(encoding="utf-8")
        data = yaml_rt().load(cfg_pre)
        data["schema_version"] = self.to_version
        atomic_write_yaml(roots.cfg_path, data)

        # Only mutate the GLOBAL store + commit the durable transition during
        # the REAL apply (pre_chain_snapshot set), never the shadow preview —
        # the preview redirects state_root elsewhere, but keeping the store
        # write real-only mirrors the actual cutovers' preview posture.
        if roots.pre_chain_snapshot is None:
            return

        # Mutate the GLOBAL reconcile store — a leg keyed off state_root(),
        # NOT in the driver's file snapshot set.
        target = transitions._snapshot_target(
            transitions.SnapshotStore.LOCAL_CONTENT, self.profile, self.key
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"CUTOVER-MUTATED\n")

        # Commit the cutover's OWN durable transition, carrying its store leg
        # as state_snapshots (mirrors the real cutovers).
        pre = roots.pre_chain_snapshot
        file_pre: dict[Path, str | None] = (
            dict(pre) if pre is not None else {roots.cfg_path: cfg_pre}
        )
        file_post = transitions.snapshot_paths(tuple(file_pre))
        transitions.write_transition(
            transitions.make_meta(
                transitions.TransitionCommand.MIGRATE,
                transitions.MIGRATE_TRANSITION_PROFILE,
                end_timestamp=transitions.now_utc().isoformat(),
                command_line=None,
            ),
            file_pre,
            file_post,
            None,
            state_snapshots=(
                transitions.snapshot_store_state(
                    transitions.SnapshotStore.LOCAL_CONTENT, self.profile, self.key
                ),
            ),
        )


@dataclass(slots=True, frozen=True)
class _RaisingStep:
    """Terminal step that raises AFTER a preceding cutover has committed."""

    from_version: str = "2.0"
    to_version: str = "2.1"

    @property
    def reverse(self) -> _RaisingStep:
        return _RaisingStep(from_version=self.to_version, to_version=self.from_version)

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (ManifestEntry(type=ManifestType.NOTE, description="boom"),)

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        # Fail only during the real apply (see _InterruptStep) so the preview
        # pass renders without aborting.
        if roots.pre_chain_snapshot is None:
            return
        raise RuntimeError("terminal step deliberately fails")


def test_keyboard_interrupt_mid_chain_rolls_back_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Ctrl-C mid-apply rolls the file back AND propagates the interrupt.

    The interrupt must NOT be swallowed into a ``typer.Exit(1)`` (user-cancel
    is distinct from a migration failure).
    """
    cfg = _write_cfg(tmp_path, _AT_1_0)
    original = cfg.read_text()
    monkeypatch.setattr(
        "setforge.migrations.MIGRATIONS", (_StampStep(), _InterruptStep())
    )
    result = runner.invoke(
        app,
        ["migrate", "--config", str(cfg), "--to", "1.2", "--apply", "--yes"],
        catch_exceptions=True,
    )
    # File rolled back to pre-migration bytes despite the interrupt — the loop
    # must call _rollback on KeyboardInterrupt, not leave it half-applied.
    assert cfg.read_text() == original, (
        f"file left half-applied after interrupt: {cfg.read_text()!r}"
    )
    # The interrupt is NOT misreported as a migration error (typer.Exit(1)):
    # Click converts a propagated KeyboardInterrupt to exit 130, distinct from
    # the Exception branch's exit 1.
    assert result.exit_code != 1, (
        f"user-cancel misreported as migration error: exit_code={result.exit_code}"
    )
    assert result.exit_code == 130, f"expected SIGINT exit 130, got {result.exit_code}"


def test_store_cutover_then_failure_leaves_store_and_log_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure AFTER a store cutover restores the store legs to pre-chain state
    and leaves NO phantom/superseding cutover transition behind."""
    cfg = _write_cfg(tmp_path, _AT_1_0)
    original = cfg.read_text()
    monkeypatch.setattr(
        "setforge.migrations.MIGRATIONS",
        (_StampStep(), _StoreCutoverStep(), _RaisingStep()),
    )
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "2.1", "--apply", "--yes"]
    )
    assert result.exit_code == 1, result.output
    assert "rolled back" in result.output

    # File rolled back.
    assert cfg.read_text() == original

    # Store leg restored to its pre-chain (absent) state — the cutover's
    # b"CUTOVER-MUTATED" write is gone.
    leg = transitions._snapshot_target(
        transitions.SnapshotStore.LOCAL_CONTENT, "default", "cutover-key"
    )
    assert not leg.exists(), (
        f"cutover store leg should be gone after rollback, but exists: "
        f"{leg.read_bytes()!r}"
    )

    # No phantom migrate transition remains — the cutover committed one, but
    # rollback must remove it so a later `revert` does not reverse it against
    # an already-rolled-back tree.
    assert (
        transitions.load_latest(
            transitions.MIGRATE_TRANSITION_PROFILE,
            command=transitions.TransitionCommand.MIGRATE,
        )
        is None
    ), "phantom cutover transition survived rollback"


@dataclass(slots=True, frozen=True)
class _ConcurrentInstallStep:
    """Simulates a concurrent ``install`` for ANOTHER profile landing a
    transition record mid-migrate — AFTER the driver's chain-start snapshot,
    so it is absent from that snapshot and a candidate for the rollback sweep.

    A step (not a pre-seed) because a pre-seeded record IS in the chain-start
    snapshot and would survive even an unscoped sweep; the cross-profile bug
    only bites records created after the snapshot.
    """

    from_version: str = "2.0"
    to_version: str = "2.1"
    installed_dir_holder: list[Path] | None = None

    @property
    def reverse(self) -> _ConcurrentInstallStep:
        return _ConcurrentInstallStep(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (ManifestEntry(type=ManifestType.NOTE, description="concurrent"),)

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        if roots.pre_chain_snapshot is None:
            return
        sentinel = roots.cfg_path.parent / "installed-file.txt"
        tx = transitions.write_transition(
            transitions.make_meta(
                transitions.TransitionCommand.INSTALL,
                "some-other-profile",
                end_timestamp=transitions.now_utc().isoformat(),
                command_line=None,
            ),
            {sentinel: None},
            {sentinel: "installed\n"},
            None,
        )
        if self.installed_dir_holder is not None:
            self.installed_dir_holder.append(Path(tx))


def test_rollback_sweep_spares_other_profiles_transition_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent non-migrate transition record (e.g. an install for another
    profile) landing MID-migrate must SURVIVE the migrate rollback sweep.

    state_root() is host-wide/all-profile. The whole-tree store-leg sweep is
    accepted best-effort, but the transitions/ deletion is scoped to the
    MIGRATE profile — a concurrent install/sync's own-profile record is left
    untouched, while the phantom migrate cutover transition is still removed."""
    cfg = _write_cfg(tmp_path, _AT_1_0)
    holder: list[Path] = []
    monkeypatch.setattr(
        "setforge.migrations.MIGRATIONS",
        (
            _StampStep(),
            _StoreCutoverStep(),
            _ConcurrentInstallStep(installed_dir_holder=holder),
            _RaisingStep(from_version="2.1", to_version="2.2"),
        ),
    )
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "2.2", "--apply", "--yes"]
    )
    assert result.exit_code == 1, result.output

    # The concurrent install landed a record mid-migrate (after chain-start).
    assert holder, "concurrent install step did not run in the real apply"
    other_dir = holder[0]

    # It is untouched by the sweep — scoping spares non-migrate profiles.
    assert other_dir.exists(), (
        "concurrent non-migrate transition record was deleted by migrate rollback"
    )
    assert (other_dir / "meta.json").exists()

    # But the phantom migrate transition IS still removed (scoping preserved
    # phantom removal).
    assert (
        transitions.load_latest(
            transitions.MIGRATE_TRANSITION_PROFILE,
            command=transitions.TransitionCommand.MIGRATE,
        )
        is None
    ), "phantom cutover transition survived the scoped rollback"


def test_store_snapshot_captured_at_chain_start_preserves_prior_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legitimate PRE-EXISTING store leg (from a prior install/sync) must be
    restored byte-exact after a rolled-back migrate.

    The prior content lives at the SAME key the cutover overwrites. Correct
    chain-start capture restores the PRIOR bytes; a snapshot taken too late
    (after the cutover mutated the leg) would restore the cutover's bytes —
    clobbering the legitimate prior install/sync state. This discriminates
    chain-start capture from mid-chain capture."""
    cfg = _write_cfg(tmp_path, _AT_1_0)

    # Seed a legitimate prior store leg at the cutover's OWN key — the cutover
    # will overwrite it with b"CUTOVER-MUTATED\n" during apply.
    leg = transitions._snapshot_target(
        transitions.SnapshotStore.LOCAL_CONTENT, "default", "cutover-key"
    )
    leg.parent.mkdir(parents=True, exist_ok=True)
    leg.write_bytes(b"PRIOR-INSTALL-CONTENT\n")

    monkeypatch.setattr(
        "setforge.migrations.MIGRATIONS",
        (_StampStep(), _StoreCutoverStep(), _RaisingStep()),
    )
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "2.1", "--apply", "--yes"]
    )
    assert result.exit_code == 1, result.output

    # The prior leg is restored byte-exact — rollback used the CHAIN-START
    # snapshot, not the post-cutover bytes.
    assert leg.exists(), "legitimate prior store leg was deleted by rollback"
    assert leg.read_bytes() == b"PRIOR-INSTALL-CONTENT\n", (
        f"rollback restored wrong bytes (late snapshot?): {leg.read_bytes()!r}"
    )
