"""Tests for VSCode extension reconcile.

``subprocess.run`` is monkeypatched to a fake driver that records every
call and updates an in-memory installed-extensions set, so tests can
assert on the exact sequence of install/uninstall invocations without
touching a real ``code`` CLI.
"""

import logging
import subprocess

import pytest

from my_setup.config import Extensions, ReconcilePolicy
from my_setup.errors import ExtensionToolMissing
from my_setup.extensions import (
    ReconcileReport,
    list_installed,
    reconcile,
)


class FakeCode:
    """Tracks calls to a faked ``code`` CLI and mutates installed state."""

    def __init__(self, installed: list[str]):
        self.installed: list[str] = list(installed)
        self.calls: list[list[str]] = []

    def run(self, args, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[1] == "--list-extensions":
            stdout = (
                "\n".join(self.installed) + ("\n" if self.installed else "")
            )
            return subprocess.CompletedProcess(args, 0, stdout, "")
        if args[1] == "--install-extension":
            ext_id = args[2]
            if ext_id not in self.installed:
                self.installed.append(ext_id)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "--uninstall-extension":
            ext_id = args[2]
            if ext_id in self.installed:
                self.installed.remove(ext_id)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected code invocation: {args!r}")

    @property
    def install_args(self) -> list[str]:
        return [c[2] for c in self.calls if c[1] == "--install-extension"]

    @property
    def uninstall_args(self) -> list[str]:
        return [c[2] for c in self.calls if c[1] == "--uninstall-extension"]


@pytest.fixture
def fake_code(monkeypatch: pytest.MonkeyPatch):
    """Default fixture: ``code`` resolves and starts with no installed extensions."""

    def factory(installed: list[str]) -> FakeCode:
        fake = FakeCode(installed)
        monkeypatch.setattr(
            "my_setup.extensions.shutil.which",
            lambda name: "/usr/bin/code" if name == "code" else None,
        )
        monkeypatch.setattr("my_setup.extensions.subprocess.run", fake.run)
        return fake

    return factory


def test_list_installed_parses_lines(fake_code) -> None:
    fake_code(["a.x", "b.y", "c.z"])
    assert list_installed() == {"a.x", "b.y", "c.z"}


def test_list_installed_skips_blank_lines(fake_code) -> None:
    fake = fake_code([])
    fake.installed = ["a.x", "", "b.y", "  "]
    assert list_installed() == {"a.x", "b.y"}


def test_list_installed_skips_non_extension_id_lines(fake_code) -> None:
    """The Remote-SSH `code` CLI prepends a header line; ignore it."""
    fake = fake_code([])
    fake.installed = [
        "Extensions installed on SSH: 1.2.3.4:",
        "anthropic.claude-code",
        "ms-python.python",
        "Some other noise line",
    ]
    assert list_installed() == {"anthropic.claude-code", "ms-python.python"}


def test_missing_code_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("my_setup.extensions.shutil.which", lambda _: None)
    with pytest.raises(ExtensionToolMissing, match="not found"):
        list_installed()
    with pytest.raises(ExtensionToolMissing, match="not found"):
        reconcile(Extensions(include=["x"]))


def test_additive_fresh_host_installs_declared(fake_code) -> None:
    fake = fake_code([])
    ext = Extensions(
        include=["a.x", "b.y"], reconcile=ReconcilePolicy.ADDITIVE
    )
    report = reconcile(ext)
    assert report.to_install == ["a.x", "b.y"]
    assert report.to_uninstall == []
    assert sorted(fake.install_args) == ["a.x", "b.y"]
    assert fake.uninstall_args == []


def test_additive_leaves_extras_untouched(fake_code) -> None:
    fake = fake_code(["a.x", "extra.one", "extra.two"])
    ext = Extensions(
        include=["a.x", "new.one"], reconcile=ReconcilePolicy.ADDITIVE
    )
    report = reconcile(ext)
    assert report.to_install == ["new.one"]
    assert report.to_uninstall == []
    assert fake.install_args == ["new.one"]
    assert fake.uninstall_args == []


def test_prune_removes_extras_and_excluded(fake_code) -> None:
    fake = fake_code(["a.x", "b.y", "extra.one", "github.copilot-chat"])
    ext = Extensions(
        include=["a.x", "b.y"],
        exclude=["github.copilot-chat"],
        reconcile=ReconcilePolicy.PRUNE,
    )
    report = reconcile(ext)
    assert report.to_install == []
    assert sorted(report.to_uninstall) == ["extra.one", "github.copilot-chat"]
    assert fake.install_args == []
    assert sorted(fake.uninstall_args) == ["extra.one", "github.copilot-chat"]


def test_prune_installs_missing_and_uninstalls_extras(fake_code) -> None:
    fake = fake_code(["existing.one", "extra.one"])
    ext = Extensions(
        include=["existing.one", "new.one"],
        reconcile=ReconcilePolicy.PRUNE,
    )
    report = reconcile(ext)
    assert report.to_install == ["new.one"]
    assert report.to_uninstall == ["extra.one"]
    assert fake.install_args == ["new.one"]
    assert fake.uninstall_args == ["extra.one"]


def test_report_computes_diffs_without_acting(fake_code) -> None:
    fake = fake_code(["a.x", "extra.one"])
    ext = Extensions(
        include=["a.x", "b.y"], reconcile=ReconcilePolicy.REPORT
    )
    report = reconcile(ext)
    assert report.to_install == ["b.y"]
    assert report.to_uninstall == ["extra.one"]
    assert bool(report) is True
    # Only --list-extensions runs; no install/uninstall.
    assert fake.install_args == []
    assert fake.uninstall_args == []


def test_dry_run_runs_no_install_or_uninstall(
    fake_code, caplog: pytest.LogCaptureFixture
) -> None:
    fake = fake_code(["existing.one"])
    ext = Extensions(
        include=["existing.one", "new.one"],
        reconcile=ReconcilePolicy.PRUNE,
    )
    with caplog.at_level(logging.INFO, logger="my_setup.extensions"):
        report = reconcile(ext, dry_run=True)
    assert report.dry_run is True
    assert report.to_install == ["new.one"]
    assert fake.install_args == []
    assert fake.uninstall_args == []
    assert any("would install" in rec.message for rec in caplog.records)


def test_exclude_overrides_include(fake_code) -> None:
    fake_code([])
    ext = Extensions(
        include=["keep.me", "drop.me"],
        exclude=["drop.me"],
        reconcile=ReconcilePolicy.ADDITIVE,
    )
    report = reconcile(ext)
    assert report.to_install == ["keep.me"]


def test_clean_state_returns_falsy_report(fake_code) -> None:
    fake_code(["a.x", "b.y"])
    ext = Extensions(
        include=["a.x", "b.y"], reconcile=ReconcilePolicy.PRUNE
    )
    report = reconcile(ext)
    assert isinstance(report, ReconcileReport)
    assert bool(report) is False


# ---- YAML-edit helpers ---------------------------------------------------

from pathlib import Path

from my_setup.extensions import add_to_include, remove_from_include
from my_setup.errors import ProfileNotFound


_FIXTURE_YAML = """\
version: 1

# Top-level comment.
dotfiles:
  d:
    src: x
    dst: y

profiles:
  base:
    # Base profile comment.
    dotfiles:
      - d
    extensions:
      include:
        - keep.me
      # Inline comment between keys.
      exclude:
        - drop.me
  bare:
    dotfiles:
      - d
"""


def _write_fixture(tmp_path: Path) -> Path:
    p = tmp_path / "my_setup.yaml"
    p.write_text(_FIXTURE_YAML, encoding="utf-8")
    return p


def test_add_to_include_appends(tmp_path: Path) -> None:
    cfg = _write_fixture(tmp_path)
    added = add_to_include(cfg, "base", "new.one")
    assert added is True
    text = cfg.read_text()
    assert "new.one" in text
    assert "Top-level comment." in text
    assert "Base profile comment." in text
    assert "Inline comment between keys." in text


def test_add_to_include_idempotent(tmp_path: Path) -> None:
    cfg = _write_fixture(tmp_path)
    added = add_to_include(cfg, "base", "keep.me")
    assert added is False
    assert cfg.read_text().count("keep.me") == 1


def test_add_to_include_creates_extensions_block_when_missing(
    tmp_path: Path,
) -> None:
    cfg = _write_fixture(tmp_path)
    added = add_to_include(cfg, "bare", "first.one")
    assert added is True
    text = cfg.read_text()
    assert "first.one" in text


def test_add_to_include_unknown_profile_raises(tmp_path: Path) -> None:
    cfg = _write_fixture(tmp_path)
    with pytest.raises(ProfileNotFound):
        add_to_include(cfg, "ghost", "x")


def test_remove_from_include_drops_entry(tmp_path: Path) -> None:
    cfg = _write_fixture(tmp_path)
    changed = remove_from_include(cfg, "base", "keep.me")
    assert changed is True
    assert "keep.me" not in cfg.read_text()


def test_remove_from_include_with_exclude_flag_appends_to_exclude(
    tmp_path: Path,
) -> None:
    cfg = _write_fixture(tmp_path)
    changed = remove_from_include(
        cfg, "base", "keep.me", add_to_exclude_list=True
    )
    assert changed is True
    text = cfg.read_text()
    assert "keep.me" in text  # under exclude now
    # And no longer under include — count once total
    assert text.count("keep.me") == 1


def test_remove_from_include_idempotent_when_absent(tmp_path: Path) -> None:
    cfg = _write_fixture(tmp_path)
    before = cfg.read_text()
    changed = remove_from_include(cfg, "base", "never.was")
    assert changed is False
    assert cfg.read_text() == before


def test_yaml_edits_preserve_structure_via_pydantic_round_trip(
    tmp_path: Path,
) -> None:
    """After an edit, load_config must still validate the file."""
    from my_setup.config import load_config

    cfg = _write_fixture(tmp_path)
    add_to_include(cfg, "base", "post.edit")
    config = load_config(cfg)
    assert "post.edit" in config.profiles["base"].extensions.include


# ---- capture_extensions --------------------------------------------------

from my_setup.extensions import capture_extensions


def test_capture_extensions_writes_installed_minus_exclude(
    tmp_path: Path, fake_code
) -> None:
    cfg = _write_fixture(tmp_path)
    fake_code(["a.x", "b.y", "drop.me", "extra.one"])

    changed = capture_extensions(cfg, "base")

    assert changed is True
    text = cfg.read_text()
    assert "a.x" in text
    assert "b.y" in text
    assert "extra.one" in text
    assert "drop.me" in text  # appears under exclude (already there)
    # "drop.me" is excluded so it shouldn't end up in the new include list
    from my_setup.config import load_config

    reloaded = load_config(cfg)
    assert "drop.me" not in reloaded.profiles["base"].extensions.include
    assert "drop.me" in reloaded.profiles["base"].extensions.exclude


def test_capture_extensions_preserves_comments(
    tmp_path: Path, fake_code
) -> None:
    cfg = _write_fixture(tmp_path)
    fake_code(["a.x"])
    capture_extensions(cfg, "base")
    text = cfg.read_text()
    assert "Top-level comment." in text
    assert "Base profile comment." in text


def test_capture_extensions_idempotent(tmp_path: Path, fake_code) -> None:
    """Two captures in a row: the second is a no-op."""
    cfg = _write_fixture(tmp_path)
    fake_code(["keep.me", "added.one"])
    first = capture_extensions(cfg, "base")
    second = capture_extensions(cfg, "base")
    assert first is True
    assert second is False


def test_capture_extensions_does_not_touch_exclude(
    tmp_path: Path, fake_code
) -> None:
    cfg = _write_fixture(tmp_path)
    before = cfg.read_text()
    fake_code(["drop.me", "a.x"])
    capture_extensions(cfg, "base")
    after = cfg.read_text()
    # exclude block survives intact (drop.me still listed there once)
    assert before.count("drop.me") == after.count("drop.me")
