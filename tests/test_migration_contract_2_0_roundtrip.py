"""Round-trip + reverse tests for the 1.2 <-> 2.0 CONTRACT migration.

The reverse untranslates ONLY 2.0-exclusive spans (deep structural spans, and
section spans carrying ``capture_mode``) back to their legacy preserve_* shape;
plain PINNED / FORKED / OVERLAY spans (valid at 1.2 already) survive untouched.
The round-trip restores schema SHAPE + behavior-equivalence, not byte identity.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

from setforge.migrations import MigrationRoots
from setforge.migrations._contract_2_0 import Contract20Migration


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


_FLOOR = 'minimum_version: "2.0"\n'


def test_roundtrip_shallow_keys_behavior_equivalent(tmp_path: Path) -> None:
    """1.2 -> 2.0 -> 1.2 keeps preserve_user_keys behavior (as plain spans)."""
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
    roots = _roots(tmp_path)
    fwd = Contract20Migration()
    fwd.apply(roots=roots)
    fwd.reverse.apply(roots=roots)
    data = _load(tmp_path / "setforge.yaml")
    assert data["schema_version"] == "1.2"
    tf = data["tracked_files"]["settings"]
    # A plain PINNED span is behavior-equivalent at 1.2, so it survives as a
    # span (the reverse does not re-create the legacy preserve_user_keys list).
    assert any(s["anchor"] == "editor.fontSize" for s in tf["spans"])


def test_roundtrip_deep_key_untranslates(tmp_path: Path) -> None:
    """A deep=True span untranslates back to preserve_user_keys_deep."""
    (tmp_path / "setforge.yaml").write_text(
        _FLOOR + "schema_version: '1.2'\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: settings.yaml\n"
        "    dst: ~/settings.yaml\n"
        "    preserve_user_keys_deep:\n"
        "      - editor\n",
        encoding="utf-8",
    )
    roots = _roots(tmp_path)
    fwd = Contract20Migration()
    fwd.apply(roots=roots)
    fwd.reverse.apply(roots=roots)
    tf = _load(tmp_path / "setforge.yaml")["tracked_files"]["settings"]
    assert list(tf["preserve_user_keys_deep"]) == ["editor"]
    # The 2.0-exclusive deep span is gone (untranslated, not left dangling).
    assert "spans" not in tf or all(s.get("deep") is not True for s in tf["spans"])


def test_roundtrip_section_untranslates_with_mode(tmp_path: Path) -> None:
    """A section span with capture_mode untranslates to preserve_user_sections+mode."""
    (tmp_path / "doc.md").write_text(
        "<!-- setforge:user-section start shared notes -->\n"
        "body\n"
        "<!-- setforge:user-section end shared notes -->\n",
        encoding="utf-8",
    )
    (tmp_path / "setforge.yaml").write_text(
        _FLOOR + "schema_version: '1.2'\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/doc.md\n"
        "    preserve_user_sections: true\n"
        "    preserve_user_sections_mode: strip\n",
        encoding="utf-8",
    )
    roots = _roots(tmp_path)
    fwd = Contract20Migration()
    fwd.apply(roots=roots)
    fwd.reverse.apply(roots=roots)
    tf = _load(tmp_path / "setforge.yaml")["tracked_files"]["doc"]
    assert tf["preserve_user_sections"] is True
    assert tf["preserve_user_sections_mode"] == "strip"


def test_reverse_leaves_native_plain_span_untouched(tmp_path: Path) -> None:
    """A natively-authored plain span (no 2.0 attr) survives the reverse intact."""
    (tmp_path / "setforge.yaml").write_text(
        "schema_version: '2.0'\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: settings.yaml\n"
        "    dst: ~/settings.yaml\n"
        "    spans:\n"
        "      - anchor: editor.fontSize\n"
        "        kind: forked\n"
        "        semantics: shared\n",
        encoding="utf-8",
    )
    roots = _roots(tmp_path)
    Contract20Migration().reverse.apply(roots=roots)
    tf = _load(tmp_path / "setforge.yaml")["tracked_files"]["settings"]
    span = tf["spans"][0]
    assert span["anchor"] == "editor.fontSize"
    assert span["kind"] == "forked"
    assert span["semantics"] == "shared"
    # No legacy preserve_* fields re-created for a plain span.
    assert "preserve_user_keys" not in tf
    assert "preserve_user_keys_deep" not in tf
    assert "preserve_user_sections" not in tf


def test_validate_registry_passes_with_contract_migration() -> None:
    """The forward/reverse pair has a correctly-swapped reverse (registry-clean)."""
    fwd = Contract20Migration()
    rev = fwd.reverse
    assert rev.from_version == fwd.to_version
    assert rev.to_version == fwd.from_version
    # reverse.reverse is the forward direction again.
    assert rev.reverse.from_version == fwd.from_version
    assert rev.reverse.to_version == fwd.to_version
