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
    preserve_user_keys:
      - allowDangerouslySkipPermissions
      - some.nested > key
marketplaces:
  shared-market:
    source: github
    repo: owner/marketplace
claude_plugins:
  base-plugin:
    marketplace: shared-market
  derived-plugin:
    marketplace: shared-market
profiles:
  base:
    tracked_files: [base_tracked, has_preserve]
    claude_plugins: [base-plugin]
    bootstrap: [~/.claude/header.md]
    extensions:
      include: [ms-python.python]
  derived:
    extends: base
    tracked_files: [derived_tracked]
    claude_plugins: [derived-plugin]
    extensions:
      include: [ms-vscode.cpptools]
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
        "preserve_user_keys",
    )
    for section in expected_sections:
        assert section in result.output, f"missing section {section!r}: {result.output}"


def test_profile_show_provenance_tags_base_vs_derived(tmp_path: Path) -> None:
    """Items defined in the base profile are tagged as such; leaf items too."""
    cfg = _write_config(tmp_path, _MULTI_PROFILE_YAML)
    result = CliRunner().invoke(app, ["profile", "show", "derived", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    # base_tracked is inherited from `base`; derived_tracked is from `derived`.
    base_line = next(
        (line for line in result.output.splitlines() if "base_tracked" in line),
        None,
    )
    assert base_line is not None, result.output
    assert "[from profile base]" in base_line
    derived_line = next(
        (line for line in result.output.splitlines() if "derived_tracked" in line),
        None,
    )
    assert derived_line is not None, result.output
    assert "[from profile derived]" in derived_line


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


def test_profile_show_preserve_user_keys_lists_keys_no_overlay_diff_yet(
    tmp_path: Path,
) -> None:
    """preserve_user_keys section lists per-file keys and cites bd setforge-lgvp.

    The overlay +N/-M diff is out of scope for this bead (per Q10);
    the section is expected to print the bd-id citation line, NOT a
    ``TODO`` comment.
    """
    cfg = _write_config(tmp_path, _MULTI_PROFILE_YAML)
    result = CliRunner().invoke(app, ["profile", "show", "derived", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "has_preserve" in result.output
    assert "allowDangerouslySkipPermissions" in result.output
    # bd id citation visible; no TODO scaffolding leak.
    assert "bd setforge-lgvp" in result.output
    assert "TODO" not in result.output


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
