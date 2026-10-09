"""A misspelled destination-template variable stops install before any write.

``validate`` renders a ``template: true`` destination with strict undefined
handling and rejects an unknown variable. ``install`` (and its dry-run) must
refuse the same config rather than collapse the unknown variable to nothing and
deploy to a different path than the one written.

Drives the real ``setforge`` CLI against a temp config repo with a sandboxed
``$HOME`` + ``$SETFORGE_STATE_DIR``.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import transitions
from setforge.cli import app
from setforge.cli import orphans as orphans_mod
from setforge.errors import ConfigError
from setforge.file_ownership import file_resource_id
from setforge.ownership import OwnershipStore
from tests.shared_fixtures import ConfigRepo

_PROFILE = "test-template"
_BODY = "tracked body\n"


def _write_config(config_repo: ConfigRepo, dst: str) -> Path:
    config_repo.write_tracked("wrong.txt", _BODY)
    return config_repo.write_config(
        profile=_PROFILE,
        tracked_files={"t": {"src": "wrong.txt", "dst": dst, "template": True}},
    )


def _run(verb: str, config: Path, *extra: str) -> Result:
    args = [verb, f"--profile={_PROFILE}", f"--config={config}", *extra]
    return CliRunner().invoke(app, args)


def _install(config: Path, *extra: str) -> Result:
    return _run(
        "install", config, "--no-secrets-scan", "--no-git-check", "--yes", *extra
    )


def _transition_count() -> int:
    root = transitions.transitions_root()
    if not root.exists():
        return 0
    return sum(1 for entry in root.iterdir() if entry.is_dir())


@pytest.mark.parametrize("extra", [(), ("--dry-run",)], ids=["install", "dry-run"])
def test_install_refuses_undefined_destination_variable_before_writing(
    config_repo: ConfigRepo, extra: tuple[str, ...]
) -> None:
    """A typo'd variable exits non-zero, names the variable, writes nothing."""
    config = _write_config(config_repo, "{{ home }}/{{ home_typo }}/wrong.txt")

    validated = _run("validate", config)
    assert validated.exit_code == 1, validated.output
    assert "home_typo" in validated.output

    result = _install(config, *extra)

    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, ConfigError)
    assert "home_typo" in str(result.exception)
    assert not (Path.home() / "wrong.txt").exists()
    assert _transition_count() == 0


def test_install_defined_destination_variable_still_deploys(
    config_repo: ConfigRepo,
) -> None:
    """A valid ``{{ home }}`` destination resolves and deploys as before."""
    config = _write_config(config_repo, "{{ home }}/.setforge_template/ok.txt")

    assert _run("validate", config).exit_code == 0

    result = _install(config)

    assert result.exit_code == 0, result.output
    deployed = Path.home() / ".setforge_template" / "ok.txt"
    assert deployed.read_text(encoding="utf-8") == _BODY


def _break_destination(config: Path) -> None:
    """Misspell the destination variable, as a hasty config edit would."""
    text = config.read_text(encoding="utf-8")
    assert "{{ home }}/out" in text
    config.write_text(
        text.replace("{{ home }}/out", "{{ home_typo }}/out"), encoding="utf-8"
    )


def test_revert_still_undoes_install_after_the_destination_is_misspelled(
    config_repo: ConfigRepo,
) -> None:
    """A bad config edit must not trap the user: undo has to keep working."""
    config = _write_config(config_repo, "{{ home }}/out/good.txt")
    deployed = Path.home() / "out" / "good.txt"
    assert _install(config).exit_code == 0
    config_repo.write_tracked("wrong.txt", "second body\n")
    assert _install(config).exit_code == 0
    assert deployed.read_text(encoding="utf-8") == "second body\n"

    _break_destination(config)
    assert _run("validate", config).exit_code == 1

    result = _run("revert", config, "--yes")

    assert result.exit_code == 0, result.output
    assert deployed.read_text(encoding="utf-8") == _BODY

    redo = _run("revert", config, "--yes")

    assert redo.exit_code == 0, redo.output
    assert deployed.read_text(encoding="utf-8") == "second body\n"


