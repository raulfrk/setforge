"""Tests for the 5.0 -> 6.0 profile-fields-retire migration.

The migration folds the four legacy per-profile fields —
``cargo_binaries`` / ``claude_plugins`` / ``plugins_reconcile`` /
``extensions`` — into the modern flat top-level ``packages`` registry plus
the per-profile ``packages`` ref-list and ``reconcile`` block, then drops the
legacy keys and stamps ``schema_version: '6.0'``.

Forward + reverse are exercised directly on ruamel fixtures built in
``tmp_path`` (never through the CLI driver), constructing a
:class:`~setforge.migrations.MigrationRoots` pointed at the fixture. The
destructive drop is gated behind an operator-declared ``minimum_version >=
6.0`` floor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from setforge.errors import ConfigError
from setforge.migrations import MigrationRoots
from setforge.migrations._profile_fields_retire import (
    ProfileFieldsRetireMigration,
    _ProfileFieldsRetireReverse,
)
from setforge.migrations._yaml_ops import yaml_rt

_CFG = """\
schema_version: "5.0"
minimum_version: "6.0"
tracked_files: {}
claude_plugins:
  registry-entry:
    marketplace: some-market
marketplaces:
  some-market:
    kind: github
    repo: acme/plugins
profiles:
  default:
    cargo_binaries: [rg]
    claude_plugins: [my-plugin]
    plugins_reconcile: prune
    extensions:
      include: [ms-python.python]
      exclude: [GitHub.copilot]
      reconcile: additive
"""


def _write_cfg(tmp_path: Path, body: str = _CFG) -> MigrationRoots:
    """Write ``body`` to ``tmp_path/setforge.yaml`` and return roots at it."""
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body, encoding="utf-8")
    return MigrationRoots(cfg_path=cfg, repo_root=tmp_path, home=tmp_path)


def _load(roots: MigrationRoots) -> Any:
    with roots.cfg_path.open("r", encoding="utf-8") as fh:
        return yaml_rt().load(fh)


def test_forward_translates_four_fields(tmp_path: Path) -> None:
    roots = _write_cfg(tmp_path)
    ProfileFieldsRetireMigration().apply(roots=roots)

    data = _load(roots)
    packages = data["packages"]
    assert dict(packages["rg"]) == {"type": "cargo", "crate": "rg"}
    assert dict(packages["my-plugin"]) == {"type": "plugin", "plugin": "my-plugin"}
    assert dict(packages["ms-python.python"]) == {
        "type": "extension",
        "extension": "ms-python.python",
    }

    profile = data["profiles"]["default"]
    assert list(profile["packages"]) == ["rg", "my-plugin", "ms-python.python"]
    assert profile["reconcile"]["plugins"]["policy"] == "prune"
    assert dict(profile["reconcile"]["extensions"]) == {
        "exclude": ["GitHub.copilot"],
        "policy": "additive",
    }

    legacy_fields = (
        "cargo_binaries",
        "claude_plugins",
        "plugins_reconcile",
        "extensions",
    )
    for legacy in legacy_fields:
        assert legacy not in profile

    assert data["schema_version"] == "6.0"
    assert "registry-entry" in data["claude_plugins"]
    assert "some-market" in data["marketplaces"]


def test_round_trip_restores_legacy_config(tmp_path: Path) -> None:
    roots = _write_cfg(tmp_path)
    original = _load(roots)

    forward = ProfileFieldsRetireMigration()
    forward.apply(roots=roots)
    forward.reverse.apply(roots=roots)

    restored = _plain(_load(roots))
    original_plain = _plain(original)
    # The reverse LOWERS a stale >5.0 floor (operator attestation, not user data).
    assert restored["minimum_version"] == "1.0"
    restored.pop("minimum_version")
    original_plain.pop("minimum_version")
    assert restored == original_plain
    assert restored["schema_version"] == "5.0"
    assert "schema_version" in restored

    rev = _ProfileFieldsRetireReverse()
    assert rev.reverse.from_version == "5.0"
    assert rev.reverse.to_version == "6.0"


# rg is a MINTED cargo ref; mytool is a NATIVE 5.0+ top-level package ref.
_CFG_NATIVE_REF = """\
schema_version: "6.0"
minimum_version: "6.0"
tracked_files: {}
packages:
  rg:
    type: cargo
    crate: rg
  mytool:
    type: python
    package: mytool
    version: "1.2.3"
profiles:
  default:
    packages: [rg, mytool]
