from __future__ import annotations

import subprocess

import pytest

from tests.docker import container_runtime


def test_env_args_handles_none_and_preserves_mapping_order() -> None:
    assert container_runtime.env_args(None) == []
    assert container_runtime.env_args({"ONE": "1", "TWO": "2"}) == [
        "-e",
        "ONE=1",
        "-e",
        "TWO=2",
    ]


def test_container_run_argv_labels_container_and_appends_command() -> None:
    assert container_runtime.container_run_argv(
        name="case-name",
        image="setforge-e2e:full-hash",
        env={"SETFORGE_NO_WELCOME": "1"},
        cmd=["sleep", "5"],
    ) == [
        "docker",
        "run",
        "--rm",
        "-d",
        "--name",
        "case-name",
        "--label",
        container_runtime.E2E_CONTAINER_LABEL,
        "-w",
        "/workspace",
        "-e",
        "SETFORGE_NO_WELCOME=1",
        "setforge-e2e:full-hash",
        "sleep",
        "5",
    ]


def test_container_run_argv_accepts_default_image_command() -> None:
    argv = container_runtime.container_run_argv(
        name="case-name",
        image="setforge-e2e:smoke-hash",
        env={},
        cmd=None,
    )
    assert argv[-1] == "setforge-e2e:smoke-hash"


@pytest.mark.parametrize(
    ("path", "expected_parent"),
    [("/tmp/nested/data.bin", "/tmp/nested"), ("data.bin", "/")],
)
def test_stream_bytes_covers_nested_and_root_paths(
    path: str,
    expected_parent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parents: list[str] = []
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(container_runtime.subprocess, "run", fake_run)
    container_runtime.stream_bytes(
        cid="container-id",
        path=path,
        content=b"\x00\xff",
        ensure_parent=parents.append,
        timeout=120,
    )

    assert parents == [expected_parent]
    assert calls == [
        (
            ["docker", "exec", "-i", "container-id", "tee", path],
            {
                "input": b"\x00\xff",
                "check": True,
                "capture_output": True,
                "timeout": 120,
            },
        )
    ]
