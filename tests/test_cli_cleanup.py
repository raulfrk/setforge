from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path

import pytest
from rich.console import Console
from ruamel.yaml import YAML
from typer.testing import CliRunner

from setforge import binaries as binaries_mod
from setforge.cli import app
from setforge.cli import cleanup as cleanup_mod
from setforge.local_config import LocalConfig
from setforge.ownership import OwnershipStore
from setforge.provision.ownership import (
    PackageAction,
    decide_package,
    package_resource_id,
    publish_claim_locked,
)
from setforge.provision.protocol import (
    Identity,
    ObservationOrigin,
    PackageObservation,
    ProvisionItem,
)
from setforge.provision.receipt import ReceiptStore


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def confine_root(tmp_path: Path) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    return root


def _ident(key: str) -> Identity:
    return Identity(key=key, display=key)


def _write_binary(confine_root: Path, name: str) -> Path:
    binpath = confine_root / ".local" / "bin" / name
    binpath.parent.mkdir(parents=True, exist_ok=True)
    binpath.write_text("#!/bin/sh\n", encoding="utf-8")
    return binpath


def test_discovery_lists_undeclared_only(tmp_path: Path, confine_root: Path) -> None:
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


def test_discovery_refuses_typed_receipt_without_matching_claim(
    tmp_path: Path, confine_root: Path
) -> None:
    receipts = ReceiptStore(tmp_path / "receipts")
    identity = _ident("gone")
    path = _write_binary(confine_root, "gone")
    receipts.record(identity, version="1", checksum=None, path=path, provider="cargo")
    items = cleanup_mod.discover_cleanup_items(
        receipts,
        declared=set(),
        console=Console(),
        ownership_store=OwnershipStore(tmp_path / "ownership"),
    )
    assert len(items) == 1
    assert items[0].managed is False
    with pytest.raises(cleanup_mod.ConfinementError):
        cleanup_mod.delete_provisioned(
            receipts, items[0], confine_root=confine_root, console=Console()
        )
    assert path.exists()


def test_discovery_accepts_exact_owned_typed_receipt(
    tmp_path: Path, confine_root: Path
) -> None:
    owner = uuid.uuid4()
    receipts = ReceiptStore(tmp_path / "receipts")
    identity = _ident("gone")
    path = _write_binary(confine_root, "gone")
    receipts.record(identity, version="1", checksum=None, path=path, provider="cargo")
    observation = PackageObservation(
        identity,
        ObservationOrigin.CURRENT_RECEIPT,
        version="1",
        locator=str(path),
    )
    item = ProvisionItem(type="cargo", identity=identity)
    ownership = OwnershipStore(tmp_path / "ownership")
    decision = decide_package(item, observation, None, owner_id=owner)
    assert decision.action is PackageAction.ADOPT
    with cleanup_mod.mutation_locks(resources=True):
        publish_claim_locked(
            ownership,
            decision,
            owner_id=owner,
            declaration_ref="packages.gone",
            acquisition="adopted-external",
        )
    items = cleanup_mod.discover_cleanup_items(
        receipts,
        declared=set(),
        console=Console(),
        ownership_store=ownership,
        owner_id=owner,
    )
    assert len(items) == 1
    assert items[0].managed is True


def test_declared_provider_does_not_hide_same_key_other_provider_receipt(
    tmp_path: Path, confine_root: Path
) -> None:
    receipts = ReceiptStore(tmp_path / "receipts")
    identity = _ident("tool")
    cargo_path = _write_binary(confine_root, "cargo-tool")
    go_path = _write_binary(confine_root, "go-tool")
    receipts.record(
        identity, version="1", checksum=None, path=cargo_path, provider="cargo"
    )
    receipts.record(identity, version="1", checksum=None, path=go_path, provider="go")
    cargo_resource = package_resource_id(ProvisionItem(type="cargo", identity=identity))

    items = cleanup_mod.discover_cleanup_items(
        receipts,
        declared={identity},
        declared_resources=frozenset({cargo_resource}),
        console=Console(),
        ownership_store=OwnershipStore(tmp_path / "ownership"),
    )

    assert [(item.provider, item.identity.key) for item in items] == [("go", "tool")]


