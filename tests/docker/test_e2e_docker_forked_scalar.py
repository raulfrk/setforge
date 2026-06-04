"""Docker e2e tests for the forked-scalar 3-way ``preserve_user_keys`` merge.

Exercises the stored-base 3-way SCALAR overlay
(:mod:`setforge.scalar_overlay` + :mod:`setforge.scalar_base_store`) end-to-end
against a fresh Debian container with the actual installed ``setforge`` CLI,
through the non-interactive install / compare surfaces.

Shallow ``preserve_user_keys`` with NO ``disposition`` was upgraded from a
blind live-wins overlay to a stored-base 3-way merge of
{stored-scalar-base, live, tracked}: an upstream (tracked) change to a key the
user did NOT locally edit now propagates; the user's own edits survive; a
same-key conflict honors ``--auto`` else keeps live + warns. The FIRST install
(no base yet) keeps today's blind behavior and SEEDS the per-host scalar base
under ``state_root()/scalar-base/<profile>/<file_id>.json``.

Behavior under exercise:

- **first install seeds base** — live deployed verbatim from tracked; the
  scalar-base manifest exists with the deployed scalar values.
- **upstream propagates** — a tracked change to an untouched preserve key
  lands live on the next install (the new 3-way behavior; the OLD blind
  overlay would have kept live).
- **user edit preserved** — a live edit to a preserve key survives an install
  when tracked is unchanged.
- **same-key conflict** — both sides edit the same key: bare install keeps
  live + warns + exits 0; ``--auto=use-tracked`` takes the tracked value.
- **prune** — dropping a preserve key from the config removes its scalar-base
  manifest entry on the next install.

Profiles under exercise (declared in
``tests/fixtures/e2e/setforge.test.yaml``): ``test-forked-scalar-jsonc`` /
``test-forked-scalar-yaml``.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

# Tracked sources inside the container workspace — editing one simulates an
# upstream (tracked-side) change.
_TRACKED_JSONC = "/workspace/tests/fixtures/e2e/tracked/forked-scalar/settings.json"
_TRACKED_YAML = "/workspace/tests/fixtures/e2e/tracked/forked-scalar/config.yaml"

# Live destinations (one per format so their stored scalar bases never cross).
_LIVE_JSONC = "/home/tester/.setforge_e2e/forked-scalar/settings.json"
_LIVE_YAML = "/home/tester/.setforge_e2e/forked-scalar/config.yaml"

# Scalar-base manifests (default state root; no SETFORGE_STATE_DIR override).
_BASE_JSONC = (
    "/home/tester/.local/state/setforge/scalar-base/"
    "test-forked-scalar-jsonc/forked_scalar_jsonc.json"
)
_BASE_YAML = (
    "/home/tester/.local/state/setforge/scalar-base/"
    "test-forked-scalar-yaml/forked_scalar_yaml.json"
)


def _setforge(
    c: ContainerHandle, args: list[str], *, check: bool = False
) -> tuple[int, str, str]:
    """Run ``uv run setforge <args>`` and return (returncode, stdout, stderr)."""
    result = c.exec(["uv", "run", "setforge", *args], check=check)
    return result.returncode, result.stdout, result.stderr


def _install(
    c: ContainerHandle, profile: str, *, extra: list[str] | None = None
) -> tuple[int, str, str]:
    """Run ``setforge install --profile=<profile> --config=<fixture>``."""
    args = ["install", f"--profile={profile}", f"--config={CONFIG_FIXTURE}"]
    if extra:
        args.extend(extra)
    return _setforge(c, args)


def _read_jsonc(c: ContainerHandle, path_in_container: str) -> dict[str, object]:
    """Parse a JSONC live file, stripping whole-line ``//`` comments.

    The forked-scalar JSONC fixture carries a ``//`` comment that survives the
    overlay round-trip into the live file, so :func:`json.loads` cannot parse
    it directly. The fixture uses only whole-line comments, so dropping lines
    whose first non-space token is ``//`` is sufficient.
    """
    lines = [
        line
        for line in c.read_text(path_in_container).splitlines()
        if not line.lstrip().startswith("//")
    ]
    parsed = json.loads("\n".join(lines))
    assert isinstance(parsed, dict), parsed
    return parsed


def _read_base_manifest(
    c: ContainerHandle, path_in_container: str
) -> dict[str, object]:
    """Parse the scalar-base JSON manifest at ``path_in_container``."""
    parsed = json.loads(c.read_text(path_in_container))
    assert isinstance(parsed, dict), parsed
    return parsed


def _base_value(manifest: dict[str, object], key: str) -> object:
    """Return the ``value`` of a ``present`` scalar-base record for ``key``."""
    record = manifest[key]
    assert isinstance(record, dict), record
    assert record.get("present") is True, record
    return record.get("value")


# ---------------------------------------------------------------------------
# Scenario 1 — first install seeds the scalar base
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_first_install_seeds_scalar_base_jsonc(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """first install: live deployed; the scalar base seeds the deployed values.

    With no prior base, the JSONC file is created from tracked verbatim and
    each shallow preserve path's base is seeded to its deployed (tracked)
    value — the ancestor the NEXT install resolves against.
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-forked-scalar-jsonc")
    assert rc == 0, stderr

    live = _read_jsonc(c, _LIVE_JSONC)
    assert live["userKeyA"] == "tracked-A", live
    assert live["userKeyB"] == "tracked-B", live

    manifest = _read_base_manifest(c, _BASE_JSONC)
    assert _base_value(manifest, "userKeyA") == "tracked-A", manifest
    assert _base_value(manifest, "userKeyB") == "tracked-B", manifest


