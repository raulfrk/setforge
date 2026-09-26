from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from scripts import release_preflight


def test_installed_checks_share_isolated_uv_tool_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "setforge-preflight-test"
    root.mkdir()
    monkeypatch.setattr(
        release_preflight.tempfile,
        "mkdtemp",
        lambda **_kwargs: str(root),
    )
    monkeypatch.setattr(release_preflight.atexit, "register", lambda *_a, **_kw: None)
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def _run(
        *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        assert env is not None
        calls.append((args, env))
        stdout = (
            "1.3.4"
            if "--version" in args
            else " ".join(release_preflight._REQUIRED_COMMANDS)
        )
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(release_preflight, "_run", _run)

    installed_root = release_preflight.step_3_install_in_tmp("1.3.4")
    release_preflight.step_4_installed_version(installed_root, "1.3.4")
    release_preflight.step_5_installed_help(installed_root)

    assert installed_root == root
    assert len(calls) == 3
    for _, env in calls:
        assert env["UV_TOOL_DIR"] == str(root / "tools")
        assert env["UV_TOOL_BIN_DIR"] == str(root / "bin")


def test_preflight_accepts_current_repository_workflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    release_preflight.step_7_workflow_yaml_integrity()


@pytest.mark.parametrize(
    ("workflow", "missing_job"),
    [
        ("ci.yml", "workbox-unit"),
        ("ci.yml", "workbox-integration"),
        ("ci.yml", "secrets-scan"),
        ("publish-pypi.yml", "build-and-publish"),
        ("release.yml", "release"),
    ],
)
def test_preflight_refuses_missing_required_workflow_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workflow: str,
    missing_job: str,
) -> None:
    source = Path(__file__).resolve().parent.parent / ".github/workflows"
    destination = tmp_path / ".github/workflows"
    destination.mkdir(parents=True)
    for path in source.glob("*.yml"):
        (destination / path.name).write_bytes(path.read_bytes())
    yaml = YAML(typ="safe")
    path = destination / workflow
    data = yaml.load(path.read_text())
    del data["jobs"][missing_job]
    with path.open("w") as stream:
        yaml.dump(data, stream)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(AssertionError, match=missing_job):
        release_preflight.step_7_workflow_yaml_integrity()
