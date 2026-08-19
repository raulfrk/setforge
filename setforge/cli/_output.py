"""Output-mode boundary for setforge subcommands.

Concentrates the JSON-versus-human dispatch that every JSON-emitting
subcommand (``compare``, ``status``, ``inspect``, ``profile show``,
``transitions list``, ``stage --list``, ``config show --effective``)
needs into one renderer so the per-subcommand bodies stay human-shape
and the JSON envelope ships in one place.

Three pieces:

- :class:`OutputFormat` — the closed set ``{HUMAN, JSON}`` declared as a
  ``StrEnum`` so Typer renders it as ``--format=human|json`` and the
  values stay typed end-to-end.
- :class:`OutputContext` — the immutable per-invocation envelope wired
  onto ``ctx.obj`` by the root callback (``--format``).
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
from typing import NotRequired, TypedDict

OUTPUT_SCHEMA_VERSION: int = 1
"""Cross-tool contract version for the JSON envelope.

Downstream ``jq`` consumers branch on ``schema_version``; bump this
constant on any breaking change to the ``data`` shapes.
"""


class _JsonEnvelope(TypedDict):
    """Shape of the dict serialised by :func:`wrap_json`.

    ``schema_version`` / ``command`` / ``data`` are always present in
    the emitted envelope; ``errors`` is included only when non-empty.
    """

    schema_version: int
    command: str
    data: object
    errors: NotRequired[list[str]]


class OutputFormat(StrEnum):
    """Output rendering mode selected via ``--format/-o``."""

    HUMAN = "human"
    JSON = "json"


@dataclass(slots=True, frozen=True)
class OutputContext:
    """Per-invocation output-mode envelope wired onto ``ctx.obj``.

    Carries the rendering mode selected by ``--format/-o``. The root
    ``quiet`` suppresses the human success renderer while leaving errors on
    stderr. Logging still uses the level configured by the root callback.
    """

    format: OutputFormat
    quiet: bool = False


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
    envelope: _JsonEnvelope = {
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

    JSON mode writes the envelope to ``sys.stdout`` followed by a newline.
    Quiet human mode returns without invoking ``human_fn``. Default human mode
    invokes the closure unchanged. JSON output is stdout-only by contract;
    logs and warnings go to stderr exclusively, so a downstream ``| jq``
    pipeline never has to filter mixed streams.

    ``ctx_obj=None`` always raises :class:`RuntimeError`: a subcommand
    that forgets to declare ``ctx: typer.Context`` (or otherwise fails
    to thread the root callback's :class:`OutputContext`) must fail
    loudly instead of silently downgrading JSON mode to human output.
    Tests exercising the renderer construct a real ``OutputContext``.

    ``human_fn`` is a zero-arg closure rather than a function-of-data so
    subcommand call sites can keep their Rich ``Console`` instances and
    ad-hoc multi-block layouts inside the closure.
    """
    if ctx_obj is None:
        raise RuntimeError(
            "render() called with ctx_obj=None — "
            "subcommand must thread ctx.obj from root callback"
        )
    if ctx_obj.format is OutputFormat.JSON:
        sys.stdout.write(wrap_json(command, data))
        sys.stdout.write("\n")
        return
    if ctx_obj.quiet:
        return
    human_fn()
