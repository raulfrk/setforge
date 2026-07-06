"""Exception hierarchy for setforge.

All recoverable failures inherit from SetforgeError so the CLI top-level
handler can render them as ``error: <message>`` and exit 1, while
unexpected exceptions bubble with a traceback.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ValidationErrorWithContext:
    """Structured carrier for a single mockup-D validate failure.

    Surfaces the file:line + snippet + offending-value column + Fix
    hint + optional close-match suggestion through the
    :mod:`setforge.cli._validate_errors` formatters. Not an
    :class:`Exception` — validate collects these into a ``list`` and
    renders all of them before exiting non-zero (mockup D's
    report-all-then-refuse contract).

    ``snippet_lines`` carries the rendered snippet rows in display
    order; the last row is the one the ``←─── line N`` marker
    annotates. ``column`` is 1-indexed to match ruamel.yaml's
    ``.lc.value`` convention (line, column) tuple.
    """

    file_path: Path
    line: int
    column: int
    snippet_lines: list[str]
    field_value: str
    fix_hint: str
    suggestion: str | None = None


class SetforgeError(Exception):
    """Base class for all setforge recoverable failures."""


class ConfigError(SetforgeError):
    """Raised when the YAML config is malformed, fails schema validation,
    or has an invalid profile chain (e.g. a cycle in extends:)."""


class ProfileNotFound(ConfigError):
    """Raised when the user requests a --profile=<name> that does not
    exist in the loaded config."""


class MissingTrackedFile(SetforgeError):
    """Raised when a TrackedFile entry's ``src`` path does not exist on disk
    at deploy time."""


class NoSourceConfigured(SetforgeError):
    """Raised when ``setforge.source.resolve_source`` walks all four
    precedence layers (CLI flag, env var, host-local ``local.yaml``,
    CWD fallback) and none produces a source. The message lists each
    layer so the user knows where to configure."""


class SourceNotCloned(SetforgeError):
    """Raised when a :class:`setforge.source.GitSource`'s expected
    ``clone_dest`` directory does not exist on disk. The user must run
    ``setforge fetch`` to clone the source before any read command."""


class GitOpError(SetforgeError):
    """Raised when a subprocess invocation of ``git`` exits non-zero or
    times out. The wrapped error's stderr is surfaced in the message
    so the user sees git's own diagnostic."""


class DirtySourceCheckout(SetforgeError):
    """Raised by the sync/capture pre-write gate when the source's
    ``tracked/`` subtree has uncommitted changes. The message lists
    the affected file count and a recovery hint ("commit or stash
    before retrying"). No ``--force`` flag — the user takes the
    explicit recovery action."""


class BackupCollision(SetforgeError):
    """Reserved for backup-path collisions that cannot be safely overwritten.

    Note: the default copy_atomic policy overwrites a pre-existing dst.bak
    silently; this exception is for future strict-mode use.
    """


class MarkerError(SetforgeError):
    """Raised when user-section markers in a tracked file are malformed
    (mismatched start/end, nesting, or unknown directives)."""


class AnchorNotFoundError(ConfigError):
    """Raised when a :data:`setforge.source.Anchor` does not match any
    line in the tracked file at install time.

    Subclass of :class:`ConfigError` so the existing ``ConfigError``
    catch in the validate/install paths surfaces the message verbatim
    without a separate handler.
    """


class AnchorAmbiguousError(ConfigError):
    """Raised when an anchor matches more than one candidate line in the
    tracked file.

    Duplicate ``## Profiles`` headings, two end markers carrying the
    same after-section name, etc. The message names every match's line
    number so the user can disambiguate by renaming or removing the
    duplicate.
    """


class ExtensionToolMissing(SetforgeError):
    """Raised when the ``code`` CLI is required for an action but is not
    on PATH."""


class PluginToolMissing(SetforgeError):
    """Raised when the ``claude`` CLI is required for an action but is
    not on PATH."""


class PluginReconcileItemFailed(SetforgeError):
    """Raised by a per-plugin reconcile attempt when the underlying
    ``claude plugin <verb>`` subprocess (or marketplace add/fetch)
    exits non-zero or times out.

    Carries the plugin ID, a one-line ``error_summary`` (tail of stderr),
    and the full captured stderr/diagnostic trace as ``full_stderr`` so
    the reconcile loop in :mod:`setforge.cli._plugin_helpers` can surface
    a ``skip / retry / abort / diagnose`` arrow-key prompt without
    aborting the outer install batch. Subclass of :class:`SetforgeError`
    so the global handler renders it cleanly when it escapes the prompt
    boundary (ABORT path)."""

    def __init__(
        self,
        *,
        item_id: str,
        error_summary: str,
        full_stderr: str,
    ) -> None:
        self.item_id = item_id
        self.error_summary = error_summary
        self.full_stderr = full_stderr
        super().__init__(f"plugin reconcile failed for {item_id!r}: {error_summary}")


