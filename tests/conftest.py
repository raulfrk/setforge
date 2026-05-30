"""Shared pytest fixtures for the setforge test suite.

Two autouse fixtures here form a defense-in-depth around the
``~/.config/setforge/local.yaml`` stub-creation race that surfaces
when CliRunner tests share ``$HOME``:

- :func:`_isolated_local_config` redirects the ``LOCAL_CONFIG_PATH``
  module constants in ``setforge.binaries`` and ``setforge.source`` to
  a per-test ``tmp_path`` directory.
- :func:`_isolate_home` monkeypatches ``$HOME`` and ``pathlib.Path.home``
  to a per-test tmp directory. Catches any production code path that
  resolves ``Path.home()`` lazily (completion, snapshots, transitions,
  migrations) — without this, parallel workers would still race on the
  dev-host home for those code paths.

The :class:`FakeClaude` / :class:`FakeGit` fakes and their
``fake_claude`` / ``fake_git`` fixtures live here (rather than in a
single test file) so ``test_claude_plugins.py``,
``test_claude_marketplace_cache.py``, and ``test_cli_e2e.py`` can all
discover them via pytest's standard conftest mechanism.
"""

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from setforge import claude_marketplace_cache as _mp_cache
from setforge import claude_plugins as _cp
from setforge.config import (
    Config,
    Profile,
    ReconcilePolicy,
    ResolvedProfile,
    TrackedFile,
)


