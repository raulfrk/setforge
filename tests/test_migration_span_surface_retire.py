"""Tests for the 3.0 -> 4.0 span-surface-retire migration.

These cover the migration's identity + registration surface and the real
``apply``: it folds the residual ``local.yaml`` host-local
section surface into the per-unit reconcile LOCAL store (MERGING into any
existing entry so shared drift + staged classifications survive), stamps schema
4.0, and strips the retired ``host_local_sections`` / overlay-``spans`` keys,
transition-revertibly.

The fold is heading-identity based: a host-local section is folded onto a
``reloc_anchor`` minted from the body's own markdown heading, so a body without a
heading is refused up front by a pre-flight check.
"""

from __future__ import annotations

import textwrap
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import locking, reconcile
from setforge.cli import app
from setforge.errors import ConfigError
from setforge.migrations import (
    MigrationRoots,
    current_expected_schema_version,
    detect_current_schema,
    find_migration_path,
    parse_schema_version,
)
from setforge.migrations._span_surface_retire import (
    SpanSurfaceRetireMigration,
    _SpanSurfaceRetireReverse,
)
from setforge.reconcile import file_id
from setforge.reconcile.host_local_view import host_local_sections_from_store
from setforge.reconcile.hunks import extract_hunks, serialize
from setforge.reconcile.types import HunkClass
from setforge.source import HostLocalSectionName

runner = CliRunner()

_BASE = b"## Alpha\naaa\n## Beta\nbbb\n## Gamma\nccc\n"
_SECTION_BODY = "## My Tweaks\nmy custom line\n"

_CFG = """schema_version: "3.0"
tracked_files:
  notes:
    src: notes.md
    dst: ~/notes.md
profiles:
  default:
    tracked_files: [notes]
"""


def _local_yaml_body(
    *, body: str = _SECTION_BODY, section_name: str = "my-tweaks"
) -> str:
    """A ``local.yaml`` declaring one host-local section on ``notes``.

    ``section_name`` is the (arbitrary) legacy dict-key; it deliberately differs
    from the body's heading so the tests prove the fold keys on the HEADING
    identity, not the declaration name.
    """
    escaped = body.replace("\n", "\\n")
    return textwrap.dedent(
        f"""\
        tracked_files:
          notes:
            host_local_sections:
              {section_name}:
                anchor:
                  kind: after-heading
                  value: Alpha
                body: "{escaped}"
        """
    )


def _roots_only(tmp_path: Path) -> MigrationRoots:
    return MigrationRoots(
        cfg_path=tmp_path / "setforge.yaml", repo_root=tmp_path, home=tmp_path
    )


def _setup(
    tmp_path: Path,
    *,
    base: bytes = _BASE,
    local_yaml_body: str | None = None,
    deploy: bool = True,
) -> MigrationRoots:
    """Write setforge.yaml + tracked source + (optionally) local.yaml + live file.

    ``home`` is the per-test isolated ``Path.home()`` (autouse fixture), the SAME
    root the reconcile store resolves under — so a test can pre-seed the store and
    the migration observes it.
    """
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    (repo / "setforge.yaml").write_text(_CFG, encoding="utf-8")
    (repo / "tracked" / "notes.md").write_bytes(base)
    home = Path.home()
    if local_yaml_body is not None:
        lc = home / ".config" / "setforge" / "local.yaml"
        lc.parent.mkdir(parents=True, exist_ok=True)
        lc.write_text(local_yaml_body, encoding="utf-8")
    if deploy:
        (home / "notes.md").write_bytes(base)
    return MigrationRoots(cfg_path=repo / "setforge.yaml", repo_root=repo, home=home)


def _local_yaml_path(roots: MigrationRoots) -> Path:
    return roots.home / ".config" / "setforge" / "local.yaml"


def test_span_surface_retire_is_no_longer_terminal() -> None:
    # The build advanced well past 4.0 (span_types retirement → 5.0, then the
    # profile-fields contract → 6.0); the span-surface cutover is now an
    # intermediate chain step, not the head.
    assert current_expected_schema_version == "6.5"
    assert parse_schema_version(current_expected_schema_version) > (4, 0)


def test_parse_four_zero() -> None:
    assert parse_schema_version("4.0") == (4, 0)


def test_find_migration_path_three_zero_to_four_zero_is_one_step() -> None:
    chain = find_migration_path(from_v="3.0", to_v="4.0")
    assert len(chain) == 1
    assert (chain[0].from_version, chain[0].to_version) == ("3.0", "4.0")


def test_forward_versions() -> None:
    m = SpanSurfaceRetireMigration()
    assert (m.from_version, m.to_version) == ("3.0", "4.0")
    assert m.writes_own_transition is True


def test_reverse_is_swapped() -> None:
    rev = SpanSurfaceRetireMigration().reverse
    assert isinstance(rev, _SpanSurfaceRetireReverse)
    assert (rev.from_version, rev.to_version) == ("4.0", "3.0")
    assert (rev.reverse.from_version, rev.reverse.to_version) == ("3.0", "4.0")


