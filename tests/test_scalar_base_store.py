"""Tests for the per-host forked-scalar stored-base store."""

import json
from pathlib import Path

import pytest

from setforge import base_store_format, scalar_base_store, scalar_merge
from setforge.errors import BaseStoreError, BaseStoreIOError, BaseStoreSchemaError


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    return tmp_path


def _manifest_path(state_dir: Path, profile: str, file_id: str) -> Path:
    return state_dir / "scalar-base" / profile / f"{file_id}.json"


# --- root + layout -------------------------------------------------------


def test_scalar_base_root_is_sibling_of_base(state_dir: Path) -> None:
    assert scalar_base_store.scalar_base_root() == state_dir / "scalar-base"


def test_manifest_path_per_profile_and_file(state_dir: Path) -> None:
    scalar_base_store.set_base("vm", "settings", "a.b", 1)
    assert _manifest_path(state_dir, "vm", "settings").is_file()


# --- type fidelity round-trips ------------------------------------------


def test_round_trip_int_stays_int() -> None:
    scalar_base_store.set_base("vm", "f", "x", 1)
    value = scalar_base_store.get_base("vm", "f", "x")
    assert value == 1
    assert type(value) is int


def test_round_trip_float_stays_float() -> None:
    scalar_base_store.set_base("vm", "f", "x", 1.0)
    value = scalar_base_store.get_base("vm", "f", "x")
    assert value == 1.0
    assert type(value) is float


def test_round_trip_bool_stays_bool() -> None:
    scalar_base_store.set_base("vm", "f", "x", True)
    value = scalar_base_store.get_base("vm", "f", "x")
    assert value is True
    assert type(value) is bool


def test_round_trip_str_preserved() -> None:
    scalar_base_store.set_base("vm", "f", "x", "hello")
    assert scalar_base_store.get_base("vm", "f", "x") == "hello"


def test_bool_not_collapsed_to_int_on_disk(state_dir: Path) -> None:
    scalar_base_store.set_base("vm", "f", "x", True)
    raw = json.loads(_manifest_path(state_dir, "vm", "f").read_text())
    assert raw["x"]["value"] is True


# --- present:false vs value:null ----------------------------------------


def test_stored_null_returns_none() -> None:
    scalar_base_store.set_base("vm", "f", "x", None)
    assert scalar_base_store.get_base("vm", "f", "x") is None


def test_stored_null_is_present_true_on_disk(state_dir: Path) -> None:
    scalar_base_store.set_base("vm", "f", "x", None)
    raw = json.loads(_manifest_path(state_dir, "vm", "f").read_text())
    assert raw["x"] == {"present": True, "value": None}


def test_stored_absent_returns_sentinel() -> None:
    scalar_base_store.re_baseline("vm", "f", "x", scalar_merge.ABSENT)
    assert scalar_base_store.get_base("vm", "f", "x") is scalar_merge.ABSENT


def test_missing_path_returns_sentinel() -> None:
    scalar_base_store.set_base("vm", "f", "other", 1)
    assert scalar_base_store.get_base("vm", "f", "x") is scalar_merge.ABSENT


def test_missing_manifest_returns_sentinel() -> None:
    assert scalar_base_store.get_base("vm", "nofile", "x") is scalar_merge.ABSENT


# --- re_baseline ---------------------------------------------------------


def test_re_baseline_overwrites_value() -> None:
    scalar_base_store.set_base("vm", "f", "x", 1)
    scalar_base_store.re_baseline("vm", "f", "x", 2)
    assert scalar_base_store.get_base("vm", "f", "x") == 2


def test_re_baseline_absent_writes_present_false(state_dir: Path) -> None:
    scalar_base_store.set_base("vm", "f", "x", 1)
    scalar_base_store.re_baseline("vm", "f", "x", scalar_merge.ABSENT)
    raw = json.loads(_manifest_path(state_dir, "vm", "f").read_text())
    assert raw["x"] == {"present": False}
    assert scalar_base_store.get_base("vm", "f", "x") is scalar_merge.ABSENT


# --- batch set_bases -----------------------------------------------------


def test_set_bases_writes_two_paths_no_lost_update() -> None:
    scalar_base_store.set_bases("vm", "f", {"a.b": 1, "c.d": "two"})
    assert scalar_base_store.get_base("vm", "f", "a.b") == 1
    assert scalar_base_store.get_base("vm", "f", "c.d") == "two"


