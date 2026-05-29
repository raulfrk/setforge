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
Redaction covers both the interpolated ``record.msg`` string and the
``record.args`` payload of lazy %-format records: a non-string ``msg``
(the deferred-interpolation case) still has its str-valued args masked,
so secrets passed positionally (``log.debug("token=%s", secret)``) or
by mapping (``log.debug("token=%(t)s", {"t": secret})``) are caught.
Non-str args (ints, paths, objects) pass through unchanged and the
container shape (tuple vs. mapping, arity, keys) is preserved, so the
handler's deferred ``%``-interpolation still succeeds.
"""

from __future__ import annotations

import collections.abc
import logging
import re
from typing import override


class RedactingFilter(logging.Filter):
    """Mask token / credential shapes in ``record.msg`` / args before emission.

    Stateless; safe to attach to multiple loggers — redaction uses only
    locals and the class-level compiled patterns, and ``record.args`` is
    rebuilt into a fresh container rather than mutated in place, so a
    ``LogRecord`` shared across handlers/threads is never corrupted.
    Returns ``True`` unconditionally — the filter never drops a record,
    only rewrites its message and args.
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

    def _redact(self, value: str) -> str:
        """Apply the secret-masking substitution chain to ``value``.

        Pure ``str -> str`` transform using only locals and the
        class-level compiled patterns; holds no instance state.

        Order matters: JWT and GitHub-PAT patterns are more specific
        than the generic KEY=VALUE shape, so run them first to avoid
        the generic pattern's ``\\S+`` eating a token that would have
        gotten a more informative prefix-preserving mask.
        """
        value = self._JWT_RE.sub("<REDACTED-JWT>", value)
        value = self._GITHUB_PAT_RE.sub("gh<REDACTED>", value)
        value = self._AWS_KEY_RE.sub("AKIA<REDACTED>", value)
        value = self._BEARER_RE.sub(lambda m: f"{m.group(1)} <REDACTED>", value)
        value = self._SECRET_KEY_RE.sub(lambda m: f"{m.group(1)}=<REDACTED>", value)
        value = self._SECRET_FLAG_RE.sub(lambda m: f"{m.group(1)} <REDACTED>", value)
        value = self._CRED_URL_RE.sub(lambda m: f"{m.group(1)}<REDACTED>@", value)
        return value

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite secret-shaped substrings in ``record.msg`` and ``record.args``.

        Args redaction runs independent of the non-str ``record.msg``
        early return: a lazy %-format record (non-str/lazy ``msg`` with
        secret str values in ``record.args``) still gets its args
        masked. Branches on ``Mapping`` before tuple because a dict is
        also iterable; both forms rebuild a fresh container (never
        mutated in place) so the shared ``LogRecord`` is not corrupted
        across handlers/threads.
        """
        if record.args is not None:
            if isinstance(record.args, collections.abc.Mapping):
                record.args = {
                    key: self._redact(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._redact(arg) if isinstance(arg, str) else arg
                    for arg in record.args
                )
        if not isinstance(record.msg, str):
            return True
        record.msg = self._redact(record.msg)
        return True
