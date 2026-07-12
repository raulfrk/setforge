"""Security-critical validation gates for bundle ``file`` components.

Every gate runs BEFORE any synthetic tracked-file is minted (so it fires on
BOTH ``setforge validate`` and ``setforge install``, ahead of any deploy) and
raises :class:`~setforge.errors.ConfigError` (a :class:`SetforgeError`, so the
CLI maps it to a non-zero exit). The five gates:

1. name-collision — a synthetic ``<bundle>.<component>`` key must not collide
   with a real ``tracked_files`` key or another bundle's synthetic key.
2. dst-collision — no two resolved dst targets (real + synthetic) may coincide.
3. dst-confinement — every dst must ``.resolve()`` under ``$HOME`` (collapses
   ``..`` and intermediate symlinks, so a symlink-escape is caught).
4. id charset — bundle-id / component-id may not contain ``/``, ``..``, or a
   leading dot (else the synthetic id defeats the base-store traversal guard).
5. src-under-tracked — every file-component ``src`` must resolve under the
   config repo's ``tracked/`` dir (else it bypasses the gitleaks sweep).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from setforge.config import (
    BundleComponent,
    BundleSpec,
    Config,
    FileComponent,
    Profile,
    TrackedFile,
    expand_bundle_file_components,
    resolve_profile,
    validate_bundle_file_components,
)
from setforge.errors import ConfigError

_PROFILE = "gate-test"


def _file_comp(id_: str, **fc_kw: object) -> BundleComponent:
    base: dict[str, object] = {"src": Path("launch.sh"), "dst": "~/.local/bin/launch"}
    base.update(fc_kw)
    return BundleComponent(id=id_, file=FileComponent.model_validate(base))


def _cfg(
    *,
    bundles: dict[str, BundleSpec],
    profile_bundles: list[str] | None = None,
    tracked_files: dict[str, TrackedFile] | None = None,
    profile_tracked: list[str] | None = None,
) -> Config:
    return Config(
        tracked_files=tracked_files or {},
        bundles=bundles,
        profiles={
            _PROFILE: Profile(
                bundles=profile_bundles or list(bundles),
                tracked_files=profile_tracked or [],
            ),
        },
    )


def _resolved(cfg: Config):
    return resolve_profile(cfg, _PROFILE)


# --- gate 1: name-collision -------------------------------------------------


def test_name_collision_synthetic_vs_real_tracked_file() -> None:
    """A synthetic key equal to a real ``tracked_files`` key is rejected,
    naming the colliding id."""
    cfg = _cfg(
        bundles={"revdiff": BundleSpec(components=[_file_comp("launcher")])},
        tracked_files={"revdiff.launcher": TrackedFile(src=Path("x"), dst="~/x")},
        profile_tracked=["revdiff.launcher"],
    )
    with pytest.raises(ConfigError) as exc:
        validate_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))
    assert "revdiff.launcher" in str(exc.value)


def test_name_collision_fires_before_clobbering_overwrite() -> None:
    """The collision must be raised by ``expand_bundle_file_components`` itself,
    BEFORE it overwrites ``config.tracked_files[synthetic_id]`` — else the real
    body is silently clobbered."""
    real = TrackedFile(src=Path("real-src"), dst="~/real")
    cfg = _cfg(
        bundles={"revdiff": BundleSpec(components=[_file_comp("launcher")])},
        tracked_files={"revdiff.launcher": real},
        profile_tracked=["revdiff.launcher"],
    )
    with pytest.raises(ConfigError):
        expand_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))
    # The real body must be untouched (not clobbered by the synthetic overwrite).
    assert cfg.tracked_files["revdiff.launcher"].src == Path("real-src")


def test_name_collision_synthetic_vs_synthetic_is_charset_guarded() -> None:
    """Two DISTINCT bundles can only mint the same ``<bundle>.<component>`` key
    if a dot is embedded in a bundle/component id (e.g. bundle ``a`` comp ``b.c``
    vs bundle ``a.b`` comp ``c`` -> both ``a.b.c``). The id-charset gate rejects
    such an id first, so a genuine synthetic-vs-synthetic collision across
    distinct bundle keys is unreachable — assert the charset gate catches the
    only route to it."""
    cfg = _cfg(
        bundles={
            "a": BundleSpec(components=[_file_comp("b.c")]),  # component id has a dot
            "a.b": BundleSpec(components=[_file_comp("c")]),  # bundle id has a dot
        },
    )
    with pytest.raises(ConfigError):
        validate_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))


# --- gate 2: dst-collision --------------------------------------------------


def test_dst_collision_two_synthetics_same_target() -> None:
    cfg = _cfg(
        bundles={
            "b": BundleSpec(
                components=[
                    _file_comp("one", dst="~/.local/bin/dup"),
                    _file_comp("two", dst="~/.local/bin/dup"),
                ]
            )
        },
    )
    with pytest.raises(ConfigError) as exc:
        validate_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))
    msg = str(exc.value)
    assert "b.one" in msg
    assert "b.two" in msg


def test_dst_collision_synthetic_vs_real() -> None:
    cfg = _cfg(
        bundles={"b": BundleSpec(components=[_file_comp("one", dst="~/dup")])},
        tracked_files={"real": TrackedFile(src=Path("r"), dst="~/dup")},
        profile_tracked=["real"],
    )
    with pytest.raises(ConfigError):
        validate_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))


# --- gate 3: dst-confinement ------------------------------------------------


@pytest.mark.parametrize(
    "bad_dst",
    ["~/../etc/passwd", "/etc/cronjob", "/tmp/outside"],
)
def test_dst_confinement_rejects_out_of_home(bad_dst: str) -> None:
    cfg = _cfg(
        bundles={"b": BundleSpec(components=[_file_comp("one", dst=bad_dst)])},
    )
    with pytest.raises(ConfigError) as exc:
        validate_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))
    assert "b.one" in str(exc.value)


def test_dst_confinement_accepts_under_home() -> None:
    cfg = _cfg(
        bundles={
            "b": BundleSpec(
                components=[_file_comp("one", dst="~/.claude/plugins/x/launch.sh")]
            )
        },
    )
    # Must NOT raise.
    validate_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))


def test_dst_confinement_rejects_symlink_parent_escape(tmp_path: Path) -> None:
    """A dst whose parent is a symlink pointing outside ``$HOME`` must be
    rejected — the check runs on ``.resolve()`` (collapses the symlink), not a
    string ``startswith($HOME)``."""
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # ~/escape -> /outside ; dst ~/escape/file resolves to /outside/file.
    (home / "escape").symlink_to(outside)
    cfg = _cfg(
        bundles={
            "b": BundleSpec(
                components=[_file_comp("one", dst=str(home / "escape" / "file"))]
            )
        },
    )
    old = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        with pytest.raises(ConfigError):
            validate_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))
    finally:
        if old is not None:
            os.environ["HOME"] = old


# --- gate 4: id charset -----------------------------------------------------


@pytest.mark.parametrize("bad_component", ["a/b", "..", ".hidden", "x/../y"])
def test_id_charset_rejects_bad_component_id(bad_component: str) -> None:
    cfg = _cfg(
        bundles={"b": BundleSpec(components=[_file_comp(bad_component)])},
    )
    with pytest.raises(ConfigError):
        validate_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))


@pytest.mark.parametrize("bad_bundle", ["a/b", "..", ".hidden"])
def test_id_charset_rejects_bad_bundle_id(bad_bundle: str) -> None:
    cfg = _cfg(
        bundles={bad_bundle: BundleSpec(components=[_file_comp("ok")])},
    )
    with pytest.raises(ConfigError):
        validate_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))


def test_id_charset_only_applies_to_file_bundles() -> None:
    """A bundle WITHOUT file components with an otherwise odd id is not gated
    here (charset only matters for the synthetic tracked-file id path)."""
    cfg = Config(
        tracked_files={},
        bundles={
            "no-file": BundleSpec(
                components=[BundleComponent(id="plug", plugin="revdiff@revdiff")]
            )
        },
        profiles={_PROFILE: Profile(bundles=["no-file"])},
    )
    # No file component -> the gate is a no-op regardless of ids.
    validate_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))


# --- gate 5: src-under-tracked/ ---------------------------------------------


def test_src_must_resolve_under_tracked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    cfg = _cfg(
        bundles={
            "b": BundleSpec(
                components=[_file_comp("one", src=Path("../../outside.sh"))]
            )
        },
    )
    with pytest.raises(ConfigError) as exc:
        validate_bundle_file_components(cfg, _resolved(cfg), repo)
    assert "b.one" in str(exc.value)


def test_src_under_tracked_accepts_normal_src(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    cfg = _cfg(
        bundles={"b": BundleSpec(components=[_file_comp("one", src=Path("sub/l.sh"))])},
    )
    validate_bundle_file_components(cfg, _resolved(cfg), repo)


# --- regression: a clean bundle passes + expands ----------------------------


def test_valid_bundle_passes_gates_and_expands(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    cfg = _cfg(
        bundles={
            "revdiff": BundleSpec(
                components=[
                    _file_comp(
                        "launcher",
                        src=Path("launch.sh"),
                        dst="~/.claude/plugins/data/revdiff/launch.sh",
                        mode=0o755,
                    )
                ]
            )
        },
    )
    resolved = _resolved(cfg)
    expand_bundle_file_components(cfg, resolved, repo)
    assert "revdiff.launcher" in resolved.tracked_files
    assert cfg.tracked_files["revdiff.launcher"].mode == 0o755
