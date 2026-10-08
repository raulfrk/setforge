"""Durable ownership identity, ledger, migration, and checkout tests."""

from __future__ import annotations

import itertools
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
from setforge.locking import mutation_locks
from setforge.ownership import (
    Authority,
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
)
from tests.shared_helpers import at_record_write, crash


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
    with mutation_locks(resources=True):
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
    with mutation_locks(resources=True):
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
        object_scope = ResourceScope.target_root(target)
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
        mutation_locks(resources=True),
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
        mutation_locks(resources=True),
        pytest.raises(OwnershipError, match="stale ownership generation"),
    ):
        store.release_locked(
            _resource(), expected_owner=first_owner, expected_generation=2
        )

    with mutation_locks(resources=True):
        transferred = store.transfer_locked(
            _resource(),
            expected_owner=first_owner,
            new_owner=second_owner,
            expected_generation=1,
            declaration_refs=("profiles.work.packages.ripgrep",),
        )
    assert transferred.owner_id == second_owner
    assert transferred.generation == 2

    with mutation_locks(resources=True):
        released = store.release_locked(
            _resource(), expected_owner=second_owner, expected_generation=2
        )
    assert released.generation == 3
    assert released.authority is Authority.NONE
    assert released.lifecycle is ClaimLifecycle.RELEASED
    with mutation_locks(resources=True):
        assert (
            store.release_locked(
                _resource(), expected_owner=second_owner, expected_generation=3
            )
            == released
        )


def test_claim_refresh_preserves_acquisition_provenance(tmp_path: Path) -> None:
    store = OwnershipStore(tmp_path)
    owner = uuid.uuid4()
    with mutation_locks(resources=True):
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


def _interrupted_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[OwnershipStore, OwnershipClaim, ResourceId]]:
    """Yield a store whose move of one claim stopped before each of its writes.

    Ends at the first write the move does not reach, where the move completes.
    """
    destination = _resource("rg")
    for step in itertools.count(1):
        store = OwnershipStore(tmp_path / f"stopped-before-write-{step}")
        owner = uuid.uuid4()
        source = _claim(store, owner)
        try:
            with (
                at_record_write(monkeypatch, step, before=crash),
                mutation_locks(resources=True),
            ):
                store.move_locked(
                    source.resource_id,
                    destination,
                    expected_owner=owner,
                    expected_generation=1,
                )
        except OSError:
            yield store, source, destination
        else:
            assert step > 1, "the move made no write the test could interrupt"
            return


def _pending_intents(store: OwnershipStore) -> tuple[Path, ...]:
    return tuple(store.intents_root.glob("*.json"))


def _claim_files(store: OwnershipStore) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in store.claims_root.glob("*.json")}


def test_a_move_stopped_at_any_write_is_completed_by_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved_by_recovery = 0
    for store, source, destination in _interrupted_moves(tmp_path, monkeypatch):
        decided = bool(_pending_intents(store))
        if decided:
            with pytest.raises(OwnershipError, match="unfinished ownership move"):
                store.read(source.resource_id)

        with mutation_locks(resources=True):
            store.recover_moves_locked()

        assert not _pending_intents(store)
        if decided:
            resolved_by_recovery += 1
            assert store.read(source.resource_id) is None
            moved = store.read(destination)
            assert moved is not None
            assert moved.generation == 2
        else:
            assert store.read(source.resource_id) == source
            assert store.read(destination) is None
    assert resolved_by_recovery > 1


def test_move_destination_collision_refuses_without_intent(tmp_path: Path) -> None:
    store = OwnershipStore(tmp_path)
    owner = uuid.uuid4()
    source = _claim(store, owner)
    destination = _resource("rg")
    _claim(store, owner, destination)

    with (
        mutation_locks(resources=True),
        pytest.raises(OwnershipCollisionError, match="destination"),
    ):
        store.move_locked(
            source.resource_id,
            destination,
            expected_owner=owner,
            expected_generation=1,
        )
    assert not store.intents_root.exists()


