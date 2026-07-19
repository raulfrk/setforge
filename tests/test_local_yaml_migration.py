"""local.yaml schema versioning + retired-key tolerance ordering.

Covers the dedicated local.yaml migration module
(:mod:`setforge.migrations._local_yaml`) and its wiring into the runtime
load path (:func:`setforge.source._load_local_source_config`) and the
``validate`` CLI:

- ``LocalConfig`` carries a ``schema_version`` field defaulting to the
  local baseline ``"1.0"``.
- ``detect_local_yaml_schema`` reads the version via a RAW ruamel read
  (never through the ``extra="forbid"`` model), defaulting to the
  baseline on an absent file / absent key.
- ``migrate_local_yaml`` wraps the host_local_sections → spans rewrite,
  version-gated and idempotent.
- detect-before-validate: a retired-key (``host_local_sections``)
  local.yaml migrates cleanly on load — no ``extra_forbidden`` refusal.
- a newer-MAJOR local.yaml refuses cleanly (``ConfigError`` →
  one-line "upgrade setforge" message + nonzero CLI exit, no traceback);
  a malformed version → ``ConfigError``.
- set-delta overlays (plugins / extensions / marketplaces) survive a
  baseline 1.0 load unchanged (the set-delta home design decision:
  these stay in local.yaml and are versioned in place).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from setforge.errors import ConfigError
from setforge.local_config import LocalConfig
from setforge.migrations._local_yaml import (
    LOCAL_YAML_BASELINE_VERSION,
    detect_local_yaml_schema,
    guard_local_yaml_schema,
    migrate_local_yaml,
    strip_retired_keys,
)

# A local.yaml carrying the retired ``host_local_sections`` block — the
# shape that, absent detect-before-validate, would trip the
# ``extra="forbid"`` model with an ``extra_forbidden`` error.
_RETIRED_KEY_LOCAL = """\
tracked_files:
  claude_md:
    host_local_sections:
      my-notes:
        anchor:
          kind: after-heading
          value: "Notes"
        body: |
          host-only body
"""

# A local.yaml carrying only set-delta overlays (set-delta overlays stay
# in local.yaml and are versioned in place — NOT on the retired-key list).
_SET_DELTA_LOCAL = """\
plugins:
  add:
    - example-plugin@example-market
extensions:
  add:
    - ms-python.python
marketplaces:
  add:
    example-market:
      source: github
      repo: example/market
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --- Task 1: schema_version field + raw-detect helper -------------------


def test_local_config_accepts_schema_version() -> None:
    """``LocalConfig`` validates an explicit ``schema_version`` key."""
    cfg = LocalConfig.model_validate({"schema_version": "1.0"})
    assert cfg.schema_version == "1.0"


def test_local_config_schema_version_defaults_to_baseline() -> None:
    """The field defaults to the local baseline when absent."""
    cfg = LocalConfig.model_validate({})
    assert cfg.schema_version == LOCAL_YAML_BASELINE_VERSION


def test_detect_absent_file_returns_baseline(tmp_path: Path) -> None:
    """A missing local.yaml detects as the baseline version."""
    assert detect_local_yaml_schema(tmp_path / "missing.yaml") == (
        LOCAL_YAML_BASELINE_VERSION
    )


def test_detect_missing_key_returns_baseline(tmp_path: Path) -> None:
    """A local.yaml with no ``schema_version`` key detects as baseline."""
    path = _write(tmp_path / "local.yaml", _RETIRED_KEY_LOCAL)
    assert detect_local_yaml_schema(path) == LOCAL_YAML_BASELINE_VERSION


def test_detect_reads_declared_version(tmp_path: Path) -> None:
    """A declared ``schema_version`` is read via the raw read."""
    path = _write(tmp_path / "local.yaml", "schema_version: '1.0'\n")
    assert detect_local_yaml_schema(path) == "1.0"


