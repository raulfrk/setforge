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


class OverlayBodyUnlocatable(SetforgeError):
    """Raised when a deployed host-local overlay body cannot be proven excised.

    The capture-side leak gate (:func:`setforge.capture._capture_overlay_bodies`)
    must guarantee the tracked write is body-free. A span whose sidecar records
    a ``last_deployed_body`` (a body WAS deployed at this anchor) but whose body
    is now neither exactly present in the live file NOR fuzzy-locatable near its
    anchor cannot be safely excised — capturing would leak the hand-edited
    host-local body into the shared tracked repo. Rather than fail open, the
    gate REFUSES before any tracked write.

    The message names the file + anchor and the recovery: re-run
    ``setforge install`` to re-impose the canonical body (so the exact needle
    matches again), or lift the live edit into ``local.yaml`` by hand first.
    """

    def __init__(self, *, sub_name: str, anchor: str) -> None:
        self.sub_name = sub_name
        self.anchor = anchor
        super().__init__(
            f"host-local overlay body at {anchor!r} in {sub_name!r} was deployed "
            "but is now neither present verbatim nor locatable near its anchor; "
            "refusing to capture (would leak the host-local body into tracked). "
            "Re-run `setforge install` to re-impose the canonical body, or lift "
            "the live edit into local.yaml by hand first."
        )


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


class InvalidLocalConfigShape(SetforgeError):
    """Raised when hand-edited ``local.yaml`` has a wrong shape at an
    overlay-body writeback target.

    Surfaced by
    :func:`setforge.overlay_body_wizard.write_edited_body_to_local` when the
    keep-path tries to locate ``tracked_files.<id>.spans[*].overlay.body`` and
    finds the ``tracked_file`` entry, the matching span, or its ``overlay``
    node missing or a scalar rather than a mapping. ``local.yaml`` is
    hand-editable, so this is a trust-boundary shape failure — same precedent
    as :class:`InvalidTransitionRecord` for hand-edited JSON. Subclass of
    :class:`SetforgeError` so the global handler renders it as
    ``error: <message>`` naming the file + the missing/scalar key instead of
    an opaque ``KeyError`` traceback."""


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
