"""Seed-once host-local section template tests (install integration + unit).

Exercises the SEED-ONCE library end to end through the real ``install``
command (CliRunner) and at the unit level
(:func:`setforge.section_templates.plan_section_seeds` /
:func:`~setforge.section_templates.seed_section_templates`):

- seed-empty: a slot whose host-local section is missing on the host is
  seeded with the template body, which then deploys into the live file.
- leave-populated: a section the host already carries (an overlay body)
  is NEVER reseeded — neither the local.yaml body nor the live content is
  overwritten.
- survive-reinstall: a live edit captured into the host's overlay survives
  a re-install; the library template does not clobber it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import section_templates as st
from setforge import source as source_mod
from setforge.cli import app
from setforge.cli._welcome import WelcomeChoice
from setforge.config import (
    Config,
    Profile,
    SectionTemplateRef,
    TrackedFile,
    resolve_profile,
)
from setforge.errors import ConfigError, DirtySourceCheckout

_PROFILE = "seed-test"

_DOC = """\
# Title

## Notes

upstream notes body
"""

_TEMPLATE_BODY = "SEEDED PYTHON CONVENTIONS\n"


# --------------------------------------------------------------------------
# Unit: plan_section_seeds + seed_section_templates
# --------------------------------------------------------------------------


def _cfg() -> Config:
    return Config(
        tracked_files={"doc": TrackedFile(src=Path("doc.md"), dst="~/x/doc.md")},
        section_templates={"py-conv": SectionTemplateRef(src=Path("py-conv.md"))},
        profiles={
            _PROFILE: Profile(tracked_files=["doc"], section_slots={"py": "py-conv"})
        },
    )


def _repo_with_template(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "templates").mkdir(parents=True)
    (repo / "templates" / "py-conv.md").write_text(_TEMPLATE_BODY, encoding="utf-8")
    return repo


def test_resolve_template_src_roots_at_templates_dir(tmp_path: Path) -> None:
    ref = SectionTemplateRef(src=Path("sub/py-conv.md"))
    expected = tmp_path / "templates" / "sub/py-conv.md"
    assert st.resolve_template_src(ref, tmp_path) == expected


def test_plan_seeds_empty_section(tmp_path: Path) -> None:
    repo = _repo_with_template(tmp_path)
    cfg = _cfg()
    resolved = resolve_profile(cfg, _PROFILE)
    plan = st.plan_section_seeds(cfg, resolved, repo, existing_overlay={})
    assert len(plan) == 1
    assert plan[0].section_name == "py"
    assert plan[0].template_name == "py-conv"
    assert plan[0].body == _TEMPLATE_BODY
    assert plan[0].tracked_file_id == "doc"


def test_plan_leaves_populated_section(tmp_path: Path) -> None:
    """A section already present on the host yields no seed entry."""
    repo = _repo_with_template(tmp_path)
    cfg = _cfg()
    resolved = resolve_profile(cfg, _PROFILE)
    plan = st.plan_section_seeds(cfg, resolved, repo, existing_overlay={"doc": {"py"}})
    assert plan == []


def test_seed_writes_then_is_seed_once(tmp_path: Path) -> None:
    repo = _repo_with_template(tmp_path)
    cfg = _cfg()
    resolved = resolve_profile(cfg, _PROFILE)
    local = tmp_path / "local.yaml"

    plan = st.plan_section_seeds(cfg, resolved, repo, existing_overlay={})
    assert st.seed_section_templates(plan, local) is True
    text = local.read_text(encoding="utf-8")
    assert "host_local_sections:" in text
    assert "SEEDED PYTHON CONVENTIONS" in text
    assert "kind: at-end-of-file" in text

    # Re-plan against the now-populated overlay → empty plan → no-op write.
    plan2 = st.plan_section_seeds(cfg, resolved, repo, existing_overlay={"doc": {"py"}})
    assert plan2 == []
    assert st.seed_section_templates(plan2, local) is False


def test_seed_no_markdown_tracked_file_is_noop(tmp_path: Path) -> None:
    repo = _repo_with_template(tmp_path)
    cfg = Config(
        tracked_files={"cfg": TrackedFile(src=Path("settings.json"), dst="~/s.json")},
        section_templates={"py-conv": SectionTemplateRef(src=Path("py-conv.md"))},
        profiles={
            _PROFILE: Profile(tracked_files=["cfg"], section_slots={"py": "py-conv"})
        },
    )
    resolved = resolve_profile(cfg, _PROFILE)
    assert st.plan_section_seeds(cfg, resolved, repo, existing_overlay={}) == []


def test_seed_refuses_non_mapping_local_yaml(tmp_path: Path) -> None:
    """A non-mapping local.yaml must raise rather than be clobbered with a
    fresh map (which would discard the user's content)."""
    repo = _repo_with_template(tmp_path)
    cfg = _cfg()
    resolved = resolve_profile(cfg, _PROFILE)
    local = tmp_path / "local.yaml"
    local.write_text("- just\n- a\n- list\n", encoding="utf-8")
    plan = st.plan_section_seeds(cfg, resolved, repo, existing_overlay={})
    with pytest.raises(ConfigError, match="must be a mapping"):
        st.seed_section_templates(plan, local)
    # File left untouched.
    assert local.read_text(encoding="utf-8") == "- just\n- a\n- list\n"


def test_seed_refuses_non_mapping_tracked_files(tmp_path: Path) -> None:
    """A non-mapping tracked_files value must raise rather than be clobbered."""
    repo = _repo_with_template(tmp_path)
    cfg = _cfg()
    resolved = resolve_profile(cfg, _PROFILE)
    local = tmp_path / "local.yaml"
    local.write_text("tracked_files: not-a-map\n", encoding="utf-8")
    plan = st.plan_section_seeds(cfg, resolved, repo, existing_overlay={})
    with pytest.raises(ConfigError, match=r"tracked_files .* must be a mapping"):
        st.seed_section_templates(plan, local)


# --------------------------------------------------------------------------
# Integration: real install command
# --------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    (target / "tracked").mkdir(parents=True)
    (target / "tracked" / "doc.md").write_text(_DOC, encoding="utf-8")
    (target / "templates").mkdir(parents=True)
    (target / "templates" / "py-conv.md").write_text(_TEMPLATE_BODY, encoding="utf-8")
    return target


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.setforge_seed/doc.md\n"
        "section_templates:\n"
        "  py-conv:\n"
        "    src: py-conv.md\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - doc\n"
        "    section_slots:\n"
        "      python-conventions: py-conv\n",
        encoding="utf-8",
    )
    return config