def test_detect_reads_version_through_forbidden_shape(tmp_path: Path) -> None:
    """Detection works even when the doc would fail the forbid-model.

    A doc carrying an UNKNOWN top-level key (which ``extra="forbid"``
    rejects) must still be readable for its declared version — proving
    detection does not route through the strict model.
    """
    doc = "schema_version: '2.0'\nfuture_only_key: present\n"
    # The strict model rejects the unknown key outright...
    with pytest.raises(ValidationError):
        LocalConfig.model_validate({"future_only_key": "present"})
    # ...yet raw detection still reads the declared version.
    path = _write(tmp_path / "local.yaml", doc)
    assert detect_local_yaml_schema(path) == "2.0"


def test_detect_malformed_yaml_raises_config_error(tmp_path: Path) -> None:
    """A non-well-formed local.yaml raises ConfigError naming the file.

    The raw ``yaml.load`` used to propagate a ruamel ``YAMLError`` unwrapped
    (the docstring even admitted it), diverging from the ``_require_mapping_root``
    trust boundary applied on the next line. The load is now wrapped so a
    malformed doc surfaces as a domain ``ConfigError``.
    """
    path = _write(tmp_path / "local.yaml", "key: [unclosed\n  nested: : :\n")
    with pytest.raises(ConfigError, match=str(path)):
        detect_local_yaml_schema(path)


def test_detect_non_utf8_raises_config_error(tmp_path: Path) -> None:
    """A non-UTF-8 local.yaml raises ConfigError, not a raw UnicodeDecodeError.

    The ``.open(encoding="utf-8")`` decode raises ``UnicodeDecodeError`` BEFORE
    ruamel sees the bytes, so wrapping only ``YAMLError`` left the raw builtin
    to bubble — the same trust-boundary gap as its ``setforge.yaml`` sibling.
    """
    path = tmp_path / "local.yaml"
    path.write_bytes(b"schema_version: \xff\xfe 2.0\n")
    with pytest.raises(ConfigError, match=str(path)):
        detect_local_yaml_schema(path)


# --- Task 2: version-gated migration wrapper ---------------------------


def test_migrate_rewrites_retired_key(tmp_path: Path) -> None:
    """A ``host_local_sections`` local.yaml migrates to spans on disk."""
    path = _write(tmp_path / "local.yaml", _RETIRED_KEY_LOCAL)
    result = migrate_local_yaml(path)
    assert result.migrated is True
    reloaded = YAML(typ="rt").load(path.read_text(encoding="utf-8"))
    tracked = reloaded["tracked_files"]["claude_md"]
    assert "host_local_sections" not in tracked
    assert "spans" in tracked


def test_migrate_clean_file_is_noop(tmp_path: Path) -> None:
    """A file with no retired key is left untouched (idempotent)."""
    path = _write(tmp_path / "local.yaml", _SET_DELTA_LOCAL)
    before = path.read_text(encoding="utf-8")
    result = migrate_local_yaml(path)
    assert result.migrated is False
    assert path.read_text(encoding="utf-8") == before


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    """Re-running migrate after a real migration converges to no-op."""
    path = _write(tmp_path / "local.yaml", _RETIRED_KEY_LOCAL)
    assert migrate_local_yaml(path).migrated is True
    assert migrate_local_yaml(path).migrated is False


# --- Task 3: detect-before-validate hook in the load path --------------


def test_load_path_tolerates_retired_key(tmp_path: Path) -> None:
    """Loading a retired-key local.yaml does not raise (in-memory relocate).

    The runtime loader funnels through ``_load_local_source_config``; the
    detect→guard→relocate hook converts the legacy shape IN MEMORY BEFORE
    the ``extra="forbid"`` model sees it — without ``extra_forbidden``.
    """
    from setforge.source import _load_local_source_config

    path = _write(tmp_path / "local.yaml", _RETIRED_KEY_LOCAL)
    cfg = _load_local_source_config(path)
    assert "claude_md" in cfg.tracked_files


