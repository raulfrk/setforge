"""Unit tests for the project-root xdist prebuild hook."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

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
        ("not (e2e_docker and smoke)", False),
        ("not not e2e_docker", True),
        ("prefix_e2e_docker_suffix", False),
        ("integration", False),
        ("e2e_docker and (", False),
    ],
)
def test_selects_e2e_docker(expression: str, selected: bool) -> None:
    assert e2e_xdist._selects_e2e_docker(expression) is selected


@pytest.mark.parametrize(
    ("expression", "target"),
    [
        ("e2e_docker", "full"),
        ("e2e_docker and smoke", "smoke"),
        ("smoke and e2e_docker", "smoke"),
        ("e2e_docker and not smoke", "full"),
        ("not (e2e_docker and smoke)", "full"),
        ("e2e_docker or smoke", "full"),
        ("not smoke and (e2e_docker or smoke)", "full"),
        ("e2e_docker and (smoke or integration)", "full"),
        ("e2e_docker and smoke and integration", "smoke"),
        ("smoke", "full"),
    ],
)
def test_image_target_for_marker_expression(expression: str, target: str) -> None:
    assert e2e_xdist.image_target_for_markexpr(expression) == target


@given(
    e2e_negations=st.integers(min_value=0, max_value=6),
    smoke_negations=st.integers(min_value=0, max_value=6),
)
def test_marker_polarity_selects_only_evenly_negated_markers(
    e2e_negations: int, smoke_negations: int
) -> None:
    def nested_not(marker: str, count: int) -> str:
        return "not (" * count + marker + ")" * count

    expression = (
        f"{nested_not('e2e_docker', e2e_negations)} and "
        f"{nested_not('smoke', smoke_negations)}"
    )
    e2e_positive = e2e_negations % 2 == 0
    smoke_positive = smoke_negations % 2 == 0

    assert e2e_xdist._selects_e2e_docker(expression) is e2e_positive
    assert e2e_xdist.image_target_for_markexpr(expression) == (
        "smoke" if e2e_positive and smoke_positive else "full"
    )


@given(extra_marker=st.sampled_from(["integration", "fresh_host", "slow"]))
def test_smoke_target_requires_logical_implication(extra_marker: str) -> None:
    assert (
        e2e_xdist.image_target_for_markexpr(f"e2e_docker and (smoke or {extra_marker})")
        == "full"
    )
    assert (
        e2e_xdist.image_target_for_markexpr(
            f"e2e_docker and smoke and ({extra_marker} or not {extra_marker})"
        )
        == "smoke"
    )


@pytest.mark.parametrize(
    ("expression", "fixed", "possible"),
    [
        ("a and b", {"a": True, "b": True}, {True}),
        ("a and b", {"a": False, "b": True}, {False}),
        ("a or b", {"a": False, "b": False}, {False}),
        ("a or b", {"a": True, "b": False}, {True}),
        ("marker()", {}, {False, True}),
    ],
)
def test_possible_marker_values_fail_safe(
    expression: str, fixed: dict[str, bool], possible: set[bool]
) -> None:
    tree = ast.parse(expression, mode="eval")
    assert e2e_xdist._possible_values(tree, fixed) == possible


def test_invalid_implication_expression_may_select_non_smoke() -> None:
    assert e2e_xdist._may_select_non_smoke_e2e("e2e_docker and (") is True


def test_xdist_controller_prepares_image_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(image, "ensure_docker_image", calls.append)

    e2e_xdist.pytest_xdist_setupnodes(_pytest_config(_Config("e2e_docker")), specs=[])

    assert calls == ["full"]


def test_xdist_controller_prepares_smoke_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(image, "ensure_docker_image", calls.append)

    e2e_xdist.pytest_xdist_setupnodes(
        _pytest_config(_Config("e2e_docker and smoke")), specs=[]
    )

    assert calls == ["smoke"]


def test_xdist_controller_ignores_non_docker_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(_target: str) -> None:
        raise AssertionError("Docker preparation must not run")

    monkeypatch.setattr(image, "ensure_docker_image", unexpected)

    e2e_xdist.pytest_xdist_setupnodes(
        _pytest_config(_Config("not e2e_docker")), specs=[]
    )


def test_xdist_controller_turns_build_error_into_session_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_target: str) -> None:
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
