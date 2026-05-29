"""Tests for the generic merge-wizard orchestrator — :mod:`setforge.wizard`.

These tests target :func:`run_wizard_loop` in isolation. The mechanics it
delegates to (snapshot, prompt, action handlers) are already covered by
``tests/test_merge.py``; this file covers the orchestration seams
(per-item dispatch, transition recording, MANUAL_PENDING break,
trigger-specific pending message).
"""

from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from setforge.transitions import TransitionCommand
from setforge.wizard import (
    ActionResult,
    DriftItem,
    FileFormat,
    run_wizard_loop,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_item(tmp_path: Path, name: str) -> DriftItem:
    """Build a synthetic DriftItem pointing at temp YAML files."""
    src = tmp_path / "tracked" / f"{name}.yaml"
    dst = tmp_path / "live" / f"{name}.yaml"
    _write(src, f"k: tracked_{name}\n")
    _write(dst, f"k: live_{name}\n")
    return DriftItem(
        tracked_file_name=name,
        src_path=src,
        dst_path=dst,
        key_path="k",
        tracked_value=f"tracked_{name}",
        live_value=f"live_{name}",
        file_format=FileFormat.YAML,
    )


def _make_setforge_yaml(tmp_path: Path) -> Path:
    """Write a minimal valid setforge.yaml stub."""
    path = tmp_path / "setforge.yaml"
    path.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  one:\n"
        "    src: one.yaml\n"
        "    dst: /tmp/one.yaml\n"
        "    preserve_user_keys: []\n"
        "  two:\n"
        "    src: two.yaml\n"
        "    dst: /tmp/two.yaml\n"
        "    preserve_user_keys: []\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [one, two]\n",
        encoding="utf-8",
    )
    return path


def test_run_wizard_loop_dispatches_per_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_wizard_loop dispatches the chosen action per item; records one transition."""
    item1 = _make_item(tmp_path, "one")
    item2 = _make_item(tmp_path, "two")
    setforge_yaml = _make_setforge_yaml(tmp_path)

    transition_calls: list[Any] = []

    def _fake_write_transition(*a: Any, **kw: Any) -> Path:
        transition_calls.append(1)
        return Path("/tmp/fake")

    monkeypatch.setattr(
        "setforge.wizard.transitions.write_transition",
        _fake_write_transition,
    )

    console = Console(file=StringIO(), force_terminal=False, no_color=True)
    decisions = run_wizard_loop(
        iter([item1, item2]),
        setforge_yaml_path=setforge_yaml,
        snapshot_base=tmp_path / "snaps",
        console=console,
        auto_accept="k",
        transition_command=TransitionCommand.MERGE,
        profile="p",
        pending_message="unused",
    )

    assert decisions == [
        (item1, ActionResult.KEEP_TRACKED),
        (item2, ActionResult.KEEP_TRACKED),
    ]
    assert len(transition_calls) == 1


def test_run_wizard_loop_empty_writes_no_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No drift items → no transition record, no snapshot base, empty result.

    A no-op transition (identical file_pre/file_post) would pollute the
    history; the empty-items short-circuit must skip the record entirely
    and must not leave a snapshot directory behind.
    """
    setforge_yaml = _make_setforge_yaml(tmp_path)

    transition_calls: list[Any] = []

    def _fake_write_transition(*a: Any, **kw: Any) -> Path:
        transition_calls.append(1)
        return Path("/tmp/fake")

    monkeypatch.setattr(
        "setforge.wizard.transitions.write_transition",
        _fake_write_transition,
    )

    snapshot_base = tmp_path / "snaps"
    console = Console(file=StringIO(), force_terminal=False, no_color=True)
    decisions = run_wizard_loop(
        iter([]),
        setforge_yaml_path=setforge_yaml,
        snapshot_base=snapshot_base,
        console=console,
        auto_accept="k",
        transition_command=TransitionCommand.MERGE,
        profile="p",
        pending_message="unused",
    )

    assert decisions == []
    assert transition_calls == []
    # The short-circuit precedes Snapshot construction, so no snapshot
    # base directory is allocated for the no-drift path.
    assert not snapshot_base.exists()


def test_run_wizard_loop_breaks_on_manual_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First MANUAL_PENDING halts the loop; prints the trigger-specific message."""
    item1 = _make_item(tmp_path, "one")
    item2 = _make_item(tmp_path, "two")
    setforge_yaml = _make_setforge_yaml(tmp_path)

    apply_call_count = {"n": 0}

    def fake_apply_action(item: DriftItem, choice: str, **kw: Any) -> ActionResult:
        apply_call_count["n"] += 1
        return ActionResult.MANUAL_PENDING

    monkeypatch.setattr("setforge.wizard.apply_action", fake_apply_action)
    # Stub write_transition so the test does not touch real state dirs.
    monkeypatch.setattr(
        "setforge.wizard.transitions.write_transition",
        lambda *a, **kw: Path("/tmp/fake"),
    )

    buf = StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=200)

    decisions = run_wizard_loop(
        iter([item1, item2]),
        setforge_yaml_path=setforge_yaml,
        snapshot_base=tmp_path / "snaps",
        console=console,
        auto_accept="m",
        transition_command=TransitionCommand.MERGE,
        profile="p",
        pending_message="resume with: foo {src_path}",
    )

    assert decisions == [(item1, ActionResult.MANUAL_PENDING)]
    assert apply_call_count["n"] == 1
    assert "resume with: foo" in buf.getvalue()
