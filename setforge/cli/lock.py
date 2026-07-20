"""The ``lock`` subcommand: FAIL-CLOSED, all pins resolve before the single write."""

from __future__ import annotations

from pathlib import Path

import typer

import setforge.provision.resolve.cargo as _cargo_resolver  # noqa: F401
import setforge.provision.resolve.extension as _extension_resolver  # noqa: F401
import setforge.provision.resolve.github_release as _ghr_resolver  # noqa: F401
import setforge.provision.resolve.go as _go_resolver  # noqa: F401
import setforge.provision.resolve.plugin as _plugin_resolver  # noqa: F401
import setforge.provision.resolve.python as _python_resolver  # noqa: F401
from setforge.cli import _CONFIG_OPTION, _resolve_config_arg, app
from setforge.cli._help_examples import LOCK_EXAMPLES
from setforge.cli._lock_enumerate import _LockItem, enumerate_lock_items
from setforge.config import load_config, resolve_and_expand
from setforge.errors import LockConflict, ResolveError
from setforge.lockfile import LockFile, lock_path, parse_lock, write_lock
from setforge.locking import lockfile_lock
from setforge.provision.resolve.protocol import ResolvedPin
from setforge.provision.resolve.registry import get_resolver


def resolve_pins(items: list[_LockItem], profile: str) -> list[ResolvedPin]:
    pins: list[ResolvedPin] = []
    for item in items:
        resolver = get_resolver(item.pkg_type)
        pin = resolver.resolve(item.resolve_input)
        pins.append(pin.model_copy(update={"profiles": (profile,)}))
    return pins


def merge_lock(existing: LockFile | None, new_pins: list[ResolvedPin]) -> LockFile:
    # A shared key at a different version/integrity is a hard LockConflict.
    merged: dict[tuple[str, str], ResolvedPin] = {}
    if existing is not None:
        for pin in existing.packages:
            merged[pin.sort_key()] = pin

    for pin in new_pins:
        key = pin.sort_key()
        prior = merged.get(key)
        if prior is None:
            merged[key] = pin
            continue
        if prior.version != pin.version:
            raise LockConflict(
                f"package {pin.key!r} (type {pin.type.value!r}) resolves to "
                f"{pin.version!r} for profile(s) {sorted(pin.profiles)} but the "
                f"lock already pins {prior.version!r} for profile(s) "
                f"{sorted(prior.profiles)}; a shared package must resolve to one "
                f"version across profiles"
            )
        if prior.integrity != pin.integrity:
            raise LockConflict(
                f"package {pin.key!r} (type {pin.type.value!r}) at version "
                f"{pin.version!r} resolves to integrity {pin.integrity!r} for "
                f"profile(s) {sorted(pin.profiles)} but the lock already pins "
                f"{prior.integrity!r} for profile(s) {sorted(prior.profiles)}; a "
                f"same-version package must resolve to one integrity across "
                f"profiles"
            )
        merged[key] = prior.model_copy(
            update={"profiles": _union_profiles(prior.profiles, pin.profiles)}
        )

    return LockFile(packages=tuple(merged.values()))


def _union_profiles(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(a) | set(b)))


def apply_update(existing: LockFile, updated: ResolvedPin) -> LockFile:
    key = updated.sort_key()
    packages: list[ResolvedPin] = []
    replaced = False
    for pin in existing.packages:
        if pin.sort_key() == key:
            packages.append(updated.model_copy(update={"profiles": pin.profiles}))
            replaced = True
        else:
            packages.append(pin)
    if not replaced:
        packages.append(updated)
    return LockFile(packages=tuple(packages))


def _load_existing_lock(path: Path) -> LockFile | None:
    if not path.exists():
        return None
    return parse_lock(path.read_text(encoding="utf-8"))


@app.command("lock", epilog=LOCK_EXAMPLES)
def lock(
    profile: str = typer.Option(..., "--profile", "-p", help="Profile to lock."),
    update: str | None = typer.Option(
        None,
        "--update",
        help="Re-resolve ONLY this package (by its lock key), preserving the "
        "rest of the lock.",
    ),
    config: Path = _CONFIG_OPTION,
) -> None:
    """Resolve the profile's package pins and write the shared lockfile.

    ``--update <key>`` re-resolves only that one package, preserving the
    rest of the lock. The read→merge→write is serialized under the config
    dir so concurrent per-profile locks cannot clobber each other.
    """
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_and_expand(cfg, profile, repo_root)

    path = lock_path(config)
    items = enumerate_lock_items(cfg, resolved)

    # The lockfile is shared across ALL profiles, so concurrent
    # `setforge lock --profile=A` and `--profile=B` would each read the same
    # baseline and clobber each other on write (silent lost update). Serialize
    # the whole read -> merge -> write on the config dir, and re-read the
    # existing lock UNDER the lock so a second writer sees the first's pins and
    # merge_lock unions them. Keyed on the config dir (not the profile) because
    # the lockfile is profile-independent.
    with lockfile_lock(path.parent):
        existing = _load_existing_lock(path)

        if update is not None:
            _run_update(items, existing, update, profile, path)
            return

        typer.echo(f"resolving {len(items)} package(s) for profile {profile!r}…")
        new_pins = resolve_pins(items, profile)
        lockfile = merge_lock(existing, new_pins)
        write_lock(lockfile, path)
        typer.echo(f"wrote {path.name} ({len(lockfile.packages)} pin(s))")


def _run_update(
    items: list[_LockItem],
    existing: LockFile | None,
    update_key: str,
    profile: str,
    path: Path,
) -> None:
    if existing is None:
        raise ResolveError(
            f"cannot --update {update_key!r}: no {path.name} exists yet; run "
            f"'setforge lock --profile={profile}' first"
        )
    target = next((item for item in items if item.lock_key() == update_key), None)
    if target is None:
        raise ResolveError(
            f"cannot --update {update_key!r}: no package with that lock key is "
            f"declared in profile {profile!r}"
        )
    pin = get_resolver(target.pkg_type).resolve(target.resolve_input)
    updated = pin.model_copy(update={"profiles": (profile,)})
    write_lock(apply_update(existing, updated), path)
    typer.echo(f"updated {update_key!r} in {path.name}")
