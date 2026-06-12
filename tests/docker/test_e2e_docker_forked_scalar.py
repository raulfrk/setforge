"""Docker e2e tests for the forked-scalar reconciliation surfaces.

A ``disposition: forked`` YAML / JSONC file routes through the structural
(comment-preserving) 3-way merge, where every SCALAR leaf is decided by the
stored-base scalar resolver: live==base with tracked advanced auto-takes the
tracked value, while base≠live AND base≠tracked at the same key is a genuine
conflict. Exercised end to end here, for BOTH structural parser seams (ruamel
YAML / json-five JSONC):

- **install auto-resolve** — an upstream scalar advance on an untouched live
  file deploys the tracked value with NO conflict prompt, even under the
  interactive ``--reconcile-user-sections`` install.
- **conflict + wizard** — a same-key scalar divergence under the interactive
  install renders the pick-one conflict prompt (``Choice (k/t/e/s)``);
  ``t`` takes the tracked value, ``k`` keeps the live value.
- **validate refusal** — a FORKED span whose dotted path resolves to a
  mapping (declared host-locally in ``local.yaml``) is refused offline:
  forked spans take a scalar path.
- **re-baseline prevents re-prompt** — resolving a conflict once advances the
  stored base, so a second interactive install neither re-prompts nor
  rewrites the live file (fractional-mtime no-write probe).
- **compare** — a forked file's live divergence reports ``disposition`` +
  ``status`` through the stable ``-o json`` payload fields.
- **conflicted drift class** — a genuine scalar conflict surfaces through
  ``compare -o json`` as ``drift_class == "conflicted"`` with the
  pre-rendered ``path: base → tracked | live`` line in
  ``forked_scalar_conflicts``, fails ``compare --check``, and clears once
  the wizard resolves it.

Wizard harness: the conflict prompt reads one raw-mode keypress and renders
through a Rich console whose styled prompt line can land split across
cursor-positioned bytes, so the interactive scenarios drive
:func:`pyte_pty_session` and anchor on the EMULATED display — never the raw
pexpect byte stream (see ``tests/docker/test_e2e_docker_conflict_wizard.py``).

Reuses the ``test-yaml-shallow`` / ``test-jsonc-shallow`` profiles
(``disposition: forked``, flat scalar bodies) already declared in
``tests/fixtures/e2e/setforge.test.yaml`` — no new fixture is added. Each
test runs on a fresh container and re-seeds its own base via a first
install, so no state crosses cases.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle
from tests.docker.pyte_session import PyteSession

pytestmark = pytest.mark.e2e_docker

# Host-local overlay config inside the container (the validate fold source).
_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"


@dataclass(frozen=True, slots=True)
class _Fmt:
    """One structural format's profile / path / body bundle.

    ``body`` is the canonical tracked fixture body (byte-exact, including the
    trailing newline); the derived bodies replace exactly one scalar value so
    the merge rows under test are isolated to a single key:

    - ``tracked_advanced`` — ``trackedKey`` moved upstream (auto-resolve row).
    - ``live_conflict`` / ``tracked_conflict`` — ``userKeyA`` diverged on
      BOTH sides (the genuine-conflict row).
    - ``nested_src`` — the body plus a ``nested`` MAPPING for the
      forked-span-on-a-non-scalar validate refusal.
    """

    profile: str
    file_id: str
    tracked: str
    live: str
    body: str
    tracked_advanced: str
    live_conflict: str
    tracked_conflict: str
    nested_src: str


_YAML_BODY = (
    "# YAML shallow-preserve fixture.\n"
    "trackedKey: tracked-value\n"
    "userKeyA: tracked-placeholder-A\n"
    "userKeyB: tracked-placeholder-B\n"
)

_JSONC_BODY = (
    "{\n"
    "  // tracked side comment for shallow-preserve JSONC fixture\n"
    '  "trackedKey": "tracked-value",\n'
    '  "userKeyA": "tracked-placeholder-A",\n'
    '  "userKeyB": "tracked-placeholder-B"\n'
    "}\n"
)

_YAML_FMT = _Fmt(
    profile="test-yaml-shallow",
    file_id="yaml_shallow",
    tracked="/workspace/tests/fixtures/e2e/tracked/yaml/shallow.yaml",
    live="/home/tester/.setforge_e2e/yaml/shallow.yaml",
    body=_YAML_BODY,
    tracked_advanced=_YAML_BODY.replace("tracked-value", "tracked-v2"),
    live_conflict=_YAML_BODY.replace("tracked-placeholder-A", "live-edit"),
    tracked_conflict=_YAML_BODY.replace("tracked-placeholder-A", "tracked-edit"),
    nested_src=_YAML_BODY + "nested:\n  inner: 1\n",
)

_JSONC_FMT = _Fmt(
    profile="test-jsonc-shallow",
    file_id="jsonc_shallow",
    tracked="/workspace/tests/fixtures/e2e/tracked/jsonc/shallow.json",
    live="/home/tester/.setforge_e2e/jsonc/shallow.json",
    body=_JSONC_BODY,
    tracked_advanced=_JSONC_BODY.replace("tracked-value", "tracked-v2"),
    live_conflict=_JSONC_BODY.replace("tracked-placeholder-A", "live-edit"),
    tracked_conflict=_JSONC_BODY.replace("tracked-placeholder-A", "tracked-edit"),
    nested_src=_JSONC_BODY.replace(
        '"userKeyB": "tracked-placeholder-B"\n',
        '"userKeyB": "tracked-placeholder-B",\n  "nested": {"inner": 1}\n',
    ),
)

_FORMATS = [
    pytest.param(_YAML_FMT, id="yaml"),
    pytest.param(_JSONC_FMT, id="jsonc"),
]


def _setforge(c: ContainerHandle, args: list[str]) -> tuple[int, str, str]:
    """Run ``uv run setforge <args>`` and return (returncode, stdout, stderr)."""
    result = c.exec(["uv", "run", "setforge", *args], check=False)
    return result.returncode, result.stdout, result.stderr


def _install_cmd(fmt: _Fmt, *, interactive: bool = False) -> list[str]:
    """Build the full ``uv run setforge install`` argv for ``fmt``'s profile."""
    cmd = [
        "uv",
        "run",
        "setforge",
        "install",
        f"--profile={fmt.profile}",
        f"--config={CONFIG_FIXTURE}",
    ]
    if interactive:
        cmd.append("--reconcile-user-sections")
    return cmd


