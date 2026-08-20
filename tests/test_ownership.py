"""Durable ownership identity, ledger, migration, and checkout tests."""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from setforge import ownership as ownership_module
from setforge.errors import (
    CorruptOwnershipState,
    OwnershipCollisionError,
    OwnershipError,
    SetforgeError,
)
from setforge.locking import install_resources_lock, mutation_locks
from setforge.ownership import (
    Authority,
    ClaimEvent,
    ClaimLifecycle,
    OwnershipClaim,
    OwnershipStore,
    ProvenanceFact,
    ProvenanceFactKind,
    ResourceId,
    ResourceScope,
    ScopeKind,
    load_or_create_owner_id,
    read_owner_id,
    scan_legacy_receipts,
    scan_legacy_reconcile,
)


def _resource(coordinate: str = "ripgrep", *, provider: str = "cargo") -> ResourceId:
    return ResourceId(
        kind="package",
        provider=provider,
        coordinate=coordinate,
        scope=ResourceScope(ScopeKind.USER_HOST, "current-user"),
    )


def _fact(value: str = "external") -> ProvenanceFact:
    return ProvenanceFact(ProvenanceFactKind.ORIGIN, value)


def _claim(
    store: OwnershipStore,
    owner: uuid.UUID,
    resource: ResourceId | None = None,
    *,
    expected_generation: int | None = None,
) -> OwnershipClaim:
    with install_resources_lock():
        return store.claim_locked(
            resource_id=resource or _resource(),
            owner_id=owner,
            declaration_refs=("packages.ripgrep",),
            provenance=(_fact(),),
            locator="~/.cargo/bin/rg",
            fingerprint="sha256:abc",
            expected_generation=expected_generation,
        )


def _owner_worker(repo: str) -> str:
    return str(load_or_create_owner_id(Path(repo)))


def test_resource_id_is_typed_deterministic_and_scope_sensitive() -> None:
    resource = _resource()
    same = _resource()
    other_provider = _resource(provider="python")
    other_scope = ResourceId(
        kind=resource.kind,
        provider=resource.provider,
        coordinate=resource.coordinate,
        scope=ResourceScope(ScopeKind.APPLICATION, "editor/default"),
    )

    assert resource.canonical() == same.canonical()
    assert (
        len({resource.canonical(), other_provider.canonical(), other_scope.canonical()})
        == 3
    )
    with pytest.raises(OwnershipError, match="not canonical"):
        _resource(provider="Cargo")

    go = ResourceId(
        "package",
        "go",
        "example.com/Owner/Tool",
        ResourceScope(ScopeKind.USER_HOST, "current-user"),
    )
    local = ResourceId(
        "package",
        "local",
        "MyTool",
        ResourceScope(ScopeKind.USER_HOST, "current-user"),
    )
    assert go.coordinate.endswith("Owner/Tool")
    assert local.coordinate == "MyTool"
    with pytest.raises(OwnershipError, match="unsupported"):
        _resource(provider="tracked")


@pytest.mark.parametrize(
    "resource",
    [
        lambda: ResourceId(
            "package",
            "cargo",
            "Serde",
            ResourceScope(ScopeKind.USER_HOST, "current-user"),
        ),
        lambda: ResourceId(
            "package",
            "python",
            "typing_extensions",
            ResourceScope(ScopeKind.USER_HOST, "current-user"),
        ),
        lambda: ResourceId(
            "file",
            "tracked",
            "a/../b",
            ResourceScope(ScopeKind.TARGET_ROOT, "/projects/demo"),
        ),
        lambda: ResourceId(
            "file",
            "tracked",
            "config",
            ResourceScope(ScopeKind.TARGET_ROOT, "//projects/demo"),
        ),
    ],
)
def test_resource_id_rejects_semantic_aliases(
    resource: Callable[[], ResourceId],
) -> None:
    with pytest.raises(OwnershipError, match=r"canonical|contained|verified"):
        resource()


def test_target_scope_public_constructor_cannot_forge_verified_identity() -> None:
    with pytest.raises(OwnershipError, match="created from verified filesystem"):
        ResourceScope(ScopeKind.TARGET_ROOT, "object:1:1")


