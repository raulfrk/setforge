"""Project-root conftest: auto-activate xdist with ``-n 2`` for ``-m e2e_docker``.

Sole responsibility of this file is the xdist auto-activation. Per-test
fixtures (HOME isolation, LOCAL_CONFIG_PATH redirect) live in
``tests/conftest.py``.

Worker count
============

Capped at 2. The earlier ``-n 4`` cap (setforge-hpd4 first take, reverted
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

import pytest

# Cap rationale (per setforge-hpd4 + setforge-pfqe investigation, 2026-05-20):
# -n 2 is the empirically-validated stable equilibrium on this host (109 tests,
# zero TimeoutExpired flakes, zero daemon crashes). The original hpd4 spec
# target of < 6 min wall-time was aspirational, set pre-daemon-saturation;
# current measurements span ~6:30 to ~7:45 wall depending on host load
# (idle host ≈ 6:30; under concurrent ambient load ≈ 7:45). That slip is
# the cost of stability, not a regression to fix at the cap level.
#
# Earlier -n 4 attempts saturated the Docker daemon AND the host VM (the cap
# was reverted in 6874cfe / 1bf1d18); -n auto (= 6 on a 6-core host) produces
# ~20 transient timeouts per run. Safely raising the cap above 2 requires
# per-host profiling of overlay2 disk-IO starvation, container cgroup memory
# pressure, and fixture-setup GIL contention — deferred to v0.3.x. See
# CLAUDE.md "## Docker e2e tests" for operator instructions. Override the
# cap at the CLI with `-n N` for hosts with different daemon throughput;
# `-n 0` opts out of xdist for serial debugging.
_XDIST_WORKER_CAP: int = 2


def _selects_e2e_docker(markexpr: str) -> bool:
    """Return True iff the marker expression positively selects ``e2e_docker``.

    Unit-test runs default to ``-m 'not e2e_docker'`` (pyproject.toml
    addopts). A naive substring check on ``"e2e_docker" in markexpr``
    would match BOTH selection forms — and accidentally fire xdist on
    every unit run.

    The check distinguishes by tokenizing the expression. ``e2e_docker``
    counts as positively selected when it appears as a bare token
    (``-m e2e_docker``) or composed with positive operators (``and``,
    ``or``). Negation by the immediately preceding ``not`` flips the
    selection off — covering both the unit-test default and any
    user-supplied ``-m "not e2e_docker"`` invocation.
    """
    if "e2e_docker" not in markexpr:
        return False
    tokens = markexpr.replace("(", " ( ").replace(")", " ) ").split()
    if "e2e_docker" not in tokens:
        return False
    idx = tokens.index("e2e_docker")
    return not (idx > 0 and tokens[idx - 1] == "not")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Auto-activate xdist with ``-n 2`` when ``-m e2e_docker`` is selected.

    Activates only when:

    - the ``-m`` markexpr positively selects ``e2e_docker``
      (see :func:`_selects_e2e_docker` for the token-walk discriminator
      that distinguishes ``-m e2e_docker`` from ``-m 'not e2e_docker'``,
      which is the project's default unit-test markexpr via
      ``pyproject.toml`` addopts), AND
    - the user has not already passed ``-n`` / ``--numprocesses`` on the
      CLI (preserves ``-n 0`` opt-out for serial-mode debugging), AND
    - this isn't an xdist worker subprocess (the worker re-runs
      ``pytest_configure`` and would otherwise recurse).
    """
    markexpr = config.getoption("markexpr", default="") or ""
    if not _selects_e2e_docker(markexpr):
        return
    if config.getoption("numprocesses", default=None) is not None:
        return
    # Worker subprocesses re-enter pytest_configure; xdist sets workerinput
    # on them. Bail out so we don't double-activate.
    if hasattr(config, "workerinput"):
        return
    config.option.numprocesses = _XDIST_WORKER_CAP
    # Use ``loadgroup`` (not ``load``) so ``@pytest.mark.xdist_group(...)`` is
    # honored: tests sharing a group label are routed to the same worker.
    # Under ``load``, ``xdist_group`` markers are silently ignored — the
    # ``docker_daemon`` sweep (six daemon-heavy tests) would not serialize.
    # See setforge-hdlu review fix.
    if config.option.dist == "no":
        config.option.dist = "loadgroup"
    config.option.tx = ["popen"] * _XDIST_WORKER_CAP
