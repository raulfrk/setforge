"""Up-migration tests for the 1.2 -> 2.0 CONTRACT migration.

The migration translates every surviving legacy preserve_* field into spans,
drops the legacy keys, and stamps ``schema_version: "2.0"``. It is cross-doc:
it also retires the ``local.yaml`` ``preserve_user_keys`` overlay into span
overlays. A destructive drop is gated behind an operator-declared
``minimum_version >= 2.0`` floor; below the floor it refuses cleanly and mutates
nothing. The apply is all-or-nothing: a full in-memory plan for every affected
path is built + validated before any atomic write.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from setforge.errors import ConfigError
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


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


_FLOOR = 'minimum_version: "2.0"\n'


def test_translate_shallow_keys_to_pinned_host_local_spans(tmp_path: Path) -> None:
    """preserve_user_keys -> one PINNED host-local span per path."""
    _write(
        tmp_path / "setforge.yaml",
        _FLOOR + "schema_version: '1.2'\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: settings.yaml\n"
        "    dst: ~/settings.yaml\n"
        "    preserve_user_keys:\n"
        "      - editor.fontSize\n"
        "      - editor.theme\n",
    )
    Contract20Migration().apply(roots=_roots(tmp_path))
    data = _load(tmp_path / "setforge.yaml")
    tf = data["tracked_files"]["settings"]
    assert "preserve_user_keys" not in tf
    spans = tf["spans"]
    assert {s["anchor"] for s in spans} == {"editor.fontSize", "editor.theme"}
    for s in spans:
        assert s["kind"] == "pinned"
        assert s["semantics"] == "host-local"
    assert data["schema_version"] == "2.0"


def test_translate_deep_keys_to_pinned_deep_spans(tmp_path: Path) -> None:
    """preserve_user_keys_deep -> PINNED host-local span with deep=True."""
    _write(
        tmp_path / "setforge.yaml",
        _FLOOR + "schema_version: '1.2'\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: settings.yaml\n"
        "    dst: ~/settings.yaml\n"
        "    preserve_user_keys_deep:\n"
        "      - editor\n",
    )
    Contract20Migration().apply(roots=_roots(tmp_path))
    tf = _load(tmp_path / "setforge.yaml")["tracked_files"]["settings"]
    assert "preserve_user_keys_deep" not in tf
    span = tf["spans"][0]
    assert span["anchor"] == "editor"
    assert span["kind"] == "pinned"
    assert span["deep"] is True


def test_translate_sections_enumerates_from_tracked_markers(tmp_path: Path) -> None:
    """preserve_user_sections:true -> one section span per marked section."""
    src = tmp_path / "doc.md"
    _write(
        src,
        "<!-- setforge:user-section start shared notes -->\n"
        "body\n"
        "<!-- setforge:user-section end shared notes -->\n",
    )
    _write(
        tmp_path / "setforge.yaml",
        _FLOOR + "schema_version: '1.2'\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/doc.md\n"
        "    preserve_user_sections: true\n"
        "    preserve_user_sections_mode: strip\n",
    )
    Contract20Migration().apply(roots=_roots(tmp_path))
    tf = _load(tmp_path / "setforge.yaml")["tracked_files"]["doc"]
    assert "preserve_user_sections" not in tf
    assert "preserve_user_sections_mode" not in tf
    span = tf["spans"][0]
    assert span["semantics"] == "shared"
    assert span["capture_mode"] == "strip"


def test_sections_no_markers_drops_flag_emits_no_span(tmp_path: Path) -> None:
    """preserve_user_sections:true with no markers in src -> drop flag, no span."""
    _write(tmp_path / "doc.md", "plain content, no markers\n")
    _write(
        tmp_path / "setforge.yaml",
        _FLOOR + "schema_version: '1.2'\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/doc.md\n"
        "    preserve_user_sections: true\n",
    )
    Contract20Migration().apply(roots=_roots(tmp_path))
    tf = _load(tmp_path / "setforge.yaml")["tracked_files"]["doc"]
    assert "preserve_user_sections" not in tf
    assert "spans" not in tf or len(tf.get("spans", [])) == 0


def test_local_yaml_overlay_translated(tmp_path: Path) -> None:
    """local.yaml preserve_user_keys overlay -> span overlays; flag retired."""
    home = tmp_path / "home"
    local_dir = home / ".config" / "setforge"
    local_dir.mkdir(parents=True)
    local_yaml = local_dir / "local.yaml"
    _write(
        local_yaml,
        "tracked_files:\n"
        "  settings:\n"
        "    preserve_user_keys:\n"
        "      add:\n"
        "        - editor.fontSize\n",
    )
    _write(
        tmp_path / "setforge.yaml",
        _FLOOR + "schema_version: '1.2'\n",
    )
    roots = _roots(tmp_path)
    migration = Contract20Migration()
    assert local_yaml in migration.affected_paths(roots=roots)
    migration.apply(roots=roots)
    tf = _load(local_yaml)["tracked_files"]["settings"]
    assert "preserve_user_keys" not in tf
    spans = tf["spans"]
    assert any(s["anchor"] == "editor.fontSize" for s in spans)


def test_idempotent_replay(tmp_path: Path) -> None:
    """A second apply on an already-migrated config is a no-op (converges)."""
    _write(
        tmp_path / "setforge.yaml",
        _FLOOR + "schema_version: '1.2'\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: settings.yaml\n"
        "    dst: ~/settings.yaml\n"
        "    preserve_user_keys:\n"
        "      - editor.fontSize\n",
    )
    roots = _roots(tmp_path)
    Contract20Migration().apply(roots=roots)
    first = (tmp_path / "setforge.yaml").read_text(encoding="utf-8")
    Contract20Migration().apply(roots=roots)
    second = (tmp_path / "setforge.yaml").read_text(encoding="utf-8")
    assert first == second


def test_below_floor_refuses_and_mutates_nothing(tmp_path: Path) -> None:
    """A minimum_version below 2.0 (or absent) refuses; files untouched."""
    original = (
        "schema_version: '1.2'\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: settings.yaml\n"
        "    dst: ~/settings.yaml\n"
        "    preserve_user_keys:\n"
        "      - editor.fontSize\n"
    )
    _write(tmp_path / "setforge.yaml", original)
    with pytest.raises(ConfigError, match=r"2\.0"):
        Contract20Migration().apply(roots=_roots(tmp_path))
    assert (tmp_path / "setforge.yaml").read_text(encoding="utf-8") == original


def test_malformed_non_mapping_root_raises_config_error(tmp_path: Path) -> None:
    """A non-mapping setforge.yaml root raises ConfigError, not a bare TypeError."""
    _write(tmp_path / "setforge.yaml", "- just\n- a\n- list\n")
    with pytest.raises(ConfigError):
        Contract20Migration().apply(roots=_roots(tmp_path))


def test_all_or_nothing_rollback_on_mid_apply_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An injected failure mid-apply leaves every affected file untouched."""
    home = tmp_path / "home"
    local_dir = home / ".config" / "setforge"
    local_dir.mkdir(parents=True)
    local_yaml = local_dir / "local.yaml"
    local_original = (
        "tracked_files:\n"
        "  settings:\n"
        "    preserve_user_keys:\n"
        "      add:\n"
        "        - editor.fontSize\n"
    )
    _write(local_yaml, local_original)
    cfg_original = (
        _FLOOR + "schema_version: '1.2'\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: settings.yaml\n"
        "    dst: ~/settings.yaml\n"
        "    preserve_user_keys:\n"
        "      - editor.fontSize\n"
    )
    _write(tmp_path / "setforge.yaml", cfg_original)

    import setforge.migrations._contract_2_0 as mod

    calls = {"n": 0}
    real_write = mod.atomic_write_yaml

    def _boom(path: Path, data: object) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("injected mid-apply failure")
        real_write(path, data)

    monkeypatch.setattr(mod, "atomic_write_yaml", _boom)
    with pytest.raises(OSError, match="injected"):
        Contract20Migration().apply(roots=_roots(tmp_path))
    assert (tmp_path / "setforge.yaml").read_text(encoding="utf-8") == cfg_original
    assert local_yaml.read_text(encoding="utf-8") == local_original
