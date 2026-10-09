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
from ruamel.yaml import YAML
from typer.testing import CliRunner

from setforge import locking, reconcile
from setforge.cli import app
from setforge.errors import ConfigError, SetforgeError
from setforge.migrations import (
    MigrationRoots,
    current_expected_schema_version,
    detect_current_schema,
    parse_schema_version,
)
from setforge.migrations._span_surface_retire import (
    SpanSurfaceRetireMigration,
    _SpanSurfaceRetireReverse,
)
from setforge.migrations.registry import find_migration_path
from setforge.reconcile import file_id
from setforge.reconcile.host_local_view import host_local_headings_from_store
from setforge.reconcile.hunks import extract_hunks, serialize
from setforge.reconcile.types import HunkClass

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


def test_fold_preserves_legacy_profile_fields_for_later_migration(
    tmp_path: Path,
) -> None:
    roots = _setup(tmp_path, local_yaml_body=_local_yaml_body())
    roots.cfg_path.write_text(
        roots.cfg_path.read_text().replace(
            "tracked_files: [notes]", "tracked_files: [notes]\n    cargo_binaries: [rg]"
        )
    )

    SpanSurfaceRetireMigration().apply(roots=roots)

    assert "cargo_binaries: [rg]" in roots.cfg_path.read_text()
    assert detect_current_schema(roots.cfg_path) == "4.0"
    assert reconcile.read_base("default", file_id("notes")) == _BASE
    assert reconcile.read_local("default", file_id("notes")) == (
        b"## Alpha\n## My Tweaks\nmy custom line\naaa\n## Beta\nbbb\n## Gamma\nccc\n"
    )


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
    assert host_local_headings_from_store("default", fid) == {"## My Tweaks"}


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
    assert host_local_headings_from_store("default", fid) == {"## My Tweaks"}


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
    assert host_local_headings_from_store("default", fid) == {"## My Tweaks"}


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


_SECTION_YAML = """\
host_local_sections:
  my-tweaks:
    anchor:
      kind: after-heading
      value: Alpha
    body: "## My Tweaks\\nmy custom line\\n"
"""


def _notes_entry(*, before: str = "", after: str = "") -> str:
    """A ``tracked_files`` ``notes`` entry with the section between two extras."""
    section = textwrap.indent(_SECTION_YAML, "    ")
    return f"tracked_files:\n  notes:\n{before}{section}{after}"


def _strip_through_apply(tmp_path: Path, local_text: str) -> tuple[MigrationRoots, str]:
    roots = _setup(tmp_path, local_yaml_body=local_text, deploy=False)
    SpanSurfaceRetireMigration().apply(roots=roots)
    return roots, _local_yaml_path(roots).read_text(encoding="utf-8")


def test_strip_writes_the_same_bytes_for_an_ordinary_local_yaml(tmp_path) -> None:
    """The strip keeps its writer: this literal is what the migration wrote
    before the read-back check was added (comments, order and the survivor span
    kept; the document re-indented to the writer's own style)."""
    local = "# Host-local overrides.\n" + _notes_entry(
        before='    mode: "0600"  # private\n',
        after="    spans:\n      - kind: overlay\n        start: 1\n"
        "      - kind: keep\n        start: 2\n"
        "  other:\n    dst: ~/other.md\n"
        "binaries:\n    code: /usr/bin/code\n",
    )
    _, written = _strip_through_apply(tmp_path, local)
    assert written == (
        "# Host-local overrides.\n"
        "tracked_files:\n"
        "  notes:\n"
        '    mode: "0600"  # private\n'
        "    spans:\n"
        "    - kind: keep\n"
        "      start: 2\n"
        "  other:\n"
        "    dst: ~/other.md\n"
        "binaries:\n"
        "  code: /usr/bin/code\n"
    )


