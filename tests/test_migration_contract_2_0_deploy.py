"""End-to-end behavior-preservation tests for the 1.2 -> 2.0 CONTRACT migration.

These are the coverage the unit tests missed: they run the migration AND THEN
:func:`setforge.deploy.copy_atomic` on a migrated tracked_file, asserting that a
key/section the legacy ``preserve_*`` field protected STILL survives a deploy
after the migration. The migration sets a file-level ``disposition`` (Approach
A) so the translated spans are actually consumed — without it the spans are
inert (``deploy.copy_atomic`` only honors spans when ``disposition`` is set).

A non-preserved key DOES take the tracked change (the disposition 3-way merge),
proving the migration upgrades the legacy 2-way semantics to the 3-way model
without losing the preserved value.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

from setforge import deploy
from setforge.config import Disposition
from setforge.migrations import MigrationRoots
from setforge.migrations._contract_2_0 import Contract20Migration
from setforge.spans import SpanEntry

_FLOOR = 'minimum_version: "2.0"\n'


def _roots(tmp_path: Path) -> MigrationRoots:
    return MigrationRoots(
        cfg_path=tmp_path / "setforge.yaml",
        repo_root=tmp_path,
        home=tmp_path / "home",
    )


def _load(path: Path) -> dict:
    yaml = YAML(typ="rt")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.load(fh)


def _tracked_file(tmp_path: Path, file_id: str) -> dict:
    return _load(tmp_path / "setforge.yaml")["tracked_files"][file_id]


def _spans_of(tf: dict) -> list[SpanEntry]:
    return [SpanEntry.model_validate(dict(s)) for s in tf.get("spans", [])]


def _disposition_of(tf: dict) -> Disposition | None:
    raw = tf.get("disposition")
    return Disposition(raw) if raw is not None else None


def test_migrated_preserve_user_keys_survives_deploy(tmp_path: Path) -> None:
    """A preserve_user_keys path keeps its live value through a post-migration deploy.

    The migration translates ``preserve_user_keys: [editor.fontSize]`` into
    ``disposition: forked`` + a PINNED structural span. Deploying a tracked
    source that CHANGES ``editor.fontSize`` must NOT clobber the live value
    (the pin re-asserts live), while a NON-preserved key (``editor.theme``)
    DOES take the tracked change (the 3-way merge).
    """
    src = tmp_path / "settings.yaml"
    src.write_text("editor:\n  fontSize: 12\n  theme: dark\n", encoding="utf-8")
    (tmp_path / "setforge.yaml").write_text(
        _FLOOR + "schema_version: '1.2'\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: settings.yaml\n"
        "    dst: ~/settings.yaml\n"
        "    preserve_user_keys:\n"
        "      - editor.fontSize\n",
        encoding="utf-8",
    )
    Contract20Migration().apply(roots=_roots(tmp_path))
    tf = _tracked_file(tmp_path, "settings")
    disposition = _disposition_of(tf)
    spans = _spans_of(tf)
    assert disposition is Disposition.FORKED
    assert any(s.anchor == "editor.fontSize" for s in spans)

    # Live carries a user-edited fontSize + theme; the base seeds from live on
    # first install (the disposition-gated seed). Tracked CHANGES both keys.
    dst = tmp_path / "live.yaml"
    dst.write_text("editor:\n  fontSize: 18\n  theme: light\n", encoding="utf-8")
    base_text = dst.read_text(encoding="utf-8")  # first-install seed == live

    result = deploy.copy_atomic(
        src,
        dst,
        disposition=disposition,
        base_text=base_text,
        spans=spans,
        span_states={},
    )
    merged = YAML(typ="rt").load(result.dst.read_text(encoding="utf-8"))
    # PRESERVED key: the live value survives (pin re-assert beats tracked).
    assert merged["editor"]["fontSize"] == 18
    # NON-preserved key: the 3-way merge takes the tracked change.
    assert merged["editor"]["theme"] == "dark"


def test_migrated_shared_section_survives_deploy(tmp_path: Path) -> None:
    """A shared preserve_user_sections body survives a post-migration deploy.

    The migration translates a shared marked section into ``disposition:
    shared`` + a section span. With a stored base recording the previously
    shipped body, a host that LOCALLY EDITED the section keeps its edit when a
    new tracked body lands (a 3-way conflict that the bare policy resolves
    toward live) — the legacy preserve_user_sections live-wins behavior.
    """
    src = tmp_path / "doc.md"
    src.write_text(
        "intro\n"
        "<!-- setforge:user-section start shared notes -->\n"
        "tracked body v2\n"
        "<!-- setforge:user-section end shared notes -->\n",
        encoding="utf-8",
    )
    (tmp_path / "setforge.yaml").write_text(
        _FLOOR + "schema_version: '1.2'\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/doc.md\n"
        "    preserve_user_sections: true\n",
        encoding="utf-8",
    )
    Contract20Migration().apply(roots=_roots(tmp_path))
    tf = _tracked_file(tmp_path, "doc")
    assert _disposition_of(tf) is Disposition.SHARED
    spans = _spans_of(tf)

    # base = the previously shipped body; live = the host's local edit; tracked
    # = a new body. All three differ -> the section conflicts -> bare keeps live.
    base_text = (
        "intro\n"
        "<!-- setforge:user-section start shared notes -->\n"
        "tracked body v1\n"
        "<!-- setforge:user-section end shared notes -->\n"
    )
    dst = tmp_path / "live.md"
    dst.write_text(
        "intro\n"
        "<!-- setforge:user-section start shared notes -->\n"
        "LIVE EDITED body\n"
        "<!-- setforge:user-section end shared notes -->\n",
        encoding="utf-8",
    )

    result = deploy.copy_atomic(
        src,
        dst,
        disposition=Disposition.SHARED,
        base_text=base_text,
        spans=spans or None,
        span_states={},
    )
    written = result.dst.read_text(encoding="utf-8")
    # The live-edited section body survives the deploy (3-way conflict kept
    # live under the bare policy) rather than being clobbered by the new
    # tracked body — the legacy preserve_user_sections semantics.
    assert "LIVE EDITED body" in written
    assert "tracked body v2" not in written
