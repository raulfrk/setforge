"""The resolver protocol surface (spec §B2).

Mirrors :mod:`setforge.provision.protocol` (the ``Provisioner`` ABC) but for
the read-only resolution half of the split (D6): a :class:`Resolver` queries
upstream to produce a :class:`ResolvedPin` — a concrete version + the
ecosystem-natural integrity value — WITHOUT mutating the host. The lock file
(:mod:`setforge.lockfile`) is the serialized set of these pins.

Integrity is modelled as one value + a discriminating ``kind`` rather than
three optional fields, so a pin can never carry two integrity values at once
and the TOML column name is derived from ``kind`` (:data:`_KIND_FIELD`):

    * ``CHECKSUM`` -> ``checksum = "sha256:..."`` (cargo/github_release/ext/python)
    * ``SUM``      -> ``sum = "h1:..."``          (go, via the module sumdb)
    * ``SHA``      -> ``sha = "<40-hex>"``         (plugin, a git commit)
"""

from enum import StrEnum
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class PackageType(StrEnum):
    """The ecosystems a resolver + lock pin can cover (spec §B3).

    These are the registry keys for the resolver registry, matching the
    ``@register("type")`` strings the provisioners already use. ``local`` and
    ``reference`` provisioners are intentionally absent — they resolve nothing
    upstream, so they never appear in the lock.
    """

    CARGO = "cargo"
    PYTHON = "python"
    GO = "go"
    GITHUB_RELEASE = "github_release"
    EXTENSION = "extension"
    PLUGIN = "plugin"


class IntegrityKind(StrEnum):
    """Which integrity field an ecosystem's pin carries.

    The value discriminates the TOML column name (:data:`_KIND_FIELD`) so a
    pin serializes to exactly one of ``checksum``/``sum``/``sha``.
    """

    CHECKSUM = "checksum"  # sha256:... — cargo, github_release, extension, python
    SUM = "sum"  # h1:... — go module sumdb hash
    SHA = "sha"  # 40-char git commit — plugin marketplace pin


# The TOML column each integrity kind serializes to. The enum VALUE already IS
# the column name, but the explicit mapping documents the contract and gives
# the (de)serializer a single lookup point.
_KIND_FIELD: dict[IntegrityKind, str] = {
    IntegrityKind.CHECKSUM: "checksum",
    IntegrityKind.SUM: "sum",
    IntegrityKind.SHA: "sha",
}


class ResolvedPin(BaseModel):
    """One resolved package pin — the atom the lock file is built from.

    ``type``/``key``/``version`` identify a concrete (never ``latest``)
    package; ``integrity`` + ``integrity_kind`` carry the ecosystem-natural
    verification value; ``profiles`` is the back-ref naming which profiles
    declared this package (D8).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: PackageType
    key: str
    version: str
    integrity: str
    integrity_kind: IntegrityKind
    profiles: tuple[str, ...] = Field(default_factory=tuple)

    def sort_key(self) -> tuple[str, str]:
        """The stable ``(type, key)`` ordering key (spec §B1 determinism)."""
        return (self.type.value, self.key)

    def to_lock_entry(self) -> dict[str, object]:
        """Render this pin as one ``[[package]]`` TOML table.

        The integrity value lands under its ecosystem-natural column
        (``checksum``/``sum``/``sha``) per :data:`_KIND_FIELD`, and ``profiles``
        is emitted as a sorted list so the serialized bytes are input-order
        independent.
        """
        return {
            "type": self.type.value,
            "key": self.key,
            "version": self.version,
            _KIND_FIELD[self.integrity_kind]: self.integrity,
            "profiles": sorted(self.profiles),
        }


@runtime_checkable
class Resolver(Protocol):
    """The read-only resolution contract (mirrors the ``Provisioner`` ABC).

    An implementation queries upstream for the concrete version + integrity of
    ``item`` and returns a :class:`ResolvedPin`. It performs NO host mutation —
    that is the provisioner's job — so ``setforge lock`` can run without ever
    touching the install path.
    """

    type: ClassVar[PackageType]

    def resolve(self, item: object) -> ResolvedPin:
        """Query upstream and return the resolved pin for ``item``.

        ``item`` is the declared package (a ``ProvisionItem``-shaped value);
        typed ``object`` here because the concrete per-ecosystem resolvers
        (later tasks) narrow it themselves. Must not write to the host.
        """
        ...
