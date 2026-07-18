"""Byte-strong VSIX install: sha256-verify BEFORE install, fail-closed on
mismatch. Download + ``code`` subprocess both mocked."""

from __future__ import annotations

import hashlib
import io
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

from setforge.errors import ExtensionInstallFailed, ResolveError
from setforge.provision.resolve.protocol import IntegrityKind, PackageType, ResolvedPin
from setforge.vscode_extensions import install_one


def _vsix_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("extension.vsixmanifest", "<manifest/>")
        zf.writestr("extension/package.json", '{"name": "prettier-vscode"}')
    return buf.getvalue()


_VSIX = _vsix_bytes()
_VSIX_SHA = hashlib.sha256(_VSIX).hexdigest()


class _FakeCode:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args: list[str], **_: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    @property
    def install_args(self) -> list[str]:
        return [c[2] for c in self.calls if c[1] == "--install-extension"]


@pytest.fixture
def fake_code(monkeypatch: pytest.MonkeyPatch) -> _FakeCode:
    fake = _FakeCode()
    monkeypatch.setattr(
        "setforge.vscode_extensions.resolve_binary",
        lambda name: Path("/usr/bin/code") if name == "code" else None,
    )
    monkeypatch.setattr("setforge.vscode_extensions.subprocess.run", fake.run)
    return fake


def _pin(version: str, sha_hex: str) -> ResolvedPin:
    return ResolvedPin(
        type=PackageType.EXTENSION,
        key="esbenp.prettier-vscode",
        version=version,
        integrity=f"sha256:{sha_hex}",
        integrity_kind=IntegrityKind.CHECKSUM,
    )


def _patch_download(
    monkeypatch: pytest.MonkeyPatch, returns: bytes
) -> list[tuple[str, str, str]]:
    seen: list[tuple[str, str, str]] = []

    def fake_download(
        publisher: str, name: str, version: str, *, fetch: Any = None
    ) -> bytes:
        seen.append((publisher, name, version))
        return returns

    monkeypatch.setattr(
        "setforge.provision.resolve.extension.download_vsix", fake_download
    )
    return seen


def test_pinned_install_downloads_verifies_and_installs_vsix_file(
    fake_code: _FakeCode, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _patch_download(monkeypatch, _VSIX)

    install_one("esbenp.prettier-vscode", pin=_pin("1.2.3", _VSIX_SHA))

    assert seen == [("esbenp", "prettier-vscode", "1.2.3")]
    assert len(fake_code.install_args) == 1
    arg = fake_code.install_args[0]
    assert arg.endswith(".vsix")
    assert arg != "esbenp.prettier-vscode"


def test_pinned_install_hash_mismatch_fails_and_does_not_install(
    fake_code: _FakeCode, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong_hash = "0" * 64
    _patch_download(monkeypatch, _VSIX)

    with pytest.raises(ExtensionInstallFailed, match="checksum mismatch"):
        install_one("esbenp.prettier-vscode", pin=_pin("1.2.3", wrong_hash))

    assert fake_code.install_args == []


def test_pinned_install_rejects_non_sha256_integrity(
    fake_code: _FakeCode, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_download(monkeypatch, _VSIX)
    non_sha256 = ResolvedPin(
        type=PackageType.EXTENSION,
        key="esbenp.prettier-vscode",
        version="1.2.3",
        integrity=f"sha512:{_VSIX_SHA}",
        integrity_kind=IntegrityKind.CHECKSUM,
    )

    with pytest.raises(ExtensionInstallFailed, match="unsupported lock integrity"):
        install_one("esbenp.prettier-vscode", pin=non_sha256)

    assert fake_code.install_args == []


def test_pinned_install_cleans_up_temp_on_verify_failure(
    fake_code: _FakeCode, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_download(monkeypatch, _VSIX)
    before = set(Path(tempfile.gettempdir()).glob("setforge-ext-*.vsix"))

    with pytest.raises(ExtensionInstallFailed):
        install_one("esbenp.prettier-vscode", pin=_pin("1.2.3", "0" * 64))

    after = set(Path(tempfile.gettempdir()).glob("setforge-ext-*.vsix"))
    assert after == before


def test_pinned_install_cleans_up_temp_on_code_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_download(monkeypatch, _VSIX)
    monkeypatch.setattr(
        "setforge.vscode_extensions.resolve_binary",
        lambda name: Path("/usr/bin/code") if name == "code" else None,
    )

    captured_paths: list[str] = []

    def failing_run(args: list[str], **_: Any) -> subprocess.CompletedProcess:
        captured_paths.append(args[2])
        raise subprocess.CalledProcessError(1, args, stderr="dependency missing")

    monkeypatch.setattr("setforge.vscode_extensions.subprocess.run", failing_run)

    with pytest.raises(ExtensionInstallFailed):
        install_one("esbenp.prettier-vscode", pin=_pin("1.2.3", _VSIX_SHA))

    assert captured_paths
    assert captured_paths[0].endswith(".vsix")
    assert not Path(captured_paths[0]).exists()


def test_pinned_install_download_failure_cleans_up_and_wraps(
    fake_code: _FakeCode, monkeypatch: pytest.MonkeyPatch
) -> None:
    # download_vsix's guarded fetch surfaces every failure as ResolveError
    # (fetch_bytes wraps URL/timeout/OS/EOF errors); the install path catches
    # exactly that type and rewraps it as ExtensionInstallFailed.
    def boom(*_a: Any, **_k: Any) -> bytes:
        raise ResolveError("network down")

    monkeypatch.setattr("setforge.provision.resolve.extension.download_vsix", boom)

    with pytest.raises(ExtensionInstallFailed, match="could not download"):
        install_one("esbenp.prettier-vscode", pin=_pin("1.2.3", _VSIX_SHA))

    assert fake_code.install_args == []


def test_no_pin_uses_marketplace_id_unchanged(fake_code: _FakeCode) -> None:
    install_one("esbenp.prettier-vscode")
    assert fake_code.install_args == ["esbenp.prettier-vscode"]
