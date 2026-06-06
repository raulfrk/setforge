"""Proof that legacy preserve_* and disposition reconciliation models coexist.

Drives the production loader (setforge.config.load_config / Config.model_validate /
_validate_tolerant) — never a frozen pre-disposition model fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from setforge.config import (
    Disposition,
    _has_reconciliation_directive,
    _partition_reconciliation_adjacent,
    load_config,
)

_GENERIC = "ignoring unknown setforge.yaml key"
_ESCALATED = "declares a reconciliation directive"


def _write(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def _cfg(*tracked_blocks: str) -> str:
    refs = "".join(
        f"      - {b.splitlines()[0].strip().rstrip(':')}\n" for b in tracked_blocks
    )
    return (
        'schema_version: "1.1"\n'
        "version: 1\n"
        "tracked_files:\n"
        + "".join(tracked_blocks)
        + "profiles:\n  p:\n    tracked_files:\n"
        + refs
    )


_LEGACY = (
    "  legacy:\n"
    "    src: legacy.md\n"
    "    dst: ~/legacy.md\n"
    "    preserve_user_sections: true\n"
)
_MANAGED = (
    "  managed:\n    src: managed.md\n    dst: ~/managed.md\n    disposition: shared\n"
)
_PLAIN = "  plain:\n    src: plain.md\n    dst: ~/plain.md\n"


# --- Proof 1: mixed config validates both modes, no warning ---


def test_mixed_config_loads_both_modes_no_warnings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg_path = _write(tmp_path, _cfg(_LEGACY, _MANAGED))
    for tolerate in (True, False):
        cfg = load_config(cfg_path, tolerate_unknown=tolerate)
        assert cfg.tracked_files["legacy"].preserve_user_sections is True
        assert cfg.tracked_files["managed"].disposition is Disposition.SHARED
    err = capsys.readouterr().err
    assert _GENERIC not in err
    assert _ESCALATED not in err


# --- Proof 2: reconciliation-adjacent unknown -> escalated; strict refuses ---


def test_unknown_key_on_disposition_file_escalates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    managed_extra = _MANAGED + "    reconcile_v2: later\n"
    cfg_path = _write(tmp_path, _cfg(managed_extra))
    cfg = load_config(cfg_path, tolerate_unknown=True)
    assert cfg.tracked_files["managed"].disposition is Disposition.SHARED
    err = capsys.readouterr().err
    assert err.count(_ESCALATED) == 1
    assert "tracked_files.managed.reconcile_v2" in err
    assert _GENERIC not in err


def test_unknown_key_on_disposition_file_strict_refuses(tmp_path: Path) -> None:
    managed_extra = _MANAGED + "    reconcile_v2: later\n"
    cfg_path = _write(tmp_path, _cfg(managed_extra))
    with pytest.raises(ValidationError):
        load_config(cfg_path, tolerate_unknown=False)


def test_unknown_key_on_plain_file_is_generic_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plain_extra = _PLAIN + "    reconcile_v2: later\n"
    cfg_path = _write(tmp_path, _cfg(plain_extra))
    load_config(cfg_path, tolerate_unknown=True)
    err = capsys.readouterr().err
    assert _ESCALATED not in err
    assert err.count(_GENERIC) == 1


# --- Proof 3a: both-fields file is rejected by the new engine in both loader modes ---


def test_both_fields_rejected_both_modes(tmp_path: Path) -> None:
    both = (
        "  both:\n"
        "    src: both.md\n"
        "    dst: ~/both.md\n"
        "    disposition: shared\n"
        "    preserve_user_sections: true\n"
    )
    cfg_path = _write(tmp_path, _cfg(both))
    for tolerate in (True, False):
        with pytest.raises(ValidationError, match="disposition"):
            load_config(cfg_path, tolerate_unknown=tolerate)


# --- Proof 3b: predicate + partition logic (disposition-strip case) ---


@pytest.mark.parametrize(
    "mapping",
    [
        {"disposition": "shared"},
        {"preserve_user_sections": True},
        {"preserve_user_keys": ["a"]},
        {"preserve_user_keys_deep": ["a"]},
    ],
)
def test_has_reconciliation_directive_true(mapping: dict[str, object]) -> None:
    assert _has_reconciliation_directive(mapping) is True


@pytest.mark.parametrize(
    "mapping",
    [
        {},
        {"src": "a.md", "dst": "~/a.md"},
        {"preserve_user_sections": False},
        {"preserve_user_keys": []},
        {"preserve_user_sections_mode": "keep_defaults"},
    ],
)
def test_has_reconciliation_directive_false(mapping: dict[str, object]) -> None:
    assert _has_reconciliation_directive(mapping) is False


def test_partition_splits_adjacent_from_ordinary() -> None:
    data = {
        "tracked_files": {
            "managed": {"disposition": "shared", "x": 1},
            "plain": {"src": "p.md", "y": 2},
        }
    }
    locs: list[tuple[object, ...]] = [
        ("tracked_files", "managed", "x"),
        ("tracked_files", "plain", "y"),
        ("some_top_level_extra",),
    ]
    adjacent, ordinary = _partition_reconciliation_adjacent(data, locs)
    assert adjacent == [("tracked_files", "managed", "x")]
    assert ordinary == [
        ("tracked_files", "plain", "y"),
        ("some_top_level_extra",),
    ]


# --- Proof 4: escalated warning count == N for N disposition files ---


def test_escalated_warning_fires_once_per_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocks = [
        f"  d{i}:\n    src: d{i}.md\n    dst: ~/d{i}.md\n"
        f"    disposition: shared\n    reconcile_v2: later\n"
        for i in range(3)
    ]
    cfg_path = _write(tmp_path, _cfg(*blocks))
    load_config(cfg_path, tolerate_unknown=True)
    err = capsys.readouterr().err
    assert err.count(_ESCALATED) == 3