def test_set_bases_preserves_untouched_paths() -> None:
    scalar_base_store.set_base("vm", "f", "keep", 99)
    scalar_base_store.set_bases("vm", "f", {"a": 1, "b": 2})
    assert scalar_base_store.get_base("vm", "f", "keep") == 99
    assert scalar_base_store.get_base("vm", "f", "a") == 1
    assert scalar_base_store.get_base("vm", "f", "b") == 2


def test_set_bases_with_absent_sentinel() -> None:
    scalar_base_store.set_bases("vm", "f", {"a": scalar_merge.ABSENT, "b": 1})
    assert scalar_base_store.get_base("vm", "f", "a") is scalar_merge.ABSENT
    assert scalar_base_store.get_base("vm", "f", "b") == 1


# --- corrupt manifest ----------------------------------------------------


def test_corrupt_manifest_raises_base_store_error(state_dir: Path) -> None:
    path = _manifest_path(state_dir, "vm", "f")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")
    with pytest.raises(BaseStoreError):
        scalar_base_store.get_base("vm", "f", "x")


def test_corrupt_manifest_not_treated_as_empty(state_dir: Path) -> None:
    path = _manifest_path(state_dir, "vm", "f")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("garbage")
    with pytest.raises(BaseStoreError):
        scalar_base_store.set_base("vm", "f", "x", 1)


def test_non_object_top_level_raises_base_store_error(state_dir: Path) -> None:
    path = _manifest_path(state_dir, "vm", "f")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]")
    with pytest.raises(BaseStoreError):
        scalar_base_store.get_base("vm", "f", "x")


def test_scalar_record_raises_on_get(state_dir: Path) -> None:
    path = _manifest_path(state_dir, "vm", "f")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"a.b": 5}')
    with pytest.raises(BaseStoreError):
        scalar_base_store.get_base("vm", "f", "a.b")


def test_scalar_record_raises_on_set_bases(state_dir: Path) -> None:
    path = _manifest_path(state_dir, "vm", "f")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"a.b": 5}')
    with pytest.raises(BaseStoreError):
        scalar_base_store.set_bases("vm", "f", {"c.d": 1})


# --- NaN / Inf rejection -------------------------------------------------


def test_nan_rejected_at_write() -> None:
    with pytest.raises(BaseStoreError):
        scalar_base_store.set_base("vm", "f", "x", float("nan"))


def test_inf_rejected_at_write() -> None:
    with pytest.raises(BaseStoreError):
        scalar_base_store.set_base("vm", "f", "x", float("inf"))


def test_nan_rejected_in_batch() -> None:
    with pytest.raises(BaseStoreError):
        scalar_base_store.set_bases("vm", "f", {"a": 1, "b": float("nan")})


# --- path traversal ------------------------------------------------------


def test_set_base_rejects_traversal() -> None:
    with pytest.raises(BaseStoreError):
        scalar_base_store.set_base("vm", "../escape", "x", 1)


def test_set_base_rejects_absolute() -> None:
    with pytest.raises(BaseStoreError):
        scalar_base_store.set_base("vm", "/etc/passwd", "x", 1)


def test_get_base_rejects_traversal() -> None:
    with pytest.raises(BaseStoreError):
        scalar_base_store.get_base("vm", "../escape", "x")


# --- prune ---------------------------------------------------------------


def test_prune_removes_dropped_keeps_live() -> None:
    scalar_base_store.set_bases("vm", "f", {"a": 1, "b": 2, "c": 3})
    scalar_base_store.prune("vm", "f", {"a", "c"})
    assert scalar_base_store.get_base("vm", "f", "a") == 1
    assert scalar_base_store.get_base("vm", "f", "c") == 3
    assert scalar_base_store.get_base("vm", "f", "b") is scalar_merge.ABSENT


def test_prune_is_file_scoped() -> None:
    scalar_base_store.set_base("vm", "f1", "a", 1)
    scalar_base_store.set_base("vm", "f2", "a", 2)
    scalar_base_store.prune("vm", "f1", set())
    assert scalar_base_store.get_base("vm", "f1", "a") is scalar_merge.ABSENT
    assert scalar_base_store.get_base("vm", "f2", "a") == 2


def test_prune_missing_manifest_is_noop() -> None:
    scalar_base_store.prune("vm", "nofile", {"a"})


