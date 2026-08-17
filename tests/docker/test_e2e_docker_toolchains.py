"""Docker E2E: toolchain smoke test (rust + go + uv).

The B-wave package provisioners (cargo / python / go / github_release)
each need a Tier-3 real-install e2e that runs the actual toolchain inside
the container. ``uv`` was already baked in; this test asserts the image
now also ships a working ``cargo`` (rustup) and ``go`` toolchain and that
``go install``-produced binaries land on PATH.

Kept fast + hermetic: version probes are offline. The two real-install
cases (a ``go install`` of a tiny module, and a ``cargo install`` of a
locally-generated trivial crate that exercises the C linker) are gated
behind ``SETFORGE_E2E_NETWORK`` the same way other network-touching e2e
cases are, so the offline smoke assertions always run.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle
from tests.docker.network import NETWORK_ONLY

pytestmark = pytest.mark.e2e_docker

# Opt-in gate for the cases that reach the network (a real ``go install``
# and a real ``cargo install``). Mirrors the convention other
# network-touching e2e cases follow: default-off so the suite stays
# hermetic, flip the env var on to exercise the real build paths.


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


@pytest.mark.network_canary
@NETWORK_ONLY
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


@pytest.mark.network_canary
@NETWORK_ONLY
def test_cargo_install_compiles_links_and_lands_on_path(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A real ``cargo install`` compiles + links + lands on ``~/.cargo/bin``.

    Even a pure-Rust crate needs a system C toolchain (``cc``) for the
    final link step; this case proves the image's ``build-essential``
    makes a real compile+link succeed, which the cargo provisioner
    relies on. Uses a locally-generated trivial binary crate
    (``cargo new`` + ``cargo install --path``) so the compile stays fast
    and doesn't depend on any specific crates.io crate/version — but it
    is still network-gated because cargo fetches its registry index on
    first run. Asserts the installed binary lands on ``~/.cargo/bin``
    (on PATH) and actually runs.
    """
    c = docker_container()

    crate_dir = "/home/tester/hello-crate"
    new = c.exec(["cargo", "new", "--bin", crate_dir], check=False)
    assert new.returncode == 0, new.stdout + new.stderr

    # Install from the local path: this compiles AND links (exercising
    # `cc` from build-essential) and drops the binary into ~/.cargo/bin.
    inst = c.exec(
        ["cargo", "install", "--path", crate_dir],
        check=False,
    )
    assert inst.returncode == 0, inst.stdout + inst.stderr

    # `cargo new` names the crate after the dir → binary is `hello-crate`.
    which = c.exec(["which", "hello-crate"], check=False)
    assert which.returncode == 0, which.stdout + which.stderr
    assert which.stdout.strip() == "/home/tester/.cargo/bin/hello-crate", which.stdout

    # The linked binary actually runs (cargo new's default prints Hello).
    run = c.exec(["hello-crate"], check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "Hello, world!" in run.stdout, run.stdout
