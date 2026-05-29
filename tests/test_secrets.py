"""Tests for setforge.secrets — gitleaks subprocess wrapper + allowlist."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from setforge import binaries, secrets


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect binaries' LOCAL_CONFIG_PATH + clear CLI/env state per test."""
    monkeypatch.setattr(binaries, "LOCAL_CONFIG_PATH", tmp_path / "local.yaml")
    binaries._cli_overrides.clear()
    for name in binaries.SUPPORTED_BINARIES:
        monkeypatch.delenv(
            f"{binaries._ENV_VAR_PREFIX}{name.upper()}{binaries._ENV_VAR_SUFFIX}",
            raising=False,
        )


def _sha(text: str) -> str:
    """Helper: hex sha256 of a string (used to seed allowlist)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _gitleaks_json(findings: list[dict[str, object]]) -> str:
    """Render a fake gitleaks JSON report payload."""
    return json.dumps(findings)


def _fake_run(
    *,
    returncode: int,
    stdout: str = "",
    stderr: str = "",
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return a fake ``subprocess.run`` replacement returning a CompletedProcess."""

    def _runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args, returncode=returncode, stdout=stdout, stderr=stderr
        )

    return _runner


# ---------------------------------------------------------------------------
# Soft-requirement contract: missing gitleaks binary
# ---------------------------------------------------------------------------


def test_missing_gitleaks_warns_and_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When gitleaks is absent, emit yellow warning + empty result; NO raise."""
    monkeypatch.setattr(secrets.binaries, "resolve_binary", lambda _name: None)

    result = secrets.run_pre_deploy_scan(
        tracked_root=tmp_path,
        allowlist_path=tmp_path / "allow",
    )

    captured = capsys.readouterr()
    assert result.findings == ()
    assert result.files_scanned == 0
    assert "gitleaks not found on PATH" in captured.err
    assert "github.com/gitleaks/gitleaks#installing" in captured.err


def test_skip_true_emits_no_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """skip=True is explicit opt-out: no warning, no subprocess call."""
    monkeypatch.setattr(
        secrets.binaries, "resolve_binary", lambda _name: Path("/should/not/be/called")
    )

    def _fail(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess.run must not be called when skip=True")

    monkeypatch.setattr(secrets.subprocess, "run", _fail)

    result = secrets.run_pre_deploy_scan(
        tracked_root=tmp_path, allowlist_path=tmp_path / "allow", skip=True
    )

    captured = capsys.readouterr()
    assert result.findings == ()
    assert result.files_scanned == 0
    assert captured.err == ""


# ---------------------------------------------------------------------------
# Exit-code triage: 0 / 1 / other
# ---------------------------------------------------------------------------


def test_exit_zero_yields_empty_clean_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exit 0 from gitleaks = clean scan; empty findings."""
    monkeypatch.setattr(
        secrets.binaries, "resolve_binary", lambda _name: Path("/fake/gitleaks")
    )
    monkeypatch.setattr(secrets.subprocess, "run", _fake_run(returncode=0, stdout=""))

    result = secrets.run_pre_deploy_scan(
        tracked_root=tmp_path, allowlist_path=tmp_path / "allow"
    )

    assert result.findings == ()
    assert result.files_scanned == 0


def test_exit_one_parses_findings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exit 1 = leaks found; parse JSON into SecretFinding tuple."""
    payload = _gitleaks_json(
        [
            {
                "RuleID": "github-pat",
                "Description": "GitHub Personal Access Token",
                "File": "tracked/skills/x/SKILL.md",
                "StartLine": 42,
                "Secret": "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                "Match": "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            }
        ]
    )
    monkeypatch.setattr(
        secrets.binaries, "resolve_binary", lambda _name: Path("/fake/gitleaks")
    )
    monkeypatch.setattr(
        secrets.subprocess, "run", _fake_run(returncode=1, stdout=payload)
    )

    result = secrets.run_pre_deploy_scan(
        tracked_root=tmp_path, allowlist_path=tmp_path / "allow"
    )

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.rule_id == "github-pat"
    assert finding.line_number == 42
    assert finding.snippet.startswith("ghp_")
    assert finding.snippet_hash == _sha(finding.snippet)
    assert finding.secret_kind == "GitHub Personal Access Token"


def test_exit_one_filters_via_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Allowlist filtering drops findings whose snippet hash is allowlisted."""
    snippet = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    payload = _gitleaks_json(
        [
            {
                "RuleID": "github-pat",
                "Description": "GitHub Personal Access Token",
                "File": "tracked/x.md",
                "StartLine": 1,
                "Secret": snippet,
                "Match": snippet,
            }
        ]
    )
    allow_path = tmp_path / "allow"
    allow_path.write_text(f"# header\n{_sha(snippet)}\n", encoding="utf-8")

    monkeypatch.setattr(
        secrets.binaries, "resolve_binary", lambda _name: Path("/fake/gitleaks")
    )
    monkeypatch.setattr(
        secrets.subprocess, "run", _fake_run(returncode=1, stdout=payload)
    )

    result = secrets.run_pre_deploy_scan(
        tracked_root=tmp_path, allowlist_path=allow_path
    )

    assert result.findings == ()
    # files_scanned reflects the count of files walked under tracked_root
    # (here: just the allowlist file written into tmp_path), NOT the
    # number of findings gitleaks reported.
    assert result.files_scanned == 1


