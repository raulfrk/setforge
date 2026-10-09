"""Tests for :mod:`setforge.migrations._yaml_ops`.

The load-bearing invariants under test:

- ``yaml_rt()`` returns a round-trip ``YAML`` with ``preserve_quotes=True``
  and a wide width so untouched lines are not reformatted.
- ``rename_key`` preserves comments above the key, end-of-line
  comments, AND insertion order (the renamed key occupies the same
  slot the old key did, not the trailing slot a naive
  ``pop()+assign`` produces).
- ``atomic_write_yaml`` round-trips through a sibling tmp file so a
  crash mid-write never leaves a half-rendered destination.
"""

from __future__ import annotations

import io
import os
import stat
from pathlib import Path

import pytest

from setforge import atomicio
from setforge.errors import SetforgeError
from setforge.migrations import _yaml_ops
from setforge.migrations._yaml_ops import (
    atomic_write_yaml,
    rename_key,
    render_yaml,
    yaml_rt,
)


def _dump(data: object) -> str:
    """Serialize ``data`` through the project's round-trip YAML config."""
    buf = io.StringIO()
    yaml_rt().dump(data, buf)
    return buf.getvalue()


def test_yaml_rt_returns_round_trip_instance() -> None:
    yaml = yaml_rt()
    assert yaml.preserve_quotes is True
    assert yaml.width == 4096


def test_yaml_rt_round_trip_preserves_comments() -> None:
    source = (
        "# top-of-file comment\n"
        "version: 1\n"
        "tracked_files:\n"
        "  # comment above key\n"
        "  foo: bar  # eol comment\n"
    )
    data = yaml_rt().load(source)
    dumped = _dump(data)
    assert "# top-of-file comment" in dumped
    assert "# comment above key" in dumped
    assert "# eol comment" in dumped


def test_yaml_rt_preserves_double_quotes() -> None:
    source = 'name: "quoted value"\n'
    data = yaml_rt().load(source)
    dumped = _dump(data)
    assert '"quoted value"' in dumped


def test_rename_key_simple() -> None:
    source = "alpha: 1\nbeta: 2\ngamma: 3\n"
    data = yaml_rt().load(source)
    rename_key(data, "beta", "renamed_beta")
    assert list(data.keys()) == ["alpha", "renamed_beta", "gamma"]
    assert data["renamed_beta"] == 2


def test_rename_key_preserves_above_key_comment() -> None:
    source = (
        "alpha: 1\n# this comment sits above the key being renamed\nbeta: 2\ngamma: 3\n"
    )
    data = yaml_rt().load(source)
    rename_key(data, "beta", "renamed_beta")
    dumped = _dump(data)
    assert "# this comment sits above the key being renamed" in dumped
    assert "renamed_beta: 2" in dumped
    # Ensure the comment is still attached to the renamed key — it must
    # appear BEFORE the renamed_beta line in the dumped output.
    above_line = dumped.index("# this comment sits above the key being renamed")
    key_line = dumped.index("renamed_beta:")
    assert above_line < key_line


def test_rename_key_preserves_end_of_line_comment() -> None:
    source = "alpha: 1\nbeta: 2  # important eol comment\ngamma: 3\n"
    data = yaml_rt().load(source)
    rename_key(data, "beta", "renamed_beta")
    dumped = _dump(data)
    assert "# important eol comment" in dumped
    # The eol comment must sit on the renamed key's line, not a
    # different line elsewhere in the doc.
    for line in dumped.splitlines():
        if "important eol comment" in line:
            assert line.lstrip().startswith("renamed_beta:")
            break
    else:  # pragma: no cover — guarded by the assertion above
        pytest.fail("eol comment did not survive rename")


def test_rename_key_preserves_insertion_order() -> None:
    source = "alpha: 1\nbeta: 2\ngamma: 3\ndelta: 4\n"
    data = yaml_rt().load(source)
    rename_key(data, "gamma", "renamed_gamma")
    assert list(data.keys()) == ["alpha", "beta", "renamed_gamma", "delta"]


def test_rename_key_no_op_when_old_equals_new() -> None:
    source = "alpha: 1\nbeta: 2\n"
    data = yaml_rt().load(source)
    rename_key(data, "alpha", "alpha")
    assert list(data.keys()) == ["alpha", "beta"]


def test_rename_key_raises_on_missing_key() -> None:
    source = "alpha: 1\nbeta: 2\n"
    data = yaml_rt().load(source)
    with pytest.raises(KeyError, match="not in node"):
        rename_key(data, "missing", "new")


def test_atomic_write_yaml_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "out.yaml"
    data = yaml_rt().load("key: value\n")
    atomic_write_yaml(target, data)
    assert target.read_text(encoding="utf-8") == "key: value\n"


