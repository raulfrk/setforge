"""Integration tier: real-git harness + per-binary subprocess policy.

This is a NEW test layer, distinct from the Docker e2e suite. It drives
the real Typer verb surface (``install`` / ``sync`` / ``compare`` /
``revert`` / ``migrate`` / ...) against a MINIMAL config-repo tree seeded
on an isolated ``$HOME``, and asserts on POST-PARSE OBSERVABLES — exit
code, on-disk store bodies, transition records, gitleaks-filtered finding
counts — never on registered fake stdout.

Two pieces of infra live here:

- :func:`integration_subprocess` — the per-binary subprocess policy
  (§"Mock policy" below). ``git`` runs FOR REAL but NARROWLY scoped to
  read-only / local subcommands so it can never reach a network remote,
  under a hardened env that cannot read the host ``~/.gitconfig``.
  ``claude`` / ``code`` / ``gitleaks`` are NOT passed through: an
  unregistered invocation of any of them RAISES
  (``ProcessNotRegisteredError``) — the network-leak proof. A test that
  legitimately needs one registers its exact argv.
- :func:`integration_env` — a parameterized builder that seeds a minimal
  valid config repo (``setforge.yaml`` + ``tracked/`` + a profile) on the
  isolated home, overrides EVERY data-dir constant the verbs resolve
  (state dir, marketplace cache root, local.yaml, binary-override env
  vars), and returns a ``run_verb(argv)`` helper wrapping ``CliRunner``.

Why real git (not the in-Python ``fake_git``): the source-clean gate
(``source.check_source_clean`` → ``git status --porcelain``) and the
status/upstream probes shell out to the real binary. Exercising the real
git seam catches env-hardening / argv regressions the in-memory fake
cannot see, while ``pass_command`` scoping keeps the blast radius to
``status`` / ``rev-parse`` / ``init`` / ``add`` / ``commit`` / ``config``
/ ``symbolic-ref`` — never ``clone`` / ``fetch`` / ``remote`` / ``pull``
/ ``push`` — so no test can accidentally hit the network.
"""

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

# ---------------------------------------------------------------------------
# Mock policy — per-binary subprocess boundary
# ---------------------------------------------------------------------------

# git subcommands the verbs legitimately shell out to. Each is either
# read-only (status / rev-parse / rev-list / symbolic-ref / merge-base /
# show-ref) or purely local (init / add / commit / config) — NONE of them
# can contact a network remote. `clone` / `fetch` / `remote` / `pull` /
# `push` are deliberately absent: an attempt to run one is unregistered and
# RAISES, so a verb that grows a network git call surfaces loudly here
# instead of leaking. Verbs invoke git in two argv shapes — bare
# `git <sub> ...` (cwd-scoped) and `git -C <dir> <sub> ...` (explicit dir);
# the fixture registers both per subcommand.
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

# A high passthrough budget: a single verb may run several git probes
# (status gate + upstream rev-parse), and idempotent re-runs across a
# test double them. `occurrences` in pytest-subprocess is per-registration;
# picking a generous ceiling avoids a spurious ProcessNotRegisteredError
# on the Nth identical call without reopening any binary we mean to block.
_GIT_PASSTHROUGH_OCCURRENCES = 64


