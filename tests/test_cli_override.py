"""CLI-level tests for the ``override`` command group (the disposition+span
user-facing front door).

Drives the real CLI via Typer's :class:`CliRunner`. Covers the full
verb x file-type x scope matrix (``fork`` / ``pin`` / ``list`` / ``show``,
markdown + structural, host-local + ``--shared``), every parse-time guard
rail, and every ``--shared`` write-discipline item (B-C1..B-C5).

A host-local override writes ``~/.config/setforge/local.yaml``
``tracked_files.<id>.{disposition,spans}`` via the ruamel round-trip; a
``--shared`` override writes the resolved config-repo ``setforge.yaml`` via
``atomicio.atomic_write_text`` and prints the commit/push hint.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from ruamel.yaml import YAML
from typer.testing import CliRunner

import setforge.source as source_mod
from setforge.cli import app

_YAML = YAML(typ="rt")

_MD_DOC = """\
# Title

## Mine

Mine body original.

## Shared

Shared body original.
"""

_YAML_DOC = "editor:\n  fontSize: 12\n  tabSize: 4\nshared:\n  theme: dark\n"

_SETFORGE_YAML = (
    "version: 1\n"
    'schema_version: "1.1"\n'
    "tracked_files:\n"
    "  doc:\n"
    "    src: doc.md\n"
    "    dst: ~/.x/doc.md\n"
    "    disposition: shared\n"
    "  conf:\n"
    "    src: conf.yaml\n"
    "    dst: ~/.x/conf.yaml\n"
    "    disposition: shared\n"
    "profiles:\n"
    "  p:\n"
    "    tracked_files:\n"
    "      - doc\n"
    "      - conf\n"
)


def _make_repo(tmp_path: Path) -> Path:
    """Write a minimal config repo (setforge.yaml + tracked/ sources)."""
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(_SETFORGE_YAML, encoding="utf-8")
    tracked = tmp_path / "tracked"
    tracked.mkdir(exist_ok=True)
    (tracked / "doc.md").write_text(_MD_DOC, encoding="utf-8")
    (tracked / "conf.yaml").write_text(_YAML_DOC, encoding="utf-8")
    return cfg


def _invoke(cfg: Path, *args: str) -> Result:
    """Invoke the CLI with ``--config <cfg>`` so the source layer is bypassed."""
    return CliRunner().invoke(app, [*args, "--config", str(cfg), "--profile", "p"])


def _local_overlay(tf_id: str) -> dict[str, Any]:
    """Read the host-local overlay block for ``tf_id`` from the redirected local.yaml.

    Reads ``source_mod.LOCAL_CONFIG_PATH`` dynamically (not a load-time-bound
    import) so the conftest per-test redirect is honored.
    """
    data = _YAML.load(source_mod.LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
    return dict(data["tracked_files"][tf_id])


# ---------------------------------------------------------------------------
# fork / pin — file-level disposition (no anchor)
# ---------------------------------------------------------------------------


def test_fork_file_level_host_local_writes_local_yaml(tmp_path: Path) -> None:
    """fork <file> (no anchor, host-local) writes local.yaml disposition."""
    cfg = _make_repo(tmp_path)
    result = _invoke(cfg, "override", "fork", "doc")
    assert result.exit_code == 0, result.output
    overlay = _local_overlay("doc")
    assert overlay["disposition"] == "forked"


def test_pin_file_level_host_local_writes_local_yaml(tmp_path: Path) -> None:
    """pin <file> (no anchor, host-local) writes local.yaml disposition."""
    cfg = _make_repo(tmp_path)
    result = _invoke(cfg, "override", "pin", "doc")
    assert result.exit_code == 0, result.output
    overlay = _local_overlay("doc")
    assert overlay["disposition"] == "pinned"


def test_fork_file_level_shared_writes_setforge_yaml(tmp_path: Path) -> None:
    """fork <file> --shared writes setforge.yaml tracked_files.<id>.disposition."""
    cfg = _make_repo(tmp_path)
    # Seed the tracked-side disposition to None so forked is a real change.
    body = _SETFORGE_YAML.replace("    disposition: shared\n  conf:", "  conf:")
    cfg.write_text(body, encoding="utf-8")
    result = _invoke(cfg, "override", "fork", "doc", "--shared")
    assert result.exit_code == 0, result.output
    data = _YAML.load(cfg.read_text(encoding="utf-8"))
    assert data["tracked_files"]["doc"]["disposition"] == "forked"


def test_pin_file_level_shared_writes_setforge_yaml(tmp_path: Path) -> None:
    """pin <file> --shared writes setforge.yaml disposition pinned."""
    cfg = _make_repo(tmp_path)
    result = _invoke(cfg, "override", "pin", "doc", "--shared")
    assert result.exit_code == 0, result.output
    data = _YAML.load(cfg.read_text(encoding="utf-8"))
    assert data["tracked_files"]["doc"]["disposition"] == "pinned"


# ---------------------------------------------------------------------------
# fork / pin — span (with anchor)
# ---------------------------------------------------------------------------


def test_pin_span_md_host_local(tmp_path: Path) -> None:
    """pin <md> "## Mine" appends a host-local SpanEntry to local.yaml."""
    cfg = _make_repo(tmp_path)
    result = _invoke(cfg, "override", "pin", "doc", "## Mine")
    assert result.exit_code == 0, result.output
    overlay = _local_overlay("doc")
    spans = list(overlay["spans"])
    assert len(spans) == 1
    assert spans[0]["anchor"] == "## Mine"
    assert spans[0]["kind"] == "pinned"
    assert spans[0]["semantics"] == "host-local"


def test_fork_span_md_host_local(tmp_path: Path) -> None:
    """fork <md> "## Mine" appends a forked host-local SpanEntry."""
    cfg = _make_repo(tmp_path)
    result = _invoke(cfg, "override", "fork", "doc", "## Mine")
    assert result.exit_code == 0, result.output
    overlay = _local_overlay("doc")
    assert next(iter(overlay["spans"]))["kind"] == "forked"


def test_pin_span_md_shared(tmp_path: Path) -> None:
    """pin <md> "## Mine" --shared appends a shared SpanEntry to setforge.yaml."""
    cfg = _make_repo(tmp_path)
    result = _invoke(cfg, "override", "pin", "doc", "## Mine", "--shared")
    assert result.exit_code == 0, result.output
    data = _YAML.load(cfg.read_text(encoding="utf-8"))
    spans = list(data["tracked_files"]["doc"]["spans"])
    assert spans[0]["anchor"] == "## Mine"
    assert spans[0]["semantics"] == "shared"


def test_pin_span_structural_host_local(tmp_path: Path) -> None:
    """pin <yaml> editor.fontSize appends a host-local structural SpanEntry."""
    cfg = _make_repo(tmp_path)
    result = _invoke(cfg, "override", "pin", "conf", "editor.fontSize")
    assert result.exit_code == 0, result.output
    overlay = _local_overlay("conf")
    assert next(iter(overlay["spans"]))["anchor"] == "editor.fontSize"


def test_pin_span_structural_shared(tmp_path: Path) -> None:
    """pin <yaml> editor.fontSize --shared appends a shared structural span."""
    cfg = _make_repo(tmp_path)
    result = _invoke(cfg, "override", "pin", "conf", "editor.fontSize", "--shared")
    assert result.exit_code == 0, result.output
    data = _YAML.load(cfg.read_text(encoding="utf-8"))
    span = next(iter(data["tracked_files"]["conf"]["spans"]))
    assert span["anchor"] == "editor.fontSize"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_shows_md_disposition_and_span(tmp_path: Path) -> None:
    """list renders the markdown file's disposition + span state."""
    cfg = _make_repo(tmp_path)
    _invoke(cfg, "override", "pin", "doc", "## Mine", "--shared")
    result = _invoke(cfg, "override", "list")
    assert result.exit_code == 0, result.output
    assert "doc" in result.output
    assert "pinned" in result.output or "shared" in result.output


