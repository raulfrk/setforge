"""Lock -> extension reconcile threading: pinned routes through the
byte-strong VSIX path."""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from setforge.config import Extensions, ReconcilePolicy
from setforge.lockfile import LockFile
from setforge.provision.extension import ExtensionProvisioner
from setforge.provision.lock_apply import extension_pins
from setforge.provision.protocol import Identity, ProvisionItem
from setforge.provision.resolve.protocol import IntegrityKind, PackageType, ResolvedPin
from setforge.vscode_extensions import reconcile


def _vsix() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("extension.vsixmanifest", "<manifest/>")
    return buf.getvalue()


_VSIX = _vsix()
_VSIX_SHA = hashlib.sha256(_VSIX).hexdigest()


def _pin(key: str, version: str, sha_hex: str = _VSIX_SHA) -> ResolvedPin:
    return ResolvedPin(
        type=PackageType.EXTENSION,
        key=key,
        version=version,
        integrity=f"sha256:{sha_hex}",
        integrity_kind=IntegrityKind.CHECKSUM,
    )


def test_extension_pins_filters_and_casefolds() -> None:
    ext_pin = _pin("Esbenp.Prettier-Vscode", "1.2.3")
    cargo_pin = ResolvedPin(
        type=PackageType.CARGO,
        key="ripgrep",
        version="14.0.0",
        integrity="sha256:" + "a" * 64,
        integrity_kind=IntegrityKind.CHECKSUM,
    )
    lock = LockFile(version=1, packages=(ext_pin, cargo_pin))

    pins = extension_pins(lock)

    assert set(pins) == {"esbenp.prettier-vscode"}
    assert pins["esbenp.prettier-vscode"].version == "1.2.3"


def test_extension_pins_none_is_empty() -> None:
    assert extension_pins(None) == {}


def test_provisioner_detaches_pin_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    pin = _pin("pub.ext", "1.2.3")
    pins = {"pub.ext": pin}
    provisioner = ExtensionProvisioner(pins=pins, installed_snapshot=set())
    pins.clear()
    seen: list[object] = []
    monkeypatch.setattr(
        "setforge.provision.extension.vscode_extensions.install_one",
        lambda _ext_id, *, pin=None: seen.append(pin),
    )

    provisioner.apply_one(
        ProvisionItem(
            type="extension",
            identity=Identity(key="pub.ext", display="pub.ext"),
        )
    )

    assert seen == [pin]


def test_reconcile_routes_pinned_ext_through_strong_path(
    monkeypatch: pytest.MonkeyPatch, fake_code
) -> None:
    fake = fake_code()
    monkeypatch.setattr(
        "setforge.provision.resolve.extension.download_vsix",
        lambda *a, **k: _VSIX,
    )

    ext = Extensions(
        include=["esbenp.prettier-vscode", "ms-python.python"],
        reconcile=ReconcilePolicy.ADDITIVE,
    )
    pins = {"esbenp.prettier-vscode": _pin("esbenp.prettier-vscode", "1.2.3")}

    reconcile(ext, pins=pins)

    installs = fake.install_args
    vsix_installs = [a for a in installs if a.endswith(".vsix")]
    id_installs = [a for a in installs if not a.endswith(".vsix")]
    assert len(vsix_installs) == 1
    assert id_installs == ["ms-python.python"]
