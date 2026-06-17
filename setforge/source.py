"""Config-source discovery layer for setforge.

The engine reads its declarative config (``setforge.yaml`` + ``tracked/``)
from a *source* — a directory containing both. Sources are typed as a
discriminated union of ``PathSource`` (a plain directory path on disk)
and ``GitSource`` (a clone destination derived from a git URL; the actual
clone/fetch logic lives in :mod:`setforge.git_ops`, not yet
implemented).

Discovery walks four precedence layers, first non-empty wins entirely
(mirrors :func:`setforge.binaries.resolve_binary`):

1. CLI flag — ``--source PATH`` (paths only; git URLs require fields
   that don't fit a single CLI flag, so they live in ``local.yaml``).
2. Env var — ``SETFORGE_SOURCE=PATH`` (paths only).
3. Host-local config — ``~/.config/setforge/local.yaml`` top-level
   ``source:`` block (PathSource OR GitSource).
4. Fallback — CWD if it contains ``setforge.yaml``.

Multi-source / stacked sources are explicitly OUT OF SCOPE per the
parent spec. The Pydantic schema's ``source:`` key is
singular; a list-shaped value raises a :class:`pydantic.ValidationError`
at load time.
"""

import os
import shlex
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal, NewType

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from setforge import git_ops
from setforge.anchors import (
    Anchor,
    AnchorAfterHeading,
    AnchorAfterSection,
    AnchorAtEndOfFile,
    AnchorAtStartOfFile,
    AnchorBeforeHeading,
    AnchorInSection,
    AnchorKind,
)
from setforge.config import (
    Disposition,
    MarketplaceSourceKind,
    _check_yaml_octal_mode,
)
from setforge.errors import (
    ConfigError,
    DirtySourceCheckout,
    NoSourceConfigured,
    SourceNotCloned,
)
from setforge.migrations import _local_yaml
from setforge.spans import SpanEntry, SpanKind

if TYPE_CHECKING:
    from setforge.section_templates import SeedPlanEntry

_STRICT = ConfigDict(extra="forbid")

CLI_FLAG: Final[str] = "--source"
ENV_VAR: Final[str] = "SETFORGE_SOURCE"
LOCAL_CONFIG_PATH: Final[Path] = Path.home() / ".config" / "setforge" / "local.yaml"
DEFAULT_CLONE_ROOT: Final[Path] = (
    Path.home() / ".local" / "share" / "setforge" / "sources"
)
CONFIG_FILENAME: Final[str] = "setforge.yaml"
# Pre-rename filename, retained as a one-shot migration target for
# validate_source_dir's friendly ConfigError. Mirrors the
# CONFIG_FILENAME shape so a future removal of legacy support is a
# single-symbol edit.
_LEGACY_CONFIG_FILENAME: Final[str] = "my_setup.yaml"


class SourceKind(StrEnum):
    """Discriminator for the :data:`Source` tagged union.

    Mirrors :class:`setforge.config.MarketplaceSourceKind` (the
    project's established pattern for Pydantic discriminator values).
    """

    PATH = "path"
    GIT = "git"


class PathSource(BaseModel):
    """Source backed by a directory already on disk.

    The directory must contain ``setforge.yaml`` at its root (validated
    lazily by :func:`validate_source_dir`, not at model construction).
    """

    model_config = _STRICT

    kind: Literal[SourceKind.PATH] = SourceKind.PATH
    path: Path
    name: str | None = None

    @property
    def display_name(self) -> str:
        """Return ``name`` if set, otherwise the directory basename."""
        return self.name or self.path.expanduser().name


class GitSource(BaseModel):
    """Source backed by a git repository to be cloned to ``clone_dest``.

    Cloning + checkout is handled by :mod:`setforge.git_ops` (not yet
    implemented). This module only resolves the *expected on-disk location*:
    ``clone_dest`` if set, otherwise ``DEFAULT_CLONE_ROOT / <name>``.
    """

    model_config = _STRICT

    kind: Literal[SourceKind.GIT] = SourceKind.GIT
    url: str
    ref: str = "main"
    name: str | None = None
    clone_dest: Path | None = None

    @property
    def display_name(self) -> str:
        """Return ``name`` if set, otherwise the URL basename minus ``.git``."""
        if self.name:
            return self.name
        tail = self.url.rstrip("/").rsplit("/", 1)[-1]
        return tail.removesuffix(".git")

    @property
    def resolved_clone_dest(self) -> Path:
        """Return the on-disk location where this source's clone lives."""
        if self.clone_dest is not None:
            return self.clone_dest.expanduser()
        return DEFAULT_CLONE_ROOT / self.display_name


Source = Annotated[PathSource | GitSource, Field(discriminator="kind")]


HostLocalSectionName = NewType("HostLocalSectionName", str)
"""Provenance-marked name of a host-local user-section.

A ``HostLocalSectionName`` MUST originate from a key in the local.yaml
``host_local_sections:`` block. Constructed at parse time by
:func:`load_local_host_local_sections` and threaded through the
injection module so callers cannot accidentally substitute a tracked-side
shared-section name (which has different drift semantics — shared
sections participate in section-reconcile; host-local sections do
not). Mirrors the :data:`setforge.sections.LiveSections` /
:data:`setforge.transitions.TransitionDir` pattern: a name-only
NewType wrapping ``str`` so call sites stay backwards-compatible at
runtime while the static type carries the provenance constraint.
"""


