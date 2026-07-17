from pathlib import Path

import pytest

from setforge.compare import resolve_dst, warn_if_dst_outside_home
from setforge.config import TrackedFile

_WARN_FRAGMENT = "outside $HOME"


@pytest.mark.parametrize(
    "bad_dst",
    ["~/../../etc/cron.d/pwn", "/etc/cronjob", "/tmp/outside"],
)
def test_warn_on_out_of_home_dst(
    bad_dst: str, capsys: pytest.CaptureFixture[str]
) -> None:
    tf = TrackedFile(src=Path("x"), dst=bad_dst)
    warn_if_dst_outside_home(tf, resolve_dst(tf))
    err = capsys.readouterr().err
    assert _WARN_FRAGMENT in err
    assert "deploying anyway" in err


def test_no_warn_when_allow_outside_home(
    capsys: pytest.CaptureFixture[str],
) -> None:
    tf = TrackedFile(
        src=Path("x"), dst="~/../../etc/cron.d/pwn", allow_outside_home=True
    )
    warn_if_dst_outside_home(tf, resolve_dst(tf))
    assert capsys.readouterr().err == ""


def test_no_warn_under_home(capsys: pytest.CaptureFixture[str]) -> None:
    tf = TrackedFile(src=Path("x"), dst="~/.claude/x")
    dst = resolve_dst(tf)
    warn_if_dst_outside_home(tf, dst)
    assert capsys.readouterr().err == ""
    assert dst == Path.home() / ".claude" / "x"


def test_no_warn_home_itself(capsys: pytest.CaptureFixture[str]) -> None:
    tf = TrackedFile(src=Path("x"), dst=str(Path.home()))
    warn_if_dst_outside_home(tf, resolve_dst(tf))
    assert capsys.readouterr().err == ""


def test_warn_on_symlink_parent_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "escape").symlink_to(outside)
    monkeypatch.setenv("HOME", str(home))
    tf = TrackedFile(src=Path("x"), dst=str(home / "escape" / "file"))
    warn_if_dst_outside_home(tf, resolve_dst(tf))
    assert _WARN_FRAGMENT in capsys.readouterr().err


def test_allow_outside_home_defaults_false() -> None:
    tf = TrackedFile(src=Path("x"), dst="~/.claude/x")
    assert tf.allow_outside_home is False
