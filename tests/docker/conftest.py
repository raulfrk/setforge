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
import functools
import hashlib
import posixpath
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

# pexpect ships no stubs; types-pexpect not added as a dev dep (per qzq scope).
import pexpect  # type: ignore[import-untyped]
import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Auto-activate pytest-xdist when running with ``-m e2e_docker``.

    Sets ``--numprocesses=auto`` only when:
    - the markexpr contains ``e2e_docker`` (substring match — covers
      compound expressions like ``-m "e2e_docker and not slow"``), AND
    - ``-n``/``--numprocesses`` was not set explicitly on the CLI
      (preserves user opt-out via ``-n 0`` for serial-mode debugging).
    """
    markexpr = config.getoption("markexpr", default="") or ""
    if "e2e_docker" not in markexpr:
        return
    explicit = config.getoption("numprocesses", default=None)
    if explicit is not None:
        return
    config.option.numprocesses = "auto"


CONFIG_FIXTURE: str = "tests/fixtures/e2e/my_setup.test.yaml"
"""Shared fixture path for the setforge test config used by every Docker e2e test."""

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
DOCKERFILE: Path = REPO_ROOT / "tests" / "docker" / "Dockerfile"
IMAGE_TAG_PREFIX: str = "setforge-e2e:test"


def _parse_dockerignore(path: Path) -> tuple[set[str], set[str], set[str]]:
    """Parse a .dockerignore file into (dirs, suffixes, filenames).

    - ``#``/blank → skipped.
    - trailing ``/`` → directory pattern.
    - leading ``*`` → suffix pattern.
    - other → filename pattern.

    Glob metacharacters (``**``, ``?``, brace expansion) are not
    supported; the current ``.dockerignore`` does not use them.
    """
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
            if not suffix:
                continue
            suffixes.add(suffix)
        else:
            filenames.add(line)
    return dirs, suffixes, filenames


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
    REPO_ROOT / "setforge",
    REPO_ROOT / "tracked",
)

# Patterns harvested from .dockerignore at import time so the hash exclusion
# list stays aligned with what docker build actually filters out of the
# context. The hardcoded baselines below are unioned with these so behavior is
# resilient if .dockerignore is deleted or unreadable: _parse_dockerignore
# returns empty sets on UnicodeDecodeError rather than blocking test
# collection at module load.
_DOCKERIGNORE_DIRS, _DOCKERIGNORE_SUFFIXES, _DOCKERIGNORE_FILES = _parse_dockerignore(
    REPO_ROOT / ".dockerignore"
)


def _iter_hash_input_paths() -> Iterator[Path]:
    """Yield every file feeding the image-tag hash, in deterministic order.

    Sorted by repo-relative POSIX path so the hash is stable across
    filesystems and OS walk orders. Missing inputs are silently skipped:
    a deleted input legitimately changes the hash via its absence.

    Excludes anything Python or test tooling regenerates at runtime
    (``__pycache__`` bytecode, ``.pytest_cache``, ``.ruff_cache``,
    editor swap files) plus everything ``.dockerignore`` filters out
    of the build context (``.coverage``, ``htmlcov/``, etc.). Without
    the ``.dockerignore`` union the hash flaps when ephemerals land
    under hash-input dirs even though docker build cache is unaffected.
    """
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


# Session-scoped cache: do NOT invoke _image_tag.cache_clear() mid-session
# — it breaks the per-session hash invariant the docstring promises.
@functools.cache
def _image_tag() -> str:
    """Return the per-session content-hashed image tag (cached)."""
    return f"{IMAGE_TAG_PREFIX}-{_compute_inputs_hash()}"


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
    ``setforge/**``) — see :func:`_compute_inputs_hash`. A workspace
    edit flips the hash, flips the tag, and naturally invalidates the
    local image cache. When the hashed tag already exists locally the
    rebuild is skipped (fast cache hit); when no image carries the
    current hash we build. See dotfiles-0ci for the footgun this
    replaces.

    Concurrent pytest sessions on the same host can race the inspect/build
    step: both see returncode != 0 from ``docker image inspect``, both invoke
    ``docker build -t <same-hashed-tag>``. Both builds run concurrently;
    whichever finishes last rewrites the tag ref. The final image is
    byte-equivalent because the inputs hash matches, but the second build
    is wasted work. Currently mitigated by CI being single-stream; if a
    matrix is added, wrap the inspect+build sequence in ``flock`` against a
    tag-keyed lockfile (e.g. ``flock /tmp/setforge-build-${tag}.lock``).
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
            c.exec(["uv", "run", "setforge", "validate", "--all"])
    """
    spawned: list[str] = []

    def launch(
        *,
        cmd: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> ContainerHandle:
        name = f"setforge-e2e-{uuid.uuid4().hex[:10]}"
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
            pty = docker_pty_session(c, ["uv", "run", "setforge", "sync",
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
