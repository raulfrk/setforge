"""Regression: ``sync --auto=use-live`` must record local.yaml so revert undoes it.

When a host-local OVERLAY body has been hand-edited in the live file, capture's
``_capture_overlay_bodies`` resolves the edit and, on KEEP, writes the new body
into ``local.yaml`` via ``write_edited_body_to_local``. ``resolve_auto`` maps
``CaptureAuto.USE_LIVE`` -> KEEP, so even the non-interactive
``sync --auto=use-live`` path performs this local.yaml write — INSIDE
``_run_capture``, AFTER the SYNC transition's ``file_pre`` snapshot.

Before the fix, ``_sync_snapshot_paths`` deliberately EXCLUDED
``LOCAL_CONFIG_PATH`` on the (false) assumption that only the PROMOTE wizard
mutates local.yaml at sync time. The capture-time overlay-keep write therefore
rode no transition at all: ``setforge revert`` after such a sync could not
restore the pre-edit local.yaml body — a silent loss with no undo path.

This drives a real install (migrate legacy host-local section -> spans overlay,
inject the body into live) -> hand-edit the live body ->
``sync --auto=use-live`` (KEEP writes the edit into local.yaml) -> ``revert``,
and asserts local.yaml is restored to its pre-sync content. The post-revert
assertion is the one that failed against the old behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app

_PROFILE = "test-sync-localyaml"

_DOC = """\
# Title

## Notes

upstream notes body
"""

_LOCAL_YAML = """\
# host config
tracked_files:
  doc:
    host_local_sections:
      my-notes:
        anchor:
          kind: after-heading
          value: "Notes"
        body: |
          host-local notes
"""

# The edited body must NOT contain the original as a substring: the capture
# detector excises the exact recorded ``last_deployed_body`` needle, and a
# substring hit would carve only the original out (leaving a fragment leaking
# into tracked) instead of resolving the whole line as a hand-edit.
_ORIGINAL_BODY = "host-local notes"
_EDITED_BODY = "COMPLETELY DIFFERENT BODY"


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.setforge_sync_localyaml/doc.md\n"
        # SHARED disposition routes the hand-edited overlay body through
        # the capture-time overlay-keep wizard (write_edited_body_to_local)
        # under --auto=use-live; that is the local.yaml write this guards.
        "    disposition: shared\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - doc\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path) -> None:
    src = repo / "tracked" / "doc.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(_DOC, encoding="utf-8")


def _live_doc_path() -> Path:
    return Path("~/.setforge_sync_localyaml/doc.md").expanduser()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _local_yaml_path(tmp_path: Path) -> Path:
    # conftest._isolated_local_config redirects source.LOCAL_CONFIG_PATH here.
    return tmp_path / "local.yaml"


def _install(config: Path) -> Result:
    return CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--no-secrets-scan",
            "--no-git-check",
            "--yes",
        ],
    )


def _sync(config: Path) -> Result:
    return CliRunner().invoke(
        app,
        [
            "sync",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--auto=use-live",
            "--yes",
        ],
    )


def _revert(config: Path) -> Result:
    return CliRunner().invoke(
        app,
        ["revert", f"--profile={_PROFILE}", f"--config={config}", "--yes"],
    )


def test_sync_uselive_overlay_keep_localyaml_write_is_revertible(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sync --auto=use-live that KEEPs a hand-edited overlay body writes
    local.yaml; revert must restore the pre-sync local.yaml content.

    Before the fix the local.yaml write rode no transition, so revert
    left the hand-edited body in local.yaml (the pre-edit body lost).
    """
    _write_tracked(repo)
    config = _write_config(repo)
    local = _local_yaml_path(tmp_path)
    local.write_text(_LOCAL_YAML, encoding="utf-8")
    tracked_src = repo / "tracked" / "doc.md"

    # The host-local loader's ``path`` default is bound at function-def time,
    # so the conftest constant monkeypatch does not reach it. Repoint the
    # default at the isolated local.yaml so capture/promote read the fixture.
    import setforge.source as _source

    monkeypatch.setattr(
        _source.load_local_host_local_sections, "__defaults__", (local,)
    )

    # Install: migrates legacy host_local_sections -> spans overlay AND
    # injects the host-local body into the live file.
    assert _install(config).exit_code == 0
    live = _live_doc_path()
    assert _ORIGINAL_BODY in live.read_text(encoding="utf-8")
    # Post-migration local.yaml is the spans form write_edited_body_to_local
    # mutates; snapshot it as the pre-sync baseline.
    pre_sync_local = local.read_text(encoding="utf-8")
    assert _EDITED_BODY not in pre_sync_local

    # Hand-edit the injected body in the live file. The recorded body needle
    # no longer matches verbatim, so capture resolves it as a hand-edit and
    # USE_LIVE maps to KEEP -> write_edited_body_to_local mutates local.yaml.
    live_text = live.read_text(encoding="utf-8")
    assert _ORIGINAL_BODY in live_text
    live.write_text(live_text.replace(_ORIGINAL_BODY, _EDITED_BODY), encoding="utf-8")

    # sync --auto=use-live: KEEP writes the edited body into local.yaml.
    result = _sync(config)
    assert result.exit_code == 0, result.output
    post_sync_local = local.read_text(encoding="utf-8")
    assert post_sync_local != pre_sync_local
    assert _EDITED_BODY in post_sync_local
    # Leak gate: the host-local body never reaches the tracked src.
    assert _EDITED_BODY not in tracked_src.read_text(encoding="utf-8")

    # Revert must restore local.yaml to its pre-sync content. Before the fix
    # _sync_snapshot_paths excluded LOCAL_CONFIG_PATH, so the SYNC transition
    # carried no local.yaml diff and revert left post_sync_local in place.
    result = _revert(config)
    assert result.exit_code == 0, result.output
    restored_local = local.read_text(encoding="utf-8")
    assert restored_local == pre_sync_local
    assert _EDITED_BODY not in restored_local
