"""Tests for the atomic deploy primitive."""

import hashlib
import os
import stat
from pathlib import Path
from typing import Any, NoReturn
from unittest import mock

import pytest

from my_setup import sections
from my_setup.config import Config, Dotfile, Profile, resolve_profile
from my_setup.deploy import (
    DeployAction,
    DeployResult,
    bootstrap_local,
    copy_atomic,
    validate_srcs_exist,
)
from my_setup.errors import MergeTypeMismatch, MissingTrackedFile
from my_setup.sections import (
    detect_legacy_markers,
    extract_marker_hashes,
    hash_sections,
)


def test_fresh_deploy_creates_dst(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("hello\n")
    dst = tmp_path / "out" / "dst"
    result = copy_atomic(src, dst)
    assert isinstance(result, DeployResult)
    assert result.action is DeployAction.CREATED
    assert result.backup_path is None
    assert dst.read_text() == "hello\n"


def test_redeploy_with_backup(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("new\n")
    dst = tmp_path / "dst"
    dst.write_text("old\n")
    result = copy_atomic(src, dst)
    assert result.action is DeployAction.UPDATED
    assert result.backup_path == Path(str(dst) + ".bak")
    assert result.backup_path.read_text() == "old\n"
    assert dst.read_text() == "new\n"


def test_redeploy_overwrites_existing_backup(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("v3\n")
    dst = tmp_path / "dst"
    dst.write_text("v2\n")
    bak = Path(str(dst) + ".bak")
    bak.write_text("v1\n")
    result = copy_atomic(src, dst)
    assert result.action is DeployAction.UPDATED
    assert bak.read_text() == "v2\n"
    assert dst.read_text() == "v3\n"


def test_redeploy_without_backup(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("new\n")
    dst = tmp_path / "dst"
    dst.write_text("old\n")
    result = copy_atomic(src, dst, backup=False)
    assert result.backup_path is None
    assert not Path(str(dst) + ".bak").exists()


def test_identical_content_is_noop(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("same\n")
    dst = tmp_path / "dst"
    dst.write_text("same\n")
    result = copy_atomic(src, dst)
    assert result.action is DeployAction.NOOP
    assert result.backup_path is None
    assert not Path(str(dst) + ".bak").exists()


def test_markdown_user_section_preserved(tmp_path: Path) -> None:
    src = tmp_path / "src.md"
    # Tracked side ships with a hash-stamped end marker (post-9by canonical).
    src.write_text(
        "header\n"
        "<!-- my-setup:user-section start host-local -->\n"
        f"<!-- my-setup:user-section end host-local hash={'a' * 64} -->\n"
        "footer\n"
    )
    dst = tmp_path / "dst.md"
    # Live side is hashless (legacy live); install's allow_legacy=True
    # tolerates it and rewrites the marker on write.
    dst.write_text(
        "old header\n"
        "<!-- my-setup:user-section start host-local -->\n"
        "USER CONTENT\n"
        f"<!-- my-setup:user-section end host-local hash={'a' * 64} -->\n"
        "old footer\n"
    )
    copy_atomic(src, dst, preserve_user_sections=True)
    final = dst.read_text()
    assert "header\n" in final
    assert "USER CONTENT\n" in final
    assert "footer\n" in final


def test_copy_atomic_precomputed_live_sections_skips_reparse(
    tmp_path: Path,
) -> None:
    """When ``precomputed_live_sections`` is supplied, ``_compute_content``
    must skip the re-read for content extraction; only the diff-check read
    in ``copy_atomic`` remains.
    """
    src = tmp_path / "src.md"
    src.write_text(
        "header\n"
        "<!-- my-setup:user-section start host-local s -->\n"
        f"<!-- my-setup:user-section end host-local s hash={'a' * 64} -->\n"
        "footer\n"
    )

    # Placeholder hash; the strict parser only checks regex shape (64 hex
    # chars), not body-content correctness. Drift classifier sees a hash
    # mismatch which is fine — the test asserts read-count delta, not
    # drift state.
    _H = "a" * 64

    def _seed_dst(name: str) -> Path:
        d = tmp_path / name
        d.write_text(
            "old header\n"
            "<!-- my-setup:user-section start host-local s -->\n"
            "USER CONTENT\n"
            f"<!-- my-setup:user-section end host-local s hash={_H} -->\n"
            "old footer\n"
        )
        return d

    # --- Baseline: no precomputed dict; copy_atomic re-reads live. ---
    dst_a = _seed_dst("dst_a.md")
    live_text_a = dst_a.read_text(encoding="utf-8")
    precomputed = sections.extract_live_sections(live_text_a)

    original_read_text = Path.read_text
    counts = {"a": 0, "b": 0}
    target_a = dst_a.resolve()

    def _counting_read_text_a(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.resolve() == target_a:
            counts["a"] += 1
        return original_read_text(self, *args, **kwargs)

    with mock.patch.object(Path, "read_text", _counting_read_text_a):
        copy_atomic(src, dst_a, preserve_user_sections=True)

    # --- Precomputed: copy_atomic must skip the live re-read. ---
    dst_b = _seed_dst("dst_b.md")
    target_b = dst_b.resolve()

    def _counting_read_text_b(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.resolve() == target_b:
            counts["b"] += 1
        return original_read_text(self, *args, **kwargs)

    with mock.patch.object(Path, "read_text", _counting_read_text_b):
        copy_atomic(
            src,
            dst_b,
            preserve_user_sections=True,
            precomputed_live_sections=precomputed,
        )

    # Precomputed path: only the diff-check read in copy_atomic remains.
    # Baseline path: _compute_content re-read + diff-check = 2 reads.
    assert counts["b"] == 1, (
        "the diff-check read in copy_atomic should be the only read on path B"
    )
    assert counts["a"] == 2, (
        "compute_content re-read + diff-check should be the only 2 reads on path A"
    )


def test_copy_atomic_precomputed_live_sections_matches_fresh_read(
    tmp_path: Path,
) -> None:
    """Passing ``precomputed_live_sections=extract_live_sections(live_text)``
    must yield byte-identical output to the no-precompute path.
    """
    src_text = (
        "header\n"
        "<!-- my-setup:user-section start host-local s -->\n"
        f"<!-- my-setup:user-section end host-local s hash={'a' * 64} -->\n"
        "footer\n"
    )
    live_text = (
        "old header\n"
        "<!-- my-setup:user-section start host-local s -->\n"
        "USER BODY\n"
        f"<!-- my-setup:user-section end host-local s hash={'a' * 64} -->\n"
        "old footer\n"
    )

    src = tmp_path / "src.md"
    src.write_text(src_text)
    dst_a = tmp_path / "dst_a.md"
    dst_a.write_text(live_text)
    dst_b = tmp_path / "dst_b.md"
    dst_b.write_text(live_text)

    copy_atomic(src, dst_a, preserve_user_sections=True)
    copy_atomic(
        src,
        dst_b,
        preserve_user_sections=True,
        precomputed_live_sections=sections.extract_live_sections(live_text),
    )

    assert dst_a.read_bytes() == dst_b.read_bytes()


def test_copy_atomic_section_bodies_override_still_takes_precedence_with_precomputed(
    tmp_path: Path,
) -> None:
    """With both ``precomputed_live_sections`` and ``section_bodies_override``
    set, the override wins per-key (same precedence as the no-precompute
    path's `{**live_sections, **override}` merge).
    """
    src_text = (
        "header\n"
        "<!-- my-setup:user-section start host-local s -->\n"
        f"<!-- my-setup:user-section end host-local s hash={'a' * 64} -->\n"
        "footer\n"
    )
    live_text = (
        "old header\n"
        "<!-- my-setup:user-section start host-local s -->\n"
        "LIVE BODY\n"
        f"<!-- my-setup:user-section end host-local s hash={'a' * 64} -->\n"
        "old footer\n"
    )

    src = tmp_path / "src.md"
    src.write_text(src_text)
    dst = tmp_path / "dst.md"
    dst.write_text(live_text)

    copy_atomic(
        src,
        dst,
        preserve_user_sections=True,
        precomputed_live_sections=sections.LiveSections({"s": "LIVE BODY\n"}),
        section_bodies_override={"s": "OVERRIDE BODY\n"},
    )
    final = dst.read_text()
    assert "OVERRIDE BODY" in final
    assert "LIVE BODY" not in final


def test_yaml_user_keys_preserved(tmp_path: Path) -> None:
    src = tmp_path / "src.yaml"
    src.write_text("a: 1\nb: 2\nc: 3\n")
    dst = tmp_path / "dst.yaml"
    dst.write_text("a: 10\nb: 20\nc: 30\n")
    copy_atomic(src, dst, preserve_user_keys=["a", "c"])
    text = dst.read_text()
    assert "a: 10" in text
    assert "b: 2" in text
    assert "c: 30" in text


def test_yaml_user_keys_type_mismatch_raises(tmp_path: Path) -> None:
    src = tmp_path / "src.yaml"
    src.write_text("a: scalar\n")
    dst = tmp_path / "dst.yaml"
    dst.write_text("a:\n  - 1\n  - 2\n")
    with pytest.raises(MergeTypeMismatch):
        copy_atomic(src, dst, preserve_user_keys=["a"])


def test_mode_preserved(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("data\n")
    src.chmod(0o644)
    dst = tmp_path / "dst"
    copy_atomic(src, dst)
    assert stat.S_IMODE(dst.stat().st_mode) == 0o644


def test_dst_parent_created(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("x\n")
    dst = tmp_path / "deeply" / "nested" / "dst"
    copy_atomic(src, dst)
    assert dst.read_text() == "x\n"


def test_tmp_cleaned_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src"
    src.write_text("data\n")
    dst = tmp_path / "dst"

    def _boom(*args: Any, **kwargs: Any) -> NoReturn:
        raise OSError("simulated")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError, match="simulated"):
        copy_atomic(src, dst)
    leftover = list(tmp_path.glob(".dst.*.tmp"))
    assert leftover == []


def test_missing_src_raises(tmp_path: Path) -> None:
    with pytest.raises(MissingTrackedFile):
        copy_atomic(tmp_path / "ghost", tmp_path / "dst")


def test_bootstrap_local_creates_missing(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "file.md"
    bootstrap_local([target])
    assert target.exists()
    assert target.read_text() == ""


def test_bootstrap_local_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "file.md"
    target.write_text("existing\n")
    bootstrap_local([target])
    assert target.read_text() == "existing\n"


def _build_profile(tmp_path: Path, present: list[str], missing: list[str]):
    repo = tmp_path / "repo"
    live = tmp_path / "live"
    for name in present:
        (repo / "tracked" / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / "tracked" / name).write_text("data\n")
    cfg = Config(
        dotfiles={
            name: Dotfile(src=Path(name), dst=str(live / name))
            for name in (*present, *missing)
        },
        profiles={"p": Profile(dotfiles=[*present, *missing])},
    )
    return repo, live, cfg, resolve_profile(cfg, "p")


def test_validate_srcs_exist_passes_when_all_present(tmp_path: Path) -> None:
    repo, _, cfg, resolved = _build_profile(tmp_path, ["a", "b"], [])
    validate_srcs_exist(cfg, resolved, repo)


def test_validate_srcs_exist_raises_with_single_missing(tmp_path: Path) -> None:
    repo, _, cfg, resolved = _build_profile(tmp_path, ["a"], ["ghost"])
    with pytest.raises(MissingTrackedFile, match="ghost"):
        validate_srcs_exist(cfg, resolved, repo)


def test_validate_srcs_exist_lists_all_missing(tmp_path: Path) -> None:
    repo, _, cfg, resolved = _build_profile(
        tmp_path, ["ok"], ["miss1", "miss2", "miss3"]
    )
    with pytest.raises(MissingTrackedFile) as exc_info:
        validate_srcs_exist(cfg, resolved, repo)
    msg = str(exc_info.value)
    assert "miss1" in msg
    assert "miss2" in msg
    assert "miss3" in msg


def test_validate_srcs_exist_failure_leaves_live_untouched(tmp_path: Path) -> None:
    """Pre-flight runs before any deploy, so a missing src must not
    leave any dotfile half-applied to live.
    """
    repo, live, cfg, resolved = _build_profile(tmp_path, ["a"], ["ghost"])
    with pytest.raises(MissingTrackedFile):
        validate_srcs_exist(cfg, resolved, repo)
    assert not (live / "a").exists()
    assert not (live / "ghost").exists()


# ---------------------------------------------------------------------------
# dotfiles-9ln — install migrates legacy live; post-install invariant holds
# ---------------------------------------------------------------------------


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_copy_atomic_legacy_live_body_preserved(tmp_path: Path) -> None:
    """``copy_atomic`` onto a legacy live file preserves the body bytes
    and re-tags the markers to match tracked (semantics keyword + hash)."""
    body = "preserved live content\n"
    src = tmp_path / "src.md"
    src.write_text(
        "header\n"
        "<!-- my-setup:user-section start shared notes -->\n"
        f"{body}"
        f"<!-- my-setup:user-section end shared notes hash={_sha256_hex(body)} -->\n"
        "footer\n"
    )
    dst = tmp_path / "dst.md"
    # Live with pre-9by untagged markers and no hash segment.
    dst.write_text(
        "header\n"
        "<!-- my-setup:user-section start notes -->\n"
        f"{body}"
        "<!-- my-setup:user-section end notes -->\n"
        "footer\n"
    )
    copy_atomic(src, dst, preserve_user_sections=True)
    final = dst.read_text()
    assert body in final
    # Markers were re-tagged to match tracked's semantics + hash.
    assert "end shared notes hash=" in final
    # Untagged markers no longer present.
    assert detect_legacy_markers(final) is False


def test_copy_atomic_post_install_invariant_holds_for_all_sections(
    tmp_path: Path,
) -> None:
    """Post-install invariant: every section's embedded hash equals
    :func:`hash_sections` for the actual body written. Asserts no
    ``None`` values remain and every section satisfies the equality."""
    body_a = "section A body\n"
    body_b = "section B body\n"
    src = tmp_path / "src.md"
    src.write_text(
        "head\n"
        "<!-- my-setup:user-section start shared a -->\n"
        f"{body_a}"
        f"<!-- my-setup:user-section end shared a hash={_sha256_hex(body_a)} -->\n"
        "mid\n"
        "<!-- my-setup:user-section start host-local b -->\n"
        f"{body_b}"
        f"<!-- my-setup:user-section end host-local b hash={_sha256_hex(body_b)} -->\n"
        "tail\n"
    )
    dst = tmp_path / "dst.md"
    dst.write_text(
        "head\n"
        "<!-- my-setup:user-section start a -->\n"
        f"{body_a}"
        f"<!-- my-setup:user-section end a hash={'a' * 64} -->\n"
        "mid\n"
        "<!-- my-setup:user-section start b -->\n"
        f"{body_b}"
        f"<!-- my-setup:user-section end b hash={'a' * 64} -->\n"
        "tail\n"
    )
    copy_atomic(src, dst, preserve_user_sections=True)

    result_text = dst.read_text()
    embedded = extract_marker_hashes(result_text)
    computed = hash_sections(result_text)
    assert embedded == computed
    assert None not in embedded.values()


def test_copy_atomic_second_install_is_noop_after_legacy_retag(
    tmp_path: Path,
) -> None:
    """First install migrates legacy live to strict-clean. A second
    install with no body changes yields :attr:`DeployAction.NOOP`,
    proving the re-tagged file is a stable fixed point."""
    body = "host body\n"
    digest = _sha256_hex(body)
    src = tmp_path / "src.md"
    src.write_text(
        "<!-- my-setup:user-section start host-local notes -->\n"
        f"{body}"
        f"<!-- my-setup:user-section end host-local notes hash={digest} -->\n"
    )
    dst = tmp_path / "dst.md"
    dst.write_text(
        "<!-- my-setup:user-section start notes -->\n"
        f"{body}"
        f"<!-- my-setup:user-section end notes hash={'a' * 64} -->\n"
    )
    first = copy_atomic(src, dst, preserve_user_sections=True)
    assert first.action in {DeployAction.CREATED, DeployAction.UPDATED}
    second = copy_atomic(src, dst, preserve_user_sections=True)
    assert second.action is DeployAction.NOOP
