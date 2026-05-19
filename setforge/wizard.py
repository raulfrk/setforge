"""Generic wizard utilities shared by install-time merge and capture-time deep-merge.

These primitives originally lived in :mod:`setforge.merge`. They were
factored out so that the install-time wizard (`setforge.merge.run_wizard`)
and the capture-time wizard (`nen.23`'s upcoming
`setforge.capture.run_capture_wizard`) can share a single implementation.

What lives here:

- :class:`ActionResult` — closed set of per-item outcomes.
- :class:`DriftItem` — one diverged key path between tracked and live;
  produced by trigger-specific walkers, consumed by the wizard.
- :class:`Snapshot` — context manager that snapshots affected files for
  cancel-atomic semantics.
- :func:`read_one_choice` — POSIX single-keypress prompter; falls back to
  line-buffered read on non-tty stdin. Shared by :func:`prompt_one` and
  the section-reconcile wizard.
- :func:`prompt_one` — render the per-drift block and read one keypress.
- :func:`apply_action` — dispatch the chosen action (k/u/s/m).
- :func:`run_wizard_loop` — the parameterized orchestrator that snapshots,
  installs signal handlers, walks ``items``, calls
  :func:`prompt_one` + :func:`apply_action` per item, records a transition
  on success, and restores the snapshot on failure.

POSIX-only: the single-keypress prompter uses ``tty`` + ``termios`` and
is intentionally not ported to Windows (Debian VM, headless).
"""

import io
import os
import shutil
import signal
import sys
import termios
import time
import tty
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Self

from rich.console import Console
from rich.table import Table

# ruamel.yaml ships py.typed without resolvable annotations; no stub pkg on PyPI.
from ruamel.yaml import YAML  # type: ignore[import-not-found]

from setforge import jsonc, transitions, yaml_merge
from setforge._editor import run_editor
from setforge._redact import redact_argv
from setforge.transitions import TransitionCommand

# Matches signal.signal's first-arg signature; aliased here so the
# wizard's signal-handler save/restore typing is precise (mypy rejects
# the wider `object` we previously used).
_SignalHandler = (
    Callable[[int, FrameType | None], object] | int | signal.Handlers | None
)

__all__ = [
    "ActionResult",
    "DriftItem",
    "DriftMode",
    "FileFormat",
    "Snapshot",
    "apply_action",
    "prompt_one",
    "read_one_choice",
    "run_wizard_loop",
]


class ActionResult(StrEnum):
    """Closed set of outcomes from :func:`apply_action`."""

    KEEP_TRACKED = "keep_tracked"
    USE_LIVE = "use_live"
    SAVE_AS_PRESERVED = "save_as_preserved"
    MANUAL_EDIT_DONE = "manual_edit_done"
    MANUAL_PENDING = "manual_pending"


class FileFormat(StrEnum):
    """Closed set of file formats handled by the merge wizard's overlay seam."""

    YAML = "yaml"
    JSONC = "jsonc"


class DriftMode(StrEnum):
    """Closed set of preserve-layer modes for a :class:`DriftItem`.

    ``SHALLOW`` — key lives in ``preserve_user_keys`` (whole-leaf overlay).
    ``DEEP`` — key lives in ``preserve_user_keys_deep`` (recursive deep-merge).
    """

    SHALLOW = "shallow"
    DEEP = "deep"