@pytest.mark.parametrize("symlink", [False, True])
def test_target_scope_rejects_regular_file_roots(tmp_path: Path, symlink: bool) -> None:
    regular = tmp_path / "regular"
    regular.write_text("not a root", encoding="utf-8")
    target = tmp_path / "alias" if symlink else regular
    if symlink:
        target.symlink_to(regular)

    with pytest.raises(OwnershipError, match="requires a directory"):
        ResourceScope.target_root(target)


def test_target_scope_aliases_share_one_durable_claim_identity(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    real_scope = ResourceScope.target_root(target)
    alias_scope = ResourceScope.target_root(alias)
    assert real_scope == alias_scope

    real_resource = ResourceId("file", "tracked", "config/app", real_scope)
    alias_resource = ResourceId("file", "tracked", "config/app", alias_scope)
    store = OwnershipStore(tmp_path / "ownership")
    first = uuid.uuid4()
    second = uuid.uuid4()
    with install_resources_lock():
        store.claim_locked(
            resource_id=real_resource,
            owner_id=first,
            declaration_refs=("tracked_files.app",),
            provenance=(_fact(),),
            locator=str(target / "config/app"),
            fingerprint="sha256:one",
            expected_generation=None,
        )
        with pytest.raises(OwnershipCollisionError, match="another config owner"):
            store.claim_locked(
                resource_id=alias_resource,
                owner_id=second,
                declaration_refs=("tracked_files.app",),
                provenance=(_fact(),),
                locator=str(alias / "config/app"),
                fingerprint="sha256:one",
                expected_generation=None,
            )


def test_missing_target_claim_moves_to_created_object_scope_and_blocks_alias(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    coordinate_scope = ResourceScope.target_root(target)
    source = ResourceId("file", "tracked", "config/app", coordinate_scope)
    store = OwnershipStore(tmp_path / "ownership")
    first = uuid.uuid4()
    second = uuid.uuid4()
    claim = _claim(store, first, source)

    with mutation_locks(resources=True, target_roots=(target,)) as guards:
        guards.targets[0].mkdir()
        object_scope = ResourceScope.target_root_guarded(guards.targets[0])
        destination = ResourceId("file", "tracked", "config/app", object_scope)
        moved = store.move_locked(
            source,
            destination,
            expected_owner=first,
            expected_generation=claim.generation,
        )
    assert store.read(source) is None
    assert store.read(destination) == moved

    alias = tmp_path / "project-alias"
    alias.symlink_to(target, target_is_directory=True)
    alias_resource = ResourceId(
        "file", "tracked", "config/app", ResourceScope.target_root(alias)
    )
    assert alias_resource == destination
    with (
        install_resources_lock(),
        pytest.raises(OwnershipCollisionError, match="another config owner"),
    ):
        store.claim_locked(
            resource_id=alias_resource,
            owner_id=second,
            declaration_refs=("tracked_files.app",),
            provenance=(_fact(),),
            locator=str(alias / "config/app"),
            fingerprint="sha256:one",
            expected_generation=None,
        )


def test_guarded_target_scope_refuses_replacement_before_claim_move(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    source = ResourceId(
        "file", "tracked", "config/app", ResourceScope.target_root(target)
    )
    store = OwnershipStore(tmp_path / "ownership")
    owner = uuid.uuid4()
    claim = _claim(store, owner, source)
    displaced = tmp_path / "displaced-project"

    def replace_before_move() -> None:
        with mutation_locks(resources=True, target_roots=(target,)) as guards:
            guard = guards.targets[0]
            guard.mkdir()
            target.rename(displaced)
            target.mkdir()
            object_scope = ResourceScope.target_root_guarded(guard)
            destination = ResourceId("file", "tracked", "config/app", object_scope)
            store.move_locked(
                source,
                destination,
                expected_owner=owner,
                expected_generation=claim.generation,
            )

    with pytest.raises(SetforgeError, match="target changed"):
        replace_before_move()

    assert store.read(source) == claim
    assert len(store.list_claims()) == 1


def test_extension_resource_identity_uses_runtime_casefold_contract() -> None:
    scope = ResourceScope(ScopeKind.USER_HOST, "current-user")
    canonical = ResourceId("package", "extension", "github.copilot", scope)
    assert canonical.coordinate == "github.copilot"
    with pytest.raises(OwnershipError, match="not canonical"):
        ResourceId("package", "extension", "GitHub.copilot", scope)


_CANONICAL_SCOPES = st.one_of(
    st.just(ResourceScope(ScopeKind.USER_HOST, "current-user")),
    st.just(ResourceScope(ScopeKind.APPLICATION, "editor/default")),
)


@given(
    first=st.tuples(
        st.just("package"),
        st.sampled_from(["cargo", "python", "go", "local"]),
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1),
        _CANONICAL_SCOPES,
    ),
    second=st.tuples(
        st.just("package"),
        st.sampled_from(["cargo", "python", "go", "local"]),
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1),
        _CANONICAL_SCOPES,
    ),
)
def test_resource_identity_canonicalization_is_injective(
    first: tuple[str, str, str, ResourceScope],
    second: tuple[str, str, str, ResourceScope],
) -> None:
    if first == second:
        return
    first_id = ResourceId(first[0], first[1], first[2], first[3])
    second_id = ResourceId(second[0], second[1], second[2], second[3])
    assert first_id.canonical() != second_id.canonical()


def test_claim_cas_idempotency_transfer_release_and_collision(tmp_path: Path) -> None:
    store = OwnershipStore(tmp_path)
    first_owner = uuid.uuid4()
    second_owner = uuid.uuid4()

    first = _claim(store, first_owner)
    assert first.generation == 1
    assert first.authority is Authority.MANAGE
    assert first.lifecycle is ClaimLifecycle.CLAIMED
    assert _claim(store, first_owner, expected_generation=1) == first

    with pytest.raises(OwnershipCollisionError, match="another config owner"):
        _claim(store, second_owner, expected_generation=1)
    with (
        install_resources_lock(),
        pytest.raises(OwnershipError, match="stale ownership generation"),
    ):
        store.release_locked(
            _resource(), expected_owner=first_owner, expected_generation=2
        )

    with install_resources_lock():
        transferred = store.transfer_locked(
            _resource(),
            expected_owner=first_owner,
            new_owner=second_owner,
            expected_generation=1,
            declaration_refs=("profiles.work.packages.ripgrep",),
        )
    assert transferred.owner_id == second_owner
    assert transferred.generation == 2

    with install_resources_lock():
        released = store.release_locked(
            _resource(), expected_owner=second_owner, expected_generation=2
        )
    assert released.generation == 3
    assert released.authority is Authority.NONE
    assert released.lifecycle is ClaimLifecycle.RELEASED
    with install_resources_lock():
        assert (
            store.release_locked(
                _resource(), expected_owner=second_owner, expected_generation=3
            )
            == released
        )


def test_claim_refresh_preserves_acquisition_provenance(tmp_path: Path) -> None:
    store = OwnershipStore(tmp_path)
    owner = uuid.uuid4()
    with install_resources_lock():
        first = store.claim_locked(
            resource_id=_resource(),
            owner_id=owner,
            declaration_refs=("packages.ripgrep",),
            provenance=(
                ProvenanceFact(ProvenanceFactKind.ACQUISITION, "adopted-external"),
                ProvenanceFact(ProvenanceFactKind.INTEGRITY, "sha256:original"),
            ),
            locator="~/.cargo/bin/rg",
            fingerprint="sha256:original",
            expected_generation=None,
        )
        refreshed = store.claim_locked(
            resource_id=_resource(),
            owner_id=owner,
            declaration_refs=("packages.ripgrep",),
            provenance=(ProvenanceFact(ProvenanceFactKind.PLATFORM, "linux-x86_64"),),
            locator="~/.cargo/bin/rg",
            fingerprint="sha256:current",
            expected_generation=first.generation,
        )

    assert set(refreshed.provenance) == {
        ProvenanceFact(ProvenanceFactKind.ACQUISITION, "adopted-external"),
        ProvenanceFact(ProvenanceFactKind.INTEGRITY, "sha256:original"),
        ProvenanceFact(ProvenanceFactKind.PLATFORM, "linux-x86_64"),
    }


def test_mutation_without_resource_lock_refuses(tmp_path: Path) -> None:
    store = OwnershipStore(tmp_path)
    with pytest.raises(SetforgeError, match="global resource lock"):
        store.claim_locked(
            resource_id=_resource(),
            owner_id=uuid.uuid4(),
            declaration_refs=("packages.ripgrep",),
            provenance=(_fact(),),
            locator="~/.cargo/bin/rg",
            fingerprint="sha256:abc",
            expected_generation=None,
        )


def test_claim_filename_and_schema_are_bound_to_resource(tmp_path: Path) -> None:
    store = OwnershipStore(tmp_path)
    claim = _claim(store, uuid.uuid4())
    path = next(store.claims_root.glob("*.json"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["resource_id"]["provider"] = "python"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CorruptOwnershipState, match="identity/path mismatch"):
        store.list_claims()
    assert claim.resource_id.provider == "cargo"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda raw: raw.update(authority="none"), "active claim"),
        (lambda raw: raw.update(history=[]), "history"),
        (
            lambda raw: raw["history"][0].update(generation=2),
            "history",
        ),
        (
            lambda raw: raw["history"][0].update(action="invented"),
            "unsupported claim event",
        ),
        (lambda raw: raw.update(extra=True), "fields do not match"),
    ],
)
def test_claim_reader_rejects_corrupt_state_matrix_and_history(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    store = OwnershipStore(tmp_path)
    _claim(store, uuid.uuid4())
    path = next(store.claims_root.glob("*.json"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    mutate(raw)
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CorruptOwnershipState) as raised:
        store.list_claims()
    assert message in str(raised.value.__cause__)


def test_claim_reader_refuses_symlinked_state(tmp_path: Path) -> None:
    store = OwnershipStore(tmp_path / "store")
    claim = _claim(store, uuid.uuid4())
    path = next(store.claims_root.glob("*.json"))
    outside = tmp_path / "outside.json"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)

    with pytest.raises(CorruptOwnershipState, match="not trusted"):
        store.read(claim.resource_id)


def test_move_intent_blocks_reads_and_recovers_before_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OwnershipStore(tmp_path)
    owner = uuid.uuid4()
    source = _claim(store, owner)
    destination = _resource("rg")
    real_write = store._write_claim

    def fail_destination(
        claim: OwnershipClaim, *, directory_fd: int | None = None
    ) -> None:
        if claim.resource_id == destination:
            raise OSError("injected crash")
        real_write(claim, directory_fd=directory_fd)

    monkeypatch.setattr(store, "_write_claim", fail_destination)
    with install_resources_lock(), pytest.raises(OSError, match="injected crash"):
        store.move_locked(
            source.resource_id,
            destination,
            expected_owner=owner,
            expected_generation=1,
        )
    monkeypatch.setattr(store, "_write_claim", real_write)

    with pytest.raises(OwnershipError, match="unfinished ownership move"):
        store.read(source.resource_id)
    with install_resources_lock():
        store.recover_moves_locked()
    assert store.read(source.resource_id) is None
    moved = store.read(destination)
    assert moved is not None
    assert moved.generation == 2


def test_move_destination_collision_refuses_without_intent(tmp_path: Path) -> None:
    store = OwnershipStore(tmp_path)
    owner = uuid.uuid4()
    source = _claim(store, owner)
    destination = _resource("rg")
    _claim(store, owner, destination)

    with (
        install_resources_lock(),
        pytest.raises(OwnershipCollisionError, match="destination"),
    ):
        store.move_locked(
            source.resource_id,
            destination,
            expected_owner=owner,
            expected_generation=1,
        )
    assert not store.intents_root.exists()


@pytest.mark.parametrize("checkpoint", ["after_destination", "after_source"])
def test_move_recovery_completes_later_crash_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkpoint: str
) -> None:
    store = OwnershipStore(tmp_path)
    owner = uuid.uuid4()
    source = _claim(store, owner)
    destination = _resource("rg")
    real_claim_unlink = store._unlink_claim
    real_intent_unlink = store._unlink_intent
    if checkpoint == "after_destination":

        def fail_claim_unlink(
            resource_id: ResourceId, *, directory_fd: int | None = None
        ) -> None:
            raise OSError("crash after destination")

        monkeypatch.setattr(store, "_unlink_claim", fail_claim_unlink)
    else:

        def fail_intent_unlink(
            intent_id: uuid.UUID, *, directory_fd: int | None = None
        ) -> None:
            raise OSError("crash after source")

        monkeypatch.setattr(store, "_unlink_intent", fail_intent_unlink)
    with install_resources_lock(), pytest.raises(OSError, match="crash after"):
        store.move_locked(
            source.resource_id,
            destination,
            expected_owner=owner,
            expected_generation=1,
        )
    if checkpoint == "after_destination":
        monkeypatch.setattr(store, "_unlink_claim", real_claim_unlink)
    else:
        monkeypatch.setattr(store, "_unlink_intent", real_intent_unlink)

    with install_resources_lock():
        store.recover_moves_locked()
    assert store.read(source.resource_id) is None
    assert store.read(destination) is not None
    assert not tuple(store.intents_root.glob("*.json"))


