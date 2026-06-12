"""Two-pass install: refuse-before-write under ``--strict-spans``.

Drive the real ``setforge install`` CLI against a temp config repo with a
sandboxed ``$HOME`` + ``$SETFORGE_STATE_DIR`` and pin the two-pass deploy
contract: pass 1 computes every merge with ZERO filesystem writes (including
the disposition-base auto-migration), the ``--strict-spans`` pinned-orphan
gate fires BETWEEN the passes (all-or-nothing: no live file, no state store,
no transition is touched on refusal), and the non-strict path keeps today's
warn-and-proceed behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store, spans_store, transitions
from setforge.cli import app

_PROFILE = "test-two-pass"

_DOC_A_V1 = "# A\n\nbody v1\n"
_DOC_A_V2 = "# A\n\nbody v2\n"

_DOC_B = """\
# B

## PinnedB

Pinned B body.

## SharedB

Shared B body.
"""

_DOC_C = """\
# C

## PinnedC

Pinned C body.

## SharedC

Shared C body.
"""

# Orphan variants: the pinned heading is gone upstream AND from live, so the
# span anchor cannot be relocated (a pinned orphan).
_DOC_B_GONE = _DOC_B.replace("## PinnedB\n\nPinned B body.\n\n", "")
_DOC_C_GONE = _DOC_C.replace("## PinnedC\n\nPinned C body.\n\n", "")

_LIVE_A_WITH_MARKERS = (
    "intro\n"
    "<!-- setforge:user-section start shared R -->\n"
    "body\n"
    "<!-- setforge:user-section end shared R -->\n"
    "outro\n"
)
_STRIPPED_A = "intro\nbody\noutro\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _live_dir() -> Path:
    return Path.home() / ".setforge_two_pass"


def _install(
    config: Path, *, extra: list[str] | None = None, transition: bool = False
) -> Result:
    args = [
        "install",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-secrets-scan",
        "--no-git-check",
        "--yes",
    ]
    if not transition:
        args.append("--no-transition")
    if extra:
        args.extend(extra)
    return CliRunner().invoke(app, args)


def _transition_count() -> int:
    root = transitions.transitions_root()
    if not root.exists():
        return 0
    return sum(1 for entry in root.iterdir() if entry.is_dir())


def _write_orphan_config(repo: Path) -> Path:
    """Three disposition files; pinned spans on the LATER two (b and c)."""
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  a:\n"
        "    src: a.md\n"
        "    dst: ~/.setforge_two_pass/a.md\n"
        "    disposition: shared\n"
        "  b:\n"
        "    src: b.md\n"
        "    dst: ~/.setforge_two_pass/b.md\n"
        "    disposition: shared\n"
        "    spans:\n"
        '      - anchor: "## PinnedB"\n'
        "        kind: pinned\n"
        "        semantics: shared\n"
        "  c:\n"
        "    src: c.md\n"
        "    dst: ~/.setforge_two_pass/c.md\n"
        "    disposition: shared\n"
        "    spans:\n"
        '      - anchor: "## PinnedC"\n'
        "        kind: pinned\n"
        "        semantics: shared\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - a\n"
        "      - b\n"
        "      - c\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, name: str, body: str) -> None:
    src = repo / "tracked" / f"{name}.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(body, encoding="utf-8")


def _orphan_both_later_files(repo: Path) -> Path:
    """Install once cleanly, then orphan the pinned spans on b AND c.

    Returns the config path. After this: tracked ``a`` is at v2 (so a deploy
    WOULD update its live), and both ``b`` and ``c`` carry a pinned-span
    orphan (heading gone upstream and from live).
    """
    _write_tracked(repo, "a", _DOC_A_V1)
    _write_tracked(repo, "b", _DOC_B)
    _write_tracked(repo, "c", _DOC_C)
    config = _write_orphan_config(repo)
    assert _install(config).exit_code == 0

    _write_tracked(repo, "a", _DOC_A_V2)
    _write_tracked(repo, "b", _DOC_B_GONE)
    _write_tracked(repo, "c", _DOC_C_GONE)
    (_live_dir() / "b.md").write_text(_DOC_B_GONE, encoding="utf-8")
    (_live_dir() / "c.md").write_text(_DOC_C_GONE, encoding="utf-8")
    return config


def test_strict_refusal_on_later_file_leaves_all_files_unwritten(repo: Path) -> None:
    """Pinned orphans on LATER files refuse the install BEFORE any write.

    Before the two-pass split the strict raise fired per-file inside the
    deploy loop AFTER ``copy_atomic``, so file ``a`` (clean, deployed first)
    was already written — an unrevertable partial install. The gate now sits
    between pass 1 (read-only) and pass 2 (writes): nothing is written, no
    transition lands, the span sidecars stay put, and EVERY pinned orphan is
    reported (not just the first one hit).
    """
    config = _orphan_both_later_files(repo)
    a_live = _live_dir() / "a.md"
    a_before = a_live.read_bytes()
    states_b_before = spans_store.get_states(_PROFILE, "b")
    states_c_before = spans_store.get_states(_PROFILE, "c")
    # Seeded by the first install.
    assert states_b_before
    assert states_c_before

    result = _install(config, extra=["--strict-spans"], transition=True)

    assert result.exit_code != 0
    # File a (clean, earlier in profile order) was NOT deployed.
    assert a_live.read_bytes() == a_before
    # No transition was recorded for the refused install.
    assert _transition_count() == 0
    # Span sidecars did not advance.
    assert spans_store.get_states(_PROFILE, "b") == states_b_before
    assert spans_store.get_states(_PROFILE, "c") == states_c_before
    # BOTH orphans are reported, not just the first file's.
    assert "## PinnedB" in result.output
    assert "## PinnedC" in result.output


def _write_migration_config(repo: Path) -> Path:
    """File ``a`` = disposition markdown (migration candidate); ``b`` = orphan."""
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  a:\n"
        "    src: a.md\n"
        "    dst: ~/.setforge_two_pass/a.md\n"
        "    disposition: shared\n"
        "  b:\n"
        "    src: b.md\n"
        "    dst: ~/.setforge_two_pass/b.md\n"
        "    disposition: shared\n"
        "    spans:\n"
        '      - anchor: "## Pinned"\n'
        "        kind: pinned\n"
        "        semantics: shared\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - a\n"
        "      - b\n",
        encoding="utf-8",
    )
    return config


def test_pass1_defers_base_migration_write(repo: Path) -> None:
    """The disposition-base auto-migration does NOT write during pass 1.

    File ``a`` is a first-install disposition markdown whose live carries
    legacy SHARED markers (base absent → auto-migration candidate: seed the
    base, strip live). File ``b`` carries a pinned-span orphan (its anchor is
    absent from tracked). A strict install must refuse WITHOUT seeding a's
    base or stripping its live markers — otherwise refuse-before-write is
    fake. The follow-up non-strict install performs the migration normally.
    """
    _write_tracked(repo, "a", _STRIPPED_A)
    _write_tracked(repo, "b", _DOC_B_GONE)  # "## Pinned" anchor never present
    config = _write_migration_config(repo)
    a_live = _live_dir() / "a.md"
    a_live.parent.mkdir(parents=True, exist_ok=True)
    a_live.write_text(_LIVE_A_WITH_MARKERS, encoding="utf-8")

    result = _install(config, extra=["--strict-spans"])

    assert result.exit_code != 0
    # Pass 1 wrote NOTHING: no seeded base, live still marker-bearing,
    # no auto-migration warning.
    assert base_store.read_base(_PROFILE, "a") is None
    assert a_live.read_text(encoding="utf-8") == _LIVE_A_WITH_MARKERS
    assert "first install under a stored-base disposition" not in result.output

    # Without the flag the install proceeds and the migration lands.
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert base_store.read_base(_PROFILE, "a") == _STRIPPED_A.encode("utf-8")
    assert "setforge:user-section" not in a_live.read_text(encoding="utf-8")
    assert "first install under a stored-base disposition" in result.output


def test_non_strict_orphan_still_warns_and_proceeds(repo: Path) -> None:
    """Without ``--strict-spans`` the orphan warns and the install completes."""
    config = _orphan_both_later_files(repo)

    result = _install(config)

    assert result.exit_code == 0, result.output
    assert "could not be relocated" in result.output
    # The clean file actually deployed (pass 2 ran).
    assert (_live_dir() / "a.md").read_text(encoding="utf-8") == _DOC_A_V2