class HostLocalSection(BaseModel):
    """One host-local user-section overlay.

    Carries an :data:`Anchor` (where to splice the section) and exactly
    one of ``body`` (inline string) or ``body_file`` (path to a file
    read at install time). Both / neither is a configuration error
    surfaced at :class:`pydantic.ValidationError` time.

    Empty-``body_file`` validation is deferred to
    :func:`setforge.host_local_inject._read_body` (the injection / install
    path). Sniffing the filesystem inside a Pydantic model_validator
    couples schema parsing to the live FS state — a missing
    ``body_file`` slips through schema validation but fails at deploy,
    and revalidating a parsed model in a different cwd reads a
    different file. The schema check stays a pure-data invariant
    (exactly-one-of, non-empty inline body); the FS-touching empty
    check lives next to the read.
    """

    model_config = _STRICT

    anchor: Anchor
    body: str | None = None
    body_file: Path | None = None

    @model_validator(mode="after")
    def _exactly_one_body_source(self) -> "HostLocalSection":
        """Enforce exactly-one-of ``body`` / ``body_file`` + non-empty inline body.

        FS-touching checks (empty ``body_file``, missing ``body_file``)
        are intentionally NOT in scope here — the model validator stays
        pure so it can be reused at parse time without coupling to a
        specific cwd. See class docstring for the full rationale.
        """
        if (self.body is None) == (self.body_file is None):
            shape = "both" if self.body is not None else "neither"
            raise ValueError(
                "HostLocalSection requires exactly one of `body` (inline) "
                f"or `body_file` (path); got {shape}"
            )
        if self.body is not None and not self.body.strip():
            raise ValueError("HostLocalSection `body` must be non-empty")
        return self