@pytest.fixture(autouse=True)
def _isolated_local_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Redirect ``LOCAL_CONFIG_PATH`` constants to a tmp path for every test.

    Two modules carry the constant — ``binaries`` for the ``binaries:``
    block and ``source`` for the ``source:`` block — and both must be
    redirected so neither leaks to ``~/.config/setforge/local.yaml`` on
    the dev host. Also resets ``source._cli_source`` to None so a test
    that sets it directly via ``set_cli_source`` (without going through
    a ``CliRunner`` callback) doesn't leak the value to later tests.
    """
    monkeypatch.setattr(
        "setforge.binaries.LOCAL_CONFIG_PATH",
        tmp_path / "local.yaml",
    )
    monkeypatch.setattr(
        "setforge.source.LOCAL_CONFIG_PATH",
        tmp_path / "local.yaml",
    )
    # setforge.compare imports LOCAL_CONFIG_PATH for orphan_ignore reads;
    # redirect that re-export too so tests don't read
    # the dev host's local.yaml mid-compare.
    monkeypatch.setattr(
        "setforge.compare.LOCAL_CONFIG_PATH",
        tmp_path / "local.yaml",
    )
    # setforge.cli.orphans imports LOCAL_CONFIG_PATH for orphan_ignore
    # writes; redirect that re-export too.
    monkeypatch.setattr(
        "setforge.cli.orphans.LOCAL_CONFIG_PATH",
        tmp_path / "local.yaml",
    )
    monkeypatch.setattr("setforge.source._cli_source", None)


@pytest.fixture(autouse=True)
def _isolate_home(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path | None:
    """Redirect ``$HOME`` + ``Path.home()`` to a per-test tmp directory.

    Belt-and-suspenders against parallel-worker races on the shared
    ``~/.config/setforge/local.yaml`` stub that the Typer root callback
    writes via :func:`setforge.binaries.ensure_local_config_stub`.
    :func:`_isolated_local_config` already redirects the
    ``LOCAL_CONFIG_PATH`` module constants — but any other production
    code path that resolves ``Path.home()`` lazily (completion,
    snapshots, transitions, migrations) would still race on the real
    dev-host home. Monkeypatching at the ``Path.home`` level catches
    every reachable site.

    Skip on tests carrying the ``no_home_isolation`` marker — used by
    tests that legitimately need the live ``$HOME``. The marker is a
    forward escape hatch; no test ships with it today.

    The home dir lives under a per-test ``tmp_path_factory.mktemp``
    directory, NOT under the test's ``tmp_path``. This matters because
    some tests pass their ``tmp_path`` to production code and then
    assert it is empty (e.g.
    ``test_claude_marketplace_cache.test_resolve_marketplace_source_regular_returns_input``
    on its ``cache_root=tmp_path`` argument). If the autouse fixture
    seeded a subdir into ``tmp_path``, those assertions would
    false-fail. ``tmp_path_factory`` gives a separate per-test dir
    that doesn't collide with anything the test author wrote.

    The fixture returns the redirected home so a test can request it
    via parameter and inspect the contents of the sandboxed
    ``~/.config/setforge/`` directly.
    """
    if request.node.get_closest_marker("no_home_isolation") is not None:
        return None
    home = tmp_path_factory.mktemp("_autoisolated_home")
    monkeypatch.setenv("HOME", str(home))
    # ``Path.home`` is monkeypatched to read ``$HOME`` dynamically so a
    # downstream fixture that does ``monkeypatch.setenv("HOME", ...)``
    # still propagates to ``Path.home()`` calls. A captured-value lambda
    # (``lambda: home``) would ignore later env changes and silently break
    # tests that re-isolate HOME for their own purposes (e.g.
    # ``tests/test_completion.py:home``).
    monkeypatch.setattr(Path, "home", lambda: Path(os.environ["HOME"]))
    return home


@pytest.fixture(autouse=True)
def _reset_claude_bin_cache() -> None:
    """Clear the ``_get_claude_bin`` lru_cache before every test.

    ``setforge.claude_plugins._get_claude_bin`` is wrapped in
    :func:`functools.lru_cache` for process-lifetime memoization in
    production. The ``fake_claude`` / ``fake_git`` fixtures call
    ``cache_clear()`` at fixture-entry to avoid serving a stale fake
    path; but a test that does NOT request those fixtures still
    inherits whatever path was last cached by a prior test (typically
    ``/usr/local/bin/claude`` — the fake_claude factory's hardcoded
    value). On a host where that path is not a real binary the next
    install / reconcile flow surfaces ``FileNotFoundError`` from the
    eventual ``subprocess.run`` call. Surfaced by the conftest
    split (re-ordered test discovery exposed the latent leak); fixing
    here keeps every later test on a clean slate regardless of which
    fixtures the preceding test happened to use.
    """
    _cp._get_claude_bin.cache_clear()


@pytest.fixture(autouse=True)
def _suppress_fresh_host_welcome(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force :func:`setforge.cli._welcome.is_fresh_host` to return ``False``.

    Every CliRunner ``install`` invocation in the inner test ring runs
    under :func:`_isolate_home` with a transitions-free sandboxed HOME.
    Without this fixture, the fresh-host welcome gate
    would either raise :class:`WelcomeRequiresInteractive` (non-TTY
    CliRunner stdin) or reject ``--auto=*`` for every install test in
    the suite.

    We monkeypatch the symbol directly rather than planting a transition
    record because many tests redirect ``SETFORGE_STATE_DIR`` themselves
    (e.g. tests/test_cli_revert.py) and would clobber a planted marker.
    The symbol-patch overrides the welcome gate regardless of where the
    state dir resolves to.

    The welcome's behavior under fresh-host conditions is exercised
    explicitly by ``tests/test_welcome.py``, which opts out via the
    ``fresh_host`` marker.
    """
    if request.node.get_closest_marker("fresh_host") is not None:
        return

    # Patch BOTH the source module and ``setforge.cli.install``'s
    # import-site binding. ``install.py`` does
    # ``from setforge.cli._welcome import is_fresh_host``, so the
    # source-module patch alone would not reach install's bound name;
    # patching the import-site keeps the existing install gate
    # suppressed. The source-module patch covers any future call site
    # that imports ``is_fresh_host`` (e.g. a new ``setforge status``
    # branch) — single point of truth for "this test ring treats the
    # host as non-fresh".
    def _force_non_fresh() -> bool:
        return False

    monkeypatch.setattr("setforge.cli._welcome.is_fresh_host", _force_non_fresh)
    monkeypatch.setattr("setforge.cli.install.is_fresh_host", _force_non_fresh)


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: Sequence[pytest.Item],
) -> None:
    """Register custom markers for ``--strict-markers``.

    Registration via ``config.addinivalue_line`` keeps
    ``pytest --strict-markers`` happy without forcing every test author
    to remember the marker name in pyproject.toml. The collection hook
    fires once per session, so the registration cost is negligible.

    The ``fresh_host`` marker is registered in ``pyproject.toml`` —
    keeping a single registration site avoids drift between the two
    descriptions.
    """
    del items  # collection hook accepts items; we don't filter here.
    config.addinivalue_line(
        "markers",
        "no_home_isolation: opt this test out of the _isolate_home autouse fixture.",
    )


