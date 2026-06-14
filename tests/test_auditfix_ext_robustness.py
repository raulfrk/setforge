"""Regression tests for audit finding `ext_robustness`.

Two distinct hardening fixes in :mod:`setforge.vscode_extensions`:

1. **subprocess OSError degradation.** A ``which()``-resolved ``code``
   binary can still fail to exec (broken Remote-SSH wrapper, removed in
   the TOCTOU window, bad shebang). ``subprocess.run`` then raises
   ``OSError`` (FileNotFoundError / PermissionError), NOT
   ``CalledProcessError`` / ``TimeoutExpired``. Pre-fix that escaped the
   per-module handlers and crashed ``setforge install`` with a raw
   traceback. Each ``code`` site now catches ``OSError`` too —
   ``ExtensionInstallFailed`` for hard calls, ``report.failed`` for the
   per-extension loop.

2. **atomic config writes.** The ``ext``-subcommand YAML mutators wrote
   ``setforge.yaml`` via a plain truncating ``open("w")`` + ``yaml.dump``.
   A crash / serialization error mid-dump left the single source of truth
   truncated. They now serialize to a buffer and write via
   :func:`atomicio.atomic_write_text`, so a failed dump never truncates
   the live file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from setforge import vscode_extensions
from setforge.config import Extensions, ReconcilePolicy
from setforge.errors import ExtensionInstallFailed
from setforge.vscode_extensions import (
    add_to_include,
    capture_extensions,
    install_one,
    list_installed,
    reconcile,
    remove_from_include,
    uninstall_one,
)

# --------------------------------------------------------------------------
# 1. subprocess OSError → ExtensionInstallFailed / report.failed (not raw)
# --------------------------------------------------------------------------


@pytest.fixture
def _fake_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``_ensure_code`` resolve to a fake path without touching PATH."""
    monkeypatch.setattr(
        vscode_extensions, "resolve_binary", lambda _name: Path("/fake/code")
    )


@pytest.mark.usefixtures("_fake_code")
@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("code vanished"),
        PermissionError("code not executable"),
    ],
)
def test_list_installed_oserror_becomes_extension_install_failed(
    monkeypatch: pytest.MonkeyPatch,
    exc: OSError,
) -> None:
    def _raise(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise exc

    monkeypatch.setattr(vscode_extensions.subprocess, "run", _raise)

    with pytest.raises(ExtensionInstallFailed) as excinfo:
        list_installed()
    assert "list-extensions" in str(excinfo.value)


@pytest.mark.usefixtures("_fake_code")
@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("code vanished"),
        PermissionError("code not executable"),
    ],
)
def test_install_one_oserror_becomes_extension_install_failed(
    monkeypatch: pytest.MonkeyPatch,
    exc: OSError,
) -> None:
    def _raise(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise exc

    monkeypatch.setattr(vscode_extensions.subprocess, "run", _raise)

    with pytest.raises(ExtensionInstallFailed) as excinfo:
        install_one("vendor.ext")
    assert "vendor.ext" in str(excinfo.value)


@pytest.mark.usefixtures("_fake_code")
def test_uninstall_one_oserror_becomes_extension_install_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise PermissionError("code not executable")

    monkeypatch.setattr(vscode_extensions.subprocess, "run", _raise)

    with pytest.raises(ExtensionInstallFailed) as excinfo:
        uninstall_one("vendor.ext")
    assert "vendor.ext" in str(excinfo.value)


@pytest.mark.usefixtures("_fake_code")
def test_reconcile_install_oserror_recorded_in_failed_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exec failure on a per-extension install must land in
    ``report.failed`` so the loop keeps going — not escape as raw OSError."""

    def _run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        if "--list-extensions" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="")
        raise FileNotFoundError("code vanished mid-install")

    monkeypatch.setattr(vscode_extensions.subprocess, "run", _run)

    report = reconcile(
        Extensions(
            include=["vendor.ext"],
            exclude=[],
            reconcile=ReconcilePolicy.ADDITIVE,
        )
    )

    assert report.to_install == ["vendor.ext"]
    assert len(report.failed) == 1
    assert report.failed[0][0] == "vendor.ext"


@pytest.mark.usefixtures("_fake_code")
def test_reconcile_uninstall_oserror_recorded_in_failed_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        if "--list-extensions" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="extra.ext\n")
        raise PermissionError("code not executable")

    monkeypatch.setattr(vscode_extensions.subprocess, "run", _run)

    report = reconcile(
        Extensions(
            include=[],
            exclude=[],
            reconcile=ReconcilePolicy.PRUNE,
        )
    )

    assert report.to_uninstall == ["extra.ext"]
    assert len(report.failed) == 1
    assert report.failed[0][0] == "extra.ext"


# --------------------------------------------------------------------------
# 2. atomic config writes — a dump that fails mid-write never truncates
# --------------------------------------------------------------------------

_FIXTURE = """\
version: 1
tracked_files:
  d: {src: x, dst: y}
profiles:
  main:
    tracked_files: [d]
    extensions:
      include:
        - existing.ext
"""


def _write_config(tmp_path: Path) -> Path:
    p = tmp_path / "setforge.yaml"
    p.write_text(_FIXTURE, encoding="utf-8")
    return p


def test_add_to_include_dump_failure_does_not_truncate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_config(tmp_path)
    original = config_path.read_text(encoding="utf-8")

    def _boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("serialization exploded mid-dump")

    # Patch the YAML serializer so the dump fails after the helper has
    # already decided to write. Pre-fix (truncating open) this would leave
    # setforge.yaml empty; post-fix (buffer + atomic_write_text) the
    # in-memory dump fails before any on-disk replace.
    monkeypatch.setattr(vscode_extensions.YAML, "dump", _boom)

    with pytest.raises(RuntimeError):
        add_to_include(config_path, "main", "new.ext")

    assert config_path.read_text(encoding="utf-8") == original


def test_capture_extensions_dump_failure_does_not_truncate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_config(tmp_path)
    original = config_path.read_text(encoding="utf-8")

    monkeypatch.setattr(vscode_extensions, "list_installed", lambda: {"some.other-ext"})

    def _boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("serialization exploded mid-dump")

    monkeypatch.setattr(vscode_extensions.YAML, "dump", _boom)

    with pytest.raises(RuntimeError):
        capture_extensions(config_path, "main")

    assert config_path.read_text(encoding="utf-8") == original


def test_remove_from_include_dump_failure_does_not_truncate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_config(tmp_path)
    original = config_path.read_text(encoding="utf-8")

    def _boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("serialization exploded mid-dump")

    monkeypatch.setattr(vscode_extensions.YAML, "dump", _boom)

    with pytest.raises(RuntimeError):
        remove_from_include(config_path, "main", "existing.ext")

    assert config_path.read_text(encoding="utf-8") == original


def test_add_to_include_success_writes_updated_content(tmp_path: Path) -> None:
    """The happy path still produces the expected updated YAML on disk."""
    config_path = _write_config(tmp_path)

    added = add_to_include(config_path, "main", "new.ext")

    assert added is True
    text = config_path.read_text(encoding="utf-8")
    assert "new.ext" in text
    assert "existing.ext" in text