def test_list_shows_structural_disposition_and_span(tmp_path: Path) -> None:
    """list renders a structural file's disposition + span state."""
    cfg = _make_repo(tmp_path)
    _invoke(cfg, "override", "pin", "conf", "editor.fontSize", "--shared")
    result = _invoke(cfg, "override", "list")
    assert result.exit_code == 0, result.output
    assert "conf" in result.output


# ---------------------------------------------------------------------------
# show --spans (stdout only, file byte-unchanged)
# ---------------------------------------------------------------------------


def test_show_spans_md_stdout_only_file_unchanged(tmp_path: Path) -> None:
    """show <md> --spans renders to stdout; the tracked file stays byte-identical."""
    cfg = _make_repo(tmp_path)
    _invoke(cfg, "override", "pin", "doc", "## Mine", "--shared")
    src = tmp_path / "tracked" / "doc.md"
    before = src.read_bytes()
    result = _invoke(cfg, "override", "show", "doc", "--spans")
    assert result.exit_code == 0, result.output
    assert src.read_bytes() == before
    assert "(virtual)" in result.output
    assert "ORPHANED" in result.output


def test_show_spans_structural_stdout_only_file_unchanged(tmp_path: Path) -> None:
    """show <yaml> --spans renders structural virtual comments; file unchanged."""
    cfg = _make_repo(tmp_path)
    _invoke(cfg, "override", "pin", "conf", "editor.fontSize", "--shared")
    src = tmp_path / "tracked" / "conf.yaml"
    before = src.read_bytes()
    result = _invoke(cfg, "override", "show", "conf", "--spans")
    assert result.exit_code == 0, result.output
    assert src.read_bytes() == before
    assert "editor.fontSize" in result.output
    assert "ORPHANED" in result.output


