"""Docker e2e tests for ``setforge validate`` mockup-D UX.

Asserts on the literal output strings of the mockup-D validate-error
UX against a fresh container running the actual ``setforge`` CLI —
the unit suite covers shape and integration; this ring confirms the
formatter rides through the real Typer command boundary into a real
terminal capture under a real venv.

Each test spins a fresh container, writes a crafted
``~/.config/setforge/local.yaml``, runs ``uv run setforge validate
--all`` against the shared e2e config fixture, and asserts on the
captured stdout/stderr substrings.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_HOME_LOCAL_YAML: str = "/home/tester/.config/setforge/local.yaml"


def _run_validate_streams(c: ContainerHandle) -> subprocess.CompletedProcess[str]:
    """Invoke ``setforge validate --all`` and return the completed process.

    Callers needing a single ordered stream (e.g. ``.index()`` ordering
    asserts) read ``result.stdout`` alone; concatenating stdout+stderr would
    interleave non-deterministically.
    """
    return c.exec(
        ["uv", "run", "setforge", "validate", "--all", f"--config={CONFIG_FIXTURE}"],
        check=False,
    )


def _run_validate(c: ContainerHandle) -> tuple[int, str]:
    """Invoke ``setforge validate --all`` and return (returncode, combined output)."""
    result = _run_validate_streams(c)
    return result.returncode, result.stdout + result.stderr


def test_validate_local_yaml_clean_exits_zero(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A well-formed local.yaml (binaries override only) → validate exits 0."""
    c = docker_container()
    c.write_text(_HOME_LOCAL_YAML, "binaries:\n  uv: /usr/bin/uv\n")
    rc, out = _run_validate(c)
    assert rc == 0, out
    assert "ok" in out


