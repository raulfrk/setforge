"""Importable implementation of the project's Docker E2E xdist hooks."""

from __future__ import annotations

import pytest

# -n 2 is the empirically validated stable cap on the reference host. Higher
# values saturated the Docker daemon and VM; callers can still override it.
_XDIST_WORKER_CAP: int = 2


def _selects_e2e_docker(markexpr: str) -> bool:
    """Return whether a marker expression positively selects e2e_docker."""
    if "e2e_docker" not in markexpr:
        return False
    tokens = markexpr.replace("(", " ( ").replace(")", " ) ").split()
    if "e2e_docker" not in tokens:
        return False
    idx = tokens.index("e2e_docker")
    return not (idx > 0 and tokens[idx - 1] == "not")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Auto-activate stable two-worker xdist for Docker E2E selections."""
    markexpr = config.getoption("markexpr", default="") or ""
    if not _selects_e2e_docker(markexpr):
        return
    if config.getoption("numprocesses", default=None) is not None:
        return
    if hasattr(config, "workerinput"):
        return
    config.option.numprocesses = _XDIST_WORKER_CAP
    if config.option.dist == "no":
        config.option.dist = "loadgroup"
    config.option.tx = ["popen"] * _XDIST_WORKER_CAP


def pytest_xdist_setupnodes(config: pytest.Config, specs: object) -> None:
    """Prepare the Docker E2E image once before xdist starts workers."""
    del specs
    markexpr = config.getoption("markexpr", default="") or ""
    if not _selects_e2e_docker(markexpr):
        return

    from tests.docker.image import DockerImageBuildError, ensure_docker_image

    try:
        ensure_docker_image()
    except DockerImageBuildError as exc:
        pytest.exit(str(exc), returncode=1)
