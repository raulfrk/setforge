"""Tests for the file-level ``disposition`` field on ``TrackedFile``."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from setforge.config import (
    Config,
    Disposition,
    TrackedFile,
    apply_host_local_tracked_file_overrides,
    load_config,
)

# ---------------------------------------------------------------------------
# TrackedFile-level disposition field tests (existing)
# ---------------------------------------------------------------------------


def _tf(**kw: object) -> TrackedFile:
    return TrackedFile.model_validate({"src": "a.md", "dst": "~/a.md", **kw})


def test_disposition_defaults_none() -> None:
    assert _tf().disposition is None


def test_disposition_accepts_each_value() -> None:
    assert _tf(disposition="shared").disposition is Disposition.SHARED
    assert _tf(disposition="forked").disposition is Disposition.FORKED
    assert _tf(disposition="pinned").disposition is Disposition.PINNED


@pytest.mark.parametrize("bad", ["Shared", "PINNED", "shared ", "fork", "host-local"])
def test_disposition_rejects_bad_value(bad: str) -> None:
    with pytest.raises(ValidationError):
        _tf(disposition=bad)


# ---------------------------------------------------------------------------
# local.yaml per-host disposition override tests
# ---------------------------------------------------------------------------

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
    """Write a minimal setforge.yaml + tracked source to ``tmp_path``."""
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body, encoding="utf-8")
    (tmp_path / "tracked").mkdir(exist_ok=True)
    (tmp_path / "tracked" / "hook.sh").write_text(
        "#!/bin/sh\necho hi\n", encoding="utf-8"
    )
    return cfg


def _load(tmp_path: Path, body: str = _BASE_YAML) -> Config:
    """Load config from a freshly written setforge.yaml in ``tmp_path``."""
    return load_config(_write_cfg(tmp_path, body))


def test_disposition_override_forked_propagates(tmp_path: Path) -> None:
    """local.yaml ``disposition: forked`` lands on TrackedFile + override record."""
    (tmp_path / "local.yaml").write_text(
        "tracked_files:\n  hook:\n    disposition: forked\n",
        encoding="utf-8",
    )
    cfg = _load(tmp_path)
    applied = apply_host_local_tracked_file_overrides(cfg)
    assert set(applied) == {"hook"}
    assert applied["hook"].disposition is Disposition.FORKED
    assert applied["hook"].mode is None
    assert applied["hook"].dst is None
    assert applied["hook"].symlink_target is None
    # TrackedFile reflects the override after resolution.
    assert cfg.tracked_files["hook"].disposition is Disposition.FORKED


@pytest.mark.parametrize("bad", ["Shared", "bogus", "FORKED", "fork "])
def test_disposition_override_invalid_value_rejected_at_load(
    tmp_path: Path, bad: str
) -> None:
    """Invalid disposition string in local.yaml is rejected at overlay-load time."""
    (tmp_path / "local.yaml").write_text(
        f"tracked_files:\n  hook:\n    disposition: {bad!r}\n",
        encoding="utf-8",
    )
    cfg = _load(tmp_path)
    with pytest.raises(ValidationError):
        apply_host_local_tracked_file_overrides(cfg)


def test_overlay_with_mode_and_dst_no_disposition_regression(
    tmp_path: Path,
) -> None:
    """mode + dst overlay with no disposition still works; disposition stays None."""
    (tmp_path / "local.yaml").write_text(
        "tracked_files:\n  hook:\n    mode: 0o755\n    dst: /home/alt/hook.sh\n",
        encoding="utf-8",
    )
    cfg = _load(tmp_path)
    applied = apply_host_local_tracked_file_overrides(cfg)
    assert applied["hook"].mode == 0o755
    assert applied["hook"].dst == Path("/home/alt/hook.sh")
    assert applied["hook"].disposition is None
    assert cfg.tracked_files["hook"].disposition is None
