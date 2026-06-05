"""Docker e2e tests for the shared-span reconcile surface.

Exercises the intent-collision model end-to-end against a fresh Debian
container with the real ``setforge`` CLI. A *shared* span carries pure
intent (anchor/kind/semantics) in the tracked ``setforge.yaml`` — it has
no tracked body, so reconcile is about an INTENT collision: a host-local
span (seeded in the container's ``local.yaml``) that shadows a shared span
on the SAME anchor.

Behavior under exercise (the spec's acceptance + B-R6..B-R8):

- **propagation** — a shared span on a fresh host applies on bare install
  (live deploys, exit 0) with NO reconcile surface, since no host-local
  span shadows it.
- **collision + ``--reconcile-user-sections --auto=use-tracked``** — a
  seeded host-local↔shared collision resolves toward the shared intent
  and the install surfaces an explicit "host-local span ... overwritten"
  risk line (B-R7); install exits 0.
- **collision + ``--auto=keep-live``** (the inverse) — keeps the
  host-local override silently, no overwrite risk line; install exits 0.
- **bare install on a collision** — silent host-local-wins, no nag
  (B-R6): install exits 0 with no collision line in the output.

Reuses the ``spans_pinned_md`` fixture (a shared pinned span on
``## Pinned Section``, profile ``test-spans-pinned``) declared in
``tests/fixtures/e2e/setforge.test.yaml``; the seeded host-local span
lives in the container's ``~/.config/setforge/local.yaml``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

# Live destination of the shared-span fixture file.
_LIVE_PINNED = "/home/tester/.setforge_e2e/spans/pinned.md"

# The container user's host-local overlay path.
_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"

# A host-local span declaration that shadows the shared span on the SAME
# anchor (``## Pinned Section``) for the ``spans_pinned_md`` tracked_file.
_COLLISION_LOCAL_YAML = (
    "tracked_files:\n"
    "  spans_pinned_md:\n"
    "    spans:\n"
    '      - anchor: "## Pinned Section"\n'
    "        kind: forked\n"
    "        semantics: host-local\n"
)


def _setforge(
    c: ContainerHandle, args: list[str], *, check: bool = False
) -> tuple[int, str, str]:
    """Run ``uv run setforge <args>`` and return (returncode, stdout, stderr)."""
    result = c.exec(["uv", "run", "setforge", *args], check=check)
    return result.returncode, result.stdout, result.stderr


def _install(
    c: ContainerHandle, profile: str, *, extra: list[str] | None = None
) -> tuple[int, str, str]:
    """Run ``setforge install --profile=<profile> --config=<fixture>``."""
    args = ["install", f"--profile={profile}", f"--config={CONFIG_FIXTURE}"]
    if extra:
        args.extend(extra)
    return _setforge(c, args)


@pytest.mark.xdist_group("docker_daemon")
def test_shared_span_propagates_to_fresh_host(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A shared span applies on a fresh host's bare install, no reconcile.

    With NO host-local span shadowing the shared anchor, the install just
    deploys the file and seeds the span state — nothing to reconcile, so no
    collision line surfaces.
    """
    c = docker_container()
    rc, stdout, stderr = _install(c, "test-spans-pinned")
    assert rc == 0, stderr
    # File deployed (shared span applied on the fresh host).
    assert "## Pinned Section" in c.read_text(_LIVE_PINNED)
    # No collision surface on a host with no host-local span.
    assert "host-local span" not in (stdout + stderr).lower()


@pytest.mark.xdist_group("docker_daemon")
def test_collision_bare_install_does_not_nag(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """B-R6: a seeded collision on a bare install keeps silent host-local-wins."""
    c = docker_container()
    c.write_text(_LOCAL_YAML, _COLLISION_LOCAL_YAML)
    rc, stdout, stderr = _install(c, "test-spans-pinned")
    assert rc == 0, stderr
    assert "host-local span" not in (stdout + stderr).lower()
    assert "overwritten" not in (stdout + stderr).lower()


@pytest.mark.xdist_group("docker_daemon")
def test_collision_reconcile_auto_use_tracked_surfaces_risk(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """B-R7: collision + --auto=use-tracked surfaces the overwrite risk line."""
    c = docker_container()
    c.write_text(_LOCAL_YAML, _COLLISION_LOCAL_YAML)
    rc, stdout, stderr = _install(
        c, "test-spans-pinned", extra=["--auto=use-tracked", "--yes"]
    )
    assert rc == 0, stderr
    combined = (stdout + stderr).lower()
    assert "host-local span" in combined, stdout + stderr
    assert "overwritten" in combined, stdout + stderr
    assert "## pinned section" in combined, stdout + stderr


@pytest.mark.xdist_group("docker_daemon")
def test_collision_reconcile_auto_keep_live_keeps_host_local(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """The keep-live inverse: --auto=keep-live keeps host-local, no risk line."""
    c = docker_container()
    c.write_text(_LOCAL_YAML, _COLLISION_LOCAL_YAML)
    rc, stdout, stderr = _install(
        c, "test-spans-pinned", extra=["--auto=keep-live", "--yes"]
    )
    assert rc == 0, stderr
    assert "overwritten" not in (stdout + stderr).lower()
    # File still deployed regardless of the kept side.
    assert "## Pinned Section" in c.read_text(_LIVE_PINNED)
