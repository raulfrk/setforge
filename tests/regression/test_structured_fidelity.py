"""JSON, JSONC, YAML and YML tracked files keep their exact bytes.

Whatever the formatting (comments, key order, indentation, CRLF, BOM, a missing
final newline, duplicate keys, anchors, array roots), a file must come out of
install, sync and capture byte-identical wherever nothing was edited, edits
made on both sides must both survive, and a file that does not parse must be
refused or left alone, never rewritten."""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

import pytest

from tests.integration.conftest import IntegrationEnv

pytestmark = pytest.mark.integration

_INSTALL = ["install", "--yes", "--no-git-check", "--no-secrets-scan"]


class Case(NamedTuple):
    filename: str
    base: bytes
    host_old: bytes
    host_new: bytes
    tracked_old: bytes
    tracked_new: bytes


_CASES: dict[str, Case] = {
    "json-crlf-bom-4-space": Case(
        "a.json",
        b'\xef\xbb\xbf{\r\n    "a": 1,\r\n    "b": 2,\r\n    "c": [1, 2]\r\n}',
        b'"a": 1',
        b'"a": 9',
        b'"c": [1, 2]',
        b'"c": [1, 3]',
    ),
    "jsonc-comments-trailing-comma": Case(
        "a.json",
        b'{\n  // keep me\n  "a": 1, /* inline */\n  "b": 2,\n  "c": 3,\n}\n',
        b'"a": 1',
        b'"a": 9',
        b'"c": 3',
        b'"c": 4',
    ),
    "json-array-root": Case(
        "a.json",
        b'[\n  {"id": 1},\n  {"id": 2},\n  {"id": 3}\n]\n',
        b'"id": 1',
        b'"id": 10',
        b'"id": 3',
        b'"id": 30',
    ),
    "json-tabs-no-final-newline": Case(
        "a.json",
        b'{\n\t"z": 1,\n\t"a": 2,\n\t"m": {"k": [1,2,3]}\n}',
        b'"z": 1',
        b'"z": 5',
        b"[1,2,3]",
        b"[1,2,4]",
    ),
    "yaml-anchor-merge-key": Case(
        "a.yaml",
        b"# top\nbase: &b\n  x: 1\n  y: 2\nuse:\n  <<: *b\n  z: 3\nlast: 4\nmore: 5\n",
        b"x: 1",
        b"x: 7",
        b"more: 5",
        b"more: 8",
    ),
    "yml-crlf-no-final-newline": Case(
        "a.yml",
        b"a: 1\r\nb: 2\r\n# c\r\nd: 3",
        b"a: 1",
        b"a: 9",
        b"d: 3",
        b"d: 4",
    ),
    "yaml-bom": Case(
        "a.yaml",
        b"\xef\xbb\xbfa: 1\nb: 2\nc: 3\n",
        b"a: 1",
        b"a: 9",
        b"c: 3",
        b"c: 4",
    ),
    "yaml-key-order-odd-indent": Case(
        "a.yaml",
        b"z: 1\na: 2\nm:\n    deep:   [1,2]\n    k: v\n",
        b"z: 1",
        b"z: 9",
        b"k: v",
        b"k: w",
    ),
    "yaml-multi-document": Case(
        "a.yaml",
        b"---\nkind: A\ndata: 1\n---\nkind: B\ndata: 2\n",
        b"data: 1",
        b"data: 7",
        b"data: 2",
        b"data: 3",
    ),
}

_DUPLICATE_KEYS = Case(
    "a.json",
    b'{\n  "a": 1,\n  "a": 2,\n  "b": 3,\n  "c": 4\n}\n',
    b'"b": 3',
    b'"b": 5',
    b'"c": 4',
    b'"c": 6',
)

_NON_UTF8: dict[str, Case] = {
    "yaml-latin1-comment": Case(
        "a.yaml",
        b"# caf\xe9\na: 1\nb: 2\nc: 3\n",
        b"a: 1",
        b"a: 9",
        b"c: 3",
        b"c: 4",
    ),
    "json-latin1-string": Case(
        "a.json",
        b'{\n  "n": "caf\xe9",\n  "a": 1,\n  "b": 2\n}\n',
        b'"a": 1',
        b'"a": 9',
        b'"b": 2',
        b'"b": 3',
    ),
}