class ReconcileAborted(SetforgeError):
    """Raised by the per-item reconcile loop when the user selects
    ABORT from the failure-prompt arrow-key picker.

    The loop calls :func:`setforge.cli._plugin_helpers._abort_reverse_reconcile`
    to roll back items that landed in THIS install before raising. Caught
    at the install command boundary by the global :class:`SetforgeError`
    handler so the user sees a clean ``error: install aborted...`` line."""


class MergeTypeMismatch(SetforgeError):
    """Raised when a merge encounters incompatible leaf types at a key
    path (e.g. a str on one side vs a list on the other).

    Raised by the YAML/JSONC merge paths (``yaml_merge.overlay``) and by
    the scalar 3-way resolver (``scalar_merge.resolve_scalar``) when a
    non-scalar operand reaches a scalar merge boundary.
    """


class ExtensionInstallFailed(SetforgeError):
    """Raised when ``code --install-extension`` or
    ``--uninstall-extension`` exits non-zero or times out, or when
    ``code --list-extensions`` itself fails. The message includes the
    extension ID (when applicable) and the captured stderr."""


class RevertFailed(SetforgeError):
    """Raised by ``setforge revert`` when ``patch -R`` rejects the diff
    (drifted files), when the ``patch`` binary isn't on PATH, or when
    an extension reverse install/uninstall fails. Message includes the
    captured stderr or the conflicting paths."""


class CaptureRequiresInteractive(SetforgeError):
    """Raised when ``setforge sync`` would need wizard prompts but no
    TTY is available and ``--auto`` wasn't passed.

    Triggered by :func:`setforge.capture.capture_profile` when the
    capture-time wizard would surface drift (deep-merge sub-key drift or
    top-level non-preserve drift) and the caller cannot prompt. The
    escape hatch is the ``--auto={use-live, keep-tracked}`` CLI flag,
    which routes through :func:`setforge.wizard.run_wizard_loop`'s
    ``auto_accept`` parameter."""


class ConfirmRequiresInteractive(SetforgeError):
    """Raised when a mutating ``--auto*`` flag is set, stdin is not a
    TTY, and ``--yes`` was not passed.

    Sibling of :class:`CaptureRequiresInteractive` for the
    :func:`setforge.cli._confirm.confirm_auto_operation` gate that
    fronts ``install --auto-accept-*`` / ``install --auto=use-tracked``
    / ``sync --auto=use-live``. The escape hatch is ``--yes`` /
    ``-y``, which short-circuits the prompt for scripted contexts."""


class WelcomeRequiresInteractive(SetforgeError):
    """Raised when ``setforge install`` detects a fresh host but cannot
    render the welcome panel because stdin is not a TTY and ``--yes``
    was not passed.

    Sibling of :class:`ConfirmRequiresInteractive` for the
    :func:`setforge.cli._welcome.prompt_welcome` gate that fires on
    every fresh-host ``setforge install`` invocation (no transition
    record present for any profile). The welcome panel is information +
    consent; a non-TTY caller cannot act on either side, so the gate
    raises rather than falling back to a default. The escape hatch is
    ``--yes`` / ``-y``, which skips the welcome entirely (the user has
    already consented out-of-band)."""


class OrphanCleanupRequiresInteractive(SetforgeError):
    """Raised when ``setforge cleanup-orphans --apply`` is invoked
    without a TTY and without ``--yes``.

    Sibling of :class:`ConfirmRequiresInteractive` for the orphan
    cleanup arrow-key wizard. ``cleanup-orphans --apply`` is a
    mutate-gate (deletion is irreversible without a transition
    record), so the non-TTY + no-``--yes`` combination raises instead
    of falling back to a default — consent must be explicit. The
    escape hatch is ``--yes``, which short-circuits to the safe
    revert-able branch (delete + write transition)."""


class SharedSpanReconcileRequiresInteractive(SetforgeError):
    """Raised when ``setforge install --reconcile-user-sections`` detects a
    host-local↔shared span collision but cannot prompt.

    Sibling of :class:`OrphanCleanupRequiresInteractive` for the
    shared-span intent-collision reconcile surface. ``--reconcile-user-sections``
    is the interactive switch; when a same-anchor collision exists and
    stdout is not a TTY (and no ``--auto`` was passed), there is no safe
    default — silently keeping the host-local side would bury the
    collision. The escape hatch is ``--auto=use-tracked`` (adopt
    the shared intent) or ``--auto=keep-live`` (keep the host-local
    override), both of which resolve every collision non-interactively."""


class NoTransitionFound(SetforgeError):
    """Raised by ``setforge revert`` when no transition history exists
    for the requested profile."""


