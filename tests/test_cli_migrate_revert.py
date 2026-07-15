"""End-to-end tests: a successful ``setforge migrate`` is revertible.

A migrate that mutates ``setforge.yaml`` (and any other affected file)
records a transition whose recorded patch, reversed by ``setforge
revert``, restores every mutated file — including the ``schema_version``
stamp — to its exact pre-migration bytes. The byte-restore is the sole
reverse authority: revert never re-runs the down-migration, so the
restored content matches the pre-migration bytes byte-for-byte (no
ruamel re-dump skew).
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import reconcile, transitions
from setforge.cli import app
from setforge.migrations import (
    ManifestEntry,
    ManifestType,
    MigrationRoots,
    detect_current_schema,
)
from setforge.reconcile import file_id
from setforge.reconcile.host_local_view import host_local_sections_from_store

runner = CliRunner()

_AT_1_0 = "version: 1\ntracked_files: {}\nprofiles:\n  default: {}\n"
_AT_1_1 = (
    'version: 1\nschema_version: "1.1"\ntracked_files: {}\nprofiles:\n  default: {}\n'
)


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the transition state dir to a per-test sandbox."""
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def _write_cfg(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


@dataclass(slots=True, frozen=True)
class _StampStep:
    """Forward migration that stamps ``schema_version`` into setforge.yaml."""

    from_version: str = "1.0"
    to_version: str = "1.1"

    @property
    def reverse(self) -> _StampStep:
        return _StampStep(from_version=self.to_version, to_version=self.from_version)

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (
            ManifestEntry(
                type=ManifestType.ADD,
                description="stamp schema_version",
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

        data = yaml_rt().load(roots.cfg_path.read_text())
        if self.to_version == "1.0":
            data.pop("schema_version", None)
        else:
            data["schema_version"] = self.to_version
        atomic_write_yaml(roots.cfg_path, data)


@dataclass(slots=True, frozen=True)
class _SidecarStep:
    """Forward migration that edits setforge.yaml AND a tracked sidecar file."""

    from_version: str = "1.0"
    to_version: str = "1.1"

    @property
    def reverse(self) -> _SidecarStep:
        return _SidecarStep(from_version=self.to_version, to_version=self.from_version)

    def _sidecar(self, roots: MigrationRoots) -> Path:
        return roots.repo_root / "tracked" / "note.md"

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="bump schema + sidecar",
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path, self._sidecar(roots))

    def apply(self, *, roots: MigrationRoots) -> None:
        from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

        data = yaml_rt().load(roots.cfg_path.read_text())
        data["schema_version"] = self.to_version
        atomic_write_yaml(roots.cfg_path, data)
        sidecar = self._sidecar(roots)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("migrated body\n", encoding="utf-8")


@dataclass(slots=True, frozen=True)
class _OwnTransitionStep:
    """A step that records its OWN transition — mimics the disposition cutover.

    Like ``DispositionRetireMigration`` it declares ``writes_own_transition`` and
    commits its transition inside ``apply``, so the migrate driver must NOT also
    write one. When the driver threads a pre-chain snapshot via
    ``roots.pre_chain_snapshot`` this step records the FULL reverse delta to the
    chain's origin; without it, only its own pre-step state (the bug).
    """

    from_version: str = "2.1"
    to_version: str = "3.0"
    writes_own_transition: bool = True

    @property
    def reverse(self) -> _OwnTransitionStep:
        return _OwnTransitionStep(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="own-transition stamp",
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
        )


def _latest_migrate() -> transitions.TransitionDir | None:
    return transitions.load_latest(
        "migrate", command=transitions.TransitionCommand.MIGRATE
    )


def test_frozen_1_0_migrate_through_own_transition_reverts_to_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_dir: Path,
) -> None:
    """A frozen-1.0 -> 3.0 chain that ends in a writes_own_transition step is
    revertible to the byte-exact frozen-1.0 origin in ONE ``revert`` (INV-5).

    Regression for the multi-step-through-cutover gap: the driver must thread its
    pre-chain frozen-1.0 snapshot into the own-transition step so the single
    recorded transition carries the full 1.0->3.0 reverse delta, not just the
    step's pre-step (2.1) state.
    """
    cfg = _write_cfg(tmp_path, _AT_1_0)
    pre_bytes = cfg.read_bytes()
    monkeypatch.setattr(
        "setforge.migrations.MIGRATIONS",
        (_StampStep(from_version="1.0", to_version="2.1"), _OwnTransitionStep()),
    )

    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "3.0", "--apply", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "3.0" in cfg.read_text()  # forward chain applied

    revert = runner.invoke(
        app, ["revert", "--profile=migrate", f"--config={cfg}", "--yes"]
    )
    assert revert.exit_code == 0, revert.output
    # ONE revert must reach the frozen-1.0 origin, not the intermediate 2.1.
    assert cfg.read_bytes() == pre_bytes, (
        f"expected byte-exact frozen-1.0 origin, got:\n{cfg.read_text()}"
    )


def test_migrate_apply_records_revertible_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_dir: Path,
) -> None:
    """A migrate --apply records a MIGRATE transition; revert restores bytes."""
    cfg = _write_cfg(tmp_path, _AT_1_0)
    pre_bytes = cfg.read_bytes()
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", (_StampStep(),))

    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "1.1", "--apply", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert cfg.read_text().count("schema_version") == 1  # forward applied

    recorded = _latest_migrate()
    assert recorded is not None, "no migrate transition was recorded"

    revert = runner.invoke(
        app, ["revert", "--profile=migrate", f"--config={cfg}", "--yes"]
    )
    assert revert.exit_code == 0, revert.output
    assert cfg.read_bytes() == pre_bytes


