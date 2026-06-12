"""Tests for ``setforge validate`` subcommand.

Covers each of the six failure modes plus a clean-run baseline:
1. Clean run → exit 0.
2. Pydantic schema error → exit 1, message names the key.
3. Missing profile (--profile=does-not-exist) → exit 1.
4. Profile cycle (a extends b, b extends a) → exit 1.
5. Missing tracked src → exit 1.
6. Unrenderable Jinja2 template → exit 1.
7. claude_plugins references unknown marketplace → exit 1.
8. Extension include: empty ID → exit 1.
9. Extension include: duplicate ID → exit 1.
10. Undefined template variable (StrictUndefined) → exit 1.
11. claude_plugins: empty ref → exit 1.
12. claude_plugins: duplicate ref → exit 1.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app

# ---------------------------------------------------------------------------
# Minimal YAML builder helpers
# ---------------------------------------------------------------------------

_CLEAN_YAML = """\
version: 1
tracked_files:
  d:
    src: tracked_file.txt
    dst: ~/.some-tracked_file
profiles:
  p:
    tracked_files: [d]
"""

_CLEAN_WITH_PLUGIN_YAML = """\
version: 1
tracked_files:
  d:
    src: tracked_file.txt
    dst: ~/.some-tracked_file
marketplaces:
  my-market:
    source: github
    repo: owner/repo
claude_plugins:
  myplugin:
    marketplace: my-market
profiles:
  p:
    tracked_files: [d]
    claude_plugins: [myplugin]
"""


def _write_config(tmp_path: Path, content: str, *, create_src: bool = True) -> Path:
    """Write setforge.yaml and optionally create the dummy tracked file."""
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(content, encoding="utf-8")
    (tmp_path / "tracked").mkdir(exist_ok=True)
    if create_src:
        (tmp_path / "tracked" / "tracked_file.txt").write_text(
            "data\n", encoding="utf-8"
        )
    return cfg


# ---------------------------------------------------------------------------
# Test 1: clean run exits 0
# ---------------------------------------------------------------------------


def test_validate_clean_run_exits_0(tmp_path: Path) -> None:
    """A well-formed config with all srcs present exits 0 and prints 'ok'."""
    cfg = _write_config(tmp_path, _CLEAN_YAML)
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output


def test_validate_all_clean_exits_0(tmp_path: Path) -> None:
    """--all on a well-formed config exits 0."""
    cfg = _write_config(tmp_path, _CLEAN_YAML)
    result = CliRunner().invoke(app, ["validate", "--all", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output


# ---------------------------------------------------------------------------
# Test 2: Pydantic schema error
# ---------------------------------------------------------------------------


def test_validate_schema_error_exits_1(tmp_path: Path) -> None:
    """Pydantic schema error (extra field on tracked_file) → exit 1."""
    bad_yaml = """\
version: 1
tracked_files:
  d:
    src: tracked_file.txt
    dst: ~/.some-tracked_file
    not_a_real_field: true
profiles:
  p:
    tracked_files: [d]
"""
    cfg = _write_config(tmp_path, bad_yaml)
    result = CliRunner().invoke(app, ["validate", "--all", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    # Message should mention the bad key
    assert "not_a_real_field" in result.output or "schema" in result.output


# ---------------------------------------------------------------------------
# Test 3: missing profile
# ---------------------------------------------------------------------------


def test_validate_missing_profile_exits_1(tmp_path: Path) -> None:
    """--profile= pointing at a non-existent profile → exit 1."""
    cfg = _write_config(tmp_path, _CLEAN_YAML)
    result = CliRunner().invoke(
        app, ["validate", "--profile=does-not-exist", f"--config={cfg}"]
    )
    assert result.exit_code == 1, result.output
    # config.py raises ProfileNotFound with "profile not found: <name>"
    assert "does-not-exist" in result.output


# ---------------------------------------------------------------------------
# Test 4: profile cycle
# ---------------------------------------------------------------------------


def test_validate_profile_cycle_exits_1(tmp_path: Path) -> None:
    """Profile cycle (a extends b, b extends a) → exit 1."""
    cyclic_yaml = """\
