"""Helpers that were byte-identical copies across several test modules."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ruamel.yaml import YAML

from setforge.migrations import MigrationRoots
from setforge.transitions import (
    FilesystemImage,
    FilesystemKind,
    TransitionDir,
    compute_patch,
    load_file_modes,
    load_filesystem_deltas,
    snapshot_paths,
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


def record_transition(
    tmp_path: Path, live: Path, before: bytes, after: bytes
) -> TransitionDir:
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(before)
    pre = snapshot_paths([live])
    live.write_bytes(after)
    post = snapshot_paths([live])
    transition = TransitionDir(tmp_path / "transition")
    transition.mkdir()
    (transition / "changes.patch").write_text(compute_patch(pre, post))
    return transition


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


def assert_patch_matches_images(transition: TransitionDir) -> None:
    """The record's patch and mode map say exactly what its file images say."""
    deltas = {item.path: item for item in load_filesystem_deltas(transition)}
    texts = [
        {
            path: (
                payload.decode("utf-8", "surrogateescape")
                if (payload := image.payload) is not None
                else None
            )
            for path, image in (
                (path, getattr(item, side)) for path, item in deltas.items()
            )
        }
        for side in ("pre", "post")
    ]
    patch_file = transition / "changes.patch"
    recorded = (
        patch_file.read_bytes().decode("utf-8", "surrogateescape")
        if patch_file.exists()
        else ""
    )
    assert compute_patch(*texts) == recorded
    for path, mode in load_file_modes(transition).items():
        assert deltas[path].pre.mode == mode
