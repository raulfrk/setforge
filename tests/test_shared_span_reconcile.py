"""Unit tests for the shared-span reconcile surface (intent-collision model).

A *shared* span carries pure INTENT (anchor / kind / semantics) in the
tracked ``setforge.yaml`` ``tracked_files.<id>.spans`` block — it has NO
tracked body, so the section-reconcile 3-way-over-body does not apply.
The only thing to reconcile is an *intent collision*: a host-local span
in ``local.yaml`` that shadows a shared span on the SAME anchor.

Two layers are exercised here:

- the pure :func:`setforge.config.detect_shared_span_collisions` detector
  plus the ``prefer_shared_anchors`` knob on
  :func:`setforge.config.apply_host_local_tracked_file_overrides`;
- the install-CLI surface that gates the collision to
  ``--reconcile-user-sections`` (B-R6), routes ``--auto`` (B-R7 risk line
  / keep-live), and raises a require-interactive error on a non-tty
  bare-flag run (B-R8).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app
from setforge.config import (
    Config,
    SharedSpanCollision,
    apply_host_local_tracked_file_overrides,
    detect_shared_span_collisions,
    load_config,
)
from setforge.errors import SharedSpanReconcileRequiresInteractive
from setforge.spans import SpanEntry, SpanKind, SpanSemantics

# --------------------------------------------------------------------------- #
# setforge.yaml builders                                                       #
# --------------------------------------------------------------------------- #

_DOC = """# Doc

## Pinned

Pinned body original.

## Other