def test_show_spans_orphaned_column_marks_missing_anchor(tmp_path: Path) -> None:
    """A span whose anchor no longer resolves renders ORPHANED = yes."""
    cfg = _make_repo(tmp_path)
    _invoke(cfg, "override", "pin", "conf", "editor.fontSize", "--shared")
    # Remove the anchored key from the tracked source so the span orphans.
    (tmp_path / "tracked" / "conf.yaml").write_text(
        "shared:\n  theme: dark\n", encoding="utf-8"
    )
    result = _invoke(cfg, "override", "show", "conf", "--spans")
    assert result.exit_code == 0, result.output
    assert "yes" in result.output.lower()


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


def test_guard_legacy_preserve_refused(tmp_path: Path) -> None:
    """pin on a legacy preserve_* file is refused with a clear error (I14)."""
    cfg = _make_repo(tmp_path)
    body = (
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.x/doc.md\n"
        "    preserve_user_sections: true\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files:\n"
        "      - doc\n"
    )
    cfg.write_text(body, encoding="utf-8")
    result = _invoke(cfg, "override", "pin", "doc")
    assert result.exit_code != 0
    assert "preserve" in result.output.lower()


def test_guard_wrong_file_type_heading_on_yaml(tmp_path: Path) -> None:
    """A heading anchor on a structural file is refused at parse time."""
    cfg = _make_repo(tmp_path)
    result = _invoke(cfg, "override", "pin", "conf", "## Mine")
    assert result.exit_code != 0
    assert "heading" in result.output.lower() or "dotted" in result.output.lower()


def test_guard_wrong_file_type_dotted_on_md(tmp_path: Path) -> None:
    """A dotted-path anchor on a markdown file is refused at parse time."""
    cfg = _make_repo(tmp_path)
    result = _invoke(cfg, "override", "pin", "doc", "editor.fontSize")
    assert result.exit_code != 0
    assert "heading" in result.output.lower() or "dotted" in result.output.lower()


def test_guard_span_overlaps_user_section_refused(tmp_path: Path) -> None:
    """A span overlapping a user-section marker is refused at pin time."""
    cfg = _make_repo(tmp_path)
    doc = (
        "# Title\n\n"
        "## Mine\n\n"
        "<!-- setforge:user-section start host-local FOO -->\n"
        "body\n"
        "<!-- setforge:user-section end host-local FOO -->\n"
    )
    (tmp_path / "tracked" / "doc.md").write_text(doc, encoding="utf-8")
    result = _invoke(cfg, "override", "pin", "doc", "## Mine")
    assert result.exit_code != 0
    assert "user-section" in result.output.lower() or "section" in result.output.lower()


def test_guard_idempotent_file_level_no_op(tmp_path: Path) -> None:
    """Re-issuing the same disposition is a no-op, not an error."""
    cfg = _make_repo(tmp_path)
    _invoke(cfg, "override", "pin", "doc")
    result = _invoke(cfg, "override", "pin", "doc")
    assert result.exit_code == 0, result.output
    assert "already" in result.output.lower()