version: 1
tracked_files:
  d:
    src: tracked_file.txt
    dst: ~/.some-tracked_file
profiles:
  a:
    extends: b
    tracked_files: [d]
  b:
    extends: a
    tracked_files: [d]
"""
    cfg = _write_config(tmp_path, cyclic_yaml)
    result = CliRunner().invoke(app, ["validate", "--profile=a", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    # config.py raises ConfigError with "profile cycle: ..."
    assert "cycle" in result.output


# ---------------------------------------------------------------------------
# Test 5: missing tracked src
# ---------------------------------------------------------------------------


def test_validate_missing_src_exits_1(tmp_path: Path) -> None:
    """A tracked_file whose src does not exist on disk → exit 1."""
    # create_src=False so tracked_file.txt is absent
    cfg = _write_config(tmp_path, _CLEAN_YAML, create_src=False)
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    combined = result.output
    assert "tracked_file.txt" in combined or "does not exist" in combined


# ---------------------------------------------------------------------------
# Test 6: unrenderable Jinja2 template
# ---------------------------------------------------------------------------


def test_validate_unrenderable_template_exits_1(tmp_path: Path) -> None:
    """A Jinja2 syntax error in a template dst → exit 1."""
    broken_template_yaml = """\
version: 1
tracked_files:
  d:
    src: tracked_file.txt
    dst: "{% for x in %}broken"
    template: true
profiles:
  p:
    tracked_files: [d]
"""
    cfg = _write_config(tmp_path, broken_template_yaml)
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "template" in result.output or "unrenderable" in result.output


# ---------------------------------------------------------------------------
# Test 7: claude_plugins references unknown marketplace
# ---------------------------------------------------------------------------


def test_validate_unknown_marketplace_exits_1(tmp_path: Path) -> None:
    """A plugin whose marketplace is absent from the marketplaces block → exit 1."""
    bad_mp_yaml = """\
version: 1
tracked_files:
  d:
    src: tracked_file.txt
    dst: ~/.some-tracked_file
marketplaces: {}
claude_plugins:
  myplugin:
    marketplace: ghost-market
profiles:
  p:
    tracked_files: [d]
    claude_plugins: [myplugin]
"""
    cfg = _write_config(tmp_path, bad_mp_yaml)
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    combined = result.output
    assert "ghost-market" in combined or "marketplace" in combined


# ---------------------------------------------------------------------------
# Test 8: extension include — empty ID
# ---------------------------------------------------------------------------


def test_validate_ext_include_empty_id_exits_1(tmp_path: Path) -> None:
    """An empty string in extensions.include → exit 1, message names the profile."""
    ext_empty_yaml = """\
version: 1
tracked_files:
  d:
    src: tracked_file.txt
    dst: ~/.some-tracked_file
profiles:
  p:
    tracked_files: [d]
    extensions:
      include: ["valid.ext", ""]
"""
    cfg = _write_config(tmp_path, ext_empty_yaml)
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "empty" in result.output or "extensions.include" in result.output
    assert "p" in result.output


# ---------------------------------------------------------------------------
# Test 9: extension include — duplicate ID
# ---------------------------------------------------------------------------


def test_validate_ext_include_duplicate_exits_1(tmp_path: Path) -> None:
    """A duplicate extension ID within a single profile's include list → exit 1.

    The check runs against the raw profile (before extends-merging) so that
    duplicates silently dropped by _merge_list are still caught at their source.
    """
    ext_dup_yaml = """\
version: 1
tracked_files:
  d:
    src: tracked_file.txt
    dst: ~/.some-tracked_file
profiles:
  p:
    tracked_files: [d]
    extensions:
      include: ["foo.bar", "foo.bar"]
