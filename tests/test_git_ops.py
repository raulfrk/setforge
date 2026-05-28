"""Tests for setforge/git_ops.py — git subprocess wrappers.

Uses real ``git init`` + commits in tmp_path rather than subprocess
mocks. Mocks are fragile for git (argv variations, output formats);
hitting the real binary tests the actual contract. The whole git_ops
module is a thin subprocess shim, so this is the natural test surface.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from setforge.errors import GitOpError
from setforge.git_ops import (
    git_checkout,
    git_clone,
    git_fetch,
    is_git_repo,
    rev_parse_upstream,
    status_porcelain,
)


def _git_init(repo: Path, *, initial_branch: str = "main") -> Path:
    """Initialize a git repo at ``repo`` with one commit on ``initial_branch``."""
    repo.mkdir(parents=True, exist_ok=True)
    # -q quiet; -b sets initial branch (git 2.28+)
    subprocess.run(
        ["git", "init", "-q", "-b", initial_branch],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    # Configure identity (required for commit)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    # Initial commit so HEAD exists
    (repo / "README.md").write_text("# initial\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


# ---------------------------------------------------------------------------
# is_git_repo
# ---------------------------------------------------------------------------


class TestIsGitRepo:
    def test_returns_true_for_git_repo(self, tmp_path: Path) -> None:
        repo = _git_init(tmp_path / "repo")
        assert is_git_repo(repo) is True

    def test_returns_false_for_plain_directory(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert is_git_repo(plain) is False

    def test_returns_false_for_nonexistent_path(self, tmp_path: Path) -> None:
        assert is_git_repo(tmp_path / "nope") is False


# ---------------------------------------------------------------------------
# status_porcelain
# ---------------------------------------------------------------------------


class TestStatusPorcelain:
    def test_clean_repo_returns_empty_string(self, tmp_path: Path) -> None:
        repo = _git_init(tmp_path / "repo")
        assert status_porcelain(repo) == ""

    def test_modified_file_shown(self, tmp_path: Path) -> None:
        repo = _git_init(tmp_path / "repo")
        (repo / "README.md").write_text("# changed\n")
        out = status_porcelain(repo)
        assert "README.md" in out
        assert "M" in out  # modified-flag char

    def test_untracked_file_shown(self, tmp_path: Path) -> None:
        repo = _git_init(tmp_path / "repo")
        (repo / "new.txt").write_text("new\n")
        out = status_porcelain(repo)
        assert "new.txt" in out
        assert "??" in out  # untracked-flag chars

    def test_path_scope_filters_results(self, tmp_path: Path) -> None:
        repo = _git_init(tmp_path / "repo")
        (repo / "tracked").mkdir()
        (repo / "tracked" / "a.txt").write_text("a\n")
        (repo / "outside.txt").write_text("o\n")
        # Without scope, both directory entries show (default porcelain
        # collapses untracked dirs to one ?? entry per dir; we don't pass
        # -uall per the project's CLAUDE.md memory-safety guidance).
        full = status_porcelain(repo)
        assert "tracked/" in full
        assert "outside.txt" in full
        # Scoped to tracked/, only the tracked dir shows; outside.txt elided.
        scoped = status_porcelain(repo, path="tracked")
        assert "tracked/" in scoped
        assert "outside.txt" not in scoped


# ---------------------------------------------------------------------------
# rev_parse_upstream
# ---------------------------------------------------------------------------


class TestRevParseUpstream:
    def test_returns_none_when_no_upstream(self, tmp_path: Path) -> None:
        # Local-only repo (no remote configured) -> no upstream.
        repo = _git_init(tmp_path / "repo")
        assert rev_parse_upstream(repo) is None

    def test_returns_upstream_name_when_configured(self, tmp_path: Path) -> None:
        # Create a bare "remote" + a "local" clone of it; main tracks origin/main.
        remote = tmp_path / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(remote)],
            check=True,
            capture_output=True,
        )
        source_repo = _git_init(tmp_path / "source")
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)],
            cwd=source_repo,
            check=True,
        )
        subprocess.run(
            ["git", "push", "-q", "-u", "origin", "main"],
            cwd=source_repo,
            check=True,
            capture_output=True,
        )
        assert rev_parse_upstream(source_repo) == "origin/main"


# ---------------------------------------------------------------------------
# git_clone
# ---------------------------------------------------------------------------


class TestGitClone:
    def test_clones_local_path_to_dest(self, tmp_path: Path) -> None:
        upstream = _git_init(tmp_path / "upstream")
        dest = tmp_path / "clone"
        git_clone(str(upstream), dest)
        assert (dest / ".git").exists()
        assert (dest / "README.md").read_text() == "# initial\n"

    def test_clone_failure_raises_git_op_error(self, tmp_path: Path) -> None:
        # Non-existent source URL
        with pytest.raises(GitOpError, match="git clone"):
            git_clone(str(tmp_path / "nonexistent.git"), tmp_path / "dest")

    def test_clone_creates_parent_dir(self, tmp_path: Path) -> None:
        upstream = _git_init(tmp_path / "upstream")
        nested_dest = tmp_path / "a" / "b" / "c" / "clone"
        git_clone(str(upstream), nested_dest)
        assert (nested_dest / ".git").exists()


# ---------------------------------------------------------------------------
# git_fetch
# ---------------------------------------------------------------------------


class TestGitFetch:
    def test_fetch_origin_succeeds(self, tmp_path: Path) -> None:
        # Bare remote + local clone with origin configured.
        remote = tmp_path / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(remote)],
            check=True,
            capture_output=True,
        )
        source_repo = _git_init(tmp_path / "source")
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)],
            cwd=source_repo,
            check=True,
        )
        subprocess.run(
            ["git", "push", "-q", "-u", "origin", "main"],
            cwd=source_repo,
            check=True,
            capture_output=True,
        )
        # Fresh clone, then fetch should be no-op-succeed.
        clone = tmp_path / "clone"
        git_clone(str(remote), clone)
        # No exception -> success
        git_fetch(clone)

    def test_fetch_with_no_origin_raises(self, tmp_path: Path) -> None:
        repo = _git_init(tmp_path / "repo")
        with pytest.raises(GitOpError, match="git fetch"):
            git_fetch(repo)


# ---------------------------------------------------------------------------
# git_checkout
# ---------------------------------------------------------------------------


class TestGitCheckout:
    def test_checkout_branch_succeeds(self, tmp_path: Path) -> None:
        repo = _git_init(tmp_path / "repo")
        # Create a second branch with a different commit.
        subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo, check=True)
        (repo / "feature.txt").write_text("feature\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "feature"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        # Switch back to main; feature.txt should disappear from working tree.
        git_checkout(repo, "main")
        assert not (repo / "feature.txt").exists()

    def test_checkout_sha_succeeds(self, tmp_path: Path) -> None:
        repo = _git_init(tmp_path / "repo")
        # Get the SHA of the initial commit.
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        # Add another commit so HEAD moves.
        (repo / "next.txt").write_text("next\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "next"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        # Check out the initial SHA — detached HEAD.
        git_checkout(repo, sha)
        # The "next" file should be gone in the older commit.
        assert not (repo / "next.txt").exists()

    def test_checkout_nonexistent_ref_raises(self, tmp_path: Path) -> None:
        repo = _git_init(tmp_path / "repo")
        with pytest.raises(GitOpError, match="git checkout"):
            git_checkout(repo, "does-not-exist-branch")


# ---------------------------------------------------------------------------
# _sanitize_args — credential masking in error messages (setforge-ec2o.50)
# ---------------------------------------------------------------------------


class TestSanitizeArgs:
    def test_https_userinfo_masked(self) -> None:
        from setforge.git_ops import _sanitize_args

        out = _sanitize_args(["clone", "https://alice:ghp_secret@github.com/o/r"])
        assert "ghp_secret" not in out
        assert "alice" not in out
        assert "https://***@github.com/o/r" in out

    def test_ssh_remote_passes_through(self) -> None:
        from setforge.git_ops import _sanitize_args

        out = _sanitize_args(["clone", "git@github.com:owner/repo.git"])
        assert out == "clone git@github.com:owner/repo.git"

    def test_plain_args_unchanged(self) -> None:
        from setforge.git_ops import _sanitize_args

        assert _sanitize_args(["fetch", "origin"]) == "fetch origin"

    def test_clone_failure_with_token_url_masks_token(self, tmp_path: Path) -> None:
        # Clone a bogus authed URL into a dest under a nonexistent parent so
        # git fails fast; the token must not appear in the raised message.
        dest = tmp_path / "dest"
        with pytest.raises(GitOpError) as excinfo:
            git_clone("https://u:ghp_tok_abc@localhost:1/nope.git", dest)
        assert "ghp_tok_abc" not in str(excinfo.value)
