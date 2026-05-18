"""Capture: live → tracked.

The inverse of ``deploy.copy_atomic``. Reads each profile tracked_file's
``dst`` (the live copy) and writes a stripped version back to ``src``
(the tracked copy):

- ``preserve_user_sections`` files have the content between markers
  emptied (markers themselves remain, ready for a future deploy).
- ``preserve_user_keys`` files have those YAML keys removed (so live
  values stay host-local and never bake into the repo).

Since `setforge-nen.23`, capture is no longer a silent absorb. When a
tracked_file declares ``preserve_user_keys_deep`` or carries non-preserve
top-level drift between tracked and live, the capture-time merge
wizard fires (interactive by default; non-interactive via
``--auto={use-live, keep-tracked}``). The wizard mutates tracked
in-place at every drifted key path — capture's per-tracked_file writeback
then reads the post-wizard tracked, defensively strips shallow-preserve
content, and applies ``preserve_user_sections`` handling.

The ``CaptureRequiresInteractive`` exception is raised when capture
would prompt but stdin is not a TTY and ``--auto`` wasn't supplied —
the CLI layer renders this as a non-zero exit with a clear migration
hint.
"""

import io
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from rich.console import Console

# ruamel.yaml ships py.typed without resolvable annotations; no stub pkg on PyPI.
from ruamel.yaml import YAML  # type: ignore[import-not-found]

from setforge import jsonc, sections, yaml_merge
from setforge.capture_wizard import run_capture_wizard, walk_capture_drift
from setforge.compare import expand_tracked_file, resolve_dst, resolve_src
from setforge.config import Config, SectionMode, resolve_profile
from setforge.errors import CaptureRequiresInteractive


class CaptureAction(StrEnum):
    UPDATED = "updated"
    NOOP = "noop"
    SKIPPED = "skipped"


class CaptureAuto(StrEnum):
    """Closed set of non-interactive resolutions for capture-time drift.

    ``USE_LIVE`` — absorb all drift (reproduces pre-`nen.23` silent-absorb).
    ``KEEP_TRACKED`` — refuse to absorb any drift.

    ``None`` is the third valid value the CLI seam accepts (interactive mode);
    it sits outside the enum because ``StrEnum`` members must be strings.
    """

    USE_LIVE = "use-live"
    KEEP_TRACKED = "keep-tracked"


@dataclass(frozen=True, slots=True)
class CaptureResult:
    name: str
    action: CaptureAction
    reason: str = ""