"""
    cfg = _write_config(tmp_path, ext_dup_yaml)
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "foo.bar" in result.output
    assert "duplicate" in result.output


# ---------------------------------------------------------------------------
# Test 10: undefined template variable (StrictUndefined BLOCKING fix)
# ---------------------------------------------------------------------------


def test_validate_undefined_template_variable_exits_1(tmp_path: Path) -> None:
    """A dst template referencing an undefined variable → exit 1 (StrictUndefined)."""
    undef_var_yaml = """\
version: 1
tracked_files:
  d:
    src: tracked_file.txt
    dst: "{{ undefined_var }}/x.json"
    template: true
profiles:
  p:
    tracked_files: [d]
"""
    cfg = _write_config(tmp_path, undef_var_yaml)
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    # Message must identify the offending tracked_file and the undefined variable name
    assert "d" in result.output
    assert "undefined_var" in result.output


# ---------------------------------------------------------------------------
# Mutex flag validation
# ---------------------------------------------------------------------------


def test_validate_both_flags_exits_2(tmp_path: Path) -> None:
    """Passing both --profile and --all exits 2."""
    cfg = _write_config(tmp_path, _CLEAN_YAML)
    result = CliRunner().invoke(
        app, ["validate", "--profile=p", "--all", f"--config={cfg}"]
    )
    assert result.exit_code == 2, result.output


def test_validate_neither_flag_exits_2(tmp_path: Path) -> None:
    """Passing neither --profile nor --all exits 2."""
    cfg = _write_config(tmp_path, _CLEAN_YAML)
    result = CliRunner().invoke(app, ["validate", f"--config={cfg}"])
    assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# Aggregation: multiple failures reported together
# ---------------------------------------------------------------------------


def test_validate_aggregates_failures(tmp_path: Path) -> None:
    """Two profiles each with a missing src should both appear in output."""
    two_profile_yaml = """\
version: 1
tracked_files:
  d1:
    src: missing1.txt
    dst: ~/.d1
  d2:
    src: missing2.txt
    dst: ~/.d2
profiles:
  pa:
    tracked_files: [d1]
  pb:
    tracked_files: [d2]
"""
    cfg = _write_config(tmp_path, two_profile_yaml, create_src=False)
    result = CliRunner().invoke(app, ["validate", "--all", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    # Both missing srcs should be reported
    assert "missing1.txt" in result.output
    assert "missing2.txt" in result.output


# ---------------------------------------------------------------------------
# Test 11: claude_plugins — empty ref
# ---------------------------------------------------------------------------


def test_validate_empty_plugin_ref_exits_1(tmp_path: Path) -> None:
    """An empty string in claude_plugins → exit 1, message names the profile."""
    empty_plugin_yaml = """\
version: 1
tracked_files:
  d:
    src: tracked_file.txt
    dst: ~/.some-tracked_file
profiles:
  p:
    tracked_files: [d]
    claude_plugins: [""]
"""
    cfg = _write_config(tmp_path, empty_plugin_yaml)
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "claude_plugins contains empty ref" in result.output
    assert "p" in result.output


# ---------------------------------------------------------------------------
# Test 12: claude_plugins — duplicate ref
# ---------------------------------------------------------------------------


def test_validate_duplicate_plugin_ref_exits_1(tmp_path: Path) -> None:
    """A duplicate ref in claude_plugins → exit 1, message names the ref and profile.

    The check runs against the raw profile (before extends-merging) so that
    duplicates silently dropped by _merge_list are still caught at their source.
    """
    dup_plugin_yaml = """\
version: 1
tracked_files:
  d:
    src: tracked_file.txt
    dst: ~/.some-tracked_file
marketplaces:
  my-market:
    source: github
    repo: owner/repo
claude_plugins:
  myplugin:
    marketplace: my-market
profiles:
  p:
    tracked_files: [d]
    claude_plugins: ["myplugin", "myplugin"]
