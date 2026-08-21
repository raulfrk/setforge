"""Tests for the github_release provisioner (download monkeypatched to baked
fixture bytes — no network)."""

from __future__ import annotations

import hashlib
import io
import stat
import tarfile
from pathlib import Path

import pytest

from setforge.config import GitHubReleasePackage
from setforge.provision import github_release as gh
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


def _pkg(
    install_dir: Path,
    *,
    checksum: str | None,
    asset: str = "tool.tar.gz",
    binary: str = "tool",
    rename: str | None = None,
    extract: bool = True,
) -> GitHubReleasePackage:
    return GitHubReleasePackage(
        repo="owner/tool",
        tag="v1.0.0",
        asset=asset,
        binary=binary,
        install=str(install_dir),
        checksum=checksum,
        rename=rename,
        extract=extract,
    )


def _item(pkg: GitHubReleasePackage) -> ProvisionItem:
    return ProvisionItem(
        type="github_release",
        identity=Identity(key=pkg.repo, display=pkg.repo),
        config=pkg,
        version=pkg.tag,
        checksum=pkg.checksum,
    )


def _provisioner(
    tmp_path: Path, data: bytes, monkeypatch
) -> gh.GitHubReleaseProvisioner:
    receipts = ReceiptStore(tmp_path / "receipts")
    prov = gh.GitHubReleaseProvisioner(receipts=receipts)
    monkeypatch.setattr(prov, "_download", lambda url: data)
    return prov


def test_happy_path_installs_and_records_receipt(tmp_path: Path, monkeypatch) -> None:
    install_dir = tmp_path / "bin"
    payload = b"#!/bin/sh\necho hi\n"
    data = _tar_gz({"tool": payload})
    pkg = _pkg(install_dir, checksum=_sha256(data))
    prov = _provisioner(tmp_path, data, monkeypatch)

    outcome = prov.apply_one(_item(pkg))

    assert outcome.outcome is Outcome.OK
    dest = install_dir / "tool"
    assert dest.read_bytes() == payload
    assert dest.stat().st_mode & stat.S_IXUSR
    store = prov._receipts
    assert store.path_for(Identity(key=pkg.repo, display=pkg.repo)) == dest


def test_nested_binary_installs_to_bare_renamed_destination(
    tmp_path: Path, monkeypatch
) -> None:
    install_dir = tmp_path / "bin"
    payload = b"#!/bin/sh\necho nested\n"
    data = _tar_gz({"release/bin/tool": payload})
    pkg = _pkg(
        install_dir,
        checksum=_sha256(data),
        binary="release/bin/tool",
        rename="tool",
    )
    prov = _provisioner(tmp_path, data, monkeypatch)

    outcome = prov.apply_one(_item(pkg))

    dest = install_dir / "tool"
    assert outcome.outcome is Outcome.OK
    assert dest.read_bytes() == payload
    assert not (install_dir / "release").exists()
    assert prov._receipts.path_for(_item(pkg).identity) == dest


def test_rerun_is_skip_and_writes_no_new_receipt(tmp_path: Path, monkeypatch) -> None:
    install_dir = tmp_path / "bin"
    data = _tar_gz({"tool": b"payload"})
    pkg = _pkg(install_dir, checksum=_sha256(data))
    prov = _provisioner(tmp_path, data, monkeypatch)

    first = prov.apply_one(_item(pkg))
    assert first.outcome is Outcome.OK
    second = prov.apply_one(_item(pkg))
    assert second.outcome is Outcome.SKIP


def test_version_change_reinstalls_and_updates_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    install_dir = tmp_path / "bin"
    data = _tar_gz({"tool": b"payload"})
    first_pkg = _pkg(install_dir, checksum=_sha256(data))
    prov = _provisioner(tmp_path, data, monkeypatch)
    assert prov.apply_one(_item(first_pkg)).outcome is Outcome.OK
    upgraded_pkg = first_pkg.model_copy(update={"tag": "v2.0.0"})

    outcome = prov.apply_one(_item(upgraded_pkg))

    assert outcome.outcome is Outcome.OK
    entry = prov._receipts.entry_for(_item(upgraded_pkg).identity, "github_release")
    assert entry is not None
    assert entry.version == "v2.0.0"


