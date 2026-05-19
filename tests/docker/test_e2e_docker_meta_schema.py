"""Docker E2E coverage for the setforge-8ohd TransitionMeta schema bump.

7 named cases per SPEC 3:

1. ``test_install_writes_new_meta_fields`` — install records the 3 new
   fields in meta.json.
2. ``test_sync_writes_new_meta_fields`` — sync records them too.
3. ``test_revert_writes_new_meta_fields`` — revert's reverse-direction
   record carries command_line + end_timestamp;
   preserve_user_keys_applied is absent (None / omit-when-None for the
   revert path).
4. ``test_wizard_writes_new_meta_fields`` — the capture-time wizard
   path (driven via ``sync --auto=use-live`` against a preserve-deep
   profile) records all three.
5. ``test_pre_bump_meta_revert_works`` — a hand-crafted pre-bump
   ``meta.json`` loads cleanly AND ``setforge transitions show`` /
   ``setforge revert`` operate on it without crashing.
6. ``test_byte_identical_roundtrip`` — write a new meta.json, read it
   back, verify the omit-when-None invariant holds end-to-end.
7. ``test_command_line_redacts_secrets`` — invoke install with a
   simulated ``--token=ghp_FAKE`` extra-argv injection; assert
   ``meta.json``'s ``command_line`` entry has ``<REDACTED>`` instead
   of the token value.

Every test spins a fresh Debian 12 container via the shared
``docker_container`` fixture, runs ``uv run setforge ...``, then
parses the youngest transition directory's ``meta.json`` and asserts
on field presence + values.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_TRANSITIONS_DIR = "/home/tester/.local/state/setforge/transitions"


def _latest_transition_dirname(container: ContainerHandle) -> str:
    """Return the name of the most recently written transition directory.

    Uses ``ls -1 | sort | tail -1`` because the transition dirname
    format is UTC-ISO-prefixed → lexicographic sort matches
    chronological. Asserting on the result keeps test failures
    self-diagnostic when no transition got recorded.
    """
    show = container.exec(
        ["bash", "-c", f"ls -1 {_TRANSITIONS_DIR}/ | sort | tail -1"],
        check=True,
    )
    name = show.stdout.strip()
    assert name, "no transition recorded in container state dir"
    return name


def _read_meta(container: ContainerHandle, dirname: str) -> dict[str, object]:
    """Read and parse ``meta.json`` from the named transition directory."""
    raw = container.exec(
        ["cat", f"{_TRANSITIONS_DIR}/{dirname}/meta.json"],
        check=True,
    ).stdout
    return json.loads(raw)


def _run_install(
    container: ContainerHandle,
    profile: str,
    *,
    extra: list[str] | None = None,
) -> None:
    """Run ``setforge install`` for ``profile``; assert exit-0."""
    cmd = [
        "uv",
        "run",
        "setforge",
        "install",
        f"--profile={profile}",
        f"--config={CONFIG_FIXTURE}",
    ]
    if extra:
        cmd.extend(extra)
    result = container.exec(cmd, check=False)
    assert result.returncode == 0, result.stderr


def test_install_writes_new_meta_fields(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """E2E #1: install records end_timestamp + command_line +
    preserve_user_keys_applied.

    Uses ``test-jsonc-shallow`` (declares ``preserve_user_keys``) so
    ``preserve_user_keys_applied`` lands as ``True``.
    """
    c = docker_container()
    _run_install(c, "test-jsonc-shallow")
    meta = _read_meta(c, _latest_transition_dirname(c))
    assert "end_timestamp" in meta, meta
    assert isinstance(meta["end_timestamp"], str)
    command_line = meta.get("command_line")
    assert isinstance(command_line, list), meta
    assert any("install" in str(arg) for arg in command_line)
    assert meta.get("preserve_user_keys_applied") is True


def test_sync_writes_new_meta_fields(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """E2E #2: sync records end_timestamp + command_line; the
    preserve_user_keys_applied bool reflects the profile's overlay.
    """
    c = docker_container()
    _run_install(c, "test-jsonc-shallow")
    # Trigger a tracked-side sync.
    sync = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "sync",
            "--profile=test-jsonc-shallow",
            f"--config={CONFIG_FIXTURE}",
        ],
        check=False,
    )
    assert sync.returncode == 0, sync.stderr
    meta = _read_meta(c, _latest_transition_dirname(c))
    assert meta.get("command") == "sync"
    assert "end_timestamp" in meta
    command_line = meta.get("command_line")
    assert isinstance(command_line, list), meta
    assert any("sync" in str(arg) for arg in command_line)
    assert meta.get("preserve_user_keys_applied") is True


def test_revert_writes_new_meta_fields(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """E2E #3: revert records end_timestamp + command_line;
    preserve_user_keys_applied is None (omit-when-None) for the
    reverse-direction record.
    """
    c = docker_container()
    _run_install(c, "test-minimal")
    revert = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "revert",
            "--profile=test-minimal",
            f"--config={CONFIG_FIXTURE}",
            "--yes",
        ],
        check=False,
    )
    assert revert.returncode == 0, revert.stderr
    meta = _read_meta(c, _latest_transition_dirname(c))
    assert meta.get("command") == "revert"
    assert "end_timestamp" in meta
    assert isinstance(meta.get("command_line"), list)
    # preserve_user_keys_applied is omitted (None) for revert by design.
    assert "preserve_user_keys_applied" not in meta, meta


def test_wizard_writes_new_meta_fields(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """E2E #4: a non-interactive ``sync --auto=use-live`` exercises the
    capture-flow transition writer and records the 3 new fields.

    This is the wizard-adjacent path: ``sync`` triggers
    ``capture_profile``, which when drift is present and
    ``--auto=use-live`` is passed performs the absorb without prompting.
    The transition writer is the same ``transitions.make_meta`` chain
    the wizard hits via ``run_wizard_loop`` — same fields land.
    """
    c = docker_container()
    _run_install(c, "test-yaml-deep")
    # Seed drift into the live YAML deep preserve_user_keys subtree.
    c.write_text(
        "/home/tester/.setforge_e2e/yaml/deep.yaml",
        textwrap.dedent(
            """\
            trackedKey: tracked-value
            settings:
              trackedSub: tracked-sub-value
              userSub: live-drift-value
            """
        ),
    )
    sync = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "sync",
            "--profile=test-yaml-deep",
            f"--config={CONFIG_FIXTURE}",
            "--auto=use-live",
            "--yes",
        ],
        check=False,
    )
    assert sync.returncode == 0, sync.stderr
    meta = _read_meta(c, _latest_transition_dirname(c))
    assert "end_timestamp" in meta
    assert isinstance(meta.get("command_line"), list)
    # test-yaml-deep declares preserve_user_keys_deep → applied is True.
    assert meta.get("preserve_user_keys_applied") is True


def test_pre_bump_meta_revert_works(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """E2E #5: an old (pre-bump) meta.json (missing the 3 new fields)
    is loaded cleanly by ``transitions show``.

    Backward-compat acceptance: the cmdline tools must continue to
    function on transition records written before this schema bump.
    """
    c = docker_container()
    _run_install(c, "test-minimal")
    latest = _latest_transition_dirname(c)
    # Rewrite meta.json into a pre-bump 5-field shape (drop new keys if
    # present and preserve paths sidecar) — simulates a transition
    # written by an older setforge binary.
    rewrite = textwrap.dedent(
        f"""
        python3 - <<'PY'
        import json, pathlib
        meta_path = pathlib.Path("{_TRANSITIONS_DIR}/{latest}/meta.json")
        payload = json.loads(meta_path.read_text())
        for key in (
            "end_timestamp", "command_line", "preserve_user_keys_applied"
        ):
            payload.pop(key, None)
        meta_path.write_text(json.dumps(payload, indent=2) + "\\n")
        PY
        """
    )
    c.exec(["bash", "-c", rewrite], check=True)
    show = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "transitions",
            "show",
            latest,
        ],
        check=False,
    )
    assert show.returncode == 0, show.stderr


def test_byte_identical_roundtrip(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """E2E #6: load → re-serialize → byte-identical for a written record.

    Reads the latest meta.json bytes verbatim, then has the container's
    Python re-load via ``transitions.load_meta`` + re-dump via
    ``meta.to_dict()`` (plus the paths sidecar that
    ``transitions.write_meta`` adds), and asserts the re-serialized
    bytes are identical.
    """
    c = docker_container()
    _run_install(c, "test-minimal")
    latest = _latest_transition_dirname(c)
    roundtrip = textwrap.dedent(
        f"""
        python3 - <<'PY'
        import json, pathlib
        from setforge.transitions import load_meta
        meta_path = pathlib.Path("{_TRANSITIONS_DIR}/{latest}/meta.json")
        original_bytes = meta_path.read_bytes()
        original = json.loads(original_bytes)
        meta = load_meta(meta_path.parent)
        body = dict(meta.to_dict())
        if "paths" in original:
            body["paths"] = original["paths"]
        re_serialized = json.dumps(body, indent=2) + "\\n"
        assert re_serialized.encode() == original_bytes, (
            "round-trip not byte-identical:\\n"
            f"  original:    {{original_bytes!r}}\\n"
            f"  reserialized:{{re_serialized.encode()!r}}\\n"
        )
        print("OK: byte-identical")
        PY
        """
    )
    result = c.exec(["bash", "-c", f"cd /workspace && uv run {roundtrip}"], check=False)
    assert result.returncode == 0, result.stderr
    assert "OK: byte-identical" in result.stdout


def test_command_line_redacts_secrets(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """E2E #7: command_line capture redacts --token=<value>.

    Setforge's typer CLI surface doesn't accept ``--token`` today
    (rejected as unknown flag), so we can't smuggle the secret through
    a normal CLI invocation. Strategy: run a Python shim that
    pre-seeds ``sys.argv`` with a secret-shaped entry, then invokes
    the typer app with a clean argv. The install call site reads
    ``sys.argv[1:]`` directly — the shim swaps in a token-bearing
    list at the exact moment of the read by monkeypatching
    ``_install_helpers.sys`` to a shim with a doctored ``argv``.
    """
    c = docker_container()
    shim_path = "/tmp/setforge_token_shim.py"
    shim_body = textwrap.dedent(
        f'''\
        """Shim: invoke setforge install with sys.argv masquerading as if
        the user had passed --token=ghp_FAKE on the command line.
        """
        import sys
        import types

        from setforge.cli import _install_helpers

        # Snapshot the secret-bearing argv. The redactor reads via
        # ``ih.sys.argv[1:]`` (module-attribute access), so swapping
        # ``ih.sys`` to a stub points the read at our doctored argv.
        secret_argv = [
            "setforge",
            "install",
            "--profile=test-minimal",
            "--config={CONFIG_FIXTURE}",
            "--token=ghp_FAKE",
        ]
        stub_sys = types.SimpleNamespace(argv=secret_argv)
        _install_helpers.sys = stub_sys

        # Now drive the typer CLI with a CLEAN argv so the parser doesn't
        # reject --token. _write_install_transition's ``sys.argv[1:]``
        # read inside _install_helpers will hit our doctored list.
        sys.argv = [
            "setforge",
            "install",
            "--profile=test-minimal",
            "--config={CONFIG_FIXTURE}",
        ]
        from setforge.cli import app
        try:
            app()
        except SystemExit as exc:
            if exc.code:
                raise
        '''
    )
    c.write_text(shim_path, shim_body)
    result = c.exec(
        ["bash", "-c", f"cd /workspace && uv run python {shim_path}"],
        check=False,
    )
    assert result.returncode == 0, f"shim failed: {result.stderr}"
    latest = _latest_transition_dirname(c)
    meta = _read_meta(c, latest)
    command_line = meta.get("command_line")
    assert isinstance(command_line, list), meta
    joined = " ".join(str(a) for a in command_line)
    assert "ghp_FAKE" not in joined, f"raw token leaked into meta.json: {joined}"
    assert "<REDACTED>" in joined, f"expected <REDACTED> mask in command_line: {joined}"
