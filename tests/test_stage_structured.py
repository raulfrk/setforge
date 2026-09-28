"""Tests for the structured (YAML/JSON/JSONC) key-level `setforge stage` path."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from setforge.cli import stage as stage_mod
from setforge.cli.stage import (
    Decision,
    StructuredFileStage,
    _apply_structured,
    collect_stages,
    collect_structured_stages,
    walk_structured,
)
from setforge.config import Config, Profile, TrackedFile, resolve_profile
from setforge.errors import InvariantViolation, StructuredParseError
from setforge.reconcile import share_draft
from setforge.reconcile.structured_units import KeyUnit, StructuredFormat
from setforge.reconcile.types import HunkClass, UnitRef, file_id
from setforge.ui.widgets import CANCEL


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _setup_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Config, Path, str]:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge import locking
    from setforge.reconcile import store

    base = b"theme: dark\nfontSize: 14\n"
    live = b"theme: dark\nfontSize: 16\n"
    repo = tmp_path / "repo"
    src = repo / "tracked" / "settings.yaml"
    dst = tmp_path / "live" / "settings.yaml"
    _write(src, base)
    _write(dst, live)
    with locking.profile_lock("p"):
        store.record("p", file_id("settings.yaml"), base=base, local=live)
    cfg = Config(
        tracked_files={
            "settings.yaml": TrackedFile(src=Path("settings.yaml"), dst=str(dst))
        },
        profiles={"p": Profile(tracked_files=["settings.yaml"])},
    )
    return cfg, repo, "p"


def test_collect_structured_yields_key_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A structured tracked file produces per-KEY units (PENDING), not line hunks."""
    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)

    (stage,) = collect_structured_stages(cfg, resolved, repo, profile)

    assert stage.fmt is StructuredFormat.YAML
    assert [u.path for u in stage.units] == ["fontSize"]
    assert all(u.cls is HunkClass.PENDING for u in stage.units)


def test_collect_structured_rejects_persisted_line_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge import locking
    from setforge.reconcile import store

    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    with locking.profile_lock(profile):
        store.record(
            profile,
            file_id("settings.yaml"),
            base=b"theme: dark\nfontSize: 14\n",
            local=b"theme: dark\nfontSize: 16\n",
            hunks=[
                {
                    "kind": "line",
                    "cls": "local",
                    "label": "foreign",
                    "unit_id": "foreign",
                    "live_hash": "sha256:value",
                }
            ],
        )
    # Routing incompatibility wins even when the new structured representation
    # is not parseable; it must not be silently treated as an unstaged fallback.
    Path(cfg.tracked_files["settings.yaml"].dst).write_bytes(b"not: [valid")

    with pytest.raises(InvariantViolation, match="current 'key' routing"):
        collect_structured_stages(cfg, resolve_profile(cfg, profile), repo, profile)


def test_collect_stages_skips_structured_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The line-hunk collect delegates structured files to the structured path."""
    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)

    # a .yaml must NOT be line-hunk-staged (else it'd be diffed by line, not key)
    assert collect_stages(cfg, resolved, repo, profile) == []


def test_persist_structured_records_key_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Classifying a key LOCAL persists a kind:'key' row with cls=local."""
    from setforge.reconcile import store

    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_structured_stages(cfg, resolved, repo, profile)

    result = walk_structured(stage.units, lambda u, i, t: Decision(cls=HunkClass.LOCAL))
    _apply_structured(profile, stage, result)

    entry = store.read_index(profile).files[str(file_id("settings.yaml"))]
    assert entry.staged is True
    rows = {r["path"]: r["cls"] for r in entry.hunks}
    assert rows == {"fontSize": "local"}


