"""CLI-level tests for ``install`` / ``compare`` section reconciliation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from my_setup.cli import app
from my_setup.sections import (
    detect_legacy_markers,
    extract_marker_hashes,
    hash_sections,
)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _make_section_text(
    name: str,
    semantics: str,
    body: str,
    embed_hash: str | None,
) -> str:
    """Build a tiny single-section markdown file."""
    hash_segment = f" hash={embed_hash}" if embed_hash is not None else ""
    return (
        "preamble\n"
        f"<!-- my-setup:user-section start {semantics} {name} -->\n"
        f"{body}"
        f"<!-- my-setup:user-section end {semantics} {name}{hash_segment} -->\n"
        "epilogue\n"
    )


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Build a one-dotfile profile (markdown with preserve_user_sections).

    Returns a dict with ``cfg``, ``src``, ``dst`` so each test can pre-seed
    or read those paths.
    """
    src = tmp_path / "tracked" / "section.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    dst = tmp_path / "live" / "section.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "my_setup.yaml"
    cfg.write_text(
        "version: 1\n"
        "dotfiles:\n"
        "  d:\n"
        "    src: section.md\n"
        f"    dst: {dst}\n"
        "    preserve_user_sections: true\n"
        "profiles:\n"
        "  p:\n"
        "    dotfiles: [d]\n",
        encoding="utf-8",
    )

    # Stub out side effects so the test doesn't write transition state.
    monkeypatch.setattr("my_setup.vscode_extensions.resolve_binary", lambda _: None)
    monkeypatch.setattr("my_setup.transitions.ensure_state_dir_writable", lambda: None)
    monkeypatch.setattr(
        "my_setup.transitions.write_transition", lambda *a, **kw: tmp_path / "fake"
    )

    return {"cfg": cfg, "src": src, "dst": dst}


# ---------------------------------------------------------------------------
# auto modes
# ---------------------------------------------------------------------------


def test_install_auto_use_tracked_deploys_tracked_body(
    fixture: dict[str, Path],
) -> None:
    """--auto=use-tracked overwrites the live shared section with tracked body."""
    live_body = "rule A\n"
    tracked_body = "rule A\nrule B (new)\n"
    # tracked's embedded hash = live's body (last-known baseline) → PENDING_TRACKED
    fixture["src"].write_text(
        _make_section_text("workflow", "shared", tracked_body, _sha256(live_body))
    )
    fixture["dst"].write_text(
        _make_section_text("workflow", "shared", live_body, _sha256(live_body))
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={fixture['cfg']}",
            "--auto=use-tracked",
        ],
    )
    assert result.exit_code == 0, result.output
    live_post = fixture["dst"].read_text()
    assert "rule B (new)" in live_post
    # Hash maintenance invariant: embedded hashes match body content.
    assert {k: v for k, v in extract_marker_hashes(live_post).items() if v} == (
        hash_sections(live_post)
    )


def test_install_auto_keep_live_silences_warning(
    fixture: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """--auto=keep-live keeps live and does NOT emit the bare-install warning."""
    live_body = "rule A\n"
    tracked_body = "rule A\nrule B (new)\n"
    fixture["src"].write_text(
        _make_section_text("workflow", "shared", tracked_body, _sha256(live_body))
    )
    fixture["dst"].write_text(
        _make_section_text("workflow", "shared", live_body, _sha256(live_body))
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={fixture['cfg']}",
            "--auto=keep-live",
        ],
    )
    assert result.exit_code == 0, result.output
    live_post = fixture["dst"].read_text()
    # Live body preserved.
    assert "rule B (new)" not in live_post
    assert "rule A\n" in live_post
    # Hash rewritten to match live body.
    assert extract_marker_hashes(live_post) == {"workflow": _sha256(live_body)}
    # No yellow drift warning — the install output mentions the file but
    # not the aggregate "shared sections drifted" line.
    combined = result.output
    assert "shared section" not in combined


