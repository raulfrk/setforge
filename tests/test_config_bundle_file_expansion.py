"""Tests for expanding bundle ``file`` components into synthetic tracked-files."""

from __future__ import annotations

from pathlib import Path

from setforge.config import (
    BundleComponent,
    BundleSpec,
    CargoPackage,
    Config,
    FileComponent,
    Profile,
    TrackedFile,
    expand_bundle_file_components,
    resolve_profile,
)

_PROFILE = "expand-test"


def _cfg_with_bundle(bundle: BundleSpec) -> Config:
    return Config(
        tracked_files={"real": TrackedFile(src=Path("real.md"), dst="~/real.md")},
        bundles={"revdiff": bundle},
        profiles={
            _PROFILE: Profile(tracked_files=["real"], bundles=["revdiff"]),
        },
    )


def _file_comp(id_: str, **fc_kw: object) -> BundleComponent:
    base: dict[str, object] = {"src": Path("launch.sh"), "dst": "~/.local/bin/launch"}
    base.update(fc_kw)
    return BundleComponent(id=id_, file=FileComponent.model_validate(base))


def test_expansion_adds_synthetic_id_to_resolved() -> None:
    cfg = _cfg_with_bundle(BundleSpec(components=[_file_comp("launcher")]))
    resolved = resolve_profile(cfg, _PROFILE)
    expand_bundle_file_components(cfg, resolved, Path("/repo"))
    assert "revdiff.launcher" in resolved.tracked_files


def test_expansion_registers_body_in_config() -> None:
    cfg = _cfg_with_bundle(BundleSpec(components=[_file_comp("launcher", mode=0o755)]))
    resolved = resolve_profile(cfg, _PROFILE)
    expand_bundle_file_components(cfg, resolved, Path("/repo"))
    tf = cfg.tracked_files["revdiff.launcher"]
    assert isinstance(tf, TrackedFile)
    assert tf.src == Path("launch.sh")
    assert tf.dst == "~/.local/bin/launch"
    assert tf.mode == 0o755


def test_expansion_threads_mode_and_template() -> None:
    cfg = _cfg_with_bundle(
        BundleSpec(
            components=[
                _file_comp("launcher", mode=0o755, template=True),
            ]
        )
    )
    resolved = resolve_profile(cfg, _PROFILE)
    expand_bundle_file_components(cfg, resolved, Path("/repo"))
    tf = cfg.tracked_files["revdiff.launcher"]
    assert tf.mode == 0o755
    assert tf.template is True


def test_expansion_threads_symlink_and_template() -> None:
    # ``mode`` and ``symlink`` are mutually exclusive on TrackedFile
    # (chmod-on-symlink hits the target, not the link; deploy ignores
    # mode for symlinked files), so field-threading is exercised with the
    # symlink field paired with template rather than with mode.
    cfg = _cfg_with_bundle(
        BundleSpec(
            components=[
                _file_comp("launcher", template=True, symlink="~/x"),
            ]
        )
    )
    resolved = resolve_profile(cfg, _PROFILE)
    expand_bundle_file_components(cfg, resolved, Path("/repo"))
    tf = cfg.tracked_files["revdiff.launcher"]
    assert tf.symlink == "~/x"
    assert tf.template is True


def test_expansion_skips_non_file_components() -> None:
    bundle = BundleSpec(
        components=[BundleComponent(id="bin", cargo=CargoPackage(crate="rg"))]
    )
    cfg = Config(
        tracked_files={"real": TrackedFile(src=Path("real.md"), dst="~/real.md")},
        bundles={"revdiff": bundle},
        profiles={_PROFILE: Profile(tracked_files=["real"], bundles=["revdiff"])},
    )
    resolved = resolve_profile(cfg, _PROFILE)
    before = list(resolved.tracked_files)
    expand_bundle_file_components(cfg, resolved, Path("/repo"))
    assert resolved.tracked_files == before
    assert "revdiff.bin" not in cfg.tracked_files


def test_expansion_no_bundles_is_noop() -> None:
    cfg = Config(
        tracked_files={"real": TrackedFile(src=Path("real.md"), dst="~/real.md")},
        profiles={_PROFILE: Profile(tracked_files=["real"])},
    )
    resolved = resolve_profile(cfg, _PROFILE)
    before = list(resolved.tracked_files)
    before_cfg = dict(cfg.tracked_files)
    expand_bundle_file_components(cfg, resolved, Path("/repo"))
    assert resolved.tracked_files == before
    assert cfg.tracked_files == before_cfg


def test_expansion_only_inactive_bundle_not_expanded() -> None:
    cfg = Config(
        tracked_files={"real": TrackedFile(src=Path("real.md"), dst="~/real.md")},
        bundles={"revdiff": BundleSpec(components=[_file_comp("launcher")])},
        profiles={_PROFILE: Profile(tracked_files=["real"])},
    )
    resolved = resolve_profile(cfg, _PROFILE)
    expand_bundle_file_components(cfg, resolved, Path("/repo"))
    assert "revdiff.launcher" not in resolved.tracked_files
    assert "revdiff.launcher" not in cfg.tracked_files


def test_expansion_is_idempotent() -> None:
    cfg = _cfg_with_bundle(BundleSpec(components=[_file_comp("launcher")]))
    resolved = resolve_profile(cfg, _PROFILE)
    expand_bundle_file_components(cfg, resolved, Path("/repo"))
    expand_bundle_file_components(cfg, resolved, Path("/repo"))
    assert resolved.tracked_files.count("revdiff.launcher") == 1
