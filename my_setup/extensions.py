"""VSCode extension reconcile, driven by the ``code`` CLI.

All subprocess invocations honor the locked hygiene rules: ``shutil.which``
is consulted up front (raising :class:`ExtensionToolMissing` if missing),
``subprocess.run`` always uses ``check=True, text=True, capture_output=True,
timeout=30``, and args are always a list with no ``shell=True``.
"""

import logging
import shutil
import subprocess
from dataclasses import dataclass

from my_setup.config import Extensions, ReconcilePolicy
from my_setup.errors import ExtensionToolMissing

LOGGER = logging.getLogger(__name__)

_CODE_BIN = "code"
_TIMEOUT_S = 30


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
    """Return the set of currently-installed extension IDs."""
    code = _ensure_code()
    result = subprocess.run(
        [code, "--list-extensions"],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


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