def _live_doc_path() -> Path:
    return Path("~/.setforge_seed/doc.md").expanduser()


def _local_yaml_path(tmp_path: Path) -> Path:
    return tmp_path / "local.yaml"


def _install(config: Path, *, no_transition: bool = True) -> Result:
    args = [
        "install",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-secrets-scan",
        "--no-git-check",
        "--yes",
    ]
    if no_transition:
        args.append("--no-transition")
    return CliRunner().invoke(app, args)


def test_install_seeds_empty_section_into_live(repo: Path, tmp_path: Path) -> None:
    """First install seeds the empty slot; the body lands in the live file."""
    config = _write_config(repo)
    local = _local_yaml_path(tmp_path)
    assert not local.exists()

    result = _install(config)
    assert result.exit_code == 0, result.output

    # local.yaml was seeded (then migrated to an OVERLAY span by install).
    assert local.exists()
    local_text = local.read_text(encoding="utf-8")
    assert "python-conventions" in local_text

    # The seeded body deployed into the live doc.
    live = _live_doc_path().read_text(encoding="utf-8")
    assert "SEEDED PYTHON CONVENTIONS" in live
    # The template body never leaked back into tracked.
    tracked = (repo / "tracked" / "doc.md").read_text(encoding="utf-8")
    assert "SEEDED PYTHON CONVENTIONS" not in tracked


