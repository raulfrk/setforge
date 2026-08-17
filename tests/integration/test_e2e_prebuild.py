"""Process-level proof that xdist performs one controller image build."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.test_infra]


@pytest.mark.parametrize(
    ("markexpr", "target"),
    [("e2e_docker", "full"), ("e2e_docker and smoke", "smoke")],
)
def test_xdist_controller_builds_once_before_worker_collection(
    tmp_path: Path, markexpr: str, target: str
) -> None:
    """Cross the real pytest/xdist/process boundary with a stateful fake Docker."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "image-exists"
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$FAKE_DOCKER_LOG"\n'
        'if [ "$1 $2" = "image inspect" ]; then\n'
        '  test -f "$FAKE_DOCKER_STATE"\n'
        "  exit $?\n"
        "fi\n"
        'if [ "$1" = "build" ]; then\n'
        '  touch "$FAKE_DOCKER_STATE"\n'
        "  exit 0\n"
        "fi\n"
        "exit 64\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "FAKE_DOCKER_LOG": str(log),
            "FAKE_DOCKER_STATE": str(state),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
    )

    repo_root = Path(__file__).resolve().parents[2]
    probe = repo_root / "tests" / "docker" / f"test_xdist_probe_{uuid.uuid4().hex}.py"
    probe.write_text(
        "import pytest\n"
        "pytestmark = [pytest.mark.e2e_docker, pytest.mark.smoke]\n"
        "def test_probe(docker_image):\n"
        f"    assert docker_image.startswith('setforge-e2e:{target}-')\n",
        encoding="utf-8",
    )
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(probe.relative_to(repo_root)),
                "-m",
                markexpr,
                "-n",
                "2",
                "--dist=each",
                "-p",
                "xdist.plugin",
                "-o",
                "addopts=",
                "--strict-markers",
                "-q",
            ],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    finally:
        probe.unlink(missing_ok=True)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert sum(call.startswith("build ") for call in calls) == 1
    assert sum(call.startswith("image inspect ") for call in calls) == 3
    assert calls[0].startswith("image inspect ")
    assert calls[1].startswith("build ")
    assert f"--target {target}" in calls[1]
    assert all(f"setforge-e2e:{target}-" in call for call in calls)
