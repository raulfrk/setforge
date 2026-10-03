"""Human output must go through the soft-wrapping console factory."""

from __future__ import annotations

import re
from pathlib import Path

_BARE_CONSOLE = re.compile(r"(?<![\w.])Console\(")
_ALLOWED = {"setforge/cli/_output.py"}


def test_no_bare_rich_console_in_cli() -> None:
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted((root / "setforge").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in _ALLOWED:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if _BARE_CONSOLE.search(line) and not line.lstrip().startswith(("#", "``")):
                offenders.append(f"{rel}:{number}")
    assert not offenders, (
        "construct rich consoles with setforge.cli._output.make_console "
        f"(no 80-column wrapping when piped): {offenders}"
    )
