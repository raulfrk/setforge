from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from setforge import atomicio
from setforge.errors import SetforgeError
from setforge.git_visibility import (
    VisibilityClaim,
    apply_claims,
    claim_id,
    info_exclude_path,
    plan_claims,
    plan_file_visibility,
    read_claims,
)


def _git(path: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=check,
        text=True,
        capture_output=True,
    ).stdout


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    return path


def test_claim_identity_is_stable_across_processes_and_versions() -> None:
    assert (
        claim_id(
            target_git_dir=Path("/repo/.git/worktrees/demo"),
            profile="base",
            relative_path="AGENTS.md",
        )
        == "989e770cf02ffe935d0416485f37aa69a59fe731e0404fa34a56e60ca9fab29a"
    )


@pytest.mark.parametrize(
    "relative",
    [
        "plain.txt",
        "dir/space name.txt",
        "#leading",
        "!leading",
        "slash\\name",
        "tail ",
        "literal*.txt",
        "literal?.txt",
        "literal[ab].txt",
    ],
)
def test_exact_patterns_hide_only_the_claimed_path(
    tmp_path: Path, relative: str
) -> None:
    target = _repo(tmp_path / "repo")
    destination = target / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("private")
    sibling = target / relative.replace("*", "X").replace("?", "Q").replace("[ab]", "a")
    if sibling != destination:
        sibling.write_text("public")
    claim = VisibilityClaim("a" * 64, relative)

    apply_claims(plan_claims(target, add=(claim,)))

    assert (
        _git(target, "--literal-pathspecs", "status", "--short", "--", relative) == ""
    )
    assert _git(target, "check-ignore", "--", relative).strip()
    if sibling != destination:
        assert _git(
            target,
            "--literal-pathspecs",
            "status",
            "--short",
            "--",
            sibling.relative_to(target).as_posix(),
        )


def test_claims_share_linked_worktree_exclude_and_restore_exact_bytes_and_mode(
    tmp_path: Path,
) -> None:
    target = _repo(tmp_path / "repo")
    _git(
        target,
        "-c",
        "user.name=SetForge Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "base",
    )
    linked = tmp_path / "linked"
    _git(target, "worktree", "add", "-q", "-b", "linked", str(linked))
    exclude = info_exclude_path(target)
    exclude.write_bytes(b"user-rule-without-newline")
    exclude.chmod(0o640)
    original = exclude.read_bytes()
    mode = exclude.stat().st_mode & 0o777
    first = VisibilityClaim("a" * 64, "shared.txt")
    second = VisibilityClaim("b" * 64, "shared.txt")

    apply_claims(plan_claims(target, add=(first, second)))
    assert info_exclude_path(linked) == exclude
    apply_claims(plan_claims(linked, remove=(first,)))
    assert read_claims(target)[3] == (second,)
    apply_claims(plan_claims(target, remove=(second,)))

    assert exclude.read_bytes() == original
    assert exclude.stat().st_mode & 0o777 == mode


def test_transition_round_trips_untracked_file_without_staging_or_byte_change(
    tmp_path: Path,
) -> None:
    target = _repo(tmp_path / "repo")
    destination = target / "AGENTS.md"
    destination.write_bytes(b"private\x00content")
    claim = VisibilityClaim("c" * 64, "AGENTS.md")

    apply_claims(
        plan_file_visibility(
            target, claim=claim, hidden=True, tracked_sibling_paths=frozenset()
        )
    )
    assert _git(target, "status", "--short") == ""
    apply_claims(
        plan_file_visibility(
            target, claim=claim, hidden=False, tracked_sibling_paths=frozenset()
        )
    )

    assert destination.read_bytes() == b"private\x00content"
    assert _git(target, "status", "--short") == "?? AGENTS.md\n"
    assert _git(target, "diff", "--cached") == ""


def test_transition_refuses_committed_and_unrepresentable_paths(tmp_path: Path) -> None:
    target = _repo(tmp_path / "repo")
    (target / "tracked.txt").write_text("team")
    _git(target, "add", "tracked.txt")
    with pytest.raises(SetforgeError, match="G5"):
        plan_file_visibility(
            target,
            claim=VisibilityClaim("d" * 64, "tracked.txt"),
            hidden=True,
            tracked_sibling_paths=frozenset(),
        )

    with pytest.raises(SetforgeError, match="line break"):
        plan_claims(target, add=(VisibilityClaim("e" * 64, "bad\nname"),))


@pytest.mark.parametrize("relative", [".", "../escape", "/absolute"])
def test_transition_refuses_non_normalized_paths(tmp_path: Path, relative: str) -> None:
    target = _repo(tmp_path / "repo")
    with pytest.raises(SetforgeError, match="not normalized"):
        plan_file_visibility(
            target,
            claim=VisibilityClaim("e" * 64, relative),
            hidden=True,
            tracked_sibling_paths=frozenset(),
        )


