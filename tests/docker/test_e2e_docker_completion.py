"""Docker E2E tests for ``setforge completion install``.

What only a real host can show:

1. ``test_installed_zsh_script_passes_syntax_check`` — the zsh script
   ``completion install zsh`` writes is accepted by the real ``zsh -n``.
2. ``test_installed_bash_script_passes_shellcheck`` — the bash script
   ``completion install bash`` writes passes ``shellcheck -S error``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

# Absolute path to the project venv's setforge binary inside the
# container (set in the Dockerfile's tester USER step).
_VENV_BIN = "/workspace/.venv/bin"
_SETFORGE_BIN = f"{_VENV_BIN}/setforge"
_ENV = {"PATH": f"{_VENV_BIN}:/usr/local/bin:/usr/bin:/bin"}


def _install_script(c: ContainerHandle, shell: str) -> None:
    """Run ``completion install <shell>`` without touching any rc file."""
    result = c.exec(
        [
            _SETFORGE_BIN,
            "completion",
            "install",
            shell,
            "--non-interactive",
            "--no-wire",
        ],
        env=_ENV,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# E2E #1 — the installed zsh script passes `zsh -n`
# ---------------------------------------------------------------------------


def test_installed_zsh_script_passes_syntax_check(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _install_script(c, "zsh")

    result = c.exec(
        ["zsh", "-n", "/home/tester/.config/setforge/completions/_setforge"],
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# E2E #2 — the installed bash script passes `shellcheck -S error`
# ---------------------------------------------------------------------------


def test_installed_bash_script_passes_shellcheck(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _install_script(c, "bash")

    result = c.exec(
        [
            "shellcheck",
            "-S",
            "error",
            "/home/tester/.config/setforge/completions/setforge.bash",
        ],
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