def test_move_recovery_retains_conflicting_destination(tmp_path: Path) -> None:
    store = OwnershipStore(tmp_path)
    owner = uuid.uuid4()
    source = _claim(store, owner)
    destination_id = _resource("rg")
    moved_history = (*source.history, ClaimEvent("move", owner, 2))
    destination = OwnershipClaim(
        resource_id=destination_id,
        owner_id=owner,
        declaration_refs=source.declaration_refs,
        authority=source.authority,
        lifecycle=source.lifecycle,
        provenance=source.provenance,
        locator=source.locator,
        fingerprint="sha256:conflict",
        generation=source.generation + 1,
        history=moved_history,
    )
    intended = OwnershipClaim(
        resource_id=destination_id,
        owner_id=owner,
        declaration_refs=source.declaration_refs,
        authority=source.authority,
        lifecycle=source.lifecycle,
        provenance=source.provenance,
        locator=source.locator,
        fingerprint=source.fingerprint,
        generation=source.generation + 1,
        history=moved_history,
    )
    intent = store._intent_path(uuid.uuid4())
    store.intents_root.mkdir(parents=True)
    intent.write_text(
        json.dumps(
            {
                "destination": ownership_module._claim_to_json(intended),
                "intent_id": intent.stem,
                "schema_version": "1.0",
                "source": ownership_module._claim_to_json(source),
            }
        ),
        encoding="utf-8",
    )
    store._write_claim(destination)

    with (
        install_resources_lock(),
        pytest.raises(CorruptOwnershipState, match="conflicts with live claims"),
    ):
        store.recover_moves_locked()
    assert intent.exists()
    assert store._read_path(store._claim_path(destination_id)) == destination


