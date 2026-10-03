from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

import setforge.tree_management as tree_management
from setforge.config import TreeOrphanPolicy, TreePolicy, TreeSymlinkPolicy
from setforge.errors import InvariantViolation, SetforgeError
from setforge.tree_management import (
    TreeActionKind,
    TreeEntry,
    TreeEntryKind,
    TreeHoldResolution,
    apply_tree,
    dumps_inventory,
    loads_inventory,
    plan_tree,
    scan_tree,
)


def test_scan_tree_is_deterministic_and_captures_modes(tmp_path: Path) -> None:
    root = tmp_path / "source"
    nested = root / "nested"
    nested.mkdir(parents=True)
    file = nested / "tool"
    file.write_bytes(b"tool\n")
    file.chmod(0o750)

    first = scan_tree(root, TreePolicy(), capture_payloads=True)
    second = scan_tree(root, TreePolicy(), capture_payloads=True)

    assert first == second
    assert [entry.path for entry in first.inventory.entries] == [
        "nested",
        "nested/tool",
    ]
    assert first.inventory.entries[1].kind is TreeEntryKind.FILE
    assert first.inventory.entries[1].mode == 0o750
    assert first.payload_map() == {"nested/tool": b"tool\n"}


def test_scan_tree_excludes_without_claiming_nested_content(tmp_path: Path) -> None:
    root = tmp_path / "source"
    (root / "cache").mkdir(parents=True)
    (root / "cache" / "private").write_text("secret", encoding="utf-8")
    (root / "kept").write_text("portable", encoding="utf-8")

    frozen = scan_tree(root, TreePolicy(exclude=["cache/"]))

    assert [entry.path for entry in frozen.inventory.entries] == ["kept"]


def test_scan_tree_refuses_or_preserves_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "link").symlink_to("target")

    with pytest.raises(SetforgeError, match="contains a symlink"):
        scan_tree(root, TreePolicy())

    frozen = scan_tree(root, TreePolicy(symlinks=TreeSymlinkPolicy.PRESERVE))
    assert frozen.inventory.entries[0].kind is TreeEntryKind.SYMLINK
    assert frozen.inventory.entries[0].link_target == "target"


