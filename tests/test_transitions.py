"""Tests for the transitions module."""

import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from setforge.errors import InvalidTransitionRecord, RevertFailed, SetforgeError
from setforge.transitions import (
    ExtensionDelta,
    PluginDelta,
    TransitionCommand,
    TransitionListing,
    TransitionMeta,
    apply_patch_reverse,
    compute_patch,
    extension_delta_from_json,
    list_transitions,
    load_latest,
    load_meta,
    make_meta,
    now_utc,
    plugin_delta_from_json,
    resolve_transition_prefix,
    snapshot_paths,
    state_root,
    summarize_transition,
    transition_dirname,
    transitions_root,
    write_meta,
    write_transition,
)


def test_state_root_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SETFORGE_STATE_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/test")))
    assert state_root() == Path("/home/test/.local/state/setforge")


def test_state_root_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    assert state_root() == tmp_path
    assert transitions_root() == tmp_path / "transitions"


def test_transition_dirname_format() -> None:
    ts = datetime(2026, 5, 7, 12, 30, 45, tzinfo=UTC)
    assert transition_dirname(ts, "install", "vm-headless") == (
        "20260507T123045000000Z-install-vm-headless"
    )


def test_transition_dirname_sort_matches_time() -> None:
    """Lexicographic sort across dirnames must match chronological sort."""
    earlier = datetime(2026, 5, 7, 9, 0, 0, tzinfo=UTC)
    later = datetime(2026, 5, 7, 17, 0, 0, tzinfo=UTC)
    a = transition_dirname(earlier, "install", "vm-headless")
    b = transition_dirname(later, "install", "vm-headless")
    assert sorted([b, a]) == [a, b]


def test_transition_dirname_includes_microseconds() -> None:
    """Microsecond field renders as six digits between seconds and ``Z``."""
    ts = datetime(2026, 5, 8, 12, 7, 30, 123456, tzinfo=UTC)
    assert transition_dirname(ts, "install", "vm-headless").startswith(
        "20260508T120730123456Z-"
    )


def test_transition_dirname_zero_microseconds_zero_padded() -> None:
    """Zero microseconds must render as six padded zeros, not be omitted —
    that's what keeps lexicographic sort matching chronological sort across
    sub-second and whole-second timestamps."""
    ts = datetime(2026, 5, 8, 12, 7, 30, 0, tzinfo=UTC)
    assert transition_dirname(ts, "install", "vm-headless").startswith(
        "20260508T120730000000Z-"
    )


def test_two_writes_in_same_second_produce_distinct_dirnames() -> None:
    """Two timestamps in the same wall-clock second but different microseconds
    must produce distinct dirnames — this is the collision the format change
    eliminates (setforge-nen.16)."""
    a = datetime(2026, 5, 8, 12, 7, 30, 1, tzinfo=UTC)
    b = datetime(2026, 5, 8, 12, 7, 30, 2, tzinfo=UTC)
    assert transition_dirname(a, "install", "vmh") != transition_dirname(
        b, "install", "vmh"
    )


def test_now_utc_is_aware() -> None:
    ts = now_utc()
    assert ts.tzinfo is UTC


def test_ensure_state_dir_writable_creates_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.transitions import ensure_state_dir_writable

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "fresh"))
    ensure_state_dir_writable()
    assert (tmp_path / "fresh" / "transitions").is_dir()
    # No probe file should be left.
    assert not (tmp_path / "fresh" / "transitions" / ".setforge-write-probe").exists()


def test_ensure_state_dir_writable_raises_on_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.errors import SetforgeError
    from setforge.transitions import ensure_state_dir_writable

    target = tmp_path / "ro" / "transitions"
    target.mkdir(parents=True)
    target.chmod(0o500)  # read+execute only, no write
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "ro"))
    try:
        with pytest.raises(SetforgeError, match="not writable"):
            ensure_state_dir_writable()
    finally:
        target.chmod(0o700)  # restore for cleanup


def test_transition_command_values() -> None:
    """Closed set must round-trip through json as the bare string value."""
    assert TransitionCommand.INSTALL.value == "install"
    assert TransitionCommand.SYNC.value == "sync"
    assert TransitionCommand.REVERT.value == "revert"


def test_transition_meta_to_dict_iso_timestamp() -> None:
    ts = datetime(2026, 5, 7, 12, 30, 45, tzinfo=UTC)
    meta = TransitionMeta(
        command=TransitionCommand.INSTALL,
        profile="vm-headless",
        timestamp=ts,
        host="example",
        version="0.1.0",
    )
    payload = meta.to_dict()
    assert payload == {
        "command": "install",
        "profile": "vm-headless",
        "timestamp": "2026-05-07T12:30:45+00:00",
        "host": "example",
        "version": "0.1.0",
    }


def test_make_meta_uses_current_host_and_version() -> None:
    meta = make_meta(TransitionCommand.INSTALL, "vm-headless")
    assert meta.command is TransitionCommand.INSTALL
    assert meta.profile == "vm-headless"
    assert meta.host  # not empty
    assert meta.version  # not empty


def test_make_meta_source_sha_none_when_source_dir_omitted() -> None:
    """``source_dir`` defaulting to None must leave ``source_sha`` unset."""
    meta = make_meta(TransitionCommand.INSTALL, "vm-headless")
    assert meta.source_sha is None


def test_make_meta_source_sha_none_when_source_dir_is_not_git_repo(
    tmp_path: Path,
) -> None:
    """A non-git directory must not crash; ``source_sha`` stays None."""
    meta = make_meta(TransitionCommand.INSTALL, "vm-headless", source_dir=tmp_path)
    assert meta.source_sha is None


