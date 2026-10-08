from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app
from tests.shared_fixtures import ConfigRepo

_BASE = b"line one\nline two\nline three\n"
_LIVE = b"line one\nlive edit\nline three\n"
_UPSTREAM = b"line one\nupstream edit\nline three\n"


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _config(dst: Path) -> str:
    return (
        "version: 1\ntracked_files:\n  CLAUDE.md:\n    src: CLAUDE.md\n"
        f"    dst: {dst}\n"
        "profiles:\n  p:\n    tracked_files: [CLAUDE.md]\n"
    )


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    base: bytes | None = _BASE,
    live: bytes = _LIVE,
    tracked: bytes = _UPSTREAM,
    hunks: list[dict[str, object]] | None = None,
) -> tuple[Path, Path]:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge import locking
    from setforge.reconcile import store
    from setforge.reconcile.types import file_id

    repo = tmp_path / "repo"
    src = repo / "tracked" / "CLAUDE.md"
    dst = tmp_path / "live" / "CLAUDE.md"
    _write(src, tracked)
    _write(dst, live)
    if base is not None:
        with locking.profile_lock("p"):
            store.record(
                "p",
                file_id("CLAUDE.md"),
                base=base,
                local=live,
                staged=bool(hunks),
                hunks=hunks,
            )
    cfg_path = repo / "setforge.yaml"
    cfg_path.write_text(_config(dst), encoding="utf-8")
    return cfg_path, dst


def test_inspect_human_renders_three_panes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, _ = _setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        app, ["inspect", "CLAUDE.md", "--profile=p", f"--config={cfg_path}"]
    )
    assert result.exit_code == 0, result.output
    assert "live edit" in result.output
    assert "upstream edit" in result.output


@pytest.mark.parametrize("with_base", [False, True])
def test_inspect_generated_resource_uses_rendered_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_base: bool
) -> None:
    template = b"root={{ host.home }}\n"
    rendered = f"root={Path.home().resolve()}\n"
    cfg_path, _ = _setup(
        tmp_path,
        monkeypatch,
        base=rendered.encode() if with_base else None,
        live=rendered.encode(),
        tracked=template,
    )
    cfg_path.write_text(
        "schema_version: '6.1'\nminimum_version: '6.1'\n"
        + cfg_path.read_text().replace(
            "    src: CLAUDE.md\n",
            "    src: CLAUDE.md\n    generated: {inputs: {home: home}}\n",
        )
    )

    result = CliRunner().invoke(
        app,
        [
            "--format=json",
            "inspect",
            "CLAUDE.md",
            "--profile=p",
            f"--config={cfg_path}",
        ],
    )

    assert result.exit_code == 0, result.output + str(result.exception)
    data = json.loads(result.stdout)["data"]
    assert data["panes"]["merge"] == rendered
    assert data["index"]["conflict"] == []
    assert data["staging"] is None
    assert (cfg_path.parent / "tracked" / "CLAUDE.md").read_bytes() == template