"""
    cfg = _write_config(tmp_path, dup_plugin_yaml)
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "claude_plugins duplicate" in result.output
    assert "'myplugin'" in result.output


def test_validate_double_empty_emits_single_message_per_field(tmp_path: Path) -> None:
    """N empty entries collapse to one error line per field per profile.

    Without dedup, ['', ''] would produce two identical 'contains empty'
    messages — same shape as Check 5 / 5b's loop firing per-iteration.
    """
    double_empty_yaml = """\
version: 1
tracked_files:
  d:
    src: tracked_file.txt
    dst: ~/.some-tracked_file
marketplaces:
  my-market:
    source: github
    repo: owner/repo
claude_plugins:
  myplugin:
    marketplace: my-market
profiles:
  p:
    tracked_files: [d]
    extensions:
      include: ["", ""]
    claude_plugins: ["", ""]
"""
    cfg = _write_config(tmp_path, double_empty_yaml)
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert result.output.count("extensions.include contains empty ID") == 1
    assert result.output.count("claude_plugins contains empty ref") == 1


def test_validate_triple_duplicate_emits_single_message_per_value(
    tmp_path: Path,
) -> None:
    """N copies of the same value collapse to one duplicate line per value per field.

    Without dedup, ['x', 'x', 'x'] would produce two identical 'duplicate' messages
    (one per repeat after the first) — same shape as Check 5 / 5b's loop firing
    per-iteration on each subsequent occurrence.
    """
    triple_dup_yaml = """\
version: 1
tracked_files:
  d:
    src: tracked_file.txt
    dst: ~/.some-tracked_file
marketplaces:
  my-market:
    source: github
    repo: owner/repo
claude_plugins:
  myplugin:
    marketplace: my-market
profiles:
  p:
    tracked_files: [d]
    extensions:
      include: ["foo.bar", "foo.bar", "foo.bar"]
    claude_plugins: ["myplugin", "myplugin", "myplugin"]
"""
    cfg = _write_config(tmp_path, triple_dup_yaml)
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert result.output.count("extensions.include duplicate: 'foo.bar'") == 1
    assert result.output.count("claude_plugins duplicate: 'myplugin'") == 1


# ---------------------------------------------------------------------------
# Test 13 / 14: preserve_user_keys overlay.
# Validate must surface the same collision / unknown-remove failures as
# install / compare so misconfigured local.yaml is caught offline before
# the user runs a deploy.
# ---------------------------------------------------------------------------


def test_validate_span_on_non_markdown_file_exits_1(tmp_path: Path) -> None:
    """A HEADING span anchor declared on a yaml/json tracked_file → exit 1.

    Mirrors the install-time file-type gate so the offline CI gate
    (``setforge validate``) catches a wrong-grammar span anchor (here a
    heading anchor on a structural file) before install would fail with a
    confusing runtime re-assert miss.
    """
    span_yaml = """\
version: 1
tracked_files:
  d:
    src: config.json
    dst: ~/.some-tracked_file
    spans:
      - anchor: "## My Tweaks"
        kind: pinned
        semantics: shared
profiles:
  p:
    tracked_files: [d]
"""
    cfg = _write_config(tmp_path, span_yaml, create_src=False)
    (tmp_path / "tracked" / "config.json").write_text("{}\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    # A heading anchor on a structural file is rejected: anchors must be dotted.
    assert "dotted path" in result.output, result.output
    assert "'d'" in result.output, result.output


def test_validate_dotted_span_on_structural_file_exits_0(tmp_path: Path) -> None:
    """A DOTTED-PATH span anchor on a yaml/json tracked_file passes the gate."""
    span_yaml = """\
version: 1
tracked_files:
  d:
    src: config.json
    dst: ~/.some-tracked_file
    disposition: shared
    spans:
      - anchor: editor.fontSize
        kind: pinned
        semantics: shared
profiles:
  p:
    tracked_files: [d]
