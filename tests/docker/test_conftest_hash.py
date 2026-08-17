"""Tests for the Docker image hash-input filter.

Covers :func:`tests.docker.image._parse_dockerignore` (pattern
classification) plus the integration behavior that the parsed patterns
keep the docker image-tag hash stable when ephemerals listed in
``.dockerignore`` appear under hash-input dirs.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from tests.docker import image as docker_image
from tests.docker.image import (
    _compute_inputs_hash,
    _iter_hash_input_paths,
    _parse_dockerignore,
)


def test_parse_dockerignore_directory_pattern(tmp_path: Path) -> None:
    """Trailing ``/`` lines classify as directory patterns."""
    di = tmp_path / ".dockerignore"
    di.write_text("htmlcov/\nbuild/\n", encoding="utf-8")
    dirs, suffixes, filenames = _parse_dockerignore(di)
    assert dirs == {"htmlcov", "build"}
    assert suffixes == set()
    assert filenames == set()


def test_parse_dockerignore_suffix_pattern(tmp_path: Path) -> None:
    """Leading ``*`` lines classify as suffix patterns (with the ``*`` stripped)."""
    di = tmp_path / ".dockerignore"
    di.write_text("*.pyc\n*.log\n", encoding="utf-8")
    dirs, suffixes, filenames = _parse_dockerignore(di)
    assert dirs == set()
    assert suffixes == {".pyc", ".log"}
    assert filenames == set()


def test_parse_dockerignore_filename_pattern(tmp_path: Path) -> None:
    """Plain non-glob, non-dir lines classify as filename patterns."""
    di = tmp_path / ".dockerignore"
    di.write_text(".coverage\nTODO\n", encoding="utf-8")
    dirs, suffixes, filenames = _parse_dockerignore(di)
    assert dirs == set()
    assert suffixes == set()
    assert filenames == {".coverage", "TODO"}


def test_parse_dockerignore_skips_comments_and_blank(tmp_path: Path) -> None:
    """``#`` comment lines and blank/whitespace-only lines are skipped."""
    di = tmp_path / ".dockerignore"
    di.write_text(
        "# leading comment\n\n   \n.coverage\n# another comment\nhtmlcov/\n",
        encoding="utf-8",
    )
    dirs, suffixes, filenames = _parse_dockerignore(di)
    assert dirs == {"htmlcov"}
    assert suffixes == set()
    assert filenames == {".coverage"}


def test_parse_dockerignore_missing_file(tmp_path: Path) -> None:
    """A missing .dockerignore returns three empty sets (no exception)."""
    dirs, suffixes, filenames = _parse_dockerignore(tmp_path / "nope.ignore")
    assert dirs == set()
    assert suffixes == set()
    assert filenames == set()


def test_parse_dockerignore_rejects_bare_star(tmp_path: Path) -> None:
    """A bare ``*`` line does not introduce an empty suffix.

    Without this guard, ``line[1:]`` on ``*`` derives ``""`` which would
    match every extensionless file via ``str.endswith("")``.
    """
    dockerignore = tmp_path / ".dockerignore"
    dockerignore.write_text("*\n", encoding="utf-8")
    _dirs, suffixes, _files = docker_image._parse_dockerignore(dockerignore)
    assert "" not in suffixes


def test_parse_dockerignore_survives_malformed_utf8(tmp_path: Path) -> None:
    """Invalid UTF-8 bytes return empty sets instead of raising at import."""
    dockerignore = tmp_path / ".dockerignore"
    dockerignore.write_bytes(b"\xff\xfe")
    dirs, suffixes, files = docker_image._parse_dockerignore(dockerignore)
    assert dirs == set()
    assert suffixes == set()
    assert files == set()


