"""Regression test: ``migrate --apply`` preview must include section spans.

Audit finding (Important): the ``--apply`` confirm preview is computed by
``_render_chain_previews``, which copies only a migration's declared
``affected_paths`` into the shadow tree. The breaking 1.2->2.0 contract step
(:class:`Contract20Migration`) declares only ``setforge.yaml`` + ``local.yaml``
as affected, yet its ``apply()`` ALSO READS each tracked markdown ``src`` to
enumerate ``preserve_user_sections`` markers and emit one section span (plus
``disposition: shared``) per marked section.

Because the tracked markdown was never mirrored into the shadow tree,
``src.exists()`` was False during the preview, so the rendered ``setforge.yaml``
"after" image contained NONE of the section spans the real apply writes — the
user confirmed an irreversible, floor-gated migration against a diff that
materially under-represented the change.

The fix mirrors read-only dependency srcs into the shadow tree (see
``_preview_read_dependencies``). These tests fail pre-fix (the spans are absent
from the preview) and pass post-fix.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.cli import migrate as migrate_cli
from setforge.migrations import MigrationRoots

_FLOOR = 'minimum_version: "2.0"\n'

_DOC_WITH_SHARED_SECTION = (
    "# header\n"
    "<!-- setforge:user-section start shared notes -->\n"
    "body\n"
    "<!-- setforge:user-section end shared notes -->\n"
)

_SETFORGE_YAML = (
    _FLOOR + "schema_version: '1.2'\n"
    "tracked_files:\n"
    "  doc:\n"
    "    src: doc.md\n"
    "    dst: ~/doc.md\n"
    "    preserve_user_sections: true\n"
    "profiles: {p: {}}\n"
)


def _seed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "setforge.yaml").write_text(_SETFORGE_YAML, encoding="utf-8")
    (repo / "doc.md").write_text(_DOC_WITH_SHARED_SECTION, encoding="utf-8")
    return repo


def _invoke_apply(repo: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    home = repo.parent / "home"
    home.mkdir()
    monkeypatch.setattr("setforge.cli.migrate.Path.home", staticmethod(lambda: home))
    # ``migrate --apply`` writes a real transition via transitions_root() →
    # Path.home(); pin SETFORGE_STATE_DIR so the record lands in a per-test
    # tmp tree independent of the autouse HOME-isolation fixture (belt-and-
    # suspenders — the HOME fixture alone is a single point of failure).
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(repo.parent / "state"))
    monkeypatch.setattr("setforge.cli.migrate.shutil.which", lambda _: None)
    cfg = repo / "setforge.yaml"
    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--apply", "--yes", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "preview of changes" in result.output
    return result.output


def test_apply_preview_includes_section_span_from_tracked_src(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview's setforge.yaml diff must show the span + shared disposition.

    Pre-fix: the tracked ``doc.md`` was absent from the shadow tree, so the
    1.2->2.0 contract step found no markers and the preview omitted the span.
    Post-fix: the src is mirrored, so the preview surfaces the span the real
    apply writes — and it MATCHES what the applied file ends up containing.
    """
    repo = _seed_repo(tmp_path)
    out = _invoke_apply(repo, monkeypatch)

    # The applied file is the ground truth: read what apply actually wrote.
    applied = (repo / "setforge.yaml").read_text(encoding="utf-8")
    assert "disposition: shared" in applied, applied
    assert "anchor: '## notes'" in applied or "## notes" in applied, applied

    # The preview MUST contain the same span material as additions.
    assert "+    disposition: shared" in out, out
    assert "notes" in out
    # The shared span the real apply emits must appear in the preview diff.
    assert "+    - anchor" in out or "anchor:" in out, out


def test_preview_read_dependencies_enumerates_shared_section_srcs(
    tmp_path: Path,
) -> None:
    """``_preview_read_dependencies`` returns the repo-relative tracked srcs.

    Resolution must match the migration's own ``roots.repo_root / src`` join
    so the mirrored shadow copy lands exactly where apply reads it.
    """
    repo = _seed_repo(tmp_path)
    roots = MigrationRoots(
        cfg_path=repo / "setforge.yaml",
        repo_root=repo,
        home=tmp_path / "home",
    )
    deps = migrate_cli._preview_read_dependencies(roots)
    assert deps == (repo / "doc.md",)


def test_preview_read_dependencies_skips_non_preserve_entries(
    tmp_path: Path,
) -> None:
    """A tracked_file WITHOUT ``preserve_user_sections: true`` is not a dep."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "setforge.yaml").write_text(
        _FLOOR + "schema_version: '1.2'\n"
        "tracked_files:\n"
        "  plain:\n"
        "    src: plain.yaml\n"
        "    dst: ~/plain.yaml\n",
        encoding="utf-8",
    )
    roots = MigrationRoots(
        cfg_path=repo / "setforge.yaml",
        repo_root=repo,
        home=tmp_path / "home",
    )
    assert migrate_cli._preview_read_dependencies(roots) == ()


def test_preview_read_dependencies_tolerates_malformed_config(
    tmp_path: Path,
) -> None:
    """A malformed/unreadable config yields ``()`` (preview still renders)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "setforge.yaml").write_text("just: a: broken: yaml\n", encoding="utf-8")
    roots = MigrationRoots(
        cfg_path=repo / "setforge.yaml",
        repo_root=repo,
        home=tmp_path / "home",
    )
    assert migrate_cli._preview_read_dependencies(roots) == ()

    # A missing config also yields () without raising.
    missing_roots = MigrationRoots(
        cfg_path=tmp_path / "nope" / "setforge.yaml",
        repo_root=tmp_path / "nope",
        home=tmp_path / "home",
    )
    assert migrate_cli._preview_read_dependencies(missing_roots) == ()
