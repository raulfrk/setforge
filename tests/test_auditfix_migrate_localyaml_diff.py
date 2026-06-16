"""Regression test for the hidden ``local.yaml`` diff in ``migrate --apply``.

Audit finding (Important): the ``--apply`` confirm preview shadows only
``cfg_path`` faithfully; a migration that writes a ``roots.home``-derived
path (e.g. ``~/.config/setforge/local.yaml``) wrote into an unmirrored
tmp subtree, so the preview rendered "no diff" for that file even though
apply rewrites it — the user confirmed a destructive change against an
incomplete preview.

The fix mirrors the real directory layout in the shadow tree and derives
``shadow_roots`` by the same lexical transform, so every root-derived
path lands on the shadow copy the preview reads back. This test fails on
the old flat-``_shadow_name`` behavior and passes with the mirrored tree.
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
)

_LOCAL_YAML_RELPARTS = (".config", "setforge", "local.yaml")


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the transition log into the test's tmp dir.

    ``migrate --apply`` writes a real transition via ``transitions_root()`` →
    ``Path.home()``; pinning ``SETFORGE_STATE_DIR`` keeps the record in a
    per-test tmp tree independent of the autouse HOME-isolation fixture
    (belt-and-suspenders — the HOME fixture alone is a single point of failure).
    """
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))


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
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", chain)
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
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", chain)
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
