"""Tests for the A7 ``setforge inspect`` command (3-way viewer + hunk index)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app

_BASE = b"line one\nline two\nline three\n"
_LIVE = b"line one\nlive edit\nline three\n"
_UPSTREAM = b"line one\nupstream edit\nline three\n"


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _config(dst: Path) -> str:
    return (
        "version: 1\ntracked_files:\n  CLAUDE.md:\n    src: CLAUDE.md\n"
        f"    dst: {dst}\n"
        "profiles:\n  p:\n    tracked_files: [CLAUDE.md]\n"
    )


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    base: bytes | None = _BASE,
    live: bytes = _LIVE,
    tracked: bytes = _UPSTREAM,
    hunks: list[dict[str, object]] | None = None,
) -> tuple[Path, Path]:
    """Wire a tracked file with a live file, a tracked (upstream) src, and a
    recorded reconcile base/local. ``base=None`` skips the base recording (the
    base-absent case). ``hunks`` records per-hunk index classes into the store
    (the shape the staging layer will produce). Returns ``(cfg_path, dst)``."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge import locking
    from setforge.reconcile import store
    from setforge.reconcile.types import file_id

    repo = tmp_path / "repo"
    src = repo / "tracked" / "CLAUDE.md"
    dst = tmp_path / "live" / "CLAUDE.md"
    _write(src, tracked)
    _write(dst, live)
    if base is not None:
        with locking.profile_lock("p"):
            store.record("p", file_id("CLAUDE.md"), base=base, local=live, hunks=hunks)
    cfg_path = repo / "setforge.yaml"
    cfg_path.write_text(_config(dst), encoding="utf-8")
    return cfg_path, dst


def test_inspect_human_renders_three_panes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, _ = _setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        app, ["inspect", "CLAUDE.md", "--profile=p", f"--config={cfg_path}"]
    )
    assert result.exit_code == 0, result.output
    # base / live / merge-preview content all surface in the human view.
    assert "live edit" in result.output
    assert "upstream edit" in result.output


def test_inspect_json_envelope_is_ansi_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, _ = _setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        app,
        [
            "--format=json",
            "inspect",
            "CLAUDE.md",
            "--profile=p",
            f"--config={cfg_path}",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "\x1b[" not in result.stdout  # no ANSI escape into a JSON pipe
    payload = json.loads(result.stdout)
    assert payload["command"] == "inspect"
    data = payload["data"]
    assert data["base_present"] is True
    assert set(data["panes"]) == {"base", "live", "merge"}
    assert "live edit" in data["panes"]["live"]
    assert "upstream edit" in data["panes"]["merge"]  # theirs applied cleanly
    assert set(data["index"]) == {"shared", "kept_local", "conflict"}
    assert data.get("errors", []) == []


def test_inspect_no_base_collapses_to_two_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, _ = _setup(tmp_path, monkeypatch, base=None)
    result = CliRunner().invoke(
        app,
        [
            "--format=json",
            "inspect",
            "CLAUDE.md",
            "--profile=p",
            f"--config={cfg_path}",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["base_present"] is False
    assert data["panes"]["base"] is None  # no base pane in the 2-pane view


def test_inspect_untracked_exits_2_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, _ = _setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        app,
        ["--format=json", "inspect", "nope.md", "--profile=p", f"--config={cfg_path}"],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["command"] == "inspect"
    assert payload["errors"]  # structured error rides the envelope
    assert any("nope.md" in e for e in payload["errors"])


def test_inspect_untracked_human_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, _ = _setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        app, ["inspect", "nope.md", "--profile=p", f"--config={cfg_path}"]
    )
    assert result.exit_code == 2, result.output


def test_inspect_binary_degrades_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload_bytes = b"\x00\x01binary\x00stuff\n"
    cfg_path, _ = _setup(
        tmp_path,
        monkeypatch,
        base=payload_bytes,
        live=payload_bytes,
        tracked=payload_bytes,
    )
    result = CliRunner().invoke(
        app,
        [
            "--format=json",
            "inspect",
            "CLAUDE.md",
            "--profile=p",
            f"--config={cfg_path}",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    # binary degrades to a stat placeholder in the merge pane, never a crash.
    assert "binary" in data["panes"]["merge"].lower()


def test_inspect_non_tty_is_deterministic_stacked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Under CliRunner the output stream is not a tty; the layout must resolve
    # deterministically (stacked) rather than branch on a live terminal width.
    cfg_path, _ = _setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        app, ["inspect", "CLAUDE.md", "--profile=p", f"--config={cfg_path}"]
    )
    assert result.exit_code == 0, result.output
    # both sides present, stacked in document order — upstream after live edit.
    assert result.output.index("live edit") < result.output.index("upstream edit")


def test_inspect_index_shared_kept_local_empty_without_recorded_hunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # shared/kept_local come from the STORE index (FileEntry.hunks), NOT the
    # merge-conflict sides. With no recorded hunks (today's storage layer) they
    # are honestly empty — a clean re-merge here would have wrongly reported a
    # kept-local under the old conflict-side derivation.
    cfg_path, _ = _setup(tmp_path, monkeypatch)  # base/live diverge, no hunks
    result = CliRunner().invoke(
        app,
        [
            "--format=json",
            "inspect",
            "CLAUDE.md",
            "--profile=p",
            f"--config={cfg_path}",
        ],
    )
    assert result.exit_code == 0, result.output
    index = json.loads(result.stdout)["data"]["index"]
    assert index["shared"] == []
    assert index["kept_local"] == []


def test_inspect_index_reflects_recorded_store_classes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise the read_index path: a recorded index with a SHARED + a LOCAL hunk
    # must surface as shared / kept_local (the store classification is the
    # authority, keyed by HunkClass value — not the merge stream).
    hunks: list[dict[str, object]] = [
        {"cls": "shared", "label": "## Shell", "live_hash": "sha256:a", "anchor": "s"},
        {"cls": "local", "label": "## Host", "live_hash": "sha256:b", "anchor": "h"},
    ]
    cfg_path, _ = _setup(tmp_path, monkeypatch, hunks=hunks)
    result = CliRunner().invoke(
        app,
        [
            "--format=json",
            "inspect",
            "CLAUDE.md",
            "--profile=p",
            f"--config={cfg_path}",
        ],
    )
    assert result.exit_code == 0, result.output
    index = json.loads(result.stdout)["data"]["index"]
    assert [r["label"] for r in index["shared"]] == ["## Shell"]
    assert [r["label"] for r in index["kept_local"]] == ["## Host"]