def test_migrate_downgrade_records_revertible_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_dir: Path,
) -> None:
    """A migrate --to=<older> downgrade is revertible to pre-downgrade bytes."""
    cfg = _write_cfg(tmp_path, _AT_1_1)
    pre_bytes = cfg.read_bytes()
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", (_StampStep(),))

    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "1.0", "--apply", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "schema_version" not in cfg.read_text()  # downgraded

    recorded = _latest_migrate()
    assert recorded is not None

    revert = runner.invoke(
        app, ["revert", "--profile=migrate", f"--config={cfg}", "--yes"]
    )
    assert revert.exit_code == 0, revert.output
    assert cfg.read_bytes() == pre_bytes


def test_migrate_revert_round_trip_is_byte_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_dir: Path,
) -> None:
    """revert(migrate(X)) == X byte-for-byte across setforge.yaml + sidecar.

    The sidecar did not exist pre-migration, so revert must delete it
    (restore to absent), proving the byte-restore covers creations too.
    """
    cfg = _write_cfg(tmp_path, _AT_1_0)
    pre_cfg = cfg.read_bytes()
    sidecar = tmp_path / "tracked" / "note.md"
    assert not sidecar.exists()
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", (_SidecarStep(),))

    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "1.1", "--apply", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert sidecar.exists()  # forward created the sidecar

    revert = runner.invoke(
        app, ["revert", "--profile=migrate", f"--config={cfg}", "--yes"]
    )
    assert revert.exit_code == 0, revert.output
    assert cfg.read_bytes() == pre_cfg
    assert not sidecar.exists(), "revert must remove the migration-created sidecar"


def test_migrate_transition_round_trips_through_metadata_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_dir: Path,
) -> None:
    """The MIGRATE enum member deserializes via load_meta / load_latest."""
    cfg = _write_cfg(tmp_path, _AT_1_0)
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", (_StampStep(),))

    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "1.1", "--apply", "--yes"]
    )
    assert result.exit_code == 0, result.output

    recorded = _latest_migrate()
    assert recorded is not None
    meta = transitions.load_meta(recorded)
    assert meta.command is transitions.TransitionCommand.MIGRATE
    assert meta.profile == "migrate"


