"""Interactive merge wizard for unexpected dotfile drift — Pillar 4.

Walks every unexpected drift key across YAML and JSONC dotfiles,
presents a rich-rendered per-drift block, reads a single-keypress
action choice, and applies the selected action atomically with full
snapshot/restore semantics.

Signal handlers for SIGINT (130), SIGTERM (143), SIGHUP (129) restore
all affected files from the snapshot taken at wizard start. Successful
completion records exactly one MERGE transition so ``my-setup revert``
can undo the whole session uniformly.

POSIX-only: the single-keypress prompter uses ``tty`` + ``termios`` and
is intentionally not ported to Windows (Debian VM, headless).
"""

import io
import os
import shutil
import signal
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal

from rich.console import Console
from rich.table import Table
from ruamel.yaml import YAML

from my_setup import jsonc, transitions, yaml_merge
from my_setup.compare import CompareReport, CompareStatus
from my_setup.config import Config


class ActionResult(StrEnum):
    """Closed set of outcomes from :func:`apply_action`."""

    KEEP_TRACKED = "keep_tracked"
    USE_LIVE = "use_live"
    SAVE_AS_PRESERVED = "save_as_preserved"
    MANUAL_EDIT_DONE = "manual_edit_done"
    MANUAL_PENDING = "manual_pending"


@dataclass(frozen=True, slots=True)
class UnexpectedKey:
    """One diverged key path between tracked and live for an unexpected drift item.

    Produced by :func:`walk_unexpected_drift`; consumed by the merge wizard.
    """

    dotfile_name: str
    """The ``my_setup.yaml`` ``dotfiles.<key>`` identifier."""

    src_path: Path
    """Tracked path (under tracked/)."""

    dst_path: Path
    """Live path (resolved from dotfile.dst)."""

    key_path: str
    """The JSONPath-lite or literal-key path that diverged."""

    tracked_value: object
    """Value at key_path in src."""

    live_value: object
    """Value at key_path in dst."""

    file_format: Literal["yaml", "jsonc"]
    """Routes [u]se-live action to the correct write primitive."""


@dataclass
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

    def __enter__(self) -> "Snapshot":
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

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        """Do NOT auto-restore; caller owns success/failure decision."""
        return False

    def _snap_path(self, original: Path) -> Path:
        """Return the snapshot path for ``original``.

        Uses a flattened encoding: sha256 first-8-chars prefix + basename to
        avoid collisions between files with the same name in different dirs.
        """
        import hashlib
        hex_prefix = hashlib.sha256(str(original).encode()).hexdigest()[:8]
        return self.snapshot_dir / f"{hex_prefix}_{original.name}"


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