def test_guard_idempotent_span_no_op(tmp_path: Path) -> None:
    """Re-issuing the same span is a no-op."""
    cfg = _make_repo(tmp_path)
    _invoke(cfg, "override", "pin", "doc", "## Mine")
    result = _invoke(cfg, "override", "pin", "doc", "## Mine")
    assert result.exit_code == 0, result.output
    assert "already" in result.output.lower()
    overlay = _local_overlay("doc")
    assert len(list(overlay["spans"])) == 1


def test_guard_pin_over_fork_upgrade_allowed(tmp_path: Path) -> None:
    """pin upgrading an existing fork is allowed (file-level)."""
    cfg = _make_repo(tmp_path)
    _invoke(cfg, "override", "fork", "doc")
    result = _invoke(cfg, "override", "pin", "doc")
    assert result.exit_code == 0, result.output
    overlay = _local_overlay("doc")
    assert overlay["disposition"] == "pinned"


def test_guard_fork_over_pin_downgrade_refused(tmp_path: Path) -> None:
    """fork downgrading an existing pin is refused (file-level)."""
    cfg = _make_repo(tmp_path)
    _invoke(cfg, "override", "pin", "doc")
    result = _invoke(cfg, "override", "fork", "doc")
    assert result.exit_code != 0
    assert "downgrade" in result.output.lower() or "pinned" in result.output.lower()
    overlay = _local_overlay("doc")
    assert overlay["disposition"] == "pinned"


def test_guard_pin_over_fork_span_upgrade_allowed(tmp_path: Path) -> None:
    """pin upgrading a forked span on the same anchor is allowed."""
    cfg = _make_repo(tmp_path)
    _invoke(cfg, "override", "fork", "doc", "## Mine")
    result = _invoke(cfg, "override", "pin", "doc", "## Mine")
    assert result.exit_code == 0, result.output
    overlay = _local_overlay("doc")
    spans = list(overlay["spans"])
    assert len(spans) == 1
    assert spans[0]["kind"] == "pinned"


def test_guard_fork_over_pin_span_downgrade_refused(tmp_path: Path) -> None:
    """fork downgrading a pinned span on the same anchor is refused."""
    cfg = _make_repo(tmp_path)
    _invoke(cfg, "override", "pin", "doc", "## Mine")
    result = _invoke(cfg, "override", "fork", "doc", "## Mine")
    assert result.exit_code != 0
    overlay = _local_overlay("doc")
    assert next(iter(overlay["spans"]))["kind"] == "pinned"


def test_guard_overlapping_structural_span_refused(tmp_path: Path) -> None:
    """A structural span nested under an existing pin is refused (I11)."""
    cfg = _make_repo(tmp_path)
    _invoke(cfg, "override", "pin", "conf", "editor")
    result = _invoke(cfg, "override", "pin", "conf", "editor.fontSize")
    assert result.exit_code != 0
    assert "overlap" in result.output.lower() or "prefix" in result.output.lower()


def test_guard_unknown_tracked_file_refused(tmp_path: Path) -> None:
    """An unknown tracked_file id is refused with a clear error."""
    cfg = _make_repo(tmp_path)
    result = _invoke(cfg, "override", "pin", "nope")
    assert result.exit_code != 0
    assert "nope" in result.output


def test_guard_anchor_not_found_refused(tmp_path: Path) -> None:
    """A heading anchor absent from the markdown file is refused."""
    cfg = _make_repo(tmp_path)
    result = _invoke(cfg, "override", "pin", "doc", "## Nonexistent")
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# --shared write discipline (B-C1..B-C5)
# ---------------------------------------------------------------------------


def _git_init(repo: Path) -> None:
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "init"],
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True, text=True)


