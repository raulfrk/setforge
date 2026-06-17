"""Guard that ``init``'s completion-report next-steps stay valid commands.

The printed "next steps" previously suggested ``setforge validate
--list-profiles`` — a flag that does not exist; the profile-listing verb is
``setforge profile list``. Pin both branches (source skipped vs pre-configured)
to a current command so a stale flag cannot creep back.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from setforge.cli.init import SourceChoice, SourceSpec, _print_completion_report


def _render(spec: SourceSpec) -> str:
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=200)
    _print_completion_report(source_spec=spec, console=console)
    return buf.getvalue()


@pytest.mark.parametrize(
    "spec",
    [
        SourceSpec(choice=SourceChoice.SKIP),
        SourceSpec(choice=SourceChoice.PATH, path=Path("/tmp/cfg")),
    ],
)
def test_completion_next_steps_use_valid_profile_list(spec: SourceSpec) -> None:
    out = _render(spec)
    # The stale, nonexistent flag must be gone from both branches.
    assert "--list-profiles" not in out
    # The real profile-listing verb is suggested instead.
    assert "setforge profile list" in out
