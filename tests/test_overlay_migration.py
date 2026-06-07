"""Physical ``local.yaml`` rewrite: ``host_local_sections`` → OVERLAY spans.

Covers :func:`setforge.overlay_migration.migrate_local_yaml_overlay_spans`:
the on-disk retirement of the legacy host-local mechanism into the unified
span model, with comment / order / quoting / mode preservation, idempotency,
all five anchor kinds + the ``body_file`` variant, and behavioral equivalence
(the rewritten config resolves to the SAME overlay payload the legacy loader
projected pre-migration).
"""

from __future__ import annotations

import stat
from pathlib import Path

from ruamel.yaml import YAML

from setforge.anchors import AnchorKind
from setforge.overlay_migration import migrate_local_yaml_overlay_spans
from setforge.source import (
    load_local_host_local_sections,
    load_local_tracked_file_overlays,
)
from setforge.spans import SpanKind, SpanSemantics

# A representative local.yaml carrying comments, a quoted scalar, and a
# pre-existing (non-overlay) span the migration must leave intact.
_REPRESENTATIVE = """\
# top-of-file comment
schema_version: '1.2'

tracked_files:
  claude_md:
    # claude_md tweaks
    spans:
      - anchor: "## Pinned"
        kind: pinned
    host_local_sections:
      my-notes:
        # the notes splice point
        anchor:
          kind: after-heading
          value: "Notes"
        body: |
          host-local notes body
"""


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_rewrites_representative_local_yaml(tmp_path: Path) -> None:
    """host_local_sections is retired into an equivalent overlay span entry."""
    local = _write(tmp_path / "local.yaml", _REPRESENTATIVE)

    result = migrate_local_yaml_overlay_spans(local)

    assert result.migrated is True
    assert result.section_count == 1
    text = local.read_text(encoding="utf-8")
    # Legacy block gone; overlay entry present.
    assert "host_local_sections:" not in text
    assert "kind: overlay" in text
    assert "semantics: host-local" in text
    # Comments + key order + quoting preserved by the round-trip.
    assert "# top-of-file comment" in text
    assert "# claude_md tweaks" in text
    assert "# the notes splice point" in text
    assert "schema_version: '1.2'" in text
    # The pre-existing pinned span survives ahead of the new overlay entry.
    assert text.index('anchor: "## Pinned"') < text.index("kind: overlay")


def test_migrated_yaml_round_trips(tmp_path: Path) -> None:
    """The rewritten document re-parses cleanly and validates as a span."""
    local = _write(tmp_path / "local.yaml", _REPRESENTATIVE)
    migrate_local_yaml_overlay_spans(local)

    overlays = load_local_tracked_file_overlays(local)
    spans = overlays["claude_md"].spans
    overlay_spans = [s for s in spans if s.kind is SpanKind.OVERLAY]
    assert len(overlay_spans) == 1
    span = overlay_spans[0]
    assert span.anchor == "my-notes"
    assert span.semantics is SpanSemantics.HOST_LOCAL
    assert span.overlay is not None
    assert span.overlay.anchor.kind is AnchorKind.AFTER_HEADING
    assert span.overlay.body == "host-local notes body\n"


def test_idempotent_second_run_is_byte_identical(tmp_path: Path) -> None:
    """Re-running on an already-migrated file is a no-op (byte-for-byte)."""
    local = _write(tmp_path / "local.yaml", _REPRESENTATIVE)
    migrate_local_yaml_overlay_spans(local)
    after_first = local.read_bytes()

    result = migrate_local_yaml_overlay_spans(local)

    assert result.migrated is False
    assert result.section_count == 0
    assert local.read_bytes() == after_first


def test_no_host_local_sections_is_noop(tmp_path: Path) -> None:
    """A file with no host_local_sections is left byte-for-byte untouched."""
    content = (
        "tracked_files:\n"
        "  claude_md:\n"
        "    spans:\n"
        '      - anchor: "## Pinned"\n'
        "        kind: pinned\n"
    )
    local = _write(tmp_path / "local.yaml", content)
    before = local.read_bytes()

    result = migrate_local_yaml_overlay_spans(local)

    assert result.migrated is False
    assert local.read_bytes() == before


def test_absent_file_is_noop(tmp_path: Path) -> None:
    """A missing local.yaml reports no migration without raising."""
    result = migrate_local_yaml_overlay_spans(tmp_path / "absent.yaml")
    assert result.migrated is False
    assert result.section_count == 0


def test_preserves_file_mode(tmp_path: Path) -> None:
    """The rewritten file keeps the original POSIX mode bits."""
    local = _write(tmp_path / "local.yaml", _REPRESENTATIVE)
    local.chmod(0o640)
    pre_mode = stat.S_IMODE(local.stat().st_mode)

    migrate_local_yaml_overlay_spans(local)

    assert stat.S_IMODE(local.stat().st_mode) == pre_mode


