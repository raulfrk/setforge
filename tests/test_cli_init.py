"""Unit tests for ``setforge init`` — bootstrap + env health (mockup J)."""

from __future__ import annotations

import re
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.cli._init_helpers import (
    BinaryProbe,
    CapabilityProbe,
    CapabilityState,
    DirProbe,
    EnvProbe,
    _mkdir_with_retry,
    backup_suffix_now,
    config_dir_path,
    host_local_dir_path,
    is_initialized,
    probe_environment,
)

# Typer's rich help renderer interleaves ANSI escapes between option-name
# characters (`\x1b[36m-\x1b[0m\x1b[36m-force\x1b[0m`); strip ANSI before
# substring assertions so `--force` resolves bit-for-bit.
_ANSI_RE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# CliRunner formats help text into a terminal-width column; tighten the
# wrap so option-name substring assertions don't run afoul of wrapping.
_HELP_RUNNER = CliRunner(env={"COLUMNS": "200"})

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Re-point ``$HOME`` and the binaries module's LOCAL_CONFIG_PATH at a tmp dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # binaries.LOCAL_CONFIG_PATH was bound at import time against
    # Path.home(); rebind to the tmp_path equivalent so the probe and
    # the bootstrap write through the same target.
    monkeypatch.setattr(
        "setforge.binaries.LOCAL_CONFIG_PATH",
        tmp_path / ".config" / "setforge" / "local.yaml",
    )
    monkeypatch.setattr(
        "setforge.cli._init_helpers.LOCAL_CONFIG_PATH",
        tmp_path / ".config" / "setforge" / "local.yaml",
    )
    monkeypatch.setattr(
        "setforge.cli.init.LOCAL_CONFIG_PATH",
        tmp_path / ".config" / "setforge" / "local.yaml",
    )
    return tmp_path


class _FakeDialogResult:
    """Stand-in for ``radiolist_dialog(...).run()``."""

    def __init__(self, return_value: object) -> None:
        self._return_value = return_value
        self.run_calls = 0

    def run(self) -> object:
        self.run_calls += 1
        return self._return_value


class _DialogRecorder:
    """Pluggable replacement for ``setforge.cli.init.radiolist_dialog``."""

    def __init__(self, returns: list[object]) -> None:
        self._returns = list(returns)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> _FakeDialogResult:
        self.calls.append(kwargs)
        return _FakeDialogResult(self._returns.pop(0))


def _patch_init_dialog(
    monkeypatch: pytest.MonkeyPatch, returns: list[object]
) -> _DialogRecorder:
    recorder = _DialogRecorder(returns)
    monkeypatch.setattr("setforge.cli.init.radiolist_dialog", recorder)
    return recorder


# ---------------------------------------------------------------------------
# Interactive GIT/PATH source entry
# ---------------------------------------------------------------------------


def test_source_prompt_git_collects_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting GIT collects a URL via input_dialog and builds a GIT spec."""
    import setforge.cli.init as init_mod

    monkeypatch.setattr(
        "setforge.cli.init.radiolist_dialog",
        _DialogRecorder([init_mod.SourceChoice.GIT]),
    )
    monkeypatch.setattr(
        "setforge.cli.init.input_dialog",
        _DialogRecorder(["https://github.com/o/r"]),
    )
    spec = init_mod._prompt_source_config(
        no_prompt=False, path_source=None, git_source=None, git_ref="main"
    )
    assert spec.choice is init_mod.SourceChoice.GIT
    assert spec.url == "https://github.com/o/r"
    assert spec.ref == "main"


def test_source_prompt_path_collects_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting PATH collects a directory via input_dialog and builds a PATH spec."""
    import setforge.cli.init as init_mod

    monkeypatch.setattr(
        "setforge.cli.init.radiolist_dialog",
        _DialogRecorder([init_mod.SourceChoice.PATH]),
    )
    monkeypatch.setattr("setforge.cli.init.input_dialog", _DialogRecorder(["/tmp/cfg"]))
    spec = init_mod._prompt_source_config(
        no_prompt=False, path_source=None, git_source=None, git_ref="main"
    )
    assert spec.choice is init_mod.SourceChoice.PATH
    assert spec.path == Path("/tmp/cfg")


