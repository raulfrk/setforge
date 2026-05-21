"""Timing constraint: shell completion callbacks must be < 100 ms.

Anti-smell #16: completion fires on every tab press. A slow callback
is felt directly by the user. The contract is "< 100 ms typical", and
"typical" is enforced as per-call (not mean) — averaging across N
calls hides a single 200 ms outlier that the user actually
experiences. No full validate, no prompt_toolkit import inside the
callback.
"""

from __future__ import annotations

import time
from typing import Any

from setforge.cli.config import _complete_path_local, _complete_path_tracked


class _FakeCtx:
    def __init__(self) -> None:
        self.params: dict[str, Any] = {}
        self.info_name: str | None = None


_PER_CALL_BUDGET_MS: float = 100.0


def test_local_path_completion_under_100ms_timing() -> None:
    """Every ``_complete_path_local`` call returns inside the < 100ms budget.

    Asserts the per-call maximum (not the mean) — a single tab press
    that takes 150 ms is felt by the user even if siblings amortize to
    a fast mean.
    """
    deltas_ms: list[float] = []
    for _ in range(10):
        t0 = time.perf_counter()
        _complete_path_local(_FakeCtx(), "")
        deltas_ms.append((time.perf_counter() - t0) * 1000.0)
    worst = max(deltas_ms)
    assert worst < _PER_CALL_BUDGET_MS, (
        f"worst call too slow: {worst:.1f} ms (all: {deltas_ms})"
    )


def test_tracked_path_completion_under_100ms_timing() -> None:
    """Every ``_complete_path_tracked`` call returns inside the < 100ms budget."""
    deltas_ms: list[float] = []
    for _ in range(10):
        t0 = time.perf_counter()
        _complete_path_tracked(_FakeCtx(), "")
        deltas_ms.append((time.perf_counter() - t0) * 1000.0)
    worst = max(deltas_ms)
    assert worst < _PER_CALL_BUDGET_MS, (
        f"worst call too slow: {worst:.1f} ms (all: {deltas_ms})"
    )
