"""Docker E2E tests for ``setforge completion install``.

What only a real host can show:

1. ``test_installed_zsh_script_passes_syntax_check`` — the zsh script
   ``completion install zsh`` writes is accepted by the real ``zsh -n``.
2. ``test_installed_bash_script_passes_shellcheck`` — the bash script
   ``completion install bash`` writes passes ``shellcheck -S error``.
3. ``test_rc_file_atomic_under_sigint`` — SIGINT mid-write leaves the
   rc-file byte-identical to its original content.
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
_PYTHON_BIN = f"{_VENV_BIN}/python"
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


# ---------------------------------------------------------------------------
# E2E #3 — rc-file atomic under SIGINT: original bytes preserved
# ---------------------------------------------------------------------------


def test_rc_file_atomic_under_sigint(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """SIGINT mid-write leaves the rc-file byte-identical to its original.

    Drives ``_atomic_write_rc_file`` directly inside the container via a
    one-shot python -c invocation: monkeypatch ``Path.write_text`` on
    the ``.setforge-tmp`` file to raise ``KeyboardInterrupt`` mid-write,
    confirm rc-file content is unchanged AND non-empty.
    """
    c = docker_container()
    rc_path = "/home/tester/.zshrc"
    original = (
        "# rc atomicity fixture\nexport TEST_VAR=42\nalias ls='ls --color=auto'\n"
    )
    c.write_text(rc_path, original)

    payload = (
        "import pathlib, sys\n"
        "from setforge.cli.completion import _atomic_write_rc_file\n"
        f"rc = pathlib.Path({rc_path!r})\n"
        "orig = pathlib.Path.write_text\n"
        "def boom(self, *a, **kw):\n"
        "    if self.name.endswith('.setforge-tmp'):\n"
        "        raise KeyboardInterrupt('simulated SIGINT')\n"
        "    return orig(self, *a, **kw)\n"
        "pathlib.Path.write_text = boom\n"
        "try:\n"
        "    _atomic_write_rc_file(rc, '# CORRUPTED — must not land\\n')\n"
        "    sys.exit(99)\n"
        "except KeyboardInterrupt:\n"
        "    sys.exit(130)\n"
    )

    result = c.exec(
        [_PYTHON_BIN, "-c", payload],
        env={"PATH": f"{_VENV_BIN}:/usr/local/bin:/usr/bin:/bin"},
        workdir="/workspace",
        check=False,
    )

    assert result.returncode == 130, result.stdout + result.stderr
    # rc-file content unchanged + non-empty.
    after = c.read_text(rc_path)
    assert after == original, after
    # No leftover tmp file (cleanup not required by spec but documents
    # the invariant — tmp creation aborted before file existed).
    tmp_ls = c.exec(["ls", "-la", "/home/tester/"], check=False).stdout
    assert ".zshrc.setforge-tmp" not in tmp_ls, tmp_ls
