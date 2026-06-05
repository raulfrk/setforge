"""Integration tests for the interactive conflict wizard wired into install.

Drive the real ``setforge install`` CLI over a conflicting ``disposition:
shared`` file with a SCRIPTED resolver injected through the
``_build_conflict_resolver`` seam (monkeypatched in :mod:`setforge.cli.install`),
so no real tty is needed. Assert:

- the deployed live file reflects the scripted per-conflict choice
  (KEEP_OURS / TAKE_THEIRS / EDIT);
- a SKIP keeps live and does NOT advance the base (conflict re-detects);
- under ``--auto=use-tracked`` the wizard resolver is NEVER invoked (auto wins);
- a bare non-interactive install still warns + defers (non-interactive path).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store
from setforge.cli import app
from setforge.cli import install as install_mod
from setforge.disposition_merge import (
    ConflictChoice,
    ConflictResolution,
    ConflictResolver,
)
from setforge.markdown_merge import LineConflict
from setforge.scalar_merge import ScalarConflict
from setforge.structural_merge import PathConflict

_PROFILE = "test-wizard"
_FILE_ID = "shared_text"


def _write_config(repo: Path) -> Path:
    """Write a setforge.yaml with a shared disposition file; return its path."""
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  shared_text:\n"
        "    src: text/note.txt\n"
        "    dst: ~/.setforge_wiz/note.txt\n"
        "    disposition: shared\n"
        "  anchor:\n"
        "    src: text/anchor.txt\n"
        "    dst: ~/.setforge_wiz/anchor.txt\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - shared_text\n"
        "      - anchor\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, body: str) -> None:
    """Write the tracked sources for ``shared_text`` and ``anchor``."""
    src = repo / "tracked" / "text" / "note.txt"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(body, encoding="utf-8")
    (src.parent / "anchor.txt").write_text("anchor\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp config repo with sandboxed ``$HOME`` + ``$SETFORGE_STATE_DIR``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _live_path() -> Path:
    """Resolve the sandboxed live destination path."""
    return Path.home() / ".setforge_wiz" / "note.txt"


def _install(config: Path, *, extra: list[str] | None = None) -> Result:
    """Run ``setforge install`` against ``config``; return the CliRunner result."""
    args = [
        "install",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-transition",
        "--no-secrets-scan",
        "--no-git-check",
        "--yes",
    ]
    if extra:
        args.extend(extra)
    return CliRunner().invoke(app, args)


def _inject_resolver(
    monkeypatch: pytest.MonkeyPatch, resolver: ConflictResolver
) -> list[LineConflict | PathConflict | ScalarConflict]:
    """Force ``install`` to use ``resolver`` regardless of tty; record conflicts.

    Patches the ``_build_conflict_resolver`` seam so the wizard gate (tty +
    interactive) is bypassed for the test. Returns a list the wrapped resolver
    appends each seen conflict to, so a test can assert the resolver was (or was
    not) invoked.
    """
    seen: list[LineConflict | PathConflict | ScalarConflict] = []

    def _wrapped(
        conflict: LineConflict | PathConflict | ScalarConflict,
    ) -> ConflictResolution:
        seen.append(conflict)
        return resolver(conflict)

    def _fake_build(
        *, reconcile_user_sections: bool, section_auto: object
    ) -> ConflictResolver | None:
        # Honor the --auto short-circuit: when an auto mode is set, install
        # never builds a resolver (auto wins). Mirror that here so the
        # auto-path test exercises the real gate.
        if section_auto is not None:
            return None
        return _wrapped

    monkeypatch.setattr(install_mod, "_build_conflict_resolver", _fake_build)
    return seen


def _const_resolver(res: ConflictResolution) -> ConflictResolver:
    """A resolver returning ``res`` for every conflict."""

    def _resolve(
        _conflict: LineConflict | PathConflict | ScalarConflict,
    ) -> ConflictResolution:
        return res

    return _resolve


def _seed_conflict(repo: Path) -> Path:
    """First-install, then diverge both sides on the same line for a conflict."""
    _write_tracked(repo, "one\ntwo\nthree\n")
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    _live_path().write_text("one\ntwo-LIVE\nthree\n", encoding="utf-8")
    _write_tracked(repo, "one\ntwo-TRACKED\nthree\n")
    return config


def test_wizard_keep_ours_keeps_live_and_advances(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scripted KEEP_OURS: live kept, base advances (no skip)."""
    config = _seed_conflict(repo)
    seen = _inject_resolver(
        monkeypatch, _const_resolver(ConflictResolution(ConflictChoice.KEEP_OURS))
    )
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert seen, "resolver was not invoked"
    live = _live_path().read_text(encoding="utf-8")
    assert "two-LIVE" in live
    # KEEP_OURS is not a skip → base advances to the merged (live) text.
    assert base_store.read_base(_PROFILE, _FILE_ID) == live.encode("utf-8")


