"""The resolver protocol surface: read-only resolution (D6), no host mutation."""

import hashlib
import json
from enum import StrEnum
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class ResolvedArtifact(BaseModel):
    """One canonical platform-specific release artifact stored in lock v2."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    os: str
    arch: str
    asset: str
    checksum: str

    @field_validator("os")
    @classmethod
    def _canonical_os(cls, value: str) -> str:
        if value not in {"*", "linux", "macos"}:
            raise ValueError("artifact os must be canonical")
        return value

    @field_validator("arch")
    @classmethod
    def _canonical_arch(cls, value: str) -> str:
        if value not in {"*", "x86_64", "aarch64"}:
            raise ValueError("artifact arch must be canonical")
        return value

    @field_validator("asset")
    @classmethod
    def _asset_is_bare(cls, value: str) -> str:
        if not value or "/" in value or value in {".", ".."}:
            raise ValueError("artifact asset must be a non-empty bare name")
        return value

    @field_validator("checksum")
    @classmethod
    def _checksum_is_sha256(cls, value: str) -> str:
        prefix, separator, digest = value.partition(":")
        if (
            prefix != "sha256"
            or not separator
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError("artifact checksum must be canonical sha256")
        return value

    def sort_key(self) -> tuple[str, str, str]:
        return (self.os, self.arch, self.asset)

    def to_lock_entry(self) -> dict[str, str]:
        return {
            "os": self.os,
            "arch": self.arch,
            "asset": self.asset,
            "checksum": self.checksum,
        }


def artifact_set_integrity(artifacts: tuple[ResolvedArtifact, ...]) -> str:
    """Return the host-neutral integrity witness for a canonical artifact set."""
    canonical = json.dumps(
        [item.to_lock_entry() for item in artifacts],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


class ResolvedPin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: PackageType
    key: str
    version: str
    integrity: str
    integrity_kind: IntegrityKind
    profiles: tuple[str, ...] = Field(default_factory=tuple)
    artifacts: tuple[ResolvedArtifact, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _artifacts_are_canonical(self) -> "ResolvedPin":
        if self.artifacts and self.type is not PackageType.GITHUB_RELEASE:
            raise ValueError("only github_release pins may carry platform artifacts")
        if self.artifacts and self.integrity_kind is not IntegrityKind.CHECKSUM:
            raise ValueError("platform artifact pins require checksum integrity")
        ordered = tuple(sorted(self.artifacts, key=ResolvedArtifact.sort_key))
        if self.artifacts != ordered or len(
            {(item.os, item.arch) for item in self.artifacts}
        ) != len(self.artifacts):
            raise ValueError(
                "platform artifacts must be sorted and unique by canonical os/arch"
            )
        if self.artifacts and self.integrity != artifact_set_integrity(self.artifacts):
            raise ValueError("platform artifact-set integrity witness does not match")
        return self

    def sort_key(self) -> tuple[str, str]:
        return (self.type.value, self.key)

    def to_lock_entry(self) -> dict[str, object]:
        entry: dict[str, object] = {
            "type": self.type.value,
            "key": self.key,
            "version": self.version,
            "profiles": sorted(self.profiles),
        }
        if self.artifacts:
            entry["artifact"] = [item.to_lock_entry() for item in self.artifacts]
        else:
            entry[_KIND_FIELD[self.integrity_kind]] = self.integrity
        return entry


@runtime_checkable
class Resolver(Protocol):
    """Read-only resolution contract: query upstream, never mutate the host."""

    type: ClassVar[PackageType]

    def resolve(self, item: object) -> ResolvedPin: ...
