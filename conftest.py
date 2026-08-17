"""Project-root hooks for stable, prebuilt Docker E2E xdist sessions.

This file exposes the importable hooks implemented in ``tests.e2e_xdist``:
auto-activation with ``-n 2`` and controller-side image preparation before
workers start. Per-test fixtures live in ``tests/conftest.py``.

Worker count
============

Capped at 2. The earlier ``-n 4`` cap (first take, reverted
in 6874cfe / 1bf1d18) saturated the Docker daemon AND the host VM under
sustained parallel load — combined with a retry-on-Timeout helper that
doubled exec load on transient hiccups, the host crashed mid-run.

``-n 2`` was empirically validated on this 6-core host: 109 tests in
6:30 wall, zero ``TimeoutExpired`` flakes, zero crashes. Slightly
slower than the original ``-n 4`` target but stable.

Override the cap with ``-n N`` on the CLI when running on a host with
different daemon throughput; ``-n 0`` opts out of xdist entirely for
serial-mode debugging.

Hook placement is load-bearing
==============================

xdist activates distributed mode in ``pytest_cmdline_main(tryfirst=True)``
by converting ``config.option.numprocesses`` → ``config.option.tx`` and
flipping ``config.option.dist`` away from ``"no"``. By the time any
``pytest_configure`` hook fires, ``pytest_cmdline_main`` has already run.

This means a subdir ``pytest_configure(tryfirst=True)`` that sets
``config.option.numprocesses`` is too late — xdist already read
``numprocesses=None`` and skipped the conversion. The previous
incarnation at ``tests/docker/conftest.py`` failed for exactly this
reason (xdist#917).

The fix here sets ALL THREE values that xdist's
``pytest_configure(trylast=True)`` checks via ``_is_distribution_mode``:

- ``config.option.numprocesses`` (for documentation / external readers)
- ``config.option.dist`` (read by ``_is_distribution_mode``)
- ``config.option.tx`` (the actual transport list xdist consumes)

xdist's later ``pytest_configure(trylast=True)`` sees a fully-populated
distribution config and registers ``DSession`` exactly as if the user
had passed ``-n 2`` on the CLI.

This conftest lives at project root (not under ``tests/``) so it gets
discovered as part of pytest's rootdir conftest set — that's the
earliest layer at which a project-local conftest fires.
"""

from __future__ import annotations

from tests.e2e_xdist import (  # noqa: F401
    pytest_configure,
    pytest_xdist_setupnodes,
)
