"""Unit tests for the project-root xdist prebuild hook."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest

from tests import e2e_xdist
from tests.docker import image


@dataclass
class _Option:
    numprocesses: int | None = None
    dist: str = "no"
    tx: list[str] | None = None


class _Config:
    def __init__(self, markexpr: str, *, numprocesses: int | None = None) -> None:
        self.markexpr = markexpr
        self.option = _Option(numprocesses=numprocesses)

    def getoption(self, name: str, default: object = None) -> object:
        if name == "markexpr":
            return self.markexpr
        if name == "numprocesses":
            return self.option.numprocesses
        return default


def _pytest_config(config: _Config) -> pytest.Config:
    """Cast the deliberately minimal hook-test double at the pytest seam."""
    return cast(pytest.Config, config)


@pytest.mark.parametrize(
    ("expression", "selected"),
    [
        ("e2e_docker", True),
        ("e2e_docker and smoke", True),
        ("not e2e_docker", False),
        ("prefix_e2e_docker_suffix", False),
        ("integration", False),
    ],
)
def test_selects_e2e_docker(expression: str, selected: bool) -> None:
    assert e2e_xdist._selects_e2e_docker(expression) is selected


def test_xdist_controller_prepares_image_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[None] = []
    monkeypatch.setattr(image, "ensure_docker_image", lambda: calls.append(None))

    e2e_xdist.pytest_xdist_setupnodes(_pytest_config(_Config("e2e_docker")), specs=[])

    assert calls == [None]


def test_xdist_controller_ignores_non_docker_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected() -> None:
        raise AssertionError("Docker preparation must not run")

    monkeypatch.setattr(image, "ensure_docker_image", unexpected)

    e2e_xdist.pytest_xdist_setupnodes(
        _pytest_config(_Config("not e2e_docker")), specs=[]
    )


def test_xdist_controller_turns_build_error_into_session_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> None:
        raise image.DockerImageBuildError("controlled build failure")

    monkeypatch.setattr(image, "ensure_docker_image", fail)

    with pytest.raises(pytest.exit.Exception, match="controlled build failure"):
        e2e_xdist.pytest_xdist_setupnodes(
            _pytest_config(_Config("e2e_docker")), specs=[]
        )


def test_configure_enables_stable_loadgroup_defaults() -> None:
    config = _Config("e2e_docker")

    e2e_xdist.pytest_configure(_pytest_config(config))

    assert config.option.numprocesses == 2
    assert config.option.dist == "loadgroup"
    assert config.option.tx == ["popen", "popen"]


def test_configure_preserves_explicit_worker_count() -> None:
    config = _Config("e2e_docker", numprocesses=1)

    e2e_xdist.pytest_configure(_pytest_config(config))

    assert config.option.numprocesses == 1
    assert config.option.tx is None


def test_configure_does_not_recurse_in_worker() -> None:
    config = _Config("e2e_docker")
    config.workerinput = {}  # type: ignore[attr-defined]

    e2e_xdist.pytest_configure(_pytest_config(config))

    assert config.option.numprocesses is None


def test_configure_ignores_non_docker_selection() -> None:
    config = _Config("not e2e_docker")

    e2e_xdist.pytest_configure(_pytest_config(config))

    assert config.option.numprocesses is None


def test_configure_preserves_explicit_distribution_mode() -> None:
    config = _Config("e2e_docker")
    config.option.dist = "loadfile"

    e2e_xdist.pytest_configure(_pytest_config(config))

    assert config.option.dist == "loadfile"
