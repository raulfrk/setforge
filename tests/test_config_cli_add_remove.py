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
from ruamel.yaml import YAML
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


# The five host-local list keys: (path, entry already in local.yaml, next entry).
# Every entry is valid for ``list_overlay_config`` below, in both orders.
_LIST_KEYS = [
    pytest.param("plugins.add", "lint@team", "fmt@team", id="plugins.add"),
    pytest.param("plugins.remove", "review", "docs", id="plugins.remove"),
    pytest.param(
        "extensions.add", "ms-python.python", "rust-lang.rust-analyzer", id="ext.add"
    ),
    pytest.param(
        "extensions.remove", "redhat.vscode-yaml", "rust-lang.rust", id="ext.remove"
    ),
    pytest.param("marketplaces.remove", "team", "spare", id="marketplaces.remove"),
]


@pytest.fixture
def list_overlay_config(config_repo: ConfigRepo) -> Path:
    """Profile ``base``: plugins from two marketplaces and two extensions."""
    config_repo.write_tracked("d.txt", "x\n")
    github = {"source": "github", "repo": "owner/mp"}
    return config_repo.write_config(
        profile="base",
        tracked_files={"d": {"src": "d.txt", "dst": "~/.d"}},
        profile_extra={"packages": ["review", "docs", "yaml-ext", "rust-ext"]},
        extra={
            "marketplaces": {"team": github, "spare": github},
            "claude_plugins": {
                "review": {"marketplace": "team"},
                "docs": {"marketplace": "spare"},
            },
            "packages": {
                "review": {"type": "plugin", "plugin": "review"},
                "docs": {"type": "plugin", "plugin": "docs"},
                "yaml-ext": {"type": "extension", "extension": "redhat.vscode-yaml"},
                "rust-ext": {"type": "extension", "extension": "rust-lang.rust"},
            },
        },
    )


def _local_with_list(key: str, entries: list[str], *, as_scalar: bool = False) -> str:
    """A local.yaml carrying comments and ``key`` set to ``entries``.

    A marketplace can only be removed together with the plugins that use it, so
    the ``marketplaces.remove`` seed also removes both plugins.
    """
    block, leaf = key.split(".")
    head = "# keep this comment\nbinaries:\n  code: /usr/bin/code\n"
    if block == "marketplaces":
        head += "plugins:\n  remove: [review, docs]\n"
    if not entries:
        return head
    if as_scalar:
        body = f"  {leaf}: {entries[0]}\n"
    else:
        body = f"  # existing entries\n  {leaf}:\n" + "".join(
            f"    - {entry}\n" for entry in entries
        )
    return f"{head}{block}:\n{body}"


def _list_at(seed_local: Path, key: str) -> object:
    from setforge.migrations._yaml_ops import load_yaml_mapping

    block, leaf = key.split(".")
    return load_yaml_mapping(seed_local)[block][leaf]


# Not 0o600: the writer creates new files at 0o600, so only another mode shows
# that an existing file's permissions are kept.
_SEEDED_MODE = 0o640


def _write_local(seed_local: Path, text: str) -> None:
    seed_local.write_text(text, encoding="utf-8")
    seed_local.chmod(_SEEDED_MODE)


@pytest.mark.parametrize(("key", "existing", "new"), _LIST_KEYS)
def test_config_add_creates_the_list_entry(
    runner: CliRunner,
    seed_local: Path,
    list_overlay_config: Path,
    key: str,
    existing: str,
    new: str,
) -> None:
    """A local.yaml without the block gets ``<block>: {<leaf>: [value]}``.

    The loader reads these keys as lists; a scalar there fails ``validate``
    with "Input should be a valid list".
    """
    _write_local(seed_local, _local_with_list(key, []))

    result = runner.invoke(app, ["config", "add", "--local", key, new, "--yes"])

    assert result.exit_code == 0, result.output
    assert _list_at(seed_local, key) == [new]
    _assert_profile_still_resolves(runner, list_overlay_config)