@dataclass(frozen=True, slots=True)
class DriftItem:
    """One diverged key path between tracked and live for a drift item.

    Produced by trigger-specific walkers (:func:`setforge.merge.walk_unexpected_drift`
    for install, :func:`walk_capture_drift` for capture in `nen.23`); consumed by
    the merge wizard.
    """

    tracked_file_name: str
    """The ``setforge.yaml`` ``tracked_files.<key>`` identifier."""

    src_path: Path
    """Tracked path (under tracked/)."""

    dst_path: Path
    """Live path (resolved from tracked_file.dst)."""

    key_path: str
    """The JSONPath-lite or literal-key path that diverged."""

    tracked_value: object
    """Value at key_path in src."""

    live_value: object
    """Value at key_path in dst."""

    file_format: FileFormat
    """Routes [u]se-live action to the correct write primitive."""

    mode: DriftMode = DriftMode.SHALLOW
    """Whether the key sits in ``preserve_user_keys`` (shallow whole-leaf
    overlay) or ``preserve_user_keys_deep`` (recursive deep-merge).
    Routes the [u]se-live action to the matching overlay variant.
    Defaults to ``"shallow"`` for back-compat with callers that
    construct :class:`DriftItem` without the field; trigger-specific
    walkers populate it from the tracked_file's two preserve lists."""


