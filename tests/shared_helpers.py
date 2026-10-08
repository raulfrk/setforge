"""Helpers that were byte-identical copies across several test modules."""

from __future__ import annotations

import errno
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

import pytest
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


def crash() -> NoReturn:
    """A write hook that stops the write the way a killed process would."""
    raise OSError(errno.EIO, "injected crash")


@contextmanager
def at_record_write(
    monkeypatch: pytest.MonkeyPatch,
    step: int,
    *,
    before: Callable[[], None] | None = None,
    after: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Run ``before`` or ``after`` around the ``step``-th record write (1-based).

    A record write publishes (``os.replace``) or removes (``os.unlink``) a
    ``*.json`` record through a directory descriptor, which is how ownership
    claims, move intents and history transitions reach disk. Staging a
    temporary file does not count. ``before=crash`` leaves the store as if the
    process died just before that write; ``after=crash`` as if it died just
    after. The hooks are active only inside the ``with`` block.
    """
    real_replace, real_unlink = os.replace, os.unlink
    writes = 0

    def hooked(name: object, write: Callable[[], None]) -> None:
        nonlocal writes
        if not (
            isinstance(name, str)
            and name.endswith(".json")
            and not name.startswith(".")
        ):
            write()
            return
        writes += 1
        mine = writes == step
        if mine and before is not None:
            before()
        write()
        if mine and after is not None:
            after()

    def replace(
        src: str,
        dst: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        def write() -> None:
            real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        hooked(dst if dst_dir_fd is not None else None, write)

    def unlink(path: str, *, dir_fd: int | None = None) -> None:
        def write() -> None:
            real_unlink(path, dir_fd=dir_fd)

        hooked(path if dir_fd is not None else None, write)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace)
        patch.setattr(os, "unlink", unlink)
        yield
