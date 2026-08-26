import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from setforge.config import (
    Config,
    ProjectFile,
    ProjectVisibility,
    resolve_project_profile,
)
from setforge.errors import ConfigError


def _config(project_profiles: dict[str, object]) -> Config:
    return Config.model_validate(
        {
            "tracked_files": {},
            "profiles": {},
            "project_profiles": project_profiles,
        }
    )


def test_project_profile_inheritance_replaces_destination_and_keeps_provenance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    for profile, filename in (
        ("base", "base.md"),
        ("base", "guide.md"),
        ("child", "child.md"),
        ("child", "readme.md"),
    ):
        source = root / profile / filename
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(filename)
    config = _config(
        {
            "base": {
                "default_visibility": "tracked",
                "files": {
                    "instructions": {"src": "base.md", "dst": "CLAUDE.md"},
                    "guide": {"src": "guide.md", "dst": "docs/guide.md"},
                },
            },
            "child": {
                "extends": "base",
                "files": {
                    "instructions": {"src": "child.md", "dst": "./CLAUDE.md"},
                    "readme": {"src": "readme.md", "dst": "README.md"},
                },
            },
        }
    )

    resolved = resolve_project_profile(config, "child", tmp_path)

    assert resolved.default_visibility is ProjectVisibility.TRACKED
    assert [(item.id, item.declaring_profile, item.dst) for item in resolved.files] == [
        ("instructions", "child", Path("CLAUDE.md")),
        ("guide", "base", Path("docs/guide.md")),
        ("readme", "child", Path("README.md")),
    ]
    assert resolved.files[0].src == (root / "child" / "child.md").resolve()
    assert resolved.files[1].src == (root / "base" / "guide.md").resolve()


def test_project_profile_defaults_visibility_after_inheritance(tmp_path: Path) -> None:
    config = _config({"empty": {}})

    resolved = resolve_project_profile(config, "empty", tmp_path)

    assert resolved.default_visibility is ProjectVisibility.HIDDEN
    assert resolved.files == ()


@pytest.mark.parametrize("dst", ["", ".", "../escape", "/absolute", ".git/config"])
def test_project_profile_rejects_unsafe_destination(dst: str) -> None:
    with pytest.raises(ValidationError, match="destination"):
        _config({"bad": {"files": {"bad": {"src": "file", "dst": dst}}}})


def test_project_profile_rejects_duplicate_normalized_destination() -> None:
    with pytest.raises(ValidationError, match="duplicate normalized destination"):
        _config(
            {
                "bad": {
                    "files": {
                        "one": {"src": "one", "dst": "docs/guide.md"},
                        "two": {"src": "two", "dst": "docs/./guide.md"},
                    }
                }
            }
        )


def test_project_profile_reports_unknown_parent_and_cycle(tmp_path: Path) -> None:
    unknown = _config({"child": {"extends": "missing"}})
    with pytest.raises(ConfigError, match="project profile not found: missing"):
        resolve_project_profile(unknown, "child", tmp_path)

    cyclic = _config({"a": {"extends": "b"}, "b": {"extends": "a"}})
    with pytest.raises(ConfigError, match=r"project profile cycle: a → b → a"):
        resolve_project_profile(cyclic, "a", tmp_path)


def test_project_profile_rejects_missing_directory_and_symlink_escape(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project" / "p"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (root / "escape").symlink_to(outside)
    (root / "directory").mkdir()
    config = _config(
        {
            "p": {
                "files": {
                    "missing": {"src": "missing", "dst": "missing"},
                    "directory": {"src": "directory", "dst": "directory"},
                    "escape": {"src": "escape", "dst": "escape"},
                }
            }
        }
    )

    cases = {
        "does not exist": "missing",
        "regular file": "directory",
        "escapes source root": "escape",
    }
    for message, file_id in cases.items():
        reduced = config.model_copy(
            update={
                "project_profiles": {
                    "p": config.project_profiles["p"].model_copy(
                        update={
                            "files": {
                                file_id: config.project_profiles["p"].files[file_id]
                            }
                        }
                    )
                }
            }
        )
        with pytest.raises(ConfigError, match=message):
            resolve_project_profile(reduced, "p", tmp_path)


def test_project_profile_rejects_symlinked_declaring_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_text("outside")
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "p").symlink_to(outside, target_is_directory=True)
    config = _config({"p": {"files": {"file": {"src": "file", "dst": "file"}}}})

    with pytest.raises(ConfigError, match="source root must not be a symlink"):
        resolve_project_profile(config, "p", tmp_path)


