"""Focused unit tests for Docker E2E image preparation."""

from __future__ import annotations

import subprocess

import pytest

from tests.docker import conftest as docker_fixtures
from tests.docker import image


@pytest.fixture(autouse=True)
def clear_image_tag_cache() -> None:
    """Keep tag-cache behavior isolated between unit tests."""
    image._image_tag.cache_clear()


def test_ensure_docker_image_returns_none_when_docker_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image, "docker_available", lambda: False)

    assert image.ensure_docker_image() is None


def test_ensure_docker_image_reuses_existing_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(image, "docker_available", lambda: True)
    monkeypatch.setattr(image, "_image_tag", lambda: "setforge-e2e:test-existing")
    monkeypatch.setattr(image.subprocess, "run", fake_run)

    assert image.ensure_docker_image() == "setforge-e2e:test-existing"
    assert calls == [["docker", "image", "inspect", "setforge-e2e:test-existing"]]


def test_ensure_docker_image_builds_after_inspect_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    results = iter(
        (
            subprocess.CompletedProcess([], 1, "", "missing"),
            subprocess.CompletedProcess([], 0, "built", ""),
        )
    )

    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return next(results)

    monkeypatch.setattr(image, "docker_available", lambda: True)
    monkeypatch.setattr(image, "_image_tag", lambda: "setforge-e2e:test-new")
    monkeypatch.setattr(image.subprocess, "run", fake_run)

    assert image.ensure_docker_image() == "setforge-e2e:test-new"
    assert calls[0] == ["docker", "image", "inspect", "setforge-e2e:test-new"]
    assert calls[1][:5] == ["docker", "build", "-t", "setforge-e2e:test-new", "-f"]


def test_ensure_docker_image_reports_build_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = iter(
        (
            subprocess.CompletedProcess([], 1, "", "missing"),
            subprocess.CompletedProcess([], 2, "build stdout", "build stderr"),
        )
    )
    monkeypatch.setattr(image, "docker_available", lambda: True)
    monkeypatch.setattr(image, "_image_tag", lambda: "setforge-e2e:test-broken")
    monkeypatch.setattr(
        image.subprocess, "run", lambda *_args, **_kwargs: next(results)
    )

    with pytest.raises(image.DockerImageBuildError, match="build stderr") as exc_info:
        image.ensure_docker_image()

    assert "build stdout" in str(exc_info.value)


def test_ensure_docker_image_normalizes_inspect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = subprocess.TimeoutExpired(
        ["docker", "image", "inspect"], 30, output="partial", stderr="hung"
    )

    def time_out(*_args: object, **_kwargs: object) -> None:
        raise timeout

    monkeypatch.setattr(image, "docker_available", lambda: True)
    monkeypatch.setattr(image, "_image_tag", lambda: "setforge-e2e:test-timeout")
    monkeypatch.setattr(image.subprocess, "run", time_out)

    with pytest.raises(image.DockerImageBuildError, match="timed out") as exc_info:
        image.ensure_docker_image()

    assert exc_info.value.__cause__ is timeout
    assert "partial" in str(exc_info.value)
    assert "hung" in str(exc_info.value)


def test_ensure_docker_image_normalizes_build_launch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results: list[subprocess.CompletedProcess[str] | OSError] = [
        subprocess.CompletedProcess([], 1, "", "missing"),
        OSError("docker vanished"),
    ]

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        result = results.pop(0)
        if isinstance(result, OSError):
            raise result
        return result

    monkeypatch.setattr(image, "docker_available", lambda: True)
    monkeypatch.setattr(image, "_image_tag", lambda: "setforge-e2e:test-launch")
    monkeypatch.setattr(image.subprocess, "run", fake_run)

    with pytest.raises(
        image.DockerImageBuildError, match="docker vanished"
    ) as exc_info:
        image.ensure_docker_image()

    assert isinstance(exc_info.value.__cause__, OSError)


def test_docker_image_fixture_returns_prepared_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        docker_fixtures, "ensure_docker_image", lambda: "setforge-e2e:test-ready"
    )

    assert (
        docker_fixtures.docker_image.__wrapped__()  # type: ignore[attr-defined]
        == "setforge-e2e:test-ready"
    )


def test_docker_image_fixture_skips_without_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_fixtures, "ensure_docker_image", lambda: None)

    with pytest.raises(pytest.skip.Exception, match="docker binary not on PATH"):
        docker_fixtures.docker_image.__wrapped__()  # type: ignore[attr-defined]


def test_docker_image_fixture_reports_build_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> None:
        raise image.DockerImageBuildError("fixture build failure")

    monkeypatch.setattr(docker_fixtures, "ensure_docker_image", fail)

    with pytest.raises(pytest.fail.Exception, match="fixture build failure"):
        docker_fixtures.docker_image.__wrapped__()  # type: ignore[attr-defined]
