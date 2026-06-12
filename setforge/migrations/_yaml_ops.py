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

import io
import stat
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from setforge import atomicio

__all__ = ["atomic_write_yaml", "rename_key", "yaml_rt"]


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


def atomic_write_yaml(yaml_path: Path, data: Any) -> None:  # noqa: ANN401 — ruamel round-trip data is untyped
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
    buf = io.StringIO()
    yaml_rt().dump(data, buf)
    dst_mode = stat.S_IMODE(yaml_path.stat().st_mode) if yaml_path.exists() else None
    atomicio.atomic_write_text(yaml_path, buf.getvalue(), mode=dst_mode)