def test_scan_tree_refuses_fifo(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    os.mkfifo(root / "pipe")

    with pytest.raises(SetforgeError, match="unsupported entry"):
        scan_tree(root, TreePolicy())


@pytest.mark.parametrize("purpose", ["create", "update", "remove"])
def test_scan_tree_refuses_reserved_publication_names(
    tmp_path: Path, purpose: str
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / f".value.setforge-{purpose}").write_text("collision", encoding="utf-8")

    with pytest.raises(SetforgeError, match="reserved name"):
        scan_tree(root, TreePolicy())


def test_plan_removes_only_unchanged_owned_orphans(tmp_path: Path) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    source.mkdir()
    live.mkdir()
    (source / "keep").write_text("new", encoding="utf-8")
    (live / "keep").write_text("old", encoding="utf-8")
    (live / "remove").write_text("owned", encoding="utf-8")
    (live / "drifted").write_text("changed", encoding="utf-8")
    (live / "unowned").write_text("host", encoding="utf-8")
    policy = TreePolicy(orphans=TreeOrphanPolicy.REMOVE_OWNED)
    desired = scan_tree(source, policy, capture_payloads=True)
    current = scan_tree(live, policy).inventory

    prior_root = tmp_path / "prior"
    prior_root.mkdir()
    (prior_root / "remove").write_text("owned", encoding="utf-8")
    (prior_root / "drifted").write_text("original", encoding="utf-8")
    prior = scan_tree(prior_root, policy).inventory
    plan = plan_tree(desired, current, prior, policy)
    by_path = {action.path: action.kind for action in plan.actions}

    assert by_path == {
        "drifted": TreeActionKind.HOLD,
        "keep": TreeActionKind.UPDATE,
        "remove": TreeActionKind.REMOVE,
        "unowned": TreeActionKind.KEEP,
    }


def _drifted_orphan_trees(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    live = tmp_path / "live"
    prior_root = tmp_path / "prior"
    for root in (source, live, prior_root):
        root.mkdir()
        (root / "kept").write_text("kept\n", encoding="utf-8")
    (live / "drifted").write_text("edited\n", encoding="utf-8")
    (prior_root / "drifted").write_text("installed\n", encoding="utf-8")
    return source, live, prior_root


def test_keep_live_releases_drifted_owned_orphan(tmp_path: Path) -> None:
    source, live, prior_root = _drifted_orphan_trees(tmp_path)
    policy = TreePolicy(orphans=TreeOrphanPolicy.REMOVE_OWNED)
    desired = scan_tree(source, policy, capture_payloads=True)
    prior = scan_tree(prior_root, policy).inventory
    current = scan_tree(live, policy).inventory

    held = plan_tree(desired, current, prior, policy)
    plan = plan_tree(desired, current, prior, policy, TreeHoldResolution.KEEP_LIVE)

    assert held.blocked
    assert [(action.path, action.kind) for action in plan.actions] == [
        ("drifted", TreeActionKind.RELEASE)
    ]
    applied = apply_tree(plan, live, policy)
    assert (live / "drifted").read_text(encoding="utf-8") == "edited\n"
    assert applied.owned_paths == ("kept",)
    repeated = plan_tree(desired, scan_tree(live, policy).inventory, applied, policy)
    assert [(action.path, action.kind) for action in repeated.actions] == [
        ("drifted", TreeActionKind.KEEP)
    ]


def test_use_tracked_removes_drifted_owned_orphan(tmp_path: Path) -> None:
    source, live, prior_root = _drifted_orphan_trees(tmp_path)
    policy = TreePolicy(orphans=TreeOrphanPolicy.REMOVE_OWNED)
    plan = plan_tree(
        scan_tree(source, policy, capture_payloads=True),
        scan_tree(live, policy).inventory,
        scan_tree(prior_root, policy).inventory,
        policy,
        TreeHoldResolution.USE_TRACKED,
    )

    assert [(action.path, action.kind) for action in plan.actions] == [
        ("drifted", TreeActionKind.REMOVE)
    ]
    applied = apply_tree(plan, live, policy)
    assert not (live / "drifted").exists()
    assert [entry.path for entry in applied.entries] == ["kept"]


def test_keep_live_leaves_kind_conflict_and_its_tracked_children(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    (source / "entry").mkdir(parents=True)
    (source / "entry" / "child").write_text("child\n", encoding="utf-8")
    (source / "other").write_text("other\n", encoding="utf-8")
    live.mkdir()
    (live / "entry").write_text("host file\n", encoding="utf-8")
    policy = TreePolicy()
    desired = scan_tree(source, policy, capture_payloads=True)
    current = scan_tree(live, policy).inventory

    for resolution in (None, TreeHoldResolution.USE_TRACKED):
        held = plan_tree(desired, current, None, policy, resolution)
        assert held.blocked
        assert {a.path for a in held.actions if a.kind is TreeActionKind.HOLD} == {
            "entry"
        }
    plan = plan_tree(desired, current, None, policy, TreeHoldResolution.KEEP_LIVE)

    assert {action.path: action.kind for action in plan.actions} == {
        "entry": TreeActionKind.RELEASE,
        "other": TreeActionKind.CREATE,
    }
    applied = apply_tree(plan, live, policy)
    assert (live / "entry").read_text(encoding="utf-8") == "host file\n"
    assert (live / "other").read_text(encoding="utf-8") == "other\n"
    assert applied.owned_paths == ("other",)


def test_use_tracked_replaces_symlink_kind_conflict(tmp_path: Path) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    source.mkdir()
    live.mkdir()
    (source / "entry").write_text("tracked\n", encoding="utf-8")
    (live / "entry").symlink_to("elsewhere")
    policy = TreePolicy()
    plan = plan_tree(
        scan_tree(source, policy, capture_payloads=True),
        scan_tree(
            live, policy.model_copy(update={"symlinks": TreeSymlinkPolicy.PRESERVE})
        ).inventory,
        None,
        policy,
        TreeHoldResolution.USE_TRACKED,
    )

    assert [(action.path, action.kind) for action in plan.actions] == [
        ("entry", TreeActionKind.UPDATE)
    ]
    apply_tree(plan, live, policy)
    assert not (live / "entry").is_symlink()
    assert (live / "entry").read_text(encoding="utf-8") == "tracked\n"


def test_remove_owned_preserves_parent_of_unowned_descendant(tmp_path: Path) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    prior_root = tmp_path / "prior"
    for root in (source, live, prior_root):
        root.mkdir()
    (live / "owned").mkdir()
    unowned = live / "owned" / "external.txt"
    unowned.write_text("host-only\n", encoding="utf-8")
    (prior_root / "owned").mkdir()
    policy = TreePolicy(orphans=TreeOrphanPolicy.REMOVE_OWNED)
    desired = scan_tree(source, policy, capture_payloads=True)
    prior = scan_tree(prior_root, policy).inventory
    plan = plan_tree(desired, scan_tree(live, policy).inventory, prior, policy)
    by_path = {action.path: action.kind for action in plan.actions}

    assert by_path == {
        "owned": TreeActionKind.KEEP,
        "owned/external.txt": TreeActionKind.KEEP,
    }

    applied = apply_tree(plan, live, policy)
    repeated = apply_tree(
        plan_tree(desired, scan_tree(live, policy).inventory, applied, policy),
        live,
        policy,
    )

    assert unowned.read_bytes() == b"host-only\n"
    assert {entry.path for entry in repeated.entries} == {
        "owned",
        "owned/external.txt",
    }


def test_apply_tree_preserves_unowned_and_removes_owned(tmp_path: Path) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    prior_root = tmp_path / "prior"
    for root in (source, live, prior_root):
        root.mkdir()
    (source / "keep").write_text("new", encoding="utf-8")
    (live / "keep").write_text("old", encoding="utf-8")
    (live / "remove").write_text("owned", encoding="utf-8")
    (live / "unowned").write_text("host", encoding="utf-8")
    (prior_root / "remove").write_text("owned", encoding="utf-8")
    policy = TreePolicy(orphans=TreeOrphanPolicy.REMOVE_OWNED)
    plan = plan_tree(
        scan_tree(source, policy, capture_payloads=True),
        scan_tree(live, policy).inventory,
        scan_tree(prior_root, policy).inventory,
        policy,
    )

    applied = apply_tree(plan, live, policy)

    assert (live / "keep").read_text(encoding="utf-8") == "new"
    assert not (live / "remove").exists()
    assert (live / "unowned").read_text(encoding="utf-8") == "host"
    assert {entry.path for entry in applied.entries} == {"keep", "unowned"}


def test_apply_tree_creates_an_absent_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("content\n", encoding="utf-8")
    destination = tmp_path / "live"
    policy = TreePolicy()
    desired = scan_tree(source, policy, capture_payloads=True)
    live = scan_tree(destination, policy).inventory

    applied = apply_tree(plan_tree(desired, live, None, policy), destination, policy)

    assert applied.root_present
    assert (destination / "file.txt").read_text(encoding="utf-8") == "content\n"


def test_apply_tree_preserves_zero_root_mode(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    policy = TreePolicy()
    desired = scan_tree(source, policy, capture_payloads=True)
    inventory = replace(
        desired.inventory,
        root_mode=0o000,
        fingerprint=tree_management._inventory_fingerprint(
            root_present=True,
            root_mode=0o000,
            entries=desired.inventory.entries,
        ),
    )
    desired = replace(desired, inventory=inventory)
    destination = tmp_path / "live"
    destination.mkdir()
    live = scan_tree(destination, policy).inventory
    root_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)

    try:
        applied = apply_tree(
            plan_tree(desired, live, None, policy),
            destination,
            policy,
            anchor_fd=root_fd,
        )
        observed_mode = stat.S_IMODE(destination.stat().st_mode)
    finally:
        os.close(root_fd)
        destination.chmod(0o700)

    assert applied.root_mode == 0o000
    assert observed_mode == 0o000


@pytest.mark.parametrize("operation", ["create", "update", "remove"])
def test_apply_tree_refuses_intermediate_symlink_swap_without_outside_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    prior_root = tmp_path / "prior"
    outside = tmp_path / "outside"
    for root in (source, live, prior_root):
        (root / "nested").mkdir(parents=True)
    outside.mkdir()
    wanted = source / "nested" / "value"
    current = live / "nested" / "value"
    prior = prior_root / "nested" / "value"
    outside_value = outside / "value"
    if operation == "create":
        wanted.write_text("created\n", encoding="utf-8")
    elif operation == "update":
        wanted.write_text("updated\n", encoding="utf-8")
        current.write_text("current\n", encoding="utf-8")
        outside_value.write_text("outside\n", encoding="utf-8")
    else:
        current.write_text("owned\n", encoding="utf-8")
        prior.write_text("owned\n", encoding="utf-8")
        outside_value.write_text("outside\n", encoding="utf-8")
    policy = TreePolicy(orphans=TreeOrphanPolicy.REMOVE_OWNED)
    plan = plan_tree(
        scan_tree(source, policy, capture_payloads=True),
        scan_tree(live, policy).inventory,
        scan_tree(prior_root, policy).inventory,
        policy,
    )
    original_open_parent = tree_management._open_parent
    swapped = False

    def swap_then_open(root_fd: int, relative: str) -> tuple[int, str]:
        nonlocal swapped
        if not swapped and relative == "nested/value":
            swapped = True
            (live / "nested").rename(live / "detached")
            (live / "nested").symlink_to(outside, target_is_directory=True)
        return original_open_parent(root_fd, relative)

    monkeypatch.setattr(tree_management, "_open_parent", swap_then_open)

    with pytest.raises(SetforgeError, match="changed during apply"):
        apply_tree(plan, live, policy)

    assert swapped
    if operation == "create":
        assert not outside_value.exists()
    else:
        assert outside_value.read_text(encoding="utf-8") == "outside\n"


@pytest.mark.parametrize("operation", ["create", "update", "remove"])
def test_apply_tree_final_leaf_publication_is_compare_and_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    prior_root = tmp_path / "prior"
    for root in (source, live, prior_root):
        root.mkdir()
    wanted = source / "value"
    current = live / "value"
    prior = prior_root / "value"
    if operation == "create":
        wanted.write_text("wanted\n", encoding="utf-8")
    elif operation == "update":
        wanted.write_text("wanted\n", encoding="utf-8")
        current.write_text("planned\n", encoding="utf-8")
    else:
        current.write_text("owned\n", encoding="utf-8")
        prior.write_text("owned\n", encoding="utf-8")
    policy = TreePolicy(orphans=TreeOrphanPolicy.REMOVE_OWNED)
    plan = plan_tree(
        scan_tree(source, policy, capture_payloads=True),
        scan_tree(live, policy).inventory,
        scan_tree(prior_root, policy).inventory,
        policy,
    )
    original_renameat2 = tree_management._renameat2
    swapped = False

    def swap_leaf_then_rename(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
        flags: int,
    ) -> None:
        nonlocal swapped
        if not swapped and (destination_name == "value" or source_name == "value"):
            swapped = True
            current.write_text("external\n", encoding="utf-8")
        original_renameat2(
            source_fd, source_name, destination_fd, destination_name, flags
        )

    monkeypatch.setattr(tree_management, "_renameat2", swap_leaf_then_rename)

    with pytest.raises(SetforgeError):
        apply_tree(plan, live, policy)

    assert swapped
    assert current.read_text(encoding="utf-8") == "external\n"


def test_apply_tree_uses_held_root_descriptor_after_lexical_swap(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    source.mkdir()
    live.mkdir()
    (source / "value").write_text("wanted\n", encoding="utf-8")
    (live / "value").write_text("planned\n", encoding="utf-8")
    policy = TreePolicy()
    plan = plan_tree(
        scan_tree(source, policy, capture_payloads=True),
        scan_tree(live, policy).inventory,
        None,
        policy,
    )
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(live, flags)
    detached = tmp_path / "detached"
    live.rename(detached)
    live.mkdir()
    (live / "value").write_text("replacement\n", encoding="utf-8")
    try:
        apply_tree(plan, live, policy, anchor_fd=root_fd)
    finally:
        os.close(root_fd)

    assert (live / "value").read_text(encoding="utf-8") == "replacement\n"
    assert (detached / "value").read_text(encoding="utf-8") == "wanted\n"


def test_apply_tree_refuses_retargeted_symlink_before_owned_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    prior_root = tmp_path / "prior"
    for root in (source, live, prior_root):
        root.mkdir()
    (live / "link").symlink_to("old-target")
    (prior_root / "link").symlink_to("old-target")
    policy = TreePolicy(
        orphans=TreeOrphanPolicy.REMOVE_OWNED,
        symlinks=TreeSymlinkPolicy.PRESERVE,
    )
    plan = plan_tree(
        scan_tree(source, policy, capture_payloads=True),
        scan_tree(live, policy).inventory,
        scan_tree(prior_root, policy).inventory,
        policy,
    )
    original_renameat2 = tree_management._renameat2
    swapped = False

    def retarget_then_rename(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
        flags: int,
    ) -> None:
        nonlocal swapped
        if not swapped and source_name == "link":
            swapped = True
            (live / "link").unlink()
            (live / "link").symlink_to("new-target")
        original_renameat2(
            source_fd, source_name, destination_fd, destination_name, flags
        )

    monkeypatch.setattr(tree_management, "_renameat2", retarget_then_rename)

    with pytest.raises(SetforgeError, match="changed before removal"):
        apply_tree(plan, live, policy)

    assert swapped
    assert (live / "link").readlink() == Path("new-target")


def test_apply_tree_refuses_concurrent_directory_create_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    source.mkdir()
    live.mkdir()
    (source / "nested").mkdir()
    policy = TreePolicy()
    plan = plan_tree(
        scan_tree(source, policy, capture_payloads=True),
        scan_tree(live, policy).inventory,
        None,
        policy,
    )
    original_create = tree_management._create_directory_at

    def collide(root_fd: int, relative: str, mode: int) -> None:
        (live / relative).mkdir()
        (live / relative / "external").write_text("keep\n", encoding="utf-8")
        original_create(root_fd, relative, mode)

    monkeypatch.setattr(tree_management, "_create_directory_at", collide)

    with pytest.raises(SetforgeError, match="changed during apply"):
        apply_tree(plan, live, policy)

    assert (live / "nested" / "external").read_text(encoding="utf-8") == "keep\n"


def test_apply_tree_refuses_replaced_directory_before_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    (source / "nested").mkdir(parents=True)
    (live / "nested").mkdir(parents=True)
    (source / "nested").chmod(0o700)
    (live / "nested").chmod(0o755)
    policy = TreePolicy()
    plan = plan_tree(
        scan_tree(source, policy, capture_payloads=True),
        scan_tree(live, policy).inventory,
        None,
        policy,
    )
    original_chmod = tree_management._chmod_directory_at

    def swap(root_fd: int, relative: str, expected: TreeEntry, mode: int) -> None:
        (live / relative).rename(live / "detached")
        (live / relative).mkdir()
        (live / relative).chmod(0o711)
        original_chmod(root_fd, relative, expected, mode)

    monkeypatch.setattr(tree_management, "_chmod_directory_at", swap)

    with pytest.raises(SetforgeError, match="changed before chmod"):
        apply_tree(plan, live, policy)

    assert stat.S_IMODE((live / "nested").stat().st_mode) == 0o711


def test_apply_tree_restores_directory_when_child_appears_after_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    prior_root = tmp_path / "prior"
    source.mkdir()
    (live / "orphan").mkdir(parents=True)
    (prior_root / "orphan").mkdir(parents=True)
    policy = TreePolicy(orphans=TreeOrphanPolicy.REMOVE_OWNED)
    plan = plan_tree(
        scan_tree(source, policy, capture_payloads=True),
        scan_tree(live, policy).inventory,
        scan_tree(prior_root, policy).inventory,
        policy,
    )
    original_renameat2 = tree_management._renameat2
    injected = False

    def add_child_after_isolation(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
        flags: int,
    ) -> None:
        nonlocal injected
        original_renameat2(
            source_fd, source_name, destination_fd, destination_name, flags
        )
        if not injected and source_name == "orphan":
            injected = True
            (live / destination_name / "external").write_text(
                "keep\n", encoding="utf-8"
            )

    monkeypatch.setattr(tree_management, "_renameat2", add_child_after_isolation)

    with pytest.raises(SetforgeError, match="unsafe managed tree removal"):
        apply_tree(plan, live, policy)

    assert (live / "orphan" / "external").read_text(encoding="utf-8") == "keep\n"
    assert not list(live.glob(".orphan.setforge-remove-*"))


def test_apply_tree_refuses_higher_anchor_root_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "value").write_text("wanted\n", encoding="utf-8")
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    destination = anchor / "nested" / "live"
    policy = TreePolicy()
    plan = plan_tree(
        scan_tree(source, policy, capture_payloads=True),
        scan_tree(destination, policy).inventory,
        None,
        policy,
    )
    anchor_fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY)
    original_verify = tree_management._verify_relative_binding

    def swap_before_verify(
        supplied_anchor_fd: int,
        relative_parts: tuple[str, ...],
        expected_fd: int,
    ) -> None:
        destination.rename(anchor / "detached")
        destination.mkdir()
        (destination / "replacement").write_text("keep\n", encoding="utf-8")
        original_verify(supplied_anchor_fd, relative_parts, expected_fd)

    monkeypatch.setattr(tree_management, "_verify_relative_binding", swap_before_verify)
    try:
        with pytest.raises(SetforgeError, match="root binding changed"):
            apply_tree(
                plan,
                destination,
                policy,
                anchor_fd=anchor_fd,
                anchor_relative=("nested", "live"),
            )
    finally:
        os.close(anchor_fd)

    assert (destination / "replacement").read_text(encoding="utf-8") == "keep\n"


def _reject_rename_flags(
    monkeypatch: pytest.MonkeyPatch, error: int = errno.EINVAL
) -> None:
    def reject(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
        flags: int,
    ) -> None:
        del source_fd, source_name, destination_fd, flags
        raise OSError(error, os.strerror(error), destination_name)

    monkeypatch.setattr(tree_management, "_renameat2", reject)


def _reserved_leftovers(root: Path) -> list[Path]:
    return [path for path in root.rglob(".*") if ".setforge-" in path.name]


@pytest.mark.parametrize("error", [errno.EINVAL, errno.ENOTSUP, errno.ENOSYS])
def test_apply_tree_publishes_when_filesystem_rejects_rename_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    prior_root = tmp_path / "prior"
    for root in (source, live, prior_root):
        root.mkdir()
    (source / "nested").mkdir()
    (source / "nested" / "created").write_text("created\n", encoding="utf-8")
    (source / "link").symlink_to("created-target")
    (source / "changed").write_text("wanted\n", encoding="utf-8")
    (source / "relinked").symlink_to("new-target")
    (live / "changed").write_text("planned\n", encoding="utf-8")
    (live / "relinked").symlink_to("old-target")
    for root in (live, prior_root):
        (root / "orphan").write_text("owned\n", encoding="utf-8")
        (root / "orphan-dir").mkdir()
        (root / "orphan-link").symlink_to("owned-target")
    policy = TreePolicy(
        orphans=TreeOrphanPolicy.REMOVE_OWNED,
        symlinks=TreeSymlinkPolicy.PRESERVE,
    )
    desired = scan_tree(source, policy, capture_payloads=True)
    plan = plan_tree(
        desired,
        scan_tree(live, policy).inventory,
        scan_tree(prior_root, policy).inventory,
        policy,
    )
    fresh = tmp_path / "fresh"
    _reject_rename_flags(monkeypatch, error)

    applied = apply_tree(plan, live, policy)
    created = apply_tree(
        plan_tree(desired, scan_tree(fresh, policy).inventory, None, policy),
        fresh,
        policy,
    )

    for root, inventory in ((live, applied), (fresh, created)):
        assert (root / "nested" / "created").read_text(encoding="utf-8") == "created\n"
        assert (root / "link").readlink() == Path("created-target")
        assert (root / "changed").read_text(encoding="utf-8") == "wanted\n"
        assert (root / "relinked").readlink() == Path("new-target")
        assert inventory.entries == desired.inventory.entries
        assert not _reserved_leftovers(root)
    assert not any((live / name).is_symlink() for name in ("orphan", "orphan-link"))
    assert not any((live / name).exists() for name in ("orphan", "orphan-dir"))


@pytest.mark.parametrize("operation", ["create", "update", "remove"])
def test_apply_tree_without_rename_flags_keeps_concurrent_leaf_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    prior_root = tmp_path / "prior"
    for root in (source, live, prior_root):
        root.mkdir()
    current = live / "value"
    if operation == "create":
        (source / "value").write_text("wanted\n", encoding="utf-8")
    elif operation == "update":
        (source / "value").write_text("wanted\n", encoding="utf-8")
        current.write_text("planned\n", encoding="utf-8")
    else:
        current.write_text("owned\n", encoding="utf-8")
        (prior_root / "value").write_text("owned\n", encoding="utf-8")
    policy = TreePolicy(orphans=TreeOrphanPolicy.REMOVE_OWNED)
    plan = plan_tree(
        scan_tree(source, policy, capture_payloads=True),
        scan_tree(live, policy).inventory,
        scan_tree(prior_root, policy).inventory,
        policy,
    )
    swapped = False

    def swap_leaf_then_reject(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
        flags: int,
    ) -> None:
        nonlocal swapped
        del source_fd, destination_fd, flags
        if not swapped and "value" in (source_name, destination_name):
            swapped = True
            current.write_text("external\n", encoding="utf-8")
        raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), destination_name)

    monkeypatch.setattr(tree_management, "_renameat2", swap_leaf_then_reject)

    with pytest.raises(SetforgeError):
        apply_tree(plan, live, policy)

    assert swapped
    assert current.read_text(encoding="utf-8") == "external\n"
    assert not _reserved_leftovers(live)


