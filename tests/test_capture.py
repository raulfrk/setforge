"""Tests for capture (live → tracked)."""

from pathlib import Path

import pytest

from setforge.capture import CaptureAction, CaptureResult
from setforge.config import Config, Profile, TrackedFile, resolve_profile
from setforge.errors import InvariantViolation
from tests.verb_calls import capture_profile, preview_capture_profile


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _file_config(name: str, dst: Path) -> Config:
    return Config(
        tracked_files={name: TrackedFile(src=Path(name), dst=str(dst))},
        profiles={"p": Profile(tracked_files=[name])},
    )


def _capture_file(src: Path, dst: Path) -> CaptureResult:
    """Capture the one unstaged file whose source is ``<repo>/tracked/<name>``."""
    (result,) = capture_profile(_file_config(src.name, dst), "p", src.parent.parent)
    return result


def test_capture_plain_copy(tmp_path: Path) -> None:
    src = tmp_path / "tracked" / "src"
    dst = tmp_path / "dst"
    _write(dst, "live content\n")
    result = _capture_file(src, dst)
    assert result.action is CaptureAction.UPDATED
    assert src.read_text() == "live content\n"


def test_capture_noop_when_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "tracked" / "src"
    dst = tmp_path / "dst"
    _write(src, "same\n")
    _write(dst, "same\n")
    result = _capture_file(src, dst)
    assert result.action is CaptureAction.NOOP


def test_capture_skips_missing_dst(tmp_path: Path) -> None:
    src = tmp_path / "tracked" / "src"
    dst = tmp_path / "missing"
    result = _capture_file(src, dst)
    assert result.action is CaptureAction.SKIPPED
    assert not src.exists()


def test_capture_profile_iterates_tracked_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    src1 = repo / "tracked" / "x"
    src2 = repo / "tracked" / "y"
    dst1 = tmp_path / "live" / "x"
    dst2 = tmp_path / "live" / "y"
    _write(dst1, "x-live\n")
    _write(dst2, "y-live\n")

    config = Config(
        tracked_files={
            "x": TrackedFile(src=Path("x"), dst=str(dst1)),
            "y": TrackedFile(src=Path("y"), dst=str(dst2)),
        },
        profiles={"p": Profile(tracked_files=["x", "y"])},
    )
    results = capture_profile(config, "p", repo)
    assert {r.name for r in results} == {"x", "y"}
    assert all(r.action is CaptureAction.UPDATED for r in results)
    assert src1.read_text() == "x-live\n"
    assert src2.read_text() == "y-live\n"


# --------------------------------------------------------------------------- #
# A5 staged plain-file capture (only SHARED hunks promote into tracked/)
# --------------------------------------------------------------------------- #

_A5_BASE = b"## Tool prefs\nUse rg not grep.\n\n## Host paths\nworkdir: /home/generic\n"
_A5_LIVE = (
    b"## Tool prefs\nUse rg not grep.\n\n"
    b"## Shell\nPrefer zsh.\n\n"
    b"## Host paths\nworkdir: /home/raul\n"
)


def _stage_index(profile: str, fid, base: bytes, live: bytes, classes: dict) -> None:
    """Seed the reconcile base + a staged index for a plain file (simulate a
    prior install + `setforge stage`)."""
    from setforge import locking
    from setforge.reconcile import hunks as H
    from setforge.reconcile import store
    from setforge.reconcile.types import HunkClass

    staged = [
        H.Hunk(
            cls=classes.get(h.label, HunkClass.PENDING),
            label=h.label,
            live_hash=h.live_hash,
            unit_id=h.unit_id,
            base_span=h.base_span,
            live_span=h.live_span,
            legacy_anchor=h.legacy_anchor,
        )
        for h in H.extract_hunks(base, live)
    ]
    with locking.profile_lock(profile):
        store.record(
            profile,
            fid,
            base=base,
            local=live,
            staged=bool(classes),
            hunks=H.serialize(staged),
        )


