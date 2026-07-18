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
    _parse_ls_remote_sha,
    marketplace_git_url,
)
from setforge.provision.resolve.protocol import IntegrityKind, PackageType

_SHA = "4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f"

_LS_REMOTE_HEAD = f"{_SHA}\tHEAD\n"


def _runner_ok(
    stdout: str = _LS_REMOTE_HEAD,
    captured: list[list[str]] | None = None,
    captured_env: list[dict[str, str] | None] | None = None,
):
    def _run(
        argv: list[str],
        *,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if captured is not None:
            captured.append(argv)
        if captured_env is not None:
            captured_env.append(env)
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


def test_ls_remote_restricts_git_transports() -> None:
    captured_env: list[dict[str, str] | None] = []
    resolver = PluginResolver(runner=_runner_ok(captured_env=captured_env))
    resolver.resolve(_item())
    env = captured_env[0]
    assert env is not None
    assert env.get("GIT_ALLOW_PROTOCOL") == "https:ssh:file"
    # A copy of the real environment is passed, not a bare dict.
    assert "PATH" in env


def test_ls_remote_env_refuses_ext_transport_helper() -> None:
    # Fake a git that honors GIT_ALLOW_PROTOCOL: an ext:: URL is refused.
    def _run(
        argv: list[str],
        *,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        allowed = (env or {}).get("GIT_ALLOW_PROTOCOL", "")
        url = argv[argv.index("--") + 1]
        scheme = url.split("::", 1)[0] if "::" in url else url.split("://", 1)[0]
        if scheme not in allowed.split(":"):
            return subprocess.CompletedProcess(
                argv, 128, stdout="", stderr=f"transport '{scheme}' not allowed"
            )
        return subprocess.CompletedProcess(argv, 0, stdout=_LS_REMOTE_HEAD, stderr="")

    resolver = PluginResolver(runner=_run)
    with pytest.raises(ResolveError, match="not allowed"):
        resolver.resolve(_item(git_url="ext::sh -c 'touch /tmp/pwned'"))


def test_resolve_concrete_sha_not_ref_name() -> None:
    resolver = PluginResolver(runner=_runner_ok())
    pin = resolver.resolve(_item(ref="HEAD"))
    assert pin.version != "HEAD"
    assert len(pin.version) == 40
    assert all(c in "0123456789abcdef" for c in pin.version)


def test_resolve_git_failure_raises_clean_error() -> None:
    def _run(
        argv: list[str],
        *,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 128, stdout="", stderr="repo not found"
        )

    with pytest.raises(ResolveError, match="repo not found"):
        PluginResolver(runner=_run).resolve(_item())


def test_resolve_timeout_surfaces_as_resolve_error() -> None:
    def _run(
        argv: list[str],
        *,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
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


_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_parse_accepts_sha1_object_id() -> None:
    assert _parse_ls_remote_sha(f"{_SHA}\tHEAD\n", "u", "HEAD") == _SHA


def test_parse_accepts_sha256_object_id() -> None:
    assert _parse_ls_remote_sha(f"{_SHA256}\tHEAD\n", "u", "HEAD") == _SHA256


@pytest.mark.parametrize(
    "bad",
    [
        "a" * 39,  # 39 hex — too short for SHA-1
        "a" * 41,  # 41 hex — between SHA-1 and SHA-256
        "a" * 63,  # 63 hex — one short of SHA-256
        "a" * 65,  # 65 hex — one over SHA-256
        "A" * 64,  # 64 hex but uppercase
        "z" * 40,  # right length, non-hex
    ],
)
def test_parse_rejects_non_object_ids(bad: str) -> None:
    with pytest.raises(ResolveError, match="non-SHA"):
        _parse_ls_remote_sha(f"{bad}\tHEAD\n", "u", "HEAD")


def test_resolve_registered_and_retrievable() -> None:
    from setforge.provision.resolve import plugin  # noqa: F401  (import registers)
    from setforge.provision.resolve.registry import get_resolver

    assert isinstance(get_resolver(PackageType.PLUGIN), PluginResolver)