def test_install_seed_once_preserves_live_edit(repo: Path, tmp_path: Path) -> None:
    """A live edit captured into the overlay survives re-install; the
    template does NOT overwrite the populated section."""
    config = _write_config(repo)
    local = _local_yaml_path(tmp_path)

    # First install seeds the section.
    assert _install(config).exit_code == 0

    # Host edits the adopted body in local.yaml (the host-owned store).
    local_text = local.read_text(encoding="utf-8")
    edited = local_text.replace("SEEDED PYTHON CONVENTIONS", "MY HOST EDIT")
    assert edited != local_text
    local.write_text(edited, encoding="utf-8")

    # Re-install: seed-once must NOT reseed (section already populated).
    result = _install(config)
    assert result.exit_code == 0, result.output

    final_local = local.read_text(encoding="utf-8")
    assert "MY HOST EDIT" in final_local
    assert "SEEDED PYTHON CONVENTIONS" not in final_local

    live = _live_doc_path().read_text(encoding="utf-8")
    assert "MY HOST EDIT" in live
    assert "SEEDED PYTHON CONVENTIONS" not in live


def test_install_leaves_prepopulated_section_untouched(
    repo: Path, tmp_path: Path
) -> None:
    """A section the host already declared is left as-is on first install."""
    config = _write_config(repo)
    local = _local_yaml_path(tmp_path)
    local.write_text(
        "tracked_files:\n"
        "  doc:\n"
        "    host_local_sections:\n"
        "      python-conventions:\n"
        "        anchor:\n"
        "          kind: after-heading\n"
        "          value: Notes\n"
        "        body: |\n"
        "          PRE-EXISTING HOST BODY\n",
        encoding="utf-8",
    )

    result = _install(config)
    assert result.exit_code == 0, result.output

    final_local = local.read_text(encoding="utf-8")
    assert "PRE-EXISTING HOST BODY" in final_local
    assert "SEEDED PYTHON CONVENTIONS" not in final_local

    live = _live_doc_path().read_text(encoding="utf-8")
    assert "PRE-EXISTING HOST BODY" in live
    assert "SEEDED PYTHON CONVENTIONS" not in live


# --------------------------------------------------------------------------
# Consent-gate ordering: the seed must NOT touch local.yaml until
# AFTER the welcome consent and git-status gates have passed.
# --------------------------------------------------------------------------


def _is_seeded(local: Path) -> bool:
    """True when local.yaml carries the seeded template body.

    The Typer root callback writes a comment-only stub on every
    invocation, so file existence is NOT a seed signal — the body marker
    is. The stub never contains the template body.
    """
    if not local.exists():
        return False
    return "SEEDED PYTHON CONVENTIONS" in local.read_text(encoding="utf-8")


@pytest.mark.fresh_host
def test_install_welcome_decline_leaves_local_unseeded(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Declining the fresh-host welcome leaves local.yaml unwritten.

    The seed is only PLANNED before consent; the disk COMMIT is gated
    behind a PROCEED. A decline must return with zero mutation.
    """
    config = _write_config(repo)
    local = _local_yaml_path(tmp_path)
    assert not local.exists()
    # Reach the welcome gate (no --yes) and force a non-PROCEED choice.
    monkeypatch.setattr(
        "setforge.cli.install.prompt_welcome",
        lambda **_kwargs: WelcomeChoice.ABORT,
    )
    result = CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--no-secrets-scan",
            "--no-git-check",
            "--no-transition",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not _is_seeded(local), "declining the welcome must not seed local.yaml: " + (
        local.read_text(encoding="utf-8") if local.exists() else ""
    )
    assert source_mod._pending_seed is None


def test_install_git_check_abort_leaves_local_unseeded(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git-status abort leaves local.yaml unwritten.

    The git-check gate fires before the under-lock COMMIT, so a dirty /
    stale source that raises must leave local.yaml untouched.
    """
    config = _write_config(repo)
    local = _local_yaml_path(tmp_path)
    assert not local.exists()

    def _abort(**_kwargs: object) -> None:
        raise DirtySourceCheckout("source tree is dirty")

    monkeypatch.setattr("setforge.cli.install.run_git_check_or_raise", _abort)
    result = CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--no-secrets-scan",
            "--yes",
            "--no-transition",
        ],
    )
    assert result.exit_code != 0
    assert not _is_seeded(local), "a git-check abort must not seed local.yaml: " + (
        local.read_text(encoding="utf-8") if local.exists() else ""
    )
    assert source_mod._pending_seed is None


