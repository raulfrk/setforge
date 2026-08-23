"""Tests for the ``setforge profile`` subgroup.

Exercises ``profile list`` and ``profile show`` via Typer's
:class:`CliRunner` against fixture ``setforge.yaml`` files written
to ``tmp_path``. Read-only — no install / sync invocation needed.

The fixture profiles use a small extends chain (``base`` -> ``derived``)
so the provenance tags differentiate ``[from profile base]`` vs
``[from profile derived]`` items.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app

# ---------------------------------------------------------------------------
# Fixture YAML
# ---------------------------------------------------------------------------

_MULTI_PROFILE_YAML = """\
version: 1
tracked_files:
  base_tracked:
    src: base.txt
    dst: ~/.base
  derived_tracked:
    src: derived.txt
    dst: ~/.derived
  has_preserve:
    src: settings.json
    dst: ~/.settings.json
    disposition: forked
marketplaces:
  shared-market:
    source: github
    repo: owner/marketplace
claude_plugins:
  base-plugin:
    marketplace: shared-market
  derived-plugin:
    marketplace: shared-market
packages:
  base-plugin:
    type: plugin
    plugin: base-plugin
  derived-plugin:
    type: plugin
    plugin: derived-plugin
  ms-python.python:
    type: extension
    extension: ms-python.python
  ms-vscode.cpptools:
    type: extension
    extension: ms-vscode.cpptools
profiles:
  base:
    tracked_files: [base_tracked, has_preserve]
    packages: [base-plugin, ms-python.python]
    bootstrap: [~/.claude/header.md]
  derived:
    extends: base
    tracked_files: [derived_tracked]
    packages: [derived-plugin, ms-vscode.cpptools]
"""


def _write_config(tmp_path: Path, content: str) -> Path:
    """Write ``setforge.yaml`` and stub the tracked sources it references."""
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(content, encoding="utf-8")
    tracked = tmp_path / "tracked"
    tracked.mkdir(exist_ok=True)
    for src_name in ("base.txt", "derived.txt", "settings.json"):
        (tracked / src_name).write_text("data\n", encoding="utf-8")
    return cfg


# ---------------------------------------------------------------------------
# profile list
# ---------------------------------------------------------------------------


def test_profile_list_enumerates_profiles_and_extends_chain(tmp_path: Path) -> None:
    """``profile list`` exits 0 and prints every profile plus its chain."""
    cfg = _write_config(tmp_path, _MULTI_PROFILE_YAML)
    result = CliRunner().invoke(app, ["profile", "list", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "base" in result.output
    assert "derived" in result.output
    # The derived profile shows its extends chain root-first.
    assert "base" in result.output.split("derived", 1)[1]


def test_profile_list_empty_config_shows_placeholder(tmp_path: Path) -> None:
    """A config without profiles still exits 0 with a clear placeholder."""
    minimal = """\
version: 1
tracked_files:
  d:
    src: base.txt
    dst: ~/.d