@pytest.fixture
def hash_inputs_in_tmp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Redirect the hash-input globals at a fresh tmp tree.

    Creates a single hash-input directory ``tracked/`` under ``tmp_path``
    with one real file (so the hash is non-empty), then returns the
    ``tracked/`` path so tests can drop synthetic ephemerals into it.
    The ``monkeypatch`` fixture handles teardown.

    Hermetic isolation: ``_DOCKERIGNORE_FILES`` is pinned to ``{".coverage"}``
    so these tests do not depend on the real repo's ``.dockerignore``
    continuing to list ``.coverage``. Other dockerignore-related constants
    (``_DOCKERIGNORE_DIRS``, ``_DOCKERIGNORE_SUFFIXES``) are left untouched
    because no test here exercises them.
    """
    tracked = tmp_path / "tracked"
    tracked.mkdir()
    (tracked / "real.txt").write_text("payload\n", encoding="utf-8")
    monkeypatch.setattr(docker_image, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(docker_image, "_HASH_INPUT_FILES", ())
    monkeypatch.setattr(docker_image, "_HASH_INPUT_DIRS", (tracked,))
    monkeypatch.setattr(docker_image, "_DOCKERIGNORE_FILES", {".coverage"})
    return tracked


def test_iter_hash_input_paths_excludes_coverage_file(
    hash_inputs_in_tmp: Path,
) -> None:
    """A ``.coverage`` file under a hash-input dir is NOT yielded."""
    (hash_inputs_in_tmp / ".coverage").write_text("ephemeral", encoding="utf-8")
    yielded = {p.name for p in _iter_hash_input_paths()}
    assert "real.txt" in yielded
    assert ".coverage" not in yielded


def test_compute_inputs_hash_stable_against_coverage_file(
    hash_inputs_in_tmp: Path,
) -> None:
    """Adding/removing a ``.coverage`` ephemeral does not flip the hash."""
    before = _compute_inputs_hash()
    coverage = hash_inputs_in_tmp / ".coverage"
    coverage.write_text("ephemeral", encoding="utf-8")
    after_add = _compute_inputs_hash()
    coverage.unlink()
    after_remove = _compute_inputs_hash()
    assert before == after_add == after_remove


def test_iter_hash_input_paths_excludes_outside_repo_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlink under a hash-input dir whose target resolves OUTSIDE
    ``REPO_ROOT`` is NOT yielded.

    Locks the ``is_relative_to(REPO_ROOT)`` guard in
    ``_iter_hash_input_paths`` against regressions: removing it would let
    escaping symlinks leak into the hash input set.
    """
    repo_root = tmp_path / "repo"
    inside = repo_root / "inside"
    inside.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("escape", encoding="utf-8")
    link = inside / "link"
    link.symlink_to(outside)
    monkeypatch.setattr(docker_image, "REPO_ROOT", repo_root)
    monkeypatch.setattr(docker_image, "_HASH_INPUT_FILES", ())
    monkeypatch.setattr(docker_image, "_HASH_INPUT_DIRS", (inside,))
    yielded = set(_iter_hash_input_paths())
    assert link.resolve() not in yielded


def test_iter_hash_input_paths_includes_inside_repo_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: a symlink whose target resolves INSIDE ``REPO_ROOT`` — its
    resolved target IS yielded.

    Without this case, the exclude-test alone could pass on a broken guard
    that drops every symlink unconditionally.
    """
    repo_root = tmp_path / "repo"
    inside = repo_root / "inside"
    inside.mkdir(parents=True)
    target = inside / "target.txt"
    target.write_text("kept", encoding="utf-8")
    link = inside / "link"
    link.symlink_to(target)
    monkeypatch.setattr(docker_image, "REPO_ROOT", repo_root)
    monkeypatch.setattr(docker_image, "_HASH_INPUT_FILES", ())
    monkeypatch.setattr(docker_image, "_HASH_INPUT_DIRS", (inside,))
    yielded = set(_iter_hash_input_paths())
    assert target.resolve() in yielded


def test_compute_inputs_hash_changes_on_file_edit_in_input_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editing a file under a single pinned ``_HASH_INPUT_DIRS`` entry flips the hash.

    Mirrors the 4nm monkeypatch convention: redirect ``REPO_ROOT`` at a
    fresh ``tmp_path / "repo"`` AND pin ``_HASH_INPUT_FILES = ()`` so the
    real anchor files (Dockerfile, pyproject.toml, ...) cannot leak into
    the digest from disk. Single-dir scope — cross-entry coverage was
    dropped with the parametrize in the f9ef316 review-fix.
    """
    repo_root = tmp_path / "repo"
    inside = repo_root / "inside"
    inside.mkdir(parents=True)
    target = inside / "file.txt"
    target.write_text("before", encoding="utf-8")
    monkeypatch.setattr(docker_image, "REPO_ROOT", repo_root)
    monkeypatch.setattr(docker_image, "_HASH_INPUT_FILES", ())
    monkeypatch.setattr(docker_image, "_HASH_INPUT_DIRS", (inside,))
    h1 = _compute_inputs_hash()
    target.write_text("after", encoding="utf-8")
    h2 = _compute_inputs_hash()
    assert h1 != h2


