from __future__ import annotations

import hashlib
import io
import stat
import tarfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from setforge.config import LocalPackage
from setforge.provision import local as loc
from setforge.provision.protocol import Identity, Outcome, ProvisionItem
from setforge.provision.receipt import ReceiptStore


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _tar_gz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _symlink_tar_gz(link_name: str, target: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=link_name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        tar.addfile(info)
    return buf.getvalue()


def _pkg(
    install_dir: Path,
    *,
    path: str,
    checksum: str | None = None,
    binary: str = "tool",
    rename: str | None = None,
    extract: bool = True,
) -> LocalPackage:
    return LocalPackage(
        path=path,
        binary=binary,
        install=str(install_dir),
        checksum=checksum,
        rename=rename,
        extract=extract,
    )


def _item(pkg: LocalPackage) -> ProvisionItem:
    return ProvisionItem(
        type="local",
        identity=Identity(key=pkg.binary, display=pkg.binary),
        config=pkg,
        checksum=pkg.checksum,
    )


def _provisioner(tmp_path: Path, tracked_root: Path) -> loc.LocalProvisioner:
    receipts = ReceiptStore(tmp_path / "receipts")
    return loc.LocalProvisioner(receipts=receipts, tracked_root=tracked_root)


def _write(tracked_root: Path, rel: str, data: bytes) -> None:
    dest = tracked_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def test_installs_bare_binary_from_tracked_file(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked"
    payload = b"#!/bin/sh\necho hi\n"
    _write(tracked, "bin/tool", payload)
    install_dir = tmp_path / "out"
    pkg = _pkg(install_dir, path="bin/tool", binary="tool", extract=False)
    prov = _provisioner(tmp_path, tracked)

    outcome = prov.apply_one(_item(pkg))

    assert outcome.outcome is Outcome.OK, outcome.detail
    dest = install_dir / "tool"
    assert dest.read_bytes() == payload
    assert dest.stat().st_mode & stat.S_IXUSR


def test_installs_from_archive(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked"
    payload = b"binary-bytes"
    _write(tracked, "assets/tool.tar.gz", _tar_gz({"tool": payload}))
    install_dir = tmp_path / "out"
    pkg = _pkg(install_dir, path="assets/tool.tar.gz", binary="tool", extract=True)
    prov = _provisioner(tmp_path, tracked)

    outcome = prov.apply_one(_item(pkg))

    assert outcome.outcome is Outcome.OK, outcome.detail
    assert (install_dir / "tool").read_bytes() == payload


def test_source_path_escaping_tracked_root_is_rejected(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked"
    tracked.mkdir()
    outside = tmp_path / "secret"
    outside.write_bytes(b"top-secret")
    install_dir = tmp_path / "out"
    pkg = _pkg(install_dir, path="../secret", binary="tool", extract=False)
    prov = _provisioner(tmp_path, tracked)

    outcome = prov.apply_one(_item(pkg))

    assert outcome.outcome is Outcome.HARD, outcome.detail
    assert not (install_dir / "tool").exists()


def test_absolute_source_path_is_rejected(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked"
    tracked.mkdir()
    outside = tmp_path / "secret"
    outside.write_bytes(b"top-secret")
    install_dir = tmp_path / "out"
    pkg = _pkg(install_dir, path=str(outside), binary="tool", extract=False)
    prov = _provisioner(tmp_path, tracked)

    outcome = prov.apply_one(_item(pkg))

    assert outcome.outcome is Outcome.HARD, outcome.detail
    assert not (install_dir / "tool").exists()


def test_symlink_escape_source_is_rejected(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked"
    tracked.mkdir()
    outside = tmp_path / "secret"
    outside.write_bytes(b"top-secret")
    (tracked / "evil").symlink_to(outside)
    install_dir = tmp_path / "out"
    pkg = _pkg(install_dir, path="evil", binary="tool", extract=False)
    prov = _provisioner(tmp_path, tracked)

    outcome = prov.apply_one(_item(pkg))

    assert outcome.outcome is Outcome.HARD, outcome.detail
    assert not (install_dir / "tool").exists()


def test_missing_source_file_is_reported_not_crashes(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked"
    tracked.mkdir()
    install_dir = tmp_path / "out"
    pkg = _pkg(install_dir, path="bin/absent", binary="tool", extract=False)
    prov = _provisioner(tmp_path, tracked)

    outcome = prov.apply_one(_item(pkg))

    assert outcome.outcome is Outcome.HARD, outcome.detail
    assert not (install_dir / "tool").exists()


def test_checksum_absent_installs_without_verify(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked"
    payload = b"no-checksum-content"
    _write(tracked, "bin/tool", payload)
    install_dir = tmp_path / "out"
    pkg = _pkg(
        install_dir, path="bin/tool", binary="tool", extract=False, checksum=None
    )
    prov = _provisioner(tmp_path, tracked)

    outcome = prov.apply_one(_item(pkg))

    assert outcome.outcome is Outcome.OK, outcome.detail
    assert (install_dir / "tool").read_bytes() == payload


def test_checksum_present_and_correct_installs(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked"
    payload = b"verified-content"
    _write(tracked, "bin/tool", payload)
    install_dir = tmp_path / "out"
    pkg = _pkg(
        install_dir,
        path="bin/tool",
        binary="tool",
        extract=False,
        checksum=_sha256(payload),
    )
    prov = _provisioner(tmp_path, tracked)

    outcome = prov.apply_one(_item(pkg))

    assert outcome.outcome is Outcome.OK, outcome.detail
    assert (install_dir / "tool").read_bytes() == payload


def test_checksum_present_and_wrong_is_hard_nothing_installed(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked"
    payload = b"real-content"
    _write(tracked, "bin/tool", payload)
    install_dir = tmp_path / "out"
    wrong = _sha256(b"different-content")
    pkg = _pkg(
        install_dir, path="bin/tool", binary="tool", extract=False, checksum=wrong
    )
    prov = _provisioner(tmp_path, tracked)

    outcome = prov.apply_one(_item(pkg))

    assert outcome.outcome is Outcome.HARD, outcome.detail
    assert not (install_dir / "tool").exists()


def test_malicious_archive_member_is_rejected(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked"
    _write(tracked, "assets/evil.tar.gz", _symlink_tar_gz("tool", "/etc/passwd"))
    install_dir = tmp_path / "out"
    pkg = _pkg(install_dir, path="assets/evil.tar.gz", binary="tool", extract=True)
    prov = _provisioner(tmp_path, tracked)

    outcome = prov.apply_one(_item(pkg))

    assert outcome.outcome is Outcome.HARD, outcome.detail
    assert not (install_dir / "tool").exists()


def test_rerun_no_ops_when_receipt_present(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked"
    _write(tracked, "bin/tool", b"content")
    install_dir = tmp_path / "out"
    pkg = _pkg(install_dir, path="bin/tool", binary="tool", extract=False)
    prov = _provisioner(tmp_path, tracked)

    first = prov.apply_one(_item(pkg))
    assert first.outcome is Outcome.OK, first.detail

    second = prov.apply_one(_item(pkg))
    assert second.outcome is Outcome.SKIP, second.detail
    assert second.detail == "present"


def test_reproducible_from_committed_file(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked"
    payload = b"deterministic"
    _write(tracked, "bin/tool", payload)
    pkg_a = _pkg(tmp_path / "a", path="bin/tool", binary="tool", extract=False)
    pkg_b = _pkg(tmp_path / "b", path="bin/tool", binary="tool", extract=False)

    prov_a = loc.LocalProvisioner(
        receipts=ReceiptStore(tmp_path / "ra"), tracked_root=tracked
    )
    prov_b = loc.LocalProvisioner(
        receipts=ReceiptStore(tmp_path / "rb"), tracked_root=tracked
    )
    prov_a.apply_one(_item(pkg_a))
    prov_b.apply_one(_item(pkg_b))

    assert (
        (tmp_path / "a" / "tool").read_bytes()
        == (tmp_path / "b" / "tool").read_bytes()
        == payload
    )


def test_lazy_tracked_root_resolves_via_source_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fixture sits under source/tracked/: proves apply_one joins via the source layer.
    source_dir = tmp_path / "source"
    payload = b"#!/bin/sh\necho lazy\n"
    _write(source_dir / "tracked", "bin/tool", payload)

    sentinel = object()
    monkeypatch.setattr(loc, "get_resolved_source", lambda: sentinel)

    def fake_resolve(arg: object) -> Path:
        assert arg is sentinel
        return source_dir

    monkeypatch.setattr(loc, "resolve_source_dir", fake_resolve)

    install_dir = tmp_path / "out"
    pkg = _pkg(install_dir, path="bin/tool", binary="tool", extract=False)
    prov = loc.LocalProvisioner(receipts=ReceiptStore(tmp_path / "receipts"))

    outcome = prov.apply_one(_item(pkg))

    assert outcome.outcome is Outcome.OK, outcome.detail
    dest = install_dir / "tool"
    assert dest.read_bytes() == payload
    assert dest.stat().st_mode & stat.S_IXUSR


def test_uninstall_removes_binary_and_receipt(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked"
    _write(tracked, "bin/tool", b"content")
    install_dir = tmp_path / "out"
    pkg = _pkg(install_dir, path="bin/tool", binary="tool", extract=False)
    receipts = ReceiptStore(tmp_path / "receipts")
    prov = loc.LocalProvisioner(receipts=receipts, tracked_root=tracked)
    item = _item(pkg)

    prov.apply_one(item)
    dest = install_dir / "tool"
    assert dest.exists()
    assert item.identity in receipts.installed()

    prov.uninstall_one(item.identity)

    assert not dest.exists()
    assert item.identity not in receipts.installed()


def test_install_path_confinement_end_to_end(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked"
    _write(tracked, "bin/tool", b"content")
    install_dir = tmp_path / "out"
    install_dir.mkdir()
    with pytest.raises(ValidationError):
        _pkg(
            install_dir,
            path="bin/tool",
            binary="tool",
            rename="../escape",
            extract=False,
        )
    assert not (tmp_path / "escape").exists()
    assert list(install_dir.iterdir()) == []
