"""Test doubles that were copied across several test modules."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

# Taken at import, before any fixture replaces ``subprocess.run``.
REAL_SUBPROCESS_RUN = subprocess.run


class FakeCode:
    """In-memory ``code`` CLI: tracks installed extension ids, records calls.

    ``installed`` is a plain list so a test can seed or rewrite it (including
    blank entries) between calls. Fails closed: argv whose binary is not
    ``code`` raises unless ``delegate`` is set and the binary is ``claude`` (a
    co-resident ``fake_claude``), or the binary is named in ``real_binaries``,
    which a test opts into to reach the real ``real_run``.
    """

    def __init__(self, installed: Iterable[str] = ()) -> None:
        self.installed: list[str] = list(installed)
        self.calls: list[list[str]] = []
        self.delegate: Callable[..., Any] | None = None
        self.real_run: Callable[..., Any] = REAL_SUBPROCESS_RUN
        self.real_binaries: frozenset[str] = frozenset()

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        binary = Path(args[0]).name if args else ""
        if binary != "code":
            if binary == "claude" and self.delegate is not None:
                return self.delegate(args, **kwargs)
            if binary in self.real_binaries:
                return self.real_run(args, **kwargs)
            raise AssertionError(f"unexpected non-code invocation: {args!r}")

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
    ``n``) and records ``n`` in ``read_calls`` when given a list. With
    ``chunks``, each ``read`` instead returns the next chunk whatever ``n`` is,
    then ``b""``, to model a streamed download.
    """

    def __init__(
        self,
        body: bytes = b"{}",
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        final_url: str = "",
        read_calls: list[int] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = body
        self._chunks = None if chunks is None else list(chunks)
        self._final_url = final_url
        self._read_calls = read_calls

    def read(self, n: int = -1) -> bytes:
        if self._read_calls is not None:
            self._read_calls.append(n)
        if self._chunks is not None:
            return self._chunks.pop(0) if self._chunks else b""
        return self._body if n < 0 else self._body[:n]

    def geturl(self) -> str:
        return self._final_url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeMcpCli:
    """Scripted ``claude mcp`` driver recording argv lists.

    ``registry`` maps name -> (command_tokens, scope) and models the live
    server state. ``add``/``remove`` mutate it; ``get --json`` reads it.
    ``get_payloads`` overrides the generated JSON response for a server.
    ``add_errors`` / ``remove_errors`` map a name -> stderr string to raise
    a :class:`subprocess.CalledProcessError` for that op.
    """

    def __init__(
        self,
        *,
        registry: dict[str, tuple[list[str], str]] | None = None,
        get_payloads: dict[str, object] | None = None,
        add_errors: dict[str, str] | None = None,
        remove_errors: dict[str, str] | None = None,
    ) -> None:
        self.real_run = subprocess.run
        self.registry = registry or {}
        self.get_payloads = get_payloads or {}
        self.add_errors = add_errors or {}
        self.remove_errors = remove_errors or {}
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "git":
            return self.real_run(argv, **kwargs)
        self.calls.append(list(argv))
        # argv[0] is the claude binary; argv[1] == "mcp".
        verb = argv[2]
        if verb == "get":
            name = argv[3]
            if name not in self.registry:
                raise subprocess.CalledProcessError(
                    1, argv, stderr="No MCP server found"
                )
            if name in self.get_payloads:
                payload = self.get_payloads[name]
            else:
                command, scope = self.registry[name]
                payload = {
                    "command": command[0],
                    "args": command[1:],
                    "scope": scope,
                }
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload))
        if verb == "add":
            # [claude, mcp, add, --scope, <scope>, <name>, --, *tokens]
            scope = argv[4]
            name = argv[5]
            assert argv[6] == "--", f"expected literal -- separator, got {argv[6]!r}"
            tokens = list(argv[7:])
            if name in self.add_errors:
                raise subprocess.CalledProcessError(
                    1, argv, stderr=self.add_errors[name]
                )
            self.registry[name] = (tokens, scope)
            return subprocess.CompletedProcess(argv, 0, stdout="")
        if verb == "remove":
            # [claude, mcp, remove, --scope, <scope>, <name>]
            name = argv[5]
            if name in self.remove_errors:
                raise subprocess.CalledProcessError(
                    1, argv, stderr=self.remove_errors[name]
                )
            self.registry.pop(name, None)
            return subprocess.CompletedProcess(argv, 0, stdout="")
        raise AssertionError(f"unexpected mcp verb {verb!r}")
