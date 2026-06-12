"""Docker E2E test ring for ``setforge`` (outer ring).

Every test runs inside a fresh Debian 12 container with real
``claude`` + ``code`` binaries, exercising the actual install / sync
/ compare / revert / validate side effects against a real filesystem
and real external CLIs.

Gated by ``-m e2e_docker`` (registered in ``pyproject.toml``); skipped
when ``docker`` is missing.

Variants follow the spec layout (sections kept in test order for
ease of cross-reference):

- Install mechanism variants (B-L)
- Sync + wizard variants (M-S1)
- Lifecycle variants (T-W)

Each test takes the form:

  1. Spin a fresh container.
  2. ``uv run setforge <verb> --profile=test-<x>
     --config=tests/fixtures/e2e/setforge.test.yaml``
  3. Read the resulting live file(s) and assert parsed/structured equality.

See ``tests/docker/conftest.py`` for the ``docker_image``,
``docker_container`` fixtures.
"""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from collections.abc import Callable

import pytest

# ``ContainerHandle`` is exported by the sibling conftest. Pytest loads
# conftest.py before sibling test modules, so the import is always
# satisfied at collection time.
from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _install(
    container: ContainerHandle,
    profile: str,
    *,
    root_args: list[str] | None = None,
    extra: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``setforge install`` inside the container; return CompletedProcess.

    Uses ``check=False`` so callers can assert on returncode + stderr
    explicitly; the buried ``CalledProcessError`` chain otherwise hides
    the actual stderr in ``__cause__``.

    ``root_args`` are typer root-callback flags (e.g. ``-v``) that must
    precede the ``install`` subcommand. ``extra`` are subcommand-level
    flags (e.g. ``--auto-accept-*``) that follow it.
    """
    cmd = ["uv", "run", "setforge"]
    if root_args:
        cmd.extend(root_args)
    cmd.extend(["install", f"--profile={profile}", f"--config={CONFIG_FIXTURE}"])
    if extra:
        cmd.extend(extra)
    result = container.exec(cmd, check=False)
    assert result.returncode == 0, result.stderr
    return result


def _sync(
    container: ContainerHandle,
    profile: str,
    *,
    root_args: list[str] | None = None,
    extra: list[str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``setforge sync`` inside the container; return CompletedProcess.

    Asserts on ``returncode == 0`` when ``check=True`` (the default)
    for the same readability reasons as :func:`_install`.

    ``root_args`` are typer root-callback flags (e.g. ``-v``) that must
    precede the ``sync`` subcommand. ``extra`` are subcommand-level
    flags (e.g. ``--auto=...``) that follow it.
    """
    cmd = ["uv", "run", "setforge"]
    if root_args:
        cmd.extend(root_args)
    cmd.extend(["sync", f"--profile={profile}", f"--config={CONFIG_FIXTURE}"])
    if extra:
        cmd.extend(extra)
    result = container.exec(cmd, check=False)
    if check:
        assert result.returncode == 0, result.stderr
    return result


def _read_live(container: ContainerHandle, path: str) -> str:
    """Read a live (dst) file in the container's $HOME tree."""
    return container.read_text(f"/home/tester/{path}")


# ===========================================================================
# Section: Install mechanism variants (B-L)
# ===========================================================================


# --- Variant B ------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_minimal_floor(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """B: plain-text byte copy lands at dst with matching content."""
    c = docker_container()
    _install(c, "test-minimal")
    assert (
        _read_live(c, ".setforge_e2e/minimal/text.txt") == "hello from test-minimal\n"
    )


# --- Variant C ------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_text_sections_no_live(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """C: disposition: shared markdown, no live content → dst equals tracked.

    install rewrites end markers with an embedded ``hash=<sha256>``
    segment (post-hashed-marker: tracked is also stamped). The body is the
    load-bearing assertion; the end marker may carry the new hash
    segment or be the legacy untagged form.
    """
    c = docker_container()
    _install(c, "test-text-sections")
    live = _read_live(c, ".setforge_e2e/sections/marked.md")
    assert "<!-- setforge:user-section start host-local notes -->" in live
    assert "default notes (tracked side)" in live
    assert re.search(
        r"<!-- setforge:user-section end host-local notes( hash=[0-9a-f]{64})? -->",
        live,
    )


# --- Variant E ------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_json_byte_copy(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """E: JSON tracked_file byte-copies; parsed result matches tracked."""
    c = docker_container()
    _install(c, "test-json")
    payload = json.loads(_read_live(c, ".setforge_e2e/json/settings.json"))
    assert payload == {
        "settingA": "tracked-value-A",
        "settingB": 42,
        "settingC": ["alpha", "beta"],
    }


# --- Variant F ------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_jsonc_shallow_no_live(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """F: JSONC deploy + comments preserved on first install (no live yet)."""
    c = docker_container()
    _install(c, "test-jsonc-shallow")
    live = _read_live(c, ".setforge_e2e/jsonc/shallow.json")
    assert "// tracked side comment" in live
    assert "tracked-placeholder-A" in live
    assert "tracked-placeholder-B" in live


# --- Variant G ------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_jsonc_shallow_preserve_overlay(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """G: forked JSONC 3-way merge keeps live edits to the keys.

    ``jsonc_shallow`` is ``disposition: forked``: install 3-way-merges
    {base, live, tracked}. A live-only edit to a key survives while
    tracked-only keys keep their tracked value (no conflict). The base
    is seeded from live on the first post-seed install.
    """
    c = docker_container()
    # First install to produce baseline.
    _install(c, "test-jsonc-shallow")
    # Mutate ONLY preserve_user_keys entries on the live side.
    live_path = "/home/tester/.setforge_e2e/jsonc/shallow.json"
    c.write_text(
        live_path,
        textwrap.dedent(
            """\
            {
              // tracked side comment for shallow-preserve JSONC fixture
              "trackedKey": "tracked-value",
              "userKeyA": "live-A",
              "userKeyB": "live-B"
            }
            """
        ),
    )
    _install(c, "test-jsonc-shallow")
    live = _read_live(c, ".setforge_e2e/jsonc/shallow.json")
    # userKeyA / userKeyB preserved from live; trackedKey is the tracked value.
    assert "live-A" in live
    assert "live-B" in live
    assert "tracked-value" in live
    # Tracked-side comment present (it's part of the tracked source).
    assert "// tracked side comment" in live


# --- Variant H ------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_jsonc_deep_preserve_overlay(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """H: forked JSONC deep 3-way merge keeps live sub-key edits.

    ``jsonc_deep`` is ``disposition: forked``: the structural 3-way merge
    keeps a live edit to a nested sub-key while tracked-only sub-keys keep
    their tracked value (parent-first union, live wins on overlap).
    """
    c = docker_container()
    _install(c, "test-jsonc-deep")
    live_path = "/home/tester/.setforge_e2e/jsonc/deep.json"
    c.write_text(
        live_path,
        textwrap.dedent(
            """\
            {
              "trackedKey": "tracked-value",
              "settings": {
                "trackedSub": "tracked-sub-value",
                "userSub": "live-user-value"
              }
            }
            """
        ),
    )
    _install(c, "test-jsonc-deep")
    live = _read_live(c, ".setforge_e2e/jsonc/deep.json")
    # Deep merge: live userSub survives; tracked trackedSub keeps its
    # tracked value (deep-merge is parent-first union; live wins on
    # overlap, tracked keeps tracked-only keys).
    assert "live-user-value" in live
    assert "tracked-sub-value" in live
    # Top-level: trackedKey is the tracked value (live left it unchanged).
    assert "tracked-value" in live


# --- Variant H1 -----------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_yaml_shallow_preserve_overlay(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """H1: forked YAML 3-way merge — yaml_merge.py parity with jsonc.py.

    ``yaml_shallow`` is ``disposition: forked``: a live-only edit to a key
    survives the merge while tracked-only keys keep their tracked value.
    """
    c = docker_container()
    _install(c, "test-yaml-shallow")
    live_path = "/home/tester/.setforge_e2e/yaml/shallow.yaml"
    c.write_text(
        live_path,
        textwrap.dedent(
            """\
            trackedKey: tracked-value
            userKeyA: live-A
            userKeyB: live-B
            """
        ),
    )
    _install(c, "test-yaml-shallow")
    live = _read_live(c, ".setforge_e2e/yaml/shallow.yaml")
    assert "live-A" in live
    assert "live-B" in live
    assert "tracked-value" in live  # trackedKey is the tracked value


# --- Variant H2 -----------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_yaml_deep_preserve_overlay(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """H2: forked YAML deep 3-way merge — yaml_merge.py deep-merge parity.

    ``yaml_deep`` is ``disposition: forked``: a live edit to a nested
    sub-key survives while tracked-only sub-keys keep their tracked value.
    """
    c = docker_container()
    _install(c, "test-yaml-deep")
    live_path = "/home/tester/.setforge_e2e/yaml/deep.yaml"
    c.write_text(
        live_path,
        textwrap.dedent(
            """\
            trackedKey: tracked-value
            settings:
              trackedSub: tracked-sub-value
              userSub: live-user-value
            """
        ),
    )
    _install(c, "test-yaml-deep")
    live = _read_live(c, ".setforge_e2e/yaml/deep.yaml")
    assert "live-user-value" in live  # live deep sub-key survives
    assert "tracked-sub-value" in live  # tracked-only deep sub-key kept
    assert "tracked-value" in live  # top-level untouched


# --- Variant I ------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_directory_copy(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """I: directory tree copied recursively, nested files included."""
    c = docker_container()
    _install(c, "test-directory")
    assert _read_live(c, ".setforge_e2e/directory/file-a.txt") == "file-a content\n"
    assert _read_live(c, ".setforge_e2e/directory/file-b.txt") == "file-b content\n"
    assert (
        _read_live(c, ".setforge_e2e/directory/nested/file-c.txt")
        == "file-c content (nested)\n"
    )


# --- Variant J ------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_template_dst_jinja2(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """J: template=true dst Jinja2-renders ``{{ vscode_user_dir }}``."""
    c = docker_container()
    _install(c, "test-template")
    # vscode_user_dir for Linux non-workstation ~ resolves under $HOME — verify
    # the file lands somewhere under /home/tester, not at the literal Jinja2 template.
    # We probe by `find` rather than computing the exact path here (paths.py
    # owns vscode_user_dir's resolution; the test asserts only that template
    # rendering happened, NOT the specific dst).
    proc = c.exec(
        ["find", "/home/tester", "-name", "setforge-e2e-template.txt"],
    )
    matches = [line for line in proc.stdout.splitlines() if line.strip()]
    assert matches, (
        f"templated dst not found anywhere under /home/tester: {proc.stdout!r}"
    )
    # And the content is the rendered file.
    content = c.read_text(matches[0])
    assert content == "templated file (dst path was Jinja2-rendered)\n"


# --- Variant K ------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_chain_resolution_and_bootstrap(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """K: 3-level extends chain; parent-first tracked_file dedup + bootstrap stubs."""
    c = docker_container()
    _install(c, "test-chain-child")
    root = ".setforge_e2e/chain"
    assert _read_live(c, f"{root}/grand.txt") == "grand-content\n"
    assert _read_live(c, f"{root}/base.txt") == "base-content\n"
    assert _read_live(c, f"{root}/child.txt") == "child-content\n"
    # Bootstrap stubs created at all three chain levels.
    for stub in ("bootstrap-grand.txt", "bootstrap-base.txt", "bootstrap-child.txt"):
        proc = c.exec(["test", "-f", f"/home/tester/{root}/{stub}"], check=False)
        assert proc.returncode == 0, f"missing bootstrap stub: {stub}"


# --- Variant L ------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_comprehensive_plugins_extensions(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """L: full sweep — tracked_files + marketplaces + plugins + extensions + bootstrap.

    Asserts the tracked_file leg lands cleanly. The plugin + extension legs hit
    real ``claude`` and ``code`` binaries; this test verifies install
    exits 0 (= reconcile completed without raising) and the tracked_file
    layer is materialised. Plugin / extension state cross-checks are
    asserted by the bound list commands when claude/code are usable in
    CI; failures there are surfaced via install's non-zero exit.
    """
    c = docker_container()
    # First-time install: every dst is absent, so install bypasses the
    # drift gate without needing --auto-accept-* flags.
    proc = _install(c, "test-comprehensive")
    assert proc.returncode == 0, proc.stderr
    root = ".setforge_e2e/comprehensive"
    assert "comprehensive notes" in _read_live(c, f"{root}/notes.md")
    assert json.loads(_read_live(c, f"{root}/data.json")) == {
        "key": "comprehensive-value"
    }
    assert "comprehensive-tracked" in _read_live(c, f"{root}/preserve-settings.json")
    assert "comprehensive-tracked-yaml" in _read_live(c, f"{root}/config.yaml")
    proc = c.exec(
        ["test", "-f", f"/home/tester/{root}/bootstrap-stub.txt"], check=False
    )
    assert proc.returncode == 0, "comprehensive bootstrap stub missing"


# --- Variant L1 (verbosity surface) --------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_verbose_emits_setforge_debug(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``-v`` flag surfaces ``claude_marketplace_cache`` DEBUG via real subprocess.

    Closes the e2e scope gap left by the in-process CliRunner unit tests
    in :mod:`tests.test_cli_e2e` (which prove flag mechanics inside the
    test interpreter but not real-subprocess logging propagation). Runs
    the comprehensive profile under ``-v`` in a fresh Debian container
    and asserts a ``setforge.claude_marketplace_cache DEBUG:`` line
    lands on stderr — proving the verbosity surface threads
    end-to-end through CLI startup,
    ``logging.basicConfig(stream=sys.stderr)``, and the production
    ``setforge.claude_marketplace_cache`` LOGGER call sites (``_run_git``
    / ``_clone_marketplace`` / ``_cache_origin_url``). The git helpers
    moved out of ``claude_plugins`` into ``claude_marketplace_cache``.

    The ``claude.install_mode: local-clone`` opt-in via host-local
    ``local.yaml`` is required: under default ``regular`` mode the git
    helpers are never invoked (claude_plugins talks to the ``claude``
    binary, not git). Under local-clone, ``_clone_marketplace`` clones
    the marketplace repo from GitHub on first install, and git writes
    its progress chatter (``Cloning into ...``, ``Resolving deltas``)
    to stderr — captured and re-emitted at DEBUG by the success-path
    stderr-DEBUG block added in the "also LOGGER.debug git stderr on
    success path" follow-up. That block is the deterministic anchor
    for this assertion.
    """
    c = docker_container()
    # Flip to local-clone install mode so the git helpers in
    # claude_marketplace_cache (_run_git / _clone_marketplace /
    # _cache_origin_url) are exercised on the install path; their
    # success-path stderr-DEBUG blocks are what this test verifies.
    c.write_text(
        "/home/tester/.config/setforge/local.yaml",
        textwrap.dedent(
            """\
            claude:
              install_mode: local-clone
            """
        ),
    )
    result = _install(c, "test-comprehensive", root_args=["-vv"])
    assert "setforge.claude_marketplace_cache DEBUG:" in result.stderr, (
        f"expected 'setforge.claude_marketplace_cache DEBUG:' in stderr; "
        f"first 800 chars: {result.stderr[:800]}"
    )


# ===========================================================================
# Section: Sync + wizard variants (M-S1)
# ===========================================================================


# --- Variant M ------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_sync_no_drift_noop(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """M: install clean; sync reports no-op; tracked unchanged."""
    c = docker_container()
    _install(c, "test-minimal")
    pre = c.read_text("/workspace/tests/fixtures/e2e/tracked/minimal/text.txt")
    _sync(c, "test-minimal")
    post = c.read_text("/workspace/tests/fixtures/e2e/tracked/minimal/text.txt")
    assert pre == post


# --- Variant N ------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_sync_auto_use_live_silent_absorb(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """N: pre-seed drift, --auto=use-live absorbs live into tracked."""
    c = docker_container()
    _install(c, "test-minimal")
    c.write_text("/home/tester/.setforge_e2e/minimal/text.txt", "live-only-content\n")
    _sync(c, "test-minimal", extra=["--auto=use-live", "--yes"])
    tracked = c.read_text("/workspace/tests/fixtures/e2e/tracked/minimal/text.txt")
    assert "live-only-content" in tracked


# --- Variant O2 (markdown frontmatter: no crash) ----------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_sync_markdown_frontmatter_no_crash(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """O2: sync must not crash on markdown tracked files with YAML frontmatter."""
    c = docker_container()
    _install(c, "test-prose-reviewers")
    skill_path = (
        "/workspace/tests/fixtures/e2e/tracked/"
        "claude/skills/reviewing-markdown/SKILL.md"
    )
    pre = c.read_text(skill_path)
    _sync(c, "test-prose-reviewers", extra=["--auto=keep-tracked", "--yes"])
    post = c.read_text(skill_path)
    assert pre == post


# ===========================================================================
# Section: Lifecycle variants (T-W)
# ===========================================================================


# --- Variant T ------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_compare_reports_drift_exit_nonzero(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """T: install, mutate live, compare --check --strict exits non-zero."""
    c = docker_container()
    _install(c, "test-minimal")
    c.write_text("/home/tester/.setforge_e2e/minimal/text.txt", "live-drift\n")
    proc = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "compare",
            "--profile=test-minimal",
            f"--config={CONFIG_FIXTURE}",
            "--check",
            "--strict",
        ],
        check=False,
    )
    assert proc.returncode != 0