def test_rendered_inspect_json_help_example_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, _ = _setup(tmp_path, monkeypatch)
    runner = CliRunner()
    help_result = runner.invoke(app, ["inspect", "--help"], terminal_width=140)
    assert help_result.exit_code == 0
    example = next(
        line.strip()
        for line in help_result.stdout.splitlines()
        if "setforge " in line and "--format=json" in line
    )
    args = shlex.split(example.replace("<profile>", "p"))
    result = runner.invoke(app, [*args[1:], f"--config={cfg_path}"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["command"] == "inspect"


def test_inspect_json_envelope_is_ansi_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, _ = _setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        app,
        [
            "--format=json",
            "inspect",
            "CLAUDE.md",
            "--profile=p",
            f"--config={cfg_path}",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "\x1b[" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["command"] == "inspect"
    data = payload["data"]
    assert data["base_present"] is True
    assert set(data["panes"]) == {"base", "live", "merge"}
    assert "live edit" in data["panes"]["live"]
    assert "upstream edit" in data["panes"]["merge"]
    assert set(data["index"]) == {"shared", "kept_local", "conflict"}
    assert data["staging"]["pending"] == 1
    assert data["staging"]["local"] == 0
    assert data.get("errors", []) == []


def test_inspect_no_base_collapses_to_two_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, _ = _setup(tmp_path, monkeypatch, base=None)
    result = CliRunner().invoke(
        app,
        [
            "--format=json",
            "inspect",
            "CLAUDE.md",
            "--profile=p",
            f"--config={cfg_path}",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["base_present"] is False
    assert data["panes"]["base"] is None
    assert data["staging"] is None


def test_inspect_eligible_converged_file_has_zero_staging_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, _ = _setup(tmp_path, monkeypatch, base=_BASE, live=_BASE, tracked=_BASE)
    result = CliRunner().invoke(
        app,
        [
            "--format=json",
            "inspect",
            "CLAUDE.md",
            "--profile=p",
            f"--config={cfg_path}",
        ],
    )
    assert result.exit_code == 0, result.output
    staging = json.loads(result.stdout)["data"]["staging"]
    assert staging is not None
    assert [
        staging[key]
        for key in (
            "shared",
            "shared_promotable",
            "drafted",
            "reconfirm_required",
            "local",
            "pending",
        )
    ] == [0, 0, 0, 0, 0, 0]


def test_inspect_human_reports_conflict_and_pending_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, _ = _setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        app, ["inspect", "CLAUDE.md", "--profile=p", f"--config={cfg_path}"]
    )
    assert result.exit_code == 0, result.output
    assert "merge conflicts" in result.output
    assert "1 pending" in result.output
    assert "pending unit(s): run `setforge stage CLAUDE.md`" in result.output


def test_inspect_untracked_exits_2_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, _ = _setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        app,
        ["--format=json", "inspect", "nope.md", "--profile=p", f"--config={cfg_path}"],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["command"] == "inspect"
    assert payload["errors"]
    assert any("nope.md" in e for e in payload["errors"])


def test_inspect_untracked_human_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, _ = _setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        app, ["inspect", "nope.md", "--profile=p", f"--config={cfg_path}"]
    )
    assert result.exit_code == 2, result.output


def test_inspect_binary_degrades_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload_bytes = b"\x00\x01binary\x00stuff\n"
    cfg_path, _ = _setup(
        tmp_path,
        monkeypatch,
        base=payload_bytes,
        live=payload_bytes,
        tracked=payload_bytes,
    )
    result = CliRunner().invoke(
        app,
        [
            "--format=json",
            "inspect",
            "CLAUDE.md",
            "--profile=p",
            f"--config={cfg_path}",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert "binary" in data["panes"]["merge"].lower()


def test_inspect_non_tty_is_deterministic_stacked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, _ = _setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        app, ["inspect", "CLAUDE.md", "--profile=p", f"--config={cfg_path}"]
    )
    assert result.exit_code == 0, result.output
    assert result.output.index("live edit") < result.output.index("upstream edit")


def test_inspect_index_shared_kept_local_empty_without_recorded_hunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # shared/kept_local come from the STORE index, not the merge-conflict sides;
    # the old conflict-side derivation would have wrongly reported a kept-local.
    cfg_path, _ = _setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        app,
        [
            "--format=json",
            "inspect",
            "CLAUDE.md",
            "--profile=p",
            f"--config={cfg_path}",
        ],
    )
    assert result.exit_code == 0, result.output
    index = json.loads(result.stdout)["data"]["index"]
    assert index["shared"] == []
    assert index["kept_local"] == []


def test_inspect_index_reflects_recorded_store_classes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Store classification (HunkClass) is authoritative, not the merge stream.
    hunks: list[dict[str, object]] = [
        {
            "kind": "line",
            "cls": "shared",
            "label": "## Shell",
            "live_hash": "sha256:a",
            "unit_id": "s",
        },
        {
            "kind": "line",
            "cls": "local",
            "label": "## Host",
            "live_hash": "sha256:b",
            "unit_id": "h",
        },
    ]
    cfg_path, _ = _setup(tmp_path, monkeypatch, hunks=hunks)
    result = CliRunner().invoke(
        app,
        [
            "--format=json",
            "inspect",
            "CLAUDE.md",
            "--profile=p",
            f"--config={cfg_path}",
        ],
    )
    assert result.exit_code == 0, result.output
    index = json.loads(result.stdout)["data"]["index"]
    assert [r["label"] for r in index["shared"]] == ["## Shell"]
    assert [r["label"] for r in index["kept_local"]] == ["## Host"]


def _two_config_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    dst_a = tmp_path / "live" / "a" / "config.txt"
    dst_b = tmp_path / "live" / "b" / "config.txt"
    for name, dst in (("a", dst_a), ("b", dst_b)):
        _write(repo / "tracked" / f"{name}.txt", f"{name}\n".encode())
        _write(dst, f"{name} live\n".encode())
    cfg_path = repo / "setforge.yaml"
    cfg_path.write_text(
        "version: 1\ntracked_files:\n"
        f"  one:\n    src: a.txt\n    dst: {dst_a}\n"
        f"  two:\n    src: b.txt\n    dst: {dst_b}\n"
        "profiles:\n  p:\n    tracked_files: [one, two]\n",
        encoding="utf-8",
    )
    return cfg_path, dst_a, dst_b


def test_inspect_ambiguous_file_name_lists_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, dst_a, dst_b = _two_config_setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        app, ["inspect", "config.txt", "--profile=p", f"--config={cfg_path}"]
    )
    assert result.exit_code == 2
    assert str(dst_a) in result.output
    assert str(dst_b) in result.output


def test_inspect_resolves_relative_and_home_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, dst_a, dst_b = _two_config_setup(tmp_path, monkeypatch)
    monkeypatch.chdir(dst_b.parent)
    monkeypatch.setenv("HOME", str(dst_a.parent.parent))
    for arg, expected in (
        ("./config.txt", dst_b),
        ("../b/config.txt", dst_b),
        ("~/a/config.txt", dst_a),
    ):
        result = CliRunner().invoke(
            app,
            [
                "--format=json",
                "inspect",
                arg,
                "--profile=p",
                f"--config={cfg_path}",
            ],
        )
        assert result.exit_code == 0, (arg, result.output)
        assert json.loads(result.stdout)["data"]["file"] == str(expected)


def test_inspect_reports_unparseable_structured_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    dst = tmp_path / "live" / "s.json"
    _write(repo / "tracked" / "s.json", b'{"a": 1}\n')
    _write(dst, b'{ "a": ')
    cfg_path = repo / "setforge.yaml"
    cfg_path.write_text(
        f"version: 1\ntracked_files:\n  s:\n    src: s.json\n    dst: {dst}\n"
        "profiles:\n  p:\n    tracked_files: [s]\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app, ["inspect", "s", "--profile=p", f"--config={cfg_path}"]
    )
    assert result.exit_code == 0, result.output
    assert "not parseable" in result.output
    assert "merge clean" not in result.output
    assert "merge is clean" not in result.output


def test_inspect_header_keeps_long_path_unbroken_when_piped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COLUMNS", raising=False)
    cfg_path, dst = _setup(tmp_path, monkeypatch)
    long_dst = dst.parent / ("long-directory-name-" * 6) / "CLAUDE.md"
    _write(long_dst, _LIVE)
    cfg_path.write_text(_config(long_dst), encoding="utf-8")

    result = CliRunner().invoke(
        app, ["inspect", "CLAUDE.md", "--profile=p", f"--config={cfg_path}"]
    )

    assert result.exit_code == 0, result.output
    assert f"inspect {long_dst}" in result.output


_P = "p"


def _installed(
    config_repo: ConfigRepo, name: str, body: bytes
) -> tuple[Path, Path, Path]:
    """Install ``body`` as the tracked file ``name``; return (config, live, tracked)."""
    tracked = config_repo.write_tracked(name, body)
    config = config_repo.write_config(
        profile=_P,
        tracked_files={"cfg": {"src": name, "dst": f"~/.inspect_acc/{name}"}},
    )
    live = Path.home() / ".inspect_acc" / name
    result = _run("install", config)
    assert result.exit_code == 0, result.output
    assert live.read_bytes() == body
    return config, live, tracked


def _run(verb: str, config: Path, *extra: str) -> Result:
    common = [f"--profile={_P}", f"--config={config}"]
    if verb == "install":
        return CliRunner().invoke(
            app,
            [verb, *extra, *common, "--no-secrets-scan", "--no-git-check", "--yes"],
        )
    return CliRunner().invoke(app, ["--format=json", verb, *extra, *common])


def _inspect(config: Path) -> dict[str, Any]:
    result = _run("inspect", config, "cfg")
    assert result.exit_code == 0, result.output
    data: dict[str, Any] = json.loads(result.stdout)["data"]
    return data


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.mark.parametrize("live_after", [None, b""], ids=["deleted", "emptied"])
def test_inspect_shows_a_deleted_destination_as_absent_not_as_stored_bytes(
    config_repo: ConfigRepo, tmp_path: Path, live_after: bytes | None
) -> None:
    config, live, _ = _installed(config_repo, "note.md", b"original text\n")
    if live_after is None:
        live.unlink()
    else:
        live.write_bytes(live_after)
    before = _files(tmp_path)

    data = _inspect(config)

    assert _files(tmp_path) == before
    assert data["base_present"] is True
    assert data["panes"]["base"] == "original text\n"
    assert data["panes"]["live"] == (None if live_after is None else "")
    assert "original text" not in data["panes"]["merge"]
    assert data["index"]["conflict"] == []
    human = CliRunner().invoke(
        app, ["inspect", "cfg", f"--profile={_P}", f"--config={config}"]
    )
    assert human.exit_code == 0, human.output
    assert ("live file is missing" in human.output) is (live_after is None)

    install = _run("install", config)
    assert install.exit_code == 0, install.output
    if live_after is None:
        assert not live.exists()
    else:
        assert live.read_bytes() == b""


def test_inspect_deleted_destination_without_a_recorded_base(
    config_repo: ConfigRepo,
) -> None:
    config_repo.write_tracked("note.md", "tracked text\n")
    config = config_repo.write_config(
        profile=_P,
        tracked_files={"cfg": {"src": "note.md", "dst": "~/.inspect_acc/note.md"}},
    )

    data = _inspect(config)

    assert data["base_present"] is False
    assert data["panes"]["live"] is None
