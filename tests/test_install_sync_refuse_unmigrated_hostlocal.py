"""Refuse install/sync on an un-migrated host that still declares host-local content.

The host-local declaration surface (``host_local_sections`` / overlay ``spans``)
was retired from ``local.yaml``: host-local content now lives in the reconcile
store, folded in by the schema migration that bumps the major. A host that
upgraded the ENGINE past that major but has NOT run ``setforge migrate`` still
carries the retired surface in ``local.yaml``. The runtime load path silently
STRIPS it, so a ``sync`` / ``install`` there would bake the un-folded host-local
body into the SHARED tracked source (a silent host-local leak). The mutating
verbs refuse cleanly in that state.

The refuse is TARGETED: it fires only when an OLDER-major config coexists with a
retired-surface ``local.yaml``. An older-major config with no retired surface
(the deliberate pin workflow) and a current-major config both proceed.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from setforge.cli import app
from setforge.errors import ConfigError

runner = CliRunner()

_NOTES = "## Alpha\naaa\n## Beta\nbbb\n"

# An OLDER-major config (schema 3.0 under the now-4.0 engine).
_CFG_OLD = """\
schema_version: "3.0"
tracked_files:
  notes:
    src: notes.md
    dst: ~/notes.md
profiles:
  default:
    tracked_files: [notes]
"""

# A CURRENT-major config (matches this engine's expected major).
_CFG_CURRENT = """\
schema_version: "4.0"
tracked_files:
  notes:
    src: notes.md
    dst: ~/notes.md
profiles:
  default:
    tracked_files: [notes]
"""

# A local.yaml still declaring the retired host-local surface on ``notes``.
_LOCAL_WITH_HOSTLOCAL = textwrap.dedent(
    """\
    tracked_files:
      notes:
        host_local_sections:
          my-tweaks:
            anchor:
              kind: after-heading
              value: Alpha
            body: "## My Tweaks\\nmy custom line\\n"
    """
)

# A local.yaml with NO retired surface (an empty overlay on ``notes``).
_LOCAL_NO_HOSTLOCAL = "tracked_files:\n  notes: {}\n"

_MIGRATE_HINT = "setforge migrate --profile=default"


def _write_repo(tmp_path: Path, cfg_body: str) -> Path:
    """Write setforge.yaml + the tracked source; return the config path."""
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    (repo / "setforge.yaml").write_text(cfg_body, encoding="utf-8")
    (repo / "tracked" / "notes.md").write_text(_NOTES, encoding="utf-8")
    return repo / "setforge.yaml"


def _write_local_yaml(body: str | None) -> None:
    """Write ``body`` to the (test-redirected) local.yaml, or leave it absent."""
    from setforge import source as source_mod

    if body is None:
        return
    path = source_mod.LOCAL_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _install(config: Path) -> Result:
    return runner.invoke(
        app,
        [
            "install",
            "--profile=default",
            f"--config={config}",
            "--no-git-check",
            "--no-secrets-scan",
            "--no-transition",
            "--yes",
        ],
    )


def _sync(config: Path) -> Result:
    return runner.invoke(
        app,
        [
            "sync",
            "--profile=default",
            f"--config={config}",
            "--auto=use-live",
            "--no-transition",
            "--yes",
        ],
    )


@pytest.mark.parametrize("invoke", [_install, _sync], ids=["install", "sync"])
def test_refuses_unmigrated_config_with_host_local_surface(
    tmp_path: Path, invoke
) -> None:
    """Older-major config + retired local.yaml surface ⇒ clean refuse, no mutation."""
    config = _write_repo(tmp_path, _CFG_OLD)
    _write_local_yaml(_LOCAL_WITH_HOSTLOCAL)
    tracked = config.parent / "tracked" / "notes.md"
    before = tracked.read_text(encoding="utf-8")

    result = invoke(config)

    assert result.exit_code != 0, result.output
    # A clean ConfigError (SetforgeError subclass) — the CLI handler renders it
    # as one line + nonzero exit, never a traceback.
    assert isinstance(result.exception, ConfigError), result.exception
    assert _MIGRATE_HINT in str(result.exception)
    # No mutation: the shared tracked source is byte-for-byte unchanged.
    assert tracked.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("invoke", [_install, _sync], ids=["install", "sync"])
def test_older_major_without_host_local_surface_proceeds(
    tmp_path: Path, invoke
) -> None:
    """Older-major config + NO retired surface ⇒ gate stays silent (pin workflow)."""
    config = _write_repo(tmp_path, _CFG_OLD)
    _write_local_yaml(_LOCAL_NO_HOSTLOCAL)

    result = invoke(config)

    # The migrate-refuse must NOT fire; the run may exit for unrelated reasons,
    # but never with our host-local-leak gate.
    if isinstance(result.exception, ConfigError):
        assert _MIGRATE_HINT not in str(result.exception)
    assert _MIGRATE_HINT not in result.output


@pytest.mark.parametrize("invoke", [_install, _sync], ids=["install", "sync"])
def test_current_major_config_proceeds(tmp_path: Path, invoke) -> None:
    """Current-major config ⇒ gate stays silent even with a retired surface present."""
    config = _write_repo(tmp_path, _CFG_CURRENT)
    _write_local_yaml(_LOCAL_WITH_HOSTLOCAL)

    result = invoke(config)

    if isinstance(result.exception, ConfigError):
        assert _MIGRATE_HINT not in str(result.exception)
    assert _MIGRATE_HINT not in result.output
