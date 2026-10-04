"""The two ``orphan_ignore`` / ``provision_ignore`` writers must not truncate
``local.yaml`` on a crash and must keep the file's comments and layout."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from setforge import paths
from setforge.cli import cleanup as cleanup_mod
from setforge.cli import orphans as orphans_mod
from setforge.migrations import _yaml_ops
from setforge.provision.protocol import Identity

_ORIGINAL = (
    "# my host config\n"
    "source:\n"
    "    url: https://example.invalid/repo.git  # pinned\n"
    "orphan_ignore:\n"
    "    - old\n"
)


def _write_cleanup(path: Path) -> None:
    cleanup_mod.mark_orphan(Identity(key="tool", display="tool"), console=Console())


def _write_orphans(path: Path) -> None:
    orphans_mod._append_ignored_orphan("tool")


@pytest.mark.parametrize(
    ("module", "writer"), [(cleanup_mod, _write_cleanup), (orphans_mod, _write_orphans)]
)
def test_local_yaml_writer_uses_atomic_primitive(
    module: object, writer: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = paths.local_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_ORIGINAL, encoding="utf-8")
    calls: list[Path] = []
    real = _yaml_ops.atomic_write_yaml

    def spy(yaml_path: Path, data: object, **kwargs: object) -> None:
        calls.append(yaml_path)
        real(yaml_path, data, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "atomic_write_yaml", spy, raising=False)
    writer(path)  # type: ignore[operator]

    assert calls == [path]
    text = path.read_text(encoding="utf-8")
    assert "# my host config" in text
    assert "# pinned" in text
    assert "    - old\n" in text
    assert "tool" in text


def test_orphans_writer_creates_missing_file(tmp_path: Path) -> None:
    path = paths.local_config_path()
    assert not path.exists()
    orphans_mod._append_ignored_orphan("tool")
    assert "orphan_ignore" in path.read_text(encoding="utf-8")
