"""Tests for the A5 `setforge stage` command core (collect / walk / persist)."""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.cli.stage import QUIT, FileStage, _Quit, collect_stages, counts, walk
from setforge.config import Config, Profile, TrackedFile, resolve_profile
from setforge.reconcile.types import HunkClass, file_id

_BASE = b"## Tool prefs\nUse rg not grep.\n\n## Host paths\nworkdir: /home/generic\n"
_LIVE = (
    b"## Tool prefs\nUse rg not grep.\n\n"
    b"## Shell\nPrefer zsh.\n\n"
    b"## Host paths\nworkdir: /home/raul\n"
)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Path, str]:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge import locking
    from setforge.reconcile import store

    repo = tmp_path / "repo"
    src = repo / "tracked" / "CLAUDE.md"
    dst = tmp_path / "live" / "CLAUDE.md"
    _write(src, _BASE)
    _write(dst, _LIVE)
    # install-style: record the merge base + empty hunks for the file.
    with locking.profile_lock("p"):
        store.record("p", file_id("CLAUDE.md"), base=_BASE, local=_LIVE)
    cfg = Config(
        tracked_files={"CLAUDE.md": TrackedFile(src=Path("CLAUDE.md"), dst=str(dst))},
        profiles={"p": Profile(tracked_files=["CLAUDE.md"])},
    )
    return cfg, repo, "p"


def test_collect_classifies_unstaged_as_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_stages(cfg, resolved, repo, profile)
    assert {h.label for h in stage.hunks} == {"## Shell", "## Host paths"}
    assert all(h.cls is HunkClass.PENDING for h in stage.hunks)


def test_collect_skips_file_with_no_recorded_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    dst = tmp_path / "live" / "x"
    _write(dst, b"local only\n")
    cfg = Config(
        tracked_files={"x": TrackedFile(src=Path("x"), dst=str(dst))},
        profiles={"p": Profile(tracked_files=["x"])},
    )
    resolved = resolve_profile(cfg, "p")
    assert collect_stages(cfg, resolved, repo, "p") == []  # no base → not eligible


def test_collect_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    from setforge.reconcile import store

    resolved = resolve_profile(cfg, profile)
    index_path = store._index_path(profile)
    before = index_path.stat().st_mtime_ns
    collect_stages(cfg, resolved, repo, profile)  # the `--list` data path
    assert index_path.stat().st_mtime_ns == before  # wrote nothing


def test_walk_applies_choices_and_quits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.cli.stage import Decision

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_stages(cfg, resolved, repo, profile)

    # Share the first hunk, then quit before the second.
    decisions: list[Decision | None | _Quit] = [Decision(HunkClass.SHARED), QUIT]
    scripted = iter(decisions)
    result = walk(stage.hunks, lambda h, i, n: next(scripted))

    assert result.hunks[0].cls is HunkClass.SHARED
    assert result.hunks[1].cls is HunkClass.PENDING  # untouched (quit before it)


def test_walk_skip_leaves_class_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_stages(cfg, resolved, repo, profile)
    result = walk(stage.hunks, lambda h, i, n: None)  # skip every hunk
    assert all(h.cls is HunkClass.PENDING for h in result.hunks)


def test_apply_writes_classes_and_keeps_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.cli.stage import Decision, _apply
    from setforge.reconcile import store

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_stages(cfg, resolved, repo, profile)
    result = walk(
        stage.hunks,
        lambda h, i, n: (
            Decision(HunkClass.SHARED)
            if h.label == "## Shell"
            else Decision(HunkClass.LOCAL)
        ),
    )
    _apply(profile, stage, result)

    entry = store.read_index(profile).files["CLAUDE.md"]
    classes = {row["label"]: row["cls"] for row in entry.hunks}
    assert classes == {"## Shell": "shared", "## Host paths": "local"}
    assert store.read_base(profile, file_id("CLAUDE.md")) == _BASE  # base unchanged
    assert store.reconstruct(profile, file_id("CLAUDE.md")) == _LIVE  # full live


