"""Docker E2E test ring for ``my-setup`` (dotfiles-nen.9 outer ring).

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
  2. ``uv run my-setup <verb> --profile=test-<x>
     --config=tests/fixtures/e2e/my_setup.test.yaml``
  3. Read the resulting live file(s) and assert parsed/structured equality.

See ``tests/docker/conftest.py`` for the ``docker_image``,
``docker_container``, ``docker_pty_session`` fixtures.
"""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from collections.abc import Callable

# pexpect ships no stubs; types-pexpect not added as a dev dep (per qzq scope).
import pexpect  # type: ignore[import-untyped]
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
    """Run ``my-setup install`` inside the container; return CompletedProcess.

    Uses ``check=False`` so callers can assert on returncode + stderr
    explicitly; the buried ``CalledProcessError`` chain otherwise hides
    the actual stderr in ``__cause__``.

    ``root_args`` are typer root-callback flags (e.g. ``-v``) that must
    precede the ``install`` subcommand. ``extra`` are subcommand-level
    flags (e.g. ``--auto-accept-*``) that follow it.
    """
    cmd = ["uv", "run", "my-setup"]
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
    """Run ``my-setup sync`` inside the container; return CompletedProcess.

    Asserts on ``returncode == 0`` when ``check=True`` (the default)
    for the same readability reasons as :func:`_install`.

    ``root_args`` are typer root-callback flags (e.g. ``-v``) that must
    precede the ``sync`` subcommand. ``extra`` are subcommand-level
    flags (e.g. ``--auto=...``) that follow it.
    """
    cmd = ["uv", "run", "my-setup"]
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


# --- PTY sync wizard helper -----------------------------------------------
#
# Variants P/Q/R/S/S1 share the same scaffold: install YAML deep, write
# a drift body into the live file, drive ``sync`` via PTY through one
# (or two) wizard prompts, then return pre/post snapshots of the target.

_YAML_DEEP_LIVE = "/home/tester/.my_setup_e2e/yaml/deep.yaml"
_YAML_DEEP_TRACKED = "/workspace/tests/fixtures/e2e/tracked/yaml/deep.yaml"


def _drive_pty_sync(
    container: ContainerHandle,
    docker_pty_session: Callable[..., pexpect.spawn],
    *,
    drift_body: str,
    prompts: list[tuple[str, str]],
    snapshot_path: str = _YAML_DEEP_TRACKED,
) -> tuple[str, str]:
    """Install YAML deep, seed live drift, drive ``sync`` through PTY prompts.

    ``prompts`` is a list of ``(expected_prompt_regex, keypress)`` —
    the helper asserts each prompt fires (``idx == 0`` against
    ``[regex, EOF, TIMEOUT]``) and sends the keypress. After draining
    EOF, returns ``(pre, post)`` content of ``snapshot_path``.

    Asserting ``idx == 0`` is load-bearing: a bare three-alternative
    ``expect()`` would accept ``EOF`` / ``TIMEOUT`` as a match, masking
    cases where the wizard hung before prompting (e.g. test passes
    vacuously because the keypress went into the void and tracked
    legitimately didn't change).
    """
    _install(container, "test-yaml-deep")
    pre = container.read_text(snapshot_path)
    container.write_text(_YAML_DEEP_LIVE, drift_body)
    session = docker_pty_session(
        container,
        [
            "uv",
            "run",
            "my-setup",
            "sync",
            "--profile=test-yaml-deep",
            f"--config={CONFIG_FIXTURE}",
        ],
        timeout=120,
    )
    for regex, key in prompts:
        idx = session.expect([regex, pexpect.EOF, pexpect.TIMEOUT], timeout=30)
        assert idx == 0, (
            f"wizard prompt {regex!r} never appeared; saw: {session.before!r}"
        )
        session.send(key)
    session.expect(pexpect.EOF)
    post = container.read_text(snapshot_path)
    return pre, post


def _drift_body(user_sub: str) -> str:
    """Render the canonical drift YAML body with the given userSub value."""
    return textwrap.dedent(
        f"""\
        trackedKey: tracked-value
        settings:
          trackedSub: tracked-sub-value
          userSub: {user_sub}
        """
    )


