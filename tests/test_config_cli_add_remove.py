"""Unit tests for ``setforge config add`` / ``setforge config remove``.

Covers:
- list-vs-scalar dispatch via Pydantic ``model_fields`` introspection,
- ``--local`` / ``--tracked`` mutex,
- ``--yes`` short-circuit (mutate-gate posture),
- round-trip preservation (comments / key-order kept by ruamel.yaml rt).

See the config SPEC 4 acceptance for the enumerated cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.errors import SetforgeError
from tests.conftest import redirect_local_config_path
from tests.shared_fixtures import ConfigRepo


@pytest.fixture
def seed_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed a local.yaml with binaries + a comment for round-trip checks."""
    local = tmp_path / "local.yaml"
    local.write_text(
        "# comment-A\nbinaries:\n  code: /usr/bin/code\n",
        encoding="utf-8",
    )
    return local


@pytest.fixture
def seed_tracked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed a tracked setforge.yaml and bypass the git-clean check."""
    tracked = tmp_path / "tracked" / "setforge.yaml"
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text(
        "version: 1\n"
        "schema_version: '1.0'\n"
        "tracked_files:\n"
        "  foo:\n"
        "    src: foo.md\n"
        "    dst: foo.md\n"
        "profiles:\n"
        "  base:\n"
        "    tracked_files:\n"
        "      - foo\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("setforge.cli.config._tracked_yaml_path", lambda: tracked)
    monkeypatch.setattr(
        "setforge.cli.config._run_tracked_git_check", lambda yaml_path: None
    )
    return tracked


@pytest.mark.parametrize("value", ["/usr/local/bin/code", "true", "0o755"])
def test_add_local_scalar_with_yes(
    runner: CliRunner, seed_local: Path, value: str
) -> None:
    """``add --local binaries.code /usr/local/bin/code --yes`` rewrites scalar."""
    result = runner.invoke(
        app,
        ["config", "add", "--local", "binaries.code", value, "--yes"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    text = seed_local.read_text(encoding="utf-8")
    from setforge.migrations._yaml_ops import load_yaml_mapping

    assert load_yaml_mapping(seed_local)["binaries"]["code"] == value
    # Round-trip preserved the leading comment.
    assert "# comment-A" in text


@pytest.mark.parametrize("value", ["0o755", "0o640", "0o1755", "0o000"])
def test_add_tracked_octal_mode_round_trips(
    runner: CliRunner, seed_tracked: Path, value: str
) -> None:
    from ruamel.yaml.scalarint import OctalInt

    from setforge.config import load_config
    from setforge.migrations._yaml_ops import load_yaml_mapping

    result = runner.invoke(
        app, ["config", "add", "--tracked", "tracked_files.foo.mode", value, "--yes"]
    )
    assert result.exit_code == 0, result.output
    mode = load_yaml_mapping(seed_tracked)["tracked_files"]["foo"]["mode"]
    assert isinstance(mode, OctalInt)
    assert load_config(seed_tracked).tracked_files["foo"].mode == int(value, 8)


@pytest.mark.parametrize(
    "value",
    [
        "755",
        "0755",
        "0o4755",
        "0o2755",
        "0o10000",
        "0o888",
        "true",
        "null",
        '"0o755"',
        "[invalid",
    ],
)
def test_add_invalid_mode_leaves_config_unchanged(
    runner: CliRunner, seed_tracked: Path, value: str
) -> None:
    before = seed_tracked.read_bytes()
    result = runner.invoke(
        app, ["config", "add", "--tracked", "tracked_files.foo.mode", value, "--yes"]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, SetforgeError)
    assert "mode" in str(result.exception)
    assert seed_tracked.read_bytes() == before


def test_add_local_unknown_path_errors(runner: CliRunner, seed_local: Path) -> None:
    """Adding to an unknown dotted-path surfaces a SetforgeError."""
    result = runner.invoke(
        app, ["config", "add", "--local", "bogus.field", "val", "--yes"]
    )
    assert result.exit_code != 0


def test_add_rejects_both_local_and_tracked(runner: CliRunner) -> None:
    """``--local`` + ``--tracked`` raises typer.BadParameter."""
    result = runner.invoke(
        app, ["config", "add", "--local", "--tracked", "binaries.code", "/x", "--yes"]
    )
    assert result.exit_code != 0


def test_add_requires_scope_flag(runner: CliRunner) -> None:
    """No scope flag → typer.BadParameter, non-zero exit."""
    result = runner.invoke(app, ["config", "add", "binaries.code", "/x", "--yes"])
    assert result.exit_code != 0


def test_remove_local_scalar_with_yes(runner: CliRunner, seed_local: Path) -> None:
    """``remove --local binaries.code --yes`` unsets the scalar."""
    result = runner.invoke(
        app, ["config", "remove", "--local", "binaries.code", "--yes"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    text = seed_local.read_text(encoding="utf-8")
    assert "code:" not in text


def test_remove_unknown_path_errors(runner: CliRunner, seed_local: Path) -> None:
    """Removing an absent path errors out cleanly."""
    result = runner.invoke(
        app, ["config", "remove", "--local", "binaries.nope", "--yes"]
    )
    assert result.exit_code != 0


def test_add_tracked_appends_to_profile_list(
    runner: CliRunner, seed_tracked: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding to a profile's ``tracked_files`` list works (existing key)."""
    result = runner.invoke(
        app,
        [
            "config",
            "add",
            "--tracked",
            "profiles.base.tracked_files",
            "foo",  # already in list — should error (duplicate)
            "--profile=base",
            "--yes",
        ],
    )
    # 'foo' is already present → SetforgeError "already contains".
    assert result.exit_code != 0


