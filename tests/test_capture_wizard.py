"""Tests for the capture-time merge wizard — :mod:`setforge.capture_wizard`.

Walker tests (Phase A) cover the two flavors of capture-time drift the
walker yields:

1. Deep-merge sub-key drift — for paths in
   ``TrackedFile.preserve_user_keys_deep``, walk per-sub-key.
2. Non-preserve top-level drift — for top-level keys not covered by
   any preserve list (symmetric with install's
   ``walk_unexpected_drift``).

Wrapper test (Phase B) verifies ``run_capture_wizard`` delegates to
:func:`setforge.wizard.run_wizard_loop` with ``TransitionCommand.SYNC``.

Orchestration tests (Phase G) verify ``capture_profile`` fires the
wizard, raises :class:`CaptureRequiresInteractive` in non-TTY contexts
without ``--auto``, and surfaces interactive decisions to tracked /
``setforge.yaml``.

Cancel-atomicity test (Phase G) verifies Ctrl-C mid-wizard restores
tracked + ``setforge.yaml`` to pre-wizard state and short-circuits the
writeback.
"""

from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from setforge.capture import CaptureAction, CaptureAuto, capture_profile
from setforge.capture_wizard import run_capture_wizard, walk_capture_drift
from setforge.config import Config, Profile, TrackedFile
from setforge.errors import CaptureRequiresInteractive
from setforge.transitions import TransitionCommand
from setforge.wizard import ActionResult, DriftItem, DriftMode, FileFormat

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_config(
    tmp_path: Path,
    *,
    src_text: str | None,
    dst_text: str,
    tracked_file_name: str = "x",
    preserve_user_keys: list[str] | None = None,
    preserve_user_keys_deep: list[str] | None = None,
    is_json: bool = False,
) -> tuple[Config, Path, Path, Path]:
    """Build a (Config, repo_root, src, dst) tuple with one tracked_file.

    When ``src_text`` is None the tracked file is not created (fresh
    capture path).
    """
    ext = ".json" if is_json else ".yaml"
    repo = tmp_path / "repo"
    src = repo / "tracked" / f"{tracked_file_name}{ext}"
    if src_text is not None:
        _write(src, src_text)
    dst = tmp_path / "live" / f"{tracked_file_name}{ext}"
    _write(dst, dst_text)
    config = Config(
        tracked_files={
            tracked_file_name: TrackedFile(
                src=Path(f"{tracked_file_name}{ext}"),
                dst=str(dst),
                preserve_user_keys=preserve_user_keys or [],
                preserve_user_keys_deep=preserve_user_keys_deep or [],
            )
        },
        profiles={"p": Profile(tracked_files=[tracked_file_name])},
    )
    return config, repo, src, dst


def _make_setforge_yaml(tmp_path: Path, *, tracked_file_name: str = "x") -> Path:
    """Write a minimal valid setforge.yaml referencing the test tracked_file."""
    path = tmp_path / "setforge.yaml"
    path.write_text(
        f"version: 1\n"
        f"tracked_files:\n"
        f"  {tracked_file_name}:\n"
        f"    src: {tracked_file_name}.yaml\n"
        f"    dst: /tmp/{tracked_file_name}.yaml\n"
        f"    preserve_user_keys: []\n"
        f"profiles:\n"
        f"  p:\n"
        f"    tracked_files: [{tracked_file_name}]\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect transition state to ``tmp_path`` so tests never touch ~."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))


# ---------------------------------------------------------------------------
# Phase A — Walker tests for deep-merge sub-key drift
# ---------------------------------------------------------------------------