def test_wizard_take_theirs_writes_tracked(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scripted TAKE_THEIRS: live takes the tracked side; base advances."""
    config = _seed_conflict(repo)
    _inject_resolver(
        monkeypatch, _const_resolver(ConflictResolution(ConflictChoice.TAKE_THEIRS))
    )
    result = _install(config)
    assert result.exit_code == 0, result.output
    live = _live_path().read_text(encoding="utf-8")
    assert "two-TRACKED" in live
    assert "two-LIVE" not in live
    assert base_store.read_base(_PROFILE, _FILE_ID) == live.encode("utf-8")


def test_wizard_edit_splices_edited_lines(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scripted EDIT: the edited lines land in the live file."""
    config = _seed_conflict(repo)
    edit = ConflictResolution(ConflictChoice.EDIT, edited_lines=["two-EDITED\n"])
    _inject_resolver(monkeypatch, _const_resolver(edit))
    result = _install(config)
    assert result.exit_code == 0, result.output
    live = _live_path().read_text(encoding="utf-8")
    assert "two-EDITED" in live
    assert "two-LIVE" not in live
    assert "two-TRACKED" not in live


def test_wizard_skip_keeps_live_and_defers_base(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scripted SKIP: live kept, base NOT advanced — conflict re-detects."""
    config = _seed_conflict(repo)
    base_before = base_store.read_base(_PROFILE, _FILE_ID)
    _inject_resolver(
        monkeypatch, _const_resolver(ConflictResolution(ConflictChoice.SKIP))
    )
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "two-LIVE" in _live_path().read_text(encoding="utf-8")
    # Any-skip-defers: base stays put.
    assert base_store.read_base(_PROFILE, _FILE_ID) == base_before


def test_auto_use_tracked_does_not_invoke_wizard(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under --auto=use-tracked the resolver is never built/invoked (auto wins)."""
    config = _seed_conflict(repo)
    seen = _inject_resolver(
        monkeypatch, _const_resolver(ConflictResolution(ConflictChoice.KEEP_OURS))
    )
    result = _install(config, extra=["--auto=use-tracked"])
    assert result.exit_code == 0, result.output
    # Resolver never saw a conflict — the auto path resolved it.
    assert seen == []
    live = _live_path().read_text(encoding="utf-8")
    assert "two-TRACKED" in live
    assert base_store.read_base(_PROFILE, _FILE_ID) == live.encode("utf-8")


def test_bare_noninteractive_install_warns_and_defers(repo: Path) -> None:
    """No resolver injected (bare, non-tty CliRunner): warn + defer path."""
    config = _seed_conflict(repo)
    base_before = base_store.read_base(_PROFILE, _FILE_ID)
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "two-LIVE" in _live_path().read_text(encoding="utf-8")
    assert "conflict" in result.output.lower()
    assert base_store.read_base(_PROFILE, _FILE_ID) == base_before
