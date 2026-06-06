"""Integration tests for base-seeding of non-markdown disposition files.

A structured (JSON / JSONC / YAML) tracked file entering the disposition
(stored-base) world for the first time has NO inline markers to strip — so
unlike the markdown migration, the seed is simply the current LIVE bytes. The
first install seeds ``base == live`` and then routes the structural three-way
merge, so a pre-existing live edit is preserved instead of clobbered by a
verbatim tracked deploy.

These cases assert, against the real ``setforge install`` CLI:

1. First install of a JSON (and a YAML) disposition file with a pre-existing
   live file seeds ``base == live`` bytes; the structural merge runs clean
   (no spurious conflict, live keys preserved).
2. A subsequent structural merge with a non-overlapping live edit + tracked
   edit merges cleanly (no data loss).
3. The seeded base + any rewrite preserve the live file mode (0600 stays 0600).
4. ``base == live`` holds at the level the merge actually reads ``ours``
   (``read_text``), not merely ``read_bytes``.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store
from setforge.cli import app
from setforge.cli._install_helpers import _read_or_migrate_disposition_base

_PROFILE = "test-disposition-nonmd"
_FILE_ID = "structured"


def _write_config(repo: Path, *, suffix: str) -> Path:
    """Write a setforge.yaml whose disposition file's dst has ``suffix``.

    ``suffix`` (``.json`` / ``.jsonc`` / ``.yaml``) drives the install's
    structural-vs-line format detection (by dst suffix). The profile carries
    an inert ``anchor`` tracked file so it stays a valid non-empty list.
    """
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  structured:\n"
        f"    src: data/source{suffix}\n"
        f"    dst: ~/.setforge_disp/config{suffix}\n"
        "    disposition: shared\n"
        "  anchor:\n"
        "    src: data/anchor.txt\n"
        "    dst: ~/.setforge_disp/anchor.txt\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - structured\n"
        "      - anchor\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, body: str, *, suffix: str) -> None:
    """Write the tracked source body for the structured + anchor files."""
    src = repo / "tracked" / "data" / f"source{suffix}"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(body, encoding="utf-8")
    (src.parent / "anchor.txt").write_text("anchor\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp config repo with sandboxed ``$HOME`` + ``$SETFORGE_STATE_DIR``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _live_path(suffix: str) -> Path:
    """Resolve the sandboxed live destination path for ``suffix``."""
    return Path.home() / ".setforge_disp" / f"config{suffix}"


def _seed_live(content: str, suffix: str, *, mode: int = 0o644) -> Path:
    """Pre-create the live dst with ``content`` at ``mode``."""
    live = _live_path(suffix)
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(content, encoding="utf-8")
    live.chmod(mode)
    return live


def _install(config: Path, *, extra: list[str] | None = None) -> Result:
    """Run ``setforge install`` against ``config``; return the CliRunner result."""
    args = [
        "install",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-transition",
        "--no-secrets-scan",
        "--no-git-check",
        "--yes",
    ]
    if extra:
        args.extend(extra)
    return CliRunner().invoke(app, args)


# Live carries the user's customized value of a shared key (``shared``). Tracked
# shares that key at its template value and ADDS a key the live file lacks
# (``added``). Seeding base == live means: for the shared key ours == base so the
# merge takes tracked's template value (3-way semantics now own that key — the
# legacy preserve-key live-wins overlay is retired); the tracked-added key is an
# upstream addition folded in. Nothing the user holds is silently dropped.
_JSON_LIVE = '{\n  "a": 1,\n  "shared": "user-value"\n}\n'
_JSON_TRACKED = (
    '{\n  "a": 1,\n  "shared": "user-value",\n  "added": "from-tracked"\n}\n'
)

_YAML_LIVE = "a: 1\nshared: user-value\n"
_YAML_TRACKED = "a: 1\nshared: user-value\nadded: from-tracked\n"


@pytest.mark.parametrize(
    ("suffix", "live", "tracked"),
    [
        (".json", _JSON_LIVE, _JSON_TRACKED),
        (".yaml", _YAML_LIVE, _YAML_TRACKED),
    ],
)
def test_first_install_seeds_base_equals_live(
    repo: Path, suffix: str, live: str, tracked: str
) -> None:
    """First install of a non-md disposition file seeds base == live, merges clean.

    A pre-existing live file is present and NO stored base exists. The merge
    ancestor is seeded from the LIVE bytes (not absent), so the structural
    three-way merge runs with base == live: an upstream-added key is folded in
    and the shared key the user already holds is NOT manufactured into a
    spurious both-add conflict — the clean no-delta path a ``None`` base (which
    would treat the whole file as a both-add against tracked) could not give.
    After the clean merge the durable base re-baselines to the merged result
    (== final live), the steady-state contract.
    """
    _write_tracked(repo, tracked, suffix=suffix)
    config = _write_config(repo, suffix=suffix)
    _seed_live(live, suffix)

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "conflict" not in result.output.lower()

    merged = _live_path(suffix).read_text(encoding="utf-8")
    # The user's existing shared key is intact (no spurious conflict / loss).
    assert "user-value" in merged
    # The tracked-added key is folded in by the three-way merge.
    assert "from-tracked" in merged

    # Post-merge the durable base advances to the merged result (== final live).
    base = base_store.read_base(_PROFILE, _FILE_ID)
    assert base is not None
    assert base == merged.encode("utf-8")


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
def test_seed_base_equals_live_at_merge_read_level(repo: Path, suffix: str) -> None:
    """Seed invariant: the seeded base == what copy_atomic re-reads as ``ours``.

    Probes the seed helper directly (before the post-deploy advance overwrites
    the base with the merge result): the seeded merge-ancestor must equal the
    live file as read via ``read_text`` (universal-newline) — the exact view
    the structural merge parses as ``ours`` — not merely ``read_bytes``. A CRLF
    live file is collapsed to LF on both the seed side and the merge-read side,
    so the two stay byte-equal.

    ``repo`` is required (not used directly) so its sandboxed
    ``$SETFORGE_STATE_DIR`` / ``$HOME`` are active when the seed helper writes
    the base.
    """
    live_body = _JSON_LIVE if suffix == ".json" else _YAML_LIVE
    live = _seed_live(live_body.replace("\n", "\r\n"), suffix)

    seeded = _read_or_migrate_disposition_base(_PROFILE, _FILE_ID, live)
    assert seeded is not None
    stored = base_store.read_base(_PROFILE, _FILE_ID)
    assert stored is not None
    # Returned seed, stored seed, and copy_atomic's ``ours`` read all agree.
    merge_ours = live.read_text(encoding="utf-8")
    assert seeded == merge_ours
    assert stored.decode("utf-8") == merge_ours


@pytest.mark.parametrize(
    ("suffix", "shared", "live_edit", "tracked_edit"),
    [
        (
            ".json",
            '{\n  "user": "mine",\n  "upstream": "base"\n}\n',
            '{\n  "user": "mine-EDITED",\n  "upstream": "base"\n}\n',
            '{\n  "user": "mine",\n  "upstream": "base-EDITED"\n}\n',
        ),
        (
            ".yaml",
            "user: mine\nupstream: base\n",
            "user: mine-EDITED\nupstream: base\n",
            "user: mine\nupstream: base-EDITED\n",
        ),
    ],
)
def test_subsequent_merge_no_data_loss(
    repo: Path,
    suffix: str,
    shared: str,
    live_edit: str,
    tracked_edit: str,
) -> None:
    """A non-overlapping live + tracked edit merges cleanly after the seed.

    Proves the seeded base is a usable three-way ancestor: the first install
    seeds base == live (live == tracked == ``shared``, a pure no-op seed). Then
    the user edits the ``user`` key and tracked edits the DISJOINT ``upstream``
    key. The next install clean-merges both — the user's live edit is NOT
    clobbered.
    """
    _write_tracked(repo, shared, suffix=suffix)
    config = _write_config(repo, suffix=suffix)
    _seed_live(shared, suffix)
    assert _install(config).exit_code == 0  # seeds base == live (== tracked).

    _live_path(suffix).write_text(live_edit, encoding="utf-8")
    _write_tracked(repo, tracked_edit, suffix=suffix)

    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "conflict" not in result.output.lower()
    merged = _live_path(suffix).read_text(encoding="utf-8")
    assert "base-EDITED" in merged  # tracked's change landed.
    assert "mine-EDITED" in merged  # user's live edit SURVIVED.


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
def test_seed_preserves_live_mode(repo: Path, suffix: str) -> None:
    """The seeded base + any rewrite preserve the existing live mode (0600)."""
    live_body = _JSON_LIVE if suffix == ".json" else _YAML_LIVE
    tracked_body = _JSON_TRACKED if suffix == ".json" else _YAML_TRACKED
    _write_tracked(repo, tracked_body, suffix=suffix)
    config = _write_config(repo, suffix=suffix)
    live = _seed_live(live_body, suffix, mode=0o600)

    assert _install(config).exit_code == 0
    assert stat.S_IMODE(live.stat().st_mode) == 0o600


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
def test_no_live_file_takes_verbatim_seed(repo: Path, suffix: str) -> None:
    """No pre-existing live file: ordinary base-absent path (seed == tracked).

    The live-seed only applies when a live file exists to seed from. With no
    live file, the first install deploys tracked verbatim and seeds the base
    from tracked — today's base-absent behavior, unchanged.
    """
    tracked_body = _JSON_TRACKED if suffix == ".json" else _YAML_TRACKED
    _write_tracked(repo, tracked_body, suffix=suffix)
    config = _write_config(repo, suffix=suffix)

    assert _install(config).exit_code == 0
    assert _live_path(suffix).read_text(encoding="utf-8") == tracked_body
    assert base_store.read_base(_PROFILE, _FILE_ID) == tracked_body.encode("utf-8")