def test_add_tracked_profile_required_for_profile_paths(
    runner: CliRunner, seed_tracked: Path
) -> None:
    """``profiles.*`` paths require ``--profile=NAME``."""
    result = runner.invoke(
        app,
        [
            "config",
            "add",
            "--tracked",
            "profiles.base.tracked_files",
            "bar",
            "--yes",
        ],
    )
    assert result.exit_code != 0


@pytest.mark.parametrize("operation", ["add", "remove"])
@pytest.mark.parametrize("profile", ["base", "other"])
def test_profile_argument_must_match_mutated_path(
    runner: CliRunner, seed_tracked: Path, operation: str, profile: str
) -> None:
    if operation == "add":
        seed_tracked.write_text(
            seed_tracked.read_text().replace("      - foo\n", "      []\n")
        )
    before = seed_tracked.read_bytes()
    result = runner.invoke(
        app,
        [
            "config",
            operation,
            "--tracked",
            "profiles.base.tracked_files",
            "foo",
            f"--profile={profile}",
            "--yes",
        ],
    )
    if profile == "other":
        assert result.exit_code != 0
        assert "does not match" in result.output
        assert seed_tracked.read_bytes() == before
    else:
        assert result.exit_code == 0, result.output
        assert seed_tracked.read_bytes() != before


def test_add_tracked_profile_rejected_for_top_level_paths(
    runner: CliRunner, seed_tracked: Path
) -> None:
    """Top-level paths reject ``--profile``."""
    result = runner.invoke(
        app,
        [
            "config",
            "add",
            "--tracked",
            "schema_version",
            "1.1",
            "--profile=base",
            "--yes",
        ],
    )
    assert result.exit_code != 0


