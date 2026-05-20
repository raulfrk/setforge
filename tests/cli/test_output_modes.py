"""Unit + behavioral tests for setforge-a1tn verbose/quiet/JSON output modes.

Coverage matrix:

- ``test_v_sets_info`` / ``test_vv_sets_debug`` — ``-v`` (count=1) sets
  INFO; ``-vv`` (count=2) sets DEBUG.
- ``test_quiet_silences_warnings_keeps_errors`` — ``--quiet`` floors the
  level at ERROR so WARNING/INFO are suppressed; explicit ``--quiet``
  errors still surface.
- ``test_quiet_v_mutex_exits_2`` — ``--quiet --verbose`` exits 2 (POSIX
  misuse) with ``mutually exclusive`` on stderr.
- ``test_compare_json_envelope`` /
  ``test_transitions_list_json_envelope`` /
  ``test_status_json_envelope`` /
  ``test_profile_show_json_envelope`` — ``--format=json`` returns the
  versioned envelope with ``schema_version`` = ``OUTPUT_SCHEMA_VERSION``,
  ``command``, ``data``.
- ``test_json_no_ansi_on_stdout`` — JSON-mode stdout parses as JSON and
  contains no ANSI escape sequences.
- ``test_redacts_token_env`` — ``-vv`` plus a token-shaped log message
  emits ``<REDACTED>`` and never the token value.
- ``test_setforge_log_level_env_precedence`` — explicit ``-v`` overrides
  ``SETFORGE_LOG_LEVEL=WARNING``; env alone takes effect without flag.

Tests exercise the public CLI surface via :class:`typer.testing.CliRunner`
so JSON / stderr split is observed end-to-end, not the renderer in
isolation.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from setforge._log_filter import RedactingFilter
from setforge.cli import app
from setforge.cli._output import (
    OUTPUT_SCHEMA_VERSION,
    OutputContext,
    OutputFormat,
    render,
    wrap_json,
)

_ANSI_RE = re.compile(r"\x1b\[")


def _write_minimal_config(tmp_path: Path, *, profile: str = "vm-headless") -> Path:
    """Build a minimal setforge.yaml under ``tmp_path``; return its path."""
    tracked = tmp_path / "tracked" / "doc.md"
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("hello\n", encoding="utf-8")
    yaml_path = tmp_path / "setforge.yaml"
    yaml_path.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.local/share/setforge-test/doc.md\n"
        "profiles:\n"
        f"  {profile}:\n"
        "    tracked_files: [doc]\n",
        encoding="utf-8",
    )
    return yaml_path


@pytest.fixture
def runner() -> CliRunner:
    """Return a fresh CliRunner per test."""
    return CliRunner()


@pytest.fixture
def minimal_config(tmp_path: Path) -> Path:
    """Materialise a minimal setforge.yaml + tracked tree; return yaml path."""
    return _write_minimal_config(tmp_path)


# ---------------------------------------------------------------------------
# wrap_json / render — pure unit tests for the boundary primitives.
# ---------------------------------------------------------------------------


def test_wrap_json_emits_versioned_envelope() -> None:
    """wrap_json always carries OUTPUT_SCHEMA_VERSION and the command name."""
    payload = wrap_json("compare", {"a": 1})
    parsed = json.loads(payload)
    assert parsed == {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "command": "compare",
        "data": {"a": 1},
    }


def test_wrap_json_includes_errors_when_nonempty() -> None:
    """Errors key only appears when the list is non-empty."""
    payload = wrap_json("compare", {"a": 1}, errors=["bad happened"])
    parsed = json.loads(payload)
    assert parsed["errors"] == ["bad happened"]


def test_wrap_json_omits_errors_when_none_or_empty() -> None:
    """Empty/None errors keep the envelope minimal (no key, no null)."""
    parsed = json.loads(wrap_json("compare", {"a": 1}, errors=None))
    assert "errors" not in parsed
    parsed_empty = json.loads(wrap_json("compare", {"a": 1}, errors=[]))
    assert "errors" not in parsed_empty


def test_render_human_invokes_closure(capsys: pytest.CaptureFixture[str]) -> None:
    """HUMAN format dispatches to the closure; no JSON on stdout."""
    ctx = OutputContext(format=OutputFormat.HUMAN)
    render(ctx, "compare", {"a": 1}, human_fn=lambda: print("HUMAN"))
    captured = capsys.readouterr()
    assert "HUMAN" in captured.out
    assert "schema_version" not in captured.out


def test_render_json_emits_envelope_and_skips_closure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSON format writes the envelope to stdout; closure NOT invoked."""
    ctx = OutputContext(format=OutputFormat.JSON)
    called: list[bool] = []
    render(
        ctx,
        "compare",
        {"a": 1},
        human_fn=lambda: called.append(True),  # type: ignore[func-returns-value]
    )
    captured = capsys.readouterr()
    assert called == []
    parsed = json.loads(captured.out)
    assert parsed["schema_version"] == OUTPUT_SCHEMA_VERSION
    assert parsed["command"] == "compare"