Other body original.
"""


def _md_config_body(*, shared_anchor: str = "## Pinned") -> str:
    """A setforge.yaml declaring one markdown tracked_file with a shared span."""
    return (
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.setforge_sshare/doc.md\n"
        "    disposition: shared\n"
        "    spans:\n"
        f'      - anchor: "{shared_anchor}"\n'
        "        kind: pinned\n"
        "        semantics: shared\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files:\n"
        "      - doc\n"
    )


def _structural_config_body(*, shared_anchor: str = "editor.fontSize") -> str:
    """A setforge.yaml declaring one structural tracked_file with a shared span."""
    return (
        "version: 1\n"
        "tracked_files:\n"
        "  conf:\n"
        "    src: conf.yaml\n"
        "    dst: ~/.setforge_sshare/conf.yaml\n"
        "    disposition: shared\n"
        "    spans:\n"
        f'      - anchor: "{shared_anchor}"\n'
        "        kind: pinned\n"
        "        semantics: shared\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files:\n"
        "      - conf\n"
    )


def _write_md_repo(tmp_path: Path, *, body: str | None = None) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body or _md_config_body(), encoding="utf-8")
    tracked = tmp_path / "tracked"
    tracked.mkdir(exist_ok=True)
    (tracked / "doc.md").write_text(_DOC, encoding="utf-8")
    return cfg


def _write_structural_repo(tmp_path: Path, *, body: str | None = None) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body or _structural_config_body(), encoding="utf-8")
    tracked = tmp_path / "tracked"
    tracked.mkdir(exist_ok=True)
    (tracked / "conf.yaml").write_text(
        "editor:\n  fontSize: 12\n  tabSize: 2\n", encoding="utf-8"
    )
    return cfg


def _write_local(tmp_path: Path, body: str) -> Path:
    local = tmp_path / "local.yaml"
    local.write_text(body, encoding="utf-8")
    return local


# --------------------------------------------------------------------------- #
# detect_shared_span_collisions — pure detector                               #
# --------------------------------------------------------------------------- #


def test_no_local_span_means_no_collision(tmp_path: Path) -> None:
    """A shared span with no host-local counterpart surfaces no collision."""
    cfg = load_config(_write_md_repo(tmp_path))
    local = tmp_path / "local.yaml"  # absent on purpose
    collisions = detect_shared_span_collisions(cfg, local_config_path=local)
    assert collisions == []


def test_host_local_only_span_is_not_a_collision(tmp_path: Path) -> None:
    """A host-local span on an anchor with NO shared counterpart is untouched."""
    cfg = load_config(_write_md_repo(tmp_path))
    local = _write_local(
        tmp_path,
        "tracked_files:\n"
        "  doc:\n"
        "    spans:\n"
        '      - anchor: "## Other"\n'
        "        kind: pinned\n"
        "        semantics: host-local\n",
    )
    collisions = detect_shared_span_collisions(cfg, local_config_path=local)
    assert collisions == []


def test_same_anchor_collision_is_detected(tmp_path: Path) -> None:
    """A host-local span on the SAME anchor as a shared span is a collision."""
    cfg = load_config(_write_md_repo(tmp_path))
    local = _write_local(
        tmp_path,
        "tracked_files:\n"
        "  doc:\n"
        "    spans:\n"
        '      - anchor: "## Pinned"\n'
        "        kind: forked\n"
        "        semantics: host-local\n",
    )
    collisions = detect_shared_span_collisions(cfg, local_config_path=local)
    assert collisions == [
        SharedSpanCollision(tracked_file_id="doc", anchor="## Pinned")
    ]


def test_structural_collision_is_detected(tmp_path: Path) -> None:
    """Collision detection works for structural (dotted-path) shared spans too."""
    cfg = load_config(_write_structural_repo(tmp_path))
    local = _write_local(
        tmp_path,
        "tracked_files:\n"
        "  conf:\n"
        "    spans:\n"
        '      - anchor: "editor.fontSize"\n'
        "        kind: forked\n"
        "        semantics: host-local\n",
    )
    collisions = detect_shared_span_collisions(cfg, local_config_path=local)
    assert collisions == [
        SharedSpanCollision(tracked_file_id="conf", anchor="editor.fontSize")
    ]


def test_local_span_shadowing_a_local_span_is_not_a_collision(tmp_path: Path) -> None:
    """A host-local span shadowing a host-local (not shared) span is no collision.

    The shared-span reconcile path only cares about host-local-vs-SHARED
    anchor collisions; two host-local declarations are a config dup, not a
    cross-repo intent collision.
    """
    body = _md_config_body().replace("semantics: shared", "semantics: host-local")
    cfg = load_config(_write_md_repo(tmp_path, body=body))
    local = _write_local(
        tmp_path,
        "tracked_files:\n"
        "  doc:\n"
        "    spans:\n"
        '      - anchor: "## Pinned"\n'
        "        kind: forked\n"
        "        semantics: host-local\n",
    )
    collisions = detect_shared_span_collisions(cfg, local_config_path=local)
    assert collisions == []


# --------------------------------------------------------------------------- #
# apply_host_local_tracked_file_overrides — fold direction                    #
# --------------------------------------------------------------------------- #


def _resolved_span(cfg: Config, anchor: str) -> SpanEntry:
    return next(s for s in cfg.tracked_files["doc"].spans if s.anchor == anchor)


def test_fold_keeps_host_local_per_anchor_by_default(tmp_path: Path) -> None:
    """Default fold: the host-local span wins the collided anchor (status quo)."""
    cfg = load_config(_write_md_repo(tmp_path))
    local = _write_local(
        tmp_path,
        "tracked_files:\n"
        "  doc:\n"
        "    spans:\n"
        '      - anchor: "## Pinned"\n'
        "        kind: forked\n"
        "        semantics: host-local\n",
    )
    apply_host_local_tracked_file_overrides(cfg, local_config_path=local)
    span = _resolved_span(cfg, "## Pinned")
    assert span.kind is SpanKind.FORKED
    assert span.semantics is SpanSemantics.HOST_LOCAL


def test_prefer_shared_anchor_lets_shared_win_the_collision(tmp_path: Path) -> None:
    """``prefer_shared_anchors`` flips the collided anchor to the shared intent."""
    cfg = load_config(_write_md_repo(tmp_path))
    local = _write_local(
        tmp_path,
        "tracked_files:\n"
        "  doc:\n"
        "    spans:\n"
        '      - anchor: "## Pinned"\n'
        "        kind: forked\n"
        "        semantics: host-local\n",
    )
    apply_host_local_tracked_file_overrides(
        cfg,
        local_config_path=local,
        prefer_shared_anchors=frozenset({("doc", "## Pinned")}),
    )
    span = _resolved_span(cfg, "## Pinned")
    assert span.kind is SpanKind.PINNED
    assert span.semantics is SpanSemantics.SHARED


def test_prefer_shared_anchor_leaves_non_collided_host_local_span(
    tmp_path: Path,
) -> None:
    """A host-local-only span is never flipped, even with prefer_shared set."""
    cfg = load_config(_write_md_repo(tmp_path))
    local = _write_local(
        tmp_path,
        "tracked_files:\n"
        "  doc:\n"
        "    spans:\n"
        '      - anchor: "## Pinned"\n'
        "        kind: forked\n"
        "        semantics: host-local\n"
        '      - anchor: "## Other"\n'
        "        kind: pinned\n"
        "        semantics: host-local\n",
    )
    apply_host_local_tracked_file_overrides(
        cfg,
        local_config_path=local,
        prefer_shared_anchors=frozenset({("doc", "## Pinned")}),
    )
    other = _resolved_span(cfg, "## Other")
    assert other.semantics is SpanSemantics.HOST_LOCAL


# --------------------------------------------------------------------------- #
# install CLI surface — B-R6 / B-R7 / B-R8                                     #
# --------------------------------------------------------------------------- #

_INSTALL_BASE = [
    "--no-transition",
    "--no-secrets-scan",
    "--no-git-check",
]


def _install(config: Path, *, extra: list[str]) -> Result:
    return CliRunner().invoke(
        app,
        ["install", "--profile=p", f"--config={config}", *_INSTALL_BASE, *extra],
    )


def _seed_collision_local(tmp_path: Path) -> Path:
    return _write_local(
        tmp_path,
        "tracked_files:\n"
        "  doc:\n"
        "    spans:\n"
        '      - anchor: "## Pinned"\n'
        "        kind: forked\n"
        "        semantics: host-local\n",
    )


def test_bare_install_does_not_nag_on_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-R6: a bare install keeps silent host-local-wins; no collision warning."""
    config = _write_md_repo(tmp_path)
    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", tmp_path / "local.yaml")
    _seed_collision_local(tmp_path)
    result = _install(config, extra=["--yes"])
    assert result.exit_code == 0, result.output
    assert "host-local span" not in result.output
    assert "## Pinned" not in result.output


