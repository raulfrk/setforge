"""Unit tests for :mod:`setforge.cli.upgrade`."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from setforge._pypi_client import PyPIVersionInfo
from setforge.cli import app
from setforge.cli import upgrade as upgrade_mod
from setforge.cli.upgrade import (
    _CHANGELOG_URL,
    UpgradeChoice,
    UpgradePlan,
    _build_upgrade_plan,
    _confirm_upgrade,
)

# Captured from ``uv tool list`` on a real host (uv 0.9.x): each package prints as
# ``<name> v<version>`` with its executables on ``- <name>`` lines beneath. The
# hand-written ``setforge <version>`` shape used by earlier mocks never occurs in
# practice, which is how the post-upgrade check stayed green.
_REAL_UV_TOOL_LIST = """\
ansible-core v2.20.3
- ansible
- ansible-playbook
molecule v26.4.0
- molecule
setforge v1.3.2
- setforge
"""


@pytest.mark.parametrize("operation", ["upgrade", "verify", "migrate"])
def test_unlaunchable_uv_is_reported_as_upgrade_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from setforge.errors import UpgradeError

    # which() finds this executable, but the OS cannot load its interpreter.
    uv = tmp_path / "uv"
    uv.write_text("#!/missing-setforge-test-interpreter\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    action = {
        "upgrade": lambda: upgrade_mod._run_uv_tool_upgrade(
            target="9.9.9", pinned=True
        ),
        "verify": lambda: upgrade_mod._verify_post_upgrade(expected="9.9.9"),
        "migrate": lambda: upgrade_mod._run_migrate_check_subprocess(
            config=tmp_path / "setforge.yaml"
        ),
    }[operation]
    with pytest.raises(UpgradeError, match="could not start"):
        action()


# ---------------------------------------------------------------------------
# Confirm panel rendering
# ---------------------------------------------------------------------------


def _make_plan(
    *,
    target: str = "0.3.0",
    current: str = "0.2.0",
    is_major_bump: bool = False,
) -> UpgradePlan:
    return UpgradePlan(
        current_version=current,
        target_version=target,
        is_major_bump=is_major_bump,
    )


class _DialogRecorder:
    def __init__(self, return_value: object) -> None:
        self._return_value = return_value
        self.call_count = 0
        self.args: list[tuple[Any, ...]] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> object:
        self.call_count += 1
        self.args.append(args)
        self.kwargs.append(kwargs)
        return self._return_value

    def initial_value(self, call: int = 0) -> object:
        buttons = self.args[call][0]
        return buttons[self.kwargs[call]["initial"]].value


def _patch_button_bar(
    monkeypatch: pytest.MonkeyPatch, *, return_value: object
) -> _DialogRecorder:
    recorder = _DialogRecorder(return_value)
    monkeypatch.setattr("setforge.ui.widgets.button_bar", recorder)
    return recorder


def test_confirm_panel_prints_the_changelog_url_instead_of_release_notes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_button_bar(monkeypatch, return_value=UpgradeChoice.UPGRADE)
    _confirm_upgrade(_make_plan(), yes=False)
    captured = capsys.readouterr().out
    assert _CHANGELOG_URL in captured
    assert "schema impact" not in captured
    assert "release notes" not in captured


def test_confirm_panel_offers_upgrade_and_upgrade_with_migrate_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _patch_button_bar(monkeypatch, return_value=UpgradeChoice.UPGRADE)
    _confirm_upgrade(_make_plan(), yes=False)
    offered = {button.value for button in recorder.args[0][0]}
    assert offered == set(UpgradeChoice)


def test_confirm_panel_no_prompt_picks_upgrade_and_migrate_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _patch_button_bar(monkeypatch, return_value=UpgradeChoice.ABORT)
    choice = _confirm_upgrade(_make_plan(), yes=True)
    assert choice is UpgradeChoice.UPGRADE_AND_MIGRATE_CHECK
    assert recorder.call_count == 0


def test_confirm_panel_esc_returns_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    from setforge.ui.widgets import CANCEL

    _patch_button_bar(monkeypatch, return_value=CANCEL)
    choice = _confirm_upgrade(_make_plan(), yes=False)
    assert choice is UpgradeChoice.ABORT


def test_confirm_panel_preselects_upgrade_and_migrate_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _patch_button_bar(
        monkeypatch, return_value=UpgradeChoice.UPGRADE_AND_MIGRATE_CHECK
    )
    _confirm_upgrade(_make_plan(), yes=False)
    assert recorder.initial_value() is UpgradeChoice.UPGRADE_AND_MIGRATE_CHECK


def test_changelog_url_matches_the_package_metadata() -> None:
    import tomllib

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    urls = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["urls"]
    assert urls["Changelog"] == _CHANGELOG_URL


# ---------------------------------------------------------------------------
# _build_upgrade_plan integration (PyPI + CHANGELOG mocked at function-level)
# ---------------------------------------------------------------------------


def _patch_pypi(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str,
    is_prerelease: bool = False,
    yanked: bool = False,
) -> None:
    def fake_fetch(**_kwargs: Any) -> PyPIVersionInfo:
        return PyPIVersionInfo(
            version=version,
            is_prerelease=is_prerelease,
            yanked=yanked,
            yanked_reason=None,
        )

    monkeypatch.setattr("setforge.cli.upgrade.fetch_latest_version", fake_fetch)

    def fake_fetch_version(*, version: str, **_kwargs: Any) -> PyPIVersionInfo:
        return PyPIVersionInfo(
            version=version,
            is_prerelease=is_prerelease,
            yanked=yanked,
            yanked_reason=None,
        )

    monkeypatch.setattr("setforge.cli.upgrade.fetch_version_info", fake_fetch_version)


def _newer_version() -> str:
    """Return a version string strictly greater than the installed one.

    ``upgrade()`` short-circuits to exit 0 when the planned target equals the
    installed ``setforge.__version__`` ("already on the latest version"). The
    integration tests below need a target that is *newer* than whatever is
    installed; deriving it from ``__version__`` keeps them immune to release
    bumps that would otherwise collide with a hardcoded literal. Only the
    leading numeric ``MAJOR.MINOR.PATCH`` segment is parsed, so a future
    ``.devN`` / ``+local`` / ``rcN`` suffix on ``__version__`` is tolerated.
    """
    import re

    from setforge import __version__

    match = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", __version__)
    if match is None:  # unparseable — fall back to an unmistakably newer value
        return "999.0.0"
    major, minor, patch = (int(group) for group in match.groups())
    return f"{major}.{minor}.{patch + 1}"


_NEXT_VERSION = _newer_version()


def test_build_upgrade_plan_passes_through_pypi_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pypi(monkeypatch, version="0.5.0")
    plan = _build_upgrade_plan(to=None, prerelease=False)
    assert plan.target_version == "0.5.0"


def test_build_upgrade_plan_to_pins_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pypi(monkeypatch, version="0.5.0")
    plan = _build_upgrade_plan(to="0.4.2", prerelease=False)
    assert plan.target_version == "0.4.2"
    assert any("--to=0.4.2 pins" in w for w in plan.extra_warnings)


def test_build_upgrade_plan_surfaces_pinned_yanked_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pypi(monkeypatch, version="0.5.0")
    monkeypatch.setattr(
        "setforge.cli.upgrade.fetch_version_info",
        lambda **_kwargs: PyPIVersionInfo(
            version="0.4.2",
            is_prerelease=False,
            yanked=True,
            yanked_reason="broken release",
        ),
    )

    result = CliRunner().invoke(app, ["upgrade", "--check", "--to", "0.4.2"])

    assert result.exit_code == 0
    assert "YANKED" in result.output
    assert "broken release" in result.output


def test_build_upgrade_plan_surfaces_pinned_prerelease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pypi(monkeypatch, version="9.9.9", is_prerelease=False)
    monkeypatch.setattr(
        "setforge.cli.upgrade.fetch_version_info",
        lambda **_kwargs: PyPIVersionInfo(
            version="8.0.0rc1",
            is_prerelease=True,
            yanked=False,
            yanked_reason=None,
        ),
    )

    result = CliRunner().invoke(app, ["upgrade", "--check", "--to", "8.0.0rc1"])

    assert result.exit_code == 0
    assert "PRE-RELEASE" in result.output
    assert "8.0.0rc1 is a pre-release" in result.output


def test_build_upgrade_plan_rejects_invalid_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pypi(monkeypatch, version="0.5.0")
    from setforge.errors import UpgradeError

    with pytest.raises(UpgradeError, match="not a valid version"):
        _build_upgrade_plan(to="not-a-version", prerelease=False)


def test_build_upgrade_plan_accepts_canonical_prerelease_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2.0.0rc1 is how pip and PyPI write a release candidate."""
    _patch_pypi(monkeypatch, version="2.0.0rc1")

    plan = _build_upgrade_plan(to="2.0.0rc1", prerelease=False)

    assert plan.target_version == "2.0.0rc1"