def test_install_bare_warns_on_shared_pending_tracked(
    fixture: dict[str, Path],
) -> None:
    """Bare install with shared PENDING_TRACKED drift surfaces a warning."""
    live_body = "rule A\n"
    tracked_body = "rule A\nrule B\n"
    fixture["src"].write_text(
        _make_section_text("workflow", "shared", tracked_body, _sha256(live_body))
    )
    fixture["dst"].write_text(
        _make_section_text("workflow", "shared", live_body, _sha256(live_body))
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["install", "--profile=p", f"--config={fixture['cfg']}"]
    )
    assert result.exit_code == 0, result.output
    combined = result.output
    assert "shared section" in combined
    assert "pending tracked update" in combined
    # Live preserved on bare install.
    assert "rule B" not in fixture["dst"].read_text()


def test_install_bare_no_warn_when_host_local_drift_only(
    fixture: dict[str, Path],
) -> None:
    """host-local drift never surfaces a warning."""
    body = "host-local\n"
    fixture["src"].write_text(
        _make_section_text("notes", "host-local", body, _sha256(body))
    )
    fixture["dst"].write_text(
        _make_section_text("notes", "host-local", body + "edits\n", _sha256(body))
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["install", "--profile=p", f"--config={fixture['cfg']}"]
    )
    assert result.exit_code == 0, result.output
    assert "shared section" not in result.output


