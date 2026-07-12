"""Security-critical validation gates for bundle ``file`` components."""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.config import (
    BundleComponent,
    BundleSpec,
    Config,
    FileComponent,
    Profile,
    ResolvedProfile,
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


def _resolved(cfg: Config) -> ResolvedProfile:
    return resolve_profile(cfg, _PROFILE)


def test_name_collision_synthetic_vs_real_tracked_file() -> None:
    cfg = _cfg(
        bundles={"revdiff": BundleSpec(components=[_file_comp("launcher")])},
        tracked_files={"revdiff.launcher": TrackedFile(src=Path("x"), dst="~/x")},
        profile_tracked=["revdiff.launcher"],
    )
    with pytest.raises(ConfigError) as exc:
        validate_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))
    assert "revdiff.launcher" in str(exc.value)


def test_name_collision_fires_before_clobbering_overwrite() -> None:
    real = TrackedFile(src=Path("real-src"), dst="~/real")
    cfg = _cfg(
        bundles={"revdiff": BundleSpec(components=[_file_comp("launcher")])},
        tracked_files={"revdiff.launcher": real},
        profile_tracked=["revdiff.launcher"],
    )
    with pytest.raises(ConfigError):
        expand_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))
    assert cfg.tracked_files["revdiff.launcher"].src == Path("real-src")


def test_name_collision_synthetic_vs_synthetic_is_charset_guarded() -> None:
    cfg = _cfg(
        bundles={
            "a": BundleSpec(components=[_file_comp("b.c")]),
            "a.b": BundleSpec(components=[_file_comp("c")]),
        },
    )
    with pytest.raises(ConfigError):
        validate_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))


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
    validate_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))


def test_dst_confinement_rejects_symlink_parent_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "escape").symlink_to(outside)
    cfg = _cfg(
        bundles={
            "b": BundleSpec(
                components=[_file_comp("one", dst=str(home / "escape" / "file"))]
            )
        },
    )
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(ConfigError):
        validate_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))


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
    cfg = Config(
        tracked_files={},
        bundles={
            "no-file": BundleSpec(
                components=[BundleComponent(id="plug", plugin="revdiff@revdiff")]
            )
        },
        profiles={_PROFILE: Profile(bundles=["no-file"])},
    )
    validate_bundle_file_components(cfg, _resolved(cfg), Path("/repo"))


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