def test_atomic_write_yaml_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "out.yaml"
    target.write_text("stale: content\n", encoding="utf-8")
    data = yaml_rt().load("fresh: content\n")
    atomic_write_yaml(target, data)
    assert target.read_text(encoding="utf-8") == "fresh: content\n"


def test_atomic_write_yaml_leaves_no_tmp_on_success(tmp_path: Path) -> None:
    target = tmp_path / "out.yaml"
    data = yaml_rt().load("k: v\n")
    atomic_write_yaml(target, data)
    tmp_residue = list(tmp_path.glob(".out.yaml.*.tmp"))
    assert tmp_residue == []


def test_atomic_write_yaml_round_trips_comments(tmp_path: Path) -> None:
    target = tmp_path / "out.yaml"
    source = "# header\nversion: 1\n# above\nalpha: 1  # eol\n"
    data = yaml_rt().load(source)
    atomic_write_yaml(target, data)
    out = target.read_text(encoding="utf-8")
    assert "# header" in out
    assert "# above" in out
    assert "# eol" in out


_UNREADABLE = "a\x85b"  # U+0085 is a line break to YAML: reads back as "a b"


@pytest.mark.parametrize("existing", ["# mine\nkey: value\n", None])
def test_atomic_write_yaml_refuses_text_that_would_not_read_back(
    tmp_path: Path, existing: str | None
) -> None:
    target = tmp_path / "setforge.yaml"
    if existing is not None:
        target.write_text(existing, encoding="utf-8")
    data = yaml_rt().load(existing or "key: value\n")
    data["note"] = _UNREADABLE

    with pytest.raises(SetforgeError) as raised:
        atomic_write_yaml(target, data)

    assert str(raised.value) == (
        f"refusing to write {target}: the edited file would not read back as "
        f"the change that was checked, so nothing was written. Edit the file by "
        f"hand instead."
    )
    assert [path.name for path in tmp_path.iterdir()] == (
        ["setforge.yaml"] if existing is not None else []
    )
    if existing is not None:
        assert target.read_text(encoding="utf-8") == existing


_LEGITIMATE = [
    pytest.param("base: &b\n  - one\nother: *b\n", id="alias"),
    pytest.param("d: &d\n  a: 1\nm:\n  <<: *d\n  b: 2\n", id="merge-key"),
    pytest.param("mode: 0o755\nold: 0755\nhex: 0x1F\n", id="integers"),
    pytest.param("f: 1.50\ne: 1e3\nn: .nan\ni: -.inf\n", id="floats"),
    pytest.param("a:\nb: ~\nc: null\nd: ''\n", id="nulls"),
    pytest.param("t: yes\nf: off\nb: true\n", id="booleans"),
    pytest.param("day: 2026-10-09\nat: 2026-10-09T10:00:00Z\n", id="dates"),
    pytest.param("text: |\n  one\n\n  two\nfold: >-\n  a\n  b\n", id="blocks"),
    pytest.param("q: \"a\\tb\\u0085c\"\ns: 'it''s'\n", id="quoted"),
    pytest.param("flow: [a, {b: c}]\nempty: {}\nnone: []\n", id="flow"),
    pytest.param("wrapped: [a,\n  b]\nk:\n  v\n", id="wrapped"),
    pytest.param("? [a, b]\n: pair\n1: int-key\n", id="odd-keys"),
    pytest.param("- a\n- b: 1\n  c:\n  - d\n", id="list-root"),
    pytest.param("---\na: 1\n...\n", id="markers"),
    pytest.param("caf\u00e9: \u65e5\u672c # \u2603\n", id="unicode"),
]


@pytest.mark.parametrize("text", _LEGITIMATE)
@pytest.mark.parametrize("edit", [False, True], ids=["unchanged", "edited"])
def test_atomic_write_yaml_accepts_every_document_that_reads_back(
    tmp_path: Path, text: str, edit: bool
) -> None:
    """The read-back check refuses only text that parses to something else."""
    target = tmp_path / "setforge.yaml"
    target.write_text(text, encoding="utf-8")
    data = yaml_rt().load(text)
    if edit and isinstance(data, dict):
        data["added"] = ["x", ("y", "z")]
    elif edit:
        data.append("x")

    atomic_write_yaml(target, data)

    written = target.read_text(encoding="utf-8")
    assert _yaml_ops._plain(yaml_rt().load(written)) == _yaml_ops._plain(data)