def test_bundle_declared_package_is_not_offered_for_cleanup(
    tmp_path: Path, confine_root: Path
) -> None:
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files: {}\n"
        "bundles:\n"
        "  tools:\n"
        "    components:\n"
        "      - id: tool\n"
        "        cargo: {crate: tool}\n"
        "profiles:\n"
        "  p: {bundles: [tools]}\n",
        encoding="utf-8",
    )
    identity = _ident("tool")
    receipts = ReceiptStore(tmp_path / "receipts")
    receipts.record(
        identity,
        version="1",
        checksum=None,
        path=_write_binary(confine_root, "tool"),
        provider="cargo",
    )

    declared = cleanup_mod._resolve_declared(config, "p")
    declared_resources = cleanup_mod._resolve_declared_resources(config, "p")
    items = cleanup_mod.discover_cleanup_items(
        receipts,
        declared=declared,
        declared_resources=declared_resources,
        console=Console(),
        ownership_store=OwnershipStore(tmp_path / "ownership"),
    )

    assert items == []


def test_discovery_includes_owned_ambient_package_without_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = uuid.uuid4()
    identity = _ident("ripgrep")
    observation = PackageObservation(
        identity, ObservationOrigin.EXTERNAL, version="14.1.0", source="crates.io"
    )
    ownership = OwnershipStore(tmp_path / "ownership")
    decision = decide_package(
        ProvisionItem(type="cargo", identity=identity),
        observation,
        None,
        owner_id=owner,
    )
    with cleanup_mod.mutation_locks(resources=True):
        publish_claim_locked(
            ownership,
            decision,
            owner_id=owner,
            declaration_ref="packages.ripgrep",
            acquisition="adopted-external",
        )

    class _Provider:
        def probe(self) -> set[Identity]:
            return {identity}

        def observations(
            self, installed: set[Identity]
        ) -> tuple[PackageObservation, ...]:
            assert installed == {identity}
            return (observation,)

    monkeypatch.setattr(cleanup_mod, "build", lambda _item: _Provider())
    items = cleanup_mod.discover_cleanup_items(
        ReceiptStore(tmp_path / "receipts"),
        declared=set(),
        console=Console(),
        ownership_store=ownership,
        owner_id=owner,
    )
    assert [(item.provider, item.identity.key, item.managed) for item in items] == [
        ("cargo", "ripgrep", True)
    ]


def test_apply_removes_owned_ambient_package_then_releases_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    owner = uuid.uuid4()
    identity = _ident("ripgrep")
    observation = PackageObservation(
        identity, ObservationOrigin.EXTERNAL, version="14.1.0", source="crates.io"
    )
    decision = decide_package(
        ProvisionItem(type="cargo", identity=identity),
        observation,
        None,
        owner_id=owner,
    )
    with cleanup_mod.mutation_locks(resources=True):
        claim = publish_claim_locked(
            OwnershipStore(),
            decision,
            owner_id=owner,
            declaration_ref="packages.ripgrep",
            acquisition="adopted-external",
        )
    installed = {identity}

    class _Provider:
        def probe(self) -> set[Identity]:
            return set(installed)

        def observations(
            self, current: set[Identity]
        ) -> tuple[PackageObservation, ...]:
            return (observation,) if identity in current else ()

        def uninstall_one(self, removed: Identity) -> None:
            installed.remove(removed)

    monkeypatch.setattr(cleanup_mod, "build", lambda _item: _Provider())
    monkeypatch.setattr(
        cleanup_mod, "_pick_action", lambda _item: cleanup_mod.CleanupAction.DELETE
    )
    item = cleanup_mod.CleanupItem(
        identity=identity,
        path=None,
        provider="cargo",
        owner_id=owner,
        claim_generation=claim.generation,
    )
    cleanup_mod._apply_cleanup(
        "p", [item], ReceiptStore(tmp_path / "receipts"), Console()
    )
    released = OwnershipStore().read(decision.resource_id)
    assert installed == set()
    assert released is not None
    assert released.authority.value == "none"