def test_plan_is_pure_and_skips_installed(tmp_path: Path, monkeypatch) -> None:
    install_dir = tmp_path / "bin"
    data = _tar_gz({"tool": b"payload"})
    pkg = _pkg(install_dir, checksum=_sha256(data))
    prov = _provisioner(tmp_path, data, monkeypatch)
    item = _item(pkg)

    delta = prov.plan([item], set())
    assert delta.installed == (item.identity,)
    delta2 = prov.plan([item], {item.identity})
    assert delta2.is_empty()


def test_bad_checksum_is_hard_nothing_installed(tmp_path: Path, monkeypatch) -> None:
    install_dir = tmp_path / "bin"
    data = _tar_gz({"tool": b"payload"})
    pkg = _pkg(install_dir, checksum="sha256:" + "0" * 64)
    prov = _provisioner(tmp_path, data, monkeypatch)

    outcome = prov.apply_one(_item(pkg))

    assert outcome.outcome is Outcome.HARD
    assert not install_dir.exists() or not any(install_dir.iterdir())
    assert prov._receipts.installed() == set()


def test_missing_checksum_is_hard(tmp_path: Path, monkeypatch) -> None:
    install_dir = tmp_path / "bin"
    data = _tar_gz({"tool": b"payload"})
    pkg = _pkg(install_dir, checksum=None)
    prov = _provisioner(tmp_path, data, monkeypatch)

    outcome = prov.apply_one(_item(pkg))

    assert outcome.outcome is Outcome.HARD
    assert prov._receipts.installed() == set()


def test_symlink_member_is_hard(tmp_path: Path, monkeypatch) -> None:
    install_dir = tmp_path / "bin"
    info = tarfile.TarInfo(name="tool")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.addfile(info)
    data = buf.getvalue()
    pkg = _pkg(install_dir, checksum=_sha256(data))
    prov = _provisioner(tmp_path, data, monkeypatch)

    outcome = prov.apply_one(_item(pkg))

    assert outcome.outcome is Outcome.HARD
    assert prov._receipts.installed() == set()


def test_download_failure_is_soft(tmp_path: Path, monkeypatch) -> None:
    install_dir = tmp_path / "bin"
    pkg = _pkg(install_dir, checksum="sha256:" + "0" * 64)
    receipts = ReceiptStore(tmp_path / "receipts")
    prov = gh.GitHubReleaseProvisioner(receipts=receipts)

    def _boom(url: str) -> bytes:
        raise gh.DownloadError("network down")

    monkeypatch.setattr(prov, "_download", _boom)

    outcome = prov.apply_one(_item(pkg))

    assert outcome.outcome is Outcome.SOFT
    assert prov._receipts.installed() == set()