def capture_tracked_file(
    src: Path,
    dst: Path,
    *,
    preserve_user_sections: bool,
    preserve_user_keys: list[str],
    preserve_user_keys_deep: list[str] | None = None,
    preserve_user_sections_mode: SectionMode = SectionMode.KEEP_DEFAULTS,
) -> CaptureResult:
    """Write a stripped version of ``dst`` (live) back to ``src`` (tracked).

    Empty ``preserve_user_keys`` and ``preserve_user_sections`` mean a
    direct copy. Returns :class:`CaptureResult.NOOP` if the resulting
    tracked content is byte-identical to the existing tracked file.

    ``preserve_user_sections_mode`` decides whether marker bodies in
    tracked are preserved (``KEEP_DEFAULTS``, default) or wiped
    (``STRIP``). KEEP_DEFAULTS falls back to STRIP semantics when src
    doesn't yet exist — no defaults to preserve.

    ``preserve_user_keys_deep`` (since `setforge-nen.23`) signals that
    tracked-only sub-keys at those paths must survive the live → tracked
    overlay. The capture-time wizard (fired by :func:`capture_profile`
    upstream) mutates tracked in place at the per-sub-key level before
    this function runs. When tracked already exists this function reads
    post-wizard tracked content and avoids clobbering tracked-only
    top-level keys; the resulting writeback is a defensive
    shallow-preserve strip + section handling on the post-wizard
    tracked, NOT a wholesale live-stripped overwrite.
    """
    if not dst.exists():
        return CaptureResult(
            name=src.name, action=CaptureAction.SKIPPED, reason="live missing"
        )

    if preserve_user_sections:
        # Markdown / preserve_user_sections path: capture's section
        # handling is unchanged from pre-`nen.23` (the capture-time wizard
        # does not fire for these tracked_files). Read live, optionally
        # strip shallow keys, merge tracked sections.
        content = _read_with_shallow_strip(dst, preserve_user_keys)
        if preserve_user_sections_mode is SectionMode.KEEP_DEFAULTS and src.exists():
            tracked_text = src.read_text(encoding="utf-8")
            tracked_sections = sections.extract_sections(tracked_text)
            content = sections.merge_sections(content, tracked_sections)
        else:
            content = sections.strip_section_content(content, allow_legacy=True)
        return _write_if_changed(src, content)

    if not src.exists():
        # Fresh capture (no tracked) — today's behavior: strip shallow
        # preserves from live, write to tracked. Deep paths can't apply
        # here because there's nothing to preserve on the tracked side.
        content = _read_with_shallow_strip(dst, preserve_user_keys)
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(content, encoding="utf-8")
        return CaptureResult(name=src.name, action=CaptureAction.UPDATED)

    # Tracked exists, no section handling.
    has_structured_preserve = bool(preserve_user_keys) or bool(preserve_user_keys_deep)

    if not has_structured_preserve:
        # No preserve declarations on this tracked_file — capture's
        # contract for unstructured files (plain text, markdown
        # without sections, list-only YAML) is unchanged from
        # pre-`nen.23`: wholesale live → tracked. The capture-time
        # wizard didn't fire here (the walker silently skips files
        # whose parsed root isn't a dict), so live's content is
        # the desired tracked content.
        return _write_if_changed(src, dst.read_text(encoding="utf-8"))

    # Structured file with at least one preserve declaration. The
    # capture-time wizard (upstream) has already absorbed every drift
    # item into tracked at the per-key level (deep sub-keys via deep
    # overlay; non-preserve top-level via shallow overlay).
    # Tracked-only top-level keys and tracked-only deep sub-keys
    # survive untouched.
    #
    # Defensively strip any shallow-preserve content from tracked — it
    # shouldn't be there post-wizard but the strip is the canonical
    # enforcement of the shallow-preserve contract. When only
    # preserve_user_keys_deep is set, tracked is already in the desired
    # state and we just round-trip the file.
    content = _read_with_shallow_strip(src, preserve_user_keys)
    return _write_if_changed(src, content)


def _read_with_shallow_strip(path: Path, preserve_user_keys: list[str]) -> str:
    """Return ``path`` contents with any ``preserve_user_keys`` stripped.

    Dispatches to JSONC or YAML strip per the file's extension; falls
    back to a plain read when no shallow keys are declared.
    """
    if not preserve_user_keys:
        return path.read_text(encoding="utf-8")
    if jsonc.is_jsonc_file(path):
        return _strip_shallow_keys_jsonc(path, preserve_user_keys)
    return _strip_shallow_keys_yaml(path, preserve_user_keys)


def _strip_shallow_keys_jsonc(path: Path, preserve_user_keys: list[str]) -> str:
    """Read JSONC ``path`` and drop every top-level key in ``preserve_user_keys``."""
    text = path.read_text(encoding="utf-8")
    return jsonc.strip_user_keys(text, preserve_user_keys)


def _strip_shallow_keys_yaml(path: Path, preserve_user_keys: list[str]) -> str:
    """Read YAML ``path``, drop ``preserve_user_keys``, return round-tripped text."""
    yaml = YAML(typ="rt")
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.load(fh)
    yaml_merge.delete_keys(doc, preserve_user_keys)
    buf = io.StringIO()
    yaml.dump(doc, buf)
    return buf.getvalue()


