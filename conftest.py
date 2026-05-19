"""Project-root conftest: auto-activate pytest-xdist for ``-m e2e_docker``.

Sole responsibility of this file is the xdist auto-activation. Per-test
fixtures (HOME isolation, LOCAL_CONFIG_PATH redirect) live in
``tests/conftest.py``.

Hook placement is load-bearing
==============================

xdist activates distributed mode in ``pytest_cmdline_main(tryfirst=True)``
by converting ``config.option.numprocesses`` → ``config.option.tx`` and
flipping ``config.option.dist`` away from ``"no"``. By the time any
``pytest_configure`` hook fires, ``pytest_cmdline_main`` has already run.

This means a subdir ``pytest_configure(tryfirst=True)`` that sets
``config.option.numprocesses = "auto"`` is too late — xdist already
read ``numprocesses=None`` and skipped the conversion. The previous
incarnation at ``tests/docker/conftest.py`` failed for exactly this
reason (xdist#917).

The fix here sets ALL THREE values that xdist's
``pytest_configure(trylast=True)`` checks via ``_is_distribution_mode``:

- ``config.option.numprocesses`` (for documentation / external readers)
- ``config.option.dist`` (read by ``_is_distribution_mode``)
- ``config.option.tx`` (the actual transport list xdist consumes)

xdist's later ``pytest_configure(trylast=True)`` sees a fully-populated
distribution config and registers ``DSession`` exactly as if the user
had passed ``-n 4`` on the CLI.

This conftest lives at project root (not under ``tests/``) so it gets
discovered as part of pytest's rootdir conftest set — that's the
earliest layer at which a project-local conftest fires.

Worker count
============

Capped at 4 — empirical on a 6-core host: ``-n auto`` (= 6 workers)
saturates the local Docker daemon and produces transient
``subprocess.TimeoutExpired`` flakes across the 107-test e2e suite.
``-n 4`` keeps wall-time under 6 minutes while staying inside the
daemon's parallel-exec budget.
"""

from __future__ import annotations

import pytest

_XDIST_WORKER_CAP: int = 4


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

    A precise marker-expression parser would defer to pytest's own
    ``_pytest.mark.expression`` module, but the conservative token walk
    here is sufficient for the few argv shapes that reach this hook
    and avoids reaching into a private pytest API.
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
    """Auto-activate xdist with ``-n 4`` when ``-m e2e_docker`` is selected.

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
    if config.option.dist == "no":
        config.option.dist = "load"
    config.option.tx = ["popen"] * _XDIST_WORKER_CAP
