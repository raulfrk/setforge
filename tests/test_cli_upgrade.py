"""Unit tests for :mod:`setforge.cli.upgrade`."""

from __future__ import annotations

import http.server
import json
import shutil
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

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


class _QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A client that gave up or refused our certificate is expected here."""


class FakePyPI:
    """Loopback HTTP server standing in for pypi.org.

    The real urllib stack talks to it, so redirects, status codes, timeouts and
    bad bodies behave as they would against PyPI, and nothing leaves the
    machine. ``requests`` records every ``(path, headers)`` it served.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.stall = False
        self._unblock = threading.Event()
        self._routes: dict[str, tuple[int, bytes, dict[str, str]]] = {}
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                fake.requests.append((self.path, dict(self.headers.items())))
                if fake.stall:
                    fake._unblock.wait(30)
                status, body, headers = fake._routes.get(
                    self.path, (404, b"not found", {})
                )
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = _QuietServer(("127.0.0.1", 0), Handler)
        threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        ).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/pypi"

    def route(
        self,
        path: str,
        status: int = 200,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._routes[path] = (status, body, headers or {})

    def publish_releases(self, releases: dict[str, list[dict[str, Any]]]) -> None:
        body = json.dumps({"info": {}, "releases": releases}).encode("utf-8")
        self.route("/pypi/setforge/json", 200, body)

    def publish(
        self, *versions: str, yanked: dict[str, str | None] | None = None
    ) -> None:
        """Serve a project JSON listing ``versions``; ``yanked`` maps to a reason."""
        yanked = yanked or {}
        self.publish_releases(
            {
                v: [{"yanked": v in yanked, "yanked_reason": yanked.get(v)}]
                for v in versions
            }
        )

    def close(self) -> None:
        self._unblock.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def fake_pypi(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePyPI]:
    """Point every test in this file at a loopback PyPI, never the real one."""
    server = FakePyPI()
    monkeypatch.setenv("SETFORGE_PYPI_BASE", server.base_url)
    for var in ("http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(var.upper(), raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    try:
        yield server
    finally:
        server.close()


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
    fake_pypi: FakePyPI,
) -> None:
    fake_pypi.publish("0.4.0", "0.5.0")
    plan = _build_upgrade_plan(to=None, prerelease=False)
    assert plan.target_version == "0.5.0"


def test_build_upgrade_plan_to_pins_target(
    fake_pypi: FakePyPI,
) -> None:
    fake_pypi.publish("0.4.2", "0.5.0")
    plan = _build_upgrade_plan(to="0.4.2", prerelease=False)
    assert plan.target_version == "0.4.2"
    assert any("--to=0.4.2 pins" in w for w in plan.extra_warnings)


def test_build_upgrade_plan_surfaces_pinned_yanked_release(
    fake_pypi: FakePyPI,
) -> None:
    fake_pypi.publish("0.4.2", "0.5.0", yanked={"0.4.2": "broken release"})

    result = CliRunner().invoke(app, ["upgrade", "--check", "--to", "0.4.2"])

    assert result.exit_code == 0
    assert "YANKED" in result.output
    assert "broken release" in result.output


def test_build_upgrade_plan_surfaces_pinned_prerelease(
    fake_pypi: FakePyPI,
) -> None:
    fake_pypi.publish("8.0.0rc1", "9.9.9")

    result = CliRunner().invoke(app, ["upgrade", "--check", "--to", "8.0.0rc1"])

    assert result.exit_code == 0
    assert "PRE-RELEASE" in result.output
    assert "8.0.0rc1 is a pre-release" in result.output


def test_build_upgrade_plan_rejects_invalid_to() -> None:
    from setforge.errors import UpgradeError

    with pytest.raises(UpgradeError, match="not a valid version"):
        _build_upgrade_plan(to="not-a-version", prerelease=False)


def test_build_upgrade_plan_accepts_canonical_prerelease_spelling(
    fake_pypi: FakePyPI,
) -> None:
    """2.0.0rc1 is how pip and PyPI write a release candidate."""
    fake_pypi.publish("2.0.0rc1")

    plan = _build_upgrade_plan(to="2.0.0rc1", prerelease=False)

    assert plan.target_version == "2.0.0rc1"


def test_build_upgrade_plan_canonicalises_a_non_canonical_pin(
    fake_pypi: FakePyPI,
) -> None:
    """A non-canonical spelling resolves to what uv will install and report."""
    fake_pypi.publish("1.3.2", "1.3.2.post1")

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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_pypi: FakePyPI
) -> None:
    """``--check`` neither shells out nor bootstraps host-local config."""
    fake_pypi.publish("0.3.0")

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
    monkeypatch: pytest.MonkeyPatch, fake_pypi: FakePyPI
) -> None:
    from setforge import __version__ as current

    fake_pypi.publish(current)
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
    monkeypatch: pytest.MonkeyPatch, fake_pypi: FakePyPI
) -> None:
    """Choosing "Upgrade": pypi → wrap → verify → changelog URL + rollback line."""
    fake_pypi.publish(_NEXT_VERSION)
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_pypi: FakePyPI
) -> None:
    """``--no-prompt`` runs the default choice: upgrade, then migrate --check."""
    fake_pypi.publish(_NEXT_VERSION)
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_pypi: FakePyPI
) -> None:
    fake_pypi.publish(_NEXT_VERSION)
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_pypi: FakePyPI
) -> None:
    """An unresolvable manifest must not fail an upgrade that succeeded."""
    fake_pypi.publish(_NEXT_VERSION)
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_pypi: FakePyPI
) -> None:
    """A pydantic ValidationError must not fail an upgrade that succeeded."""
    fake_pypi.publish(_NEXT_VERSION)
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
    monkeypatch: pytest.MonkeyPatch, fake_pypi: FakePyPI
) -> None:
    """Per research brief §2: STDOUT 'Nothing to upgrade' = no-op, exit 0."""
    fake_pypi.publish(_NEXT_VERSION)
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
    monkeypatch: pytest.MonkeyPatch, fake_pypi: FakePyPI
) -> None:
    fake_pypi.publish(_NEXT_VERSION)
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
    monkeypatch: pytest.MonkeyPatch, fake_pypi: FakePyPI
) -> None:
    fake_pypi.publish(_NEXT_VERSION)
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


