"""CLI tests for ``setforge migrate --to`` + migration write-safety.

Covers the ``--to=<version>`` target modifier
(up/down), its guards (mutex with ``--pin``, unknown-target rejection,
already-at no-op), and the partial-chain rollback / backup no-clobber
write-safety behaviors.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.migrations import (
    ManifestEntry,
    ManifestType,
    MigrationRoots,
    detect_current_schema,
)
from tests.shared_helpers import write_setforge_yaml

runner = CliRunner()

_AT_1_1 = (
    'version: 1\nschema_version: "1.1"\ntracked_files: {}\nprofiles:\n  default: {}\n'
)
_AT_1_0 = "version: 1\ntracked_files: {}\nprofiles:\n  default: {}\n"


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the transition log into the test's tmp dir.

    ``migrate --apply`` writes a real transition via ``transitions_root()`` →
    ``Path.home()``; pinning ``SETFORGE_STATE_DIR`` keeps the record in a
    per-test tmp tree independent of the autouse HOME-isolation fixture
    (belt-and-suspenders — the HOME fixture alone is a single point of failure).
    """
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))


# ---------------------------------------------------------------------------
# --to guards
# ---------------------------------------------------------------------------


def test_to_equals_current_is_noop(tmp_path: Path) -> None:
    cfg = write_setforge_yaml(tmp_path, _AT_1_0)  # absent schema_version -> 1.0
    result = runner.invoke(app, ["migrate", "--config", str(cfg), "--to", "1.0"])
    assert result.exit_code == 0
    assert "already at schema_version 1.0" in result.stdout
    # no write — schema_version still absent
    assert detect_current_schema(cfg) == "1.0"


def test_to_unknown_version_rejected(tmp_path: Path) -> None:
    cfg = write_setforge_yaml(tmp_path, _AT_1_1)
    result = runner.invoke(app, ["migrate", "--config", str(cfg), "--to", "9.9"])
    assert result.exit_code != 0
    assert "unknown schema version" in result.output


def test_pin_rejects_non_mapping_root(tmp_path: Path) -> None:
    """A list/scalar-root config yields a clean CLI error, not a TypeError."""
    cfg = write_setforge_yaml(tmp_path, "- just\n- a\n- list\n")
    result = runner.invoke(app, ["migrate", "--config", str(cfg), "--pin", "1.1"])
    assert result.exit_code != 0
    assert "root must be a mapping" in result.output


def test_to_and_pin_mutually_exclusive(tmp_path: Path) -> None:
    cfg = write_setforge_yaml(tmp_path, _AT_1_1)
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--pin", "1.0", "--to", "1.0"]
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
    # the pin must NOT have been written despite being parsed first
    assert detect_current_schema(cfg) == "1.1"


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def test_to_downgrade_check_previews_reverse(tmp_path: Path) -> None:
    cfg = write_setforge_yaml(tmp_path, _AT_1_1)
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "1.0", "--check"]
    )
    assert result.exit_code == 0
    assert "1.1 → 1.0" in result.stdout
    # check never writes
    assert detect_current_schema(cfg) == "1.1"


def test_to_downgrade_apply_yes_strips_stamp(tmp_path: Path) -> None:
    cfg = write_setforge_yaml(tmp_path, _AT_1_1)
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "1.0", "--apply", "--yes"]
    )
    assert result.exit_code == 0
    # down-converted: schema_version stamp removed -> detect defaults to 1.0
    assert detect_current_schema(cfg) == "1.0"


def test_downgrade_non_tty_without_yes_requires_interactive(tmp_path: Path) -> None:
    cfg = write_setforge_yaml(tmp_path, _AT_1_1)
    # CliRunner stdin is not a TTY; no --yes -> ConfirmRequiresInteractive
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "1.0", "--apply"]
    )
    assert result.exit_code != 0
    assert detect_current_schema(cfg) == "1.1"  # unchanged


