"""The plugin :class:`Resolver`: pins the concrete commit SHA via ``git
ls-remote``, never a moving ref, WITHOUT cloning."""

from __future__ import annotations

import os
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
# Refuse transport-helper prefixes (ext::/fd::/transport::...) that run a shell
# command; `--` stops flag parsing but git still honors them in the URL. Only the
# real network/filesystem schemes are allowed for the ls-remote subprocess.
_ALLOWED_GIT_PROTOCOLS = "https:ssh:file"
# Exactly 40 (SHA-1) or 64 (SHA-256) lowercase hex chars — git's two object-id
# formats. Nothing in between is a valid object id.
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class PluginResolveItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    git_url: str
    ref: str = _DEFAULT_REF


def marketplace_git_url(source: MarketplaceSource) -> str:
    if source.source is MarketplaceSourceKind.PATH:
        return str(source.path)
    if not source.repo:
        raise ResolveError(
            "GITHUB marketplace source missing 'repo' field; cannot resolve "
            "plugin marketplace git URL"
        )
    return _github_clone_url(source.repo)


def _default_runner(
    argv: list[str], *, timeout: float, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


@register(PackageType.PLUGIN)
class PluginResolver:
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
        # Copy the real environment so PATH/SSH config survive, then pin the
        # transport allowlist so a malicious git_url can't invoke a shell helper.
        env = dict(os.environ)
        env["GIT_ALLOW_PROTOCOL"] = _ALLOWED_GIT_PROTOCOLS
        try:
            completed = self._runner(argv, timeout=_LS_REMOTE_TIMEOUT_S, env=env)
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
    # SHA-1/SHA-256 hex validated so a moving ref or malformed line fails closed.
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