def _a5_config(dst: Path) -> Config:
    return _file_config("CLAUDE.md", dst)


def test_binary_plain_capture_rejects_persisted_key_route_before_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge import locking
    from setforge.reconcile import store
    from setforge.reconcile.types import file_id

    src = tmp_path / "tracked" / "blob"
    dst = tmp_path / "live" / "blob"
    src.parent.mkdir(parents=True)
    dst.parent.mkdir(parents=True)
    src.write_bytes(b"tracked\x00")
    dst.write_bytes(b"live\xff")
    fid = file_id("blob")
    with locking.profile_lock("p"):
        store.record(
            "p",
            fid,
            base=b"base\xff",
            local=dst.read_bytes(),
            staged=True,
            hunks=[
                {
                    "kind": "key",
                    "cls": "local",
                    "label": "foreign",
                    "path": "foreign",
                    "value_hash": "sha256:value",
                }
            ],
        )

    with pytest.raises(InvariantViolation, match="current 'line' routing"):
        capture_profile(_file_config("blob", dst), "p", tmp_path)
    assert src.read_bytes() == b"tracked\x00"


def test_participating_plain_binary_fails_closed_before_wholesale_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge import locking
    from setforge.reconcile import store
    from setforge.reconcile.types import file_id

    src = tmp_path / "tracked" / "blob"
    dst = tmp_path / "live" / "blob"
    _write(src, "tracked before\n")
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"host-secret\xff")
    with locking.profile_lock("p"):
        store.record(
            "p",
            file_id("blob"),
            base=b"base\xff",
            local=dst.read_bytes(),
            staged=True,
            hunks=[],
        )

    with pytest.raises(InvariantViolation, match="not valid UTF-8"):
        capture_profile(_file_config("blob", dst), "p", tmp_path)
    assert src.read_bytes() == b"tracked before\n"


@pytest.mark.parametrize("filename", ["missing.md", "missing.json"])
def test_participating_plain_missing_live_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge import locking
    from setforge.reconcile import store
    from setforge.reconcile.types import file_id

    src = tmp_path / "tracked" / filename
    dst = tmp_path / "live" / filename
    _write(src, "tracked before\n")
    _write(dst, "prior live\n")
    with locking.profile_lock("p"):
        store.record(
            "p",
            file_id("missing"),
            base=b"base\n",
            local=b"prior live\n",
            staged=True,
            hunks=[],
        )

    dst.unlink()
    config = Config(
        tracked_files={"missing": TrackedFile(src=src, dst=str(dst))},
        profiles={"p": Profile(tracked_files=["missing"])},
    )
    with pytest.raises(InvariantViolation, match="has no live file"):
        preview_capture_profile(
            config, "p", tmp_path, resolved=resolve_profile(config, "p")
        )
    with pytest.raises(InvariantViolation, match="has no live file"):
        capture_profile(config, "p", tmp_path)
    assert src.read_bytes() == b"tracked before\n"


def test_staged_capture_promotes_only_shared_and_keeps_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge.reconcile import store
    from setforge.reconcile.types import HunkClass, file_id

    repo = tmp_path / "repo"
    src = repo / "tracked" / "CLAUDE.md"
    dst = tmp_path / "live" / "CLAUDE.md"
    _write(src, _A5_BASE.decode())  # tracked currently == upstream base
    _write(dst, _A5_LIVE.decode())  # live carries the host edits

    fid = file_id("CLAUDE.md")
    _stage_index(
        "p",
        fid,
        _A5_BASE,
        _A5_LIVE,
        {"## Shell": HunkClass.SHARED, "## Host paths": HunkClass.LOCAL},
    )

    capture_profile(_a5_config(dst), "p", repo)

    out = src.read_bytes()
    assert b"## Shell" in out  # SHARED promoted
    assert b"Prefer zsh." in out
    assert b"workdir: /home/generic" in out  # LOCAL kept base
    assert b"workdir: /home/raul" not in out  # LOCAL not leaked to tracked
    assert store.read_base("p", fid) == _A5_BASE  # base UNCHANGED on sync
    assert store.reconstruct("p", fid) == _A5_LIVE  # local holds full live (INV-2)
    store.verify("p")


