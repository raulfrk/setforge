"""Docker fixtures for the E2E test ring (dotfiles-nen.9).

Three fixtures:

- :func:`docker_image` — session-scoped: builds the image once per
  pytest session via ``docker/build-push-action``-equivalent CLI. Skips
  every dependent test cleanly when ``docker`` is missing or build
  fails (with stderr captured).
- :func:`docker_container` — function-scoped factory: ``docker run
  --rm -d`` a fresh container, yields a wrapper with ``.exec()``,
  ``.copy_out()``, ``.write_text()`` / ``.write_bytes()``. Tears down
  on test end.
- :func:`docker_pty_session` — function-scoped factory: wraps ``docker
  exec -it`` with :class:`pexpect.spawn` for interactive sync wizard
  variants (P/Q/R/S/S1). Yields the spawned session; finalizer kills it.

Only :mod:`tests.test_e2e_docker` consumes these fixtures. They live
under ``tests/docker/`` (not ``tests/``) to keep the Docker-specific
helpers segregated from the inner-ring CliRunner tests.
"""

from __future__ import annotations

import contextlib
import hashlib
import posixpath
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pexpect  # type: ignore[import-untyped]
import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
DOCKERFILE: Path = REPO_ROOT / "tests" / "docker" / "Dockerfile"
IMAGE_TAG_PREFIX: str = "my-setup-e2e:test"

# Inputs whose content determines the docker image identity. Anything baked
# into the image (Dockerfile + sources copied in) or read by the e2e tests
# from inside the image (fixture yaml + canonical my_setup.yaml) goes here.
# A change to any of these flips the content hash, which flips the image
# tag, which naturally invalidates the build cache — see dotfiles-0ci.
_HASH_INPUT_FILES: tuple[Path, ...] = (
    REPO_ROOT / "tests" / "docker" / "Dockerfile",
    REPO_ROOT / "my_setup.yaml",
    REPO_ROOT / "pyproject.toml",
    REPO_ROOT / "uv.lock",
)
_HASH_INPUT_DIRS: tuple[Path, ...] = (
    REPO_ROOT / "tests" / "fixtures" / "e2e",
    REPO_ROOT / "my_setup",
    REPO_ROOT / "tracked",
)


def _iter_hash_input_paths() -> Iterator[Path]:
    """Yield every file feeding the image-tag hash, in deterministic order.

    Sorted by repo-relative POSIX path so the hash is stable across
    filesystems and OS walk orders. Missing inputs are silently skipped:
    a deleted input legitimately changes the hash via its absence.

    Excludes anything Python or test tooling regenerates at runtime
    (``__pycache__`` bytecode, ``.pytest_cache``, ``.ruff_cache``,
    editor swap files) — those would make the hash flap mid-session
    and defeat the cache.
    """
    excluded_dirs = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
    excluded_suffixes = {".pyc", ".pyo", ".swp", ".swo"}
    seen: set[Path] = set()
    for path in _HASH_INPUT_FILES:
        if path.is_file():
            seen.add(path.resolve())
    for root in _HASH_INPUT_DIRS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix in excluded_suffixes:
                continue
            if any(part in excluded_dirs for part in path.parts):
                continue
            seen.add(path.resolve())
    yield from sorted(seen, key=lambda p: p.relative_to(REPO_ROOT).as_posix())


def _compute_inputs_hash() -> str:
    """Return a short content hash over the files that define the image.

    First 12 hex chars of SHA-256 over each input's repo-relative POSIX
    path, a NUL separator, the byte content, and a record separator.
    Twelve chars is ~48 bits — collision risk is negligible for the
    handful of distinct workspace states a developer holds at once.
    """
    digest = hashlib.sha256()
    for path in _iter_hash_input_paths():
        rel = path.relative_to(REPO_ROOT).as_posix().encode("utf-8")
        digest.update(rel)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\x1e")
    return digest.hexdigest()[:12]


# Cache the per-session tag so we hash inputs at most once per pytest run.
_IMAGE_TAG_CACHE: str | None = None


def _image_tag() -> str:
    """Return the per-session content-hashed image tag (cached)."""
    global _IMAGE_TAG_CACHE
    if _IMAGE_TAG_CACHE is None:
        _IMAGE_TAG_CACHE = f"{IMAGE_TAG_PREFIX}-{_compute_inputs_hash()}"
    return _IMAGE_TAG_CACHE


def _env_args(env: dict[str, str] | None) -> list[str]:
    """Return ``-e KEY=VAL`` argv chunks for a ``docker`` env mapping."""
    if env is None:
        return []
    args: list[str] = []
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    return args


# ---------------------------------------------------------------------------
# Image: build once per session
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """Return True iff a usable ``docker`` binary is on PATH."""
    return shutil.which("docker") is not None