def test_project_profile_rejects_sibling_symlinked_declaring_root(
    tmp_path: Path,
) -> None:
    sibling = tmp_path / "project" / "sibling"
    sibling.mkdir(parents=True)
    (sibling / "file").write_text("sibling")
    (tmp_path / "project" / "alias").symlink_to(sibling, target_is_directory=True)
    config = _config({"alias": {"files": {"file": {"src": "file", "dst": "file"}}}})

    with pytest.raises(ConfigError, match="source root must not be a symlink"):
        resolve_project_profile(config, "alias", tmp_path)


def test_project_profile_wraps_symlink_loop(tmp_path: Path) -> None:
    source_root = tmp_path / "project" / "p"
    source_root.mkdir(parents=True)
    (source_root / "loop").symlink_to("loop")
    config = _config({"p": {"files": {"loop": {"src": "loop", "dst": "loop"}}}})

    with pytest.raises(ConfigError, match="source cannot be resolved"):
        resolve_project_profile(config, "p", tmp_path)


def test_project_profile_wraps_config_root_symlink_loop(tmp_path: Path) -> None:
    config_root = tmp_path / "loop"
    config_root.symlink_to(config_root, target_is_directory=True)
    config = _config({"p": {"files": {"file": {"src": "file", "dst": "file"}}}})

    with pytest.raises(ConfigError, match="cannot resolve project source directory"):
        resolve_project_profile(config, "p", config_root)


def test_project_profile_rejects_fifo_source(tmp_path: Path) -> None:
    source = tmp_path / "project" / "p" / "pipe"
    source.parent.mkdir(parents=True)
    os.mkfifo(source)
    config = _config({"p": {"files": {"pipe": {"src": "pipe", "dst": "pipe"}}}})

    with pytest.raises(ConfigError, match="not a regular file"):
        resolve_project_profile(config, "p", tmp_path)


@pytest.mark.parametrize("src", ["/absolute", "../traversal", "C:\\absolute"])
def test_project_profile_rejects_unsafe_source(src: str) -> None:
    with pytest.raises(ValidationError, match="project file source"):
        _config({"p": {"files": {"file": {"src": src, "dst": "file"}}}})


def test_project_profile_rejects_invalid_visibility() -> None:
    with pytest.raises(ValidationError, match="default_visibility"):
        _config({"p": {"default_visibility": "private"}})


@given(
    parts=st.lists(
        st.text(
            alphabet=st.characters(
                whitelist_categories=("Ll", "Lu", "Nd"),
                whitelist_characters="-_",
            ),
            min_size=1,
            max_size=12,
        ).filter(lambda part: part != ".git"),
        min_size=1,
        max_size=5,
    )
)
def test_project_destination_preserves_safe_relative_components(
    parts: list[str],
) -> None:
    destination = "/".join(parts)

    project_file = ProjectFile(src=Path("source"), dst=Path(destination))

    assert project_file.dst == Path(*parts)
    assert not project_file.dst.is_absolute()
    assert ".." not in project_file.dst.parts


@given(depth=st.integers(min_value=1, max_value=20))
def test_project_profile_chain_keeps_override_position_and_visibility(
    depth: int,
) -> None:
    with TemporaryDirectory() as directory:
        config_root = Path(directory)
        profiles: dict[str, object] = {}
        for index in range(depth):
            name = f"p{index}"
            source = config_root / "project" / name / "source"
            source.parent.mkdir(parents=True)
            source.write_text(name)
            profiles[name] = {
                "extends": f"p{index - 1}" if index else None,
                "default_visibility": "tracked" if index == 0 else None,
                "files": {"shared": {"src": "source", "dst": "shared"}},
            }
        config = _config(profiles)

        resolved = resolve_project_profile(config, f"p{depth - 1}", config_root)

        assert resolved.default_visibility is ProjectVisibility.TRACKED
        assert len(resolved.files) == 1
        assert resolved.files[0].declaring_profile == f"p{depth - 1}"
        assert resolved.files[0].dst == Path("shared")
