"""Tests for expanding bundle ``file`` components into synthetic tracked-files.

A bundle ``file`` component does NOT run through the provisioner driver; it
DEPLOYS like a tracked-file. :func:`expand_bundle_file_components` mints one
synthetic :class:`TrackedFile` per ``file`` component of every bundle active in
the resolved profile, keyed ``<bundle-id>.<component-id>``, and injects it into
BOTH ``resolved.tracked_files`` (the id the install-time walk iterates) AND
``config.tracked_files`` (where that walk resolves the body) — so the synthetic
entry rides the existing ``_deploy_all_tracked_files`` / revert-snapshot path
with no new deploy code.
"""

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
    expand_bundle_file_components(cfg, resolved)
    assert "revdiff.launcher" in resolved.tracked_files


def test_expansion_registers_body_in_config() -> None:
    cfg = _cfg_with_bundle(BundleSpec(components=[_file_comp("launcher", mode=0o755)]))
    resolved = resolve_profile(cfg, _PROFILE)
    expand_bundle_file_components(cfg, resolved)
    tf = cfg.tracked_files["revdiff.launcher"]
    assert isinstance(tf, TrackedFile)
    assert tf.src == Path("launch.sh")
    assert tf.dst == "~/.local/bin/launch"
    assert tf.mode == 0o755


def test_expansion_threads_all_fields() -> None:
    cfg = _cfg_with_bundle(
        BundleSpec(
            components=[
                _file_comp("launcher", mode=0o755, template=True, symlink="~/x"),
            ]
        )
    )
    resolved = resolve_profile(cfg, _PROFILE)
    expand_bundle_file_components(cfg, resolved)
    tf = cfg.tracked_files["revdiff.launcher"]
    assert tf.mode == 0o755
    assert tf.template is True
    assert tf.symlink == "~/x"


def test_expansion_skips_non_file_components() -> None:
    """A bundle with only package/plugin components mints nothing."""
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
    expand_bundle_file_components(cfg, resolved)
    assert resolved.tracked_files == before
    assert "revdiff.bin" not in cfg.tracked_files


def test_expansion_no_bundles_is_noop() -> None:
    """A profile with no active bundles is unchanged (regression)."""
    cfg = Config(
        tracked_files={"real": TrackedFile(src=Path("real.md"), dst="~/real.md")},
        profiles={_PROFILE: Profile(tracked_files=["real"])},
    )
    resolved = resolve_profile(cfg, _PROFILE)
    before = list(resolved.tracked_files)
    before_cfg = dict(cfg.tracked_files)
    expand_bundle_file_components(cfg, resolved)
    assert resolved.tracked_files == before
    assert cfg.tracked_files == before_cfg


def test_expansion_only_inactive_bundle_not_expanded() -> None:
    """A bundle declared in config but NOT in the resolved profile is skipped."""
    cfg = Config(
        tracked_files={"real": TrackedFile(src=Path("real.md"), dst="~/real.md")},
        bundles={"revdiff": BundleSpec(components=[_file_comp("launcher")])},
        # profile does NOT list the bundle
        profiles={_PROFILE: Profile(tracked_files=["real"])},
    )
    resolved = resolve_profile(cfg, _PROFILE)
    expand_bundle_file_components(cfg, resolved)
    assert "revdiff.launcher" not in resolved.tracked_files
    assert "revdiff.launcher" not in cfg.tracked_files