def test_make_meta_records_source_sha_when_source_dir_is_git_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When ``git rev-parse HEAD`` succeeds, ``source_sha`` is its stdout."""
    captured_args: list[list[str]] = []
    import subprocess as sp

    def fake_run(args: list[str], **kwargs: object) -> sp.CompletedProcess[str]:
        captured_args.append(list(args))
        return sp.CompletedProcess(
            args=args,
            returncode=0,
            stdout="1f37cb1abcdef1234567890abcdef1234567890ab\n",
            stderr="",
        )

    import setforge.transitions as transitions_mod

    monkeypatch.setattr(transitions_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(transitions_mod.shutil, "which", lambda name: "/usr/bin/git")
    meta = make_meta(TransitionCommand.INSTALL, "vmh", source_dir=tmp_path)
    assert meta.source_sha == "1f37cb1abcdef1234567890abcdef1234567890ab"
    assert any(part == "rev-parse" for part in captured_args[-1])
    assert any(part == "HEAD" for part in captured_args[-1])


def test_make_meta_source_sha_none_when_git_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If ``git`` is not on PATH, _git_head returns None without exception."""
    import setforge.transitions as transitions_mod

    monkeypatch.setattr(transitions_mod.shutil, "which", lambda name: None)
    meta = make_meta(TransitionCommand.INSTALL, "vmh", source_dir=tmp_path)
    assert meta.source_sha is None


def test_to_dict_omits_source_sha_when_none() -> None:
    """Old transitions must round-trip byte-identically: no ``source_sha`` key."""
    ts = datetime(2026, 5, 7, 12, 30, 45, tzinfo=UTC)
    meta = TransitionMeta(
        command=TransitionCommand.INSTALL,
        profile="vmh",
        timestamp=ts,
        host="h",
        version="0.1.0",
    )
    payload = meta.to_dict()
    assert "source_sha" not in payload
    assert payload == {
        "command": "install",
        "profile": "vmh",
        "timestamp": "2026-05-07T12:30:45+00:00",
        "host": "h",
        "version": "0.1.0",
    }


def test_to_dict_includes_source_sha_when_set() -> None:
    """When ``source_sha`` is populated, it must appear in the serialized dict."""
    ts = datetime(2026, 5, 7, 12, 30, 45, tzinfo=UTC)
    meta = TransitionMeta(
        command=TransitionCommand.INSTALL,
        profile="vmh",
        timestamp=ts,
        host="h",
        version="0.1.0",
        source_sha="abc123",
    )
    payload = meta.to_dict()
    assert payload["source_sha"] == "abc123"


def test_load_meta_old_record_without_source_sha_round_trips_to_none(
    tmp_path: Path,
) -> None:
    """meta.json written before setforge-xra8 must deserialize cleanly."""
    target = tmp_path / "20260507T120000000000Z-install-vmh"
    target.mkdir()
    # Hand-craft a pre-xra8 payload — no ``source_sha`` key.
    payload = {
        "command": "install",
        "profile": "vmh",
        "timestamp": "2026-05-07T12:00:00+00:00",
        "host": "h",
        "version": "0.1.0",
    }
    (target / "meta.json").write_text(json.dumps(payload), encoding="utf-8")

    meta = load_meta(target)
    assert meta.source_sha is None
    assert meta.command is TransitionCommand.INSTALL
    assert meta.profile == "vmh"


def test_load_meta_new_record_with_source_sha(tmp_path: Path) -> None:
    """meta.json carrying ``source_sha`` must round-trip the value."""
    target = tmp_path / "20260518T120000000000Z-install-vmh"
    write_meta(
        target,
        TransitionMeta(
            command=TransitionCommand.INSTALL,
            profile="vmh",
            timestamp=datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC),
            host="h",
            version="0.2.0",
            source_sha="deadbeef",
        ),
    )
    meta = load_meta(target)
    assert meta.source_sha == "deadbeef"


def test_load_meta_raises_on_missing_file(tmp_path: Path) -> None:
    """Missing meta.json must surface a clear InvalidTransitionRecord."""
    with pytest.raises(InvalidTransitionRecord, match=r"cannot read meta\.json"):
        load_meta(tmp_path / "nonexistent")


def test_write_meta_creates_dir_and_file(tmp_path: Path) -> None:
    target = tmp_path / "20260507T120000000000Z-install-vmh"
    meta = TransitionMeta(
        command=TransitionCommand.INSTALL,
        profile="vmh",
        timestamp=datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC),
        host="h",
        version="0.1.0",
    )
    write_meta(target, meta)
    payload = json.loads((target / "meta.json").read_text())
    assert payload["command"] == "install"
    assert payload["profile"] == "vmh"


