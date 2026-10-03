"""Bad config inputs surface as clean errors naming the right file."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from setforge.errors import SetforgeError

from .conftest import IntegrationEnv

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("verb", ["compare", "status", "fetch"])
def test_bad_local_source_block_names_local_yaml(
    integration_env: Callable[..., IntegrationEnv], verb: str
) -> None:
    env = integration_env()
    env.local_config.parent.mkdir(parents=True, exist_ok=True)
    env.local_config.write_text("source:\n  kind: path\n", encoding="utf-8")

    result = env.run_verb(
        [verb], inject_config=verb != "fetch", inject_profile=verb != "fetch"
    )

    assert isinstance(result.exception, SetforgeError), result.output
    message = str(result.exception)
    assert str(env.local_config) in message
    assert "source.path" in message
    assert "SCHEMA VALIDATION ERROR" not in result.output
