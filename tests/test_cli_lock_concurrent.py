"""Concurrent `setforge lock` on distinct profiles must UNION, not lost-update.

The committed ``setforge.lock`` is shared across all profiles, so two
``setforge lock --profile=A`` / ``--profile=B`` invocations racing on the same
file both read the same baseline, merge only their own pins, then write — the
second write clobbers the first unless the read -> merge -> write is
serialized and the baseline is re-read UNDER the lock.
"""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

import setforge.cli.lock as lock_mod
from setforge.cli import app
from setforge.lockfile import parse_lock
from setforge.provision.resolve import registry
from setforge.provision.resolve.protocol import IntegrityKind, PackageType, ResolvedPin


@pytest.fixture(autouse=True)
def _isolate_registry() -> object:
    saved = dict(registry._REGISTRY)
    registry._REGISTRY.clear()
    try:
        yield
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(saved)


def _register_stub(pkg_type: PackageType, key: str, version: str) -> None:
    class _Stub:
        type: ClassVar[PackageType] = pkg_type

        def resolve(self, item: object) -> ResolvedPin:
            return ResolvedPin(
                type=pkg_type,
                key=key,
                version=version,
                integrity="sha256:cafe",
                integrity_kind=IntegrityKind.CHECKSUM,
            )

    registry.register(pkg_type)(_Stub)


_YAML = """\
version: 1
tracked_files:
  d: {src: tracked_file.txt, dst: ~/.some-tracked_file}
packages:
  ripgrep: {type: cargo, crate: ripgrep}
  black: {type: python, package: black}
profiles:
  a: {packages: [ripgrep]}
  b: {packages: [black]}
"""


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(_YAML, encoding="utf-8")
    (tmp_path / "tracked").mkdir(exist_ok=True)
    (tmp_path / "tracked" / "tracked_file.txt").write_text("data\n", encoding="utf-8")
    return cfg


def test_concurrent_lock_distinct_profiles_unions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two racing lock() invocations (profile a, profile b) must both survive.

    A barrier forces BOTH baseline reads to attempt to happen before either
    write. With the bug (read outside the lock) both read the empty baseline
    and the second write wins — one profile's pin vanishes. With the fix (read
    under a config-dir lock) the second reader blocks until the first has
    written+released, never both-read the empty baseline, so merge_lock unions
    the two profiles' pins. The barrier is timeout-tolerant so the fix's
    serialized (non-concurrent) path does not deadlock on it.
    """
    cfg = _write_config(tmp_path)
    _register_stub(PackageType.CARGO, "ripgrep", "14.0.0")
    _register_stub(PackageType.PYTHON, "black", "24.1.0")

    barrier = threading.Barrier(2, timeout=1.0)
    real_load = lock_mod._load_existing_lock

    def _load_with_barrier(path: Path) -> object:
        result = real_load(path)
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait()
        return result

    monkeypatch.setattr(lock_mod, "_load_existing_lock", _load_with_barrier)

    errors: list[BaseException] = []

    def _run(profile: str) -> None:
        try:
            result = CliRunner().invoke(
                app, ["lock", f"--profile={profile}", f"--config={cfg}"]
            )
            if result.exit_code != 0:
                errors.append(result.exception or AssertionError(result.output))
        except BaseException as exc:  # surface into main thread
            errors.append(exc)

    threads = [
        threading.Thread(target=_run, args=("a",)),
        threading.Thread(target=_run, args=("b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, errors
    assert not any(thread.is_alive() for thread in threads), "lock() deadlocked"

    lock = parse_lock((tmp_path / "setforge.lock").read_text(encoding="utf-8"))
    by_key = {(p.type.value, p.key): p for p in lock.packages}
    assert set(by_key) == {("cargo", "ripgrep"), ("python", "black")}, (
        "both profiles' pins must survive the concurrent lock; a missing key "
        "means the second writer clobbered the first (lost update)"
    )
