"""Process-level contract for SetForge's non-interactive CLI startup."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING, assert_type

import pytest

if TYPE_CHECKING:
    import setforge.reconcile as reconcile
    import setforge.ui as ui
    from setforge.reconcile import WizardResult
    from setforge.reconcile.merge_model import Clean, MergeResult
    from setforge.reconcile.types import FileId
    from setforge.ui.primitives import Button, Cancelled

    button_result = ui.button_bar([Button("choose", 1)])
    assert_type(button_result, int | Cancelled)
    wizard = reconcile.WizardResult(MergeResult((Clean(b"x"),)), deferred=False)
    assert_type(wizard, WizardResult)
    resolved = reconcile.resolve_conflicts(FileId("x"), MergeResult((Clean(b"x"),)))
    assert_type(resolved, WizardResult | Cancelled)


@pytest.mark.test_infra
def test_cli_import_defers_the_interactive_terminal_stack() -> None:
    """Registering commands must not import prompt-toolkit or conflict UIs."""
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import setforge.cli; "
            "blocked={'prompt_toolkit', 'setforge.reconcile.wizard', "
            "'setforge.reconcile.claude_merge'}; "
            "print('\\n'.join(sorted(blocked & sys.modules.keys())))",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert probe.stdout.strip() == ""
