"""Test doubles that were copied across several test modules."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

# Taken at import, before any fixture replaces ``subprocess.run``.
REAL_SUBPROCESS_RUN = subprocess.run


class FakeCode:
    """In-memory ``code`` CLI: tracks installed extension ids, records calls.

    ``installed`` is a plain list so a test can seed or rewrite it (including
    blank entries) between calls. Argv whose binary is not ``code`` goes to
    ``delegate`` when it is ``claude`` and to ``real_run`` otherwise; with
    neither set it raises.
    """

    def __init__(self, installed: Iterable[str] = ()) -> None:
        self.installed: list[str] = list(installed)
        self.calls: list[list[str]] = []
        self.delegate: Callable[..., Any] | None = None
        self.real_run: Callable[..., Any] | None = None

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        binary = Path(args[0]).name if args else ""
        if binary != "code":
            forward = self.delegate if binary == "claude" else None
            forward = forward or self.real_run
            if forward is None:
                raise AssertionError(f"unexpected non-code invocation: {args!r}")
            return forward(args, **kwargs)

        self.calls.append(list(args))
        flag = args[1]
        if flag == "--list-extensions":
            out = "\n".join(self.installed) + ("\n" if self.installed else "")
            return subprocess.CompletedProcess(args, 0, out, "")
        if flag == "--install-extension":
            if args[2] not in self.installed:
                self.installed.append(args[2])
            return subprocess.CompletedProcess(args, 0, "", "")
        if flag == "--uninstall-extension":
            if args[2] in self.installed:
                self.installed.remove(args[2])
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected code invocation: {args!r}")

    @property
    def install_args(self) -> list[str]:
        return [c[2] for c in self.calls if c[1] == "--install-extension"]

    @property
    def uninstall_args(self) -> list[str]:
        return [c[2] for c in self.calls if c[1] == "--uninstall-extension"]

    def installed_set(self) -> set[str]:
        return set(self.installed)


class FakeCtx:
    """Stand-in for ``typer.Context`` carrying only ``params`` and ``info_name``."""

    def __init__(
        self,
        *,
        path: str | None = None,
        local: bool = True,
        info_name: str | None = None,
    ) -> None:
        self.params: dict[str, Any] = {
            "path": path,
            "local": local,
            "tracked": not local,
        }
        self.info_name = info_name


class FakeResponse:
    """Context-managed stand-in for the object ``urllib.request.urlopen`` returns.

    ``read(n)`` returns the first ``n`` bytes of ``body`` (all of it without
    ``n``) and records ``n`` in ``read_calls`` when given a list.
    """

    def __init__(
        self,
        body: bytes = b"{}",
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        final_url: str = "",
        read_calls: list[int] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = body
        self._final_url = final_url
        self._read_calls = read_calls

    def read(self, n: int = -1) -> bytes:
        if self._read_calls is not None:
            self._read_calls.append(n)
        return self._body if n < 0 else self._body[:n]

    def geturl(self) -> str:
        return self._final_url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None
