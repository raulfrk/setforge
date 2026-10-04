"""A throwaway SetForge host for the regression tests.

``Host`` owns an isolated home, state directory and config repository under a
test's ``tmp_path``. The home may be reached through symlinks, as on an NFS or
automounted machine. Verbs run either in-process (``cli``) or as a real
``python -m setforge.cli`` child (``proc``) where the behaviour needs a real
process: being killed, or a directory swapped by an external program."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app

from ..conftest import redirect_local_config_path

REPO_ROOT = Path(__file__).resolve().parents[2]

HOME_KINDS = (
    "plain",
    "symlink",
    "relative",
    "trailing-slash",
    "ancestor",
    "cross-device",
)

_SECOND_DEVICE = Path("/dev/shm")
CROSS_DEVICE_DIRS: list[Path] = []

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "regression",
    "GIT_AUTHOR_EMAIL": "regression@setforge.invalid",
    "GIT_COMMITTER_NAME": "regression",
    "GIT_COMMITTER_EMAIL": "regression@setforge.invalid",
}

_DROPPED_ENV = (
    "SETFORGE_SOURCE",
    "SETFORGE_CODE_BIN",
    "SETFORGE_CLAUDE_BIN",
    "SETFORGE_GITLEAKS_BIN",
    "SETFORGE_PATCH_BIN",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
)

INSTALL_FLAGS = ("--yes", "--no-fetch", "--no-git-check", "--no-secrets-scan")


def make_home(root: Path, kind: str) -> tuple[Path, Path]:
    """Create a home reached through ``kind`` of symlink.

    Returns ``(home, real_home)``: the path the user sees and the directory
    that really holds the files."""
    if kind == "plain":
        home = root / "home"
        home.mkdir()
        return home, home
    if kind == "cross-device":
        return _cross_device_home(root)
    mnt = root / "mnt"
    mnt.mkdir()
    if kind == "ancestor":
        real = mnt / "user"
        real.mkdir()
        (root / "auto").symlink_to("mnt", target_is_directory=True)
        return root / "auto" / "user", real
    real = mnt / "x"
    real.mkdir()
    home = root / "home"
    if kind == "symlink":
        home.symlink_to(real, target_is_directory=True)
    elif kind == "relative":
        home.symlink_to("mnt/x", target_is_directory=True)
    elif kind == "trailing-slash":
        home.symlink_to(f"{real}/", target_is_directory=True)
    else:
        raise ValueError(kind)
    return home, real


def _cross_device_home(root: Path) -> tuple[Path, Path]:
    """A symlinked home whose files live on another filesystem (an NFS home)."""
    if not (_SECOND_DEVICE.is_dir() and os.access(_SECOND_DEVICE, os.W_OK)):
        pytest.skip(f"{_SECOND_DEVICE} is not a writable directory")
    real = Path(tempfile.mkdtemp(prefix="setforge-xdev-", dir=_SECOND_DEVICE))
    CROSS_DEVICE_DIRS.append(real)
    if real.stat().st_dev == root.stat().st_dev:
        pytest.skip(f"{_SECOND_DEVICE} is on the same device as {root}")
    home = root / "home"
    home.symlink_to(real, target_is_directory=True)
    return home, real


def tree(root: Path) -> dict[str, bytes | str]:
    """Relative path -> bytes (files) or link text (symlinks) under ``root``."""
    seen: dict[str, bytes | str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            seen[rel] = f"-> {path.readlink()}"
        elif path.is_file():
            seen[rel] = path.read_bytes()
        else:
            seen[rel] = "<dir>"
    return seen


class Host:
    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        kind: str = "plain",
        state_in_home: bool = False,
        tracked: Mapping[str, bytes | str] | None = None,
        profile: str = "p",
        extra_yaml: str = "",
        profile_yaml: str = "",
        repo_parent: str = "",
        dsts: Mapping[str, str] | None = None,
    ) -> None:
        self.root = tmp_path
        self.profile = profile
        self.home, self.real_home = make_home(tmp_path, kind)
        self.state = (
            self.real_home / ".local" / "state" / "setforge"
            if state_in_home
            else tmp_path / "state"
        )
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir()
        self.repo = tmp_path / repo_parent / "repo"
        self.config = self.repo / "setforge.yaml"
        self.tracked_root = self.repo / "tracked"
        self.tracked_root.mkdir(parents=True)
        self.shims: dict[str, Path] = {}
        self.state_in_home = state_in_home
        self.live_dir = self.home / ".x"
        files = {"note.txt": "one\n"} if tracked is None else dict(tracked)
        lines = ["schema_version: '6.0'", "version: 1", "tracked_files:"]
        for src, body in files.items():
            target = self.tracked_root / src
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body if isinstance(body, bytes) else body.encode())
            dst = (dsts or {}).get(src, f"~/.x/{src}")
            lines.append(f"  {_fid(src)}: {{src: {src}, dst: '{dst}'}}")
        if extra_yaml:
            lines.append(extra_yaml.rstrip("\n"))
        lines += ["profiles:", f"  {profile}:", "    tracked_files:"]
        lines += [f"      - {_fid(src)}" for src in files]
        if profile_yaml:
            lines.append(profile_yaml.rstrip("\n"))
        self.config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _git_init(self.repo)
        monkeypatch.setenv("HOME", str(self.home))
        for name in _DROPPED_ENV:
            monkeypatch.delenv(name, raising=False)
        for key, value in _GIT_ENV.items():
            monkeypatch.setenv(key, value)
        if state_in_home:
            monkeypatch.delenv("SETFORGE_STATE_DIR", raising=False)
        else:
            monkeypatch.setenv("SETFORGE_STATE_DIR", str(self.state))
        monkeypatch.setattr(Path, "home", lambda: Path(os.environ["HOME"]))
        redirect_local_config_path(
            monkeypatch, self.home / ".config" / "setforge" / "local.yaml"
        )

    def live(self, rel: str) -> Path:
        return self.live_dir / rel

    def tracked(self, rel: str) -> Path:
        return self.tracked_root / rel

    def _argv(self, argv: tuple[str, ...], config: bool, profile: bool) -> list[str]:
        args = list(argv)
        if config:
            args.append(f"--config={self.config}")
        if profile:
            args.append(f"--profile={self.profile}")
        return args

    def cli(self, *argv: str, config: bool = True, profile: bool = True) -> Result:
        return CliRunner().invoke(app, self._argv(argv, config, profile))

    def install(self, *extra: str) -> Result:
        return self.cli("install", *INSTALL_FLAGS, *extra)

    def shim(self, name: str, script: str) -> Path:
        """Write an executable stand-in for an external program."""
        path = self.bin_dir / name
        path.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
        path.chmod(0o755)
        self.shims[name] = path
        return path

    def proc_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE")}
        for name in _DROPPED_ENV:
            env.pop(name, None)
        env.update(_GIT_ENV)
        env["HOME"] = str(self.home)
        env["_ZO_DOCTOR"] = "0"
        if self.state_in_home:
            env.pop("SETFORGE_STATE_DIR", None)
        else:
            env["SETFORGE_STATE_DIR"] = str(self.state)
        for name, path in self.shims.items():
            env[f"SETFORGE_{name.upper()}_BIN"] = str(path)
        return env

    def proc(
        self,
        *argv: str,
        config: bool = True,
        profile: bool = True,
        timeout: int = 60,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "setforge.cli", *self._argv(argv, config, profile)],
            cwd=REPO_ROOT,
            env=self.proc_env(),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def other_checkout(self) -> Path:
        """A second, separate checkout of the same configuration (its own owner)."""
        other = self.root / "repo2"
        shutil.copytree(self.repo, other, ignore=shutil.ignore_patterns(".git"))
        _git_init(other)
        return other / "setforge.yaml"

    def arm(self, when: str) -> None:
        (self.bin_dir / "when").write_text(when, encoding="utf-8")

    def release(self) -> None:
        (self.bin_dir / "release").write_text("", encoding="utf-8")

    def installed_extensions(self) -> list[str]:
        path = self.bin_dir / "installed"
        return path.read_text(encoding="utf-8").split() if path.exists() else []

    def proc_install(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.proc("install", *INSTALL_FLAGS, *extra)


def _git_init(repo: Path) -> None:
    env = {**os.environ, **_GIT_ENV}
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["commit", "-q", "-m", "seed"],
    ):
        subprocess.run(
            ["git", *args], cwd=repo, env=env, check=True, capture_output=True
        )


def _fid(src: str) -> str:
    return Path(src).name.replace(".", "_").replace("-", "_")


def claim_ids_for(listing: str, suffix: str) -> list[str]:
    """Claim ids in ``ownership list`` output whose locator ends with ``suffix``."""
    ids: list[str] = []
    current = ""
    for line in listing.splitlines():
        if line and not line.startswith(" "):
            current = line.strip()
        elif line.strip().startswith("locator:") and line.strip().endswith(suffix):
            ids.append(current)
    return ids


EXTENSION_YAML = "packages:\n  pe: {type: extension, extension: pub.name}\n"
EXTENSION_PROFILE = "    packages: [pe]"

_FAKE_CODE = """
D="$(dirname "$0")"
WHEN="$(cat "$D/when" 2>/dev/null)"
case "$1" in
  --list-extensions)
    [ "$WHEN" = list ] && kill -9 $PPID
    cat "$D/installed" 2>/dev/null; exit 0;;
  --install-extension)
    [ "$WHEN" = install ] && kill -9 $PPID
    [ "$WHEN" = hold ] && while [ ! -e "$D/release" ]; do sleep 0.1; done
    echo "$2" >> "$D/installed"; exit 0;;
  --uninstall-extension)
    [ "$WHEN" = uninstall ] && kill -9 $PPID
    grep -vx "$2" "$D/installed" > "$D/installed.new"
    mv "$D/installed.new" "$D/installed"; exit 0;;
