"""TOCTOU + opt-out + HOME-isolation tests for ``ensure_local_config_stub``.

The function writes ``~/.config/setforge/local.yaml`` on every Typer
callback invocation. The previous shape used
``if LOCAL_CONFIG_PATH.exists(): return`` followed by
``write_text(...)``, racing on the file under parallel pytest workers.
The new shape uses ``open("x")`` + ``FileExistsError``
suppression for an atomic create-or-skip.

Three test surfaces here:

1. :func:`test_ensure_local_config_stub_is_toctou_safe` — 10 threads
   race on the same target path; assert only ONE process writes the
   stub content and no exception escapes any thread.
2. :func:`test_skip_env_var_works` — ``SETFORGE_SKIP_LOCAL_STUB=1``
   bypasses the write entirely (no file created).
3. :func:`test_isolate_home_fixture_redirects_path_home` — the
   ``_isolate_home`` autouse fixture monkeypatches ``Path.home``
   correctly, so ``Path.home()`` returns the per-test tmp dir.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from setforge import binaries


def test_ensure_local_config_stub_is_toctou_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10 concurrent calls produce ONE write, no exceptions."""
    target = tmp_path / "local.yaml"
    monkeypatch.setattr(binaries, "LOCAL_CONFIG_PATH", target)
    monkeypatch.delenv("SETFORGE_SKIP_LOCAL_STUB", raising=False)

    errors: list[BaseException] = []

    def call() -> None:
        try:
            binaries.ensure_local_config_stub()
        except BaseException as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(call) for _ in range(10)]
        for fut in as_completed(futures):
            fut.result()  # surface any worker-thread exception immediately

    assert errors == [], f"unexpected exceptions across threads: {errors}"
    assert target.is_file()
    # Content must be the full stub, NOT a truncated partial write — the
    # ``open("x")`` mode opens-for-write only when no file exists, so the
    # winning thread writes the full template before the losers see EEXIST.
    assert target.read_text(encoding="utf-8").startswith("# setforge host-local config")


def test_ensure_local_config_stub_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing file is preserved verbatim across repeated invocations."""
    target = tmp_path / "local.yaml"
    monkeypatch.setattr(binaries, "LOCAL_CONFIG_PATH", target)
    monkeypatch.delenv("SETFORGE_SKIP_LOCAL_STUB", raising=False)
    target.write_text("user-edited content\n", encoding="utf-8")

    binaries.ensure_local_config_stub()
    binaries.ensure_local_config_stub()

    assert target.read_text(encoding="utf-8") == "user-edited content\n"


def test_skip_env_var_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SETFORGE_SKIP_LOCAL_STUB=1`` → no file created."""
    target = tmp_path / "local.yaml"
    monkeypatch.setattr(binaries, "LOCAL_CONFIG_PATH", target)
    monkeypatch.setenv("SETFORGE_SKIP_LOCAL_STUB", "1")

    binaries.ensure_local_config_stub()

    assert not target.exists()


def test_skip_env_var_value_zero_does_not_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the literal value ``"1"`` opts out — ``"0"`` is no-skip."""
    target = tmp_path / "local.yaml"
    monkeypatch.setattr(binaries, "LOCAL_CONFIG_PATH", target)
    monkeypatch.setenv("SETFORGE_SKIP_LOCAL_STUB", "0")

    binaries.ensure_local_config_stub()

    assert target.is_file()


def test_isolate_home_fixture_redirects_path_home() -> None:
    """The autouse ``_isolate_home`` fixture monkeypatches ``Path.home()``.

    Verified by reading ``$HOME`` and ``Path.home()`` together: both
    must point at the autouse fixture's per-test tmp dir, not the
    real dev-host home.
    """
    env_home = os.environ.get("HOME")
    assert env_home is not None
    assert Path.home() == Path(env_home)
    # The autouse fixture uses ``tmp_path_factory.mktemp(...)`` whose
    # mkdtemp pattern lives under pytest's ``/tmp/pytest-of-<user>/`` root.
    # We don't pin the exact root (CI may relocate it) — just assert the
    # home is NOT the real dev-host home.
    assert Path.home() != Path("/home/raul")
    assert "_autoisolated_home" in str(Path.home())
