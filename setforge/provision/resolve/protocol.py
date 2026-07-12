"""The resolver protocol surface: read-only resolution (D6), no host mutation."""

from enum import StrEnum
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class PackageType(StrEnum):
    CARGO = "cargo"
    PYTHON = "python"
    GO = "go"
    GITHUB_RELEASE = "github_release"
    EXTENSION = "extension"
    PLUGIN = "plugin"


class IntegrityKind(StrEnum):
    CHECKSUM = "checksum"
    SUM = "sum"
    SHA = "sha"


_KIND_FIELD: dict[IntegrityKind, str] = {
    IntegrityKind.CHECKSUM: "checksum",
    IntegrityKind.SUM: "sum",
    IntegrityKind.SHA: "sha",
}


class ResolvedPin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: PackageType
    key: str
    version: str
    integrity: str
    integrity_kind: IntegrityKind
    profiles: tuple[str, ...] = Field(default_factory=tuple)

    def sort_key(self) -> tuple[str, str]:
        return (self.type.value, self.key)

    def to_lock_entry(self) -> dict[str, object]:
        return {
            "type": self.type.value,
            "key": self.key,
            "version": self.version,
            _KIND_FIELD[self.integrity_kind]: self.integrity,
            "profiles": sorted(self.profiles),
        }


@runtime_checkable
class Resolver(Protocol):
    """Read-only resolution contract: query upstream, never mutate the host."""

    type: ClassVar[PackageType]

    def resolve(self, item: object) -> ResolvedPin: ...
