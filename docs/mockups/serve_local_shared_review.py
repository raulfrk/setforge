#!/usr/bin/env python3
"""Serve the SetForge audit and persist section feedback outside the worktree."""

from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypedDict, TypeGuard
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
PAGE = "local-shared-reconcile-audit.html"
MAX_BODY = 32_768
WRITE_LOCK = threading.Lock()
REVIEW_SECTIONS = frozenset(
    {"summary", "reconciliation", "workflow", "adoption", "feedback", "remaining"}
)


class FeedbackEntry(TypedDict):
    id: str
    section: str
    note: str
    author: str
    created_at: str


class FeedbackStateError(RuntimeError):
    """The persisted review state cannot be read without risking data loss."""


def feedback_path() -> Path:
    """Return the private feedback path, allowing tests to override it."""
    override = os.environ.get("SETFORGE_REVIEW_FEEDBACK_FILE")
    if override:
        return Path(override).expanduser().resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=HERE,
        check=True,
        capture_output=True,
        text=True,
    )
    git_dir = Path(result.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (HERE / git_dir).resolve()
    return git_dir / "setforge-review" / "local-shared-feedback.json"


def is_feedback_entry(value: object) -> TypeGuard[FeedbackEntry]:
    """Return whether a persisted value matches the rendered feedback schema."""
    if not isinstance(value, dict):
        return False
    required = ("id", "section", "note", "author", "created_at")
    return (
        all(isinstance(value.get(field), str) for field in required)
        and value.get("section") in REVIEW_SECTIONS
    )


def read_feedback(path: Path) -> list[FeedbackEntry]:
    """Read feedback entries while refusing invalid persisted state."""
    if not path.exists():
        return []
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FeedbackStateError(
            "persisted feedback is unreadable or invalid"
        ) from error
    if not isinstance(value, list):
        raise FeedbackStateError("persisted feedback must be a JSON array")
    entries: list[FeedbackEntry] = []
    for entry in value:
        if not is_feedback_entry(entry):
            raise FeedbackStateError("persisted feedback contains an invalid entry")
        entries.append(entry)
    return entries


@contextmanager
def feedback_lock(path: Path) -> Iterator[None]:
    """Serialize read-modify-replace across threads and server processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    with WRITE_LOCK, lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def append_feedback(path: Path, entry: FeedbackEntry) -> None:
    """Append one entry with a locked, atomic replacement."""
    with feedback_lock(path):
        entries = read_feedback(path)
        entries.append(entry)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix="feedback-",
            suffix=".json",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(entries, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class ReviewHandler(SimpleHTTPRequestHandler):
    """Serve the static audit and its deliberately narrow feedback API."""

    server_version = "SetForgeAudit/1"

    def __init__(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
        server: socketserver.BaseServer,
        directory: str | None = None,
    ) -> None:
        super().__init__(
            request,
            client_address,
            server,
            directory=str(HERE) if directory is None else directory,
        )

    def send_json(self, status: HTTPStatus, payload: object) -> None:
        """Send a no-cache JSON response."""
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def same_origin_request(self) -> bool:
        """Accept browser writes only from this server's own origin."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        parsed = urlparse(origin)
        return parsed.scheme == "http" and parsed.netloc == self.headers.get("Host")

    def read_json_request(self) -> dict[str, object] | None:
        """Validate the feedback request envelope and return its JSON object."""
        if not self.same_origin_request():
            self.send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "cross-origin feedback is forbidden"},
            )
            return None
        content_type = self.headers.get("Content-Type", "")
        media_type = content_type.partition(";")[0].strip().lower()
        if media_type != "application/json":
            self.send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "application/json is required"},
            )
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
            return None
        if not 0 < length <= MAX_BODY:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "feedback body is empty or too large"},
            )
            return None
        try:
            payload: object = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return None
        if not isinstance(payload, dict):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "feedback must be an object"},
            )
            return None
        return payload

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if path == "/api/feedback":
            try:
                feedback = read_feedback(feedback_path())
            except FeedbackStateError as error:
                self.send_json(HTTPStatus.CONFLICT, {"error": str(error)})
                return
            self.send_json(HTTPStatus.OK, {"feedback": feedback})
            return
        if path == "/":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", f"/{PAGE}")
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/feedback":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        payload = self.read_json_request()
        if payload is None:
            return
        section = payload.get("section")
        note = payload.get("note")
        author = payload.get("author", "Reviewer")
        if (
            not isinstance(section, str)
            or not isinstance(note, str)
            or not isinstance(author, str)
        ):
            self.send_json(
                HTTPStatus.BAD_REQUEST, {"error": "feedback fields must be strings"}
            )
            return
        section = section.strip()
        note = note.strip()
        author = author.strip() or "Reviewer"
        if not section or not note:
            self.send_json(
                HTTPStatus.BAD_REQUEST, {"error": "section and note are required"}
            )
            return
        if section not in REVIEW_SECTIONS:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "unknown review section"})
            return
        entry: FeedbackEntry = {
            "id": f"fb-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}",
            "section": section[:200],
            "note": note[:10_000],
            "author": author[:200],
            "created_at": datetime.now(UTC).isoformat(),
        }
        try:
            append_feedback(feedback_path(), entry)
        except FeedbackStateError as error:
            self.send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        self.send_json(HTTPStatus.CREATED, {"feedback": entry})

    def log_message(self, message: str, *args: object) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {message % args}\n")


class ReviewServer(ThreadingHTTPServer):
    """HTTP server whose completed review requests never hold process shutdown."""

    daemon_threads = True


def host_is_loopback(host: str) -> bool:
    """Return whether every address resolved for host is loopback-only."""
    try:
        addresses = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return bool(addresses) and all(
        ipaddress.ip_address(address[4][0]).is_loopback for address in addresses
    )


def main() -> None:
    """Run the local review server until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not host_is_loopback(args.host):
        parser.error("--host must resolve only to loopback addresses")
    server = ReviewServer((args.host, args.port), ReviewHandler)
    sys.stdout.write(f"Review page: http://{args.host}:{args.port}/\n")
    sys.stdout.write(f"Feedback: {feedback_path()}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
