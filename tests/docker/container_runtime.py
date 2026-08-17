"""Small, fully covered runtime helpers for Docker E2E containers."""

from __future__ import annotations

import posixpath
import subprocess
from collections.abc import Callable, Mapping, Sequence

E2E_CONTAINER_LABEL = "setforge.e2e.managed=true"


def env_args(env: Mapping[str, str] | None) -> list[str]:
    """Return ``-e KEY=VALUE`` argv chunks for a Docker environment mapping."""
    if env is None:
        return []
    return [part for key, value in env.items() for part in ("-e", f"{key}={value}")]


def container_run_argv(
    *,
    name: str,
    image: str,
    env: Mapping[str, str],
    cmd: Sequence[str] | None,
) -> list[str]:
    """Build the labeled ``docker run`` argv used by every E2E container."""
    argv = [
        "docker",
        "run",
        "--rm",
        "-d",
        "--name",
        name,
        "--label",
        E2E_CONTAINER_LABEL,
        "-w",
        "/workspace",
        *env_args(env),
        image,
    ]
    if cmd is not None:
        argv.extend(cmd)
    return argv


def stream_bytes(
    *,
    cid: str,
    path: str,
    content: bytes,
    ensure_parent: Callable[[str], object],
    timeout: int,
) -> None:
    """Stream bytes through ``tee`` after ensuring the container parent exists."""
    ensure_parent(posixpath.dirname(path) or "/")
    subprocess.run(
        ["docker", "exec", "-i", cid, "tee", path],
        input=content,
        check=True,
        capture_output=True,
        timeout=timeout,
    )