class InvalidTransitionRecord(SetforgeError):
    """Raised when an on-disk transition record (extensions.json /
    plugins.json) has a corrupt shape.

    Surfaced by :func:`setforge.transitions.plugin_delta_from_json`
    when a ``marketplaces_removed`` entry fails its (name, dict) shape
    check — e.g. hand-edited plugins.json, partial-write damage, or a
    bug in a future writer. Caught at the revert command boundary by
    the existing :class:`SetforgeError` handler so the user sees a
    clean error instead of an opaque ``ValueError`` from a tuple
    unpack mid-revert."""


class MarketplaceCacheMiss(SetforgeError):
    """Raised when local-clone install mode cannot resolve a marketplace
    to a local cache directory.

    Triggered by :func:`setforge.claude_plugins._clone_marketplace` in
    three cases: the ``git`` binary is missing from PATH, the on-demand
    ``git clone`` failed (typically offline), or an existing cache's
    ``origin`` remote no longer matches the configured source repo and
    a re-clone failed. The message names the marketplace and the exact
    remediation (``setforge plugin sync-cache --profile=<name>`` while
    online, or fall back to ``claude.install_mode: regular``)."""


class PyPIFetchError(SetforgeError):
    """Raised when ``setforge upgrade`` cannot fetch latest-version metadata
    from the PyPI JSON API.

    Triggered by :func:`setforge._pypi_client.fetch_latest_version` on
    network failure, HTTP non-200/304 responses, JSON decode errors, or
    on cache-disk failures when reading/writing the ETag sidecar.
    Message is suitable for direct surface to the user — the CLI top-
    level handler renders it as ``error: <message>`` and exits 1.
    """


class UpgradeError(SetforgeError):
    """Raised when ``setforge upgrade`` cannot complete its wrapped
    ``uv tool upgrade`` invocation.

    Triggered by :mod:`setforge.cli.upgrade` when the ``uv`` binary is
    missing from ``PATH``, the ``uv tool upgrade`` subprocess exits
    non-zero, the post-upgrade ``uv tool list`` verification step does
    not see the expected version pinned, or the user-supplied
    ``--to=<version>`` cannot be located on PyPI. Distinct from
    :class:`PyPIFetchError` (purely-fetch-time concerns).
    """


class BaseStoreError(SetforgeError):
    """Base class for per-host stored-base failures.

    The stored-base layer (:mod:`setforge.base_store`) persists the
    verbatim last-deployed bytes of each tracked file under
    ``<state_root>/base/<profile>/<file-id>``. Failures reading or
    writing that store inherit from this class so the CLI top-level
    handler renders them as ``error: <message>`` and exits 1.
    """


class BaseStoreIOError(BaseStoreError):
    """Raised when a stored-base read or write fails at the OS level.

    Wraps the underlying :class:`OSError` (permissions, disk full,
    missing parent that cannot be created) so callers see a setforge
    diagnostic naming the profile and file-id rather than an opaque
    filesystem traceback.
    """


class BaseStoreSchemaError(BaseStoreError):
    """Raised when a stored-base root carries an incompatible format version.

    Each per-profile stored-base root (byte store
    ``<state_root>/base/<profile>/``, scalar store
    ``<state_root>/scalar-base/<profile>/``) carries a ``.format-version``
    sidecar recording the on-disk format the writer used. A read refuses
    with this error when the sidecar's recorded version does not match the
    version this engine writes, or when the sidecar is present but
    unparseable / unreadable — so a future-format root is refused rather
    than silently mis-parsed.

    The message names the offending store root and, on a version
    mismatch, the expected-vs-found pair (the unparseable-sidecar and
    OSError-while-reading sites carry no such pair), and points at the
    recovery: deleting that root re-grandfathers
    it (the next merge degrades to a noisier full-content merge, never a
    crash). A SIBLING of :class:`BaseStoreIOError` — a version mismatch is a
    schema-contract failure, distinct from an OS-level read/write failure.
    """


class BinaryOverrideInvalid(SetforgeError):
    """Raised when a host-local binary override (CLI flag, env var, or
    ``~/.config/setforge/local.yaml``) points at a path that does not
    exist or is not executable. Carries the layer, binary name, path,
    and reason as structured fields so callers can render or test
    against them precisely."""

    def __init__(self, *, layer: str, binary: str, path: str, reason: str) -> None:
        self.layer = layer
        self.binary = binary
        self.path = path
        self.reason = reason
        super().__init__(
            f"{layer} override for {binary!r} → {path!r}: {reason}. "
            f"Edit ~/.config/setforge/local.yaml or unset the override."
        )