def test_apply_tree_without_rename_flags_keeps_both_sides_of_racing_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    source.mkdir()
    live.mkdir()
    (source / "value").write_text("wanted\n", encoding="utf-8")
    (live / "value").write_text("planned\n", encoding="utf-8")
    policy = TreePolicy()
    plan = plan_tree(
        scan_tree(source, policy, capture_payloads=True),
        scan_tree(live, policy).inventory,
        None,
        policy,
    )
    _reject_rename_flags(monkeypatch)
    original_entry_at = tree_management._entry_at

    def recreate_while_isolated(parent_fd: int, name: str, relative: str) -> TreeEntry:
        entry = original_entry_at(parent_fd, name, relative)
        (live / "value").write_text("racer\n", encoding="utf-8")
        return entry

    monkeypatch.setattr(tree_management, "_entry_at", recreate_while_isolated)

    with pytest.raises(SetforgeError, match=r"retained as \.value\.setforge-remove"):
        apply_tree(plan, live, policy)

    assert (live / "value").read_text(encoding="utf-8") == "racer\n"
    assert (live / ".value.setforge-remove").read_text(encoding="utf-8") == "planned\n"


def test_apply_tree_without_rename_flags_restores_directory_with_new_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    prior_root = tmp_path / "prior"
    source.mkdir()
    (live / "orphan").mkdir(parents=True)
    (prior_root / "orphan").mkdir(parents=True)
    policy = TreePolicy(orphans=TreeOrphanPolicy.REMOVE_OWNED)
    plan = plan_tree(
        scan_tree(source, policy, capture_payloads=True),
        scan_tree(live, policy).inventory,
        scan_tree(prior_root, policy).inventory,
        policy,
    )
    _reject_rename_flags(monkeypatch)
    original_entry_at = tree_management._entry_at

    def add_child_after_isolation(
        parent_fd: int, name: str, relative: str
    ) -> TreeEntry:
        entry = original_entry_at(parent_fd, name, relative)
        (live / name / "external").write_text("keep\n", encoding="utf-8")
        return entry

    monkeypatch.setattr(tree_management, "_entry_at", add_child_after_isolation)

    with pytest.raises(SetforgeError, match="unsafe managed tree removal"):
        apply_tree(plan, live, policy)

    assert (live / "orphan" / "external").read_text(encoding="utf-8") == "keep\n"
    assert not _reserved_leftovers(live)


