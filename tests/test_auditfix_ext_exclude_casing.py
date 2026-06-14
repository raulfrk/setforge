"""Regression tests: `exclude` must win case-insensitively.

VSCode treats extension IDs case-insensitively and `code --list-extensions`
echoes the publisher's canonical casing (e.g. ``GitHub.copilot``). A user who
types ``exclude: [github.copilot]`` (lowercase) against an installed/included
``GitHub.copilot`` must still have it excluded. The pre-fix code subtracted
``exclude`` from ``include`` (and ``installed``) case-SENSITIVELY, silently
defeating the "exclude always wins" invariant in both :func:`reconcile`
(PRUNE/ADDITIVE) and :func:`capture_extensions`.

The ``subprocess.run`` driver and config fixtures mirror
``tests/test_vscode_extensions.py``.
"""

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from setforge.config import Extensions, ReconcilePolicy, load_config
from setforge.vscode_extensions import capture_extensions, reconcile


class FakeCode:
    """Tracks calls to a faked ``code`` CLI and mutates installed state."""

    def __init__(self, installed: list[str]) -> None:
        self.installed: list[str] = list(installed)
        self.calls: list[list[str]] = []

    def run(self, args, **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[1] == "--list-extensions":
            stdout = "\n".join(self.installed) + ("\n" if self.installed else "")
            return subprocess.CompletedProcess(args, 0, stdout, "")
        if args[1] == "--install-extension":
            ext_id = args[2]
            if ext_id not in self.installed:
                self.installed.append(ext_id)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "--uninstall-extension":
            ext_id = args[2]
            if ext_id in self.installed:
                self.installed.remove(ext_id)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected code invocation: {args!r}")

    @property
    def install_args(self) -> list[str]:
        return [c[2] for c in self.calls if c[1] == "--install-extension"]

    @property
    def uninstall_args(self) -> list[str]:
        return [c[2] for c in self.calls if c[1] == "--uninstall-extension"]


@pytest.fixture
def fake_code(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[str]], FakeCode]:
    def factory(installed: list[str]) -> FakeCode:
        fake = FakeCode(installed)
        monkeypatch.setattr(
            "setforge.vscode_extensions.resolve_binary",
            lambda name: Path("/usr/bin/code") if name == "code" else None,
        )
        monkeypatch.setattr("setforge.vscode_extensions.subprocess.run", fake.run)
        return fake

    return factory


def test_exclude_wins_under_case_mismatch_prune(fake_code) -> None:
    """PRUNE: a lowercase exclude must drop a canonically-cased included id."""
    fake = fake_code(["GitHub.copilot", "keep.me"])
    ext = Extensions(
        include=["GitHub.copilot", "keep.me"],
        exclude=["github.copilot"],  # lowercase, as commonly typed
        reconcile=ReconcilePolicy.PRUNE,
    )
    report = reconcile(ext)

    assert report.to_uninstall == ["GitHub.copilot"]
    assert report.to_install == []
    assert fake.uninstall_args == ["GitHub.copilot"]
    assert "GitHub.copilot" not in fake.installed


def test_exclude_wins_under_case_mismatch_additive(fake_code) -> None:
    """ADDITIVE: a lowercase exclude must not re-install the included id."""
    fake = fake_code([])
    ext = Extensions(
        include=["GitHub.copilot", "keep.me"],
        exclude=["github.copilot"],
        reconcile=ReconcilePolicy.ADDITIVE,
    )
    report = reconcile(ext)

    assert report.to_install == ["keep.me"]
    assert "GitHub.copilot" not in report.to_install
    assert fake.install_args == ["keep.me"]


_FIXTURE_YAML = """\
version: 1

tracked_files:
  d:
    src: x
    dst: y

profiles:
  base:
    tracked_files:
      - d
    extensions:
      include: []
      exclude:
        - github.copilot
"""


def test_capture_excludes_case_insensitively(tmp_path: Path, fake_code) -> None:
    """capture must not write an excluded id back into ``include`` just
    because the installed set uses different casing."""
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(_FIXTURE_YAML, encoding="utf-8")
    fake_code(["GitHub.copilot", "keep.me"])  # canonical casing from `code`

    capture_extensions(cfg, "base")

    reloaded = load_config(cfg)
    include = reloaded.profiles["base"].extensions.include
    assert "GitHub.copilot" not in include
    assert "github.copilot" not in include
    assert "keep.me" in include