def test_walk_structured_shared_records_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Classifying a key SHARED persists cls=shared (capture promotes on sync)."""
    from setforge.reconcile import store

    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_structured_stages(cfg, resolved, repo, profile)

    result = walk_structured(
        stage.units, lambda u, i, t: Decision(cls=HunkClass.SHARED)
    )
    _apply_structured(profile, stage, result)

    entry = store.read_index(profile).files[str(file_id("settings.yaml"))]
    assert {r["path"]: r["cls"] for r in entry.hunks} == {"fontSize": "shared"}


@pytest.mark.parametrize("parent", [HunkClass.SHARED, HunkClass.LOCAL])
@pytest.mark.parametrize("child", [HunkClass.SHARED, HunkClass.LOCAL])
@pytest.mark.parametrize("reverse", [False, True])
def test_structured_stage_validates_parent_child_intent_before_persist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent: HunkClass,
    child: HunkClass,
    reverse: bool,
) -> None:
    from setforge import locking
    from setforge.reconcile import store
    from setforge.reconcile.structured_units import reconstruct_structured

    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    base, live = b"a:\n  b: 1\n", b"a: 2\n"
    if reverse:
        base, live = live, base
    dst = Path(cfg.tracked_files["settings.yaml"].dst)
    dst.write_bytes(live)
    with locking.profile_lock(profile):
        store.record(profile, file_id("settings.yaml"), base=base, local=live)
    (stage,) = collect_structured_stages(
        cfg, resolve_profile(cfg, profile), repo, profile
    )
    result = walk_structured(
        stage.units, lambda unit, i, t: Decision(parent if unit.path == "a" else child)
    )
    before = store.read_index(profile)
    if parent != child:
        with pytest.raises(
            StructuredParseError, match="incompatible parent/descendant"
        ):
            _apply_structured(profile, stage, result)
        assert store.read_index(profile) == before
        assert store.read_base(profile, stage.fid) == base
        assert store.read_local(profile, stage.fid) == live
        assert store.read_drafts(profile, stage.fid) == {}
    else:
        _apply_structured(profile, stage, result)
        (saved,) = collect_structured_stages(
            cfg, resolve_profile(cfg, profile), repo, profile
        )
        assert reconstruct_structured(base, live, saved.units, {}, saved.fmt) == (
            live if parent is HunkClass.SHARED else base
        )
    assert dst.read_bytes() == live


def test_render_list_json_reports_structured_drafted_and_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from setforge.cli._output import OutputContext, OutputFormat
    from setforge.cli.stage import _render_list

    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (first,) = collect_structured_stages(cfg, resolved, repo, profile)
    _apply_structured(
        profile,
        first,
        walk_structured(
            first.units,
            lambda u, i, t: Decision(HunkClass.SHARED_DRAFTED, draft=b"18"),
        ),
    )
    first.dst.write_bytes(b"theme: dark\nfontSize: 16\nnewKey: host\n")
    (changed,) = collect_structured_stages(cfg, resolved, repo, profile)
    changed = replace(
        changed,
        units=[
            replace(unit, changed=True)
            if unit.cls is HunkClass.SHARED_DRAFTED
            else unit
            for unit in changed.units
        ],
    )

    _render_list(OutputContext(OutputFormat.JSON), [], [changed])

    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "stage"
    assert payload["schema_version"] == 1
    assert payload["data"] == [
        {
            "name": "settings.yaml",
            "participating": True,
            "shared": 0,
            "shared_promotable": 0,
            "drafted": 1,
            "reconfirm_required": 0,
            "local": 0,
            "pending": 1,
            "blockers": [
                "1 pending unit(s): run `setforge stage settings.yaml` to classify",
                "container ownership: present, external, unowned",
            ],
            "ownership": "adopt",
        }
    ]


def test_render_list_json_changed_structured_local_needs_no_reconfirm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from setforge.cli._output import OutputContext, OutputFormat
    from setforge.cli.stage import _render_list

    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    (stage,) = collect_structured_stages(
        cfg, resolve_profile(cfg, profile), repo, profile
    )
    local = replace(stage.units[0], cls=HunkClass.LOCAL, changed=True)

    _render_list(
        OutputContext(OutputFormat.JSON),
        [],
        [replace(stage, participating=True, units=[local])],
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    (row,) = payload["data"]
    assert row["shared"] == 0
    assert row["reconfirm_required"] == 0
    assert row["local"] == 1
    assert row["ownership"] == "adopt"
    assert row["blockers"] == ["container ownership: present, external, unowned"]


def test_structured_skip_then_same_class_reconfirm_controls_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.reconcile import store

    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (first,) = collect_structured_stages(cfg, resolved, repo, profile)
    _apply_structured(
        profile,
        first,
        walk_structured(first.units, lambda u, i, t: Decision(HunkClass.SHARED)),
    )
    old_hash = store.read_index(profile).files["settings.yaml"].hunks[0]["value_hash"]
    first.dst.write_bytes(b"theme: dark\nfontSize: 18\n")
    (changed,) = collect_structured_stages(cfg, resolved, repo, profile)
    assert changed.units[0].changed is True

    skipped = walk_structured(changed.units, lambda u, i, t: None)
    assert skipped.decided_refs == set()
    _apply_structured(profile, changed, skipped)
    (still_changed,) = collect_structured_stages(cfg, resolved, repo, profile)
    assert still_changed.units[0].changed is True
    assert (
        store.read_index(profile).files["settings.yaml"].hunks[0]["value_hash"]
        == old_hash
    )

    reconfirmed = walk_structured(
        still_changed.units, lambda u, i, t: Decision(HunkClass.SHARED)
    )
    assert reconfirmed.decided_refs == {UnitRef.key("fontSize")}
    _apply_structured(profile, still_changed, reconfirmed)
    (after,) = collect_structured_stages(cfg, resolved, repo, profile)
    assert after.units[0].changed is False
    assert after.units[0].confirmed_hash == after.units[0].value_hash


def test_structured_prompt_to_lock_live_race_refuses_stale_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.reconcile import store

    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    (stage,) = collect_structured_stages(
        cfg, resolve_profile(cfg, profile), repo, profile
    )
    result = walk_structured(stage.units, lambda u, i, t: Decision(HunkClass.SHARED))
    stage.dst.write_bytes(b"theme: dark\nfontSize: 18\n")

    with pytest.raises(InvariantViolation, match="changed after it was shown"):
        _apply_structured(profile, stage, result)

    entry = store.read_index(profile).files["settings.yaml"]
    assert entry.staged is False
    assert entry.hunks == []


def test_structured_apply_refuses_stale_recorded_base_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge import locking
    from setforge.reconcile import store

    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    (stage,) = collect_structured_stages(
        cfg, resolve_profile(cfg, profile), repo, profile
    )
    result = walk_structured(stage.units, lambda u, i, t: Decision(HunkClass.SHARED))
    newer_base = b"theme: light\nfontSize: 14\n"
    with locking.profile_lock(profile):
        store.record(
            profile,
            file_id("settings.yaml"),
            base=newer_base,
            local=stage.live,
        )
    before_live = stage.dst.read_bytes()
    before_index = store._index_path(profile).read_bytes()
    drafts_path = store._drafts_path(profile, file_id("settings.yaml"))
    assert not drafts_path.exists()

    with pytest.raises(InvariantViolation, match=r"recorded base.*changed"):
        _apply_structured(profile, stage, result)

    assert stage.dst.read_bytes() == before_live
    assert store.read_base(profile, file_id("settings.yaml")) == newer_base
    assert store.read_local(profile, file_id("settings.yaml")) == stage.live
    assert store._index_path(profile).read_bytes() == before_index
    assert not drafts_path.exists()


def test_walk_structured_draft_uses_typed_key_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.reconcile import store

    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_structured_stages(cfg, resolved, repo, profile)
    draft = b"18"
    result = walk_structured(
        stage.units,
        lambda u, i, t: Decision(cls=HunkClass.SHARED_DRAFTED, draft=draft),
    )

    assert result.drafts == {UnitRef.key("fontSize"): draft}
    _apply_structured(profile, stage, result)
    assert store.read_drafts(profile, file_id("settings.yaml")) == {
        UnitRef.key("fontSize"): draft
    }


# --- structured Share sub-menu (Draft button → type-confined key draft) -------


def _value_stage() -> tuple[StructuredFileStage, KeyUnit]:
    """A one-key structured stage whose live ``workdir`` is a host-specific path."""
    live = b"workdir: /home/raul/projects\n"
    unit = KeyUnit(HunkClass.PENDING, "workdir", "workdir", "sha256:v")
    stage = StructuredFileStage(
        sub_name="settings.yaml",
        fid=file_id("settings.yaml"),
        src=Path("src"),
        dst=Path("dst"),
        base=live,
        live=live,
        fmt=StructuredFormat.YAML,
        units=[unit],
    )
    return stage, unit


def test_structured_share_submenu_draft_returns_shared_drafted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Draft → key-unit draft with the LIVE typed value; yields SHARED_DRAFTED."""
    stage, unit = _value_stage()
    captured: dict[str, object] = {}
    monkeypatch.setattr(stage_mod, "button_bar", lambda *a, **k: stage_mod._Menu.DRAFT)

    def _fake_draft(original: object, *, display_path: str, fmt: object) -> object:
        captured["original"] = original
        captured["fmt"] = fmt
        return share_draft.DraftResult(draft=b"~/projects", adopt=False)

    monkeypatch.setattr(stage_mod.share_draft, "draft_key_unit", _fake_draft)

    decision = stage_mod._structured_share_submenu(stage, unit, style=None)  # type: ignore[arg-type]

    assert decision == Decision(
        HunkClass.SHARED_DRAFTED, draft=b"~/projects", adopt=False
    )
    assert captured["original"] == "/home/raul/projects"  # the typed live scalar
    assert captured["fmt"] is StructuredFormat.YAML