def walk_unexpected_drift(
    report: CompareReport,
    config: Config,
    repo_root: Path,
    dotfile_filter: str | None = None,
) -> Iterator[UnexpectedKey]:
    """Yield one :class:`UnexpectedKey` per unexpected drift key in ``report``.

    Iterates over every ``DRIFTED`` entry that has unexpected keys.
    When ``dotfile_filter`` is set, entries whose dotfile name does not match
    are skipped. Values are resolved from the live/tracked files at yield time.
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
            fmt: Literal["yaml", "jsonc"] = "jsonc"
            tracked_parsed = jsonc.parse_jsonc(src.read_text(encoding="utf-8"))
            live_parsed = jsonc.parse_jsonc(dst.read_text(encoding="utf-8"))
        else:
            fmt = "yaml"
            y = YAML(typ="rt")
            tracked_parsed = y.load(src.read_text(encoding="utf-8"))
            live_parsed = y.load(dst.read_text(encoding="utf-8"))

        for key_path in entry.unexpected_drift_keys:
            tracked_val = _get_value(tracked_parsed, key_path, fmt)
            live_val = _get_value(live_parsed, key_path, fmt)
            yield UnexpectedKey(
                dotfile_name=dotfile_base,
                src_path=src,
                dst_path=dst,
                key_path=key_path,
                tracked_value=tracked_val,
                live_value=live_val,
                file_format=fmt,
            )


def _get_value(doc: object, key_path: str, fmt: Literal["yaml", "jsonc"]) -> object:
    """Extract a value from a parsed document at ``key_path``.

    JSONC: key_path is a literal top-level key name (flat, no nesting).
    YAML: key_path is a dotted path (e.g. ``"b.c"``).
    """
    if fmt == "jsonc":
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
# Single-keypress prompter (POSIX raw mode)
# ---------------------------------------------------------------------------


def _read_one_choice(prompt: str, choices: set[str]) -> str:
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
    except io.UnsupportedOperation:
        # Non-tty stdin (tests, piped input) — read one char without raw mode
        while True:
            ch = sys.stdin.read(1)
            if not ch:
                raise EOFError("stdin closed")
            if ch == "\x03":
                raise KeyboardInterrupt
            ch_l = ch.lower()
            if ch_l in choices:
                print(ch_l)
                return ch_l
            sys.stdout.write("\a")
            sys.stdout.flush()

    old = termios.tcgetattr(fd)
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


def prompt_one(uk: UnexpectedKey, console: Console) -> str:
    """Render the per-drift block for ``uk`` and return the chosen action key.

    Uses rich for the header and value display; reads action via
    :func:`_read_one_choice` (single keypress, no Enter required).
    """
    sep = "─" * 57
    console.print(f"\n[dim]{sep}[/dim]")
    console.print(f" [bold]{uk.src_path}[/bold] :: [cyan]{uk.key_path}[/cyan]")
    console.print(f"[dim]{sep}[/dim]")

    # Two-row aligned block
    t = Table.grid(padding=(0, 1))
    t.add_column(justify="right", style="dim")
    t.add_column()
    t.add_row("tracked", f"[yellow]{uk.tracked_value!r}[/yellow]")
    t.add_row("live", f"[green]{uk.live_value!r}[/green]")
    console.print(t)

    console.print("")
    console.print("   [bold][[k]][/bold] keep tracked       [dim](live overwritten on next deploy)[/dim]")
    console.print("   [bold][[u]][/bold] use live           [dim](write live value into tracked now)[/dim]")
    console.print("   [bold][[s]][/bold] save-as-preserved  [dim](extend preserve_user_keys; live stays)[/dim]")
    console.print("   [bold][[m]][/bold] manual edit")
    console.print("")

    return _read_one_choice("   Choice (k/u/s/m): ", {"k", "u", "s", "m"})


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------


def apply_action(
    uk: UnexpectedKey,
    choice: str,
    *,
    my_setup_yaml_path: Path,
) -> ActionResult:
    """Apply the chosen action for ``uk`` and return the result.

    Actions:

    - ``k`` — no-op (caller handles re-deploy).
    - ``u`` — write live value into tracked (YAML or JSONC round-trip).
    - ``s`` — append ``uk.key_path`` to ``preserve_user_keys`` in ``my_setup_yaml_path``.
    - ``m`` — sub-prompt y/n; y launches ``$EDITOR``; n returns pending.
    """
    if choice == "k":
        return ActionResult.KEEP_TRACKED

    if choice == "u":
        return _action_use_live(uk)

    if choice == "s":
        return _action_save_as_preserved(uk, my_setup_yaml_path)

    if choice == "m":
        return _action_manual_edit(uk)

    raise ValueError(f"unknown choice: {choice!r}")


def _action_use_live(uk: UnexpectedKey) -> ActionResult:
    """Write the live key value into the tracked file."""
    if uk.file_format == "jsonc":
        tracked_text = uk.src_path.read_text(encoding="utf-8")
        live_text = uk.dst_path.read_text(encoding="utf-8")
        result_text = jsonc.overlay_user_keys(tracked_text, live_text, [uk.key_path])
        uk.src_path.write_text(result_text, encoding="utf-8")
    else:
        y = YAML(typ="rt")
        with uk.src_path.open("r", encoding="utf-8") as fh:
            src_doc = y.load(fh)
        with uk.dst_path.open("r", encoding="utf-8") as fh:
            live_doc = y.load(fh)
        merged = yaml_merge.overlay(src_doc, live_doc, [uk.key_path])
        buf = io.StringIO()
        y.dump(merged, buf)
        uk.src_path.write_text(buf.getvalue(), encoding="utf-8")
    return ActionResult.USE_LIVE


def _action_save_as_preserved(uk: UnexpectedKey, my_setup_yaml_path: Path) -> ActionResult:
    """Append ``uk.key_path`` to the dotfile's ``preserve_user_keys`` in my_setup.yaml."""
    y = YAML(typ="rt")
    with my_setup_yaml_path.open("r", encoding="utf-8") as fh:
        doc = y.load(fh)

    # Navigate: dotfiles -> <name> -> preserve_user_keys
    dotfiles_node = doc.get("dotfiles") if isinstance(doc, dict) else None
    if dotfiles_node is None:
        return ActionResult.SAVE_AS_PRESERVED

    dotfile_node = dotfiles_node.get(uk.dotfile_name)
    if dotfile_node is None:
        return ActionResult.SAVE_AS_PRESERVED

    puk = dotfile_node.get("preserve_user_keys")
    if puk is None:
        dotfile_node["preserve_user_keys"] = [uk.key_path]
    else:
        if uk.key_path not in puk:
            puk.append(uk.key_path)

    buf = io.StringIO()
    y.dump(doc, buf)
    my_setup_yaml_path.write_text(buf.getvalue(), encoding="utf-8")
    return ActionResult.SAVE_AS_PRESERVED


