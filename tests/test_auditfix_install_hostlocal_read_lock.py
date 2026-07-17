"""Regression: ``install`` must read the host-local overlay under ``profile_lock``.

The host-local sections overlay is projected from the reconcile store via
three unsynchronized reads (``read_index`` → ``read_base`` → ``read_local``),
while ``store.record`` writes base+local BEFORE index. Reading the overlay
OUTSIDE ``profile_lock`` lets a concurrent install/sync hand this reader a
stale-index / already-rewritten-body pair, feeding a corrupt overlay into the
pre-deploy drift gate and the dry-run display.

``install`` previously loaded that overlay
(``_load_validated_host_local_sections``) BEFORE entering
``with profile_lock(profile):`` — the same anti-pattern
``test_auditfix_revert_lock`` guards for ``revert``'s mutations. These tests
assert the overlay read now happens INSIDE the lock frame, for both the
mutating install path and the ``--dry-run`` preview path.

They fail against the old (pre-lock) behavior: the read event was recorded
BEFORE the lock's ``enter`` event.
"""

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
    """Build a tracked/ tree + setforge.yaml at tmp_path. Returns (cfg, dst)."""
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    src = repo / "tracked" / "greeting.md"
    src.write_text("hello\n", encoding="utf-8")
    # dst must stay under $HOME (the sandboxed home from the autouse isolation
    # fixture) so the deploy-path home-confinement gate accepts it.
    dst = Path.home() / "live" / "greeting.md"
    cfg = repo / "setforge.yaml"
    cfg.write_text(_FIXTURE_YAML.format(dst=dst), encoding="utf-8")
    return cfg, dst


def _state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def _no_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the `code` CLI absent so the extension leg warn-and-skips."""
    monkeypatch.setattr(
        "setforge.vscode_extensions.resolve_binary",
        lambda name: None,
    )


def _recording_lock() -> tuple[Callable[..., object], list[str]]:
    """A ``profile_lock`` stand-in that records ``enter`` / ``exit`` events.

    Wraps the real :func:`setforge.locking.profile_lock` so serialization is
    exercised for real while the enter/exit boundary is observable. Returns
    ``(context_manager_factory, events)``.
    """
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
    """Wrap ``_load_validated_host_local_sections`` to append a ``read`` event.

    Delegates to the real loader so the overlay projection still runs; the
    event only marks WHEN the read fired relative to the lock's enter/exit.
    """
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
    """Mutating ``install`` must read the host-local overlay inside profile_lock."""
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


def test_dry_run_reads_host_local_overlay_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``install --dry-run`` must read the host-local overlay inside profile_lock."""
    cfg, _dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    recording_lock, events = _recording_lock()
    recording_read = _recording_read(events)
    monkeypatch.setattr("setforge.cli._install_helpers.profile_lock", recording_lock)
    monkeypatch.setattr(
        "setforge.cli._install_helpers._load_validated_host_local_sections",
        recording_read,
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["install", "--profile=vmh", f"--config={cfg}", "--dry-run"]
    )
    assert result.exit_code == 0, result.output

    assert "enter" in events, "dry-run never acquired the profile lock"
    assert "read" in events, "dry-run never read the host-local overlay"
    assert events.index("enter") < events.index("read"), (
        f"overlay read must happen inside the lock; observed order: {events}"
    )
    assert events.index("read") < events.index("exit"), (
        f"overlay read must happen before lock release; observed order: {events}"
    )