def test_move_recovery_rejects_semantically_tampered_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OwnershipStore(tmp_path)
    owner = uuid.uuid4()
    source = _claim(store, owner)
    destination = _resource("rg")
    real_write = store._write_claim

    def fail_destination(
        claim: OwnershipClaim, *, directory_fd: int | None = None
    ) -> None:
        if claim.resource_id == destination:
            raise OSError("stop after intent")
        real_write(claim, directory_fd=directory_fd)

    monkeypatch.setattr(store, "_write_claim", fail_destination)
    with install_resources_lock(), pytest.raises(OSError, match="stop after intent"):
        store.move_locked(
            source.resource_id,
            destination,
            expected_owner=owner,
            expected_generation=1,
        )
    monkeypatch.setattr(store, "_write_claim", real_write)
    intent = next(store.intents_root.glob("*.json"))
    raw = json.loads(intent.read_text(encoding="utf-8"))
    raw["destination"]["fingerprint"] = "sha256:tampered"
    intent.write_text(json.dumps(raw), encoding="utf-8")

    with (
        install_resources_lock(),
        pytest.raises(CorruptOwnershipState, match="invalid ownership move intent"),
    ):
        store.recover_moves_locked()
    assert store._read_path(store._claim_path(source.resource_id)) == source
    assert store._read_path(store._claim_path(destination)) is None
    assert intent.exists()