# ---------------------------------------------------------------------------
# Shared Config / ResolvedProfile builders
# ---------------------------------------------------------------------------


def _make_config(
    *,
    marketplaces: dict | None = None,
    claude_plugins: dict | None = None,
) -> Config:
    """Build a minimal Config for reconcile / sync-cache tests."""
    return Config(
        tracked_files={"d": TrackedFile(src=Path("tracked/x"), dst="~/x")},
        marketplaces=marketplaces or {},
        claude_plugins=claude_plugins or {},
        profiles={"default": Profile(tracked_files=["d"])},
    )


def _make_resolved(
    *,
    claude_plugins: list[str] | None = None,
    plugins_reconcile: ReconcilePolicy = ReconcilePolicy.ADDITIVE,
) -> ResolvedProfile:
    return ResolvedProfile(
        claude_plugins=claude_plugins or [],
        plugins_reconcile=plugins_reconcile,
    )


# ---------------------------------------------------------------------------
# Fake ``claude`` driver + ``fake_claude`` fixture
# ---------------------------------------------------------------------------


class FakeClaude:
    """In-memory simulation of ``claude plugin`` commands.

    Non-claude argv (e.g. ``patch``, ``git``) is forwarded to a captured
    ``_real_run`` so the transitions layer's reverse-patch step works
    even when ``fake_claude`` is the only fixture wired in. The
    ``fake_claude`` fixture captures ``subprocess.run`` BEFORE
    monkeypatching it, mirroring the delegation pattern used by
    :class:`tests.test_cli_e2e.FakeCode` for the co-resident fixture
    case.
    """

    def __init__(
        self,
        *,
        marketplaces: list[dict] | None = None,
        plugins: list[dict] | None = None,
    ) -> None:
        # Each marketplace entry: {"name": str, "source": str, ...}
        self._marketplaces: list[dict] = list(marketplaces or [])
        # Each plugin entry: {"id": "<name>@<mp>", "enabled": bool, ...}
        self._plugins: list[dict] = list(plugins or [])
        self.calls: list[list[str]] = []
        # Captured pre-monkeypatch ``subprocess.run`` for non-claude argv.
        # ``None`` until the ``fake_claude`` fixture wires it; with the
        # delegate unset, non-claude argv raises (today's behavior).
        self._real_run: Any = None

    def run(self, args, **kwargs: Any) -> subprocess.CompletedProcess:
        # Forward non-claude invocations (patch / git etc.) to the
        # captured real subprocess.run. Argv[0] is the binary path
        # (str already at this point).
        if args and Path(args[0]).name != "claude":
            if self._real_run is not None:
                return self._real_run(args, **kwargs)
            raise AssertionError(f"unexpected non-claude invocation: {args!r}")

        self.calls.append(list(args))
        cmd = args[1:]  # ["plugin", "marketplace", "list", "--json"]

        if cmd == ["plugin", "marketplace", "list", "--json"]:
            return subprocess.CompletedProcess(
                args, 0, json.dumps(self._marketplaces), ""
            )
        if cmd == ["plugin", "list", "--json"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(self._plugins), "")
        if len(cmd) >= 3 and cmd[:2] == ["plugin", "marketplace"] and cmd[2] == "add":
            # claude plugin marketplace add <source-url>
            # Production claude derives the marketplace name from the
            # repo's marketplace.json. For test fidelity we derive name
            # from the last path component of the source URL — matches
            # the convention real marketplaces follow (the marketplace
            # name == the repo basename). Necessary so a later
            # ``marketplace remove <name>`` matches the entry recorded
            # here (revert flow uses the declared YAML name).
            source_url = cmd[3]
            name = source_url.rsplit("/", 1)[-1]
            self._marketplaces.append({"name": name, "source": source_url})
            return subprocess.CompletedProcess(args, 0, "", "")
        if (
            len(cmd) >= 3
            and cmd[:2] == ["plugin", "marketplace"]
            and cmd[2] == "remove"
        ):
            name = cmd[3]
            self._marketplaces = [
                m for m in self._marketplaces if m.get("name") != name
            ]
            return subprocess.CompletedProcess(args, 0, "", "")
        if (
            len(cmd) >= 3
            and cmd[:2] == ["plugin", "marketplace"]
            and cmd[2] == "update"
        ):
            return subprocess.CompletedProcess(args, 0, "", "")
        if len(cmd) >= 3 and cmd[:2] == ["plugin", "install"]:
            plugin_arg = cmd[2]  # "name@marketplace" or similar
            # "--scope=user" may follow
            if not any(p["id"] == plugin_arg for p in self._plugins):
                # Match production: install adds to installed_plugins.json
                # without touching enabledPlugins. Plugin lands disabled
                # until 'enable' runs.
                self._plugins.append(
                    {"id": plugin_arg, "enabled": False, "scope": "user"}
                )
            # Re-install of an already-installed plugin: no-op on enabled
            # state (production claude doesn't touch enabledPlugins on
            # re-install).
            return subprocess.CompletedProcess(args, 0, "", "")
        if len(cmd) >= 3 and cmd[:2] == ["plugin", "enable"]:
            name = cmd[2]
            for p in self._plugins:
                if p["id"] == name:
                    p["enabled"] = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if len(cmd) >= 3 and cmd[:2] == ["plugin", "disable"]:
            name = cmd[2]
            for p in self._plugins:
                if p["id"] == name:
                    p["enabled"] = False
            return subprocess.CompletedProcess(args, 0, "", "")
        if len(cmd) >= 3 and cmd[:2] == ["plugin", "uninstall"]:
            # Mirror production: `claude plugin uninstall <id>` removes
            # the plugin entry from installed_plugins.json entirely.
            name = cmd[2]
            self._plugins = [p for p in self._plugins if p["id"] != name]
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected claude invocation: {args!r}")

    # Convenience query helpers
    def install_args(self) -> list[str]:
        return [c[3] for c in self.calls if c[1:3] == ["plugin", "install"]]

    def enable_args(self) -> list[str]:
        return [c[3] for c in self.calls if c[1:3] == ["plugin", "enable"]]

    def disable_args(self) -> list[str]:
        return [c[3] for c in self.calls if c[1:3] == ["plugin", "disable"]]

    def uninstall_args(self) -> list[str]:
        return [c[3] for c in self.calls if c[1:3] == ["plugin", "uninstall"]]

    def mp_add_args(self) -> list[str]:
        return [
            c[4]
            for c in self.calls
            if len(c) > 4 and c[1:4] == ["plugin", "marketplace", "add"]
        ]

    def installed_state(self) -> dict[str, dict]:
        """Snapshot of installed plugins keyed by plugin id.

        In-memory analog of ``~/.claude/installed_plugins.json``. Tests
        assert against this rather than reaching into the private
        ``_plugins`` list.
        """
        return {p["id"]: dict(p) for p in self._plugins}

    def marketplaces_state(self) -> list[dict]:
        """Snapshot of registered marketplaces, in registration order.

        In-memory analog of ``claude plugin marketplace list --json``
        output. Returns shallow-copied entries so test mutations cannot
        leak into the fake's internal ``_marketplaces`` list. Tests
        assert against this rather than reaching into the private
        attribute.
        """
        return [dict(m) for m in self._marketplaces]


