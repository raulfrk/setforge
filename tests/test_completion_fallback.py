"""Unit tests for the static-template fallback (setforge-gtqv).

When ``setforge --show-completion=<shell>`` fails, ``_render_completion_script``
must:

1. Log a WARNING naming the failure mode (binary-missing, timeout,
   non-zero exit, empty stdout).
2. Return the vendored template from :mod:`setforge.cli.completions`.

Plus an atomicity test for ``_atomic_write_rc_file`` under SIGINT.
"""

from __future__ import annotations

import ast
import importlib.resources
import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

import pytest

from setforge.cli.completion import (
    ShellKind,
    _atomic_write_rc_file,
    _load_vendored_template,
    _render_completion_script,
)

# Sentinel fragments that appear in the typer-generated output for each
# shell but NOT in arbitrary stdout. Tests assert these to confirm the
# returned content is the vendored template (and not e.g. fake stdout
# from a botched mock).
_ZSH_SIGIL = "#compdef setforge"
_BASH_SIGIL = "_setforge_completion"
_FISH_SIGIL = "_SETFORGE_COMPLETE=complete_fish"


@pytest.fixture
def fake_subprocess_run(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Return a setter that pins ``subprocess.run`` to a canned callable.

    Each test passes its own ``fake_run`` so we can branch on
    returncode, raise ``FileNotFoundError``, or raise
    ``subprocess.TimeoutExpired`` per case.
    """

    def install(fake_run):
        monkeypatch.setattr("setforge.cli.completion.subprocess.run", fake_run)

    return install


# ---------------------------------------------------------------------------
# Fallback cases — each test asserts (a) vendored content returned, (b)
# distinct WARNING log emitted naming the failure mode.
# ---------------------------------------------------------------------------


def test_show_completion_returncode_nonzero_uses_vendored(
    caplog: pytest.LogCaptureFixture,
    fake_subprocess_run,
) -> None:
    """returncode=2 (typer regression) → vendored fallback + WARNING."""

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="boom")

    fake_subprocess_run(fake_run)

    with caplog.at_level(logging.WARNING, logger="setforge.cli.completion"):
        out = _render_completion_script(ShellKind.ZSH)

    assert _ZSH_SIGIL in out
    assert out == _load_vendored_template(ShellKind.ZSH)
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("typer regression" in m and "exit 2" in m for m in msgs), msgs
    assert any("boom" in m for m in msgs), msgs


def test_show_completion_filenotfound_uses_vendored(
    caplog: pytest.LogCaptureFixture,
    fake_subprocess_run,
) -> None:
    """FileNotFoundError → vendored fallback + 'binary not found' WARNING."""

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del argv, kwargs
        raise FileNotFoundError("setforge: No such file or directory")

    fake_subprocess_run(fake_run)

    with caplog.at_level(logging.WARNING, logger="setforge.cli.completion"):
        out = _render_completion_script(ShellKind.BASH)

    assert _BASH_SIGIL in out
    assert out == _load_vendored_template(ShellKind.BASH)
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("binary not found" in m for m in msgs), msgs
    assert any("FileNotFoundError" in m for m in msgs), msgs


def test_show_completion_timeout_uses_vendored(
    caplog: pytest.LogCaptureFixture,
    fake_subprocess_run,
) -> None:
    """TimeoutExpired → vendored fallback + 'subprocess timeout' WARNING."""

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        raise subprocess.TimeoutExpired(cmd=argv, timeout=10.0)

    fake_subprocess_run(fake_run)

    with caplog.at_level(logging.WARNING, logger="setforge.cli.completion"):
        out = _render_completion_script(ShellKind.FISH)

    assert _FISH_SIGIL in out
    assert out == _load_vendored_template(ShellKind.FISH)
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("subprocess timeout" in m for m in msgs), msgs


def test_show_completion_empty_stdout_uses_vendored(
    caplog: pytest.LogCaptureFixture,
    fake_subprocess_run,
) -> None:
    """Empty stdout (exit 0) → vendored fallback + 'empty stdout' WARNING."""

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="   \n", stderr="")

    fake_subprocess_run(fake_run)

    with caplog.at_level(logging.WARNING, logger="setforge.cli.completion"):
        out = _render_completion_script(ShellKind.ZSH)

    assert _ZSH_SIGIL in out
    assert out == _load_vendored_template(ShellKind.ZSH)
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("empty stdout" in m for m in msgs), msgs


def test_show_completion_happy_path_uses_typer_output(
    caplog: pytest.LogCaptureFixture,
    fake_subprocess_run,
) -> None:
    """subprocess.run rc=0 with body → returns body verbatim, NO warning."""
    typer_body = "#compdef setforge\n# typer generated\n_setforge_completion(){:;}\n"

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(argv, 0, stdout=typer_body, stderr="")

    fake_subprocess_run(fake_run)

    with caplog.at_level(logging.WARNING, logger="setforge.cli.completion"):
        out = _render_completion_script(ShellKind.ZSH)

    assert out == typer_body
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert msgs == [], msgs


# ---------------------------------------------------------------------------
# Vendored templates: package data + content sanity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shell", "expected_filename", "expected_sigil"),
    [
        (ShellKind.ZSH, "_setforge", _ZSH_SIGIL),
        (ShellKind.BASH, "setforge.bash", _BASH_SIGIL),
        (ShellKind.FISH, "setforge.fish", _FISH_SIGIL),
    ],
)
def test_vendored_template_loads_via_importlib_resources(
    shell: ShellKind, expected_filename: str, expected_sigil: str
) -> None:
    """Vendored templates are real package data, not generated at install."""
    resource = importlib.resources.files("setforge.cli.completions").joinpath(
        expected_filename
    )
    assert resource.is_file(), f"{expected_filename} missing from package data"
    body = resource.read_text(encoding="utf-8")
    assert expected_sigil in body
    assert _load_vendored_template(shell) == body


def test_vendored_zsh_template_does_not_call_compinit() -> None:
    """Anti-pattern check #5: the vendored zsh template MUST NOT call compinit.

    The wiring block in ~/.zshrc is the only place compinit is invoked
    (and it's guarded by ``command -v compinit``). The template itself
    is just the typer ``_setforge_completion`` function + ``compdef``
    binding.
    """
    body = _load_vendored_template(ShellKind.ZSH)
    for line in body.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("compinit"), line
        assert "autoload" not in stripped or "compinit" not in stripped, line


# ---------------------------------------------------------------------------
# AST-level anti-pattern check: _render_completion_script has ≥3 distinct
# fallback WARNING messages (spec line 113).
# ---------------------------------------------------------------------------


def test_render_completion_script_has_three_distinct_warning_messages() -> None:
    """spec anti-pattern check #3: AST count of WARNING-message format strings."""
    src = Path(
        importlib.resources.files("setforge.cli").joinpath("completion.py")  # type: ignore[arg-type]
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    target_name = "_render_completion_script"
    render = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == target_name
    )
    # Collect every constant-string literal passed as the FIRST arg to a
    # LOGGER.warning(...) call inside the function body.
    messages: list[str] = []
    for node in ast.walk(render):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "warning"
            and isinstance(func.value, ast.Name)
            and func.value.id == "LOGGER"
        ):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            messages.append(first.value)
    assert len(set(messages)) >= 3, messages


# ---------------------------------------------------------------------------
# Atomic rc-file write: SIGINT mid-write leaves original untouched.
# ---------------------------------------------------------------------------


def test_atomic_write_rc_file_preserves_mode(tmp_path: Path) -> None:
    """``_atomic_write_rc_file`` mirrors original mode bits via copystat."""
    rc = tmp_path / ".zshrc"
    rc.write_text("# original\n")
    rc.chmod(0o600)
    _atomic_write_rc_file(rc, "# replaced\n")
    assert rc.read_text() == "# replaced\n"
    assert rc.stat().st_mode & 0o777 == 0o600


def test_atomic_write_rc_file_no_tmp_residue(tmp_path: Path) -> None:
    """After a successful atomic replace, no ``.setforge-tmp`` file remains."""
    rc = tmp_path / ".bashrc"
    rc.write_text("# original\n")
    _atomic_write_rc_file(rc, "# new content\n")
    assert not (tmp_path / ".bashrc.setforge-tmp").exists()
    assert list(tmp_path.iterdir()) == [rc]


def test_rc_file_write_is_atomic_under_sigint(tmp_path: Path) -> None:
    """SIGINT mid-write must leave ``rc_path`` byte-identical to before.

    Simulates SIGINT landing inside the tmp-file ``write_text`` call by
    monkeypatching ``Path.write_text`` to raise ``KeyboardInterrupt`` when
    the target's name ends with ``.setforge-tmp``. The invariants are:

    1. ``KeyboardInterrupt`` propagates out of ``_atomic_write_rc_file``
       (the caller's outer SIGINT handler is responsible for graceful
       exit — we don't swallow it).
    2. The original rc-file's content is byte-identical to before the
       call (``os.replace`` never ran).
    3. The original rc-file is non-empty (not zero-byte) — confirming we
       did NOT truncate the original on the way to the failed replace.
    """
    rc = tmp_path / ".zshrc"
    original = "# user content\nexport FOO=1\nalias ls='ls --color=auto'\n"
    rc.write_text(original)

    real_write_text = Path.write_text
    write_text_calls: list[Path] = []

    def faulty_write_text(self: Path, *args: Any, **kwargs: Any) -> int:
        write_text_calls.append(self)
        if self.name.endswith(".setforge-tmp"):
            # Emulate SIGINT landing during the tmp-file write.
            raise KeyboardInterrupt("simulated SIGINT mid-write")
        return real_write_text(self, *args, **kwargs)

    try:
        Path.write_text = faulty_write_text  # type: ignore[method-assign]
        with pytest.raises(KeyboardInterrupt):
            _atomic_write_rc_file(rc, "# CORRUPTED — must not land\n")
    finally:
        Path.write_text = real_write_text  # type: ignore[method-assign]

    # The tmp file was the target of the raising write_text.
    assert any(p.name.endswith(".setforge-tmp") for p in write_text_calls)
    # The rc file content survived intact: os.replace never ran.
    assert rc.read_text() == original
    # And the rc file is non-empty (zero-byte regression guard).
    assert rc.stat().st_size == len(original.encode("utf-8"))


# ---------------------------------------------------------------------------
# CI drift gate: vendored copies must match what `setforge --show-completion`
# emits today. When they drift, the test fails red and a maintainer either
# regenerates or investigates the typer change. We do NOT auto-regenerate.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shell", "filename"),
    [
        (ShellKind.ZSH, "_setforge"),
        (ShellKind.BASH, "setforge.bash"),
        (ShellKind.FISH, "setforge.fish"),
    ],
)
def test_vendored_templates_match_typer_output(shell: ShellKind, filename: str) -> None:
    """Drift gate: vendored content must match `setforge --show-completion`.

    Invokes the real setforge binary in the project venv and diffs its
    stdout against the committed vendored copy. Drift → red; the fix is
    to regenerate the vendored copy (and audit the change), NOT to
    silently overwrite.
    """
    repo_root = Path(__file__).resolve().parents[1]
    vendored = (repo_root / "setforge" / "cli" / "completions" / filename).read_text(
        encoding="utf-8"
    )
    env = {**os.environ, "_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION": "1"}
    # Locate the actual setforge entry point in the active venv.
    import shutil as _shutil

    bin_path = _shutil.which("setforge")
    if bin_path is None:
        pytest.skip("setforge binary not on PATH (test runs in project venv)")
    proc = subprocess.run(
        [bin_path, f"--show-completion={shell.value}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == vendored, (
        f"Vendored {filename} drifted from typer output. "
        "Regenerate via:\n"
        f"  _TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION=1 "
        f"setforge --show-completion={shell.value} > "
        f"setforge/cli/completions/{filename}"
    )


# ---------------------------------------------------------------------------
# Defensive: SIGINT-handler integration check (signals only work in the
# main thread; included to document the contract). Skip on Windows.
# ---------------------------------------------------------------------------


def test_sigint_handler_default_installed_for_test_environment() -> None:
    """Sanity check: default SIGINT handler raises KeyboardInterrupt.

    Documents the invariant the atomic-write test relies on: under the
    default Python handler, SIGINT mid-syscall raises
    ``KeyboardInterrupt`` which the test catches.
    """
    if not hasattr(signal, "SIGINT"):  # pragma: no cover — POSIX-only
        pytest.skip("SIGINT not available on this platform")
    handler = signal.getsignal(signal.SIGINT)
    # pytest installs its own handler, but it still raises KeyboardInterrupt
    # — assert it's not SIG_IGN (which would mask the SIGINT-mid-write contract).
    assert handler not in (signal.SIG_IGN, None), handler
