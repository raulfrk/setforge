"""Docker E2E: real ``setforge lock`` + ``install --locked`` across ecosystems.

The Tier-3 half of the lockfile subsystem — the ONLY place the resolvers hit
their REAL
upstreams inside a fresh Debian 12 container:

- **cargo** → the real crates.io sparse index (concrete version + ``checksum``).
- **github_release** → the real GitHub releases API + a real asset
  download-and-hash (concrete tag + ``checksum``).
- **extension** → the real Visual Studio Marketplace ``extensionquery`` +
  ``vspackage`` download-and-hash (concrete version + ``checksum``). This is the
  case that VALIDATES the marketplace query/response shape end-to-end: a
  plausible-but-wrong ``extensionquery`` body would fail here, not in the mocked
  unit tests.

Cases (per the lockfile acceptance):

1. ``setforge lock`` writes a ``setforge.lock`` whose cargo / github_release /
   extension pins carry CONCRETE versions + a ``checksum`` field, no ``latest``.
2. ``install --locked`` exits 0 on that matching lock, and NON-ZERO on a drifted
   lock (a tampered github_release checksum the install-side verify rejects).

Network gate: these hit crates.io / github.com / the marketplace, so they are
opt-in behind ``SETFORGE_E2E_NETWORK`` (the same convention
``test_e2e_docker_toolchains.py`` uses) — the suite stays hermetic by default.
Same-daemon lifecycle ⇒ ``xdist_group("docker_daemon")``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

_NETWORK_ENV = "SETFORGE_E2E_NETWORK"
_network_only = pytest.mark.skipif(
    os.environ.get(_NETWORK_ENV, "") == "",
    reason=f"set {_NETWORK_ENV}=1 to run the real lock/marketplace network cases",
)

_SRC_REPO = "/tmp/cfg-lock"
_CONFIG = f"{_SRC_REPO}/setforge.yaml"
_LOCK = f"{_SRC_REPO}/setforge.lock"
_PROFILE = "lock-e2e"

# A small, permanently-stable extension (concrete pin keeps the version + hash
# deterministic across marketplace re-publishes).
_EXT_ID = "esbenp.prettier-vscode"
# jqlang/jq at a concrete tag with a ~1.7 MB linux asset — a real, stable
# github_release asset the resolver downloads + hashes.
_GHR_REPO = "jqlang/jq"
_GHR_TAG = "jq-1.7.1"
_GHR_ASSET = "jq-linux-amd64"
# A tiny, ubiquitous crate.
_CRATE = "cfg-if"

_CONFIG_YAML = f"""\
version: 1
schema_version: '5.0'
tracked_files:
  note:
    src: note.md
    dst: /tmp/lock-out/note.md
packages:
  {_CRATE}:
    type: cargo
    crate: {_CRATE}
  jq:
    type: github_release
    repo: {_GHR_REPO}
    tag: {_GHR_TAG}
    asset: {_GHR_ASSET}
    binary: jq
    install: /tmp/lock-out/bin
    extract: false
profiles:
  {_PROFILE}:
    tracked_files:
      - note
    packages:
      - {_CRATE}
      - jq
    extensions:
      include:
        - {_EXT_ID}