@pytest.fixture
def fake_claude(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeClaude]:
    """Return a factory that wires :class:`FakeClaude` into ``claude_plugins``.

    The factory builds a fresh fake from optional ``marketplaces`` /
    ``plugins`` snapshots and patches ``resolve_binary`` +
    ``subprocess.run`` on ``setforge.claude_plugins`` so every claude-CLI
    invocation hits the fake. The pre-monkeypatch ``subprocess.run`` is
    captured into ``fake._real_run`` so non-claude argv (e.g. ``patch``,
    ``git``) keeps reaching the real subprocess layer.
    """

    def factory(
        *,
        marketplaces: list[dict] | None = None,
        plugins: list[dict] | None = None,
    ) -> FakeClaude:
        fake = FakeClaude(marketplaces=marketplaces, plugins=plugins)
        # Snapshot the pre-monkeypatch ``subprocess.run`` so FakeClaude
        # can forward non-claude argv (patch / git etc. used by the
        # transitions revert path) to the real function. Capturing
        # ``subprocess.run`` here — before the ``monkeypatch.setattr``
        # below — preserves the delegate even when the e2e test path
        # exercises ``apply_patch_reverse``.
        fake._real_run = subprocess.run
        monkeypatch.setattr(
            "setforge.claude_plugins.resolve_binary",
            lambda name: Path("/usr/local/bin/claude") if name == "claude" else None,
        )
        monkeypatch.setattr("setforge.claude_plugins.subprocess.run", fake.run)
        _cp._get_claude_bin.cache_clear()
        return fake

    return factory