@dataclass(slots=True)
class Snapshot:
    """Context manager that snapshots files at wizard start for cancel atomicity.

    Usage::

        snap = Snapshot(files=[path1, path2], snapshot_base=some_dir)
        with snap:
            # do work
            snap.discard()          # success — delete snapshot
            # or
            n = snap.restore()      # cancel — revert all files

    ``snapshot_dir`` is set by ``__enter__`` to a timestamped subdirectory
    under ``snapshot_base``. On cancel, :meth:`restore` copies the snapshots
    back; on success, :meth:`discard` removes the dir.

    ``__exit__`` deliberately does NOT auto-restore so the caller can decide
    the outcome (success vs failure).
    """

    files: list[Path]
    snapshot_base: Path
    snapshot_dir: Path = field(default_factory=lambda: Path("."))

    def __enter__(self) -> Self:
        """Create snapshot dir and copy each file into it."""
        ts = time.strftime("%Y%m%dT%H%M%S")
        self.snapshot_dir = self.snapshot_base / ts
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        for original in self.files:
            if original.exists():
                copy_dst = self._snap_path(original)
                copy_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, copy_dst)
        return self

    def restore(self) -> int:
        """Restore all snapshotted files back to their original paths.

        Returns the number of files successfully restored.
        """
        n = 0
        for original in self.files:
            snap_copy = self._snap_path(original)
            if snap_copy.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(snap_copy, original)
                n += 1
        return n

    def discard(self) -> None:
        """Remove the snapshot directory (call on successful completion)."""
        shutil.rmtree(self.snapshot_dir, ignore_errors=True)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Do NOT auto-restore; caller owns success/failure decision."""
        return None

    def _snap_path(self, original: Path) -> Path:
        """Return the snapshot path for ``original``.

        Uses a flattened encoding: sha256 first-8-chars prefix + basename to
        avoid collisions between files with the same name in different dirs.
        """
        import hashlib

        hex_prefix = hashlib.sha256(str(original).encode()).hexdigest()[:8]
        return self.snapshot_dir / f"{hex_prefix}_{original.name}"


# ---------------------------------------------------------------------------
# Single-keypress prompter (POSIX raw mode)
# ---------------------------------------------------------------------------


def read_one_choice(prompt: str, choices: set[str]) -> str:
    """Read one keypress from stdin in raw mode. POSIX-only.

    Echoes the chosen key and prints a newline, then returns the lowercase
    character. Re-prompts (with a terminal bell ``\\a``) on invalid keys.
    Ctrl-C (``\\x03``) raises :class:`KeyboardInterrupt`.

    Falls back to a simple line-buffered read when stdin has no ``fileno``
    (e.g. during testing with a ``StringIO`` substitute).
    """
    print(prompt, end="", flush=True)
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except (io.UnsupportedOperation, termios.error):
        # Non-tty stdin (tests, piped input, pipe-to-docker-exec) —
        # line-buffered fallback, no raw mode
        while True:
            ch = sys.stdin.read(1)
            if not ch:
                raise EOFError("stdin closed") from None
            if ch == "\x03":
                raise KeyboardInterrupt from None
            ch_l = ch.lower()
            if ch_l in choices:
                print(ch_l)
                return ch_l
            sys.stdout.write("\a")
            sys.stdout.flush()

    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch == "\x03":  # Ctrl-C
                raise KeyboardInterrupt
            ch_l = ch.lower()
            if ch_l in choices:
                # Restore briefly so print goes through cooked stdout
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                print(ch_l)
                return ch_l
            sys.stdout.write("\a")
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ---------------------------------------------------------------------------
# Rich-rendered per-drift header
# ---------------------------------------------------------------------------


def prompt_one(item: DriftItem, console: Console) -> str:
    """Render the per-drift block for ``item`` and return the chosen action key.

    Uses rich for the header and value display; reads action via
    :func:`read_one_choice` (single keypress, no Enter required).
    """
    sep = "─" * 57
    console.print(f"\n[dim]{sep}[/dim]")
    console.print(f" [bold]{item.src_path}[/bold] :: [cyan]{item.key_path}[/cyan]")
    console.print(f"[dim]{sep}[/dim]")

    # Two-row aligned block
    t = Table.grid(padding=(0, 1))
    t.add_column(justify="right", style="dim")
    t.add_column()
    t.add_row("tracked", f"[yellow]{item.tracked_value!r}[/yellow]")
    t.add_row("live", f"[green]{item.live_value!r}[/green]")
    console.print(t)

    console.print("")
    console.print(
        "   [bold][[k]][/bold] keep tracked       "
        "[dim](live overwritten on next deploy)[/dim]"
    )
    console.print(
        "   [bold][[u]][/bold] use live           "
        "[dim](write live value into tracked now)[/dim]"
    )
    console.print(
        "   [bold][[s]][/bold] save-as-preserved  "
        "[dim](extend preserve_user_keys; live stays)[/dim]"
    )
    console.print("   [bold][[m]][/bold] manual edit")
    console.print("")

    return read_one_choice("   Choice (k/u/s/m): ", {"k", "u", "s", "m"})


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------


def apply_action(
    item: DriftItem,
    choice: str,
    *,
    setforge_yaml_path: Path,
) -> ActionResult:
    """Apply the chosen action for ``item`` and return the result.

    Actions:

    - ``k`` — no-op (caller handles re-deploy).
    - ``u`` — write live value into tracked (YAML or JSONC round-trip).
    - ``s`` — append ``item.key_path`` to ``preserve_user_keys`` in
      ``setforge_yaml_path``.
    - ``m`` — sub-prompt y/n; y launches ``$EDITOR``; n returns pending.
    """
    if choice == "k":
        return ActionResult.KEEP_TRACKED

    if choice == "u":
        return _action_use_live(item)

    if choice == "s":
        return _action_save_as_preserved(item, setforge_yaml_path)

    if choice == "m":
        return _action_manual_edit(item)

    raise ValueError(f"unknown choice: {choice!r}")


def _action_use_live(item: DriftItem) -> ActionResult:
    """Write the live key value into the tracked file.

    Per the `nen.23` locked design (spec table line 51), ``mode`` on a
    :class:`DriftItem` is informational only — both ``"shallow"`` and
    ``"deep"`` walker output reach this action as per-leaf items, and
    a shallow whole-leaf overlay write covers both. The deep-overlay
    primitives (``deep_key_paths`` / ``deep_key_names``) require the
    terminal value to be a dict on both sides; capture's walker yields
    leaves under deep-merge top-level paths, so shallow overlay is
    the right primitive at this seam.
    """
    if item.file_format is FileFormat.JSONC:
        tracked_text = item.src_path.read_text(encoding="utf-8")
        live_text = item.dst_path.read_text(encoding="utf-8")
        result_text = jsonc.overlay_user_keys(tracked_text, live_text, [item.key_path])
        item.src_path.write_text(result_text, encoding="utf-8")
    else:
        y = YAML(typ="rt")
        with item.src_path.open("r", encoding="utf-8") as fh:
            src_doc = y.load(fh)
        with item.dst_path.open("r", encoding="utf-8") as fh:
            live_doc = y.load(fh)
        merged = yaml_merge.overlay(src_doc, live_doc, [item.key_path])
        buf = io.StringIO()
        y.dump(merged, buf)
        item.src_path.write_text(buf.getvalue(), encoding="utf-8")
    return ActionResult.USE_LIVE


def _action_save_as_preserved(
    item: DriftItem, setforge_yaml_path: Path
) -> ActionResult:
    """Append ``item.key_path`` to the tracked_file's ``preserve_user_keys``."""
    y = YAML(typ="rt")
    with setforge_yaml_path.open("r", encoding="utf-8") as fh:
        doc = y.load(fh)

    # Navigate: tracked_files -> <name> -> preserve_user_keys
    tracked_files_node = doc.get("tracked_files") if isinstance(doc, dict) else None
    if tracked_files_node is None:
        return ActionResult.SAVE_AS_PRESERVED

    tracked_file_node = tracked_files_node.get(item.tracked_file_name)
    if tracked_file_node is None:
        return ActionResult.SAVE_AS_PRESERVED

    puk = tracked_file_node.get("preserve_user_keys")
    if puk is None:
        tracked_file_node["preserve_user_keys"] = [item.key_path]
    else:
        if item.key_path not in puk:
            puk.append(item.key_path)

    buf = io.StringIO()
    y.dump(doc, buf)
    setforge_yaml_path.write_text(buf.getvalue(), encoding="utf-8")
    return ActionResult.SAVE_AS_PRESERVED