_ALL_ANCHOR_KINDS = """\
tracked_files:
  claude_md:
    host_local_sections:
      sec-after-heading:
        anchor:
          kind: after-heading
          value: "Notes"
        body: "after-heading body\\n"
      sec-before-heading:
        anchor:
          kind: before-heading
          value: "Notes"
        body: "before-heading body\\n"
      sec-at-start:
        anchor:
          kind: at-start-of-file
        body: "at-start body\\n"
      sec-at-end:
        anchor:
          kind: at-end-of-file
        body: "at-end body\\n"
      sec-after-section:
        anchor:
          kind: after-section
          name: "MySection"
        body: "after-section body\\n"
      sec-body-file:
        anchor:
          kind: after-heading
          value: "Snippets"
        body_file: snippets/extra.md
"""


def test_all_anchor_kinds_and_body_file_migrate(tmp_path: Path) -> None:
    """All five anchor kinds + the body_file variant migrate faithfully."""
    local = _write(tmp_path / "local.yaml", _ALL_ANCHOR_KINDS)

    result = migrate_local_yaml_overlay_spans(local)

    assert result.migrated is True
    assert result.section_count == 6
    overlays = load_local_tracked_file_overlays(local)
    spans = {s.anchor: s for s in overlays["claude_md"].spans}
    kinds = {}
    for name in (
        "sec-after-heading",
        "sec-before-heading",
        "sec-at-start",
        "sec-at-end",
        "sec-after-section",
    ):
        payload = spans[name].overlay
        assert payload is not None
        kinds[name] = payload.anchor.kind
    assert kinds["sec-after-heading"] is AnchorKind.AFTER_HEADING
    assert kinds["sec-before-heading"] is AnchorKind.BEFORE_HEADING
    assert kinds["sec-at-start"] is AnchorKind.AT_START_OF_FILE
    assert kinds["sec-at-end"] is AnchorKind.AT_END_OF_FILE
    assert kinds["sec-after-section"] is AnchorKind.AFTER_SECTION
    # body_file variant carries the path, not an inline body.
    bf = spans["sec-body-file"].overlay
    assert bf is not None
    assert bf.body is None
    assert bf.body_file == Path("snippets/extra.md")


def test_resolved_overlay_equivalent_to_legacy_projection(tmp_path: Path) -> None:
    """The migrated config resolves the SAME overlay payload as the legacy loader.

    Proves the rewrite changed REPRESENTATION, not behavior: each migrated
    OVERLAY span's structured anchor + body matches the legacy
    ``HostLocalSection`` the pre-migration loader projected.
    """
    local = _write(tmp_path / "local.yaml", _ALL_ANCHOR_KINDS)
    # Capture the legacy projection BEFORE the rewrite.
    legacy = load_local_host_local_sections(local)["claude_md"]

    migrate_local_yaml_overlay_spans(local)

    overlays = load_local_tracked_file_overlays(local)
    overlay_spans = {
        s.anchor: s for s in overlays["claude_md"].spans if s.kind is SpanKind.OVERLAY
    }
    assert set(overlay_spans) == set(legacy)
    for name, section in legacy.items():
        payload = overlay_spans[name].overlay
        assert payload is not None
        # Structured anchor identical (model-dump compare covers every kind).
        assert payload.anchor.model_dump() == section.anchor.model_dump()
        assert payload.body == section.body
        assert payload.body_file == section.body_file


_MULTI_FILE = """\
tracked_files:
  claude_md:
    host_local_sections:
      a:
        anchor: {kind: at-end-of-file}
        body: "a body\\n"
      b:
        anchor: {kind: at-start-of-file}
        body: "b body\\n"
  gitconfig:
    host_local_sections:
      c:
        anchor: {kind: after-heading, value: "X"}
        body: "c body\\n"
"""


def test_multiple_sections_and_files_all_migrate(tmp_path: Path) -> None:
    """Multiple host_local_sections across multiple files all migrate."""
    local = _write(tmp_path / "local.yaml", _MULTI_FILE)

    result = migrate_local_yaml_overlay_spans(local)

    assert result.migrated is True
    assert result.section_count == 3
    overlays = load_local_tracked_file_overlays(local)
    claude = {
        s.anchor for s in overlays["claude_md"].spans if s.kind is SpanKind.OVERLAY
    }
    gitcfg = {
        s.anchor for s in overlays["gitconfig"].spans if s.kind is SpanKind.OVERLAY
    }
    assert claude == {"a", "b"}
    assert gitcfg == {"c"}
    text = local.read_text(encoding="utf-8")
    assert "host_local_sections:" not in text


def test_yaml_load_round_trips_via_ruamel(tmp_path: Path) -> None:
    """Sanity: ruamel can re-load the rewritten document without error."""
    local = _write(tmp_path / "local.yaml", _REPRESENTATIVE)
    migrate_local_yaml_overlay_spans(local)
    yaml = YAML(typ="rt")
    with local.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    assert "host_local_sections" not in data["tracked_files"]["claude_md"]