def test_apply_merges_concurrent_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Lost-update guard (D6): the walk reads/classifies at collect time outside
    # the lock; a concurrent sync that classifies a DIFFERENT hunk between collect
    # and persist must NOT be clobbered by the persist write. This is the A5c §9
    # "race test (proves D6)": the interleaving is scripted deterministically
    # (the concurrent writer commits between collect and the merging persist) so
    # the lost-update window is exercised without a flaky real-thread race.
    from dataclasses import replace

    from setforge import locking
    from setforge.cli.stage import Decision, _apply
    from setforge.reconcile import hunks as hunks_mod
    from setforge.reconcile import store

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_stages(cfg, resolved, repo, profile)  # both PENDING

    # host shares "## Shell" interactively, SKIPS "## Host paths".
    result = walk(
        stage.hunks,
        lambda h, i, n: Decision(HunkClass.SHARED) if h.label == "## Shell" else None,
    )
    # concurrent sync classifies "## Host paths" LOCAL and commits it first.
    concurrent = [
        replace(h, cls=HunkClass.LOCAL) if h.label == "## Host paths" else h
        for h in stage.hunks
    ]
    with locking.profile_lock(profile):
        store.record(
            profile,
            file_id("CLAUDE.md"),
            base=_BASE,
            local=_LIVE,
            hunks=hunks_mod.serialize(concurrent),
        )

    _apply(profile, stage, result)  # must merge, not clobber

    classes = {
        row["label"]: row["cls"]
        for row in store.read_index(profile).files["CLAUDE.md"].hunks
    }
    assert classes["## Shell"] == "shared"  # host's explicit choice won
    assert classes["## Host paths"] == "local"  # concurrent sync preserved (not reset)


def _shell_anchor(stage: FileStage) -> str:
    return next(h.anchor for h in stage.hunks if h.label == "## Shell")


def test_apply_keep_local_draft_stores_draft_and_keeps_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Keep-mine-local: tracked gets the shareable draft, the live file is UNCHANGED.
    from setforge.cli.stage import Decision, _apply
    from setforge.reconcile import store

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_stages(cfg, resolved, repo, profile)
    draft = b"## Shell\nPrefer a portable shell.\n\n"
    result = walk(
        stage.hunks,
        lambda h, i, n: (
            Decision(HunkClass.SHARED_DRAFTED, draft=draft)
            if h.label == "## Shell"
            else None
        ),
    )
    _apply(profile, stage, result)

    fid = file_id("CLAUDE.md")
    entry = store.read_index(profile).files["CLAUDE.md"]
    classes = {row["label"]: row["cls"] for row in entry.hunks}
    assert classes["## Shell"] == "shared_drafted"
    assert store.read_drafts(profile, fid) == {_shell_anchor(stage): draft}
    assert stage.dst.read_bytes() == _LIVE  # live file UNCHANGED (host keeps theirs)
    store.verify(profile, fid)  # manifest matches the SHARED_DRAFTED hunk


def test_apply_adopt_rewrites_live_to_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Adopt: the host's live region is rewritten to the draft (no divergence).
    from setforge.cli.stage import Decision, _apply
    from setforge.reconcile import store

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_stages(cfg, resolved, repo, profile)
    draft = b"## Shell\nPrefer a portable shell.\n\n"
    result = walk(
        stage.hunks,
        lambda h, i, n: (
            Decision(HunkClass.SHARED_DRAFTED, draft=draft, adopt=True)
            if h.label == "## Shell"
            else None
        ),
    )
    _apply(profile, stage, result)

    fid = file_id("CLAUDE.md")
    live_now = stage.dst.read_bytes()
    assert b"Prefer a portable shell." in live_now  # live rewritten to the draft
    assert b"Prefer zsh." not in live_now  # the host original is gone (adopted)
    classes = {
        row["label"]: row["cls"]
        for row in store.read_index(profile).files["CLAUDE.md"].hunks
    }
    assert classes["## Shell"] == "shared_drafted"
    store.verify(profile, fid)  # manifest still matches