def test_download_url_shape(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    pkg = _pkg(install_dir, checksum=None)
    url = gh._asset_url(pkg)
    assert url == "https://github.com/owner/tool/releases/download/v1.0.0/tool.tar.gz"


class _FakeResponse:
    def __init__(self, *, chunks: list[bytes], final_url: str) -> None:
        self._chunks = list(chunks)
        self._final_url = final_url

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def geturl(self) -> str:
        return self._final_url

    def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


def _real_download_prov(tmp_path: Path) -> gh.GitHubReleaseProvisioner:
    return gh.GitHubReleaseProvisioner(receipts=ReceiptStore(tmp_path / "receipts"))


def test_download_rejects_http_scheme(tmp_path: Path) -> None:
    prov = _real_download_prov(tmp_path)
    with pytest.raises(gh.DownloadError):
        prov._download("http://github.com/owner/tool/releases/download/v1/tool.tar.gz")


def test_download_rejects_non_http_scheme(tmp_path: Path) -> None:
    prov = _real_download_prov(tmp_path)
    with pytest.raises(gh.DownloadError):
        prov._download("ftp://example.com/tool.tar.gz")


def test_download_rejects_http_redirect_downgrade(tmp_path: Path, monkeypatch) -> None:
    prov = _real_download_prov(tmp_path)
    fake = _FakeResponse(chunks=[b"data"], final_url="http://evil.example/tool")

    def _fake_urlopen(_request: object, timeout: float = 0) -> _FakeResponse:
        return fake

    monkeypatch.setattr(gh.urllib.request, "urlopen", _fake_urlopen)
    with pytest.raises(gh.DownloadError):
        prov._download("https://github.com/owner/tool/releases/download/v1/tool.tar.gz")


def test_download_wire_cap_aborts_oversize_stream(tmp_path: Path, monkeypatch) -> None:
    prov = _real_download_prov(tmp_path)
    monkeypatch.setattr(gh, "_MAX_WIRE_BYTES", 4)
    monkeypatch.setattr(gh, "_CHUNK", 4)
    fake = _FakeResponse(
        chunks=[b"aaaa", b"bbbb", b"cccc"], final_url="https://github.com/x"
    )

    def _fake_urlopen(_request: object, timeout: float = 0) -> _FakeResponse:
        return fake

    monkeypatch.setattr(gh.urllib.request, "urlopen", _fake_urlopen)
    with pytest.raises(gh.DownloadError):
        prov._download("https://github.com/owner/tool/releases/download/v1/tool.tar.gz")


def test_download_deadline_aborts_slow_drip_stream(tmp_path: Path, monkeypatch) -> None:
    prov = _real_download_prov(tmp_path)
    # A slow-drip server: each read returns a small chunk under the per-read
    # socket timeout, so the transfer never trips the stdlib timeout. Advance a
    # fake clock past the overall deadline as chunks arrive.
    clock = {"now": 0.0}
    monkeypatch.setattr(gh.time, "monotonic", lambda: clock["now"])

    class _DripResponse(_FakeResponse):
        def read(self, n: int) -> bytes:
            clock["now"] += gh._DOWNLOAD_DEADLINE_S  # one drip blows the budget
            return super().read(n)

    fake = _DripResponse(
        chunks=[b"a", b"b", b"c", b"d"], final_url="https://github.com/x"
    )

    def _fake_urlopen(_request: object, timeout: float = 0) -> _DripResponse:
        return fake

    monkeypatch.setattr(gh.urllib.request, "urlopen", _fake_urlopen)
    with pytest.raises(gh.DownloadError, match="deadline"):
        prov._download("https://github.com/owner/tool/releases/download/v1/tool.tar.gz")


def test_download_fast_stream_completes_within_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    prov = _real_download_prov(tmp_path)
    # Clock never advances: a fast transfer stays well under the deadline.
    monkeypatch.setattr(gh.time, "monotonic", lambda: 0.0)
    fake = _FakeResponse(chunks=[b"aa", b"bb", b"cc"], final_url="https://github.com/x")

    def _fake_urlopen(_request: object, timeout: float = 0) -> _FakeResponse:
        return fake

    monkeypatch.setattr(gh.urllib.request, "urlopen", _fake_urlopen)
    data = prov._download(
        "https://github.com/owner/tool/releases/download/v1/tool.tar.gz"
    )
    assert data == b"aabbcc"


def test_probe_before_download_short_circuits(tmp_path: Path, monkeypatch) -> None:
    install_dir = tmp_path / "bin"
    data = _tar_gz({"tool": b"payload"})
    pkg = _pkg(install_dir, checksum=_sha256(data))
    receipts = ReceiptStore(tmp_path / "receipts")
    receipts.record(
        Identity(key=pkg.repo, display=pkg.repo),
        version=pkg.tag,
        checksum=pkg.checksum,
        path=install_dir / "tool",
    )
    prov = gh.GitHubReleaseProvisioner(receipts=receipts)

    def _must_not_download(url: str) -> bytes:
        raise AssertionError("download attempted for an already-present item")

    monkeypatch.setattr(prov, "_download", _must_not_download)
    outcome = prov.apply_one(_item(pkg))
    assert outcome.outcome is Outcome.SKIP


def test_uninstall_removes_receipt_and_binary(tmp_path: Path, monkeypatch) -> None:
    install_dir = tmp_path / "bin"
    data = _tar_gz({"tool": b"payload"})
    pkg = _pkg(install_dir, checksum=_sha256(data))
    prov = _provisioner(tmp_path, data, monkeypatch)
    ident = Identity(key=pkg.repo, display=pkg.repo)

    prov.apply_one(_item(pkg))
    dest = install_dir / "tool"
    assert dest.exists()

    prov.uninstall_one(ident)

    assert not dest.exists()
    assert prov._receipts.installed() == set()


def test_uninstall_is_idempotent(tmp_path: Path) -> None:
    receipts = ReceiptStore(tmp_path / "receipts")
    prov = gh.GitHubReleaseProvisioner(receipts=receipts)
    prov.uninstall_one(Identity(key="owner/tool", display="owner/tool"))


def test_registry_builds_github_release_provisioner() -> None:
    import setforge.provision.dispatch  # noqa: F401  (imports register the type)
    from setforge.provision.registry import build

    pkg = _pkg(Path("/tmp/bin"), checksum=None)
    prov = build(_item(pkg))
    assert isinstance(prov, gh.GitHubReleaseProvisioner)
    assert prov.type == "github_release"
