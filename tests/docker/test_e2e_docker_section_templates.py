"""Docker e2e tests for seed-once host-local section templates.

Exercises the SEED-ONCE template library end to end against a fresh
Debian container with the real installed ``setforge`` CLI:

- First ``install`` seeds an empty host-local section named in the
  profile's ``section_slots`` with the ``section_templates`` library body;
  the seeded body lands in the deployed live file.
- A live edit captured into the host's overlay survives a re-install: the
  library template does NOT overwrite the populated section.
- A section the host already declared (a pre-existing overlay body) is
  left untouched on first install — never reseeded.

Profile under exercise: ``test-section-templates`` (declared in
``tests/fixtures/e2e/setforge.test.yaml`` with a ``section_templates``
registry entry and a ``section_slots`` map).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_HOME_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"
_LIVE_DOC = "/home/tester/.setforge_e2e/section-templates/seed-doc.md"
_TEMPLATE_BODY_MARKER = "SEEDED PYTHON CONVENTIONS"
_PROFILE = "test-section-templates"


def _install(c: ContainerHandle, *, check: bool = False) -> tuple[int, str, str]:
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
        ],
        check=check,
    )
    return result.returncode, result.stdout, result.stderr


def test_install_seeds_empty_section(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """First install seeds the empty slot; the template body lands in live."""
    c = docker_container()
    rc, _stdout, stderr = _install(c)
    assert rc == 0, stderr

    # The seeded body deployed into the live doc.
    live = c.read_text(_LIVE_DOC)
    assert _TEMPLATE_BODY_MARKER in live, live

    # local.yaml was seeded by install (then migrated to an OVERLAY span).
    local = c.read_text(_HOME_LOCAL_YAML)
    assert "python-conventions" in local, local


def test_live_edit_survives_reinstall(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A host edit to the seeded section survives re-install; the template
    does NOT clobber the populated section."""
    c = docker_container()
    assert _install(c)[0] == 0

    # Host edits the adopted body in local.yaml (the host-owned store).
    local = c.read_text(_HOME_LOCAL_YAML)
    edited = local.replace(_TEMPLATE_BODY_MARKER, "MY HOST EDIT")
    assert edited != local, "expected the seeded body in local.yaml to edit"
    c.write_text(_HOME_LOCAL_YAML, edited)

    # Re-install must NOT reseed the now-populated section.
    rc, _stdout, stderr = _install(c)
    assert rc == 0, stderr

    final_local = c.read_text(_HOME_LOCAL_YAML)
    assert "MY HOST EDIT" in final_local, final_local
    assert _TEMPLATE_BODY_MARKER not in final_local, final_local

    live = c.read_text(_LIVE_DOC)
    assert "MY HOST EDIT" in live, live
    assert _TEMPLATE_BODY_MARKER not in live, live


def test_prepopulated_section_left_untouched(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A section the host already declared is never reseeded on first install."""
    c = docker_container()
    c.write_text(
        _HOME_LOCAL_YAML,
        "tracked_files:\n"
        "  section_seed_doc:\n"
        "    host_local_sections:\n"
        "      python-conventions:\n"
        "        anchor: {kind: at-end-of-file}\n"
        "        body: |\n"
        "          PRE-EXISTING HOST BODY\n",
    )

    rc, _stdout, stderr = _install(c)
    assert rc == 0, stderr

    final_local = c.read_text(_HOME_LOCAL_YAML)
    assert "PRE-EXISTING HOST BODY" in final_local, final_local
    assert _TEMPLATE_BODY_MARKER not in final_local, final_local

    live = c.read_text(_LIVE_DOC)
    assert "PRE-EXISTING HOST BODY" in live, live
    assert _TEMPLATE_BODY_MARKER not in live, live
