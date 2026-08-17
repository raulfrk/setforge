"""Importable implementation of the project's Docker E2E xdist hooks."""

from __future__ import annotations

import ast

import pytest

from tests.docker.image import DEFAULT_IMAGE_TARGET, ImageTarget

# -n 2 is the empirically validated stable cap on the reference host. Higher
# values saturated the Docker daemon and VM; callers can still override it.
_XDIST_WORKER_CAP: int = 2


def _positive_markers(markexpr: str) -> frozenset[str]:
    """Return marker names appearing beneath an even number of negations."""
    try:
        tree = ast.parse(markexpr, mode="eval")
    except SyntaxError:
        return frozenset()

    positive: set[str] = set()

    def visit(node: ast.AST, *, negated: bool = False) -> None:
        if isinstance(node, ast.Name):
            if not negated:
                positive.add(node.id)
            return
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            visit(node.operand, negated=not negated)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, negated=negated)

    visit(tree)
    return frozenset(positive)


def _possible_values(node: ast.AST, fixed: dict[str, bool]) -> frozenset[bool]:
    """Conservatively evaluate a marker expression under fixed assignments."""
    if isinstance(node, ast.Expression):
        return _possible_values(node.body, fixed)
    if isinstance(node, ast.Name):
        value = fixed.get(node.id)
        return frozenset((False, True)) if value is None else frozenset((value,))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return frozenset(not value for value in _possible_values(node.operand, fixed))
    if isinstance(node, ast.BoolOp):
        children = [_possible_values(value, fixed) for value in node.values]
        if isinstance(node.op, ast.And):
            return frozenset(
                value
                for value in (False, True)
                if (value is True and all(True in child for child in children))
                or (value is False and any(False in child for child in children))
            )
        return frozenset(
            value
            for value in (False, True)
            if (value is True and any(True in child for child in children))
            or (value is False and all(False in child for child in children))
        )
    return frozenset((False, True))


def _may_select_non_smoke_e2e(markexpr: str) -> bool:
    """Return whether an E2E item without ``smoke`` may satisfy the expression."""
    try:
        tree = ast.parse(markexpr, mode="eval")
    except SyntaxError:
        return True
    return True in _possible_values(tree, {"e2e_docker": True, "smoke": False})


def _selects_e2e_docker(markexpr: str) -> bool:
    """Return whether a marker expression positively selects e2e_docker."""
    return "e2e_docker" in _positive_markers(markexpr)


def image_target_for_markexpr(markexpr: str) -> ImageTarget:
    """Choose smoke only when every possibly selected E2E item is smoke."""
    positive = _positive_markers(markexpr)
    if {"e2e_docker", "smoke"} <= positive and not _may_select_non_smoke_e2e(markexpr):
        return "smoke"
    return DEFAULT_IMAGE_TARGET


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Auto-activate stable two-worker xdist for Docker E2E selections."""
    markexpr = config.getoption("markexpr", default="") or ""
    if not _selects_e2e_docker(markexpr):
        return
    if config.getoption("numprocesses", default=None) is not None:
        return
    if hasattr(config, "workerinput"):
        return
    config.option.numprocesses = _XDIST_WORKER_CAP
    if config.option.dist == "no":
        config.option.dist = "loadgroup"
    config.option.tx = ["popen"] * _XDIST_WORKER_CAP


def pytest_xdist_setupnodes(config: pytest.Config, specs: object) -> None:
    """Prepare the Docker E2E image once before xdist starts workers."""
    del specs
    markexpr = config.getoption("markexpr", default="") or ""
    if not _selects_e2e_docker(markexpr):
        return

    from tests.docker.image import DockerImageBuildError, ensure_docker_image

    try:
        ensure_docker_image(image_target_for_markexpr(markexpr))
    except DockerImageBuildError as exc:
        pytest.exit(str(exc), returncode=1)
