"""Unit tests for the local.yaml host-local mode/dst/symlink_target
overlay resolver.

Validates the semantics of
:func:`setforge.config.apply_host_local_tracked_file_overrides` — the
contract that ``compare`` / ``install`` rely on to surface per-host
chmod / install-path / symlink overrides without rewriting the shared
``setforge.yaml``. Companion to the pydantic-shape validator tests in
:mod:`tests.test_source` (which lock down the parse-time invariants).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from setforge.config import (
    Config,
    apply_host_local_tracked_file_overrides,
    load_config,
)

_BASE_YAML = (
    "version: 1\n"
    "tracked_files:\n"
    "  hook:\n"
    "    src: hook.sh\n"
    "    dst: ~/.tracked-host/hook.sh\n"
    "profiles:\n"
    "  p:\n"
    "    tracked_files:\n"
    "      - hook\n"
)


def _write_cfg(tmp_path: Path, body: str = _BASE_YAML) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body, encoding="utf-8")
    (tmp_path / "tracked").mkdir(exist_ok=True)
    (tmp_path / "tracked" / "hook.sh").write_text(
        "#!/bin/sh\necho hi\n", encoding="utf-8"
    )
    return cfg


def _load(tmp_path: Path) -> Config:
    return load_config(_write_cfg(tmp_path))


def test_empty_overlay_returns_no_applied_entries(tmp_path: Path) -> None:
    """No local.yaml ⇒ resolver no-ops and returns an empty mapping."""
    cfg = _load(tmp_path)
    applied = apply_host_local_tracked_file_overrides(cfg)
    assert applied == {}
    # And the tracked_file is unmutated.
    assert cfg.tracked_files["hook"].mode is None
    assert cfg.tracked_files["hook"].symlink is None


def test_overlay_with_only_preserve_user_keys_is_no_op(tmp_path: Path) -> None:
    """An overlay that only declares preserve_user_keys / host_local_sections
    does not trigger the m3qx applied-entry surface."""
    (tmp_path / "local.yaml").write_text(
        "tracked_files:\n"
        "  hook:\n"
        "    preserve_user_keys:\n"
        "      add: []\n"
        "      remove: []\n",
        encoding="utf-8",
    )
    cfg = _load(tmp_path)
    applied = apply_host_local_tracked_file_overrides(cfg)
    assert applied == {}


def test_mode_override_propagates_to_tracked_file(tmp_path: Path) -> None:
    """``mode: 0o755`` in local.yaml lands on the TrackedFile model."""
    (tmp_path / "local.yaml").write_text(
        "tracked_files:\n  hook:\n    mode: 0o755\n",
        encoding="utf-8",
    )
    cfg = _load(tmp_path)
    applied = apply_host_local_tracked_file_overrides(cfg)
    assert set(applied) == {"hook"}
    assert applied["hook"].mode == 0o755
    assert applied["hook"].dst is None
    assert applied["hook"].symlink_target is None
    # TrackedFile carries the override post-resolve.
    assert cfg.tracked_files["hook"].mode == 0o755


def test_dst_override_propagates_to_tracked_file(tmp_path: Path) -> None:
    """``dst: /home/alt/x`` in local.yaml lands on TrackedFile.dst."""
    (tmp_path / "local.yaml").write_text(
        "tracked_files:\n  hook:\n    dst: /home/alt/hook.sh\n",
        encoding="utf-8",
    )
    cfg = _load(tmp_path)
    applied = apply_host_local_tracked_file_overrides(cfg)
    assert applied["hook"].dst == Path("/home/alt/hook.sh")
    assert cfg.tracked_files["hook"].dst == "/home/alt/hook.sh"


def test_symlink_target_override_propagates_to_tracked_file(
    tmp_path: Path,
) -> None:
    """``symlink_target: /usr/local/x`` overrides TrackedFile.symlink."""
    (tmp_path / "local.yaml").write_text(
        "tracked_files:\n  hook:\n    symlink_target: /usr/local/share/hook.sh\n",
        encoding="utf-8",
    )
    cfg = _load(tmp_path)
    applied = apply_host_local_tracked_file_overrides(cfg)
    assert applied["hook"].symlink_target == Path("/usr/local/share/hook.sh")
    assert cfg.tracked_files["hook"].symlink == "/usr/local/share/hook.sh"


def test_overlay_for_unknown_tracked_file_is_silent(tmp_path: Path) -> None:
    """An overlay entry referencing a tracked_file id absent from the
    setforge.yaml registry is silently skipped (the validate CLI is
    the surface for unknown-id diagnostics)."""
    (tmp_path / "local.yaml").write_text(
        "tracked_files:\n  not_a_real_tracked_file:\n    mode: 0o755\n",
        encoding="utf-8",
    )
    cfg = _load(tmp_path)
    applied = apply_host_local_tracked_file_overrides(cfg)
    assert applied == {}


def test_combined_dst_and_mode_overlay_applies_both(tmp_path: Path) -> None:
    """Two of the three fields together: both land on TrackedFile."""
    (tmp_path / "local.yaml").write_text(
        "tracked_files:\n  hook:\n    mode: 0o600\n    dst: /home/alt/hook.sh\n",
        encoding="utf-8",
    )
    cfg = _load(tmp_path)
    applied = apply_host_local_tracked_file_overrides(cfg)
    assert applied["hook"].mode == 0o600
    assert applied["hook"].dst == Path("/home/alt/hook.sh")
    assert cfg.tracked_files["hook"].mode == 0o600
    assert cfg.tracked_files["hook"].dst == "/home/alt/hook.sh"


def test_revalidate_catches_post_merge_self_loop(tmp_path: Path) -> None:
    """When an overlay sets ``symlink_target`` equal to ``dst``, the
    revalidate-after-merge path surfaces ``_symlink_no_self_loop`` —
    silently dropping that check would break the cross-host portability
    invariant the TrackedFile model guards."""
    cfg = _load(tmp_path)
    # Force dst into a known shape so the overlay's symlink_target can
    # equal it after Path.expanduser.
    cfg.tracked_files["hook"] = cfg.tracked_files["hook"].model_copy(
        update={"dst": "/home/x/hook.sh"}
    )
    (tmp_path / "local.yaml").write_text(
        "tracked_files:\n  hook:\n    symlink_target: /home/x/hook.sh\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match=r"self-loop"):
        apply_host_local_tracked_file_overrides(cfg)