def test_legacy_evidence_never_creates_claims(tmp_path: Path) -> None:
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "one.json").write_text('{"key":"rg"}', encoding="utf-8")
    (receipts / "two.json").write_text('{"key":"rg"}', encoding="utf-8")
    (receipts / "bad.json").write_bytes(b"\xff")
    state = tmp_path / "state"
    artifact = state / "base" / "default" / "shell"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"base")
    (receipts / "link.json").symlink_to(receipts / "one.json")
    reconcile_link = state / "index" / "escaped"
    reconcile_link.parent.mkdir(parents=True)
    reconcile_link.symlink_to(artifact)

    receipt_evidence = scan_legacy_receipts(receipts)
    reconcile_evidence = scan_legacy_reconcile(state)

    assert sum(item.ambiguous for item in receipt_evidence) == 2
    assert sum(item.corrupt for item in receipt_evidence) == 2
    assert any(
        item.corrupt and item.locator == reconcile_link for item in reconcile_evidence
    )
    assert reconcile_evidence[0].source == "reconcile-base"
    assert not (state / "ownership").exists()


def test_legacy_evidence_refuses_symlinked_roots_and_legs(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "one.json").write_text('{"key":"rg"}', encoding="utf-8")
    receipts = tmp_path / "receipts"
    receipts.symlink_to(outside, target_is_directory=True)
    assert scan_legacy_receipts(receipts) == (
        ownership_module.LegacyOwnershipEvidence(
            "receipt", "receipts", receipts, corrupt=True
        ),
    )

    state = tmp_path / "state"
    state.mkdir()
    (state / "base").symlink_to(outside, target_is_directory=True)
    evidence = scan_legacy_reconcile(state)
    assert evidence == (
        ownership_module.LegacyOwnershipEvidence(
            "reconcile-base", "base", state / "base", corrupt=True
        ),
    )


