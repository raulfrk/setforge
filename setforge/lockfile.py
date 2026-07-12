"""The committed ``setforge.lock`` model + (de)serialization (spec §B1).

The lock is a resolved-graph artifact — one pin per ``(type, key)`` with the
ecosystem-natural integrity value + a ``profiles`` back-ref (D8) — committed at
the CONFIG-REPO ROOT (``config.resolve().parent / "setforge.lock"``), NOT under
the host-local state dir (that is the receipt store). It carries its own
``version`` independent of the config ``schema_version`` (D8), so it is a
SEPARATE file with a SEPARATE schema and does not go through the config-schema
gates.

``dump_lock`` is DETERMINISTIC (spec §C anti-pitfall): pins are sorted by
``(type, key)`` and ``profiles`` lists are sorted, so shuffling the input pin
list yields byte-identical output; there is no timestamp/host/duration field.
``write_lock`` lands the bytes via :func:`setforge.atomicio.atomic_write_text`
(never a raw ``write_text``), mirroring the receipt store's durability
discipline but for TOML.
"""

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

# The TOML column -> integrity kind, inverted from _KIND_FIELD so the parser
# can discover which single integrity field a pin table carries.
_FIELD_KIND: dict[str, IntegrityKind] = {v: k for k, v in _KIND_FIELD.items()}


class LockFile(BaseModel):
    """The parsed ``setforge.lock``: a format ``version`` + a set of pins.

    Equality is structural (frozen pydantic model), so the round-trip test
    ``parse_lock(dump_lock(lf)) == lf`` compares by value. Pins are held in the
    order supplied; :meth:`dump` imposes the deterministic ordering, so two
    ``LockFile``s with the same pins in a different order are NOT ``==`` but
    serialize identically.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = LOCK_VERSION
    packages: tuple[ResolvedPin, ...] = Field(default_factory=tuple)


def lock_path(config_path: Path) -> Path:
    """Return the committed lock path for a given config file.

    ``config.resolve().parent / "setforge.lock"`` — the config-repo root, per
    spec §B1. Resolving first collapses symlinks so the lock lands beside the
    real config, not beside a symlink to it.
    """
    return config_path.resolve().parent / LOCK_FILENAME


def dump_lock(lockfile: LockFile) -> str:
    """Serialize ``lockfile`` to deterministic TOML text.

    Pins are sorted by ``(type, key)`` and each pin's ``profiles`` is sorted,
    so the output is byte-identical for equal pin SETS regardless of input
    order. No timestamp/host/duration is emitted. ``tomli_w`` does the
    canonical formatting.
    """
    packages = [
        pin.to_lock_entry()
        for pin in sorted(lockfile.packages, key=ResolvedPin.sort_key)
    ]
    document: dict[str, object] = {"version": lockfile.version, "package": packages}
    return tomli_w.dumps(document)


def parse_lock(text: str) -> LockFile:
    """Parse ``setforge.lock`` TOML text into a :class:`LockFile`.

    Raises :class:`~setforge.errors.MalformedLockError` (a clean message, never
    a raw traceback) on: unparseable TOML, a missing/non-int ``version``, a
    non-array ``package``, a pin missing ``type``/``key``/``version``, an
    unknown ``type``, or an integrity shape that is absent, doubled, or does
    not match the ecosystem's expected column.
    """
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
    """Atomically write ``lockfile`` to ``path`` (spec §B1 atomic write).

    Delegates to :func:`~setforge.atomicio.atomic_write_text` so a crash
    mid-write never leaves a torn lock — the same discipline the receipt store
    uses, but for the committed TOML lock. The bytes are the deterministic
    :func:`dump_lock` output.
    """
    atomic_write_text(path, dump_lock(lockfile))


def _parse_pin(entry: object) -> ResolvedPin:
    """Parse one ``[[package]]`` table into a :class:`ResolvedPin`.

    Enforces the integrity discipline: exactly ONE of ``checksum``/``sum``/
    ``sha`` must be present, and it must be the column the pin's ecosystem
    uses. All failures raise :class:`~setforge.errors.MalformedLockError`.
    """
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
    """The integrity kind an ecosystem's pin must carry (spec §B3).

    go uses the sumdb ``h1:`` hash; plugin uses a git-commit ``sha``; every
    other ecosystem uses a ``sha256:`` ``checksum``.
    """
    if pkg_type is PackageType.GO:
        return IntegrityKind.SUM
    if pkg_type is PackageType.PLUGIN:
        return IntegrityKind.SHA
    return IntegrityKind.CHECKSUM


def _require(entry: dict[str, object], field: str) -> str:
    """Return ``entry[field]`` as a non-empty ``str`` or raise.

    A mandatory scalar field that is missing, non-string, or empty is a
    malformed lock, not a legitimately-absent value.
    """
    value = entry.get(field)
    if not isinstance(value, str) or not value:
        raise MalformedLockError(
            f"setforge.lock: package field {field!r} must be a non-empty "
            f"string, got {value!r}"
        )
    return value
