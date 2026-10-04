"""compare classifies a reconcile-staged plain file's drift as EXPECTED (A5/A5c).

A staged plain (``disposition: None``) file's tracked holds only the promoted
set, so its live↔tracked diff (host-only LOCAL/PENDING hunks, and the
host-specific side of a SHARED_DRAFTED hunk) is the *expected* staging
divergence — not unsynced drift. A tracked hand-edit that breaks INV-8 still
classifies UNEXPECTED.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from setforge.compare import CompareStatus, DriftClass, compare_profile
from setforge.config import Config, Profile, TrackedFile
from setforge.reconcile import hunks as reconcile_hunks
from setforge.reconcile import store as reconcile_store
from setforge.reconcile.types import HunkClass, UnitRef, file_id

BASE = b"## Worktrees\nUse wt.\n\n## Paths\nworkdir: /home/generic\n"
LIVE = b"## Worktrees\nUse wt.\n\n## Shell\nzsh\n\n## Paths\nworkdir: /home/raul\n"


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def _stage(
    classes: dict[str, HunkClass], drafts: dict[UnitRef, bytes] | None = None
) -> list:
    """Record a reconcile-staged state for file-id 'x' over BASE/LIVE."""
    drafts = drafts or {}
    hunks = []
    for h in reconcile_hunks.extract_hunks(BASE, LIVE):
        cls = classes.get(h.label, HunkClass.PENDING)
        draft_hash = (
            reconcile_store.content_sha(drafts[h.ref])
            if cls is HunkClass.SHARED_DRAFTED
            else None
        )
        hunks.append(replace(h, cls=cls, draft_hash=draft_hash))
    reconcile_store.record(
        "p",
        file_id("x"),
        base=BASE,
        local=LIVE,
        staged=True,
        hunks=reconcile_hunks.serialize(hunks),
        drafts=drafts,
    )
    return hunks


def _config(tmp_path: Path, tracked: bytes) -> tuple[Config, Path]:
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True, exist_ok=True)
    (repo / "tracked" / "x").write_bytes(tracked)
    dst = tmp_path / "live" / "x"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(LIVE)  # the host's live (with Shell + /home/raul)
    config = Config(
        tracked_files={"x": TrackedFile.model_validate({"src": "x", "dst": str(dst)})},
        profiles={"p": Profile(tracked_files=["x"])},
    )
    return config, repo


def test_staged_shared_local_drift_is_expected(tmp_path: Path) -> None:
    # Shell SHARED (promoted), Paths LOCAL (kept host-only) → tracked omits the
    # host workdir, so tracked != live, but it IS the staged shape.
    hunks = _stage({"## Shell": HunkClass.SHARED, "## Paths": HunkClass.LOCAL})
    tracked = reconcile_hunks.reconstruct(BASE, LIVE, hunks, {})
    config, repo = _config(tmp_path, tracked)

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]
    assert entry.status is CompareStatus.DRIFTED  # live carries the host-only path
    assert entry.drift_class is DriftClass.EXPECTED
    # the CI gate keys on drift_class, not the disposition-intent property:
    # an EXPECTED staged file must not trip `compare --check`.
    assert report.has_unexpected_drift is False


@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_staged_content_does_not_hide_declared_mode_drift(
    tmp_path: Path, mode: int
) -> None:
    from typer.testing import CliRunner

    from setforge.cli import app

    hunks = _stage({"## Shell": HunkClass.SHARED, "## Paths": HunkClass.LOCAL})
    tracked = reconcile_hunks.reconstruct(BASE, LIVE, hunks, {})
    config, repo = _config(tmp_path, tracked)
    config.tracked_files["x"].mode = 0o600
    destination = Path(config.tracked_files["x"].dst)
    destination.chmod(mode)
    entry = compare_profile(config, "p", repo).entries[0]
    assert entry.mode_drift is (mode != 0o600)
    assert entry.drift_class is (
        DriftClass.EXPECTED if mode == 0o600 else DriftClass.UNEXPECTED
    )
    assert "staged" in (entry.reason or "")
    if mode != 0o600:
        assert "mode" in (entry.reason or "")
    path = repo / "setforge.yaml"
    path.write_text(
        "version: 1\ntracked_files:\n"
        f"  x: {{src: x, dst: {destination}, mode: 0o600}}\n"
        "profiles:\n  p: {tracked_files: [x]}\n"
    )
    result = CliRunner().invoke(
        app, ["compare", "--check", "--profile=p", f"--config={path}"]
    )
    assert result.exit_code == (0 if mode == 0o600 else 1), result.output


def test_git_backed_staged_drift_without_container_claim_is_unexpected(
    tmp_path: Path,
) -> None:
    import subprocess

    hunks = _stage({"## Shell": HunkClass.SHARED, "## Paths": HunkClass.LOCAL})
    tracked = reconcile_hunks.reconstruct(BASE, LIVE, hunks, {})
    config, repo = _config(tmp_path, tracked)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)

    report = compare_profile(config, "p", repo)

    assert report.entries[0].drift_class is DriftClass.UNEXPECTED
    assert report.has_unexpected_drift is True


def test_staged_drafted_divergence_is_expected(tmp_path: Path) -> None:
    # Paths SHARED_DRAFTED: tracked has the shareable draft, live keeps host bytes.
    paths = next(
        h for h in reconcile_hunks.extract_hunks(BASE, LIVE) if h.label == "## Paths"
    )
    draft = b"workdir: $HOME\n"
    hunks = _stage({"## Paths": HunkClass.SHARED_DRAFTED}, {paths.ref: draft})
    tracked = reconcile_hunks.reconstruct(BASE, LIVE, hunks, {paths.ref: draft})
    config, repo = _config(tmp_path, tracked)

    entry = compare_profile(config, "p", repo).entries[0]
    assert entry.drift_class is DriftClass.EXPECTED  # blessed divergence, no re-flag


def test_drafted_tracked_missing_the_draft_is_unexpected(tmp_path: Path) -> None:
    # Negative discriminator for the SHARED_DRAFTED branch: if tracked does NOT
    # carry the spliced draft (here tracked == base, draft never applied), INV-8
    # over the drafted hunk fails → real drift, not the blessed EXPECTED.
    paths = next(
        h for h in reconcile_hunks.extract_hunks(BASE, LIVE) if h.label == "## Paths"
    )
    _stage(
        {"## Paths": HunkClass.SHARED_DRAFTED},
        {paths.ref: b"workdir: $HOME\n"},
    )
    config, repo = _config(tmp_path, BASE)  # tracked lacks the $HOME draft splice

    entry = compare_profile(config, "p", repo).entries[0]
    assert entry.drift_class is DriftClass.UNEXPECTED


def test_tracked_handedit_breaks_inv8_is_unexpected(tmp_path: Path) -> None:
    # tracked carries something the promoted set does not explain → INV-8 fails.
    _stage({"## Shell": HunkClass.SHARED, "## Paths": HunkClass.LOCAL})
    config, repo = _config(tmp_path, BASE + b"\n## Rogue\nhand-edited tracked\n")

    entry = compare_profile(config, "p", repo).entries[0]
    assert entry.drift_class is DriftClass.UNEXPECTED


def test_unstaged_plain_file_is_unexpected(tmp_path: Path) -> None:
    # No reconcile index entry → not this slot's case → ordinary UNEXPECTED drift.
    config, repo = _config(tmp_path, BASE)  # tracked = base, no staging recorded
    entry = compare_profile(config, "p", repo).entries[0]
    assert entry.drift_class is DriftClass.UNEXPECTED


def _structured_dst(tmp_path: Path, name: str, body: bytes) -> Path:
    dst = tmp_path / "live" / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(body)
    return dst


def test_structured_base_missing_degrades_to_false(tmp_path: Path) -> None:
    from setforge.compare import _reconcile_staged_expected

    src = tmp_path / "repo" / "tracked" / "settings.yaml"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"theme: dark\n")
    dst = _structured_dst(tmp_path, "settings.yaml", b"theme: light\n")

    assert _reconcile_staged_expected("p", "settings.yaml", src, dst) is False


def test_structured_no_staged_units_degrades_to_false(tmp_path: Path) -> None:
    from setforge import locking
    from setforge.compare import _reconcile_staged_expected

    base, live = b"theme: dark\n", b"theme: light\n"
    with locking.profile_lock("p"):
        reconcile_store.record("p", file_id("settings.yaml"), base=base, local=live)

    src = tmp_path / "repo" / "tracked" / "settings.yaml"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(base)
    dst = _structured_dst(tmp_path, "settings.yaml", live)

    assert _reconcile_staged_expected("p", "settings.yaml", src, dst) is False


def test_structured_unparseable_live_degrades_to_false(tmp_path: Path) -> None:
    from setforge import locking
    from setforge.compare import _reconcile_staged_expected
    from setforge.reconcile.structured_units import KeyUnit, serialize_structured
    from setforge.reconcile.types import HunkClass

    base = b'{"theme": "dark"}'
    unit = KeyUnit(HunkClass.LOCAL, "theme", "theme", "sha256:x")
    with locking.profile_lock("p"):
        reconcile_store.record(
            "p",
            file_id("settings.json"),
            base=base,
            local=b'{"theme": "light"}',
            staged=True,
            hunks=serialize_structured([unit]),
        )

    src = tmp_path / "repo" / "tracked" / "settings.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(base)
    dst = _structured_dst(tmp_path, "settings.json", b"{ not valid json")

    assert _reconcile_staged_expected("p", "settings.json", src, dst) is False


def _claim_container(repo: Path, dst: Path, claimant: str, tmp_path: Path) -> None:
    """Give ``dst`` a current container claim held by this or another checkout."""
    import subprocess

    from setforge.file_ownership import observe_file
    from setforge.locking import mutation_locks
    from setforge.ownership import OwnershipStore, load_or_create_owner_id

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    own = load_or_create_owner_id(repo)
    other = tmp_path / "other-checkout"
    other.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=other, check=True)
    foreign = load_or_create_owner_id(other)
    if claimant == "none":
        return
    observation = observe_file(dst)
    with mutation_locks(resources=True):
        OwnershipStore().claim_locked(
            resource_id=observation.resource_id,
            owner_id=own if claimant == "own" else foreign,
            declaration_refs=("tracked_files.x",),
            provenance=(),
            locator=str(dst),
            fingerprint=observation.fingerprint,
            expected_generation=None,
        )


@pytest.mark.parametrize(
    ("claimant", "action", "authorized"),
    [
        ("none", "adopt", False),
        ("foreign", "transfer", False),
        ("own", "manage", True),
    ],
)
def test_compare_and_install_agree_on_container_authority(
    tmp_path: Path, claimant: str, action: str, authorized: bool
) -> None:
    """A container this checkout must first adopt or take over is unauthorized.

    Staged units cannot explain drift in a file another configuration owns, so
    compare classifies it exactly as the install plan does.
    """
    import setforge.cli.install as install_mod
    from setforge.compare import file_authorization_map
    from setforge.config import resolve_profile
    from setforge.ownership import read_owner_id

    hunks = _stage({"## Shell": HunkClass.SHARED, "## Paths": HunkClass.LOCAL})
    tracked = reconcile_hunks.reconstruct(BASE, LIVE, hunks, {})
    config, repo = _config(tmp_path, tracked)
    dst = Path(config.tracked_files["x"].dst)
    _claim_container(repo, dst, claimant, tmp_path)
    resolved = resolve_profile(config, "p")
    entries = ((config.tracked_files["x"], "x", repo / "tracked" / "x", dst),)

    decisions = install_mod._plan_file_ownership(
        entries, profile="p", owner_id=read_owner_id(repo)
    )
    assert [decision.action.value for decision in decisions] == [action]
    compare_map = file_authorization_map(config, resolved, repo)
    assert compare_map == {"x": authorized}
    assert install_mod._file_ownership_authorization(entries, decisions) == compare_map

    report = compare_profile(
        config, "p", repo, resolved=resolved, ownership_authorized=compare_map
    )
    assert report.entries[0].drift_class is (
        DriftClass.EXPECTED if authorized else DriftClass.UNEXPECTED
    )
    assert report.has_unexpected_drift is not authorized