def test_staged_capture_demote_uncaptures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge.reconcile.types import HunkClass, file_id

    repo = tmp_path / "repo"
    src = repo / "tracked" / "CLAUDE.md"
    dst = tmp_path / "live" / "CLAUDE.md"
    # tracked already carries the shared Shell block (from a prior sync).
    _write(
        src,
        _A5_LIVE.replace(b"workdir: /home/raul", b"workdir: /home/generic").decode(),
    )
    _write(dst, _A5_LIVE.decode())

    fid = file_id("CLAUDE.md")
    # the host re-stages Shell SHARED -> LOCAL (demote); base still lacks Shell.
    _stage_index("p", fid, _A5_BASE, _A5_LIVE, {"## Shell": HunkClass.LOCAL})

    capture_profile(_a5_config(dst), "p", repo)

    out = src.read_bytes()
    assert b"## Shell" not in out  # demote removed the shared bytes from tracked/
    assert out == _A5_BASE  # tracked back to pure upstream base


def test_staged_capture_keeps_host_local_section_out_of_tracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-retirement: a host-local section kept out of tracked WITHOUT the
    legacy local.yaml ``host_local_sections`` strip.

    STAGE B retires the local.yaml host-local declaration; ``sync`` no longer
    threads a ``host_local_sections_map`` into ``capture_profile``. A host-local
    section is instead a LOCAL unit in the reconcile store (a purely-additive
    ``## My Tweaks`` section carrying a minted ``reloc_anchor``). This pins that
    the reconcile-native staged capture keeps that LOCAL content host-only — it
    must NOT round-trip into the shared tracked source — proving the strip was
    redundant.
    """
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge.reconcile import store
    from setforge.reconcile.types import HunkClass, file_id

    base = b"## Alpha\naaa\n## Beta\nbbb\n"
    live = b"## Alpha\naaa\n## My Tweaks\nmy host-only line\n## Beta\nbbb\n"

    repo = tmp_path / "repo"
    src = repo / "tracked" / "CLAUDE.md"
    dst = tmp_path / "live" / "CLAUDE.md"
    _write(src, base.decode())
    _write(dst, live.decode())

    fid = file_id("CLAUDE.md")
    _stage_index("p", fid, base, live, {"## My Tweaks": HunkClass.LOCAL})

    capture_profile(_a5_config(dst), "p", repo)

    out = src.read_bytes()
    assert b"## My Tweaks" not in out
    assert b"my host-only line" not in out
    assert out == base
    store.verify("p")


def test_unstaged_file_falls_back_to_legacy_absorb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A plain reconcile file with a recorded base but NO staged hunks (all
    # PENDING) must keep the legacy sync behavior: live drift is absorbed into
    # tracked. This guards the opt-in-per-file fallback (a mutant flipping the
    # `not any(SHARED/LOCAL)` guard would let an unstaged file stop absorbing).
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge.reconcile import store
    from setforge.reconcile.types import file_id

    repo = tmp_path / "repo"
    src = repo / "tracked" / "CLAUDE.md"
    dst = tmp_path / "live" / "CLAUDE.md"
    _write(src, _A5_BASE.decode())
    _write(dst, _A5_LIVE.decode())

    fid = file_id("CLAUDE.md")
    _stage_index("p", fid, _A5_BASE, _A5_LIVE, {})  # base recorded, nothing staged

    capture_profile(_a5_config(dst), "p", repo)

    assert src.read_bytes() == _A5_LIVE  # legacy absorb: full live → tracked
    # the legacy plain-capture path does not touch the reconcile store — base is
    # left as recorded; it self-heals to the absorbed content on the next install.
    assert store.read_base("p", fid) == _A5_BASE


def test_git_backed_staged_capture_requires_container_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from setforge.errors import InvariantViolation
    from setforge.reconcile.types import HunkClass, file_id

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    src = repo / "tracked" / "CLAUDE.md"
    dst = tmp_path / "live" / "CLAUDE.md"
    _write(src, _A5_BASE.decode())
    _write(dst, _A5_LIVE.decode())
    _stage_index(
        "p",
        file_id("CLAUDE.md"),
        _A5_BASE,
        _A5_LIVE,
        {"## Shell": HunkClass.SHARED},
    )

    with pytest.raises(InvariantViolation, match="container ownership claim"):
        capture_profile(
            _a5_config(dst),
            "p",
            repo,
        )

    assert src.read_bytes() == _A5_BASE


def test_participating_file_with_invalidated_identity_never_wholesale_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge import locking
    from setforge.reconcile import store
    from setforge.reconcile.types import file_id

    base = b"before\nold\nafter\n"
    live = b"before\nHOST SECRET\nafter\n"
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    dst = tmp_path / "live" / "x"
    _write(src, base.decode())
    _write(dst, live.decode())
    with locking.profile_lock("p"):
        store.record(
            "p",
            file_id("x"),
            base=base,
            local=live,
            staged=True,
            hunks=[
                {
                    "kind": "line",
                    "cls": "shared",
                    "label": "removed identity",
                    "unit_id": "sha256:no-longer-present",
                    "live_hash": "sha256:old",
                }
            ],
        )
    config = Config(
        tracked_files={"x": TrackedFile(src=Path("x"), dst=str(dst))},
        profiles={"p": Profile(tracked_files=["x"])},
    )

    capture_profile(config, "p", repo)

    assert src.read_bytes() == base
    entry = store.read_index("p").files["x"]
    assert entry.staged is True
    assert entry.hunks[0]["cls"] == "pending"


def test_profile_preflight_prevents_earlier_write_when_later_participant_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge import base_store, locking
    from setforge.reconcile import store
    from setforge.reconcile.types import file_id

    repo = tmp_path / "repo"
    x_src = repo / "tracked" / "x"
    x_dst = tmp_path / "live" / "x"
    y_src = repo / "tracked" / "y"
    y_dst = tmp_path / "live" / "y"
    _write(x_src, "tracked-before\n")
    _write(x_dst, "would-be-captured\n")
    _write(y_src, "y-base\n")
    _write(y_dst, "y-live\n")
    with locking.profile_lock("p"):
        store.record(
            "p",
            file_id("y"),
            base=b"y-base\n",
            local=b"y-live\n",
            staged=True,
            hunks=[],
        )
    base_store.base_path("p", "y").unlink()
    config = Config(
        tracked_files={
            "x": TrackedFile(src=Path("x"), dst=str(x_dst)),
            "y": TrackedFile(src=Path("y"), dst=str(y_dst)),
        },
        profiles={"p": Profile(tracked_files=["x", "y"])},
    )

    with pytest.raises(InvariantViolation, match="no recorded reconciliation base"):
        capture_profile(config, "p", repo)

    assert x_src.read_bytes() == b"tracked-before\n"


@pytest.mark.parametrize("replacement", [b"Prefer fish.", b"Prefer zsh.  "])
def test_staged_capture_changed_shared_held_local_with_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: bytes
) -> None:
    # Editing a previously-SHARED hunk's content must NOT auto-promote the drift
    # into tracked/ (the security fix), AND must warn so the now-un-shared hunk
    # is not a silent surprise.
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge.reconcile import store
    from setforge.reconcile.types import HunkClass, file_id

    repo = tmp_path / "repo"
    src = repo / "tracked" / "CLAUDE.md"
    dst = tmp_path / "live" / "CLAUDE.md"
    drifted = _A5_LIVE.replace(b"Prefer zsh.", replacement)
    _write(src, _A5_BASE.decode())  # tracked currently == base
    _write(dst, drifted.decode())  # live's Shell content has drifted since staging

    fid = file_id("CLAUDE.md")
    # staged the ORIGINAL Shell content as SHARED (its live_hash != the drift's).
    _stage_index("p", fid, _A5_BASE, _A5_LIVE, {"## Shell": HunkClass.SHARED})

    config = _a5_config(dst)
    (preview,) = preview_capture_profile(
        config, "p", repo, resolved=resolve_profile(config, "p")
    )
    assert preview.action is CaptureAction.NOOP
    assert preview.store_update is True
    assert any("re-confirm" in warning for warning in preview.warnings)

    results = capture_profile(config, "p", repo)

    out = src.read_bytes()
    assert b"## Shell" not in out  # changed-SHARED held at base, not promoted
    assert replacement not in out  # the drifted bytes never reached tracked
    (result,) = [r for r in results if r.name == "CLAUDE.md"]
    assert any("re-confirm" in w for w in result.warnings)

    confirmed_hash = next(
        row["live_hash"]
        for row in store.read_index("p").files["CLAUDE.md"].hunks
        if row["label"] == "## Shell"
    )
    capture_profile(_a5_config(dst), "p", repo)
    assert src.read_bytes() == _A5_BASE
    assert (
        next(
            row["live_hash"]
            for row in store.read_index("p").files["CLAUDE.md"].hunks
            if row["label"] == "## Shell"
        )
        == confirmed_hash
    )


def test_staged_capture_changed_local_has_no_reconfirm_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only changed SHARED units need explicit re-confirmation."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge.reconcile.types import HunkClass, file_id

    repo = tmp_path / "repo"
    src = repo / "tracked" / "CLAUDE.md"
    dst = tmp_path / "live" / "CLAUDE.md"
    drifted = _A5_LIVE.replace(b"workdir: /home/raul", b"workdir: /srv/private")
    _write(src, _A5_BASE.decode())
    _write(dst, drifted.decode())
    _stage_index(
        "p",
        file_id("CLAUDE.md"),
        _A5_BASE,
        _A5_LIVE,
        {"## Host paths": HunkClass.LOCAL},
    )

    config = _a5_config(dst)
    (preview,) = preview_capture_profile(
        config, "p", repo, resolved=resolve_profile(config, "p")
    )
    assert not any("re-confirm" in warning for warning in preview.warnings)
    (result,) = capture_profile(config, "p", repo)
    assert not any("re-confirm" in warning for warning in result.warnings)
    assert src.read_bytes() == _A5_BASE


def test_staged_capture_pending_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge.reconcile.types import HunkClass, file_id

    repo = tmp_path / "repo"
    src = repo / "tracked" / "CLAUDE.md"
    dst = tmp_path / "live" / "CLAUDE.md"
    _write(src, _A5_BASE.decode())
    _write(dst, _A5_LIVE.decode())

    fid = file_id("CLAUDE.md")
    # The file IS under staging (Shell SHARED) but workdir is left PENDING.
    _stage_index("p", fid, _A5_BASE, _A5_LIVE, {"## Shell": HunkClass.SHARED})

    results = capture_profile(_a5_config(dst), "p", repo)

    out = src.read_bytes()
    assert b"## Shell" in out  # SHARED promoted
    assert b"workdir: /home/raul" not in out  # PENDING workdir kept host-only
    (result,) = [r for r in results if r.name == "CLAUDE.md"]
    assert any("stage" in w for w in result.warnings)  # the "run setforge stage" hint


# --------------------------------------------------------------------------- #
# A5b staged STRUCTURED (YAML/JSON) capture — only SHARED keys promote (INV-8)
# --------------------------------------------------------------------------- #

_SY_BASE = b"theme: dark\nworkdir: /home/generic\n"
_SY_LIVE = b"theme: light\nworkdir: /home/raul\n"


def _stage_structured_index(
    profile: str, fid, base: bytes, live: bytes, classes: dict
) -> None:
    """Seed the reconcile base + a staged KEY-unit index (simulate install + stage)."""
    from dataclasses import replace

    from setforge import locking
    from setforge.reconcile import store
    from setforge.reconcile import structured_units as su
    from setforge.reconcile.types import HunkClass

    fresh = su.extract_structured_units(base, live, su.StructuredFormat.YAML)
    staged = [replace(u, cls=classes.get(u.path, HunkClass.PENDING)) for u in fresh]
    with locking.profile_lock(profile):
        store.record(
            profile,
            fid,
            base=base,
            local=live,
            staged=bool(classes),
            hunks=su.serialize_structured(staged),
        )


def _sy_config(dst: Path) -> Config:
    return _file_config("settings.yaml", dst)


def test_participating_structured_parse_failure_never_wholesale_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge import locking
    from setforge.reconcile import store
    from setforge.reconcile.types import file_id

    src = tmp_path / "tracked" / "settings.yaml"
    dst = tmp_path / "live" / "settings.yaml"
    _write(src, "theme: dark\n")
    _write(dst, "theme: [not valid\n")
    with locking.profile_lock("p"):
        store.record(
            "p",
            file_id("settings.yaml"),
            base=b"theme: dark\n",
            local=dst.read_bytes(),
            staged=True,
            hunks=[],
        )

    with pytest.raises(InvariantViolation, match="cannot be parsed as yaml"):
        capture_profile(_sy_config(dst), "p", tmp_path)
    assert src.read_bytes() == b"theme: dark\n"


def test_staged_capture_structured_promotes_only_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a SHARED key promotes into tracked/; a LOCAL key stays at base value."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge.reconcile import store
    from setforge.reconcile.types import HunkClass, file_id

    repo = tmp_path / "repo"
    src = repo / "tracked" / "settings.yaml"
    dst = tmp_path / "live" / "settings.yaml"
    _write(src, _SY_BASE.decode())  # tracked == upstream base
    _write(dst, _SY_LIVE.decode())  # live carries host edits

    fid = file_id("settings.yaml")
    _stage_structured_index(
        "p",
        fid,
        _SY_BASE,
        _SY_LIVE,
        {"theme": HunkClass.SHARED, "workdir": HunkClass.LOCAL},
    )

    capture_profile(_sy_config(dst), "p", repo)

    out = src.read_bytes()
    assert b"theme: light" in out  # SHARED key promoted
    assert b"workdir: /home/generic" in out  # LOCAL key kept base value
    assert b"/home/raul" not in out  # LOCAL host value not leaked to tracked
    assert store.read_base("p", fid) == _SY_BASE  # base UNCHANGED on sync
    assert store.reconstruct("p", fid) == _SY_LIVE  # local holds full live (INV-2)
    store.verify("p")


def test_staged_capture_structured_demote_uncaptures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INV-8: a SHARED→LOCAL demote removes the key's value from tracked/."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge.reconcile.types import HunkClass, file_id

    repo = tmp_path / "repo"
    src = repo / "tracked" / "settings.yaml"
    dst = tmp_path / "live" / "settings.yaml"
    # tracked already carries the shared theme (from a prior sync).
    _write(src, b"theme: light\nworkdir: /home/generic\n".decode())
    _write(dst, _SY_LIVE.decode())

    fid = file_id("settings.yaml")
    # host demotes theme SHARED -> LOCAL; base still carries the upstream value.
    _stage_structured_index("p", fid, _SY_BASE, _SY_LIVE, {"theme": HunkClass.LOCAL})

    capture_profile(_sy_config(dst), "p", repo)

    out = src.read_bytes()
    assert b"theme: dark" in out  # demote restored the base value in tracked/
    assert b"theme: light" not in out  # the host's shared value is gone
    assert out == _SY_BASE  # tracked back to pure upstream base


def test_changed_structured_shared_stays_held_across_two_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge.reconcile import store
    from setforge.reconcile.types import HunkClass, file_id

    repo = tmp_path / "repo"
    src = repo / "tracked" / "settings.yaml"
    dst = tmp_path / "live" / "settings.yaml"
    drifted = _SY_LIVE.replace(b"theme: light", b"theme: solarized")
    _write(src, _SY_BASE.decode())
    _write(dst, drifted.decode())
    _stage_structured_index(
        "p",
        file_id("settings.yaml"),
        _SY_BASE,
        _SY_LIVE,
        {"theme": HunkClass.SHARED},
    )

    before = next(
        row["value_hash"]
        for row in store.read_index("p").files["settings.yaml"].hunks
        if row["path"] == "theme"
    )
    for _ in range(2):
        capture_profile(_sy_config(dst), "p", repo)
        assert src.read_bytes() == _SY_BASE
        assert (
            next(
                row["value_hash"]
                for row in store.read_index("p").files["settings.yaml"].hunks
                if row["path"] == "theme"
            )
            == before
        )


def test_changed_structured_local_has_no_reconfirm_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed LOCAL key stays host-only without a SHARED warning."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge.reconcile.types import HunkClass, file_id

    repo = tmp_path / "repo"
    src = repo / "tracked" / "settings.yaml"
    dst = tmp_path / "live" / "settings.yaml"
    drifted = _SY_LIVE.replace(b"/home/raul", b"/srv/private")
    _write(src, _SY_BASE.decode())
    _write(dst, drifted.decode())
    _stage_structured_index(
        "p",
        file_id("settings.yaml"),
        _SY_BASE,
        _SY_LIVE,
        {"workdir": HunkClass.LOCAL},
    )

    config = _sy_config(dst)
    (preview,) = preview_capture_profile(
        config, "p", repo, resolved=resolve_profile(config, "p")
    )
    assert not any("re-confirm" in warning for warning in preview.warnings)
    (result,) = capture_profile(config, "p", repo)
    assert not any("re-confirm" in warning for warning in result.warnings)
    assert src.read_bytes() == _SY_BASE


def test_structured_capture_plan_carries_the_legacy_draft_rebind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan reports, and apply writes, a draft rebound to its canonical key."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge import locking
    from setforge.reconcile import store
    from setforge.reconcile import structured_units as su
    from setforge.reconcile.types import HunkClass, UnitRef, content_sha, file_id

    base = b'"name[]": old\n'
    live = b'"name[]": host\n'
    legacy = "name[]"
    draft = b"shared\n"
    (fresh,) = su.extract_structured_units(base, live, su.StructuredFormat.YAML)
    assert fresh.path != legacy
    repo = tmp_path / "repo"
    src = repo / "tracked" / "settings.yaml"
    dst = tmp_path / "live" / "settings.yaml"
    _write(src, base.decode())
    _write(dst, live.decode())
    fid = file_id("settings.yaml")
    with locking.profile_lock("p"):
        store.record(
            "p",
            fid,
            base=base,
            local=live,
            staged=True,
            hunks=[
                {
                    "kind": "key",
                    "cls": HunkClass.SHARED_DRAFTED.value,
                    "label": legacy,
                    "path": legacy,
                    "value_hash": fresh.value_hash,
                    "draft_hash": content_sha(draft),
                }
            ],
            drafts={UnitRef.key(legacy): draft},
        )

    config = _sy_config(dst)
    resolved = resolve_profile(config, "p")
    (preview,) = preview_capture_profile(config, "p", repo, resolved=resolved)
    assert preview.action is CaptureAction.UPDATED
    assert preview.store_update is True

    (result,) = capture_profile(config, "p", repo)

    assert result.action is CaptureAction.UPDATED
    assert src.read_bytes() == b'"name[]": shared\n'
    assert store.read_drafts("p", fid) == {UnitRef.key(fresh.path): draft}
    (settled,) = preview_capture_profile(config, "p", repo, resolved=resolved)
    assert settled.action is CaptureAction.NOOP
    assert settled.store_update is False
    store.verify("p")


def test_capture_keeps_crlf_bytes(tmp_path: Path) -> None:
    src = tmp_path / "tracked" / "src"
    dst = tmp_path / "dst"
    src.parent.mkdir()
    src.write_bytes(b"a\r\nb\r\n")
    dst.write_bytes(b"a\r\nB\r\n")
    result = _capture_file(src, dst)
    assert result.action is CaptureAction.UPDATED
    assert src.read_bytes() == b"a\r\nB\r\n"


def test_capture_line_ending_only_difference_is_an_update(tmp_path: Path) -> None:
    src = tmp_path / "tracked" / "src"
    dst = tmp_path / "dst"
    src.parent.mkdir()
    src.write_bytes(b"a\nb\n")
    dst.write_bytes(b"a\r\nb\r\n")
    assert _capture_file(src, dst).action is CaptureAction.UPDATED
    assert src.read_bytes() == b"a\r\nb\r\n"
    assert _capture_file(src, dst).action is CaptureAction.NOOP


def test_capture_copies_undecodable_bytes_verbatim(tmp_path: Path) -> None:
    payload = b"\x89PNG\r\n\x1a\n\x00caf\xe9\xff\n"
    src = tmp_path / "tracked" / "src"
    dst = tmp_path / "dst"
    dst.write_bytes(payload)
    assert _capture_file(src, dst).action is CaptureAction.UPDATED
    assert src.read_bytes() == payload
    assert _capture_file(src, dst).action is CaptureAction.NOOP


def test_capture_undecodable_file_is_written_byte_exact(tmp_path: Path) -> None:
    payload = b"caf\xe9\n"
    src = tmp_path / "tracked" / "src"
    dst = tmp_path / "dst"
    dst.write_bytes(payload)
    result = _capture_file(src, dst)
    assert result.action is CaptureAction.UPDATED
    assert src.read_bytes() == payload


def test_sync_refuses_staged_file_claimed_by_another_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staged units are not published from a container this checkout must take over."""
    import subprocess

    from typer.testing import CliRunner

    from setforge.cli import app
    from setforge.file_ownership import observe_file
    from setforge.locking import mutation_locks
    from setforge.ownership import OwnershipStore, load_or_create_owner_id
    from setforge.reconcile.types import HunkClass, file_id

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    other = tmp_path / "other-checkout"
    for checkout in (repo, other):
        checkout.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=checkout, check=True)
    load_or_create_owner_id(repo)
    foreign = load_or_create_owner_id(other)
    src = repo / "tracked" / "CLAUDE.md"
    dst = tmp_path / "live" / "CLAUDE.md"
    _write(src, _A5_BASE.decode())
    _write(dst, _A5_LIVE.decode())
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\ntracked_files:\n"
        f"  CLAUDE.md: {{src: CLAUDE.md, dst: {dst}}}\n"
        "profiles:\n  p: {tracked_files: [CLAUDE.md]}\n",
        encoding="utf-8",
    )
    _stage_index(
        "p",
        file_id("CLAUDE.md"),
        _A5_BASE,
        _A5_LIVE,
        {"## Shell": HunkClass.SHARED},
    )
    observation = observe_file(dst)
    with mutation_locks(resources=True):
        OwnershipStore().claim_locked(
            resource_id=observation.resource_id,
            owner_id=foreign,
            declaration_refs=("tracked_files.CLAUDE.md",),
            provenance=(),
            locator=str(dst),
            fingerprint=observation.fingerprint,
            expected_generation=None,
        )

    result = CliRunner().invoke(
        app, ["sync", "--profile=p", f"--config={config}", "--auto=use-live", "--yes"]
    )

    assert result.exit_code != 0
    assert "container ownership claim" in str(result.exception)
    assert src.read_bytes() == _A5_BASE
    claim = OwnershipStore().read(observation.resource_id)
    assert claim is not None
    assert claim.owner_id == foreign