# ---------------------------------------------------------------------------
# Fake ``git`` driver + ``fake_git`` fixture
# ---------------------------------------------------------------------------


class FakeGit:
    """In-memory simulation of ``git clone`` / ``git fetch`` / ``git reset``.

    Records every invocation in ``calls`` and tracks per-cache origin URLs
    in ``cloned`` so tests can assert on the exact sequence. Mirrors
    :class:`FakeClaude`'s design: forwarded non-git argv (e.g. ``claude``)
    delegates to ``_real_run`` so the same monkeypatch can host both
    fakes when needed.

    A repo is "successfully cloned" if it appears in ``known_repos``;
    cloning an unknown repo raises ``CalledProcessError`` so tests can
    exercise the cache-miss + offline path.
    """

    def __init__(
        self,
        *,
        known_repos: set[str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.cloned: dict[Path, str] = {}
        # ``None`` means "no restriction" — every clone succeeds. A set
        # (even an empty one) means strict membership: only listed repos
        # clone successfully; anything else raises CalledProcessError so
        # tests can exercise the cache-miss + offline path. An empty set
        # therefore means "every clone fails."
        self.known_repos: set[str] | None = known_repos
        self._real_run: Callable[..., Any] | None = None

    def run(self, args, **kwargs: Any) -> subprocess.CompletedProcess:
        if not args or Path(args[0]).name != "git":
            if self._real_run is not None:
                return self._real_run(args, **kwargs)
            raise AssertionError(f"unexpected non-git invocation: {args!r}")
        self.calls.append(list(args))
        cmd = args[1:]

        # git clone -- <repo> <dest>
        # Defense-in-depth: `_clone_marketplace` always passes the `--`
        # separator before source.repo to prevent argv flag injection.
        if len(cmd) >= 4 and cmd[0] == "clone" and cmd[1] == "--":
            repo = cmd[2]
            dest = Path(cmd[3])
            if self.known_repos is not None and repo not in self.known_repos:
                raise subprocess.CalledProcessError(
                    128, args, stderr=f"fatal: repository '{repo}' not found"
                )
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir(exist_ok=True)
            self.cloned[dest] = repo
            return subprocess.CompletedProcess(args, 0, "", "")

        # git -C <dir> remote get-url origin
        if (
            len(cmd) >= 5
            and cmd[0] == "-C"
            and cmd[2:5] == ["remote", "get-url", "origin"]
        ):
            cache_dir = Path(cmd[1])
            url = self.cloned.get(cache_dir, "")
            return subprocess.CompletedProcess(args, 0, url + "\n", "")

        # git -C <dir> fetch origin
        if len(cmd) >= 4 and cmd[0] == "-C" and cmd[2:4] == ["fetch", "origin"]:
            return subprocess.CompletedProcess(args, 0, "", "")

        # git -C <dir> reset --hard origin/HEAD
        if (
            len(cmd) >= 5
            and cmd[0] == "-C"
            and cmd[2:5] == ["reset", "--hard", "origin/HEAD"]
        ):
            return subprocess.CompletedProcess(args, 0, "", "")

        raise AssertionError(f"unexpected git invocation: {args!r}")

    def clone_count(self) -> int:
        return sum(1 for c in self.calls if c[1:2] == ["clone"])


@pytest.fixture
def fake_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., FakeGit]:
    """Wire :class:`FakeGit` into ``claude_marketplace_cache``.

    Returns a factory: ``fake = fake_git(known_repos={"a/b"})``. Sets
    ``shutil.which`` to report ``git`` resolvable, redirects the cache
    root into ``tmp_path``, and clears the ``_get_claude_bin`` lru-cache
    so tests that combine the two fakes don't see stale binary state.
    Patches target the ``claude_marketplace_cache`` module (after the
    split) — the cache helpers now own the ``shutil`` / ``subprocess``
    namespace attributes that previously sat on ``claude_plugins``.
    """

    def factory(*, known_repos: set[str] | None = None) -> FakeGit:
        fake = FakeGit(known_repos=known_repos)
        fake._real_run = subprocess.run
        cache_root = tmp_path / "marketplaces"
        monkeypatch.setattr(_mp_cache, "MARKETPLACE_CACHE_ROOT", cache_root)
        monkeypatch.setattr(
            "setforge.claude_marketplace_cache.shutil.which",
            lambda name: "/usr/bin/git" if name == "git" else None,
        )
        # If a prior test wired subprocess.run via fake_claude, this
        # overwrite is fine — FakeGit's run delegates non-git argv to
        # _real_run (which here is the un-monkeypatched subprocess.run
        # captured BEFORE this setattr).
        monkeypatch.setattr(
            "setforge.claude_marketplace_cache.subprocess.run", fake.run
        )
        _cp._get_claude_bin.cache_clear()
        return fake

    return factory


# ---------------------------------------------------------------------------
# Host-local install-mode helpers
# ---------------------------------------------------------------------------


def _local_clone_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point binaries.LOCAL_CONFIG_PATH at a local.yaml selecting LOCAL_CLONE."""
    from setforge import binaries as bin_mod

    local_path = tmp_path / "local.yaml"
    local_path.write_text("claude:\n  install_mode: local-clone\n")
    monkeypatch.setattr(bin_mod, "LOCAL_CONFIG_PATH", local_path)


def _regular_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point binaries.LOCAL_CONFIG_PATH at a local.yaml selecting REGULAR."""
    from setforge import binaries as bin_mod

    local_path = tmp_path / "local.yaml"
    local_path.write_text("claude:\n  install_mode: regular\n")
    monkeypatch.setattr(bin_mod, "LOCAL_CONFIG_PATH", local_path)
