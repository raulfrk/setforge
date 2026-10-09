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
from ruamel.yaml.error import CommentMark, YAMLError
from ruamel.yaml.tokens import CommentToken

from setforge import atomicio
from setforge.errors import ConfigError, SetforgeError

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
            # ``lc.data`` holds the keys written here; one merged in with
            # ``<<`` has no position of its own.
            for key, position in (node.lc.data or {}).items():
                value, key_col = node[key], position[1]
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
    path: Path | None = None,
) -> str:
    """Dump ``data`` in the indentation style of the ``original`` document.

    The mapping indent and sequence indent/offset are taken from
    ``original`` (``fallback`` when it has none to learn from). When the
    original mixes styles, the lines the edit did not touch are kept
    byte-for-byte rather than normalised, and each new line is indented like
    the entries it joins. The original's BOM and CRLF line ends are carried
    over. An original without a final newline is read as if it had one
    (editors often omit it), so the result gains it.

    A comment that sat between a key and a block value that is now empty is
    moved after the ``{}`` / ``[]`` in ``data`` itself: ruamel would write it
    before the brackets, which does not parse.

    Raises:
        SetforgeError: The rendered text would not parse back to ``data``.
            Every YAML writer renders through here, so none of them can put
            a document on disk that reads as something else. ``path`` names
            the file in the message.
    """
    text = (original or "").lstrip(_BOM).replace("\r\n", "\n")
    if text and not text.endswith("\n"):
        text += "\n"
    # ruamel drops blank lines above the first key; carry them over as they are.
    body = text.lstrip("\n")
    for candidate in _render_lf(data, body, fallback):
        if _reads_back(candidate, data):
            rendered = text[: len(text) - len(body)] + candidate
            break
    else:
        raise SetforgeError(
            f"refusing to write {path or 'the YAML file'}: the edited file would "
            f"not read back as the change that was checked, so nothing was "
            f"written. Edit the file by hand instead."
        )
    if original is not None:
        if original.count("\r\n") * 2 > original.count("\n"):
            rendered = rendered.replace("\n", "\r\n")
        if original.startswith(_BOM):
            rendered = _BOM + rendered
    return rendered