_ALL = {**_CASES, "json-duplicate-keys": _DUPLICATE_KEYS, **_NON_UTF8}

Factory = Callable[..., IntegrationEnv]


def _setup(factory: Factory, case: Case) -> tuple[IntegrationEnv, str]:
    env = factory(tracked={"f": (case.filename, "seed\n")})
    env.tracked(case.filename).write_bytes(case.base)
    installed = env.run_verb(_INSTALL)
    assert installed.exit_code == 0, installed.output
    return env, f".setforge_it/{case.filename}"


def _host_edit(case: Case) -> bytes:
    return case.base.replace(case.host_old, case.host_new, 1)


def _tracked_edit(case: Case) -> bytes:
    return case.base.replace(case.tracked_old, case.tracked_new, 1)


@pytest.mark.parametrize("name", sorted(_ALL))
def test_install_is_byte_exact_and_a_second_install_changes_nothing(
    name: str, integration_env: Factory, integration_subprocess
) -> None:
    case = _ALL[name]
    env, rel = _setup(integration_env, case)
    assert env.live(rel).read_bytes() == case.base

    again = env.run_verb(_INSTALL)

    assert again.exit_code == 0, again.output
    assert env.live(rel).read_bytes() == case.base
    assert env.tracked(case.filename).read_bytes() == case.base
    assert sorted(p.name for p in env.live(rel).parent.iterdir()) == [case.filename]
    assert env.run_verb(["compare", "--check"]).exit_code == 0
    assert env.run_verb(["stage", "--list"]).exit_code == 0


@pytest.mark.parametrize("name", sorted(_ALL))
def test_tracked_only_change_reaches_live_byte_for_byte(
    name: str, integration_env: Factory, integration_subprocess
) -> None:
    case = _ALL[name]
    env, rel = _setup(integration_env, case)
    env.tracked(case.filename).write_bytes(_tracked_edit(case))

    result = env.run_verb(_INSTALL)

    assert result.exit_code == 0, result.output
    assert env.live(rel).read_bytes() == _tracked_edit(case)
    assert env.run_verb(["compare", "--check"]).exit_code == 0


@pytest.mark.parametrize("name", sorted(_CASES))
def test_host_only_edit_survives_install_untouched(
    name: str, integration_env: Factory, integration_subprocess
) -> None:
    case = _CASES[name]
    env, rel = _setup(integration_env, case)
    env.live(rel).write_bytes(_host_edit(case))

    result = env.run_verb(_INSTALL)

    assert result.exit_code == 0, result.output
    assert env.live(rel).read_bytes() == _host_edit(case)
    assert env.tracked(case.filename).read_bytes() == case.base


@pytest.mark.parametrize("name", sorted(_CASES))
def test_edits_made_on_both_sides_merge_without_losing_either(
    name: str, integration_env: Factory, integration_subprocess
) -> None:
    case = _CASES[name]
    env, rel = _setup(integration_env, case)
    env.live(rel).write_bytes(_host_edit(case))
    env.tracked(case.filename).write_bytes(_tracked_edit(case))
    both = _host_edit(case).replace(case.tracked_old, case.tracked_new, 1)

    result = env.run_verb(_INSTALL)

    assert result.exit_code == 0, result.output
    assert env.live(rel).read_bytes() == both
    assert env.run_verb(_INSTALL).exit_code == 0
    assert env.live(rel).read_bytes() == both


