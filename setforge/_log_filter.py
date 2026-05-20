"""Log-record filter that masks common secret shapes from ``-vv`` output.

Wired at the root callback (``cli/__init__.py``) so every logger under
the ``setforge`` namespace passes through. Two regex patterns cover the
realistic leak surfaces:

- ``(TOKEN|KEY|SECRET|PASSWORD|PASSWD|AUTH)\\w*=<value>`` — env-var-shape
  assignments and `--token=<value>` style flags.
- ``https://<user>:<password>@`` — credentials embedded in URLs (the
  shape git uses for HTTP-auth remotes).

Both patterns rewrite the VALUE to ``<REDACTED>`` while preserving the
key name so a user reading a debug log can still see *what* was logged
without seeing *what value*. The filter is conservative: it never
mutates non-string ``record.msg`` payloads (e.g. lazy %-format LogRecord
tuples), so callers that rely on string interpolation deferred to the
handler still work — they just don't get redaction. Callers that want
guaranteed redaction should pass the already-interpolated string.
"""

from __future__ import annotations

import logging
import re


class RedactingFilter(logging.Filter):
    """Mask token / credential shapes in ``record.msg`` before emission.

    Stateless; safe to attach to multiple loggers. Returns ``True``
    unconditionally — the filter never drops a record, only rewrites
    its message.
    """

    _SECRET_KEY_RE: re.Pattern[str] = re.compile(
        r"(TOKEN|KEY|SECRET|PASSWORD|PASSWD|AUTH)\w*=\S+",
        re.IGNORECASE,
    )
    _CRED_URL_RE: re.Pattern[str] = re.compile(r"https://[^:/@\s]+:[^@\s]+@")

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite secret-shaped substrings in ``record.msg`` in-place."""
        if isinstance(record.msg, str):
            redacted = self._SECRET_KEY_RE.sub(
                lambda m: f"{m.group(1)}=<REDACTED>", record.msg
            )
            redacted = self._CRED_URL_RE.sub("https://<REDACTED>@", redacted)
            record.msg = redacted
        return True