def test_walker_yields_for_shared_different_subkey(tmp_path: Path) -> None:
    """Deep path 'a' with both sides having a.b but differing values →
    one item, key_path='a.b', mode='deep'."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text="a:\n  b: 1\n",
        dst_text="a:\n  b: 99\n",
        preserve_user_keys_deep=["a"],
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert len(items) == 1
    item = items[0]
    assert item.key_path == "a.b"
    assert item.mode is DriftMode.DEEP
    assert item.tracked_value == 1
    assert item.live_value == 99
    assert item.file_format is FileFormat.YAML


def test_walker_yields_for_live_only_subkey(tmp_path: Path) -> None:
    """Live has a.c but tracked doesn't → item with tracked_value=None."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text="a:\n  b: 1\n",
        dst_text="a:\n  b: 1\n  c: new\n",
        preserve_user_keys_deep=["a"],
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert len(items) == 1
    item = items[0]
    assert item.key_path == "a.c"
    assert item.tracked_value is None
    assert item.live_value == "new"
    assert item.mode is DriftMode.DEEP


def test_walker_silent_on_shared_identical_subkey(tmp_path: Path) -> None:
    """Identical sub-key on both sides → no item."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text="a:\n  b: 1\n",
        dst_text="a:\n  b: 1\n",
        preserve_user_keys_deep=["a"],
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert items == []


def test_walker_silent_on_tracked_only_subkey(tmp_path: Path) -> None:
    """Tracked-only sub-key under deep path → no item (preserved at writeback)."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text="a:\n  b: 1\n  e: tracked_only\n",
        dst_text="a:\n  b: 1\n",
        preserve_user_keys_deep=["a"],
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert items == []


def test_walker_recurses_into_nested_dicts(tmp_path: Path) -> None:
    """Three-level nesting under deep path; one leaf differs → one
    item with full dotted path."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text="root:\n  mid:\n    leaf: tracked\n    other: same\n",
        dst_text="root:\n  mid:\n    leaf: live\n    other: same\n",
        preserve_user_keys_deep=["root"],
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert len(items) == 1
    assert items[0].key_path == "root.mid.leaf"
    assert items[0].mode is DriftMode.DEEP


# ---------------------------------------------------------------------------
# Phase A — Walker tests for non-preserve top-level drift
# ---------------------------------------------------------------------------


def test_walker_yields_for_shared_different_top_level_non_preserve(
    tmp_path: Path,
) -> None:
    """Tracked editor.tabSize=2, live editor.tabSize=4, no preserve →
    one item, mode='shallow', key_path='editor'."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text="editor:\n  tabSize: 2\n",
        dst_text="editor:\n  tabSize: 4\n",
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert len(items) == 1
    item = items[0]
    assert item.key_path == "editor"
    assert item.mode is DriftMode.SHALLOW


def test_walker_yields_for_live_only_top_level(tmp_path: Path) -> None:
    """Live has 'extra_key', tracked doesn't, no preserve → one item,
    tracked_value=None, mode='shallow'."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text="kept: 1\n",
        dst_text="kept: 1\nextra_key: hello\n",
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert len(items) == 1
    item = items[0]
    assert item.key_path == "extra_key"
    assert item.tracked_value is None
    assert item.live_value == "hello"
    assert item.mode is DriftMode.SHALLOW


def test_walker_silent_on_tracked_only_top_level(tmp_path: Path) -> None:
    """Tracked has legacy_key, live doesn't → no item (preserved at
    writeback by symmetric capture behavior)."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text="kept: 1\nlegacy_key: oldvalue\n",
        dst_text="kept: 1\n",
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert items == []


def test_walker_silent_on_keys_in_preserve_user_keys(tmp_path: Path) -> None:
    """Drift on a shallow-preserve key → silent (capture strips, no wizard)."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text="kept: 1\nhost_key: tracked\n",
        dst_text="kept: 1\nhost_key: live\n",
        preserve_user_keys=["host_key"],
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert items == []


def test_walker_silent_on_top_level_prefix_of_deep_path(
    tmp_path: Path,
) -> None:
    """When deep path is 'config.settings', the top-level 'config' key
    must NOT generate a non-preserve top-level drift item — it's handled
    by the deep walk."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text="config:\n  settings:\n    a: 1\n",
        dst_text="config:\n  settings:\n    a: 99\n",
        preserve_user_keys_deep=["config.settings"],
    )
    items = list(walk_capture_drift(config, "p", repo))
    # Only the deep-leaf drift; no shallow item for top-level "config".
    assert len(items) == 1
    assert items[0].key_path == "config.settings.a"
    assert items[0].mode is DriftMode.DEEP


