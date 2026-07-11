"""Unit tests for the ``setforge cleanup`` provisioned-binary wizard.

Covers :mod:`setforge.cli.cleanup` — receipt-scoped discovery of
undeclared provisioned binaries, the confined delete path (binary +
receipt), the mark-orphan bookkeeping path (zero filesystem writes to
the binary), the dry-run default (no auto-prune), and the safety
invariants (confinement, corrupt-receipt skip, missing-binary warn,
path=None receipt). Mirrors ``tests/test_orphans.py``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from rich.console import Console
from ruamel.yaml import YAML
from typer.testing import CliRunner

from setforge import binaries as binaries_mod
from setforge.cli import app
from setforge.cli import cleanup as cleanup_mod
from setforge.provision.protocol import Identity
from setforge.provision.receipt import ReceiptStore

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def confine_root(tmp_path: Path) -> Path:
    """A confinement root under which fake binaries live."""
    root = tmp_path / "home"
    root.mkdir()
    return root


def _ident(key: str) -> Identity:
    return Identity(key=key, display=key)


def _write_binary(confine_root: Path, name: str) -> Path:
    """Create a fake installed binary file under the confinement root."""
    binpath = confine_root / ".local" / "bin" / name
    binpath.parent.mkdir(parents=True, exist_ok=True)
    binpath.write_text("#!/bin/sh\n", encoding="utf-8")
    return binpath


# ---------------------------------------------------------------------------
# Discovery: receipt-scoped, undeclared-only
# ---------------------------------------------------------------------------


def test_discovery_lists_undeclared_only(tmp_path: Path, confine_root: Path) -> None:
    """An item still in the declared set is never offered for delete; an
    undeclared receipt is."""
    store = ReceiptStore(tmp_path / "receipts")
    declared_bin = _write_binary(confine_root, "kept")
    undeclared_bin = _write_binary(confine_root, "gone")
    store.record(_ident("kept"), version="1", checksum=None, path=declared_bin)
    store.record(_ident("gone"), version="1", checksum=None, path=undeclared_bin)

    items = cleanup_mod.discover_cleanup_items(
        store, declared={_ident("kept")}, console=Console()
    )
    keys = {it.identity.key for it in items}
    assert keys == {"gone"}


def test_discovery_skips_corrupt_receipt_not_fatal(
    tmp_path: Path, confine_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corrupt receipt is named + skipped, discovery continues."""
    receipts = tmp_path / "receipts"
    receipts.mkdir(parents=True)
    good_bin = _write_binary(confine_root, "good")
    store = ReceiptStore(receipts)
    store.record(_ident("good"), version="1", checksum=None, path=good_bin)
    # A corrupt receipt file alongside the good one.
    (receipts / "deadbeef.json").write_text("{not json", encoding="utf-8")

    items = cleanup_mod.discover_cleanup_items(
        store, declared=set(), console=Console(stderr=True)
    )
    assert {it.identity.key for it in items} == {"good"}
    err = capsys.readouterr().err
    assert "corrupt" in err.lower()


# ---------------------------------------------------------------------------
# DELETE: confined unlink of binary + receipt
# ---------------------------------------------------------------------------


def test_delete_removes_binary_and_receipt_under_confinement(
    tmp_path: Path, confine_root: Path
) -> None:
    store = ReceiptStore(tmp_path / "receipts")
    binpath = _write_binary(confine_root, "gone")
    store.record(_ident("gone"), version="1", checksum=None, path=binpath)
    item = cleanup_mod.CleanupItem(identity=_ident("gone"), path=binpath)

    cleanup_mod.delete_provisioned(
        store, item, confine_root=confine_root, console=Console()
    )
    assert not binpath.exists()
    assert store.installed() == set()


def test_delete_path_none_drops_only_receipt(
    tmp_path: Path, confine_root: Path
) -> None:
    """An old receipt with path=None: skip the binary-unlink, drop the
    receipt + warn."""
    store = ReceiptStore(tmp_path / "receipts")
    store.record(_ident("old"), version="1", checksum=None, path=None)
    item = cleanup_mod.CleanupItem(identity=_ident("old"), path=None)

    cleanup_mod.delete_provisioned(
        store, item, confine_root=confine_root, console=Console()
    )
    assert store.installed() == set()