# 2.1-origin: disposition-retire seeds the store base, span-surface-retire
# then folds the local.yaml section into that same entry (chained 2.1 -> 4.0).
_CHAIN_BASE = b"## Alpha\naaa\n## Beta\nbbb\n## Gamma\nccc\n"

_CHAIN_CFG_2_1 = """schema_version: "2.1"
tracked_files:
  notes:
    src: notes.md
    dst: ~/notes.md
profiles:
  default:
    tracked_files: [notes]
"""

_CHAIN_LOCAL_YAML = textwrap.dedent(
    """\
    tracked_files:
      notes:
        host_local_sections:
          my-tweaks:
            anchor:
              kind: after-heading
              value: Alpha
            body: "## My Tweaks\\nmy custom line\\n"
    """
)


def _write_chain_origin(tmp_path: Path) -> tuple[Path, Path]:
    """Lay down a 2.1-origin config + deployed live file + host-local local.yaml.

    Returns ``(cfg_path, local_yaml_path)``. ``Path.home()`` is the per-test
    isolated home (the conftest ``_isolate_home`` autouse fixture), the same root
    the reconcile store + local.yaml resolve under.
    """
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    cfg = repo / "setforge.yaml"
    cfg.write_text(_CHAIN_CFG_2_1, encoding="utf-8")
    (repo / "tracked" / "notes.md").write_bytes(_CHAIN_BASE)
    home = Path.home()
    (home / "notes.md").write_bytes(_CHAIN_BASE)
    local_yaml = home / ".config" / "setforge" / "local.yaml"
    local_yaml.parent.mkdir(parents=True, exist_ok=True)
    local_yaml.write_text(_CHAIN_LOCAL_YAML, encoding="utf-8")
    return cfg, local_yaml


def test_chained_2_1_to_4_0_apply_folds_and_stamps(
    tmp_path: Path, state_dir: Path
) -> None:
    """A single ``migrate --to 4.0`` from a 2.1 origin runs BOTH cutovers.

    The disposition-retire (2.1->3.0) and span-surface-retire (3.0->4.0) steps
    each set ``writes_own_transition``; the one command applies both, reaching 4.0
    with the local.yaml section folded as a LOCAL+reloc unit and the retired
    surface stripped. Because both steps own a transition, TWO durable MIGRATE
    transitions land on disk.
    """
    cfg, local_yaml = _write_chain_origin(tmp_path)

    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "4.0", "--apply", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert detect_current_schema(cfg) == "4.0"

    proj = host_local_sections_from_store("default", file_id("notes"))
    assert "notes" in proj
    assert "## My Tweaks" in {str(k) for k in proj["notes"]}
    assert "host_local_sections" not in local_yaml.read_text(encoding="utf-8")

    migrate_transitions = transitions.list_transitions(["migrate"])
    assert len(migrate_transitions) == 2


