"""Integration tier: real-git harness + per-binary subprocess policy.

git runs for REAL but scoped to read-only/local subcommands (no network);
claude/code/gitleaks are mocked. Any unregistered call RAISES rather than
reaching the host binary — the no-leak proof for this whole tier."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import Result
from pytest_subprocess import FakeProcess
from typer.testing import CliRunner

from setforge.cli import app

_REAL_GIT_SUBCOMMANDS: tuple[str, ...] = (
    "init",
    "add",
    "commit",
    "config",
    "status",
    "rev-parse",
    "rev-list",
    "symbolic-ref",
    "show-ref",
    "merge-base",
)

_GIT_PASSTHROUGH_OCCURRENCES = 64


def _harden_git_environment(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    # Blocks host ~/.gitconfig (credential helpers) and network protocols.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_SSH_COMMAND", "/bin/false")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "setforge-it")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "it@setforge.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "setforge-it")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "it@setforge.invalid")
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture
def integration_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[FakeProcess]:
    # allow_unregistered is NEVER enabled here — see module docstring.
    _harden_git_environment(monkeypatch, tmp_path / "home")
    git_bins = ["git"]
    resolved = shutil.which("git")
    if resolved is not None:
        git_bins.append(resolved)
    with FakeProcess() as process:
        patch_bins = ["patch"]
        resolved_patch = shutil.which("patch")
        if resolved_patch is not None:
            patch_bins.append(resolved_patch)
        for patch_bin in patch_bins:
            process.pass_command(
                [patch_bin, process.any(min=0)],
                occurrences=_GIT_PASSTHROUGH_OCCURRENCES,
            )
        for git_bin in git_bins:
            for sub in _REAL_GIT_SUBCOMMANDS:
                process.pass_command(
                    [git_bin, sub, process.any(min=0)],
                    occurrences=_GIT_PASSTHROUGH_OCCURRENCES,
                )
                process.pass_command(
                    [git_bin, "-C", process.any(min=1, max=1), sub, process.any(min=0)],
                    occurrences=_GIT_PASSTHROUGH_OCCURRENCES,
                )
        yield process


@dataclass(frozen=True, slots=True)
class IntegrationEnv:
    home: Path
    state_dir: Path
    local_config: Path
    repo: Path
    config: Path
    profile: str
    _run: Callable[..., Result] = field(repr=False)
    _present: Callable[[str], Path] = field(repr=False)

    def run_verb(
        self,
        argv: Sequence[str],
        *,
        inject_config: bool = True,
        inject_profile: bool = True,
    ) -> Result:
        return self._run(
            argv, inject_config=inject_config, inject_profile=inject_profile
        )

    def present_binary(self, name: str) -> Path:
        return self._present(name)

    def live(self, rel: str) -> Path:
        return self.home / rel

    def tracked(self, rel: str) -> Path:
        return self.repo / "tracked" / rel


_DEFAULT_TRACKED: dict[str, tuple[str, str]] = {
    "note": ("text/note.txt", "hello from tracked\n"),
    "settings": ("json/settings.json", '{\n  "a": 1,\n  "b": "two"\n}\n'),
}

_DST_TEMPLATE = "~/.setforge_it/{src}"

_MOCKED_BINARIES: frozenset[str] = frozenset({"claude", "code", "gitleaks"})


def _write_config_repo(
    repo: Path,
    *,
    profile: str,
    tracked: dict[str, tuple[str, str]],
    git_init: bool,
) -> Path:
    tracked_root = repo / "tracked"
    tracked_root.mkdir(parents=True, exist_ok=True)
    lines = ["schema_version: '5.0'", "version: 1", "tracked_files:"]
    for tid, (src, body) in tracked.items():
        dst = _DST_TEMPLATE.format(src=src)
        target = tracked_root / src
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        lines += [f"  {tid}:", f"    src: {src}", f"    dst: {dst}"]
    lines += ["profiles:", f"  {profile}:", "    tracked_files:"]
    lines += [f"      - {tid}" for tid in tracked]
    cfg = repo / "setforge.yaml"
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if git_init:
        env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "setforge-it",
            "GIT_AUTHOR_EMAIL": "it@setforge.invalid",
            "GIT_COMMITTER_NAME": "setforge-it",
            "GIT_COMMITTER_EMAIL": "it@setforge.invalid",
        }
        for args in (
            ["init", "-q", "-b", "main"],
            ["add", "-A"],
            ["commit", "-q", "-m", "seed"],
        ):
            subprocess.run(
                ["git", *args],
                cwd=repo,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
    return cfg


@pytest.fixture
def integration_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Callable[..., IntegrationEnv]:
    home = tmp_path / "home"
    state_dir = tmp_path / "state"
    mp_cache = tmp_path / "mp"
    local_yaml = home / ".config" / "setforge" / "local.yaml"
    for d in (home, state_dir, mp_cache):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_dir))
    monkeypatch.setattr(Path, "home", lambda: Path(os.environ["HOME"]))
    monkeypatch.setattr(
        "setforge.claude_marketplace_cache.MARKETPLACE_CACHE_ROOT", mp_cache
    )
    for mod_attr in (
        "setforge.binaries.LOCAL_CONFIG_PATH",
        "setforge.source.LOCAL_CONFIG_PATH",
        "setforge.compare.LOCAL_CONFIG_PATH",
        "setforge.cli.orphans.LOCAL_CONFIG_PATH",
    ):
        monkeypatch.setattr(mod_attr, local_yaml)
    for name in _MOCKED_BINARIES:
        monkeypatch.delenv(f"SETFORGE_{name.upper()}_BIN", raising=False)

    _present_paths: dict[str, Path] = {}
    fake_bin_dir = tmp_path / "fakebin"
    fake_bin_dir.mkdir(exist_ok=True)

    from setforge import binaries as _binaries_mod

    _real_resolve = _binaries_mod.resolve_binary

    def _resolver(name: str) -> Path | None:
        if name in _MOCKED_BINARIES:
            return _present_paths.get(name)
        return _real_resolve(name)

    from setforge.claude_plugins import _get_claude_bin as _claude_bin_cache

    monkeypatch.setattr("setforge.claude_plugins.resolve_binary", _resolver)
    monkeypatch.setattr("setforge.vscode_extensions.resolve_binary", _resolver)
    monkeypatch.setattr("setforge.binaries.resolve_binary", _resolver)
    monkeypatch.setattr("setforge.transitions.resolve_binary", _resolver, raising=False)

    def _present(name: str) -> Path:
        path = fake_bin_dir / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        _present_paths[name] = path
        _claude_bin_cache.cache_clear()
        return path

    def build(
        *,
        profile: str = "it",
        tracked: dict[str, tuple[str, str]] | None = None,
        git_init: bool = True,
    ) -> IntegrationEnv:
        repo = tmp_path / "repo"
        repo.mkdir(exist_ok=True)
        corpus = dict(tracked) if tracked is not None else dict(_DEFAULT_TRACKED)
        cfg = _write_config_repo(
            repo, profile=profile, tracked=corpus, git_init=git_init
        )

        def _run(
            argv: Sequence[str],
            *,
            inject_config: bool = True,
            inject_profile: bool = True,
        ) -> Result:
            args = list(argv)
            if inject_config and not any(
                a == "--config" or a.startswith("--config=") for a in args
            ):
                args.append(f"--config={cfg}")
            if inject_profile and not any(
                a in ("--profile", "-p") or a.startswith("--profile=") for a in args
            ):
                args.append(f"--profile={profile}")
            return CliRunner().invoke(app, args)

        return IntegrationEnv(
            home=home,
            state_dir=state_dir,
            local_config=local_yaml,
            repo=repo,
            config=cfg,
            profile=profile,
            _run=_run,
            _present=_present,
        )

    return build