_TYPO_DST = "{{ home }}/{{ home_typo }}/bad.txt"


def _write_two_profile_config(config_repo: ConfigRepo, *, with_typo: bool) -> Path:
    """Profile ``p`` uses a valid file; profile ``q`` uses the typo'd one."""
    config_repo.write_tracked("ok.txt", _BODY)
    config_repo.write_tracked("bad.txt", _BODY)
    typo = (
        f"  bad:\n    src: bad.txt\n    dst: {_TYPO_DST!r}\n    template: true\n"
        if with_typo
        else ""
    )
    q = "  q:\n    tracked_files: [bad]\n" if with_typo else ""
    config_repo.config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  good:\n    src: ok.txt\n    dst: '{{ home }}/out/ok.txt'\n"
        "    template: true\n"
        f"{typo}"
        "profiles:\n  p:\n    tracked_files: [good]\n"
        f"{q}",
        encoding="utf-8",
    )
    return config_repo.config


def _run_as(profile: str, verb: str, config: Path, *extra: str) -> Result:
    args = [verb, f"--profile={profile}", f"--config={config}", *extra]
    return CliRunner().invoke(app, args)


def test_typo_in_a_file_another_profile_uses_does_not_break_this_profile(
    config_repo: ConfigRepo,
) -> None:
    config = _write_two_profile_config(config_repo, with_typo=True)
    assert _run_as("p", "validate", config).exit_code == 0
    install_args = ("--no-secrets-scan", "--no-git-check", "--yes")
    assert _run_as("p", "install", config, *install_args).exit_code == 0

    compared = _run_as("p", "compare", config)
    cleaned = _run_as("p", "cleanup-orphans", config)

    assert compared.exit_code == 0, compared.output
    assert cleaned.exit_code == 0, cleaned.output


def test_scan_lists_the_same_files_when_another_profile_has_a_typo(
    config_repo: ConfigRepo,
) -> None:
    config = _write_two_profile_config(config_repo, with_typo=False)
    install_args = ("--no-secrets-scan", "--no-git-check", "--yes")
    assert _run_as("p", "install", config, *install_args).exit_code == 0
    stray = Path.home() / "out" / "stray.txt"
    stray.write_text("not recorded\n", encoding="utf-8")
    baseline = _run_as("p", "cleanup-orphans", config, "--scan")
    assert baseline.exit_code == 0, baseline.output
    assert str(stray) in baseline.output

    _write_two_profile_config(config_repo, with_typo=True)
    result = _run_as("p", "cleanup-orphans", config, "--scan")

    assert result.exit_code == 0, result.output
    listing = "=== unrecorded"
    assert result.output.split(listing)[1:] == baseline.output.split(listing)[1:]
    assert "profileqcouldnotberesolved" in "".join(result.output.split())


@pytest.mark.parametrize(
    ("verb", "extra"),
    [("compare", ()), ("cleanup-orphans", ()), ("cleanup-orphans", ("--scan",))],
    ids=["compare", "cleanup-orphans", "cleanup-orphans-scan"],
)
def test_typo_in_this_profiles_own_file_is_still_refused(
    config_repo: ConfigRepo, verb: str, extra: tuple[str, ...]
) -> None:
    config = _write_two_profile_config(config_repo, with_typo=True)

    result = _run_as("q", verb, config, *extra)

    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, ConfigError)
    assert "home_typo" in str(result.exception)