"""


def test_reverse_preserves_native_package_refs(tmp_path: Path) -> None:
    roots = _write_cfg(tmp_path, _CFG_NATIVE_REF)
    _ProfileFieldsRetireReverse().apply(roots=roots)

    data = _load(roots)
    profile = data["profiles"]["default"]
    assert list(profile["cargo_binaries"]) == ["rg"]
    assert "rg" not in data["packages"]
    # mytool was never minted, so the reverse never drops it.
    assert list(profile["packages"]) == ["mytool"]
    assert dict(data["packages"]["mytool"]) == {
        "type": "python",
        "package": "mytool",
        "version": "1.2.3",
    }
    assert data["schema_version"] == "5.0"


def _plain(node: Any) -> Any:
    """Recursively convert a ruamel document into plain dict/list for equality."""
    from collections.abc import Mapping, Sequence

    if isinstance(node, Mapping):
        return {k: _plain(v) for k, v in node.items()}
    if isinstance(node, Sequence) and not isinstance(node, str | bytes):
        return [_plain(v) for v in node]
    return node


_CFG_COLLISION_SAME = """\
schema_version: "5.0"
minimum_version: "6.0"
tracked_files: {}
profiles:
  a:
    cargo_binaries: [rg]
  b:
    cargo_binaries: [rg]
"""

_CFG_COLLISION_DIFF = """\
schema_version: "5.0"
minimum_version: "6.0"
tracked_files: {}
profiles:
  a:
    cargo_binaries: [rg]
  b:
    claude_plugins: [rg]
"""


def test_collision_same_body_dedups(tmp_path: Path) -> None:
    roots = _write_cfg(tmp_path, _CFG_COLLISION_SAME)
    ProfileFieldsRetireMigration().apply(roots=roots)
    data = _load(roots)
    assert dict(data["packages"]["rg"]) == {"type": "cargo", "crate": "rg"}
    assert list(data["profiles"]["a"]["packages"]) == ["rg"]
    assert list(data["profiles"]["b"]["packages"]) == ["rg"]


def test_collision_diff_body_raises(tmp_path: Path) -> None:
    roots = _write_cfg(tmp_path, _CFG_COLLISION_DIFF)
    with pytest.raises(ConfigError) as exc:
        ProfileFieldsRetireMigration().apply(roots=roots)
    msg = str(exc.value)
    assert "rg" in msg
    assert "rename" in msg.lower()


_CFG_MALFORMED_PACKAGES = """\
schema_version: "5.0"
minimum_version: "6.0"
tracked_files: {}
packages:
  - not
  - a
  - mapping
profiles:
  default:
    cargo_binaries: [rg]
"""


def test_non_mapping_packages_refuses_without_clobbering(tmp_path: Path) -> None:
    roots = _write_cfg(tmp_path, _CFG_MALFORMED_PACKAGES)
    before = roots.cfg_path.read_bytes()
    with pytest.raises(ConfigError) as exc:
        ProfileFieldsRetireMigration().apply(roots=roots)
    assert "packages" in str(exc.value)
    assert roots.cfg_path.read_bytes() == before


_CFG_NO_FLOOR = """\
schema_version: "5.0"
tracked_files: {}
profiles:
  default:
    cargo_binaries: [rg]
"""

_CFG_LOW_FLOOR = """\
schema_version: "5.0"
minimum_version: "5.0"
tracked_files: {}
profiles:
  default:
    cargo_binaries: [rg]
"""


@pytest.mark.parametrize("body", [_CFG_NO_FLOOR, _CFG_LOW_FLOOR])
def test_floor_gate_refuses_below_6(tmp_path: Path, body: str) -> None:
    roots = _write_cfg(tmp_path, body)
    before = roots.cfg_path.read_bytes()
    with pytest.raises(ConfigError):
        ProfileFieldsRetireMigration().apply(roots=roots)
    assert roots.cfg_path.read_bytes() == before


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def test_apply_writes_terminal_transition(tmp_path: Path, state_dir: Path) -> None:
    from setforge import transitions

    roots_base = _write_cfg(tmp_path)
    origin = roots_base.cfg_path.read_text(encoding="utf-8")
    roots = MigrationRoots(
        cfg_path=roots_base.cfg_path,
        repo_root=tmp_path,
        home=tmp_path,
        pre_chain_snapshot={roots_base.cfg_path: origin},
    )

    ProfileFieldsRetireMigration().apply(roots=roots)
    transformed = roots.cfg_path.read_text(encoding="utf-8")

    latest = transitions.load_latest(transitions.MIGRATE_TRANSITION_PROFILE)
    assert latest is not None
    meta = transitions.load_meta(latest)
    assert meta.command is transitions.TransitionCommand.MIGRATE

    assert transitions.load_state_snapshots(latest) is None

    patch = (latest / "changes.patch").read_text(encoding="utf-8")
    assert patch == transitions.compute_patch(
        {roots.cfg_path: origin}, {roots.cfg_path: transformed}
    )
