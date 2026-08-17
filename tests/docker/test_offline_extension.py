from __future__ import annotations

import io
import json
from zipfile import ZipFile

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.docker import offline_extension


def test_fixture_vsix_has_consistent_identity_and_fixed_metadata() -> None:
    first = offline_extension.fixture_vsix_bytes()
    second = offline_extension.fixture_vsix_bytes()

    assert first == second
    with ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == [
            "[Content_Types].xml",
            "extension.vsixmanifest",
            "extension/package.json",
        ]
        assert all(
            info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()
        )
        package = json.loads(archive.read("extension/package.json"))
        manifest = archive.read("extension.vsixmanifest").decode()

    assert (
        f"{package['publisher']}.{package['name']}"
        == offline_extension.FIXTURE_EXTENSION_ID
    )
    assert f'Publisher="{package["publisher"]}"' in manifest
    assert f'Id="{package["name"]}"' in manifest
    assert f'Version="{package["version"]}"' in manifest


def test_rewritten_code_argv_maps_only_exact_fixture_install() -> None:
    assert offline_extension.rewritten_code_argv(
        ["--install-extension", offline_extension.FIXTURE_EXTENSION_ID]
    ) == [
        offline_extension.REAL_CODE_PATH,
        "--install-extension",
        offline_extension.FIXTURE_VSIX_PATH,
    ]


@given(st.lists(st.text(min_size=1), max_size=5))
def test_rewritten_code_argv_transparently_delegates_other_argv(
    args: list[str],
) -> None:
    exact_fixture_install = [
        "--install-extension",
        offline_extension.FIXTURE_EXTENSION_ID,
    ]
    if args == exact_fixture_install:
        args = [*args, "--force"]
    assert offline_extension.rewritten_code_argv(args) == [
        offline_extension.REAL_CODE_PATH,
        *args,
    ]


def test_main_execs_rewritten_real_code_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        offline_extension.sys,
        "argv",
        ["adapter", "--list-extensions"],
    )
    monkeypatch.setattr(
        offline_extension.os,
        "execv",
        lambda path, argv: calls.append((path, argv)),
    )

    offline_extension.main()

    assert calls == [
        (
            offline_extension.REAL_CODE_PATH,
            [offline_extension.REAL_CODE_PATH, "--list-extensions"],
        )
    ]