def test_reverse_manifest_is_note_only_and_touches_nothing(tmp_path) -> None:
    rev = _SpanSurfaceRetireReverse()
    roots = _roots_only(tmp_path)
    entries = rev.manifest(roots=roots)
    assert len(entries) == 1
    assert entries[0].affected_path == roots.cfg_path
    assert rev.affected_paths(roots=roots) == ()


def test_reverse_refuses_cleanly(tmp_path) -> None:
    rev = _SpanSurfaceRetireReverse()
    with pytest.raises(ConfigError, match=r"setforge revert --profile=migrate"):
        rev.apply(roots=_roots_only(tmp_path))


def test_apply_without_local_yaml_just_stamps(tmp_path) -> None:
    """No residual host-local surface ⇒ advance schema to 4.0, nothing folded."""
    roots = _setup(tmp_path, local_yaml_body=None, deploy=False)
    SpanSurfaceRetireMigration().apply(roots=roots)
    assert detect_current_schema(roots.cfg_path) == "4.0"


def test_apply_folds_deployed_section_preserving_drift(tmp_path) -> None:
    """(a) A deployed file with a PRE-EXISTING store entry (shared drift + a staged
    hunk) folds the local.yaml section as a LOCAL+reloc unit WITHOUT clobbering the
    recorded drift or the staged classification (INV-1 / INV-8)."""
    roots = _setup(tmp_path, local_yaml_body=_local_yaml_body())
    fid = file_id("notes")

    drift_local = b"## Alpha\naaa\n## Beta\nbbb\n## Gamma\ncccEDIT\n"
    with locking.profile_lock("default"):
        drift_rows = serialize(
            [
                replace(h, cls=HunkClass.SHARED)
                for h in extract_hunks(_BASE, drift_local)
            ]
        )
        reconcile.record(
            "default",
            fid,
            base=_BASE,
            local=drift_local,
            staged=True,
            hunks=drift_rows,
        )

    SpanSurfaceRetireMigration().apply(roots=roots)

    assert detect_current_schema(roots.cfg_path) == "4.0"
    assert reconcile.read_base("default", fid) == _BASE
    expected_local = (
        b"## Alpha\n## My Tweaks\nmy custom line\naaa\n"
        b"## Beta\nbbb\n## Gamma\ncccEDIT\n"
    )
    assert reconcile.read_local("default", fid) == expected_local

    hunks = reconcile.read_index("default").files["notes"].hunks
    assert any(h["cls"] == HunkClass.SHARED.value for h in hunks)
    assert any(
        h["cls"] == HunkClass.LOCAL.value and h.get("reloc_anchor") == "## My Tweaks"
        for h in hunks
    )
    proj = host_local_sections_from_store("default", fid)
    assert set(proj["notes"]) == {"## My Tweaks"}
    assert proj["notes"][HostLocalSectionName("## My Tweaks")].body == _SECTION_BODY


def test_apply_fold_preserves_shared_drafted_class_and_draft_bytes(tmp_path) -> None:
    """A fid whose store already carries a SHARED_DRAFTED hunk (+ its draft_hash +
    draft bytes) survives the fold of a NEW residual host-local section: the fold
    carries the SHARED_DRAFTED classification forward AND leaves the draft bytes in
    the drafts store untouched (record with no explicit drafts= preserves them)."""
    from setforge.reconcile import store as reconcile_store

    roots = _setup(tmp_path, local_yaml_body=_local_yaml_body())
    fid = file_id("notes")

    drift_local = b"## Alpha\naaa\n## Beta\nbbb\n## Gamma\ncccLOCAL\n"
    (gamma_hunk,) = extract_hunks(_BASE, drift_local)
    draft_bytes = b"cccSHARED\n"
    with locking.profile_lock("default"):
        drafted_rows = serialize(
            [replace(gamma_hunk, cls=HunkClass.SHARED_DRAFTED, draft_hash="sha256:gd")]
        )
        reconcile.record(
            "default",
            fid,
            base=_BASE,
            local=drift_local,
            staged=True,
            hunks=drafted_rows,
            drafts={gamma_hunk.ref: draft_bytes},
        )

    SpanSurfaceRetireMigration().apply(roots=roots)

    assert detect_current_schema(roots.cfg_path) == "4.0"
    hunks = reconcile.read_index("default").files["notes"].hunks
    assert any(
        h["cls"] == HunkClass.SHARED_DRAFTED.value
        and h.get("draft_hash") == "sha256:gd"
        for h in hunks
    )
    assert any(
        h["cls"] == HunkClass.LOCAL.value and h.get("reloc_anchor") == "## My Tweaks"
        for h in hunks
    )
    assert reconcile_store.read_drafts("default", fid) == {gamma_hunk.ref: draft_bytes}


