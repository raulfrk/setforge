"""Contract tests for the explicit fast local pytest lane."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from scripts import test_fast


def test_pytest_argv_uses_bounded_worksteal_lane() -> None:
    assert test_fast.pytest_argv(workers=3, extra=["-q", "tests/test_cli.py"]) == [
        sys.executable,
        "-m",
        "pytest",
        "-n",
        "3",
        "--dist=worksteal",
        "--no-cov",
        "-m",
        test_fast.FAST_MARKER,
        "-q",
        "tests/test_cli.py",
    ]
    assert 1 <= test_fast.DEFAULT_WORKERS <= test_fast.MAX_WORKERS == 6


def test_main_sets_parallel_profile_and_returns_pytest_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        argv: list[str], *, env: dict[str, str], check: bool
    ) -> SimpleNamespace:
        captured.update(argv=argv, env=env, check=check)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(test_fast.subprocess, "run", fake_run)

    assert test_fast.main(["--workers", "2", "-q", "--maxfail=1"]) == 7
    assert captured["argv"] == test_fast.pytest_argv(
        workers=2, extra=["-q", "--maxfail=1"]
    )
    assert captured["check"] is False
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["HYPOTHESIS_PROFILE"] == "parallel"


@pytest.mark.parametrize("workers", ["0", "7"])
def test_main_rejects_out_of_range_worker_count_before_launch(
    monkeypatch: pytest.MonkeyPatch,
    workers: str,
) -> None:
    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("pytest must not launch for an invalid worker count")

    monkeypatch.setattr(test_fast.subprocess, "run", unexpected_run)

    with pytest.raises(SystemExit) as exc_info:
        test_fast.main(["-n", workers])

    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    "reserved",
    [
        ["-m", "e2e_docker"],
        ["-me2e_docker"],
        ["--dist=loadscope"],
        ["--numprocesses=12"],
        ["--cov=setforge"],
        ["--no-cov"],
        ["--"],
    ],
)
def test_main_rejects_options_that_override_the_fast_lane(
    monkeypatch: pytest.MonkeyPatch,
    reserved: list[str],
) -> None:
    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("pytest must not launch with a lane override")

    monkeypatch.setattr(test_fast.subprocess, "run", unexpected_run)

    with pytest.raises(SystemExit) as exc_info:
        test_fast.main(reserved)

    assert exc_info.value.code == 2