@pytest.mark.parametrize(("key", "existing", "new"), _LIST_KEYS)
def test_config_add_appends_to_the_existing_list(
    runner: CliRunner,
    seed_local: Path,
    list_overlay_config: Path,
    key: str,
    existing: str,
    new: str,
) -> None:
    _write_local(seed_local, _local_with_list(key, [existing]))
    _assert_profile_still_resolves(runner, list_overlay_config)

    result = runner.invoke(app, ["config", "add", "--local", key, new, "--yes"])

    assert result.exit_code == 0, result.output
    assert _list_at(seed_local, key) == [existing, new]
    text = seed_local.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert "# existing entries" in text
    assert seed_local.stat().st_mode & 0o777 == _SEEDED_MODE
    _assert_profile_still_resolves(runner, list_overlay_config)


@pytest.mark.parametrize(("key", "existing", "new"), _LIST_KEYS)
def test_config_remove_drops_one_list_entry(
    runner: CliRunner,
    seed_local: Path,
    list_overlay_config: Path,
    key: str,
    existing: str,
    new: str,
) -> None:
    _write_local(seed_local, _local_with_list(key, [existing, new]))
    _assert_profile_still_resolves(runner, list_overlay_config)

    result = runner.invoke(app, ["config", "remove", "--local", key, new, "--yes"])

    assert result.exit_code == 0, result.output
    assert _list_at(seed_local, key) == [existing]
    text = seed_local.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert "# existing entries" in text
    assert seed_local.stat().st_mode & 0o777 == _SEEDED_MODE
    _assert_profile_still_resolves(runner, list_overlay_config)


@pytest.mark.parametrize(("key", "existing", "new"), _LIST_KEYS)
@pytest.mark.parametrize(
    ("verb", "entry", "message"),
    [
        ("add", "existing", "already contains"),
        ("remove", "new", "not in"),
    ],
    ids=["add-duplicate", "remove-absent"],
)
def test_config_list_edit_refused_leaves_local_yaml_unchanged(
    runner: CliRunner,
    seed_local: Path,
    key: str,
    existing: str,
    new: str,
    verb: str,
    entry: str,
    message: str,
) -> None:
    _write_local(seed_local, _local_with_list(key, [existing]))
    before = seed_local.read_bytes()

    value = {"existing": existing, "new": new}[entry]
    result = runner.invoke(app, ["config", verb, "--local", key, value, "--yes"])

    assert result.exit_code != 0
    assert message in str(result.exception)
    assert seed_local.read_bytes() == before


@pytest.mark.parametrize(("key", "existing", "new"), _LIST_KEYS)
@pytest.mark.parametrize("verb", ["add", "remove"])
def test_config_list_edit_refuses_a_scalar_left_by_hand_or_by_older_versions(
    runner: CliRunner,
    seed_local: Path,
    key: str,
    existing: str,
    new: str,
    verb: str,
) -> None:
    """A scalar under a list key is reported, not appended to or rewritten."""
    _write_local(seed_local, _local_with_list(key, [existing], as_scalar=True))
    before = seed_local.read_bytes()

    result = runner.invoke(app, ["config", verb, "--local", key, existing, "--yes"])

    assert result.exit_code != 0
    assert "is a scalar, not a list" in str(result.exception)
    assert seed_local.read_bytes() == before


@pytest.mark.parametrize(("key", "existing", "new"), _LIST_KEYS)
def test_config_remove_list_key_without_an_entry_is_refused(
    runner: CliRunner, seed_local: Path, key: str, existing: str, new: str
) -> None:
    """``remove`` takes one entry at a time; with none it refuses, not unsets."""
    _write_local(seed_local, _local_with_list(key, [existing]))
    before = seed_local.read_bytes()

    result = runner.invoke(app, ["config", "remove", "--local", key, "--yes"])

    assert result.exit_code != 0
    assert "requires <value>" in str(result.exception)
    assert seed_local.read_bytes() == before


def test_config_add_plugin_refuses_an_option_shaped_name(
    runner: CliRunner, seed_local: Path
) -> None:
    """The overlay schema rejects ``-x@team``; nothing is written."""
    _write_local(seed_local, _local_with_list("plugins.add", ["lint@team"]))
    before = seed_local.read_bytes()

    argv = ["config", "add", "--local", "plugins.add", "--yes", "--", "-bad@team"]
    result = runner.invoke(app, argv)

    assert result.exit_code != 0
    assert "must not begin with '-'" in str(result.exception)
    assert seed_local.read_bytes() == before


