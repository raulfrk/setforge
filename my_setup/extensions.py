"""VSCode extension reconcile, driven by the ``code`` CLI.

All subprocess invocations honor the locked hygiene rules: ``shutil.which``
is consulted up front (raising :class:`ExtensionToolMissing` if missing),
``subprocess.run`` always uses ``check=True, text=True, capture_output=True,
timeout=30``, and args are always a list with no ``shell=True``.

Also exposes YAML-edit helpers used by the ``ext`` subcommand group to
mutate a profile's ``extensions.include`` / ``extensions.exclude`` lists
in ``my_setup.yaml`` without losing comments.
"""

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from my_setup.config import Extensions, ReconcilePolicy
from my_setup.errors import ConfigError, ExtensionToolMissing, ProfileNotFound

LOGGER = logging.getLogger(__name__)

_CODE_BIN = "code"
_TIMEOUT_S = 30
# Extension IDs are publisher.name where each part is lowercase alphanum +
# hyphens (matches the legacy Makefile's grep filter). The Remote-SSH `code`
# CLI prepends an "Extensions installed on SSH: <ip>:" header line that we
# filter out via this regex.
_EXT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Summary of what reconcile did (or would do for REPORT / dry_run)."""

    policy: ReconcilePolicy
    to_install: list[str]
    to_uninstall: list[str]
    dry_run: bool

    def __bool__(self) -> bool:
        return bool(self.to_install or self.to_uninstall)


def _ensure_code() -> str:
    """Resolve the ``code`` binary on PATH or raise."""
    path = shutil.which(_CODE_BIN)
    if path is None:
        raise ExtensionToolMissing(
            f"{_CODE_BIN!r} CLI not found on PATH; install VSCode "
            "or open a terminal in a VSCode session"
        )
    return path


def list_installed() -> set[str]:
    """Return the set of currently-installed extension IDs.

    Lines that don't match the ``publisher.name`` extension-ID format are
    silently dropped — the Remote-SSH ``code`` CLI emits a header line
    (``"Extensions installed on SSH: <ip>:"``) on stdout before the IDs.
    """
    code = _ensure_code()
    result = subprocess.run(
        [code, "--list-extensions"],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )
    return {
        line.strip()
        for line in result.stdout.splitlines()
        if _EXT_ID_RE.match(line.strip())
    }


def reconcile(ext: Extensions, *, dry_run: bool = False) -> ReconcileReport:
    """Reconcile installed VSCode extensions to the declared set.

    - effective_declared = ``include - exclude`` (exclude always wins).
    - ``ADDITIVE``: install missing only; never uninstall.
    - ``PRUNE``: install missing AND uninstall everything not declared
      (extras the user added, plus any explicit ``exclude`` entries that
      happen to be installed).
    - ``REPORT``: compute both diffs, run no subprocess install/uninstall,
      return a non-empty report.

    ``dry_run=True`` logs intended actions without invoking subprocess.
    """
    code = _ensure_code()
    installed = list_installed()
    effective = set(ext.include) - set(ext.exclude)

    to_install = sorted(effective - installed)
    if ext.reconcile is ReconcilePolicy.ADDITIVE:
        to_uninstall: list[str] = []
    else:
        to_uninstall = sorted(installed - effective)

    report = ReconcileReport(
        policy=ext.reconcile,
        to_install=to_install,
        to_uninstall=to_uninstall,
        dry_run=dry_run,
    )

    if ext.reconcile is ReconcilePolicy.REPORT or dry_run:
        prefix = "dry-run" if dry_run else "report"
        for name in to_install:
            LOGGER.info("[%s] would install: %s", prefix, name)
        for name in to_uninstall:
            LOGGER.info("[%s] would uninstall: %s", prefix, name)
        return report

    for name in to_install:
        LOGGER.info("installing extension: %s", name)
        subprocess.run(
            [code, "--install-extension", name],
            check=True,
            text=True,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )

    if ext.reconcile is ReconcilePolicy.PRUNE:
        for name in to_uninstall:
            LOGGER.info("uninstalling extension: %s", name)
            subprocess.run(
                [code, "--uninstall-extension", name],
                check=True,
                text=True,
                capture_output=True,
                timeout=_TIMEOUT_S,
            )

    return report


def install_one(ext_id: str) -> None:
    """Install a single extension via ``code --install-extension``."""
    code = _ensure_code()
    subprocess.run(
        [code, "--install-extension", ext_id],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


def _load_yaml_doc(config_path: Path):
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")
    yaml = YAML(typ="rt")
    with config_path.open("r", encoding="utf-8") as fh:
        return yaml, yaml.load(fh)


def _profile_extensions_block(doc, profile: str) -> CommentedMap:
    """Return the profile's ``extensions:`` block, creating it if absent."""
    profiles = doc.get("profiles")
    if profiles is None or profile not in profiles:
        raise ProfileNotFound(f"profile not found: {profile}")
    profile_block = profiles[profile]
    if "extensions" not in profile_block:
        profile_block["extensions"] = CommentedMap()
    return profile_block["extensions"]


def _ensure_list(block: CommentedMap, key: str) -> CommentedSeq:
    if key not in block:
        block[key] = CommentedSeq()
    return block[key]


def add_to_include(config_path: Path, profile: str, ext_id: str) -> bool:
    """Append ``ext_id`` to ``profiles.<profile>.extensions.include`` in
    ``config_path``. Idempotent: returns ``False`` if already present.

    Comments and key order in the YAML document are preserved via
    ruamel.yaml round-trip mode.
    """
    yaml, doc = _load_yaml_doc(config_path)
    ext_block = _profile_extensions_block(doc, profile)
    include = _ensure_list(ext_block, "include")
    if ext_id in include:
        return False
    include.append(ext_id)
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.dump(doc, fh)
    return True


def remove_from_include(
    config_path: Path,
    profile: str,
    ext_id: str,
    *,
    add_to_exclude_list: bool = False,
) -> bool:
    """Remove ``ext_id`` from ``profiles.<profile>.extensions.include``.

    If ``add_to_exclude_list`` is true, also append it to ``exclude``
    (idempotent). Returns ``True`` if any change was made.
    """
    yaml, doc = _load_yaml_doc(config_path)
    ext_block = _profile_extensions_block(doc, profile)
    include = _ensure_list(ext_block, "include")
    changed = False
    if ext_id in include:
        include.remove(ext_id)
        changed = True
    if add_to_exclude_list:
        exclude = _ensure_list(ext_block, "exclude")
        if ext_id not in exclude:
            exclude.append(ext_id)
            changed = True
    if not changed:
        return False
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.dump(doc, fh)
    return True