def test_no_collision_install_surfaces_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared span with no host-local span applies with no reconcile surface."""
    config = _write_md_repo(tmp_path)
    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", tmp_path / "local.yaml")
    result = _install(config, extra=["--reconcile-user-sections"])
    assert result.exit_code == 0, result.output
    assert "host-local span" not in result.output


def test_auto_use_tracked_surfaces_risk_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-R7: --auto=use-tracked adopts shared intent and surfaces a risk line."""
    config = _write_md_repo(tmp_path)
    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", tmp_path / "local.yaml")
    _seed_collision_local(tmp_path)
    result = _install(config, extra=["--auto=use-tracked", "--yes"])
    assert result.exit_code == 0, result.output
    assert "host-local span" in result.output
    assert "## Pinned" in result.output
    assert "overwritten" in result.output


def test_auto_keep_live_keeps_host_local_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--auto=keep-live keeps the host-local override; no overwrite risk line."""
    config = _write_md_repo(tmp_path)
    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", tmp_path / "local.yaml")
    _seed_collision_local(tmp_path)
    result = _install(config, extra=["--auto=keep-live", "--yes"])
    assert result.exit_code == 0, result.output
    assert "overwritten" not in result.output


def test_non_tty_reconcile_without_auto_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-R8: --reconcile-user-sections, no --auto, non-tty ⇒ require-interactive."""
    config = _write_md_repo(tmp_path)
    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", tmp_path / "local.yaml")
    _seed_collision_local(tmp_path)
    # CliRunner stdin is not a tty, so the interactive gate cannot prompt.
    result = _install(config, extra=["--reconcile-user-sections"])
    assert result.exit_code != 0
    assert isinstance(result.exception, SharedSpanReconcileRequiresInteractive) or (
        "requires" in result.output.lower()
    )
