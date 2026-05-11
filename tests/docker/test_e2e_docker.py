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
  2. ``uv run my-setup <verb> --profile=test-<x> --config=tests/fixtures/e2e/my_setup.test.yaml``
  3. Read the resulting live file(s) and assert parsed/structured equality.

See ``tests/docker/conftest.py`` for the ``docker_image``,
``docker_container``, ``docker_pty_session`` fixtures.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Callable

import pytest

# ``ContainerHandle`` is exported by the sibling conftest; we import
# only for type hints. Avoid a hard ImportError when pytest collects
# this file without the conftest having loaded yet.
try:
    from tests.docker.conftest import ContainerHandle  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — collect-only path
    ContainerHandle = object  # type: ignore[assignment,misc]

pytestmark = pytest.mark.e2e_docker


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


_CONFIG = "tests/fixtures/e2e/my_setup.test.yaml"


def _install(
    container: ContainerHandle,
    profile: str,
    *,
    extra: list[str] | None = None,
) -> object:
    """Run ``my-setup install`` inside the container; return CompletedProcess."""
    cmd = [
        "uv",
        "run",
        "my-setup",
        "install",
        f"--profile={profile}",
        f"--config={_CONFIG}",
    ]
    if extra:
        cmd.extend(extra)
    return container.exec(cmd)  # type: ignore[attr-defined]


def _sync(
    container: ContainerHandle,
    profile: str,
    *,
    extra: list[str] | None = None,
    check: bool = True,
) -> object:
    """Run ``my-setup sync`` inside the container; return CompletedProcess."""
    cmd = [
        "uv",
        "run",
        "my-setup",
        "sync",
        f"--profile={profile}",
        f"--config={_CONFIG}",
    ]
    if extra:
        cmd.extend(extra)
    return container.exec(cmd, check=check)  # type: ignore[attr-defined]