def test_install_clears_pending_seed_on_happy_path(repo: Path, tmp_path: Path) -> None:
    """A successful install leaves no leaked seed payload in module state."""
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    assert source_mod._pending_seed is None


def test_dry_run_install_does_not_seed_or_leak(repo: Path, tmp_path: Path) -> None:
    """A top-level --dry-run neither writes local.yaml nor leaves a seed set."""
    config = _write_config(repo)
    local = _local_yaml_path(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--no-secrets-scan",
            "--no-git-check",
            "--yes",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not _is_seeded(local)
    assert source_mod._pending_seed is None


# --------------------------------------------------------------------------
# In-memory injection: with a seed plan pending, the overlay readers observe
# the seeded section WITHOUT any disk write.
# --------------------------------------------------------------------------


def test_injected_seed_feeds_overlay_readers(tmp_path: Path) -> None:
    """``injected_seed`` makes the plan visible to local.yaml parses.

    All three pre-consent readers funnel through
    ``source._load_local_source_config``; a pending seed must surface in
    ``load_local_host_local_sections`` with no file on disk, then clear
    on context exit.
    """
    repo = _repo_with_template(tmp_path)
    cfg = _cfg()
    resolved = resolve_profile(cfg, _PROFILE)
    plan = st.plan_section_seeds(cfg, resolved, repo, existing_overlay={})
    local = tmp_path / "absent-local.yaml"
    assert not local.exists()

    # Without the injection, an absent local.yaml projects no sections.
    assert source_mod.load_local_host_local_sections(local) == {}

    with source_mod.injected_seed(plan):
        overlay = source_mod.load_local_host_local_sections(local)
        assert "doc" in overlay
        assert "py" in {str(name) for name in overlay["doc"]}
    # No disk write, and the payload is cleared on exit.
    assert not local.exists()
    assert source_mod._pending_seed is None


def test_injected_seed_is_not_reentrant(tmp_path: Path) -> None:
    """A nested injection is a caller bug — the context manager refuses it."""
    # The nesting IS the assertion (an outer injection active when an inner
    # one is opened); it cannot be flattened into a single `with`.
    with source_mod.injected_seed([]):  # noqa: SIM117
        with pytest.raises(AssertionError):
            with source_mod.injected_seed([]):
                pass
    assert source_mod._pending_seed is None


# --------------------------------------------------------------------------
# Revert: the seed COMMIT is recorded in the transition, so revert restores
# an unseeded local.yaml (acceptance #4).
# --------------------------------------------------------------------------


def test_revert_after_seed_restores_unseeded_local(repo: Path, tmp_path: Path) -> None:
    """Install seeds local.yaml; revert restores its pre-seed content."""
    config = _write_config(repo)
    local = _local_yaml_path(tmp_path)
    # Install WITH a transition record (no --no-transition) so revert has
    # something to reverse.
    install = CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--no-secrets-scan",
            "--no-git-check",
            "--yes",
        ],
    )
    assert install.exit_code == 0, install.output
    assert _is_seeded(local), "install should have seeded local.yaml"

    revert = CliRunner().invoke(
        app,
        [
            "revert",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--yes",
        ],
    )
    assert revert.exit_code == 0, revert.output
    assert not _is_seeded(local), "revert must restore an unseeded local.yaml: " + (
        local.read_text(encoding="utf-8") if local.exists() else "<absent>"
    )
