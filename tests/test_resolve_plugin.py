"""Tests for the plugin resolver (runner injected; never spawns ``git``)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from setforge.config import MarketplaceSource, MarketplaceSourceKind
from setforge.errors import ResolveError
from setforge.provision.resolve.plugin import (
    PluginResolveItem,
    PluginResolver,
    marketplace_git_url,
)
from setforge.provision.resolve.protocol import IntegrityKind, PackageType

_SHA = "4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f"

_LS_REMOTE_HEAD = f"{_SHA}\tHEAD\n"


def _runner_ok(stdout: str = _LS_REMOTE_HEAD, captured: list[list[str]] | None = None):
    def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        if captured is not None:
            captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    return _run


def _item(git_url: str = "https://github.com/owner/mp", ref: str = "HEAD"):
    return PluginResolveItem(key="revdiff@revdiff", git_url=git_url, ref=ref)


def test_resolve_parses_sha_from_ls_remote() -> None:
    resolver = PluginResolver(runner=_runner_ok())
    pin = resolver.resolve(_item())
    assert pin.type is PackageType.PLUGIN
    assert pin.key == "revdiff@revdiff"
    assert pin.version == _SHA
    assert pin.integrity == _SHA
    assert pin.integrity_kind is IntegrityKind.SHA


def test_argv_has_options_terminator_before_url() -> None:
    captured: list[list[str]] = []
    resolver = PluginResolver(runner=_runner_ok(captured=captured))
    resolver.resolve(_item(git_url="https://github.com/owner/mp", ref="HEAD"))
    argv = captured[0]
    assert "ls-remote" in argv
    assert "--" in argv
    dd = argv.index("--")
    assert argv[dd + 1 :] == ["https://github.com/owner/mp", "HEAD"]  # after terminator


def test_resolve_concrete_sha_not_ref_name() -> None:
    resolver = PluginResolver(runner=_runner_ok())
    pin = resolver.resolve(_item(ref="HEAD"))
    assert pin.version != "HEAD"
    assert len(pin.version) == 40
    assert all(c in "0123456789abcdef" for c in pin.version)


def test_resolve_git_failure_raises_clean_error() -> None:
    def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 128, stdout="", stderr="repo not found"
        )

    with pytest.raises(ResolveError, match="repo not found"):
        PluginResolver(runner=_run).resolve(_item())


def test_resolve_timeout_surfaces_as_resolve_error() -> None:
    def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    with pytest.raises(ResolveError, match="timed out"):
        PluginResolver(runner=_run).resolve(_item())


def test_resolve_empty_output_raises() -> None:
    with pytest.raises(ResolveError, match="no matching ref"):
        PluginResolver(runner=_runner_ok(stdout="")).resolve(_item())


def test_resolve_non_sha_output_raises() -> None:
    with pytest.raises(ResolveError):
        PluginResolver(runner=_runner_ok(stdout="not-a-sha\tHEAD\n")).resolve(_item())


def test_marketplace_git_url_expands_github_shorthand() -> None:
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="owner/mp")
    assert marketplace_git_url(src) == "https://github.com/owner/mp"


def test_marketplace_git_url_passes_through_path() -> None:
    src = MarketplaceSource(source=MarketplaceSourceKind.PATH, path=Path("/some/clone"))
    assert marketplace_git_url(src) == "/some/clone"


def test_marketplace_git_url_passes_through_full_url() -> None:
    src = MarketplaceSource(
        source=MarketplaceSourceKind.GITHUB, repo="https://example.com/x.git"
    )
    assert marketplace_git_url(src) == "https://example.com/x.git"


def test_resolve_registered_and_retrievable() -> None:
    from setforge.provision.resolve import plugin  # noqa: F401  (import registers)
    from setforge.provision.resolve.registry import get_resolver

    assert isinstance(get_resolver(PackageType.PLUGIN), PluginResolver)