def test_ownership_revert_ignores_a_typo_in_an_unrelated_file(
    config_repo: ConfigRepo, init_git_repo: Callable[[Path], Path]
) -> None:
    """Re-granting a released claim only needs the file the claim names."""
    init_git_repo(config_repo.root)
    config = _write_two_profile_config(config_repo, with_typo=False)
    install_args = ("--no-secrets-scan", "--no-git-check", "--no-fetch", "--yes")
    assert _run_as("p", "install", config, *install_args).exit_code == 0
    claim_id = OwnershipStore().claim_id(
        file_resource_id(Path.home() / "out" / "ok.txt")
    )
    released = CliRunner().invoke(
        app, ["ownership", "release", claim_id, f"--config={config}", "--yes"]
    )
    assert released.exit_code == 0, released.output
    transition_id = released.output.split("released ownership transition ")[1].split()[
        0
    ]
    _write_two_profile_config(config_repo, with_typo=True)

    result = CliRunner().invoke(
        app, ["ownership", "revert", transition_id, f"--config={config}", "--yes"]
    )

    assert result.exit_code == 0, result.output


_INSTALL_ARGS = ("--no-secrets-scan", "--no-git-check", "--yes")
_Q_TYPO = "{{ hom }}/qdir/q-file.txt"
_QUIET_WARNING = "files it may manage are not listed"


def _write_q_file_config(
    config_repo: ConfigRepo,
    *,
    q_dst: str | None,
    kind: str = "bundle",
    plain_in_qdir: bool = False,
) -> Path:
    """Profile ``p`` owns ``~/out/ok.txt``; profile ``q`` deploys one more file.

    ``q_dst`` is that file's destination template (``None`` leaves ``q`` out
    entirely); ``kind`` makes it a bundle file component, a tracked file, or a
    tracked directory (or managed tree) holding ``inner.txt``.
    ``plain_in_qdir`` gives ``q`` an ordinary tracked file as well, so ``~/qdir``
    is a directory only ``q`` uses.
    """
    for name in ("ok.txt", "q-file.txt", "plain.txt", "q-tree/inner.txt"):
        config_repo.write_tracked(name, _BODY)
    src = "q-file.txt" if kind in ("bundle", "tracked") else "q-tree"
    tree = "    tree: {}\n" if kind == "tree" else ""
    plain = (
        "  plain:\n    src: plain.txt\n    dst: '{{ home }}/qdir/plain.txt'\n"
        "    template: true\n"
        if plain_in_qdir
        else ""
    )
    q_file = (
        f"  q-file:\n    src: {src}\n    dst: {q_dst!r}\n    template: true\n{tree}"
        if q_dst is not None and kind != "bundle"
        else ""
    )
    bundle = (
        "bundles:\n  tools:\n    components:\n      - id: q-file\n        file:\n"
        f"          src: q-file.txt\n          dst: {q_dst!r}\n"
        "          template: true\n"
        if q_dst is not None and kind == "bundle"
        else ""
    )
    q_tracked = [*(["plain"] if plain_in_qdir else []), *(["q-file"] if q_file else [])]
    q = (
        f"  q:\n    tracked_files: [{', '.join(q_tracked)}]\n"
        + ("    bundles: [tools]\n" if bundle else "")
        if q_dst is not None
        else ""
    )
    header = (
        'schema_version: "6.2"\nminimum_version: "6.2"\n'
        if kind == "tree"
        else "version: 1\n"
    )
    config_repo.config.write_text(
        f"{header}"
        "tracked_files:\n"
        "  good:\n    src: ok.txt\n    dst: '{{ home }}/out/ok.txt'\n"
        "    template: true\n"
        f"{plain}"
        f"{q_file}"
        f"{bundle}"
        "profiles:\n  p:\n    tracked_files: [good]\n"
        f"{q}",
        encoding="utf-8",
    )
    return config_repo.config


def _scan(profile: str, config: Path, *extra: str) -> Result:
    return _run_as(profile, "cleanup-orphans", config, "--scan", *extra)


def _flat(result: Result) -> str:
    """Output with line wraps and runs of spaces removed, for substring checks."""
    return "".join(result.output.split())


