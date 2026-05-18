#!/usr/bin/env python3
"""Pre-tag local verification for setforge releases.

Runs 8 checks before pushing a v*.*.* tag. Exits 0 on success; 1 on the
first failing step (with the step name); 2 if the environment is
unusable (no `uv` binary, no `pyproject.toml`).

Invocation:

    uv run python scripts/release_preflight.py

Idempotent: each invocation starts from a clean dist/ + tmp UV_TOOL_DIR.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path

from ruamel.yaml import YAML  # type: ignore[import-not-found]

_REQUIRED_COMMANDS = (
    "install",
    "sync",
    "compare",
    "revert",
    "validate",
    "ext",
    "plugin",
    "marketplace",
)

_WORKFLOW_REQUIRED_JOBS: dict[str, frozenset[str]] = {
    "ci.yml": frozenset({"build-verify"}),
    "publish-pypi.yml": frozenset({"build-and-publish"}),
    "release.yml": frozenset({"release"}),
}


def _run(
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with the project's standard discipline."""
    return subprocess.run(
        list(args),
        check=True,
        text=True,
        capture_output=True,
        timeout=300,
        env=env,
    )


def _read_version() -> str:
    """Return the [project] version from pyproject.toml."""
    data = tomllib.loads(Path("pyproject.toml").read_text())
    return data["project"]["version"]


def step_1_uv_build(version: str) -> None:
    """Build sdist + wheel; verify both land in dist/."""
    shutil.rmtree("dist", ignore_errors=True)
    _run("uv", "build")
    sdist = Path(f"dist/setforge-{version}.tar.gz")
    wheel = Path(f"dist/setforge-{version}-py3-none-any.whl")
    if not sdist.exists():
        raise AssertionError(f"missing {sdist}")
    if not wheel.exists():
        raise AssertionError(f"missing {wheel}")


def step_2_twine_check() -> None:
    """Run twine check against dist/*."""
    _run("uv", "tool", "run", "--from", "twine", "twine", "check", "dist/*")


def step_3_install_in_tmp(version: str) -> Path:
    """Install the wheel in a tmp UV_TOOL_DIR; return the dir."""
    tmp_tool_dir = Path(tempfile.mkdtemp(prefix="setforge-preflight-"))
    atexit.register(shutil.rmtree, tmp_tool_dir, ignore_errors=True)
    wheel = f"./dist/setforge-{version}-py3-none-any.whl"
    env = {**os.environ, "UV_TOOL_DIR": str(tmp_tool_dir)}
    _run("uv", "tool", "install", wheel, env=env)
    return tmp_tool_dir


def step_4_installed_version(tmp_tool_dir: Path, expected: str) -> None:
    """Verify `setforge --version` from the tmp install matches expected."""
    env = {**os.environ, "UV_TOOL_DIR": str(tmp_tool_dir)}
    result = _run("uv", "tool", "run", "setforge", "--version", env=env)
    actual = result.stdout.strip()
    if actual != expected:
        raise AssertionError(f"version mismatch: {actual!r} != {expected!r}")


def step_5_installed_help(tmp_tool_dir: Path) -> None:
    """Verify all 8 commands appear in `setforge --help` from the tmp install."""
    env = {**os.environ, "UV_TOOL_DIR": str(tmp_tool_dir)}
    result = _run("uv", "tool", "run", "setforge", "--help", env=env)
    missing = [c for c in _REQUIRED_COMMANDS if c not in result.stdout]
    if missing:
        raise AssertionError(f"--help missing commands: {missing}")


def step_6_import_version(version: str) -> None:
    """Verify the current venv's setforge.__version__ matches pyproject."""
    _run(
        sys.executable,
        "-c",
        f"import setforge; assert setforge.__version__ == '{version}', "
        f"f'version drift: {{setforge.__version__}} != {version}'",
    )


def step_7_workflow_yaml_integrity() -> None:
    """Verify every .github/workflows/*.yml parses AND declares required jobs."""
    yaml = YAML(typ="safe")
    for path in Path(".github/workflows").glob("*.yml"):
        try:
            data = yaml.load(path)
        except Exception as exc:
            raise AssertionError(f"YAML parse failed for {path}: {exc}") from exc
        required = _WORKFLOW_REQUIRED_JOBS.get(path.name)
        if not required:
            continue
        present = frozenset((data.get("jobs") or {}).keys())
        missing = required - present
        if missing:
            raise AssertionError(
                f"{path}: missing required jobs: {sorted(missing)} "
                f"(present: {sorted(present)})"
            )


def step_8_bd_ready_p012_empty() -> None:
    """Verify `bd ready` returns no P0/P1/P2 unfinished work."""
    result = _run("bd", "ready", "--explain")
    blocking_lines = [
        line
        for line in result.stdout.splitlines()
        if "● P0 " in line or "● P1 " in line or "● P2 " in line
    ]
    if blocking_lines:
        joined = "\n    ".join(blocking_lines)
        raise AssertionError(f"unfinished P0/P1/P2 work blocks release:\n    {joined}")


def _run_step(name: str, fn: Callable[[], None]) -> bool:
    """Run one step; print result; return True on pass, False on fail."""
    print(f"  • {name}", end=" ", flush=True)
    try:
        fn()
    except subprocess.CalledProcessError as exc:
        print(f"FAILED: {exc}")
        if exc.stderr:
            print(f"    stderr: {exc.stderr.strip()}", file=sys.stderr)
        if exc.stdout:
            print(f"    stdout: {exc.stdout.strip()}", file=sys.stderr)
        return False
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        return False
    print("OK")
    return True


def main() -> int:
    """Run the 8-step preflight; return 0 on success, 1 on first failure."""
    try:
        version = _read_version()
    except (FileNotFoundError, KeyError) as exc:
        print(f"FATAL: cannot read pyproject.toml version: {exc}", file=sys.stderr)
        return 2

    print(f"===> setforge {version} preflight")

    if not _run_step("1: uv build", lambda: step_1_uv_build(version)):
        return 1
    if not _run_step("2: twine check", step_2_twine_check):
        return 1

    # Step 3 yields tmp_tool_dir for steps 4-5 to share; run inline so the
    # closures below can capture it.
    print("  • 3: install in tmp UV_TOOL_DIR", end=" ", flush=True)
    try:
        tmp_tool_dir = step_3_install_in_tmp(version)
    except subprocess.CalledProcessError as exc:
        print(f"FAILED: {exc}")
        if exc.stderr:
            print(f"    stderr: {exc.stderr.strip()}", file=sys.stderr)
        if exc.stdout:
            print(f"    stdout: {exc.stdout.strip()}", file=sys.stderr)
        return 1
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        return 1
    print("OK")

    remaining: list[tuple[str, Callable[[], None]]] = [
        (
            "4: installed --version",
            lambda: step_4_installed_version(tmp_tool_dir, version),
        ),
        (
            "5: installed --help (8 commands present)",
            lambda: step_5_installed_help(tmp_tool_dir),
        ),
        (
            "6: import setforge; assert __version__",
            lambda: step_6_import_version(version),
        ),
        ("7: workflow YAML integrity", step_7_workflow_yaml_integrity),
        ("8: bd ready P0/P1/P2 empty", step_8_bd_ready_p012_empty),
    ]
    for name, fn in remaining:
        if not _run_step(name, fn):
            return 1

    print("===> preflight passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