def test_snapshot_paths_records_existing_and_missing(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    a.write_text("hello\n")
    b = tmp_path / "missing.txt"
    snap = snapshot_paths([a, b])
    assert snap == {a: "hello\n", b: None}


def test_compute_patch_empty_when_unchanged(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    snap = {a: "x\n"}
    assert compute_patch(snap, snap) == ""


def _root_relative(p: Path) -> str:
    """Mirror transitions._diff_path: leading ``/`` stripped for assertions."""
    s = str(p)
    return s.lstrip("/") if s.startswith("/") else s


def test_compute_patch_modified_file(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    pre = {a: "before\n"}
    post = {a: "after\n"}
    patch = compute_patch(pre, post)
    assert f"--- {_root_relative(a)}" in patch
    assert f"+++ {_root_relative(a)}" in patch
    assert "-before" in patch
    assert "+after" in patch


def test_compute_patch_new_file_uses_dev_null(tmp_path: Path) -> None:
    a = tmp_path / "new.txt"
    patch = compute_patch({a: None}, {a: "fresh\n"})
    assert "--- /dev/null" in patch
    assert f"+++ {_root_relative(a)}" in patch
    assert "+fresh" in patch


def test_compute_patch_deleted_file_uses_dev_null(tmp_path: Path) -> None:
    a = tmp_path / "gone.txt"
    patch = compute_patch({a: "old\n"}, {a: None})
    assert f"--- {_root_relative(a)}" in patch
    assert "+++ /dev/null" in patch
    assert "-old" in patch


def test_compute_patch_combines_multiple_files(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    pre = {a: "1\n", b: "2\n"}
    post = {a: "1\n", b: "X\n"}  # only b changed
    patch = compute_patch(pre, post)
    assert f"+++ {_root_relative(b)}" in patch
    assert f"+++ {_root_relative(a)}" not in patch  # unchanged file omitted


def test_compute_patch_paths_are_root_relative_for_patch_safety(
    tmp_path: Path,
) -> None:
    """GNU patch rejects absolute paths as dangerous; we strip the
    leading slash and pair with `patch -d /` on apply."""
    a = tmp_path / "a.txt"
    patch = compute_patch({a: "x\n"}, {a: "y\n"})
    assert f"--- {str(a)[0]}" not in patch.split("\n")[0] or not patch.startswith(
        "--- /"
    )
    assert "--- /tmp" not in patch  # no leading slash on real paths


def _make_meta(
    command: TransitionCommand = TransitionCommand.INSTALL,
) -> TransitionMeta:
    return TransitionMeta(
        command=command,
        profile="vmh",
        timestamp=datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC),
        host="h",
        version="0.1.0",
    )


def test_extension_delta_is_empty() -> None:
    assert ExtensionDelta(added=[], removed=[]).is_empty() is True
    assert ExtensionDelta(added=["a.x"], removed=[]).is_empty() is False
    assert ExtensionDelta(added=[], removed=["b.y"]).is_empty() is False


def test_write_transition_full_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    target_file = tmp_path / "live.txt"
    pre = {target_file: "before\n"}
    post = {target_file: "after\n"}
    delta = ExtensionDelta(added=["a.x"], removed=["b.y"])

    out = write_transition(_make_meta(), pre, post, delta)

    assert out.exists()
    assert (out / "meta.json").exists()
    assert (out / "changes.patch").exists()
    assert (out / "extensions.json").exists()
    assert "before" in (out / "changes.patch").read_text()
    payload = json.loads((out / "extensions.json").read_text())
    assert payload == {"added": ["a.x"], "removed": ["b.y"]}
    # meta.json now records touched paths so revert can read them
    # without re-parsing the diff.
    meta_payload = json.loads((out / "meta.json").read_text())
    assert meta_payload["paths"] == [str(target_file)]


def test_write_transition_meta_paths_omits_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path with identical pre/post is unchanged and should NOT appear
    in meta.json's `paths` list."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    a = tmp_path / "changed.txt"
    b = tmp_path / "unchanged.txt"
    out = write_transition(
        _make_meta(),
        {a: "before\n", b: "same\n"},
        {a: "after\n", b: "same\n"},
        None,
    )
    meta_payload = json.loads((out / "meta.json").read_text())
    assert meta_payload["paths"] == [str(a)]


def test_write_transition_omits_empty_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    same = {tmp_path / "x": "same\n"}
    out = write_transition(
        _make_meta(), same, same, ExtensionDelta(added=["a.x"], removed=[])
    )
    assert (out / "meta.json").exists()
    assert not (out / "changes.patch").exists()
    assert (out / "extensions.json").exists()


def test_write_transition_omits_empty_extension_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    target_file = tmp_path / "live.txt"
    out = write_transition(
        _make_meta(),
        {target_file: "a\n"},
        {target_file: "b\n"},
        ExtensionDelta(added=[], removed=[]),
    )
    assert (out / "changes.patch").exists()
    assert not (out / "extensions.json").exists()


def test_write_transition_omits_extension_delta_when_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    out = write_transition(
        _make_meta(),
        {tmp_path / "x": "a\n"},
        {tmp_path / "x": "b\n"},
        None,
    )
    assert not (out / "extensions.json").exists()


def test_write_transition_rejects_non_str_marketplace_source_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``marketplaces_removed`` source dicts must contain only str values.

    Locks in the JSON-primitive contract documented on
    :class:`PluginDelta`. A caller that bypasses
    ``MarketplaceSource.model_dump(mode="json")`` and passes raw enum
    or :class:`pathlib.Path` values must hit a loud :class:`TypeError`
    in :func:`write_transition`, not an opaque ``json.dumps`` failure
    mid-serialization. Guards against a future trap (today's install
    path hard-codes ``()`` so the field is empty in practice).
    """
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    bad_delta = PluginDelta(
        installed=(),
        enabled=(),
        disabled=(),
        marketplaces_added=(),
        marketplaces_removed=(
            # Intentional bad-dict value to exercise runtime str-only validation.
            ("evil-mp", {"source": "github", "path": Path("/tmp/foo")}),  # type: ignore[dict-item]
        ),
    )
    with pytest.raises(TypeError, match="non-str value for key 'path'"):
        write_transition(
            _make_meta(),
            {tmp_path / "x": "a\n"},
            {tmp_path / "x": "a\n"},
            None,
            plugin_delta=bad_delta,
        )


def test_load_latest_returns_none_when_root_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "ghost"))
    assert load_latest("vmh") is None


def _stub_transition(target: Path, profile: str, *, command: str = "install") -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / "meta.json").write_text(
        json.dumps({"profile": profile, "command": command}),
        encoding="utf-8",
    )


def test_load_latest_returns_none_when_no_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    _stub_transition(
        tmp_path / "transitions" / "20260507T120000000000Z-install-other",
        profile="other",
    )
    assert load_latest("vmh") is None


def test_load_latest_picks_most_recent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    older = root / "20260507T090000000000Z-install-vmh"
    newer = root / "20260507T170000000000Z-install-vmh"
    _stub_transition(older, profile="vmh")
    _stub_transition(newer, profile="vmh")
    assert load_latest("vmh") == newer


def test_load_latest_does_not_match_profile_substring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`endswith('-vmh')` would match 'vm-headless-vmh'; the meta.json
    profile field must be an exact match."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    # Note dirname suffix is '-headless' but meta.json says 'vm-headless'.
    decoy = root / "20260507T120000000000Z-install-vm-headless"
    _stub_transition(decoy, profile="vm-headless")
    # Looking for 'headless' must NOT pick up vm-headless.
    assert load_latest("headless") is None


def test_load_latest_filters_by_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``command=`` kwarg restricts candidates to that TransitionCommand value.

    Regression for the setforge-xra8 last-install line: when a SYNC or
    REVERT lands AFTER an INSTALL, ``load_latest(profile)`` returns the
    sync/revert dir (the unconditional "latest"). Status's
    "last install:" row needs the latest INSTALL specifically — passing
    ``command=TransitionCommand.INSTALL`` filters the candidate set so
    the later non-install transitions are ignored.
    """
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    older_install = root / "20260507T090000000000Z-install-vmh"
    newer_sync = root / "20260507T170000000000Z-sync-vmh"
    _stub_transition(older_install, profile="vmh", command="install")
    _stub_transition(newer_sync, profile="vmh", command="sync")

    # Unfiltered: the sync wins (it's chronologically newer).
    assert load_latest("vmh") == newer_sync
    # Filtered to INSTALL: the install wins (sync is dropped from the set).
    assert (
        load_latest("vmh", command=TransitionCommand.INSTALL) == older_install
    )
    # Filtered to a command with no matching record: None.
    assert load_latest("vmh", command=TransitionCommand.REVERT) is None


def test_apply_patch_reverse_no_patch_is_noop(tmp_path: Path) -> None:
    apply_patch_reverse(tmp_path)  # no changes.patch → silent no-op


@pytest.mark.skipif(shutil.which("patch") is None, reason="GNU patch not on PATH")
def test_apply_patch_reverse_round_trips(tmp_path: Path) -> None:
    """Forward content edit, then apply_patch_reverse restores original."""
    target = tmp_path / "live.txt"
    target.write_text("after\n", encoding="utf-8")
    transition = tmp_path / "transition"
    transition.mkdir()
    (transition / "changes.patch").write_text(
        compute_patch({target: "before\n"}, {target: "after\n"}),
        encoding="utf-8",
    )

    apply_patch_reverse(transition)

    assert target.read_text() == "before\n"


@pytest.mark.skipif(shutil.which("patch") is None, reason="GNU patch not on PATH")
def test_apply_patch_reverse_raises_on_drift(tmp_path: Path) -> None:
    target = tmp_path / "live.txt"
    target.write_text("drifted-content\n", encoding="utf-8")
    transition = tmp_path / "transition"
    transition.mkdir()
    (transition / "changes.patch").write_text(
        compute_patch({target: "before\n"}, {target: "after\n"}),
        encoding="utf-8",
    )

    with pytest.raises(RevertFailed):
        apply_patch_reverse(transition)


@pytest.mark.skipif(shutil.which("patch") is None, reason="GNU patch not on PATH")
def test_apply_patch_reverse_atomic_on_multifile_drift(tmp_path: Path) -> None:
    """Multi-file diff with drift on one file: dry-run aborts before
    any file is written. The other (clean) file must remain at its
    post-state, and no .rej files must leak."""
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("after-a\n", encoding="utf-8")  # clean — would reverse OK
    b.write_text("DRIFTED-b\n", encoding="utf-8")  # drifted

    transition = tmp_path / "transition"
    transition.mkdir()
    (transition / "changes.patch").write_text(
        compute_patch(
            {a: "before-a\n", b: "before-b\n"},
            {a: "after-a\n", b: "after-b\n"},
        ),
        encoding="utf-8",
    )

    with pytest.raises(RevertFailed):
        apply_patch_reverse(transition)

    # No partial revert: a stays at post-state, b stays drifted.
    assert a.read_text() == "after-a\n"
    assert b.read_text() == "DRIFTED-b\n"
    # No .rej files anywhere in the tree.
    rej_files = list(tmp_path.rglob("*.rej"))
    assert rej_files == [], f"unexpected .rej files: {rej_files}"


def _stub_full_transition(
    target: Path,
    *,
    profile: str,
    command: str = "install",
    timestamp: str = "2026-05-07T12:00:00+00:00",
    paths: list[str] | None = None,
    extensions_added: list[str] | None = None,
    extensions_removed: list[str] | None = None,
    patch_text: str | None = None,
) -> None:
    """Write a self-consistent transition directory with optional sidecars.

    The dirname is left to the caller (encoded chronological order matters
    for sort tests). meta.json's `paths` field drives `file_count`, and the
    optional ``extensions.json`` sidecar drives `ext_count` — both flow
    through to TransitionListing.
    """
    target.mkdir(parents=True, exist_ok=True)
    meta: dict[str, str | list[str]] = {
        "command": command,
        "profile": profile,
        "timestamp": timestamp,
        "host": "h",
        "version": "0.1.0",
    }
    if paths is not None:
        meta["paths"] = paths
    (target / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if extensions_added is not None or extensions_removed is not None:
        (target / "extensions.json").write_text(
            json.dumps(
                {
                    "added": extensions_added or [],
                    "removed": extensions_removed or [],
                }
            ),
            encoding="utf-8",
        )
    if patch_text is not None:
        (target / "changes.patch").write_text(patch_text, encoding="utf-8")


def test_list_transitions_empty_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "ghost"))
    assert list_transitions() == []


def test_list_transitions_returns_chronological_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    _stub_full_transition(
        root / "20260507T090000000000Z-install-vmh",
        profile="vmh",
        timestamp="2026-05-07T09:00:00+00:00",
    )
    _stub_full_transition(
        root / "20260507T170000000000Z-sync-vmh",
        profile="vmh",
        command="sync",
        timestamp="2026-05-07T17:00:00+00:00",
    )

    listings = list_transitions()

    assert [entry.command for entry in listings] == ["install", "sync"]


def test_list_transitions_reverse_flips_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    _stub_full_transition(root / "20260507T090000000000Z-install-vmh", profile="vmh")
    _stub_full_transition(
        root / "20260507T170000000000Z-sync-vmh", profile="vmh", command="sync"
    )

    listings = list_transitions(reverse=True)

    assert [entry.command for entry in listings] == ["sync", "install"]


def test_list_transitions_profile_filter_or_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    _stub_full_transition(root / "20260507T090000000000Z-install-vmh", profile="vmh")
    _stub_full_transition(root / "20260507T100000000000Z-install-ws", profile="ws")
    _stub_full_transition(
        root / "20260507T110000000000Z-install-other", profile="other"
    )

    listings = list_transitions(profile_filter=["vmh", "ws"])

    assert {entry.profile for entry in listings} == {"vmh", "ws"}


def test_list_transitions_skips_corrupted_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half-written or unreadable dirs are silently skipped — graceful
    degradation matters here because partial writes are real (issue
    setforge-nen.16/.17 track atomic writes)."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    # No meta.json at all.
    (root / "20260507T080000000000Z-broken").mkdir()
    # Malformed JSON.
    bad = root / "20260507T090000000000Z-malformed-vmh"
    bad.mkdir()
    (bad / "meta.json").write_text("{not json", encoding="utf-8")
    # Valid.
    _stub_full_transition(root / "20260507T100000000000Z-install-vmh", profile="vmh")

    listings = list_transitions()

    assert len(listings) == 1
    assert listings[0].profile == "vmh"


def test_list_transitions_file_count_and_ext_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    _stub_full_transition(
        root / "20260507T090000000000Z-install-vmh",
        profile="vmh",
        paths=["/a", "/b", "/c"],
        extensions_added=["x.y", "z.w"],
        extensions_removed=["a.b"],
    )

    [entry] = list_transitions()

    assert entry.file_count == 3
    assert entry.ext_count == 3


def test_resolve_transition_prefix_exact_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    target = root / "20260507T120000000000Z-install-vmh"
    _stub_full_transition(target, profile="vmh")

    assert resolve_transition_prefix(target.name) == target


def test_resolve_transition_prefix_unique_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    target = root / "20260507T120000000000Z-install-vmh"
    _stub_full_transition(target, profile="vmh")

    assert resolve_transition_prefix("20260507T120") == target


def test_resolve_transition_prefix_zero_match_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    _stub_full_transition(root / "20260507T120000000000Z-install-vmh", profile="vmh")

    with pytest.raises(SetforgeError, match="no transition matching prefix"):
        resolve_transition_prefix("19990101")


def test_resolve_transition_prefix_ambiguous_lists_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    a = root / "20260507T120000000000Z-install-vmh"
    b = root / "20260507T130000000000Z-sync-vmh"
    _stub_full_transition(a, profile="vmh")
    _stub_full_transition(b, profile="vmh", command="sync")

    with pytest.raises(SetforgeError) as exc_info:
        resolve_transition_prefix("20260507T1")

    msg = str(exc_info.value)
    assert "matches 2 transitions" in msg
    assert a.name in msg
    assert b.name in msg


def test_resolve_transition_prefix_root_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No state dir at all → same not-found error path; don't crash on
    ``.iterdir()`` of a missing directory."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "ghost"))

    with pytest.raises(SetforgeError, match="no transition matching prefix"):
        resolve_transition_prefix("anything")


def test_summarize_transition_no_patch_returns_empty(tmp_path: Path) -> None:
    """Extension-only transitions have no changes.patch — summarize is a
    no-op for them, not an error."""
    transition = tmp_path / "extensions-only"
    transition.mkdir()
    assert summarize_transition(transition) == {}


def test_summarize_transition_classifies_each_action(tmp_path: Path) -> None:
    """One patch covering create / modify / delete in one call. Asserts
    the path round-trip too: leading-slash strip on write must be reversed
    when summarize reports back to the user."""
    created = Path("/tmp/test-summarize-created.txt")
    modified = Path("/tmp/test-summarize-modified.txt")
    deleted = Path("/tmp/test-summarize-deleted.txt")
    pre = {created: None, modified: "before\n", deleted: "old\n"}
    post = {created: "fresh\n", modified: "after\n", deleted: None}
    transition = tmp_path / "transition"
    transition.mkdir()
    (transition / "changes.patch").write_text(
        compute_patch(pre, post), encoding="utf-8"
    )

    actions = summarize_transition(transition)

    assert actions[str(created)] == "created"
    assert actions[str(modified)] == "modified"
    assert actions[str(deleted)] == "deleted"


def test_transition_listing_dataclass_is_frozen() -> None:
    """The listing struct is a value object — defending the frozen invariant
    so callers don't accidentally mutate cached entries."""
    listing = TransitionListing(
        directory=Path("/x"),
        timestamp=datetime(2026, 5, 7, tzinfo=UTC),
        command="install",
        profile="vmh",
        file_count=1,
        ext_count=0,
    )
    with pytest.raises(AttributeError):
        # Intentional read-only-property assignment to assert frozen behaviour.
        listing.command = "sync"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Atomic write (setforge-nen.17) — staging dir + os.rename commit marker
# ---------------------------------------------------------------------------


def _make_transition_args(
    tmp_path: Path,
) -> tuple[
    TransitionMeta, dict[Path, str | None], dict[Path, str | None], ExtensionDelta
]:
    """Return a minimal set of args for write_transition suitable for crash tests."""
    target_file = tmp_path / "live.txt"
    meta = TransitionMeta(
        command=TransitionCommand.INSTALL,
        profile="vmh",
        timestamp=datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC),
        host="h",
        version="0.1.0",
    )
    pre: dict[Path, str | None] = {target_file: "before\n"}
    post: dict[Path, str | None] = {target_file: "after\n"}
    delta = ExtensionDelta(added=["a.x"], removed=[])
    return meta, pre, post, delta


def test_write_transition_clean_run_no_pending_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clean run: target dir exists with meta.json; no .pending-* siblings remain."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    meta, pre, post, delta = _make_transition_args(tmp_path)

    out = write_transition(meta, pre, post, delta)

    assert out.exists()
    assert (out / "meta.json").exists()
    root = transitions_root()
    pending_siblings = [d for d in root.iterdir() if d.name.startswith(".pending-")]
    assert pending_siblings == [], f"unexpected .pending-* dirs: {pending_siblings}"


def test_write_transition_crash_before_rename_leaves_pending_not_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulated crash (os.rename raises) before rename:
    - No entry is visible to load_latest.
    - The orphan .pending-* dir exists on disk.
    """
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    meta, pre, post, delta = _make_transition_args(tmp_path)

    def _raise_on_rename(src: str | Path, dst: str | Path) -> None:
        raise SystemExit("simulated crash before rename")

    monkeypatch.setattr("setforge.transitions.os.rename", _raise_on_rename)

    with pytest.raises(SystemExit):
        write_transition(meta, pre, post, delta)

    # load_latest must not return anything — no committed transition.
    assert load_latest("vmh") is None

    # The .pending-* orphan must exist on disk.
    root = transitions_root()
    pending = [d for d in root.iterdir() if d.name.startswith(".pending-")]
    assert len(pending) == 1, f"expected exactly one .pending-* dir, got: {pending}"


def test_write_transition_crash_after_rename_before_meta_not_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulated crash (write_meta raises) after rename but before meta.json write:
    - load_latest returns None (no meta.json in the committed target dir).
    """
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    meta, pre, post, delta = _make_transition_args(tmp_path)

    def _raise_on_write_meta(
        transition_dir: Path,
        m: TransitionMeta,
        paths: list[Path] | None = None,
    ) -> None:
        raise SystemExit("simulated crash before meta.json")

    monkeypatch.setattr("setforge.transitions.write_meta", _raise_on_write_meta)

    with pytest.raises(SystemExit):
        write_transition(meta, pre, post, delta)

    assert load_latest("vmh") is None


def test_load_latest_sweeps_stale_pending_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .pending-* dir with mtime > 24h old is removed by load_latest."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()

    stale_pending = root / ".pending-20260507T120000000000Z-install-vmh"
    stale_pending.mkdir()
    past_ts = (datetime.now(UTC) - timedelta(hours=25)).timestamp()
    os.utime(stale_pending, (past_ts, past_ts))

    load_latest("vmh")  # should sweep the stale dir

    assert not stale_pending.exists(), "stale .pending-* dir should have been removed"


def test_load_latest_preserves_fresh_pending_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .pending-* dir with mtime < 24h is preserved (might be an in-flight write)."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()

    fresh_pending = root / ".pending-20260507T120000000000Z-install-vmh"
    fresh_pending.mkdir()
    recent_ts = (datetime.now(UTC) - timedelta(hours=1)).timestamp()
    os.utime(fresh_pending, (recent_ts, recent_ts))

    load_latest("vmh")  # must NOT remove the fresh dir

    assert fresh_pending.exists(), "fresh .pending-* dir must not be removed"


def test_load_latest_skips_pending_dirs_as_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .pending-* dir with a meta.json inside is NOT returned by load_latest
    (name guard must apply before the meta.json check)."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()

    # Fabricate a .pending-* dir that has a meta.json (shouldn't happen in
    # practice, but the name guard must still protect against it).
    pending = root / ".pending-20260507T120000000000Z-install-vmh"
    pending.mkdir()
    # Give it a recent mtime so the stale sweep won't remove it.
    recent_ts = (datetime.now(UTC) - timedelta(hours=1)).timestamp()
    os.utime(pending, (recent_ts, recent_ts))
    (pending / "meta.json").write_text(
        json.dumps({"profile": "vmh", "command": "install"}), encoding="utf-8"
    )

    assert load_latest("vmh") is None


def test_plugin_delta_from_json_round_trips_full_record() -> None:
    """A populated :class:`PluginDelta` survives ``json.dumps`` +
    ``json.loads`` + :func:`plugin_delta_from_json` unchanged.

    Locks in the on-disk JSON contract: every field round-trips, and
    ``marketplaces_removed`` tuples reassemble from the
    ``[name, source_dict]`` list pairs that :func:`write_transition`
    serializes.
    """
    original = PluginDelta(
        installed=("foo@mp", "bar@mp"),
        enabled=("baz@mp",),
        disabled=("qux@mp",),
        marketplaces_added=("mp-new",),
        marketplaces_removed=(
            ("mp-old", {"source": "github", "repo": "https://example.com/x.git"}),
        ),
    )
    on_disk_shape = {
        "installed": list(original.installed),
        "enabled": list(original.enabled),
        "disabled": list(original.disabled),
        "marketplaces_added": list(original.marketplaces_added),
        "marketplaces_removed": [
            [name, dict(src)] for name, src in original.marketplaces_removed
        ],
    }
    serialized = json.dumps(on_disk_shape)
    raw = json.loads(serialized)

    rebuilt = plugin_delta_from_json(raw)

    assert rebuilt == original


def test_plugin_delta_from_json_defaults_missing_fields_to_empty() -> None:
    """Missing fields default to empty tuples — matches the on-disk
    shape :func:`write_transition` produces for partial deltas."""
    rebuilt = plugin_delta_from_json({})

    assert rebuilt == PluginDelta(
        installed=(),
        enabled=(),
        disabled=(),
        marketplaces_added=(),
        marketplaces_removed=(),
    )


def test_extension_delta_from_json_round_trips_full_record() -> None:
    """An :class:`ExtensionDelta` survives ``json.dumps`` +
    ``json.loads`` + :func:`extension_delta_from_json` unchanged."""
    original = ExtensionDelta(
        added=["pub.ext-a", "pub.ext-b"],
        removed=["pub.ext-c"],
    )
    on_disk_shape = {"added": original.added, "removed": original.removed}
    raw = json.loads(json.dumps(on_disk_shape))

    rebuilt = extension_delta_from_json(raw)

    assert rebuilt == original


def test_extension_delta_from_json_defaults_missing_fields_to_empty() -> None:
    """Missing fields default to empty lists."""
    rebuilt = extension_delta_from_json({})

    assert rebuilt == ExtensionDelta(added=[], removed=[])


def test_plugin_delta_from_json_rejects_malformed_marketplaces_removed() -> None:
    """Shape-validate ``marketplaces_removed`` entries before constructing
    :class:`PluginDelta`. A corrupted plugins.json (hand-edit, partial
    write) raises :class:`InvalidTransitionRecord` at the JSON boundary
    so revert aborts cleanly via the ``SetforgeError`` handler instead
    of crashing mid-flight in
    :func:`_apply_marketplace_re_add`'s tuple unpack."""
    with pytest.raises(InvalidTransitionRecord, match="malformed"):
        plugin_delta_from_json({"marketplaces_removed": [["just-one-item"]]})
    with pytest.raises(InvalidTransitionRecord, match="wrong types"):
        plugin_delta_from_json({"marketplaces_removed": [["name", "not-a-dict"]]})
    with pytest.raises(InvalidTransitionRecord, match="wrong types"):
        plugin_delta_from_json({"marketplaces_removed": [[42, {}]]})


def test_plugin_delta_from_json_rejects_non_list_marketplaces_removed() -> None:
    """Top-level ``marketplaces_removed`` must be a list; a bare dict
    or string surfaces an :class:`InvalidTransitionRecord` instead of
    a downstream ``TypeError`` from the per-entry iteration."""
    with pytest.raises(InvalidTransitionRecord, match="must be a list"):
        plugin_delta_from_json({"marketplaces_removed": "bogus"})


def test_extension_delta_from_json_rejects_non_list_added() -> None:
    """Top-level ``added`` must be a list; a string surfaces an
    :class:`InvalidTransitionRecord` at the JSON boundary instead of a
    downstream ``TypeError`` from ``iter()``."""
    with pytest.raises(InvalidTransitionRecord, match="must be a list"):
        extension_delta_from_json({"added": "not-a-list", "removed": []})


def test_extension_delta_from_json_rejects_non_string_added_item() -> None:
    """Each ``added`` entry must be a string; a non-string item raises
    :class:`InvalidTransitionRecord` at the JSON boundary."""
    with pytest.raises(InvalidTransitionRecord, match="wrong type"):
        extension_delta_from_json({"added": [123], "removed": []})


def test_extension_delta_from_json_rejects_non_list_removed() -> None:
    """Top-level ``removed`` must be a list; a bare dict surfaces an
    :class:`InvalidTransitionRecord` at the JSON boundary."""
    with pytest.raises(InvalidTransitionRecord, match="must be a list"):
        extension_delta_from_json({"added": [], "removed": {"not": "a-list"}})


def test_extension_delta_from_json_rejects_non_string_removed_item() -> None:
    """Each ``removed`` entry must be a string; a ``None`` item raises
    :class:`InvalidTransitionRecord` at the JSON boundary."""
    with pytest.raises(InvalidTransitionRecord, match="wrong type"):
        extension_delta_from_json({"added": [], "removed": [None]})


def test_extension_delta_from_json_accepts_valid() -> None:
    """A valid record round-trips into :class:`ExtensionDelta` without
    raising."""
    rebuilt = extension_delta_from_json(
        {"added": ["ms-python.python"], "removed": ["ms-other.thing"]}
    )

    assert rebuilt == ExtensionDelta(
        added=["ms-python.python"], removed=["ms-other.thing"]
    )


# ---------------------------------------------------------------------------
# setforge-k0uj — ReconcileOutcome serialization + backward-compat
# ---------------------------------------------------------------------------


def test_reconcile_outcome_round_trips_through_write_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-empty ``reconcile_outcomes`` tuple lands at
    ``reconcile_outcomes.json`` next to ``plugins.json`` /
    ``extensions.json`` siblings, and :func:`load_reconcile_outcomes`
    decodes it back into the same tuple."""
    from setforge.transitions import (
        ReconcileKind,
        ReconcileOutcome,
        ReconcileStatus,
        load_reconcile_outcomes,
    )

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    meta = make_meta(TransitionCommand.INSTALL, "vmh")
    outcomes = (
        ReconcileOutcome(
            item_id="superpowers@official",
            kind=ReconcileKind.PLUGIN,
            status=ReconcileStatus.OK,
            error_summary=None,
        ),
        ReconcileOutcome(
            item_id="work-only-extension",
            kind=ReconcileKind.EXTENSION,
            status=ReconcileStatus.SKIPPED,
            error_summary="not found in registry",
        ),
    )
    target = write_transition(
        meta,
        {},
        {},
        ext_delta=None,
        plugin_delta=None,
        reconcile_outcomes=outcomes,
    )
    assert (target / "reconcile_outcomes.json").exists()
    rebuilt = load_reconcile_outcomes(target)
    assert rebuilt == outcomes


def test_load_reconcile_outcomes_missing_file_returns_empty_tuple(
    tmp_path: Path,
) -> None:
    """Backward-compat: a transition dir without ``reconcile_outcomes.json``
    (every install pre-setforge-k0uj) decodes to ``()`` — not an exception.

    This is the load-side anchor for the design's backward-compat
    guarantee. ``install --retry-failed`` against an old transition
    must observe an empty set of skipped ids, not crash."""
    from setforge.transitions import load_reconcile_outcomes

    # No reconcile_outcomes.json on disk — simulates a pre-setforge-k0uj
    # transition record.
    assert load_reconcile_outcomes(tmp_path) == ()


def test_write_transition_backward_compat_without_reconcile_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The setforge-k0uj backward-compat invariant: calling
    :func:`write_transition` WITHOUT the ``reconcile_outcomes`` kwarg
    (every callsite pre-setforge-k0uj) must NOT write the file, and
    :func:`load_reconcile_outcomes` on the result returns ``()``."""
    from setforge.transitions import load_reconcile_outcomes

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    meta = make_meta(TransitionCommand.INSTALL, "vmh")
    # Use the legacy call shape — no reconcile_outcomes kwarg.
    target = write_transition(
        meta,
        {},
        {},
        ext_delta=None,
        plugin_delta=None,
    )
    assert not (target / "reconcile_outcomes.json").exists()
    assert load_reconcile_outcomes(target) == ()


def test_write_transition_empty_reconcile_outcomes_skips_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit empty tuple is equivalent to omission — the file is
    NOT written. Mirrors the ``ExtensionDelta`` / ``PluginDelta``
    empty-skip pattern so empty installs don't accumulate empty
    sibling files in the transition dir."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    meta = make_meta(TransitionCommand.INSTALL, "vmh")
    target = write_transition(
        meta, {}, {}, ext_delta=None, plugin_delta=None, reconcile_outcomes=()
    )
    assert not (target / "reconcile_outcomes.json").exists()


def test_reconcile_outcomes_from_json_rejects_unknown_kind() -> None:
    """The ``kind`` Literal is closed: anything outside
    {plugin, extension} raises :class:`InvalidTransitionRecord`.

    Hand-edited or corrupted ``reconcile_outcomes.json`` surfaces a
    clean SetforgeError at the JSON boundary rather than a TypeError
    deep in the retry-filter path."""
    from setforge.transitions import reconcile_outcomes_from_json

    with pytest.raises(InvalidTransitionRecord, match="kind"):
        reconcile_outcomes_from_json(
            {
                "outcomes": [
                    {
                        "item_id": "x",
                        "kind": "bogus",
                        "status": "skipped",
                        "error_summary": None,
                    }
                ]
            }
        )


def test_reconcile_outcomes_from_json_rejects_unknown_status() -> None:
    """The ``status`` Literal is closed: anything outside the four
    documented values raises :class:`InvalidTransitionRecord`."""
    from setforge.transitions import reconcile_outcomes_from_json

    with pytest.raises(InvalidTransitionRecord, match="status"):
        reconcile_outcomes_from_json(
            {
                "outcomes": [
                    {
                        "item_id": "x",
                        "kind": "plugin",
                        "status": "halfway",
                        "error_summary": None,
                    }
                ]
            }
        )