@pytest.fixture(scope="session")
def docker_image() -> str:
    """Build the E2E image once per session; return the image tag.

    Skips every dependent test cleanly when ``docker`` is missing on
    PATH. A non-zero ``docker build`` exit is treated as a real bug
    (it's the test infrastructure failing, not a transient daemon
    blip) and surfaces via :func:`pytest.fail`, with stdout/stderr
    captured into the failure message so CI shows the actual cause
    without burying it in a fixture-error stack.

    The tag is content-hashed over the inputs that define the image
    (Dockerfile, ``my_setup.yaml``, ``tests/fixtures/e2e/**``,
    ``my_setup/**``) — see :func:`_compute_inputs_hash`. A workspace
    edit flips the hash, flips the tag, and naturally invalidates the
    local image cache. When the hashed tag already exists locally the
    rebuild is skipped (fast cache hit); when no image carries the
    current hash we build. See dotfiles-0ci for the footgun this
    replaces.
    """
    if not _docker_available():
        pytest.skip("docker binary not on PATH")

    tag = _image_tag()
    inspect = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if inspect.returncode == 0:
        return tag

    proc = subprocess.run(
        [
            "docker",
            "build",
            "-t",
            tag,
            "-f",
            str(DOCKERFILE),
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"docker build failed:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
            pytrace=False,
        )
    return tag


# ---------------------------------------------------------------------------
# Container: --rm -d per test
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ContainerHandle:
    """Wrapper around a running container with the operations tests need."""

    cid: str

    def exec(
        self,
        cmd: list[str],
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
        workdir: str | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command inside the container, return CompletedProcess.

        ``check=False`` lets tests assert on non-zero exits (e.g.
        compare --check on drift). ``input_text`` feeds stdin via
        ``subprocess`` (not a TTY — see :func:`docker_pty_session` for
        the PTY-driven wizard variants).
        """
        argv: list[str] = ["docker", "exec"]
        if workdir is not None:
            argv += ["-w", workdir]
        argv += _env_args(env)
        if input_text is not None:
            argv += ["-i"]
        argv += [self.cid, *cmd]
        return subprocess.run(
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            check=check,
            timeout=60,
        )

    def copy_out(self, src_in_container: str, host_dst: Path) -> None:
        """Copy a file out of the container to the host filesystem."""
        host_dst.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["docker", "cp", f"{self.cid}:{src_in_container}", str(host_dst)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def write_text(self, path_in_container: str, content: str) -> None:
        """Write text to a file inside the container via ``docker cp``.

        Stages the content via a heredoc-style write to a tmp file on
        the host, then ``docker cp`` it in. Handles arbitrary content
        without shell-escaping headaches.
        """
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as fh:
            fh.write(content)
            staging = fh.name
        try:
            # Ensure parent dir exists in the container.
            parent = posixpath.dirname(path_in_container) or "/"
            self.exec(["mkdir", "-p", parent], check=True)
            subprocess.run(
                ["docker", "cp", staging, f"{self.cid}:{path_in_container}"],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        finally:
            Path(staging).unlink(missing_ok=True)

    def read_text(self, path_in_container: str) -> str:
        """Read a file inside the container; return its text content."""
        return self.exec(["cat", path_in_container]).stdout


@pytest.fixture
def docker_container(
    docker_image: str,
) -> Iterator[Callable[..., ContainerHandle]]:
    """Function-scoped factory: yields a launcher that returns a
    :class:`ContainerHandle`. Tears down every container at test end.

    Usage::

        def test_x(docker_container):
            c = docker_container()
            c.exec(["uv", "run", "my-setup", "validate", "--all"])
    """
    spawned: list[str] = []

    def launch(
        *,
        cmd: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> ContainerHandle:
        name = f"my-setup-e2e-{uuid.uuid4().hex[:10]}"
        argv: list[str] = [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "-w",
            "/workspace",
        ]
        argv += _env_args(env)
        argv += [docker_image]
        if cmd is not None:
            argv += cmd
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        cid = proc.stdout.strip()
        spawned.append(cid)
        return ContainerHandle(cid=cid)

    yield launch

    for cid in spawned:
        # Best-effort teardown; --rm handles it on graceful stop, but
        # if a test leaves the container alive we kill it explicitly.
        subprocess.run(
            ["docker", "rm", "-f", cid],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )


# ---------------------------------------------------------------------------
# PTY session: pexpect against `docker exec -it`
# ---------------------------------------------------------------------------


@pytest.fixture
def docker_pty_session(
    docker_container: Callable[..., ContainerHandle],
) -> Iterator[Callable[..., pexpect.spawn]]:
    """Function-scoped factory that returns a :class:`pexpect.spawn`
    against ``docker exec -it``. Used by the interactive sync wizard
    variants (P/Q/R/S/S1).

    Usage::

        def test_pty(docker_pty_session, docker_container):
            c = docker_container()
            pty = docker_pty_session(c, ["uv", "run", "my-setup", "sync",
                                          "--profile=test-jsonc-deep",
                                          "--config=..."])
            pty.expect("Choice")
            pty.send("k")
            pty.expect(pexpect.EOF)
    """
    sessions: list[pexpect.spawn] = []

    def open_pty(
        container: ContainerHandle,
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> pexpect.spawn:
        argv = ["exec", "-it"]
        argv += _env_args(env)
        argv += [container.cid, *cmd]
        session = pexpect.spawn("docker", argv, encoding="utf-8", timeout=timeout)
        sessions.append(session)
        return session

    yield open_pty

    for s in sessions:
        with contextlib.suppress(pexpect.ExceptionPexpect, OSError):
            s.close(force=True)