def test_walker_skips_when_tracked_missing(tmp_path: Path) -> None:
    """Fresh capture (no tracked) → walker yields nothing."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text=None,
        dst_text="a: 1\n",
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert items == []


def test_walker_skips_when_live_missing(tmp_path: Path) -> None:
    """No live file → walker yields nothing for that tracked_file."""
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x.yaml"
    _write(src, "a: 1\n")
    dst = tmp_path / "live" / "x.yaml"
    # Don't create dst.
    config = Config(
        tracked_files={"x": TrackedFile(src=Path("x.yaml"), dst=str(dst))},
        profiles={"p": Profile(tracked_files=["x"])},
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert items == []


def test_walker_honors_tracked_file_filter(tmp_path: Path) -> None:
    """tracked_file_filter narrows to the named tracked_file."""
    repo = tmp_path / "repo"
    src1 = repo / "tracked" / "x.yaml"
    src2 = repo / "tracked" / "y.yaml"
    _write(src1, "a: 1\n")
    _write(src2, "a: 1\n")
    dst1 = tmp_path / "live" / "x.yaml"
    dst2 = tmp_path / "live" / "y.yaml"
    _write(dst1, "a: 99\n")
    _write(dst2, "a: 99\n")
    config = Config(
        tracked_files={
            "x": TrackedFile(src=Path("x.yaml"), dst=str(dst1)),
            "y": TrackedFile(src=Path("y.yaml"), dst=str(dst2)),
        },
        profiles={"p": Profile(tracked_files=["x", "y"])},
    )
    all_items = list(walk_capture_drift(config, "p", repo))
    only_x = list(walk_capture_drift(config, "p", repo, tracked_file_filter="x"))
    only_z = list(walk_capture_drift(config, "p", repo, tracked_file_filter="z"))
    assert len(all_items) == 2
    assert len(only_x) == 1
    assert only_x[0].tracked_file_name == "x"
    assert only_z == []


def test_walker_skips_section_tracked_files(tmp_path: Path) -> None:
    """Markdown tracked_files using preserve_user_sections aren't walked
    (capture's section handling stays as today)."""
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x.md"
    _write(src, "# tracked\n")
    dst = tmp_path / "live" / "x.md"
    _write(dst, "# live\n")
    config = Config(
        tracked_files={
            "x": TrackedFile(
                src=Path("x.md"),
                dst=str(dst),
                preserve_user_sections=True,
            )
        },
        profiles={"p": Profile(tracked_files=["x"])},
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert items == []


def test_walker_jsonc_top_level_non_preserve_drift(tmp_path: Path) -> None:
    """JSONC: top-level non-preserve shared-different key yields one
    shallow item. (JSONC deep-merge sub-key walking is out of scope
    for nen.23 v1; the wizard's [u] action uses
    :func:`setforge.jsonc.overlay_user_keys` which only handles
    top-level literal key names. Per-sub-key JSONC drift lands via
    `setforge-nen.19`.)"""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text='{\n  "tabSize": 2\n}\n',
        dst_text='{\n  "tabSize": 4\n}\n',
        is_json=True,
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert len(items) == 1
    item = items[0]
    assert item.key_path == "tabSize"
    assert item.mode is DriftMode.SHALLOW
    assert item.file_format is FileFormat.JSONC


# ---------------------------------------------------------------------------
# Phase B — Wrapper test
# ---------------------------------------------------------------------------


def test_run_capture_wizard_delegates_to_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_capture_wizard auto_accept='k' walks 2 items and records one
    SYNC transition under SETFORGE_STATE_DIR."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text="a: 1\nb: 2\n",
        dst_text="a: 99\nb: 88\n",
    )
    setforge_yaml = _make_setforge_yaml(tmp_path)

    captured_calls: list[dict[str, Any]] = []

    def fake_run_wizard_loop(items: Any, **kwargs: Any) -> Any:
        materialized = list(items)
        captured_calls.append({"items": materialized, **kwargs})
        return [(it, ActionResult.KEEP_TRACKED) for it in materialized]

    monkeypatch.setattr(
        "setforge.capture_wizard.wizard.run_wizard_loop",
        fake_run_wizard_loop,
    )

    decisions = run_capture_wizard(
        config,
        "p",
        repo,
        setforge_yaml_path=setforge_yaml,
        snapshot_base=tmp_path / "snaps",
        console=Console(file=StringIO(), force_terminal=False, no_color=True),
        auto_accept="k",
    )

    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["transition_command"] is TransitionCommand.SYNC
    assert call["profile"] == "p"
    assert call["auto_accept"] == "k"
    assert "setforge sync --profile=p" in call["pending_message"]
    # Two items walked: a (shared-different) and b (shared-different)
    assert len(call["items"]) == 2
    assert all(d[1] == ActionResult.KEEP_TRACKED for d in decisions)


# ---------------------------------------------------------------------------
# Phase G — Orchestration tests
# ---------------------------------------------------------------------------


def test_capture_profile_errors_in_non_interactive_when_drift_present_and_no_auto(
    tmp_path: Path,
) -> None:
    """Non-interactive + drift + no --auto → CaptureRequiresInteractive.
    Tracked is unchanged."""
    config, repo, src, _dst = _make_config(
        tmp_path,
        src_text="a: 1\nb: 2\n",
        dst_text="a: 99\nb: 2\n",
    )
    setforge_yaml = _make_setforge_yaml(tmp_path)
    src_before = src.read_text()

    with pytest.raises(CaptureRequiresInteractive):
        capture_profile(
            config,
            "p",
            repo,
            setforge_yaml_path=setforge_yaml,
            interactive=False,
            auto=None,
        )

    assert src.read_text() == src_before


def test_capture_profile_proceeds_in_non_interactive_when_no_drift(
    tmp_path: Path,
) -> None:
    """No drift + non-interactive → no error, results contain NOOP."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text="a: 1\n",
        dst_text="a: 1\n",
    )
    setforge_yaml = _make_setforge_yaml(tmp_path)

    results = capture_profile(
        config,
        "p",
        repo,
        setforge_yaml_path=setforge_yaml,
        interactive=False,
        auto=None,
    )

    assert any(r.action is CaptureAction.NOOP for r in results)


def test_capture_profile_auto_use_live_absorbs_all_drift(
    tmp_path: Path,
) -> None:
    """Mixed deep + non-preserve drift, auto='use-live' → tracked has
    live's values; tracked-only sub-keys preserved; tracked-only top-
    level keys preserved (the behavior change gate)."""
    config, repo, src, _dst = _make_config(
        tmp_path,
        src_text=(
            "deep_root:\n"
            "  shared: tracked_val\n"
            "  tracked_only: legacy_sub\n"
            "non_preserve: tracked_top\n"
            "tracked_only_top: legacy_top\n"
        ),
        dst_text=(
            "deep_root:\n"
            "  shared: live_val\n"
            "  live_only: new_sub\n"
            "non_preserve: live_top\n"
        ),
        preserve_user_keys_deep=["deep_root"],
    )
    setforge_yaml = _make_setforge_yaml(tmp_path)

    capture_profile(
        config,
        "p",
        repo,
        setforge_yaml_path=setforge_yaml,
        interactive=False,
        auto=CaptureAuto.USE_LIVE,
    )

    final = src.read_text()
    # Wizard absorbed the deep drift: shared sub-key now live's; live-
    # only sub-key added; tracked-only sub-key survived.
    assert "shared: live_val" in final
    assert "live_only: new_sub" in final
    assert "tracked_only: legacy_sub" in final
    # Wizard absorbed the non-preserve drift.
    assert "non_preserve: live_top" in final
    # Tracked-only top-level key survived (behavior change).
    assert "tracked_only_top: legacy_top" in final


def test_capture_profile_auto_keep_tracked_rejects_all_drift(
    tmp_path: Path,
) -> None:
    """Same fixture, auto='keep-tracked' → tracked unchanged."""
    src_text = (
        "deep_root:\n"
        "  shared: tracked_val\n"
        "  tracked_only: legacy_sub\n"
        "non_preserve: tracked_top\n"
        "tracked_only_top: legacy_top\n"
    )
    config, repo, src, _dst = _make_config(
        tmp_path,
        src_text=src_text,
        dst_text=(
            "deep_root:\n"
            "  shared: live_val\n"
            "  live_only: new_sub\n"
            "non_preserve: live_top\n"
        ),
        preserve_user_keys_deep=["deep_root"],
    )
    setforge_yaml = _make_setforge_yaml(tmp_path)

    capture_profile(
        config,
        "p",
        repo,
        setforge_yaml_path=setforge_yaml,
        interactive=False,
        auto=CaptureAuto.KEEP_TRACKED,
    )

    # Content untouched at every drift item.
    final = src.read_text()
    assert "shared: tracked_val" in final
    assert "tracked_only: legacy_sub" in final
    assert "non_preserve: tracked_top" in final
    assert "tracked_only_top: legacy_top" in final
    assert "live_val" not in final
    assert "live_only" not in final
    assert "live_top" not in final


def test_capture_profile_interactive_mixed_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkey-patched prompt_one returns mixed (u/k/s) over 3 walked
    items. Tracked reflects each decision; setforge.yaml gained the
    'save-as-preserved' key path."""
    config, repo, src, _dst = _make_config(
        tmp_path,
        src_text=(
            "deep_root:\n"
            "  k_keep: tracked_keep\n"
            "  k_use: tracked_use\n"
            "save_top: tracked_save\n"
        ),
        dst_text=(
            "deep_root:\n  k_keep: live_keep\n  k_use: live_use\nsave_top: live_save\n"
        ),
        preserve_user_keys_deep=["deep_root"],
    )
    setforge_yaml = _make_setforge_yaml(tmp_path)

    # Walker yields three items in this order:
    #   1. deep_root.k_keep (deep)
    #   2. deep_root.k_use  (deep)
    #   3. save_top         (shallow)
    choices = iter(["k", "u", "s"])

    def fake_prompt_one(item: DriftItem, console: Console) -> str:
        return next(choices)

    monkeypatch.setattr("setforge.wizard.prompt_one", fake_prompt_one)

    capture_profile(
        config,
        "p",
        repo,
        setforge_yaml_path=setforge_yaml,
        interactive=True,
        auto=None,
    )

    final = src.read_text()
    assert "k_keep: tracked_keep" in final  # [k] preserved tracked
    assert "k_use: live_use" in final  # [u] absorbed live

    yaml_text = setforge_yaml.read_text()
    # [s] appended save_top to the tracked_file's preserve_user_keys
    assert "save_top" in yaml_text


def test_capture_wizard_cancel_restores_tracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KeyboardInterrupt mid-wizard restores tracked + setforge.yaml
    from snapshot. Capture writeback does not run."""
    config, repo, src, _dst = _make_config(
        tmp_path,
        src_text=("deep_root:\n  a: tracked_a\n  b: tracked_b\n"),
        dst_text=("deep_root:\n  a: live_a\n  b: live_b\n"),
        preserve_user_keys_deep=["deep_root"],
    )
    setforge_yaml = _make_setforge_yaml(tmp_path)

    src_pre = src.read_bytes()
    yaml_pre = setforge_yaml.read_bytes()

    def fake_prompt_one(item: DriftItem, console: Console) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("setforge.wizard.prompt_one", fake_prompt_one)

    # Use temporary snapshot base so signal handlers in
    # run_wizard_loop don't touch ~/.local. The wizard re-raises
    # KeyboardInterrupt; capture_profile must propagate.
    with pytest.raises(KeyboardInterrupt):
        capture_profile(
            config,
            "p",
            repo,
            setforge_yaml_path=setforge_yaml,
            interactive=True,
            auto=None,
            snapshot_base=tmp_path / "snaps",
        )

    # KeyboardInterrupt path: snapshot is preserved; the CLI signal
    # handler is responsible for restore. capture_profile must NOT
    # run writeback after a cancel, so tracked + setforge.yaml are
    # exactly the pre-wizard bytes (no writeback over the wizard's
    # in-progress edits).
    assert src.read_bytes() == src_pre
    assert setforge_yaml.read_bytes() == yaml_pre


# ---------------------------------------------------------------------------
# JSONC nested-path walker (setforge-nen.19)
# ---------------------------------------------------------------------------


def _python_block_jsonc(extra_lines: str = "") -> str:
    """Render a ``"[python]"`` JSONC block with optional extra sub-keys.

    Centralizes the long string literals so nested-path tests don't trip
    the ``E501`` ruff rule and stay readable.
    """
    body = '    "editor.defaultFormatter": "ruff"'
    if extra_lines:
        body = body + ",\n" + extra_lines
    return '{\n  "[python]": {\n' + body + "\n  }\n}\n"


def test_walker_jsonc_path_emits_arrow_separator_for_deep_drift(
    tmp_path: Path,
) -> None:
    """Case C — JSONC deep-walk emits ` > ` paths for sub-key drift."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text=_python_block_jsonc(),
        dst_text=_python_block_jsonc('    "editor.tabSize": 4'),
        preserve_user_keys_deep=["[python]"],
        is_json=True,
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert len(items) == 1
    item = items[0]
    assert item.key_path == "[python] > editor.tabSize"
    assert item.file_format == "jsonc"
    assert item.mode == "deep"
    assert item.tracked_value is None
    assert item.live_value == 4


def test_walker_jsonc_nested_path_preserved_position_is_skipped(
    tmp_path: Path,
) -> None:
    """Case A — leaf covered by ``preserve_user_keys`` nested path is
    SILENT during sync (wizard does not prompt). The path's head is
    treated as a deep-walk anchor; the leaf is filtered out, and
    no other sub-key drifts, so the walker yields nothing."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text=_python_block_jsonc(),
        dst_text=_python_block_jsonc('    "editor.fontSize": 14'),
        preserve_user_keys=["[python] > editor.fontSize"],
        is_json=True,
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert items == []


def test_walker_jsonc_nested_path_prompts_on_unspecified_sibling(
    tmp_path: Path,
) -> None:
    """Nested path covers fontSize; tabSize is an unspecified sibling
    under the same top-level. The path's head is treated as a deep
    prefix so the top-level walker doesn't fire shallow; the deep
    walker emits drift only on tabSize (fontSize is filtered)."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text=_python_block_jsonc(),
        dst_text=_python_block_jsonc(
            '    "editor.fontSize": 14,\n    "editor.tabSize": 4'
        ),
        preserve_user_keys=["[python] > editor.fontSize"],
        is_json=True,
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert len(items) == 1
    item = items[0]
    assert item.key_path == "[python] > editor.tabSize"
    assert item.mode == "deep"
    assert item.live_value == 4


def test_walker_jsonc_deep_prompts_on_unspecified_sibling(
    tmp_path: Path,
) -> None:
    """Case C proper — `[python]` in `preserve_user_keys_deep`, no
    nested preserve path; per-sub-key drift surfaces."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text=_python_block_jsonc(),
        dst_text=_python_block_jsonc('    "editor.tabSize": 4'),
        preserve_user_keys_deep=["[python]"],
        is_json=True,
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert len(items) == 1
    item = items[0]
    assert item.key_path == "[python] > editor.tabSize"
    assert item.mode == "deep"


def test_walker_jsonc_yaml_separator_unchanged(tmp_path: Path) -> None:
    """YAML deep-walk continues to use ``.`` separators after the
    format-aware join helper is introduced."""
    config, repo, _src, _dst = _make_config(
        tmp_path,
        src_text="root:\n  mid:\n    leaf: tracked\n",
        dst_text="root:\n  mid:\n    leaf: live\n",
        preserve_user_keys_deep=["root"],
    )
    items = list(walk_capture_drift(config, "p", repo))
    assert len(items) == 1
    assert items[0].key_path == "root.mid.leaf"