def _write_if_changed(src: Path, content: str) -> CaptureResult:
    """Write ``content`` to ``src`` unless it already matches; return action."""
    src.parent.mkdir(parents=True, exist_ok=True)
    if src.exists() and src.read_text(encoding="utf-8") == content:
        return CaptureResult(name=src.name, action=CaptureAction.NOOP)
    src.write_text(content, encoding="utf-8")
    return CaptureResult(name=src.name, action=CaptureAction.UPDATED)


def capture_profile(
    config: Config,
    profile_name: str,
    repo_root: Path,
    *,
    setforge_yaml_path: Path,
    interactive: bool | None = None,
    auto: CaptureAuto | None = None,
    snapshot_base: Path | None = None,
    console: Console | None = None,
) -> list[CaptureResult]:
    """Capture every tracked_file in the resolved profile from live → tracked.

    Orchestrates the capture-time wizard (fires when there is drift the
    walker yields) and the per-tracked_file writeback that runs against
    post-wizard tracked.

    Parameters
    ----------
    config:
        Loaded :class:`setforge.config.Config`.
    profile_name:
        Profile to capture.
    repo_root:
        Repo root used for ``resolve_src``.
    setforge_yaml_path:
        Path to ``setforge.yaml`` — needed by the wizard's ``[s]``
        action.
    interactive:
        Force-toggle for whether the wizard prompts. ``None`` (default)
        auto-detects via ``sys.stdin.isatty()``. ``False`` requires
        ``auto`` to be set when drift exists.
    auto:
        Non-interactive resolution: ``"use-live"`` absorbs all drift
        (reproduces today's silent-absorb behavior),
        ``"keep-tracked"`` rejects all drift, ``None`` enables
        interactive prompts.
    snapshot_base:
        Override for the wizard's snapshot directory; defaults to
        ``~/.local/state/setforge/sync-snapshots``.
    console:
        Rich Console for the wizard (defaults to a fresh
        ``Console()``).

    Raises
    ------
    CaptureRequiresInteractive
        When drift would prompt but stdin is not a TTY and ``auto`` is
        unset.
    KeyboardInterrupt
        Propagated from the wizard when the user cancels mid-prompt;
        the CLI layer renders the cancellation and exits 130.
    """
    if interactive is None:
        interactive = sys.stdin.isatty()

    items = list(walk_capture_drift(config, profile_name, repo_root))
    if items:
        if not interactive and auto is None:
            raise CaptureRequiresInteractive(
                f"capture would prompt for {len(items)} drift item(s); "
                f"run interactively or pass --auto=use-live / "
                f"--auto=keep-tracked."
            )
        auto_accept_map: dict[CaptureAuto | None, str | None] = {
            CaptureAuto.USE_LIVE: "u",
            CaptureAuto.KEEP_TRACKED: "k",
            None: None,
        }
        run_capture_wizard(
            config,
            profile_name,
            repo_root,
            setforge_yaml_path=setforge_yaml_path,
            snapshot_base=snapshot_base,
            console=console,
            auto_accept=auto_accept_map[auto],
        )

    # Post-wizard writeback: per-tracked_file, against the tracked content
    # the wizard left behind (or unchanged tracked if no drift).
    results: list[CaptureResult] = []
    resolved = resolve_profile(config, profile_name)
    for name in resolved.tracked_files:
        tracked_file = config.tracked_files[name]
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        for sub_name, sub_src, sub_dst in expand_tracked_file(name, src, dst):
            result = capture_tracked_file(
                sub_src,
                sub_dst,
                preserve_user_sections=tracked_file.preserve_user_sections,
                preserve_user_keys=tracked_file.preserve_user_keys,
                preserve_user_keys_deep=tracked_file.preserve_user_keys_deep,
                preserve_user_sections_mode=tracked_file.preserve_user_sections_mode,
            )
            results.append(
                CaptureResult(name=sub_name, action=result.action, reason=result.reason)
            )
    return results