def test_build_upgrade_plan_canonicalises_a_non_canonical_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-canonical spelling resolves to what uv will install and report."""
    _patch_pypi(monkeypatch, version="1.3.2")

    plan = _build_upgrade_plan(to="1.3.2-1", prerelease=False)

    assert plan.target_version == "1.3.2.post1"


# ---------------------------------------------------------------------------
# CLI entry point — --check + full upgrade flow
# ---------------------------------------------------------------------------


def _patch_subprocess_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    responses: Iterable[subprocess.CompletedProcess[str]],
) -> list[list[str]]:
    """Replace ``subprocess.run`` with a scripted-response generator."""
    calls: list[list[str]] = []
    iterator = iter(responses)

    def fake_run(
        cmd: list[str],
        *,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
        timeout: float | None = None,
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        try:
            return next(iterator)
        except StopIteration as exc:
            raise AssertionError(f"unexpected subprocess call: {cmd!r}") from exc

    monkeypatch.setattr(upgrade_mod.subprocess, "run", fake_run)
    return calls


def test_cli_upgrade_check_mode_does_not_mutate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``--check`` neither shells out nor bootstraps host-local config."""
    _patch_pypi(monkeypatch, version="0.3.0")

    def fail_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("--check must not invoke subprocess.run")

    monkeypatch.setattr(upgrade_mod.subprocess, "run", fail_run)
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    runner = CliRunner()
    local_config = tmp_path / "local.yaml"
    assert not local_config.exists()
    result = runner.invoke(app, ["upgrade", "--check"])
    assert result.exit_code == 0, result.output
    assert "0.3.0" in result.output
    assert _CHANGELOG_URL in result.output
    assert "schema impact" not in result.output
    assert not local_config.exists()


