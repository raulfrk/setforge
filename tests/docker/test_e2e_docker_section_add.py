"""Docker E2E tests for ``setforge section add`` / ``section emit`` (w7x).

Run in real Debian containers with the actual installed ``setforge``
CLI. Coverage matrix:

- ``section emit`` (shared / host-local / refuse-invalid / paste-round-trip).
- ``section add`` scripted (shared / host-local / file-body / round-trip
  through ``extract_sections``).
- Refusals: non-markdown suffix, duplicate-name, anchor-past-EOF.
- Non-TTY no-flags → exit 2.
- Help-surface presence checks.
- Cross-bead-integration: bare ``install`` after ``section add`` deploys
  the new marker pair cleanly.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_TRACKED_MARKED = "/workspace/tests/fixtures/e2e/tracked/sections/marked.md"
_LIVE_MARKED = "/home/tester/.setforge_e2e/sections/marked.md"


def _setforge(
    container: ContainerHandle,
    args: list[str],
    *,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``uv run setforge ...`` inside the container."""
    return container.exec(
        ["uv", "run", "setforge", *args],
        check=check,
    )


# --- section emit ---


def test_section_emit_shared_in_container(
    docker_container: pytest.FixtureRequest,
) -> None:
    c = docker_container()
    result = _setforge(c, ["section", "emit", "shared", "foo"])
    assert result.returncode == 0, result.stderr
    assert "<!-- setforge:user-section start shared foo -->" in result.stdout
    assert "<!-- setforge:user-section end shared foo hash=" in result.stdout


def test_section_emit_host_local_in_container(
    docker_container: pytest.FixtureRequest,
) -> None:
    c = docker_container()
    result = _setforge(c, ["section", "emit", "host-local", "bar"])
    assert result.returncode == 0, result.stderr
    assert "host-local bar" in result.stdout


def test_section_emit_invalid_name_exits_2_in_container(
    docker_container: pytest.FixtureRequest,
) -> None:
    c = docker_container()
    result = _setforge(c, ["section", "emit", "shared", "Foo"])
    assert result.returncode == 2


def test_section_emit_round_trips_through_extract_sections(
    docker_container: pytest.FixtureRequest,
) -> None:
    """Emit output, paste it into a fresh markdown file, parse it back."""
    c = docker_container()
    emit = _setforge(c, ["section", "emit", "shared", "rt"])
    assert emit.returncode == 0, emit.stderr
    c.write_text("/tmp/paste.md", emit.stdout)
    # extract_sections lives in setforge.sections; round-trip via python -c.
    verify = c.exec(
        [
            "uv",
            "run",
            "python",
            "-c",
            "import sys; from pathlib import Path; "
            "from setforge.sections import extract_sections; "
            "text = Path('/tmp/paste.md').read_text(); "
            "sections = extract_sections(text, allow_legacy=True); "
            "sys.exit(0 if 'rt' in sections else 1)",
        ],
        workdir="/workspace",
        check=False,
    )
    assert verify.returncode == 0, verify.stderr


# --- section add scripted ---


def _seed_minimal_md(c: ContainerHandle, *, path: str) -> None:
    """Write a minimal markdown file with no user-section markers."""
    c.write_text(
        path,
        "line 1\nline 2\nline 3\nline 4\nline 5\n",
    )


def test_section_add_scripted_shared_writes_marker_pair_in_container(
    docker_container: pytest.FixtureRequest,
) -> None:
    c = docker_container()
    target = "/workspace/tests/fixtures/e2e/tracked/sections/marked.md"
    # Original 'notes' section exists; add a new 'extras' section.
    result = _setforge(
        c,
        [
            "section", "add",
            "--profile=test-text-sections", f"--config={CONFIG_FIXTURE}",
            "--tracked-file=sections_md", "--semantics=shared", "--name=extras",
            "--anchor-line=2", "--body-source=empty", "--yes",
        ],
        check=False,
    )
    assert result.returncode == 0, result.stderr
    content = c.read_text(target)
    assert "<!-- setforge:user-section start shared extras -->" in content
    assert "<!-- setforge:user-section end shared extras hash=" in content


