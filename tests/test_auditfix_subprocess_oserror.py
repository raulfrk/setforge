"""Regression tests: subprocess.run OSError must degrade gracefully.

A which()-resolved binary (gitleaks / cargo / git) can still fail to
exec — removed in the TOCTOU window, replaced by a non-executable
file, broken wrapper/shebang. subprocess.run then raises OSError
(FileNotFoundError / PermissionError), NOT TimeoutExpired /
CalledProcessError. Pre-fix these escaped the per-module handlers and
crashed ``setforge install`` with a raw traceback. Each module now
catches OSError alongside its existing subprocess-exception handling.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

from setforge import binaries, cargo, secrets
from setforge.errors import GitOpError


# --------------------------------------------------------------------------
# secrets.run_pre_deploy_scan — gitleaks exec failure → warn-and-continue
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("gitleaks vanished"),
        PermissionError("gitleaks not executable"),
    ],
)
def test_secrets_scan_oserror_warns_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    exc: OSError,
) -> None:
    monkeypatch.setattr(
        secrets.binaries, "resolve_binary", lambda _name: Path("/fake/gitleaks")
    )

    def _raise(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise exc

    monkeypatch.setattr(secrets.subprocess, "run", _raise)

    result = secrets.run_pre_deploy_scan(
        tracked_root=tmp_path, allowlist_path=tmp_path / "allow"
    )

    captured = capsys.readouterr()
    assert result.findings == ()
    assert result.files_scanned == 0
    assert "could not be executed" in captured.err
    assert "continuing without secrets check" in captured.err


# --------------------------------------------------------------------------
# cargo.install_cargo_binaries — `cargo install` exec failure → recorded
# --------------------------------------------------------------------------
def test_cargo_install_oserror_recorded_not_raised(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cargo, "resolve_binary", lambda _name: Path("/fake/cargo"))

    def _run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        if argv[1] == "install" and argv[2] == "--list":
            return subprocess.CompletedProcess(argv, 0, stdout="")
        raise FileNotFoundError("cargo vanished")

    monkeypatch.setattr(cargo.subprocess, "run", _run)

    failed = cargo.install_cargo_binaries(["ripgrep"])

    assert len(failed) == 1
    assert failed[0][0] == "ripgrep"
    assert "cargo install ripgrep failed" in capsys.readouterr().err


# --------------------------------------------------------------------------
# cargo._installed_crates — `--list` exec failure → empty set (degrade)
# --------------------------------------------------------------------------
def test_cargo_installed_crates_oserror_returns_empty_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise PermissionError("cargo not executable")

    monkeypatch.setattr(cargo.subprocess, "run", _run)

    assert cargo._installed_crates("/fake/cargo") == set()


# --------------------------------------------------------------------------
# git_ops._run_git — git exec failure → GitOpError (clean exit), masked args
# --------------------------------------------------------------------------
def test_git_run_oserror_becomes_gitoperror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import setforge.git_ops as git_ops_mod

    monkeypatch.setattr(git_ops_mod, "_git_bin", lambda: "git")

    def _raise(*_a: object, **_kw: object) -> NoReturn:
        raise OSError("exec format error")

    monkeypatch.setattr(git_ops_mod.subprocess, "run", _raise)

    with pytest.raises(GitOpError) as excinfo:
        git_ops_mod._run_git(["status"])

    msg = str(excinfo.value)
    assert "could not be executed" in msg
    assert "git status" in msg


def test_git_run_oserror_masks_credentials_in_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The masked-args contract holds on the OSError path too."""
    import setforge.git_ops as git_ops_mod

    monkeypatch.setattr(git_ops_mod, "_git_bin", lambda: "git")

    def _raise(*_a: object, **_kw: object) -> NoReturn:
        raise OSError("exec format error")

    monkeypatch.setattr(git_ops_mod.subprocess, "run", _raise)

    with pytest.raises(GitOpError) as excinfo:
        git_ops_mod._run_git(["clone", "https://u:ghp_SECRET@github.com/o/r.git"])

    assert "ghp_SECRET" not in str(excinfo.value)


# Keep ``binaries`` import meaningful (resolve_binary precedence surface).
assert hasattr(binaries, "resolve_binary")