def test_chained_2_1_to_4_0_single_revert_restores_config_to_origin(
    tmp_path: Path, state_dir: Path
) -> None:
    """ONE ``revert`` after a chained 2.1->4.0 restores the config files exactly.

    The later cutover's transition carries the FULL pre-chain (2.1) image as its
    text patch, so a single ``revert`` returns BOTH ``setforge.yaml`` and
    ``local.yaml`` byte-exact to the 2.1 origin.

    Documented limitation of a two-owner chain: ``revert`` replays only the LATEST
    transition (the span-surface leg). Its state-snapshots capture the store AFTER
    the disposition seed, so the reconcile store returns to the 3.0-INTERMEDIATE
    shape, not the pristine pre-2.1 no-entry state — the span-surface FOLD is
    undone (local reverts to base, no host-local reloc unit survives) but the
    disposition-retire cutover's additive base seed remains. This residue is
    benign: it is exactly what a re-run of ``migrate --to 4.0`` would (idempotently)
    re-seed, and the retired 2.1 legacy-store readers no longer exist in the
    binary. The user-facing config files are the coherent guarantee; the store
    residue is asserted here so a regression in either direction is caught.
    """
    cfg, local_yaml = _write_chain_origin(tmp_path)
    cfg_origin = cfg.read_bytes()
    local_origin = local_yaml.read_bytes()

    apply = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "4.0", "--apply", "--yes"]
    )
    assert apply.exit_code == 0, apply.output

    revert = runner.invoke(
        app, ["revert", "--profile=migrate", f"--config={cfg}", "--yes"]
    )
    assert revert.exit_code == 0, revert.output

    assert cfg.read_bytes() == cfg_origin
    assert local_yaml.read_bytes() == local_origin

    fid = file_id("notes")
    assert host_local_sections_from_store("default", fid) == {}
    assert reconcile.read_index("default").files["notes"].hunks == []
    assert reconcile.read_local("default", fid) == _CHAIN_BASE
    assert reconcile.read_base("default", fid) == _CHAIN_BASE


def _write_chain_origin_no_host_local(tmp_path: Path) -> Path:
    """A 2.1-origin config + deployed live file but NO local.yaml host-local.

    The disposition-retire (2.1->3.0) step seeds the store and owns a transition,
    but the terminal span-surface-retire (3.0->4.0) step finds nothing to fold —
    exercising its no-fold branch. Returns ``cfg_path``.
    """
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    cfg = repo / "setforge.yaml"
    cfg.write_text(_CHAIN_CFG_2_1, encoding="utf-8")
    (repo / "tracked" / "notes.md").write_bytes(_CHAIN_BASE)
    (Path.home() / "notes.md").write_bytes(_CHAIN_BASE)
    return cfg


def test_chained_2_1_to_4_0_no_fold_single_revert_restores_config_to_origin(
    tmp_path: Path, state_dir: Path
) -> None:
    """ONE ``revert`` restores the config to origin even when nothing folds.

    Regression guard for INV-5 across a two-owner chain whose TERMINAL cutover
    no-ops. With no local.yaml host-local surface, the span-surface-retire
    (3.0->4.0) step folds nothing; it must STILL record a stamp transition
    threading the chain-origin config, or the driver — which skips its own text
    transition whenever a chain step owns one — leaves the 3.0->4.0 stamp outside
    every transition, and a single ``revert`` reverses the disposition step's
    (2.1-origin -> 3.0) patch against the on-disk 4.0 config and FAILS. The docker
    e2e (``test_migrate_2_1_to_3_0_strips_disposition_and_reverts`` /
    ``test_migrate_apply_is_revertible``) caught this; this is the unit guard.
    """
    cfg = _write_chain_origin_no_host_local(tmp_path)
    cfg_origin = cfg.read_bytes()

    apply = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "4.0", "--apply", "--yes"]
    )
    assert apply.exit_code == 0, apply.output
    assert detect_current_schema(cfg) == "4.0"

    revert = runner.invoke(
        app, ["revert", "--profile=migrate", f"--config={cfg}", "--yes"]
    )
    assert revert.exit_code == 0, revert.output
    assert cfg.read_bytes() == cfg_origin


def test_chained_2_1_to_4_0_second_revert_is_redo(
    tmp_path: Path, state_dir: Path
) -> None:
    """A second ``revert`` acts as REDO — re-advancing the config to 4.0."""
    cfg, _ = _write_chain_origin(tmp_path)
    runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "4.0", "--apply", "--yes"]
    )
    first = runner.invoke(
        app, ["revert", "--profile=migrate", f"--config={cfg}", "--yes"]
    )
    assert first.exit_code == 0, first.output
    assert detect_current_schema(cfg) == "2.1"

    second = runner.invoke(
        app, ["revert", "--profile=migrate", f"--config={cfg}", "--yes"]
    )
    assert second.exit_code == 0, second.output
    assert detect_current_schema(cfg) == "4.0"