# ---------------------------------------------------------------------------
# partial-chain rollback
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _StampStep:
    """Forward step that stamps schema_version (1.0->1.1)."""

    from_version: str = "1.0"
    to_version: str = "1.1"

    @property
    def reverse(self) -> _StampStep:
        return _StampStep(from_version=self.to_version, to_version=self.from_version)

    def manifest(self, *, roots: MigrationRoots) -> tuple:
        from setforge.migrations import ManifestEntry, ManifestType

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
class _RaisingStep:
    """Second step that always raises — to exercise mid-chain rollback."""

    from_version: str = "1.1"
    to_version: str = "1.2"

    @property
    def reverse(self) -> _RaisingStep:
        return _RaisingStep(from_version=self.to_version, to_version=self.from_version)

    def manifest(self, *, roots: MigrationRoots) -> tuple:
        from setforge.migrations import ManifestEntry, ManifestType

        return (ManifestEntry(type=ManifestType.NOTE, description="boom"),)

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        raise RuntimeError("step 2 deliberately fails")


def test_partial_chain_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 2 raising must roll the file back to its pre-migration bytes."""
    cfg = write_setforge_yaml(tmp_path, _AT_1_0)
    original = cfg.read_text()
    monkeypatch.setattr(
        "setforge.migrations.registry.MIGRATIONS", (_StampStep(), _RaisingStep())
    )
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "1.2", "--apply", "--yes"]
    )
    assert result.exit_code == 1
    assert "rolled back" in result.output
    # file restored to original bytes — NOT left half-migrated at 1.1
    assert cfg.read_text() == original
    assert detect_current_schema(cfg) == "1.0"


# Regression test for the hidden ``local.yaml`` diff in ``migrate --apply``.
#
# Audit finding (Important): the ``--apply`` confirm preview shadows only
# ``cfg_path`` faithfully; a migration that writes a ``roots.home``-derived
# path (e.g. ``~/.config/setforge/local.yaml``) wrote into an unmirrored
# tmp subtree, so the preview rendered "no diff" for that file even though
# apply rewrites it — the user confirmed a destructive change against an
# incomplete preview.
#
# The fix mirrors the real directory layout in the shadow tree and derives
# ``shadow_roots`` by the same lexical transform, so every root-derived
# path lands on the shadow copy the preview reads back. This test fails on
# the old flat-``_shadow_name`` behavior and passes with the mirrored tree.

_LOCAL_YAML_RELPARTS = (".config", "setforge", "local.yaml")


def _local_yaml(roots: MigrationRoots) -> Path:
    path = roots.home
    for part in _LOCAL_YAML_RELPARTS:
        path = path / part
    return path


@dataclass(slots=True, frozen=True)
class _TwoFileMigration:
    """Fake migration writing BOTH cfg_path AND a home-derived local.yaml.

    Mirrors :class:`Contract20Migration`'s two-file footprint: it touches
    ``roots.cfg_path`` (always shadowed correctly) and a path derived from
    ``roots.home`` (the one the old flat shadow tree dropped).
    """

    from_version: str = "1.0"
    to_version: str = "1.1"

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="rewrite local.yaml overlay",
                affected_path=_local_yaml(roots),
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path, _local_yaml(roots))

    def apply(self, *, roots: MigrationRoots) -> None:
        # cfg_path edit: flip a marker so cfg.yaml also shows a diff.
        cfg = roots.cfg_path
        cfg.write_text(
            cfg.read_text(encoding="utf-8").replace("version: 1\n", "version: 2\n"),
            encoding="utf-8",
        )
        # home-derived edit: contract the legacy overlay.
        local = _local_yaml(roots)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text("preserve_user_keys: []\n", encoding="utf-8")


def _write_minimal_setforge_yaml(path: Path) -> None:
    path.write_text(
        "version: 1\ntracked_files: {}\nprofiles: {p: {}}\n", encoding="utf-8"
    )


def test_invalid_utf8_local_input_fails_before_migration_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from setforge.cli import main

    cfg = tmp_path / "setforge.yaml"
    _write_minimal_setforge_yaml(cfg)
    home = tmp_path / "home"
    local = home.joinpath(*_LOCAL_YAML_RELPARTS)
    local.parent.mkdir(parents=True)
    local.write_bytes(b"broken: \xff\n")
    before = cfg.read_bytes()
    monkeypatch.setattr("setforge.cli.migrate.Path.home", staticmethod(lambda: home))
    monkeypatch.setattr(
        "setforge.migrations.registry.MIGRATIONS", (_TwoFileMigration(),)
    )
    monkeypatch.setattr("setforge.migrations.current_expected_schema_version", "1.1")
    monkeypatch.setattr("setforge.cli.migrate.current_expected_schema_version", "1.1")
    monkeypatch.setattr("setforge.cli.migrate.shutil.which", lambda _: None)
    monkeypatch.setattr(
        "sys.argv", ["setforge", "migrate", "--apply", "--yes", f"--config={cfg}"]
    )
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    output = capsys.readouterr().err
    assert str(local) in output
    assert "UTF-8" in output
    assert "Traceback" not in output
    assert cfg.read_bytes() == before
    assert local.read_bytes() == b"broken: \xff\n"


def test_apply_preview_shows_diff_for_home_derived_local_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``--apply`` preview must include a diff hunk for local.yaml.

    On the pre-fix flat shadow tree the home-derived write landed under an
    unmirrored ``tmp/home/...`` subtree the preview never read back, so the
    rendered diff omitted local.yaml entirely. With the mirrored shadow
    tree the preview surfaces the contraction before the confirm.
    """
    cfg = tmp_path / "repo" / "setforge.yaml"
    cfg.parent.mkdir(parents=True)
    _write_minimal_setforge_yaml(cfg)

    home = tmp_path / "home"
    local = home
    for part in _LOCAL_YAML_RELPARTS:
        local = local / part
    local.parent.mkdir(parents=True)
    local.write_text(
        "preserve_user_keys:\n  - tracked_files.x.preserve_user_keys.add\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("setforge.cli.migrate.Path.home", staticmethod(lambda: home))

    chain = (_TwoFileMigration(),)
    monkeypatch.setattr("setforge.migrations.registry.MIGRATIONS", chain)
    monkeypatch.setattr("setforge.migrations.current_expected_schema_version", "1.1")
    monkeypatch.setattr("setforge.cli.migrate.current_expected_schema_version", "1.1")
    monkeypatch.setattr("setforge.cli.migrate.shutil.which", lambda _: None)

    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--apply", "--yes", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "preview of changes" in result.output

    out = result.output
    # The preview must name the local.yaml path AND show the contraction.
    assert str(local) in out, out
    # The removed overlay line must appear as a deletion in the diff.
    assert "-  - tracked_files.x.preserve_user_keys.add" in out, out
    # And the post-migration body must appear as an addition.
    assert "+preserve_user_keys: []" in out, out

    # Sanity: apply actually rewrote the real local.yaml.
    assert local.read_text(encoding="utf-8") == "preserve_user_keys: []\n"


def test_apply_preview_still_shows_cfg_path_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirrored shadow tree keeps the cfg_path diff working too.

    Guards against the mirroring change regressing the path that already
    worked (cfg_path), since it now flows through the same transform.
    """
    cfg = tmp_path / "repo" / "setforge.yaml"
    cfg.parent.mkdir(parents=True)
    _write_minimal_setforge_yaml(cfg)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("setforge.cli.migrate.Path.home", staticmethod(lambda: home))

    chain = (_TwoFileMigration(),)
    monkeypatch.setattr("setforge.migrations.registry.MIGRATIONS", chain)
    monkeypatch.setattr("setforge.migrations.current_expected_schema_version", "1.1")
    monkeypatch.setattr("setforge.cli.migrate.current_expected_schema_version", "1.1")
    monkeypatch.setattr("setforge.cli.migrate.shutil.which", lambda _: None)

    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--apply", "--yes", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert str(cfg) in out, out
    assert "-version: 1" in out, out
    assert "+version: 2" in out, out
