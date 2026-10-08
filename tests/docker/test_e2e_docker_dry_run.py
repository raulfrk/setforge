"""Docker E2E tests for ``setforge install --dry-run``.

Six named cases per SPEC 4. The single highest-value gate is
:func:`test_dry_run_zero_filesystem_diff` — a fresh container's defined
install mutation roots are snapshotted as typed path/mode/mtime/payload
records BEFORE the dry-run invocation and immediately AFTER, with the
assertion that the two snapshots are byte-identical. This is the load-bearing
acceptance for the spec; the remaining five cases anchor individual contract
points (output shape, plugin/extension reconcile coverage against the real
``claude`` and ``code`` binaries, cross-check against the real pipeline).
The confirm-wizard, state-directory, drift-preview, final-line and profile-flag
contracts run in-process in ``tests/test_install_dry_run.py`` and
``tests/test_install_reconcile.py``.

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
# - ``test-comprehensive`` — extensions + plugins + multi-file + bootstrap.
# - ``test-text-sections`` — markerless markdown tracked_file (byte-copy).
_PROFILE_COMPREHENSIVE: str = "test-comprehensive"
_PROFILE_TEXT_SECTIONS: str = "test-text-sections"

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
    "=== would-be secrets gate ===",
    "=== would-be deploy ===",
    "=== would-be plugin reconcile ===",
    "=== would-be extension reconcile ===",
    "=== would-be MCP server reconcile ===",
    "=== would-be package provision ===",
    "=== would-be transition record ===",
    _FINAL_LINE,
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
    """Snapshot the setforge-install-mutation-surface, sorted by path.

    Scopes the snapshot to the directories ``setforge install`` writes to:
    ``$HOME/.setforge_e2e/`` (tracked-file destinations),
    ``$HOME/.local/state/setforge/`` (transition state),
    ``$HOME/.config/setforge/`` (host-local configuration and allowlist), and
    ``/workspace/tracked/`` (tracked-side baseline).

    Tooling-owned ``~/.cache/``, ``~/.local/share/uv/``, and Microsoft
    DeveloperTools state remain excluded because every ``uv run`` invocation
    may update them.

    Missing roots receive an explicit marker. Existing roots include every
    directory, regular file, and symlink, so empty-directory and symlink-only
    mutations are visible. Records include the entry type, mode, nanosecond-
    resolution textual mtime (``%y``), and either file content hash or symlink
    target. Sorted by path so iteration order matches across hosts.
    """
    script = (
        "set -eu; "
        'roots=("$HOME/.setforge_e2e" "$HOME/.local/state/setforge" '
        '"$HOME/.config/setforge" "/workspace/tracked"); '
        '{ for r in "${roots[@]}"; do '
        '  if [ -e "$r" ] || [ -L "$r" ]; then find "$r" -print0; '
        "  else printf '%s\\0' \"!missing:$r\"; fi; "
        "done; } | sort -z | "
        "while IFS= read -r -d '' p; do "
        '  case "$p" in !missing:*) printf \'%s\\n\' "$p"; continue;; esac; '
        "  t=$(stat -c '%F' \"$p\"); mode=$(stat -c '%a' \"$p\"); "
        "  m=$(stat -c '%y' \"$p\"); payload=-; "
        '  if [ -f "$p" ] && [ ! -L "$p" ]; then '
        "    payload=$(sha256sum \"$p\" | awk '{print $1}'); "
        '  elif [ -L "$p" ]; then payload=$(readlink "$p"); fi; '
        '  printf \'%s|%s|%s|%s|%s\\n\' "$p" "$t" "$mode" "$m" "$payload"; '
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
    """Fresh container; snapshot install mutation roots before/after; ZERO diff.

    The single highest-value gate per spec SPEC 4. Captures the full
    install roots' typed path/mode/mtime/payload records before the dry-run
    invocation, runs ``setforge install --profile=test-comprehensive
    --dry-run``, then captures the same records after. The two
    snapshots must match byte-for-byte; any directory, file, or symlink drift
    fails the test loudly.
    """
    c = docker_container()
    pre = _snapshot_home(c)
    # Prove the snapshot itself notices the directory-only mutation that this
    # gate exists to catch, without adding another container or E2E test.
    c.exec(["bash", "-c", 'mkdir -p "$HOME/.config/setforge"'])
    assert _snapshot_home(c) != pre
    c.exec(
        [
            "bash",
            "-c",
            'touch -d @1700000000.100000000 "$HOME/.config/setforge"',
        ]
    )
    first_mtime = _snapshot_home(c)
    c.exec(
        [
            "bash",
            "-c",
            'touch -d @1700000000.200000000 "$HOME/.config/setforge"',
        ]
    )
    assert _snapshot_home(c) != first_mtime
    c.exec(
        [
            "bash",
            "-c",
            'rmdir "$HOME/.config/setforge"; rmdir "$HOME/.config" 2>/dev/null || true',
        ]
    )
    assert _snapshot_home(c) == pre
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
# E2E #3 — dry-run covers every planned phase.
# ---------------------------------------------------------------------------


def test_dry_run_covers_all_phases(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Every install-plan phase appears in dry-run stdout.

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
# E2E #4 — dry-run reports plugin reconcile.
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
    # ``superpowers`` IS declared and the container is empty, so the phase
    # always has work: it emits WOULD-lines, or skips when the ``claude`` plugin
    # tool surfaces ``PluginToolMissing`` (e.g. no marketplaces installed yet —
    # ``claude`` itself is installed in the image). "nothing to reconcile" is
    # impossible here — pinning its ABSENCE is the tightening (the old 3-way
    # any-of accepted that impossible state). The action/skip split stays
    # environmental.
    assert has_action or has_skip, (
        f"plugin reconcile block has no actionable outcome:\n{block!r}"
    )
    assert not has_nothing, (
        f"declared plugin reported 'nothing to reconcile':\n{block!r}"
    )


# ---------------------------------------------------------------------------
# E2E #5 — dry-run reports extension reconcile.
# ---------------------------------------------------------------------------


def test_dry_run_reports_ext_reconcile(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Extension reconcile phase appears in dry-run output (parallel to plugins).

    The comprehensive profile declares one ``editorconfig.editorconfig``
    extension; the e2e image ships ``code`` and the container starts empty,
    so the extension is enumerated as uninstalled and the phase has work.
    When the ``code`` binary is absent the phase emits a ``skipped
    (extension tool unavailable: ...)`` line; otherwise it emits a ``WOULD
    install`` line. "nothing to reconcile" cannot occur in this environment
    (a declared extension, uninstalled on the empty container, always
    leaves either a WOULD-line or a skip).
    """
    c = docker_container()
    result = _dry_run_install(c, _PROFILE_COMPREHENSIVE)
    assert result.returncode == 0, result.stderr or result.stdout
    block = _extract_phase_block(result.stdout, "=== would-be extension reconcile ===")
    assert block, "extension reconcile block missing from dry-run output"
    has_action = any(line.lstrip().startswith("WOULD ") for line in block)
    has_skip = any("skipped (" in line for line in block)
    has_nothing = any("nothing to reconcile" in line for line in block)
    # editorconfig IS declared and not installed, so the phase always has
    # work: a WOULD-line, or a skip when ``code`` is unavailable. Pin the
    # ABSENCE of "nothing to reconcile" (the impossible state the old any-of
    # wrongly accepted); the action/skip split stays environmental (binary).
    assert has_action or has_skip, (
        f"extension reconcile block has no actionable outcome:\n{block!r}"
    )
    assert not has_nothing, (
        f"declared extension reported 'nothing to reconcile':\n{block!r}"
    )


