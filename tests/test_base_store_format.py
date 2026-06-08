"""Tests for the shared base-store format-version sidecar helper."""

from pathlib import Path

import pytest

from setforge import base_store_format
from setforge.errors import (
    BaseStoreError,
    BaseStoreIOError,
    BaseStoreSchemaError,
)
from setforge.migrations import parse_schema_version

# --- error hierarchy -----------------------------------------------------


def test_schema_error_subclasses_base_store_error() -> None:
    assert issubclass(BaseStoreSchemaError, BaseStoreError)


def test_schema_error_is_not_io_error() -> None:
    assert not issubclass(BaseStoreSchemaError, BaseStoreIOError)
    assert not issubclass(BaseStoreIOError, BaseStoreSchemaError)


# --- check_format_version: grandfather + match ---------------------------


def test_absent_sidecar_grandfathers(tmp_path: Path) -> None:
    # No sidecar at all -> treated as current v1, no raise.
    base_store_format.check_format_version(tmp_path)


def test_present_and_matching_ok(tmp_path: Path) -> None:
    (tmp_path / base_store_format.SIDECAR_NAME).write_text(
        base_store_format.BASE_STORE_FORMAT_VERSION + "\n", encoding="utf-8"
    )
    base_store_format.check_format_version(tmp_path)


def test_matching_tolerates_whitespace(tmp_path: Path) -> None:
    (tmp_path / base_store_format.SIDECAR_NAME).write_text(
        f"  {base_store_format.BASE_STORE_FORMAT_VERSION}  \n", encoding="utf-8"
    )
    base_store_format.check_format_version(tmp_path)


# --- check_format_version: refusals --------------------------------------


def test_present_mismatch_refuses(tmp_path: Path) -> None:
    (tmp_path / base_store_format.SIDECAR_NAME).write_text("2.0\n", encoding="utf-8")
    with pytest.raises(BaseStoreSchemaError) as excinfo:
        base_store_format.check_format_version(tmp_path)
    message = str(excinfo.value)
    assert str(tmp_path) in message
    assert "2.0" in message
    assert base_store_format.BASE_STORE_FORMAT_VERSION in message


def test_present_garbage_refuses_from_config_error(tmp_path: Path) -> None:
    (tmp_path / base_store_format.SIDECAR_NAME).write_text(
        "not-a-version\n", encoding="utf-8"
    )
    with pytest.raises(BaseStoreSchemaError) as excinfo:
        base_store_format.check_format_version(tmp_path)
    # ConfigError from parse_schema_version is the documented cause.
    assert excinfo.value.__cause__ is not None
    assert type(excinfo.value.__cause__).__name__ == "ConfigError"


def test_present_unreadable_oserror_refuses(tmp_path: Path) -> None:
    # A present-but-unreadable sidecar (here: a directory at the sidecar
    # path -> IsADirectoryError, an OSError that is NOT FileNotFoundError)
    # must refuse, never grandfather.
    (tmp_path / base_store_format.SIDECAR_NAME).mkdir()
    with pytest.raises(BaseStoreSchemaError):
        base_store_format.check_format_version(tmp_path)


# --- stamp_format_version ------------------------------------------------


def test_stamp_writes_sidecar(tmp_path: Path) -> None:
    base_store_format.stamp_format_version(tmp_path)
    sidecar = tmp_path / base_store_format.SIDECAR_NAME
    assert (
        sidecar.read_text(encoding="utf-8").strip()
        == base_store_format.BASE_STORE_FORMAT_VERSION
    )


def test_stamp_leaves_no_tmp_debris(tmp_path: Path) -> None:
    base_store_format.stamp_format_version(tmp_path)
    debris = [
        p.name for p in tmp_path.iterdir() if p.name != base_store_format.SIDECAR_NAME
    ]
    assert debris == []


def test_stamp_is_idempotent(tmp_path: Path) -> None:
    base_store_format.stamp_format_version(tmp_path)
    base_store_format.stamp_format_version(tmp_path)
    sidecar = tmp_path / base_store_format.SIDECAR_NAME
    assert (
        sidecar.read_text(encoding="utf-8").strip()
        == base_store_format.BASE_STORE_FORMAT_VERSION
    )
    # A re-stamp then a check must still pass.
    base_store_format.check_format_version(tmp_path)


def test_constant_is_parseable(tmp_path: Path) -> None:
    # The accepted version constant must itself be a valid MAJOR.MINOR.
    assert parse_schema_version(base_store_format.BASE_STORE_FORMAT_VERSION) == (1, 0)