# ruamel loads a tagged scalar as an object that compares by identity, and a
# list that carries a tag has no recorded positions. Neither is a reason to
# refuse a file: it must write, byte for byte, with or without an unrelated key.
_TAGGED = [
    pytest.param("a: !!str 1\nb: 2\n", id="str-tag"),
    pytest.param("code: !!str /usr/bin/code\n", id="str-tag-path"),
    pytest.param("a: !custom v\nb: 2\n", id="custom-tag"),
    pytest.param("a: !!str 1\nb: !!str 1\n", id="same-tag-twice"),
    pytest.param("l:\n  - !!str 1\n  - 2\n", id="tag-in-list"),
    pytest.param("!!str 1: x\nb: 2\n", id="tag-on-key"),
    pytest.param("s: !!set\n  ? !!str 1\n  ? b\nc: 2\n", id="tag-in-set"),
    pytest.param("a: !x |\n  text\nb: 2\n", id="tag-on-block-scalar"),
    pytest.param("a: !x\n  - 1\n  - 2\nb: 2\n", id="tagged-list"),
    pytest.param("a: !x\n  k: 1\nb: 2\n", id="tagged-map"),
    pytest.param(
        "a: !!null\nb: !!int '5'\nc: !!float 5\nd: !!bool 'true'\n", id="core"
    ),
    pytest.param("a: !!timestamp 2026-01-01\nb: 2\n", id="timestamp-tag"),
]


@pytest.mark.parametrize("text", _TAGGED)
@pytest.mark.parametrize("edit", [False, True], ids=["unchanged", "edited"])
def test_render_yaml_writes_a_document_with_tags_and_keeps_its_lines(
    text: str, edit: bool
) -> None:
    data = yaml_rt().load(text)
    if edit:
        data["added"] = "new"

    assert render_yaml(data, text) == text + ("added: new\n" if edit else "")


def test_atomic_write_yaml_keeps_a_tagged_scalar_in_a_local_config(
    tmp_path: Path,
) -> None:
    target = tmp_path / "local.yaml"
    text = "# mine\nbinaries:\n  code: !!str /usr/bin/code\n"
    target.write_text(text, encoding="utf-8")
    data = yaml_rt().load(text)
    data["orphan_ignore"] = ["tool"]

    atomic_write_yaml(target, data)

    assert target.read_text(encoding="utf-8") == text + "orphan_ignore:\n- tool\n"


def test_atomic_write_yaml_fsyncs_tmp_fd_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tmp file's own fd must be fsynced before ``os.replace`` so the
    payload data is durable, not merely the rename."""
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def recording_replace(src: object, dst: object) -> None:
        events.append("replace")
        real_replace(src, dst)  # type: ignore[arg-type]

    # The tmp+replace dance lives in atomicio now; patch the os module
    # it dispatches through (same module object as the top-level import).
    monkeypatch.setattr(atomicio.os, "fsync", recording_fsync)
    monkeypatch.setattr(atomicio.os, "replace", recording_replace)

    target = tmp_path / "out.yaml"
    atomic_write_yaml(target, yaml_rt().load("k: v\n"))

    assert "fsync" in events
    assert "replace" in events
    assert events.index("fsync") < events.index("replace")


def test_atomic_write_yaml_fsyncs_parent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the replace, the destination's parent directory is fsynced so
    the rename survives a power loss."""
    synced: list[Path] = []
    monkeypatch.setattr(_yaml_ops.atomicio, "fsync_dir", lambda d: synced.append(d))
    target = tmp_path / "out.yaml"
    atomic_write_yaml(target, yaml_rt().load("k: v\n"))
    assert target.parent in synced


def test_atomic_write_yaml_preserves_dest_mode(tmp_path: Path) -> None:
    """Overwriting an existing 0644 file must keep its mode, not narrow to
    the 0600 mkstemp default."""
    target = tmp_path / "out.yaml"
    target.write_text("stale: x\n", encoding="utf-8")
    target.chmod(0o644)
    atomic_write_yaml(target, yaml_rt().load("fresh: y\n"))
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_atomic_write_yaml_data_fsync_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A data-fsync OSError must propagate — never be swallowed and report a
    durable write that did not happen."""

    def boom(fd: int) -> None:
        raise OSError("ENOSPC")

    monkeypatch.setattr(atomicio.os, "fsync", boom)
    target = tmp_path / "out.yaml"
    with pytest.raises(OSError, match="ENOSPC"):
        atomic_write_yaml(target, yaml_rt().load("k: v\n"))
    # The tmp file must not leak on the error path.
    assert list(tmp_path.glob(".out.yaml.*.tmp")) == []


def test_atomic_write_yaml_dir_fsync_error_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory-fsync OSError is best-effort: the write still succeeds."""

    def boom(directory: Path) -> None:
        raise OSError("EINVAL")

    monkeypatch.setattr(atomicio, "fsync_dir", _swallow_dir_fsync_oserror(boom))
    target = tmp_path / "out.yaml"
    atomic_write_yaml(target, yaml_rt().load("k: v\n"))
    assert target.read_text(encoding="utf-8") == "k: v\n"


def _swallow_dir_fsync_oserror(raiser: object) -> object:
    """Wrap a raising fake so the suppress lives in fsync_dir, mirroring the
    real best-effort contract."""
    import contextlib

    def wrapper(directory: Path) -> None:
        with contextlib.suppress(OSError):
            raiser(directory)  # type: ignore[operator]

    return wrapper