def test_chained_2_1_to_5_0_single_revert_restores_config_and_store_to_origin(
    tmp_path: Path, state_dir: Path
) -> None:
    """ONE ``revert`` after a 2.1->5.0 FOLD chain reaches the TRUE origin.

    The chain runs THREE ``writes_own_transition`` cutovers — disposition-retire
    (2.1->3.0), span-surface-retire (3.0->4.0, folds the host-local section into
    the store), and the CHAIN-TERMINAL span-types-retire (4.0->5.0). A single
    ``revert`` replays only the LATEST (span-types) transition, and span-types
    threads the FULL pre-chain origin image as its text patch while carrying NO
    ``state_snapshots``. So — unlike the 4.0-terminal case, where span-surface's
    own state_snapshots override the text patch back to the 3.0-INTERMEDIATE store
    shape and a disposition seed residue survives — here the text patch is the
    SOLE reverse authority for every path: it reverses each reconcile-store leg
    from its post-fold bytes back to its absent-at-origin state, DELETING it. So
    one revert returns BOTH the config files AND the store byte-exact to the
    pristine 2.1 origin (no seed residue), the stronger INV-5 guarantee this
    terminal owner's full-leg text threading buys.
    """
    cfg, local_yaml = _write_chain_origin(tmp_path)
    cfg_origin = cfg.read_bytes()
    local_origin = local_yaml.read_bytes()

    apply = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "5.0", "--apply", "--yes"]
    )
    assert apply.exit_code == 0, apply.output
    assert detect_current_schema(cfg) == "5.0"
    # All three cutovers own a durable transition.
    assert len(transitions.list_transitions(["migrate"])) == 3

    revert = runner.invoke(
        app, ["revert", "--profile=migrate", f"--config={cfg}", "--yes"]
    )
    assert revert.exit_code == 0, revert.output

    assert cfg.read_bytes() == cfg_origin
    assert local_yaml.read_bytes() == local_origin

    # True origin: every reconcile-store leg is gone, not merely reverted to an
    # intermediate seeded shape (contrast the 4.0-terminal residue test above).
    fid = file_id("notes")
    assert host_local_sections_from_store("default", fid) == {}
    assert "notes" not in reconcile.read_index("default").files
    assert reconcile.read_local("default", fid) is None
    assert reconcile.read_base("default", fid) is None


def test_chained_2_1_to_5_0_no_fold_single_revert_restores_config_and_store(
    tmp_path: Path, state_dir: Path
) -> None:
    """ONE ``revert`` reaches the 2.1 origin for a 5.0 chain with nothing to fold.

    With no ``local.yaml`` host-local surface the span-surface-retire step folds
    nothing (its no-fold branch), but disposition-retire still seeds the reconcile
    store and the terminal span-types-retire still threads the full pre-chain
    image. A single ``revert`` of the terminal transition (``state_snapshots=()``)
    text-reverses the config stamp back to 2.1 AND deletes the disposition seed —
    reaching the true origin, config and store alike.
    """
    cfg = _write_chain_origin_no_host_local(tmp_path)
    cfg_origin = cfg.read_bytes()

    apply = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "5.0", "--apply", "--yes"]
    )
    assert apply.exit_code == 0, apply.output
    assert detect_current_schema(cfg) == "5.0"

    revert = runner.invoke(
        app, ["revert", "--profile=migrate", f"--config={cfg}", "--yes"]
    )
    assert revert.exit_code == 0, revert.output

    assert cfg.read_bytes() == cfg_origin
    fid = file_id("notes")
    assert "notes" not in reconcile.read_index("default").files
    assert reconcile.read_base("default", fid) is None


