"""Output-mode boundary for setforge subcommands.

Concentrates the JSON-versus-human dispatch that every JSON-emitting
subcommand (``compare``, ``status``, ``profile show``, ``transitions
list``) needs into one renderer so the per-subcommand bodies stay
human-shape and the JSON envelope ships in one place.

Three pieces:

- :class:`OutputFormat` — the closed set ``{HUMAN, JSON}`` declared as a
  ``StrEnum`` so Typer renders it as ``--format=human|json`` and the
  values stay typed end-to-end.
- :class:`OutputContext` — the immutable per-invocation envelope wired
  onto ``ctx.obj`` by the root callback (``--format`` /
  ``--quiet`` / ``--verbose`` count).
- :func:`wrap_json` / :func:`render` — the JSON envelope (with
  ``schema_version`` = :data:`OUTPUT_SCHEMA_VERSION`) and the dispatch
  boundary.

Subcommand integration: compute the result, then call
``_output.render(ctx_obj, "<command>", data, human_fn=<human_renderer>)``
instead of printing directly.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


OUTPUT_SCHEMA_VERSION: int = 1
"""Cross-tool contract version for the JSON envelope.

Downstream ``jq`` consumers branch on ``schema_version``; bump this
constant on any breaking change to the ``data`` shapes.
"""


class OutputFormat(StrEnum):
    """Output rendering mode selected via ``--format/-o``."""

    HUMAN = "human"
    JSON = "json"


@dataclass(slots=True, frozen=True)
class OutputContext:
    """Per-invocation output-mode envelope wired onto ``ctx.obj``.

    ``format`` is the rendering mode; ``quiet`` and ``verbose`` are the
    raw flag values from the root callback (``--quiet`` is bool;
    ``-v``/``--verbose`` is a count via ``count=True``). The mutex
    check (``quiet and verbose``) runs in the root callback before the
    context is built, so both can be non-default here only across
    mutually-exclusive invocations from different processes.
    """

    format: OutputFormat
    quiet: bool
    verbose: int


def wrap_json(
    command: str,
    data: object,
    errors: list[str] | None = None,
) -> str:
    """Return the versioned JSON envelope as a UTF-8 string.

    The envelope shape is ``{"schema_version": OUTPUT_SCHEMA_VERSION,
    "command": <command>, "data": <data>}``; ``errors`` is included only
    when non-empty.

    ``default=str`` keeps :class:`pathlib.Path` / :class:`datetime`
    serialisable without per-callsite coercion.
    """
    envelope: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "command": command,
        "data": data,
    }
    if errors:
        envelope["errors"] = errors
    return json.dumps(envelope, indent=2, default=str)


def render(
    ctx_obj: OutputContext | None,
    command: str,
    data: object,
    *,
    human_fn: Callable[[], None],
) -> None:
    """Dispatch the final-output surface for one subcommand.

    When ``ctx_obj`` is ``None`` (test harnesses that bypass the root
    callback) or ``ctx_obj.format`` is :attr:`OutputFormat.HUMAN`,
    invokes ``human_fn`` so the existing human renderer runs unchanged.
    Otherwise writes the JSON envelope to ``sys.stdout`` followed by a
    newline — JSON output is stdout-only by contract; logs and warnings
    go to stderr exclusively, so a downstream ``| jq`` pipeline never
    has to filter mixed streams.

    ``human_fn`` is a zero-arg closure rather than a function-of-data so
    subcommand call sites can keep their Rich ``Console`` instances and
    ad-hoc multi-block layouts inside the closure.
    """
    if ctx_obj is not None and ctx_obj.format is OutputFormat.JSON:
        sys.stdout.write(wrap_json(command, data))
        sys.stdout.write("\n")
        return
    human_fn()
