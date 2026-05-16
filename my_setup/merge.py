"""Interactive merge wizard for unexpected dotfile drift — Pillar 4.

Walks every unexpected drift key across YAML and JSONC dotfiles,
presents a rich-rendered per-drift block, reads a single-keypress
action choice, and applies the selected action atomically with full
snapshot/restore semantics.

Signal handlers for SIGINT (130), SIGTERM (143), SIGHUP (129) restore
all affected files from the snapshot taken at wizard start. Successful
completion records exactly one MERGE transition so ``my-setup revert``
can undo the whole session uniformly.

Generic wizard mechanics (snapshot, prompt, action dispatch, signal
handlers, the per-item run loop) live in :mod:`my_setup.wizard` and are
shared with the capture-time wizard. This module owns only the
install-trigger walker (:func:`walk_unexpected_drift`) and the
install-trigger entry point (:func:`run_wizard`), which is a thin
wrapper over :func:`my_setup.wizard.run_wizard_loop`.

POSIX-only: the underlying single-keypress prompter uses ``tty`` +
``termios``.
"""

from collections.abc import Iterator
from pathlib import Path

from rich.console import Console

# ruamel.yaml ships py.typed without resolvable annotations; no stub pkg on PyPI.
from ruamel.yaml import YAML  # type: ignore[import-not-found]

from my_setup import jsonc, transitions, wizard
from my_setup.compare import CompareReport, CompareStatus
from my_setup.config import Config
from my_setup.wizard import ActionResult, DriftItem, DriftMode, FileFormat

# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


def walk_unexpected_drift(
    report: CompareReport,
    config: Config,
    repo_root: Path,
    dotfile_filter: str | None = None,
) -> Iterator[DriftItem]:
    """Yield one :class:`DriftItem` per unexpected drift key in ``report``.

    Iterates over every ``DRIFTED`` entry that has unexpected keys.
    When ``dotfile_filter`` is set, entries whose dotfile name does not match
    are skipped. Values are resolved from the live/tracked files at yield time.

    The ``mode`` field on each yielded item reflects whether the key sits
    in ``preserve_user_keys_deep`` (``"deep"``) or otherwise (``"shallow"``);
    ``_action_use_live`` routes through the matching overlay variant.
    """
    from my_setup.compare import resolve_dst, resolve_src

    for entry in report.entries:
        if entry.status != CompareStatus.DRIFTED:
            continue
        if not entry.unexpected_drift_keys:
            continue

        # entry.name may be "x" or "x/relpath" for directory dotfiles
        dotfile_base = entry.name.split("/")[0]
        if dotfile_filter is not None and dotfile_base != dotfile_filter:
            continue

        if dotfile_base not in config.dotfiles:
            continue

        dotfile = config.dotfiles[dotfile_base]
        src = resolve_src(dotfile, repo_root)
        dst = resolve_dst(dotfile)

        # Handle sub-file names for directory dotfiles
        if "/" in entry.name:
            rel = entry.name.split("/", 1)[1]
            src = src / rel
            dst = dst / rel

        if jsonc.is_jsonc_file(src):
            fmt: FileFormat = FileFormat.JSONC
            tracked_parsed = jsonc.parse_jsonc(src.read_text(encoding="utf-8"))
            live_parsed = jsonc.parse_jsonc(dst.read_text(encoding="utf-8"))
        else:
            fmt = FileFormat.YAML
            y = YAML(typ="rt")
            tracked_parsed = y.load(src.read_text(encoding="utf-8"))
            live_parsed = y.load(dst.read_text(encoding="utf-8"))

        deep_paths = set(dotfile.preserve_user_keys_deep)
        for key_path in entry.unexpected_drift_keys:
            tracked_val = _get_value(tracked_parsed, key_path, fmt)
            live_val = _get_value(live_parsed, key_path, fmt)
            mode = DriftMode.DEEP if key_path in deep_paths else DriftMode.SHALLOW
            yield DriftItem(
                dotfile_name=dotfile_base,
                src_path=src,
                dst_path=dst,
                key_path=key_path,
                tracked_value=tracked_val,
                live_value=live_val,
                file_format=fmt,
                mode=mode,
            )


def _get_value(doc: object, key_path: str, fmt: FileFormat) -> object:
    """Extract a value from a parsed document at ``key_path``.

    JSONC: key_path is a literal top-level key name (flat, no nesting).
    YAML: key_path is a dotted path (e.g. ``"b.c"``).
    """
    if fmt is FileFormat.JSONC:
        if isinstance(doc, dict):
            return doc.get(key_path)
        return None

    # YAML: walk dotted path
    node = doc
    for part in key_path.split("."):
        # strip any list suffix like [0] or [*]
        bare = part.rstrip("]").split("[")[0]
        if isinstance(node, dict) and bare in node:
            node = node[bare]
        else:
            return None
    return node


# ---------------------------------------------------------------------------
# Wizard runner (install-trigger entry point)
# ---------------------------------------------------------------------------


def run_wizard(
    report: CompareReport,
    config: Config,
    repo_root: Path,
    *,
    my_setup_yaml_path: Path,
    snapshot_base: Path | None = None,
    profile: str = "unknown",
    dotfile_filter: str | None = None,
    console: Console | None = None,
    auto_accept: str | None = None,
) -> list[tuple[DriftItem, ActionResult]]:
    """Run the install-time merge wizard over all unexpected drift in ``report``.

    Thin wrapper over :func:`my_setup.wizard.run_wizard_loop` that supplies
    the install-trigger walker, transition command, and pending-edit message.

    Parameters
    ----------
    report:
        Compare report to walk.
    config:
        Loaded my-setup config.
    repo_root:
        Repo root used for ``resolve_src``.
    my_setup_yaml_path:
        Path to ``my_setup.yaml`` — needed by the ``[s]`` action.
    snapshot_base:
        Parent directory for the timestamped snapshot dir. Defaults to
        ``~/.local/state/my-setup/merge-snapshots``.
    profile:
        Profile name (used in the merge-transition meta).
    dotfile_filter:
        If set, only walk drift for the named dotfile.
    console:
        Rich Console to use (defaults to a new ``Console()``).
    auto_accept:
        ``"k"`` or ``"u"`` for non-interactive runs (install gating).

    Returns
    -------
    list of (DriftItem, ActionResult) pairs — one per drift item walked.

    Raises
    ------
    KeyboardInterrupt
        When the user presses Ctrl-C and auto_accept is None (interactive
        mode). Callers are expected to restore the snapshot and exit with
        code 130.
    """
    if snapshot_base is None:
        snapshot_base = (
            Path.home() / ".local" / "state" / "my-setup" / "merge-snapshots"
        )
    if console is None:
        console = Console()

    items = walk_unexpected_drift(
        report, config, repo_root, dotfile_filter=dotfile_filter
    )
    pending_message = (
        f"[yellow]pending manual edit in {{src_path}}; "
        f"resume with: my-setup merge --profile={profile}[/yellow]"
    )
    return wizard.run_wizard_loop(
        items,
        my_setup_yaml_path=my_setup_yaml_path,
        snapshot_base=snapshot_base,
        console=console,
        auto_accept=auto_accept,
        transition_command=transitions.TransitionCommand.MERGE,
        profile=profile,
        pending_message=pending_message,
    )