def test_shared_b_c1_atomic_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B-C1: the --shared write routes through atomicio.atomic_write_text."""
    cfg = _make_repo(tmp_path)
    import setforge.cli.override as override_mod

    seen: list[Path] = []
    real = override_mod.atomic_write_text

    def spy(path: Path, text: str, **kw: object) -> None:
        seen.append(path)
        real(path, text, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(override_mod, "atomic_write_text", spy)
    result = _invoke(cfg, "override", "pin", "doc", "--shared")
    assert result.exit_code == 0, result.output
    assert cfg in seen


def test_shared_b_c2_dirty_root_refused(tmp_path: Path) -> None:
    """B-C2: a dirty setforge.yaml at the source root refuses the --shared write."""
    cfg = _make_repo(tmp_path)
    _git_init(tmp_path)
    cfg.write_text(_SETFORGE_YAML + "# dirty edit\n", encoding="utf-8")
    result = _invoke(cfg, "override", "pin", "doc", "--shared")
    assert result.exit_code != 0
    assert "uncommitted" in result.output.lower() or "dirty" in result.output.lower()


def test_shared_b_c2_clean_root_allowed(tmp_path: Path) -> None:
    """B-C2: a clean git source root permits the --shared write."""
    cfg = _make_repo(tmp_path)
    _git_init(tmp_path)
    result = _invoke(cfg, "override", "pin", "doc", "--shared")
    assert result.exit_code == 0, result.output


def test_shared_b_c3_post_write_hint_emitted(tmp_path: Path) -> None:
    """B-C3: a successful --shared write prints the commit/push hint."""
    cfg = _make_repo(tmp_path)
    _git_init(tmp_path)
    result = _invoke(cfg, "override", "pin", "doc", "--shared")
    assert result.exit_code == 0, result.output
    assert "git commit" in result.output


def test_shared_post_write_hint_names_setforge_yaml_not_tracked(
    tmp_path: Path,
) -> None:
    """The --shared hint names the written setforge.yaml, not tracked/.

    The --shared write lands on the source-root setforge.yaml, so the hint's
    ``git diff`` would surface nothing if it named tracked/ (which the write
    never touches).
    """
    cfg = _make_repo(tmp_path)
    _git_init(tmp_path)
    result = _invoke(cfg, "override", "pin", "doc", "--shared")
    assert result.exit_code == 0, result.output
    assert "setforge.yaml" in result.output
    assert "tracked/" not in result.output


def test_shared_b_c4_resolves_via_source_not_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B-C4: target resolves through _resolve_config_arg, never Path.cwd()."""
    cfg = _make_repo(tmp_path)
    monkeypatch.setenv("SETFORGE_SOURCE", str(tmp_path))
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    # No --config: must resolve via the source layer (SETFORGE_SOURCE), not CWD.
    result = CliRunner().invoke(
        app, ["override", "pin", "doc", "--shared", "--profile", "p"]
    )
    assert result.exit_code == 0, result.output
    data = _YAML.load(cfg.read_text(encoding="utf-8"))
    assert data["tracked_files"]["doc"]["disposition"] == "pinned"


def test_shared_b_c5_symlinked_config_refused(tmp_path: Path) -> None:
    """B-C5: a symlinked setforge.yaml is refused (no silent link replace)."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_cfg = real_dir / "setforge.yaml"
    real_cfg.write_text(_SETFORGE_YAML, encoding="utf-8")
    tracked = tmp_path / "tracked"
    tracked.mkdir()
    (tracked / "doc.md").write_text(_MD_DOC, encoding="utf-8")
    (tracked / "conf.yaml").write_text(_YAML_DOC, encoding="utf-8")
    link = tmp_path / "setforge.yaml"
    link.symlink_to(real_cfg)
    result = _invoke(link, "override", "pin", "doc", "--shared")
    assert result.exit_code != 0
    assert "symlink" in result.output.lower()


def test_shared_preserves_comments_and_key_order(tmp_path: Path) -> None:
    """The --shared round-trip preserves comments + key order in setforge.yaml."""
    cfg = _make_repo(tmp_path)
    body = (
        "# top comment\n"
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md  # inline\n"
        "    dst: ~/.x/doc.md\n"
        "    disposition: shared\n"
        "  conf:\n"
        "    src: conf.yaml\n"
        "    dst: ~/.x/conf.yaml\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files:\n"
        "      - doc\n"
        "      - conf\n"
    )
    cfg.write_text(body, encoding="utf-8")
    result = _invoke(cfg, "override", "pin", "doc", "## Mine", "--shared")
    assert result.exit_code == 0, result.output
    text = cfg.read_text(encoding="utf-8")
    assert "# top comment" in text
    assert "# inline" in text
    # key order: version before tracked_files before profiles
    assert (
        text.index("version:") < text.index("tracked_files:") < text.index("profiles:")
    )
