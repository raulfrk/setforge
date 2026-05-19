"""Redact secret-shaped tokens from argv before storage.

Used by :mod:`setforge.transitions` callers when recording the
``command_line`` field in a transition's ``meta.json``. Today's setforge
takes no secret args at the CLI surface, but the field is captured
verbatim from ``sys.argv[1:]`` at the call site — meaning a future flag
that accepts a secret (or a user who mistakenly invokes setforge with
a stray ``--token=...``) would otherwise land the secret on disk in
plain text. Forward-safety: scrub the obvious shapes at the boundary.

The redactor masks the VALUE side of any argv entry whose shape matches
one of the patterns below; the FLAG name is preserved so a user reading
``setforge transitions show`` can still see *what* was passed without
seeing *what value*.

Patterns:

- ``--token=<value>``     → ``--token=<REDACTED>``
- ``--password=<value>``  → ``--password=<REDACTED>``
- ``--api-key=<value>``   → ``--api-key=<REDACTED>``
- ``Authorization:<value>`` → ``Authorization:<REDACTED>`` (HTTP header shape)

Matching is case-insensitive on the flag/header name; the value is the
entire remainder after the first ``=`` (or ``:`` for the header shape).
"""

from __future__ import annotations

import re

REDACTED = "<REDACTED>"

# One regex per shape; ``re.IGNORECASE`` on the flag/header name only.
# ``(?:...)?`` would let an empty value match — we keep ``.+`` so an entry
# like ``--token=`` (empty value) passes through unchanged rather than
# emitting ``--token=<REDACTED>`` for what's clearly a typo-shaped flag.
_FLAG_VALUE = re.compile(
    r"^(?P<flag>--(?:token|password|api-key))=.+$",
    re.IGNORECASE,
)
_HEADER_VALUE = re.compile(
    r"^(?P<header>Authorization):.+$",
    re.IGNORECASE,
)


def _redact_one(arg: str) -> str:
    """Return ``arg`` with its secret value masked, or the original string."""
    m = _FLAG_VALUE.match(arg)
    if m is not None:
        return f"{m.group('flag')}={REDACTED}"
    m = _HEADER_VALUE.match(arg)
    if m is not None:
        return f"{m.group('header')}:{REDACTED}"
    return arg


def redact_argv(argv: list[str]) -> list[str]:
    """Return a defensive copy of ``argv`` with secret-shaped values masked.

    The input list is never mutated; the return value is always a fresh
    list so the caller can store it in a frozen dataclass without
    aliasing whatever object the caller pulled from ``sys.argv``.
    """
    return [_redact_one(arg) for arg in argv]
