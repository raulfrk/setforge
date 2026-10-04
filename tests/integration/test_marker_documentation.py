"""A tracked file that documents the retired marker syntax is an ordinary file."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from .conftest import IntegrationEnv

pytestmark = pytest.mark.integration

_EXAMPLE = (
    "<!-- setforge:user-section start host-local NAME -->\n"
    "(host-specific content — never touched)\n"
    "<!-- setforge:user-section end host-local NAME -->\n"
    "\n"
    "<!-- setforge:user-section start shared NAME -->\n"
    "(content with a tracked-side default)\n"
    "<!-- setforge:user-section end shared NAME -->\n"
)
_GUIDE = f"# Guide\n\nMarker syntax:\n\n{_EXAMPLE}\nAgain, for emphasis:\n\n{_EXAMPLE}"
_LIVE = ".setforge_it/docs/guide.md"


def test_marker_documentation_installs_compares_and_syncs(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    env = integration_env(tracked={"guide": ("docs/guide.md", _GUIDE)})

    install = env.run_verb(["install"])
    assert install.exit_code == 0, install.output
    assert env.live(_LIVE).read_text(encoding="utf-8") == _GUIDE

    compare = env.run_verb(["compare", "--check", "--strict"])
    assert compare.exit_code == 0, compare.output

    edited = _GUIDE + "\nA line added on this host.\n"
    env.live(_LIVE).write_text(edited, encoding="utf-8")
    sync = env.run_verb(["sync", "--auto=use-live", "--yes"])
    assert sync.exit_code == 0, sync.output
    assert env.tracked("docs/guide.md").read_text(encoding="utf-8") == edited