def test_structured_share_submenu_verbatim_returns_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verbatim shares the live value as-is (today's behaviour)."""
    stage, unit = _value_stage()
    monkeypatch.setattr(
        stage_mod, "button_bar", lambda *a, **k: stage_mod._Menu.VERBATIM
    )
    decision = stage_mod._structured_share_submenu(stage, unit, style=None)  # type: ignore[arg-type]
    assert decision == Decision(HunkClass.SHARED)


def test_structured_share_submenu_draft_cancel_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled draft leaves the key unchanged (None)."""
    stage, unit = _value_stage()
    monkeypatch.setattr(stage_mod, "button_bar", lambda *a, **k: stage_mod._Menu.DRAFT)
    monkeypatch.setattr(stage_mod.share_draft, "draft_key_unit", lambda *a, **k: CANCEL)
    decision = stage_mod._structured_share_submenu(stage, unit, style=None)  # type: ignore[arg-type]
    assert decision is None


@pytest.mark.parametrize("extension", ["yaml", "json", "jsonc"])
@pytest.mark.parametrize("verb", ["sync", "capture"])
def test_public_structured_promotion_preserves_shape_comments_and_inverse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extension: str, verb: str
) -> None:
    from importlib import import_module

    from ruamel.yaml import YAML
    from typer.testing import CliRunner

    from setforge import locking, transitions
    from setforge.cli import app
    from setforge.ownership import OwnershipStore
    from tests.test_cli_cleanup import _TerminalInput
    from tests.test_install_managed_tree import _mixed_config

    config, live_root = _mixed_config(tmp_path, monkeypatch, ("one", "two"))
    # The established JSONC route uses .json; .jsonc retains line staging.
    name = f"settings.{'json' if extension == 'jsonc' else extension}"
    yaml = YAML()
    document = yaml.load(config.read_text())
    tracked = document["tracked_files"].pop("one")
    tracked["src"] = name
    tracked["dst"] = str(live_root / name)
    document["tracked_files"][name] = tracked
    document["profiles"]["p"]["tracked_files"] = [name, "two"]
    yaml.dump(document, config)
    base_model = {
        "root": {"gone": 42, "keep": None},
        "empty": {},
        "flat.key": 1,
        "": {"leaf": 2},
        "slash\\key": True,
        ".": 1,
        "\\": 3,
    }
    live_model = {
        "root": {"keep": ""},
        "empty": {},
        "flat.key": 2,
        "": {},
        "slash\\key": False,
        ".": 2,
        "\\": 4,
    }
    if extension == "yaml":
        base_model["private"] = "base-private"
        base_model["name[]"] = 1
        base_model["star[*]"] = 1
        live_model["private"] = "host-private"
        live_model["name[]"] = 2
        live_model["star[*]"] = 2
    shared_model = dict(live_model)
    if extension == "yaml":
        shared_model["private"] = "base-private"
    source = config.parent / "tracked" / name
    if extension == "yaml":
        yaml.dump(base_model, source)
        base = b"# preserved comment\n" + source.read_bytes()
        temporary = tmp_path / "live-model.yaml"
        yaml.dump(live_model, temporary)
        edited = b"# preserved comment\n" + temporary.read_bytes()
    else:
        prefix = "// preserved comment\n" if extension == "jsonc" else ""
        base = (prefix + json.dumps(base_model, indent=2) + "\n").encode()
        edited = (prefix + json.dumps(live_model, indent=2) + "\n").encode()
    source.write_bytes(base)
    assert transitions.state_root().is_relative_to(tmp_path)
    assert OwnershipStore().root.is_relative_to(tmp_path)
    assert locking._user_global_locks_dir().is_relative_to(tmp_path)
    runner = CliRunner()
    args = ["--profile=p", f"--config={config}"]
    installed = runner.invoke(
        app, ["install", *args, "--yes", "--no-fetch", "--no-git-check"]
    )
    assert installed.exit_code == 0, (installed.output, installed.exception)
    destination = live_root / name
    destination.write_bytes(edited)
    before_control = (live_root / "two").read_bytes()
    seen: list[str] = []

    def choices(_stage: StructuredFileStage):
        def choose(unit: KeyUnit, _index: int, _total: int) -> Decision:
            seen.append(unit.path)
            return Decision(
                HunkClass.LOCAL if unit.path == "private" else HunkClass.SHARED
            )

        return choose

    monkeypatch.setattr(
        import_module("setforge.cli.stage"), "_structured_interactive_choice", choices
    )
    staged = runner.invoke(app, ["stage", name, *args], input=_TerminalInput())
    assert staged.exit_code == 0, (staged.output, staged.exception)
    if extension == "yaml":
        assert "root.gone" in seen
        assert r"flat\.key" in seen
        assert r"\." in seen
        assert r"\\" in seen
        assert any(path.startswith(r"\0") for path in seen)
    else:
        assert seen == [""]
    captured = runner.invoke(app, [verb, *args, "--auto=use-live", "--yes"])
    assert captured.exit_code == 0, (captured.output, captured.exception)
    result = source.read_bytes()
    if extension == "yaml":
        parsed = yaml.load(result)
        assert parsed == shared_model
        assert parsed["slash\\key"] is False
        assert isinstance(parsed["root"]["keep"], str)
        assert b"# preserved comment" in result
    else:
        assert result == edited
    assert destination.read_bytes() == edited
    assert (live_root / "two").read_bytes() == before_control
    if verb == "sync":
        transition = transitions.load_latest("p")
        assert transition is not None
        reversed_result = runner.invoke(app, ["revert", *args, "--yes"])
        assert reversed_result.exit_code == 0, (
            reversed_result.output,
            reversed_result.exception,
        )
        assert source.read_bytes() == base
        assert destination.read_bytes() == edited
        redone = runner.invoke(app, ["revert", *args, "--yes"])
        assert redone.exit_code == 0, (redone.output, redone.exception)
        assert source.read_bytes() == result
        assert destination.read_bytes() == edited