def test_transition_to_tracked_refuses_a_sibling_hidden_claim(tmp_path: Path) -> None:
    target = _repo(tmp_path / "repo")
    (target / "shared.txt").write_text("private")
    sibling = VisibilityClaim("a" * 64, "shared.txt")
    requested = VisibilityClaim("b" * 64, "shared.txt")
    apply_claims(
        plan_file_visibility(
            target, claim=sibling, hidden=True, tracked_sibling_paths=frozenset()
        )
    )

    with pytest.raises(SetforgeError, match="another linked-worktree claim"):
        plan_file_visibility(
            target,
            claim=requested,
            hidden=False,
            tracked_sibling_paths=frozenset(),
        )


def test_transition_requires_explicit_tracked_sibling_snapshot(tmp_path: Path) -> None:
    target = _repo(tmp_path / "repo")
    (target / "shared.txt").write_text("private")

    with pytest.raises(SetforgeError, match="tracked linked-worktree claim"):
        plan_file_visibility(
            target,
            claim=VisibilityClaim("b" * 64, "shared.txt"),
            hidden=True,
            tracked_sibling_paths=frozenset({"shared.txt"}),
        )


def test_apply_revalidates_exclude_bytes_and_mode_independently(tmp_path: Path) -> None:
    target = _repo(tmp_path / "repo")
    claim = VisibilityClaim("e" * 64, "private.txt")
    plan = plan_claims(target, add=(claim,))
    plan.exclude_path.write_bytes(plan.before + b"user change\n")
    with pytest.raises(SetforgeError, match="changed before apply"):
        apply_claims(plan)

    plan.exclude_path.write_bytes(plan.before)
    mode_plan = plan_claims(target, add=(claim,))
    mode_plan.exclude_path.chmod(mode_plan.before_mode ^ 0o100)
    with pytest.raises(SetforgeError, match="changed before apply"):
        apply_claims(mode_plan)


def test_corrupt_or_ambiguous_marker_fails_closed(tmp_path: Path) -> None:
    target = _repo(tmp_path / "repo")
    exclude = info_exclude_path(target)
    original = exclude.read_bytes()
    exclude.write_bytes(
        original + b"# >>> setforge project visibility v1 >>>\ninvalid\n"
    )

    with pytest.raises(SetforgeError, match="missing, duplicated, or ambiguous"):
        plan_claims(target, add=(VisibilityClaim("f" * 64, "private.txt"),))


def test_exclude_symlink_fails_closed_without_writing_target(tmp_path: Path) -> None:
    target = _repo(tmp_path / "repo")
    exclude = info_exclude_path(target)
    external = tmp_path / "external"
    external.write_text("do not touch\n")
    exclude.unlink()
    exclude.symlink_to(external)

    with pytest.raises(SetforgeError, match="cannot be read"):
        plan_claims(target, add=(VisibilityClaim("f" * 64, "private.txt"),))
    assert external.read_text() == "do not touch\n"


def test_apply_is_confined_when_info_parent_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _repo(tmp_path / "repo")
    plan = plan_claims(target, add=(VisibilityClaim("f" * 64, "private.txt"),))
    info = plan.exclude_path.parent
    displaced = info.with_name("info-displaced")
    external = tmp_path / "external-info"
    external.mkdir()
    external_exclude = external / "exclude"
    external_exclude.write_text("do not touch\n")
    original_write = atomicio.atomic_write_bytes_at

    def swap_then_write(
        parent_fd: int, name: str, data: bytes, *, mode: int = 0o600
    ) -> None:
        info.rename(displaced)
        info.symlink_to(external, target_is_directory=True)
        original_write(parent_fd, name, data, mode=mode)

    monkeypatch.setattr(atomicio, "atomic_write_bytes_at", swap_then_write)
    with pytest.raises(SetforgeError, match="parent changed before apply"):
        apply_claims(plan)

    assert external_exclude.read_text() == "do not touch\n"


def test_apply_rejects_replaced_info_parent_with_identical_exclude(
    tmp_path: Path,
) -> None:
    target = _repo(tmp_path / "repo")
    plan = plan_claims(target, add=(VisibilityClaim("f" * 64, "private.txt"),))
    info = plan.exclude_path.parent
    displaced = info.with_name("info-displaced")
    info.rename(displaced)
    info.mkdir()
    replacement = info / "exclude"
    replacement.write_bytes(plan.before)
    replacement.chmod(plan.before_mode)

    with pytest.raises(SetforgeError, match="parent changed before apply"):
        apply_claims(plan)

    assert replacement.read_bytes() == plan.before