def test_compute_inputs_hash_ignores_pycache_additions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding ``__pycache__/x.pyc`` under a hash-input dir does NOT flip the hash.

    Locks the ``__pycache__`` exclusion in ``_iter_hash_input_paths`` —
    without it, every byte-code regeneration during a test run would
    invalidate the docker image-tag cache.
    """
    repo_root = tmp_path / "repo"
    inside = repo_root / "inside"
    inside.mkdir(parents=True)
    (inside / "real.txt").write_text("payload", encoding="utf-8")
    monkeypatch.setattr(docker_image, "REPO_ROOT", repo_root)
    monkeypatch.setattr(docker_image, "_HASH_INPUT_FILES", ())
    monkeypatch.setattr(docker_image, "_HASH_INPUT_DIRS", (inside,))
    h1 = _compute_inputs_hash()
    pycache = inside / "__pycache__"
    pycache.mkdir()
    (pycache / "x.pyc").write_bytes(b"\x00\x01\x02")
    h2 = _compute_inputs_hash()
    assert h1 == h2


def test_iter_hash_inputs_covers_files_missing_roots_and_excluded_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file and directory input paths obey the same repository boundary."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    included = repo_root / "included.txt"
    included.write_text("included", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    missing = repo_root / "missing.txt"
    cache_dir = repo_root / "tracked" / ".pytest_cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "ignored.txt").write_text("ignored", encoding="utf-8")
    monkeypatch.setattr(docker_image, "REPO_ROOT", repo_root)
    monkeypatch.setattr(docker_image, "_HASH_INPUT_FILES", (included, outside, missing))
    monkeypatch.setattr(
        docker_image,
        "_HASH_INPUT_DIRS",
        (repo_root / "tracked", repo_root / "missing-root"),
    )

    yielded = set(_iter_hash_input_paths())

    assert yielded == {included.resolve()}


@pytest.mark.parametrize("target", ["smoke", "full"])
def test_image_tag_uses_target_and_content_hash(
    target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker_image._image_tag.cache_clear()
    monkeypatch.setattr(docker_image, "_compute_inputs_hash", lambda: "abc123")

    assert docker_image._image_tag(target) == f"setforge-e2e:{target}-abc123"


def test_image_tag_rejects_unknown_target() -> None:
    with pytest.raises(ValueError, match="expected full, smoke"):
        docker_image._image_tag("unknown")


def test_hash_inputs_match_explicit_runtime_copy_surface() -> None:
    relative_files = {
        path.relative_to(docker_image.REPO_ROOT).as_posix()
        for path in docker_image._HASH_INPUT_FILES
    }
    relative_dirs = {
        path.relative_to(docker_image.REPO_ROOT).as_posix()
        for path in docker_image._HASH_INPUT_DIRS
    }

    assert relative_files == {
        "README.md",
        "pyproject.toml",
        "tests/docker/Dockerfile",
        "tests/docker/_button_bar_demo.py",
        "uv.lock",
    }
    assert relative_dirs == {"setforge", "tests/fixtures/e2e"}


def test_dockerfile_copy_sources_match_hash_inputs() -> None:
    copied: set[str] = set()
    for line in docker_image.DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if not line.startswith("COPY "):
            continue
        words = [word for word in shlex.split(line) if not word.startswith("--")]
        copied.update(source.rstrip("/") for source in words[1:-1])

    hashed = {
        path.relative_to(docker_image.REPO_ROOT).as_posix().rstrip("/")
        for path in (*docker_image._HASH_INPUT_FILES, *docker_image._HASH_INPUT_DIRS)
        if path != docker_image.DOCKERFILE
    }
    assert copied == hashed


def test_lock_sensitive_dependencies_follow_stable_tool_stages() -> None:
    dockerfile = docker_image.DOCKERFILE.read_text(encoding="utf-8")

    assert dockerfile.index("FROM python-runtime AS vscode-base") < dockerfile.index(
        "FROM vscode-base AS smoke-deps"
    )
    assert dockerfile.index("FROM vscode-base AS full-tools") < dockerfile.index(
        "FROM full-tools AS full-deps"
    )
    assert dockerfile.count("COPY --chown=tester:tester pyproject.toml uv.lock ./") == 2


def test_docker_available_delegates_to_path_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_image.shutil, "which", lambda name: f"/bin/{name}")

    assert docker_image.docker_available() is True
