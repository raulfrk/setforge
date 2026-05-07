"""Exception hierarchy for my-setup.

All recoverable failures inherit from MySetupError so the CLI top-level
handler can render them as ``error: <message>`` and exit 1, while
unexpected exceptions bubble with a traceback.
"""


class MySetupError(Exception):
    """Base class for all my-setup recoverable failures."""


class ConfigError(MySetupError):
    """Raised when the YAML config is malformed, fails schema validation,
    or has an invalid profile chain (e.g. a cycle in extends:)."""


class ProfileNotFound(ConfigError):
    """Raised when the user requests a --profile=<name> that does not
    exist in the loaded config."""


class MissingTrackedFile(MySetupError):
    """Raised when a Dotfile entry's ``src`` path does not exist on disk
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
    """Raised by ``my-setup revert`` when ``patch -R`` rejects the diff
    (drifted files), when the ``patch`` binary isn't on PATH, or when
    an extension reverse install/uninstall fails. Message includes the
    captured stderr or the conflicting paths."""


class NoTransitionFound(MySetupError):
    """Raised by ``my-setup revert`` when no transition history exists
    for the requested profile."""
