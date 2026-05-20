"""Log-record filter that masks common secret shapes from ``-vv`` output.

Wired at the root callback (``cli/__init__.py``) so every logger under
the ``setforge`` namespace passes through. The covered shapes are the
ones realistic to leak through a debug log of CLI args or env vars:

- ``(TOKEN|KEY|SECRET|PASSWORD|PASSWD|AUTH)<rest>=<value>`` — env-var
  assignments and ``--token=<value>`` style flags.
- ``--<key>(token|key|secret|password|passwd|auth) <value>`` —
  space-separated CLI flag form (``--token abc``); the lookahead at
  the end leaves ``--`` / EOL / quoted-string boundaries intact.
- ``https://<user>:<password>@`` — credentials embedded in URLs (the
  shape git uses for HTTP-auth remotes).
- ``Bearer <token>`` / ``Basic <token>`` — HTTP ``Authorization``
  header values; case-insensitive scheme match.
- ``AKIA[0-9A-Z]{16}`` — AWS access key IDs.
- ``gh[psoru]_[A-Za-z0-9_]{36}`` — GitHub personal-access /
  server-to-server / OAuth / refresh / user tokens.
- ``eyJ<base64url>.eyJ<base64url>.<base64url>`` — JWTs (any three
  base64url-encoded segments where the first two start with the
  literal ``eyJ`` prefix every JSON header / claim set produces).

Patterns rewrite the VALUE to ``<REDACTED>`` (or, for the AWS / GitHub
shapes, preserve a short prefix so a user can still tell *which*
provider's token leaked) while leaving the surrounding context intact.
The filter is conservative: it never mutates non-string ``record.msg``
payloads (e.g. lazy %-format LogRecord tuples), so callers that rely
on string interpolation deferred to the handler still work — they
just don't get redaction. Callers that want guaranteed redaction
should pass the already-interpolated string.
"""

from __future__ import annotations

import logging
import re
from typing import override


class RedactingFilter(logging.Filter):
    """Mask token / credential shapes in ``record.msg`` before emission.

    Stateless; safe to attach to multiple loggers. Returns ``True``
    unconditionally — the filter never drops a record, only rewrites
    its message.
    """

    # KEY=VALUE form: env-var assignments and `--token=abc` flags. The
    # secret-key prefixes (TOKEN/KEY/SECRET/PASSWORD/PASSWD/AUTH) cover
    # the common-leak surface; case-insensitive so `Token=` /
    # `--AUTH=...` both match.
    _SECRET_KEY_RE: re.Pattern[str] = re.compile(
        r"(TOKEN|KEY|SECRET|PASSWORD|PASSWD|AUTH)\w*=\S+",
        re.IGNORECASE,
    )
    # KEY VALUE form (space-separated): `--token abc` style flags. The
    # leading `--` anchors the match to CLI-flag context so we don't
    # accidentally rewrite prose like "the auth and key sections".
    _SECRET_FLAG_RE: re.Pattern[str] = re.compile(
        r"(--\w*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD|AUTH)\w*)\s+(\S+)",
        re.IGNORECASE,
    )
    # https://user:pass@host (and http://) — git's HTTP-auth remote
    # shape. Scheme is case-insensitive per RFC 3986.
    _CRED_URL_RE: re.Pattern[str] = re.compile(
        r"(https?://)[^:/@\s]+:[^@\s]+@",
        re.IGNORECASE,
    )
    # `Bearer <token>` / `Basic <token>` Authorization header values.
    # The character class admits base64url + JWT-shape values; the
    # 8-char floor avoids rewriting prose containing the word "bearer".
    _BEARER_RE: re.Pattern[str] = re.compile(
        r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}",
        re.IGNORECASE,
    )
    # AWS access key IDs: AKIA followed by exactly 16 upper-alphanum
    # characters. Pinned shape; never has lowercase or punctuation.
    _AWS_KEY_RE: re.Pattern[str] = re.compile(r"AKIA[0-9A-Z]{16}")
    # GitHub PATs / server / OAuth / refresh / user tokens. The
    # `gh[psoru]_` prefix tag was introduced in GitHub's 2021 token
    # format change; the 36-char body is the canonical length.
    _GITHUB_PAT_RE: re.Pattern[str] = re.compile(r"gh[psoru]_[A-Za-z0-9_]{36}")
    # JWT: three dot-separated base64url segments where the first two
    # start with the literal `eyJ` prefix (the base64url encoding of
    # `{"` that every JSON header / claim set produces).
    _JWT_RE: re.Pattern[str] = re.compile(
        r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    )

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite secret-shaped substrings in ``record.msg`` in-place."""
        if not isinstance(record.msg, str):
            return True
        msg = record.msg
        # Order matters: JWT and GitHub-PAT patterns are more specific
        # than the generic KEY=VALUE shape, so run them first to avoid
        # the generic pattern's `\S+` eating a token that would have
        # gotten a more informative prefix-preserving mask.
        msg = self._JWT_RE.sub("<REDACTED-JWT>", msg)
        msg = self._GITHUB_PAT_RE.sub("gh<REDACTED>", msg)
        msg = self._AWS_KEY_RE.sub("AKIA<REDACTED>", msg)
        msg = self._BEARER_RE.sub(lambda m: f"{m.group(1)} <REDACTED>", msg)
        msg = self._SECRET_KEY_RE.sub(lambda m: f"{m.group(1)}=<REDACTED>", msg)
        msg = self._SECRET_FLAG_RE.sub(lambda m: f"{m.group(1)} <REDACTED>", msg)
        msg = self._CRED_URL_RE.sub(lambda m: f"{m.group(1)}<REDACTED>@", msg)
        record.msg = msg
        return True