def test_apply_folds_undeployed_section_without_loss(tmp_path) -> None:
    """(b) A section on an UNDEPLOYED file (no live file, no store entry) still folds
    — the body is the source of truth (base=tracked, local=synthesized)."""
    roots = _setup(tmp_path, local_yaml_body=_local_yaml_body(), deploy=False)
    fid = file_id("notes")
    assert reconcile.read_base("default", fid) is None

    SpanSurfaceRetireMigration().apply(roots=roots)

    assert reconcile.read_base("default", fid) == _BASE
    expected_local = (
        b"## Alpha\n## My Tweaks\nmy custom line\naaa\n## Beta\nbbb\n## Gamma\nccc\n"
    )
    assert reconcile.read_local("default", fid) == expected_local
    proj = host_local_sections_from_store("default", fid)
    assert proj["notes"][HostLocalSectionName("## My Tweaks")].body == _SECTION_BODY


def test_apply_is_idempotent_no_double_seed(tmp_path) -> None:
    """(c) A second run does not re-inject the already-folded section (idempotent),
    keyed on the heading identity, not the declaration name."""
    roots = _setup(tmp_path, local_yaml_body=_local_yaml_body(), deploy=False)
    fid = file_id("notes")

    SpanSurfaceRetireMigration().apply(roots=roots)
    local_after_first = reconcile.read_local("default", fid)
    index_after_first = reconcile.read_index("default")

    # Manually re-stamp to 3.0 + re-declare so `apply` runs its fold again.
    roots.cfg_path.write_text(_CFG, encoding="utf-8")
    _local_yaml_path(roots).write_text(_local_yaml_body(), encoding="utf-8")

    SpanSurfaceRetireMigration().apply(roots=roots)

    assert reconcile.read_local("default", fid) == local_after_first
    assert reconcile.read_index("default") == index_after_first
    proj = host_local_sections_from_store("default", fid)
    assert set(proj["notes"]) == {"## My Tweaks"}


def test_apply_strips_retired_surface_from_local_yaml(tmp_path) -> None:
    """After a fold the retired host-local surface is stripped from local.yaml."""
    roots = _setup(tmp_path, local_yaml_body=_local_yaml_body(), deploy=False)
    SpanSurfaceRetireMigration().apply(roots=roots)
    text = _local_yaml_path(roots).read_text(encoding="utf-8")
    assert "host_local_sections" not in text
    assert "My Tweaks" not in text


def test_apply_refuses_headingless_section(tmp_path) -> None:
    """A host-local section body with no markdown heading is refused up front —
    the 4.0 store identity is heading-based — and nothing is mutated."""
    roots = _setup(
        tmp_path,
        local_yaml_body=_local_yaml_body(body="just a plain note line\n"),
        deploy=False,
    )
    with pytest.raises(ConfigError, match=r"no markdown heading"):
        SpanSurfaceRetireMigration().apply(roots=roots)
    assert detect_current_schema(roots.cfg_path) == "3.0"
    assert "host_local_sections" in _local_yaml_path(roots).read_text(encoding="utf-8")
    assert reconcile.read_base("default", file_id("notes")) is None


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def test_migrate_then_revert_restores_local_yaml_and_reconcile_legs(
    tmp_path: Path, state_dir: Path
) -> None:
    """(d) A full ``migrate --apply`` through the driver is revertible byte-exact
    across BOTH the local.yaml strip AND the mutated reconcile legs (INV-5)."""
    from setforge import base_store
    from setforge.reconcile import store as reconcile_store

    roots = _setup(tmp_path, local_yaml_body=_local_yaml_body())
    cfg = roots.cfg_path
    fid = file_id("notes")

    # A drifted (not fresh) entry so revert must restore modified legs, not
    # merely delete freshly-created ones.
    drift_local = b"## Alpha\naaa\n## Beta\nbbb\n## Gamma\ncccEDIT\n"
    with locking.profile_lock("default"):
        drift_rows = serialize(
            [
                replace(h, cls=HunkClass.SHARED)
                for h in extract_hunks(_BASE, drift_local)
            ]
        )
        reconcile.record(
            "default",
            fid,
            base=_BASE,
            local=drift_local,
            staged=True,
            hunks=drift_rows,
        )

    local_yaml = _local_yaml_path(roots)
    base_path = base_store.base_path("default", str(fid))
    local_path = reconcile_store.local_content_path("default", str(fid))
    index_path = reconcile_store.index_manifest_path("default")
    pre = {p: p.read_bytes() for p in (local_yaml, base_path, local_path, index_path)}
    cfg_pre = cfg.read_bytes()

    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "4.0", "--apply", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert detect_current_schema(cfg) == "4.0"
    assert local_path.read_bytes() != pre[local_path]

    revert = runner.invoke(
        app, ["revert", "--profile=migrate", f"--config={cfg}", "--yes"]
    )
    assert revert.exit_code == 0, revert.output

    assert cfg.read_bytes() == cfg_pre
    for p, want in pre.items():
        assert p.read_bytes() == want, f"revert did not restore {p} byte-exact"
