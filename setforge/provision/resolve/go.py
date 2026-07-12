"""The go :class:`Resolver` — resolves via ``go mod download -json``.

Runs ``go mod download -json -- <module>@<version>`` in a fresh SCRATCH module
dir (a tempdir seeded with a throwaway ``go.mod`` and ``GOFLAGS=-mod=mod``) so
it can NEVER read or mutate a host ``go.mod``/``go.sum``. Parses ``.Version``
(the concrete resolved version) + ``.Sum`` (the ``h1:...`` module-sumdb hash)
into a ``sum``-kind pin — recorded for drift-detection; the actual install stays
``go install`` (PRAGMATIC, spec §B3).

The subprocess boundary is injected (the ``runner`` callable) so unit tests mock
it. The default runner uses literal-argv ``subprocess.run`` (no ``shell=True``)
with a ``--`` options-terminator before the attacker-influenced ``module@ver``
positional (arg-injection guard, spec §C) and an explicit timeout.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from setforge.binaries import resolve_binary
from setforge.config import GoPackage
from setforge.errors import ResolveError
from setforge.provision.resolve.protocol import (
    IntegrityKind,
    PackageType,
    ResolvedPin,
)
from setforge.provision.resolve.registry import register

__all__ = ["GoResolver"]

_GO_BIN_NAME = "go"
_DOWNLOAD_TIMEOUT_S = 120
_LATEST = "latest"


@dataclass(slots=True, frozen=True)
class _CompletedRun:
    """The minimal ``subprocess.run`` result the resolver reads.

    A tiny record (not the full ``CompletedProcess``) so the injected runner in
    tests need not construct a real one.
    """

    returncode: int
    stdout: str
    stderr: str


# A runner takes the literal argv + a working dir + a timeout and returns the
# completed run (or raises ``subprocess.TimeoutExpired``).
Runner = Callable[..., _CompletedRun]


def _default_runner(argv: list[str], *, cwd: str, timeout: float) -> _CompletedRun:
    """Run ``argv`` via literal-argv ``subprocess.run`` in ``cwd``.

    ``GOFLAGS=-mod=mod`` keeps resolution self-contained to the scratch module.
    Never ``check=True`` — a non-zero exit is surfaced by the caller as a clean
    :class:`ResolveError` with the captured stderr, not a raised
    ``CalledProcessError``.
    """
    env = {**os.environ, "GOFLAGS": "-mod=mod"}
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        env=env,
    )
    return _CompletedRun(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


@register(PackageType.GO)
class GoResolver:
    """Resolve a Go module to its concrete ``.Version`` + ``h1:`` sum."""

    type: ClassVar[PackageType] = PackageType.GO

    def __init__(self, *, runner: Runner | None = None) -> None:
        self._runner = runner if runner is not None else _default_runner

    def resolve(self, item: object) -> ResolvedPin:
        if not isinstance(item, GoPackage):  # pragma: no cover - typing guard
            raise ResolveError(
                f"go resolver received {type(item).__name__}, not GoPackage"
            )
        go = resolve_binary(_GO_BIN_NAME)
        if go is None:
            raise ResolveError(
                "go not found on PATH; install the Go toolchain from "
                "https://go.dev/dl/ to resolve go modules"
            )
        version = item.version or _LATEST
        spec = f"{item.module}@{version}"
        # `--` terminates options so the attacker-influenced spec can never be
        # read as a flag (arg-injection guard, spec §C).
        argv = [str(go), "mod", "download", "-json", "--", spec]
        out = self._run_in_scratch(argv, spec)
        parsed = _parse_download_json(out, spec)
        return ResolvedPin(
            type=PackageType.GO,
            key=item.module,
            version=parsed["Version"],
            integrity=parsed["Sum"],
            integrity_kind=IntegrityKind.SUM,
        )

    def _run_in_scratch(self, argv: list[str], spec: str) -> str:
        """Run ``argv`` in a throwaway module dir; return stdout or raise.

        The scratch dir carries a minimal ``go.mod`` so ``go mod download`` has a
        module context WITHOUT touching any host ``go.mod``/``go.sum``. A missing
        binary, non-zero exit, or timeout all surface as :class:`ResolveError`.
        """
        with tempfile.TemporaryDirectory(prefix="setforge-goresolve-") as scratch:
            (Path(scratch) / "go.mod").write_text(
                "module setforge.local/resolve\n\ngo 1.21\n", encoding="utf-8"
            )
            try:
                completed = self._runner(argv, cwd=scratch, timeout=_DOWNLOAD_TIMEOUT_S)
            except subprocess.TimeoutExpired as exc:
                raise ResolveError(
                    f"`go mod download` for {spec!r} timed out after "
                    f"{_DOWNLOAD_TIMEOUT_S}s"
                ) from exc
            except OSError as exc:
                raise ResolveError(
                    f"`go mod download` for {spec!r} failed to launch: {exc}"
                ) from exc
        if completed.returncode != 0:
            detail = (
                completed.stderr.strip() or completed.stdout.strip() or "(no output)"
            )
            raise ResolveError(
                f"`go mod download` for {spec!r} exited "
                f"{completed.returncode}: {detail}"
            )
        return completed.stdout


def _parse_download_json(stdout: str, spec: str) -> dict[str, str]:
    """Parse the ``go mod download -json`` payload; require ``Version``+``Sum``.

    ``go mod download`` may emit an ``Error`` field on a resolvable-but-failed
    module; a missing ``Version``/``Sum`` (str) is treated as a resolve failure.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ResolveError(
            f"`go mod download` for {spec!r} returned non-JSON output: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ResolveError(
            f"`go mod download` for {spec!r} returned a non-object payload"
        )
    error = payload.get("Error")
    if isinstance(error, str) and error:
        raise ResolveError(f"go could not resolve {spec!r}: {error}")
    result: dict[str, str] = {}
    for field in ("Version", "Sum"):
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise ResolveError(
                f"`go mod download` for {spec!r} is missing the {field!r} field"
            )
        result[field] = value
    return result