"""
    cfg = _write_config(tmp_path, span_yaml, create_src=False)
    (tmp_path / "tracked" / "config.json").write_text(
        '{"editor": {"fontSize": 12}}\n', encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0, result.output


def test_validate_overlapping_structural_pins_exits_1(tmp_path: Path) -> None:
    """Overlapping structural span pins are caught by the offline gate (I11).

    The overlap guard otherwise fires only mid-install (a ConfigError); the
    offline ``validate`` gate must reject ``editor`` and ``editor.fontSize``
    (one prefixes the other) up front with a clear message.
    """
    span_yaml = """\
version: 1
tracked_files:
  d:
    src: config.json
    dst: ~/.some-tracked_file
    disposition: shared
    spans:
      - anchor: editor
        kind: pinned
        semantics: shared
      - anchor: editor.fontSize
        kind: pinned
        semantics: shared
profiles:
  p:
    tracked_files: [d]
"""
    cfg = _write_config(tmp_path, span_yaml, create_src=False)
    (tmp_path / "tracked" / "config.json").write_text(
        '{"editor": {"fontSize": 12}}\n', encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "overlapping" in result.output, result.output
    assert "'d'" in result.output, result.output


def test_validate_list_index_structural_anchor_exits_1(tmp_path: Path) -> None:
    """A list-index structural span anchor is caught by the offline gate (I10).

    A ``a[*]`` index anchor has no stable key identity; the install-time merge
    rejects it, and so must ``validate`` — a dotted-path anchor must address a
    mapping leaf or whole subtree.
    """
    span_yaml = """\
version: 1
tracked_files:
  d:
    src: config.json
    dst: ~/.some-tracked_file
    disposition: shared
    spans:
      - anchor: "servers[*]"
        kind: pinned
        semantics: shared
profiles:
  p:
    tracked_files: [d]
"""
    cfg = _write_config(tmp_path, span_yaml, create_src=False)
    (tmp_path / "tracked" / "config.json").write_text("{}\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "list suffix" in result.output, result.output
    assert "'d'" in result.output, result.output


def test_validate_span_on_markdown_file_exits_0(tmp_path: Path) -> None:
    """A span anchor on a markdown tracked_file passes the file-type gate."""
    span_yaml = """\
version: 1
tracked_files:
  d:
    src: note.md
    dst: ~/.some-tracked_file
    spans:
      - anchor: "## My Tweaks"
        kind: pinned
        semantics: shared
profiles:
  p:
    tracked_files: [d]
"""
    cfg = _write_config(tmp_path, span_yaml, create_src=False)
    (tmp_path / "tracked" / "note.md").write_text(
        "## My Tweaks\n\nbody\n", encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Structural span path-existence check (kind-agnostic, stateless).
# A pinned/forked span whose dotted path no longer exists in the tracked src
# silently leaks values at sync / loses them at install; validate must name
# every dead path (report-all) with the first missing prefix segment.
# ---------------------------------------------------------------------------


def test_validate_absent_forked_span_path_exits_1(tmp_path: Path) -> None:
    """A forked span pointing at an absent path → exit 1, row names the anchor.

    Root-level miss: the first missing prefix is the first segment itself.
    """
    span_yaml = """\
version: 1
tracked_files:
  d:
    src: config.json
    dst: ~/.some-tracked_file
    disposition: shared
    spans:
      - anchor: telemetry.level
        kind: forked
        semantics: shared
profiles:
  p:
    tracked_files: [d]
"""
    cfg = _write_config(tmp_path, span_yaml, create_src=False)
    (tmp_path / "tracked" / "config.json").write_text("{}\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "forked span 'telemetry.level'" in result.output, result.output
    assert "path not found (missing at 'telemetry')" in result.output, result.output
    assert "add the key to the tracked src or remove the span" in result.output


def test_validate_absent_pinned_span_path_mid_walk_names_first_missing_prefix(
    tmp_path: Path,
) -> None:
    """A pinned span missing at the LEAF carries the deepest missing prefix.

    ``editor`` exists but ``editor.fontSize`` does not — the row must point
    at ``editor.fontSize`` (where the walk stopped), not the root.
    """
    span_yaml = """\
