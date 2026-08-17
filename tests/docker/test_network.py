from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.docker.network import NETWORK_ENV, network_enabled


def test_network_enabled_accepts_only_explicit_one() -> None:
    assert network_enabled({NETWORK_ENV: "1"})


@pytest.mark.slow
@settings(deadline=None)
@given(st.text().filter(lambda value: value != "1"))
def test_network_enabled_rejects_every_other_value(value: str) -> None:
    assert not network_enabled({NETWORK_ENV: value})


def test_network_enabled_rejects_missing_value() -> None:
    assert not network_enabled({})