class _LocalTrackedFileOverlay(BaseModel):
    """One tracked_file's worth of host-local overlay knobs.

    Carries a ``host_local_sections`` mapping keyed by section name, a
    ``spans`` list (host-local sub-file span intents), and three host-local
    install-time overrides: ``mode`` (chmod), ``dst`` (retarget install
    path), and ``symlink_target`` (deploy as a symlink). See each field's
    docstring for the use case and validation semantics.
    """

    model_config = _STRICT

    host_local_sections: dict[str, HostLocalSection] = {}
    mode: int | None = None
    """POSIX file-mode bits (chmod) for the live dst on this host.

    Use case: a tracked script that needs the executable bit on a
    development host but not on a CI-only host — the override lives
    in ``local.yaml`` rather than the tracked profile so a single
    setforge.yaml can serve both.

    Always write the YAML-1.2 octal literal: ``mode: 0o755``. The
    in-range bound is ``0..0o1777`` (1023 decimal + the 0o1000
    sticky bit) — covers rwxrwxrwx (0o0777) plus the sticky bit
    (0o1000). The setuid (0o4000) and setgid (0o2000) bits are
    refused for security, mirroring
    :func:`setforge.config.TrackedFile._validate_mode`. The
    out-of-range check fires first (mode <= 0o7777 is the parse
    surface); the setuid/setgid check fires second so the user
    gets a targeted message rather than a generic out-of-range.

    YAML-1.1 footgun caveat: under ``local.yaml``'s safe-yaml
    loader, BOTH ``mode: 0755`` (YAML-1.1 octal) and ``mode: 755``
    (plain decimal) parse to ``int(755)``, which IS in range
    (755 < 4095). The validator cannot distinguish "user meant
    0o755" from "user meant decimal 755" post-parse. Always
    prefer the explicit ``0o`` prefix to avoid the ambiguity; on
    the tracked-side ``setforge.yaml`` the round-trip YAML parser
    distinguishes the two and the strict octal-only field validator
    rejects the leading-zero form there. Mutually exclusive with
    ``symlink_target`` — chmod follows the link, not the symlink
    metadata itself, so the combination would silently chmod the
    target file (footgun); the validator refuses it.
    """

    dst: Path | None = None
    """Host-local override for the tracked_file's install destination.

    Use case: tracked file normally deploys to ``~/.foo``, but on
    host X it needs to deploy to ``/etc/foo`` (e.g., system-wide
    vs user-local install variant) — the override lives in
    ``local.yaml`` so the profile stays portable. Accepts
    ``~``-prefix (expanded at deploy time via
    :func:`Path.expanduser`). Forbids ``$VAR``-style env-var
    references (the validator raises on any ``$`` in the path) —
    expansion semantics are intentionally narrowed to
    ``expanduser`` only so a missing env var cannot silently
    redirect the install. Stored as raw :class:`Path`; resolved
    only at apply time.
    """

    symlink_target: Path | None = None
    """Host-local override to install the tracked_file as a symlink.

    Use case: tracked file content is a placeholder, but on host
    X the actual content lives at a system path (e.g.,
    ``/usr/local/share/foo/config.txt``); install creates a
    symlink at the tracked dst pointing to this target. Same
    path-expansion semantics as ``dst``. Mutually exclusive with
    ``mode``. Deploy-time semantics (mirrors
    :func:`setforge.deploy.deploy_symlinked_file`, the shared code
    path the overlay-fields override rides through):

    - Missing target file → :func:`_deploy_target_content` writes
      the tracked content to the target BEFORE
      :func:`_replace_symlink_atomic` places the link at dst.
      Post-install the link is NEVER dangling; the parent
      directory of ``target`` is created if absent. No warning
      is emitted — the "dangling" framing is informational only.
    - Existing dst that is a regular file (or pre-existing
      symlink that is not the desired one) → REFUSED with
      :class:`setforge.errors.SetforgeError`. The user must move
      the file aside or remove it before re-running install; the
      pre-existing content is preserved on refusal. This mirrors
      the tracked-side ``symlink:`` field's move-aside-first
      discipline (no silent clobber).
    - Existing dst that is a directory → REFUSED with
      :class:`setforge.errors.SetforgeError`. A tracked_file
      pointing at a real directory layout is almost certainly a
      config mistake.
    """

    spans: list[SpanEntry] = []
    """Host-local sub-file span intents (pinned / forked regions).

    Each :class:`~setforge.spans.SpanEntry` freezes (``pinned``) or
    host-isolates (``forked``) a sub-file region identified by a markdown
    heading-text anchor, with no in-file marker. Host-local intent lives
    here in ``local.yaml``; shared intent lives on the tracked-side
    :class:`~setforge.config.TrackedFile.spans`. The resolved offsets +
    baseline bytes are derived state in the spans sidecar
    (:mod:`setforge.spans_store`), never duplicated into this intent
    (Invariant I12). Anchor file-type legality is enforced by
    :func:`setforge.spans.validate_spans_file_type` at install time.
    """

    disposition: Disposition | None = None
    """Host-local override for the tracked_file's merge disposition.

    Use case: a tracked file is declared without a disposition in the
    shared ``setforge.yaml`` but a specific host needs to opt it into
    the ``forked`` or ``pinned`` behaviour (e.g. a host that should
    never contribute live edits back to the shared base). The override
    lives in ``local.yaml`` so the profile remains portable.

    Accepts exactly the StrEnum member values ``"shared"``, ``"forked"``,
    ``"pinned"``; any other casing or value is rejected at parse time
    (:class:`pydantic.ValidationError`). The merged result is re-validated
    by :func:`setforge.config.TrackedFile.model_validate`, so every
    ``TrackedFile`` invariant re-runs against the overridden disposition.
    """

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode_octal_only(cls, v: object) -> int | None:
        """Reject every shape EXCEPT YAML-1.2 octal (``0o755``) or a plain int.

        Dispatches to :func:`setforge.config._check_yaml_octal_mode` —
        the cascade shared with
        :func:`setforge.config.TrackedFile._validate_mode` — so a host
        that writes ``mode: 0755`` in ``local.yaml`` gets the same
        clear "use 0o755" error as a tracked-side ``mode: 0755``
        misconfig — anti-smell #6 (YAML 1.1 leading-zero footgun).
        Range and setuid/setgid bounds are enforced separately in
        :func:`_validate_host_local_overrides`.
        """
        return _check_yaml_octal_mode(v, "_LocalTrackedFileOverlay: `mode`")

    @model_validator(mode="after")
    def _validate_host_local_overrides(self) -> "_LocalTrackedFileOverlay":
        """Enforce mutual-exclusion + bounds + ``$VAR`` ban for the overlay fields.

        Four invariants in one validator so each call to
        ``_LocalTrackedFileOverlay.model_validate(...)`` sees the
        full contract:

        1. ``mode`` + ``symlink_target`` together — refused
           (chmod-on-symlink follows the link, footgun semantics).
        2. ``mode`` out of ``0..0o7777`` (4095 decimal) — refused
           (12-bit POSIX mode-bit ceiling at the parse layer).
        3. ``mode`` with setuid (0o4000) or setgid (0o2000) bits
           set — refused for security, mirroring
           :func:`setforge.config.TrackedFile._validate_mode`.
           Catching it here surfaces a clear message before the
           downstream ``TrackedFile.model_validate(merged)`` in
           :func:`setforge.config.apply_host_local_tracked_file_overrides`.
        4. ``dst`` containing ``$`` — refused (env-var expansion
           is intentionally out of contract; only ``~``-prefix is
           supported, expanded via :func:`Path.expanduser`).
        """
        if self.mode is not None and self.symlink_target is not None:
            raise ValueError(
                "_LocalTrackedFileOverlay: `mode` and `symlink_target` are "
                "mutually exclusive — chmod-on-symlink modifies the symlink "
                "target, not the link."
            )
        if self.mode is not None and (self.mode < 0 or self.mode > 0o7777):
            raise ValueError(
                "_LocalTrackedFileOverlay: `mode` must be in 0..0o7777 "
                f"(4095 decimal); got {self.mode:#o}"
            )
        if self.mode is not None and self.mode & 0o6000:
            # Mirror TrackedFile._validate_mode: setuid (0o4000) and
            # setgid (0o2000) bits are refused for security; sticky
            # (0o1000) is still permitted. Catching it at the overlay
            # layer surfaces a clear message instead of the less-clear
            # ValidationError from TrackedFile.model_validate(merged)
            # downstream in apply_host_local_tracked_file_overrides.
            raise ValueError(
                f"_LocalTrackedFileOverlay: `mode` {self.mode:#o} sets "
                "setuid/setgid bits which TrackedFile refuses for "
                "security; use 0..0o1777 (sticky bit still permitted)."
            )
        if self.dst is not None and "$" in str(self.dst):
            raise ValueError(
                "_LocalTrackedFileOverlay: `dst` must not contain env-var "
                f"references ($VAR); got {self.dst}. Use a ``~``-prefixed "
                "or absolute path; expansion is via Path.expanduser only."
            )
        return self


