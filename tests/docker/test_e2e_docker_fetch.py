"""Docker e2e tests for ``setforge fetch`` (git-mgmt).

Uses a local bare-repo inside the container served via ``file://`` URL
— no network access needed. Each test instantiates a fresh container
via the ``docker_container`` fixture and exercises the fetch flow
end-to-end (clone-on-missing, fetch+checkout, dirty-tracked abort,
PathSource no-op).

The Docker image's ``setforge`` CLI is the installed package per the
Dockerfile; tests configure ``~/.config/setforge/local.yaml`` with a
git source pointing at the bare repo, then call ``setforge fetch``
inside the container.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker


_BARE_REPO = "/tmp/upstream.git"
_CHECKOUT_AREA = "/tmp/staging"  # where we build up commits before pushing
_HOME_CONFIG = "/home/tester/.config/setforge/local.yaml"
_CLONE_DEST = "/home/tester/.local/share/setforge/sources/upstream"


def _git_setup_bare_upstream(c: ContainerHandle) -> None:
    """Initialize ``/srv/upstream.git`` as a bare repo with setforge.yaml + tracked/."""
    # Bare repo
    c.exec(["git", "init", "-q", "--bare", _BARE_REPO])
    # Staging area to build the first commit
    c.exec(["git", "init", "-q", "-b", "main", _CHECKOUT_AREA])
    c.exec(["git", "config", "user.email", "test@example.com"], workdir=_CHECKOUT_AREA)
    c.exec(["git", "config", "user.name", "Test"], workdir=_CHECKOUT_AREA)
    # Minimal config + tracked content
    c.write_text(
        f"{_CHECKOUT_AREA}/setforge.yaml",
        "version: 1\n"
        "tracked_files:\n"
        "  hello:\n"
        "    src: hello.txt\n"
        "    dst: /tmp/hello.out\n"
        "profiles:\n"
        "  minimal:\n"
        "    tracked_files: [hello]\n",
    )
    c.write_text(f"{_CHECKOUT_AREA}/tracked/hello.txt", "hello world\n")
    c.exec(["git", "add", "."], workdir=_CHECKOUT_AREA)
    c.exec(
        ["git", "commit", "-q", "-m", "initial"],
        workdir=_CHECKOUT_AREA,
    )
    # Push to bare upstream
    c.exec(
        ["git", "push", "-q", _BARE_REPO, "main"],
        workdir=_CHECKOUT_AREA,
    )


def _write_local_yaml_git_source(c: ContainerHandle, ref: str = "main") -> None:
    """Write a git-kind ``source:`` block to ``~/.config/setforge/local.yaml``."""
    c.write_text(
        _HOME_CONFIG,
        f"source:\n"
        f"  kind: git\n"
        f"  url: file://{_BARE_REPO}\n"
        f"  ref: {ref}\n"
        f"  name: upstream\n",
    )


def _write_local_yaml_path_source(c: ContainerHandle, path: str) -> None:
    """Write a path-kind ``source:`` block to ``~/.config/setforge/local.yaml``."""
    c.write_text(
        _HOME_CONFIG,
        f"source:\n  kind: path\n  path: {path}\n",
    )


class TestSetforgeFetchGitSource:
    """``setforge fetch`` against a git source: clone-on-missing + checkout."""

    def test_fetch_clones_missing_source(
        self,
        docker_container: Callable[..., ContainerHandle],
    ) -> None:
        c = docker_container()
        _git_setup_bare_upstream(c)
        _write_local_yaml_git_source(c)
        # Pre-condition: clone_dest doesn't exist yet.
        result_pre = c.exec(["test", "-d", _CLONE_DEST], check=False)
        assert result_pre.returncode != 0
        # Run fetch.
        result = c.exec(["uv", "run", "setforge", "fetch"], workdir="/workspace")
        assert result.returncode == 0
        assert "cloned and checked out main" in result.stdout
        # Post-condition: clone_dest exists with the setforge.yaml inside.
        assert (
            c.exec(
                ["test", "-f", f"{_CLONE_DEST}/setforge.yaml"], check=False
            ).returncode
            == 0
        )
        # tracked/ subtree carries over.
        assert (
            c.exec(
                ["test", "-f", f"{_CLONE_DEST}/tracked/hello.txt"], check=False
            ).returncode
            == 0
        )

    def test_fetch_twice_idempotent(
        self,
        docker_container: Callable[..., ContainerHandle],
    ) -> None:
        c = docker_container()
        _git_setup_bare_upstream(c)
        _write_local_yaml_git_source(c)
        first = c.exec(["uv", "run", "setforge", "fetch"], workdir="/workspace")
        assert first.returncode == 0
        assert "cloned" in first.stdout
        # Second fetch should not clone again; just fetch + checkout.
        second = c.exec(["uv", "run", "setforge", "fetch"], workdir="/workspace")
        assert second.returncode == 0
        assert "fetched and checked out main" in second.stdout
        assert "cloned" not in second.stdout

    def test_fetch_aborts_on_dirty_tracked(
        self,
        docker_container: Callable[..., ContainerHandle],
    ) -> None:
        c = docker_container()
        _git_setup_bare_upstream(c)
        _write_local_yaml_git_source(c)
        c.exec(["uv", "run", "setforge", "fetch"], workdir="/workspace")
        # Make tracked/ dirty inside the clone.
        c.write_text(f"{_CLONE_DEST}/tracked/hello.txt", "DIRTY EDIT\n")
        # Second fetch should abort.
        result = c.exec(
            ["uv", "run", "setforge", "fetch"], workdir="/workspace", check=False
        )
        assert result.returncode != 0
        assert "uncommitted changes" in result.stderr.lower() or (
            "uncommitted changes" in result.stdout.lower()
        )

    def test_fetch_checks_out_pinned_ref(
        self,
        docker_container: Callable[..., ContainerHandle],
    ) -> None:
        c = docker_container()
        _git_setup_bare_upstream(c)
        # Create a feature branch on the upstream and push it.
        c.exec(
            ["git", "checkout", "-q", "-b", "feature"],
            workdir=_CHECKOUT_AREA,
        )
        c.write_text(f"{_CHECKOUT_AREA}/tracked/feature.txt", "feature\n")
        c.exec(["git", "add", "."], workdir=_CHECKOUT_AREA)
        c.exec(
            ["git", "commit", "-q", "-m", "feature"],
            workdir=_CHECKOUT_AREA,
        )
        c.exec(
            ["git", "push", "-q", _BARE_REPO, "feature"],
            workdir=_CHECKOUT_AREA,
        )
        # Configure ref=feature; fetch should check out that branch.
        _write_local_yaml_git_source(c, ref="feature")
        result = c.exec(["uv", "run", "setforge", "fetch"], workdir="/workspace")
        assert result.returncode == 0
        # feature.txt should be present in the clone.
        assert (
            c.exec(
                ["test", "-f", f"{_CLONE_DEST}/tracked/feature.txt"], check=False
            ).returncode
            == 0
        )


class TestSetforgeFetchPathSource:
    """``setforge fetch`` against a PathSource is a no-op."""

    def test_fetch_path_source_is_noop(
        self,
        docker_container: Callable[..., ContainerHandle],
    ) -> None:
        c = docker_container()
        # Create a plain directory with setforge.yaml inside.
        c.exec(["mkdir", "-p", "/tmp/plain/tracked"])
        c.write_text(
            "/tmp/plain/setforge.yaml",
            "version: 1\ntracked_files: {}\nprofiles: {minimal: {tracked_files: []}}\n",
        )
        _write_local_yaml_path_source(c, "/tmp/plain")
        result = c.exec(["uv", "run", "setforge", "fetch"], workdir="/workspace")
        assert result.returncode == 0
        assert "source is a path" in result.stdout
        assert "nothing to fetch" in result.stdout


class TestSourceLayerIntegration:
    """End-to-end: `setforge install` resolves setforge.yaml via source-layer.

    The --config flag continues to work; the source-layer fires when
    --config is at its default AND CWD has no setforge.yaml. To test
    the source-layer without CWD-fallback contamination, use the
    --source root flag (highest precedence layer).
    """

    def test_install_uses_source_flag(
        self,
        docker_container: Callable[..., ContainerHandle],
    ) -> None:
        c = docker_container()
        # Build a minimal config dir at /tmp/plain/ with setforge.yaml +
        # tracked/hello.txt. Profile deploys hello -> /tmp/hello.out.
        c.exec(["mkdir", "-p", "/tmp/plain/tracked"])
        c.write_text(
            "/tmp/plain/setforge.yaml",
            "version: 1\n"
            "tracked_files:\n"
            "  hello:\n"
            "    src: hello.txt\n"
            "    dst: /tmp/hello.out\n"
            "profiles:\n"
            "  minimal:\n"
            "    tracked_files: [hello]\n",
        )
        c.write_text("/tmp/plain/tracked/hello.txt", "from-source-layer\n")
        # Use the --source root flag (highest precedence) so we don't
        # have to fight CWD-fallback for /workspace's own setforge.yaml.
        result = c.exec(
            [
                "uv",
                "run",
                "setforge",
                "--source=/tmp/plain",
                "install",
                "--profile=minimal",
            ],
            workdir="/workspace",
            check=False,
        )
        assert result.returncode == 0, (
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # Live file matches tracked content.
        assert c.read_text("/tmp/hello.out") == "from-source-layer\n"