def test_marking_managed_package_orphan_releases_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    owner = uuid.uuid4()
    identity = _ident("ripgrep")
    observation = PackageObservation(
        identity, ObservationOrigin.EXTERNAL, version="14.1.0"
    )
    decision = decide_package(
        ProvisionItem(type="cargo", identity=identity),
        observation,
        None,
        owner_id=owner,
    )
    with cleanup_mod.mutation_locks(resources=True):
        claim = publish_claim_locked(
            OwnershipStore(),
            decision,
            owner_id=owner,
            declaration_ref="packages.ripgrep",
            acquisition="adopted-external",
        )
    monkeypatch.setattr(
        cleanup_mod,
        "_pick_action",
        lambda _item: cleanup_mod.CleanupAction.MARK_ORPHAN,
    )
    cleanup_mod._apply_cleanup(
        "p",
        [
            cleanup_mod.CleanupItem(
                identity=identity,
                path=None,
                provider="cargo",
                owner_id=owner,
                claim_generation=claim.generation,
            )
        ],
        ReceiptStore(tmp_path / "receipts"),
        Console(),
    )
    released = OwnershipStore().read(decision.resource_id)
    assert released is not None
    assert released.authority.value == "none"
    assert "ripgrep" in cleanup_mod.load_ignored_provisioned()


