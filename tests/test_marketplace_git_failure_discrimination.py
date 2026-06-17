"""Git-failure discrimination for marketplace clone/refresh.

Classifies into offline / repository-not-found / auth, with a generic fallback.

Regression for the bug where every ``git clone`` / ``git fetch`` failure was
labelled "likely offline" — so a renamed/moved/private marketplace repo
(which fails with "Repository not found" *while online*) was misreported as a
connectivity problem. The classifier distinguishes offline (DNS/connection),
repository-not-found (online, repo gone), and auth failures, with a generic
fallback for unrecognised stderr.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from setforge.claude_marketplace_cache import (
    _classify_git_failure,
    _clone_marketplace,
    _GitFailureKind,
    _github_clone_url,
    _marketplace_clone_failure_message,
    _marketplace_refresh_failure_message,
)
from setforge.config import MarketplaceSource, MarketplaceSourceKind
from setforge.errors import MarketplaceCacheMiss

# Representative real git stderr per category (captured from git 2.x).
_OFFLINE = (
    "fatal: unable to access 'https://github.com/a/b.git/': "
    "Could not resolve host: github.com"
)
_REPO_NOT_FOUND = (
    "remote: Repository not found.\n"
    "fatal: repository 'https://github.com/a/b.git/' not found"
)
_AUTH = (
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
)
_AUTH_FAILED = (
    "remote: Support for password authentication was removed.\n"
    "fatal: Authentication failed for 'https://github.com/a/b.git/'"
)
_WEIRD = "fatal: something nobody has ever seen before"


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        # bare owner/repo shorthand -> expanded (the root-cause fix)
        (
            "anthropics/claude-plugins-official",
            "https://github.com/anthropics/claude-plugins-official",
        ),
        ("umputun/revdiff", "https://github.com/umputun/revdiff"),
        # already-qualified targets pass through unchanged
        ("https://github.com/a/b", "https://github.com/a/b"),
        ("https://example.com/a/b.git", "https://example.com/a/b.git"),
        ("git@github.com:a/b.git", "git@github.com:a/b.git"),
        ("ssh://git@github.com/a/b", "ssh://git@github.com/a/b"),
        # local filesystem paths (the e2e/local-bare-repo fixtures) pass through
        ("/tmp/mp-origin.git", "/tmp/mp-origin.git"),
        ("./rel/path", "./rel/path"),
        ("~/cache/mp", "~/cache/mp"),
    ],
)
def test_github_clone_url(repo: str, expected: str) -> None:
    assert _github_clone_url(repo) == expected


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (_OFFLINE, _GitFailureKind.OFFLINE),
        (
            "fatal: unable to access '...': Failed to connect to github.com port 443",
            _GitFailureKind.OFFLINE,
        ),
        ("Could not connect to server", _GitFailureKind.OFFLINE),
        (
            "ssh: connect to host github.com port 22: Network is unreachable",
            _GitFailureKind.OFFLINE,
        ),
        (_REPO_NOT_FOUND, _GitFailureKind.REPO_NOT_FOUND),
        ("fatal: repository 'x' not found", _GitFailureKind.REPO_NOT_FOUND),
        (_AUTH, _GitFailureKind.AUTH),
        (_AUTH_FAILED, _GitFailureKind.AUTH),
        ("remote: Permission denied", _GitFailureKind.AUTH),
        (_WEIRD, _GitFailureKind.UNKNOWN),
        ("", _GitFailureKind.UNKNOWN),
    ],
)
def test_classify_git_failure(stderr: str, expected: _GitFailureKind) -> None:
    assert _classify_git_failure(stderr) is expected


def test_clone_message_offline_does_not_misreport() -> None:
    msg = _marketplace_clone_failure_message("a/b", _OFFLINE)
    assert "network" in msg.lower() or "unreachable" in msg.lower()
    assert _OFFLINE.splitlines()[0] in msg or "Could not resolve host" in msg


def test_clone_message_repo_not_found_is_not_offline() -> None:
    msg = _marketplace_clone_failure_message("a/b", _REPO_NOT_FOUND)
    # The whole point of the fix: a not-found repo must NOT be called offline.
    assert "offline" not in msg.lower()
    assert "not found" in msg.lower()
    assert "setforge.yaml" in msg  # remediation points at the repo URL


def test_clone_message_auth() -> None:
    msg = _marketplace_clone_failure_message("a/b", _AUTH)
    assert "auth" in msg.lower() or "credential" in msg.lower()
    assert "offline" not in msg.lower()


def test_clone_message_unknown_is_generic_with_raw_stderr() -> None:
    msg = _marketplace_clone_failure_message("a/b", _WEIRD)
    assert _WEIRD in msg
    assert "offline" not in msg.lower()


def test_refresh_message_repo_not_found_is_not_offline(tmp_path: Path) -> None:
    msg = _marketplace_refresh_failure_message("a/b", _REPO_NOT_FOUND, tmp_path)
    assert "offline" not in msg.lower()
    assert "not found" in msg.lower()


def _wire_clone_stderr(monkeypatch: pytest.MonkeyPatch, stderr: str) -> None:
    def _raise(*_a: object, **_k: object) -> object:
        raise subprocess.CalledProcessError(128, ["git", "clone"], stderr=stderr)

    monkeypatch.setattr(
        "setforge.claude_marketplace_cache.shutil.which", lambda _n: "/usr/bin/git"
    )
    monkeypatch.setattr("setforge.claude_marketplace_cache.subprocess.run", _raise)


def test_clone_marketplace_repo_not_found_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wire_clone_stderr(monkeypatch, _REPO_NOT_FOUND)
    source = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="a/b")
    with pytest.raises(MarketplaceCacheMiss) as exc:
        _clone_marketplace(source, tmp_path / "dest")
    assert "offline" not in str(exc.value).lower()
    assert "not found" in str(exc.value).lower()


def test_clone_marketplace_offline_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wire_clone_stderr(monkeypatch, _OFFLINE)
    source = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="a/b")
    with pytest.raises(MarketplaceCacheMiss) as exc:
        _clone_marketplace(source, tmp_path / "dest")
    assert (
        "network" in str(exc.value).lower() or "unreachable" in str(exc.value).lower()
    )
