"""Docker e2e tests for the override-CLI verbs missing from the census.

``test_e2e_docker_override.py`` covers only ``override pin`` (PINNED) and
``override show --spans``. This file adds the inverse + inspection verbs the
audit flagged as e2e-uncovered: ``fork`` (the FORKED-disposition counterpart
of ``pin``), ``list`` (disposition/span/drift inspection), and the removal
round-trip (``fork`` → ``unfork`` / ``reset`` clearing the written entry).

The end-to-end consequence under test is disposition-driven: a file-level
``fork`` written via ``override fork --shared`` changes how ``install`` and
``sync`` treat the tracked_file — install 3-way merges (upstream-followed) but
``sync`` NEVER captures live edits back, exactly the behavior
``test_e2e_docker_disposition.py`` asserts for a YAML-declared ``forked``
disposition, here produced by the CLI write path instead.

The ``override_md`` / ``override_yaml`` fixtures (profile ``test-override`` in
``tests/fixtures/e2e/setforge.test.yaml``) start as file-level ``shared`` with
NO declared spans, so a ``fork`` flips the file-level disposition to forked.
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
_MD_HEADING = "# Disposition fixture"


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


def _init_shared_repo(c: ContainerHandle, repo: str) -> str:
    """Stage a git-initialized config-repo copy of the fixture; return its yaml.

    Mirrors ``test_override_shared_writes_config_repo_and_prints_hint`` — the
    ``--shared`` clean-check + post-write hint engage only against a clean git
    checkout, so the fixture is copied into a fresh repo and committed.
    """
    cfg = f"{repo}/setforge.yaml"
    c.exec(["mkdir", "-p", f"{repo}/tracked/disposition"], check=True)
    c.exec(["cp", CONFIG_FIXTURE, cfg], check=True)
    c.exec(["cp", _MD_TRACKED, f"{repo}/tracked/disposition/note.md"], check=True)
    c.exec(
        [
            "cp",
            "/workspace/tests/fixtures/e2e/tracked/disposition/config.yaml",
            f"{repo}/tracked/disposition/config.yaml",
        ],
        check=True,
    )
    for git_args in (
        ["git", "-C", repo, "init", "-q"],
        ["git", "-C", repo, "config", "user.email", "t@t"],
        ["git", "-C", repo, "config", "user.name", "t"],
        ["git", "-C", repo, "add", "-A"],
        ["git", "-C", repo, "commit", "-qm", "init"],
    ):
        c.exec(git_args, check=True)
    return cfg


# ---------------------------------------------------------------------------
# fork --shared writes the forked disposition into setforge.yaml, and the
# forked behavior holds end-to-end (install merges, sync never captures back).
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_override_fork_shared_writes_yaml_and_sync_never_captures_back(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """fork --shared (file-level) writes forked disposition; sync never captures.

    ``override fork override_md --shared`` flips the file-level disposition in
    the config-repo ``setforge.yaml`` from shared to forked and prints the
    commit/push hint (never auto-staging). A subsequent ``install`` deploys,
    and a live edit followed by ``sync`` leaves the tracked source unchanged —
    forked is upstream-followed on install but never captured back, the
    FORKED-disposition counterpart of the e2e-tested ``pin`` (live-wins).
    """
    c = docker_container()
    repo = "/tmp/override-fork-repo"
    cfg = _init_shared_repo(c, repo)

    rc, out, err = _setforge(
        c,
        [
            "override",
            "fork",
            "override_md",
            "--shared",
            f"--profile={_PROFILE}",
            f"--config={cfg}",
        ],
    )
    assert rc == 0, out + err

    # The forked disposition landed in the config-repo setforge.yaml.
    data = YAML(typ="safe").load(c.read_text(cfg))
    assert data["tracked_files"]["override_md"]["disposition"] == "forked"

    # The commit/push hint printed; the engine never auto-stages.
    assert "git commit" in out
    status = c.exec(["git", "-C", repo, "status", "--porcelain"]).stdout
    assert "setforge.yaml" in status

    # End-to-end forked behavior: install, edit live, sync — tracked unchanged.
    rc, out, err = _setforge(c, ["install", f"--profile={_PROFILE}", f"--config={cfg}"])
    assert rc == 0, out + err
    tracked_before = c.read_text(f"{repo}/tracked/disposition/note.md")
    live = c.read_text(_MD_LIVE)
    c.write_text(_MD_LIVE, live.replace("intro line", "intro line FORKED-LOCAL"))

    rc, out, err = _setforge(
        c, ["sync", f"--profile={_PROFILE}", f"--config={cfg}", "-y"]
    )
    assert rc == 0, out + err
    # forked never captures back: the tracked source is byte-unchanged.
    assert c.read_text(f"{repo}/tracked/disposition/note.md") == tracked_before


# ---------------------------------------------------------------------------
# fork (host-local span) then unfork removes the span; reset clears all state.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_override_fork_span_then_unfork_removes_entry(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """fork a host-local span → unfork removes exactly that span from local.yaml."""
    c = docker_container()
    local_yaml = "/home/tester/.config/setforge/local.yaml"

    rc, out, err = _override(c, ["fork", "override_md", _MD_HEADING])
    assert rc == 0, out + err
    local = c.read_text(local_yaml)
    assert _MD_HEADING in local
    assert "forked" in local

    # unfork removes the forked span; the anchor no longer appears.
    rc, out, err = _override(c, ["unfork", "override_md", _MD_HEADING])
    assert rc == 0, out + err
    assert "removed" in (out + err)
    after = c.read_text(local_yaml)
    assert _MD_HEADING not in after


@pytest.mark.xdist_group("docker_daemon")
def test_override_unpin_wrong_kind_leaves_forked_intact(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """unpin on a forked span warns + exits 0 without disturbing the fork."""
    c = docker_container()
    local_yaml = "/home/tester/.config/setforge/local.yaml"

    rc, out, err = _override(c, ["fork", "override_md", _MD_HEADING])
    assert rc == 0, out + err

    # unpin targets only PINNED; the forked span is the wrong kind → warn, exit 0.
    rc, out, err = _override(c, ["unpin", "override_md", _MD_HEADING])
    assert rc == 0, out + err
    assert "forked" in (out + err).lower()
    # The forked span survives untouched.
    assert _MD_HEADING in c.read_text(local_yaml)


@pytest.mark.xdist_group("docker_daemon")
def test_override_reset_clears_yaml_entry(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """reset clears ALL override state for the tracked_file from local.yaml."""
    c = docker_container()
    local_yaml = "/home/tester/.config/setforge/local.yaml"

    # Seed both a file-level disposition AND a span, then reset clears both.
    rc, out, err = _override(c, ["fork", "override_md"])
    assert rc == 0, out + err
    rc, out, err = _override(c, ["pin", "override_md", _MD_HEADING])
    assert rc == 0, out + err
    seeded = c.read_text(local_yaml)
    assert _MD_HEADING in seeded

    rc, out, err = _override(c, ["reset", "override_md"])
    assert rc == 0, out + err
    assert "reset" in (out + err)
    after = c.read_text(local_yaml)
    # Neither the span anchor nor a disposition value lingers for this file.
    assert _MD_HEADING not in after
    assert "forked" not in after


# ---------------------------------------------------------------------------
# list renders the disposition + drift state columns.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_override_list_renders_disposition_and_drift(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """list renders each tracked_file's disposition + drift state.

    The fixture declares ``override_md`` / ``override_yaml`` as ``shared``;
    after a clean install, ``list`` shows the file ids, their shared
    disposition, and an in-sync state. After a host-local pin + a live edit
    inside the pinned span, ``list`` reports that file's drift as expected.
    """
    c = docker_container()

    rc, out, err = _install(c)
    assert rc == 0, out + err

    rc, out, err = _override(c, ["list"])
    assert rc == 0, out + err
    assert "override_md" in out
    assert "override_yaml" in out
    assert "shared" in out
    assert "in sync" in out

    # Pin a span, install, then edit live inside it → expected drift surfaces.
    rc, out, err = _override(c, ["pin", "override_md", _MD_HEADING])
    assert rc == 0, out + err
    rc, out, err = _install(c)
    assert rc == 0, out + err
    live = c.read_text(_MD_LIVE)
    c.write_text(_MD_LIVE, live.replace("intro line", "intro line LOCAL"))

    rc, out, err = _override(c, ["list"])
    assert rc == 0, out + err
    assert "expected drift" in out