@pytest.mark.parametrize("verb", ["sync"])
@pytest.mark.parametrize("name", sorted({**_CASES, "dup": _DUPLICATE_KEYS}))
def test_sharing_a_host_edit_changes_only_the_edited_bytes(
    name: str, verb: str, integration_env: Factory, integration_subprocess
) -> None:
    case = {**_CASES, "dup": _DUPLICATE_KEYS}[name]
    env, rel = _setup(integration_env, case)
    env.live(rel).write_bytes(_host_edit(case))

    result = env.run_verb([verb, "--auto=use-live", "--yes"])

    assert result.exit_code == 0, result.output
    assert env.tracked(case.filename).read_bytes() == _host_edit(case)
    assert env.live(rel).read_bytes() == _host_edit(case)


@pytest.mark.parametrize("name", sorted(_NON_UTF8))
def test_non_utf8_files_never_lose_host_or_tracked_bytes(
    name: str, integration_env: Factory, integration_subprocess
) -> None:
    case = _NON_UTF8[name]
    env, rel = _setup(integration_env, case)
    live = env.live(rel)

    live.write_bytes(_host_edit(case))
    shared = env.run_verb(["sync", "--auto=use-live", "--yes"])
    assert shared.exit_code == 0, shared.output
    assert env.tracked(case.filename).read_bytes() == _host_edit(case)

    env.tracked(case.filename).write_bytes(_tracked_edit(case))
    live.write_bytes(_host_edit(case))
    result = env.run_verb(_INSTALL)
    assert result.exit_code == 0, result.output
    held = {p.read_bytes() for p in live.parent.iterdir()}
    assert _host_edit(case) in held
    assert _tracked_edit(case) in held


def test_duplicate_key_conflict_keeps_both_sides_untouched(
    integration_env: Factory, integration_subprocess
) -> None:
    case = _DUPLICATE_KEYS
    env, rel = _setup(integration_env, case)
    env.live(rel).write_bytes(_host_edit(case))
    env.tracked(case.filename).write_bytes(_tracked_edit(case))

    result = env.run_verb(_INSTALL)

    assert result.exit_code == 1, result.output
    assert env.live(rel).read_bytes() == _host_edit(case)
    assert env.tracked(case.filename).read_bytes() == _tracked_edit(case)


_BROKEN = {
    "json": ("a.json", b'{\n  "a": 1,\n  "b": 2\n}\n', b'{\n  "a": 1,\n  "b": \n'),
    "yaml": ("a.yaml", b"a: 1\nb:\n  - x\n", b"a: [1\nb: : :\n"),
}


@pytest.mark.parametrize("verb", ["sync"])
@pytest.mark.parametrize("kind", sorted(_BROKEN))
def test_a_live_file_that_no_longer_parses_is_never_promoted(
    kind: str, verb: str, integration_env: Factory, integration_subprocess
) -> None:
    filename, base, broken = _BROKEN[kind]
    env = integration_env(tracked={"f": (filename, "seed\n")})
    env.tracked(filename).write_bytes(base)
    assert env.run_verb(_INSTALL).exit_code == 0
    live = env.live(f".setforge_it/{filename}")
    live.write_bytes(broken)

    result = env.run_verb([verb, "--auto=use-live", "--yes"])

    assert result.exit_code == 1, result.output
    assert env.tracked(filename).read_bytes() == base
    assert live.read_bytes() == broken


@pytest.mark.parametrize("kind", sorted(_BROKEN))
def test_install_does_not_rewrite_a_live_file_that_no_longer_parses(
    kind: str, integration_env: Factory, integration_subprocess
) -> None:
    filename, base, broken = _BROKEN[kind]
    env = integration_env(tracked={"f": (filename, "seed\n"), "n": ("n.txt", "one\n")})
    env.tracked(filename).write_bytes(base)
    assert env.run_verb(_INSTALL).exit_code == 0
    live = env.live(f".setforge_it/{filename}")
    live.write_bytes(broken)
    env.tracked(filename).write_bytes(base.replace(b"1", b"2"))
    env.tracked("n.txt").write_bytes(b"two\n")

    result = env.run_verb(_INSTALL)

    assert result.exit_code == 1, result.output
    assert filename in result.output.replace("\n", "")
    assert live.read_bytes() == broken
    assert env.live(".setforge_it/n.txt").read_bytes() == b"two\n"