def _plain(obj: Any) -> Any:  # noqa: ANN401 — recursive YAML coercion
    """Reduce round-trip data to plain values that compare by meaning."""
    if isinstance(obj, Mapping):
        return {key: _plain(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(value) for value in obj]
    if isinstance(obj, float) and obj != obj:
        return "<nan>"
    return obj


def _reads_back(text: str, data: Any) -> bool:  # noqa: ANN401
    try:
        return bool(_plain(yaml_rt().load(text)) == _plain(data))
    except YAMLError:
        return False


def _rehome_comments_of_emptied(node: Any) -> None:  # noqa: ANN401
    """Move a comment between a key and its now-empty value after the value."""
    if isinstance(node, CommentedSeq):
        for item in node:
            _rehome_comments_of_emptied(item)
    if not isinstance(node, CommentedMap):
        return
    for key, value in node.items():
        _rehome_comments_of_emptied(value)
        entry = node.ca.items.get(key)
        if (
            not isinstance(value, (CommentedMap, CommentedSeq))
            or value
            or not entry
            or not (entry[2] or entry[3])
        ):
            continue
        own = "".join(
            " " * token.start_mark.column + token.value
            if token.value.strip()
            else token.value
            for token in entry[3] or []
        )
        attached = value.ca.comment
        if attached is not None:
            if attached[0] is not None and attached[0] is not entry[2]:
                # Already placed after the value; it would hide behind ours.
                own += attached[0].value.removeprefix("\n")
            attached[0] = None
            if attached[1] is not None and attached[1] == entry[3]:
                attached[1] = None
        entry[3] = None
        value.fa.set_flow_style()
        if entry[2] is None:
            entry[2] = CommentToken("\n" + own, CommentMark(0))
        else:
            entry[2].value += own


def _render_lf(
    data: Any,  # noqa: ANN401 — ruamel round-trip data is untyped
    text: str,
    fallback: tuple[int, int, int],
) -> list[str]:
    """Return the renderings of ``data`` to try, the most faithful first.

    The last is always the plain dump in the original's first indent style.
    """
    indent = (_detect_indent(text) if text.strip() else None) or fallback

    def dump(value: Any) -> str:  # noqa: ANN401
        yaml = yaml_rt()
        yaml.indent(mapping=indent[0], sequence=indent[1], offset=indent[2])
        buf = io.StringIO()
        yaml.dump(value, buf)
        return buf.getvalue()

    _rehome_comments_of_emptied(data)
    new = dump(data)
    if not text.strip():
        return [new]
    try:
        loaded = yaml_rt().load(text)
    except YAMLError:
        return [new]
    if loaded is None:
        # Only comments and blank lines: keep them above the new content.
        return [text + new, new]
    baseline = dump(loaded)
    if baseline == text:
        return [new]
    spliced = _keep_original_lines(text, baseline, new)
    return [new] if spliced is None else [spliced, new]


def _leading(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _entry_lines(new: str) -> tuple[dict[int, int], dict[int, int | None]] | None:
    """Map each line of the dump ``new`` that starts an entry to its collection.

    Returns ``(owner, holder)``: ``owner[line]`` is the id of the outermost
    block mapping or sequence with a key or item starting on ``line``, and
    ``holder[id]`` is the line of the entry that collection is the value of
    (``None`` for the document root).
    """
    try:
        root = yaml_rt().load(new)
    except YAMLError:
        return None
    lines = new.splitlines()
    owner: dict[int, int] = {}
    holder: dict[int, int | None] = {}

    def walk(node: object, held_by: int | None) -> None:
        if not isinstance(node, (CommentedMap, CommentedSeq)):
            return
        if not node or node.fa.flow_style():
            return
        holder[id(node)] = held_by
        children = node.items() if isinstance(node, CommentedMap) else enumerate(node)
        for key, value in children:
            position = (node.lc.data or {}).get(key)
            if position is None:
                continue
            line = position[0]
            is_dash = lines[line].lstrip(" ").startswith("-")
            if isinstance(node, CommentedMap) or is_dash:
                owner.setdefault(line, id(node))
            walk(value, line if line in owner else held_by)

    walk(root, None)
    return owner, holder


def _keep_original_lines(text: str, baseline: str, new: str) -> str | None:
    """Splice ``new`` into ``text``, keeping the lines the edit did not touch.

    ``baseline`` is the unedited document dumped the way ``new`` was, so a
    line the two share stands for the original line at the same position.
    Every other line of ``new`` is shifted by the difference between the
    original and the dumped indentation of the entries it sits among: its
    collection's other entries, else the entry holding that collection. A
    line that starts no entry moves with the entry above it.
    Returns ``None`` when the lines cannot be paired or shifted.
    """
    base_lines = baseline.splitlines(keepends=True)
    orig_lines = text.splitlines(keepends=True)
    entries = _entry_lines(new)
    if len(base_lines) != len(orig_lines) or entries is None:
        return None
    owner, holder = entries
    new_lines = new.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, base_lines, new_lines, autojunk=False)
    kept = {
        j1 + step: orig_lines[i1 + step]
        for tag, i1, i2, j1, _j2 in matcher.get_opcodes()
        if tag == "equal"
        for step in range(i2 - i1)
    }
    # One kept entry per collection: its entries share a column in both texts.
    peer = {owner[line]: line for line in kept if line in owner}

    def shift_at(at: int | None) -> int:
        while at is not None and at not in kept:
            at = peer.get(owner[at], holder[owner[at]])
        return 0 if at is None else _leading(kept[at]) - _leading(new_lines[at])

    out: list[str] = []
    entry_above: int | None = None
    for line, dumped in enumerate(new_lines):
        if line in owner:
            entry_above = line
        if line in kept or not dumped.strip():
            out.append(kept.get(line, dumped))
            continue
        shift = shift_at(entry_above)
        if _leading(dumped) + shift < 0:
            return None
        out.append(" " * shift + dumped if shift > 0 else dumped[-shift:])
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
    from the existing file): the tmp is created at 0600, so a
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
    text = render_yaml(data, raw, fallback=fallback, path=yaml_path)
    dst_mode = stat.S_IMODE(yaml_path.stat().st_mode) if yaml_path.exists() else None
    atomicio.atomic_write_text(yaml_path, text, mode=dst_mode)
