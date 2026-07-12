"""The go :class:`Resolver` — resolves in a scratch module dir (never touches
a host go.mod/go.sum); argv guards arg-injection with a ``--`` terminator."""

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
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., _CompletedRun]


def _default_runner(argv: list[str], *, cwd: str, timeout: float) -> _CompletedRun:
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