_CFG_AT_5_0_WITH_LEGACY = textwrap.dedent(
    """\
    version: 1
    schema_version: "5.0"
    minimum_version: "6.0"
    tracked_files: {}
    claude_plugins:
      superpowers:
        marketplace: mkt
    profiles:
      default:
        cargo_binaries:
          - ripgrep
        claude_plugins:
          - superpowers
        plugins_reconcile: strict
        extensions:
          include:
            - ms-python.python
          exclude:
            - GitHub.copilot
          reconcile: strict
    """
)


def test_migrate_to_6_then_one_revert_reaches_origin(
    tmp_path: Path, state_dir: Path
) -> None:
    """ONE ``revert`` after a 5.0->6.0 fold restores the config byte-exact.

    The profile-fields contraction folds ``cargo_binaries`` / ``claude_plugins``
    / ``plugins_reconcile`` / ``extensions`` into ``packages`` + ``reconcile``
    and drops the four legacy keys, stamping 6.0. As the chain-terminal cutover
    it threads the pre-migration origin image as its transition's text patch, so
    a single ``setforge revert --profile=migrate`` byte-restores the 5.0 origin
    — the reverse never re-runs the down-migration, so the restore is exact
    against the original bytes (no ruamel re-dump skew).
    """
    cfg = _write_cfg(tmp_path, _CFG_AT_5_0_WITH_LEGACY)
    cfg_origin = cfg.read_bytes()

    apply = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "6.0", "--apply", "--yes"]
    )
    assert apply.exit_code == 0, apply.output
    assert detect_current_schema(cfg) == "6.0"

    revert = runner.invoke(
        app, ["revert", "--profile=migrate", f"--config={cfg}", "--yes"]
    )
    assert revert.exit_code == 0, revert.output
    assert cfg.read_bytes() == cfg_origin


# 4.0 (not 2.1) is the earliest origin that still validates with the legacy
# profile fields present — earlier chain steps model_validate the intermediate
# config, and the modern Profile model forbids these fields pre-4.0.
_CHAIN_CFG_4_0_WITH_LEGACY = """schema_version: "4.0"
minimum_version: "6.0"
tracked_files: {}
claude_plugins:
  superpowers:
    marketplace: mkt
profiles:
  default:
    cargo_binaries: [ripgrep]
    claude_plugins: [superpowers]
    plugins_reconcile: strict
    extensions:
      include: [ms-python.python]
      exclude: [GitHub.copilot]
      reconcile: strict
"""


def test_chained_4_0_to_6_0_single_revert_restores_config_to_origin(
    tmp_path: Path, state_dir: Path
) -> None:
    """ONE ``revert`` after a 4.0->6.0 chain with TWO owners reaches the origin.

    This is the delicate INV-5 multi-owner property: the chain runs TWO cutovers
    that BOTH own a transition — span-types-retire (4.0->5.0) AND the
    chain-terminal profile-fields-retire (5.0->6.0). A single ``revert`` replays
    only the LATEST (profile-fields) transition, which threads the FULL pre-chain
    (4.0) image as its text patch. Byte-exact restoration of ``setforge.yaml``
    proves one revert still reaches origin even when an EARLIER owner also stamped
    a transition in the chain — the distinguishing case from the single-owner
    5.0-origin terminal-revert test above.
    """
    cfg = _write_cfg(tmp_path, _CHAIN_CFG_4_0_WITH_LEGACY)
    cfg_origin = cfg.read_bytes()

    apply = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "6.0", "--apply", "--yes"]
    )
    assert apply.exit_code == 0, apply.output
    assert detect_current_schema(cfg) == "6.0"
    assert len(transitions.list_transitions(["migrate"])) == 2

    revert = runner.invoke(
        app, ["revert", "--profile=migrate", f"--config={cfg}", "--yes"]
    )
    assert revert.exit_code == 0, revert.output
    assert cfg.read_bytes() == cfg_origin
