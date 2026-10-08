"""The private project-injection record: its schemas and its one strict reader."""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from setforge.config import ProjectVisibility
from setforge.errors import SetforgeError

_MANIFEST_SCHEMA = 3
# Older formats: only conversion and dropping a stale record still read them.
_PRIOR_MANIFEST_SCHEMA = 2
_LEGACY_MANIFEST_SCHEMA = 1


class ProjectFileAction(StrEnum):
    """One fully preflighted destination effect."""

    CREATE = "create"
    RETAIN = "retain-identical"
    REPLACE = "replace-untracked"
    OVERLAY = "overlay-tracked"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredProjectFile:
    """Strict file state decoded from one injection manifest."""

    file_id: str
    declaring_profile: str
    source: Path
    destination: Path
    action: ProjectFileAction
    applied_payload: bytes | None
    applied_digest: str | None
    applied_mode: int | None
    upstream_payload: bytes
    upstream_mode: int
    previous_payload: bytes | None
    previous_mode: int | None
    created_parents: tuple[Path, ...]
    visibility: ProjectVisibility
    source_digest: str


def _decode_payload(value: object, *, field: str) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SetforgeError(f"project injection state has invalid {field}")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SetforgeError(f"project injection state has invalid {field}") from exc


def _valid_mode(value: object) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0o7777
    )


def _record_files(  # noqa: C901 - one fail-closed parser for untrusted state
    raw: dict[str, object], *, target: Path
) -> tuple[StoredProjectFile, ...]:
    """Decode and validate every file entry of one current-format record.

    A malformed or inconsistent entry is refused before any caller acts on
    the record.
    """
    raw_files = raw["files"]
    assert isinstance(raw_files, list)
    files: list[StoredProjectFile] = []
    destinations: set[Path] = set()
    file_ids: set[str] = set()
    fields = {
        "action",
        "applied_digest",
        "applied_mode",
        "applied_payload",
        "created_parents",
        "declaring_profile",
        "destination",
        "file_id",
        "previous_mode",
        "previous_payload",
        "source",
        "source_digest",
        "upstream_mode",
        "upstream_payload",
        "visibility",
    }
    for entry in raw_files:
        if not isinstance(entry, dict) or set(entry) != fields:
            raise SetforgeError("project injection state has invalid file fields")
        try:
            relative = Path(entry["destination"])
            file_id = entry["file_id"]
            declaring_profile = entry["declaring_profile"]
            source = Path(entry["source"])
            action = ProjectFileAction(entry["action"])
            applied_digest = entry["applied_digest"]
            source_digest = entry["source_digest"]
            visibility = ProjectVisibility(str(entry["visibility"]))
            applied_mode = entry["applied_mode"]
            previous_mode = entry["previous_mode"]
            created_parents_raw = entry["created_parents"]
        except (KeyError, TypeError, ValueError) as exc:
            raise SetforgeError(
                "project injection state has an invalid file record"
            ) from exc
        if (
            not isinstance(file_id, str)
            or not isinstance(declaring_profile, str)
            or not declaring_profile
            or not isinstance(source_digest, str)
            or not isinstance(created_parents_raw, list)
            or relative.is_absolute()
            or relative == Path()
            or ".." in relative.parts
            or relative in destinations
            or file_id in file_ids
        ):
            raise SetforgeError("project injection state has an invalid file record")
        if previous_mode is not None and not _valid_mode(previous_mode):
            raise SetforgeError("project injection state has an invalid file record")
        previous_payload = _decode_payload(
            entry["previous_payload"], field="previous payload"
        )
        applied_payload = _decode_payload(
            entry["applied_payload"], field="applied payload"
        )
        upstream_payload = _decode_payload(
            entry["upstream_payload"], field="upstream payload"
        )
        upstream_mode = entry["upstream_mode"]
        applied_absent = (
            applied_payload is None and applied_digest is None and applied_mode is None
        )
        applied_present = (
            applied_payload is not None
            and isinstance(applied_digest, str)
            and _valid_mode(applied_mode)
        )
        if (
            (not applied_absent and not applied_present)
            or upstream_payload is None
            or not _valid_mode(upstream_mode)
            or (
                applied_payload is not None
                and _sha256(applied_payload) != applied_digest
            )
            or _sha256(upstream_payload) != source_digest
        ):
            raise SetforgeError(
                "project injection state has an inconsistent file record"
            )
        assert isinstance(upstream_mode, int)
        baseline_absent = previous_payload is None and previous_mode is None
        baseline_present = previous_payload is not None and previous_mode is not None
        if (action is ProjectFileAction.CREATE and not baseline_absent) or (
            action is not ProjectFileAction.CREATE and not baseline_present
        ):
            raise SetforgeError(
                "project injection state has an inconsistent file record"
            )
        parents: list[Path] = []
        for value in created_parents_raw:
            if not isinstance(value, str):
                raise SetforgeError(
                    "project injection state has an invalid parent record"
                )
            parent_relative = Path(value)
            parent = target / parent_relative
            destination = target / relative
            if (
                parent_relative.is_absolute()
                or ".." in parent_relative.parts
                or parent == target
                or parent not in destination.parents
                or parent in parents
            ):
                raise SetforgeError(
                    "project injection state has an invalid parent record"
                )
            parents.append(parent)
        destinations.add(relative)
        file_ids.add(file_id)
        files.append(
            StoredProjectFile(
                file_id=file_id,
                declaring_profile=declaring_profile,
                source=source,
                destination=relative,
                action=action,
                applied_payload=applied_payload,
                applied_digest=applied_digest,
                applied_mode=applied_mode,
                upstream_payload=upstream_payload,
                upstream_mode=upstream_mode,
                previous_payload=previous_payload,
                previous_mode=previous_mode,
                created_parents=tuple(parents),
                visibility=visibility,
                source_digest=source_digest,
            )
        )
    return tuple(files)