# ---------------------------------------------------------------------------
# PyPI lookup over HTTP (loopback server; no real network)
# ---------------------------------------------------------------------------


def _forbid_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("nothing may be installed when the PyPI lookup fails")

    monkeypatch.setattr(upgrade_mod.subprocess, "run", fail_run)
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")


def test_check_makes_one_request_and_reports_the_latest_release(
    fake_pypi: FakePyPI,
) -> None:
    fake_pypi.publish("0.1.0", "0.2.0", _NEXT_VERSION)

    result = CliRunner().invoke(app, ["upgrade", "--check"])

    assert result.exit_code == 0, result.output
    assert f"upgrade available: {upgrade_mod._CURRENT_VERSION} → {_NEXT_VERSION}" in (
        result.output
    )
    assert [path for path, _ in fake_pypi.requests] == ["/pypi/setforge/json"]
    user_agent = fake_pypi.requests[0][1]["User-Agent"]
    assert user_agent.startswith(f"setforge/{upgrade_mod._CURRENT_VERSION} ")


def test_pinning_a_version_still_makes_one_request(fake_pypi: FakePyPI) -> None:
    fake_pypi.publish("0.4.2", "0.5.0")

    result = CliRunner().invoke(app, ["upgrade", "--check", "--to", "0.4.2"])

    assert result.exit_code == 0, result.output
    assert len(fake_pypi.requests) == 1


def test_latest_skips_prereleases_unless_asked(fake_pypi: FakePyPI) -> None:
    fake_pypi.publish("2.0.0", "3.0.0rc1")

    default = _build_upgrade_plan(to=None, prerelease=False)
    including = _build_upgrade_plan(to=None, prerelease=True)

    assert default.target_version == "2.0.0"
    assert including.target_version == "3.0.0rc1"
    assert including.is_prerelease


def test_latest_skips_yanked_and_file_less_releases(fake_pypi: FakePyPI) -> None:
    fake_pypi.publish_releases(
        {
            "1.0.0": [{"yanked": False}],
            "1.1.0": [{"yanked": True, "yanked_reason": "bad"}],
            "1.2.0": [],
            "not-a-version": [{"yanked": False}],
        }
    )

    plan = _build_upgrade_plan(to=None, prerelease=False)

    assert plan.target_version == "1.0.0"


def test_latest_compares_versions_not_strings(fake_pypi: FakePyPI) -> None:
    fake_pypi.publish("1.9.0", "1.10.0", "1.2.0")

    assert _build_upgrade_plan(to=None, prerelease=False).target_version == "1.10.0"


def test_pin_matches_a_release_listed_under_another_spelling(
    fake_pypi: FakePyPI,
) -> None:
    fake_pypi.publish("1.0", "2.0")

    plan = _build_upgrade_plan(to="1.0.0", prerelease=False)

    assert plan.target_version == "1.0.0"
    assert any("--to=1.0.0 pins" in w for w in plan.extra_warnings)


def test_pin_of_an_unlisted_release_fails_before_anything_is_installed(
    monkeypatch: pytest.MonkeyPatch, fake_pypi: FakePyPI
) -> None:
    _forbid_subprocess(monkeypatch)
    fake_pypi.publish("0.4.2", "0.5.0")

    result = CliRunner().invoke(app, ["upgrade", "--no-prompt", "--to", "9.9.9"])

    assert result.exit_code == 1
    assert "setforge 9.9.9 is not a release on PyPI" in result.output


