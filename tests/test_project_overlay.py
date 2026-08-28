from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from setforge.errors import SetforgeError
from setforge.project_overlay import (
    build_overlay,
    clean_content,
    process_filter,
    read_overlay,
    smudge_content,
    write_overlay,
)


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    return path


def _packet(payload: bytes) -> bytes:
    return f"{len(payload) + 4:04x}".encode() + payload


def _request(command: str, path: str, content: bytes) -> bytes:
    return b"".join(
        (
            _packet(f"command={command}\n".encode()),
            _packet(f"pathname={path}\n".encode()),
            b"0000",
            _packet(content),
            b"0000",
        )
    )


def test_overlay_clean_hides_only_recorded_local_hunk(tmp_path: Path) -> None:
    base = b"# Team\nteam\n"
    local = b"# Team\nteam\n\n# Local\nprivate\n"
    overlay = build_overlay(tmp_path, Path("CLAUDE.md"), base, local)

    live = b"# Team\nteam edited\n\n# Local\nprivate\n"
    cleaned = clean_content(overlay, live)

    assert cleaned == b"# Team\nteam edited\n"
    assert smudge_content(overlay, base) == local
    assert smudge_content(overlay, local) == local


def test_overlay_clean_refuses_ambiguous_local_hunk_match(tmp_path: Path) -> None:
    overlay = build_overlay(
        tmp_path,
        Path("CLAUDE.md"),
        b"team\n",
        b"team\nprivate\n",
    )

    with pytest.raises(SetforgeError, match="overlaps another edit"):
        clean_content(overlay, b"team\nprivate\nunrelated\nprivate\n")


def test_smudge_refuses_a_different_git_base(tmp_path: Path) -> None:
    overlay = build_overlay(tmp_path, Path("CLAUDE.md"), b"team\n", b"team\nlocal\n")

    with pytest.raises(SetforgeError, match="changed upstream"):
        smudge_content(overlay, b"different\n")


def test_overlay_state_round_trips_and_binds_target_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "target"
    target.mkdir()
    overlay = build_overlay(target, Path("CLAUDE.md"), b"team\n", b"team\nlocal\n")

    write_overlay(overlay)

    assert read_overlay(target, Path("CLAUDE.md")) == overlay
    assert read_overlay(target, Path("other.md")) is None


def test_filter_protocol_passes_unmanaged_and_filters_managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = _repo(tmp_path / "target")
    overlay = build_overlay(
        target,
        Path("CLAUDE.md"),
        b"team\n",
        b"team\nlocal\n",
    )
    write_overlay(overlay)
    monkeypatch.chdir(target)
    request = b"".join(
        (
            _packet(b"git-filter-client\n"),
            _packet(b"version=2\n"),
            b"0000",
            _packet(b"capability=clean\n"),
            _packet(b"capability=smudge\n"),
            b"0000",
            _request("clean", "CLAUDE.md", b"team\nlocal\n"),
            _request("smudge", "CLAUDE.md", b"team\n"),
            _request("clean", "other.md", b"plain\n"),
        )
    )
    output = io.BytesIO()

    process_filter(io.BytesIO(request), output)

    rendered = output.getvalue()
    assert b"git-filter-server\n" in rendered
    assert _packet(b"team\n") in rendered
    assert _packet(b"team\nlocal\n") in rendered
    assert _packet(b"plain\n") in rendered


def test_filter_protocol_rejects_escaping_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _repo(tmp_path / "target")
    monkeypatch.chdir(target)
    request = b"".join(
        (
            _packet(b"git-filter-client\n"),
            _packet(b"version=2\n"),
            b"0000",
            _packet(b"capability=clean\n"),
            _packet(b"capability=smudge\n"),
            b"0000",
            _request("clean", "../secret", b"private\n"),
        )
    )

    with pytest.raises(SetforgeError, match="not normalized"):
        process_filter(io.BytesIO(request), io.BytesIO())


def test_real_git_process_filter_hides_overlay_but_not_project_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = _repo(tmp_path / "target")
    subprocess.run(["git", "config", "user.name", "Test"], cwd=target, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=target,
        check=True,
    )
    destination = target / "CLAUDE.md"
    base = b"# Team\nteam\n"
    local = b"# Team\nteam\n\n# Local\nprivate\n"
    destination.write_bytes(base)
    subprocess.run(["git", "add", "CLAUDE.md"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=target, check=True)
    write_overlay(build_overlay(target, Path("CLAUDE.md"), base, local))
    command = f"{Path(sys.executable).with_name('setforge')} project filter-process"
    subprocess.run(
        ["git", "config", "filter.setforge-project.process", command],
        cwd=target,
        check=True,
    )
    subprocess.run(
        ["git", "config", "filter.setforge-project.required", "true"],
        cwd=target,
        check=True,
    )
    (target / ".git" / "info" / "attributes").write_text(
        "/CLAUDE.md filter=setforge-project\n"
    )
    destination.write_bytes(b"# Team\nteam edited\n\n# Local\nprivate\n")

    diff = subprocess.run(
        ["git", "diff", "--", "CLAUDE.md"],
        cwd=target,
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout

    assert b"team edited" in diff
    assert b"private" not in diff