def test_checkout_uuid_shared_by_worktrees_but_not_clone(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "seed"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "linked", str(linked)],
        check=True,
    )
    subprocess.run(["git", "clone", str(repo), str(clone)], check=True)

    with ProcessPoolExecutor(max_workers=2) as pool:
        ids = tuple(pool.map(_owner_worker, (str(repo), str(linked))))

    assert ids[0] == ids[1]
    assert str(load_or_create_owner_id(clone)) != ids[0]


def test_checkout_uuid_rejects_non_git_and_corrupt_state(tmp_path: Path) -> None:
    with pytest.raises(OwnershipError, match="Git-backed"):
        load_or_create_owner_id(tmp_path)

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True)
    owner_id = load_or_create_owner_id(repo)
    owner_path = repo / ".git" / "setforge" / "owner-id"
    owner_path.write_text(f"{owner_id} extra\n", encoding="ascii")
    with pytest.raises(CorruptOwnershipState, match="invalid config owner"):
        load_or_create_owner_id(repo)


def test_checkout_uuid_refuses_symlinked_identity_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True)
    (repo / ".git" / "setforge").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CorruptOwnershipState, match="directory is not trusted"):
        load_or_create_owner_id(repo)


def test_checkout_uuid_refuses_symlinked_identity_leaf(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True)
    owner = load_or_create_owner_id(repo)
    owner_path = repo / ".git" / "setforge" / "owner-id"
    outside = tmp_path / "outside"
    outside.write_text(f"{owner}\n", encoding="ascii")
    owner_path.unlink()
    owner_path.symlink_to(outside)

    with pytest.raises(CorruptOwnershipState, match="not trusted"):
        load_or_create_owner_id(repo)


def test_checkout_uuid_binds_re_resolved_common_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.git"
    second = tmp_path / "second.git"
    first.mkdir()
    second.mkdir()
    observed = iter((first, second))
    monkeypatch.setattr(
        ownership_module, "_git_common_dir", lambda _path: next(observed)
    )

    with pytest.raises(OwnershipError, match="changed while holding UUID"):
        load_or_create_owner_id(tmp_path)


def test_read_checkout_uuid_binds_re_resolved_common_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.git"
    second = tmp_path / "second.git"
    first.mkdir()
    second.mkdir()
    observed = iter((first, second))
    monkeypatch.setattr(
        ownership_module, "_git_common_dir", lambda _path: next(observed)
    )

    with pytest.raises(OwnershipError, match="changed while holding UUID"):
        read_owner_id(tmp_path)


@pytest.mark.parametrize("reader", [False, True])
def test_checkout_uuid_refuses_common_directory_switch_during_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: bool
) -> None:
    first = tmp_path / "first.git"
    second = tmp_path / "second.git"
    first.mkdir()
    second.mkdir()
    owner = uuid.uuid4()
    if reader:
        owner_dir = first / "setforge"
        owner_dir.mkdir()
        (owner_dir / "owner-id").write_text(f"{owner}\n", encoding="ascii")
    observed = iter((first, first, second))
    monkeypatch.setattr(
        ownership_module, "_git_common_dir", lambda _path: next(observed)
    )

    operation = read_owner_id if reader else load_or_create_owner_id
    with pytest.raises(OwnershipError, match="changed while holding UUID"):
        operation(tmp_path)