def test_remove_local_missing_yaml_exits_clean_without_creating(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``remove --local`` on a missing local.yaml exits 0 with a clean message.

    The guard must report nothing-to-remove and NOT materialize the file —
    a no-op remove must leave no stray artifact behind.
    """
    # Point the config command's imported path at a distinct absent location
    # so this test pins its no-op remove boundary directly.
    missing = tmp_path / "absent" / "local.yaml"
    assert not missing.exists()
    redirect_local_config_path(monkeypatch, missing)

    result = runner.invoke(
        app, ["config", "remove", "--local", "binaries.code", "--yes"]
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "nothing to remove" in result.stdout
    # The remove path must not create the file.
    assert not missing.exists()


@pytest.fixture
def git_check_calls(seed_tracked: Path, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    calls: list[bool] = []

    def record(yaml_path: Path) -> None:
        calls.append(True)

    monkeypatch.setattr("setforge.cli.config._run_tracked_git_check", record)
    return calls


def test_tracked_edit_accepts_no_git_check(
    runner: CliRunner, seed_tracked: Path, git_check_calls: list[bool]
) -> None:
    steps = [
        (["add", "--no-git-check"], "0o644", 0),
        (["remove", "--no-git-check"], None, 0),
        (["add"], "0o644", 1),
        (["remove"], None, 2),
    ]
    for verb_args, value, expected_checks in steps:
        argv = ["config", *verb_args, "--tracked", "tracked_files.foo.mode"]
        argv += [value] if value else []
        result = runner.invoke(app, [*argv, "--yes"])
        assert result.exit_code == 0, result.output
        assert len(git_check_calls) == expected_checks


@pytest.mark.parametrize(
    ("parent", "message"),
    [("base", "profile cycle"), ("ghost", "profile not found")],
)
def test_add_tracked_rejects_unresolvable_extends(
    runner: CliRunner, seed_tracked: Path, parent: str, message: str
) -> None:
    before = seed_tracked.read_bytes()

    argv = ["config", "add", "--tracked", "-p", "base", "profiles.base.extends"]
    result = runner.invoke(app, [*argv, parent, "--yes"])

    assert result.exit_code != 0
    assert message in str(result.exception)
    assert seed_tracked.read_bytes() == before


_LOCAL_WITH_OVERLAY = """\
# keep this comment
binaries:
  code: /usr/bin/code
marketplaces:
  # existing additions
  add:
    old-mp:
      source: github
      repo: owner/old
  remove:
    - team
plugins:
  remove:
    - review
"""


@pytest.fixture
def overlay_config(config_repo: ConfigRepo) -> Path:
    """A valid setforge.yaml: profile ``base`` has plugin ``review`` from ``team``."""
    config_repo.write_tracked("d.txt", "x\n")
    return config_repo.write_config(
        profile="base",
        tracked_files={"d": {"src": "d.txt", "dst": "~/.d"}},
        profile_extra={"packages": ["review"]},
        extra={
            "marketplaces": {"team": {"source": "github", "repo": "owner/team"}},
            "claude_plugins": {"review": {"marketplace": "team"}},
            "packages": {"review": {"type": "plugin", "plugin": "review"}},
        },
    )


def _assert_profile_still_resolves(runner: CliRunner, cfg: Path) -> None:
    for argv in (["validate", "--profile=base"], ["profile", "show", "base"]):
        result = runner.invoke(app, [*argv, f"--config={cfg}"])
        assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("source", "location", "entry"),
    [
        ("github", "owner/new", {"source": "github", "repo": "owner/new"}),
        ("path", "/srv/new-mp", {"source": "path", "path": "/srv/new-mp"}),
    ],
)
def test_add_marketplace_lands_under_the_overlay_add_block(
    runner: CliRunner,
    seed_local: Path,
    overlay_config: Path,
    source: str,
    location: str,
    entry: dict[str, str],
) -> None:
    """The new marketplace goes under ``marketplaces.add`` beside what is there.

    The loader reads ``marketplaces.add.<name>``; a sibling of ``add`` and
    ``remove`` is a schema error that blocks every profile command.
    """
    from setforge.migrations._yaml_ops import load_yaml_mapping

    seed_local.write_text(_LOCAL_WITH_OVERLAY, encoding="utf-8")
    seed_local.chmod(0o600)

    argv = ["config", "add", "--local", "marketplaces.add", "new-mp"]
    result = runner.invoke(
        app, [*argv, "--source", source, "--repo", location, "--yes"]
    )

    assert result.exit_code == 0, result.output
    overlay = load_yaml_mapping(seed_local)["marketplaces"]
    assert overlay == {
        "add": {"old-mp": {"source": "github", "repo": "owner/old"}, "new-mp": entry},
        "remove": ["team"],
    }
    text = seed_local.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert "# existing additions" in text
    assert seed_local.stat().st_mode & 0o777 == 0o600
    _assert_profile_still_resolves(runner, overlay_config)


def test_add_marketplace_creates_the_overlay_blocks(
    runner: CliRunner, seed_local: Path, overlay_config: Path
) -> None:
    """A local.yaml with no ``marketplaces`` block gets ``marketplaces.add.<name>``."""
    from setforge.migrations._yaml_ops import load_yaml_mapping

    argv = ["config", "add", "--local", "marketplaces.add", "new-mp"]
    result = runner.invoke(
        app, [*argv, "--source", "github", "--repo", "owner/new", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert load_yaml_mapping(seed_local)["marketplaces"] == {
        "add": {"new-mp": {"source": "github", "repo": "owner/new"}}
    }
    _assert_profile_still_resolves(runner, overlay_config)


@pytest.mark.parametrize(
    ("name", "message"),
    [("old-mp", "already exists"), ("-bad", "must not begin with '-'")],
    ids=["duplicate", "option-shaped-name"],
)
def test_add_marketplace_refused_leaves_local_yaml_unchanged(
    runner: CliRunner, seed_local: Path, name: str, message: str
) -> None:
    seed_local.write_text(_LOCAL_WITH_OVERLAY, encoding="utf-8")
    before = seed_local.read_bytes()

    argv = ["config", "add", "--local", "marketplaces.add", "--source", "github"]
    result = runner.invoke(app, [*argv, "--repo", "owner/new", "--yes", "--", name])

    assert result.exit_code != 0
    assert message in str(result.exception)
    assert seed_local.read_bytes() == before
