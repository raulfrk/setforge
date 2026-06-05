"""Integration tests for disposition-gated sync capture.

Drive the real ``setforge sync`` CLI against a temp config repo with a
sandboxed ``$HOME`` + ``$SETFORGE_STATE_DIR`` and assert how the
per-tracked-file capture loop treats each :class:`Disposition`:

1. ``shared`` — live edits captured back to tracked; the stored base
   (:mod:`setforge.base_store`) re-baselines to the converged content.
2. ``forked`` — live edits never captured; tracked + base untouched.
3. ``pinned`` — live edits never captured; tracked + base untouched.
4. ``None`` (no disposition, with ``preserve_user_keys``) — today's
   capture path, byte-for-byte unchanged (regression).
5. Ordering/consistency — after a shared capture, a subsequent install of
   the same file is a clean no-op (base == tracked == live ⇒ zero drift).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store
from setforge.cli import app

_PROFILE = "test-disposition"
_FILE_ID = "shared_text"


def _write_disposition_config(repo: Path, *, disposition: str = "shared") -> Path:
    """Write a setforge.yaml whose ``shared_text`` file carries ``disposition``.

    An inert ``anchor`` tracked file keeps the profile's tracked_files
    list non-empty regardless of the disposition under test.
    """
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  shared_text:\n"
        "    src: text/note.txt\n"
        "    dst: ~/.setforge_disp/note.txt\n"
        f"    disposition: {disposition}\n"
        "  anchor:\n"
        "    src: text/anchor.txt\n"
        "    dst: ~/.setforge_disp/anchor.txt\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - shared_text\n"
        "      - anchor\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, body: str) -> Path:
    """Write tracked source bodies; return the ``shared_text`` src path."""
    src = repo / "tracked" / "text" / "note.txt"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(body, encoding="utf-8")
    (src.parent / "anchor.txt").write_text("anchor\n", encoding="utf-8")
    return src


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
    return Path.home() / ".setforge_disp" / "note.txt"


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


def _sync(config: Path, *, extra: list[str] | None = None) -> Result:
    """Run ``setforge sync`` against ``config``; return the CliRunner result."""
    args = [
        "sync",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-transition",
        "--auto=use-live",
        "--yes",
    ]
    if extra:
        args.extend(extra)
    return CliRunner().invoke(app, args)


def test_shared_captures_and_rebaselines(repo: Path) -> None:
    """shared: live edits captured to tracked; base re-baselines to converged."""
    tracked_body = "line1\nline2\n"
    src = _write_tracked(repo, tracked_body)
    config = _write_disposition_config(repo, disposition="shared")
    assert _install(config).exit_code == 0
    # Base seeded == tracked at first install.
    assert base_store.read_base(_PROFILE, _FILE_ID) == tracked_body.encode("utf-8")

    live_body = "line1\nline2\nline3-LIVE\n"
    _live_path().write_text(live_body, encoding="utf-8")

    result = _sync(config)
    assert result.exit_code == 0, result.output
    # Tracked now equals the live content verbatim.
    assert src.read_text(encoding="utf-8") == live_body
    # Base re-baselined to the captured bytes (converged state).
    assert base_store.read_base(_PROFILE, _FILE_ID) == live_body.encode("utf-8")


def test_forked_skips_capture_and_leaves_base(repo: Path) -> None:
    """forked: live edits never captured; tracked unchanged, base untouched."""
    tracked_body = "tracked-A\ntracked-B\n"
    src = _write_tracked(repo, tracked_body)
    config = _write_disposition_config(repo, disposition="forked")
    assert _install(config).exit_code == 0
    base_before = base_store.read_base(_PROFILE, _FILE_ID)

    _live_path().write_text("DIVERGED-LIVE\n", encoding="utf-8")

    result = _sync(config)
    assert result.exit_code == 0, result.output
    # Tracked stays exactly as authored — live edits not captured.
    assert src.read_text(encoding="utf-8") == tracked_body
    # Base untouched by sync.
    assert base_store.read_base(_PROFILE, _FILE_ID) == base_before


def test_pinned_skips_capture_and_leaves_base(repo: Path) -> None:
    """pinned: live edits never captured; tracked unchanged, base untouched."""
    tracked_body = "pinned-tracked\n"
    src = _write_tracked(repo, tracked_body)
    config = _write_disposition_config(repo, disposition="pinned")
    # Pre-seed a live file install must not clobber (pinned ⇒ live wins).
    live = _live_path()
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("LIVE-ONLY\n", encoding="utf-8")
    assert _install(config).exit_code == 0
    base_before = base_store.read_base(_PROFILE, _FILE_ID)

    live.write_text("LIVE-EDITED-AGAIN\n", encoding="utf-8")

    result = _sync(config)
    assert result.exit_code == 0, result.output
    # Tracked unchanged.
    assert src.read_text(encoding="utf-8") == tracked_body
    # Base untouched (pinned never gets one).
    assert base_store.read_base(_PROFILE, _FILE_ID) == base_before


def test_none_disposition_regression_preserve_user_keys(
    repo: Path,
) -> None:
    """None disposition + preserve_user_keys: legacy capture path unchanged.

    The host-local key is stripped from the captured tracked source and
    the non-preserve key is absorbed — exactly the pre-disposition
    behavior.
    """
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: yaml/settings.yaml\n"
        "    dst: ~/.setforge_disp/settings.yaml\n"
        "    preserve_user_keys:\n"
        "      - host_key\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - settings\n",
        encoding="utf-8",
    )
    src = repo / "tracked" / "yaml" / "settings.yaml"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("shared_key: tracked\n", encoding="utf-8")

    assert _install(config).exit_code == 0
    live = Path.home() / ".setforge_disp" / "settings.yaml"
    # Live now carries the tracked shared_key; user adds a host-local key
    # and edits the shared one.
    live.write_text("shared_key: live\nhost_key: secret\n", encoding="utf-8")

    result = _sync(config)
    assert result.exit_code == 0, result.output
    captured = src.read_text(encoding="utf-8")
    # Shared key absorbed; host-local preserve key stripped from tracked.
    assert "shared_key: live" in captured
    assert "host_key" not in captured
    # A None-disposition file never gets a stored base.
    assert base_store.read_base(_PROFILE, "settings") is None


def test_shared_structural_span_excluded_from_drift_absorption(repo: Path) -> None:
    """B-S5: a structural span path is excluded from ``sync`` drift absorption.

    A ``shared`` yaml file carries a pinned span (``pinned_key``) and a forked
    span (``forked_key``). After ``install`` seeds live==tracked, the user edits
    BOTH span values live. ``sync --auto=use-live`` (the drift-absorption path)
    must keep TRACKED's value at both span paths (Invariant I2 totality), while
    a non-span shared key absorbs the live edit normally.
    """
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: yaml/settings.yaml\n"
        "    dst: ~/.setforge_disp/settings.yaml\n"
        "    disposition: shared\n"
        "    spans:\n"
        "      - anchor: pinned_key\n"
        "        kind: pinned\n"
        "      - anchor: forked_key\n"
        "        kind: forked\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - settings\n",
        encoding="utf-8",
    )
    src = repo / "tracked" / "yaml" / "settings.yaml"
    src.parent.mkdir(parents=True, exist_ok=True)
    tracked_body = "pinned_key: T-pin\nforked_key: T-fork\nshared_key: T-shared\n"
    src.write_text(tracked_body, encoding="utf-8")

    assert _install(config).exit_code == 0
    live = Path.home() / ".setforge_disp" / "settings.yaml"
    # User edits all three keys live.
    live.write_text(
        "pinned_key: L-pin\nforked_key: L-fork\nshared_key: L-shared\n",
        encoding="utf-8",
    )

    result = _sync(config)
    assert result.exit_code == 0, result.output
    captured = src.read_text(encoding="utf-8")
    # Both span paths keep TRACKED's value (excluded from capture).
    assert "pinned_key: T-pin" in captured
    assert "forked_key: T-fork" in captured
    # The non-span shared key absorbs the live edit.
    assert "shared_key: L-shared" in captured


def test_shared_capture_then_install_is_noop(repo: Path) -> None:
    """After a shared capture, a re-install is a clean no-op (zero drift)."""
    src = _write_tracked(repo, "a\nb\n")
    config = _write_disposition_config(repo, disposition="shared")
    assert _install(config).exit_code == 0

    live_body = "a\nb\nc-LIVE\n"
    _live_path().write_text(live_body, encoding="utf-8")
    assert _sync(config).exit_code == 0

    # base == tracked == live now. A re-install must produce no conflict
    # and leave live exactly as-is.
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "conflict" not in result.output.lower()
    assert _live_path().read_text(encoding="utf-8") == live_body
    assert src.read_text(encoding="utf-8") == live_body
    assert base_store.read_base(_PROFILE, _FILE_ID) == live_body.encode("utf-8")
