"""Regression: ``install`` must read the host-local overlay under ``profile_lock``."""

import contextlib
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.source import HostLocalSection, HostLocalSectionName

_FIXTURE_YAML = """\
version: 1
tracked_files:
  greeting:
    src: greeting.md
    dst: {dst}
profiles:
  vmh:
    tracked_files: [greeting]
"""


def _setup_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    src = repo / "tracked" / "greeting.md"
    src.write_text("hello\n", encoding="utf-8")
    dst = Path.home() / "live" / "greeting.md"
    cfg = repo / "setforge.yaml"
    cfg.write_text(_FIXTURE_YAML.format(dst=dst), encoding="utf-8")
    return cfg, dst


def _state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def _no_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "setforge.vscode_extensions.resolve_binary",
        lambda name: None,
    )


def _recording_lock() -> tuple[Callable[..., object], list[str]]:
    from setforge import locking

    events: list[str] = []
    real_lock = locking.profile_lock

    @contextlib.contextmanager
    def recording_lock(profile: str, timeout: float | None = None) -> Iterator[None]:
        events.append("enter")
        with real_lock(profile, timeout=timeout):
            try:
                yield
            finally:
                events.append("exit")

    return recording_lock, events


def _recording_read(
    events: list[str],
) -> Callable[..., dict[str, dict[HostLocalSectionName, HostLocalSection]]]:
    from setforge.cli import _install_helpers

    real_read = _install_helpers._load_validated_host_local_sections

    def recording_read(
        *args: object, **kwargs: object
    ) -> dict[str, dict[HostLocalSectionName, HostLocalSection]]:
        events.append("read")
        return real_read(*args, **kwargs)  # type: ignore[arg-type]

    return recording_read


def test_install_reads_host_local_overlay_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    recording_lock, events = _recording_lock()
    recording_read = _recording_read(events)
    monkeypatch.setattr("setforge.cli.install.profile_lock", recording_lock)
    monkeypatch.setattr(
        "setforge.cli.install._load_validated_host_local_sections", recording_read
    )

    runner = CliRunner()
    result = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert result.exit_code == 0, result.output

    assert "enter" in events, "install never acquired the profile lock"
    assert "read" in events, "install never read the host-local overlay"
    assert events.index("enter") < events.index("read"), (
        f"overlay read must happen inside the lock; observed order: {events}"
    )
    assert events.index("read") < events.index("exit"), (
        f"overlay read must happen before lock release; observed order: {events}"
    )
