"""Focused unit tests for Docker E2E image preparation."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import cast

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
    monkeypatch.setattr(
        image, "_image_tag", lambda _target: "setforge-e2e:full-existing"
    )
    monkeypatch.setattr(image.subprocess, "run", fake_run)

    assert image.ensure_docker_image() == "setforge-e2e:full-existing"
    assert calls == [["docker", "image", "inspect", "setforge-e2e:full-existing"]]


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
    monkeypatch.setattr(image, "_image_tag", lambda _target: "setforge-e2e:smoke-new")
    monkeypatch.setattr(image.subprocess, "run", fake_run)

    assert image.ensure_docker_image("smoke") == "setforge-e2e:smoke-new"
    assert calls[0] == ["docker", "image", "inspect", "setforge-e2e:smoke-new"]
    assert calls[1][:7] == [
        "docker",
        "build",
        "--target",
        "smoke",
        "-t",
        "setforge-e2e:smoke-new",
        "-f",
    ]


def test_ensure_docker_image_rejects_unknown_target_before_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        image, "docker_available", lambda: pytest.fail("must not inspect Docker")
    )

    with pytest.raises(ValueError, match="unknown E2E image target"):
        image.ensure_docker_image("smoke; touch /tmp/injected")


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
    monkeypatch.setattr(image, "_image_tag", lambda _target: "setforge-e2e:full-broken")
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
    monkeypatch.setattr(
        image, "_image_tag", lambda _target: "setforge-e2e:full-timeout"
    )
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
    monkeypatch.setattr(image, "_image_tag", lambda _target: "setforge-e2e:full-launch")
    monkeypatch.setattr(image.subprocess, "run", fake_run)

    with pytest.raises(
        image.DockerImageBuildError, match="docker vanished"
    ) as exc_info:
        image.ensure_docker_image()

    assert isinstance(exc_info.value.__cause__, OSError)


def test_docker_image_fixture_returns_prepared_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def prepared(target: str) -> str:
        calls.append(target)
        return "setforge-e2e:smoke-ready"

    monkeypatch.setattr(docker_fixtures, "ensure_docker_image", prepared)
    request = SimpleNamespace(
        config=SimpleNamespace(
            getoption=lambda _name, default="": "e2e_docker and smoke"
        )
    )

    assert (
        docker_fixtures.docker_image.__wrapped__(  # type: ignore[attr-defined]
            cast(pytest.FixtureRequest, request)
        )
        == "setforge-e2e:smoke-ready"
    )
    assert calls == ["smoke"]


def test_docker_image_fixture_skips_without_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_fixtures, "ensure_docker_image", lambda _target: None)
    request = SimpleNamespace(
        config=SimpleNamespace(getoption=lambda _name, default="": "e2e_docker")
    )

    with pytest.raises(pytest.skip.Exception, match="docker binary not on PATH"):
        docker_fixtures.docker_image.__wrapped__(  # type: ignore[attr-defined]
            cast(pytest.FixtureRequest, request)
        )


def test_docker_image_fixture_reports_build_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_target: str) -> None:
        raise image.DockerImageBuildError("fixture build failure")

    monkeypatch.setattr(docker_fixtures, "ensure_docker_image", fail)

    request = SimpleNamespace(
        config=SimpleNamespace(getoption=lambda _name, default="": "e2e_docker")
    )
    with pytest.raises(pytest.fail.Exception, match="fixture build failure"):
        docker_fixtures.docker_image.__wrapped__(  # type: ignore[attr-defined]
            cast(pytest.FixtureRequest, request)
        )