def test_delete_missing_binary_warns_and_reaps_receipt(
    tmp_path: Path, confine_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing binary → warn + continue, still reap the receipt (no raise,
    no missing_ok crutch)."""
    store = ReceiptStore(tmp_path / "receipts")
    binpath = confine_root / ".local" / "bin" / "vanished"
    store.record(_ident("vanished"), version="1", checksum=None, path=binpath)
    assert not binpath.exists()
    item = cleanup_mod.CleanupItem(identity=_ident("vanished"), path=binpath)

    cleanup_mod.delete_provisioned(
        store, item, confine_root=confine_root, console=Console(stderr=True)
    )
    assert store.installed() == set()  # receipt reaped despite missing binary
    err = capsys.readouterr().err
    assert "vanished" in err or "missing" in err.lower()


def test_delete_refuses_path_escaping_confinement(
    tmp_path: Path, confine_root: Path
) -> None:
    """A recorded path outside the confinement root is refused: no unlink,
    no receipt drop."""
    store = ReceiptStore(tmp_path / "receipts")
    escaped = tmp_path / "outside" / "evil"
    escaped.parent.mkdir(parents=True, exist_ok=True)
    escaped.write_text("keep me\n", encoding="utf-8")
    store.record(_ident("evil"), version="1", checksum=None, path=escaped)
    item = cleanup_mod.CleanupItem(identity=_ident("evil"), path=escaped)

    with pytest.raises(cleanup_mod.ConfinementError):
        cleanup_mod.delete_provisioned(
            store, item, confine_root=confine_root, console=Console()
        )
    assert escaped.exists()  # never unlinked
    assert store.installed() == {_ident("evil")}  # receipt untouched


def test_delete_uses_lstat_never_dereferences_symlink(
    tmp_path: Path, confine_root: Path
) -> None:
    """The recorded path is a symlink under confinement: unlink removes the
    LINK, never the target."""
    target = tmp_path / "real" / "important"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("DO NOT DELETE\n", encoding="utf-8")
    link = confine_root / ".local" / "bin" / "linky"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)

    store = ReceiptStore(tmp_path / "receipts")
    store.record(_ident("linky"), version="1", checksum=None, path=link)
    item = cleanup_mod.CleanupItem(identity=_ident("linky"), path=link)

    cleanup_mod.delete_provisioned(
        store, item, confine_root=confine_root, console=Console()
    )
    assert not link.exists()
    assert not link.is_symlink()
    assert target.exists()  # target untouched
    assert target.read_text(encoding="utf-8") == "DO NOT DELETE\n"


# ---------------------------------------------------------------------------
# MARK-ORPHAN: config-only bookkeeping, zero FS writes to the binary
# ---------------------------------------------------------------------------


def test_mark_orphan_keeps_binary_zero_binary_writes(
    tmp_path: Path, confine_root: Path
) -> None:
    """Mark-orphan keeps the binary + its receipt; it only appends to the
    host-local ignore list."""
    store = ReceiptStore(tmp_path / "receipts")
    binpath = _write_binary(confine_root, "keepme")
    before = binpath.read_bytes()
    store.record(_ident("keepme"), version="1", checksum=None, path=binpath)

    cleanup_mod.mark_orphan(_ident("keepme"), console=Console())

    assert binpath.exists()
    assert binpath.read_bytes() == before  # binary untouched
    assert store.installed() == {_ident("keepme")}  # receipt untouched
    assert "keepme" in cleanup_mod.load_ignored_provisioned()


def test_mark_orphan_drops_from_flagged_set(tmp_path: Path, confine_root: Path) -> None:
    """A marked-orphan item is excluded from the next discovery's flagged
    set (declared includes it only via the ignore list)."""
    store = ReceiptStore(tmp_path / "receipts")
    binpath = _write_binary(confine_root, "ign")
    store.record(_ident("ign"), version="1", checksum=None, path=binpath)
    cleanup_mod.mark_orphan(_ident("ign"), console=Console())

    ignored = cleanup_mod.load_ignored_provisioned()
    items = cleanup_mod.discover_cleanup_items(
        store, declared={_ident(k) for k in ignored}, console=Console()
    )
    assert {it.identity.key for it in items} == set()


def test_mark_orphan_is_idempotent(tmp_path: Path, confine_root: Path) -> None:
    cleanup_mod.mark_orphan(_ident("x"), console=Console())
    cleanup_mod.mark_orphan(_ident("x"), console=Console())
    assert list(cleanup_mod.load_ignored_provisioned()).count("x") == 1


# ---------------------------------------------------------------------------
# CLI: dry-run default makes no changes (no auto-prune)
# ---------------------------------------------------------------------------


def _write_cleanup_yaml(tmp_path: Path) -> Path:
    """Minimal setforge.yaml with no declared cargo binaries — so any
    recorded receipt is undeclared."""
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(
        "version: 1\ntracked_files: {}\nprofiles:\n  p:\n    tracked_files: []\n",
        encoding="utf-8",
    )
    return cfg


def test_cli_default_is_dry_run(
    runner: CliRunner,
    tmp_path: Path,
    confine_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--apply`` nothing is deleted (no auto-prune)."""
    receipts = tmp_path / "receipts"
    store = ReceiptStore(receipts)
    binpath = _write_binary(confine_root, "gone")
    store.record(_ident("gone"), version="1", checksum=None, path=binpath)
    monkeypatch.setattr(cleanup_mod, "_receipt_store", lambda: ReceiptStore(receipts))
    monkeypatch.setattr(cleanup_mod, "_confinement_root", lambda: confine_root)

    cfg = _write_cleanup_yaml(tmp_path)
    result = runner.invoke(app, ["cleanup", "--profile", "p", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert binpath.exists()  # CRITICAL: not deleted under dry-run
    assert store.installed() == {_ident("gone")}


# ---------------------------------------------------------------------------
# ReceiptStore.remove (the receipt-unlink verb)
# ---------------------------------------------------------------------------


def test_receipt_remove_deletes_then_idempotent(tmp_path: Path) -> None:
    store = ReceiptStore(tmp_path)
    store.record(_ident("a"), version="1", checksum=None, path=None)
    assert store.installed() == {_ident("a")}
    store.remove(_ident("a"))
    assert store.installed() == set()
    # Idempotent: removing again is a no-op, not a raise.
    store.remove(_ident("a"))


# ---------------------------------------------------------------------------
# Widget/theme construction guard (callback-injection bypasses real build)
# ---------------------------------------------------------------------------


def test_cleanup_button_bar_constructs_without_crashing() -> None:
    """Construct the real selection widget once — callback-injection in the
    other tests bypasses prompt_toolkit widget/theme construction, so this
    guards the style/build path directly."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from setforge.ui.widgets import Button, button_bar

    with create_pipe_input() as pipe:
        pipe.send_text("\r")  # Enter → select first button
        with create_app_session(input=pipe, output=DummyOutput()):
            result = button_bar(
                [
                    Button("delete", "delete"),
                    Button("mark-orphan", "orphan"),
                ],
                title="setforge cleanup",
                body="pick an action",
                initial=0,
            )
    assert result == "delete"


# ---------------------------------------------------------------------------
# Anti-pattern structural asserts (mirror tests/test_orphans.py)
# ---------------------------------------------------------------------------


def _cleanup_module_ast() -> ast.Module:
    src = Path(cleanup_mod.__file__).read_text(encoding="utf-8")
    return ast.parse(src)


def test_no_unlink_missing_ok_in_cleanup_module() -> None:
    """``unlink(missing_ok=True)`` swallows the missing-binary race; forbidden."""
    for node in ast.walk(_cleanup_module_ast()):
        if not isinstance(node, ast.Call):
            continue
        attr = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if attr != "unlink":
            continue
        for kw in node.keywords:
            if kw.arg == "missing_ok":
                raise AssertionError(
                    f"unlink(missing_ok=...) detected at line {node.lineno}"
                )


def test_no_rmtree_or_removedirs_in_cleanup_module() -> None:
    forbidden = {"rmtree", "removedirs"}
    for node in ast.walk(_cleanup_module_ast()):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in forbidden:
            raise AssertionError(
                f"forbidden recursive-delete call at line {node.lineno}"
            )


def test_no_resolve_in_cleanup_delete_helpers() -> None:
    """``.resolve()`` on a symlink before ``.unlink()`` torches the target;
    forbidden in the delete helpers."""
    helper_names = {"delete_provisioned", "_confined_unlink", "_lstat_safe"}
    for node in ast.walk(_cleanup_module_ast()):
        if not isinstance(node, ast.FunctionDef) or node.name not in helper_names:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and getattr(inner.func, "attr", None) == "resolve"
            ):
                raise AssertionError(
                    f".resolve() forbidden in {node.name} at line {inner.lineno}"
                )


def test_local_yaml_shape_for_provision_ignore(
    tmp_path: Path, confine_root: Path
) -> None:
    """mark_orphan writes a ``provision_ignore:`` list to local.yaml only."""
    cleanup_mod.mark_orphan(_ident("some_tool"), console=Console())
    payload = YAML(typ="safe").load(
        binaries_mod.LOCAL_CONFIG_PATH.read_text(encoding="utf-8")
    )
    assert payload == {"provision_ignore": ["some_tool"]}
    # And it round-trips as JSON-serializable plain data (no ruamel objects leak).
    json.dumps(list(cleanup_mod.load_ignored_provisioned()))