def test_scan_completes_when_another_profiles_bundle_file_destination_has_a_typo(
    config_repo: ConfigRepo,
) -> None:
    config = _write_q_file_config(config_repo, q_dst=None)
    assert _run_as("p", "install", config, *_INSTALL_ARGS).exit_code == 0
    stray = Path.home() / "out" / "stray.txt"
    stray.write_text("not recorded\n", encoding="utf-8")
    baseline = _scan("p", config)
    assert baseline.exit_code == 0, baseline.output
    assert str(stray) in _flat(baseline)

    _write_q_file_config(config_repo, q_dst=_Q_TYPO)
    result = _scan("p", config)

    assert result.exit_code == 0, result.output
    assert str(stray) in _flat(result)
    listing = "=== unrecorded"
    assert result.output.split(listing)[1:] == baseline.output.split(listing)[1:]
    warning = _flat(result)
    assert "profileqcouldnotberesolved" in warning
    assert "tools.q-file" in warning
    assert "hom" in warning
    assert _QUIET_WARNING.replace(" ", "") in warning
    assert stray.read_text(encoding="utf-8") == "not recorded\n"


def test_scan_reports_a_profile_that_inherits_the_misspelled_bundle_file(
    config_repo: ConfigRepo,
) -> None:
    config = _write_q_file_config(config_repo, q_dst=_Q_TYPO)
    with config.open("a", encoding="utf-8") as handle:
        handle.write("  r:\n    extends: q\n")

    result = _scan("p", config)

    assert result.exit_code == 0, result.output
    assert "profileqcouldnotberesolved" in _flat(result)
    assert "profilercouldnotberesolved" in _flat(result)


@pytest.mark.parametrize("kind", ["bundle", "tracked"])
def test_scan_does_not_offer_files_of_a_profile_it_could_not_resolve(
    config_repo: ConfigRepo, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    clean = _write_q_file_config(
        config_repo, kind=kind, q_dst="{{ home }}/qdir/q-file.txt", plain_in_qdir=True
    )
    for profile in ("p", "q"):
        assert _run_as(profile, "install", clean, *_INSTALL_ARGS).exit_code == 0
    deployed = Path.home() / "qdir" / "q-file.txt"
    q_stray = Path.home() / "qdir" / "stray.txt"
    p_stray = Path.home() / "out" / "stray.txt"
    q_stray.write_text("not recorded\n", encoding="utf-8")
    p_stray.write_text("not recorded\n", encoding="utf-8")
    baseline = _flat(_scan("p", clean))
    assert str(q_stray) in baseline
    assert str(p_stray) in baseline
    assert str(deployed) not in baseline

    config = _write_q_file_config(
        config_repo, kind=kind, q_dst=_Q_TYPO, plain_in_qdir=True
    )
    listed = _scan("p", config)

    assert listed.exit_code == 0, listed.output
    assert str(p_stray) in _flat(listed)
    assert str(q_stray) not in _flat(listed)
    assert str(deployed) not in _flat(listed)
    assert "profileqcouldnotberesolved" in _flat(listed)

    monkeypatch.setattr(
        orphans_mod, "_confirm_scan_entries", lambda entries, _console: entries
    )
    applied = _scan("p", config, "--apply")

    assert applied.exit_code == 0, applied.output
    assert "profileqcouldnotberesolved" in _flat(applied)
    assert not p_stray.exists()
    assert q_stray.exists()
    assert deployed.exists()


@pytest.mark.parametrize("kind", ["bundle", "tracked", "directory"])
def test_scan_recognises_a_file_by_the_destination_an_install_recorded(
    config_repo: ConfigRepo, kind: str
) -> None:
    """The lost file shares ``p``'s directory and was already in place at install."""
    clean = _write_q_file_config(
        config_repo, kind=kind, q_dst="{{ home }}/out/q-file.txt", plain_in_qdir=True
    )
    deployed = Path.home() / "out" / "q-file.txt"
    if kind == "directory":
        deployed = deployed / "inner.txt"
    deployed.parent.mkdir(parents=True)
    deployed.write_text(_BODY, encoding="utf-8")
    for profile in ("p", "q"):
        assert _run_as(profile, "install", clean, *_INSTALL_ARGS).exit_code == 0
    stray = Path.home() / "out" / "stray.txt"
    stray.write_text("not recorded\n", encoding="utf-8")

    config = _write_q_file_config(
        config_repo, kind=kind, q_dst=_Q_TYPO, plain_in_qdir=True
    )
    listed = _scan("p", config)

    assert listed.exit_code == 0, listed.output
    assert str(stray) in _flat(listed)
    assert str(deployed) not in _flat(listed)


@pytest.mark.parametrize("kind", ["bundle", "tracked"])
def test_scan_recognises_a_file_by_its_ownership_claim(
    config_repo: ConfigRepo, init_git_repo: Callable[[Path], Path], kind: str
) -> None:
    """The lost file shares ``p``'s directory and the transition log is gone."""
    init_git_repo(config_repo.root)
    clean = _write_q_file_config(
        config_repo, kind=kind, q_dst="{{ home }}/out/q-file.txt"
    )
    for profile in ("p", "q"):
        installed = _run_as(profile, "install", clean, *_INSTALL_ARGS, "--no-fetch")
        assert installed.exit_code == 0, installed.output
    deployed = Path.home() / "out" / "q-file.txt"
    stray = Path.home() / "out" / "stray.txt"
    stray.write_text("not recorded\n", encoding="utf-8")
    shutil.rmtree(transitions.transitions_root())

    config = _write_q_file_config(
        config_repo, kind=kind, q_dst="{{ hom }}/out/q-file.txt"
    )
    listed = _scan("p", config)

    assert listed.exit_code == 0, listed.output
    assert str(stray) in _flat(listed)
    assert str(deployed) not in _flat(listed)


@pytest.mark.parametrize("kind", ["bundle", "tracked"])
def test_scan_output_is_unchanged_by_a_sibling_file_that_renders(
    config_repo: ConfigRepo, kind: str
) -> None:
    config = _write_q_file_config(config_repo, q_dst=None)
    assert _run_as("p", "install", config, *_INSTALL_ARGS).exit_code == 0
    (Path.home() / "out" / "stray.txt").write_text("x\n", encoding="utf-8")
    baseline = _scan("p", config)
    assert baseline.exit_code == 0, baseline.output

    _write_q_file_config(config_repo, kind=kind, q_dst="{{ home }}/qdir/q-file.txt")
    result = _scan("p", config)

    assert result.exit_code == 0, result.output
    assert result.output == baseline.output


def test_bundle_file_typo_in_this_profiles_own_bundle_is_still_refused(
    config_repo: ConfigRepo,
) -> None:
    config = _write_q_file_config(config_repo, q_dst=_Q_TYPO)

    result = _scan("q", config)

    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, ConfigError)
    assert "hom" in str(result.exception)


