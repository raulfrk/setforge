from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from setforge.errors import SetforgeError
from setforge.git_overlay import (
    OverlayClaim,
    apply_overlay_git,
    overlay_claim_id,
    plan_overlay_git,
)


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=SetForge Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
    )
    return path


def _git_dir(target: Path) -> Path:
    raw = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--git-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    path = Path(raw)
    return (target / path).resolve() if not path.is_absolute() else path.resolve()


def _claim(target: Path, profile: str) -> OverlayClaim:
    relative = "AGENTS.md"
    return OverlayClaim(
        overlay_claim_id(
            git_dir=_git_dir(target),
            profile=profile,
            relative_path=relative,
        ),
        relative,
    )


def test_linked_overlay_claims_reference_count_shared_git_plumbing(
    tmp_path: Path,
) -> None:
    target = _repo(tmp_path / "target")
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(target), "worktree", "add", "-q", str(linked)],
        check=True,
    )
    first = _claim(target, "first")
    second = _claim(linked, "second")

    apply_overlay_git(plan_overlay_git(target, add=(first,)))
    apply_overlay_git(plan_overlay_git(linked, add=(second,)))
    partial = plan_overlay_git(target, remove=(first,))
    apply_overlay_git(partial)

    attributes = partial.attributes_path.read_text()
    assert first.claim_id not in attributes
    assert second.claim_id in attributes
    assert (
        subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "config",
                "--local",
                "--get",
                "filter.setforge-project.required",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "true"
    )

    final = plan_overlay_git(linked, remove=(second,))
    apply_overlay_git(final)
    assert b"setforge project overlays" not in final.attributes_path.read_bytes()
    missing = subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "config",
            "--local",
            "--get",
            "filter.setforge-project.process",
        ],
        check=False,
        capture_output=True,
    )
    assert missing.returncode == 1


def test_overlay_plan_refuses_incompatible_existing_driver(tmp_path: Path) -> None:
    target = _repo(tmp_path / "target")
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "config",
            "filter.setforge-project.process",
            "different-command",
        ],
        check=True,
    )

    with pytest.raises(SetforgeError, match="incompatible"):
        plan_overlay_git(target, add=(_claim(target, "demo"),))


def test_overlay_plan_refuses_non_string_claim_path(tmp_path: Path) -> None:
    target = _repo(tmp_path / "target")
    attributes = _git_dir(target) / "info" / "attributes"
    attributes.write_text(
        "\n# >>> setforge project overlays v1 >>>\n"
        f"# claim {'0' * 64} 1\n"
        "/AGENTS.md filter=setforge-project\n"
        "# <<< setforge project overlays v1 <<<\n"
    )

    with pytest.raises(SetforgeError, match="claim path is invalid"):
        plan_overlay_git(target)


def test_overlay_apply_refuses_shared_file_mode_drift(tmp_path: Path) -> None:
    target = _repo(tmp_path / "target")
    plan = plan_overlay_git(target, add=(_claim(target, "demo"),))
    plan.config_path.chmod(0o600 if plan.config_mode != 0o600 else 0o644)

    with pytest.raises(SetforgeError, match="changed before apply"):
        apply_overlay_git(plan)


def test_overlay_apply_refuses_replaced_info_directory(tmp_path: Path) -> None:
    target = _repo(tmp_path / "target")
    plan = plan_overlay_git(target, add=(_claim(target, "demo"),))
    original_info = plan.attributes_path.parent
    displaced_info = original_info.with_name("info-displaced")
    original_info.rename(displaced_info)
    original_info.mkdir()
    sentinel = original_info / "attributes"
    sentinel.write_text("outside sentinel\n")

    with pytest.raises(SetforgeError, match="directory changed before apply"):
        apply_overlay_git(plan)

    assert sentinel.read_text() == "outside sentinel\n"
    assert not (displaced_info / "attributes").exists()


def test_overlay_retains_compatible_preexisting_driver(tmp_path: Path) -> None:
    target = _repo(tmp_path / "target")
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "config",
            "filter.setforge-project.process",
            "setforge project filter-process",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "config",
            "filter.setforge-project.required",
            "true",
        ],
        check=True,
    )
    claim = _claim(target, "demo")

    apply_overlay_git(plan_overlay_git(target, add=(claim,)))
    apply_overlay_git(plan_overlay_git(target, remove=(claim,)))

    assert (
        subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "config",
                "--get",
                "filter.setforge-project.process",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "setforge project filter-process"
    )


@pytest.mark.parametrize("position", ["before", "after"])
def test_overlay_refuses_incompatible_attribute_assignment(
    tmp_path: Path, position: str
) -> None:
    target = _repo(tmp_path / "target")
    claim = _claim(target, "demo")
    attributes = _git_dir(target) / "info" / "attributes"
    if position == "before":
        attributes.write_text("/AGENTS.md filter=other\n")
    else:
        apply_overlay_git(plan_overlay_git(target, add=(claim,)))
        attributes.write_text(attributes.read_text() + "/AGENTS.md filter=other\n")

    before_config = (_git_dir(target) / "config").read_bytes()
    before_attributes = attributes.read_bytes()
    with pytest.raises(SetforgeError, match="incompatible filter attribute"):
        plan_overlay_git(target, add=(claim,))
    assert (_git_dir(target) / "config").read_bytes() == before_config
    assert attributes.read_bytes() == before_attributes