def test_cli_upgrade_already_latest_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge import __version__ as current

    _patch_pypi(monkeypatch, version=current)
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    runner = CliRunner()
    result = runner.invoke(app, ["upgrade", "--no-prompt"])
    assert result.exit_code == 0, result.output
    assert "already on the latest version" in result.output


class _FakeTty:
    @staticmethod
    def isatty() -> bool:
        return True


def test_cli_upgrade_plain_upgrade_choice_skips_migrate_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choosing "Upgrade": pypi → wrap → verify → changelog URL + rollback line."""
    _patch_pypi(monkeypatch, version=_NEXT_VERSION)
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    # CliRunner swaps sys.stdin during invoke, so pretend a terminal is attached.
    monkeypatch.setattr(upgrade_mod, "sys", SimpleNamespace(stdin=_FakeTty()))
    _patch_button_bar(monkeypatch, return_value=UpgradeChoice.UPGRADE)

    responses = [
        subprocess.CompletedProcess(
            args=["uv", "tool", "upgrade", "setforge"],
            returncode=0,
            stdout=(
                f"Resolved 12 packages in 200ms\nInstalled setforge=={_NEXT_VERSION}\n"
            ),
            stderr="",
        ),
        subprocess.CompletedProcess(
            args=["uv", "tool", "list"],
            returncode=0,
            stdout=f"setforge v{_NEXT_VERSION}\n",
            stderr="",
        ),
    ]
    calls = _patch_subprocess_run(monkeypatch, responses=responses)
    runner = CliRunner()
    result = runner.invoke(app, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert calls[0][1:] == ["tool", "upgrade", "setforge"]
    assert calls[1][1:] == ["tool", "list"]
    assert "rollback:" in result.output
    assert f"upgraded to {_NEXT_VERSION}" in result.output
    assert _CHANGELOG_URL in result.output


def test_cli_upgrade_full_flow_with_migrate_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--no-prompt`` runs the default choice: upgrade, then migrate --check."""
    _patch_pypi(monkeypatch, version=_NEXT_VERSION)
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text("version: 1\ntracked_files: {}\n", encoding="utf-8")
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    responses = [
        subprocess.CompletedProcess(
            args=["uv", "tool", "upgrade", "setforge"],
            returncode=0,
            stdout=f"Installed setforge=={_NEXT_VERSION}\n",
            stderr="",
        ),
        subprocess.CompletedProcess(
            args=["uv", "tool", "list"],
            returncode=0,
            stdout=f"setforge v{_NEXT_VERSION}\n",
            stderr="",
        ),
        subprocess.CompletedProcess(
            args=[
                "uv",
                "run",
                "setforge",
                "migrate",
                "--check",
                "--config",
                str(cfg),
            ],
            returncode=0,
            stdout="no migrations pending\n",
            stderr="",
        ),
    ]
    calls = _patch_subprocess_run(monkeypatch, responses=responses)
    runner = CliRunner()
    result = runner.invoke(app, ["upgrade", "--no-prompt", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert len(calls) == 3
    assert calls[2][1:] == [
        "run",
        "setforge",
        "migrate",
        "--check",
        "--config",
        str(cfg),
    ]
    assert "no migrations pending" in result.output


def test_cli_upgrade_migrate_check_soft_fails_when_command_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_pypi(monkeypatch, version=_NEXT_VERSION)
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text("version: 1\ntracked_files: {}\n", encoding="utf-8")
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    responses = [
        subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"Installed setforge=={_NEXT_VERSION}\n",
            stderr="",
        ),
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"setforge {_NEXT_VERSION}\n", stderr=""
        ),
        subprocess.CompletedProcess(
            args=[],
            returncode=2,
            stdout="",
            stderr="Error: No such command 'migrate'.",
        ),
    ]
    _patch_subprocess_run(monkeypatch, responses=responses)
    runner = CliRunner()
    result = runner.invoke(app, ["upgrade", "--no-prompt", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "is not available" in result.output


def test_migrate_check_timeout_raises_upgrade_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")

    def time_out(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(
            cmd=["uv", "run", "setforge", "migrate", "--check"], timeout=60
        )

    monkeypatch.setattr(upgrade_mod.subprocess, "run", time_out)

    with pytest.raises(
        upgrade_mod.UpgradeError,
        match=r"setforge migrate --check.*timed out after 60 seconds",
    ) as excinfo:
        upgrade_mod._run_migrate_check_subprocess(config=tmp_path / "setforge.yaml")

    assert isinstance(excinfo.value.__cause__, subprocess.TimeoutExpired)


def test_cli_upgrade_skips_migrate_check_when_no_manifest_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unresolvable manifest must not fail an upgrade that succeeded."""
    _patch_pypi(monkeypatch, version=_NEXT_VERSION)
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.chdir(tmp_path)  # no setforge.yaml in any source layer
    responses = [
        subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"Installed setforge=={_NEXT_VERSION}\n",
            stderr="",
        ),
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"setforge v{_NEXT_VERSION}\n", stderr=""
        ),
    ]
    calls = _patch_subprocess_run(monkeypatch, responses=responses)
    runner = CliRunner()

    result = runner.invoke(app, ["upgrade", "--no-prompt"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 2  # no migrate subprocess was attempted
    assert "skipping migrate --check" in result.output


def test_cli_upgrade_skips_migrate_check_when_host_config_is_broken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pydantic ValidationError must not fail an upgrade that succeeded."""
    _patch_pypi(monkeypatch, version=_NEXT_VERSION)
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    broken = tmp_path / "local.yaml"
    broken.write_text("source: {kind: bogus}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    responses = [
        subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"Installed setforge=={_NEXT_VERSION}\n",
            stderr="",
        ),
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"setforge v{_NEXT_VERSION}\n", stderr=""
        ),
    ]
    calls = _patch_subprocess_run(monkeypatch, responses=responses)

    result = CliRunner().invoke(app, ["upgrade", "--no-prompt"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert "skipping migrate --check" in result.output


def test_cli_upgrade_parses_nothing_to_upgrade_as_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per research brief §2: STDOUT 'Nothing to upgrade' = no-op, exit 0."""
    _patch_pypi(monkeypatch, version=_NEXT_VERSION)
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    responses = [
        subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Nothing to upgrade.\n",
            stderr="",
        ),
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"setforge {_NEXT_VERSION}\n", stderr=""
        ),
    ]
    _patch_subprocess_run(monkeypatch, responses=responses)
    runner = CliRunner()
    result = runner.invoke(app, ["upgrade", "--no-prompt"])
    assert result.exit_code == 0, result.output
    assert "no-op" in result.output


def test_cli_upgrade_wrap_failure_surfaces_upgrade_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pypi(monkeypatch, version=_NEXT_VERSION)
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    responses = [
        subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="permission denied"
        ),
    ]
    _patch_subprocess_run(monkeypatch, responses=responses)
    runner = CliRunner()
    # The Typer top-level main() handler renders SetforgeError → exit 1; the
    # CliRunner bypasses that handler, so the UpgradeError raises directly.
    result = runner.invoke(app, ["upgrade", "--no-prompt"])
    assert result.exit_code != 0
    assert (
        "permission denied" in (str(result.exception) if result.exception else "")
        or "permission denied" in result.output
    )


def test_cli_upgrade_post_verify_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pypi(monkeypatch, version=_NEXT_VERSION)
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    responses = [
        subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"Installed setforge=={_NEXT_VERSION}\n",
            stderr="",
        ),
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout="setforge v0.2.0\n", stderr=""
        ),
    ]
    _patch_subprocess_run(monkeypatch, responses=responses)
    runner = CliRunner()
    result = runner.invoke(app, ["upgrade", "--no-prompt"])
    assert result.exit_code != 0
    excmsg = str(result.exception) if result.exception else result.output
    assert "post-upgrade verification" in excmsg


def test_post_verify_accepts_real_uv_tool_list_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real ``uv tool list`` prints ``setforge vX.Y.Z``; a good upgrade passes."""
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    _patch_subprocess_run(
        monkeypatch,
        responses=[
            subprocess.CompletedProcess(
                args=["uv", "tool", "list"],
                returncode=0,
                stdout=_REAL_UV_TOOL_LIST,
                stderr="",
            )
        ],
    )

    upgrade_mod._verify_post_upgrade(expected="1.3.2")


def test_post_verify_rejects_mismatch_in_real_uv_tool_list_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    _patch_subprocess_run(
        monkeypatch,
        responses=[
            subprocess.CompletedProcess(
                args=["uv", "tool", "list"],
                returncode=0,
                stdout=_REAL_UV_TOOL_LIST,
                stderr="",
            )
        ],
    )

    with pytest.raises(upgrade_mod.UpgradeError, match="post-upgrade verification"):
        upgrade_mod._verify_post_upgrade(expected="0.2.0")


def test_post_verify_timeout_raises_upgrade_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")

    def time_out(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(cmd=["uv", "tool", "list"], timeout=30)

    monkeypatch.setattr(upgrade_mod.subprocess, "run", time_out)

    with pytest.raises(
        upgrade_mod.UpgradeError,
        match=r"post-upgrade verification .* timed out after 30 seconds",
    ) as excinfo:
        upgrade_mod._verify_post_upgrade(expected="1.3.2")

    assert isinstance(excinfo.value.__cause__, subprocess.TimeoutExpired)


@pytest.mark.parametrize(
    "reported", ["1.3.2.dev1", "1.3.2.post1", "1.3.2+local", "0.2.0"]
)
def test_post_verify_rejects_non_identical_reported_version(
    monkeypatch: pytest.MonkeyPatch, reported: str
) -> None:
    """Anything other than the exact target version fails, suffix variants included."""
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    _patch_subprocess_run(
        monkeypatch,
        responses=[
            subprocess.CompletedProcess(
                args=["uv", "tool", "list"],
                returncode=0,
                stdout=f"setforge v{reported}\n- setforge\n",
                stderr="",
            )
        ],
    )

    with pytest.raises(upgrade_mod.UpgradeError, match="post-upgrade verification"):
        upgrade_mod._verify_post_upgrade(expected="1.3.2")


def test_cli_upgrade_pypi_fetch_error_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge.errors import PyPIFetchError

    def boom(**_kwargs: Any) -> PyPIVersionInfo:
        raise PyPIFetchError("no network")

    monkeypatch.setattr("setforge.cli.upgrade.fetch_latest_version", boom)
    runner = CliRunner()
    result = runner.invoke(app, ["upgrade", "--check"])
    assert result.exit_code == 1
    assert "no network" in result.output


# ---------------------------------------------------------------------------
# Non-TTY confirm guard
# ---------------------------------------------------------------------------


def test_cli_upgrade_non_tty_without_no_prompt_raises_and_skips_button_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge.errors import ConfirmRequiresInteractive

    _patch_pypi(monkeypatch, version=_NEXT_VERSION)
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    recorder = _patch_button_bar(monkeypatch, return_value=UpgradeChoice.UPGRADE)

    def fail_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("non-TTY confirm guard must not shell out")

    monkeypatch.setattr(upgrade_mod.subprocess, "run", fail_run)

    runner = CliRunner()
    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code != 0
    assert isinstance(result.exception, ConfirmRequiresInteractive)
    assert recorder.call_count == 0


def test_cli_upgrade_no_prompt_non_tty_still_auto_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-prompt`` on a non-TTY auto-applies (``yes=True``) and proceeds."""
    _patch_pypi(monkeypatch, version=_NEXT_VERSION)
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    recorder = _patch_button_bar(monkeypatch, return_value=UpgradeChoice.ABORT)

    responses = [
        subprocess.CompletedProcess(
            args=["uv", "tool", "upgrade", "setforge"],
            returncode=0,
            stdout=f"Installed setforge=={_NEXT_VERSION}\n",
            stderr="",
        ),
        subprocess.CompletedProcess(
            args=["uv", "tool", "list"],
            returncode=0,
            stdout=f"setforge v{_NEXT_VERSION}\n",
            stderr="",
        ),
    ]
    calls = _patch_subprocess_run(monkeypatch, responses=responses)

    runner = CliRunner()
    result = runner.invoke(app, ["upgrade", "--no-prompt"])

    assert result.exit_code == 0, result.output
    assert recorder.call_count == 0
    assert len(calls) == 2
    assert calls[0][1:] == ["tool", "upgrade", "setforge"]
    assert f"upgraded to {_NEXT_VERSION}" in result.output


# ---------------------------------------------------------------------------
# Wizard discipline guard
# ---------------------------------------------------------------------------


def test_upgrade_module_uses_only_button_bar_no_typer_prompt() -> None:
    """Grep upgrade.py for forbidden prompt shapes."""
    text = upgrade_mod.__file__ or ""
    assert text  # path must be present
    from pathlib import Path

    source = Path(text).read_text(encoding="utf-8")
    for forbidden in (
        "typer.prompt",
        "typer.confirm",
        "click.prompt",
        "click.confirm",
        "input(",
    ):
        assert forbidden not in source, f"forbidden prompt {forbidden!r} in upgrade.py"


# ---------------------------------------------------------------------------
# _run_uv_tool_upgrade — --to version pinning
# ---------------------------------------------------------------------------


def test_pinned_upgrade_uses_install_reinstall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned target installs the exact version via reinstall-package —
    `uv tool upgrade` cannot target a version."""
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    calls = _patch_subprocess_run(
        monkeypatch,
        responses=[
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Installed setforge==9.9.9\n", stderr=""
            )
        ],
    )
    upgrade_mod._run_uv_tool_upgrade(target="9.9.9", pinned=True)
    assert calls[0][1:] == [
        "tool",
        "install",
        "--reinstall-package",
        "setforge",
        "setforge==9.9.9",
    ]


def test_unpinned_upgrade_keeps_tool_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --to, the unpinned path stays `uv tool upgrade setforge`."""
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    calls = _patch_subprocess_run(
        monkeypatch,
        responses=[
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Installed setforge==9.9.9\n", stderr=""
            )
        ],
    )
    upgrade_mod._run_uv_tool_upgrade(target="9.9.9", pinned=False)
    assert calls[0][1:] == ["tool", "upgrade", "setforge"]


def test_uv_tool_upgrade_timeout_raises_upgrade_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")

    def time_out(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(
            cmd=["uv", "tool", "upgrade", "setforge"], timeout=120
        )

    monkeypatch.setattr(upgrade_mod.subprocess, "run", time_out)

    with pytest.raises(
        upgrade_mod.UpgradeError,
        match=r"uv tool upgrade timed out after 120 seconds",
    ) as excinfo:
        upgrade_mod._run_uv_tool_upgrade(target="9.9.9", pinned=False)

    assert isinstance(excinfo.value.__cause__, subprocess.TimeoutExpired)


def test_pinned_failure_message_names_install_not_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure on the pinned path reports the command actually run
    (`uv tool install`), not a hard-coded `uv tool upgrade`."""
    from setforge.errors import UpgradeError

    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    _patch_subprocess_run(
        monkeypatch,
        responses=[
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
        ],
    )
    with pytest.raises(UpgradeError, match=r"uv tool install failed: boom"):
        upgrade_mod._run_uv_tool_upgrade(target="9.9.9", pinned=True)
