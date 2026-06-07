"""Docker e2e canonical leak gate for markerless OVERLAY spans.

The whole bead exists to guarantee one invariant end-to-end: a heading-less
host-local OVERLAY body deploys into the live file, then `sync` captures —
and the body NEVER appears in the tracked src nor the per-host stored base.
This exercises the real install + sync CLI against a fresh container.

Fixture: ``spans_overlay_md`` (a SHARED markdown tracked_file, src
``spans/note.md``, profile ``test-spans-overlay``). The OVERLAY span is
seeded host-local in the container's ``local.yaml`` at test time.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_LIVE = "/home/tester/.setforge_e2e/spans/overlay.md"
_TRACKED_SRC = "/workspace/tests/fixtures/e2e/tracked/spans/note.md"
_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"
_STATE_BASE = "/home/tester/.local/state/setforge/base"

_BODY = "HOST-LOCAL-OVERLAY-SECRET"

# A host-local OVERLAY span: a markerless body injected after the
# "## Upstream" heading of the shared note.md.
_OVERLAY_LOCAL_YAML = (
    "tracked_files:\n"
    "  spans_overlay_md:\n"
    "    spans:\n"
    '      - anchor: "## Upstream"\n'
    "        kind: overlay\n"
    "        semantics: host-local\n"
    "        overlay:\n"
    "          anchor: {kind: after-heading, value: Upstream}\n"
    f"          body: {_BODY}\n"
)


def _setforge(
    c: ContainerHandle, args: list[str], *, check: bool = False
) -> tuple[int, str, str]:
    result = c.exec(["uv", "run", "setforge", *args], check=check)
    return result.returncode, result.stdout, result.stderr


def _grep_base_for_body(c: ContainerHandle) -> str:
    """Return any stored-base file content under the state base dir."""
    result = c.exec(
        ["sh", "-c", f"cat {_STATE_BASE}/test-spans-overlay/* 2>/dev/null || true"],
    )
    return result.stdout


@pytest.mark.xdist_group("docker_daemon")
def test_overlay_body_never_leaks_to_tracked_or_base(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    c.write_text(_LOCAL_YAML, _OVERLAY_LOCAL_YAML)

    # 1. Install: the host-local body lands in the live file.
    rc, _out, err = _setforge(
        c,
        [
            "install",
            "--profile=test-spans-overlay",
            f"--config={CONFIG_FIXTURE}",
            "--yes",
        ],
    )
    assert rc == 0, err
    assert _BODY in c.read_text(_LIVE)

    # LEAK GATE (install side): tracked src + stored base are body-free.
    assert _BODY not in c.read_text(_TRACKED_SRC)
    assert _BODY not in _grep_base_for_body(c)

    # 2. Sync: capture live -> tracked. The body MUST be excised.
    rc, _out, err = _setforge(
        c,
        [
            "sync",
            "--profile=test-spans-overlay",
            f"--config={CONFIG_FIXTURE}",
            "--auto=use-live",
            "--yes",
        ],
    )
    assert rc == 0, err

    # LEAK GATE (sync side): the canonical assertion.
    assert _BODY not in c.read_text(_TRACKED_SRC)
    assert _BODY not in _grep_base_for_body(c)

    # 3. Re-install is idempotent: body present exactly once, still no leak.
    rc, _out, err = _setforge(
        c,
        [
            "install",
            "--profile=test-spans-overlay",
            f"--config={CONFIG_FIXTURE}",
            "--yes",
        ],
    )
    assert rc == 0, err
    assert c.read_text(_LIVE).count(_BODY) == 1
    assert _BODY not in c.read_text(_TRACKED_SRC)
    assert _BODY not in _grep_base_for_body(c)
