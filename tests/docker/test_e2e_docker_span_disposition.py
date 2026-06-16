"""Docker e2e: validate/install refuse a pinned span with no disposition.

A ``pinned``/``forked`` span on a tracked_file with no ``disposition`` is
silently ignored on the verbatim deploy path and not excluded on capture
(host-local content can leak into tracked). The offline ``validate`` gate
exits 1 and ``install`` refuses at pre-flight, both with the same guard.

Spins one fresh container, writes a crafted ``setforge.yaml`` whose markdown
tracked_file carries a dispositionless ``pinned`` span, and asserts the
captured stdout/stderr + exit codes for both verbs.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

_WORKDIR: str = "/workspace"
_SETFORGE_YAML: str = f"{_WORKDIR}/setforge.yaml"
_TRACKED_MD: str = f"{_WORKDIR}/tracked/note.md"

_PINNED_NO_DISPOSITION: str = (
    "version: 1\n"
    "tracked_files:\n"
    "  d:\n"
    "    src: note.md\n"
    "    dst: ~/.note.md\n"
    "    spans:\n"
    '      - anchor: "## My Tweaks"\n'
    "        kind: pinned\n"
    "        semantics: shared\n"
    "profiles:\n"
    "  p:\n"
    "    tracked_files: [d]\n"
)


def _seed(c: ContainerHandle) -> None:
    c.exec(["mkdir", "-p", f"{_WORKDIR}/tracked"], check=True)
    c.write_text(_TRACKED_MD, "## My Tweaks\n\nbody\n")
    c.write_text(_SETFORGE_YAML, _PINNED_NO_DISPOSITION)


def test_validate_and_install_reject_pinned_span_without_disposition(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``validate`` exits 1 and ``install`` refuses pre-flight for a pinned
    span declared on a tracked_file with no disposition."""
    c = docker_container()
    _seed(c)

    val = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "validate",
            "--profile=p",
            f"--config={_SETFORGE_YAML}",
        ],
        check=False,
    )
    val_out = val.stdout + val.stderr
    assert val.returncode == 1, val_out
    assert "disposition" in val_out, val_out
    assert "'d'" in val_out, val_out

    inst = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            "--profile=p",
            f"--config={_SETFORGE_YAML}",
            "--no-git-check",
            "--no-secrets-scan",
            "--yes",
        ],
        check=False,
    )
    inst_out = inst.stdout + inst.stderr
    assert inst.returncode != 0, inst_out
    assert "disposition" in inst_out, inst_out
