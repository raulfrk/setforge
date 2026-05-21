"""Timing constraint: shell completion callbacks must be < 100 ms.

Anti-smell #16: completion fires on every tab press. A slow callback
is felt directly by the user. The contract is < 100ms typical. No full
validate, no prompt_toolkit import inside the callback.
"""

from __future__ import annotations

import time
from typing import Any

from setforge.cli.config import _complete_path_local, _complete_path_tracked


class _FakeCtx:
    def __init__(self) -> None:
        self.params: dict[str, Any] = {}
        self.info_name: str | None = None


def test_local_path_completion_under_100ms_timing() -> None:
    """``_complete_path_local`` returns inside the < 100ms budget."""
    start = time.perf_counter()
    for _ in range(10):
        _complete_path_local(_FakeCtx(), "")
    elapsed_per_call_ms = (time.perf_counter() - start) * 1000 / 10
    assert elapsed_per_call_ms < 100, f"too slow: {elapsed_per_call_ms:.1f} ms"


def test_tracked_path_completion_under_100ms_timing() -> None:
    """``_complete_path_tracked`` returns inside the < 100ms budget."""
    start = time.perf_counter()
    for _ in range(10):
        _complete_path_tracked(_FakeCtx(), "")
    elapsed_per_call_ms = (time.perf_counter() - start) * 1000 / 10
    assert elapsed_per_call_ms < 100, f"too slow: {elapsed_per_call_ms:.1f} ms"
