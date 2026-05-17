"""Exception hierarchy for setforge.

All recoverable failures inherit from MySetupError so the CLI top-level
handler can render them as ``error: <message>`` and exit 1, while
unexpected exceptions bubble with a traceback.
"""


class MySetupError(Exception):
    """Base class for all setforge recoverable failures."""


class ConfigError(MySetupError):
    """Raised when the YAML config is malformed, fails schema validation,
    or has an invalid profile chain (e.g. a cycle in extends:)."""


class ProfileNotFound(ConfigError):
    """Raised when the user requests a --profile=<name> that does not
    exist in the loaded config."""


class MissingTrackedFile(MySetupError):
    """Raised when a TrackedFile entry's ``src`` path does not exist on disk
    at deploy time."""


class BackupCollision(MySetupError):
    """Reserved for backup-path collisions that cannot be safely overwritten.

    Note: the default copy_atomic policy overwrites a pre-existing dst.bak
    silently; this exception is for future strict-mode use.
    """


class MarkerError(MySetupError):
    """Raised when user-section markers in a tracked file are malformed
    (mismatched start/end, nesting, or unknown directives)."""


class ExtensionToolMissing(MySetupError):
    """Raised when the ``code`` CLI is required for an action but is not
    on PATH."""


class PluginToolMissing(MySetupError):
    """Raised when the ``claude`` CLI is required for an action but is
    not on PATH."""


class MergeTypeMismatch(MySetupError):
    """Raised by yaml_merge.overlay when a preserved key path has
    incompatible leaf types in src vs live (e.g. tracked str vs live
    list)."""


class ExtensionInstallFailed(MySetupError):
    """Raised when ``code --install-extension`` or
    ``--uninstall-extension`` exits non-zero or times out, or when
    ``code --list-extensions`` itself fails. The message includes the
    extension ID (when applicable) and the captured stderr."""


class RevertFailed(MySetupError):
    """Raised by ``setforge revert`` when ``patch -R`` rejects the diff
    (drifted files), when the ``patch`` binary isn't on PATH, or when
    an extension reverse install/uninstall fails. Message includes the
    captured stderr or the conflicting paths."""


class CaptureRequiresInteractive(MySetupError):
    """Raised when ``setforge sync`` would need wizard prompts but no
    TTY is available and ``--auto`` wasn't passed.

    Triggered by :func:`setforge.capture.capture_profile` when the
    capture-time wizard would surface drift (deep-merge sub-key drift or
    top-level non-preserve drift) and the caller cannot prompt. The
    escape hatch is the ``--auto={use-live, keep-tracked}`` CLI flag,
    which routes through :func:`setforge.wizard.run_wizard_loop`'s
    ``auto_accept`` parameter."""


class NoTransitionFound(MySetupError):
    """Raised by ``setforge revert`` when no transition history exists
    for the requested profile."""


class InvalidTransitionRecord(MySetupError):
    """Raised when an on-disk transition record (extensions.json /
    plugins.json) has a corrupt shape.

    Surfaced by :func:`setforge.transitions.plugin_delta_from_json`
    when a ``marketplaces_removed`` entry fails its (name, dict) shape
    check — e.g. hand-edited plugins.json, partial-write damage, or a
    bug in a future writer. Caught at the revert command boundary by
    the existing :class:`MySetupError` handler so the user sees a
    clean error instead of an opaque ``ValueError`` from a tuple
    unpack mid-revert."""


class MarketplaceCacheMiss(MySetupError):
    """Raised when local-clone install mode cannot resolve a marketplace
    to a local cache directory.

    Triggered by :func:`setforge.claude_plugins._clone_marketplace` in
    three cases: the ``git`` binary is missing from PATH, the on-demand
    ``git clone`` failed (typically offline), or an existing cache's
    ``origin`` remote no longer matches the configured source repo and
    a re-clone failed. The message names the marketplace and the exact
    remediation (``setforge plugin sync-cache --profile=<name>`` while
    online, or fall back to ``claude.install_mode: regular``)."""


class BinaryOverrideInvalid(MySetupError):
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
