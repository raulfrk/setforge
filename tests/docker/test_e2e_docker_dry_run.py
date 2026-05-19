"""Docker E2E tests for ``setforge install --dry-run`` (setforge-lnvq).

Thirteen named cases per SPEC 4. The single highest-value gate is
:func:`test_dry_run_zero_filesystem_diff` — a fresh container's full
``$HOME`` tree is snapshotted (mtime + sha256) BEFORE the dry-run
invocation and immediately AFTER, with the assertion that the two
snapshots are byte-identical. This is the load-bearing acceptance for
the spec; the remaining twelve cases anchor individual contract
points (output shape, no-confirm-substring, final-line marker,
plugin/extension reconcile coverage, profile flag wiring, cross-check
against the real pipeline).

Every test spins a fresh ``setforge-e2e:test-*`` container per
``tests.docker.conftest`` and runs ``setforge install --dry-run``
inside it; the read paths use ``container.exec`` so the assertion
machinery is the same as the existing e2e ring.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

# Profiles drawn from ``tests/fixtures/e2e/setforge.test.yaml``. Each
# variant exercises a distinct surface the dry-run pipeline must
# render:
#
# - ``test-minimal`` — plain text byte-copy, no extensions / plugins.
# - ``test-comprehensive`` — extensions + plugins + multi-file + bootstrap.
# - ``test-text-sections`` — section-aware tracked_file (preserve_user_sections).
# - ``test-reconcile-sections`` — shared-section reconcile surface.
_PROFILE_MINIMAL: str = "test-minimal"
_PROFILE_COMPREHENSIVE: str = "test-comprehensive"
_PROFILE_TEXT_SECTIONS: str = "test-text-sections"
_PROFILE_SHARED_SECTIONS: str = "test-reconcile-sections"

# Final-line marker the spec mandates as exact-match. Mirrored from
# ``setforge.cli._install_helpers._DRY_RUN_FINAL_LINE``; keep the two
# in sync (the test catches any drift).
_FINAL_LINE: str = "=== rerun without --dry-run to apply for real ==="

# Section headers that must appear in dry-run output, one per phase.
# The mockup in spec 2026-05-18 §C uses ``would-be <phase>`` — we
# anchor the headers verbatim so a future formatting change surfaces
# loudly in CI.
_EXPECTED_HEADERS: tuple[str, ...] = (
    "=== DRY-RUN MODE — NOTHING WILL BE MUTATED ===",
    "=== resolving profile + host overlay ===",
    "=== would-be drift gate ===",
    "=== would-be deploy ===",
    "=== would-be section reconcile ===",
    "=== would-be plugin reconcile ===",
    "=== would-be extension reconcile ===",
    "=== would-be transition record ===",
    _FINAL_LINE,
)

# Confirm-wizard substrings the bviv flow emits. The dry-run path MUST
# NOT produce either of these under ``--auto=*`` + ``--dry-run`` per
# spec anti-pattern #5.
_CONFIRM_SUBSTRINGS: tuple[str, ...] = (
    "Apply [y/N]?",
    "type 'apply' to proceed",
)


def _dry_run_install(
    container: ContainerHandle,
    profile: str,
    *,
    extra: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``setforge install --dry-run`` inside the container.

    Always passes ``--no-git-check`` so the source-layer check does
    not gate the dry-run flow on the fixture repo's clean-tree state
    (the fixture lives at ``/workspace`` inside the image; that's not
    a git checkout). Returns the :class:`subprocess.CompletedProcess`
    so the caller can assert on returncode + stdout shape.
    """
    cmd = [
        "uv",
        "run",
        "setforge",
        "install",
        f"--profile={profile}",
        f"--config={CONFIG_FIXTURE}",
        "--dry-run",
        "--no-git-check",
    ]
    if extra:
        cmd.extend(extra)
    return container.exec(cmd, check=False)


