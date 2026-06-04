"""Tests for the file-level ``disposition`` field on ``TrackedFile``."""

import pytest
from pydantic import ValidationError

from setforge.config import Disposition, TrackedFile


def _tf(**kw: object) -> TrackedFile:
    return TrackedFile.model_validate({"src": "a.md", "dst": "~/a.md", **kw})


def test_disposition_defaults_none() -> None:
    assert _tf().disposition is None


def test_disposition_accepts_each_value() -> None:
    assert _tf(disposition="shared").disposition is Disposition.SHARED
    assert _tf(disposition="forked").disposition is Disposition.FORKED
    assert _tf(disposition="pinned").disposition is Disposition.PINNED


@pytest.mark.parametrize("bad", ["Shared", "PINNED", "shared ", "fork", "host-local"])
def test_disposition_rejects_bad_value(bad: str) -> None:
    with pytest.raises(ValidationError):
        _tf(disposition=bad)


@pytest.mark.parametrize(
    "legacy",
    [
        {"preserve_user_sections": True},
        {"preserve_user_keys": ["a"]},
        {"preserve_user_keys_deep": ["a"]},
    ],
)
def test_disposition_mutually_exclusive_with_legacy(legacy: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="disposition"):
        _tf(disposition="shared", **legacy)


def test_no_disposition_allows_legacy() -> None:
    assert _tf(preserve_user_keys=["a"]).disposition is None
