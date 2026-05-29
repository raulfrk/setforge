"""Redaction coverage for ``record.args`` in :class:`RedactingFilter`.

Covers the lazy %-format leak path: secrets passed positionally
(``log.debug("t=%s", secret)``) or by mapping (``"t=%(s)s", {"s": secret}``)
must be masked in ``record.args``, while non-str args, arity, keys, and the
container type are preserved so the handler's deferred ``%``-interpolation
still succeeds. Also exercises the edge/error inputs that the logging
machinery would silently swallow: ``args=None``, empty tuple, mixed-type
tuple, and a non-str ``msg`` whose str args still carry a secret.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from setforge._log_filter import RedactingFilter

type _Args = tuple[object, ...] | Mapping[str, object] | None

_SECRET_FLAG = "TOKEN=supersecretvalue"
_REDACTED_FLAG = "TOKEN=<REDACTED>"


def _make_record(msg: object, args: _Args) -> logging.LogRecord:
    """Build a LogRecord mirroring how ``logging`` stores ``args``.

    ``logging.Logger._log`` always passes ``args`` as a tuple to the record
    factory; ``LogRecord.__init__`` then unwraps a lone ``Mapping`` element to
    the bare dict (the ``%(name)s`` form). To reproduce the mapping form we
    therefore wrap the dict in a 1-tuple, matching ``log.x("%(s)s", {...})``.
    """
    if isinstance(args, dict):
        args = (args,)
    return logging.LogRecord(
        name="setforge.test",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_tuple_secret_redacted() -> None:
    """A secret in a positional str arg is masked; getMessage interpolates."""
    record = _make_record("env=%s", (_SECRET_FLAG,))
    original_args = record.args
    assert RedactingFilter().filter(record) is True
    assert isinstance(record.args, tuple)
    assert record.args == (_REDACTED_FLAG,)
    assert record.getMessage() == f"env={_REDACTED_FLAG}"
    # Fresh tuple; the original args object is not mutated in place.
    assert original_args == (_SECRET_FLAG,)


def test_dict_secret_redacted() -> None:
    """A secret in a ``%(name)s`` mapping value is masked; keys preserved."""
    record = _make_record("env=%(creds)s", {"creds": _SECRET_FLAG})
    original_args = record.args
    assert RedactingFilter().filter(record) is True
    assert isinstance(record.args, dict)
    assert record.args == {"creds": _REDACTED_FLAG}
    assert record.getMessage() == f"env={_REDACTED_FLAG}"
    assert original_args == {"creds": _SECRET_FLAG}


def test_args_none_does_not_raise() -> None:
    """``args=None`` (no-arg log call) passes through untouched."""
    record = _make_record("plain message", None)
    assert RedactingFilter().filter(record) is True
    assert record.args is None
    assert record.getMessage() == "plain message"


def test_empty_tuple_args_preserved() -> None:
    """An empty args tuple stays an empty tuple."""
    record = _make_record("no placeholders", ())
    assert RedactingFilter().filter(record) is True
    assert record.args == ()
    assert isinstance(record.args, tuple)
    assert record.getMessage() == "no placeholders"


def test_mixed_type_tuple_keeps_int() -> None:
    """A mixed ('%s=%d', secret, 5) tuple redacts the str, keeps the int 5."""
    record = _make_record("%s=%d", (_SECRET_FLAG, 5))
    assert RedactingFilter().filter(record) is True
    assert isinstance(record.args, tuple)
    redacted_str, int_arg = record.args
    assert redacted_str == _REDACTED_FLAG
    assert int_arg == 5
    assert isinstance(int_arg, int)
    # %-interpolation must still succeed with the int as a number.
    assert record.getMessage() == f"{_REDACTED_FLAG}=5"


def test_mixed_type_dict_keeps_non_str_values() -> None:
    """A mapping with str + int values redacts the str, keeps the int."""
    record = _make_record("%(s)s=%(n)d", {"s": _SECRET_FLAG, "n": 7})
    assert RedactingFilter().filter(record) is True
    assert isinstance(record.args, dict)
    assert record.args["s"] == _REDACTED_FLAG
    assert record.args["n"] == 7
    assert isinstance(record.args["n"], int)
    assert set(record.args) == {"s", "n"}
    assert record.getMessage() == f"{_REDACTED_FLAG}=7"


def test_nonstr_msg_with_secret_args_still_redacted() -> None:
    """A lazy/non-str ``msg`` does not short-circuit args redaction."""

    class LazyMsg:
        def __str__(self) -> str:
            return "env=%s"

    lazy = LazyMsg()
    record = _make_record(lazy, (_SECRET_FLAG,))
    assert RedactingFilter().filter(record) is True
    # msg stays the non-str object (early return preserves it)...
    assert record.msg is lazy
    # ...but the args were still redacted.
    assert record.args == (_REDACTED_FLAG,)
    assert record.getMessage() == f"env={_REDACTED_FLAG}"


def test_all_nonstr_tuple_passes_through_unchanged() -> None:
    """An all-non-str args tuple is left identical and does not raise."""
    obj = object()
    record = _make_record("%d %r", (5, obj))
    assert RedactingFilter().filter(record) is True
    assert isinstance(record.args, tuple)
    assert record.args[0] == 5
    assert record.args[1] is obj


def test_args_redaction_uses_full_pattern_set() -> None:
    """Args redaction applies the same pattern chain used for ``record.msg``.

    A raw GitHub token passed positionally (not in KEY=VALUE shape) must be
    caught, proving the args path reuses the shared ``_redact`` helper rather
    than a weaker subset.
    """
    token = "ghp_" + "a" * 36
    record = _make_record("auth %s", (token,))
    assert RedactingFilter().filter(record) is True
    assert record.args == ("gh<REDACTED>",)


def test_filter_is_stateless_across_records() -> None:
    """The same filter instance handles successive records without carryover."""
    flt = RedactingFilter()
    first = _make_record("env=%s", (_SECRET_FLAG,))
    second = _make_record("clean=%s", ("nothing-here",))
    assert flt.filter(first) is True
    assert flt.filter(second) is True
    assert first.args == (_REDACTED_FLAG,)
    assert second.args == ("nothing-here",)
    # No mutable per-call state leaked onto the instance.
    assert not hasattr(flt, "args")