def test_move_recovery_retains_conflicting_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for store, _source, destination in _interrupted_moves(tmp_path, monkeypatch):
        intents = _pending_intents(store)
        if not intents or store.claim_path(destination).exists():
            continue
        # The move was decided but its destination is not written yet; a
        # different claim took the destination's place.
        conflict = {
            **json.loads(intents[0].read_text(encoding="utf-8"))["destination"],
            "fingerprint": "sha256:conflict",
        }
        store.claim_path(destination).write_text(json.dumps(conflict), encoding="utf-8")
        claims = _claim_files(store)

        with (
            mutation_locks(resources=True),
            pytest.raises(CorruptOwnershipState, match="conflicts with live claims"),
        ):
            store.recover_moves_locked()

        assert intents[0].exists()
        assert _claim_files(store) == claims
        return
    pytest.fail("no stopped move had an intent without its destination")


def test_move_recovery_rejects_semantically_tampered_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = 0
    for store, _source, _destination in _interrupted_moves(tmp_path, monkeypatch):
        if not (intents := _pending_intents(store)):
            continue
        raw = json.loads(intents[0].read_text(encoding="utf-8"))
        raw["destination"]["fingerprint"] = "sha256:tampered"
        intents[0].write_text(json.dumps(raw), encoding="utf-8")
        claims = _claim_files(store)

        with (
            mutation_locks(resources=True),
            pytest.raises(CorruptOwnershipState, match="invalid ownership move intent"),
        ):
            store.recover_moves_locked()

        assert intents[0].exists()
        assert _claim_files(store) == claims
        refused += 1
    assert refused > 1


def test_checkout_uuid_shared_by_worktrees_but_not_clone(
    tmp_path: Path, init_git_repo
) -> None:
    repo = init_git_repo(tmp_path / "repo")
    linked = tmp_path / "linked"
    clone = tmp_path / "clone"
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


@pytest.mark.parametrize("state_parts", [(".local", "state", "setforge"), ()])
def test_store_follows_symlinks_leading_to_ownership_root(
    tmp_path: Path, state_parts: tuple[str, ...]
) -> None:
    real = tmp_path / "volume"
    real.mkdir()
    alias = tmp_path / "home"
    alias.symlink_to(real, target_is_directory=True)
    store = OwnershipStore(alias.joinpath(*state_parts, "ownership"))
    owner = uuid.uuid4()

    claim = _claim(store, owner)
    with mutation_locks(resources=True):
        moved = store.move_locked(
            claim.resource_id,
            _resource("rg"),
            expected_owner=owner,
            expected_generation=1,
        )

    assert store.list_claims() == (moved,)
    assert store.read(moved.resource_id) == moved
    stored = real.joinpath(*state_parts, "ownership", "claims")
    assert [path.name for path in stored.iterdir()] == [
        store.claim_path(moved.resource_id).name
    ]


def test_store_refuses_symlinked_ownership_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    store = OwnershipStore(tmp_path / "state" / "ownership")
    store.root.parent.mkdir()
    store.root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(CorruptOwnershipState, match="not trusted"):
        _claim(store, uuid.uuid4())
    with pytest.raises(CorruptOwnershipState, match="not trusted"):
        store.list_claims()
    assert not tuple(outside.iterdir())


def test_claim_publication_is_anchored_and_detects_directory_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OwnershipStore(tmp_path / "store")
    owner = uuid.uuid4()
    _claim(store, owner)
    displaced = tmp_path / "displaced-claims"

    def swap_claims_directory() -> None:
        store.claims_root.rename(displaced)
        store.claims_root.mkdir()

    with (
        at_record_write(monkeypatch, 1, after=swap_claims_directory),
        mutation_locks(resources=True),
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
    claims = _claim_files(store)
    store.intents_root.symlink_to(outside, target_is_directory=True)

    with (
        mutation_locks(resources=True),
        pytest.raises(CorruptOwnershipState, match="not trusted"),
    ):
        store.move_locked(
            source.resource_id,
            _resource("rg"),
            expected_owner=owner,
            expected_generation=1,
        )
    assert not tuple(outside.iterdir())
    assert _claim_files(store) == claims


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
        mutation_locks(resources=True),
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

    def swap_intents_directory() -> None:
        store.intents_root.rename(displaced)
        store.intents_root.mkdir()

    with (
        at_record_write(monkeypatch, 1, before=swap_intents_directory),
        mutation_locks(resources=True),
        pytest.raises(CorruptOwnershipState, match="binding changed"),
    ):
        store.move_locked(
            source.resource_id,
            destination,
            expected_owner=owner,
            expected_generation=source.generation,
        )

    assert store.read(source.resource_id) == source
    assert store.read(destination) is None
    assert not tuple(store.intents_root.iterdir())
    assert len(tuple(displaced.glob("*.json"))) == 1