version: 1
tracked_files:
  d:
    src: config.json
    dst: ~/.some-tracked_file
    disposition: shared
    spans:
      - anchor: editor.fontSize
        kind: pinned
        semantics: shared
profiles:
  p:
    tracked_files: [d]
"""
    cfg = _write_config(tmp_path, span_yaml, create_src=False)
    (tmp_path / "tracked" / "config.json").write_text(
        '{"editor": {"tabSize": 4}}\n', encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "pinned span 'editor.fontSize'" in result.output, result.output
    assert "missing at 'editor.fontSize'" in result.output, result.output


def test_validate_absent_span_paths_report_all_across_files(tmp_path: Path) -> None:
    """Every absent span across ALL tracked files lands a row — no fail-fast."""
    span_yaml = """\
version: 1
tracked_files:
  a:
    src: a.json
    dst: ~/.a
    disposition: shared
    spans:
      - anchor: alpha.one
        kind: pinned
        semantics: shared
      - anchor: alpha.two
        kind: forked
        semantics: shared
  b:
    src: b.yaml
    dst: ~/.b
    disposition: shared
    spans:
      - anchor: beta.three
        kind: pinned
        semantics: shared
profiles:
  p:
    tracked_files: [a, b]
"""
    cfg = _write_config(tmp_path, span_yaml, create_src=False)
    (tmp_path / "tracked" / "a.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "tracked" / "b.yaml").write_text("other: 1\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "pinned span 'alpha.one'" in result.output, result.output
    assert "forked span 'alpha.two'" in result.output, result.output
    assert "pinned span 'beta.three'" in result.output, result.output


def test_validate_unparseable_structural_src_single_row_continues(
    tmp_path: Path,
) -> None:
    """An unparseable src yields exactly ONE row; other files still checked."""
    span_yaml = """\
version: 1
tracked_files:
  bad:
    src: bad.yaml
    dst: ~/.bad
    disposition: shared
    spans:
      - anchor: one.a
        kind: pinned
        semantics: shared
      - anchor: two.b
        kind: forked
        semantics: shared
  good:
    src: good.json
    dst: ~/.good
    disposition: shared
    spans:
      - anchor: gamma.leaf
        kind: pinned
        semantics: shared
profiles:
  p:
    tracked_files: [bad, good]
"""
    cfg = _write_config(tmp_path, span_yaml, create_src=False)
    (tmp_path / "tracked" / "bad.yaml").write_text("key: [unclosed\n", encoding="utf-8")
    (tmp_path / "tracked" / "good.json").write_text("{}\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert result.output.count("unparseable src") == 1, result.output
    assert "pinned span 'gamma.leaf'" in result.output, result.output


def test_validate_missing_src_skips_path_check_no_double_report(
    tmp_path: Path,
) -> None:
    """A missing src is Check 4's failure; the path check stays silent."""
    span_yaml = """\
version: 1
tracked_files:
  d:
    src: gone.json
    dst: ~/.some-tracked_file
    disposition: shared
    spans:
      - anchor: editor.fontSize
        kind: pinned
        semantics: shared
profiles:
  p:
    tracked_files: [d]
"""
    cfg = _write_config(tmp_path, span_yaml, create_src=False)
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "src gone.json does not exist" in result.output, result.output
    assert "path not found" not in result.output, result.output


