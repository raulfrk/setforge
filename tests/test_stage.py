"""Tests for the A5 `setforge stage` command core (collect / walk / persist)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from setforge.cli import stage as stage_mod
from setforge.cli.stage import QUIT, FileStage, _Quit, collect_stages, counts, walk
from setforge.config import Config, Profile, TrackedFile, resolve_profile
from setforge.errors import InvariantViolation
from setforge.reconcile.types import HunkClass, UnitRef, file_id

_BASE = b"## Tool prefs\nUse rg not grep.\n\n## Host paths\nworkdir: /home/generic\n"
_LIVE = (
    b"## Tool prefs\nUse rg not grep.\n\n"
    b"## Shell\nPrefer zsh.\n\n"
    b"## Host paths\nworkdir: /home/raul\n"
)


def test_lazy_interactive_seams_delegate(monkeypatch: pytest.MonkeyPatch) -> None:
    from prompt_toolkit.styles import Style

    from setforge.reconcile import _claude_ui, share_draft
    from setforge.ui import widgets

    sentinel = object()
    style = Style.from_dict({})
    draft_calls: list[str] = []

    def fake_hunk(*_args: object, **_kwargs: object) -> stage_mod.Cancelled:
        draft_calls.append("hunk")
        return stage_mod.CANCEL

    def fake_key(*_args: object, **_kwargs: object) -> stage_mod.Cancelled:
        draft_calls.append("key")
        return stage_mod.CANCEL

    monkeypatch.setattr(widgets, "button_bar", lambda *args, **kwargs: sentinel)
    monkeypatch.setattr(_claude_ui, "_themed_style", lambda: style)
    monkeypatch.setattr(share_draft, "draft_hunk", fake_hunk)
    monkeypatch.setattr(share_draft, "draft_key_unit", fake_key)

    assert stage_mod.button_bar([stage_mod.Button("choose", sentinel)]) is sentinel
    assert stage_mod._themed_style() is style
    stage_mod.share_draft.draft_hunk(b"x", display_path="x")
    stage_mod.share_draft.draft_key_unit(
        "x", display_path="x", fmt=stage_mod.StructuredFormat.YAML
    )
    assert draft_calls == ["hunk", "key"]


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


def test_collect_rejects_persisted_key_units_for_plain_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge import locking
    from setforge.reconcile import store

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    with locking.profile_lock(profile):
        store.record(
            profile,
            file_id("CLAUDE.md"),
            base=_BASE,
            local=_LIVE,
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
        collect_stages(cfg, resolve_profile(cfg, profile), repo, profile)


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
    assert result.decided_refs == {result.hunks[0].ref}


def test_walk_skip_leaves_class_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_stages(cfg, resolved, repo, profile)
    result = walk(stage.hunks, lambda h, i, n: None)  # skip every hunk
    assert all(h.cls is HunkClass.PENDING for h in result.hunks)
    assert result.decided_refs == set()


def test_render_list_human_reports_participation_and_actionable_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from dataclasses import replace

    from setforge.cli._output import OutputContext, OutputFormat
    from setforge.cli.stage import _render_list

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (first,) = collect_stages(cfg, resolved, repo, profile)
    shell = next(hunk for hunk in first.hunks if hunk.label == "## Shell")
    host = next(hunk for hunk in first.hunks if hunk.label == "## Host paths")
    changed = replace(
        first,
        participating=True,
        hunks=[
            replace(shell, cls=HunkClass.SHARED, changed=True),
            replace(host, cls=HunkClass.LOCAL, changed=True),
            replace(
                shell,
                cls=HunkClass.PENDING,
                label="new pending",
                unit_id="sha256:new-pending",
            ),
        ],
    )

    _render_list(OutputContext(OutputFormat.HUMAN), [changed])

    output = capsys.readouterr().out
    assert "participating=true" in output
    assert "0 shared-promotable" in output
    assert "0 drafted" in output
    assert "reconfirm-required" in output
    assert "1 local" in output
    assert "1 pending" in output
    assert "blocked:" in output
    assert "re-confirm" in output
    assert "to classify" in output


def test_render_list_json_changed_local_needs_no_reconfirm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from setforge.cli._output import OutputContext, OutputFormat
    from setforge.cli.stage import _render_list

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    (stage,) = collect_stages(cfg, resolve_profile(cfg, profile), repo, profile)
    local = replace(stage.hunks[0], cls=HunkClass.LOCAL, changed=True)

    _render_list(
        OutputContext(OutputFormat.JSON),
        [replace(stage, participating=True, hunks=[local])],
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    (row,) = payload["data"]
    assert row["shared"] == 0
    assert row["reconfirm_required"] == 0
    assert row["local"] == 1
    assert row["ownership"] == "adopt"
    assert row["blockers"] == ["container ownership: present, external, unowned"]


def test_same_class_reconfirm_refreshes_plain_confirmed_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.cli.stage import Decision, _apply
    from setforge.reconcile import store

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (first,) = collect_stages(cfg, resolved, repo, profile)
    _apply(
        profile,
        first,
        walk(
            first.hunks,
            lambda h, i, n: (
                Decision(HunkClass.SHARED) if h.label == "## Shell" else None
            ),
        ),
    )
    old_hash = next(
        row["live_hash"]
        for row in store.read_index(profile).files["CLAUDE.md"].hunks
        if row["label"] == "## Shell"
    )
    first.dst.write_bytes(_LIVE.replace(b"Prefer zsh.", b"Prefer fish."))
    (changed,) = collect_stages(cfg, resolved, repo, profile)
    shell = next(h for h in changed.hunks if h.label == "## Shell")
    assert shell.changed is True
    result = walk(
        changed.hunks,
        lambda h, i, n: Decision(HunkClass.SHARED) if h.label == "## Shell" else None,
    )
    assert shell.ref in result.decided_refs

    _apply(profile, changed, result)

    (confirmed,) = collect_stages(cfg, resolved, repo, profile)
    shell_after = next(h for h in confirmed.hunks if h.label == "## Shell")
    assert shell_after.changed is False
    assert shell_after.live_hash != old_hash
    assert shell_after.confirmed_hash == shell_after.live_hash


@pytest.mark.parametrize("choice", [None, QUIT], ids=["skip", "quit"])
def test_skip_or_quit_does_not_refresh_plain_confirmed_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    choice: None | _Quit,
) -> None:
    from setforge.cli.stage import Decision, _apply
    from setforge.reconcile import store

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (first,) = collect_stages(cfg, resolved, repo, profile)
    _apply(
        profile,
        first,
        walk(first.hunks, lambda h, i, n: Decision(HunkClass.SHARED)),
    )
    stored_before = {
        str(row["unit_id"]): row["live_hash"]
        for row in store.read_index(profile).files["CLAUDE.md"].hunks
    }
    first.dst.write_bytes(
        _LIVE.replace(b"Prefer zsh.", b"Prefer fish.").replace(
            b"/home/raul", b"/home/elsewhere"
        )
    )
    (changed,) = collect_stages(cfg, resolved, repo, profile)
    result = walk(changed.hunks, lambda h, i, n: choice)
    assert result.decided_refs == set()

    _apply(profile, changed, result)

    (again,) = collect_stages(cfg, resolved, repo, profile)
    assert all(h.changed for h in again.hunks)
    assert {
        str(row["unit_id"]): row["live_hash"]
        for row in store.read_index(profile).files["CLAUDE.md"].hunks
    } == stored_before


def test_deciding_other_plain_unit_does_not_refresh_changed_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.cli.stage import Decision, _apply

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (first,) = collect_stages(cfg, resolved, repo, profile)
    _apply(
        profile,
        first,
        walk(first.hunks, lambda h, i, n: Decision(HunkClass.SHARED)),
    )
    first.dst.write_bytes(
        _LIVE.replace(b"Prefer zsh.", b"Prefer fish.").replace(
            b"/home/raul", b"/home/elsewhere"
        )
    )
    (changed,) = collect_stages(cfg, resolved, repo, profile)
    _apply(
        profile,
        changed,
        walk(
            changed.hunks,
            lambda h, i, n: (
                Decision(HunkClass.SHARED) if h.label == "## Shell" else None
            ),
        ),
    )

    (again,) = collect_stages(cfg, resolved, repo, profile)
    by_label = {h.label: h for h in again.hunks}
    assert by_label["## Shell"].changed is False
    assert by_label["## Host paths"].changed is True


def test_plain_prompt_to_lock_live_race_refuses_stale_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.cli.stage import Decision, _apply
    from setforge.reconcile import store

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    (stage,) = collect_stages(cfg, resolve_profile(cfg, profile), repo, profile)
    assert stage.ownership is not None
    result = walk(
        stage.hunks,
        lambda h, i, n: Decision(HunkClass.SHARED) if h.label == "## Shell" else None,
    )
    stage.dst.write_bytes(_LIVE.replace(b"Prefer zsh.", b"Prefer fish."))

    with pytest.raises(InvariantViolation, match="changed after it was shown"):
        _apply(profile, stage, result)

    entry = store.read_index(profile).files["CLAUDE.md"]
    assert entry.staged is False
    assert entry.hunks == []


def test_plain_adopt_refuses_stale_recorded_base_before_live_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge import locking
    from setforge.cli.stage import Decision, _apply
    from setforge.reconcile import store

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    (stage,) = collect_stages(cfg, resolve_profile(cfg, profile), repo, profile)
    draft = b"## Shell\nPrefer a portable shell.\n\n"
    result = walk(
        stage.hunks,
        lambda h, i, n: (
            Decision(HunkClass.SHARED_DRAFTED, draft=draft, adopt=True)
            if h.label == "## Shell"
            else None
        ),
    )
    newer_base = _BASE.replace(b"Use rg not grep.", b"Use fd too.")
    with locking.profile_lock(profile):
        store.record(
            profile,
            file_id("CLAUDE.md"),
            base=newer_base,
            local=_LIVE,
        )
    before_live = stage.dst.read_bytes()
    before_index = store._index_path(profile).read_bytes()
    drafts_path = store._drafts_path(profile, file_id("CLAUDE.md"))
    assert not drafts_path.exists()

    with pytest.raises(InvariantViolation, match=r"recorded base.*changed"):
        _apply(profile, stage, result)

    assert stage.dst.read_bytes() == before_live
    assert store.read_base(profile, file_id("CLAUDE.md")) == newer_base
    assert store.read_local(profile, file_id("CLAUDE.md")) == _LIVE
    assert store._index_path(profile).read_bytes() == before_index
    assert not drafts_path.exists()


def test_adopt_corrupt_index_fails_before_live_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.cli.stage import Decision, _apply
    from setforge.errors import CorruptIndexError
    from setforge.reconcile import store

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    (stage,) = collect_stages(cfg, resolve_profile(cfg, profile), repo, profile)
    draft = b"## Shell\nPrefer a portable shell.\n\n"
    result = walk(
        stage.hunks,
        lambda h, i, n: (
            Decision(HunkClass.SHARED_DRAFTED, draft=draft, adopt=True)
            if h.label == "## Shell"
            else None
        ),
    )
    index_path = store._index_path(profile)
    index_path.write_text("{corrupt", encoding="utf-8")
    before_live = stage.dst.read_bytes()
    before_base = store.read_base(profile, file_id("CLAUDE.md"))
    drafts_path = store._drafts_path(profile, file_id("CLAUDE.md"))
    assert not drafts_path.exists()

    with pytest.raises(CorruptIndexError):
        _apply(profile, stage, result)

    assert stage.dst.read_bytes() == before_live
    assert store.read_base(profile, file_id("CLAUDE.md")) == before_base
    assert store.read_local(profile, file_id("CLAUDE.md")) == _LIVE
    assert index_path.read_text(encoding="utf-8") == "{corrupt"
    assert not drafts_path.exists()


def test_adopt_corrupt_drafts_fails_before_live_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.cli.stage import Decision, _apply
    from setforge.errors import ReconcileStoreError
    from setforge.reconcile import store

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    (stage,) = collect_stages(cfg, resolve_profile(cfg, profile), repo, profile)
    draft = b"## Shell\nPrefer a portable shell.\n\n"
    result = walk(
        stage.hunks,
        lambda h, i, n: (
            Decision(HunkClass.SHARED_DRAFTED, draft=draft, adopt=True)
            if h.label == "## Shell"
            else None
        ),
    )
    drafts_path = store._drafts_path(profile, file_id("CLAUDE.md"))
    drafts_path.parent.mkdir(parents=True, exist_ok=True)
    drafts_path.write_text("{corrupt", encoding="utf-8")
    before_live = stage.dst.read_bytes()
    before_base = store.read_base(profile, file_id("CLAUDE.md"))
    before_index = store._index_path(profile).read_bytes()

    with pytest.raises(ReconcileStoreError, match=r"drafts manifest.*corrupt"):
        _apply(profile, stage, result)

    assert stage.dst.read_bytes() == before_live
    assert store.read_base(profile, file_id("CLAUDE.md")) == before_base
    assert store.read_local(profile, file_id("CLAUDE.md")) == _LIVE
    assert store._index_path(profile).read_bytes() == before_index
    assert drafts_path.read_text(encoding="utf-8") == "{corrupt"


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
    assert entry.staged is True
    classes = {row["label"]: row["cls"] for row in entry.hunks}
    assert classes == {"## Shell": "shared", "## Host paths": "local"}
    assert store.read_base(profile, file_id("CLAUDE.md")) == _BASE  # base unchanged
    assert store.reconstruct(profile, file_id("CLAUDE.md")) == _LIVE  # full live


def test_apply_adopts_container_without_rewriting_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import uuid4

    from setforge.cli.stage import Decision, _apply
    from setforge.ownership import Authority, ClaimLifecycle, OwnershipStore

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    (stage,) = collect_stages(cfg, resolve_profile(cfg, profile), repo, profile)
    assert stage.ownership is not None
    result = walk(
        stage.hunks,
        lambda h, i, n: (
            Decision(HunkClass.SHARED)
            if h.label == "## Shell"
            else Decision(HunkClass.LOCAL)
        ),
    )
    before = stage.dst.read_bytes()
    owner_id = uuid4()

    _apply(profile, stage, result, owner_id=owner_id)

    claim = OwnershipStore().read(stage.ownership.observation.resource_id)
    assert claim is not None
    assert claim.owner_id == owner_id
    assert claim.authority is Authority.MANAGE
    assert claim.lifecycle is ClaimLifecycle.CLAIMED
    assert stage.dst.read_bytes() == before


def test_apply_transfers_container_and_records_reversible_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import uuid4

    from setforge import locking, transitions
    from setforge.cli.stage import Decision, _apply
    from setforge.file_ownership import (
        decide_file,
        observe_file,
        publish_file_claim_locked,
    )
    from setforge.ownership import OwnershipStore, load_or_create_owner_id

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    (collected,) = collect_stages(cfg, resolve_profile(cfg, profile), repo, profile)
    observation = observe_file(collected.dst)
    foreign_owner = uuid4()
    receiver_owner = load_or_create_owner_id(repo)
    store = OwnershipStore()
    initial = decide_file(observation, None, owner_id=foreign_owner)
    with locking.install_resources_lock():
        before = publish_file_claim_locked(
            store,
            initial,
            owner_id=foreign_owner,
            declaration_ref=f"tracked_files.{collected.sub_name}",
            acquisition="adopted-external",
        )
    stage = replace(
        collected,
        ownership=decide_file(observation, before, owner_id=receiver_owner),
    )
    config_path = repo / "setforge.yaml"
    config_payload = (
        "schema_version: '6.0'\n"
        "tracked_files:\n"
        "  CLAUDE.md:\n"
        "    src: CLAUDE.md\n"
        f"    dst: {stage.dst}\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [CLAUDE.md]\n"
    )
    config_path.write_text(config_payload, encoding="utf-8")
    adopt_result = walk(
        stage.hunks,
        lambda h, _i, _n: (
            Decision(
                HunkClass.SHARED_DRAFTED,
                draft=b"## Shell\nportable\n\n",
                adopt=True,
            )
            if h.label == "## Shell"
            else Decision(HunkClass.LOCAL)
        ),
    )
    live_before = stage.dst.read_bytes()
    with pytest.raises(
        InvariantViolation, match="transfer ownership before adopting live content"
    ):
        _apply(
            profile,
            stage,
            adopt_result,
            config_dir=repo,
            config_path=config_path,
            owner_id=receiver_owner,
        )
    assert store.read(observation.resource_id) == before
    assert stage.dst.read_bytes() == live_before

    result = walk(stage.hunks, lambda _h, _i, _n: Decision(HunkClass.SHARED))
    real_read_locked = stage_mod.read_owner_id_locked
    monkeypatch.setattr(stage_mod, "read_owner_id_locked", lambda *args: uuid4())
    with pytest.raises(InvariantViolation, match="owner identity changed"):
        _apply(
            profile,
            stage,
            result,
            config_dir=repo,
            config_path=config_path,
            owner_id=receiver_owner,
        )
    assert store.read(observation.resource_id) == before
    monkeypatch.setattr(stage_mod, "read_owner_id_locked", real_read_locked)

    config_path.write_text(
        "schema_version: '6.0'\ntracked_files: {}\nprofiles: {p: {}}\n",
        encoding="utf-8",
    )
    with pytest.raises(InvariantViolation, match=r"declaration.*changed"):
        _apply(
            profile,
            stage,
            result,
            config_dir=repo,
            config_path=config_path,
            owner_id=receiver_owner,
        )
    assert store.read(observation.resource_id) == before
    config_path.write_text(config_payload, encoding="utf-8")

    _apply(
        profile,
        stage,
        result,
        config_dir=repo,
        config_path=config_path,
        owner_id=receiver_owner,
    )

    after = store.read(observation.resource_id)
    assert after is not None
    assert after.owner_id == receiver_owner
    assert after.generation == before.generation + 1
    assert stage.dst.read_bytes() == live_before
    transition = transitions.load_latest(profile)
    assert transition is not None
    assert (
        transitions.load_meta(transition).command is transitions.TransitionCommand.STAGE
    )
    assert transitions.load_ownership_transfers(transition) == (
        transitions.OwnershipTransferDelta(before, after),
    )


def test_apply_adoption_failure_recovers_claim_and_reconcile_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import uuid4

    from setforge.cli.stage import Decision, _apply
    from setforge.ownership import OwnershipStore
    from setforge.reconcile import store

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    (stage,) = collect_stages(cfg, resolve_profile(cfg, profile), repo, profile)
    assert stage.ownership is not None
    result = walk(
        stage.hunks,
        lambda h, i, n: Decision(HunkClass.SHARED),
    )
    index_before = store._index_path(profile).read_bytes()
    real_commit = stage_mod._commit_persist

    def fail_after_claim(*args: object, **kwargs: object) -> None:
        real_commit(*args, **kwargs)  # type: ignore[arg-type]
        raise OSError("injected post-record failure")

    monkeypatch.setattr(stage_mod, "_commit_persist", fail_after_claim)

    with pytest.raises(OSError, match="injected post-record failure"):
        _apply(profile, stage, result, owner_id=uuid4())

    assert OwnershipStore().read(stage.ownership.observation.resource_id) is None
    assert store._index_path(profile).read_bytes() == index_before


def test_owned_adopt_live_failure_restores_file_claim_and_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import uuid4

    from setforge.cli.stage import Decision, _apply
    from setforge.ownership import OwnershipStore
    from setforge.reconcile import store

    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    (stage,) = collect_stages(cfg, resolve_profile(cfg, profile), repo, profile)
    assert stage.ownership is not None
    result = walk(
        stage.hunks,
        lambda h, i, n: (
            Decision(
                HunkClass.SHARED_DRAFTED,
                draft=b"## Shell\nportable\n\n",
                adopt=True,
            )
            if h.label == "## Shell"
            else None
        ),
    )
    live_before = stage.dst.read_bytes()
    index_before = store._index_path(profile).read_bytes()
    real_commit = stage_mod._commit_persist

    def fail_after_record(*args: object, **kwargs: object) -> None:
        real_commit(*args, **kwargs)  # type: ignore[arg-type]
        raise OSError("injected owned-adopt failure")

    monkeypatch.setattr(stage_mod, "_commit_persist", fail_after_record)

    with pytest.raises(OSError, match="owned-adopt failure"):
        _apply(profile, stage, result, owner_id=uuid4())

    assert stage.dst.read_bytes() == live_before
    assert OwnershipStore().read(stage.ownership.observation.resource_id) is None
    assert store._index_path(profile).read_bytes() == index_before
    assert not store._drafts_path(profile, stage.fid).exists()


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
    return next(h.unit_id for h in stage.hunks if h.label == "## Shell")


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
    assert store.read_drafts(profile, fid) == {
        UnitRef.line(_shell_anchor(stage)): draft
    }
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
    real_locks = stage_mod.mutation_locks
    real_write = stage_mod.atomicio.atomic_write_bytes

    @contextlib.contextmanager
    def recording_locks(**kwargs: object) -> Iterator[None]:
        events.append("enter")
        with real_locks(**kwargs):  # type: ignore[arg-type]
            try:
                yield
            finally:
                events.append("exit")

    def recording_write(path: Path, *args: object, **kwargs: object) -> None:
        # The store also writes index/base via atomicio; record only the live dst.
        if path == stage.dst:
            events.append("write")
        real_write(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(stage_mod, "mutation_locks", recording_locks)
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
