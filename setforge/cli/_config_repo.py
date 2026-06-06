"""Scaffolding for ``setforge init --config-repo`` — the config-repo layer.

``setforge init`` (bare) bootstraps only the host-local ``local.yaml``
override. ``--config-repo`` additionally scaffolds the *config-repo*
layer — a fresh git repo holding a starter ``setforge.yaml`` and an empty
``tracked/`` tree — and wires ``local.yaml``'s ``source:`` block at it.

Every step here is idempotent: a second ``init --config-repo`` run never
re-inits an existing git repo, never clobbers an existing
``setforge.yaml``, and never appends a duplicate ``source:`` block. The
helpers below are pure-logic where possible (path derivation, source-block
detection) so the orchestrator in :mod:`setforge.cli.init` stays thin.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

from ruamel.yaml import YAML

from setforge.binaries import LOCAL_CONFIG_PATH
from setforge.errors import SetforgeError
from setforge.migrations import current_expected_schema_version

__all__ = [
    "ConfigRepoScaffoldError",
    "default_config_repo_dir",
    "local_yaml_has_source",
    "scaffold_config_repo",
    "write_starter_setforge_yaml",
]

# Subprocess wall-clock cap for ``git init`` — a local filesystem op that
# completes in well under a second; 30s mirrors the
# :func:`setforge.transitions._git_head` convention with generous headroom.
_GIT_INIT_TIMEOUT_SECONDS: float = 30.0

# Starter ``setforge.yaml`` — the minimal body that passes
# ``setforge validate --all``. ``schema_version`` is sourced from
# :data:`setforge.migrations.current_expected_schema_version` at write time
# (NOT hardcoded) so a schema bump propagates to freshly-scaffolded repos.
_STARTER_YAML_TEMPLATE: str = """\
# setforge config repo — scaffolded by `setforge init --config-repo`.
#
# Add tracked files under `tracked_files:` and reference them from a
# profile; place the source content under `tracked/<src>`. See the
# setforge README for the full schema.
schema_version: "{schema_version}"
tracked_files: {{}}
profiles:
  default: {{}}
"""


class ConfigRepoScaffoldError(SetforgeError):
    """A ``--config-repo`` scaffold step failed (git absent, bad target, ...)."""


def default_config_repo_dir(home: Path | None = None) -> Path:
    """Return the default config-repo target dir: ``~/projects/<name>-config``.

    ``<name>`` derives from the host name (:func:`platform.node`), falling
    back to ``"setforge"`` when the host name is empty or unavailable so the
    default is always a well-formed path. ``home`` is injectable for tests;
    it defaults to :meth:`Path.home`.
    """
    base = home if home is not None else Path.home()
    name = platform.node().strip() or "setforge"
    # Guard against a fully-qualified host name producing a path segment with
    # dots — take the first label only so the dir name stays tidy.
    name = name.split(".", 1)[0] or "setforge"
    return base / "projects" / f"{name}-config"


def local_yaml_has_source(local_yaml: Path = LOCAL_CONFIG_PATH) -> bool:
    """Return whether ``local.yaml`` already carries a ``source:`` key.

    Parses the file with the safe YAML loader and checks for a top-level
    ``source`` key. Returns ``False`` when the file is absent, empty, or
    has no ``source`` key. This is the dedup guard for source wiring: a
    second ``init --config-repo`` run must not append a duplicate block.
    """
    if not local_yaml.exists():
        return False
    yaml = YAML(typ="safe")
    data = yaml.load(local_yaml.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return False
    return "source" in data


def write_starter_setforge_yaml(target_dir: Path) -> bool:
    """Write a starter ``setforge.yaml`` into ``target_dir`` if absent.

    Returns ``True`` when a file was written, ``False`` when one already
    existed (left untouched — never clobbered). The body is the minimal
    schema that passes ``setforge validate --all``; ``schema_version`` is
    sourced live from :data:`current_expected_schema_version`.
    """
    dst = target_dir / "setforge.yaml"
    if dst.exists():
        return False
    dst.write_text(
        _STARTER_YAML_TEMPLATE.format(schema_version=current_expected_schema_version),
        encoding="utf-8",
    )
    return True


def _git_init(target_dir: Path) -> None:
    """Run ``git init`` in ``target_dir`` — skip if already a git repo.

    Subprocess-safe per the project convention: :func:`shutil.which`
    resolves ``git`` with a None-guard (clear error if git is absent),
    list-args (never ``shell=True``), ``text=True``, ``check=True``, and a
    ``timeout=``. A directory that is already a git repo is left untouched
    (no re-init) so a second run is a true no-op.
    """
    git_bin = shutil.which("git")
    if git_bin is None:
        raise ConfigRepoScaffoldError(
            "git not found on PATH — `init --config-repo` needs git to "
            "initialize the config repo. Install git and rerun."
        )
    if (target_dir / ".git").exists():
        return
    try:
        subprocess.run(
            [git_bin, "init", str(target_dir)],
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_INIT_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as err:
        raise ConfigRepoScaffoldError(
            f"`git init` failed in {target_dir}: {err.stderr.strip() or err}"
        ) from err
    except subprocess.TimeoutExpired as err:
        raise ConfigRepoScaffoldError(f"`git init` timed out in {target_dir}") from err
    except OSError as err:
        raise ConfigRepoScaffoldError(
            f"`git init` could not run in {target_dir}: {err}"
        ) from err


def _validate_target_dir(target_dir: Path) -> None:
    """Reject targets that would clobber user files or have a missing parent.

    A pre-existing *git repo* is fine (idempotent reinit). A pre-existing
    *non-empty, non-repo* dir is rejected — scaffolding over arbitrary user
    files is unsafe. We create at most one missing level (the immediate
    parent, e.g. ``~/projects`` for the default ``~/projects/<name>-config``);
    a deeper-missing tree is a clear error rather than silently materializing
    an arbitrary path.
    """
    parent = target_dir.parent
    if not parent.exists() and not parent.parent.exists():
        raise ConfigRepoScaffoldError(
            f"parent directory {parent} does not exist — create it first "
            f"(e.g. `mkdir -p {parent}`) and rerun."
        )
    if not target_dir.exists():
        return
    if (target_dir / ".git").exists():
        # Already a git repo: idempotent reuse, scaffold-into is safe.
        return
    if any(target_dir.iterdir()):
        raise ConfigRepoScaffoldError(
            f"target {target_dir} is a non-empty directory and not a git "
            "repo — refusing to scaffold over existing files. Pick an empty "
            "or non-existent directory."
        )


def scaffold_config_repo(target_dir: Path) -> Path:
    """Scaffold a config repo at ``target_dir`` and return its path.

    Steps (all idempotent): validate the target (reject non-empty non-repo
    dirs / missing parent), ``git init`` (skip if already a repo), write a
    starter ``setforge.yaml`` if absent, and create an empty ``tracked/``
    tree. Raises :class:`ConfigRepoScaffoldError` on any unsafe condition or
    git failure.
    """
    target_dir = target_dir.expanduser()
    _validate_target_dir(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    _git_init(target_dir)
    write_starter_setforge_yaml(target_dir)
    (target_dir / "tracked").mkdir(exist_ok=True)
    return target_dir
