"""ruamel.yaml round-trip helpers shared by migrations.

Per research brief §4 ``ruamel.yaml round-trip``: every YAML edit a
migration performs must preserve comments, key insertion order, and
quoting. The helpers in this module standardize the ruamel.yaml
configuration (``YAML(typ="rt")`` with ``preserve_quotes=True`` and
a wide ``width`` so existing line-wrapping is not reflowed) and
provide a comment-preserving ``rename_key`` that explicitly migrates
the ``ca.items`` comment-association entries from the old key to the
new one — without this step, the comments attached to the renamed key
are silently dropped by ruamel.

Writes go through :func:`atomic_write_yaml`: serialize to a string
buffer, then finalize via :func:`setforge.atomicio.atomic_write_text`
(sibling tmp + ``os.replace``), so a crash mid-write never leaves a
half-rendered YAML document on disk.
"""

from __future__ import annotations

import difflib
import io
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

from setforge import atomicio
from setforge.errors import ConfigError

__all__ = [
    "atomic_write_yaml",
    "load_yaml_mapping",
    "rename_key",
    "render_yaml",
    "yaml_rt",
]

_BOM = "\ufeff"
_DEFAULT_INDENT = (2, 2, 0)


def yaml_rt() -> YAML:
    """Return a configured round-trip ``YAML`` instance.

    - ``typ="rt"`` keeps comments, key order, and quoting.
    - ``preserve_quotes=True`` keeps the original quote style on scalars.
    - ``width=4096`` suppresses ruamel's default 80-col reflow on long
      scalars, which would otherwise rewrite untouched lines as a side
      effect of round-tripping.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    return yaml


def _detect_indent(text: str) -> tuple[int, int, int] | None:
    """Return the first block mapping indent and sequence ``(indent, offset)``.

    Reads the positions ruamel records while parsing, so quoting, comments
    and block scalars cannot confuse it. Returns ``(mapping, sequence,
    offset)`` using ruamel's ``indent()`` meaning, with ``None`` entries
    replaced by the defaults, or ``None`` when the text does not parse.
    """
    try:
        root = yaml_rt().load(text)
    except YAMLError:
        return None
    found: dict[str, int] = {}

    def walk(node: object) -> None:
        if isinstance(node, CommentedMap):
            for key, value in node.items():
                key_col = node.lc.data[key][1]
                if (
                    isinstance(value, CommentedMap)
                    and value
                    and not value.fa.flow_style()
                ):
                    found.setdefault("mapping", value.lc.col - key_col)
                elif (
                    isinstance(value, CommentedSeq)
                    and value
                    and not value.fa.flow_style()
                ):
                    dash = value.lc.col
                    found.setdefault("offset", dash - key_col)
                    found.setdefault("sequence", value.lc.data[0][1] - key_col)
                walk(value)
        elif isinstance(node, CommentedSeq):
            for item in node:
                walk(item)

    walk(root)
    if not found:
        return None
    mapping = found.get("mapping", _DEFAULT_INDENT[0])
    offset = found.get("offset", _DEFAULT_INDENT[2])
    sequence = found.get("sequence", offset + 2)
    return mapping, max(sequence, offset + 2), offset


def render_yaml(
    data: Any,  # noqa: ANN401 — ruamel round-trip data is untyped
    original: str | None,
    *,
    fallback: tuple[int, int, int] = _DEFAULT_INDENT,
) -> str:
    """Dump ``data`` in the indentation style of the ``original`` document.

    The mapping indent and sequence indent/offset are taken from
    ``original`` (``fallback`` when it has none to learn from). When the
    original mixes styles, the lines the edit did not touch are kept
    byte-for-byte rather than normalised. The original's BOM and CRLF
    line ends are carried over.
    """
    text = (original or "").lstrip(_BOM).replace("\r\n", "\n")
    rendered = _render_lf(data, text, fallback)
    if original is not None:
        if original.count("\r\n") * 2 > original.count("\n"):
            rendered = rendered.replace("\n", "\r\n")
        if original.startswith(_BOM):
            rendered = _BOM + rendered
    return rendered


def _render_lf(
    data: Any,  # noqa: ANN401 — ruamel round-trip data is untyped
    text: str,
    fallback: tuple[int, int, int],
) -> str:
    indent = (_detect_indent(text) if text.strip() else None) or fallback

    def dump(value: Any) -> str:  # noqa: ANN401
        yaml = yaml_rt()
        yaml.indent(mapping=indent[0], sequence=indent[1], offset=indent[2])
        buf = io.StringIO()
        yaml.dump(value, buf)
        return buf.getvalue()

    new = dump(data)
    if not text.strip():
        return new
    try:
        baseline = dump(yaml_rt().load(text))
    except YAMLError:
        return new
    if baseline == text:
        return new
    base_lines = baseline.splitlines(keepends=True)
    orig_lines = text.splitlines(keepends=True)
    if len(base_lines) != len(orig_lines):
        return new
    new_lines = new.splitlines(keepends=True)
    out: list[str] = []
    matcher = difflib.SequenceMatcher(None, base_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        out.extend(orig_lines[i1:i2] if tag == "equal" else new_lines[j1:j2])
    return "".join(out)


def load_yaml_mapping(path: Path) -> CommentedMap:
    yaml = yaml_rt()
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.load(handle)
    if not isinstance(data, CommentedMap):
        raise ConfigError(f"setforge.yaml root must be a mapping: {path}")
    return data


def _has_tracked_file_field(data: CommentedMap, field: str) -> bool:
    """Check feature fields on tracked files and bundle file components only."""
    tracked_files = data.get("tracked_files")
    if isinstance(tracked_files, Mapping) and any(
        isinstance(item, Mapping) and field in item for item in tracked_files.values()
    ):
        return True
    bundles = data.get("bundles")
    if not isinstance(bundles, Mapping):
        return False
    for bundle in bundles.values():
        if not isinstance(bundle, Mapping):
            continue
        components = bundle.get("components")
        if not isinstance(components, Sequence):
            continue
        for component in components:
            if not isinstance(component, Mapping):
                continue
            file = component.get("file")
            if isinstance(file, Mapping) and field in file:
                return True
    return False


def rename_key(node: CommentedMap, old: str, new: str) -> None:
    """Rename ``old`` to ``new`` in ``node``, preserving comments + order.

    ruamel.yaml stores comment associations in ``node.ca.items``, keyed by
    the *original* key name. A naive ``node[new] = node.pop(old)`` drops
    the comment-association entry and orphans every nearby comment
    (above-key, end-of-line, below-key). This helper:

    1. Copies ``node.ca.items[old]`` to ``node.ca.items[new]`` BEFORE the
       key is removed (the entries hold the actual comment tokens).
    2. Rotates the internal ``OrderedDict`` so ``new`` lands at the same
       insertion-order slot ``old`` previously occupied.

    Raises ``KeyError`` when ``old`` is absent from ``node``. No-op when
    ``old == new``.
    """
    if old == new:
        return
    if old not in node:
        raise KeyError(f"rename_key: source key not in node: {old!r}")
    # Step 1: migrate comment association BEFORE the key disappears.
    ca_items = node.ca.items
    if old in ca_items:
        ca_items[new] = ca_items.pop(old)
    # Step 2: rebuild OrderedDict in original insertion order with the
    # renamed slot in place. ruamel's CommentedMap is ordered, so we
    # iterate, swap the key, and rewrite. A plain `node[new] = node.pop(
    # old)` would move the entry to the end of the map.
    keys = list(node.keys())
    new_order: list[tuple[str, Any]] = []
    for key in keys:
        if key == old:
            new_order.append((new, node[old]))
        else:
            new_order.append((key, node[key]))
    node.clear()
    for key, value in new_order:
        node[key] = value


def atomic_write_yaml(
    yaml_path: Path,
    data: Any,  # noqa: ANN401 — ruamel round-trip data is untyped
    *,
    fallback: tuple[int, int, int] = _DEFAULT_INDENT,
) -> None:
    """Serialize ``data`` to ``yaml_path`` atomically.

    Serializes through the round-trip YAML config into a string buffer,
    then finalizes via :func:`setforge.atomicio.atomic_write_text`,
    which owns the sibling-tmp + ``os.replace`` dance: crashes between
    open and rename leave only the tmp file, never a half-written
    target. The tmp file's data is fsynced before the rename and the
    destination directory is fsynced (best-effort) after, so the write
    survives power loss, not just a process crash.

    The DESTINATION's permission bits are preserved (``mode=`` computed
    from the existing file): ``mkstemp`` creates the tmp at 0600, so a
    plain replace would silently narrow a group/other-readable config
    to owner-only on every migrate/pin write. New files keep the 0600
    default (``mode=None``).

    ``data`` is the root of a ruamel round-trip document
    (``CommentedMap`` / ``CommentedSeq`` / scalars). Any object the
    round-trip ``YAML.dump`` accepts is accepted here.

    Raises:
        OSError: The tmp-file data fsync (before ``os.replace``) failed
            and propagates by design — swallowing it would report the
            write durable when its bytes never reached disk. The
            perm-preserving ``fchmod`` (whenever the destination already
            exists) propagates the same way. The best-effort parent-dir
            fsync, by contrast, swallows ``OSError``.
    """
    raw = yaml_path.read_bytes().decode("utf-8") if yaml_path.exists() else None
    text = render_yaml(data, raw, fallback=fallback)
    dst_mode = stat.S_IMODE(yaml_path.stat().st_mode) if yaml_path.exists() else None
    atomicio.atomic_write_text(yaml_path, text, mode=dst_mode)
