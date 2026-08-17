"""Docker fixtures for the E2E test ring.

Four fixtures:

- :func:`docker_image` — session-scoped: reuses the controller-prepared image
  under xdist, or builds it once in serial mode. Skips
  every dependent test cleanly when ``docker`` is missing or build
  fails (with stderr captured).
- :func:`docker_container` — function-scoped factory: ``docker run
  --rm -d`` a fresh container, yields a wrapper with ``.exec()``,
  ``.copy_out()``, ``.write_text()`` / ``.write_bytes()``. Tears down
  on test end.
- :func:`docker_pty_session` — function-scoped factory: wraps ``docker
  exec -it`` with :class:`pexpect.spawn` for stdout-anchored interactive
  variants (sync wizard P/Q/R/S/S1). Yields the spawned session;
  finalizer kills it.
- :func:`pyte_pty_session` — function-scoped factory
  that layers a :class:`pyte.HistoryScreen` over the pexpect PTY so
  prompt_toolkit's full-screen ``radiolist_dialog`` / ``input_dialog``
  panels can be anchored on the EMULATED screen (``.display`` lines)
  rather than the raw byte stream. Returns
  :class:`tests.docker.pyte_session.PyteSession` instances; finalizer
  closes every session created during the test.

Only :mod:`tests.test_e2e_docker` consumes these fixtures. They live
under ``tests/docker/`` (not ``tests/``) to keep the Docker-specific
helpers segregated from the inner-ring CliRunner tests.

xdist_group convention
----------------------
Heavy install/sync/revert/reconcile tests that hit the shared docker
daemon are tagged ``@pytest.mark.xdist_group("docker_daemon")``. pytest-xdist
routes same-group tests to one worker, serializing daemon contention while
unrelated tests still parallelize. Only tag tests that share daemon state
(container lifecycle, exec queuing); do NOT tag a test just for being slow.

pyte_pty_session vs docker_pty_session
--------------------------------------
Use ``docker_pty_session`` when the test only needs to read line-buffered
stdout/stderr (existing ``Choice:`` / ``[p]`` prompts that ship as plain
text). Use ``pyte_pty_session`` when the SUT renders a full-screen
``prompt_toolkit.Application`` (``radiolist_dialog`` / ``input_dialog`` /
``yes_no_dialog``) — those emit cursor-positioning ANSI that pexpect's
line matcher cannot reliably anchor on. The pyte harness lives in
:mod:`tests.docker.pyte_session`; see its module docstring for the
``\\x1b[A`` arrow-key + ``\\r`` Enter conventions and the
``docker exec -it`` ``-it`` requirement.
"""

from __future__ import annotations

import contextlib
import posixpath
import subprocess
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

# pexpect ships no stubs; types-pexpect not added as a dev dep (per qzq scope).
import pexpect  # type: ignore[import-untyped]
import pytest

from tests.docker.container_runtime import (
    container_run_argv,
    env_args,
    stream_bytes,
)
from tests.docker.image import DockerImageBuildError, ensure_docker_image
from tests.docker.pyte_session import PyteSession
from tests.e2e_xdist import image_target_for_markexpr

CONFIG_FIXTURE: str = "tests/fixtures/e2e/setforge.test.yaml"
"""Shared fixture path for the setforge test config used by every Docker e2e test."""

DOCKER_EXEC_TIMEOUT_S: int = 120
"""Per-call wall timeout for ``docker exec`` / ``docker cp`` subprocesses.

Raised from 60s after the 6-core test host showed transient
``subprocess.TimeoutExpired`` failures under ``-n 4`` xdist parallel
load — the daemon serializes parallel ``exec`` calls and a single test
can queue behind 3 sibling-worker calls before its budget burns. The
60s budget was tight enough that any docker daemon hiccup tipped a
test over. 120s leaves headroom without slowing the green-path case
(``docker exec`` for the e2e suite completes in 5-15s typical).
"""

