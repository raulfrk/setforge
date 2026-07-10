"""Docker E2E: toolchain smoke test (rust + go + uv).

The B-wave package provisioners (cargo / python / go / github_release)
each need a Tier-3 real-install e2e that runs the actual toolchain inside
the container. ``uv`` was already baked in; this test asserts the image
now also ships a working ``cargo`` (rustup) and ``go`` toolchain and that
``go install``-produced binaries land on PATH.

Kept fast + hermetic: version probes are offline. The one real
``go install`` builds a trivially small, well-known module and is gated
behind ``SETFORGE_E2E_NETWORK`` the same way other network-touching e2e
cases are, so the offline smoke assertions always run.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

# Opt-in gate for the case that reaches the network (a real
# ``go install``). Mirrors the convention other network-touching e2e
# cases follow: default-off so the suite stays hermetic, flip the env
# var on to exercise the real download+build path.
_NETWORK_ENV = "SETFORGE_E2E_NETWORK"
_network_only = pytest.mark.skipif(
    os.environ.get(_NETWORK_ENV, "") == "",
    reason=f"set {_NETWORK_ENV}=1 to run the real `go install` network case",
)


def test_toolchains_present(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """rust (cargo), go, and uv all resolve + report a version.

    Offline + hermetic: each is a local version probe, no network. This
    is the load-bearing infra assertion — if any toolchain is missing
    from the image the corresponding provisioner e2e cannot run.
    """
    c = docker_container()

    cargo = c.exec(["cargo", "--version"], check=False)
    assert cargo.returncode == 0, cargo.stdout + cargo.stderr
    assert cargo.stdout.startswith("cargo "), cargo.stdout

    go = c.exec(["go", "version"], check=False)
    assert go.returncode == 0, go.stdout + go.stderr
    assert go.stdout.startswith("go version "), go.stdout

    uv = c.exec(["uv", "--version"], check=False)
    assert uv.returncode == 0, uv.stdout + uv.stderr
    assert uv.stdout.startswith("uv "), uv.stdout


def test_rustc_present(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``rustc`` (the compiler rustup installs alongside cargo) resolves.

    A cargo binary without a working rustc cannot actually build a crate,
    so probe the compiler too — still offline.
    """
    c = docker_container()
    rustc = c.exec(["rustc", "--version"], check=False)
    assert rustc.returncode == 0, rustc.stdout + rustc.stderr
    assert rustc.stdout.startswith("rustc "), rustc.stdout


@_network_only
def test_go_install_lands_on_path(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A real ``go install`` produces a binary on PATH.

    Builds ``golang.org/x/example/hello@latest`` — a tiny canonical
    module — then asserts the resulting ``hello`` binary is resolvable on
    PATH (i.e. ``GOBIN``/``GOPATH/bin`` is wired into PATH for the tester
    user). Network-gated; only runs when the opt-in env var is set.
    """
    c = docker_container()

    inst = c.exec(
        ["go", "install", "golang.org/x/example/hello@latest"],
        check=False,
    )
    assert inst.returncode == 0, inst.stdout + inst.stderr

    # The produced binary must be resolvable on PATH — proves GOBIN /
    # GOPATH/bin is exported, which the go provisioner relies on.
    which = c.exec(["which", "hello"], check=False)
    assert which.returncode == 0, which.stdout + which.stderr
    assert which.stdout.strip().endswith("/hello"), which.stdout