def test_prune_empty_live_clears_all() -> None:
    scalar_base_store.set_bases("vm", "f", {"a": 1, "b": 2})
    scalar_base_store.prune("vm", "f", set())
    assert scalar_base_store.get_base("vm", "f", "a") is scalar_merge.ABSENT
    assert scalar_base_store.get_base("vm", "f", "b") is scalar_merge.ABSENT


# --- IO error wrapping ---------------------------------------------------


def test_read_io_error_wrapped(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scalar_base_store.set_base("vm", "f", "x", 1)
    real_read_text = Path.read_text

    # Fail only the manifest read so this exercises the manifest-read
    # OSError path; the sidecar read (run first by the version check) must
    # still succeed, otherwise it short-circuits to a schema error.
    def boom(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if self.suffix == ".json":
            raise PermissionError("denied")
        return real_read_text(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(BaseStoreIOError):
        scalar_base_store.get_base("vm", "f", "x")


def test_set_bases_stamp_io_error_wrapped(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An OSError from the format-version stamp surfaces as BaseStoreIOError,
    # the same as the manifest write, rather than propagating raw.
    def boom(
        root: Path, *, version: str = base_store_format.BASE_STORE_FORMAT_VERSION
    ) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(base_store_format, "stamp_format_version", boom)
    with pytest.raises(BaseStoreIOError):
        scalar_base_store.set_bases("vm", "f", {"a": 1})


# --- format-version sidecar wiring ---------------------------------------


def _scalar_sidecar(state_dir: Path, profile: str) -> Path:
    return state_dir / "scalar-base" / profile / base_store_format.SIDECAR_NAME


def test_set_bases_stamps_format_version(state_dir: Path) -> None:
    scalar_base_store.set_bases("vm", "f", {"a.b": 1})
    assert (
        _scalar_sidecar(state_dir, "vm").read_text(encoding="utf-8").strip()
        == base_store_format.BASE_STORE_FORMAT_VERSION
    )


def test_set_base_inherits_stamp(state_dir: Path) -> None:
    scalar_base_store.set_base("vm", "f", "a.b", 1)
    assert _scalar_sidecar(state_dir, "vm").exists()


def test_re_baseline_inherits_stamp(state_dir: Path) -> None:
    scalar_base_store.re_baseline("vm", "f", "a.b", 1)
    assert _scalar_sidecar(state_dir, "vm").exists()


def test_get_base_refuses_future_format_before_json_load(state_dir: Path) -> None:
    # Manifest written, then sidecar bumped to a future version: the
    # version check must fire before json.loads, refusing the read.
    scalar_base_store.set_base("vm", "f", "a.b", 1)
    _scalar_sidecar(state_dir, "vm").write_text("2.0\n", encoding="utf-8")
    with pytest.raises(BaseStoreSchemaError):
        scalar_base_store.get_base("vm", "f", "a.b")


def test_version_check_precedes_corrupt_json(state_dir: Path) -> None:
    # Both the sidecar AND the manifest are bad. The schema refusal must
    # win — the version check runs before json.loads sees the corruption.
    scalar_base_store.set_base("vm", "f", "a.b", 1)
    _scalar_sidecar(state_dir, "vm").write_text("2.0\n", encoding="utf-8")
    _manifest_path(state_dir, "vm", "f").write_text("{ not json", encoding="utf-8")
    with pytest.raises(BaseStoreSchemaError):
        scalar_base_store.get_base("vm", "f", "a.b")


def test_get_base_grandfathers_legacy_manifest(state_dir: Path) -> None:
    # Legacy manifest present, no sidecar -> read succeeds (grandfathered).
    path = _manifest_path(state_dir, "vm", "f")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"a.b": {"present": true, "value": 7}}', encoding="utf-8")
    assert not _scalar_sidecar(state_dir, "vm").exists()
    assert scalar_base_store.get_base("vm", "f", "a.b") == 7


def test_grandfather_then_stamp_on_next_set(state_dir: Path) -> None:
    path = _manifest_path(state_dir, "vm", "f")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"a.b": {"present": true, "value": 7}}', encoding="utf-8")
    assert scalar_base_store.get_base("vm", "f", "a.b") == 7
    scalar_base_store.set_base("vm", "f", "a.b", 9)
    assert (
        _scalar_sidecar(state_dir, "vm").read_text(encoding="utf-8").strip()
        == base_store_format.BASE_STORE_FORMAT_VERSION
    )