def test_install_hash_maintained_on_host_local(fixture: dict[str, Path]) -> None:
    """Even for host-local sections, install rewrites the end-marker hash."""
    body = "host body\n"
    # Post-9ln: tracked must ship with a stamped end-marker hash.
    fixture["src"].write_text(
        _make_section_text("notes", "host-local", body, _sha256(body))
    )
    # Live with stale embedded hash.
    fixture["dst"].write_text(
        _make_section_text(
            "notes",
            "host-local",
            "live-edited body\n",
            "deadbeef" * 8,
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["install", "--profile=p", f"--config={fixture['cfg']}"]
    )
    assert result.exit_code == 0, result.output
    live_post = fixture["dst"].read_text()
    # Body preserved.
    assert "live-edited body\n" in live_post
    # Hash now matches the live body.
    assert extract_marker_hashes(live_post) == {"notes": _sha256("live-edited body\n")}


def test_install_first_run_no_warning_when_no_live(fixture: dict[str, Path]) -> None:
    """First install (no live file yet) does NOT warn about shared drift —
    classify_section_drift only runs when both sides exist."""
    body = "rule A\n"
    fixture["src"].write_text(
        _make_section_text("workflow", "shared", body, _sha256(body))
    )
    # No live file exists.

    runner = CliRunner()
    result = runner.invoke(
        app, ["install", "--profile=p", f"--config={fixture['cfg']}"]
    )
    assert result.exit_code == 0, result.output
    assert "shared section" not in result.output
    # First-install live = tracked content + maintained hash.
    live_post = fixture["dst"].read_text()
    assert "rule A\n" in live_post
    assert extract_marker_hashes(live_post) == {"workflow": _sha256("rule A\n")}


def test_install_idempotent_second_run_with_hash_alignment(
    fixture: dict[str, Path],
) -> None:
    """Run install twice; second run is a NOOP (hash-aligned live already)."""
    body = "rule A\n"
    fixture["src"].write_text(
        _make_section_text("workflow", "shared", body, _sha256(body))
    )
    runner = CliRunner()
    result1 = runner.invoke(
        app, ["install", "--profile=p", f"--config={fixture['cfg']}"]
    )
    assert result1.exit_code == 0, result1.output
    live_after_first = fixture["dst"].read_text()

    result2 = runner.invoke(
        app, ["install", "--profile=p", f"--config={fixture['cfg']}"]
    )
    assert result2.exit_code == 0, result2.output
    assert fixture["dst"].read_text() == live_after_first


# ---------------------------------------------------------------------------
# compare --reconcile-user-sections
# ---------------------------------------------------------------------------


def test_compare_reconcile_dry_run_lists_shared_drift(
    fixture: dict[str, Path],
) -> None:
    live_body = "rule A\n"
    tracked_body = "rule A\nrule B\n"
    fixture["src"].write_text(
        _make_section_text("workflow", "shared", tracked_body, _sha256(live_body))
    )
    fixture["dst"].write_text(
        _make_section_text("workflow", "shared", live_body, _sha256(live_body))
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "compare",
            "--profile=p",
            f"--config={fixture['cfg']}",
            "--reconcile-user-sections",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "pending tracked update" in result.output
    assert "workflow" in result.output


def test_compare_reconcile_dry_run_does_not_mutate_live(
    fixture: dict[str, Path],
) -> None:
    """compare --reconcile-user-sections is read-only."""
    live_body = "rule A\n"
    tracked_body = "rule A\nrule B\n"
    fixture["src"].write_text(
        _make_section_text("workflow", "shared", tracked_body, _sha256(live_body))
    )
    fixture["dst"].write_text(
        _make_section_text("workflow", "shared", live_body, _sha256(live_body))
    )
    live_before = fixture["dst"].read_bytes()
    runner = CliRunner()
    runner.invoke(
        app,
        [
            "compare",
            "--profile=p",
            f"--config={fixture['cfg']}",
            "--reconcile-user-sections",
        ],
    )
    assert fixture["dst"].read_bytes() == live_before


def test_compare_reconcile_dry_run_silent_when_no_drift(
    fixture: dict[str, Path],
) -> None:
    body = "rule A\n"
    fixture["src"].write_text(
        _make_section_text("workflow", "shared", body, _sha256(body))
    )
    fixture["dst"].write_text(
        _make_section_text("workflow", "shared", body, _sha256(body))
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "compare",
            "--profile=p",
            f"--config={fixture['cfg']}",
            "--reconcile-user-sections",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "no shared user-section drift" in result.output


# ---------------------------------------------------------------------------
# Profile-extend interaction
# ---------------------------------------------------------------------------


def test_install_reconciles_profile_extend_resolved_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a profile extends another, the wizard walks the post-extend set."""
    base_src = tmp_path / "tracked" / "base.md"
    base_dst = tmp_path / "live" / "base.md"
    child_src = tmp_path / "tracked" / "child.md"
    child_dst = tmp_path / "live" / "child.md"
    base_src.parent.mkdir(parents=True, exist_ok=True)
    base_dst.parent.mkdir(parents=True, exist_ok=True)

    base_src.write_text(
        _make_section_text("base-section", "shared", "base v2\n", _sha256("base v1\n"))
    )
    base_dst.write_text(
        _make_section_text("base-section", "shared", "base v1\n", _sha256("base v1\n"))
    )
    child_src.write_text(
        _make_section_text(
            "child-section", "shared", "child v2\n", _sha256("child v1\n")
        )
    )
    child_dst.write_text(
        _make_section_text(
            "child-section", "shared", "child v1\n", _sha256("child v1\n")
        )
    )

    cfg = tmp_path / "my_setup.yaml"
    cfg.write_text(
        "version: 1\n"
        "dotfiles:\n"
        "  base:\n"
        f"    src: base.md\n    dst: {base_dst}\n"
        "    preserve_user_sections: true\n"
        "  child:\n"
        f"    src: child.md\n    dst: {child_dst}\n"
        "    preserve_user_sections: true\n"
        "profiles:\n"
        "  base-p:\n"
        "    dotfiles: [base]\n"
        "  child-p:\n"
        "    extends: base-p\n"
        "    dotfiles: [child]\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("my_setup.vscode_extensions.resolve_binary", lambda _: None)
    monkeypatch.setattr("my_setup.transitions.ensure_state_dir_writable", lambda: None)
    monkeypatch.setattr(
        "my_setup.transitions.write_transition", lambda *a, **kw: tmp_path / "fake"
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["install", "--profile=child-p", f"--config={cfg}", "--auto=use-tracked"],
    )
    assert result.exit_code == 0, result.output
    # Both base and child sections deployed (extends resolved both).
    assert "base v2\n" in base_dst.read_text()
    assert "child v2\n" in child_dst.read_text()


# ---------------------------------------------------------------------------
# Transition / revert symmetry
# ---------------------------------------------------------------------------


def test_install_with_use_tracked_records_transition(
    fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install that overwrites a live section records a transition so
    revert can undo it (records the pre/post file content via the
    normal install transition path)."""
    calls: list[Any] = []

    def fake_write(*a: Any, **kw: Any) -> Path:
        calls.append((a, kw))
        return Path("/tmp/fake")

    monkeypatch.setattr("my_setup.transitions.write_transition", fake_write)

    live_body = "rule A\n"
    tracked_body = "rule A\nrule B\n"
    fixture["src"].write_text(
        _make_section_text("workflow", "shared", tracked_body, _sha256(live_body))
    )
    fixture["dst"].write_text(
        _make_section_text("workflow", "shared", live_body, _sha256(live_body))
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={fixture['cfg']}",
            "--auto=use-tracked",
        ],
    )
    assert result.exit_code == 0, result.output
    # write_transition got called — transition recorded.
    assert calls, "install must record a transition for revert to undo"


# ---------------------------------------------------------------------------
# dotfiles-9ln — legacy live migration via install; compare/sync refuse
# ---------------------------------------------------------------------------


def _legacy_live_section_text(name: str, body: str) -> str:
    """Build a pre-9by live file shape: untagged markers, no hash segment."""
    return (
        "preamble\n"
        f"<!-- my-setup:user-section start {name} -->\n"
        f"{body}"
        f"<!-- my-setup:user-section end {name} -->\n"
        "epilogue\n"
    )


def test_compare_refuses_legacy_live_with_actionable_error(
    fixture: dict[str, Path],
) -> None:
    """``compare`` on a legacy live file exits 1 with the actionable error
    that points the user at ``my-setup install`` instead of leaking the
    raw ``MarkerError: line N: missing required keyword``."""
    body = "rule A\n"
    fixture["src"].write_text(
        _make_section_text("workflow", "shared", body, _sha256(body))
    )
    fixture["dst"].write_text(_legacy_live_section_text("workflow", body))

    runner = CliRunner()
    result = runner.invoke(
        app, ["compare", "--profile=p", f"--config={fixture['cfg']}"]
    )
    assert result.exit_code == 1, result.output
    # Either captured by typer's CliRunner as result.exception or printed.
    combined = result.output + (str(result.exception) if result.exception else "")
    assert "legacy" in combined.lower()
    assert "my-setup install" in combined


def test_sync_refuses_legacy_live_with_actionable_error(
    fixture: dict[str, Path],
) -> None:
    """``sync`` on a legacy live file exits 1 with the actionable error."""
    body = "rule A\n"
    fixture["src"].write_text(
        _make_section_text("workflow", "shared", body, _sha256(body))
    )
    fixture["dst"].write_text(_legacy_live_section_text("workflow", body))

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--profile=p", f"--config={fixture['cfg']}"])
    assert result.exit_code == 1, result.output
    combined = result.output + (str(result.exception) if result.exception else "")
    assert "legacy" in combined.lower()
    assert "my-setup install" in combined


def test_merge_refuses_legacy_live_with_actionable_error(
    fixture: dict[str, Path],
) -> None:
    """``merge`` on a legacy live file exits 1 with the actionable error
    that points the user at ``my-setup install``.

    Without the ``_refuse_legacy_live_markers`` guard, ``merge`` would
    silently proceed into ``compare_profile`` (which now passes
    ``allow_legacy=True`` on live reads to support install's pre-flight)
    instead of surfacing the actionable error before any drift work.
    """
    body = "rule A\n"
    fixture["src"].write_text(
        _make_section_text("workflow", "shared", body, _sha256(body))
    )
    fixture["dst"].write_text(_legacy_live_section_text("workflow", body))

    runner = CliRunner()
    result = runner.invoke(app, ["merge", "--profile=p", f"--config={fixture['cfg']}"])
    assert result.exit_code == 1, result.output
    combined = result.output + (str(result.exception) if result.exception else "")
    assert "legacy" in combined.lower()
    assert "my-setup install" in combined


def test_install_succeeds_on_legacy_live_and_migrates(
    fixture: dict[str, Path],
) -> None:
    """``install`` on a legacy live file exits 0; the resulting live file
    is fully strict-clean (proper semantics keyword + hash segment)."""
    body = "rule A\n"
    fixture["src"].write_text(
        _make_section_text("workflow", "shared", body, _sha256(body))
    )
    fixture["dst"].write_text(_legacy_live_section_text("workflow", body))

    runner = CliRunner()
    result = runner.invoke(
        app, ["install", "--profile=p", f"--config={fixture['cfg']}"]
    )
    assert result.exit_code == 0, result.output
    live_post = fixture["dst"].read_text()
    # No legacy markers remain.
    assert detect_legacy_markers(live_post) is False
    # End marker carries the semantics keyword and a 64-hex hash.
    assert "end shared workflow hash=" in live_post
    # Body preserved.
    assert "rule A\n" in live_post
    # The hash matches the body actually written.
    assert extract_marker_hashes(live_post) == hash_sections(live_post)