class PluginOverlay(BaseModel):
    """Per-host plugin add/remove overlay block (SPEC 2).

    Lives under ``local.yaml``'s top-level ``plugins:`` key. Both lists
    default to empty so a partial overlay (only ``add`` or only ``remove``)
    is a valid shape; the resolver merges them with the profile chain at
    load time via :func:`setforge.local_overlay.resolve_plugin_overlay`.

    ``add`` entries use the same ``name@marketplace`` shape as
    ``Profile.claude_plugins`` so the bare-name @ marketplace dispatch in
    :mod:`setforge.claude_plugins` is unchanged.
    """

    model_config = _STRICT

    add: list[str] = []
    remove: list[str] = []


class ExtensionOverlay(BaseModel):
    """Per-host VSCode-extension add/remove overlay block.

    Mirrors :class:`PluginOverlay` (both lists default empty, ``_STRICT``).
    Adds land in :attr:`setforge.config.Extensions.include`; removes drop
    matching entries from the resolved include list. Excludes are
    profile-only — out of scope per SPEC 2.
    """

    model_config = _STRICT

    add: list[str] = []
    remove: list[str] = []


class _MarketplaceLocalDecl(BaseModel):
    """One local-overlay marketplace declaration.

    Mirrors :class:`setforge.config.MarketplaceSource`'s shape and the
    ``_exactly_one`` validator at ``config.py:393`` — kept as a separate
    model so :mod:`setforge.source` does not import the heavy
    :mod:`setforge.config` module at definition time (would create a
    config <-> source cycle for the resolver's lazy-import pattern).
    """

    model_config = _STRICT

    source: MarketplaceSourceKind
    repo: str | None = None
    path: Path | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "_MarketplaceLocalDecl":
        if (self.repo is None) == (self.path is None):
            raise ValueError("_MarketplaceLocalDecl: exactly one of repo/path required")
        return self


class MarketplaceOverlay(BaseModel):
    """Per-host marketplace add/remove overlay block.

    ``add`` is keyed by marketplace name — same shape as the top-level
    :attr:`setforge.config.Config.marketplaces` mapping. ``remove`` is a
    list of marketplace names; the resolver errors on remove-of-unknown
    (mirrors :mod:`setforge.preserved_keys` precedent).
    """

    model_config = _STRICT

    add: dict[str, _MarketplaceLocalDecl] = {}
    remove: list[str] = []


class _LocalSourceConfig(BaseModel):
    """Source + tracked_files + per-host overlay blocks of ``local.yaml``.

    Loaded separately from :class:`setforge.binaries.HostLocalConfig` so
    the source-discovery layer and the binary-override layer can each
    parse the file independently without coupling. Carries the
    ``tracked_files:`` overlay block (per-tracked_file host-local knobs
    from SPEC 8) plus the per-host plugin/extension/marketplace overlay
    blocks (SPEC 2) so the loader can apply the overlays
    at profile-resolution time via :mod:`setforge.preserved_keys` and
    :mod:`setforge.local_overlay`.
    """

    model_config = _STRICT

    source: Source | None = None
    tracked_files: dict[str, _LocalTrackedFileOverlay] = {}
    plugins: PluginOverlay = PluginOverlay()
    extensions: ExtensionOverlay = ExtensionOverlay()
    marketplaces: MarketplaceOverlay = MarketplaceOverlay()

    @model_validator(mode="before")
    @classmethod
    def _reject_list_shaped_source(cls, data: object) -> object:
        """Reject ``source:`` as a list (multi-source out of scope).

        Pydantic's discriminated-union validation would error on a list
        value, but the message is opaque ("Input should be a valid
        dictionary"). Surface a clear message here so the user knows
        WHY a list shape is rejected.
        """
        if isinstance(data, Mapping) and isinstance(data.get("source"), list):
            raise ValueError(
                "`source:` must be a single mapping (path-kind or git-kind), "
                "not a list. Multi-source / stacked sources is out of scope "
                "for setforge."
            )
        return data


_pending_seed: "list[SeedPlanEntry] | None" = None
"""In-memory section-template seed plan visible to local.yaml parses.

Set by :func:`injected_seed` over the install command's pre-consent
window so the three host-local overlay readers
(:func:`detect_shared_span_collisions`,
:func:`apply_host_local_tracked_file_overrides`,
:func:`_load_validated_host_local_sections`) — all of which funnel
through :func:`_load_local_source_config` — observe a freshly-planned
seed WITHOUT any disk write, exactly as if the block had been committed.
``None`` (the unset sentinel) means no seed is pending; the disk COMMIT
happens later, under the install lock, after consent. Mirrors the
:data:`_cli_source` module-state idiom (single-process, non-reentrant).
"""


@contextmanager
def injected_seed(plan: "list[SeedPlanEntry]") -> Iterator[None]:
    """Make a pre-consent seed ``plan`` visible to local.yaml parses.

    Single-process and non-reentrant: asserts no seed is already pending
    on entry (a nested injection is a caller bug). Always clears in
    ``finally`` so a welcome decline, a git-check abort, or any other
    exception cannot leak the payload into the under-lock migration read
    or a later in-process command.
    """
    global _pending_seed
    assert _pending_seed is None, "injected_seed is not reentrant"
    _pending_seed = plan
    try:
        yield
    finally:
        _pending_seed = None


