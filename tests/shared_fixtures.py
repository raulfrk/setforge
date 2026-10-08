"""Shared fixtures; ``tests/conftest.py`` re-exports them for discovery."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from tests.fakes import REAL_SUBPROCESS_RUN, FakeCode


@dataclass(frozen=True)
class ConfigRepo:
    """A config repository directory that can write ``setforge.yaml`` and sources."""

    root: Path

    @property
    def config(self) -> Path:
        return self.root / "setforge.yaml"

    def tracked(self, src: str) -> Path:
        return self.root / "tracked" / src

    def write_tracked(self, src: str, body: str | bytes) -> Path:
        """Write ``body`` to ``tracked/<src>``, creating parent directories."""
        target = self.tracked(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            target.write_bytes(body)
        else:
            target.write_text(body, encoding="utf-8")
        return target

    def write_config(
        self,
        *,
        profile: str,
        tracked_files: Mapping[str, Mapping[str, object]],
        profile_extra: Mapping[str, object] | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> Path:
        """Write ``setforge.yaml`` whose ``profile`` lists every tracked file.

        ``extra`` adds top-level keys and ``profile_extra`` adds keys to the
        profile.
        """
        document = {
            "version": 1,
            "tracked_files": {tid: dict(spec) for tid, spec in tracked_files.items()},
            **(extra or {}),
            "profiles": {
                profile: {"tracked_files": list(tracked_files), **(profile_extra or {})}
            },
        }
        yaml = YAML(typ="rt")
        yaml.default_flow_style = False
        with self.config.open("w", encoding="utf-8") as fh:
            yaml.dump(document, fh)
        return self.config


@pytest.fixture
def config_repo(repo: Path) -> ConfigRepo:
    """The ``repo`` sandbox (home, state dir, empty repo directory) as a ConfigRepo."""
    return ConfigRepo(repo)


def _isolated_git_env() -> dict[str, str]:
    """Environment that cannot read the caller's git config or inherited git state."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.com",
    )
    return env


@pytest.fixture(scope="session")
def git_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A repository on ``main`` with identity set and one commit of ``README.md``.

    Built once per pytest session, so once per xdist worker, from a git
    environment that ignores the user's and the system's config. Copy it with
    :func:`init_git_repo`; never run git in it.
    """
    template = tmp_path_factory.mktemp("git_template")
    env = _isolated_git_env()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=template, env=env, check=True, capture_output=True
        )

    git("init", "-q", "-b", "main", "--template=")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (template / "README.md").write_text("# initial\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-q", "-m", "initial")
    return template


@pytest.fixture
def init_git_repo(git_template: Path) -> Callable[[Path], Path]:
    """Return ``init(dest)`` that copies :func:`git_template` into ``dest``.

    ``dest`` ends up a repository on ``main`` with identity configured and one
    ``initial`` commit holding ``README.md``; every copy shares that commit.
    """

    def init(dest: Path) -> Path:
        shutil.copytree(git_template, dest, symlinks=True, dirs_exist_ok=True)
        return dest

    return init


@pytest.fixture
def fake_code(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeCode]:
    """Return ``factory(installed=(), *, real_binaries=())`` wiring a :class:`FakeCode`.

    ``code`` resolves to a fake path and ``subprocess.run`` in
    ``setforge.vscode_extensions`` is the fake. It fails closed: ``claude`` argv
    reaches a co-resident ``fake_claude`` only when that fixture was built
    first, and any other argv raises unless its binary name is in
    ``real_binaries`` (for example ``("git",)``), which runs it for real.
    """

    def factory(
        installed: Iterable[str] = (), *, real_binaries: Iterable[str] = ()
    ) -> FakeCode:
        fake = FakeCode(installed)
        if subprocess.run is not REAL_SUBPROCESS_RUN:
            fake.delegate = subprocess.run
        fake.real_binaries = frozenset(real_binaries)
        monkeypatch.setattr(
            "setforge.vscode_extensions.resolve_binary",
            lambda name: Path("/usr/bin/code") if name == "code" else None,
        )
        monkeypatch.setattr("setforge.vscode_extensions.subprocess.run", fake.run)
        return fake

    return factory
