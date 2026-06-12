"""Integration tests for the span lifecycle in the install loop.

Drive the real ``setforge install`` CLI against a temp config repo with a
sandboxed ``$HOME`` + ``$SETFORGE_STATE_DIR`` and assert on the span
sidecar (:mod:`setforge.spans_store`) it seeds + advances, plus the
end-to-end acceptance: a pinned md section survives an upstream edit
ELSEWHERE in the file across two installs with no phantom conflict and a
byte-stable live region.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store, spans_store
from setforge.cli import app

_PROFILE = "test-spans"
_FILE_ID = "doc"

_DOC = """\
# Title

## Pinned

Pinned body original.

## Shared

Shared body original.
"""


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.setforge_spans/doc.md\n"
        "    disposition: shared\n"
        "    spans:\n"
        '      - anchor: "## Pinned"\n'
        "        kind: pinned\n"
        "        semantics: shared\n"
        "  anchor:\n"
        "    src: anchor.txt\n"
        "    dst: ~/.setforge_spans/anchor.txt\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - doc\n"
        "      - anchor\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, body: str) -> None:
    src = repo / "tracked" / "doc.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(body, encoding="utf-8")
    (src.parent / "anchor.txt").write_text("anchor\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _live_path() -> Path:
    return Path.home() / ".setforge_spans" / "doc.md"


def _install(config: Path, *, extra: list[str] | None = None) -> Result:
    args = [
        "install",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-transition",
        "--no-secrets-scan",
        "--no-git-check",
        "--yes",
    ]
    if extra:
        args.extend(extra)
    return CliRunner().invoke(app, args)


def test_first_install_seeds_span_state(repo: Path) -> None:
    _write_tracked(repo, _DOC)
    config = _write_config(repo)

    result = _install(config)
    assert result.exit_code == 0, result.output
    states = spans_store.get_states(_PROFILE, _FILE_ID)
    assert "## Pinned" in states


def test_pinned_span_survives_upstream_edit_two_installs(repo: Path) -> None:
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install(config).exit_code == 0

    # User edits the live pinned region; upstream edits the shared region.
    live = _live_path()
    live.write_text(
        live.read_text().replace("Pinned body original.", "MY PINNED EDIT."),
        encoding="utf-8",
    )
    _write_tracked(repo, _DOC.replace("Shared body original.", "Shared body UPSTREAM."))

    # Second install: pinned region kept live, shared region took upstream,
    # NO phantom conflict warning.
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "merge conflict" not in result.output
    body = live.read_text()
    assert "MY PINNED EDIT." in body
    assert "Shared body UPSTREAM." in body

    # Third no-edit install: byte-stable, still no conflict.
    before = live.read_text()
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert live.read_text() == before


def test_orphaned_pinned_span_warns_and_install_succeeds(repo: Path) -> None:
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install(config).exit_code == 0

    # Upstream removes the pinned heading entirely AND the live copy loses
    # it too -> orphan. Default install must warn yet still exit 0 (I6).
    gone = _DOC.replace("## Pinned\n\nPinned body original.\n\n", "")
    _write_tracked(repo, gone)
    live = _live_path()
    live.write_text(gone, encoding="utf-8")

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "could not be relocated" in result.output


def _sync(config: Path, *, extra: list[str] | None = None) -> Result:
    args = [
        "sync",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--auto=use-live",
        "--no-transition",
        "--yes",
    ]
    if extra:
        args.extend(extra)
    return CliRunner().invoke(app, args)


def test_capture_excludes_pinned_span_body_from_tracked(repo: Path) -> None:
    # Invariant I2: a host-local pinned-span edit must NEVER bake into the
    # tracked source on capture (sync --auto=use-live).
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install(config).exit_code == 0

    live = _live_path()
    # User edits BOTH the pinned region and the shared region in live.
    live.write_text(
        live.read_text()
        .replace("Pinned body original.", "SECRET host-local pin.")
        .replace("Shared body original.", "Shared body LIVE edit."),
        encoding="utf-8",
    )

    result = _sync(config)
    assert result.exit_code == 0, result.output

    tracked_src = (repo / "tracked" / "doc.md").read_text(encoding="utf-8")
    # Pinned span region excluded -> tracked keeps its original body.
    assert "SECRET host-local pin." not in tracked_src
    assert "Pinned body original." in tracked_src
    # Non-span edit captured normally.
    assert "Shared body LIVE edit." in tracked_src


def test_strict_spans_refuses_on_pinned_orphan(repo: Path) -> None:
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install(config).exit_code == 0

    gone = _DOC.replace("## Pinned\n\nPinned body original.\n\n", "")
    _write_tracked(repo, gone)
    live = _live_path()
    live.write_text(gone, encoding="utf-8")

    result = _install(config, extra=["--strict-spans"])
    assert result.exit_code != 0


# ---- structural span: upstream rename/delete classifier ----

_YAML_DOC = "editor:\n  theme: dark\n  font: mono\n"


def _write_yaml_config(repo: Path) -> Path:
    """A structural (yaml) tracked file with a pinned span on editor.theme."""
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  cfg:\n"
        "    src: cfg.yaml\n"
        "    dst: ~/.setforge_spans/cfg.yaml\n"
        "    disposition: shared\n"
        "    spans:\n"
        "      - anchor: editor.theme\n"
        "        kind: pinned\n"
        "        semantics: shared\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - cfg\n",
        encoding="utf-8",
    )
    return config


def _write_tracked_yaml(repo: Path, body: str) -> None:
    src = repo / "tracked" / "cfg.yaml"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(body, encoding="utf-8")


def _yaml_live_path() -> Path:
    return Path.home() / ".setforge_spans" / "cfg.yaml"


def test_upstream_renamed_span_path_warns_with_did_you_mean(repo: Path) -> None:
    # Upstream RENAMED the pinned dotted path's leaf key while live no longer
    # carries it: the install warning must attribute the loss to upstream
    # (distinct wording, not the generic could-not-be-relocated) and append a
    # did-you-mean naming the closest tracked sibling.
    _write_tracked_yaml(repo, _YAML_DOC)
    config = _write_yaml_config(repo)
    assert _install(config).exit_code == 0

    _write_tracked_yaml(repo, "editor:\n  themes: dark\n  font: mono\n")
    _yaml_live_path().write_text("editor:\n  font: mono\n", encoding="utf-8")

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "renamed or deleted upstream" in result.output
    assert "did you mean 'themes'?" in result.output


def test_upstream_deleted_span_path_warns_without_suggestion(repo: Path) -> None:
    # Upstream DELETED the pinned path outright (no close sibling remains):
    # the upstream attribution still fires but NO did-you-mean is appended.
    _write_tracked_yaml(repo, _YAML_DOC)
    config = _write_yaml_config(repo)
    assert _install(config).exit_code == 0

    _write_tracked_yaml(repo, "editor:\n  font: mono\n")
    _yaml_live_path().write_text("editor:\n  font: mono\n", encoding="utf-8")

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "renamed or deleted upstream" in result.output
    assert "did you mean" not in result.output


def test_strict_spans_refuses_on_upstream_renamed_orphan(repo: Path) -> None:
    # --strict-spans escalates the new reason exactly like every other pinned
    # orphan: the pass-1 gate refuses the whole install (non-zero exit) and
    # the live file stays byte-untouched.
    _write_tracked_yaml(repo, _YAML_DOC)
    config = _write_yaml_config(repo)
    assert _install(config).exit_code == 0

    _write_tracked_yaml(repo, "editor:\n  themes: dark\n  font: mono\n")
    live = _yaml_live_path()
    live.write_text("editor:\n  font: mono\n", encoding="utf-8")
    before = live.read_bytes()

    result = _install(config, extra=["--strict-spans"])
    assert result.exit_code != 0
    # The pass-1 gate renders the same upstream-attributed warning before
    # refusing (the SetforgeError itself is raised past the CliRunner).
    assert "renamed or deleted upstream" in result.output
    assert live.read_bytes() == before


def _install_with_transition(config: Path) -> Result:
    args = [
        "install",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-secrets-scan",
        "--no-git-check",
        "--yes",
    ]
    return CliRunner().invoke(app, args)


def test_revert_rolls_spans_sidecar_in_lockstep(repo: Path) -> None:
    # Invariant I5: revert restores live + base + spans sidecar atomically.
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install_with_transition(config).exit_code == 0

    live = _live_path()
    sidecar_before = spans_store.get_states(_PROFILE, _FILE_ID)
    base_before = base_store.read_base(_PROFILE, _FILE_ID)

    # Second install with a live pin edit + upstream shared edit advances
    # live, base, AND the sidecar.
    live.write_text(
        live.read_text().replace("Pinned body original.", "MY PIN."),
        encoding="utf-8",
    )
    _write_tracked(repo, _DOC.replace("Shared body original.", "Shared body UPSTREAM."))
    assert _install_with_transition(config).exit_code == 0
    assert spans_store.get_states(_PROFILE, _FILE_ID) != sidecar_before

    # Revert the second install: live + base + sidecar all roll back.
    result = CliRunner().invoke(
        app, ["revert", f"--profile={_PROFILE}", f"--config={config}", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert spans_store.get_states(_PROFILE, _FILE_ID) == sidecar_before
    assert base_store.read_base(_PROFILE, _FILE_ID) == base_before