def test_scan_leaves_a_managed_tree_alone_when_only_its_root_claim_remains(
    config_repo: ConfigRepo,
    init_git_repo: Callable[[Path], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lost tree sits inside ``p``'s directory and the transition log is gone."""
    init_git_repo(config_repo.root)
    clean = _write_q_file_config(config_repo, kind="tree", q_dst="{{ home }}/out/sub")
    for profile in ("p", "q"):
        installed = _run_as(profile, "install", clean, *_INSTALL_ARGS, "--no-fetch")
        assert installed.exit_code == 0, installed.output
    deployed = Path.home() / "out" / "sub" / "inner.txt"
    stray = Path.home() / "out" / "stray.txt"
    stray.write_text("not recorded\n", encoding="utf-8")
    shutil.rmtree(transitions.transitions_root())

    config = _write_q_file_config(config_repo, kind="tree", q_dst="{{ hom }}/out/sub")
    listed = _scan("p", config)

    assert listed.exit_code == 0, listed.output
    assert str(stray) in _flat(listed)
    assert str(deployed) not in _flat(listed)

    monkeypatch.setattr(
        orphans_mod, "_confirm_scan_entries", lambda entries, _console: entries
    )
    applied = _scan("p", config, "--apply")

    assert applied.exit_code == 0, applied.output
    assert not stray.exists()
    assert deployed.read_text(encoding="utf-8") == _BODY
