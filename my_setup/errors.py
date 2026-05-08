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


class CaptureRequiresInteractive(MySetupError):
    """Raised when ``my-setup sync`` would need wizard prompts but no
    TTY is available and ``--auto`` wasn't passed.

    Triggered by :func:`my_setup.capture.capture_profile` when the
    capture-time wizard would surface drift (deep-merge sub-key drift or
    top-level non-preserve drift) and the caller cannot prompt. The
    escape hatch is the ``--auto={use-live, keep-tracked}`` CLI flag,
    which routes through :func:`my_setup.wizard.run_wizard_loop`'s
    ``auto_accept`` parameter."""


class NoTransitionFound(MySetupError):
    """Raised by ``my-setup revert`` when no transition history exists
    for the requested profile."""


class BinaryOverrideInvalid(MySetupError):
    """Raised when a host-local binary override (CLI flag, env var, or
    ``~/.config/my-setup/local.yaml``) points at a path that does not
    exist or is not executable. Carries the layer, binary name, path,
    and reason as structured fields so callers can render or test
    against them precisely."""

    def __init__(self, *, layer: str, binary: str, path: str, reason: str):
        self.layer = layer
        self.binary = binary
        self.path = path
        self.reason = reason
        super().__init__(
            f"{layer} override for {binary!r} → {path!r}: {reason}. "
            f"Edit ~/.config/my-setup/local.yaml or unset the override."
        )
