"""Docker E2E tests for setforge-gtqv completion static-template fallback.

Six named cases per SPEC 1:

1. ``test_zsh_completion_install_falls_back`` — subprocess shim returns rc=2,
   ``setforge completion install zsh`` succeeds + uses vendored template.
2. ``test_bash_completion_install_falls_back`` — same pattern, bash branch.
3. ``test_fish_completion_install_falls_back`` — same, fish lands in
   ``~/.config/fish/completions/``.
4. ``test_rc_file_atomic_under_sigint`` — SIGINT mid-write leaves rc-file
   byte-identical to its original content.
5. ``test_happy_path_uses_typer_generated`` — subprocess succeeds, vendored
   template NOT touched.
6. ``test_vendored_zsh_passes_syntax_check`` — ``zsh -n`` exits 0 on the
   committed vendored zsh template.

The shim trick: drop a ``/tmp/shim/setforge`` script earlier in PATH that
``exit 2``s on ``--show-completion=...``. The outer invocation is the
absolute venv-bin path so it doesn't pick up the shim; the in-process
``shutil.which("setforge")`` resolves PATH at runtime and lands on the
shim, so the subprocess call inside ``_render_completion_script`` fails
and the vendored template fallback runs.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

# Absolute path to the project venv's setforge binary inside the
# container (set in the Dockerfile's tester USER step). Used so the
# outer ``setforge completion install`` invocation bypasses any shim
# we drop into PATH for the inner subprocess.
_VENV_BIN = "/workspace/.venv/bin"
_SETFORGE_BIN = f"{_VENV_BIN}/setforge"
_PYTHON_BIN = f"{_VENV_BIN}/python"
_VENDORED_DIR = "/workspace/setforge/cli/completions"


def _install_shim_returncode(c: ContainerHandle, rc: int) -> str:
    """Drop ``/tmp/shim/setforge`` that ignores argv and exits with ``rc``.

    Returns the directory we placed it under so callers can prepend it
    to PATH for the failing-subprocess child.
    """
    shim_dir = "/tmp/shim"
    c.exec(["mkdir", "-p", shim_dir], check=True)
    shim_path = f"{shim_dir}/setforge"
    c.write_text(shim_path, f"#!/bin/sh\nexit {rc}\n")
    c.exec(["chmod", "+x", shim_path], check=True)
    return shim_dir


def _ensure_rc_files(c: ContainerHandle) -> None:
    """Touch ~/.zshrc and ~/.bashrc so `_write_wiring` doesn't refuse."""
    c.exec(["touch", "/home/tester/.zshrc", "/home/tester/.bashrc"], check=True)


def _vendored_content(c: ContainerHandle, filename: str) -> str:
    return c.read_text(f"{_VENDORED_DIR}/{filename}")


# ---------------------------------------------------------------------------
# E2E #1 — zsh fallback path
# ---------------------------------------------------------------------------


def test_zsh_completion_install_falls_back(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _ensure_rc_files(c)
    shim_dir = _install_shim_returncode(c, rc=2)
    env = {"PATH": f"{shim_dir}:{_VENV_BIN}:/usr/local/bin:/usr/bin:/bin"}

    result = c.exec(
        [_SETFORGE_BIN, "completion", "install", "zsh", "--non-interactive"],
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    written = c.read_text("/home/tester/.config/setforge/completions/_setforge")
    vendored = _vendored_content(c, "_setforge")
    assert written == vendored
    # WARNING surfaced via stderr (Python logging lastResort handler).
    combined = result.stdout + result.stderr
    assert "fallback" in combined.lower() or "typer regression" in combined.lower(), (
        combined
    )


# ---------------------------------------------------------------------------
# E2E #2 — bash fallback path
# ---------------------------------------------------------------------------


def test_bash_completion_install_falls_back(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _ensure_rc_files(c)
    shim_dir = _install_shim_returncode(c, rc=2)
    env = {"PATH": f"{shim_dir}:{_VENV_BIN}:/usr/local/bin:/usr/bin:/bin"}

    result = c.exec(
        [_SETFORGE_BIN, "completion", "install", "bash", "--non-interactive"],
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    written = c.read_text("/home/tester/.config/setforge/completions/setforge.bash")
    vendored = _vendored_content(c, "setforge.bash")
    assert written == vendored


# ---------------------------------------------------------------------------
# E2E #3 — fish fallback path lands in ~/.config/fish/completions/
# ---------------------------------------------------------------------------


def test_fish_completion_install_falls_back(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    shim_dir = _install_shim_returncode(c, rc=2)
    env = {"PATH": f"{shim_dir}:{_VENV_BIN}:/usr/local/bin:/usr/bin:/bin"}

    result = c.exec(
        [_SETFORGE_BIN, "completion", "install", "fish", "--non-interactive"],
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    target = "/home/tester/.config/fish/completions/setforge.fish"
    written = c.read_text(target)
    vendored = _vendored_content(c, "setforge.fish")
    assert written == vendored


# ---------------------------------------------------------------------------
# E2E #4 — rc-file atomic under SIGINT: original bytes preserved
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


# ---------------------------------------------------------------------------
# E2E #5 — happy path: subprocess works, vendored template NOT touched
# ---------------------------------------------------------------------------


def test_happy_path_uses_typer_generated(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """When ``setforge --show-completion`` succeeds, NO fallback warning."""
    c = docker_container()
    _ensure_rc_files(c)
    # No shim: the real venv setforge handles --show-completion.
    env = {"PATH": f"{_VENV_BIN}:/usr/local/bin:/usr/bin:/bin"}

    result = c.exec(
        [_SETFORGE_BIN, "completion", "install", "zsh", "--non-interactive"],
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    # No fallback warning markers should appear on the happy path.
    assert "fallback" not in combined.lower(), combined
    assert "typer regression" not in combined.lower(), combined
    assert "FileNotFoundError" not in combined, combined
    # The script lands at the canonical zsh script path.
    written = c.read_text("/home/tester/.config/setforge/completions/_setforge")
    # Real typer output happens to be byte-identical to the vendored
    # copy (the vendored copy is seeded from typer output and the drift
    # gate enforces parity), so we assert that invariant here too.
    vendored = _vendored_content(c, "_setforge")
    assert written == vendored


# ---------------------------------------------------------------------------
# E2E #6 — vendored zsh template passes `zsh -n` syntax check
# ---------------------------------------------------------------------------


def test_vendored_zsh_passes_syntax_check(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``zsh -n`` exits 0 on the vendored ``_setforge`` template."""
    c = docker_container()
    result = c.exec(
        ["zsh", "-n", f"{_VENDORED_DIR}/_setforge"],
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Bonus: vendored bash template passes `shellcheck -S error` (anti-pattern
# check #6 + spec line 83). Not in the named-six set per spec but cheap
# to add now that shellcheck ships in the test image.
# ---------------------------------------------------------------------------


def test_vendored_bash_passes_shellcheck(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    result = c.exec(
        ["shellcheck", "-S", "error", f"{_VENDORED_DIR}/setforge.bash"],
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