def _harden_git_environment(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Neuter host git config / SSH for every real-git call in this test.

    ``setforge.git_ops._hardened_env`` overlays ``os.environ`` before every
    invocation, so hardening HERE (on the real process env) is what actually
    reaches the child git. Pinning the config files to ``/dev/null`` means
    real git cannot read the developer's ``~/.gitconfig`` (identity, aliases,
    url-insteadOf rewrites, credential helpers); the SSH override refuses any
    connection attempt instantly instead of prompting or hanging.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")  # block http/https/ssh/git
    monkeypatch.setenv("GIT_SSH_COMMAND", "/bin/false")
    # A commit made by the seeded repo needs an identity; supply a throwaway
    # one via env (survives GIT_CONFIG_GLOBAL=/dev/null) so `git commit`
    # succeeds without reading the host identity.
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
    """Per-binary subprocess policy for the integration tier.

    - ``git``: passed through to the REAL binary, but only for the
      read-only / local subcommands in :data:`_REAL_GIT_SUBCOMMANDS`. A
      ``clone`` / ``fetch`` / ``remote`` / ``pull`` / ``push`` is NOT
      registered, so it raises — no test can reach a network remote.
    - ``claude`` / ``code`` / ``gitleaks``: NOT registered by default.
      Any invocation raises ``ProcessNotRegisteredError`` (the no-leak
      proof). A test that needs one registers its exact argv on the
      yielded :class:`FakeProcess`.

    ``allow_unregistered`` is NEVER enabled — that would run every
    unmatched command for real, reopening the exact network hole this
    seam exists to close.
    """
    _harden_git_environment(monkeypatch, tmp_path / "home")
    # Register under BOTH the bare name and the resolved absolute path.
    # Production code resolves git via `shutil.which("git")` /
    # `_git_bin()`, so the invoked argv[0] is the absolute path; a bare
    # `"git"` registration does not reliably match an absolute-path
    # invocation in pytest-subprocess, so we pin the concrete path too.
    git_bins = ["git"]
    resolved = shutil.which("git")
    if resolved is not None:
        git_bins.append(resolved)
    with FakeProcess() as process:
        # `patch` (GNU patch) is the revert reverse-patch tool — a safe,
        # local, network-free binary. Pass it through so `revert` exercises
        # the real reverse-patch path. Registered under both the bare name
        # and the resolved absolute path (revert resolves via
        # `resolve_binary("patch")` → absolute path).
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
                # Bare `git <sub> [args...]`. `any(min=0)` matches the
                # zero-extra-arg form (`git status`) AND the trailing-args
                # form (`git status --porcelain -- tracked`) in one entry.
                process.pass_command(
                    [git_bin, sub, process.any(min=0)],
                    occurrences=_GIT_PASSTHROUGH_OCCURRENCES,
                )
                # `git -C <dir> <sub> [args...]` — the explicit-dir shape
                # used by the transition source-SHA probe + status subsystem.
                process.pass_command(
                    [git_bin, "-C", process.any(min=1, max=1), sub, process.any(min=0)],
                    occurrences=_GIT_PASSTHROUGH_OCCURRENCES,
                )
        yield process


# ---------------------------------------------------------------------------
# integration_env — minimal config-repo builder + run_verb helper
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntegrationEnv:
    """Handle to a seeded integration environment.

    Attributes let a test read post-parse observables directly:

    - ``home`` / ``state_dir`` — sandbox roots.
    - ``repo`` — the config-repo root (holds ``setforge.yaml`` + ``tracked/``).
    - ``config`` — path to the seeded ``setforge.yaml`` (pass to ``--config``).
    - ``profile`` — the seeded profile name.
    - ``run_verb`` — invoke a verb via ``CliRunner``; ``--config`` and a
      trailing ``--profile`` are injected automatically unless already present.
    - ``present_binary`` — mark a mocked binary (``gitleaks`` / ``claude`` /
      ``code``) as resolvable so its reconcile leg runs (against a FakeProcess
      argv the test registers) instead of warn-and-skipping. By DEFAULT all
      three resolve to absent, so the leg takes the warn-and-skip path and no
      stray subprocess fires.
    """

    home: Path
    state_dir: Path
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
        """Run ``setforge <argv>`` via ``CliRunner``.

        ``--config`` / ``--profile`` are injected by default (the common
        profile-scoped verbs). Verbs that reject one or both (``migrate``
        takes ``--config`` but no ``--profile``; ``init`` / ``fetch`` take
        neither) opt out via ``inject_config`` / ``inject_profile``.
        """
        return self._run(
            argv, inject_config=inject_config, inject_profile=inject_profile
        )

    def present_binary(self, name: str) -> Path:
        """Make ``name`` (gitleaks/claude/code) resolvable; return its fake path.

        The caller must still register the exact argv on the ``FakeProcess``
        (``integration_subprocess``) so the invocation is accounted for — an
        unregistered call still RAISES.
        """
        return self._present(name)

    def live(self, rel: str) -> Path:
        """Resolve a live-deploy path under the sandbox home."""
        return self.home / rel

    def tracked(self, rel: str) -> Path:
        """Resolve a tracked-source path under the seeded repo."""
        return self.repo / "tracked" / rel


# Minimal tracked corpus: one plain text file + one JSON file. A test that
# needs a richer surface writes it into ``env.repo`` itself — the builder
# stays minimal by contract (per-test complexity is the test's job).
_DEFAULT_TRACKED: dict[str, tuple[str, str]] = {
    # id -> (src rel path under tracked/, body)
    "note": ("text/note.txt", "hello from tracked\n"),
    "settings": ("json/settings.json", '{\n  "a": 1,\n  "b": "two"\n}\n'),
}

_DST_TEMPLATE = "~/.setforge_it/{src}"