def test_apply_tree_names_filesystem_without_rename_flags_or_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    source.mkdir()
    live.mkdir()
    (source / "value").write_text("wanted\n", encoding="utf-8")
    policy = TreePolicy()
    plan = plan_tree(
        scan_tree(source, policy, capture_payloads=True),
        scan_tree(live, policy).inventory,
        None,
        policy,
    )
    _reject_rename_flags(monkeypatch)

    def refuse_link(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(os, "link", refuse_link)

    with pytest.raises(SetforgeError, match="neither rename flags nor hard links"):
        apply_tree(plan, live, policy)

    assert not (live / "value").exists()
    assert not _reserved_leftovers(live)


def test_apply_tree_falls_back_only_when_rename_flags_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    live = tmp_path / "live"
    source.mkdir()
    live.mkdir()
    (source / "value").write_text("wanted\n", encoding="utf-8")
    (live / "value").write_text("planned\n", encoding="utf-8")
    policy = TreePolicy()
    plan = plan_tree(
        scan_tree(source, policy, capture_payloads=True),
        scan_tree(live, policy).inventory,
        None,
        policy,
    )
    _reject_rename_flags(monkeypatch, errno.EACCES)

    with pytest.raises(SetforgeError, match="changed during apply"):
        apply_tree(plan, live, policy)

    assert (live / "value").read_text(encoding="utf-8") == "planned\n"
    assert not _reserved_leftovers(live)


def test_inventory_codec_rejects_corruption(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "x").write_text("x", encoding="utf-8")
    inventory = scan_tree(root, TreePolicy()).inventory
    encoded = dumps_inventory(inventory)

    assert loads_inventory(encoded) == inventory
    with pytest.raises(InvariantViolation, match="fingerprint mismatch"):
        loads_inventory(encoded.replace(inventory.fingerprint, "0" * 64))


def _legacy_inventory_text(document: dict[str, object]) -> str:
    payload = {key: document[key] for key in ("entries", "root_mode", "root_present")}
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    document["fingerprint"] = hashlib.sha256(encoded).hexdigest()
    return json.dumps(document)


@pytest.mark.parametrize("alias", ["./x", ".//x"])
@pytest.mark.parametrize("boundary", ["read", "write"])
def test_inventory_aliases_do_not_retain_owned_orphans(
    tmp_path: Path, alias: str, boundary: str
) -> None:
    live, source = tmp_path / "live", tmp_path / "source"
    live.mkdir()
    source.mkdir()
    child = live / "x"
    child.write_bytes(b"owned")
    policy = TreePolicy(orphans=TreeOrphanPolicy.REMOVE_OWNED)
    inventory = scan_tree(live, policy).inventory
    document = json.loads(dumps_inventory(inventory))
    document["entries"][0]["path"] = alias
    document["owned_paths"] = [alias]
    encoded = _legacy_inventory_text(document)
    if boundary == "write":
        aliased = replace(
            inventory,
            entries=(replace(inventory.entries[0], path=alias),),
            owned_paths=(alias,),
            fingerprint=str(document["fingerprint"]),
        )
        encoded = dumps_inventory(aliased)
        assert json.loads(encoded)["entries"][0]["path"] == "x"
    prior = loads_inventory(encoded)
    desired = scan_tree(source, policy, capture_payloads=True)
    plan = plan_tree(desired, inventory, prior, policy)

    result = apply_tree(plan, live, policy)

    assert not child.exists()
    assert result.entries == ()
    assert prior.owned_paths == ("x",)


@pytest.mark.parametrize("conflicting", [False, True])
def test_inventory_canonical_duplicates_require_matching_payload(
    tmp_path: Path, conflicting: bool
) -> None:
    (tmp_path / "x").write_bytes(b"owned")
    document = json.loads(dumps_inventory(scan_tree(tmp_path, TreePolicy()).inventory))
    entry = document["entries"][0]
    alias = dict(entry, path="./x")
    if conflicting:
        alias["content_hash"] = "0" * 64
    document["entries"] = [alias, entry]
    document["owned_paths"] = ["./x", "x"]
    encoded = _legacy_inventory_text(document)
    if conflicting:
        with pytest.raises(InvariantViolation, match="conflicting canonical"):
            loads_inventory(encoded)
    else:
        inventory = loads_inventory(encoded)
        assert [item.path for item in inventory.entries] == ["x"]
        assert inventory.owned_paths == ("x",)
        assert loads_inventory(dumps_inventory(inventory)) == inventory


@pytest.mark.parametrize("path", ["../x", "a/../x", "/x", ".", "", "\x00"])
def test_inventory_rejects_unsafe_entry_path(path: str) -> None:
    with pytest.raises(InvariantViolation, match="unsafe tree inventory path"):
        TreeEntry(path, TreeEntryKind.FILE, 0o644, content_hash="0" * 64)