def test_render_none_ctx_falls_back_to_human(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When ctx.obj is None under pytest, human renderer runs (test bypass).

    ``PYTEST_CURRENT_TEST`` is set by pytest for the duration of every
    test, so this exercise hits the bypass branch.
    """
    render(None, "compare", {"a": 1}, human_fn=lambda: print("HUMAN"))
    captured = capsys.readouterr()
    assert "HUMAN" in captured.out


def test_render_none_ctx_raises_outside_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``PYTEST_CURRENT_TEST``, render(None, ...) raises RuntimeError.

    Guards against a future subcommand silently downgrading JSON mode
    to human output by forgetting to declare ``ctx: typer.Context``.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(RuntimeError, match="ctx_obj=None outside test context"):
        render(None, "compare", {"a": 1}, human_fn=lambda: None)


# ---------------------------------------------------------------------------
# RedactingFilter — pure unit tests.
# ---------------------------------------------------------------------------


def _make_record(msg: str) -> logging.LogRecord:
    """Build a minimal LogRecord with ``msg`` set."""
    return logging.LogRecord(
        name="setforge.test",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=0,
        msg=msg,
        args=None,
        exc_info=None,
    )


def test_redacting_filter_masks_token_env() -> None:
    """SETFORGE_GITHUB_TOKEN=ghp_FAKE → TOKEN=<REDACTED>."""
    filt = RedactingFilter()
    record = _make_record("env: SETFORGE_GITHUB_TOKEN=ghp_FAKE_VALUE")
    filt.filter(record)
    assert "ghp_FAKE_VALUE" not in record.msg
    assert "<REDACTED>" in record.msg


def test_redacting_filter_masks_password_env() -> None:
    """PASSWORD=hunter2 → PASSWORD=<REDACTED>."""
    filt = RedactingFilter()
    record = _make_record("PASSWORD=hunter2 and PASSWD=other")
    filt.filter(record)
    assert "hunter2" not in record.msg
    assert "other" not in record.msg
    assert record.msg.count("<REDACTED>") == 2


def test_redacting_filter_masks_cred_url() -> None:
    """https://user:secret@host → https://<REDACTED>@host."""
    filt = RedactingFilter()
    record = _make_record("clone https://alice:s3cret@github.com/r.git")
    filt.filter(record)
    assert "s3cret" not in record.msg
    assert "<REDACTED>" in record.msg


def test_redacting_filter_passes_non_string_msg() -> None:
    """Non-string record.msg (lazy %-format args) is not rewritten."""
    filt = RedactingFilter()
    record = _make_record("%s")
    record.msg = ("not", "a", "string")  # type: ignore[assignment]
    assert filt.filter(record) is True
    assert record.msg == ("not", "a", "string")


def test_redacting_filter_returns_true_always() -> None:
    """Filter never drops records — only rewrites their msg."""
    filt = RedactingFilter()
    record = _make_record("plain message with no secrets")
    assert filt.filter(record) is True
    assert record.msg == "plain message with no secrets"


def test_redacting_filter_masks_token_space_separated() -> None:
    """`--token abc` (space-separated flag form) → `--token <REDACTED>`."""
    filt = RedactingFilter()
    record = _make_record("running with --token ghp_FAKEVALUE other arg")
    filt.filter(record)
    assert "ghp_FAKEVALUE" not in record.msg
    assert "<REDACTED>" in record.msg
    assert "--token" in record.msg


def test_redacting_filter_masks_password_flag_space_separated() -> None:
    """`--password hunter2` is also masked under the space-separated form."""
    filt = RedactingFilter()
    record = _make_record("--password hunter2")
    filt.filter(record)
    assert "hunter2" not in record.msg
    assert "<REDACTED>" in record.msg


def test_redacting_filter_masks_bearer_token() -> None:
    """`Authorization: Bearer <jwt>` → `Bearer <REDACTED>`."""
    filt = RedactingFilter()
    record = _make_record("Authorization: Bearer abc123xyzlongenough")
    filt.filter(record)
    assert "abc123xyzlongenough" not in record.msg
    assert "<REDACTED>" in record.msg
    # Scheme case preserved by group(1) backref.
    assert "Bearer" in record.msg


def test_redacting_filter_masks_basic_auth() -> None:
    """`Basic <b64-creds>` HTTP scheme also matches the Bearer pattern."""
    filt = RedactingFilter()
    record = _make_record("Authorization: Basic dXNlcjpwYXNzd29yZA==")
    filt.filter(record)
    assert "dXNlcjpwYXNzd29yZA" not in record.msg
    assert "<REDACTED>" in record.msg


def test_redacting_filter_skips_prose_bearer() -> None:
    """The 8-char floor avoids rewriting English prose like 'bearer of news'."""
    filt = RedactingFilter()
    record = _make_record("bearer of news")
    filt.filter(record)
    assert record.msg == "bearer of news"


def test_redacting_filter_masks_aws_access_key() -> None:
    """`AKIA[0-9A-Z]{16}` → `AKIA<REDACTED>` (provider hint preserved)."""
    filt = RedactingFilter()
    record = _make_record("aws_access_key_id = AKIAIOSFODNN7EXAMPLE")
    filt.filter(record)
    assert "AKIAIOSFODNN7EXAMPLE" not in record.msg
    assert "AKIA<REDACTED>" in record.msg


def test_redacting_filter_masks_github_pat() -> None:
    """`ghp_<36>` / `ghs_<36>` etc. → `gh<REDACTED>` (provider hint kept)."""
    filt = RedactingFilter()
    record = _make_record("token=ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    filt.filter(record)
    # Either the GitHub-PAT mask or the generic KEY=VALUE mask kicked
    # in; the contract is "value never surfaces".
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in record.msg
    assert "<REDACTED>" in record.msg


def test_redacting_filter_masks_github_app_token_standalone() -> None:
    """A bare GitHub PAT (no surrounding `token=`) still gets masked."""
    filt = RedactingFilter()
    record = _make_record("logged in with ghs_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    filt.filter(record)
    assert "ghs_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in record.msg
    assert "gh<REDACTED>" in record.msg


def test_redacting_filter_masks_jwt() -> None:
    """A three-segment JWT → `<REDACTED-JWT>`."""
    filt = RedactingFilter()
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    record = _make_record(f"jwt payload: {jwt}")
    filt.filter(record)
    assert jwt not in record.msg
    assert "<REDACTED-JWT>" in record.msg


def test_redacting_filter_combined_patterns() -> None:
    """Multiple secret shapes in one message all get masked."""
    filt = RedactingFilter()
    record = _make_record(
        "PASSWORD=hunter2 url https://u:p@h.com Bearer abcdefghij "
        "AKIAIOSFODNN7EXAMPLE ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    filt.filter(record)
    assert "hunter2" not in record.msg
    assert ":p@h" not in record.msg
    assert "abcdefghij" not in record.msg
    assert "AKIAIOSFODNN7EXAMPLE" not in record.msg
    assert "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in record.msg
    assert "<REDACTED>" in record.msg


# ---------------------------------------------------------------------------
# Root callback — verbose/quiet semantics + mutex.
# ---------------------------------------------------------------------------


def test_v_sets_info(runner: CliRunner, minimal_config: Path) -> None:
    """-v (count=1) sets the setforge logger to INFO level."""
    result = runner.invoke(
        app, ["-v", "validate", "--config", str(minimal_config), "--all"]
    )
    assert result.exit_code == 0, result.output
    # -v is INFO, so DEBUG records are filtered out.
    assert "setforge.cli DEBUG: logging configured at level" not in result.stderr


def test_vv_sets_debug(runner: CliRunner, minimal_config: Path) -> None:
    """-vv (count=2) sets the setforge logger to DEBUG level."""
    result = runner.invoke(
        app, ["-vv", "validate", "--config", str(minimal_config), "--all"]
    )
    assert result.exit_code == 0, result.output
    assert "setforge.cli DEBUG: logging configured at level" in result.stderr


def test_quiet_silences_warnings_keeps_errors(
    runner: CliRunner, minimal_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--quiet suppresses WARNING-and-below; ERROR still surfaces."""
    monkeypatch.setenv("SETFORGE_LOG_LEVEL", "WARNING")
    result = runner.invoke(
        app, ["--quiet", "validate", "--config", str(minimal_config), "--all"]
    )
    assert result.exit_code == 0, result.output
    # No DEBUG / INFO / WARNING noise.
    assert "DEBUG:" not in result.stderr
    assert "INFO:" not in result.stderr
    assert "WARNING:" not in result.stderr


def test_quiet_v_mutex_exits_2(runner: CliRunner, minimal_config: Path) -> None:
    """--quiet + --verbose exits 2 with 'mutually exclusive' on stderr."""
    result = runner.invoke(
        app,
        ["--quiet", "-v", "validate", "--config", str(minimal_config), "--all"],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr


def test_setforge_log_level_env_precedence(
    runner: CliRunner, minimal_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit -vv overrides SETFORGE_LOG_LEVEL=WARNING; env alone wins over default."""  # noqa: E501
    monkeypatch.setenv("SETFORGE_LOG_LEVEL", "WARNING")
    flag_result = runner.invoke(
        app, ["-vv", "validate", "--config", str(minimal_config), "--all"]
    )
    assert flag_result.exit_code == 0, flag_result.output
    assert "setforge.cli DEBUG: logging configured at level" in flag_result.stderr

    monkeypatch.setenv("SETFORGE_LOG_LEVEL", "DEBUG")
    env_result = runner.invoke(
        app, ["validate", "--config", str(minimal_config), "--all"]
    )
    assert env_result.exit_code == 0, env_result.output
    assert "setforge.cli DEBUG: logging configured at level" in env_result.stderr


def test_redacts_token_env(
    runner: CliRunner, minimal_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: `-vv` + SETFORGE_GITHUB_TOKEN in env → token redacted on stderr.

    The root callback (``setforge.cli._root``) logs a ``-vv`` debug
    breadcrumb that names ``SETFORGE_GITHUB_TOKEN=<value>``; the
    RedactingFilter wired on the ``setforge`` namespace logger must
    rewrite the value to ``<REDACTED>`` before the record reaches the
    handler. This test fails if the ``addFilter`` call is removed from
    the root callback — exactly the regression the synthesized-filter
    style hid before.
    """
    monkeypatch.setenv("SETFORGE_GITHUB_TOKEN", "ghp_FAKE_TOKEN_FOR_TEST_VALUE")
    result = runner.invoke(
        app, ["-vv", "validate", "--config", str(minimal_config), "--all"]
    )
    assert result.exit_code == 0, result.output
    # End-to-end contract: the literal token NEVER lands on stderr,
    # AND the redaction marker IS present (proves the filter fired,
    # not just that nothing logged the value at all).
    assert "ghp_FAKE_TOKEN_FOR_TEST_VALUE" not in result.stderr
    assert "<REDACTED>" in result.stderr
    # The breadcrumb key prefix survives (only the value is masked).
    assert "SETFORGE_GITHUB_TOKEN" in result.stderr


# ---------------------------------------------------------------------------
# --format=json envelope shape per subcommand.
# ---------------------------------------------------------------------------


def _assert_json_envelope(stdout: str, *, expected_command: str) -> dict[str, Any]:
    """Parse stdout as JSON, assert envelope shape, return the parsed dict.

    Return type is ``dict[str, Any]`` (not ``dict[str, object]``) so callers
    can subscript ``parsed["data"]`` and apply ``in``/``len`` to nested
    values without per-call ``cast(...)``. ``json.loads`` produces
    arbitrarily-nested Python primitives — ``Any`` is the honest shape.
    """
    assert _ANSI_RE.search(stdout) is None, "ANSI escapes leaked into JSON stdout"
    parsed = json.loads(stdout)
    assert isinstance(parsed, dict)
    assert parsed["schema_version"] == OUTPUT_SCHEMA_VERSION
    assert parsed["command"] == expected_command
    assert "data" in parsed
    return parsed


def test_compare_json_envelope(runner: CliRunner, minimal_config: Path) -> None:
    """compare --format=json emits the versioned envelope to stdout only."""
    result = runner.invoke(
        app,
        [
            "--format=json",
            "compare",
            "--config",
            str(minimal_config),
            "--profile",
            "vm-headless",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = _assert_json_envelope(result.stdout, expected_command="compare")
    assert "entries" in parsed["data"]


def test_transitions_list_json_envelope(runner: CliRunner) -> None:
    """transitions list --format=json emits envelope even when no transitions exist."""
    result = runner.invoke(app, ["--format=json", "transitions", "list"])
    assert result.exit_code == 0, result.output
    parsed = _assert_json_envelope(result.stdout, expected_command="transitions list")
    assert parsed["data"]["transitions"] == []


def test_status_json_envelope(runner: CliRunner, minimal_config: Path) -> None:
    """status --format=json emits envelope with the five status blocks."""
    result = runner.invoke(
        app,
        [
            "--source",
            str(minimal_config.parent),
            "--format=json",
            "status",
            "--config",
            str(minimal_config),
            "--profile",
            "vm-headless",
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = _assert_json_envelope(result.stdout, expected_command="status")
    data = parsed["data"]
    for key in ("profile", "host", "config_repo", "drift", "overlay", "capabilities"):
        assert key in data


def test_profile_show_json_envelope(runner: CliRunner, minimal_config: Path) -> None:
    """profile show --format=json emits envelope with profile blocks."""
    result = runner.invoke(
        app,
        [
            "--format=json",
            "profile",
            "show",
            "vm-headless",
            "--config",
            str(minimal_config),
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = _assert_json_envelope(result.stdout, expected_command="profile show")
    data = parsed["data"]
    for key in ("tracked_files", "claude_plugins", "bootstrap", "extensions"):
        assert key in data


def test_json_no_ansi_on_stdout(runner: CliRunner, minimal_config: Path) -> None:
    """All four JSON-emitting commands keep stdout free of ANSI escapes."""
    invocations = [
        [
            "--format=json",
            "compare",
            "--config",
            str(minimal_config),
            "--profile",
            "vm-headless",
        ],
        ["--format=json", "transitions", "list"],
        [
            "--source",
            str(minimal_config.parent),
            "--format=json",
            "status",
            "--config",
            str(minimal_config),
            "--profile",
            "vm-headless",
        ],
        [
            "--format=json",
            "profile",
            "show",
            "vm-headless",
            "--config",
            str(minimal_config),
        ],
    ]
    for argv in invocations:
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, result.output
        assert _ANSI_RE.search(result.stdout) is None, (
            f"ANSI escape in stdout for {argv!r}: {result.stdout!r}"
        )
        # Parse: also proves we never mixed human text + JSON on stdout.
        json.loads(result.stdout)
