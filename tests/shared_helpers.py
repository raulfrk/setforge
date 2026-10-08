"""Helpers that were byte-identical copies across several test modules."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ruamel.yaml import YAML

from setforge.migrations import MigrationRoots
from setforge.ownership_history import OwnershipHistoryStore, OwnershipTransition
from setforge.transitions import (
    FilesystemImage,
    FilesystemKind,
    TransitionDir,
    load_filesystem_deltas,
)


def migration_roots(tmp_path: Path) -> MigrationRoots:
    return MigrationRoots(
        cfg_path=tmp_path / "setforge.yaml",
        repo_root=tmp_path,
        home=tmp_path / "home",
    )


def load_yaml(path: Path) -> dict:
    yaml = YAML(typ="rt")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.load(fh)


def write_setforge_yaml(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def text_images(texts: Mapping[Path, str | None]) -> dict[Path, FilesystemImage]:
    """Synthetic ``write_transition`` images of file texts; ``None`` is absent."""
    return {
        path: FilesystemImage(FilesystemKind.ABSENT)
        if text is None
        else FilesystemImage(
            FilesystemKind.FILE,
            payload=text.encode("utf-8", "surrogateescape"),
            mode=0o644,
            mtime_ns=0,
        )
        for path, text in texts.items()
    }


def file_images(
    transition: TransitionDir,
) -> dict[Path, tuple[bytes | None, bytes | None]]:
    """The recorded pre/post bytes of every path; ``None`` is absent."""
    return {
        item.path: (item.pre.payload, item.post.payload)
        for item in load_filesystem_deltas(transition)
    }


def legacy_crash_log(
    history: OwnershipHistoryStore,
    transition: OwnershipTransition,
    claim_before: tuple[Path, bytes] | None = None,
) -> Path:
    """Turn a finished transition into one SetForge 1.4.0 or earlier left unfinished.

    Those releases wrote ``pending/<id>.json`` first, then the claim, then the
    history record. ``claim_before`` puts the claim file back as well, for a
    crash before the claim was written.
    """
    owner_root = history.root / str(transition.owner_id)
    (owner_root / "pending").mkdir(mode=0o700)
    name = f"{transition.transition_id}.json"
    log = owner_root / "pending" / name
    (owner_root / "transitions" / name).rename(log)
    if claim_before is not None:
        claim_before[0].write_bytes(claim_before[1])
    return log
