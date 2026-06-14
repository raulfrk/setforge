"""Regression: host-local structural span overlapping a shared span (I11).

The structural-span overlap/nesting guard (Invariant I11) is enforced at
merge / install time by ``validate_structural_spans`` (a ``ConfigError`` that
aborts install mid-deploy). The offline ``validate`` gate must catch it first.

``_check_spans_path_existence`` already folds the local.yaml host-local
overlay before validating, but its sibling ``_check_spans_file_types`` (which
runs the overlap guard) validated the TRACKED-SIDE-ONLY span list. So a shared
span on a child path declared in setforge.yaml plus a host-local span on the
overlapping parent path declared in local.yaml each passed every offline gate,
yet ``install`` aborted mid-deploy once
``apply_host_local_tracked_file_overrides`` folded both scopes into one
overlapping set.

These tests pin the folded-view behavior: the overlap surfaces at ``validate``
time. On the pre-fix code ``test_validate_*`` below exit 0 (overlap missed).
"""

from pathlib import Path

from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app


def _write_config(tmp_path: Path, content: str) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(content, encoding="utf-8")
    (tmp_path / "tracked").mkdir(exist_ok=True)
    return cfg


# Shared structural span on the child path ``editor.fontSize`` declared in the
# config repo; the overlapping host-local parent ``editor`` lives in local.yaml.
_SHARED_CHILD_SPAN_YAML = """\
version: 1
tracked_files:
  d:
    src: config.json
    dst: ~/.config.json
    disposition: shared
    spans:
      - anchor: editor.fontSize
        kind: pinned
        semantics: shared
profiles:
  p:
    tracked_files: [d]
"""

# Host-local span on the parent path ``editor`` — overlaps the shared child.
_HOST_LOCAL_PARENT_OVERLAY = """\
tracked_files:
  d:
    spans:
      - anchor: editor
        kind: pinned
        semantics: host-local
"""


def test_validate_host_local_span_overlapping_shared_span_exits_1(
    tmp_path: Path,
) -> None:
    """Folded view: a host-local parent span overlapping a shared child fails.

    Pre-fix ``_check_spans_file_types`` validated only the tracked-side span
    list, so the cross-scope overlap was invisible to ``validate`` and only
    aborted install mid-deploy. The folded view must reject it offline.
    """
    cfg = _write_config(tmp_path, _SHARED_CHILD_SPAN_YAML)
    (tmp_path / "tracked" / "config.json").write_text(
        '{"editor": {"fontSize": 12}}\n', encoding="utf-8"
    )
    (tmp_path / "local.yaml").write_text(_HOST_LOCAL_PARENT_OVERLAY, encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "overlapping" in result.output, result.output
    assert "'d'" in result.output, result.output


def test_validate_no_overlay_shared_child_alone_exits_0(tmp_path: Path) -> None:
    """Control: the shared child span alone (no overlapping overlay) is clean.

    Confirms the failure in the sibling test is the overlap, not the span
    itself — without the host-local parent overlay, ``validate`` exits 0.
    """
    cfg = _write_config(tmp_path, _SHARED_CHILD_SPAN_YAML)
    (tmp_path / "tracked" / "config.json").write_text(
        '{"editor": {"fontSize": 12}}\n', encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Belt-and-suspenders: refuse the cross-scope overlap at PIN time too, not just
# at the offline validate gate. ``override pin`` reads only its own scope's
# existing spans for the idempotency guard, but the combined-overlap check must
# fold BOTH scopes — install merges host-local + shared into one set, so a span
# pinned in one scope that overlaps an existing span in the OTHER scope would
# otherwise pass pin, pass validate (only after that fix), yet abort install.
# ---------------------------------------------------------------------------

_PIN_BASE_YAML = """\
version: 1
schema_version: "1.1"
tracked_files:
  conf:
    src: conf.yaml
    dst: ~/.x/conf.yaml
    disposition: shared
profiles:
  p:
    tracked_files: [conf]
"""

_PIN_CONF_YAML = "editor:\n  fontSize: 12\n  tabSize: 4\n"


def _pin_repo(tmp_path: Path) -> Path:
    cfg = _write_config(tmp_path, _PIN_BASE_YAML)
    (tmp_path / "tracked" / "conf.yaml").write_text(_PIN_CONF_YAML, encoding="utf-8")
    return cfg


def _pin(cfg: Path, *args: str) -> Result:
    return CliRunner().invoke(app, [*args, "--config", str(cfg), "--profile", "p"])


def test_pin_host_local_overlapping_existing_shared_span_refused(
    tmp_path: Path,
) -> None:
    """Host-local pin on a parent path overlapping an existing shared child fails.

    The shared child span ``editor.fontSize`` is pinned first (``--shared``);
    pinning the overlapping host-local parent ``editor`` must be refused at pin
    time with the structural-overlap error — not silently accepted to abort
    install later.
    """
    cfg = _pin_repo(tmp_path)
    shared = _pin(cfg, "override", "pin", "conf", "editor.fontSize", "--shared")
    assert shared.exit_code == 0, shared.output
    result = _pin(cfg, "override", "pin", "conf", "editor")
    assert result.exit_code != 0, result.output
    assert "overlap" in result.output.lower() or "prefix" in result.output.lower()


def test_pin_shared_overlapping_existing_host_local_span_refused(
    tmp_path: Path,
) -> None:
    """Shared pin on a parent path overlapping an existing host-local child fails.

    The mirror of the above: a host-local child span pinned first, then a
    ``--shared`` parent span overlapping it must be refused at pin time.
    """
    cfg = _pin_repo(tmp_path)
    host_local = _pin(cfg, "override", "pin", "conf", "editor.fontSize")
    assert host_local.exit_code == 0, host_local.output
    result = _pin(cfg, "override", "pin", "conf", "editor", "--shared")
    assert result.exit_code != 0, result.output
    assert "overlap" in result.output.lower() or "prefix" in result.output.lower()