def test_source_prompt_empty_input_falls_back_to_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled/empty input_dialog collapses to SKIP rather than a
    half-built GIT/PATH spec."""
    import setforge.cli.init as init_mod

    monkeypatch.setattr(
        "setforge.cli.init.radiolist_dialog",
        _DialogRecorder([init_mod.SourceChoice.GIT]),
    )
    monkeypatch.setattr("setforge.cli.init.input_dialog", _DialogRecorder([None]))
    spec = init_mod._prompt_source_config(
        no_prompt=False, path_source=None, git_source=None, git_ref="main"
    )
    assert spec.choice is init_mod.SourceChoice.SKIP


def test_source_prompt_skip_selection_returns_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SKIP selection returns SKIP without touching input_dialog."""
    import setforge.cli.init as init_mod

    monkeypatch.setattr(
        "setforge.cli.init.radiolist_dialog",
        _DialogRecorder([init_mod.SourceChoice.SKIP]),
    )

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("input_dialog must not be called for SKIP")

    monkeypatch.setattr("setforge.cli.init.input_dialog", _boom)
    spec = init_mod._prompt_source_config(
        no_prompt=False, path_source=None, git_source=None, git_ref="main"
    )
    assert spec.choice is init_mod.SourceChoice.SKIP


# ---------------------------------------------------------------------------
# Dataclass / enum shape tests
# ---------------------------------------------------------------------------


def test_capabilitystate_strenum_values() -> None:
    assert CapabilityState.ENABLED.value == "enabled"
    assert CapabilityState.DISABLED.value == "disabled"
    assert str(CapabilityState.ENABLED) == "enabled"


def test_binaryprobe_is_frozen_slotted() -> None:
    bp = BinaryProbe(name="uv", required=True, resolved_path=None, fix_hint="x")
    with pytest.raises(FrozenInstanceError):
        bp.name = "claude"  # type: ignore[misc]
    assert "__slots__" in dir(type(bp))


def test_dirprobe_is_frozen_slotted() -> None:
    dp = DirProbe(path=Path("/x"), exists=False, will_create=True)
    with pytest.raises(FrozenInstanceError):
        dp.exists = True  # type: ignore[misc]
    assert "__slots__" in dir(type(dp))


def test_capabilityprobe_is_frozen_slotted() -> None:
    cp = CapabilityProbe(
        label="L", state=CapabilityState.ENABLED, reason="", newly_enabled=False
    )
    with pytest.raises(FrozenInstanceError):
        cp.reason = "n"  # type: ignore[misc]
    assert "__slots__" in dir(type(cp))


def test_envprobe_is_frozen_slotted() -> None:
    ep = EnvProbe(binaries=(), dirs=(), capabilities=())
    with pytest.raises(FrozenInstanceError):
        ep.binaries = ()  # type: ignore[misc]
    assert "__slots__" in dir(type(ep))


# ---------------------------------------------------------------------------
# probe_environment behavior
# ---------------------------------------------------------------------------


def test_probe_environment_returns_three_binaries(home: Path) -> None:
    probe = probe_environment()
    names = tuple(b.name for b in probe.binaries)
    assert names == ("uv", "claude", "code")


def test_probe_environment_uv_is_required_others_optional(home: Path) -> None:
    probe = probe_environment()
    by_name = {b.name: b for b in probe.binaries}
    assert by_name["uv"].required is True
    assert by_name["claude"].required is False
    assert by_name["code"].required is False


def test_probe_environment_returns_three_dirs(home: Path) -> None:
    probe = probe_environment()
    assert len(probe.dirs) == 3
    paths = tuple(d.path for d in probe.dirs)
    assert paths[0] == home / ".config" / "setforge"
    assert paths[1] == home / ".config" / "setforge" / "local.yaml"
    assert paths[2] == home / ".local" / "share" / "setforge" / "host-local"


def test_probe_environment_dirs_reflect_existence(home: Path) -> None:
    (home / ".config" / "setforge").mkdir(parents=True)
    probe = probe_environment()
    by_path = {d.path: d for d in probe.dirs}
    cfg = by_path[home / ".config" / "setforge"]
    host_local = by_path[home / ".local" / "share" / "setforge" / "host-local"]
    assert cfg.exists is True
    assert cfg.will_create is False
    assert host_local.exists is False
    assert host_local.will_create is True