def _merge_pending_seed_into(data: MutableMapping[str, object]) -> None:
    """Merge the pending seed plan into a freshly-parsed local.yaml ``data``.

    Each :class:`SeedPlanEntry` becomes a
    ``tracked_files.<id>.host_local_sections.<name>`` block (anchor
    ``at-end-of-file``, inline ``body``) — the SAME shape
    :func:`setforge.section_templates.seed_section_templates` writes to
    disk — so the downstream :func:`_local_yaml.relocate_retired_keys`
    converts it to the identical OVERLAY span the disk path would
    produce. A section name already present in ``data`` is left untouched
    (belt-and-suspenders against a host that already adopted it).

    Mutates ``data`` in place; ``data`` is the local, freshly-parsed dict
    owned by :func:`_load_local_source_config` (never an alias of a
    caller's object and never written back to disk), and the
    :data:`_pending_seed` entries are read but never mutated.
    """
    if _pending_seed is None:
        return
    tracked_files = data.get("tracked_files")
    if not isinstance(tracked_files, dict):
        tracked_files = {}
        data["tracked_files"] = tracked_files
    for entry in _pending_seed:
        tf_block = tracked_files.get(entry.tracked_file_id)
        if not isinstance(tf_block, dict):
            tf_block = {}
            tracked_files[entry.tracked_file_id] = tf_block
        sections = tf_block.get("host_local_sections")
        if not isinstance(sections, dict):
            sections = {}
            tf_block["host_local_sections"] = sections
        if entry.section_name in sections:
            continue
        sections[entry.section_name] = {
            "anchor": {"kind": "at-end-of-file"},
            "body": entry.body,
        }


def _load_local_source_config(path: Path) -> _LocalSourceConfig:
    """Parse the ``source:`` block from ``local.yaml``.

    Returns an empty :class:`_LocalSourceConfig` when the file is absent
    or carries no ``source:`` key. Raises :class:`ConfigError` on YAML
    parse failure or non-mapping top level. Pydantic validation errors
    propagate unchanged (with the field-level message).

    Runs detect-before-validate: a cross-major-newer doc refuses cleanly
    (:class:`ConfigError`), and a retired-key (``host_local_sections``)
    doc is relocated to the unified span shape IN MEMORY BEFORE the
    ``extra="forbid"`` model sees it (else it would trip an
    ``extra_forbidden`` error on the legacy key). The in-memory relocation
    deliberately does NOT touch disk: the on-disk rewrite is owned by the
    install path's snapshot-aware migration step, which captures the
    pre-migration bytes first so ``revert`` restores them byte-for-byte.
    """
    # An absent / empty local.yaml normally short-circuits, but a pending
    # pre-consent seed must still surface (a fresh host has no local.yaml
    # yet), so build an empty document to merge into instead.
    data: object
    if not path.exists():
        if _pending_seed is None:
            return _LocalSourceConfig()
        data = {}
    else:
        yaml = YAML(typ="safe")
        try:
            data = yaml.load(path.read_text(encoding="utf-8"))
        except YAMLError as exc:
            raise ConfigError(f"malformed YAML in {path}: {exc}") from exc
        if data is None:
            if _pending_seed is None:
                return _LocalSourceConfig()
            data = {}
    if not isinstance(data, MutableMapping):
        raise ConfigError(f"top-level of {path} must be a mapping")
    # Merge any pre-consent seed plan into the parsed document BEFORE the
    # retired-key relocation, so a freshly-planned host_local_sections
    # block is retired to an OVERLAY span exactly as a disk-committed seed
    # would be — the single chokepoint that makes all three overlay
    # readers observe the seed without a write. No-op when no
    # seed is pending.
    _merge_pending_seed_into(data)
    # detect→guard→relocate, BEFORE strict model_validate. The guard
    # refuses a newer-major local.yaml cleanly; the in-memory relocation
    # retires legacy keys (host_local_sections → spans) so the strict
    # model accepts the document. No disk write — see the docstring.
    _local_yaml.guard_local_yaml_schema(data, path)
    _local_yaml.relocate_retired_keys(data)
    # Extract only the keys this loader owns; ignore other blocks
    # (binaries:, claude:, orphan_ignore:) which belong to other loaders.
    payload: dict[str, object] = {}
    for key in ("source", "tracked_files", "plugins", "extensions", "marketplaces"):
        if key in data:
            payload[key] = data[key]
    if not payload:
        return _LocalSourceConfig()
    return _LocalSourceConfig.model_validate(payload)


def load_local_tracked_file_overlays(
    path: Path = LOCAL_CONFIG_PATH,
) -> dict[str, _LocalTrackedFileOverlay]:
    """Return the ``tracked_files:`` overlay block from ``local.yaml``.

    Empty dict when the file is absent, the block is missing, or the
    block is an empty mapping. Lazy-loaded — callers (the config-layer
    overlay applier) invoke this at profile-resolution time, never at
    import time, per the SPEC 8 anti-smell discipline.
    """
    return _load_local_source_config(path).tracked_files


_MARKDOWN_SUFFIXES: Final[frozenset[str]] = frozenset({".md", ".markdown"})


