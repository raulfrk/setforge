"""The span-types-retire cutover — schema 4.0 ↔ 5.0 (symmetric restamp).

The span-declaration SURFACE was already folded away and stripped at the
3.0 -> 4.0 :class:`~setforge.migrations._span_surface_retire.SpanSurfaceRetireMigration`
cutover, so at schema 4.0 no deployed config carries span data — the
``span_types`` module survived only as dead type definitions. This
migration RETIRES those type definitions and bumps the major, a pure
SCHEMA cleanup with NO data transform: the forward :meth:`apply`
defensively confirms no stray span declarations linger in ``setforge.yaml``
and then stamps ``schema_version: '5.0'`` in place.

Value-wise a :class:`~setforge.migrations.RestampMigration` (the 1.1 ↔ 1.2
symmetric-stamp pair): both endpoints carry ``schema_version``, so
:attr:`reverse` RESTAMPS the older 4.0 value — it is REACHABLE (a plain
down-stamp, never a :class:`~setforge.errors.ConfigError` refuse), because
nothing was lost that a schema restamp cannot restore. Overwrite-in-place
keeps the key at its document position, so an ``up → down → up`` cycle is
byte-identical against the post-ruamel-normalization document.

Transition ownership (INV-5): this is now the CHAIN-TERMINAL step. When a
fresh config migrates all the way from an older schema in one command, an
EARLIER cutover (span-surface-retire) records a transition whose ``file_post``
is the 4.0 image — but this restamp advances the on-disk config to 5.0
AFTER it. A single ``revert`` reverses only the LATEST transition, so if this
step did not record one the newest transition's 4.0 ``file_post`` would no
longer match the on-disk 5.0 and ``patch -R`` would fail. So the forward
:meth:`apply` records its OWN terminal transition (``writes_own_transition``
= ``True``) threading the driver's ``pre_chain_snapshot`` as ``file_pre``, so
that ONE ``revert`` reaches the chain's byte-exact ORIGIN.

The step's OWN mutation touches only ``setforge.yaml`` (the schema stamp), but
as the SOLE transition a single ``revert`` replays it must carry the FULL
reverse delta of the whole chain. So its ``file_pre`` is not restricted to
``setforge.yaml``: it threads EVERY path the ``pre_chain_snapshot`` carries —
``local.yaml`` and each reconcile-store leg an EARLIER cutover
(disposition-retire, span-surface-retire) mutated — with ``file_post``
re-snapshotting those same paths now. It carries NO ``state_snapshots``, and
that emptiness is load-bearing: with none to override the text patch, the
threaded ``file_pre`` -> ``file_post`` diff is the sole reverse authority for
the store legs too, so ``patch -R`` restores each to its absent-at-origin
state (DELETING the seeds) and one revert reaches the TRUE origin — store and
all — not the intermediate seeded shape the 3.0->4.0 terminal owner leaves
behind (it attaches its folded legs as ``state_snapshots``, which win over the
text patch and stop at the post-disposition-seed store). The two terminal
owners diverge deliberately: this restamp folds nothing of its own, so it has
no leg to snapshot and lets the origin-threaded text patch reach all the way
down. Text-threading the store legs is sound because the migrate transition
system is text/UTF-8 throughout (the driver already snapshots the same paths
as text to build ``pre_chain_snapshot``); binary tracked-file store is out of
scope for v1 chain-revert here as everywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from setforge.errors import ConfigError
from setforge.migrations import ManifestEntry, ManifestType

if TYPE_CHECKING:
    from setforge.migrations import MigrationRoots


def _stamp_schema_version(roots: MigrationRoots, to_version: str) -> None:
    """Confirm no stray span data, then overwrite ``schema_version`` in place.

    Defensive pre-flight (span data was already stripped at 3.0 -> 4.0):
    refuses if any ``tracked_files`` entry still carries a ``spans`` block, so
    a config that somehow retained one is caught loudly here rather than
    silently re-stamped over data whose model no longer exists. On a clean
    config the write touches ONLY ``schema_version`` — every other key,
    comment, and its position is preserved (overwrite-in-place, never
    ``del``-then-reinsert), so the file is byte-identical apart from the stamp.
    """
    from setforge.migrations import _require_mapping_root
    from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

    yaml = yaml_rt()
    with roots.cfg_path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    data = _require_mapping_root(data, roots.cfg_path)
    _reject_stray_spans(data, roots.cfg_path)
    data["schema_version"] = to_version
    atomic_write_yaml(roots.cfg_path, data)


def _reject_stray_spans(data: object, cfg_path: Path) -> None:
    """Raise :class:`ConfigError` if any tracked_file still declares ``spans``.

    The span surface was retired at the 3.0 -> 4.0 cutover, so a 4.0 config is
    expected to carry none. A lingering ``spans`` block would be data whose
    model (``SpanEntry``) this migration deletes, so it is refused up front
    with a pointer at the transition-based recovery rather than being stamped
    over.
    """
    from collections.abc import Mapping

    if not isinstance(data, Mapping):
        return
    tracked_files = data.get("tracked_files")
    if not isinstance(tracked_files, Mapping):
        return
    offenders = [
        str(name)
        for name, entry in tracked_files.items()
        if isinstance(entry, Mapping) and entry.get("spans") is not None
    ]
    if offenders:
        listing = ", ".join(sorted(offenders))
        raise ConfigError(
            f"cannot migrate to schema 5.0: tracked_file(s) {listing} still "
            f"declare a 'spans' block in {cfg_path}, but the span-declaration "
            "surface was retired at schema 4.0. Remove the stray 'spans' entries "
            "(or 'setforge revert --profile=migrate' to the pre-4.0 state) and "
            "re-run (nothing was changed)."
        )


def _write_stamp_transition(roots: MigrationRoots, cfg_pre: str) -> None:
    """Record the terminal cutover's origin-threading stamp transition.

    Threads the driver's chain-origin image (``pre_chain_snapshot``) as
    ``file_pre`` so a single ``revert`` reaches the chain's ORIGIN (INV-5), not
    the intermediate 4.0 state; ``file_post`` re-snapshots the SAME paths now
    (schema 5.0). In a multi-owner chain that image spans more than
    ``setforge.yaml`` — ``local.yaml`` and every reconcile-store leg an earlier
    cutover mutated — so the recorded text patch reverses ALL of them (see the
    module docstring). Applied OUTSIDE the driver (``pre_chain_snapshot is
    None``) the transition is a plain ``cfg_pre`` -> ``cfg_post`` restamp.
    Carries NO ``state_snapshots`` — with none to override it, the threaded text
    patch is the sole reverse authority, so ``patch -R`` restores each store leg
    to its absent-at-origin state rather than an intermediate seeded one.
    """
    import sys

    from setforge import transitions
    from setforge._redact import redact_argv

    pre = roots.pre_chain_snapshot
    file_pre: dict[Path, str | None]
    file_post: dict[Path, str | None]
    if pre is not None:
        file_pre = dict(pre)
        file_post = dict(transitions.snapshot_paths(tuple(pre)))
    else:
        file_pre = {roots.cfg_path: cfg_pre}
        file_post = {roots.cfg_path: roots.cfg_path.read_text(encoding="utf-8")}

    transitions.write_transition(
        transitions.make_meta(
            transitions.TransitionCommand.MIGRATE,
            transitions.MIGRATE_TRANSITION_PROFILE,
            end_timestamp=transitions.now_utc().isoformat(),
            command_line=redact_argv(sys.argv[1:]),
        ),
        file_pre,
        file_post,
        None,
        state_snapshots=(),
    )


@dataclass(slots=True, frozen=True)
class SpanTypesRetireMigration:
    """The 4.0 -> 5.0 forward restamp retiring the ``span_types`` definitions.

    A symmetric restamp (see the module docstring): both endpoints carry
    ``schema_version``, so :attr:`reverse` down-stamps to 4.0 rather than
    stripping the key. As the CHAIN-TERMINAL step it records its OWN durable
    transition threading the chain origin, so ``writes_own_transition`` is
    ``True`` (see the module docstring's INV-5 note).
    """

    from_version: str = "4.0"
    to_version: str = "5.0"

    @property
    def writes_own_transition(self) -> bool:
        """Terminal step: records its own origin-threading stamp transition."""
        return True

    @property
    def reverse(self) -> _SpanTypesRetireReverse:
        """The reachable 5.0 -> 4.0 down-restamp (from/to swapped)."""
        return _SpanTypesRetireReverse(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        """Single-file in-place stamp: an EDIT of the ``schema_version`` value."""
        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description=(
                    f"restamp schema_version: {self.from_version!r} → "
                    f"{self.to_version!r} (retire span_types definitions)"
                ),
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        """Only ``setforge.yaml`` is touched."""
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        """Confirm no stray span data, stamp 5.0, then commit the transition."""
        cfg_pre = roots.cfg_path.read_text(encoding="utf-8")
        _stamp_schema_version(roots, self.to_version)
        _write_stamp_transition(roots, cfg_pre)


@dataclass(slots=True, frozen=True)
class _SpanTypesRetireReverse:
    """The reachable 5.0 -> 4.0 inverse of :class:`SpanTypesRetireMigration`.

    Symmetric with the forward restamp: down-stamps ``schema_version`` to 4.0
    IN PLACE (never strips the key — both versions carry it, so a strip would
    let :func:`~setforge.migrations.detect_current_schema` misread the result
    as an older baseline). A standalone 5.0 -> 4.0 down-migration is a single
    step, so ``writes_own_transition`` is ``False`` and the migrate driver
    records the ordinary text transition. :attr:`reverse` returns the forward
    migration so ``reverse.reverse`` is the original 4.0 -> 5.0 direction.
    """

    from_version: str = "5.0"
    to_version: str = "4.0"

    @property
    def writes_own_transition(self) -> bool:
        """Single-step down-migration: the migrate driver records the transition."""
        return False

    @property
    def reverse(self) -> SpanTypesRetireMigration:
        """The forward 4.0 -> 5.0 cutover (keeps the Protocol symmetric)."""
        return SpanTypesRetireMigration(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        """Single-file in-place stamp: an EDIT of the ``schema_version`` value."""
        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description=(
                    f"restamp schema_version: {self.from_version!r} → "
                    f"{self.to_version!r}"
                ),
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        """Only ``setforge.yaml`` is touched."""
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        """Down-stamp ``schema_version: '4.0'`` in place (never strip)."""
        _stamp_schema_version(roots, self.to_version)
