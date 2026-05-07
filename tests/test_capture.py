"""Tests for capture (live → tracked)."""

from pathlib import Path

from my_setup.capture import (
    CaptureAction,
    capture_dotfile,
    capture_profile,
)
from my_setup.config import Config, Dotfile, Profile


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_capture_plain_copy(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _write(dst, "live content\n")
    result = capture_dotfile(
        src, dst, preserve_user_sections=False, preserve_user_keys=[]
    )
    assert result.action is CaptureAction.UPDATED
    assert src.read_text() == "live content\n"


def test_capture_noop_when_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _write(src, "same\n")
    _write(dst, "same\n")
    result = capture_dotfile(
        src, dst, preserve_user_sections=False, preserve_user_keys=[]
    )
    assert result.action is CaptureAction.NOOP


def test_capture_strips_user_sections(tmp_path: Path) -> None:
    src = tmp_path / "src.md"
    dst = tmp_path / "dst.md"
    _write(
        dst,
        "header\n"
        "<!-- my-setup:user-section start -->\n"
        "host-specific stuff\n"
        "<!-- my-setup:user-section end -->\n"
        "footer\n",
    )
    capture_dotfile(
        src, dst, preserve_user_sections=True, preserve_user_keys=[]
    )
    text = src.read_text()
    assert "host-specific stuff" not in text
    assert "<!-- my-setup:user-section start -->" in text
    assert "<!-- my-setup:user-section end -->" in text
    assert "header\n" in text
    assert "footer\n" in text


def test_capture_strips_yaml_keys(tmp_path: Path) -> None:
    src = tmp_path / "src.yaml"
    dst = tmp_path / "dst.yaml"
    _write(dst, "a: 1\nb: 2\nc: 3\n")
    capture_dotfile(
        src, dst, preserve_user_sections=False, preserve_user_keys=["a", "c"]
    )
    text = src.read_text()
    assert "a:" not in text
    assert "c:" not in text
    assert "b: 2" in text


def test_capture_yaml_preserves_comments(tmp_path: Path) -> None:
    src = tmp_path / "src.yaml"
    dst = tmp_path / "dst.yaml"
    _write(
        dst,
        "# leading comment\n"
        "a: 1  # inline a\n"
        "b: 2  # inline b\n"
        "# trailing comment\n",
    )
    capture_dotfile(
        src, dst, preserve_user_sections=False, preserve_user_keys=["a"]
    )
    text = src.read_text()
    assert "# leading comment" in text
    assert "# inline b" in text
    assert "b: 2" in text


def test_capture_skips_missing_dst(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "missing"
    result = capture_dotfile(
        src, dst, preserve_user_sections=False, preserve_user_keys=[]
    )
    assert result.action is CaptureAction.SKIPPED
    assert not src.exists()


def test_capture_profile_iterates_dotfiles(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    src1 = repo / "tracked" / "x"
    src2 = repo / "tracked" / "y"
    dst1 = tmp_path / "live" / "x"
    dst2 = tmp_path / "live" / "y"
    _write(dst1, "x-live\n")
    _write(dst2, "y-live\n")

    config = Config(
        dotfiles={
            "x": Dotfile(src=Path("x"), dst=str(dst1)),
            "y": Dotfile(src=Path("y"), dst=str(dst2)),
        },
        profiles={"p": Profile(dotfiles=["x", "y"])},
    )
    results = capture_profile(config, "p", repo)
    assert {r.name for r in results} == {"x", "y"}
    assert all(r.action is CaptureAction.UPDATED for r in results)
    assert src1.read_text() == "x-live\n"
    assert src2.read_text() == "y-live\n"
