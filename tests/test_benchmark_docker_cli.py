"""Unit tests for the reproducible Docker CLI benchmark."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from scripts import benchmark_docker_cli as benchmark
from scripts.benchmark_docker_cli import _measure, _summary


def test_summary_reports_center_spread_and_bounds() -> None:
    summary = _summary([1.0, 2.0, 3.0])

    assert summary == {
        "median_s": 2.0,
        "mean_s": 2.0,
        "stdev_s": 1.0,
        "mean_95ci_half_width_s": pytest.approx(1.96 / 3**0.5),
        "min_s": 1.0,
        "max_s": 3.0,
    }


@pytest.mark.parametrize("sample", [pytest.param([1.25]), pytest.param([1.25, 1.25])])
def test_summary_has_zero_spread_for_constant_samples(sample: list[float]) -> None:
    assert _summary(sample)["stdev_s"] == 0.0


def test_measure_discards_warmups_and_cleans_up_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    clock = iter((0.0, 1.0, 1.0, 3.0, 3.0, 6.0))

    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        stdout = "cid-123\n" if argv[:2] == ["docker", "run"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)
    monkeypatch.setattr(benchmark.time, "perf_counter", lambda: next(clock))

    assert _measure("image", repeats=2, warmups=1) == [2.0, 3.0]
    assert sum(call[:2] == ["docker", "exec"] for call in calls) == 3
    assert calls[-1][:3] == ["docker", "rm", "-f"]
    assert calls[-1][-1].startswith("setforge-e2e-benchmark-")


@pytest.mark.parametrize("failure_at", ["launch", "exec"])
def test_measure_cleans_up_after_subprocess_failure(
    monkeypatch: pytest.MonkeyPatch, failure_at: str
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if failure_at == "launch" and argv[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(argv, 120)
        if failure_at == "exec" and argv[:2] == ["docker", "exec"]:
            raise subprocess.CalledProcessError(1, argv)
        stdout = "cid-123\n" if argv[:2] == ["docker", "run"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)

    expected = (
        subprocess.TimeoutExpired
        if failure_at == "launch"
        else subprocess.CalledProcessError
    )
    with pytest.raises(expected):
        _measure("image", repeats=2, warmups=0)
    assert calls[-1][:3] == ["docker", "rm", "-f"]
    assert calls[-1][-1].startswith("setforge-e2e-benchmark-")


def test_cleanup_timeout_does_not_mask_the_exec_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["docker", "rm", "-f"]:
            raise subprocess.TimeoutExpired(argv, 30)
        if argv[:2] == ["docker", "exec"]:
            raise subprocess.CalledProcessError(7, argv)
        return subprocess.CompletedProcess(argv, 0, "cid-123\n", "")

    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        _measure("image", repeats=2, warmups=0)
    assert exc_info.value.returncode == 7


@pytest.mark.parametrize("failure", ["timeout", "nonzero"])
def test_successful_measurement_surfaces_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["docker", "rm", "-f"]:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 30)
            return subprocess.CompletedProcess(argv, 1, "", "still running")
        stdout = "cid-123\n" if argv[:2] == ["docker", "run"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)

    expected = (
        subprocess.TimeoutExpired
        if failure == "timeout"
        else benchmark.DockerBenchmarkCleanupError
    )
    with pytest.raises(expected):
        _measure("image", repeats=2, warmups=0)


def test_main_emits_raw_samples_and_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(benchmark, "ensure_docker_image", lambda _target: "image")
    monkeypatch.setattr(
        benchmark, "_measure", lambda _image, *, repeats, warmups: [1.0, 3.0]
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["benchmark", "--target", "smoke", "--repeats", "2", "--warmups", "1"],
    )

    benchmark.main()

    result = json.loads(capsys.readouterr().out)
    assert result["image"] == "image"
    assert result["repeats"] == 2
    assert result["warmups"] == 1
    assert result["samples_s"] == [1.0, 3.0]
    assert result["mean_s"] == 2.0


@pytest.mark.parametrize(
    "extra",
    [pytest.param(["--repeats", "1"]), pytest.param(["--warmups", "-1"])],
)
def test_main_rejects_invalid_sample_counts(
    monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["benchmark", *extra])

    with pytest.raises(SystemExit) as exc_info:
        benchmark.main()
    assert exc_info.value.code == 2


def test_main_fails_cleanly_without_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["benchmark"])
    monkeypatch.setattr(benchmark, "ensure_docker_image", lambda _target: None)

    with pytest.raises(SystemExit, match="docker binary not found"):
        benchmark.main()