def validate_host_local_sections_file_type(
    tracked_file_id: str,
    section_count: int,
    src: Path,
) -> None:
    """Raise :class:`ConfigError` if ``src`` is not markdown.

    ``host_local_sections`` is REJECTED for non-markdown tracked_files
    (.md / .markdown). Anchor grammar (after-heading / before-heading /
    after-section) is intrinsically markdown-shaped; JSON / JSONC /
    YAML files have no headings. Host-local JSON/JSONC keys are a deferred
    follow-up (``host_local_keys for JSON and YAML tracked_files``,
    deferred at batch close-out per SPEC 1).

    No-op when ``section_count`` is 0 — the file may not be markdown
    but no host-local sections were declared.
    """
    if section_count == 0:
        return
    suffix = src.suffix.lower()
    if suffix in _MARKDOWN_SUFFIXES:
        return
    raise ConfigError(
        "host_local_sections is supported only for markdown tracked_files "
        f"(.md / .markdown). tracked_file {tracked_file_id!r} resolves to "
        f"src={src} (extension {suffix!r} not in {sorted(_MARKDOWN_SUFFIXES)}). "
        "Host-local JSON/JSONC/YAML keys are a deferred follow-up "
        "('host_local_keys for JSON and YAML tracked_files')."
    )


def _host_local_sections_for_overlay(
    overlay: _LocalTrackedFileOverlay,
) -> dict[HostLocalSectionName, HostLocalSection]:
    """Project one overlay's host-local sections, legacy + migrated unified.

    Returns the union of:

    - the legacy ``host_local_sections`` block (pre-migration hosts), and
    - every migrated OVERLAY ``spans`` entry (``kind=overlay``,
      ``semantics=host-local``), reconstructed back into a
      :class:`HostLocalSection` keyed by the span's identity ``anchor``
      (the original section name).

    The migration (:mod:`setforge.overlay_migration`) physically rewrites
    ``host_local_sections`` into OVERLAY spans on the first install, so a
    legacy reader that projected only the old block returned ``{}`` for an
    already-migrated host — blinding the capture host-local strip, the
    compare overlay mask, the promote wizard, and the install injection.
    Projecting the migrated spans back here keeps every one of those
    consumers seeing the host-local content after the migration, so the
    leak / erase / drift gaps the half-wired migration opened stay closed.

    A legacy block and a migrated span for the SAME name cannot coexist
    (the migration deletes the legacy block as it appends the span);
    should both ever appear, the legacy entry wins (it is inserted last)
    — a no-op in practice since they carry identical bodies.
    """
    sections: dict[HostLocalSectionName, HostLocalSection] = {}
    for span in overlay.spans:
        if span.kind is not SpanKind.OVERLAY or span.overlay is None:
            continue
        payload = span.overlay
        sections[HostLocalSectionName(span.anchor)] = HostLocalSection(
            anchor=payload.anchor,
            body=payload.body,
            body_file=payload.body_file,
        )
    for name, section in overlay.host_local_sections.items():
        sections[HostLocalSectionName(name)] = section
    return sections


def load_local_host_local_sections(
    path: Path = LOCAL_CONFIG_PATH,
) -> dict[str, dict[HostLocalSectionName, HostLocalSection]]:
    """Return ``{tracked_file_id: {section_name: HostLocalSection}}``.

    Mirrors :func:`load_local_tracked_file_overlays` shape but projects
    each :class:`_LocalTrackedFileOverlay` to its host-local sections —
    BOTH the legacy ``host_local_sections`` block AND the migrated OVERLAY
    ``spans`` entries (see :func:`_host_local_sections_for_overlay` for the
    unification rationale). Empty dict when the file is absent or no
    tracked_file declares any host-local section. Entries that project to
    an empty mapping are dropped from the result so callers can treat
    presence as "has at least one section".

    Section-name keys are constructed as :data:`HostLocalSectionName`
    here at the parse boundary (the local.yaml load point); downstream
    callers receive the provenance-marked NewType so a type-checker
    flags any attempt to pass a tracked-side shared-section name in.
    """
    overlays = _load_local_source_config(path).tracked_files
    projected = {
        tf_id: _host_local_sections_for_overlay(overlay)
        for tf_id, overlay in overlays.items()
    }
    return {tf_id: sections for tf_id, sections in projected.items() if sections}


_cli_source: Path | None = None


def set_cli_source(value: Path | None) -> None:
    """Capture the ``--source`` flag value from the Typer callback.

    Stored at module scope so commands can call :func:`get_resolved_source`
    without re-threading the flag through every signature. Mirrors the
    pattern in :func:`setforge.binaries.set_cli_overrides`.
    """
    global _cli_source
    _cli_source = value


def get_resolved_source() -> Source:
    """Resolve the current source using module-state CLI flag + live env.

    Convenience wrapper around :func:`resolve_source` for use inside
    Typer command bodies that don't carry the flag through their own
    signature. Reads ``os.environ`` and ``Path.cwd()`` live.
    """
    return resolve_source(
        cli_path=_cli_source,
        env=os.environ,
        local_config_path=LOCAL_CONFIG_PATH,
        cwd=Path.cwd(),
    )


