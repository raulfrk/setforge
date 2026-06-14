"""Regression: capture writes the tracked source atomically.

A crash (SIGTERM / power loss / ENOSPC) mid-writeback must never leave
the tracked source-of-truth truncated or half-written. ``_write_if_changed``
must route through ``atomicio.atomic_write_text`` (tempfile + os.replace),
so an injected failure leaves the original tracked bytes intact and no
``.tmp`` debris behind.
"""

from pathlib import Path

import pytest

from setforge import atomicio, capture
from setforge.capture import CaptureAction, _write_if_changed


def test_write_if_changed_is_atomic_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "tracked" / "CLAUDE.md"
    src.parent.mkdir(parents=True)
    original = "original tracked bytes\n"
    src.write_text(original, encoding="utf-8")

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("injected mid-write failure")

    # os.replace is the publish step; failing it simulates a crash after the
    # temp file is written but before the destination is swapped.
    monkeypatch.setattr("setforge.atomicio.os.replace", boom)

    with pytest.raises(OSError, match="injected mid-write failure"):
        _write_if_changed(src, "new content that must NOT land\n")

    # All-or-nothing: original survives untruncated.
    assert src.read_text(encoding="utf-8") == original


def test_write_if_changed_leaves_no_tmp_debris_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "tracked" / "CLAUDE.md"
    src.parent.mkdir(parents=True)
    original = "original\n"
    src.write_text(original, encoding="utf-8")

    monkeypatch.setattr(
        "setforge.atomicio.os.replace",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("boom")),
    )

    with pytest.raises(OSError, match="boom"):
        _write_if_changed(src, "new\n")

    # atomicio unlinks its temp file on failure: only the original remains.
    survivors = sorted(p.name for p in src.parent.iterdir())
    assert survivors == ["CLAUDE.md"]
    assert src.read_text(encoding="utf-8") == original


def test_write_if_changed_uses_atomic_write_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Routes through the shared atomic primitive, not bare write_text."""
    src = tmp_path / "tracked" / "CLAUDE.md"
    src.parent.mkdir(parents=True)
    src.write_text("old\n", encoding="utf-8")

    called: dict[str, object] = {}
    real = atomicio.atomic_write_text

    def spy(
        path: Path,
        text: str,
        *,
        encoding: str = "utf-8",
        fsync: bool = True,
        mode: int | None = None,
        backup: bool = False,
    ) -> Path | None:
        called["path"] = path
        called["text"] = text
        return real(
            path,
            text,
            encoding=encoding,
            fsync=fsync,
            mode=mode,
            backup=backup,
        )

    monkeypatch.setattr(capture.atomicio, "atomic_write_text", spy)

    result = _write_if_changed(src, "new\n")

    assert result.action is CaptureAction.UPDATED
    assert called["path"] == src
    assert called["text"] == "new\n"
    assert src.read_text(encoding="utf-8") == "new\n"


def test_write_if_changed_noop_skips_write(tmp_path: Path) -> None:
    src = tmp_path / "tracked" / "CLAUDE.md"
    src.parent.mkdir(parents=True)
    src.write_text("same\n", encoding="utf-8")

    result = _write_if_changed(src, "same\n")

    assert result.action is CaptureAction.NOOP
    assert src.read_text(encoding="utf-8") == "same\n"
