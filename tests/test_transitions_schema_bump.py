"""Tests for the setforge-8ohd TransitionMeta schema bump.

Covers three forward-compat invariants:

1. Backward-compat: an old (pre-bump) ``meta.json`` with no
   ``end_timestamp`` / ``command_line`` / ``preserve_user_keys_applied``
   keys loads cleanly via :func:`setforge.transitions.load_meta`.
2. Omit-when-None: a :class:`TransitionMeta` instance with all three
   new fields left at their ``None`` defaults emits a ``to_dict()``
   payload that contains NONE of the new keys (preserves byte-identical
   round-trip with pre-bump records).
3. Byte-identical round-trip: loading the pre-bump fixture and
   re-serializing through ``to_dict()`` (plus the same ``paths``
   sidecar handling that :func:`write_meta` applies) produces a payload
   that re-serializes byte-identically to the original fixture.

Plus argv redaction tests for :func:`setforge._redact.redact_argv`
covering ``--token=``, ``--password=``, ``--api-key=``, and
``Authorization:`` shapes per SPEC 3.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from setforge._redact import REDACTED, redact_argv
from setforge.transitions import (
    TransitionCommand,
    TransitionDir,
    TransitionMeta,
    load_meta,
)

_FIXTURE_DIR = TransitionDir(Path(__file__).parent / "fixtures" / "pre_bump_meta_v1")


def test_load_meta_backward_compat_old_record() -> None:
    """An old (pre-bump) meta.json missing the 3 new fields loads cleanly.

    Hydrated dataclass has ``None`` for every new field; no
    :class:`InvalidTransitionRecord` raised — that's the regression
    contract for the dozens of pre-bump records on real users' disks.
    """
    meta = load_meta(_FIXTURE_DIR)
    assert meta.command is TransitionCommand.INSTALL
    assert meta.profile == "vm-headless"
    assert meta.host == "test-host.example"
    assert meta.version == "0.1.42"
    assert meta.source_sha == "abc123def456789abcdef0123456789abcdef012"
    # The setforge-8ohd schema-bump fields default to None when absent:
    assert meta.end_timestamp is None
    assert meta.command_line is None
    assert meta.preserve_user_keys_applied is None


def test_to_dict_omits_none_fields() -> None:
    """When the 3 new fields are None, to_dict() emits none of those keys.

    The omit-when-None invariant is what keeps pre-bump records
    round-tripping byte-identically; if to_dict() emitted ``null``
    for unset fields, every reload + redump would mutate the on-disk
    bytes.
    """
    meta = TransitionMeta(
        command=TransitionCommand.INSTALL,
        profile="vm-headless",
        timestamp=datetime(2026, 5, 10, 14, 23, 17, 123456, tzinfo=UTC),
        host="test-host.example",
        version="0.1.42",
        source_sha="abc123",
        # end_timestamp / command_line / preserve_user_keys_applied
        # left at their None defaults.
    )
    payload = meta.to_dict()
    assert "end_timestamp" not in payload
    assert "command_line" not in payload
    assert "preserve_user_keys_applied" not in payload
    # And the pre-bump fields are still present:
    assert payload["command"] == "install"
    assert payload["profile"] == "vm-headless"
    assert payload["source_sha"] == "abc123"


def test_to_dict_emits_new_fields_when_set() -> None:
    """When the new fields ARE set, to_dict() emits them with correct shape.

    Complements the omit-when-None test by pinning the positive shape:
    ``command_line`` defensive-copies (list, not tuple),
    ``preserve_user_keys_applied`` round-trips as bool, ``end_timestamp``
    as str. This is the forward-direction half of the invariant.
    """
    argv = ["install", "--profile=vm-headless"]
    meta = TransitionMeta(
        command=TransitionCommand.INSTALL,
        profile="vm-headless",
        timestamp=datetime(2026, 5, 10, 14, 23, 17, tzinfo=UTC),
        host="test-host.example",
        version="0.1.42",
        end_timestamp="2026-05-10T14:23:42+00:00",
        command_line=argv,
        preserve_user_keys_applied=True,
    )
    payload = meta.to_dict()
    assert payload["end_timestamp"] == "2026-05-10T14:23:42+00:00"
    assert payload["command_line"] == argv
    # Defensive copy: mutating the returned list must NOT mutate the
    # dataclass's stored field (we cannot mutate a frozen attr, but the
    # underlying list IS mutable through the attr reference).
    assert payload["command_line"] is not argv
    assert payload["preserve_user_keys_applied"] is True


def test_byte_identical_roundtrip() -> None:
    """Pre-bump fixture → load_meta → to_dict() → re-serialize is byte-identical.

    Locks in the forward-compat invariant for every pre-bump record on
    disk: a fresh-installed setforge must reload + re-dump such records
    without changing the JSON bytes. ``write_meta`` adds the ``paths``
    sidecar to the payload AFTER ``to_dict()``, so the comparison
    includes that step to mirror real production behavior.
    """
    original_bytes = (_FIXTURE_DIR / "meta.json").read_bytes()
    original = json.loads(original_bytes)
    meta = load_meta(_FIXTURE_DIR)

    body: dict[str, object] = dict(meta.to_dict())
    # write_meta re-attaches the paths sidecar after to_dict; replicate
    # so the comparison sees the exact on-disk shape, not a stripped
    # subset.
    body["paths"] = original["paths"]
    re_serialized = json.dumps(body, indent=2) + "\n"

    assert re_serialized == original_bytes.decode("utf-8")


def test_command_line_redacts_token_password() -> None:
    """redact_argv masks --token=/--password=/--api-key=/Authorization: values.

    Verifies all four declared shapes per SPEC 3 anti-pattern check 6.
    The flag/header NAME is preserved (``--token=<REDACTED>``, not
    ``<REDACTED>``) so a user reading ``transitions show`` can still
    see what flag was passed without seeing the value.
    """
    masked = redact_argv(
        [
            "--token=ghp_abc123secret",
            "--password=hunter2",
            "--api-key=sk-live-xxxxx",
            "Authorization:Bearer eyJhbGci",
            "--profile=vm-headless",  # non-secret: passes through
        ]
    )
    assert masked == [
        f"--token={REDACTED}",
        f"--password={REDACTED}",
        f"--api-key={REDACTED}",
        f"Authorization:{REDACTED}",
        "--profile=vm-headless",
    ]


def test_command_line_redact_case_insensitive() -> None:
    """Flag/header name matching is case-insensitive on the NAME only.

    A user (or test fixture) that writes ``--TOKEN=...`` or
    ``authorization:...`` shouldn't slip through; the value is what
    matters, not the casing of the flag.
    """
    masked = redact_argv(["--TOKEN=secret", "authorization:Bearer xyz"])
    assert masked == [f"--TOKEN={REDACTED}", f"authorization:{REDACTED}"]


def test_command_line_redact_returns_fresh_list() -> None:
    """The returned list is never the same object as the input list.

    Callers store the result in a frozen dataclass — aliasing the
    caller's sys.argv reference would let later mutation leak in.
    """
    argv = ["--profile=vm-headless"]
    result = redact_argv(argv)
    assert result is not argv
    assert result == argv  # content equal: no secret-shaped entries


def test_command_line_redact_empty_value_passes_through() -> None:
    """Empty-value shapes like ``--token=`` are typos, not secrets.

    The redactor's regex requires ``.+`` on the value side so a flag
    with an empty value passes through unchanged. Catching an empty
    value as ``--token=<REDACTED>`` would mask a user typo with
    fake-secret optics.
    """
    assert redact_argv(["--token="]) == ["--token="]
