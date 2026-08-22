"""The committed ``setforge.lock`` model + (de)serialization: DETERMINISTIC
(pins/profiles sorted, no timestamp/host field)."""

import tomllib
from pathlib import Path

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, model_validator

from setforge.atomicio import atomic_write_text
from setforge.errors import LockVersionError, MalformedLockError
from setforge.provision.resolve.protocol import (
    _KIND_FIELD,
    IntegrityKind,
    PackageType,
    ResolvedArtifact,
    ResolvedPin,
    artifact_set_integrity,
)

LOCK_FILENAME = "setforge.lock"
LOCK_VERSION = 2

_FIELD_KIND: dict[str, IntegrityKind] = {v: k for k, v in _KIND_FIELD.items()}


class LockFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = LOCK_VERSION
    packages: tuple[ResolvedPin, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _version_supports_payload(self) -> "LockFile":
        if self.version < 2 and any(pin.artifacts for pin in self.packages):
            raise ValueError("lock format v1 cannot carry platform artifacts")
        return self


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

    if version > LOCK_VERSION:
        raise LockVersionError(
            f"setforge.lock was written by a newer setforge (lock format v{version}; "
            f"this build reads v{LOCK_VERSION}). Upgrade setforge."
        )

    packages_raw = raw.get("package", [])
    if not isinstance(packages_raw, list):
        raise MalformedLockError("setforge.lock: 'package' must be an array of tables")

    pins = tuple(_parse_pin(entry, lock_version=version) for entry in packages_raw)
    return LockFile(version=version, packages=pins)


def write_lock(lockfile: LockFile, path: Path) -> None:
    atomic_write_text(path, dump_lock(lockfile))


def _parse_pin(entry: object, *, lock_version: int) -> ResolvedPin:
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

    artifact_raw = entry.get("artifact")
    if artifact_raw is not None and lock_version < 2:
        raise MalformedLockError(
            "setforge.lock: lock format v1 does not support platform artifacts"
        )
    artifacts = _parse_artifacts(artifact_raw, key=key, pkg_type=pkg_type)
    present = [field for field in _FIELD_KIND if field in entry]
    if artifacts and present:
        raise MalformedLockError(
            f"setforge.lock: package {key!r} carries both artifact tables and "
            "a top-level integrity field"
        )
    if not present and not artifacts:
        raise MalformedLockError(
            f"setforge.lock: package {key!r} has no integrity field "
            f"(one of {', '.join(_FIELD_KIND)} required)"
        )
    if len(present) > 1:
        raise MalformedLockError(
            f"setforge.lock: package {key!r} carries multiple integrity fields "
            f"({', '.join(present)}); exactly one is allowed"
        )
    field = present[0] if present else None
    if field is not None:
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
        integrity=(
            _require(entry, field)
            if field is not None
            else artifact_set_integrity(artifacts)
        ),
        integrity_kind=(
            _FIELD_KIND[field] if field is not None else IntegrityKind.CHECKSUM
        ),
        profiles=tuple(profiles_raw),
        artifacts=artifacts,
    )


def _parse_artifacts(
    raw: object, *, key: str, pkg_type: PackageType
) -> tuple[ResolvedArtifact, ...]:
    if raw is None:
        return ()
    if pkg_type is not PackageType.GITHUB_RELEASE:
        raise MalformedLockError(
            f"setforge.lock: package {key!r} is not github_release but carries "
            "platform artifacts"
        )
    if not isinstance(raw, list) or not raw:
        raise MalformedLockError(
            f"setforge.lock: package {key!r} 'artifact' must be a non-empty "
            "array of tables"
        )
    parsed: list[ResolvedArtifact] = []
    for row in raw:
        if not isinstance(row, dict) or set(row) != {"os", "arch", "asset", "checksum"}:
            raise MalformedLockError(
                f"setforge.lock: package {key!r} artifact must contain exactly "
                "os, arch, asset, and checksum"
            )
        try:
            parsed.append(
                ResolvedArtifact(
                    os=_require(row, "os"),
                    arch=_require(row, "arch"),
                    asset=_require(row, "asset"),
                    checksum=_require(row, "checksum"),
                )
            )
        except ValueError as exc:
            raise MalformedLockError(
                f"setforge.lock: package {key!r} has invalid artifact: {exc}"
            ) from exc
    ordered = tuple(sorted(parsed, key=ResolvedArtifact.sort_key))
    if tuple(parsed) != ordered or len({(a.os, a.arch) for a in parsed}) != len(parsed):
        raise MalformedLockError(
            f"setforge.lock: package {key!r} artifacts must be sorted and unique "
            "by os/arch"
        )
    return ordered


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