def resolve_source(
    *,
    cli_path: Path | None,
    env: Mapping[str, str],
    local_config_path: Path = LOCAL_CONFIG_PATH,
    cwd: Path | None = None,
) -> Source:
    """Walk the 4-layer precedence chain and return the resolved source.

    Layers (first non-empty wins entirely):

    1. ``cli_path`` (from ``--source PATH`` on the command line).
    2. ``env[ENV_VAR]`` (``SETFORGE_SOURCE=PATH``).
    3. ``local_config_path`` ``source:`` block (path OR git source).
    4. ``cwd / "setforge.yaml"`` exists (back-compat for run-from-repo).

    Raises :class:`NoSourceConfigured` when no layer produces a source,
    listing all four layers in the message so the user knows where to
    configure.
    """
    if cli_path is not None:
        return PathSource(path=cli_path)
    env_value = env.get(ENV_VAR)
    if env_value:
        return PathSource(path=Path(env_value))
    local = _load_local_source_config(local_config_path)
    if local.source is not None:
        return local.source
    cwd_resolved = cwd or Path.cwd()
    cwd_yaml = cwd_resolved / CONFIG_FILENAME
    if cwd_yaml.exists():
        return PathSource(path=cwd_resolved)
    raise NoSourceConfigured(
        "no config source configured. Layers checked in order:\n"
        f"  1. CLI flag {CLI_FLAG} PATH (not provided)\n"
        f"  2. env {ENV_VAR}=PATH (unset or empty)\n"
        f"  3. {local_config_path} `source:` block (absent or missing key)\n"
        f"  4. CWD fallback {cwd_yaml} (file not found)"
    )


def resolve_source_dir(source: Source) -> Path:
    """Return the on-disk directory where ``source``'s contents live.

    For :class:`PathSource`: returns ``path`` expanded.
    For :class:`GitSource`: returns ``clone_dest`` (or its default);
    raises :class:`SourceNotCloned` if the directory does not exist on
    disk (the user must run ``setforge fetch`` first).
    """
    if isinstance(source, PathSource):
        return source.path.expanduser()
    resolved = source.resolved_clone_dest
    if not resolved.exists():
        raise SourceNotCloned(
            f"git source {source.display_name!r} not cloned at {resolved}. "
            f"Run `setforge fetch` to clone."
        )
    return resolved


def validate_source_dir(source: Source) -> Path:
    """Verify the source's directory contains ``setforge.yaml``; return its path.

    Raises :class:`SourceNotCloned` if a :class:`GitSource`'s clone is
    absent; raises :class:`ConfigError` if the directory exists but does
    not contain ``setforge.yaml`` at its root. When a legacy
    ``my_setup.yaml`` is present, the error message surfaces a ``git mv``
    migration recipe.
    """
    source_dir = resolve_source_dir(source)
    config_path = source_dir / CONFIG_FILENAME
    if config_path.exists():
        return config_path
    # Friendly migration error for the my_setup.yaml -> setforge.yaml
    # rename. Mirrors the legacy-namespace detector
    # pattern in setforge.sections.detect_legacy_namespace_markers.
    legacy_path = source_dir / _LEGACY_CONFIG_FILENAME
    if legacy_path.exists():
        quoted_dir = shlex.quote(str(source_dir))
        raise ConfigError(
            f"source {source.display_name!r} at {source_dir} contains a "
            f"legacy {_LEGACY_CONFIG_FILENAME!r}. setforge expects "
            f"'{CONFIG_FILENAME}'. Rename: (cd {quoted_dir} && "
            f"git mv {_LEGACY_CONFIG_FILENAME} {CONFIG_FILENAME})"
        )
    raise ConfigError(
        f"source {source.display_name!r} at {source_dir} does not contain "
        f"{CONFIG_FILENAME}"
    )


def check_source_clean(source: Source) -> None:
    """Pre-write gate: raise :class:`DirtySourceCheckout` on dirty source.

    Scopes the porcelain check to ``tracked/`` (the engine's only write
    surface). Non-git PathSource dirs skip the check (the user isn't
    using git here; nothing to protect against). GitSource always runs
    the check; if its clone is missing, :class:`SourceNotCloned` from
    :func:`resolve_source_dir` propagates.
    """
    source_dir = resolve_source_dir(source)
    if not git_ops.is_git_repo(source_dir):
        return
    porcelain = git_ops.status_porcelain(source_dir, path="tracked")
    if not porcelain:
        return
    file_count = len([line for line in porcelain.splitlines() if line.strip()])
    raise DirtySourceCheckout(
        f"{source_dir}/tracked/ has uncommitted changes "
        f"({file_count} file{'s' if file_count != 1 else ''}). "
        "Commit or stash before retrying."
    )


def check_source_yaml_clean(source: Source) -> None:
    """Pre-write gate for a ``setforge.yaml``-root write (the ``--shared`` path).

    The sibling of :func:`check_source_clean`, but scoped to the
    version-controlled ``setforge.yaml`` at the source ROOT rather than
    the engine's ``tracked/`` write surface — :func:`check_source_clean`
    deliberately misses the root config. The ``override --shared``
    write mutates ``setforge.yaml`` in place, so a dirty / mid-rebase
    config must refuse before the round-trip clobbers an uncommitted edit.

    Non-git PathSource dirs skip the check (no git, nothing to protect).
    GitSource always runs; a missing clone surfaces
    :class:`SourceNotCloned` from :func:`resolve_source_dir`.
    """
    source_dir = resolve_source_dir(source)
    if not git_ops.is_git_repo(source_dir):
        return
    porcelain = git_ops.status_porcelain(source_dir, path=CONFIG_FILENAME)
    if not porcelain:
        return
    raise DirtySourceCheckout(
        f"{source_dir}/{CONFIG_FILENAME} has uncommitted changes. "
        "Commit or stash before retrying the --shared override."
    )