def test_files_scanned_counts_walked_files_not_findings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``files_scanned`` is the count of files under tracked_root, not findings.

    Seed a tree with several files (including a nested one) and a single
    gitleaks finding; ``files_scanned`` must equal the walked file count,
    never ``len(findings)``.
    """
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "b.md").write_text("y", encoding="utf-8")
    nested = tmp_path / "sub" / "c.md"
    nested.parent.mkdir()
    nested.write_text("z", encoding="utf-8")
    payload = _gitleaks_json(
        [
            {
                "RuleID": "github-pat",
                "Description": "GitHub Personal Access Token",
                "File": "a.md",
                "StartLine": 1,
                "Secret": "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                "Match": "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            }
        ]
    )
    monkeypatch.setattr(
        secrets.binaries, "resolve_binary", lambda _name: Path("/fake/gitleaks")
    )
    monkeypatch.setattr(
        secrets.subprocess, "run", _fake_run(returncode=1, stdout=payload)
    )

    result = secrets.run_pre_deploy_scan(
        tracked_root=tmp_path, allowlist_path=tmp_path / "nonexistent-allow"
    )

    assert len(result.findings) == 1
    # Three files were walked; the single finding must not drive the count.
    assert result.files_scanned == 3


def test_exit_other_warns_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Other non-zero exit (e.g. 2) = scan-runtime failure; warn-and-continue."""
    monkeypatch.setattr(
        secrets.binaries, "resolve_binary", lambda _name: Path("/fake/gitleaks")
    )
    monkeypatch.setattr(
        secrets.subprocess,
        "run",
        _fake_run(returncode=2, stdout="", stderr="config file not found"),
    )

    result = secrets.run_pre_deploy_scan(
        tracked_root=tmp_path, allowlist_path=tmp_path / "allow"
    )

    captured = capsys.readouterr()
    assert result.findings == ()
    assert result.files_scanned == 0
    assert "gitleaks scan failed" in captured.err
    assert "exit 2" in captured.err
    assert "continuing without secrets check" in captured.err


def test_timeout_warns_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A TimeoutExpired from subprocess yields warn-and-empty (no raise)."""
    monkeypatch.setattr(
        secrets.binaries, "resolve_binary", lambda _name: Path("/fake/gitleaks")
    )

    def _raise_timeout(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="gitleaks", timeout=60)

    monkeypatch.setattr(secrets.subprocess, "run", _raise_timeout)

    result = secrets.run_pre_deploy_scan(
        tracked_root=tmp_path, allowlist_path=tmp_path / "allow"
    )

    captured = capsys.readouterr()
    assert result.findings == ()
    assert "timed out" in captured.err


# ---------------------------------------------------------------------------
# Allowlist file IO
# ---------------------------------------------------------------------------


def test_append_to_allowlist_creates_file_and_header(tmp_path: Path) -> None:
    """First-write: creates parent dir, header, and the hash line."""
    allow = tmp_path / "nested" / "allow"
    secrets.append_to_allowlist(snippet_hash="deadbeef", allowlist_path=allow)

    text = allow.read_text(encoding="utf-8")
    assert "# setforge secrets-allowlist" in text
    assert "deadbeef" in text


def test_append_to_allowlist_appends_without_duplicating_header(tmp_path: Path) -> None:
    """Second-write: leaves the header alone, appends a fresh hash."""
    allow = tmp_path / "allow"
    secrets.append_to_allowlist(snippet_hash="aaa", allowlist_path=allow)
    secrets.append_to_allowlist(snippet_hash="bbb", allowlist_path=allow)

    text = allow.read_text(encoding="utf-8")
    assert text.count("setforge secrets-allowlist") == 1
    assert "aaa" in text
    assert "bbb" in text


def test_load_allowlist_skips_comments_and_blank(tmp_path: Path) -> None:
    """Internal helper: comments + blank lines are ignored when parsing."""
    allow = tmp_path / "allow"
    hash_a = _sha("a")
    hash_b = _sha("b")
    allow.write_text(
        f"# leading comment\n\n  # indented comment\n{hash_a}\n\n{hash_b}\n",
        encoding="utf-8",
    )
    parsed = secrets._load_allowlist(allow)
    assert parsed == frozenset({hash_a, hash_b})


def test_load_allowlist_warns_on_malformed_hash(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-64-hex tokens emit a yellow warning and are excluded from the set."""
    allow = tmp_path / "allow"
    valid_hash = _sha("real")
    allow.write_text(
        f"# header\nabc123\n{valid_hash}\nNOT_A_HASH\n",
        encoding="utf-8",
    )

    parsed = secrets._load_allowlist(allow)

    captured = capsys.readouterr()
    assert parsed == frozenset({valid_hash})
    assert "'abc123'" in captured.err
    assert "'NOT_A_HASH'" in captured.err
    assert "is not a 64-hex sha256" in captured.err
    # Line numbers in the warnings (abc123 is line 2, NOT_A_HASH is line 4).
    assert "line 2" in captured.err
    assert "line 4" in captured.err