def test_validate_local_yaml_typo_top_level_key_emits_mockup_d(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A typo'd top-level key surfaces all four mockup-D UX elements:
    ``← line``, ``Did you mean``, ``Fix:``, and ``validation FAILED``."""
    c = docker_container()
    # 'binares' is Levenshtein distance 2 from 'binaries' (insert 'i' + reorder).
    c.write_text(_HOME_LOCAL_YAML, "binares:\n  uv: /usr/bin/uv\n")
    rc, out = _run_validate(c)
    assert rc == 1, out
    assert "←─── line" in out
    assert "Did you mean 'binaries'" in out
    assert "Fix:" in out
    assert "validation FAILED" in out


def test_validate_local_yaml_parse_error_emits_yaml_parse_category(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A malformed local.yaml surfaces in the YAML PARSE category (NOT
    the schema-validation category), and the final summary still
    reports the failure count."""
    c = docker_container()
    # Tab-indented mapping value is a YAML parse error in ruamel's safe loader.
    c.write_text(_HOME_LOCAL_YAML, "source:\n\tpath: /tmp\n")
    rc, out = _run_validate(c)
    assert rc == 1, out
    assert "✗ YAML PARSE ERROR" in out
    assert "local.yaml" in out
    assert "validation FAILED" in out


def test_validate_local_yaml_multi_error_reports_all_then_refuses(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Two schema errors both land in the output; the summary names
    the count; the refusal line trails the summary (mockup D's
    report-all-then-refuse contract)."""
    c = docker_container()
    c.write_text(_HOME_LOCAL_YAML, "unknown_a: 1\nunknown_b: 2\n")
    # Ordering assertion must read a single stream: the summary and refusal
    # are both ``typer.echo`` → stdout (validate.py:1187-1188), and indexing
    # into a stdout+stderr concat would be non-deterministic (interleave).
    result = _run_validate_streams(c)
    out = result.stdout
    assert result.returncode == 1, result.stdout + result.stderr
    assert out.count("✗ SCHEMA VALIDATION ERROR") >= 2
    assert "validation FAILED:" in out
    assert "no changes will be made" in out
    # The refusal line trails the error summary (mockup-D report-all-then-refuse).
    assert out.index("no changes will be made") > out.index("validation FAILED:"), out


# ---------------------------------------------------------------------------
# overlay-class typo paths. Each test seeds a local.yaml that
# typos a sub-key inside one overlay-class block, runs ``validate``, and
# asserts (a) the schema-validation category fires, (b) the ``Did you mean``
# suggestion surfaces the dispatched candidate, and (c) the ``Fix:`` action
# names the offending file. The mockup-D ``←─── line N`` marker confirms the
# walker resolved a real nested line (NOT the legacy (1, 1) fallback).
# ---------------------------------------------------------------------------


def test_validate_local_yaml_plugins_typo_suggests_plugins(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A typo'd sub-key inside the ``plugins:`` overlay block triggers
    a "Did you mean" against :class:`PluginOverlay.model_fields`."""
    c = docker_container()
    # ``ad`` is Levenshtein distance 1 from ``add`` — the PluginOverlay
    # candidate list dispatched by ``_candidate_list_for(('plugins',))``.
    c.write_text(_HOME_LOCAL_YAML, "plugins:\n  ad:\n    - foo@bar\n")
    rc, out = _run_validate(c)
    assert rc == 1, out
    assert "✗ SCHEMA VALIDATION ERROR" in out
    assert "Did you mean 'add'" in out
    assert "Fix:" in out
    assert "local.yaml:" in out
    # Walker resolves the nested loc to a real line > 1 (the ``ad:`` row).
    assert "←─── line 2" in out


def test_validate_local_yaml_extensions_typo_suggests_extensions(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A typo'd sub-key inside the ``extensions:`` overlay block triggers
    a "Did you mean" against :class:`ExtensionOverlay.model_fields`."""
    c = docker_container()
    # ``adde`` is distance 1 from ``add`` (extra 'e').
    c.write_text(_HOME_LOCAL_YAML, "extensions:\n  adde:\n    - some.ext\n")
    rc, out = _run_validate(c)
    assert rc == 1, out
    assert "✗ SCHEMA VALIDATION ERROR" in out
    assert "Did you mean 'add'" in out
    assert "Fix:" in out
    assert "←─── line 2" in out


def test_validate_local_yaml_marketplaces_typo_suggests_marketplaces(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A typo'd sub-key inside the ``marketplaces:`` overlay block
    triggers a "Did you mean" against :class:`MarketplaceOverlay.model_fields`.
    """
    c = docker_container()
    # ``rem`` is distance 3 from ``remove`` — TOO FAR for the
    # Levenshtein-2 gate. Use ``remov`` (distance 1) instead.
    c.write_text(_HOME_LOCAL_YAML, "marketplaces:\n  remov:\n    - foo\n")
    rc, out = _run_validate(c)
    assert rc == 1, out
    assert "✗ SCHEMA VALIDATION ERROR" in out
    assert "Did you mean 'remove'" in out
    assert "Fix:" in out
    assert "←─── line 2" in out


def test_validate_local_yaml_host_local_sections_typo_suggests(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A typo'd sub-key inside a ``host_local_sections.<name>`` block
    triggers a "Did you mean" against :class:`HostLocalSection.model_fields`
    (``anchor`` / ``body`` / ``body_file``)."""
    c = docker_container()
    # ``bdy`` is distance 1 from ``body``. The full host_local_sections
    # shape requires nested ``anchor:`` + body discriminator, but
    # extra_forbidden fires on ``bdy`` before the exactly-one-of validator
    # runs (Pydantic processes extra-key checks first).
    c.write_text(
        _HOME_LOCAL_YAML,
        "tracked_files:\n"
        "  d:\n"
        "    host_local_sections:\n"
        "      foo:\n"
        "        bdy: hello\n",
    )
    rc, out = _run_validate(c)
    assert rc == 1, out
    assert "✗ SCHEMA VALIDATION ERROR" in out
    assert "Did you mean 'body'" in out
    assert "Fix:" in out


def test_validate_local_yaml_tracked_files_nested_typo_suggests(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A typo'd sub-key inside ``tracked_files.<id>`` (not
    host_local_sections) triggers a "Did you mean" against
    :class:`_LocalTrackedFileOverlay.model_fields`."""
    c = docker_container()
    # ``dispositon`` (dropped 'i') is distance 1 from ``disposition`` —
    # a live _LocalTrackedFileOverlay field name.
    c.write_text(
        _HOME_LOCAL_YAML,
        "tracked_files:\n  d:\n    dispositon: forked\n",
    )
    rc, out = _run_validate(c)
    assert rc == 1, out
    assert "✗ SCHEMA VALIDATION ERROR" in out
    assert "Did you mean 'disposition'" in out
    assert "Fix:" in out