@pytest.mark.parametrize("args", [["--check"], ["--no-prompt"]])
@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (500, b"boom", "PyPI returned HTTP 500"),
        (404, b"nope", "PyPI returned HTTP 404"),
        (200, b"<html>not json</html>", "non-JSON body"),
        (200, b"[]", "missing 'releases' map"),
        (200, b'{"releases": {}}', "no non-yanked release found"),
    ],
)
def test_a_pypi_failure_is_one_clear_error_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    fake_pypi: FakePyPI,
    args: list[str],
    status: int,
    body: bytes,
    message: str,
) -> None:
    _forbid_subprocess(monkeypatch)
    fake_pypi.route("/pypi/setforge/json", status, body)

    result = CliRunner().invoke(app, ["upgrade", *args])

    assert result.exit_code == 1
    assert "error:" in result.output
    assert message in result.output


@pytest.mark.parametrize("args", [["--check"], ["--no-prompt"]])
def test_offline_is_one_clear_error_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    _forbid_subprocess(monkeypatch)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    monkeypatch.setenv("SETFORGE_PYPI_BASE", f"http://127.0.0.1:{closed_port}/pypi")

    result = CliRunner().invoke(app, ["upgrade", *args])

    assert result.exit_code == 1
    assert "error: network error contacting PyPI" in result.output


def test_a_stalled_pypi_times_out_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch, fake_pypi: FakePyPI
) -> None:
    _forbid_subprocess(monkeypatch)
    monkeypatch.setattr(upgrade_mod, "_PYPI_TIMEOUT_SECONDS", 0.3)
    fake_pypi.publish(_NEXT_VERSION)
    fake_pypi.stall = True

    result = CliRunner().invoke(app, ["upgrade", "--no-prompt"])

    assert result.exit_code == 1
    assert "error: timeout contacting PyPI" in result.output


def test_the_default_pypi_url_and_timeout_are_unchanged() -> None:
    assert upgrade_mod._DEFAULT_PYPI_BASE == "https://pypi.org/pypi"
    assert upgrade_mod._PYPI_TIMEOUT_SECONDS == 10.0


def test_a_redirect_to_another_host_is_refused(
    monkeypatch: pytest.MonkeyPatch, fake_pypi: FakePyPI
) -> None:
    _forbid_subprocess(monkeypatch)
    fake_pypi.route(
        "/pypi/setforge/json",
        302,
        headers={"Location": "https://evil.example.invalid/pypi/setforge/json"},
    )

    result = CliRunner().invoke(app, ["upgrade", "--no-prompt"])

    assert result.exit_code == 1
    assert "different host or scheme; refusing to follow it" in result.output
    assert len(fake_pypi.requests) == 1


def test_a_redirect_to_another_port_on_the_same_machine_is_refused(
    fake_pypi: FakePyPI,
) -> None:
    other = FakePyPI()
    try:
        other.publish(_NEXT_VERSION)
        fake_pypi.route(
            "/pypi/setforge/json",
            301,
            headers={"Location": f"{other.base_url}/setforge/json"},
        )

        result = CliRunner().invoke(app, ["upgrade", "--check"])

        assert result.exit_code == 1
        assert "refusing to follow it" in result.output
        assert other.requests == []
    finally:
        other.close()


def test_a_redirect_within_the_same_host_is_followed(fake_pypi: FakePyPI) -> None:
    fake_pypi.route(
        "/pypi/setforge/json", 301, headers={"Location": "/mirror/setforge/json"}
    )
    fake_pypi.route(
        "/mirror/setforge/json",
        200,
        json.dumps({"releases": {_NEXT_VERSION: [{"yanked": False}]}}).encode("utf-8"),
    )

    result = CliRunner().invoke(app, ["upgrade", "--check"])

    assert result.exit_code == 0, result.output
    assert _NEXT_VERSION in result.output
    assert [path for path, _ in fake_pypi.requests] == [
        "/pypi/setforge/json",
        "/mirror/setforge/json",
    ]


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")
def test_a_certificate_that_does_not_verify_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TLS verification stays on: a self-signed server must not be trusted."""
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    _forbid_subprocess(monkeypatch)
    tls = FakePyPI()
    try:
        tls.publish(_NEXT_VERSION)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        tls.server.socket = context.wrap_socket(tls.server.socket, server_side=True)
        port = tls.server.server_address[1]
        monkeypatch.setenv("SETFORGE_PYPI_BASE", f"https://127.0.0.1:{port}/pypi")

        result = CliRunner().invoke(app, ["upgrade", "--check"])

        assert result.exit_code == 1
        assert "error: network error contacting PyPI" in result.output
        assert "CERTIFICATE_VERIFY_FAILED" in result.output
        assert tls.requests == []
    finally:
        tls.close()


# ---------------------------------------------------------------------------
# Non-TTY confirm guard
# ---------------------------------------------------------------------------


def test_cli_upgrade_non_tty_without_no_prompt_raises_and_skips_button_bar(
    monkeypatch: pytest.MonkeyPatch, fake_pypi: FakePyPI
) -> None:
    from setforge.errors import ConfirmRequiresInteractive

    fake_pypi.publish(_NEXT_VERSION)
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
    monkeypatch: pytest.MonkeyPatch, fake_pypi: FakePyPI
) -> None:
    """``--no-prompt`` on a non-TTY auto-applies (``yes=True``) and proceeds."""
    fake_pypi.publish(_NEXT_VERSION)
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
