"""Docker E2E: ext/plugin/merge commands resolve the source layer.

Before the audit fix, ``merge`` and every ext/plugin/marketplace command
passed the raw default config path straight to ``load_config`` without
consulting the source layer — so running them from any directory other
than the config-repo root raised ``FileNotFoundError``. These tests
configure a path-kind ``source:`` in ``~/.config/setforge/local.yaml``,
then invoke the commands from a CWD that is NOT the config repo and
assert they resolve the config instead of failing.

Self-contained: each test writes its own config repo under ``/tmp/cfg``
and does not touch the shared fixtures tree.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

_SRC_REPO = "/tmp/cfg"
_HOME_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"
# Commands run from the container's default CWD (/workspace — the engine
# repo root), which is NOT the config repo (_SRC_REPO) and contains no
# setforge.yaml. Pre-fix, the broken commands passed a bare relative
# "setforge.yaml" to load_config and failed there; post-fix they resolve
# via the local.yaml path source. (We can't cd into an arbitrary dir
# because `uv run` must see the project at /workspace.)


def _bootstrap_source_repo(c: ContainerHandle) -> None:
    """Write a minimal config repo at ``_SRC_REPO`` + a path source block."""
    c.write_text(
        f"{_SRC_REPO}/setforge.yaml",
        "version: 1\n"
        "schema_version: '1.0'\n"
        "tracked_files:\n"
        "  foo:\n"
        "    src: foo.md\n"
        "    dst: /tmp/out/foo.md\n"
        "profiles:\n"
        "  base:\n"
        "    tracked_files:\n"
        "      - foo\n",
    )
    # resolve_src is repo_root/tracked/<src>; the file must exist so
    # compare (driven by merge) doesn't error on a missing source.
    c.write_text(f"{_SRC_REPO}/tracked/foo.md", "# foo\n")
    c.write_text(
        _HOME_LOCAL_YAML,
        f"source:\n  kind: path\n  path: {_SRC_REPO}\n",
    )


def _assert_resolved(stdout: str, stderr: str, returncode: int) -> None:
    """A resolved command exits 0 and never reports a missing config file."""
    combined = stdout + stderr
    assert returncode == 0, combined
    assert "FileNotFoundError" not in combined, combined
    assert "config file not found" not in combined.lower(), combined


def test_merge_resolves_source_layer_from_wrong_cwd(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``setforge merge`` resolves a path source from a non-config CWD."""
    c = docker_container()
    _bootstrap_source_repo(c)
    res = c.exec(
        ["uv", "run", "setforge", "merge", "--profile=base"],
        check=False,
    )
    # Exit 0 with no config-not-found error proves merge resolved the path
    # source instead of failing on a bare relative "setforge.yaml" (the
    # pre-fix behavior). The exact stdout message is incidental.
    _assert_resolved(res.stdout, res.stderr, res.returncode)


def test_ext_list_resolves_source_layer_from_wrong_cwd(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``setforge ext list`` resolves a path source from a non-config CWD."""
    c = docker_container()
    _bootstrap_source_repo(c)
    res = c.exec(
        ["uv", "run", "setforge", "ext", "list", "--profile=base"],
        check=False,
    )
    _assert_resolved(res.stdout, res.stderr, res.returncode)


def test_plugin_list_resolves_source_layer_from_wrong_cwd(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``setforge plugin list`` resolves a path source from a non-config CWD."""
    c = docker_container()
    _bootstrap_source_repo(c)
    res = c.exec(
        ["uv", "run", "setforge", "plugin", "list", "--profile=base"],
        check=False,
    )
    _assert_resolved(res.stdout, res.stderr, res.returncode)


def test_marketplace_update_needs_no_source(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """The counterpoint: ``marketplace update`` only shells to ``claude`` and
    loads no config, so it must run with NO source configured. It must NOT
    gain the spurious ``NoSourceConfigured`` failure the resolve batch added
    to the config-consuming commands — run with no ``source:`` block and no
    ``setforge.yaml`` in CWD and assert no source error / traceback."""
    c = docker_container()
    # Deliberately NO _bootstrap_source_repo: no source block, and the
    # default CWD (/workspace) has no setforge.yaml. A command that wrongly
    # resolved config would raise NoSourceConfigured here.
    res = c.exec(
        ["uv", "run", "setforge", "marketplace", "update", "mp"],
        check=False,
    )
    combined = res.stdout + res.stderr
    assert "NoSourceConfigured" not in combined, combined
    assert "Traceback (most recent call last)" not in combined, combined