def test_load_path_does_not_rewrite_disk(tmp_path: Path) -> None:
    """The load path leaves local.yaml on disk untouched (no race w/ revert).

    The on-disk ``host_local_sections`` → spans rewrite is owned by the
    install path's snapshot-aware step; the load path must NOT mutate the
    file, else the install snapshot would capture already-migrated bytes
    and break byte-exact ``revert``.
    """
    from setforge.source import _load_local_source_config

    path = _write(tmp_path / "local.yaml", _RETIRED_KEY_LOCAL)
    before = path.read_bytes()
    _load_local_source_config(path)
    assert path.read_bytes() == before


def test_strip_retired_keys_in_memory() -> None:
    """``strip_retired_keys`` drops the retired keys from the parsed mapping.

    The span-declaration surface is retired: both ``host_local_sections``
    AND ``spans`` are removed in place (not relocated), so the strict
    overlay model — which declares neither field — accepts the document.
    """
    data = YAML(typ="safe").load(_RETIRED_KEY_LOCAL)
    assert strip_retired_keys(data) is True
    tracked = data["tracked_files"]["claude_md"]
    assert "host_local_sections" not in tracked
    assert "spans" not in tracked


def test_strip_removes_overlay_spans() -> None:
    """A pre-4.0 doc carrying OVERLAY ``spans`` has them stripped too."""
    doc = """\
tracked_files:
  claude_md:
    spans:
      - anchor: my-notes
        kind: overlay
        semantics: host-local
        overlay:
          anchor:
            kind: after-heading
            value: Notes
          body: host-only body
"""
    data = YAML(typ="safe").load(doc)
    assert strip_retired_keys(data) is True
    assert "spans" not in data["tracked_files"]["claude_md"]


def test_strip_clean_mapping_is_noop() -> None:
    """A mapping with no retired key is reported unchanged."""
    data = YAML(typ="safe").load(_SET_DELTA_LOCAL)
    assert strip_retired_keys(data) is False


def test_load_path_set_delta_survives_baseline_load(tmp_path: Path) -> None:
    """Set-delta overlays resolve unchanged on a baseline 1.0 load."""
    from setforge.source import _load_local_source_config

    path = _write(tmp_path / "local.yaml", _SET_DELTA_LOCAL)
    cfg = _load_local_source_config(path)
    assert "ms-python.python" in cfg.extensions.add
    # No spurious rewrite of a clean set-delta file.
    assert "host_local_sections" not in path.read_text(encoding="utf-8")


# --- Task 4: cross-major refuse guard + malformed → ConfigError --------


def test_guard_refuses_newer_major(tmp_path: Path) -> None:
    """A newer-MAJOR local.yaml refuses with an upgrade message."""
    path = tmp_path / "local.yaml"
    with pytest.raises(ConfigError, match="upgrade setforge"):
        guard_local_yaml_schema({"schema_version": "2.0"}, path)


def test_guard_allows_baseline(tmp_path: Path) -> None:
    """A baseline-major local.yaml passes the guard."""
    guard_local_yaml_schema({"schema_version": "1.0"}, tmp_path / "local.yaml")
    guard_local_yaml_schema({}, tmp_path / "local.yaml")


def test_guard_malformed_version_raises_config_error(tmp_path: Path) -> None:
    """A malformed ``schema_version`` raises ConfigError, not ValueError."""
    path = tmp_path / "local.yaml"
    with pytest.raises(ConfigError):
        guard_local_yaml_schema({"schema_version": "v2"}, path)


def test_load_path_refuses_newer_major(tmp_path: Path) -> None:
    """The load path raises ConfigError on a newer-major local.yaml."""
    from setforge.source import _load_local_source_config

    path = _write(tmp_path / "local.yaml", "schema_version: '2.0'\nsource: null\n")
    with pytest.raises(ConfigError, match="upgrade setforge"):
        _load_local_source_config(path)


def test_cli_validate_refuses_newer_major_clean(tmp_path: Path) -> None:
    """A newer-major local.yaml gives a nonzero exit, no traceback."""
    import setforge.cli.validate as validate_mod

    failures: list[object] = []
    path = _write(tmp_path / "local.yaml", "schema_version: '2.0'\n")
    with pytest.raises(ConfigError, match="upgrade setforge"):
        validate_mod._check_local_yaml(path, failures)  # type: ignore[arg-type]