def test_validate_forked_span_subtree_exits_1(tmp_path: Path) -> None:
    """A forked span resolving to a MAPPING fails: forked paths are scalar."""
    span_yaml = """\
version: 1
tracked_files:
  d:
    src: config.json
    dst: ~/.some-tracked_file
    disposition: shared
    spans:
      - anchor: editor
        kind: forked
        semantics: shared
profiles:
  p:
    tracked_files: [d]
"""
    cfg = _write_config(tmp_path, span_yaml, create_src=False)
    (tmp_path / "tracked" / "config.json").write_text(
        '{"editor": {"fontSize": 12}}\n', encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "forked spans take a scalar path" in result.output, result.output
    assert "'editor'" in result.output, result.output


def test_validate_forked_span_list_exits_1(tmp_path: Path) -> None:
    """A forked span resolving to a LIST fails the same scalar-only contract.

    The scalar three-way merge refuses every non-scalar operand
    (``MergeTypeMismatch``), and a forked list would silently degrade to
    whole-replace at merge time — the exact leak class this check exists
    to surface offline.
    """
    span_yaml = """\
version: 1
tracked_files:
  d:
    src: config.json
    dst: ~/.some-tracked_file
    disposition: shared
    spans:
      - anchor: editor.rulers
        kind: forked
        semantics: shared
profiles:
  p:
    tracked_files: [d]
"""
    cfg = _write_config(tmp_path, span_yaml, create_src=False)
    (tmp_path / "tracked" / "config.json").write_text(
        '{"editor": {"rulers": [80, 100]}}\n', encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "forked spans take a scalar path" in result.output, result.output
    assert "'editor.rulers'" in result.output, result.output


def test_validate_pinned_span_subtree_exits_0(tmp_path: Path) -> None:
    """A pinned span on a whole SUBTREE stays legal (whole-replace re-assert)."""
    span_yaml = """\
version: 1
tracked_files:
  d:
    src: config.json
    dst: ~/.some-tracked_file
    disposition: shared
    spans:
      - anchor: editor
        kind: pinned
        semantics: shared
profiles:
  p:
    tracked_files: [d]
"""
    cfg = _write_config(tmp_path, span_yaml, create_src=False)
    (tmp_path / "tracked" / "config.json").write_text(
        '{"editor": {"fontSize": 12}}\n', encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0, result.output


def test_validate_span_paths_present_exits_0_without_base_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path check is STATELESS: green with NO base-store dir anywhere.

    Guard test for the contract that validate reads only the tracked src —
    no scalar-base manifests, no spans sidecar, no live files. The state
    root is pointed at a directory that does not exist; validate must exit
    0 and never create it.
    """
    state_dir = tmp_path / "state-never-created"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_dir))
    span_yaml = """\
version: 1
tracked_files:
  d:
    src: config.yaml
    dst: ~/.some-tracked_file
    disposition: shared
    spans:
      - anchor: editor.fontSize
        kind: pinned
        semantics: shared
      - anchor: telemetry.level
        kind: forked
        semantics: shared
profiles:
  p:
    tracked_files: [d]
"""
    cfg = _write_config(tmp_path, span_yaml, create_src=False)
    (tmp_path / "tracked" / "config.yaml").write_text(
        "editor:\n  fontSize: 12\ntelemetry:\n  level: all\n", encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert not state_dir.exists()


# ---------------------------------------------------------------------------
# Markdown span anchor resolution (offline, exact — no fuzzy relocation).
# A pinned/forked markdown span whose heading is absent or duplicated would
# orphan at install time; validate must surface both cases as distinct rows.
# ---------------------------------------------------------------------------

_MD_SPAN_YAML = """\
version: 1
tracked_files:
  d:
    src: note.md
    dst: ~/.note.md
    disposition: shared
    spans:
      - anchor: "## Missing"
        kind: pinned
        semantics: shared
profiles:
  p:
    tracked_files: [d]
"""


def test_validate_markdown_span_anchor_absent_exits_1(tmp_path: Path) -> None:
    """A pinned markdown span whose heading is absent → exit 1, distinct row."""
    cfg = _write_config(tmp_path, _MD_SPAN_YAML, create_src=False)
    (tmp_path / "tracked" / "note.md").write_text(
        "# Title\n\n## Present\nbody\n", encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "pinned span '## Missing'" in result.output, result.output
    assert "no heading matched" in result.output, result.output
    # Did-you-mean suggestions are a structural-path-only feature.
    assert "did you mean" not in result.output, result.output


def test_validate_markdown_span_anchor_ambiguous_exits_1(tmp_path: Path) -> None:
    """A duplicate heading → exit 1 with the AMBIGUOUS row, not not-found."""
    yaml_text = _MD_SPAN_YAML.replace('"## Missing"', '"## Dup"')
    cfg = _write_config(tmp_path, yaml_text, create_src=False)
    (tmp_path / "tracked" / "note.md").write_text(
        "## Dup\na\n\n## Dup\nb\n", encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "matches multiple headings" in result.output, result.output
    assert "no heading matched" not in result.output, result.output


def test_validate_markdown_span_anchor_present_passes(tmp_path: Path) -> None:
    """A heading that resolves exactly once keeps validate green."""
    yaml_text = _MD_SPAN_YAML.replace('"## Missing"', '"## Present"')
    cfg = _write_config(tmp_path, yaml_text, create_src=False)
    (tmp_path / "tracked" / "note.md").write_text(
        "# Title\n\n## Present\nbody\n", encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# local.yaml overlay span fold: host-local span declarations are part of the
# checked view (on a local copy — validate --all must never double-apply).
# ---------------------------------------------------------------------------

_NO_SPAN_JSON_YAML = """\
version: 1
tracked_files:
  d:
    src: config.json
    dst: ~/.config.json
    disposition: shared
profiles:
  p:
    tracked_files: [d]
"""

_OVERLAY_SPAN_LOCAL_YAML = """\
tracked_files:
  d:
    spans:
      - anchor: missing.key
        kind: forked
        semantics: host-local
"""


def test_validate_overlay_span_dead_path_exits_1(tmp_path: Path) -> None:
    """A local.yaml overlay span (no tracked-side spans at all) is checked too."""
    cfg = _write_config(tmp_path, _NO_SPAN_JSON_YAML, create_src=False)
    (tmp_path / "tracked" / "config.json").write_text(
        '{"editor": {"fontSize": 12}}\n', encoding="utf-8"
    )
    (tmp_path / "local.yaml").write_text(_OVERLAY_SPAN_LOCAL_YAML, encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "forked span 'missing.key'" in result.output, result.output


def test_validate_overlay_fold_applied_once_under_all(tmp_path: Path) -> None:
    """--all over two profiles sharing the file: one row per profile, no
    accumulation — the fold must run on a copy, never mutate cfg."""
    two_profiles = _NO_SPAN_JSON_YAML.replace(
        "profiles:\n  p:\n    tracked_files: [d]\n",
        "profiles:\n  p1:\n    tracked_files: [d]\n  p2:\n    tracked_files: [d]\n",
    )
    cfg = _write_config(tmp_path, two_profiles, create_src=False)
    (tmp_path / "tracked" / "config.json").write_text(
        '{"editor": {"fontSize": 12}}\n', encoding="utf-8"
    )
    (tmp_path / "local.yaml").write_text(_OVERLAY_SPAN_LOCAL_YAML, encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--all", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert result.output.count("forked span 'missing.key'") == 2, result.output


def test_validate_overlay_kind_span_dead_anchor_not_checked(tmp_path: Path) -> None:
    """OVERLAY spans carry a local.yaml body, not tracked content — never row.

    The payload anchor resolves (so the host-local-sections gate stays
    green); only the span-level anchor is dead, which this check must skip.
    """
    overlay_yaml = """\
tracked_files:
  d:
    spans:
      - anchor: "## Nowhere"
        kind: overlay
        overlay:
          anchor:
            kind: after-heading
            value: Title
          body: "extra\\n"
"""
    md_yaml = _NO_SPAN_JSON_YAML.replace("config.json", "note.md").replace(
        "~/.config.json", "~/.note.md"
    )
    cfg = _write_config(tmp_path, md_yaml, create_src=False)
    (tmp_path / "tracked" / "note.md").write_text("# Title\nbody\n", encoding="utf-8")
    (tmp_path / "local.yaml").write_text(overlay_yaml, encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