@pytest.mark.parametrize(
    "path", ["plugins.typo", "extensions.ad", "marketplaces.removes"]
)
def test_config_add_refuses_an_unknown_overlay_key(
    runner: CliRunner, seed_local: Path, path: str
) -> None:
    """A misspelled key under an overlay block is refused, not written."""
    before = seed_local.read_bytes()

    result = runner.invoke(app, ["config", "add", "--local", path, "x", "--yes"])

    assert result.exit_code != 0
    assert "unknown path" in str(result.exception)
    assert seed_local.read_bytes() == before


# Lists indented differently from one another in the same file. The renderer
# keeps the lines an edit did not touch and writes the new entry in the first
# list's style, so an append to the other list lands at the wrong indentation.
_MIXED_INDENT = [
    pytest.param(
        "plugins:\n  add:\n    - a\nextensions:\n  remove:\n  - e.two\n",
        "  - pub.ext\n",
        id="indented-then-flush",
    ),
    pytest.param(
        "plugins:\n  add:\n  - a\nextensions:\n  remove:\n    - e.two\n",
        "    - pub.ext\n",
        id="flush-then-indented",
    ),
]


@pytest.mark.parametrize(("seed", "added"), _MIXED_INDENT)
def test_config_add_indents_the_entry_like_the_list_it_joins(
    runner: CliRunner, seed_local: Path, seed: str, added: str
) -> None:
    """Lists indented differently in one file each keep their own indentation.

    The new entry used to be written in the file's first list style: glued onto
    the entry above as ``['e.two - pub.ext']`` in the first layout, YAML that
    does not parse in the second.
    """
    _write_local(seed_local, seed)

    argv = ["config", "add", "--local", "extensions.remove", "pub.ext", "--yes"]
    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert seed_local.read_text(encoding="utf-8") == seed + added
    assert _list_at(seed_local, "extensions.remove") == ["e.two", "pub.ext"]
    assert _list_at(seed_local, "plugins.add") == ["a"]


