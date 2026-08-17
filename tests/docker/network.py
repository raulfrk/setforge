"""Shared opt-in contract for Docker tests that contact live upstreams."""

from __future__ import annotations

import os
from collections.abc import Mapping

import pytest

NETWORK_ENV = "SETFORGE_E2E_NETWORK"


def network_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return true only for the documented explicit ``SETFORGE_E2E_NETWORK=1``."""
    source = os.environ if environ is None else environ
    return source.get(NETWORK_ENV) == "1"


NETWORK_ONLY = pytest.mark.skipif(
    not network_enabled(),
    reason=f"set {NETWORK_ENV}=1 to run live-upstream Docker canaries",
)
