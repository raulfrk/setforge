"""Fixtures for the black-box regression tests.

The isolated home and config repository come from the integration tier; they
are re-exported here so the regression modules can request them directly."""

from tests.integration.conftest import (  # noqa: F401
    integration_env,
    integration_subprocess,
)
