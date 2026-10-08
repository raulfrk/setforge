"""Section-template seeding into the reconcile store (STAGE B).

On a fresh host an install seeds each ``section_slots`` template as a LOCAL
reconcile-store unit — NOT a ``local.yaml`` ``host_local_sections`` block.
The unit is host-local (survives re-baselining) and deploys through the
normal reconcile path; the seed-once gate reads the STORE, so a re-install
whose heading is already a LOCAL unit does not reseed.

These tests pin that behavior end-to-end through the ``install`` CLI:

- fresh install records a LOCAL store unit carrying the template body and
  writes NOTHING to ``local.yaml``;
- a second install does not reseed (gate reads the store) and deploys the
  seeded host-local body into the live file;
- a seeded section the user deleted from the live file stays deleted, also when
  the same install advances the merge base (the gate reads the store row alone);
- a seeded section the user edited keeps its edit and is not seeded again.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app
from setforge.reconcile.host_local_view import host_local_headings_from_store
from setforge.reconcile.types import file_id

_PROFILE = "seed-test"

_DOC = """\
# Title

## Notes

upstream notes body
"""

_TEMPLATE_BODY = "## Python conventions\n\nSEEDED PYTHON CONVENTIONS\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    (target / "tracked").mkdir(parents=True)
    (target / "tracked" / "doc.md").write_text(_DOC, encoding="utf-8")
    (target / "templates").mkdir(parents=True)
    (target / "templates" / "py-conv.md").write_text(_TEMPLATE_BODY, encoding="utf-8")
    return target


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.setforge_seed/doc.md\n"
        "section_templates:\n"
        "  py-conv:\n"
        "    src: py-conv.md\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - doc\n"
        "    section_slots:\n"
        "      python-conventions: py-conv\n",
        encoding="utf-8",
    )
    return config


def _invoke(config: Path) -> Result:
    return CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--no-git-check",
            "--yes",
            "--no-transition",
        ],
    )


def _dst(repo_home_root: Path) -> Path:
    return repo_home_root / "home" / ".setforge_seed" / "doc.md"


def _has_active_host_local_sections(local_yaml: Path) -> bool:
    """True when local.yaml carries a live (non-comment) host_local_sections key.

    ``install`` scaffolds a local.yaml stub whose commented examples mention
    ``# host_local_sections:``; only an UNcommented key is a real write.
    """
    if not local_yaml.exists():
        return False
    return any(
        "host_local_sections" in line and not line.lstrip().startswith("#")
        for line in local_yaml.read_text(encoding="utf-8").splitlines()
    )


def test_fresh_install_seeds_local_store_unit_not_local_yaml(
    repo: Path, tmp_path: Path
) -> None:
    """A fresh install records the template as a LOCAL store unit and writes
    NOTHING to local.yaml."""
    config = _write_config(repo)
    # Must be $HOME (the fixture's monkeypatched real scaffold root), not
    # tmp_path, or the no-write assert below would pass vacuously.
    local_yaml = tmp_path / "home" / ".config" / "setforge" / "local.yaml"

    result = _invoke(config)
    assert result.exit_code == 0, result.output
    assert "seeded host-local section template(s): python-conventions" in result.output

    assert host_local_headings_from_store(_PROFILE, file_id("doc")) == {
        "## Python conventions"
    }

    deployed = _dst(tmp_path).read_text(encoding="utf-8")
    assert "SEEDED PYTHON CONVENTIONS" in deployed

    assert not _has_active_host_local_sections(local_yaml)


def test_fresh_seed_participates_and_capture_keeps_body_host_local(
    repo: Path, tmp_path: Path
) -> None:
    """A fresh LOCAL seed opts into staged capture before any prior index exists."""
    from setforge.config import load_config
    from setforge.reconcile import store
    from tests.verb_calls import capture_profile

    config_path = _write_config(repo)
    result = _invoke(config_path)
    assert result.exit_code == 0, result.output

    entry = store.read_index(_PROFILE).files["doc"]
    assert entry.staged is True
    assert any(row["cls"] == "local" for row in entry.hunks)

    capture_profile(
        load_config(config_path),
        _PROFILE,
        repo,
    )

    tracked = (repo / "tracked" / "doc.md").read_text(encoding="utf-8")
    assert "SEEDED PYTHON CONVENTIONS" not in tracked
    assert store.read_index(_PROFILE).files["doc"].staged is True


def test_second_install_does_not_reseed_and_deploys_host_local(
    repo: Path, tmp_path: Path
) -> None:
    """The seed-once gate reads the store: a second install neither reseeds nor
    duplicates, and the seeded host-local body deploys into the live file."""
    config = _write_config(repo)

    first = _invoke(config)
    assert first.exit_code == 0, first.output
    assert "seeded host-local section template(s)" in first.output

    second = _invoke(config)
    assert second.exit_code == 0, second.output
    assert "seeded host-local section template(s)" not in second.output
    assert host_local_headings_from_store(_PROFILE, file_id("doc")) == {
        "## Python conventions"
    }

    deployed = _dst(tmp_path).read_text(encoding="utf-8")
    assert "SEEDED PYTHON CONVENTIONS" in deployed


def test_deleted_seeded_section_stays_deleted_when_upstream_changes(
    repo: Path, tmp_path: Path
) -> None:
    """A seeded section the user deletes from the live file is not seeded again,
    whether or not the next install also advances the merge base."""
    config = _write_config(repo)
    first = _invoke(config)
    assert first.exit_code == 0, first.output

    _dst(tmp_path).write_text(_DOC, encoding="utf-8")

    unchanged = _invoke(config)
    assert unchanged.exit_code == 0, unchanged.output
    assert "seeded host-local section template(s)" not in unchanged.output
    assert "SEEDED PYTHON CONVENTIONS" not in _dst(tmp_path).read_text(encoding="utf-8")

    (repo / "tracked" / "doc.md").write_text(
        _DOC.replace("upstream notes body", "upstream notes body v2"),
        encoding="utf-8",
    )

    changed = _invoke(config)
    assert changed.exit_code == 0, changed.output
    assert "seeded host-local section template(s)" not in changed.output
    deployed = _dst(tmp_path).read_text(encoding="utf-8")
    assert "upstream notes body v2" in deployed
    assert "SEEDED PYTHON CONVENTIONS" not in deployed


def test_edited_seeded_section_is_preserved_when_upstream_changes(
    repo: Path, tmp_path: Path
) -> None:
    """A seeded section the user edited keeps its edit when upstream changes, and
    the template is not injected a second time."""
    config = _write_config(repo)
    first = _invoke(config)
    assert first.exit_code == 0, first.output

    live = _dst(tmp_path)
    live.write_text(
        live.read_text(encoding="utf-8").replace(
            "SEEDED PYTHON CONVENTIONS", "MY OWN PYTHON CONVENTIONS"
        ),
        encoding="utf-8",
    )
    (repo / "tracked" / "doc.md").write_text(
        _DOC.replace("# Title", "# Title v2"),
        encoding="utf-8",
    )

    second = _invoke(config)
    assert second.exit_code == 0, second.output
    assert "seeded host-local section template(s)" not in second.output
    deployed = live.read_text(encoding="utf-8")
    assert "# Title v2" in deployed
    assert "MY OWN PYTHON CONVENTIONS" in deployed
    assert "SEEDED PYTHON CONVENTIONS" not in deployed
    assert deployed.count("## Python conventions") == 1
