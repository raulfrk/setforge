"""Fixtures for the black-box regression tests.

The isolated home and config repository come from the integration tier; they
are re-exported here so the regression modules can request them directly."""

import shutil
from collections.abc import Iterator

import pytest

from tests.integration.conftest import (  # noqa: F401
    integration_env,
    integration_subprocess,
)
from tests.regression.support import CROSS_DEVICE_DIRS


@pytest.fixture(autouse=True)
def _remove_cross_device_homes() -> Iterator[None]:
    yield
    while CROSS_DEVICE_DIRS:
        shutil.rmtree(CROSS_DEVICE_DIRS.pop(), ignore_errors=True)