def test_section_add_scripted_host_local_writes_marker_pair_in_container(
    docker_container: pytest.FixtureRequest,
) -> None:
    c = docker_container()
    result = _setforge(
        c,
        [
            "section", "add",
            "--profile=test-text-sections", f"--config={CONFIG_FIXTURE}",
            "--tracked-file=sections_md", "--semantics=host-local",
            "--name=morenotes",
            "--anchor-line=1", "--body-source=empty", "--yes",
        ],
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "host-local morenotes" in c.read_text(_TRACKED_MARKED)


def test_section_add_scripted_with_file_body_in_container(
    docker_container: pytest.FixtureRequest,
) -> None:
    c = docker_container()
    c.write_text("/tmp/body.md", "custom body content\n")
    result = _setforge(
        c,
        [
            "section", "add",
            "--profile=test-text-sections", f"--config={CONFIG_FIXTURE}",
            "--tracked-file=sections_md", "--semantics=shared",
            "--name=custom",
            "--anchor-line=1", "--body-source=file",
            "--body-file=/tmp/body.md", "--yes",
        ],
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "custom body content" in c.read_text(_TRACKED_MARKED)


def test_section_add_then_install_deploys_marker_pair_to_live(
    docker_container: pytest.FixtureRequest,
) -> None:
    """Cross-bead: add a shared marker, install, the live file gets it."""
    c = docker_container()
    add = _setforge(
        c,
        [
            "section", "add",
            "--profile=test-text-sections", f"--config={CONFIG_FIXTURE}",
            "--tracked-file=sections_md", "--semantics=shared",
            "--name=postinstall",
            "--anchor-line=2", "--body-source=empty", "--yes",
        ],
        check=False,
    )
    assert add.returncode == 0, add.stderr
    install = _setforge(
        c,
        [
            "install",
            "--profile=test-text-sections", f"--config={CONFIG_FIXTURE}",
        ],
        check=False,
    )
    assert install.returncode == 0, install.stderr
    live = c.read_text(_LIVE_MARKED)
    assert "shared postinstall" in live


# --- section add refusal cases ---


def test_section_add_refuses_non_markdown_in_container(
    docker_container: pytest.FixtureRequest,
) -> None:
    c = docker_container()
    # json_settings -> json/settings.json (not markdown).
    result = _setforge(
        c,
        [
            "section", "add",
            "--profile=test-json", f"--config={CONFIG_FIXTURE}",
            "--tracked-file=json_settings", "--semantics=shared",
            "--name=foo",
            "--anchor-line=1", "--body-source=empty", "--yes",
        ],
        check=False,
    )
    assert result.returncode == 2
    # Hint should mention `section emit`.
    combined = result.stderr + result.stdout
    assert "section emit" in combined or "markdown" in combined.lower()


def test_section_add_refuses_duplicate_name_in_container(
    docker_container: pytest.FixtureRequest,
) -> None:
    c = docker_container()
    # marked.md fixture already has a section named 'notes'.
    result = _setforge(
        c,
        [
            "section", "add",
            "--profile=test-text-sections", f"--config={CONFIG_FIXTURE}",
            "--tracked-file=sections_md", "--semantics=shared",
            "--name=notes",
            "--anchor-line=1", "--body-source=empty", "--yes",
        ],
        check=False,
    )
    assert result.returncode == 2


def test_section_add_refuses_anchor_past_eof_in_container(
    docker_container: pytest.FixtureRequest,
) -> None:
    c = docker_container()
    result = _setforge(
        c,
        [
            "section", "add",
            "--profile=test-text-sections", f"--config={CONFIG_FIXTURE}",
            "--tracked-file=sections_md", "--semantics=shared",
            "--name=eof",
            "--anchor-line=9999", "--body-source=empty", "--yes",
        ],
        check=False,
    )
    assert result.returncode == 2


def test_section_add_non_tty_no_flags_exits_2_in_container(
    docker_container: pytest.FixtureRequest,
) -> None:
    """No --tracked-file/--semantics/--name flags + piped stdin → exit 2."""
    c = docker_container()
    result = _setforge(
        c,
        [
            "section", "add",
            "--profile=test-text-sections", f"--config={CONFIG_FIXTURE}",
        ],
        check=False,
    )
    assert result.returncode == 2
    combined = result.stderr + result.stdout
    assert "interactive" in combined.lower() or "--yes" in combined


# --- help-surface presence ---


def test_section_visible_in_root_help_in_container(
    docker_container: pytest.FixtureRequest,
) -> None:
    c = docker_container()
    result = _setforge(c, ["--help"])
    assert result.returncode == 0
    assert "section" in result.stdout


def test_section_subcommands_visible_in_section_help_in_container(
    docker_container: pytest.FixtureRequest,
) -> None:
    c = docker_container()
    result = _setforge(c, ["section", "--help"])
    assert result.returncode == 0
    assert "add" in result.stdout
    assert "emit" in result.stdout
