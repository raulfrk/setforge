"""Docker e2e tests for STRUCTURAL (yaml dotted-path) sub-file span pinning.

The structural sibling of :mod:`tests.docker.test_e2e_docker_spans` (which
covers markdown heading spans). A structural span's anchor is a DOTTED PATH
(a mapping leaf or whole subtree) in the ``set_at_path`` grammar, validated +
resolved against the comment-preserving 3-way merge — not a heading region.

Behavior under exercise (structural sub-span acceptance):

- **install (pinned)** re-asserts the LIVE value at the pinned dotted path
  AFTER the whole-file merge, so an upstream-changed-but-live-unchanged path
  keeps the live value across TWO installs with no phantom conflict (B-S6).
- **compare** marks the pinned-path drift as expected (Invariant I13).
- **capture / sync (forked)** excludes the forked path from a tracked
  writeback round-trip while the rest of the file captures (B-S5 / I2).
- **orphan** — an upstream-removed pinned path orphans: bare install WARNS
  and still exits 0 (Invariant I6 / B-S3).

Profile ``test-spans-structural`` (declared in
``tests/fixtures/e2e/setforge.test.yaml``) pins ``editor.fontSize`` and forks
``telemetry.level`` on ``spans/structural.yaml``. Editing that src per fresh
container simulates an upstream change at the path.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from ruamel.yaml import YAML

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_PROFILE = "test-spans-structural"
_TRACKED = "/workspace/tests/fixtures/e2e/tracked/spans/structural.yaml"
_LIVE = "/home/tester/.setforge_e2e/spans/structural.yaml"

_TRACKED_BODY = (
    "editor:\n"
    "  fontSize: 12\n"
    "  tabSize: 4\n"
    "telemetry:\n"
    "  level: all\n"
    "  endpoint: tracked-endpoint\n"
    "shared:\n"
    "  theme: dark\n"
)


def _setforge(c: ContainerHandle, args: list[str]) -> tuple[int, str, str]:
    """Run ``uv run setforge <args>`` and return (returncode, stdout, stderr)."""
    result = c.exec(["uv", "run", "setforge", *args])
    return result.returncode, result.stdout, result.stderr


def _install(
    c: ContainerHandle, *, extra: list[str] | None = None
) -> tuple[int, str, str]:
    args = ["install", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}"]
    if extra:
        args.extend(extra)
    return _setforge(c, args)


def _sync(
    c: ContainerHandle, *, extra: list[str] | None = None
) -> tuple[int, str, str]:
    args = ["sync", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}", "-y"]
    if extra:
        args.extend(extra)
    return _setforge(c, args)


def _value_at(text: str, path: str) -> object:
    """Parse YAML ``text`` and return the value at dotted ``path``."""
    doc = YAML(typ="safe").load(text)
    node: object = doc
    for seg in path.split("."):
        node = node[seg]  # type: ignore[index]
    return node


# ---------------------------------------------------------------------------
# install (pinned) — pinned dotted path keeps live across an upstream edit
# at that path, over TWO installs, with no phantom conflict.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_pinned_structural_path_roundtrip_keeps_live(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """pinned dotted path: live value survives an upstream change at the path.

    Install once (live == tracked). Edit the LIVE pinned value (host freeze),
    then UPSTREAM changes the SAME pinned path in tracked. A second install
    must re-assert the LIVE value at the pinned path (not take upstream's),
    leave tracked unchanged, and a third install is a clean no-op with no
    phantom conflict (B-S6 / I1).
    """
    c = docker_container()
    rc, _out, err = _install(c)
    assert rc == 0, err
    assert _value_at(c.read_text(_LIVE), "editor.fontSize") == 12

    # Host freezes the pinned path to a local value.
    live_frozen = _TRACKED_BODY.replace("fontSize: 12", "fontSize: 20")
    c.write_text(_LIVE, live_frozen)

    # Upstream changes the SAME pinned path in tracked.
    c.write_text(_TRACKED, _TRACKED_BODY.replace("fontSize: 12", "fontSize: 99"))

    rc, _out, err = _install(c)
    assert rc == 0, err
    merged = c.read_text(_LIVE)
    # The pin re-asserted the LIVE value, not upstream's 99.
    assert _value_at(merged, "editor.fontSize") == 20, merged
    # Tracked src is unchanged by install.
    assert _value_at(c.read_text(_TRACKED), "editor.fontSize") == 99

    # Third install, no new edits → clean no-op, NO phantom conflict.
    rc, out, err = _install(c)
    assert rc == 0, err
    assert "conflict" not in (out + err).lower(), out + err
    assert _value_at(c.read_text(_LIVE), "editor.fontSize") == 20


@pytest.mark.xdist_group("docker_daemon")
def test_compare_marks_pinned_structural_drift_expected(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """compare: drift confined to the pinned path is expected, not flagged."""
    c = docker_container()
    rc, _out, err = _install(c)
    assert rc == 0, err

    c.write_text(_LIVE, _TRACKED_BODY.replace("fontSize: 12", "fontSize: 20"))

    rc, _out, err = _setforge(
        c,
        ["compare", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}", "--check"],
    )
    assert rc == 0, err


# ---------------------------------------------------------------------------
# capture (forked) — a forked path round-trip is excluded from capture.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_forked_structural_path_excluded_from_capture(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """forked dotted path: live edit at the path NEVER captures back to tracked.

    Install, edit BOTH the forked path AND an unrelated shared key in live,
    then sync. The unrelated edit captures into tracked but the forked path
    keeps tracked's value — a host-local span value never leaks into the
    shared config repo (B-S5 / I2 totality).
    """
    c = docker_container()
    rc, _out, err = _install(c)
    assert rc == 0, err

    live = _TRACKED_BODY.replace("level: all", "level: none").replace(
        "theme: dark", "theme: solarized"
    )
    c.write_text(_LIVE, live)

    rc, _out, err = _sync(c)
    assert rc == 0, err
    tracked = c.read_text(_TRACKED)
    # The forked path kept tracked's value (excluded from capture).
    assert _value_at(tracked, "telemetry.level") == "all", tracked
    # The unrelated shared key absorbed the live edit.
    assert _value_at(tracked, "shared.theme") == "solarized", tracked


# ---------------------------------------------------------------------------
# orphan — an upstream-removed pinned path warns + install still exits 0.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_orphaned_pinned_structural_path_warns_and_succeeds(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """orphan: upstream removing the pinned path's parent warns, install exits 0.

    Install once. Then UPSTREAM removes the whole ``editor`` mapping so the
    pinned path ``editor.fontSize`` can no longer be re-asserted (its parent
    is gone from the merged model). Bare install WARNS (region preserved,
    not dropped) and still exits 0 (Invariant I6 / B-S3) — never an uncaught
    crash.
    """
    c = docker_container()
    rc, _out, err = _install(c)
    assert rc == 0, err

    # Upstream removes the entire `editor` mapping; live keeps it, so the
    # merge cleanly takes the deletion, leaving no `editor` parent to pin to.
    upstream = (
        "telemetry:\n"
        "  level: all\n"
        "  endpoint: tracked-endpoint\n"
        "shared:\n"
        "  theme: dark\n"
    )
    c.write_text(_TRACKED, upstream)

    rc, out, err = _install(c)
    assert rc == 0, out + err
    assert "span" in (out + err).lower(), out + err