def _read_live(container: ContainerHandle, path: str) -> str:
    """Read a live (dst) file in the container's $HOME tree."""
    return container.read_text(f"/home/tester/{path}")  # type: ignore[attr-defined]


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
    """C: preserve_user_sections=true, no live content → dst equals tracked."""
    c = docker_container()
    _install(c, "test-text-sections")
    live = _read_live(c, ".my_setup_e2e/sections/marked.md")
    # Marker pair + default body all present verbatim.
    assert "<!-- my-setup:user-section start notes -->" in live
    assert "default notes (tracked side)" in live
    assert "<!-- my-setup:user-section end notes -->" in live


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

        <!-- my-setup:user-section start notes -->
        host-local marker body content
        <!-- my-setup:user-section end notes -->

        Trailing live content (not preserved on next install).
        """
    )
    c.write_text("/home/tester/.my_setup_e2e/sections/marked.md", pre_seeded)  # type: ignore[attr-defined]

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
    c.write_text(  # type: ignore[attr-defined]
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
    c.write_text(  # type: ignore[attr-defined]
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
    c.write_text(  # type: ignore[attr-defined]
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
    c.write_text(  # type: ignore[attr-defined]
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
    proc = c.exec(  # type: ignore[attr-defined]
        ["find", "/home/tester", "-name", "my-setup-e2e-template.txt"],
    )
    matches = [line for line in proc.stdout.splitlines() if line.strip()]
    assert matches, (
        f"templated dst not found anywhere under /home/tester: {proc.stdout!r}"
    )
    # And the content is the rendered file.
    content = c.read_text(matches[0])  # type: ignore[attr-defined]
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
        proc = c.exec(  # type: ignore[attr-defined]
            ["test", "-f", f"/home/tester/{root}/{stub}"], check=False
        )
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
    # Use --auto-accept-live for first-time install to avoid drift gating
    # if any of the comprehensive dotfiles' dst paths already exist.
    proc = _install(c, "test-comprehensive")
    assert proc.returncode == 0, getattr(proc, "stderr", "")  # type: ignore[attr-defined]
    root = ".my_setup_e2e/comprehensive"
    assert "comprehensive notes" in _read_live(c, f"{root}/notes.md")
    assert json.loads(_read_live(c, f"{root}/data.json")) == {
        "key": "comprehensive-value"
    }
    assert "comprehensive-tracked" in _read_live(c, f"{root}/preserve-settings.json")
    assert "comprehensive-tracked-yaml" in _read_live(c, f"{root}/config.yaml")
    proc = c.exec(  # type: ignore[attr-defined]
        ["test", "-f", f"/home/tester/{root}/bootstrap-stub.txt"], check=False
    )
    assert proc.returncode == 0, "comprehensive bootstrap stub missing"


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
    pre = c.read_text("/workspace/tests/fixtures/e2e/tracked/minimal/text.txt")  # type: ignore[attr-defined]
    _sync(c, "test-minimal")
    post = c.read_text("/workspace/tests/fixtures/e2e/tracked/minimal/text.txt")  # type: ignore[attr-defined]
    assert pre == post


# --- Variant N ------------------------------------------------------------


def test_sync_auto_use_live_silent_absorb(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """N: pre-seed drift, --auto=use-live absorbs live into tracked."""
    c = docker_container()
    _install(c, "test-minimal")
    c.write_text(  # type: ignore[attr-defined]
        "/home/tester/.my_setup_e2e/minimal/text.txt", "live-only-content\n"
    )
    _sync(c, "test-minimal", extra=["--auto=use-live"])
    tracked = c.read_text(  # type: ignore[attr-defined]
        "/workspace/tests/fixtures/e2e/tracked/minimal/text.txt"
    )
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
    pre = c.read_text(  # type: ignore[attr-defined]
        "/workspace/tests/fixtures/e2e/tracked/yaml/deep.yaml"
    )
    # Pre-seed live drift inside the preserve_user_keys_deep `settings` subtree.
    c.write_text(  # type: ignore[attr-defined]
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
    post = c.read_text(  # type: ignore[attr-defined]
        "/workspace/tests/fixtures/e2e/tracked/yaml/deep.yaml"
    )
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
    docker_pty_session: Callable[..., object],
) -> None:
    """P: docker exec -it + pexpect; send 'k' on YAML deep drift; tracked unchanged."""
    import pexpect  # type: ignore[import-untyped]

    c = docker_container()
    _install(c, "test-yaml-deep")
    pre = c.read_text("/workspace/tests/fixtures/e2e/tracked/yaml/deep.yaml")  # type: ignore[attr-defined]
    c.write_text(  # type: ignore[attr-defined]
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
    session = docker_pty_session(
        c,
        [
            "uv",
            "run",
            "my-setup",
            "sync",
            "--profile=test-yaml-deep",
            f"--config={_CONFIG}",
        ],
        timeout=120,
    )
    session.expect(["Choice", pexpect.EOF, pexpect.TIMEOUT])  # type: ignore[attr-defined]
    session.send("k")  # type: ignore[attr-defined]
    session.expect(pexpect.EOF)  # type: ignore[attr-defined]
    post = c.read_text("/workspace/tests/fixtures/e2e/tracked/yaml/deep.yaml")  # type: ignore[attr-defined]
    # k = keep tracked → tracked is unchanged after the sync.
    assert pre == post


# --- Variant Q (interactive: pty + 'u') -----------------------------------


def test_sync_interactive_use_via_pty(
    docker_container: Callable[..., ContainerHandle],
    docker_pty_session: Callable[..., object],
) -> None:
    """Q: pexpect; send 'u' on YAML deep drift; tracked absorbs live."""
    import pexpect  # type: ignore[import-untyped]

    c = docker_container()
    _install(c, "test-yaml-deep")
    c.write_text(  # type: ignore[attr-defined]
        "/home/tester/.my_setup_e2e/yaml/deep.yaml",
        textwrap.dedent(
            """\
            trackedKey: tracked-value
            settings:
              trackedSub: tracked-sub-value
              userSub: live-absorbed-value
            """
        ),
    )
    session = docker_pty_session(
        c,
        [
            "uv",
            "run",
            "my-setup",
            "sync",
            "--profile=test-yaml-deep",
            f"--config={_CONFIG}",
        ],
        timeout=120,
    )
    session.expect(["Choice", pexpect.EOF, pexpect.TIMEOUT])  # type: ignore[attr-defined]
    session.send("u")  # type: ignore[attr-defined]
    session.expect(pexpect.EOF)  # type: ignore[attr-defined]
    post = c.read_text("/workspace/tests/fixtures/e2e/tracked/yaml/deep.yaml")  # type: ignore[attr-defined]
    assert "live-absorbed-value" in post


# --- Variant R (interactive: pty + 's') -----------------------------------


def test_sync_interactive_skip_via_pty(
    docker_container: Callable[..., ContainerHandle],
    docker_pty_session: Callable[..., object],
) -> None:
    """R: pexpect; send 's' (save-as-preserved); my_setup.yaml gets the key added.

    Per ``my_setup/wizard.py`` _action_save_as_preserved (verified
    against wizard source per open question 8): ``s`` appends
    ``item.key_path`` to the dotfile's ``preserve_user_keys`` list in
    my_setup.yaml. The tracked file is unchanged; only the YAML
    config gets the new preserve entry.
    """
    import pexpect  # type: ignore[import-untyped]

    c = docker_container()
    _install(c, "test-yaml-deep")
    pre_yaml = c.read_text(f"/workspace/{_CONFIG}")  # type: ignore[attr-defined]
    c.write_text(  # type: ignore[attr-defined]
        "/home/tester/.my_setup_e2e/yaml/deep.yaml",
        textwrap.dedent(
            """\
            trackedKey: tracked-value
            settings:
              trackedSub: tracked-sub-value
              userSub: live-value-for-s
            """
        ),
    )
    session = docker_pty_session(
        c,
        [
            "uv",
            "run",
            "my-setup",
            "sync",
            "--profile=test-yaml-deep",
            f"--config={_CONFIG}",
        ],
        timeout=120,
    )
    session.expect(["Choice", pexpect.EOF, pexpect.TIMEOUT])  # type: ignore[attr-defined]
    session.send("s")  # type: ignore[attr-defined]
    session.expect(pexpect.EOF)  # type: ignore[attr-defined]
    post_yaml = c.read_text(f"/workspace/{_CONFIG}")  # type: ignore[attr-defined]
    # YAML should have the preserve key path added under yaml_deep.
    # The action appends `settings.userSub` (the diverged key path) to
    # preserve_user_keys list in the YAML.
    assert "userSub" in post_yaml or "settings.userSub" in post_yaml
    assert pre_yaml != post_yaml


# --- Variant S (interactive: pty + 'm') -----------------------------------


def test_sync_interactive_merge_via_pty(
    docker_container: Callable[..., ContainerHandle],
    docker_pty_session: Callable[..., object],
) -> None:
    """S: pexpect; send 'm' (manual edit) then 'n' (decline editor) → pending state.

    Per ``my_setup/wizard.py`` _action_manual_edit (verified against
    wizard source per open question 8): ``m`` prompts ``y/n``; ``y``
    launches ``$EDITOR``, ``n`` returns MANUAL_PENDING which halts the
    wizard at this drift item. The pending state means tracked is
    unchanged for this item — perfect for asserting in an automated
    test without a real interactive editor.
    """
    import pexpect  # type: ignore[import-untyped]

    c = docker_container()
    _install(c, "test-yaml-deep")
    pre = c.read_text("/workspace/tests/fixtures/e2e/tracked/yaml/deep.yaml")  # type: ignore[attr-defined]
    c.write_text(  # type: ignore[attr-defined]
        "/home/tester/.my_setup_e2e/yaml/deep.yaml",
        textwrap.dedent(
            """\
            trackedKey: tracked-value
            settings:
              trackedSub: tracked-sub-value
              userSub: live-value-for-m
            """
        ),
    )
    session = docker_pty_session(
        c,
        [
            "uv",
            "run",
            "my-setup",
            "sync",
            "--profile=test-yaml-deep",
            f"--config={_CONFIG}",
        ],
        timeout=120,
    )
    session.expect(["Choice", pexpect.EOF, pexpect.TIMEOUT])  # type: ignore[attr-defined]
    session.send("m")  # type: ignore[attr-defined]
    # The 'm' branch prompts y/n on whether to open $EDITOR now.
    session.expect(["y/n", pexpect.EOF, pexpect.TIMEOUT])  # type: ignore[attr-defined]
    session.send("n")  # type: ignore[attr-defined]
    session.expect(pexpect.EOF)  # type: ignore[attr-defined]
    post = c.read_text("/workspace/tests/fixtures/e2e/tracked/yaml/deep.yaml")  # type: ignore[attr-defined]
    # Manual edit declined → tracked unchanged (MANUAL_PENDING halts).
    assert pre == post


# --- Variant S1 (YAML deep wizard parity) ---------------------------------


def test_sync_yaml_deep_interactive_use_via_pty(
    docker_container: Callable[..., ContainerHandle],
    docker_pty_session: Callable[..., object],
) -> None:
    """S1: same shape as Q but on a YAML deep dotfile — yaml_merge round-trip."""
    import pexpect  # type: ignore[import-untyped]

    c = docker_container()
    _install(c, "test-yaml-deep")
    c.write_text(  # type: ignore[attr-defined]
        "/home/tester/.my_setup_e2e/yaml/deep.yaml",
        textwrap.dedent(
            """\
            trackedKey: tracked-value
            settings:
              trackedSub: tracked-sub-value
              userSub: live-yaml-absorbed
            """
        ),
    )
    session = docker_pty_session(
        c,
        [
            "uv",
            "run",
            "my-setup",
            "sync",
            "--profile=test-yaml-deep",
            f"--config={_CONFIG}",
        ],
        timeout=120,
    )
    session.expect(["Choice", pexpect.EOF, pexpect.TIMEOUT])  # type: ignore[attr-defined]
    session.send("u")  # type: ignore[attr-defined]
    session.expect(pexpect.EOF)  # type: ignore[attr-defined]
    post = c.read_text(  # type: ignore[attr-defined]
        "/workspace/tests/fixtures/e2e/tracked/yaml/deep.yaml"
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
    c.write_text(  # type: ignore[attr-defined]
        "/home/tester/.my_setup_e2e/minimal/text.txt", "live-drift\n"
    )
    proc = c.exec(  # type: ignore[attr-defined]
        [
            "uv",
            "run",
            "my-setup",
            "compare",
            "--profile=test-minimal",
            f"--config={_CONFIG}",
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
        c.exec(  # type: ignore[attr-defined]
            ["test", "-f", "/home/tester/.my_setup_e2e/minimal/text.txt"], check=False
        ).returncode
        == 0
    )
    revert = c.exec(  # type: ignore[attr-defined]
        [
            "uv",
            "run",
            "my-setup",
            "revert",
            "--profile=test-minimal",
            f"--config={_CONFIG}",
        ]
    )
    assert revert.returncode == 0  # type: ignore[attr-defined]
    # File is gone after revert (it was created from absence on install).
    assert (
        c.exec(  # type: ignore[attr-defined]
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
    first = c.read_text("/home/tester/.my_setup_e2e/minimal/text.txt")  # type: ignore[attr-defined]
    second = _install(c, "test-minimal")
    assert second.returncode == 0  # type: ignore[attr-defined]
    after = c.read_text("/home/tester/.my_setup_e2e/minimal/text.txt")  # type: ignore[attr-defined]
    assert first == after


# --- Variant W ------------------------------------------------------------


def test_validate_clean_yaml_exit_zero(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """W: validate --all against the fixture config exits 0."""
    c = docker_container()
    proc = c.exec(  # type: ignore[attr-defined]
        ["uv", "run", "my-setup", "validate", "--all", f"--config={_CONFIG}"]
    )
    assert proc.returncode == 0  # type: ignore[attr-defined]
    assert "ok" in proc.stdout  # type: ignore[attr-defined]