# ---------------------------------------------------------------------------
# Image: build once per session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def docker_image(request: pytest.FixtureRequest) -> str:
    """Build the E2E image once per session; return the image tag.

    Skips every dependent test cleanly when ``docker`` is missing on
    PATH. A non-zero ``docker build`` exit is treated as a real bug
    (it's the test infrastructure failing, not a transient daemon
    blip) and surfaces via :func:`pytest.fail`, with stdout/stderr
    captured into the failure message so CI shows the actual cause
    without burying it in a fixture-error stack.

    The tag is content-hashed over the inputs that define the image
    (Dockerfile, package metadata/README, ``tests/fixtures/e2e/**``,
    ``tests/docker/_button_bar_demo.py``, and ``setforge/**``) — see
    :func:`tests.docker.image._compute_inputs_hash`. A workspace
    edit flips the hash, flips the tag, and naturally invalidates the
    local image cache. When the hashed tag already exists locally the
    rebuild is skipped (fast cache hit); when no image carries the
    current hash we build.

    Concurrent pytest sessions on the same host can race the inspect/build
    step: both see returncode != 0 from ``docker image inspect``, both invoke
    ``docker build -t <same-hashed-tag>``. Both builds run concurrently;
    whichever finishes last rewrites the tag ref. The final image is
    byte-equivalent because the inputs hash matches, but the second build
    is wasted work. Currently mitigated by CI being single-stream; if a
    matrix is added, wrap the inspect+build sequence in ``flock`` against a
    tag-keyed lockfile (e.g. ``flock /tmp/setforge-build-${tag}.lock``).
    """
    try:
        markexpr = request.config.getoption("markexpr", default="") or ""
        tag = ensure_docker_image(image_target_for_markexpr(markexpr))
    except DockerImageBuildError as exc:
        pytest.fail(
            str(exc),
            pytrace=False,
        )
    if tag is None:
        pytest.skip("docker binary not on PATH")
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
        argv += env_args(env)
        if input_text is not None:
            argv += ["-i"]
        argv += [self.cid, *cmd]
        return subprocess.run(
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            check=check,
            timeout=DOCKER_EXEC_TIMEOUT_S,
        )

    def copy_out(self, src_in_container: str, host_dst: Path) -> None:
        """Copy a file out of the container to the host filesystem."""
        host_dst.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["docker", "cp", f"{self.cid}:{src_in_container}", str(host_dst)],
            check=True,
            capture_output=True,
            text=True,
            timeout=DOCKER_EXEC_TIMEOUT_S,
        )

    def write_text(self, path_in_container: str, content: str) -> None:
        """Write text to a file inside the container.

        Streams the content into ``tee`` via ``docker exec -i`` so the
        file is created BY the container's runtime user (``tester``) and
        is therefore owned by it. An earlier implementation staged the
        content in a host tmp file and ``docker cp``'d it in, but
        ``docker cp`` preserves the host file's numeric owner uid: on CI
        runners (uid 1001 != tester's 1000) the container user could
        neither read nor ``chmod`` the result, failing every
        file-touching test. Streaming from stdin also keeps arbitrary
        content free of shell-escaping headaches.
        """
        # Ensure parent dir exists in the container.
        parent = posixpath.dirname(path_in_container) or "/"
        self.exec(["mkdir", "-p", parent], check=True)
        subprocess.run(
            ["docker", "exec", "-i", self.cid, "tee", path_in_container],
            input=content,
            check=True,
            capture_output=True,
            text=True,
            timeout=DOCKER_EXEC_TIMEOUT_S,
        )

    def write_bytes(self, path_in_container: str, content: bytes) -> None:
        """Stream arbitrary bytes into a container-owned file."""
        stream_bytes(
            cid=self.cid,
            path=path_in_container,
            content=content,
            ensure_parent=lambda parent: self.exec(["mkdir", "-p", parent]),
            timeout=DOCKER_EXEC_TIMEOUT_S,
        )

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
        # Suppress the fresh-host welcome panel by default
        # for the Docker e2e suite. The welcome's spec behavior raises
        # WelcomeRequiresInteractive on non-TTY + no --yes, which would
        # trip every existing install-touching test on the fresh
        # containers the suite uses. The welcome-specific tests in
        # ``tests/docker/test_e2e_docker_fresh_host.py`` override this
        # by passing ``env={"SETFORGE_NO_WELCOME": ""}`` to exercise the
        # welcome path end-to-end.
        merged_env = {"SETFORGE_NO_WELCOME": "1"} | (env or {})
        argv = container_run_argv(
            name=name,
            image=docker_image,
            env=merged_env,
            cmd=cmd,
        )
        proc = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            timeout=DOCKER_EXEC_TIMEOUT_S,
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
        argv += env_args(env)
        argv += [container.cid, *cmd]
        session = pexpect.spawn("docker", argv, encoding="utf-8", timeout=timeout)
        sessions.append(session)
        return session

    yield open_pty

    for s in sessions:
        with contextlib.suppress(pexpect.ExceptionPexpect, OSError):
            s.close(force=True)


# ---------------------------------------------------------------------------
# pyte-backed PTY session: full-screen TUI assertions via emulated terminal
# ---------------------------------------------------------------------------


@pytest.fixture
def pyte_pty_session() -> Iterator[Callable[..., PyteSession]]:
    """Function-scoped factory for :class:`PyteSession` instances.

    Spawns ``docker exec -it <container> <cmd>`` through pexpect and
    feeds the byte stream into a :class:`pyte.HistoryScreen`. Use this
    fixture when the SUT renders a full-screen prompt_toolkit
    ``Application`` (``radiolist_dialog`` / ``input_dialog`` /
    ``yes_no_dialog``); use :func:`docker_pty_session` for plain stdout
    interactives.

    Each call to the factory creates a fresh session; the finalizer
    closes every session at test teardown. See
    :mod:`tests.docker.pyte_session` for the per-keystroke / per-escape
    anti-smell items (``\\x1b[A`` arrows, ``\\r`` Enter, ``-it`` PTY
    requirement, pyte ``>=0.8.2`` minimum).

    Usage::

        def test_radiolist_confirm(pyte_pty_session, docker_container):
            c = docker_container()
            session = pyte_pty_session(
                container=c.cid,
                cmd=["uv", "run", "setforge", "install",
                     "--profile=foo", "--auto=use-tracked"],
            )
            session.expect_in_display("Proceed with the mutation")
            session.send_keys("\\x1b[B\\r")  # arrow down → yes, Enter
            session.wait_for_exit(timeout=30, expected_code=0)
    """
    sessions: list[PyteSession] = []

    def _factory(
        *,
        container: str,
        cmd: list[str],
        cols: int = 120,
        lines: int = 40,
        timeout: float = 30.0,
    ) -> PyteSession:
        session = PyteSession.spawn(
            container=container,
            cmd=cmd,
            cols=cols,
            lines=lines,
            timeout=timeout,
        )
        sessions.append(session)
        return session

    yield _factory

    for s in sessions:
        s.close()