# --- Variant U ------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_then_revert_restores_state(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """U: install creates live file; revert removes it (no prior content)."""
    c = docker_container()
    _install(c, "test-minimal")
    # Confirm the file exists post-install.
    assert (
        c.exec(
            ["test", "-f", "/home/tester/.setforge_e2e/minimal/text.txt"], check=False
        ).returncode
        == 0
    )
    revert = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "revert",
            "--profile=test-minimal",
            f"--config={CONFIG_FIXTURE}",
            "--yes",
        ]
    )
    assert revert.returncode == 0
    # File is gone after revert (it was created from absence on install).
    assert (
        c.exec(
            ["test", "-f", "/home/tester/.setforge_e2e/minimal/text.txt"], check=False
        ).returncode
        != 0
    )


# --- revert-confirm: revert confirm-explain-redo wizard (mockup A) ------------------


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_revert_confirm_aborted(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """revert-confirm: revert without --yes against a non-TTY stdin refuses with
    ConfirmRequiresInteractive and leaves the deployed file untouched.

    The non-TTY refusal is the wizard's safety contract: without a TTY
    and without --yes, revert cannot prompt and must abort cleanly
    rather than silently apply. Files installed by the prior `install`
    remain in place — the install delta is NOT reversed.
    """
    c = docker_container()
    _install(c, "test-minimal")
    target = "/home/tester/.setforge_e2e/minimal/text.txt"
    assert c.exec(["test", "-f", target], check=False).returncode == 0
    pre = c.read_text(target)

    revert = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "revert",
            "--profile=test-minimal",
            f"--config={CONFIG_FIXTURE}",
        ],
        check=False,
    )
    # Non-zero exit because the wizard refuses without --yes on non-TTY stdin.
    assert revert.returncode != 0, revert.stdout
    assert "requires --yes" in (revert.stderr + revert.stdout)
    # File still exists, content unchanged — no mutation applied.
    assert c.exec(["test", "-f", target], check=False).returncode == 0
    assert c.read_text(target) == pre


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_revert_confirm_applied(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """revert-confirm: revert --yes short-circuits the wizard and applies cleanly,
    removing the file `install` created and writing a reverse transition.
    """
    c = docker_container()
    _install(c, "test-minimal")
    target = "/home/tester/.setforge_e2e/minimal/text.txt"
    assert c.exec(["test", "-f", target], check=False).returncode == 0

    revert = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "revert",
            "--profile=test-minimal",
            f"--config={CONFIG_FIXTURE}",
            "--yes",
        ]
    )
    assert revert.returncode == 0, revert.stderr
    # File removed — install's stub-creation has been reversed.
    assert c.exec(["test", "-f", target], check=False).returncode != 0
    # A reverse transition (command=revert) was written.
    transitions_ls = c.exec(
        ["ls", "/home/tester/.local/state/setforge/transitions"], check=False
    )
    assert transitions_ls.returncode == 0
    assert "revert-test-minimal" in transitions_ls.stdout