def test_apply_writes_live_under_profile_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Adopt live write must land INSIDE the profile lock that guards the
    index record — one lock spanning write+record, mirroring install/sync/revert.

    Without the hoist the write happens before the lock is acquired, so a
    concurrent install/sync could land between the live write and the recorded
    classification. Records the lock enter/exit and the live write into one event
    log and asserts ``enter < write < exit``.
    """
    import contextlib
    from collections.abc import Iterator

    from setforge.cli import stage as stage_mod
    from setforge.cli.stage import Decision, _apply

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_stages(cfg, resolved, repo, profile)

    events: list[str] = []
    real_lock = stage_mod.profile_lock
    real_write = stage_mod.atomicio.atomic_write_bytes

    @contextlib.contextmanager
    def recording_lock(prof: str, timeout: float | None = None) -> Iterator[None]:
        events.append("enter")
        with real_lock(prof, timeout=timeout):
            try:
                yield
            finally:
                events.append("exit")

    def recording_write(path: Path, *args: object, **kwargs: object) -> None:
        # The store also writes index/base via atomicio; record only the live dst.
        if path == stage.dst:
            events.append("write")
        real_write(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(stage_mod, "profile_lock", recording_lock)
    monkeypatch.setattr(stage_mod.atomicio, "atomic_write_bytes", recording_write)

    draft = b"## Shell\nPrefer a portable shell.\n\n"
    result = walk(
        stage.hunks,
        lambda h, i, n: (
            Decision(HunkClass.SHARED_DRAFTED, draft=draft, adopt=True)
            if h.label == "## Shell"
            else None
        ),
    )
    _apply(profile, stage, result)

    assert events.count("write") == 1, f"expected one live write; got {events}"
    assert events.index("enter") < events.index("write") < events.index("exit"), (
        f"live write must be inside the profile lock; observed order: {events}"
    )


def test_counts_tallies_by_class() -> None:
    from setforge.reconcile.hunks import extract_hunks

    hunks = extract_hunks(_BASE, _LIVE)
    tally = counts(hunks)
    assert tally[HunkClass.PENDING] == 2
    assert tally[HunkClass.SHARED] == 0
    assert tally[HunkClass.LOCAL] == 0


def test_interactive_choice_strips_class_prefix_for_style(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_interactive_choice`` must strip the ``class:`` prefix ``pt_style``
    emits before ``Style.from_dict`` (which keys on bare role names).

    The guard is explicit in two halves: the raw ``pt_style`` mapping is
    ``class:``-prefixed and ``Style.from_dict`` rejects it outright, so the
    walk's style construction MUST strip the prefix or it raises
    ``AssertionError: 'class:accent'``. Every other unit test scripts the
    ``choose`` callback directly and never builds the real prompt_toolkit style,
    so without this guard only a full TTY (container e2e) covers it.
    """
    from prompt_toolkit.styles import Style

    from setforge.cli.stage import _interactive_choice
    from setforge.ui import THEME, pt_style

    # pt_style emits class:-prefixed keys, which Style.from_dict rejects.
    raw = pt_style(THEME)
    assert all(key.startswith("class:") for key in raw)
    with pytest.raises(AssertionError):
        Style.from_dict(raw)

    # So _interactive_choice must strip them — building the choose callback
    # constructs the Style eagerly and must NOT raise.
    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_stages(cfg, resolved, repo, profile)
    assert callable(_interactive_choice(stage))


def test_walk_scales_to_many_hunks_no_whole_file_degrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large multi-hunk plain file stages + reconstructs per-hunk at scale.

    A5/A5c §9 scale coverage. Staging is a 2-way *line* diff
    (:func:`extract_hunks` -> :func:`classify` -> :func:`reconstruct`); it never
    routes through the 3-way ``merge._body_merge``, so it is structurally
    independent of that path's ``_MAX_LINES`` (100k) whole-file-conflict degrade
    — ``extract_hunks`` carries no such ceiling. The merge-side degrade is owned
    by ``tests/reconcile/test_merge.py::test_max_lines_degrades_to_conflict``;
    here we pin the staging side: hundreds of independent edits decompose into
    hundreds of hunks (never collapsed into one whole-file unit), and a full
    share then a full demote both round-trip byte-exact.
    """
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge import locking
    from setforge.cli.stage import Decision, _apply
    from setforge.reconcile import hunks as hunks_mod
    from setforge.reconcile import store

    sections = 300
    base = "".join(f"## Section {i}\nbody {i}\n\n" for i in range(sections)).encode()
    # Edit every section's body so each becomes its own independent diff hunk.
    live = "".join(
        f"## Section {i}\nbody {i} EDITED\n\n" for i in range(sections)
    ).encode()

    repo = tmp_path / "repo"
    _write(repo / "tracked" / "notes.md", base)
    dst = tmp_path / "live" / "notes.md"
    _write(dst, live)
    with locking.profile_lock("p"):
        store.record("p", file_id("notes.md"), base=base, local=live)
    cfg = Config(
        tracked_files={"notes.md": TrackedFile(src=Path("notes.md"), dst=str(dst))},
        profiles={"p": Profile(tracked_files=["notes.md"])},
    )
    resolved = resolve_profile(cfg, "p")

    def tracked_promotion() -> bytes:
        # What `sync` would promote into tracked/: the hunk-granular reconstruct
        # over the freshly-collected classes (NOT store.reconstruct, which is the
        # storage identity = recorded local).
        (st,) = collect_stages(cfg, resolved, repo, "p")
        return hunks_mod.reconstruct(st.base, st.live, st.hunks, {})

    (stage,) = collect_stages(cfg, resolved, repo, "p")
    # Each edited section is its own hunk — NOT one whole-file conflict.
    assert len(stage.hunks) == sections

    # Share every hunk → tracked promotion takes the host's edits verbatim.
    _apply("p", stage, walk(stage.hunks, lambda h, i, n: Decision(HunkClass.SHARED)))
    assert tracked_promotion() == live

    # Re-walk demoting every hunk to LOCAL → tracked promotion falls back to base.
    (stage2,) = collect_stages(cfg, resolved, repo, "p")
    _apply("p", stage2, walk(stage2.hunks, lambda h, i, n: Decision(HunkClass.LOCAL)))
    assert tracked_promotion() == base
