"""Tests for the go resolver (spec §B3).

Resolves via ``go mod download -json <module>@<version>`` run in a SCRATCH
module dir (GOFLAGS + GONOSUMCHECK-free env) so it can NEVER mutate a host
go.mod/go.sum. Parses ``.Version`` (concrete) + ``.Sum`` (the ``h1:...`` module
hash) into a ``sum``-kind pin.

The subprocess boundary is INJECTABLE (a ``runner`` callable receiving the
literal argv) so these unit tests never spawn ``go`` — the real invocation is
Task-6's docker e2e. The tests assert the argv carries a ``--``
options-terminator before the attacker-influenced ``module@version`` positional.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from setforge.config import GoPackage
from setforge.errors import ResolveError
from setforge.provision.resolve.go import GoResolver, _CompletedRun
from setforge.provision.resolve.protocol import IntegrityKind, PackageType

# A realistic `go mod download -json` payload (real output also carries Path/
# Info/GoMod/Zip/Dir file paths, which the resolver must ignore).
_GOLANG_X_TEXT = {
    "Path": "golang.org/x/text",
    "Version": "v0.14.0",
    "Sum": "h1:ScX5w1eTa3QqT8oi6+ziP7dTV1S2+ALU0bI+0zXKWiQ=",
    "GoMod": "/tmp/x/go.mod",
}


def _runner_ok(captured: list[list[str]] | None = None):
    def _run(argv: list[str], *, cwd: str, timeout: float) -> _CompletedRun:
        if captured is not None:
            captured.append(argv)
        return _CompletedRun(returncode=0, stdout=json.dumps(_GOLANG_X_TEXT), stderr="")

    return _run


def test_resolve_parses_version_and_sum() -> None:
    resolver = GoResolver(runner=_runner_ok())
    pin = resolver.resolve(GoPackage(module="golang.org/x/text"))
    assert pin.type is PackageType.GO
    assert pin.key == "golang.org/x/text"
    assert pin.version == "v0.14.0"
    # The sum kind stores the h1: value verbatim (no sha256: reformatting).
    assert pin.integrity == "h1:ScX5w1eTa3QqT8oi6+ziP7dTV1S2+ALU0bI+0zXKWiQ="
    assert pin.integrity_kind is IntegrityKind.SUM


def test_argv_has_options_terminator_before_positional() -> None:
    captured: list[list[str]] = []
    resolver = GoResolver(runner=_runner_ok(captured))
    resolver.resolve(GoPackage(module="golang.org/x/text", version="v0.14.0"))
    argv = captured[0]
    assert "--" in argv
    dd = argv.index("--")
    # The module@version positional sits AFTER the terminator (arg-injection guard).
    assert argv[dd + 1] == "golang.org/x/text@v0.14.0"
    # Nothing dashy is passed as a positional after the terminator.
    assert all(not a.startswith("-") for a in argv[dd + 1 :])


def test_latest_defaults_when_no_version() -> None:
    captured: list[list[str]] = []
    resolver = GoResolver(runner=_runner_ok(captured))
    resolver.resolve(GoPackage(module="golang.org/x/text"))
    argv = captured[0]
    assert argv[argv.index("--") + 1] == "golang.org/x/text@latest"


def test_nonzero_exit_raises_clean_error() -> None:
    def _run(argv: list[str], *, cwd: str, timeout: float) -> _CompletedRun:
        return _CompletedRun(returncode=1, stdout="", stderr="module not found")

    with pytest.raises(ResolveError, match="module not found"):
        GoResolver(runner=_run).resolve(GoPackage(module="does.not/exist"))


def test_timeout_surfaces_as_resolve_error() -> None:
    def _run(argv: list[str], *, cwd: str, timeout: float) -> _CompletedRun:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    with pytest.raises(ResolveError, match="timed out"):
        GoResolver(runner=_run).resolve(GoPackage(module="slow.example/mod"))


def test_missing_sum_field_raises() -> None:
    def _run(argv: list[str], *, cwd: str, timeout: float) -> _CompletedRun:
        return _CompletedRun(
            returncode=0, stdout=json.dumps({"Version": "v1.0.0"}), stderr=""
        )

    with pytest.raises(ResolveError, match="Sum"):
        GoResolver(runner=_run).resolve(GoPackage(module="x.example/mod"))


def test_resolve_registered_and_retrievable() -> None:
    from setforge.provision.resolve import go  # noqa: F401  (import registers)
    from setforge.provision.resolve.registry import get_resolver

    assert isinstance(get_resolver(PackageType.GO), GoResolver)