def test_claim_publication_refuses_symlinked_claims_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    store = OwnershipStore(tmp_path / "store")
    store.root.mkdir()
    store.claims_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(CorruptOwnershipState, match="not trusted"):
        _claim(store, uuid.uuid4())
    assert not tuple(outside.iterdir())


def test_claim_publication_is_anchored_and_detects_directory_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OwnershipStore(tmp_path / "store")
    owner = uuid.uuid4()
    _claim(store, owner)
    displaced = tmp_path / "displaced-claims"
    real_write = ownership_module._atomic_write_at

    def swap_after_write(directory_fd: int, name: str, payload: bytes) -> None:
        real_write(directory_fd, name, payload)
        store.claims_root.rename(displaced)
        store.claims_root.mkdir()

    monkeypatch.setattr(ownership_module, "_atomic_write_at", swap_after_write)
    with (
        install_resources_lock(),
        pytest.raises(CorruptOwnershipState, match="binding changed"),
    ):
        store.claim_locked(
            resource_id=_resource(),
            owner_id=owner,
            declaration_refs=("packages.ripgrep",),
            provenance=(_fact(),),
            locator="~/.cargo/bin/rg",
            fingerprint="sha256:changed",
            expected_generation=1,
        )
    assert not tuple(store.claims_root.iterdir())
    assert len(tuple(displaced.glob("*.json"))) == 1


def test_move_refuses_symlinked_intents_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    store = OwnershipStore(tmp_path / "store")
    owner = uuid.uuid4()
    source = _claim(store, owner)
    store.intents_root.symlink_to(outside, target_is_directory=True)

    with (
        install_resources_lock(),
        pytest.raises(CorruptOwnershipState, match="not trusted"),
    ):
        store.move_locked(
            source.resource_id,
            _resource("rg"),
            expected_owner=owner,
            expected_generation=1,
        )
    assert not tuple(outside.iterdir())
    assert store._read_path(store._claim_path(source.resource_id)) == source


def test_move_refuses_ownership_root_swap_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OwnershipStore(tmp_path / "store")
    owner = uuid.uuid4()
    source = _claim(store, owner)
    displaced = tmp_path / "displaced-store"
    real_open = ownership_module._open_bound_child

    @contextmanager
    def swap_before_intents(
        parent_fd: int, parent_path: Path, name: str, *, create: bool
    ) -> Iterator[int | None]:
        if name == "intents":
            store.root.rename(displaced)
            store.root.mkdir()
        with real_open(parent_fd, parent_path, name, create=create) as descriptor:
            yield descriptor

    monkeypatch.setattr(ownership_module, "_open_bound_child", swap_before_intents)
    with (
        install_resources_lock(),
        pytest.raises(CorruptOwnershipState, match="binding changed"),
    ):
        store.move_locked(
            source.resource_id,
            _resource("rg"),
            expected_owner=owner,
            expected_generation=1,
        )
    assert not tuple(store.root.iterdir())
    assert len(tuple((displaced / "claims").glob("*.json"))) == 1
    assert not tuple((displaced / "intents").iterdir())


def test_move_refuses_intents_child_swap_before_claim_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OwnershipStore(tmp_path / "store")
    owner = uuid.uuid4()
    source = _claim(store, owner)
    destination = _resource("rg")
    displaced = tmp_path / "displaced-intents"
    real_write = store._write_intent

    def swap_then_write(
        intent: ownership_module._MoveIntent, *, directory_fd: int | None = None
    ) -> None:
        store.intents_root.rename(displaced)
        store.intents_root.mkdir()
        real_write(intent, directory_fd=directory_fd)

    monkeypatch.setattr(store, "_write_intent", swap_then_write)
    with (
        install_resources_lock(),
        pytest.raises(CorruptOwnershipState, match="binding changed"),
    ):
        store.move_locked(
            source.resource_id,
            destination,
            expected_owner=owner,
            expected_generation=source.generation,
        )

    assert store._read_path(store._claim_path(source.resource_id)) == source
    assert store._read_path(store._claim_path(destination)) is None
    assert not tuple(store.intents_root.iterdir())
    assert len(tuple(displaced.glob("*.json"))) == 1