# ---------------------------------------------------------------------------
# E2E #6 — cross-check: dry-run output predicts real install state.
# ---------------------------------------------------------------------------


def test_dry_run_predicts_real_install(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Dry-run on fresh host predicts which files the real install will create.

    Captures the dry-run output, extracts every ``WOULD install
    <path>`` line for filesystem paths (the regex restricts to
    absolute paths so plugin/extension reconcile lines are excluded —
    those use ``WOULD install <id>`` with an id like
    ``superpowers@marketplace``). Runs the real install, then
    asserts each predicted path now exists on disk.

    Uses ``test-text-sections`` (a no-plugin no-extension profile)
    so the real install completes well under the 60s container exec
    timeout; the cross-check is still meaningful — the dry-run
    predicts the file deploys, the real install produces them.
    """
    c = docker_container()
    dry = _dry_run_install(c, _PROFILE_TEXT_SECTIONS)
    assert dry.returncode == 0, dry.stderr or dry.stdout
    predicted_install: list[str] = []
    for line in dry.stdout.splitlines():
        m = re.match(r"^\s*WOULD install\s+(/\S+)\s*$", line)
        if m:
            predicted_install.append(m.group(1))
    assert predicted_install, f"dry-run produced no WOULD install lines:\n{dry.stdout}"
    real = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            f"--profile={_PROFILE_TEXT_SECTIONS}",
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