# ===========================================================================
# Section: Install mechanism variants (B-L)
# ===========================================================================


# --- Variant B ------------------------------------------------------------


def test_install_minimal_floor(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """B: plain-text byte copy lands at dst with matching content."""
    c = docker_container()
    _install(c, "test-minimal")
    assert (
        _read_live(c, ".my_setup_e2e/minimal/text.txt") == "hello from test-minimal\n"
    )


# --- Variant C ------------------------------------------------------------


def test_install_text_sections_no_live(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """C: preserve_user_sections=true, no live content → dst equals tracked.

    install rewrites end markers with an embedded ``hash=<sha256>``
    segment (post-9by: tracked is also stamped). The body is the
    load-bearing assertion; the end marker may carry the new hash
    segment or be the legacy untagged form.
    """
    c = docker_container()
    _install(c, "test-text-sections")
    live = _read_live(c, ".my_setup_e2e/sections/marked.md")
    assert "<!-- my-setup:user-section start host-local notes -->" in live
    assert "default notes (tracked side)" in live
    assert re.search(
        r"<!-- my-setup:user-section end host-local notes( hash=[0-9a-f]{64})? -->",
        live,
    )


# --- Variant D ------------------------------------------------------------


def test_install_text_sections_preserve_user_content(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """D: pre-seed marker-bracketed live content; survives the next install."""
    c = docker_container()
    # First install to produce baseline (so the live file exists for editing).
    _install(c, "test-text-sections")
    # Mutate the live file's marker body — user-local edit.
    pre_seeded = textwrap.dedent(
        """\
        # local title overrides tracked title

        <!-- my-setup:user-section start host-local notes -->
        host-local marker body content
        <!-- my-setup:user-section end host-local notes -->

        Trailing live content (not preserved on next install).
        """
    )
    c.write_text("/home/tester/.my_setup_e2e/sections/marked.md", pre_seeded)

    _install(c, "test-text-sections")
    live = _read_live(c, ".my_setup_e2e/sections/marked.md")
    # Marker body preserved (inside-markers user content survives).
    assert "host-local marker body content" in live
    # Outside-markers content reverted to tracked.
    assert "Trailing tracked content." in live


# --- Variant E ------------------------------------------------------------


def test_install_json_byte_copy(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """E: JSON dotfile byte-copies; parsed result matches tracked."""
    c = docker_container()
    _install(c, "test-json")
    payload = json.loads(_read_live(c, ".my_setup_e2e/json/settings.json"))
    assert payload == {
        "settingA": "tracked-value-A",
        "settingB": 42,
        "settingC": ["alpha", "beta"],
    }


# --- Variant F ------------------------------------------------------------


def test_install_jsonc_shallow_no_live(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """F: JSONC byte copy + comments preserved when no preserve overlay applies."""
    c = docker_container()
    _install(c, "test-jsonc-shallow")
    live = _read_live(c, ".my_setup_e2e/jsonc/shallow.json")
    assert "// tracked side comment" in live
    assert "tracked-placeholder-A" in live
    assert "tracked-placeholder-B" in live


# --- Variant G ------------------------------------------------------------


def test_install_jsonc_shallow_preserve_overlay(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """G: pre-seed preserve keys only; live values overlaid into tracked.

    Mutating non-preserve top-level keys would trigger the install
    drift-gate (CompareStatus.DRIFTED with unexpected_drift_keys); the
    variant under test is the overlay PATH, not the drift-gate path,
    so the pre-seed only mutates preserve_user_keys entries.
    """
    c = docker_container()
    # First install to produce baseline.
    _install(c, "test-jsonc-shallow")
    # Mutate ONLY preserve_user_keys entries on the live side.
    live_path = "/home/tester/.my_setup_e2e/jsonc/shallow.json"
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
    live = _read_live(c, ".my_setup_e2e/jsonc/shallow.json")
    # userKeyA / userKeyB preserved from live; trackedKey is the tracked value.
    assert "live-A" in live
    assert "live-B" in live
    assert "tracked-value" in live
    # Tracked-side comment present (it's part of the tracked source).
    assert "// tracked side comment" in live


# --- Variant H ------------------------------------------------------------


def test_install_jsonc_deep_preserve_overlay(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """H: pre-seed live drift inside the deep preserve subtree only.

    `settings` is in preserve_user_keys_deep so ANY sub-key drift
    beneath it is expected; mutating top-level non-preserve keys
    would trigger the drift-gate. Pre-seed only inside `settings`.
    """
    c = docker_container()
    _install(c, "test-jsonc-deep")
    live_path = "/home/tester/.my_setup_e2e/jsonc/deep.json"
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
    live = _read_live(c, ".my_setup_e2e/jsonc/deep.json")
    # Deep merge: live userSub survives; tracked trackedSub keeps its
    # tracked value (deep-merge is parent-first union; live wins on
    # overlap, tracked keeps tracked-only keys).
    assert "live-user-value" in live
    assert "tracked-sub-value" in live
    # Top-level non-preserve: trackedKey is the tracked value.
    assert "tracked-value" in live


# --- Variant H1 -----------------------------------------------------------


def test_install_yaml_shallow_preserve_overlay(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """H1: shallow preserve for YAML — yaml_merge.py parity with jsonc.py.

    Pre-seed only the preserve keys (mutating non-preserve top-level
    keys would trigger the install drift-gate).
    """
    c = docker_container()
    _install(c, "test-yaml-shallow")
    live_path = "/home/tester/.my_setup_e2e/yaml/shallow.yaml"
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
    live = _read_live(c, ".my_setup_e2e/yaml/shallow.yaml")
    assert "live-A" in live
    assert "live-B" in live
    assert "tracked-value" in live  # trackedKey is the tracked value


# --- Variant H2 -----------------------------------------------------------


def test_install_yaml_deep_preserve_overlay(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """H2: deep preserve for YAML — yaml_merge.py deep-merge parity.

    Pre-seed drift inside the `settings` deep preserve subtree only;
    keep top-level non-preserve keys at their tracked values.
    """
    c = docker_container()
    _install(c, "test-yaml-deep")
    live_path = "/home/tester/.my_setup_e2e/yaml/deep.yaml"
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
    live = _read_live(c, ".my_setup_e2e/yaml/deep.yaml")
    assert "live-user-value" in live  # live deep sub-key survives
    assert "tracked-sub-value" in live  # tracked-only deep sub-key kept
    assert "tracked-value" in live  # top-level untouched


# --- Variant I ------------------------------------------------------------


def test_install_directory_copy(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """I: directory tree copied recursively, nested files included."""
    c = docker_container()
    _install(c, "test-directory")
    assert _read_live(c, ".my_setup_e2e/directory/file-a.txt") == "file-a content\n"
    assert _read_live(c, ".my_setup_e2e/directory/file-b.txt") == "file-b content\n"
    assert (
        _read_live(c, ".my_setup_e2e/directory/nested/file-c.txt")
        == "file-c content (nested)\n"
    )


# --- Variant J ------------------------------------------------------------


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
        ["find", "/home/tester", "-name", "my-setup-e2e-template.txt"],
    )
    matches = [line for line in proc.stdout.splitlines() if line.strip()]
    assert matches, (
        f"templated dst not found anywhere under /home/tester: {proc.stdout!r}"
    )
    # And the content is the rendered file.
    content = c.read_text(matches[0])
    assert content == "templated file (dst path was Jinja2-rendered)\n"


# --- Variant K ------------------------------------------------------------


def test_install_chain_resolution_and_bootstrap(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """K: 3-level extends chain; parent-first dotfile dedup + bootstrap stubs."""
    c = docker_container()
    _install(c, "test-chain-child")
    root = ".my_setup_e2e/chain"
    assert _read_live(c, f"{root}/grand.txt") == "grand-content\n"
    assert _read_live(c, f"{root}/base.txt") == "base-content\n"
    assert _read_live(c, f"{root}/child.txt") == "child-content\n"
    # Bootstrap stubs created at all three chain levels.
    for stub in ("bootstrap-grand.txt", "bootstrap-base.txt", "bootstrap-child.txt"):
        proc = c.exec(["test", "-f", f"/home/tester/{root}/{stub}"], check=False)
        assert proc.returncode == 0, f"missing bootstrap stub: {stub}"


# --- Variant L ------------------------------------------------------------


def test_install_comprehensive_plugins_extensions(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """L: full sweep — dotfiles + marketplaces + plugins + extensions + bootstrap.

    Asserts the dotfile leg lands cleanly. The plugin + extension legs hit
    real ``claude`` and ``code`` binaries; this test verifies install
    exits 0 (= reconcile completed without raising) and the dotfile
    layer is materialised. Plugin / extension state cross-checks are
    asserted by the bound list commands when claude/code are usable in
    CI; failures there are surfaced via install's non-zero exit.
    """
    c = docker_container()
    # First-time install: every dst is absent, so install bypasses the
    # drift gate without needing --auto-accept-* flags.
    proc = _install(c, "test-comprehensive")
    assert proc.returncode == 0, proc.stderr
    root = ".my_setup_e2e/comprehensive"
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


# --- Variant L1 (dotfiles-58x verbosity surface) --------------------------


def test_install_verbose_emits_my_setup_debug(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``-v`` flag surfaces ``my_setup.claude_plugins`` DEBUG from a real subprocess.

    Closes the e2e scope gap left by the in-process CliRunner unit tests
    in :mod:`tests.test_cli_e2e` (which prove flag mechanics inside the
    test interpreter but not real-subprocess logging propagation). Runs
    the comprehensive profile under ``-v`` in a fresh Debian container
    and asserts a ``my_setup.claude_plugins DEBUG:`` line lands on
    stderr — proving the dotfiles-58x verbosity surface threads
    end-to-end through CLI startup, ``logging.basicConfig(stream=sys.stderr)``,
    and the production ``my_setup.claude_plugins`` LOGGER call sites
    (``_run_git`` / ``_clone_marketplace`` / ``_cache_origin_url``).

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
    # claude_plugins (_run_git / _clone_marketplace / _cache_origin_url)
    # are exercised on the install path; their success-path stderr-DEBUG
    # blocks are what this test verifies.
    c.write_text(
        "/home/tester/.config/my-setup/local.yaml",
        textwrap.dedent(
            """\
            claude:
              install_mode: local-clone
            """
        ),
    )
    result = _install(c, "test-comprehensive", root_args=["-v"])
    assert "my_setup.claude_plugins DEBUG:" in result.stderr, (
        f"expected 'my_setup.claude_plugins DEBUG:' in stderr; "
        f"first 800 chars: {result.stderr[:800]}"
    )


# ===========================================================================
# Section: Sync + wizard variants (M-S1)
# ===========================================================================


# --- Variant M ------------------------------------------------------------


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


def test_sync_auto_use_live_silent_absorb(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """N: pre-seed drift, --auto=use-live absorbs live into tracked."""
    c = docker_container()
    _install(c, "test-minimal")
    c.write_text("/home/tester/.my_setup_e2e/minimal/text.txt", "live-only-content\n")
    _sync(c, "test-minimal", extra=["--auto=use-live"])
    tracked = c.read_text("/workspace/tests/fixtures/e2e/tracked/minimal/text.txt")
    assert "live-only-content" in tracked


# --- Variant O ------------------------------------------------------------


def test_sync_auto_keep_tracked_refuse_absorb(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """O: pre-seed drift on YAML deep; --auto=keep-tracked leaves tracked unchanged.

    Uses YAML deep (not JSONC deep) because capture-time wizard
    deep-merge walking is intentionally skipped for JSONC per
    my_setup/capture_wizard.py:175 (deep_paths_to_walk = []
    for JSONC). YAML deep is where the capture wizard's auto-accept
    plumbing actually fires today.
    """
    c = docker_container()
    _install(c, "test-yaml-deep")
    pre = c.read_text("/workspace/tests/fixtures/e2e/tracked/yaml/deep.yaml")
    # Pre-seed live drift inside the preserve_user_keys_deep `settings` subtree.
    c.write_text(
        "/home/tester/.my_setup_e2e/yaml/deep.yaml",
        textwrap.dedent(
            """\
            trackedKey: tracked-value
            settings:
              trackedSub: tracked-sub-value
              userSub: live-drift-value
            """
        ),
    )
    _sync(c, "test-yaml-deep", extra=["--auto=keep-tracked"], check=False)
    # keep-tracked refuses absorb — tracked content unchanged.
    post = c.read_text("/workspace/tests/fixtures/e2e/tracked/yaml/deep.yaml")
    assert pre == post


# --- Variant P (interactive: pty + 'k') -----------------------------------
#
# Wizard surface note (verified against my_setup/capture_wizard.py:175):
# the capture-time wizard's deep-merge walker SKIPS JSONC files
# (deep_paths_to_walk = preserve_user_keys_deep if fmt != "jsonc"
# else []). JSONC deep-merge per-sub-key drift is handled by deploy's
# overlay, not the capture-time wizard. So PTY variants P/Q/R/S
# target YAML deep (where the walker actually fires), not JSONC.
# This is the empirical resolution of open question 8.


def test_sync_interactive_keep_via_pty(
    docker_container: Callable[..., ContainerHandle],
    docker_pty_session: Callable[..., pexpect.spawn],
) -> None:
    """P: docker exec -it + pexpect; send 'k' on YAML deep drift; tracked unchanged."""
    c = docker_container()
    pre, post = _drive_pty_sync(
        c,
        docker_pty_session,
        drift_body=_drift_body("live-drift-value"),
        prompts=[("Choice", "k")],
    )
    # k = keep tracked → tracked is unchanged after the sync.
    assert pre == post


# --- Variant Q (interactive: pty + 'u') -----------------------------------


def test_sync_interactive_use_via_pty(
    docker_container: Callable[..., ContainerHandle],
    docker_pty_session: Callable[..., pexpect.spawn],
) -> None:
    """Q: pexpect; send 'u' on YAML deep drift; tracked absorbs live."""
    c = docker_container()
    _, post = _drive_pty_sync(
        c,
        docker_pty_session,
        drift_body=_drift_body("live-absorbed-value"),
        prompts=[("Choice", "u")],
    )
    assert "live-absorbed-value" in post


# --- Variant R (interactive: pty + 's') -----------------------------------


def test_sync_interactive_skip_via_pty(
    docker_container: Callable[..., ContainerHandle],
    docker_pty_session: Callable[..., pexpect.spawn],
) -> None:
    """R: pexpect; send 's' (save-as-preserved); my_setup.yaml gets the key added.

    Per ``my_setup/wizard.py`` _action_save_as_preserved (verified
    against wizard source per open question 8): ``s`` appends
    ``item.key_path`` to the dotfile's ``preserve_user_keys`` list in
    my_setup.yaml. The tracked file is unchanged; only the YAML
    config gets the new preserve entry.
    """
    c = docker_container()
    # Snapshot the YAML config (the file that ``s`` mutates) rather than
    # the tracked-deep yaml file (which ``s`` leaves alone).
    pre_yaml, post_yaml = _drive_pty_sync(
        c,
        docker_pty_session,
        drift_body=_drift_body("live-value-for-s"),
        prompts=[("Choice", "s")],
        snapshot_path=f"/workspace/{CONFIG_FIXTURE}",
    )
    # The action appends `settings.userSub` (the diverged key path) to
    # the dotfile's preserve_user_keys list in the YAML config. Diff
    # pre vs post and assert the new preserve entry is in the diff —
    # ``"userSub" in pre_yaml`` is already true (the fixture mentions
    # it elsewhere), so a bare ``in post_yaml`` check is vacuous.
    assert pre_yaml != post_yaml
    diff_lines = set(post_yaml.splitlines()) - set(pre_yaml.splitlines())
    diff = "\n".join(diff_lines)
    assert "settings.userSub" in diff or "userSub" in diff, (
        f"preserve entry not in diff between pre/post YAML config:\n{diff}"
    )


# --- Variant S (interactive: pty + 'm') -----------------------------------


def test_sync_interactive_merge_via_pty(
    docker_container: Callable[..., ContainerHandle],
    docker_pty_session: Callable[..., pexpect.spawn],
) -> None:
    """S: pexpect; send 'm' (manual edit) then 'n' (decline editor) → pending state.

    Per ``my_setup/wizard.py`` _action_manual_edit (verified against
    wizard source per open question 8): ``m`` prompts ``y/n``; ``y``
    launches ``$EDITOR``, ``n`` returns MANUAL_PENDING which halts the
    wizard at this drift item. The pending state means tracked is
    unchanged for this item — perfect for asserting in an automated
    test without a real interactive editor.
    """
    c = docker_container()
    pre, post = _drive_pty_sync(
        c,
        docker_pty_session,
        drift_body=_drift_body("live-value-for-m"),
        prompts=[("Choice", "m"), ("y/n", "n")],
    )
    # Manual edit declined → tracked unchanged (MANUAL_PENDING halts).
    assert pre == post


# --- Variant S1 (YAML deep wizard parity) ---------------------------------


def test_sync_yaml_deep_interactive_use_via_pty(
    docker_container: Callable[..., ContainerHandle],
    docker_pty_session: Callable[..., pexpect.spawn],
) -> None:
    """S1: same shape as Q but on a YAML deep dotfile — yaml_merge round-trip."""
    c = docker_container()
    _, post = _drive_pty_sync(
        c,
        docker_pty_session,
        drift_body=_drift_body("live-yaml-absorbed"),
        prompts=[("Choice", "u")],
    )
    assert "live-yaml-absorbed" in post


# ===========================================================================
# Section: Lifecycle variants (T-W)
# ===========================================================================


# --- Variant T ------------------------------------------------------------


def test_compare_reports_drift_exit_nonzero(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """T: install, mutate live, compare --check --strict exits non-zero."""
    c = docker_container()
    _install(c, "test-minimal")
    c.write_text("/home/tester/.my_setup_e2e/minimal/text.txt", "live-drift\n")
    proc = c.exec(
        [
            "uv",
            "run",
            "my-setup",
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


def test_install_then_revert_restores_state(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """U: install creates live file; revert removes it (no prior content)."""
    c = docker_container()
    _install(c, "test-minimal")
    # Confirm the file exists post-install.
    assert (
        c.exec(
            ["test", "-f", "/home/tester/.my_setup_e2e/minimal/text.txt"], check=False
        ).returncode
        == 0
    )
    revert = c.exec(
        [
            "uv",
            "run",
            "my-setup",
            "revert",
            "--profile=test-minimal",
            f"--config={CONFIG_FIXTURE}",
        ]
    )
    assert revert.returncode == 0
    # File is gone after revert (it was created from absence on install).
    assert (
        c.exec(
            ["test", "-f", "/home/tester/.my_setup_e2e/minimal/text.txt"], check=False
        ).returncode
        != 0
    )


# --- Variant V ------------------------------------------------------------


def test_install_idempotent_second_run_noop(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """V: install twice; second run exits 0 with consistent dst state."""
    c = docker_container()
    _install(c, "test-minimal")
    first = c.read_text("/home/tester/.my_setup_e2e/minimal/text.txt")
    second = _install(c, "test-minimal")
    assert second.returncode == 0
    after = c.read_text("/home/tester/.my_setup_e2e/minimal/text.txt")
    assert first == after


# --- Variant W ------------------------------------------------------------


def test_validate_clean_yaml_exit_zero(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """W: validate --all against the fixture config exits 0."""
    c = docker_container()
    proc = c.exec(
        ["uv", "run", "my-setup", "validate", "--all", f"--config={CONFIG_FIXTURE}"]
    )
    assert proc.returncode == 0
    assert "ok" in proc.stdout


# ===========================================================================
# Section: Legacy (pre-9by) marker migration (dotfiles-9ln)
# ===========================================================================
#
# These variants exercise the install / compare flow against a live
# file whose user-section markers are in the pre-9by shape: no
# host-local|shared semantics keyword on the start marker, and no
# ``hash=<sha256>`` segment on the end marker. The strict parser rejects
# these; install opts into ``allow_legacy=True`` to migrate the file in
# place; compare / sync / merge refuse with an actionable error pointing
# the user at install.


_LEGACY_BODY = "host-local body content that must survive migration\n"
_LEGACY_LIVE_TEXT = (
    "# local title overrides tracked title\n"
    "\n"
    "<!-- my-setup:user-section start notes -->\n"
    f"{_LEGACY_BODY}"
    "<!-- my-setup:user-section end notes -->\n"
    "\n"
    "Trailing live content.\n"
)


def test_compare_legacy_live_refuses_with_pointer_to_install(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """compare refuses legacy live markers with actionable MySetupError.

    Seeds a pre-9by-shaped live ``marked.md`` (untagged markers, no
    hash segment) and runs ``my-setup compare``; asserts non-zero
    exit AND that the combined stdout+stderr names ``my-setup
    install`` as the next step. Without the refusal guard, the
    strict parser would leak an opaque ``MarkerError: line N: missing
    required keyword`` instead.
    """
    c = docker_container()
    c.write_text(
        "/home/tester/.my_setup_e2e/sections/marked.md",
        _LEGACY_LIVE_TEXT,
    )
    proc = c.exec(
        [
            "uv",
            "run",
            "my-setup",
            "compare",
            "--profile=test-text-sections",
            f"--config={CONFIG_FIXTURE}",
        ],
        check=False,
    )
    assert proc.returncode != 0, (
        f"compare should refuse legacy live; "
        f"got returncode={proc.returncode}\nstdout:{proc.stdout}\nstderr:{proc.stderr}"
    )
    combined = (proc.stdout + proc.stderr).lower()
    assert "legacy" in combined, f"expected 'legacy' in output: {combined!r}"
    assert "my-setup install" in proc.stdout + proc.stderr, (
        f"expected 'my-setup install' in output: "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_install_legacy_live_markers_preserves_body_and_retags(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """install migrates legacy live markers in place: body bytes preserved,
    end markers re-tagged with the ``host-local`` semantics keyword and a
    ``hash=<64-hex>`` segment that matches the migrated body."""
    c = docker_container()
    live_path = "/home/tester/.my_setup_e2e/sections/marked.md"
    c.write_text(live_path, _LEGACY_LIVE_TEXT)

    _install(c, "test-text-sections")

    live_post = c.read_text(live_path)
    # Body bytes byte-preserved from the seed (the legacy body wins because
    # it was inside the markers — sections.merge_sections preserves it).
    assert _LEGACY_BODY in live_post, (
        f"legacy body should survive migration: {live_post!r}"
    )
    # Every end marker carries the new tagged shape: semantics + hash=64hex.
    match = re.search(
        r"<!-- my-setup:user-section end host-local notes hash=([0-9a-f]{64}) -->",
        live_post,
    )
    assert match is not None, (
        f"expected end marker with semantics + hash=64hex; got: {live_post!r}"
    )
    # No legacy untagged markers remain.
    assert "<!-- my-setup:user-section start notes -->" not in live_post
    assert "<!-- my-setup:user-section end notes -->" not in live_post


def test_compare_after_legacy_install_is_clean(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """After install migrates the legacy live file, compare exits 0:
    the migrated live is strict-clean and the reconciler sees no
    unexpected drift."""
    c = docker_container()
    live_path = "/home/tester/.my_setup_e2e/sections/marked.md"
    c.write_text(live_path, _LEGACY_LIVE_TEXT)

    # First migrate via install.
    _install(c, "test-text-sections")

    # Then compare must succeed (no longer legacy; reconciler sees the
    # host-local body as expected drift, which compare without --check
    # exits 0 on).
    proc = c.exec(
        [
            "uv",
            "run",
            "my-setup",
            "compare",
            "--profile=test-text-sections",
            f"--config={CONFIG_FIXTURE}",
        ],
        check=False,
    )
    assert proc.returncode == 0, (
        f"compare after legacy migration should exit 0; "
        f"got returncode={proc.returncode}\nstdout:{proc.stdout}\nstderr:{proc.stderr}"
    )


# ===========================================================================
# Section: Prose-reviewer artifacts (dotfiles-h5k)
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
# Implicitly verifies (per dotfiles-h5k --notes): 9by's strict-tag
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


def test_install_deploys_new_reviewing_markdown_skill(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Install deploys reviewing-markdown SKILL.md byte-identical to tracked."""
    c = docker_container()
    _install(c, "test-prose-reviewers")
    live = c.read_text("/home/tester/.claude/skills/reviewing-markdown/SKILL.md")
    tracked = c.read_text(_PROSE_SKILL_TRACKED)
    assert live == tracked


def test_compare_after_install_clean_no_drift_for_new_agents_and_skill(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """After clean install, compare --check --strict exits 0 (no drift)."""
    c = docker_container()
    _install(c, "test-prose-reviewers")
    proc = c.exec(
        [
            "uv",
            "run",
            "my-setup",
            "compare",
            "--profile=test-prose-reviewers",
            f"--config={CONFIG_FIXTURE}",
            "--check",
        ],
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


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
            "my-setup",
            "revert",
            "--profile=test-prose-reviewers",
            f"--config={CONFIG_FIXTURE}",
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


def test_merge_legacy_live_refuses_with_pointer_to_install(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """merge on a pre-9by live file refuses with the actionable error.

    Pairs with the unit-level
    ``test_merge_refuses_legacy_live_with_actionable_error`` in
    ``tests/test_cli_section_reconcile.py``. Seeds a pre-9by-shaped live
    ``~/.claude/CLAUDE.md`` (no ``host-local``/``shared`` semantics keyword
    on the start marker, no ``hash=<sha256>`` segment on the end marker) and
    runs ``my-setup merge --profile=vm-headless``; asserts non-zero exit
    AND that combined stdout+stderr names ``my-setup install`` as the next
    step. Without the refusal guard, ``merge`` would proceed silently into
    ``compare_profile`` instead of surfacing the actionable error.
    """
    c = docker_container()
    c.write_text(
        "/home/tester/.claude/CLAUDE.md",
        "intro\n"
        "<!-- my-setup:user-section start workflow -->\n"
        "- body line\n"
        "<!-- my-setup:user-section end workflow -->\n"
        "outro\n",
    )
    result = c.exec(
        ["uv", "run", "my-setup", "merge", "--profile=vm-headless"],
        check=False,
    )
    assert result.returncode != 0, (
        f"merge should refuse legacy live; "
        f"got returncode={result.returncode}\n"
        f"stdout:{result.stdout}\nstderr:{result.stderr}"
    )
    combined = (result.stdout or "") + (result.stderr or "")
    assert "Run 'uv run my-setup install" in combined, (
        f"expected 'Run 'uv run my-setup install' in output: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_sync_legacy_live_refuses_with_pointer_to_install(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """sync on a pre-9by live file refuses with the actionable error.

    Pairs with the unit-level
    ``test_sync_refuses_legacy_live_with_actionable_error`` in
    ``tests/test_cli_section_reconcile.py``. Seeds a pre-9by-shaped live
    ``~/.claude/CLAUDE.md`` and runs ``my-setup sync --profile=vm-headless``;
    asserts non-zero exit AND that combined stdout+stderr names
    ``my-setup install`` as the next step.
    """
    c = docker_container()
    c.write_text(
        "/home/tester/.claude/CLAUDE.md",
        "intro\n"
        "<!-- my-setup:user-section start workflow -->\n"
        "- body line\n"
        "<!-- my-setup:user-section end workflow -->\n"
        "outro\n",
    )
    result = c.exec(
        ["uv", "run", "my-setup", "sync", "--profile=vm-headless"],
        check=False,
    )
    assert result.returncode != 0, (
        f"sync should refuse legacy live; "
        f"got returncode={result.returncode}\n"
        f"stdout:{result.stdout}\nstderr:{result.stderr}"
    )
    combined = (result.stdout or "") + (result.stderr or "")
    assert "Run 'uv run my-setup install" in combined, (
        f"expected 'Run 'uv run my-setup install' in output: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