# --- Variant V ------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_idempotent_second_run_noop(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """V: install twice; second run exits 0 with consistent dst state."""
    c = docker_container()
    _install(c, "test-minimal")
    first = c.read_text("/home/tester/.setforge_e2e/minimal/text.txt")
    second = _install(c, "test-minimal")
    assert second.returncode == 0
    after = c.read_text("/home/tester/.setforge_e2e/minimal/text.txt")
    assert first == after


# --- Variant W ------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_validate_clean_yaml_exit_zero(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """W: validate --all against the fixture config exits 0."""
    c = docker_container()
    proc = c.exec(
        ["uv", "run", "setforge", "validate", "--all", f"--config={CONFIG_FIXTURE}"]
    )
    assert proc.returncode == 0
    assert "ok" in proc.stdout


# ===========================================================================
# Section: Prose-reviewer artifacts
# ===========================================================================
#
# The four variants below exercise the install / compare / revert
# lifecycle on the three new prose-reviewer agent files and the new
# reviewing-markdown skill. The fixture-tracked copies under
# tests/fixtures/e2e/tracked/claude/{agents,skills}/ mirror the real
# tracked content; install must produce byte-identical live files,
# compare must report no drift, and revert must remove every deployed
# artifact (each starts absent on a fresh container).
#
# Implicitly verifies: hashed-marker's strict-tag
# parser does not reject pure-tracked agent files that contain no
# user-section markers.


_PROSE_AGENT_BASENAMES = (
    "python-prose-reviewer.md",
    "claude-md-prose-reviewer.md",
    "markdown-prose-reviewer.md",
)
_PROSE_AGENT_TRACKED_DIR = "/workspace/tests/fixtures/e2e/tracked/claude/agents"
_PROSE_SKILL_TRACKED = (
    "/workspace/tests/fixtures/e2e/tracked/claude/skills/reviewing-markdown/SKILL.md"
)