def test_probe_environment_capabilities_disabled_when_binary_missing(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("setforge.cli._init_helpers.resolve_binary", lambda name: None)
    monkeypatch.setattr("setforge.cli._init_helpers._resolve_uv", lambda: None)
    probe = probe_environment()
    caps_by_label = {c.label: c for c in probe.capabilities}
    assert caps_by_label["claude_plugins reconcile"].state is CapabilityState.DISABLED
    assert "claude binary missing" in caps_by_label["claude_plugins reconcile"].reason
    assert (
        caps_by_label["vscode_extensions reconcile"].state is CapabilityState.DISABLED
    )


def test_probe_environment_capabilities_enabled_when_binary_present(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "setforge.cli._init_helpers.resolve_binary",
        lambda name: Path("/fake") / name,
    )
    monkeypatch.setattr(
        "setforge.cli._init_helpers._resolve_uv", lambda: Path("/fake/uv")
    )
    probe = probe_environment()
    caps_by_label = {c.label: c for c in probe.capabilities}
    assert caps_by_label["claude_plugins reconcile"].state is CapabilityState.ENABLED
    assert caps_by_label["vscode_extensions reconcile"].state is CapabilityState.ENABLED


def test_probe_environment_marks_newly_enabled_against_prev_state(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prev = EnvProbe(
        binaries=(
            BinaryProbe(
                name="uv", required=True, resolved_path=Path("/u"), fix_hint=""
            ),
            BinaryProbe(name="claude", required=False, resolved_path=None, fix_hint=""),
            BinaryProbe(name="code", required=False, resolved_path=None, fix_hint=""),
        ),
        dirs=(),
        capabilities=(),
    )
    monkeypatch.setattr(
        "setforge.cli._init_helpers.resolve_binary",
        lambda name: Path("/fake") / name if name == "claude" else None,
    )
    monkeypatch.setattr("setforge.cli._init_helpers._resolve_uv", lambda: Path("/u"))
    probe = probe_environment(prev_state=prev)
    caps = {c.label: c for c in probe.capabilities}
    assert caps["claude_plugins reconcile"].newly_enabled is True
    assert caps["vscode_extensions reconcile"].newly_enabled is False


# ---------------------------------------------------------------------------
# is_initialized + helpers
# ---------------------------------------------------------------------------


def test_is_initialized_false_when_no_local_yaml(home: Path) -> None:
    probe = probe_environment()
    assert is_initialized(probe) is False


def test_is_initialized_true_when_sentinel_present(home: Path) -> None:
    cfg = home / ".config" / "setforge"
    cfg.mkdir(parents=True)
    (cfg / "local.yaml").write_text(
        "# setforge host-local config — never tracked in git.\n", encoding="utf-8"
    )
    # is_initialized requires the host-local dir too; create it.
    host_local_dir_path().mkdir(parents=True)
    probe = probe_environment()
    assert is_initialized(probe) is True


def test_is_initialized_true_when_binaries_block_present(home: Path) -> None:
    cfg = home / ".config" / "setforge"
    cfg.mkdir(parents=True)
    # Use sys.executable as a guaranteed-valid binary path so
    # resolve_binary's validate gate doesn't reject the override
    # before probe_environment returns.
    (cfg / "local.yaml").write_text(
        f"binaries:\n  code: {sys.executable}\n", encoding="utf-8"
    )
    host_local_dir_path().mkdir(parents=True)
    probe = probe_environment()
    assert is_initialized(probe) is True


def test_is_initialized_false_when_yaml_is_empty(home: Path) -> None:
    cfg = home / ".config" / "setforge"
    cfg.mkdir(parents=True)
    (cfg / "local.yaml").write_text("", encoding="utf-8")
    host_local_dir_path().mkdir(parents=True)
    probe = probe_environment()
    assert is_initialized(probe) is False


def test_is_initialized_false_when_host_local_missing(home: Path) -> None:
    """Stub-only state (root callback wrote local.yaml) doesn't count as initialized."""
    cfg = home / ".config" / "setforge"
    cfg.mkdir(parents=True)
    (cfg / "local.yaml").write_text("# setforge host-local config\n", encoding="utf-8")
    probe = probe_environment()
    assert is_initialized(probe) is False


def test_config_dir_path_under_home(home: Path) -> None:
    assert config_dir_path() == home / ".config" / "setforge"


def test_host_local_dir_path_under_home(home: Path) -> None:
    assert (
        host_local_dir_path() == home / ".local" / "share" / "setforge" / "host-local"
    )


def test_mkdir_with_retry_creates_parents(home: Path) -> None:
    target = home / "a" / "b" / "c"
    _mkdir_with_retry(target)
    assert target.is_dir()


def test_mkdir_with_retry_idempotent(home: Path) -> None:
    target = home / "x"
    _mkdir_with_retry(target)
    _mkdir_with_retry(target)
    assert target.is_dir()


def test_backup_suffix_format() -> None:
    suffix = backup_suffix_now()
    # YYYYMMDDTHHMMSSZ — 8 digits, T, 6 digits, Z = 16 chars.
    assert len(suffix) == 16
    assert suffix[8] == "T"
    assert suffix[-1] == "Z"
    assert suffix[:8].isdigit()
    assert suffix[9:15].isdigit()


# ---------------------------------------------------------------------------
# CLI behavior — --check / --no-prompt / --force
# ---------------------------------------------------------------------------


def test_init_help_shows_force_and_check_flags(home: Path) -> None:
    result = _HELP_RUNNER.invoke(app, ["init", "--help"])
    assert result.exit_code == 0
    plain = _strip_ansi(result.output)
    assert "--force" in plain
    assert "--check" in plain


def test_init_check_prints_checking_and_exits_zero(home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--check"])
    assert result.exit_code == 0
    assert "checking environment" in result.output
    assert "checking config directories" in result.output


def test_init_check_does_not_write_host_local_dir(home: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["init", "--check"])
    # Root callback creates ~/.config/setforge/local.yaml; --check
    # must NOT additionally create the host-local share dir.
    assert not (home / ".local" / "share" / "setforge" / "host-local").exists()


def test_init_fresh_creates_three_paths_with_no_prompt(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ensure no prior init state exists at the start (root callback runs first
    # and may create local.yaml; we wipe to simulate a true fresh init).
    cfg = home / ".config" / "setforge"
    runner = CliRunner()
    # Source-config & apply prompts are bypassed under --no-prompt; the
    # force prompt is not reached because no prior local.yaml exists.
    _patch_init_dialog(monkeypatch, returns=[])
    if (cfg / "local.yaml").exists():
        (cfg / "local.yaml").unlink()
    result = runner.invoke(app, ["init", "--no-prompt"])
    assert result.exit_code == 0, result.output
    assert (cfg / "local.yaml").exists()
    assert (home / ".local" / "share" / "setforge" / "host-local").is_dir()
    assert "init complete" in result.output


def test_init_reinit_is_idempotent_without_force(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = home / ".config" / "setforge"
    cfg.mkdir(parents=True)
    # Sentinel + an existing-binary override so resolve_binary's
    # validate gate doesn't reject it before the reinit branch runs.
    (cfg / "local.yaml").write_text(
        f"# setforge host-local config\nbinaries:\n  code: {sys.executable}\n",
        encoding="utf-8",
    )
    host_local_dir_path().mkdir(parents=True)
    runner = CliRunner()
    _patch_init_dialog(monkeypatch, returns=[])
    result = runner.invoke(app, ["init", "--no-prompt"])
    assert result.exit_code == 0, result.output
    assert "nothing to create" in result.output
    # Customization preserved on idempotent reinit.
    content = (cfg / "local.yaml").read_text(encoding="utf-8")
    assert sys.executable in content


def test_init_force_with_backup_creates_timestamped_bak(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = home / ".config" / "setforge"
    cfg.mkdir(parents=True)
    (cfg / "local.yaml").write_text(
        "# setforge host-local config\ncustom: marker\n", encoding="utf-8"
    )
    runner = CliRunner()
    _patch_init_dialog(monkeypatch, returns=[])  # --no-prompt → with-backup
    result = runner.invoke(app, ["init", "--force", "--no-prompt"])
    assert result.exit_code == 0, result.output
    backups = list(cfg.glob("local.yaml.bak.*"))
    assert len(backups) == 1, f"expected one backup, got {backups}"
    # Backup retains the original custom marker.
    assert "custom: marker" in backups[0].read_text(encoding="utf-8")
    # New local.yaml is the fresh stub.
    new = (cfg / "local.yaml").read_text(encoding="utf-8")
    assert "setforge host-local config" in new


def test_init_no_prompt_path_source_skips_source_prompt(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Wipe any pre-existing local.yaml so we hit the fresh-init branch.
    cfg = home / ".config" / "setforge"
    if (cfg / "local.yaml").exists():
        (cfg / "local.yaml").unlink()
    recorder = _patch_init_dialog(monkeypatch, returns=[])
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["init", "--no-prompt", "--path-source", str(home / "fake-source")],
    )
    assert result.exit_code == 0, result.output
    # No radiolist_dialog calls under --no-prompt.
    assert recorder.calls == []