def _action_manual_edit(item: DriftItem) -> ActionResult:
    """Sub-prompt y/n; y opens ``$EDITOR`` on the tracked file; n returns pending."""
    file_display = item.src_path
    yn = read_one_choice(f"   Open $EDITOR on {file_display} now? (y/n): ", {"y", "n"})
    if yn == "y":
        run_editor(item.src_path)
        return ActionResult.MANUAL_EDIT_DONE
    return ActionResult.MANUAL_PENDING


# ---------------------------------------------------------------------------
# Signal handler factory
# ---------------------------------------------------------------------------


def _make_signal_handler(
    snapshot: Snapshot, sig_name: str
) -> Callable[[int, FrameType | None], None]:
    """Return a signal handler that restores ``snapshot`` then re-raises the signal."""

    def _handler(signum: int, frame: FrameType | None) -> None:
        n = snapshot.restore()
        print(
            f"\nmerge cancelled ({sig_name}); reverted {n} file(s)",
            file=sys.stderr,
        )
        # Reset to default handler and re-raise so the process exits with
        # the expected signal-derived exit code.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    return _handler


def _install_signal_handlers(
    snapshot: Snapshot,
) -> dict[int, _SignalHandler]:
    """Install SIGINT / SIGTERM / SIGHUP handlers for the wizard's lifetime.

    Returns the previous handlers so the caller can restore them when the
    wizard exits normally.
    """
    prev: dict[int, _SignalHandler] = {}
    for sig, name in [
        (signal.SIGINT, "SIGINT"),
        (signal.SIGTERM, "SIGTERM"),
        (signal.SIGHUP, "SIGHUP"),
    ]:
        prev[sig] = signal.signal(sig, _make_signal_handler(snapshot, name))
    return prev


def _restore_signal_handlers(prev: dict[int, _SignalHandler]) -> None:
    """Restore previously-saved signal handlers."""
    for sig, handler in prev.items():
        signal.signal(sig, handler)


# ---------------------------------------------------------------------------
# Generic wizard orchestrator
# ---------------------------------------------------------------------------


