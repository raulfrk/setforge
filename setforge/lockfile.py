"""The committed ``setforge.lock`` model + (de)serialization: DETERMINISTIC
(pins/profiles sorted, no timestamp/host field)."""

import tomllib
from pathlib import Path

import tomli_w
from pydantic import BaseModel, ConfigDict, Field

from setforge.atomicio import atomic_write_text
from setforge.errors import MalformedLockError
from setforge.provision.resolve.protocol import (
    _KIND_FIELD,
    IntegrityKind,
    PackageType,
    ResolvedPin,
)

LOCK_FILENAME = "setforge.lock"
LOCK_VERSION = 1

_FIELD_KIND: dict[str, IntegrityKind] = {v: k for k, v in _KIND_FIELD.items()}


class LockFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = LOCK_VERSION
    packages: tuple[ResolvedPin, ...] = Field(default_factory=tuple)


def lock_path(config_path: Path) -> Path:
    return config_path.resolve().parent / LOCK_FILENAME


def dump_lock(lockfile: LockFile) -> str:
    packages = [
        pin.to_lock_entry()
        for pin in sorted(lockfile.packages, key=ResolvedPin.sort_key)
    ]
    document: dict[str, object] = {"version": lockfile.version, "package": packages}
    return tomli_w.dumps(document)


def parse_lock(text: str) -> LockFile:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise MalformedLockError(f"setforge.lock is not valid TOML: {exc}") from exc

    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise MalformedLockError(
            f"setforge.lock: 'version' must be an integer, got {version!r}"
        )

    packages_raw = raw.get("package", [])
    if not isinstance(packages_raw, list):
        raise MalformedLockError("setforge.lock: 'package' must be an array of tables")

    pins = tuple(_parse_pin(entry) for entry in packages_raw)
    return LockFile(version=version, packages=pins)


def write_lock(lockfile: LockFile, path: Path) -> None:
    atomic_write_text(path, dump_lock(lockfile))


def _parse_pin(entry: object) -> ResolvedPin:
    if not isinstance(entry, dict):
        raise MalformedLockError(
            f"setforge.lock: each 'package' must be a table, got {type(entry).__name__}"
        )

    type_raw = _require(entry, "type")
    try:
        pkg_type = PackageType(type_raw)
    except ValueError:
        known = ", ".join(t.value for t in PackageType)
        raise MalformedLockError(
            f"setforge.lock: unknown package type {type_raw!r}; known types: {known}"
        ) from None

    key = _require(entry, "key")
    version = _require(entry, "version")

    present = [field for field in _FIELD_KIND if field in entry]
    if not present:
        raise MalformedLockError(
            f"setforge.lock: package {key!r} has no integrity field "
            f"(one of {', '.join(_FIELD_KIND)} required)"
        )
    if len(present) > 1:
        raise MalformedLockError(
            f"setforge.lock: package {key!r} carries multiple integrity fields "
            f"({', '.join(present)}); exactly one is allowed"
        )
    field = present[0]
    expected = _KIND_FIELD[_expected_kind(pkg_type)]
    if field != expected:
        raise MalformedLockError(
            f"setforge.lock: package {key!r} (type {pkg_type.value!r}) uses "
            f"integrity field {field!r}, but this ecosystem uses {expected!r}"
        )

    profiles_raw = entry.get("profiles", [])
    if not isinstance(profiles_raw, list) or not all(
        isinstance(p, str) for p in profiles_raw
    ):
        raise MalformedLockError(
            f"setforge.lock: package {key!r} 'profiles' must be a list of strings"
        )

    return ResolvedPin(
        type=pkg_type,
        key=key,
        version=version,
        integrity=_require(entry, field),
        integrity_kind=_FIELD_KIND[field],
        profiles=tuple(profiles_raw),
    )


def _expected_kind(pkg_type: PackageType) -> IntegrityKind:
    if pkg_type is PackageType.GO:
        return IntegrityKind.SUM
    if pkg_type is PackageType.PLUGIN:
        return IntegrityKind.SHA
    return IntegrityKind.CHECKSUM


def _require(entry: dict[str, object], field: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value:
        raise MalformedLockError(
            f"setforge.lock: package field {field!r} must be a non-empty "
            f"string, got {value!r}"
        )
    return value
