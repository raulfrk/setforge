"""Integration tests for the scalar-base lifecycle in the install loop.

Drive the real ``setforge install`` CLI against a temp config repo with a
sandboxed ``$HOME`` + ``$SETFORGE_STATE_DIR`` and assert on the per-host
scalar base (:mod:`setforge.scalar_base_store`) it seeds, advances, defers,
and prunes for a ``preserve_user_keys`` (non-disposition) tracked file.

The cases mirror Task 3's acceptance grid:

1. First install seeds a scalar base for every preserve path.
2. Editing tracked for a key the live did NOT touch + re-install: live now
   gets the tracked value (upstream propagates via the 3-way merge).
3. Editing a live key (tracked unchanged): that key is preserved.
4. Same-key conflict under a bare install: keep-live + warn + base NOT
   advanced.
5. The same conflict under ``--auto=use-tracked``: takes tracked.
6. Dropping a preserve path from the config prunes its stored base.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import scalar_base_store
from setforge.cli import app
from setforge.scalar_merge import ABSENT

_PROFILE = "test-scalar"
_FILE_ID = "settings"


def _write_config(repo: Path, *, keys: tuple[str, ...] = ("a", "b")) -> Path:
    """Write a setforge.yaml whose ``settings`` file preserves ``keys``."""
    preserve_lines = "".join(f"      - {k}\n" for k in keys)
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: cfg/settings.yaml\n"
        "    dst: ~/.setforge_scalar/settings.yaml\n"
        "    preserve_user_keys:\n"
        f"{preserve_lines}"
        "  anchor:\n"
        "    src: cfg/anchor.txt\n"
        "    dst: ~/.setforge_scalar/anchor.txt\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - settings\n"
        "      - anchor\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, body: str) -> None:
    """Write the tracked source bodies for ``settings`` and ``anchor``."""
    src = repo / "tracked" / "cfg" / "settings.yaml"
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
    return Path.home() / ".setforge_scalar" / "settings.yaml"


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


def test_first_install_seeds_scalar_base(repo: Path) -> None:
    """First install: a scalar base is seeded for each preserve path."""
    _write_tracked(repo, "a: 1\nb: 2\n")
    config = _write_config(repo)

    result = _install(config)
    assert result.exit_code == 0, result.output
    # First run: no live yet, so dst is created from tracked verbatim and
    # each preserve path's base is seeded to the deployed (tracked) value.
    assert scalar_base_store.get_base(_PROFILE, _FILE_ID, "a") == 1
    assert scalar_base_store.get_base(_PROFILE, _FILE_ID, "b") == 2


def test_upstream_propagates_for_untouched_key(repo: Path) -> None:
    """Tracked edits a key the live never touched → live gets the tracked value."""
    _write_tracked(repo, "a: 1\nb: 2\n")
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    # Live exists now == tracked. User does NOT touch `a`.
    # Tracked moves `a` upstream to 9.
    _write_tracked(repo, "a: 9\nb: 2\n")

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "a: 9" in _live_path().read_text(encoding="utf-8")
    assert scalar_base_store.get_base(_PROFILE, _FILE_ID, "a") == 9


def test_live_edit_preserved(repo: Path) -> None:
    """Live edits a key (tracked unchanged) → that key is preserved."""
    _write_tracked(repo, "a: 1\nb: 2\n")
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    # User edits `a` live; tracked stays at 1.
    _live_path().write_text("a: 99\nb: 2\n", encoding="utf-8")

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "a: 99" in _live_path().read_text(encoding="utf-8")
    assert scalar_base_store.get_base(_PROFILE, _FILE_ID, "a") == 99


def test_conflict_bare_keeps_live_and_defers_base(repo: Path) -> None:
    """Same-key divergence, bare install: live kept, warned, base NOT advanced."""
    _write_tracked(repo, "a: 1\nb: 2\n")
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    base_before = scalar_base_store.get_base(_PROFILE, _FILE_ID, "a")

    # Both sides move `a` away from base (1) to DIFFERENT values → conflict.
    _live_path().write_text("a: 7\nb: 2\n", encoding="utf-8")
    _write_tracked(repo, "a: 8\nb: 2\n")

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "a: 7" in _live_path().read_text(encoding="utf-8")  # live kept
    assert "conflict" in result.output.lower()
    # Base NOT advanced — still the previous base so divergence re-surfaces.
    assert scalar_base_store.get_base(_PROFILE, _FILE_ID, "a") == base_before


def test_conflict_use_tracked_takes_tracked(repo: Path) -> None:
    """Same conflict under --auto=use-tracked: live takes tracked, base advances."""
    _write_tracked(repo, "a: 1\nb: 2\n")
    config = _write_config(repo)
    assert _install(config).exit_code == 0

    _live_path().write_text("a: 7\nb: 2\n", encoding="utf-8")
    _write_tracked(repo, "a: 8\nb: 2\n")

    result = _install(config, extra=["--auto=use-tracked"])
    assert result.exit_code == 0, result.output
    assert "a: 8" in _live_path().read_text(encoding="utf-8")
    assert scalar_base_store.get_base(_PROFILE, _FILE_ID, "a") == 8


def test_prune_drops_removed_path_base(repo: Path) -> None:
    """Dropping a preserve path from the config prunes its stored base."""
    _write_tracked(repo, "a: 1\nb: 2\n")
    config = _write_config(repo, keys=("a", "b"))
    assert _install(config).exit_code == 0
    assert scalar_base_store.get_base(_PROFILE, _FILE_ID, "b") == 2

    # Re-write the config preserving ONLY `a`; `b`'s base must be pruned.
    config = _write_config(repo, keys=("a",))
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert scalar_base_store.get_base(_PROFILE, _FILE_ID, "a") == 1
    assert scalar_base_store.get_base(_PROFILE, _FILE_ID, "b") is ABSENT


def test_second_install_no_edits_is_noop(repo: Path) -> None:
    """Self-install idempotency: re-installing unchanged files changes nothing.

    First install writes tracked verbatim and seeds the scalar bases.
    A second install with NO edits between the two must produce an identical
    live file (NOOP action) and leave every scalar base value unchanged.
    This guards the "install twice = zero drift" property.
    """
    _write_tracked(repo, "a: 1\nb: 2\n")
    config = _write_config(repo)
    assert _install(config).exit_code == 0

    # Capture state after first install.
    live_after_first = _live_path().read_text(encoding="utf-8")
    base_a = scalar_base_store.get_base(_PROFILE, _FILE_ID, "a")
    base_b = scalar_base_store.get_base(_PROFILE, _FILE_ID, "b")

    # Second install: no edits to tracked or live.
    result = _install(config)
    assert result.exit_code == 0, result.output

    # Live file must be unchanged (NOOP).
    assert _live_path().read_text(encoding="utf-8") == live_after_first
    # Scalar bases must be unchanged.
    assert scalar_base_store.get_base(_PROFILE, _FILE_ID, "a") == base_a
    assert scalar_base_store.get_base(_PROFILE, _FILE_ID, "b") == base_b


def test_batched_set_bases_not_per_path_set_base(repo: Path) -> None:
    """Scalar base advancement uses ONE batched set_bases call per file.

    The install loop must call ``scalar_base_store.set_bases`` ONCE per file
    (with all advancing paths in one dict), never per-path ``set_base`` calls
    in a loop. This guards the single-write contract documented in
    :mod:`setforge.scalar_base_store`.
    """
    _write_tracked(repo, "a: 1\nb: 2\n")
    config = _write_config(repo)

    set_bases_calls: list[tuple[str, str, dict[str, object]]] = []

    original_set_bases = scalar_base_store.set_bases

    def _spy_set_bases(profile: str, file_id: str, values: dict[str, object]) -> None:
        set_bases_calls.append((profile, file_id, values))
        original_set_bases(profile, file_id, values)

    with patch.object(scalar_base_store, "set_bases", side_effect=_spy_set_bases):
        result = _install(config)

    assert result.exit_code == 0, result.output
    # The settings file (file_id "settings") must have had set_bases called
    # exactly ONCE, with both `a` and `b` in the same dict.
    settings_calls = [c for c in set_bases_calls if c[1] == _FILE_ID]
    assert len(settings_calls) == 1, (
        f"expected 1 set_bases call for {_FILE_ID!r}, got {len(settings_calls)}"
    )
    _, _, values = settings_calls[0]
    assert set(values.keys()) == {"a", "b"}
