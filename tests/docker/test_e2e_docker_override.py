"""Docker e2e tests for the ``override`` CLI (the disposition+span front door).

Exercises the user-visible round-trips unit tests cannot: pinning a span via
``override pin`` writes the host-local ``local.yaml`` (or, with ``--shared``,
the config-repo ``setforge.yaml``), ``override show --spans`` renders to
stdout WITHOUT touching the file on disk, and a subsequent ``install`` +
``compare`` reports the pinned region's drift as EXPECTED (Invariant I13).

Three journeys:

- **markdown** — ``override pin <md> "# heading"`` (host-local) → ``show
  --spans`` (file byte-unchanged) → ``install`` → live edit inside the pinned
  span → ``compare --check`` exits 0 (expected drift).
- **structural** — the same for a yaml dotted-path anchor.
- **--shared** — ``override pin <md> "# heading" --shared`` against a
  git-initialized config-repo copy writes the entry into ``setforge.yaml`` and
  prints the commit/push hint; the engine never auto-stages.

Profile ``test-override`` (in ``tests/fixtures/e2e/setforge.test.yaml``)
declares ``override_md`` (``disposition/note.md``) and ``override_yaml``
(``disposition/config.yaml``) as shared with NO pre-declared spans.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from ruamel.yaml import YAML

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_PROFILE = "test-override"
_MD_TRACKED = "/workspace/tests/fixtures/e2e/tracked/disposition/note.md"
_MD_LIVE = "/home/tester/.setforge_e2e/override/note.md"
_YAML_TRACKED = "/workspace/tests/fixtures/e2e/tracked/disposition/config.yaml"
_YAML_LIVE = "/home/tester/.setforge_e2e/override/config.yaml"
_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"

_MD_HEADING = "# Disposition fixture"
_YAML_ANCHOR = "sharedKey"


def _setforge(c: ContainerHandle, args: list[str]) -> tuple[int, str, str]:
    """Run ``uv run setforge <args>``; return (returncode, stdout, stderr)."""
    result = c.exec(["uv", "run", "setforge", *args], check=False)
    return result.returncode, result.stdout, result.stderr


def _override(c: ContainerHandle, args: list[str]) -> tuple[int, str, str]:
    return _setforge(
        c, ["override", *args, f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}"]
    )


def _install(c: ContainerHandle) -> tuple[int, str, str]:
    return _setforge(
        c, ["install", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}"]
    )


def _value_at(text: str, path: str) -> object:
    doc = YAML(typ="safe").load(text)
    node: object = doc
    for seg in path.split("."):
        node = node[seg]  # type: ignore[index]
    return node


# ---------------------------------------------------------------------------
# markdown journey
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_override_markdown_pin_show_install_compare(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """pin a markdown span (host-local) → show (stdout only) → install → compare."""
    c = docker_container()

    # pin host-local: writes ~/.config/setforge/local.yaml, not the tracked src.
    rc, out, err = _override(c, ["pin", "override_md", _MD_HEADING])
    assert rc == 0, out + err
    local = c.read_text(_LOCAL_YAML)
    assert "override_md" in local
    assert _MD_HEADING in local

    # show --spans renders to stdout; the tracked file stays byte-identical.
    before = c.read_text(_MD_TRACKED)
    rc, out, err = _override(c, ["show", "override_md", "--spans"])
    assert rc == 0, out + err
    assert "(virtual)" in out
    assert "ORPHANED" in out
    assert c.read_text(_MD_TRACKED) == before, "show must not mutate the tracked file"

    # install deploys; then a live edit INSIDE the pinned span is expected drift.
    rc, out, err = _install(c)
    assert rc == 0, out + err
    live = c.read_text(_MD_LIVE)
    c.write_text(_MD_LIVE, live.replace("intro line", "intro line LOCAL"))

    rc, out, err = _setforge(
        c, ["compare", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}", "--check"]
    )
    assert rc == 0, out + err


# ---------------------------------------------------------------------------
# structural journey
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_override_structural_pin_show_install_compare(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """pin a yaml dotted path (host-local) → show → install → compare."""
    c = docker_container()

    rc, out, err = _override(c, ["pin", "override_yaml", _YAML_ANCHOR])
    assert rc == 0, out + err
    local = c.read_text(_LOCAL_YAML)
    assert _YAML_ANCHOR in local

    before = c.read_text(_YAML_TRACKED)
    rc, out, err = _override(c, ["show", "override_yaml", "--spans"])
    assert rc == 0, out + err
    assert _YAML_ANCHOR in out
    assert "ORPHANED" in out
    assert c.read_text(_YAML_TRACKED) == before

    rc, out, err = _install(c)
    assert rc == 0, out + err
    live = c.read_text(_YAML_LIVE)
    c.write_text(_YAML_LIVE, live.replace("sharedKey: original", "sharedKey: LOCAL"))

    rc, out, err = _setforge(
        c, ["compare", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}", "--check"]
    )
    assert rc == 0, out + err
    # The pinned value survives install (live wins); tracked is unchanged.
    assert _value_at(c.read_text(_YAML_TRACKED), _YAML_ANCHOR) == "original"


# ---------------------------------------------------------------------------
# --shared journey
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_override_shared_writes_config_repo_and_prints_hint(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """override pin --shared writes the config-repo setforge.yaml + prints the hint.

    Copy the fixture into a git-initialized config-repo so the clean-check +
    post-write hint engage, then assert the shared span landed in
    ``setforge.yaml`` AND the commit/push reminder printed (never auto-staged).
    """
    c = docker_container()
    repo = "/tmp/override-shared-repo"
    cfg = f"{repo}/setforge.yaml"
    # Stage a config-repo copy (setforge.yaml + tracked/) and git-init it clean.
    c.exec(["mkdir", "-p", f"{repo}/tracked/disposition"], check=True)
    c.exec(["cp", CONFIG_FIXTURE, cfg], check=True)
    c.exec(["cp", _MD_TRACKED, f"{repo}/tracked/disposition/note.md"], check=True)
    c.exec(["cp", _YAML_TRACKED, f"{repo}/tracked/disposition/config.yaml"], check=True)
    for git_args in (
        ["git", "-C", repo, "init", "-q"],
        ["git", "-C", repo, "config", "user.email", "t@t"],
        ["git", "-C", repo, "config", "user.name", "t"],
        ["git", "-C", repo, "add", "-A"],
        ["git", "-C", repo, "commit", "-qm", "init"],
    ):
        c.exec(git_args, check=True)

    rc, out, err = _setforge(
        c,
        [
            "override",
            "pin",
            "override_md",
            _MD_HEADING,
            "--shared",
            f"--profile={_PROFILE}",
            f"--config={cfg}",
        ],
    )
    assert rc == 0, out + err

    # The shared span landed in the config-repo setforge.yaml.
    data = YAML(typ="safe").load(c.read_text(cfg))
    spans = data["tracked_files"]["override_md"]["spans"]
    assert any(s["anchor"] == _MD_HEADING and s["semantics"] == "shared" for s in spans)

    # The commit/push hint printed (B-C3).
    assert "git commit" in out

    # The engine NEVER auto-stages: the working tree is dirty (setforge.yaml
    # modified but not added).
    status = c.exec(["git", "-C", repo, "status", "--porcelain"]).stdout
    assert "setforge.yaml" in status