@pytest.mark.parametrize(
    ("local", "want"),
    [
        pytest.param(
            "plugins:\n    add:\n        - foo\n        - bar\n"
            + _notes_entry(after='    mode: "0600"\n')
            + "extensions:\n  add:\n  - a.b\n  - c.d\n",
            {
                "plugins": {"add": ["foo", "bar"]},
                "tracked_files": {"notes": {"mode": "0600"}},
                "extensions": {"add": ["a.b", "c.d"]},
            },
            id="mixed-indentation",
        ),
        pytest.param(
            "tracked_files:\n    notes:\n        mode: '0600'\n"
            + textwrap.indent(_SECTION_YAML, "        ")
            + "    other:\n      dst: ~/o.md\n",
            {"tracked_files": {"notes": {"mode": "0600"}, "other": {"dst": "~/o.md"}}},
            id="mixed-mapping-indent",
        ),
        pytest.param(
            'tracked_files:\n  notes: {mode: "0600", host_local_sections: '
            "{my-tweaks: {anchor: {kind: after-heading, value: Alpha}, "
            'body: "## My Tweaks\\nmy custom line\\n"}}}\n',
            {"tracked_files": {"notes": {"mode": "0600"}}},
            id="flow-mapping",
        ),
        pytest.param(
            _notes_entry(after="    mode: '0600'").rstrip("\n"),
            {"tracked_files": {"notes": {"mode": "0600"}}},
            id="no-final-newline",
        ),
        pytest.param(
            "# a\n# b\n" + _notes_entry(after="    mode: '0600'\n") + "# end\n",
            {"tracked_files": {"notes": {"mode": "0600"}}},
            id="comments-outside-the-entry",
        ),
    ],
)
def test_strip_keeps_the_data_of_an_unusual_layout(
    tmp_path, local: str, want: object
) -> None:
    roots, written = _strip_through_apply(tmp_path, local)
    assert YAML(typ="safe").load(written) == want
    assert detect_current_schema(roots.cfg_path) == "4.0"


def test_a_local_yaml_of_only_comments_is_left_alone(tmp_path) -> None:
    text = "# nothing here yet\n# still nothing\n"
    roots, written = _strip_through_apply(tmp_path, text)
    assert written == text
    assert detect_current_schema(roots.cfg_path) == "4.0"


_NOT_READ_BACK = (
    "refusing to write {path}: the edited file would not read back as the change "
    "that was checked, so nothing was written. Remove the comments and blank "
    "lines inside its tracked_files entries, then run the migration again."
)

# The entry holds nothing but the section, so stripping empties it; a comment
# or blank line between its key and the block makes the emptied entry unreadable.
_EMPTIED_ENTRY = [
    pytest.param(
        "tracked_files:\n  notes:  # the notes\n"
        + textwrap.indent(_SECTION_YAML, "    "),
        id="comment-on-the-key-line",
    ),
    pytest.param(
        "tracked_files:\n  notes:\n    # why\n"
        + textwrap.indent(_SECTION_YAML, "    "),
        id="comment-above-the-section",
    ),
    pytest.param(
        "tracked_files:\n  notes:\n\n" + textwrap.indent(_SECTION_YAML, "    "),
        id="blank-line-above-the-section",
    ),
]


@pytest.mark.parametrize("local", _EMPTIED_ENTRY)
def test_apply_refuses_a_strip_that_would_not_read_back(tmp_path, local: str) -> None:
    """Such a file used to be written unreadable, with the migration reporting
    success; now it is refused before anything changes."""
    roots = _setup(tmp_path, local_yaml_body=local, deploy=False)
    local_yaml = _local_yaml_path(roots)
    cfg_before = roots.cfg_path.read_bytes()

    with pytest.raises(SetforgeError) as raised:
        SpanSurfaceRetireMigration().apply(roots=roots)

    assert str(raised.value) == _NOT_READ_BACK.format(path=local_yaml)
    assert local_yaml.read_text(encoding="utf-8") == local
    assert roots.cfg_path.read_bytes() == cfg_before
    assert reconcile.read_base("default", file_id("notes")) is None


@pytest.mark.parametrize("local", _EMPTIED_ENTRY)
def test_migrate_apply_refuses_a_strip_that_would_not_read_back(
    tmp_path: Path, state_dir: Path, local: str
) -> None:
    roots = _setup(tmp_path, local_yaml_body=local)
    local_yaml = _local_yaml_path(roots)
    cfg_before = roots.cfg_path.read_bytes()

    result = runner.invoke(
        app,
        ["migrate", "--config", str(roots.cfg_path), "--to", "4.0", "--apply", "--yes"],
    )

    assert result.exit_code != 0
    assert _NOT_READ_BACK.format(path=local_yaml) in " ".join(result.output.split())
    assert local_yaml.read_text(encoding="utf-8") == local
    assert roots.cfg_path.read_bytes() == cfg_before
    assert reconcile.read_base("default", file_id("notes")) is None


def test_the_refusal_clears_once_the_comment_is_removed(tmp_path) -> None:
    roots = _setup(
        tmp_path,
        local_yaml_body="tracked_files:\n  notes:\n    # why\n"
        + textwrap.indent(_SECTION_YAML, "    "),
        deploy=False,
    )
    with pytest.raises(SetforgeError):
        SpanSurfaceRetireMigration().apply(roots=roots)

    _local_yaml_path(roots).write_text(_notes_entry(), encoding="utf-8")
    SpanSurfaceRetireMigration().apply(roots=roots)

    assert YAML(typ="safe").load(_local_yaml_path(roots).read_text()) == {
        "tracked_files": {"notes": {}}
    }
    assert detect_current_schema(roots.cfg_path) == "4.0"