def test_discovery_skips_corrupt_receipt_not_fatal(
    tmp_path: Path, confine_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    receipts = tmp_path / "receipts"
    receipts.mkdir(parents=True)
    good_bin = _write_binary(confine_root, "good")
    store = ReceiptStore(receipts)
    store.record(_ident("good"), version="1", checksum=None, path=good_bin)
    (receipts / "deadbeef.json").write_text("{not json", encoding="utf-8")

    items = cleanup_mod.discover_cleanup_items(
        store, declared=set(), console=Console(stderr=True)
    )
    assert {it.identity.key for it in items} == {"good"}
    err = capsys.readouterr().err
    assert "corrupt" in err.lower()


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


def test_apply_delete_holds_profile_lock(
    tmp_path: Path, confine_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_apply_cleanup`'s DELETE branch must enter profile_lock BEFORE it
    writes the transition / unlinks the binary — like every other mutating
    verb. Fails against the old unlocked behavior: no "enter" precedes
    "write_transition".
    """
    import contextlib

    from setforge import locking

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cleanup_mod, "_confinement_root", lambda: confine_root)
    monkeypatch.setattr(
        cleanup_mod, "_pick_action", lambda _item: cleanup_mod.CleanupAction.DELETE
    )

    store = ReceiptStore(tmp_path / "receipts")
    binpath = _write_binary(confine_root, "gone")
    identity = _ident("gone")
    store.record(identity, version="1", checksum=None, path=binpath, provider="cargo")
    owner = uuid.uuid4()
    observation = PackageObservation(
        identity,
        ObservationOrigin.CURRENT_RECEIPT,
        version="1",
        locator=str(binpath),
    )
    decision = decide_package(
        ProvisionItem(type="cargo", identity=identity),
        observation,
        None,
        owner_id=owner,
    )
    with locking.mutation_locks(resources=True):
        claim = publish_claim_locked(
            OwnershipStore(),
            decision,
            owner_id=owner,
            declaration_ref="packages.gone",
            acquisition="adopted-external",
        )
    item = cleanup_mod.CleanupItem(
        identity=identity,
        path=binpath,
        provider="cargo",
        owner_id=owner,
        claim_generation=claim.generation,
    )

    class _Provider:
        def probe(self) -> set[Identity]:
            return {identity} if binpath.exists() else set()

        def observations(
            self, installed: set[Identity]
        ) -> tuple[PackageObservation, ...]:
            return (observation,) if installed else ()

    monkeypatch.setattr(cleanup_mod, "build", lambda _item: _Provider())

    events: list[str] = []
    real_locks = locking.mutation_locks

    @contextlib.contextmanager
    def _recording_locks(**kwargs: object):
        events.append("enter")
        with real_locks(**kwargs):  # type: ignore[arg-type]
            try:
                yield
            finally:
                events.append("exit")

    real_write = cleanup_mod.transitions.write_transition

    def _spy_write(*args: object, **kwargs: object) -> Path:
        events.append("write_transition")
        return real_write(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cleanup_mod, "mutation_locks", _recording_locks)
    monkeypatch.setattr(cleanup_mod.transitions, "write_transition", _spy_write)

    cleanup_mod._apply_cleanup("p", [item], store, Console())

    assert "enter" in events, "cleanup delete never acquired the profile lock"
    assert "write_transition" in events, "cleanup delete never wrote a transition"
    assert events.index("enter") < events.index("write_transition"), (
        f"lock must be held before mutating; order: {events}"
    )
    assert events[-1] == "exit", f"lock must be released last; order: {events}"
    assert not binpath.exists()


def test_delete_path_none_drops_only_receipt(
    tmp_path: Path, confine_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = ReceiptStore(tmp_path / "receipts")
    store.record(_ident("old"), version="1", checksum=None, path=None)
    item = cleanup_mod.CleanupItem(identity=_ident("old"), path=None)

    cleanup_mod.delete_provisioned(
        store, item, confine_root=confine_root, console=Console(stderr=True)
    )
    assert store.installed() == set()
    err = capsys.readouterr().err
    assert "no recorded path" in err


def test_delete_missing_binary_warns_and_reaps_receipt(
    tmp_path: Path, confine_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = ReceiptStore(tmp_path / "receipts")
    binpath = confine_root / ".local" / "bin" / "vanished"
    store.record(_ident("vanished"), version="1", checksum=None, path=binpath)
    assert not binpath.exists()
    item = cleanup_mod.CleanupItem(identity=_ident("vanished"), path=binpath)

    cleanup_mod.delete_provisioned(
        store, item, confine_root=confine_root, console=Console(stderr=True)
    )
    assert store.installed() == set()
    err = capsys.readouterr().err
    assert "vanished" in err or "missing" in err.lower()


def test_delete_refuses_path_escaping_confinement(
    tmp_path: Path, confine_root: Path
) -> None:
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
    assert escaped.exists()
    assert store.installed() == {_ident("evil")}


def test_delete_refuses_dotdot_escaping_confinement(
    tmp_path: Path, confine_root: Path
) -> None:
    outside = tmp_path / "outside" / "target.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("keep me\n", encoding="utf-8")
    escaped = confine_root / ".." / "outside" / "target.txt"
    assert escaped.is_relative_to(confine_root)

    store = ReceiptStore(tmp_path / "receipts")
    store.record(_ident("evil"), version="1", checksum=None, path=escaped)
    item = cleanup_mod.CleanupItem(identity=_ident("evil"), path=escaped)

    with pytest.raises(cleanup_mod.ConfinementError):
        cleanup_mod.delete_provisioned(
            store, item, confine_root=confine_root, console=Console()
        )
    assert outside.exists()
    assert store.installed() == {_ident("evil")}


def test_delete_refuses_symlinked_parent_escaping_confinement(
    tmp_path: Path, confine_root: Path
) -> None:
    # Parent is a symlink that escapes confinement: the path string stays under
    # confine_root (a lexical is_relative_to would pass), but resolving the
    # parent's symlink reveals the escape. Guards realpath/resolve of the parent.
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    victim = outside / "target.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    link_dir = confine_root / "link"
    link_dir.symlink_to(outside, target_is_directory=True)
    escaped = link_dir / "target.txt"
    assert escaped.is_relative_to(confine_root)

    store = ReceiptStore(tmp_path / "receipts")
    store.record(_ident("evil"), version="1", checksum=None, path=escaped)
    item = cleanup_mod.CleanupItem(identity=_ident("evil"), path=escaped)

    with pytest.raises(cleanup_mod.ConfinementError):
        cleanup_mod.delete_provisioned(
            store, item, confine_root=confine_root, console=Console()
        )
    assert victim.exists()
    assert store.installed() == {_ident("evil")}


def test_delete_uses_lstat_never_dereferences_symlink(
    tmp_path: Path, confine_root: Path
) -> None:
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
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "DO NOT DELETE\n"


def test_mark_orphan_keeps_binary_zero_binary_writes(
    tmp_path: Path, confine_root: Path
) -> None:
    store = ReceiptStore(tmp_path / "receipts")
    binpath = _write_binary(confine_root, "keepme")
    before = binpath.read_bytes()
    store.record(_ident("keepme"), version="1", checksum=None, path=binpath)

    cleanup_mod.mark_orphan(_ident("keepme"), console=Console())

    assert binpath.exists()
    assert binpath.read_bytes() == before
    assert store.installed() == {_ident("keepme")}
    assert "keepme" in cleanup_mod.load_ignored_provisioned()


def test_mark_orphan_drops_from_flagged_set(tmp_path: Path, confine_root: Path) -> None:
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


def _write_cleanup_yaml(tmp_path: Path) -> Path:
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
    receipts = tmp_path / "receipts"
    store = ReceiptStore(receipts)
    binpath = _write_binary(confine_root, "gone")
    store.record(_ident("gone"), version="1", checksum=None, path=binpath)
    monkeypatch.setattr(cleanup_mod, "_receipt_store", lambda: ReceiptStore(receipts))
    monkeypatch.setattr(cleanup_mod, "_confinement_root", lambda: confine_root)

    cfg = _write_cleanup_yaml(tmp_path)
    result = runner.invoke(app, ["cleanup", "--profile", "p", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert binpath.exists()
    assert store.installed() == {_ident("gone")}


def test_receipt_remove_deletes_then_idempotent(tmp_path: Path) -> None:
    store = ReceiptStore(tmp_path)
    store.record(_ident("a"), version="1", checksum=None, path=None)
    assert store.installed() == {_ident("a")}
    store.remove(_ident("a"))
    assert store.installed() == set()
    store.remove(_ident("a"))


def test_cleanup_button_bar_constructs_without_crashing() -> None:
    # Other tests inject the choice callback; this guards the real widget build once.
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from setforge.ui.widgets import Button, button_bar

    with create_pipe_input() as pipe:
        pipe.send_text("\r")
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


def _cleanup_module_ast() -> ast.Module:
    src = Path(cleanup_mod.__file__).read_text(encoding="utf-8")
    return ast.parse(src)


def test_no_unlink_missing_ok_in_cleanup_module() -> None:
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


def _resolve_receiver_ok(recv: ast.expr) -> bool:
    # `.resolve()` is only safe on the confinement root or on a `.parent` —
    # never on the receipt path / unlink target (whose final component is the
    # symlink we unlink, not follow). Allow `confine_root.resolve()` and any
    # `<expr>.parent.resolve()`; forbid everything else.
    if isinstance(recv, ast.Name) and recv.id == "confine_root":
        return True
    return isinstance(recv, ast.Attribute) and recv.attr == "parent"


def test_no_resolve_on_unlink_target_in_cleanup_delete_helpers() -> None:
    # `.resolve()` on a symlink before `.unlink()` torches the target; forbidden
    # on the receipt path / unlink target in the delete helpers. Resolving the
    # confinement root or the PARENT directory (for the containment check) is
    # allowed — those are never the thing we unlink.
    helper_names = {"delete_provisioned", "_confined_unlink", "_lstat_safe"}
    for node in ast.walk(_cleanup_module_ast()):
        if not isinstance(node, ast.FunctionDef) or node.name not in helper_names:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "resolve"
                and not _resolve_receiver_ok(inner.func.value)
            ):
                raise AssertionError(
                    f".resolve() on the unlink target forbidden in {node.name} "
                    f"at line {inner.lineno}"
                )


def test_local_yaml_shape_for_provision_ignore(
    tmp_path: Path, confine_root: Path
) -> None:
    cleanup_mod.mark_orphan(_ident("some_tool"), console=Console())
    payload = YAML(typ="safe").load(
        binaries_mod.LOCAL_CONFIG_PATH.read_text(encoding="utf-8")
    )
    assert payload == {"provision_ignore": ["some_tool"]}
    json.dumps(list(cleanup_mod.load_ignored_provisioned()))


def test_mark_orphan_output_validates_under_local_config(
    tmp_path: Path, confine_root: Path
) -> None:
    # A ruamel-only check missed an undeclared key that broke `validate --all`.
    cleanup_mod.mark_orphan(_ident("some_tool"), console=Console())
    payload = YAML(typ="safe").load(
        binaries_mod.LOCAL_CONFIG_PATH.read_text(encoding="utf-8")
    )
    model = LocalConfig.model_validate(payload)
    assert model.provision_ignore == ["some_tool"]
