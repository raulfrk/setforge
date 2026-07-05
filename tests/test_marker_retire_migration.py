"""Up-migration tests for the 2.0 -> 2.1 MARKER-RETIRE migration.

The migration strips every ``setforge:user-section`` marker from each profile's
tracked + live files (shared bodies stay inline; host-local bodies are captured
into ``local.yaml`` overlay spans first), seeds the reconcile store with a
baseline-derived 3-way base, and stamps ``schema_version: "2.1"`` last. It is
data-loss-critical, so the host-local capture, the fail-closed refusal, the
baseline-not-live base seeding (MP-4), per-file idempotency (MP-3), and the
profile x file enumeration (C-7) are pinned here end to end.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from setforge import reconcile
from setforge.errors import ConfigError, MarkerError
from setforge.migrations import MigrationRoots
from setforge.migrations._marker_retire import MarkerRetireMigration
from setforge.reconcile import file_id
from setforge.source import HostLocalSectionName, load_local_host_local_sections


def _h(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _shared(name: str, body: str, *, hash_: str | None = None) -> str:
    end = f" hash={hash_}" if hash_ is not None else ""
    return (
        f"<!-- setforge:user-section start shared {name} -->\n"
        f"{body}"
        f"<!-- setforge:user-section end shared {name}{end} -->\n"
    )


def _host_local(name: str, body: str, *, hash_: str | None = None) -> str:
    end = f" hash={hash_}" if hash_ is not None else ""
    return (
        f"<!-- setforge:user-section start host-local {name} -->\n"
        f"{body}"
        f"<!-- setforge:user-section end host-local {name}{end} -->\n"
    )


_CFG = (
    'schema_version: "2.0"\n'
    "tracked_files:\n"
    "  claude:\n"
    "    src: claude/CLAUDE.md\n"
    "    dst: ~/.claude/CLAUDE.md\n"
    "profiles:\n"
    "  default:\n"
    "    tracked_files: [claude]\n"
)


def _setup(
    tmp_path: Path, *, tracked: str, live: str, cfg: str = _CFG
) -> MigrationRoots:
    """Lay down setforge.yaml + the tracked source + the deployed live file."""
    repo = tmp_path / "repo"
    (repo / "tracked" / "claude").mkdir(parents=True)
    (repo / "setforge.yaml").write_text(cfg, encoding="utf-8")
    (repo / "tracked" / "claude" / "CLAUDE.md").write_text(tracked, encoding="utf-8")
    live_path = Path("~/.claude/CLAUDE.md").expanduser()
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text(live, encoding="utf-8")
    return MigrationRoots(
        cfg_path=repo / "setforge.yaml", repo_root=repo, home=Path.home()
    )


def _tracked_path(roots: MigrationRoots) -> Path:
    return roots.repo_root / "tracked" / "claude" / "CLAUDE.md"


def _live_path() -> Path:
    return Path("~/.claude/CLAUDE.md").expanduser()


# --- strip + stamp ----------------------------------------------------------


def test_apply_strips_markers_from_tracked_and_live(tmp_path: Path) -> None:
    body = "Use rg not grep.\n"
    text = "# Rules\n" + _shared("rules", body, hash_=_h(body)) + "tail\n"
    roots = _setup(tmp_path, tracked=text, live=text)

    MarkerRetireMigration().apply(roots=roots)

    stripped = "# Rules\nUse rg not grep.\ntail\n"
    assert _tracked_path(roots).read_text(encoding="utf-8") == stripped
    assert _live_path().read_text(encoding="utf-8") == stripped
    assert "setforge:user-section" not in _tracked_path(roots).read_text("utf-8")


def test_apply_stamps_schema_version_last(tmp_path: Path) -> None:
    body = "x\n"
    text = _shared("r", body, hash_=_h(body))
    roots = _setup(tmp_path, tracked=text, live=text)

    MarkerRetireMigration().apply(roots=roots)

    assert 'schema_version: "2.1"' in roots.cfg_path.read_text(encoding="utf-8")


# --- host-local capture (DL4) ----------------------------------------------


def test_apply_captures_host_local_to_overlay_then_strips(tmp_path: Path) -> None:
    shared_body = "shared rule\n"
    host_body = "workdir: /home/raul\n"
    live = _shared("rules", shared_body, hash_=_h(shared_body)) + _host_local(
        "paths", host_body, hash_=_h(host_body)
    )
    tracked = _shared("rules", shared_body, hash_=_h(shared_body))
    roots = _setup(tmp_path, tracked=tracked, live=live)

    MarkerRetireMigration().apply(roots=roots)

    local_yaml = roots.home / ".config" / "setforge" / "local.yaml"
    sections = load_local_host_local_sections(local_yaml)["claude"]
    assert set(sections) == {"paths"}
    assert sections[HostLocalSectionName("paths")].body == host_body
    # ...and absent from both stripped files (it lives only in the overlay now).
    assert host_body not in _live_path().read_text(encoding="utf-8")
    assert host_body not in _tracked_path(roots).read_text(encoding="utf-8")


# --- fail-closed (KQ3) ------------------------------------------------------


def test_apply_refuses_unnamed_host_local_marker(tmp_path: Path) -> None:
    # An unnamed host-local marker is missing its keyword grammar -> refuse,
    # never silently reclassify the body as shared.
    bad = "<!-- setforge:user-section start rules -->\nsecret\n"
    roots = _setup(tmp_path, tracked=bad, live=bad)

    with pytest.raises(MarkerError):
        MarkerRetireMigration().apply(roots=roots)

    # Nothing stripped: the file still carries its original markers.
    assert "setforge:user-section" in _tracked_path(roots).read_text("utf-8")


def test_apply_refuses_legacy_namespace_marker(tmp_path: Path) -> None:
    bad = (
        "<!-- my-setup:user-section start host-local p -->\n"
        "secret\n"
        "<!-- my-setup:user-section end host-local p -->\n"
    )
    roots = _setup(tmp_path, tracked=bad, live=bad)

    with pytest.raises(MarkerError):
        MarkerRetireMigration().apply(roots=roots)
    # The host body never reached a shared/tracked surface.
    assert "secret" in _live_path().read_text("utf-8")  # untouched live


# --- base seeding (MP-4) ----------------------------------------------------


def test_apply_seeds_base_from_live_when_pending_tracked(tmp_path: Path) -> None:
    # Live pristine (matches its baseline), tracked moved => the recorded base is
    # the live body so the next install applies the pending tracked update.
    base_body = "old shared\n"
    live = _shared("r", base_body, hash_=_h(base_body))
    tracked = _shared("r", "new shared\n", hash_=_h(base_body))
    roots = _setup(tmp_path, tracked=tracked, live=live)

    MarkerRetireMigration().apply(roots=roots)

    base = reconcile.read_base("default", file_id("claude"))
    assert base == b"old shared\n"


def test_apply_seeds_base_from_tracked_when_live_edited(tmp_path: Path) -> None:
    # Live moved, tracked pristine => base is the TRACKED baseline, NOT live.
    # Seeding live here would make ours==base and lose the live edit on next merge.
    base_body = "baseline\n"
    live = _shared("r", "my live edit\n", hash_=_h(base_body))
    tracked = _shared("r", base_body, hash_=_h(base_body))
    roots = _setup(tmp_path, tracked=tracked, live=live)

    MarkerRetireMigration().apply(roots=roots)

    assert reconcile.read_base("default", file_id("claude")) == b"baseline\n"
    # Local keeps the host's edited content.
    assert reconcile.read_local("default", file_id("claude")) == b"my live edit\n"


# --- per-file idempotency (MP-3) -------------------------------------------


def test_apply_is_idempotent_per_file(tmp_path: Path) -> None:
    # A file with no markers left (already stripped) is skipped on replay: the
    # base is not overwritten with the stripped bytes.
    base_body = "old shared\n"
    live = _shared("r", base_body, hash_=_h(base_body))
    tracked = _shared("r", "new shared\n", hash_=_h(base_body))
    roots = _setup(tmp_path, tracked=tracked, live=live)

    MarkerRetireMigration().apply(roots=roots)
    first = reconcile.read_base("default", file_id("claude"))
    # Re-run over the now-markerless files.
    MarkerRetireMigration().apply(roots=roots)
    assert reconcile.read_base("default", file_id("claude")) == first


# --- enumeration (C-7) ------------------------------------------------------


def test_apply_seeds_every_profile_the_file_resolves_under(tmp_path: Path) -> None:
    cfg = (
        'schema_version: "2.0"\n'
        "tracked_files:\n"
        "  claude:\n"
        "    src: claude/CLAUDE.md\n"
        "    dst: ~/.claude/CLAUDE.md\n"
        "profiles:\n"
        "  default:\n"
        "    tracked_files: [claude]\n"
        "  other:\n"
        "    tracked_files: [claude]\n"
    )
    body = "shared\n"
    text = _shared("r", body, hash_=_h(body))
    roots = _setup(tmp_path, tracked=text, live=text, cfg=cfg)

    MarkerRetireMigration().apply(roots=roots)

    assert reconcile.read_base("default", file_id("claude")) == b"shared\n"
    assert reconcile.read_base("other", file_id("claude")) == b"shared\n"


# --- reverse (KQ5 / MP-2) ---------------------------------------------------


def test_reverse_refuses_irreversible(tmp_path: Path) -> None:
    # Marker retirement is one-way: stripped markers cannot be regenerated, so the
    # down-migration refuses cleanly rather than restamping to 2.0.
    rev = MarkerRetireMigration().reverse
    roots = _setup(tmp_path, tracked="x\n", live="x\n")
    with pytest.raises(ConfigError):
        rev.apply(roots=roots)