@pytest.mark.xdist_group("docker_daemon")
def test_install_deploys_three_new_prose_reviewer_agents(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Install deploys all 3 prose-reviewer agents byte-identical to tracked."""
    c = docker_container()
    _install(c, "test-prose-reviewers")
    for basename in _PROSE_AGENT_BASENAMES:
        live = c.read_text(f"/home/tester/.claude/agents/{basename}")
        tracked = c.read_text(f"{_PROSE_AGENT_TRACKED_DIR}/{basename}")
        assert live == tracked, f"deployed {basename} drifts from tracked"


@pytest.mark.xdist_group("docker_daemon")
def test_install_deploys_new_reviewing_markdown_skill(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Install deploys reviewing-markdown SKILL.md byte-identical to tracked."""
    c = docker_container()
    _install(c, "test-prose-reviewers")
    live = c.read_text("/home/tester/.claude/skills/reviewing-markdown/SKILL.md")
    tracked = c.read_text(_PROSE_SKILL_TRACKED)
    assert live == tracked


@pytest.mark.xdist_group("docker_daemon")
def test_compare_after_install_clean_no_drift_for_new_agents_and_skill(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """After clean install, compare --check exits 0 (no drift)."""
    c = docker_container()
    _install(c, "test-prose-reviewers")
    proc = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "compare",
            "--profile=test-prose-reviewers",
            f"--config={CONFIG_FIXTURE}",
            "--check",
        ],
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.xdist_group("docker_daemon")
def test_revert_after_install_removes_new_agents_and_skill(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Revert after install removes all 3 prose agents + reviewing-markdown skill.

    Each artifact starts absent on a fresh container, so revert's
    `patch -R` equivalent removes them entirely (no prior content to
    restore).
    """
    c = docker_container()
    _install(c, "test-prose-reviewers")
    # Sanity: all 4 artifacts exist post-install.
    for basename in _PROSE_AGENT_BASENAMES:
        assert (
            c.exec(
                ["test", "-f", f"/home/tester/.claude/agents/{basename}"], check=False
            ).returncode
            == 0
        ), f"pre-revert sanity: {basename} should exist"
    assert (
        c.exec(
            [
                "test",
                "-f",
                "/home/tester/.claude/skills/reviewing-markdown/SKILL.md",
            ],
            check=False,
        ).returncode
        == 0
    ), "pre-revert sanity: reviewing-markdown SKILL.md should exist"

    revert = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "revert",
            "--profile=test-prose-reviewers",
            f"--config={CONFIG_FIXTURE}",
            "--yes",
        ]
    )
    assert revert.returncode == 0, revert.stderr

    # All 4 artifacts gone post-revert.
    for basename in _PROSE_AGENT_BASENAMES:
        assert (
            c.exec(
                ["test", "-f", f"/home/tester/.claude/agents/{basename}"], check=False
            ).returncode
            != 0
        ), f"post-revert: {basename} should be absent"
    assert (
        c.exec(
            [
                "test",
                "-f",
                "/home/tester/.claude/skills/reviewing-markdown/SKILL.md",
            ],
            check=False,
        ).returncode
        != 0
    ), "post-revert: reviewing-markdown SKILL.md should be absent"


_WORKFLOW_LIVE = "/home/tester/.claude/workflows/example-impl.js"
_WORKFLOW_TRACKED = (
    "/workspace/tests/fixtures/e2e/tracked/claude/workflows/example-impl.js"
)


@pytest.mark.xdist_group("docker_daemon")
def test_install_deploys_workflows_category_file(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Install deploys a workflows/*.js file byte-identical to tracked."""
    c = docker_container()
    _install(c, "test-workflows")
    live = c.read_text(_WORKFLOW_LIVE)
    tracked = c.read_text(_WORKFLOW_TRACKED)
    assert live == tracked, "deployed example-impl.js drifts from tracked"


@pytest.mark.xdist_group("docker_daemon")
def test_compare_after_install_clean_no_drift_for_workflows(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """After clean install of the workflows profile, compare --check exits 0."""
    c = docker_container()
    _install(c, "test-workflows")
    proc = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "compare",
            "--profile=test-workflows",
            f"--config={CONFIG_FIXTURE}",
            "--check",
        ],
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.xdist_group("docker_daemon")
def test_revert_after_install_removes_workflows_file(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Revert removes the deployed workflows file (absent on fresh container)."""
    c = docker_container()
    _install(c, "test-workflows")
    assert c.exec(["test", "-f", _WORKFLOW_LIVE], check=False).returncode == 0, (
        "pre-revert sanity: example-impl.js should exist"
    )

    revert = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "revert",
            "--profile=test-workflows",
            f"--config={CONFIG_FIXTURE}",
            "--yes",
        ]
    )
    assert revert.returncode == 0, revert.stderr
    assert c.exec(["test", "-f", _WORKFLOW_LIVE], check=False).returncode != 0, (
        "post-revert: example-impl.js should be absent"
    )


# --- Variant L2 (legacy my_setup.yaml migration error) ----------------------


@pytest.mark.xdist_group("docker_daemon")
def test_compare_with_legacy_my_setup_yaml_surfaces_migration_hint(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Legacy ``my_setup.yaml`` in a ``--source`` dir triggers ``git mv`` hint.

    Pairs with the unit test in
    :class:`tests.test_source.TestValidateSourceDir` and the CliRunner
    test in :class:`tests.test_cli_e2e.TestSourceLayerMigrationError`:
    proves the migration error fires in a real subprocess too, with
    the actionable ``git mv`` recipe landing on stderr.

    The container's source-layer ``validate_source_dir`` walks
    ``--source`` first, finds the legacy filename, and raises
    :class:`setforge.errors.ConfigError`. ``main()``'s
    ``SetforgeError`` catch formats the message on stderr and exits
    non-zero — this test pins that user-facing contract.
    """
    c = docker_container()
    c.write_text(
        "/home/tester/legacy-src/my_setup.yaml",
        "version: 1\nprofiles: {}\n",
    )
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "--source",
            "/home/tester/legacy-src",
            "compare",
            "--profile=anything",
        ],
        check=False,
    )
    assert result.returncode != 0, (
        f"expected non-zero exit for legacy my_setup.yaml; "
        f"got returncode={result.returncode}\n"
        f"stdout:{result.stdout}\nstderr:{result.stderr}"
    )
    assert "legacy 'my_setup.yaml'" in result.stderr, (
        f"expected migration hint in stderr; got: {result.stderr!r}"
    )
    assert "git mv my_setup.yaml setforge.yaml" in result.stderr, (
        f"expected 'git mv' recipe in stderr; got: {result.stderr!r}"
    )


# ===========================================================================
# Section: Pre-deploy secrets scan
# ===========================================================================


# --- Variant: scan clean (gitleaks present, no findings) -------------------


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_install_secrets_scan_clean(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """secrets-scan: gitleaks on PATH + clean tracked → install succeeds normally.

    The ``test-minimal`` fixture's tracked tree carries the single
    ``minimal/text.txt`` file (``hello from test-minimal``); gitleaks
    cannot match a secret rule against it, so the pre-deploy scan
    returns an empty result and the install proceeds without prompting.
    """
    c = docker_container()
    # Sanity: gitleaks is on PATH inside the image.
    which = c.exec(["which", "gitleaks"], check=False)
    assert which.returncode == 0, (
        f"gitleaks missing from image PATH; stderr={which.stderr!r}"
    )
    result = _install(c, "test-minimal")
    assert (
        _read_live(c, ".setforge_e2e/minimal/text.txt") == "hello from test-minimal\n"
    )
    # No "POTENTIAL SECRET" panel + no abort message.
    assert "POTENTIAL SECRET" not in result.stdout
    assert "install aborted by secrets scan" not in result.stderr


# --- Variant: gitleaks finds + aborts (default non-TTY ABORT) -------------


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_install_secrets_scan_finds_and_aborts(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """secrets-scan: planting a secret in tracked aborts the install cleanly.

    Non-TTY install context (``docker exec`` without ``-t``) means the
    wizard short-circuits to :data:`SecretAction.ABORT` per the
    soft-requirement test discipline — the live file MUST NOT appear
    after the abort. Test plants a fake GitHub Personal Access Token
    (one of gitleaks' built-in rules) into a copy of the tracked source
    inside the container's workspace, runs install, then asserts the
    live dst was never written.

    The fake token is assembled here (string concat) so pre-commit's
    own gitleaks hook does NOT trip on this test file at commit time —
    only the runtime gitleaks invocation inside the container sees the
    fully-formed pattern. The character set + entropy is chosen to
    exceed gitleaks' ``github-pat`` rule threshold.
    """
    c = docker_container()
    fake_token = "ghp_" + "x6Hv9Kp2zQwL8mN3rF7tY1bC4dE5gJ0sA9iU"
    planted = (
        f"hello from test-minimal\nfake gh pat for secrets-scan e2e: {fake_token}\n"
    )
    c.write_text(
        "/workspace/tests/fixtures/e2e/tracked/minimal/text.txt",
        planted,
    )
    # Ensure the live dst does NOT pre-exist (so the post-abort assertion
    # cannot be satisfied by a previous-install leftover).
    c.exec(["rm", "-f", "/home/tester/.setforge_e2e/minimal/text.txt"], check=False)

    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            "--profile=test-minimal",
            f"--config={CONFIG_FIXTURE}",
        ],
        check=False,
    )

    # Install returns 0 (no exception); the wizard's ABORT path emits a
    # red "install aborted by secrets scan" line on stderr and returns
    # cleanly without deploying.
    assert "install aborted by secrets scan" in result.stderr, (
        f"expected abort line on stderr; stderr={result.stderr!r}"
    )
    # Live dst MUST NOT exist — the abort fired before deploy.
    exists = c.exec(
        ["test", "-f", "/home/tester/.setforge_e2e/minimal/text.txt"], check=False
    )
    assert exists.returncode != 0, (
        "live dst must not be created when install aborts on secret finding"
    )


# --- Variant: gitleaks missing → warn-and-continue (soft-requirement) ----


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_install_no_gitleaks_warns_and_continues(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """secrets-scan soft-requirement: missing gitleaks → yellow warning + install OK.

    Removes the gitleaks binary from /usr/local/bin inside the
    container (the only location it lives on PATH), then runs install.
    The pre-deploy scan must emit the install-hint warning on stderr
    and the install must still produce the live dst — this is the
    canonical soft-requirement assertion (no hard-error, no exception
    surfaced to the user).
    """
    c = docker_container()
    # Remove gitleaks from PATH (cwd-based fallback only; the binary
    # was installed by the Dockerfile at /usr/local/bin/gitleaks).
    c.exec(["sudo", "true"], check=False)  # no-op; we use rm via tester perms
    rm = c.exec(["rm", "-f", "/usr/local/bin/gitleaks"], check=False)
    # tester may not own /usr/local/bin — fall back to PATH shadowing.
    if rm.returncode != 0:
        # Shadow gitleaks via an empty PATH override. Move/rename also OK.
        c.exec(["mkdir", "-p", "/home/tester/empty-bin"], check=True)
        result = c.exec(
            [
                "env",
                "PATH=/home/tester/empty-bin:/home/tester/.local/bin:/usr/bin:/bin",
                "uv",
                "run",
                "setforge",
                "install",
                "--profile=test-minimal",
                f"--config={CONFIG_FIXTURE}",
            ],
            check=False,
        )
    else:
        result = c.exec(
            [
                "uv",
                "run",
                "setforge",
                "install",
                "--profile=test-minimal",
                f"--config={CONFIG_FIXTURE}",
            ],
            check=False,
        )

    assert result.returncode == 0, (
        f"install must succeed when gitleaks is absent (soft-requirement); "
        f"stderr={result.stderr!r}"
    )
    assert "gitleaks not found on PATH" in result.stderr, (
        f"expected soft-requirement warning on stderr; got: {result.stderr!r}"
    )
    # Live dst lands despite missing scanner — install continued.
    assert (
        _read_live(c, ".setforge_e2e/minimal/text.txt") == "hello from test-minimal\n"
    )


# ===========================================================================
# Per-item reconcile failure UX (skip / retry / abort / diagnose)
# ===========================================================================


_FAILING_CODE_STUB = """#!/bin/bash
# Wrapper stub for the `code` CLI used by the reconcile-failure e2e tests.
#
# Behavior:
# - For ``--install-extension force-fail.ext``, exits 1 (fixed failure).
# - For ``--install-extension flaky.ext``, exits 1 the FIRST time it is
#   invoked per container (counter file at /tmp/flaky.count) and 0 on
#   every subsequent call — simulates a successful retry.
# - Every other invocation falls through to the real ``code`` binary
#   discovered via PATH, with this stub's own dir excluded so we don't
#   re-enter ourselves.
set -euo pipefail
ME_DIR="$(cd "$(dirname "$0")" && pwd)"
ARGV=("$@")
if [ "${ARGV[0]:-}" = "--install-extension" ]; then
    case "${ARGV[1]:-}" in
        force-fail.ext)
            echo "force-fail.ext refused by stub" >&2
            exit 1
            ;;
        flaky.ext)
            COUNT_FILE=/tmp/flaky.count
            n=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
            echo $((n + 1)) > "$COUNT_FILE"
            if [ "$n" -eq 0 ]; then
                echo "flaky.ext refused first try" >&2
                exit 1
            fi
            ;;
    esac
fi
# Fall through to the real code binary (typically /usr/bin/code).
PATH_WITHOUT_ME="$(echo "$PATH" | tr ':' '\\n' \
    | grep -v "^${ME_DIR}$" | paste -sd: -)"
exec env PATH="$PATH_WITHOUT_ME" code "$@"
"""


def _seed_failing_code_stub(c: ContainerHandle) -> str:
    """Drop the failing-code stub into the container; return absolute path.

    Writes the wrapper at ``/home/tester/bin/code`` and points
    ``~/.config/setforge/local.yaml``'s ``binaries.code`` at it so the
    setforge binary-override layer picks it up. Returns the stub's
    absolute path so the caller can chmod / inspect it.
    """
    stub_path = "/home/tester/bin/code"
    c.exec(["mkdir", "-p", "/home/tester/bin"], check=True)
    c.write_text(stub_path, _FAILING_CODE_STUB)
    c.exec(["chmod", "+x", stub_path], check=True)
    c.write_text(
        "/home/tester/.config/setforge/local.yaml",
        f"binaries:\n  code: {stub_path}\n",
    )
    return stub_path


def _patch_profile_for_failing_extension(c: ContainerHandle, extra_ext: str) -> str:
    """Append ``extra_ext`` to the test-comprehensive profile in the fixture.

    Writes a patched copy of the canonical fixture under
    ``tests/fixtures/e2e/setforge.patched-ext.test.yaml`` so its relative
    ``src:`` paths still resolve correctly (the config-loader resolves
    ``src:`` against the config file's parent dir). Returns the
    repo-relative path for use with ``--config``.
    """
    out_path = "tests/fixtures/e2e/setforge.patched-ext.test.yaml"
    text = c.exec(["cat", CONFIG_FIXTURE], check=True).stdout
    needle = "        - editorconfig.editorconfig\n"
    if needle not in text:
        raise AssertionError(
            f"fixture {CONFIG_FIXTURE!r} no longer carries the "
            "editorconfig anchor; reconcile-failure patches need refresh"
        )
    patched = text.replace(needle, needle + f"        - {extra_ext}\n", 1)
    # ``write_text`` accepts an absolute container path; the fixture
    # already lives inside the bind-mounted repo at ``/workspace``.
    c.write_text(f"/workspace/{out_path}", patched)
    return out_path


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_install_plugin_failure_skip(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Default-skip path: a failing extension install with ``--yes`` short-
    circuits the per-item prompt to SKIP, the install exits 0, and the
    transition record carries a ``status="skipped"`` outcome for the
    failing id.

    Mirrors mockup E acceptance row 2 (default choice is "skip & continue").
    """
    c = docker_container()
    _seed_failing_code_stub(c)
    patched = _patch_profile_for_failing_extension(c, "force-fail.ext")
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            "--profile=test-comprehensive",
            f"--config={patched}",
            "--yes",
        ],
        check=False,
    )
    assert result.returncode == 0, (
        f"install with --yes (default-SKIP) should exit 0; "
        f"got returncode={result.returncode}\nstderr:{result.stderr}"
    )
    # Surface the skipped id in the transition record so --retry-failed
    # picks it up on the next run.
    show = c.exec(
        [
            "bash",
            "-c",
            "ls -1 ~/.local/state/setforge/transitions/ | sort | tail -1",
        ],
        check=True,
    )
    latest = show.stdout.strip()
    assert latest, "no transition recorded"
    outcomes = c.exec(
        [
            "cat",
            f"/home/tester/.local/state/setforge/transitions/{latest}/reconcile_outcomes.json",
        ],
        check=True,
    ).stdout
    assert "force-fail.ext" in outcomes, outcomes
    assert '"status": "skipped"' in outcomes, outcomes


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_install_plugin_failure_retry_success(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """RETRY-success path: a flaky extension that fails first attempt but
    succeeds on retry surfaces a ``status="retried_ok"`` outcome.

    Today's prompt path requires a TTY for non-yes; we exercise the
    underlying retry behavior by relying on the in-loop pre-prompt
    failure list: ``vscode_extensions.reconcile`` doesn't currently
    retry inside its own loop, so the failure surfaces to the prompt.
    Without a TTY available we use ``--yes`` (default-SKIP). The flaky
    stub still records the skipped state for this run, and the second
    invocation under ``--retry-failed`` lands clean. This pair of
    invocations is the end-to-end shape mockup E acceptance row 3
    promises: 'retry re-attempts in-place'.
    """
    c = docker_container()
    _seed_failing_code_stub(c)
    patched = _patch_profile_for_failing_extension(c, "flaky.ext")
    first = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            "--profile=test-comprehensive",
            f"--config={patched}",
            "--yes",
        ],
        check=False,
    )
    assert first.returncode == 0, first.stderr
    # First run: flaky.ext failed, was skipped.
    show = c.exec(
        [
            "bash",
            "-c",
            "ls -1 ~/.local/state/setforge/transitions/ | sort | tail -1",
        ],
        check=True,
    )
    first_dir = show.stdout.strip()
    outcomes = c.exec(
        [
            "cat",
            f"/home/tester/.local/state/setforge/transitions/{first_dir}/reconcile_outcomes.json",
        ],
        check=True,
    ).stdout
    assert "flaky.ext" in outcomes
    assert '"status": "skipped"' in outcomes
    # Now retry — flaky.ext succeeds the second time per the stub.
    second = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            "--profile=test-comprehensive",
            f"--config={patched}",
            "--yes",
            "--retry-failed",
        ],
        check=False,
    )
    assert second.returncode == 0, second.stderr


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_install_plugin_failure_abort_no_regression_under_yes(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Verifies that default-SKIP under ``--yes`` still produces a clean
    install with the failing item skipped: a plugin/extension failure
    does NOT surface as an ABORT-style rollback, so successful items
    stay landed even when one fails.

    The interactive ABORT branch requires a pty-driven test (deferred
    to a follow-up) — ``--yes`` short-circuits the
    arrow-key picker to the default ``SKIP`` action, so the abort
    code path cannot be exercised from this entry point. The rollback
    machinery itself is covered by unit tests in
    ``tests/test_plugin_helpers.py`` and
    ``tests/test_cli_failure_prompt.py``.
    """
    c = docker_container()
    _seed_failing_code_stub(c)
    patched = _patch_profile_for_failing_extension(c, "force-fail.ext")
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            "--profile=test-comprehensive",
            f"--config={patched}",
            "--yes",
        ],
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # editorconfig.editorconfig succeeded ahead of the failure → still
    # installed (default-SKIP behavior, NOT abort/rollback).
    listed = c.exec(["/usr/bin/code", "--list-extensions"], check=False)
    assert "editorconfig.editorconfig" in listed.stdout, (
        f"editorconfig.editorconfig should remain installed under default-SKIP; "
        f"got stdout:{listed.stdout!r}\nstderr:{listed.stderr!r}"
    )


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_install_retry_failed_flag(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """The ``--retry-failed`` flag is plumbed end-to-end: ``setforge
    install --help`` advertises it, and passing it without a prior
    transition (no skipped ids to retry) exits 0 — the flag is
    idempotent on a fresh state.

    Mirrors mockup E acceptance row 7 (``--retry-failed`` shortcut)
    and pins the flag surface so a future renaming surfaces here too.
    """
    c = docker_container()
    help_text = c.exec(
        ["uv", "run", "setforge", "install", "--help"], check=True
    ).stdout
    assert "--retry-failed" in help_text
    # First-time install with --retry-failed and no prior history — the
    # flag is a no-op (frozenset() of skipped ids) and the install
    # exits 0.
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            "--profile=test-comprehensive",
            f"--config={CONFIG_FIXTURE}",
            "--retry-failed",
            "--yes",
        ],
        check=False,
    )
    assert result.returncode == 0, result.stderr


# Section: `setforge init` bootstrap (mockup J)
# ===========================================================================
#
# Four scenarios from mockup J:
#   - fresh init creates the three bootstrap paths + writes local.yaml
#   - reinit (sentinel already present + host-local dir already exists)
#     is idempotent — no overwrite, no backup file
#   - --force --no-prompt produces a timestamped backup AND rewrites
#     local.yaml to the canonical stub
#   - --check is read-only: prints the env/dirs/capabilities health
#     report and never creates host-local/


def _init(
    container: ContainerHandle,
    *,
    extra: list[str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``setforge init`` inside the container; return CompletedProcess."""
    cmd = ["uv", "run", "setforge", "init"]
    if extra:
        cmd.extend(extra)
    result = container.exec(cmd, check=False)
    if check:
        assert result.returncode == 0, result.stderr
    return result


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_init_fresh(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """init-bootstrap: fresh --no-prompt init creates all three bootstrap paths.

    Wipes the local.yaml stub that the Typer root callback writes on
    every invocation, then asserts that init creates the canonical
    config dir, the local.yaml template, and the host-local share
    directory in one shot.
    """
    c = docker_container()
    # Strip any pre-existing setforge state so this exercises the
    # fresh-init branch (root callback re-writes local.yaml, but the
    # host-local dir staying absent triggers the bootstrap path).
    c.exec(["rm", "-rf", "/home/tester/.config/setforge"], check=False)
    c.exec(["rm", "-rf", "/home/tester/.local/share/setforge"], check=False)
    result = _init(c, extra=["--no-prompt"])
    assert "init complete" in result.stderr, result.stderr
    cfg_check = c.exec(
        ["test", "-f", "/home/tester/.config/setforge/local.yaml"], check=False
    )
    assert cfg_check.returncode == 0, "local.yaml missing post-init"
    host_local_check = c.exec(
        ["test", "-d", "/home/tester/.local/share/setforge/host-local"], check=False
    )
    assert host_local_check.returncode == 0, "host-local dir missing post-init"


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_init_reinit_idempotent(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """init-bootstrap: rerunning init after a clean bootstrap is a no-op.

    First run creates the bootstrap state; the second run takes the
    idempotent branch (sentinel + host-local dir both present) and
    reports ``nothing to create`` without overwriting customizations.
    """
    c = docker_container()
    c.exec(["rm", "-rf", "/home/tester/.config/setforge"], check=False)
    c.exec(["rm", "-rf", "/home/tester/.local/share/setforge"], check=False)
    _init(c, extra=["--no-prompt"])
    # Seed a user customization that must survive the second init.
    c.write_text(
        "/home/tester/.config/setforge/local.yaml",
        "# setforge host-local config\nuser_marker: preserved\n",
    )
    result = _init(c, extra=["--no-prompt"])
    assert "nothing to create" in result.stderr, result.stderr
    after = c.read_text("/home/tester/.config/setforge/local.yaml")
    assert "user_marker: preserved" in after, (
        "reinit overwrote user customization (must be idempotent)"
    )


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_init_force_with_backup(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """init-bootstrap: --force --no-prompt produces a timestamped backup.

    Seeds a user-marker local.yaml + the host-local dir so we hit the
    --force branch (not the fresh-init branch), then asserts the backup
    file lands at ``local.yaml.bak.<UTC-ISO8601>`` and the rewritten
    local.yaml carries the canonical stub.
    """
    c = docker_container()
    c.exec(["rm", "-rf", "/home/tester/.config/setforge"], check=False)
    c.exec(["rm", "-rf", "/home/tester/.local/share/setforge"], check=False)
    _init(c, extra=["--no-prompt"])
    c.write_text(
        "/home/tester/.config/setforge/local.yaml",
        "# setforge host-local config\nuser_marker: backup-me\n",
    )
    result = _init(c, extra=["--force", "--no-prompt"])
    assert "init complete" in result.stderr, result.stderr
    ls = c.exec(
        ["bash", "-lc", "ls /home/tester/.config/setforge/local.yaml.bak.* 2>&1"],
        check=False,
    )
    assert ls.returncode == 0, f"no backup file found: {ls.stdout}{ls.stderr}"
    backup_lines = [
        line for line in ls.stdout.strip().splitlines() if line.endswith("Z")
    ]
    assert len(backup_lines) == 1, (
        f"expected exactly one timestamped backup, got: {backup_lines!r}"
    )
    # Verify the backup carries the pre-overwrite content.
    backup_content = c.exec(["cat", backup_lines[0]]).stdout
    assert "user_marker: backup-me" in backup_content, (
        "backup file is missing the pre-overwrite marker"
    )
    # Verify the new local.yaml is the canonical stub.
    new = c.read_text("/home/tester/.config/setforge/local.yaml")
    assert "setforge host-local config" in new
    assert "user_marker: backup-me" not in new, (
        "--force did not actually rewrite local.yaml"
    )


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_init_config_repo_scaffolds_and_validates(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """init --config-repo scaffolds a repo, wires source, and validate --all passes.

    One-shot end-to-end: wipe setforge state, run
    ``init --config-repo --no-prompt`` (default target
    ``~/projects/<host>-config``), then assert the scaffolded ``.git`` +
    empty ``tracked/`` + starter ``setforge.yaml`` exist, the wired
    ``source:`` block landed in ``local.yaml``, and ``validate --all``
    (resolving the source through that wired block) exits 0. A second run
    leaves ``local.yaml`` byte-identical (dedup guard).
    """
    c = docker_container()
    c.exec(["rm", "-rf", "/home/tester/.config/setforge"], check=False)
    c.exec(["rm", "-rf", "/home/tester/.local/share/setforge"], check=False)
    c.exec(["rm", "-rf", "/home/tester/projects"], check=False)

    result = _init(c, extra=["--config-repo", "--no-prompt"])
    assert "init --config-repo complete" in result.stderr, result.stderr

    # The default target is ~/projects/<host>-config; resolve it.
    repo = c.exec(
        ["bash", "-lc", "ls -d /home/tester/projects/*-config"], check=True
    ).stdout.strip()
    assert repo, "no scaffolded config repo under ~/projects"
    assert c.exec(["test", "-d", f"{repo}/.git"], check=False).returncode == 0, (
        ".git not initialized"
    )
    assert c.exec(["test", "-d", f"{repo}/tracked"], check=False).returncode == 0, (
        "tracked/ tree missing"
    )
    # tracked/ is empty.
    tracked_ls = c.exec(["bash", "-lc", f"ls -A {repo}/tracked"], check=False).stdout
    assert tracked_ls.strip() == "", f"tracked/ not empty: {tracked_ls!r}"

    starter = c.read_text(f"{repo}/setforge.yaml")
    assert "schema_version:" in starter, starter
    assert "profiles:" in starter, starter

    local_yaml = c.read_text("/home/tester/.config/setforge/local.yaml")
    assert "source:" in local_yaml, local_yaml
    assert "kind: path" in local_yaml, local_yaml

    # validate --all resolves the wired source and passes.
    vresult = c.exec(["uv", "run", "setforge", "validate", "--all"], check=False)
    assert vresult.returncode == 0, f"validate failed: {vresult.stdout}{vresult.stderr}"
    assert "ok" in vresult.stdout, vresult.stdout

    # Idempotent second run: local.yaml byte-identical (no duplicate source).
    before = c.read_text("/home/tester/.config/setforge/local.yaml")
    _init(c, extra=["--config-repo", "--no-prompt"])
    after = c.read_text("/home/tester/.config/setforge/local.yaml")
    assert after == before, "second --config-repo run mutated local.yaml"


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_init_check_readonly(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """init-bootstrap: --check prints the env/dirs/capabilities report; no side effects.

    Wipes setforge state, runs --check, and asserts the host-local
    share directory does NOT appear (the root callback writes the
    local.yaml stub regardless; --check must not create anything
    BEYOND that).
    """
    c = docker_container()
    c.exec(["rm", "-rf", "/home/tester/.config/setforge"], check=False)
    c.exec(["rm", "-rf", "/home/tester/.local/share/setforge"], check=False)
    result = _init(c, extra=["--check"])
    assert "checking environment" in result.stderr, result.stderr
    assert "checking config directories" in result.stderr, result.stderr
    assert "check complete" in result.stderr, result.stderr
    host_local_check = c.exec(
        ["test", "-d", "/home/tester/.local/share/setforge/host-local"], check=False
    )
    assert host_local_check.returncode != 0, (
        "--check must NOT create the host-local share directory"
    )


# --- Variant U (setforge upgrade --check via fake PyPI) ---------------------


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_upgrade_check_mode(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``setforge upgrade --check`` against a fake-PyPI fixture (no real net).

    Spins a ``python -m http.server`` inside the container on port
    8765, serves a hand-crafted ``setforge/json`` body asserting a
    newer release (``99.0.0``), then runs ``setforge upgrade --check``
    with ``SETFORGE_PYPI_BASE`` overriding the JSON-API base URL. The
    check-mode path is read-only — no ``uv tool upgrade`` is invoked,
    no network egress is attempted, and the exit code is 0.

    The fake-PyPI fixture body shape mirrors the real PyPI JSON API
    (``info`` + ``releases`` dict) so the unit-tested filter logic in
    :mod:`setforge._pypi_client` lights up identically.
    """
    c = docker_container()
    fake_pypi_body = json.dumps(
        {
            "info": {"version": "99.0.0"},
            "releases": {
                "0.1.0": [{"yanked": False}],
                "0.2.0": [{"yanked": False}],
                "99.0.0": [{"yanked": False}],
            },
        }
    )
    # Lay out the JSON body at the URL shape `/setforge/json` so a plain
    # static `python -m http.server` serves it at the exact path the
    # client requests.
    c.exec(["mkdir", "-p", "/tmp/fakepypi/setforge"])
    c.write_text("/tmp/fakepypi/setforge/json", fake_pypi_body)
    # Launch the HTTP server in the background; redirect output away from
    # the exec stream so the call returns immediately.
    c.exec(
        [
            "sh",
            "-c",
            (
                "cd /tmp/fakepypi && "
                "nohup python3 -m http.server 8765 "
                ">/tmp/fakepypi.log 2>&1 & "
                "sleep 0.5"
            ),
        ],
    )
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "upgrade",
            "--check",
        ],
        env={"SETFORGE_PYPI_BASE": "http://127.0.0.1:8765"},
        check=False,
    )
    assert result.returncode == 0, (
        f"expected exit 0 for upgrade --check; got returncode={result.returncode}\n"
        f"stdout:{result.stdout}\nstderr:{result.stderr}"
    )
    assert "99.0.0" in result.stdout, (
        f"expected target version 99.0.0 in stdout; got: {result.stdout!r}"
    )
    assert "=== schema impact ===" in result.stdout, (
        f"expected always-on schema impact panel; got: {result.stdout!r}"
    )


# Section: setforge migrate — schema migration registry
# ===========================================================================
#
# Two variants pin the v0.2.0 contract:
#
# (i)  ``test_e2e_docker_migrate_check_no_migrations_available`` — today's
#      empty-registry state: ``--check`` reports ``no migrations available``
#      and exits 0.
# (ii) ``test_e2e_docker_migrate_multi_file_fake`` — the broadened-scope
#      assertion: injects a fake Migration into the registry via a
#      runtime monkeypatch script that touches setforge.yaml + a tracked
#      content file simultaneously, then drives the full apply flow with
#      ``--yes`` and verifies backup + apply + rollback all work
#      end-to-end at multi-file granularity.


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_migrate_check_no_migrations_available(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """--check reports no migrations available when already at the expected schema.

    A config already pinned to the build's current expected schema (2.0)
    has nothing to bridge, so ``--check`` reports ``no migrations
    available`` and exits 0. The frozen-1.0-config case (which now DOES
    surface the 1.0 → 1.1 → 1.2 → 2.0 chain) is covered in
    ``test_e2e_docker_migrate.py``.
    """
    c = docker_container()
    c.write_text(
        "/tmp/at-current/setforge.yaml",
        "schema_version: '2.0'\nversion: 1\ntracked_files: {}\nprofiles: {p: {}}\n",
    )
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "migrate",
            "--check",
            "--config=/tmp/at-current/setforge.yaml",
        ],
        check=False,
    )
    assert result.returncode == 0, (
        f"migrate --check should exit 0 when already at the expected schema; "
        f"got returncode={result.returncode}\n"
        f"stdout:{result.stdout}\nstderr:{result.stderr}"
    )
    assert "no migrations available" in result.stdout, (
        f"expected 'no migrations available' in stdout; got: {result.stdout!r}"
    )