"""


def _write_source(c: ContainerHandle) -> None:
    c.write_text(f"{_SRC_REPO}/setforge.yaml", _CONFIG_YAML)
    c.write_text(f"{_SRC_REPO}/tracked/note.md", "# note\n")


def _lock(c: ContainerHandle, *, check: bool = True):
    return c.exec(
        [
            "uv",
            "run",
            "setforge",
            "lock",
            f"--profile={_PROFILE}",
            f"--config={_CONFIG}",
        ],
        check=check,
    )


def _install_locked(c: ContainerHandle, *, check: bool = False):
    return c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            "--locked",
            "--yes",
            f"--profile={_PROFILE}",
            f"--config={_CONFIG}",
        ],
        check=check,
    )


@_network_only
@pytest.mark.xdist_group("docker_daemon")
def test_lock_writes_concrete_pins_across_ecosystems(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``setforge lock`` pins cargo + github_release + extension concretely.

    Hits crates.io, github.com, and the REAL marketplace. Asserts the written
    lock carries a concrete (non-``latest``) version + a ``checksum`` field for
    each of the three ecosystems — the marketplace-shape validation is implicit:
    a wrong ``extensionquery`` body makes the extension pin fail to resolve and
    this test go red.
    """
    c = docker_container()
    _write_source(c)

    res = _lock(c, check=False)
    assert res.returncode == 0, res.stdout + res.stderr

    lock_text = c.read_text(_LOCK)
    assert "latest" not in lock_text, f"lock must never record 'latest':\n{lock_text}"

    cargo_pin = _pin_table(lock_text, pkg_type="cargo", key=_CRATE)
    assert re.fullmatch(r"\d+\.\d+\.\d+", cargo_pin["version"]), cargo_pin
    assert cargo_pin["checksum"].startswith("sha256:"), cargo_pin

    ghr_pin = _pin_table(lock_text, pkg_type="github_release", key=_GHR_REPO)
    assert ghr_pin["version"] == _GHR_TAG, ghr_pin
    assert ghr_pin["checksum"].startswith("sha256:"), ghr_pin

    ext_pin = _pin_table(lock_text, pkg_type="extension", key=_EXT_ID)
    # Marketplace-shape proof: a concrete semver-ish version came back.
    assert re.match(r"\d+\.\d+", ext_pin["version"]), ext_pin
    assert ext_pin["checksum"].startswith("sha256:"), ext_pin


@_network_only
@pytest.mark.xdist_group("docker_daemon")
def test_install_locked_passes_on_match_and_fails_on_drift(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``install --locked`` accepts a matching lock and rejects a drifted one.

    A tampered github_release checksum makes the install-side verify reject the
    downloaded asset → non-zero exit. Proves the lock's integrity value is
    load-bearing at install, not decorative.
    """
    c = docker_container()
    _write_source(c)

    assert _lock(c, check=False).returncode == 0

    ok = _install_locked(c, check=False)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # The pinned github_release binary landed (checksum matched).
    installed = c.exec(["test", "-f", "/tmp/lock-out/bin/jq"], check=False)
    assert installed.returncode == 0, ok.stdout + ok.stderr

    # Tamper the github_release checksum in the lock, wipe the receipt + binary
    # so the item is not SKIP-present, then re-install: the verify must fail.
    lock_text = c.read_text(_LOCK)
    tampered = _tamper_checksum(lock_text, key=_GHR_REPO)
    assert tampered != lock_text, "tamper did not alter the lock"
    c.write_text(_LOCK, tampered)
    c.exec(["rm", "-f", "/tmp/lock-out/bin/jq"], check=False)
    c.exec(
        ["sh", "-c", "rm -f ~/.local/state/setforge/receipts/* 2>/dev/null || true"],
        check=False,
    )

    drift = _install_locked(c, check=False)
    assert drift.returncode != 0, (
        "install --locked must fail on a drifted checksum:\n"
        f"{drift.stdout}{drift.stderr}"
    )


# ---------------------------------------------------------------------------
# TOML lock helpers (tomllib is stdlib, but the container may not expose it to
# a host-side parse; we parse the copied-out text on the host instead).
# ---------------------------------------------------------------------------


def _pin_table(lock_text: str, *, pkg_type: str, key: str) -> dict[str, str]:
    """Return the ``[[package]]`` table matching ``(type, key)`` as a dict.

    Parses the lock with the host's stdlib ``tomllib`` (the text was read out of
    the container) — a real parse, not a regex scrape, so a malformed lock fails
    loudly here.
    """
    import tomllib

    doc = tomllib.loads(lock_text)
    for entry in doc.get("package", []):
        if entry.get("type") == pkg_type and entry.get("key") == key:
            return {k: str(v) for k, v in entry.items()}
    raise AssertionError(f"no ({pkg_type}, {key}) pin in lock:\n{lock_text}")


def _tamper_checksum(lock_text: str, *, key: str) -> str:
    """Flip one hex nibble of the ``checksum`` on the pin whose ``key`` matches.

    A minimal, targeted mutation (not a blanket replace) so only the intended
    pin drifts and the rest of the lock stays byte-valid.
    """
    import tomllib

    doc = tomllib.loads(lock_text)
    for entry in doc.get("package", []):
        if entry.get("key") == key:
            old = entry["checksum"]
            digest = old.split(":", 1)[1]
            flipped = ("f" if digest[0] != "f" else "0") + digest[1:]
            return lock_text.replace(old, f"sha256:{flipped}")
    raise AssertionError(f"no pin with key {key!r} to tamper")