def _seed(c: ContainerHandle, fmt: _Fmt) -> None:
    """First install: deploys tracked verbatim and seeds the stored base."""
    result = c.exec(_install_cmd(fmt), check=False)
    assert result.returncode == 0, result.stderr or result.stdout
    assert c.read_text(fmt.live) == fmt.body


def _seed_scalar_conflict(c: ContainerHandle, fmt: _Fmt) -> None:
    """Seed the base, then diverge the SAME scalar key on both sides.

    ``userKeyA`` moves to ``live-edit`` in the live file and to
    ``tracked-edit`` in the tracked source — base≠live AND base≠tracked at
    one key, a genuine scalar conflict for the next merge.
    """
    _seed(c, fmt)
    c.write_text(fmt.live, fmt.live_conflict)
    c.write_text(fmt.tracked, fmt.tracked_conflict)


def _interactive_install(
    pyte_pty_session: Callable[..., PyteSession],
    c: ContainerHandle,
    fmt: _Fmt,
) -> PyteSession:
    """Spawn the interactive (``--reconcile-user-sections``) install under pyte."""
    return pyte_pty_session(
        container=c.cid,
        cmd=_install_cmd(fmt, interactive=True),
        timeout=120.0,
    )


def _mtime(c: ContainerHandle, path: str) -> str:
    """Fractional mtime probe (``stat -c %.Y``) — sub-second write detection."""
    return c.exec(["stat", "-c", "%.Y", path], check=True).stdout.strip()


# ---------------------------------------------------------------------------
# 1: install auto-resolve — live==base, tracked advanced → take tracked,
#    NO prompt (asserted under the interactive install, where the wizard
#    gate is satisfied and a regression WOULD prompt).
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
@pytest.mark.parametrize("fmt", _FORMATS)
def test_install_auto_resolves_upstream_scalar_advance(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
    fmt: _Fmt,
) -> None:
    """An upstream scalar advance on an untouched live auto-takes tracked.

    Seed the base (install #1, live == base), then move ``trackedKey``
    upstream only. The second install — run INTERACTIVELY so a phantom
    conflict would actually prompt (and hang ``wait_for_exit``) — resolves
    without any wizard prompt and deploys the tracked value.
    """
    c = docker_container()
    _seed(c, fmt)
    c.write_text(fmt.tracked, fmt.tracked_advanced)

    session = _interactive_install(pyte_pty_session, c, fmt)
    session.wait_for_exit(timeout=60, expected_code=0)
    assert "Choice" not in "\n".join(session.display)

    live = c.read_text(fmt.live)
    assert "tracked-v2" in live, live
    assert "tracked-value" not in live, live
    # Untouched keys keep their value.
    assert "tracked-placeholder-A" in live, live