def run_wizard_loop(
    items: Iterator[DriftItem],
    *,
    setforge_yaml_path: Path,
    snapshot_base: Path,
    console: Console,
    auto_accept: str | None,
    transition_command: TransitionCommand,
    profile: str,
    pending_message: str,
) -> list[tuple[DriftItem, ActionResult]]:
    """Run the generic merge-wizard loop over ``items``.

    Trigger-agnostic orchestrator shared by install-time and capture-time
    wizards. Snapshots all affected files, installs SIGINT / SIGTERM /
    SIGHUP handlers (when interactive), prompts per item via
    :func:`prompt_one`, dispatches via :func:`apply_action`, records one
    transition on success, and restores the snapshot on failure.

    Parameters
    ----------
    items:
        Iterator of :class:`DriftItem` produced by a trigger-specific
        walker. Materialized once internally to build the snapshot file
        list, so callers may pass a generator.
    setforge_yaml_path:
        Path to ``setforge.yaml`` — needed by the ``[s]`` action.
    snapshot_base:
        Parent directory for the timestamped snapshot dir.
    console:
        Rich Console for output.
    auto_accept:
        ``"k"`` or ``"u"`` for non-interactive runs (install gating).
        ``None`` enables interactive prompts and signal handlers.
    transition_command:
        Which :class:`TransitionCommand` variant to record on success
        (e.g. ``MERGE`` for install, ``CAPTURE``-flavored for `nen.23`).
    profile:
        Profile name (used in the transition meta).
    pending_message:
        Trigger-specific message rendered when an item resolves to
        ``MANUAL_PENDING`` and halts the loop. Format-string template
        with a ``{src_path}`` placeholder that is interpolated to the
        offending file path. Example::

            "[yellow]pending manual edit in {src_path}; "
            "resume with: setforge merge --profile=p[/yellow]"

    Returns
    -------
    list of (DriftItem, ActionResult) pairs — one per drift item walked,
    in walk order. The list ends at the first ``MANUAL_PENDING``.

    Raises
    ------
    KeyboardInterrupt
        When the user presses Ctrl-C and ``auto_accept`` is ``None``.
        Callers are expected to restore the snapshot and exit with code
        130. The internal snapshot is preserved (not restored or
        discarded) so signal handlers — installed for the wizard's
        lifetime — can do the restore.
    """
    items_list = list(items)

    # Collect all affected paths for the snapshot
    affected_paths: list[Path] = [setforge_yaml_path]
    for item in items_list:
        if item.src_path not in affected_paths:
            affected_paths.append(item.src_path)
        if item.dst_path not in affected_paths:
            affected_paths.append(item.dst_path)

    snap = Snapshot(files=affected_paths, snapshot_base=snapshot_base)
    decisions: list[tuple[DriftItem, ActionResult]] = []

    with snap:
        prev_handlers = _install_signal_handlers(snap) if auto_accept is None else {}

        try:
            file_pre = transitions.snapshot_paths(affected_paths)

            for item in items_list:
                if auto_accept is not None:
                    choice = auto_accept
                else:
                    choice = prompt_one(item, console)

                result = apply_action(
                    item, choice, setforge_yaml_path=setforge_yaml_path
                )
                decisions.append((item, result))

                if result == ActionResult.MANUAL_PENDING:
                    # User declined the editor — stop the walk but record what
                    # was applied
                    console.print(pending_message.format(src_path=item.src_path))
                    break

            file_post = transitions.snapshot_paths(affected_paths)

            # Record one transition for revert symmetry. The wizard
            # fires specifically for preserve_user_keys deep-merge
            # drift, so preserve_user_keys_applied=True is the accurate
            # signal here — the overlay path did run.
            meta = transitions.make_meta(
                transition_command,
                profile,
                end_timestamp=transitions.now_utc().astimezone(UTC).isoformat(),
                command_line=redact_argv(sys.argv[1:]),
                preserve_user_keys_applied=True,
            )
            transitions.write_transition(meta, file_pre, file_post, None)

            snap.discard()

        except KeyboardInterrupt:
            # Re-raise — CLI layer handles restore + exit code
            raise

        except Exception:
            snap.restore()
            raise

        finally:
            if prev_handlers:
                _restore_signal_handlers(prev_handlers)

    return decisions
