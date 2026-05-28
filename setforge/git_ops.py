"""Git operations for the source-management subsystem.

Thin subprocess wrappers around the ``git`` binary. Setforge delegates
auth entirely to the user's git/SSH/credential-helper configuration —
the engine never reads ``~/.ssh/``, never prompts for credentials, never
embeds tokens. If a clone or fetch needs auth, the user's git config
handles it (or the operation fails with the standard git error message).

All operations:
- Use ``shutil.which("git")`` to locate the binary at module-import time.
- Pass a list of args (never ``shell=True``).
- Call ``subprocess.run(..., check=True, text=True, timeout=...)``.
- Use ``import subprocess`` + ``subprocess.run(...)`` (not
  ``from subprocess import run``) so test monkeypatch via the
  ``setforge.git_ops.subprocess.run`` attribute path works.
- Wrap ``CalledProcessError`` into :class:`GitOpError` with the git
  stderr surfaced in the message.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

from setforge.errors import GitOpError

_GIT_TIMEOUT_SECONDS: Final[int] = 300

_URL_USERINFO_RE: Final[re.Pattern[str]] = re.compile(r"(?P<scheme>\w+://)[^/@\s]+@")


def _sanitize_args(args: list[str]) -> str:
    """Join ``args`` for display, masking credentials in URL-shaped tokens.

    Any arg matching ``scheme://userinfo@host`` has its userinfo replaced
    with ``***`` so an embedded ``user:token@`` never reaches an error
    message, log line, or error-tracking surface. SSH-style remotes
    (``git@host:path`` — no ``://``) carry a username, not a secret, and
    pass through untouched.
    """
    return " ".join(_URL_USERINFO_RE.sub(r"\g<scheme>***@", arg) for arg in args)


def _git_bin() -> str:
    """Locate ``git`` on PATH; raise :class:`GitOpError` if absent."""
    found = shutil.which("git")
    if found is None:
        raise GitOpError("git binary not found on PATH. Install git or adjust PATH.")
    return found


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``git`` with the given args, surfacing failures as GitOpError.

    Wraps ``subprocess.run`` with the project's standard kwargs
    (``text=True``, ``check=True``, ``timeout=_GIT_TIMEOUT_SECONDS``,
    ``capture_output=True``). ``cwd`` is forwarded as the working
    directory; pass the source repo's path (equivalent to ``git -C``).
    """
    cmd = [_git_bin(), *args]
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            check=check,
            text=True,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        # check=True path; surface git's stderr in the error message.
        stderr = (exc.stderr or "").strip()
        raise GitOpError(
            f"git {_sanitize_args(args)} failed (exit {exc.returncode}): {stderr}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitOpError(
            f"git {_sanitize_args(args)} timed out after {_GIT_TIMEOUT_SECONDS}s"
        ) from exc


def git_clone(url: str, dest: Path) -> None:
    """Clone ``url`` into ``dest``.

    ``dest`` must not already exist (git's standard behavior). The
    parent dir is created if missing. Auth delegates to the user's
    git config; if credentials fail, git's error surfaces unmodified.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run_git(["clone", url, str(dest)])


def git_fetch(repo: Path, remote: str = "origin") -> None:
    """Fetch from ``remote`` (default ``origin``) in ``repo``."""
    _run_git(["fetch", remote], cwd=repo)


def git_checkout(repo: Path, ref: str) -> None:
    """Check out ``ref`` (branch or SHA) in ``repo``.

    For branch refs, this moves HEAD to the branch's current commit.
    For SHA refs, this puts HEAD in detached mode. Fails if the working
    tree has uncommitted changes blocking the checkout — call
    :func:`status_porcelain` first and abort with a clean error if
    the working tree is dirty.
    """
    _run_git(["checkout", ref], cwd=repo)


def status_porcelain(repo: Path, path: str | None = None) -> str:
    """Return ``git status --porcelain`` output for ``repo``.

    Empty string means clean. When ``path`` is set, scopes the status
    to that pathspec (e.g. ``"tracked/"``) — useful for the dirty-gate
    pre-write check that only cares about the engine's write surface.
    """
    args = ["status", "--porcelain"]
    if path is not None:
        args.extend(["--", path])
    result = _run_git(args, cwd=repo)
    return result.stdout


def rev_parse_upstream(repo: Path) -> str | None:
    """Return the upstream-tracking ref (e.g. ``origin/main``) or None.

    Wraps ``git rev-parse --abbrev-ref @{upstream}``. Returns ``None``
    when there's no upstream configured (the rev-parse exits non-zero
    with "no upstream configured" — that's expected, not an error).
    """
    result = _run_git(
        ["rev-parse", "--abbrev-ref", "@{upstream}"],
        cwd=repo,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def is_git_repo(path: Path) -> bool:
    """Return True if ``path/.git`` exists (file or directory).

    Cheap structural check — doesn't shell out. Used by the dirty-gate
    to skip the porcelain check when a PathSource is not under git.
    """
    return (path / ".git").exists()


__all__ = [
    "git_checkout",
    "git_clone",
    "git_fetch",
    "is_git_repo",
    "rev_parse_upstream",
    "status_porcelain",
]
