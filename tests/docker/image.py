"""Docker E2E image identity and preparation.

This module is deliberately fixture-free so both pytest's xdist controller
and worker/serial fixtures can use exactly the same inspect/build operation.
"""

from __future__ import annotations

import functools
import hashlib
import shlex
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
DOCKERFILE: Path = REPO_ROOT / "tests" / "docker" / "Dockerfile"
IMAGE_TAG_PREFIX: str = "setforge-e2e:test"


class DockerImageBuildError(RuntimeError):
    """Raised when Docker cannot build the content-addressed E2E image."""


def _parse_dockerignore(path: Path) -> tuple[set[str], set[str], set[str]]:
    """Parse the simple patterns used by this repository's .dockerignore."""
    dirs: set[str] = set()
    suffixes: set[str] = set()
    filenames: set[str] = set()
    if not path.is_file():
        return dirs, suffixes, filenames
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return dirs, suffixes, filenames
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("/"):
            dirs.add(line.rstrip("/"))
        elif line.startswith("*"):
            suffix = line[1:]
            if suffix:
                suffixes.add(suffix)
        else:
            filenames.add(line)
    return dirs, suffixes, filenames


_HASH_INPUT_FILES: tuple[Path, ...] = (
    DOCKERFILE,
    REPO_ROOT / "pyproject.toml",
    REPO_ROOT / "uv.lock",
)
_HASH_INPUT_DIRS: tuple[Path, ...] = (
    REPO_ROOT / "tests" / "fixtures" / "e2e",
    REPO_ROOT / "setforge",
    REPO_ROOT / "tracked",
)
_DOCKERIGNORE_DIRS, _DOCKERIGNORE_SUFFIXES, _DOCKERIGNORE_FILES = _parse_dockerignore(
    REPO_ROOT / ".dockerignore"
)


def _iter_hash_input_paths() -> Iterator[Path]:
    """Yield image inputs in deterministic repo-relative order."""
    excluded_dirs = {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
    } | _DOCKERIGNORE_DIRS
    excluded_suffixes = {".pyc", ".pyo", ".swp", ".swo"} | _DOCKERIGNORE_SUFFIXES
    excluded_filenames = set(_DOCKERIGNORE_FILES)
    seen: set[Path] = set()
    for path in _HASH_INPUT_FILES:
        if path.is_file():
            resolved = path.resolve()
            if resolved.is_relative_to(REPO_ROOT):
                seen.add(resolved)
    for root in _HASH_INPUT_DIRS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix in excluded_suffixes:
                continue
            if path.name in excluded_filenames:
                continue
            if any(part in excluded_dirs for part in path.parts):
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(REPO_ROOT):
                continue
            seen.add(resolved)
    yield from sorted(seen, key=lambda p: p.relative_to(REPO_ROOT).as_posix())


def _compute_inputs_hash() -> str:
    """Return a short content hash over the files that define the image."""
    digest = hashlib.sha256()
    for path in _iter_hash_input_paths():
        rel = path.relative_to(REPO_ROOT).as_posix().encode("utf-8")
        digest.update(rel)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\x1e")
    return digest.hexdigest()[:12]


@functools.cache
def _image_tag() -> str:
    """Return the per-process content-hashed image tag."""
    return f"{IMAGE_TAG_PREFIX}-{_compute_inputs_hash()}"


def docker_available() -> bool:
    """Return whether a Docker executable is available on PATH."""
    return shutil.which("docker") is not None


def _run_docker(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run Docker while normalizing launch and timeout failures."""
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        stdout = getattr(exc, "stdout", None) or ""
        stderr = getattr(exc, "stderr", None) or ""
        raise DockerImageBuildError(
            f"docker command failed: {shlex.join(argv)}: {exc}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        ) from exc


def ensure_docker_image() -> str | None:
    """Inspect or build the E2E image, returning ``None`` without Docker."""
    if not docker_available():
        return None

    tag = _image_tag()
    inspect = _run_docker(
        ["docker", "image", "inspect", tag],
        timeout=30,
    )
    if inspect.returncode == 0:
        return tag

    build = _run_docker(
        ["docker", "build", "-t", tag, "-f", str(DOCKERFILE), str(REPO_ROOT)],
        timeout=600,
    )
    if build.returncode != 0:
        raise DockerImageBuildError(
            f"docker build failed:\nstdout:\n{build.stdout}\nstderr:\n{build.stderr}"
        )
    return tag