@pytest.mark.parametrize(("seed", "added"), _MIXED_INDENT)
def test_config_add_to_the_first_list_of_a_mixed_indent_file(
    runner: CliRunner, seed_local: Path, seed: str, added: str
) -> None:
    del added
    _write_local(seed_local, seed)

    result = runner.invoke(
        app, ["config", "add", "--local", "plugins.add", "pub@team", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert _list_at(seed_local, "plugins.add") == ["a", "pub@team"]
    assert _list_at(seed_local, "extensions.remove") == ["e.two"]


def test_config_add_refuses_a_write_that_would_not_read_back_as_the_edit(
    runner: CliRunner, seed_local: Path
) -> None:
    """A value YAML cannot hold as written is refused, not written changed.

    U+0085 is a line break to YAML: the value would read back as ``/opt/a b``.
    """
    _write_local(seed_local, "binaries:\n  code: /usr/bin/code\n")
    before = seed_local.read_bytes()

    argv = ["config", "add", "--local", "binaries.tool", "/opt/a\x85b", "--yes"]
    result = runner.invoke(app, argv)

    assert result.exit_code != 0
    message = str(result.exception)
    assert "would not read back" in message
    assert "nothing was written" in message
    assert "Edit the file by hand" in message
    assert seed_local.read_bytes() == before


# An editor may leave out the final newline. The renderer used to join the new
# line onto the last one, so every add to such a file was refused; it now reads
# the file as newline-terminated and the written file gains the newline.
_NO_FINAL_NEWLINE = [
    pytest.param(
        "# keep this comment\nprovision_ignore:\n  - p",
        ["provision_ignore", "q"],
        "# keep this comment\nprovision_ignore:\n  - p\n  - q\n",
        {"provision_ignore": ["p", "q"]},
        id="add-entry",
    ),
    pytest.param(
        "# keep this comment\nprovision_ignore:\n  - p",
        ["binaries.code", "/usr/bin/code"],
        "# keep this comment\nprovision_ignore:\n  - p\n"
        "binaries:\n  code: /usr/bin/code\n",
        {"provision_ignore": ["p"], "binaries": {"code": "/usr/bin/code"}},
        id="add-key",
    ),
    pytest.param(
        "provision_ignore:\r\n  - p",
        ["provision_ignore", "q"],
        "provision_ignore:\r\n  - p\r\n  - q\r\n",
        {"provision_ignore": ["p", "q"]},
        id="crlf",
    ),
]


@pytest.mark.parametrize(
    ("seed", "args", "expected_text", "expected"), _NO_FINAL_NEWLINE
)
def test_config_add_writes_a_file_that_has_no_final_newline(
    runner: CliRunner,
    seed_local: Path,
    seed: str,
    args: list[str],
    expected_text: str,
    expected: dict[str, object],
) -> None:
    """An add to a file without a final newline lands, comments kept."""
    seed_local.write_bytes(seed.encode())
    seed_local.chmod(_SEEDED_MODE)

    result = runner.invoke(app, ["config", "add", "--local", *args, "--yes"])

    assert result.exit_code == 0, result.output
    written = seed_local.read_bytes().decode()
    assert written == expected_text
    assert YAML(typ="safe").load(written) == expected


def test_config_remove_refuses_when_a_blank_line_follows_the_parent_key(
    runner: CliRunner, seed_local: Path
) -> None:
    """The renderer would misplace this edit; it is refused, not written."""
    _write_local(seed_local, "binaries:\n\n  code: /usr/bin/code\n")
    before = seed_local.read_bytes()

    argv = ["config", "remove", "--local", "binaries.code", "--yes"]
    result = runner.invoke(app, argv)

    assert result.exit_code != 0
    assert "would not read back" in str(result.exception)
    assert seed_local.read_bytes() == before


_AFTER = "# why extensions\nextensions:\n  remove:\n    - redhat.vscode-yaml\n"


@pytest.mark.parametrize(
    ("seed", "entry", "expected"),
    [
        pytest.param(
            "plugins:\n  add:\n    - lint@team\n    - fmt@team\n",
            "fmt@team",
            "plugins:\n  add:\n    - lint@team\n",
            id="last-of-two",
        ),
        pytest.param(
            "plugins:\n  add:\n    - lint@team\n",
            "lint@team",
            "plugins:\n  add: []\n",
            id="only-entry",
        ),
        pytest.param(
            "plugins:\n  add:\n    - lint@team\n    - fmt@team\n    - docs@team\n"
            "    # about docs\n",
            "docs@team",
            "plugins:\n  add:\n    - lint@team\n    - fmt@team\n    # about docs\n",
            id="indented-comment-after-last",
        ),
        pytest.param(
            "plugins:\n  add:\n    - lint@team\n    # about fmt\n    - fmt@team\n"
            "    - docs@team\n",
            "lint@team",
            "plugins:\n  add:\n    # about fmt\n    - fmt@team\n    - docs@team\n",
            id="first-of-three",
        ),
        pytest.param(
            "plugins:\n  add:\n    - lint@team\n    - fmt@team\n    # about docs\n"
            "    - docs@team\n",
            "fmt@team",
            "plugins:\n  add:\n    - lint@team\n    # about docs\n    - docs@team\n",
            id="middle-of-three",
        ),
        pytest.param(
            "plugins:\n  add:\n    - lint@team\n    - fmt@team  # own note\n",
            "fmt@team",
            "plugins:\n  add:\n    - lint@team\n",
            id="own-inline-comment-goes-with-the-entry",
        ),
    ],
)
def test_config_remove_keeps_the_comments_that_follow_the_entry(
    runner: CliRunner, seed_local: Path, seed: str, entry: str, expected: str
) -> None:
    """Comments after the removed entry belong to what follows it; keep them.

    The one on the entry's own line goes with it. A comment above the next key
    used to vanish when the last entry of the list before it was removed.
    """
    _write_local(seed_local, seed + _AFTER)

    argv = ["config", "remove", "--local", "plugins.add", entry, "--yes"]
    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert seed_local.read_text(encoding="utf-8") == expected + _AFTER