esac
exit 0
"""


def extension_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: object
) -> Host:
    """A host whose profile installs one extension through a scriptable ``code``.

    ``host.arm("install")`` makes the next ``--install-extension`` (or
    ``list``/``uninstall``) kill its parent SetForge process with SIGKILL;
    ``arm("hold")`` makes it wait until ``host.release()``."""
    host = Host(
        tmp_path,
        monkeypatch,
        extra_yaml=EXTENSION_YAML,
        profile_yaml=EXTENSION_PROFILE,
        **kwargs,  # type: ignore[arg-type]
    )
    host.shim("code", _FAKE_CODE)
    return host


PROJECT_YAML = (
    "project_profiles:\n"
    "  demo:\n"
    "    files:\n"
    "      agents:\n"
    "        src: AGENTS.md\n"
    "        dst: AGENTS.md\n"
)


def git(path: Path, *args: str, check: bool = True) -> str:
    env = {**os.environ, **_GIT_ENV}
    done = subprocess.run(
        ["git", "-C", str(path), *args],
        check=check,
        env=env,
        text=True,
        capture_output=True,
    )
    return done.stdout


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    return path


def candidate_setforge_on_path(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the Git filter children of tracked overlays run this source tree."""
    binary_dir = root / "candidate-bin"
    binary_dir.mkdir()
    entrypoint = binary_dir / "setforge"
    entrypoint.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "_original_cwd = os.getcwd()\n"
        "try:\n"
        f"    os.chdir({str(REPO_ROOT)!r})\n"
        "    from setforge.cli import main\n"
        "finally:\n"
        "    os.chdir(_original_cwd)\n"
        "main()\n",
        encoding="utf-8",
    )
    entrypoint.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