def _action_manual_edit(uk: UnexpectedKey) -> ActionResult:
    """Sub-prompt y/n; y opens ``$EDITOR`` on the tracked file; n returns pending."""
    file_display = uk.src_path
    yn = _read_one_choice(
        f"   Open $EDITOR on {file_display} now? (y/n): ", {"y", "n"}
    )
    if yn == "y":
        editor = os.environ.get("EDITOR", "vi")
        subprocess.run([editor, str(uk.src_path)], check=True)
        return ActionResult.MANUAL_EDIT_DONE
    return ActionResult.MANUAL_PENDING


# ---------------------------------------------------------------------------
# Signal handler factory
# ---------------------------------------------------------------------------


def _make_signal_handler(snapshot: Snapshot, sig_name: str) -> object:
    """Return a signal handler that restores ``snapshot`` then re-raises the signal."""
    def _handler(signum: int, frame: object) -> None:
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
) -> dict[int, object]:
    """Install SIGINT / SIGTERM / SIGHUP handlers for the wizard's lifetime.

    Returns the previous handlers so the caller can restore them when the
    wizard exits normally.
    """
    prev: dict[int, object] = {}
    for sig, name in [
        (signal.SIGINT, "SIGINT"),
        (signal.SIGTERM, "SIGTERM"),
        (signal.SIGHUP, "SIGHUP"),
    ]:
        prev[sig] = signal.signal(sig, _make_signal_handler(snapshot, name))
    return prev


def _restore_signal_handlers(prev: dict[int, object]) -> None:
    """Restore previously-saved signal handlers."""
    for sig, handler in prev.items():
        signal.signal(sig, handler)


# ---------------------------------------------------------------------------
# Wizard runner (used both by the `merge` CLI command and tests)
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
) -> list[tuple[UnexpectedKey, ActionResult]]:
    """Run the interactive merge wizard over all unexpected drift in ``report``.

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
    list of (UnexpectedKey, ActionResult) pairs — one per drift item walked.

    Raises
    ------
    KeyboardInterrupt
        When the user presses Ctrl-C and auto_accept is None (interactive
        mode). Callers are expected to restore the snapshot and exit with
        code 130.
    """
    if snapshot_base is None:
        snapshot_base = Path.home() / ".local" / "state" / "my-setup" / "merge-snapshots"

    if console is None:
        console = Console()

    # Collect all affected paths for the snapshot
    affected_paths: list[Path] = [my_setup_yaml_path]
    for uk in walk_unexpected_drift(report, config, repo_root, dotfile_filter=dotfile_filter):
        if uk.src_path not in affected_paths:
            affected_paths.append(uk.src_path)
        if uk.dst_path not in affected_paths:
            affected_paths.append(uk.dst_path)

    snap = Snapshot(files=affected_paths, snapshot_base=snapshot_base)
    decisions: list[tuple[UnexpectedKey, ActionResult]] = []

    with snap:
        if auto_accept is None:
            prev_handlers = _install_signal_handlers(snap)
        else:
            prev_handlers = {}

        try:
            file_pre = transitions.snapshot_paths(affected_paths)

            for uk in walk_unexpected_drift(
                report, config, repo_root, dotfile_filter=dotfile_filter
            ):
                if auto_accept is not None:
                    choice = auto_accept
                else:
                    choice = prompt_one(uk, console)

                result = apply_action(uk, choice, my_setup_yaml_path=my_setup_yaml_path)
                decisions.append((uk, result))

                if result == ActionResult.MANUAL_PENDING:
                    # User declined the editor — stop the walk but record what was applied
                    console.print(
                        f"[yellow]pending manual edit in {uk.src_path}; "
                        f"resume with: my-setup merge --profile={profile}[/yellow]"
                    )
                    break

            file_post = transitions.snapshot_paths(affected_paths)

            # Record one merge-transition for revert symmetry
            meta = transitions.make_meta(transitions.TransitionCommand.MERGE, profile)
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
