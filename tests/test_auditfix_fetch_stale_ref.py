"""Regression test for the stale-branch-ref audit finding (fetch_stale_ref).

``fetch_source`` used to ``git fetch`` (advancing only ``origin/<branch>``)
then ``git checkout <branch>`` (landing on the never-advanced local branch),
so a re-fetch of an existing clone kept serving the commit the branch had at
first clone. The fix fast-forwards the local branch to the fetched upstream
tip when the ref is a branch. Uses real ``git`` (the git_ops test surface
convention) so the actual ref-advancement contract is exercised.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from setforge.source import GitSource, SourceKind, fetch_source


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _make_upstream(tmp_path: Path) -> Path:
    """Create a bare upstream with a ``tracked/hello.txt`` on ``main``."""
    work = tmp_path / "work"
    work.mkdir()
    _run(["git", "init", "-q", "-b", "main"], cwd=work)
    _run(["git", "config", "user.email", "t@t"], cwd=work)
    _run(["git", "config", "user.name", "Test"], cwd=work)
    tracked = work / "tracked"
    tracked.mkdir()
    (tracked / "hello.txt").write_text("v1\n")
    _run(["git", "add", "."], cwd=work)
    _run(["git", "commit", "-q", "-m", "v1"], cwd=work)

    bare = tmp_path / "upstream.git"
    _run(["git", "init", "-q", "-b", "main", "--bare", str(bare)], cwd=tmp_path)
    _run(["git", "remote", "add", "origin", str(bare)], cwd=work)
    _run(["git", "push", "-q", "origin", "main"], cwd=work)
    return bare


def _push_new_commit(tmp_path: Path, bare: Path) -> None:
    """Clone the bare, change ``tracked/hello.txt``, push a new ``main`` tip."""
    pusher = tmp_path / "pusher"
    _run(["git", "clone", "-q", str(bare), str(pusher)], cwd=tmp_path)
    _run(["git", "config", "user.email", "t@t"], cwd=pusher)
    _run(["git", "config", "user.name", "Test"], cwd=pusher)
    (pusher / "tracked" / "hello.txt").write_text("v2\n")
    _run(["git", "commit", "-aqm", "v2"], cwd=pusher)
    _run(["git", "push", "-q", "origin", "main"], cwd=pusher)


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git binary required",
)
class TestFetchUpdatesBranchToNewUpstreamCommit:
    def test_refetch_branch_advances_to_new_upstream_tip(self, tmp_path: Path) -> None:
        bare = _make_upstream(tmp_path)
        clone_dest = tmp_path / "clone"
        source = GitSource(
            kind=SourceKind.GIT,
            url=str(bare),
            ref="main",
            clone_dest=clone_dest,
        )

        # First fetch clones and checks out main at v1.
        fetch_source(source)
        assert (clone_dest / "tracked" / "hello.txt").read_text() == "v1\n"

        # Upstream main advances to v2.
        _push_new_commit(tmp_path, bare)

        # Re-fetch of the EXISTING clone must serve the new upstream content.
        # Pre-fix this stayed "v1\n" because the local branch never advanced.
        fetch_source(source)
        assert (clone_dest / "tracked" / "hello.txt").read_text() == "v2\n"

    def test_sha_ref_is_not_fast_forwarded(self, tmp_path: Path) -> None:
        """A pinned SHA ref must stay detached at that commit, not advance."""
        bare = _make_upstream(tmp_path)
        # Resolve the v1 SHA from a throwaway clone.
        probe = tmp_path / "probe"
        _run(["git", "clone", "-q", str(bare), str(probe)], cwd=tmp_path)
        v1_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=probe,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        clone_dest = tmp_path / "clone"
        source = GitSource(
            kind=SourceKind.GIT,
            url=str(bare),
            ref=v1_sha,
            clone_dest=clone_dest,
        )
        fetch_source(source)
        _push_new_commit(tmp_path, bare)
        fetch_source(source)

        # Pinned SHA: working tree stays at v1 and HEAD is detached at the SHA.
        assert (clone_dest / "tracked" / "hello.txt").read_text() == "v1\n"
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=clone_dest,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head == v1_sha