# The three binaries this tier MOCKS (never passes through). Every one is
# defaulted to ABSENT so its reconcile leg warn-and-skips and no stray host
# subprocess fires; ``present_binary(name)`` selectively re-enables one. The
# ``SETFORGE_<NAME>_BIN`` override env var for each is cleared from the shape
# below so ``resolve_binary`` falls through to the sandboxed resolver. Single
# source of truth reused by BOTH the env-clear loop and the resolver.
_MOCKED_BINARIES: frozenset[str] = frozenset({"claude", "code", "gitleaks"})


def _write_config_repo(
    repo: Path,
    *,
    profile: str,
    tracked: dict[str, tuple[str, str]],
    git_init: bool,
) -> Path:
    """Seed a minimal valid config repo; return the ``setforge.yaml`` path.

    ``git_init=True`` makes the repo a real git checkout with a clean
    committed tree, so the verbs' source-clean gate exercises the REAL
    ``git status --porcelain`` path (and finds it clean). ``False`` leaves
    a plain PathSource dir — the gate short-circuits on the missing
    ``.git`` and no git runs at all.
    """
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
        # Seed the repo with the REAL git binary directly (bypassing the
        # FakeProcess seam, which isn't active at fixture-build time). The
        # verbs' own git calls run later, under the FakeProcess policy.
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
    """Return a builder for a seeded integration environment.

    The builder overrides EVERY data-dir constant the verbs resolve — not
    just ``$HOME`` — so a verb cannot escape the sandbox onto the dev host:

    - ``HOME`` + ``Path.home`` → ``tmp_path/home``.
    - ``SETFORGE_STATE_DIR`` (transition records) → ``tmp_path/state``.
    - ``MARKETPLACE_CACHE_ROOT`` (git-clone cache root) → ``tmp_path/mp``.
    - ``LOCAL_CONFIG_PATH`` (all three re-export sites) → ``tmp_path/local.yaml``.
    - the ``SETFORGE_<NAME>_BIN`` override env var for each mocked binary
      (see :data:`_MOCKED_BINARIES`) → cleared, so ``resolve_binary`` falls
      through to ``shutil.which`` and the tests control presence/absence
      explicitly.

    Parameters (all optional): ``profile`` name and ``tracked`` corpus map,
    and ``git_init`` to seed the repo as a real git checkout (default True,
    so the real source-clean gate runs). Seeds MINIMAL only.
    """
    home = tmp_path / "home"
    state_dir = tmp_path / "state"
    mp_cache = tmp_path / "mp"
    local_yaml = tmp_path / "local.yaml"
    for d in (home, state_dir, mp_cache):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_dir))
    monkeypatch.setattr(Path, "home", lambda: Path(os.environ["HOME"]))
    # Redirect the marketplace clone cache off the dev host.
    monkeypatch.setattr(
        "setforge.claude_marketplace_cache.MARKETPLACE_CACHE_ROOT", mp_cache
    )
    # LOCAL_CONFIG_PATH is re-exported from several modules; the project
    # conftest already redirects binaries/source/compare/cli.orphans to a
    # per-test tmp local.yaml, but pin it to OUR known path so a test can
    # read it back deterministically.
    for mod_attr in (
        "setforge.binaries.LOCAL_CONFIG_PATH",
        "setforge.source.LOCAL_CONFIG_PATH",
        "setforge.compare.LOCAL_CONFIG_PATH",
        "setforge.cli.orphans.LOCAL_CONFIG_PATH",
    ):
        monkeypatch.setattr(mod_attr, local_yaml)
    # Clear binary-override env so resolve_binary uses shutil.which and the
    # FakeProcess policy governs whether a call is allowed.
    for name in _MOCKED_BINARIES:
        monkeypatch.delenv(f"SETFORGE_{name.upper()}_BIN", raising=False)

    # The three MOCKED binaries default to ABSENT (see _MOCKED_BINARIES) so
    # each reconcile leg takes its warn-and-skip path and no stray subprocess
    # fires. `resolve_binary` would otherwise fall through to `shutil.which`,
    # which finds the DEV HOST's real claude/code/gitleaks and shells out to
    # them — the exact leak this tier guards against. `present_binary(name)`
    # selectively re-enables one. Every OTHER binary (notably `patch`, used
    # by the revert reverse-patch path, and `git`) delegates to the REAL
    # `resolve_binary` — those are safe local tools the tier passes through.
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
        # A real on-disk executable path so `_get_claude_bin` /
        # `_ensure_code` accept it; the FakeProcess seam intercepts the
        # actual exec, so the file body never runs.
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
            repo=repo,
            config=cfg,
            profile=profile,
            _run=_run,
            _present=_present,
        )

    return build
