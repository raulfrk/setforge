"""Regression tests for the down-migration shallow-key disposition audit fix.

A tracked_file mixing ``preserve_user_keys`` (shallow) AND
``preserve_user_keys_deep`` round-trips 1.2 -> 2.0 -> 1.2. The forward
migration translates both into PINNED spans and stamps a single
``disposition: forked``. The reverse untranslates the deep span back to
``preserve_user_keys_deep`` but KEEPS the shallow span as a plain span — and
that kept span is inert at install unless ``disposition: forked`` survives.

The bug: the reverse dropped the forked disposition whenever any deep key was
untranslated, even when a kept shallow span still needed it. These tests pin
both the mixed-key behavior and the single-shallow-key invariant.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

from setforge.migrations import MigrationRoots
from setforge.migrations._contract_2_0 import Contract20Migration

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


def _roundtrip(tmp_path: Path) -> dict:
    roots = _roots(tmp_path)
    fwd = Contract20Migration()
    fwd.apply(roots=roots)
    fwd.reverse.apply(roots=roots)
    return _load(tmp_path / "setforge.yaml")


def test_mixed_shallow_and_deep_keeps_disposition_for_kept_shallow_span(
    tmp_path: Path,
) -> None:
    """1.2 -> 2.0 -> 1.2 on a file mixing shallow + deep keys keeps the shallow
    span functional: the kept PINNED span needs ``disposition: forked`` to be
    consumed at install, so the reverse must NOT drop it.
    """
    (tmp_path / "setforge.yaml").write_text(
        _FLOOR + "schema_version: '1.2'\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: settings.yaml\n"
        "    dst: ~/settings.yaml\n"
        "    preserve_user_keys:\n"
        "      - editor.fontSize\n"
        "    preserve_user_keys_deep:\n"
        "      - editor\n",
        encoding="utf-8",
    )
    data = _roundtrip(tmp_path)
    tf = data["tracked_files"]["settings"]
    # The deep key round-trips back to its legacy field.
    assert list(tf["preserve_user_keys_deep"]) == ["editor"]
    # The shallow span is kept (not untranslated)...
    assert any(s["anchor"] == "editor.fontSize" for s in tf["spans"])
    # ...and its disposition survives so it is NOT inert at install. This is the
    # load-bearing invariant the bug violated (it dropped the disposition).
    assert tf.get("disposition") == "forked"


def test_shallow_only_retains_disposition(tmp_path: Path) -> None:
    """A shallow-only round-trip retains ``disposition: forked`` (the condition
    that keeps the kept PINNED span functional)."""
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
    data = _roundtrip(tmp_path)
    tf = data["tracked_files"]["settings"]
    assert any(s["anchor"] == "editor.fontSize" for s in tf["spans"])
    assert tf.get("disposition") == "forked"


def test_deep_only_drops_disposition(tmp_path: Path) -> None:
    """A deep-only round-trip leaves no kept structural span, so the forked
    disposition is correctly dropped (it has no altitude at 1.2)."""
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
    data = _roundtrip(tmp_path)
    tf = data["tracked_files"]["settings"]
    assert list(tf["preserve_user_keys_deep"]) == ["editor"]
    assert "disposition" not in tf
