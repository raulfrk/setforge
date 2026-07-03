"""Integration tests for the interactive conflict wizard wired into install.

Drive the real ``setforge install`` CLI over a plain tracked file with a 3-way
conflict, forcing the interactive path by monkeypatching
``_want_interactive_reconcile`` (→ ``True``) and scripting
``reconcile_apply.resolve_conflicts``, so no real tty is needed. Assert:

- the deployed live file reflects the scripted per-conflict choice
  (KEEP_OURS / TAKE_THEIRS / EDIT);
- a SKIP keeps live and does NOT advance the base (conflict re-detects);
- under ``--auto=use-tracked`` the wizard is NEVER invoked (auto wins);
- a bare non-interactive install still warns + defers (non-interactive path).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store, reconcile_apply
from setforge.cli import app
from setforge.cli import install as install_mod
from setforge.reconcile.merge_model import Clean, Conflict, MergeResult, Segment
from setforge.reconcile.wizard import WizardResult

_PROFILE = "test-wizard"
_FILE_ID = "shared_text"
_TRACKED_CONFLICT = b"one\ntwo-TRACKED\nthree\n"


def _write_config(repo: Path) -> Path:
    """Write a setforge.yaml with a shared tracked file; return its path."""
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  shared_text:\n"
        "    src: text/note.txt\n"
        "    dst: ~/.setforge_wiz/note.txt\n"
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


def _script_wizard(
    monkeypatch: pytest.MonkeyPatch,
    *,
    choose: Callable[[Conflict], tuple[bytes, bool]],
) -> list[Conflict]:
    """Script the reconcile conflict wizard regardless of tty; record regions.

    Under the unified reconcile model the plain-file 3-way merge resolves a
    conflict through :func:`setforge.reconcile.wizard.resolve_conflicts`. Two
    seams are patched: install's ``_want_interactive_reconcile`` returns
    ``True`` so the deploy routes the file interactively (still honoring the
    ``--auto`` short-circuit), and ``reconcile_apply.resolve_conflicts`` is
    replaced with a scripted resolver that maps each conflict region to
    ``(chosen_bytes, was_skip)`` via ``choose``. Returns a list every seen
    conflict region is appended to, so a test can assert the wizard was (or was
    not) invoked.
    """
    seen: list[Conflict] = []

    def _fake_resolve(
        file_id: object,
        result: MergeResult,
        *,
        display_path: str | None = None,
        claude_merge: object = None,
    ) -> WizardResult:
        out: list[Segment] = []
        deferred = False
        for seg in result.segments:
            if isinstance(seg, Clean):
                out.append(seg)
                continue
            seen.append(seg)
            chosen, was_skip = choose(seg)
            out.append(Clean(chosen))
            deferred = deferred or was_skip
        return WizardResult(MergeResult(tuple(out), absent=result.absent), deferred)

    def _fake_want_interactive(
        *, reconcile_user_sections: bool, section_auto: object
    ) -> bool:
        # Honor the --auto short-circuit: when an auto mode is set, install
        # never offers the interactive wizard (auto wins). Mirror that here so
        # the auto-path test exercises the real gate.
        return section_auto is None

    monkeypatch.setattr(
        install_mod, "_want_interactive_reconcile", _fake_want_interactive
    )
    monkeypatch.setattr(reconcile_apply, "resolve_conflicts", _fake_resolve)
    return seen


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
    """Scripted keep-ours: live kept, base advances (no skip)."""
    config = _seed_conflict(repo)
    seen = _script_wizard(monkeypatch, choose=lambda c: (c.ours, False))
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert seen, "wizard was not invoked"
    live = _live_path().read_text(encoding="utf-8")
    assert "two-LIVE" in live
    # Not a skip → the reconcile base advances to the tracked (upstream) side;
    # the deployed live rides the reconcile store's local leg, not the base.
    assert base_store.read_base(_PROFILE, _FILE_ID) == _TRACKED_CONFLICT


def test_wizard_take_theirs_writes_tracked(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scripted take-theirs: live takes the tracked side; base advances."""
    config = _seed_conflict(repo)
    _script_wizard(monkeypatch, choose=lambda c: (c.theirs, False))
    result = _install(config)
    assert result.exit_code == 0, result.output
    live = _live_path().read_text(encoding="utf-8")
    assert "two-TRACKED" in live
    assert "two-LIVE" not in live
    # Live now equals tracked, and the base advances to tracked too.
    assert base_store.read_base(_PROFILE, _FILE_ID) == live.encode("utf-8")


def test_wizard_edit_splices_edited_lines(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scripted edit: the edited region lands in the live file."""
    config = _seed_conflict(repo)
    _script_wizard(monkeypatch, choose=lambda c: (b"two-EDITED\n", False))
    result = _install(config)
    assert result.exit_code == 0, result.output
    live = _live_path().read_text(encoding="utf-8")
    assert "two-EDITED" in live
    assert "two-LIVE" not in live
    assert "two-TRACKED" not in live


def test_wizard_skip_keeps_live_and_defers_base(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scripted skip: live kept, base NOT advanced — conflict re-detects.

    An interactive skip (the wizard was offered) keeps live and does NOT gate
    the exit code — only a NON-interactive deferral gates.
    """
    config = _seed_conflict(repo)
    base_before = base_store.read_base(_PROFILE, _FILE_ID)
    _script_wizard(monkeypatch, choose=lambda c: (c.ours, True))
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "two-LIVE" in _live_path().read_text(encoding="utf-8")
    # Any-skip-defers: base stays put.
    assert base_store.read_base(_PROFILE, _FILE_ID) == base_before


def test_auto_use_tracked_does_not_invoke_wizard(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under --auto=use-tracked the wizard is never built/invoked (auto wins)."""
    config = _seed_conflict(repo)
    seen = _script_wizard(monkeypatch, choose=lambda c: (c.ours, False))
    result = _install(config, extra=["--auto=use-tracked"])
    assert result.exit_code == 0, result.output
    # The wizard never saw a conflict — the auto path resolved it.
    assert seen == []
    live = _live_path().read_text(encoding="utf-8")
    assert "two-TRACKED" in live
    assert base_store.read_base(_PROFILE, _FILE_ID) == live.encode("utf-8")


def test_bare_noninteractive_install_warns_and_defers(repo: Path) -> None:
    """No wizard (bare, non-tty CliRunner): a deferred conflict gates non-zero.

    Under the unified reconcile model a non-interactive conflict DEFERS (keeps
    live, base not advanced) and the install gates a non-zero exit so CI / cron
    fails loudly on the unresolved change.
    """
    config = _seed_conflict(repo)
    base_before = base_store.read_base(_PROFILE, _FILE_ID)
    result = _install(config)
    assert result.exit_code != 0, result.output
    assert "two-LIVE" in _live_path().read_text(encoding="utf-8")
    assert "conflict" in result.output.lower()
    assert base_store.read_base(_PROFILE, _FILE_ID) == base_before
