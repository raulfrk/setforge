"""The plugin :class:`Resolver` — pins the marketplace repo's commit SHA.

A Claude plugin key is ``name@marketplace``; the marketplace is a git repo.
The strong pin (spec §B3) is that repo's concrete commit SHA, resolved via
``git ls-remote <marketplace-git-url> <ref>`` — WITHOUT cloning or checking out
(that host mutation is the install path's job, Task 6). The pin carries
``version = sha`` and ``sha`` integrity kind; a moving ref (``HEAD``, a branch)
is resolved to the 40-hex commit it points at and NEVER stored verbatim.

The marketplace git URL is NOT derivable from the plugin key alone — the key's
``@marketplace`` segment is a YAML registry name, and the git URL lives in the
config's ``marketplaces:`` map. :func:`marketplace_git_url` reuses
``claude_marketplace_cache``'s shorthand expansion so the caller (Task 6's lock
verb) derives the URL from a :class:`~setforge.config.MarketplaceSource` in one
place; the resolver itself takes an already-derived :class:`PluginResolveItem`.

The subprocess boundary is injected (the ``runner`` callable) so unit tests
never spawn git; the default uses literal-argv ``subprocess.run`` (no
``shell=True``) with a ``--`` options-terminator before the URL/ref positionals
(arg-injection guard, spec §C) and an explicit timeout.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from setforge.claude_marketplace_cache import _github_clone_url
from setforge.config import MarketplaceSource, MarketplaceSourceKind
from setforge.errors import ResolveError
from setforge.provision.resolve.protocol import (
    IntegrityKind,
    PackageType,
    ResolvedPin,
)
from setforge.provision.resolve.registry import register

__all__ = ["PluginResolveItem", "PluginResolver", "marketplace_git_url"]

_GIT_BIN_NAME = "git"
_LS_REMOTE_TIMEOUT_S = 30.0
_DEFAULT_REF = "HEAD"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# A runner takes the literal argv + a timeout and returns the completed run
# (or raises ``subprocess.TimeoutExpired``).
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class PluginResolveItem(BaseModel):
    """The already-derived input a :class:`PluginResolver` resolves.

    Distinct from :class:`~setforge.config.ClaudePluginRef` (which carries only
    the marketplace YAML name): ``git_url`` is the concrete marketplace repo URL
    the caller derived via :func:`marketplace_git_url`, and ``ref`` is the ref
    to pin (``HEAD`` by default). ``key`` is the ``name@marketplace`` lock key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    git_url: str
    ref: str = _DEFAULT_REF


def marketplace_git_url(source: MarketplaceSource) -> str:
    """Return the clonable git URL for a marketplace :class:`MarketplaceSource`.

    Reuses ``claude_marketplace_cache``'s shorthand expansion so a bare GITHUB
    ``owner/repo`` becomes ``https://github.com/owner/repo`` and full URLs / SSH
    remotes pass through unchanged; a PATH source returns its filesystem path
    (``git ls-remote`` accepts a local repo path directly). Raises
    :class:`ResolveError` for a GITHUB source missing its ``repo`` field.
    """
    if source.source is MarketplaceSourceKind.PATH:
        return str(source.path)
    if not source.repo:
        raise ResolveError(
            "GITHUB marketplace source missing 'repo' field; cannot resolve "
            "plugin marketplace git URL"
        )
    return _github_clone_url(source.repo)


def _default_runner(
    argv: list[str], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` via literal-argv ``subprocess.run`` (no ``shell=True``).

    Never ``check=True`` — a non-zero exit is surfaced by the caller as a clean
    :class:`ResolveError` with the captured stderr, not a raised
    ``CalledProcessError``.
    """
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@register(PackageType.PLUGIN)
class PluginResolver:
    """Resolve a plugin to its marketplace repo's concrete commit SHA."""

    type: ClassVar[PackageType] = PackageType.PLUGIN

    def __init__(self, *, runner: Runner | None = None) -> None:
        self._runner = runner if runner is not None else _default_runner

    def resolve(self, item: object) -> ResolvedPin:
        if not isinstance(item, PluginResolveItem):  # pragma: no cover - guard
            raise ResolveError(
                f"plugin resolver received {type(item).__name__}, not PluginResolveItem"
            )
        git = shutil.which(_GIT_BIN_NAME)
        if git is None:
            raise ResolveError(
                "git not found on PATH; install git to resolve plugin "
                "marketplace commit pins"
            )
        # `--` terminates options so the URL/ref positionals can never be read
        # as flags (arg-injection guard, spec §C).
        argv = [git, "ls-remote", "--", item.git_url, item.ref]
        sha = self._ls_remote_sha(argv, item.git_url, item.ref)
        return ResolvedPin(
            type=PackageType.PLUGIN,
            key=item.key,
            version=sha,
            integrity=sha,
            integrity_kind=IntegrityKind.SHA,
        )

    def _ls_remote_sha(self, argv: list[str], git_url: str, ref: str) -> str:
        """Run ``git ls-remote`` and return the 40-hex SHA for ``ref``, or raise.

        A missing binary, non-zero exit, or timeout all surface as
        :class:`ResolveError`. The SHA is the first whitespace-delimited token
        of the first output line, validated against a 40-hex pattern so a moving
        ref name or malformed line fails closed rather than pinning garbage.
        """
        try:
            completed = self._runner(argv, timeout=_LS_REMOTE_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise ResolveError(
                f"`git ls-remote {git_url} {ref}` timed out after "
                f"{_LS_REMOTE_TIMEOUT_S}s"
            ) from exc
        except OSError as exc:
            raise ResolveError(
                f"`git ls-remote {git_url} {ref}` failed to launch: {exc}"
            ) from exc
        if completed.returncode != 0:
            detail = (
                completed.stderr.strip() or completed.stdout.strip() or "(no output)"
            )
            raise ResolveError(
                f"`git ls-remote {git_url} {ref}` exited "
                f"{completed.returncode}: {detail}"
            )
        return _parse_ls_remote_sha(completed.stdout, git_url, ref)


def _parse_ls_remote_sha(stdout: str, git_url: str, ref: str) -> str:
    """Return the 40-hex SHA from the first non-empty ``git ls-remote`` line.

    Each line is ``<40-hex><whitespace><refname>``. Empty output means the ref
    did not resolve; a leading token that is not a 40-hex SHA is a malformed
    line. Both fail closed with :class:`ResolveError`.
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        sha = stripped.split()[0]
        if not _SHA_RE.match(sha):
            raise ResolveError(
                f"`git ls-remote {git_url} {ref}` returned a non-SHA line: {stripped!r}"
            )
        return sha
    raise ResolveError(f"`git ls-remote {git_url} {ref}` returned no matching ref")