@pytest.mark.xdist_group("docker_daemon")
def test_first_install_seeds_scalar_base_yaml(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """first install (YAML parity): live deployed; base seeds deployed values."""
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-forked-scalar-yaml")
    assert rc == 0, stderr

    manifest = _read_base_manifest(c, _BASE_YAML)
    assert _base_value(manifest, "userKeyA") == "tracked-A", manifest
    assert _base_value(manifest, "userKeyB") == "tracked-B", manifest


# ---------------------------------------------------------------------------
# Scenario 2 — upstream (tracked) change propagates to an untouched key
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_upstream_change_propagates_to_untouched_key_jsonc(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """upstream propagates: a tracked change to an unedited preserve key lands live.

    Install once (base seeded). Then change ONLY the TRACKED value of
    ``userKeyA`` — the live side never touched it. The second install
    3-way-merges and live now carries the upstream value. Under the OLD blind
    live-wins overlay the live value would have stayed.
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-forked-scalar-jsonc")
    assert rc == 0, stderr
    assert _read_jsonc(c, _LIVE_JSONC)["userKeyA"] == "tracked-A"

    # Upstream moves userKeyA; live is untouched.
    c.write_text(
        _TRACKED_JSONC,
        '{\n  "trackedKey": "tracked-value",\n'
        '  "userKeyA": "tracked-A-v2",\n  "userKeyB": "tracked-B"\n}\n',
    )
    rc, _stdout, stderr = _install(c, "test-forked-scalar-jsonc")
    assert rc == 0, stderr
    live = _read_jsonc(c, _LIVE_JSONC)
    assert live["userKeyA"] == "tracked-A-v2", live
    # Base advanced to the new upstream value.
    manifest = _read_base_manifest(c, _BASE_JSONC)
    assert _base_value(manifest, "userKeyA") == "tracked-A-v2", manifest


@pytest.mark.xdist_group("docker_daemon")
def test_upstream_change_propagates_to_untouched_key_yaml(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """upstream propagates (YAML parity): tracked change to an unedited key lands."""
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-forked-scalar-yaml")
    assert rc == 0, stderr

    c.write_text(
        _TRACKED_YAML,
        "# Forked-scalar YAML fixture.\ntrackedKey: tracked-value\n"
        "userKeyA: tracked-A-v2\nuserKeyB: tracked-B\n",
    )
    rc, _stdout, stderr = _install(c, "test-forked-scalar-yaml")
    assert rc == 0, stderr
    assert "tracked-A-v2" in c.read_text(_LIVE_YAML), c.read_text(_LIVE_YAML)


# ---------------------------------------------------------------------------
# Scenario 3 — a user (live) edit is preserved when tracked is unchanged
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_user_edit_preserved_when_tracked_unchanged_jsonc(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """user edit preserved: a live-only edit to a preserve key survives install.

    Install once (base seeded). Edit ONLY the live ``userKeyA``; tracked is
    unchanged. The next install keeps the user's value (ours == base on the
    tracked side → live wins cleanly).
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-forked-scalar-jsonc")
    assert rc == 0, stderr

    # User edits live; tracked untouched.
    c.write_text(
        _LIVE_JSONC,
        '{\n  "trackedKey": "tracked-value",\n'
        '  "userKeyA": "live-A-edit",\n  "userKeyB": "tracked-B"\n}\n',
    )
    rc, _stdout, stderr = _install(c, "test-forked-scalar-jsonc")
    assert rc == 0, stderr
    live = _read_jsonc(c, _LIVE_JSONC)
    assert live["userKeyA"] == "live-A-edit", live


@pytest.mark.xdist_group("docker_daemon")
def test_user_edit_preserved_when_tracked_unchanged_yaml(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """user edit preserved (YAML parity): a live-only edit survives install."""
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-forked-scalar-yaml")
    assert rc == 0, stderr

    c.write_text(
        _LIVE_YAML,
        "# Forked-scalar YAML fixture.\ntrackedKey: tracked-value\n"
        "userKeyA: live-A-edit\nuserKeyB: tracked-B\n",
    )
    rc, _stdout, stderr = _install(c, "test-forked-scalar-yaml")
    assert rc == 0, stderr
    assert "live-A-edit" in c.read_text(_LIVE_YAML), c.read_text(_LIVE_YAML)


# ---------------------------------------------------------------------------
# Scenario 4 — same-key conflict, bare install keeps live + warns + exits 0
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_same_key_conflict_bare_install_keeps_live_and_warns_jsonc(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """conflict (bare): both sides edit the same key → keep live, warn, exit 0.

    Install once (base seeded). Then change BOTH the live and the tracked
    value of ``userKeyA`` to different values. Bare install (no --auto) defers
    the conflict: live keeps its own value, a conflict warning is emitted, and
    the command still exits 0.
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-forked-scalar-jsonc")
    assert rc == 0, stderr

    c.write_text(
        _LIVE_JSONC,
        '{\n  "trackedKey": "tracked-value",\n'
        '  "userKeyA": "live-A",\n  "userKeyB": "tracked-B"\n}\n',
    )
    c.write_text(
        _TRACKED_JSONC,
        '{\n  "trackedKey": "tracked-value",\n'
        '  "userKeyA": "tracked-A-v2",\n  "userKeyB": "tracked-B"\n}\n',
    )
    rc, stdout, stderr = _install(c, "test-forked-scalar-jsonc")
    # Bare install DEFERS the conflict — it does not abort.
    assert rc == 0, stderr
    live = _read_jsonc(c, _LIVE_JSONC)
    assert live["userKeyA"] == "live-A", live
    # Conflict warning emitted (the warning lands on stderr).
    assert "conflict" in (stdout + stderr).lower(), stdout + stderr


# ---------------------------------------------------------------------------
# Scenario 5 — same-key conflict, --auto=use-tracked takes the tracked value
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_same_key_conflict_auto_use_tracked_takes_tracked_jsonc(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """conflict (--auto=use-tracked): the same conflict takes the tracked value."""
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-forked-scalar-jsonc")
    assert rc == 0, stderr

    c.write_text(
        _LIVE_JSONC,
        '{\n  "trackedKey": "tracked-value",\n'
        '  "userKeyA": "live-A",\n  "userKeyB": "tracked-B"\n}\n',
    )
    c.write_text(
        _TRACKED_JSONC,
        '{\n  "trackedKey": "tracked-value",\n'
        '  "userKeyA": "tracked-A-v2",\n  "userKeyB": "tracked-B"\n}\n',
    )
    rc, _stdout, stderr = _install(
        c, "test-forked-scalar-jsonc", extra=["--auto=use-tracked"]
    )
    assert rc == 0, stderr
    live = _read_jsonc(c, _LIVE_JSONC)
    assert live["userKeyA"] == "tracked-A-v2", live
    # Base advanced to the chosen tracked value.
    manifest = _read_base_manifest(c, _BASE_JSONC)
    assert _base_value(manifest, "userKeyA") == "tracked-A-v2", manifest


# ---------------------------------------------------------------------------
# Scenario 6 — pruning a preserve key drops its scalar-base manifest entry
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_prune_drops_removed_key_from_scalar_base_jsonc(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """prune: removing a preserve key from the config drops its base entry.

    Install once (base seeds both userKeyA + userKeyB). Then rewrite the
    in-container config to drop ``userKeyB`` from the file's
    ``preserve_user_keys`` and install again. The scalar-base manifest no
    longer carries ``userKeyB`` (pruned), while ``userKeyA`` survives.
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-forked-scalar-jsonc")
    assert rc == 0, stderr
    manifest = _read_base_manifest(c, _BASE_JSONC)
    assert "userKeyA" in manifest, manifest
    assert "userKeyB" in manifest, manifest

    # Rewrite the config in-container: drop userKeyB from the file's
    # preserve_user_keys. The block is uniquely identified by the two
    # listed keys under the forked_scalar_jsonc tracked_file.
    config_in_container = f"/workspace/{CONFIG_FIXTURE}"
    original = c.read_text(config_in_container)
    pruned = original.replace(
        "  forked_scalar_jsonc:\n"
        "    src: forked-scalar/settings.json\n"
        "    dst: ~/.setforge_e2e/forked-scalar/settings.json\n"
        "    preserve_user_keys:\n"
        "      - userKeyA\n"
        "      - userKeyB\n",
        "  forked_scalar_jsonc:\n"
        "    src: forked-scalar/settings.json\n"
        "    dst: ~/.setforge_e2e/forked-scalar/settings.json\n"
        "    preserve_user_keys:\n"
        "      - userKeyA\n",
    )
    assert pruned != original, "config rewrite did not match the expected block"
    c.write_text(config_in_container, pruned)

    rc, _stdout, stderr = _install(c, "test-forked-scalar-jsonc")
    assert rc == 0, stderr
    manifest = _read_base_manifest(c, _BASE_JSONC)
    assert "userKeyA" in manifest, manifest
    assert "userKeyB" not in manifest, manifest