def _fast_forward_branch_ref(clone_dest: Path, ref: str) -> None:
    """Fast-forward a checked-out branch ref to its fetched remote-tracking tip.

    ``git fetch`` advances ``origin/<ref>`` but never the local branch, and
    ``git checkout <branch>`` lands on the (stale) local branch — so without
    this step a re-fetch of an existing clone keeps serving the commit the
    branch had at first clone. After fetch + checkout, if ``ref`` resolves
    to a remote-tracking branch (``refs/remotes/origin/<ref>``), advance the
    local branch with ``merge --ff-only`` so the working tree reflects the
    upstream tip. SHAs and tags do not resolve to that ref, so they are
    left untouched (detached / pinned, as intended). ``--ff-only`` keeps the
    update a pure fast-forward: it refuses (and surfaces a clear error)
    rather than creating a merge commit if the local branch ever diverged.

    Uses :mod:`git_ops` subprocess hygiene (credential masking + GitOpError
    wrapping) so a failure surfaces consistently with the rest of fetch.
    """
    probe = git_ops._run_git(
        ["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{ref}"],
        cwd=clone_dest,
        check=False,
    )
    if probe.returncode != 0:
        return  # not a branch (SHA / tag); nothing to fast-forward
    git_ops._run_git(["merge", "--ff-only", f"origin/{ref}"], cwd=clone_dest)


def fetch_source(source: Source) -> str:
    """Clone-on-missing + fetch + ref-checkout the given git source.

    Returns a one-line human-readable status message describing the
    operation performed. :class:`PathSource` returns
    ``"source is a path; nothing to fetch"`` immediately (no git ops).
    GitSource: (1) compute clone_dest, clone if missing; (2) fetch
    origin; (3) verify ``tracked/`` is clean; (4) check out the
    pinned ref; (5) fast-forward the local branch to the fetched
    upstream tip when ``ref`` is a branch (a no-op for SHAs/tags).
    Errors propagate (:class:`GitOpError`, :class:`DirtySourceCheckout`).
    """
    if isinstance(source, PathSource):
        return "source is a path; nothing to fetch"
    clone_dest = source.resolved_clone_dest
    cloned = False
    if not clone_dest.exists():
        git_ops.git_clone(source.url, clone_dest)
        cloned = True
    git_ops.git_fetch(clone_dest)
    porcelain = git_ops.status_porcelain(clone_dest, path="tracked")
    if porcelain:
        file_count = len([line for line in porcelain.splitlines() if line.strip()])
        raise DirtySourceCheckout(
            f"{clone_dest}/tracked/ has uncommitted changes "
            f"({file_count} file{'s' if file_count != 1 else ''}). "
            "Commit or stash before fetching."
        )
    git_ops.git_checkout(clone_dest, source.ref)
    _fast_forward_branch_ref(clone_dest, source.ref)
    action = "cloned and checked out" if cloned else "fetched and checked out"
    return f"{action} {source.ref} at {clone_dest}"


def format_post_write_hint(
    source: Source, file_count: int, *, subpath: str = "tracked/"
) -> str:
    """Build the post-sync/capture hint message pointing at the source dir.

    ``subpath`` is the source-root-relative path the write landed at,
    rendered verbatim in the hint. The default ``tracked/`` matches the
    sync/capture target. Pass the actual file (e.g. ``setforge.yaml``) when
    the write lands directly on the source root — as an ``override --shared``
    write does — so the hint names the path the user must ``git diff``, not a
    ``tracked/`` they never touched.

    Three shapes (decided by source kind + git upstream presence):

    * PathSource without ``.git/``: bare file-count message, no git hint.
    * Git repo without upstream: ``cd ... && git diff && git commit``.
    * Git repo with upstream: ``... && git push`` appended.
    """
    try:
        source_dir = resolve_source_dir(source)
    except SourceNotCloned:
        return f"→ wrote {file_count} files to <source> (not on disk?)"
    plural = "s" if file_count != 1 else ""
    base = f"→ wrote {file_count} file{plural} to {source_dir}/{subpath}"
    if not git_ops.is_git_repo(source_dir):
        return base
    upstream = git_ops.rev_parse_upstream(source_dir)
    if upstream is not None:
        return f"{base}; cd {source_dir} && git diff && git commit && git push"
    return f"{base}; cd {source_dir} && git diff && git commit"


__all__ = [
    "CLI_FLAG",
    "CONFIG_FILENAME",
    "DEFAULT_CLONE_ROOT",
    "ENV_VAR",
    "LOCAL_CONFIG_PATH",
    "Anchor",
    "AnchorAfterHeading",
    "AnchorAfterSection",
    "AnchorAtEndOfFile",
    "AnchorAtStartOfFile",
    "AnchorBeforeHeading",
    "AnchorInSection",
    "AnchorKind",
    "ExtensionOverlay",
    "GitSource",
    "HostLocalSection",
    "HostLocalSectionName",
    "MarketplaceOverlay",
    "PathSource",
    "PluginOverlay",
    "Source",
    "SourceKind",
    "check_source_clean",
    "check_source_yaml_clean",
    "fetch_source",
    "format_post_write_hint",
    "get_resolved_source",
    "load_local_host_local_sections",
    "resolve_source",
    "resolve_source_dir",
    "set_cli_source",
    "validate_host_local_sections_file_type",
    "validate_source_dir",
]
