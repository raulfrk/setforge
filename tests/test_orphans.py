"""Unit tests for tracked-file orphan detection + cleanup-orphans subcommand.

Covers :func:`setforge.compare.detect_orphans` and the CLI surface in
:mod:`setforge.cli.orphans`. Docker-end-to-end coverage lives in
``tests/docker/test_e2e_docker_orphans.py``.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from ruamel.yaml import YAML
from typer.testing import CliRunner

from setforge import compare as compare_mod
from setforge import transitions
from setforge.cli import app
from setforge.cli import orphans as orphans_mod
from setforge.compare import OrphanEntry, detect_orphans, load_ignored_orphans
from setforge.config import Config, Profile, TrackedFile
from setforge.errors import OrphanCleanupRequiresInteractive

_ANSI_RE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi_and_newlines(text: str) -> str:
    """Strip ANSI escapes AND collapse newlines; CliRunner's Rich console
    wraps paths and counts across both, fragmenting substring asserts."""
    return _ANSI_RE.sub("", text).replace("\n", "")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect ``SETFORGE_STATE_DIR`` to a per-test tmp tree."""
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def _write_config_file(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _make_config_with(tracked: dict[str, TrackedFile]) -> Config:
    """Build a Config with ``tracked`` and a single profile referencing all of them."""
    return Config(
        tracked_files=tracked,
        profiles={"p": Profile(tracked_files=list(tracked))},
    )


def _write_meta_record(transitions_root: Path, dirname: str, paths: list[str]) -> Path:
    """Write a minimal ``meta.json`` record exposing ``paths`` for detection."""
    target = transitions_root / dirname
    target.mkdir(parents=True, exist_ok=True)
    payload = {
        "command": "install",
        "profile": "p",
        "timestamp": "2026-05-19T12:00:00+00:00",
        "host": "test-host",
        "version": "0.2.0",
        "paths": paths,
    }
    (target / "meta.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    return target


# ---------------------------------------------------------------------------
# detect_orphans()
# ---------------------------------------------------------------------------


def test_detect_orphans_finds_removed_from_yaml(tmp_path: Path) -> None:
    """A path in meta.json's `paths` that's no longer in resolved
    tracked_files surfaces as an orphan."""
    transitions_dir = tmp_path / "transitions"
    live_orphan = tmp_path / "live" / "orphan.txt"
    live_kept = tmp_path / "live" / "kept.txt"
    _write_meta_record(
        transitions_dir,
        "20260518T120000000000Z-install-p",
        [str(live_orphan), str(live_kept)],
    )

    config = _make_config_with(
        {"kept": TrackedFile(src=Path("kept.txt"), dst=str(live_kept))}
    )

    orphans = detect_orphans(
        resolve_profile_wrap(config, "p"),
        config,
        transitions_dir,
    )
    assert orphans == [OrphanEntry(path=live_orphan)]


def test_detect_orphans_empty_when_transitions_missing(tmp_path: Path) -> None:
    transitions_dir = tmp_path / "no-transitions"
    config = _make_config_with(
        {"x": TrackedFile(src=Path("x"), dst=str(tmp_path / "x"))}
    )
    assert (
        detect_orphans(resolve_profile_wrap(config, "p"), config, transitions_dir) == []
    )


def test_detect_orphans_ignores_corrupt_meta(tmp_path: Path) -> None:
    transitions_dir = tmp_path / "transitions"
    target = transitions_dir / "20260518T120000000000Z-install-p"
    target.mkdir(parents=True)
    (target / "meta.json").write_text("{not json", encoding="utf-8")
    # Second, valid record with one orphan.
    live_orphan = tmp_path / "live" / "orphan.txt"
    _write_meta_record(
        transitions_dir,
        "20260518T130000000000Z-install-p",
        [str(live_orphan)],
    )
    config = _make_config_with({})
    orphans = detect_orphans(resolve_profile_wrap(config, "p"), config, transitions_dir)
    assert orphans == [OrphanEntry(path=live_orphan)]


def test_detect_orphans_respects_ignore_list(tmp_path: Path) -> None:
    """Tracked_file IDs in ``ignored`` are excluded from orphan output
    via their resolved destination."""
    transitions_dir = tmp_path / "transitions"
    live_ignored = tmp_path / "live" / "ignored.txt"
    _write_meta_record(
        transitions_dir,
        "20260518T120000000000Z-install-p",
        [str(live_ignored)],
    )
    # 'ignored_id' is in the config but NOT in the profile's tracked_files
    # → it would be an orphan by default; the ignored frozenset must
    # suppress it.
    config = Config(
        tracked_files={
            "ignored_id": TrackedFile(src=Path("ignored.txt"), dst=str(live_ignored)),
        },
        profiles={"p": Profile(tracked_files=[])},
    )
    orphans = detect_orphans(
        resolve_profile_wrap(config, "p"),
        config,
        transitions_dir,
        ignored=frozenset({"ignored_id"}),
    )
    assert orphans == []


def resolve_profile_wrap(config: Config, name: str) -> Any:
    """Wrap ``resolve_profile`` so the helper above stays single-line."""
    return compare_mod.resolve_profile(config, name)


# ---------------------------------------------------------------------------
# load_ignored_orphans()
# ---------------------------------------------------------------------------


def test_load_ignored_orphans_missing_returns_empty() -> None:
    """No local.yaml on disk → empty frozenset (no surprise crash)."""
    assert load_ignored_orphans() == frozenset()


def test_load_ignored_orphans_parses_list(tmp_path: Path) -> None:
    """A `orphan_ignore:` block round-trips into a frozenset."""
    cfg_path = compare_mod.LOCAL_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("orphan_ignore:\n  - foo\n  - bar\n", encoding="utf-8")
    assert load_ignored_orphans() == frozenset({"foo", "bar"})


def test_load_ignored_orphans_corrupt_yaml_returns_empty(tmp_path: Path) -> None:
    """Best-effort: malformed YAML must NOT crash compare."""
    cfg_path = compare_mod.LOCAL_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("not: [valid: yaml\n", encoding="utf-8")
    assert load_ignored_orphans() == frozenset()


# ---------------------------------------------------------------------------
# CLI: dry-run default
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write_minimal_yaml(tmp_path: Path) -> Path:
    """Build a minimal setforge.yaml with one tracked_file."""
    cfg = tmp_path / "setforge.yaml"
    body = (
        "version: 1\n"
        "tracked_files:\n"
        "  kept:\n"
        "    src: kept.txt\n"
        f"    dst: {tmp_path / 'live' / 'kept.txt'}\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [kept]\n"
    )
    _write_config_file(cfg, body)
    return cfg


def test_apply_default_is_dry_run(
    runner: CliRunner, tmp_path: Path, isolated_state_dir: Path
) -> None:
    """Without `--apply`, the subcommand must not delete anything."""
    cfg = _write_minimal_yaml(tmp_path)
    live_orphan = tmp_path / "live" / "orphan.txt"
    live_orphan.parent.mkdir(parents=True, exist_ok=True)
    live_orphan.write_text("orphan body\n", encoding="utf-8")

    _write_meta_record(
        isolated_state_dir / "transitions",
        "20260518T120000000000Z-install-p",
        [str(live_orphan)],
    )

    result = runner.invoke(
        app, ["cleanup-orphans", "--profile", "p", "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.stdout
    assert live_orphan.exists()  # CRITICAL: not deleted under dry-run.
    plain = _strip_ansi_and_newlines(result.stdout)
    assert "WOULD delete" in plain
    assert live_orphan.name in plain


def test_apply_non_tty_raises(
    runner: CliRunner,
    tmp_path: Path,
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--apply` + non-TTY + no `--yes` raises OrphanCleanupRequiresInteractive."""
    cfg = _write_minimal_yaml(tmp_path)
    live_orphan = tmp_path / "live" / "orphan.txt"
    live_orphan.parent.mkdir(parents=True, exist_ok=True)
    live_orphan.write_text("orphan body\n", encoding="utf-8")
    _write_meta_record(
        isolated_state_dir / "transitions",
        "20260518T120000000000Z-install-p",
        [str(live_orphan)],
    )

    # CliRunner stdin is always non-TTY; no monkeypatching needed.
    # CliRunner invokes the Typer `app` directly (NOT `main()`), so the
    # SetforgeError surfaces on `result.exception`, not the stderr-formatted
    # exit code that `main()`'s top-level handler produces.
    result = runner.invoke(
        app, ["cleanup-orphans", "--profile", "p", "--config", str(cfg), "--apply"]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, OrphanCleanupRequiresInteractive)
    assert "requires --yes when stdin is not a TTY" in str(result.exception)
    assert live_orphan.exists()  # mutate-gate: no deletion on raise.


def test_apply_yes_writes_transition_first(
    tmp_path: Path,
    isolated_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--apply --yes` → `_write_orphan_transition` must fire BEFORE any unlink.

    Probes the transition-first invariant directly on
    :func:`_execute_cleanup` so the test doesn't depend on
    prompt_toolkit being available or on CliRunner stdin behavior.
    """
    live_orphan = tmp_path / "live" / "orphan.txt"
    live_orphan.parent.mkdir(parents=True, exist_ok=True)
    live_orphan.write_text("body\n", encoding="utf-8")
    orphan_entry = OrphanEntry(path=live_orphan)

    order: list[str] = []
    real_write = transitions.write_transition

    def _spy_write_transition(*args: object, **kwargs: object) -> Path:
        order.append("write_transition")
        return real_write(*args, **kwargs)  # type: ignore[arg-type]

    real_unlink = Path.unlink

    def _spy_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == live_orphan:
            order.append("unlink")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(
        "setforge.cli.orphans.transitions.write_transition", _spy_write_transition
    )
    monkeypatch.setattr(Path, "unlink", _spy_unlink)

    orphans_mod._execute_cleanup(
        "p",
        [orphan_entry],
        orphans_mod.ApplyChoice.DELETE_AND_TRANSITION,
        Console(),
    )
    assert order == ["write_transition", "unlink"], order
    assert not live_orphan.exists()


def test_apply_default_branch_uses_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--apply --yes` short-circuits to DELETE_AND_TRANSITION (safe default)."""
    assert (
        orphans_mod._pick_cleanup_branch(yes=True)
        is orphans_mod.ApplyChoice.DELETE_AND_TRANSITION
    )


def test_apply_non_tty_resolver_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct mutate-gate probe: non-TTY + no-yes → raise."""

    class _FakeStdin:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr("sys.stdin", _FakeStdin)
    with pytest.raises(OrphanCleanupRequiresInteractive):
        orphans_mod._pick_cleanup_branch(yes=False)


# ---------------------------------------------------------------------------
# CLI: --ignore writes local.yaml only
# ---------------------------------------------------------------------------


def test_ignore_writes_local_yaml_not_tracked(
    runner: CliRunner, tmp_path: Path
) -> None:
    """`--ignore <id>` mutates ~/.config/setforge/local.yaml ONLY; the
    tracked setforge.yaml is untouched."""
    cfg = _write_minimal_yaml(tmp_path)
    cfg_bytes_before = cfg.read_bytes()

    result = runner.invoke(
        app,
        [
            "cleanup-orphans",
            "--profile",
            "p",
            "--config",
            str(cfg),
            "--ignore",
            "some_old_id",
        ],
    )
    assert result.exit_code == 0, result.stdout
    # Tracked setforge.yaml: untouched, byte-for-byte.
    assert cfg.read_bytes() == cfg_bytes_before
    # Host-local local.yaml (redirected by conftest's autouse fixture
    # to tmp_path/local.yaml) now contains the ignore entry.
    yaml = YAML(typ="safe")
    payload = yaml.load(compare_mod.LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert payload == {"orphan_ignore": ["some_old_id"]}


def test_ignore_is_idempotent(runner: CliRunner, tmp_path: Path) -> None:
    """Re-adding an existing id leaves the list shape unchanged."""
    cfg = _write_minimal_yaml(tmp_path)
    for _ in range(2):
        result = runner.invoke(
            app,
            [
                "cleanup-orphans",
                "--profile",
                "p",
                "--config",
                str(cfg),
                "--ignore",
                "id_a",
            ],
        )
        assert result.exit_code == 0, result.stdout
    yaml = YAML(typ="safe")
    payload = yaml.load(compare_mod.LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
    # ruamel rt-loaded list is a CommentedSeq under the hood; compare contents.
    assert payload == {"orphan_ignore": ["id_a"]}


# ---------------------------------------------------------------------------
# Symlink safety
# ---------------------------------------------------------------------------


def test_symlink_orphan_uses_lstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symlink orphan: unlink removes the link only, never the target."""
    target = tmp_path / "real_data" / "important.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("DO NOT DELETE\n", encoding="utf-8")

    link = tmp_path / "live" / "orphan_link"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)

    assert link.is_symlink()
    orphan_entry = OrphanEntry(path=link)

    orphans_mod._unlink_orphan_path(orphan_entry.path, Console())

    # The link is gone; the target survives.
    assert not link.exists()
    assert not link.is_symlink()
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "DO NOT DELETE\n"


def test_unlink_missing_path_warns_does_not_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A vanished orphan logs a warning, NOT a crash (no missing_ok=True
    swallow — we want the race surfaced explicitly)."""

    ghost = tmp_path / "vanished.txt"
    assert not ghost.exists()
    orphans_mod._unlink_orphan_path(ghost, Console())
    captured = capsys.readouterr()
    assert "vanished before delete" in captured.out


# ---------------------------------------------------------------------------
# Anti-pattern checks: source-code structural assertions
# ---------------------------------------------------------------------------


def _orphans_module_ast() -> ast.Module:
    """Parsed AST of :mod:`setforge.cli.orphans` for structural assertions."""
    src = Path(orphans_mod.__file__).read_text(encoding="utf-8")
    return ast.parse(src)


def test_no_unlink_missing_ok_in_orphans_module() -> None:
    """`unlink(missing_ok=True)` swallows the "user re-added the file"
    race; must not appear in any AST-level Call in the cleanup module.
    (Source-text grep would false-positive on the prohibition's own
    docstring.)"""
    tree = _orphans_module_ast()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        attr = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if attr != "unlink":
            continue
        for kw in node.keywords:
            if kw.arg == "missing_ok":
                raise AssertionError(
                    f"unlink(missing_ok=...) detected at line {node.lineno}"
                )


def test_no_shutil_rmtree_or_removedirs() -> None:
    """`shutil.rmtree` (recursive) and `os.removedirs` (walks up) are
    both forbidden — single-level Path.rmdir() only. AST-walk for
    matching attribute call shapes."""
    tree = _orphans_module_ast()
    forbidden = {"rmtree", "removedirs"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        attr = getattr(node.func, "attr", None)
        if attr in forbidden:
            raise AssertionError(
                f"forbidden recursive-delete call {attr!r} at line {node.lineno}"
            )


def test_no_resolve_in_orphan_unlink_helpers() -> None:
    """Calling `.resolve()` on a symlink before `.unlink()` torches the
    pointed-to file. None of the per-orphan helpers
    (`_unlink_orphan_path`, `_rmdir_empty_parents`, `_execute_cleanup`,
    `_write_orphan_transition`, `_read_orphan_content`,
    `_lstat_safe`) may call `.resolve()`. The
    `_detect_orphans_live` helper is allowed to call
    `config_path.resolve()` for source-dir normalization (Typer config
    path, not an orphan path)."""
    tree = _orphans_module_ast()
    helper_names = {
        "_unlink_orphan_path",
        "_rmdir_empty_parents",
        "_execute_cleanup",
        "_write_orphan_transition",
        "_read_orphan_content",
        "_lstat_safe",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in helper_names:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and getattr(inner.func, "attr", None) == "resolve"
            ):
                raise AssertionError(
                    f".resolve() forbidden in {node.name} at line {inner.lineno}"
                )


def test_apply_path_calls_detect_orphans() -> None:
    """The `--apply` code path MUST re-compute orphans live (via
    `_detect_orphans_live`, which dispatches to `compare_profile`
    AND `detect_orphans`), NOT cache from a prior `compare` call.

    Mirrors the SPEC 2 robust acceptance command — the FIRST function
    whose name contains "apply" (case-insensitive) must transitively
    reach `detect_orphans`.
    """
    tree = _orphans_module_ast()
    apply_fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and "apply" in n.name.lower()
    )
    # Direct calls inside apply_fn.
    direct_calls = {
        getattr(c.func, "attr", None) or getattr(c.func, "id", None)
        for c in ast.walk(apply_fn)
        if isinstance(c, ast.Call)
    }
    # Transitive call set: include the bodies of any helper called by
    # apply_fn that is also defined in this module (e.g.
    # `_detect_orphans_live`).
    transitive_names = {n for n in direct_calls if isinstance(n, str)}
    transitive_calls: set[str] = set(transitive_names)
    helper_fns = {
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name in transitive_names
    }
    for helper in helper_fns:
        for c in ast.walk(helper):
            if isinstance(c, ast.Call):
                attr = getattr(c.func, "attr", None) or getattr(c.func, "id", None)
                if attr is not None:
                    transitive_calls.add(attr)
    assert "detect_orphans" in transitive_calls or "compare_profile" in transitive_calls


# ---------------------------------------------------------------------------
# Compare output includes Orphans block
# ---------------------------------------------------------------------------


def test_compare_renders_orphans_block(
    runner: CliRunner, tmp_path: Path, isolated_state_dir: Path
) -> None:
    """`setforge compare` surfaces orphans as a separate `Orphans (N):` block."""
    cfg = _write_minimal_yaml(tmp_path)
    live_orphan = tmp_path / "live" / "orphan.txt"
    live_orphan.parent.mkdir(parents=True, exist_ok=True)
    live_orphan.write_text("body\n", encoding="utf-8")
    _write_meta_record(
        isolated_state_dir / "transitions",
        "20260518T120000000000Z-install-p",
        [str(live_orphan)],
    )
    # Also create the kept tracked source so compare's missing-src guard
    # doesn't trip.
    (tmp_path / "kept.txt").write_text("k", encoding="utf-8")

    result = runner.invoke(app, ["compare", "--profile", "p", "--config", str(cfg)])
    assert result.exit_code == 0, result.stdout
    # Rich wraps fragments in ANSI escapes AND newlines for narrow
    # terminal width; strip both before substring assertions.
    plain = _strip_ansi_and_newlines(result.stdout)
    assert "Orphans (1):" in plain
    assert live_orphan.name in plain