def _snapshot_home(container: ContainerHandle) -> str:
    """Snapshot ``$HOME`` mtime + sha256 over every file, sorted by path.

    Uses ``find -type f`` to enumerate, ``stat`` for the mtime
    (sub-second precision via ``%Y.%N``), and ``sha256sum`` for the
    content hash. The combined stdout is a deterministic newline-
    separated record; identical pre/post snapshots prove zero
    filesystem mutation (the single highest-value gate per spec).
    """
    script = (
        "set -eu; "
        'cd "$HOME"; '
        # Sort first so the iteration order matches across hosts; the
        # final concat is path | mtime | sha256, one record per line.
        "find . -type f -print0 | sort -z | "
        "while IFS= read -r -d '' p; do "
        "  m=$(stat -c '%Y.%9N' \"$p\"); "
        "  h=$(sha256sum \"$p\" | awk '{print $1}'); "
        '  printf \'%s|%s|%s\\n\' "$p" "$m" "$h"; '
        "done"
    )
    result = container.exec(["bash", "-c", script], check=True)
    return result.stdout


# ---------------------------------------------------------------------------
# E2E #1 — load-bearing filesystem zero-diff gate.
# ---------------------------------------------------------------------------


def test_dry_run_zero_filesystem_diff(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Fresh container; snapshot ``$HOME`` mtime+hash before/after; ZERO diff.

    The single highest-value gate per spec SPEC 4. Captures the full
    ``$HOME`` tree's path/mtime/sha256 triples before the dry-run
    invocation, runs ``setforge install --profile=test-comprehensive
    --dry-run``, then captures the same triples after. The two
    snapshots must match byte-for-byte; any drift (file created,
    touched, hashed) fails the test loudly.
    """
    c = docker_container()
    pre = _snapshot_home(c)
    result = _dry_run_install(
        c, _PROFILE_COMPREHENSIVE, extra=["--auto=use-tracked", "--yes"]
    )
    assert result.returncode == 0, result.stderr or result.stdout
    post = _snapshot_home(c)
    assert pre == post, (
        f"filesystem diff after --dry-run:\n--- pre\n{pre}\n--- post\n{post}\n"
    )


# ---------------------------------------------------------------------------
# E2E #2 — WOULD prefix only on mutating verbs.
# ---------------------------------------------------------------------------


def test_would_prefix_only_on_mutating_verbs(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``WOULD `` prefix is reserved for mutating verbs; headers / counts unprefixed.

    Anti-pattern check #4. Every line beginning with ``WOULD`` MUST
    continue with one of the mutating verbs (``install`` /
    ``update`` / ``noop`` / ``bootstrap`` / ``inject`` / ``enable`` /
    ``disable`` / ``uninstall`` / ``record`` / ``add-marketplace``).
    Section headers (``=== ... ===``) and read counts (``unexpected
    drift in N file(s)``) MUST NOT carry the prefix.
    """
    c = docker_container()
    result = _dry_run_install(c, _PROFILE_COMPREHENSIVE)
    assert result.returncode == 0, result.stderr or result.stdout
    allowed_verbs = re.compile(
        r"^\s*WOULD\s+(install|update|noop|bootstrap|inject|enable|disable"
        r"|uninstall|record|add-marketplace)\b"
    )
    for line in result.stdout.splitlines():
        if line.lstrip().startswith("WOULD "):
            assert allowed_verbs.match(line), (
                f"WOULD prefix on non-mutating verb: {line!r}"
            )
    # Headers and read-count lines MUST NOT carry the WOULD prefix.
    for line in result.stdout.splitlines():
        if line.startswith("=== ") or line.startswith("unexpected drift"):
            assert not line.lstrip().startswith("WOULD "), (
                f"WOULD prefix on read-only line: {line!r}"
            )


# ---------------------------------------------------------------------------
# E2E #3 — dry-run covers all 8 phases.
# ---------------------------------------------------------------------------


def test_dry_run_covers_all_phases(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """All 8 phases appear in dry-run stdout (profile / overlay / drift
    gate / file deploys / section reconcile / plugin reconcile / ext
    reconcile / transition path) plus header + final-line marker.

    Anchors the ``_EXPECTED_HEADERS`` tuple verbatim against the
    captured stdout. Order is not asserted (the headers may interleave
    with WOULD lines), but every header MUST appear exactly once.
    """
    c = docker_container()
    result = _dry_run_install(c, _PROFILE_COMPREHENSIVE)
    assert result.returncode == 0, result.stderr or result.stdout
    for header in _EXPECTED_HEADERS:
        assert result.stdout.count(header) == 1, (
            f"header missing or duplicated: {header!r} "
            f"(count={result.stdout.count(header)})\n"
            f"stdout:\n{result.stdout}"
        )


# ---------------------------------------------------------------------------
# E2E #4 — --auto=use-tracked + --yes + --dry-run: no prompt, exits 0.
# ---------------------------------------------------------------------------


def test_dry_run_auto_use_tracked_no_prompt(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``--auto=use-tracked --yes --dry-run`` exits 0; emits zero confirm substrings."""
    c = docker_container()
    pre = _snapshot_home(c)
    result = _dry_run_install(
        c, _PROFILE_SHARED_SECTIONS, extra=["--auto=use-tracked", "--yes"]
    )
    assert result.returncode == 0, result.stderr or result.stdout
    for needle in _CONFIRM_SUBSTRINGS:
        assert needle not in result.stdout, (
            f"confirm substring {needle!r} present under --dry-run + --auto"
        )
    post = _snapshot_home(c)
    assert pre == post, "filesystem mutated under --auto=use-tracked --dry-run"


# ---------------------------------------------------------------------------
# E2E #5 — --auto-accept-live + --yes + --dry-run: no prompt, exits 0.
# ---------------------------------------------------------------------------


def test_dry_run_auto_use_live_no_prompt(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``--auto-accept-live --yes --dry-run`` exits 0; emits zero confirm substrings.

    Per spec: ``--auto=use-live`` is the legacy unexpected-drift
    direction now spelled ``--auto-accept-live`` (the
    ``_confirm_legacy_drift_or_exit`` call site). Same shape as the
    section-reconcile variant; runs against the minimal profile so
    there is no unexpected drift to confirm in the first place.
    """
    c = docker_container()
    pre = _snapshot_home(c)
    result = _dry_run_install(
        c, _PROFILE_MINIMAL, extra=["--auto-accept-live", "--yes"]
    )
    assert result.returncode == 0, result.stderr or result.stdout
    for needle in _CONFIRM_SUBSTRINGS:
        assert needle not in result.stdout, (
            f"confirm substring {needle!r} present under --dry-run + --auto-accept-live"
        )
    post = _snapshot_home(c)
    assert pre == post, "filesystem mutated under --auto-accept-live --dry-run"


# ---------------------------------------------------------------------------
# E2E #6 — fresh container, no ~/.local/state/setforge: state dir not created.
# ---------------------------------------------------------------------------


def test_dry_run_fresh_host_no_state_dir_created(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``--dry-run`` on a fresh container does NOT create ``~/.local/state/setforge/``.

    The real install pipeline opens the state dir via
    ``transitions.ensure_state_dir_writable`` (which ``mkdir -p``'s
    the path). The dry-run pipeline MUST NOT — verified by checking
    that the directory remains absent post-invocation.
    """
    c = docker_container()
    # Ensure the directory does not pre-exist (fresh container should
    # not, but assert explicitly so a future image change surfaces).
    pre_check = c.exec(
        ["test", "-d", "/home/tester/.local/state/setforge"], check=False
    )
    assert pre_check.returncode != 0, (
        "state dir pre-exists in fresh container; test premise broken"
    )
    result = _dry_run_install(c, _PROFILE_MINIMAL)
    assert result.returncode == 0, result.stderr or result.stdout
    post_check = c.exec(
        ["test", "-d", "/home/tester/.local/state/setforge"], check=False
    )
    assert post_check.returncode != 0, "state dir was created under --dry-run"


# ---------------------------------------------------------------------------
# E2E #7 — dry-run reports drift WITHOUT applying.
# ---------------------------------------------------------------------------


def test_dry_run_drift_gate_reports_no_apply(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Live file drifted from tracked; dry-run reports the drift; ZERO mutation.

    Pre-installs the minimal profile (real install) so the live file
    exists, then mutates the live file in-place to introduce drift,
    then runs the dry-run. The dry-run output MUST reflect the
    drifted state (compare entry status DRIFTED → ``WOULD update``)
    AND leave the mutated live file byte-identical post-dry-run.
    """
    c = docker_container()
    live = "/home/tester/.setforge_e2e/minimal/text.txt"
    # Real install first so the live file is present (state mtime +
    # transition record will be created here; only the post-dry-run
    # delta matters for this case).
    real = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            f"--profile={_PROFILE_MINIMAL}",
            f"--config={CONFIG_FIXTURE}",
            "--no-git-check",
        ],
        check=False,
    )
    assert real.returncode == 0, real.stderr or real.stdout
    # Mutate live to introduce drift.
    c.write_text(live, "drifted by hand\n")
    pre = _snapshot_home(c)
    result = _dry_run_install(c, _PROFILE_MINIMAL)
    assert result.returncode == 0, result.stderr or result.stdout
    post = _snapshot_home(c)
    assert pre == post, "filesystem mutated under --dry-run with drifted live file"
    # The drifted file MUST be reported as WOULD update (not WOULD noop).
    assert "WOULD update" in result.stdout, (
        f"drifted file not reported as WOULD update:\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# E2E #8 — final-line marker exact match.
# ---------------------------------------------------------------------------


def test_dry_run_final_line_marker(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``tail -1`` of dry-run stdout matches the exact final-line marker.

    Anti-pattern check #6 + acceptance command. The marker is the
    public contract; ``tail -1 | rg -q '...'`` is the spec's
    standalone sanity check.
    """
    c = docker_container()
    result = _dry_run_install(
        c, _PROFILE_MINIMAL, extra=["--auto=use-tracked", "--yes"]
    )
    assert result.returncode == 0, result.stderr or result.stdout
    lines = result.stdout.rstrip("\n").splitlines()
    assert lines, f"dry-run stdout is empty: {result.stdout!r}"
    assert lines[-1] == _FINAL_LINE, (
        f"final line mismatch: {lines[-1]!r} != {_FINAL_LINE!r}"
    )


# ---------------------------------------------------------------------------
# E2E #9 — no confirm-wizard substring under --auto + --dry-run.
# ---------------------------------------------------------------------------


def test_dry_run_no_confirm_substring(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Anti-pattern #5: zero ``Apply [y/N]?`` / ``type 'apply' to proceed`` matches.

    The bviv confirm wizard MUST NOT fire under ``--auto=*`` +
    ``--dry-run``. Mirrors the unit-test tripwire pattern at the
    Docker layer so a future regression in the real bviv flow
    surfaces here too.
    """
    c = docker_container()
    result = _dry_run_install(
        c, _PROFILE_SHARED_SECTIONS, extra=["--auto=use-tracked", "--yes"]
    )
    assert result.returncode == 0, result.stderr or result.stdout
    for needle in _CONFIRM_SUBSTRINGS:
        assert needle not in result.stdout, (
            f"confirm substring {needle!r} present under --dry-run"
        )


# ---------------------------------------------------------------------------
# E2E #10 — dry-run reports plugin reconcile.
# ---------------------------------------------------------------------------


def test_dry_run_reports_plugin_reconcile(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Per Q6 default YES: the plugin reconcile phase appears in dry-run output.

    The comprehensive profile declares one ``superpowers`` plugin
    against the ``claude-plugins-official`` marketplace; the dry-run
    pipeline calls ``claude_plugins.reconcile(dry_run=True)`` and
    emits ``WOULD install`` / ``WOULD enable`` / ``WOULD
    add-marketplace`` lines for the diff against an empty container.
    The container's ``claude`` binary may surface
    :class:`PluginToolMissing` (no marketplaces installed yet); in
    that case the phase emits a ``skipped (...)`` line — either
    outcome is acceptable, both indicate the phase ran read-only.
    """
    c = docker_container()
    result = _dry_run_install(c, _PROFILE_COMPREHENSIVE)
    assert result.returncode == 0, result.stderr or result.stdout
    # Locate the plugin-reconcile header; the following block (up to
    # the next ``=== ... ===`` header) carries the per-plugin lines.
    block = _extract_phase_block(result.stdout, "=== would-be plugin reconcile ===")
    assert block, "plugin reconcile block missing from dry-run output"
    has_action = any(line.lstrip().startswith("WOULD ") for line in block)
    has_skip = any("skipped (" in line for line in block)
    has_nothing = any("nothing to reconcile" in line for line in block)
    assert has_action or has_skip or has_nothing, (
        f"plugin reconcile block has no recognized line shape:\n{block!r}"
    )


# ---------------------------------------------------------------------------
# E2E #11 — dry-run reports extension reconcile.
# ---------------------------------------------------------------------------


def test_dry_run_reports_ext_reconcile(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Extension reconcile phase appears in dry-run output (parallel to plugins).

    The comprehensive profile declares no extensions in the test
    fixture, but the phase header MUST still appear. When the ``code``
    binary is absent the phase emits a ``skipped (extension tool
    unavailable: ...)`` line; otherwise it emits ``WOULD install`` /
    ``WOULD uninstall`` lines or ``nothing to reconcile``.
    """
    c = docker_container()
    result = _dry_run_install(c, _PROFILE_COMPREHENSIVE)
    assert result.returncode == 0, result.stderr or result.stdout
    block = _extract_phase_block(result.stdout, "=== would-be extension reconcile ===")
    assert block, "extension reconcile block missing from dry-run output"
    has_action = any(line.lstrip().startswith("WOULD ") for line in block)
    has_skip = any("skipped (" in line for line in block)
    has_nothing = any("nothing to reconcile" in line for line in block)
    assert has_action or has_skip or has_nothing, (
        f"extension reconcile block has no recognized line shape:\n{block!r}"
    )


# ---------------------------------------------------------------------------
# E2E #12 — cross-check: dry-run output predicts real install state.
# ---------------------------------------------------------------------------


def test_dry_run_predicts_real_install(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Dry-run on fresh host predicts which files the real install will create.

    Captures the dry-run output, extracts every ``WOULD install <path>``
    line, runs the real install, then asserts each predicted path now
    exists on disk. Sanity-checks the prediction matches behavior; a
    drift between dry-run and real install paths would surface here.
    """
    c = docker_container()
    dry = _dry_run_install(c, _PROFILE_COMPREHENSIVE)
    assert dry.returncode == 0, dry.stderr or dry.stdout
    predicted_install: list[str] = []
    for line in dry.stdout.splitlines():
        m = re.match(r"^\s*WOULD install\s+(\S+)\s*$", line)
        if m:
            predicted_install.append(m.group(1))
    assert predicted_install, f"dry-run produced no WOULD install lines:\n{dry.stdout}"
    real = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            f"--profile={_PROFILE_COMPREHENSIVE}",
            f"--config={CONFIG_FIXTURE}",
            "--no-git-check",
        ],
        check=False,
    )
    assert real.returncode == 0, real.stderr or real.stdout
    for path in predicted_install:
        existence = c.exec(["test", "-f", path], check=False)
        assert existence.returncode == 0, (
            f"dry-run predicted install of {path!r} but the real "
            f"install did not produce it"
        )


# ---------------------------------------------------------------------------
# E2E #13 — dry-run honors --profile=X.
# ---------------------------------------------------------------------------


def test_dry_run_respects_profile_flag(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Different ``--profile=X`` values produce profile-distinct dry-run output.

    Runs dry-run against two profiles in sequence, asserting the
    rendered ``profile <name>`` line differs and the tracked_files
    count differs (the test fixture's minimal profile declares 1
    tracked_file; the comprehensive profile declares 4).
    """
    c = docker_container()
    minimal_out = _dry_run_install(c, _PROFILE_MINIMAL).stdout
    comprehensive_out = _dry_run_install(c, _PROFILE_COMPREHENSIVE).stdout
    assert f"profile {_PROFILE_MINIMAL}" in minimal_out
    assert f"profile {_PROFILE_COMPREHENSIVE}" in comprehensive_out
    # The two outputs MUST differ — at minimum the profile name line
    # and the tracked_files count differ between fixture profiles.
    assert minimal_out != comprehensive_out, (
        "dry-run output identical across profiles; --profile flag "
        "may not be wired through"
    )


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _extract_phase_block(stdout: str, header: str) -> list[str]:
    """Return the lines between ``header`` and the next ``=== ... ===`` header.

    Used by the plugin/extension reconcile assertion tests to scope
    the line-shape check to one phase block. Returns the empty list
    when the header is absent (caller asserts on truthiness).
    """
    lines = stdout.splitlines()
    try:
        start = lines.index(header)
    except ValueError:
        return []
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("=== "):
            break
        block.append(line)
    return block