class ReconcileStoreError(SetforgeError):
    """Base class for reconcile-engine store (base/local/index) failures.

    The reconcile store (:mod:`setforge.reconcile.store`) holds the merge
    ``base``, the recorded keep-local content, and a per-profile ``index``
    under ``<state_root>/{base,local,index}/``. Failures inherit from this
    class so the CLI top-level handler renders them as ``error: <message>``
    and exits 1 — a raw :class:`json.JSONDecodeError` or :class:`OSError`
    must never escape the store.
    """


class CorruptIndexError(ReconcileStoreError):
    """Raised when ``index/<profile>.json`` is unparseable, wrong-shaped, or
    missing a mandatory field.

    A parsed-but-incomplete document (e.g. no ``schema_version`` key) is
    corruption, NOT a legitimately-absent index. Raised first by the codec
    (:func:`~setforge.reconcile.index_model.loads`) with the shape error;
    :func:`~setforge.reconcile.store.read_index` re-wraps it to add the
    profile, path, and recovery hint (inspect or delete the index to rebuild
    it). Fail-closed: the store refuses rather than silently re-seeding, which
    would resurrect the silent-overwrite class of bug the store exists to kill.
    """


class IndexVersionError(ReconcileStoreError):
    """Raised when ``index/<profile>.json`` records a ``schema_version``
    NEWER than this engine can read.

    The message names the found-vs-expected versions and the recovery
    (upgrade setforge). An older version is migrated in memory; only a
    forward-incompatible (newer) version is refused — a future format is
    never best-effort parsed.
    """


class InvariantViolation(ReconcileStoreError):
    """Raised when INV-2 or INV-10 is detected at runtime.

    INV-2 (``base + recorded-local == live``, byte-exact) and INV-10
    (index ↔ on-disk consistent, no orphan classification) are the store's
    load-bearing invariants. In this storage layer the INV-2 check is the
    degenerate case — :func:`~setforge.reconcile.store.verify` confirms the
    recorded-local bytes hash matches the index (reconstruction is the
    identity), with the full base-plus-merge form arriving with the 3-way
    merge. The message names the profile, file-id, and which invariant failed.
    """


class DraftConfinementError(InvariantViolation):
    """Raised when a structured share-draft escapes scalar type-confinement (SEC2-8).

    A structured key-unit draft replaces ONE scalar leaf value. The draft must
    parse to a single scalar of the original's type — never a mapping/list
    (sibling/nesting injection), a type change, or a YAML anchor/alias/merge
    construct (``&``/``*``/``<<``). The interactive accept-time validator catches
    this to re-prompt;
    :func:`~setforge.reconcile.structured_units.reconstruct_structured`
    lets it propagate at splice time so a corrupted draft store cannot inject
    structure either (fail-closed). A subclass of :class:`InvariantViolation` so
    reconstruct's "raises InvariantViolation on a bad draft" contract still holds.
    """


class StructuredParseError(ReconcileStoreError):
    """Raised when structured (YAML/JSON5) input cannot be parsed into a model.

    The trigger is malformed, multi-document, or non-UTF-8 structured content:
    ruamel's round-trip loader raises a ``YAMLError`` (a ``ComposerError`` for a
    multi-document stream) and json5 raises its own parse error, while non-UTF-8
    bytes raise ``UnicodeDecodeError`` at decode.
    :func:`~setforge.reconcile.structured_units._load_model` wraps all of these
    into this single typed error so callers never catch a raw parser exception.
    The recovery is to fix
    the source file or fall back to line-level staging (the stage walk skips an
    unparseable structured file onto the line-hunk path).
    """


class UnsafeFileId(ReconcileStoreError):
    """Raised when a profile or file-id fails the store's path-safety check.

    Rejects an absolute path, a ``..`` / ``.`` / empty path component, or a
    C0/DEL control char in either the profile or the file-id, so a
    malicious or buggy key can never resolve a path outside the store
    subtree. The message names the offending value.
    """


class MergeError(ReconcileStoreError):
    """Raised when the 3-way merge engine hits an internal failure.

    The merge primitive (:func:`setforge.reconcile.merge.merge`) is *total* on
    content — binary / oversize / recursion-blown inputs degrade to a whole-file
    conflict, never an error. This wraps the residual case: an unexpected
    ``merge3`` internal failure (other than recursion), re-raised ``from`` the
    original so a raw third-party exception never escapes the engine.
    """


class MergeInvariantError(MergeError):
    """Raised when the merge's fail-closed verify pass detects a dropped edit.

    Before returning, the engine checks that every line a side changed relative to
    base survives into the result (INV-1, one-directional drop check — see
    :func:`setforge.reconcile.merge._verify`); a missing edit line means the
    engine or ``merge3`` lost a user's bytes. Fail-closed: it raises rather than
    return a lossy result. Should never fire in practice; if it does, it is a
    defect, not a content condition.
    """