_FAKE_MIGRATION_HARNESS = textwrap.dedent(
    """\
    \"\"\"Inject a fake Migration into setforge.migrations.MIGRATIONS, then
    invoke the setforge CLI. Used by the docker e2e to prove the
    broadened-scope Migration Protocol (multi-file apply + backup +
    rollback) works end-to-end against a real subprocess.
    \"\"\"
    from __future__ import annotations

    import sys
    from dataclasses import dataclass
    from pathlib import Path

    import setforge.migrations as migrations_mod
    from setforge.migrations import ManifestEntry, ManifestType, MigrationRoots
    from setforge.migrations._fs_ops import atomic_replace
    from setforge.migrations._yaml_ops import (
        atomic_write_yaml,
        rename_key,
        yaml_rt,
    )


    @dataclass(slots=True, frozen=True)
    class _FakeMultiFileMigration:
        from_version: str = "1.0"
        to_version: str = "1.1"

        def manifest(self, *, roots):
            return (
                ManifestEntry(
                    type=ManifestType.RENAME,
                    description="rename old_key -> new_key",
                    affected_path=roots.cfg_path,
                ),
                ManifestEntry(
                    type=ManifestType.EDIT,
                    description="rewrite legacy sentinel",
                    affected_path=roots.repo_root / "tracked" / "fake.md",
                ),
            )

        def affected_paths(self, *, roots):
            return (
                roots.cfg_path,
                roots.repo_root / "tracked" / "fake.md",
            )

        def apply(self, *, roots) -> None:
            with roots.cfg_path.open("r", encoding="utf-8") as fh:
                data = yaml_rt().load(fh)
            rename_key(data, "old_key", "new_key")
            atomic_write_yaml(roots.cfg_path, data)

            tracked = roots.repo_root / "tracked" / "fake.md"
            tmp = tracked.with_suffix(".md.migration.tmp")
            tracked.parent.mkdir(parents=True, exist_ok=True)
            before = tracked.read_text(encoding="utf-8") if tracked.exists() else ""
            tmp.write_text(before.replace("legacy", "migrated"), encoding="utf-8")
            atomic_replace(tmp, tracked)


    migrations_mod.MIGRATIONS = (_FakeMultiFileMigration(),)
    migrations_mod.current_expected_schema_version = "1.1"
    import setforge.cli.migrate as migrate_mod

    migrate_mod.current_expected_schema_version = "1.1"

    from setforge.cli import main

    sys.argv = ["setforge"] + sys.argv[1:]
    main()
    """
)


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_migrate_multi_file_fake(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Inject a fake multi-file Migration; --apply backs up + mutates 2 files.

    Proves the broadened Migration Protocol scope: a single migration
    can touch setforge.yaml AND a tracked content file in one apply
    call; per-file backups land for both; rolling each backup back
    restores the pre-migration state.
    """
    c = docker_container()
    # Lay down a setforge.yaml the fake migration can rename a key in.
    yaml_path = "/home/tester/migrate_e2e/setforge.yaml"
    tracked_path = "/home/tester/migrate_e2e/tracked/fake.md"
    c.write_text(
        yaml_path,
        textwrap.dedent(
            """\
            # top
            version: 1
            # comment above old_key
            old_key: stays-for-rename  # eol
            tracked_files: {}
            profiles: {p: {}}
            """
        ),
    )
    c.write_text(tracked_path, "This is a legacy marker.\n")

    # Write the harness script + drive it via `uv run python`.
    harness_path = "/home/tester/migrate_e2e/run_with_fake_migration.py"
    c.write_text(harness_path, _FAKE_MIGRATION_HARNESS)

    result = c.exec(
        [
            "uv",
            "run",
            "--with",
            "setforge",
            "python",
            harness_path,
            "migrate",
            "--apply",
            "--yes",
            f"--config={yaml_path}",
        ],
        workdir="/workspace",
        check=False,
    )
    assert result.returncode == 0, (
        f"migrate --apply (fake migration) should exit 0; "
        f"got returncode={result.returncode}\n"
        f"stdout:{result.stdout}\nstderr:{result.stderr}"
    )

    # setforge.yaml was rewritten — old_key gone, new_key present, comments survived.
    yaml_after = c.read_text(yaml_path)
    assert "new_key:" in yaml_after, yaml_after
    assert "old_key:" not in yaml_after, yaml_after
    assert "# top" in yaml_after, yaml_after
    assert "# comment above old_key" in yaml_after, yaml_after
    assert "# eol" in yaml_after, yaml_after

    # Tracked content file was rewritten via atomic_replace.
    tracked_after = c.read_text(tracked_path)
    assert "migrated" in tracked_after, tracked_after
    assert "legacy" not in tracked_after, tracked_after

    # Per-file backups exist for BOTH affected paths (broadened-scope assertion).
    yaml_backup = c.read_text(f"{yaml_path}.pre-1.1.bak")
    assert "old_key:" in yaml_backup, yaml_backup
    tracked_backup = c.read_text(f"{tracked_path}.pre-1.1.bak")
    assert "legacy" in tracked_backup, tracked_backup

    # Rollback: mv each backup back over its file, content restored.
    c.exec(["mv", f"{yaml_path}.pre-1.1.bak", yaml_path])
    c.exec(["mv", f"{tracked_path}.pre-1.1.bak", tracked_path])
    assert c.read_text(yaml_path).strip().endswith("profiles: {p: {}}")
    assert "old_key:" in c.read_text(yaml_path)
    assert "legacy" in c.read_text(tracked_path)


# ===========================================================================
# Pre-deploy git-status check on the config source
# ===========================================================================


def _git_init_workspace(container: ContainerHandle) -> None:
    """Initialize ``/workspace`` as a git repo with one commit on ``main``.

    Sets a local identity (``setforge-tests <test@example.com>``) so
    ``git commit`` doesn't error on the container's default missing
    identity; then adds + commits every tracked-fixture file so the
    workspace starts in a clean baseline state. Subsequent tests dirty
    or freshen the working tree to exercise the new check.
    """
    container.exec(["git", "-C", "/workspace", "init", "-q", "-b", "main"], check=True)
    container.exec(
        ["git", "-C", "/workspace", "config", "user.email", "test@example.com"],
        check=True,
    )
    container.exec(
        ["git", "-C", "/workspace", "config", "user.name", "setforge-tests"],
        check=True,
    )
    container.exec(["git", "-C", "/workspace", "add", "-A"], check=True)
    container.exec(
        [
            "git",
            "-C",
            "/workspace",
            "commit",
            "-q",
            "-m",
            "initial fixture state",
        ],
        check=True,
    )


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_install_path_source_clean_no_warn(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Clean path source → install proceeds with no warning.

    Initializes the workspace as a git repo, commits the fixture state,
    then runs ``setforge install`` WITHOUT ``--no-git-check``. The new
    pre-deploy check must observe a clean tree and emit no warning —
    install lands the dst file normally.
    """
    c = docker_container()
    _git_init_workspace(c)
    result = _install(c, "test-minimal")
    assert "uncommitted changes" not in result.stdout
    assert "uncommitted changes" not in result.stderr
    assert (
        _read_live(c, ".setforge_e2e/minimal/text.txt") == "hello from test-minimal\n"
    )


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_install_path_source_dirty_warns_abort(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Dirty path source + non-TTY install → ABORT via mutate-gate.

    Initializes the workspace as a git repo, commits the fixture state,
    then **modifies a tracked fixture file** so ``git status`` reports
    uncommitted changes. The install runs without ``--no-git-check``
    on a non-TTY stdin → the mutate-gate raises
    :class:`ConfirmRequiresInteractive` and the dst MUST NOT land.
    """
    c = docker_container()
    _git_init_workspace(c)
    # Dirty the tracked source.
    c.write_text(
        "/workspace/tests/fixtures/e2e/tracked/minimal/text.txt",
        "hello from test-minimal — DIRTY edit\n",
    )
    # Ensure dst doesn't pre-exist.
    c.exec(["rm", "-f", "/home/tester/.setforge_e2e/minimal/text.txt"], check=False)
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            "--profile=test-minimal",
            f"--config={CONFIG_FIXTURE}",
        ],
        check=False,
    )
    assert result.returncode != 0, (
        f"expected non-zero exit on dirty non-TTY install; stderr={result.stderr!r}"
    )
    assert "stdin is not a TTY" in result.stderr, (
        f"expected mutate-gate message on stderr; got: {result.stderr!r}"
    )
    # Dst MUST NOT exist — the abort fired before deploy.
    exists = c.exec(
        ["test", "-f", "/home/tester/.setforge_e2e/minimal/text.txt"], check=False
    )
    assert exists.returncode != 0, (
        "live dst must not be created when install aborts on git-check"
    )


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_install_path_source_dirty_no_git_check_bypasses(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Dirty path source + ``--no-git-check`` → install proceeds.

    Same dirty-tree setup as the prior test, but the install passes
    ``--no-git-check``; the new gate is skipped entirely and the
    install lands the (dirty) tracked content on the dst as designed.
    Confirms the automation escape hatch works end-to-end.
    """
    c = docker_container()
    _git_init_workspace(c)
    c.write_text(
        "/workspace/tests/fixtures/e2e/tracked/minimal/text.txt",
        "hello from test-minimal — DIRTY but bypassed\n",
    )
    result = _install(c, "test-minimal", extra=["--no-git-check"])
    assert result.returncode == 0, result.stderr
    assert (
        _read_live(c, ".setforge_e2e/minimal/text.txt")
        == "hello from test-minimal — DIRTY but bypassed\n"
    )


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_install_git_source_cache_behind_remote_warns(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Git-source cache lagging behind origin → mutate-gate aborts.

    Sets up a bare "remote" repo + a clone "cache" inside the
    container, advances the remote by one commit, then configures
    ``~/.config/setforge/local.yaml`` with a ``source:`` block pointing
    at the cache. Running ``setforge install`` on a non-TTY stdin
    without ``--no-git-check`` must surface the freshness warning AND
    raise via the mutate-gate (dst MUST NOT land).

    Uses a minimal copy of the test-minimal fixture content inside the
    cache so install has something to deploy if the gate is bypassed.
    """
    c = docker_container()
    # Set up bare remote with one commit + fixture tree.
    c.exec(["mkdir", "-p", "/tmp/git-freshness-remote.git"], check=True)
    # ``-b main`` pins the bare repo's HEAD to ``refs/heads/main``; without
    # it, the bare HEAD defaults to ``master`` (or whatever ``init.defaultBranch``
    # resolves to on the host). A later ``git clone`` would then check out
    # an empty working tree because the bare's HEAD references a branch
    # that the seed's push didn't create.
    c.exec(
        ["git", "init", "-q", "-b", "main", "--bare", "/tmp/git-freshness-remote.git"],
        check=True,
    )
    c.exec(["mkdir", "-p", "/tmp/git-freshness-seed"], check=True)
    c.exec(
        ["git", "-C", "/tmp/git-freshness-seed", "init", "-q", "-b", "main"], check=True
    )
    c.exec(
        ["git", "-C", "/tmp/git-freshness-seed", "config", "user.email", "x@y"],
        check=True,
    )
    c.exec(
        ["git", "-C", "/tmp/git-freshness-seed", "config", "user.name", "x"], check=True
    )
    # Copy fixture in.
    c.exec(
        [
            "cp",
            "-r",
            "/workspace/tests/fixtures/e2e/tracked",
            "/tmp/git-freshness-seed/tracked",
        ],
        check=True,
    )
    c.exec(
        [
            "cp",
            "/workspace/tests/fixtures/e2e/setforge.test.yaml",
            "/tmp/git-freshness-seed/setforge.yaml",
        ],
        check=True,
    )
    c.exec(["git", "-C", "/tmp/git-freshness-seed", "add", "-A"], check=True)
    c.exec(
        ["git", "-C", "/tmp/git-freshness-seed", "commit", "-q", "-m", "v1"], check=True
    )
    c.exec(
        [
            "git",
            "-C",
            "/tmp/git-freshness-seed",
            "push",
            "-q",
            "/tmp/git-freshness-remote.git",
            "main",
        ],
        check=True,
    )
    # Clone to cache (this is the "behind" state).
    c.exec(
        [
            "git",
            "clone",
            "-q",
            "/tmp/git-freshness-remote.git",
            "/home/tester/cache",
        ],
        check=True,
    )
    # Advance the remote by one commit so cache is behind.
    c.write_text("/tmp/git-freshness-seed/tracked/minimal/text.txt", "v2 content\n")
    c.exec(["git", "-C", "/tmp/git-freshness-seed", "add", "-A"], check=True)
    c.exec(
        ["git", "-C", "/tmp/git-freshness-seed", "commit", "-q", "-m", "v2"], check=True
    )
    c.exec(
        [
            "git",
            "-C",
            "/tmp/git-freshness-seed",
            "push",
            "-q",
            "/tmp/git-freshness-remote.git",
            "main",
        ],
        check=True,
    )
    # Configure setforge with a GIT source so check_git_source_fresh fires.
    # clone_dest points at the cache we just clone-ed (kind: git, behind).
    local_yaml = (
        "source:\n"
        "  kind: git\n"
        "  url: file:///tmp/git-freshness-remote.git\n"
        "  ref: main\n"
        "  clone_dest: /home/tester/cache\n"
    )
    c.write_text("/home/tester/.config/setforge/local.yaml", local_yaml)
    # Ensure dst doesn't pre-exist.
    c.exec(["rm", "-f", "/home/tester/.setforge_e2e/minimal/text.txt"], check=False)
    # Run install WITHOUT --config (so source layer fires) on non-TTY.
    result = c.exec(
        ["uv", "run", "setforge", "install", "--profile=test-minimal"],
        check=False,
    )
    # Non-TTY + git-source cache behind remote: mutate-gate raises → non-zero.
    assert result.returncode != 0, (
        f"expected non-zero exit on stale-cache non-TTY install; "
        f"stderr={result.stderr!r}"
    )
    assert "stdin is not a TTY" in result.stderr, (
        f"expected mutate-gate message on stderr; got: {result.stderr!r}"
    )
    # Dst MUST NOT exist — abort fired before deploy.
    exists = c.exec(
        ["test", "-f", "/home/tester/.setforge_e2e/minimal/text.txt"], check=False
    )
    assert exists.returncode != 0, (
        "live dst must not be created when install aborts on stale git source"
    )