profiles: {}
"""
    cfg = _write_config(tmp_path, minimal)
    result = CliRunner().invoke(app, ["profile", "list", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "no profiles defined" in result.output


# ---------------------------------------------------------------------------
# profile show
# ---------------------------------------------------------------------------


def test_profile_show_includes_all_sections(tmp_path: Path) -> None:
    """``profile show`` renders every documented section in the mockup."""
    cfg = _write_config(tmp_path, _MULTI_PROFILE_YAML)
    result = CliRunner().invoke(app, ["profile", "show", "derived", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    expected_sections = (
        "tracked_files",
        "claude_plugins",
        "marketplaces",
        "host_local_sections",
        "bootstrap",
        "extensions.include",
    )
    for section in expected_sections:
        assert section in result.output, f"missing section {section!r}: {result.output}"


def test_profile_show_renders_inherited_codex_provenance_without_source_contents(
    tmp_path: Path,
) -> None:
    tracked = tmp_path / "tracked/codex"
    tracked.mkdir(parents=True)
    (tracked / "model.toml").write_text(
        'model = "SECRET_SENTINEL_MUST_NOT_RENDER"\n', encoding="utf-8"
    )
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "schema_version: '6.4'\n"
        "minimum_version: '6.4'\n"
        "tracked_files: {}\n"
        "codex:\n"
        "  config:\n"
        "    model: {source: codex/model.toml}\n"
        "  marketplaces:\n"
        "    official: {source: github, repo: owner/repo}\n"
        "  plugins:\n"
        "    review: {marketplace: official}\n"
        "profiles:\n"
        "  base:\n"
        "    codex:\n"
        "      config: [model]\n"
        "  derived:\n"
        "    extends: base\n"
        "    codex:\n"
        "      plugins: [review]\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app, ["profile", "show", "derived", f"--config={config}"]
    )

    assert result.exit_code == 0, result.output
    assert "codex.config (1 effective):" in result.output
    assert "model" in result.output
    assert "[from profile base]" in result.output
    assert "codex.plugins (1 effective):" in result.output
    assert "review" in result.output
    assert "[from profile derived]" in result.output
    assert "SECRET_SENTINEL_MUST_NOT_RENDER" not in result.output


def test_profile_show_renders_host_local_tracked_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tracked destination, mode, and symlink provenance are all visible."""
    from setforge import source as source_mod

    cfg = _write_config(tmp_path, _MULTI_PROFILE_YAML)
    local_config = tmp_path / "local.yaml"
    local_config.write_text(
        """\
tracked_files:
  base_tracked:
    dst: /host/base.txt
    mode: 0o600
  derived_tracked:
    symlink_target: /host/derived-target.txt
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(source_mod, "LOCAL_CONFIG_PATH", local_config)

    result = CliRunner().invoke(app, ["profile", "show", "derived", f"--config={cfg}"])

    assert result.exit_code == 0, result.output
    assert "dst=/host/base.txt" in result.output
    assert "mode=0o600" in result.output
    assert "symlink→/host/derived-target.txt" in result.output


def test_profile_show_provenance_tags_base_vs_derived(tmp_path: Path) -> None:
    """Items defined in the base profile are tagged as such; leaf items too.

    Covers all four list-shaped sections that carry provenance tags:
    tracked_files, claude_plugins, bootstrap, and extensions.include.
    Each has one base-inherited entry and one derived-leaf entry in
    the fixture so the same root-first chain walk is exercised on
    every renderer.
    """
    cfg = _write_config(tmp_path, _MULTI_PROFILE_YAML)
    result = CliRunner().invoke(app, ["profile", "show", "derived", f"--config={cfg}"])
    assert result.exit_code == 0, result.output

    def _line_for(token: str) -> str:
        line = next(
            (entry for entry in result.output.splitlines() if token in entry),
            None,
        )
        assert line is not None, f"missing {token!r} in output: {result.output}"
        return line

    # tracked_files: base_tracked inherited, derived_tracked leaf.
    assert "[from profile base]" in _line_for("base_tracked")
    assert "[from profile derived]" in _line_for("derived_tracked")
    # claude_plugins: base-plugin inherited, derived-plugin leaf.
    assert "[from profile base]" in _line_for("base-plugin")
    assert "[from profile derived]" in _line_for("derived-plugin")
    # bootstrap: header.md is only defined on `base`.
    assert "[from profile base]" in _line_for("header.md")
    # extensions.include: ms-python.python on base, ms-vscode.cpptools on leaf.
    assert "[from profile base]" in _line_for("ms-python.python")
    assert "[from profile derived]" in _line_for("ms-vscode.cpptools")


def test_profile_show_unknown_name_exits_nonzero(tmp_path: Path) -> None:
    """A missing profile triggers SetforgeError → exit 1.

    ``CliRunner`` invokes ``app`` directly, so the outer ``main()``
    wrapper that converts :class:`SetforgeError` to ``typer.secho`` +
    ``sys.exit(1)`` isn't on the call path; instead, the exception
    propagates and is captured via ``result.exception``. The
    matching production-shell behavior is exercised by the
    ``main()``-anchored e2e tests.
    """
    from setforge.errors import SetforgeError

    cfg = _write_config(tmp_path, _MULTI_PROFILE_YAML)
    result = CliRunner().invoke(
        app, ["profile", "show", "nonexistent", f"--config={cfg}"]
    )
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SetforgeError), result.exception
    message = str(result.exception)
    assert "nonexistent" in message
    assert "not defined" in message


def test_profile_show_marketplaces_lists_global_entries(tmp_path: Path) -> None:
    """marketplaces section enumerates the config-level registry."""
    cfg = _write_config(tmp_path, _MULTI_PROFILE_YAML)
    result = CliRunner().invoke(app, ["profile", "show", "derived", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "shared-market" in result.output
    assert "owner/marketplace" in result.output


def test_profile_show_help_exits_0(tmp_path: Path) -> None:
    """``profile show --help`` is callable without a profile argument."""
    del tmp_path
    result = CliRunner().invoke(app, ["profile", "show", "--help"])
    assert result.exit_code == 0, result.output
    assert "Profile name" in result.output


def test_profile_list_help_exits_0(tmp_path: Path) -> None:
    """``profile list --help`` exits 0 with a non-empty help body."""
    del tmp_path
    result = CliRunner().invoke(app, ["profile", "list", "--help"])
    assert result.exit_code == 0, result.output
    # Rich/typer panel rendering wraps the body across many cells; the
    # cheapest invariant is that the call succeeds with a non-trivial body.
    assert len(result.output.strip()) > 0