# ---------------------------------------------------------------------------
# 2: conflict + wizard — same-key scalar divergence renders the pick-one
#    prompt; 't' takes tracked (YAML seam), 'k' keeps live (JSONC seam).
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_conflict_wizard_take_upstream_yaml(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """YAML: the scalar conflict prompts; 't' deploys the tracked value."""
    c = docker_container()
    _seed_scalar_conflict(c, _YAML_FMT)

    session = _interactive_install(pyte_pty_session, c, _YAML_FMT)
    # The wizard names the conflicting dotted path, then prompts.
    session.expect_in_display("userKeyA", timeout=60.0)
    session.expect_in_display("Choice", timeout=60.0)
    session.send_keys("t")
    session.wait_for_exit(timeout=60, expected_code=0)

    live = c.read_text(_YAML_FMT.live)
    assert "tracked-edit" in live, live
    assert "live-edit" not in live, live


@pytest.mark.xdist_group("docker_daemon")
def test_conflict_wizard_keep_yours_jsonc(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """JSONC: the scalar conflict prompts; 'k' keeps the live value."""
    c = docker_container()
    _seed_scalar_conflict(c, _JSONC_FMT)

    session = _interactive_install(pyte_pty_session, c, _JSONC_FMT)
    session.expect_in_display("userKeyA", timeout=60.0)
    session.expect_in_display("Choice", timeout=60.0)
    session.send_keys("k")
    session.wait_for_exit(timeout=60, expected_code=0)

    live = c.read_text(_JSONC_FMT.live)
    assert "live-edit" in live, live
    assert "tracked-edit" not in live, live
    # The tracked source is never touched by install.
    assert "tracked-edit" in c.read_text(_JSONC_FMT.tracked)


# ---------------------------------------------------------------------------
# 3: validate refusal — a FORKED span resolving to a mapping is refused
#    offline (forked spans take a scalar path). The span is declared
#    host-locally in local.yaml; validate folds it into the checked view.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
@pytest.mark.parametrize("fmt", _FORMATS)
def test_validate_refuses_forked_span_on_mapping(
    docker_container: Callable[..., ContainerHandle],
    fmt: _Fmt,
) -> None:
    """validate exits 1 when a forked span's dotted path is a mapping.

    The tracked src gains a ``nested`` MAPPING, and ``local.yaml`` declares
    a host-local FORKED span anchored at it. The offline guard refuses —
    a forked subtree would silently degrade to whole-replace at merge time.
    """
    c = docker_container()
    c.write_text(fmt.tracked, fmt.nested_src)
    c.write_text(
        _LOCAL_YAML,
        f"tracked_files:\n"
        f"  {fmt.file_id}:\n"
        f"    spans:\n"
        f"      - anchor: nested\n"
        f"        kind: forked\n",
    )
    rc, stdout, stderr = _setforge(
        c, ["validate", f"--profile={fmt.profile}", f"--config={CONFIG_FIXTURE}"]
    )
    assert rc == 1, stdout + stderr
    assert "forked spans take a scalar path" in stdout + stderr, stdout + stderr


# ---------------------------------------------------------------------------
# 4: re-baseline prevents re-prompt — after resolving once, a second
#    interactive install neither prompts again nor rewrites the file.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
@pytest.mark.parametrize("fmt", _FORMATS)
def test_rebaseline_prevents_reprompt_and_rewrite(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
    fmt: _Fmt,
) -> None:
    """Resolving a conflict advances the base; the next install is silent.

    Resolve the scalar conflict once ('t' takes tracked). The second
    INTERACTIVE install finds base == live with tracked unchanged, so it
    must not re-prompt (a regression would hang ``wait_for_exit``) and must
    not rewrite the already-correct live file: the no-write probe is
    fractional-mtime equality (``stat -c %.Y`` — deploy's NOOP detection
    skips the write entirely, so any rewrite, even of identical bytes,
    would advance the sub-second mtime).
    """
    c = docker_container()
    _seed_scalar_conflict(c, fmt)

    session = _interactive_install(pyte_pty_session, c, fmt)
    session.expect_in_display("Choice", timeout=60.0)
    session.send_keys("t")
    session.wait_for_exit(timeout=60, expected_code=0)
    live = c.read_text(fmt.live)
    assert "tracked-edit" in live, live

    mtime_first = _mtime(c, fmt.live)
    second = _interactive_install(pyte_pty_session, c, fmt)
    second.wait_for_exit(timeout=60, expected_code=0)
    assert "Choice" not in "\n".join(second.display)
    assert _mtime(c, fmt.live) == mtime_first, (
        "second install must not rewrite the resolved file"
    )
    assert c.read_text(fmt.live) == live


# ---------------------------------------------------------------------------
# 5: compare — forked live divergence through the STABLE -o json fields
#    (disposition / status only; class + rendering are a sibling surface).
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
@pytest.mark.parametrize("fmt", _FORMATS)
def test_compare_reports_forked_disposition_and_status(
    docker_container: Callable[..., ContainerHandle],
    fmt: _Fmt,
) -> None:
    """compare -o json: a diverged forked file is disposition=forked, drifted."""
    c = docker_container()
    _seed(c, fmt)
    c.write_text(fmt.live, fmt.live_conflict)

    rc, stdout, stderr = _setforge(
        c,
        [
            "-o",
            "json",
            "compare",
            f"--profile={fmt.profile}",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, stderr
    entries = {e["name"]: e for e in json.loads(stdout)["data"]["entries"]}
    assert fmt.file_id in entries, stdout
    entry = entries[fmt.file_id]
    # Required-keys subset of the STABLE JSON surface only.
    assert entry["disposition"] == "forked", entry
    assert entry["status"] == "drifted", entry


# ---------------------------------------------------------------------------
# 6: conflicted drift class — a genuine scalar conflict surfaces through
#    compare (class + rendered conflict line + --check exit 1) and clears
#    once the wizard resolves it.
# ---------------------------------------------------------------------------


def _compare_json_entry(c: ContainerHandle, fmt: _Fmt) -> dict[str, object]:
    """Run ``compare -o json`` for ``fmt``'s profile; return its entry."""
    rc, stdout, stderr = _setforge(
        c,
        [
            "-o",
            "json",
            "compare",
            f"--profile={fmt.profile}",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, stderr
    entries = {e["name"]: e for e in json.loads(stdout)["data"]["entries"]}
    assert fmt.file_id in entries, stdout
    return entries[fmt.file_id]


@pytest.mark.xdist_group("docker_daemon")
@pytest.mark.parametrize("fmt", _FORMATS)
def test_compare_conflicted_class_until_wizard_resolves(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
    fmt: _Fmt,
) -> None:
    """A genuine scalar conflict is drift_class=="conflicted" until resolved.

    Seed base≠live AND base≠tracked at ``userKeyA``: compare reports the
    conflicted class with the exact ``path: base → tracked | live`` line,
    and ``--check`` exits 1. Resolving via the wizard ('t' takes tracked)
    re-baselines, after which compare shows the file unchanged with the
    conflict list empty.
    """
    c = docker_container()
    _seed_scalar_conflict(c, fmt)

    entry = _compare_json_entry(c, fmt)
    assert entry["drift_class"] == "conflicted", entry
    assert entry["forked_scalar_conflicts"] == [
        "userKeyA: tracked-placeholder-A → tracked-edit | live-edit"
    ], entry

    rc, stdout, stderr = _setforge(
        c,
        [
            "compare",
            f"--profile={fmt.profile}",
            f"--config={CONFIG_FIXTURE}",
            "--check",
        ],
    )
    assert rc == 1, stdout + stderr

    session = _interactive_install(pyte_pty_session, c, fmt)
    session.expect_in_display("Choice", timeout=60.0)
    session.send_keys("t")
    session.wait_for_exit(timeout=60, expected_code=0)

    entry = _compare_json_entry(c, fmt)
    assert entry["status"] == "unchanged", entry
    assert entry["drift_class"] is None, entry
    assert entry["forked_scalar_conflicts"] == [], entry
