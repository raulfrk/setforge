"""Pre-deploy secrets scan via the ``gitleaks`` subprocess.

Soft-requirement contract (modeled on ``claude`` / ``code`` per
``setforge/cli/_plugin_helpers.py:74`` and ``:128``): when the
``gitleaks`` binary is absent on PATH (resolved via
:func:`setforge.binaries.resolve_binary`), this module emits a single
yellow warning to stderr and returns an empty
:class:`SecretsScanResult`. No exception is raised; install continues.

Gitleaks exit-code triage per research brief §5:

- ``0`` — scan ran cleanly, no findings.
- ``1`` — scan found ≥1 finding; parse JSON, filter via allowlist. An
  *unparseable* exit-1 report raises :class:`SetforgeError` (fail
  closed) rather than degrading to a clean/empty result — gitleaks
  positively detected secrets, so a parse failure must block the
  install, never wave it through.
- other — scan-runtime failure; emit yellow warning + empty result;
  install proceeds (soft-requirement contract).

The allowlist file at ``~/.config/setforge/secrets-allowlist`` is keyed
on ``sha256(snippet)`` hex (NOT ``file:line`` — refactors invalidate
the latter per research brief §5 anti-pattern (2)).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

import typer

from setforge import binaries
from setforge.errors import SetforgeError

_DEFAULT_ALLOWLIST_PATH: Final[Path] = (
    Path.home() / ".config" / "setforge" / "secrets-allowlist"
)
_GITLEAKS_TIMEOUT_SECONDS: Final[int] = 60
_MISSING_BINARY_MESSAGE: Final[str] = (
    "warning: skipping pre-deploy secrets scan — gitleaks not found on PATH; "
    "install via https://github.com/gitleaks/gitleaks#installing for defense-in-depth"
)
_HASH_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class SecretAction(StrEnum):
    """Resolution chosen by the user for a single secret finding."""

    ABORT = "abort"
    ALLOWLIST = "allowlist"
    SILENCE_ONE_SHOT = "silence-one-shot"


@dataclass(slots=True, frozen=True)
class SecretFinding:
    """One gitleaks finding, normalized for the wizard + allowlist layer.

    ``snippet_hash`` is ``sha256(snippet)`` hex; it is the durable
    allowlist key (refactors that move ``file_path`` / ``line_number``
    leave the hash intact).
    """

    rule_id: str
    file_path: Path
    line_number: int
    snippet: str
    snippet_hash: str
    secret_kind: str


@dataclass(slots=True, frozen=True)
class SecretsScanResult:
    """Outcome of a single gitleaks invocation.

    ``findings`` is post-allowlist filtering; ``files_scanned`` is the
    real count of files under ``tracked_root`` walked for the scan,
    computed directly (gitleaks' JSON report carries no scanned-file
    count). It is ``0`` on paths where nothing was scanned (explicit
    skip, missing binary, timeout, or scan-runtime failure).
    """

    findings: tuple[SecretFinding, ...]
    files_scanned: int


def _sha256_hex(text: str) -> str:
    """Return ``sha256(text)`` as a lowercase hex string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _warn(message: str) -> None:
    """Emit a yellow warning to stderr (soft-requirement convention)."""
    typer.secho(message, err=True, fg=typer.colors.YELLOW)


def _load_allowlist(allowlist_path: Path) -> frozenset[str]:
    """Parse the allowlist file into a set of snippet-hash strings.

    Lines starting with ``#`` or empty after strip are ignored. Missing
    file returns empty set. Each remaining line contributes its
    first whitespace-separated token (so an inline comment after the
    hash on the same line is tolerated, though :func:`append_to_allowlist`
    writes the comment on a separate preceding line).

    Defense-in-depth: tokens that are not a 64-hex-char sha256 hash are
    rejected with a single yellow warning to stderr and excluded from
    the loaded set. Malformed entries would otherwise silently never
    match a real snippet hash — confusing UX for users hand-editing
    the file.
    """
    if not allowlist_path.exists():
        return frozenset()
    out: set[str] = set()
    for lineno, raw in enumerate(
        allowlist_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        token = stripped.split()[0]
        if not _HASH_RE.fullmatch(token):
            _warn(
                f"warning: secrets-allowlist line {lineno}: {token!r} "
                "is not a 64-hex sha256; ignoring"
            )
            continue
        out.add(token)
    return frozenset(out)


def _parse_gitleaks_json(stdout: str) -> tuple[SecretFinding, ...]:
    """Parse gitleaks' ``--report-format=json`` stdout into findings.

    Gitleaks v8 emits a JSON array of finding objects. This is only
    called on exit 1 (gitleaks positively detected ≥1 secret), so an
    *unparseable* report is a fail-open hazard: if we returned an empty
    tuple, the install gate (``if scan_result.findings: ...``) would read
    the scan as clean and deploy secrets that gitleaks just flagged.

    A valid JSON array (including an empty ``[]``) parses normally — zero
    findings after a successful parse means every leak was allowlisted.
    But empty / whitespace stdout, malformed JSON, or a non-array payload
    on an exit-1 scan all :class:`SetforgeError` rather than degrading the
    security gate to "clean proceed". The error is surfaced by the CLI
    top-level handler as ``error: <message>`` and exits 1, so a parse
    failure blocks the install instead of silently waving it through.
    """
    payload = stdout.strip()
    if not payload:
        raise SetforgeError(
            "gitleaks reported secrets (exit 1) but its report was empty; "
            "refusing to deploy on an unreadable secrets-scan result. "
            "Re-run the scan or check the gitleaks invocation."
        )
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SetforgeError(
            "gitleaks reported secrets (exit 1) but its JSON report could "
            f"not be parsed ({exc}); refusing to deploy on an unreadable "
            "secrets-scan result. Re-run the scan or check the gitleaks "
            "invocation."
        ) from exc
    if not isinstance(raw, list):
        raise SetforgeError(
            "gitleaks reported secrets (exit 1) but its JSON report was not "
            "a list; refusing to deploy on an unreadable secrets-scan "
            "result. Re-run the scan or check the gitleaks invocation."
        )
    out: list[SecretFinding] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        snippet = str(entry.get("Secret", entry.get("Match", "")))
        # StartLine is cosmetic; a non-numeric value (malformed report or a
        # future gitleaks shape change) must not raise a bare ValueError on
        # this fail-closed exit-1 path. Fall back to 0 so the finding is still
        # produced and the install gate still fires on the detected secret.
        try:
            line_number = int(entry.get("StartLine", 0) or 0)
        except (ValueError, TypeError):
            line_number = 0
        out.append(
            SecretFinding(
                rule_id=str(entry.get("RuleID", "")),
                file_path=Path(str(entry.get("File", ""))),
                line_number=line_number,
                snippet=snippet,
                snippet_hash=_sha256_hex(snippet),
                secret_kind=str(entry.get("Description", entry.get("RuleID", ""))),
            )
        )
    return tuple(out)


def _filter_allowlist(
    findings: tuple[SecretFinding, ...], allowlist_path: Path
) -> tuple[SecretFinding, ...]:
    """Drop findings whose ``snippet_hash`` appears in the allowlist file."""
    if not findings:
        return ()
    allow = _load_allowlist(allowlist_path)
    if not allow:
        return findings
    return tuple(f for f in findings if f.snippet_hash not in allow)


def run_pre_deploy_scan(
    *,
    tracked_root: Path,
    allowlist_path: Path = _DEFAULT_ALLOWLIST_PATH,
    skip: bool = False,
) -> SecretsScanResult:
    """Run ``gitleaks detect`` on ``tracked_root``; return filtered findings.

    Soft-requirement: if ``skip=True``, returns an empty result silently
    (no warning — explicit opt-out). If the binary is absent, emits the
    single yellow ``_MISSING_BINARY_MESSAGE`` warning and returns an
    empty result; install proceeds. Exit-code triage per research brief
    §5:

    - ``0`` — clean; empty result.
    - ``1`` — findings; parse JSON, filter via allowlist. An unparseable
      exit-1 report raises :class:`SetforgeError` (fail closed).
    - other — scan-runtime failure; yellow warning + empty result.

    Subprocess invocation uses ``check=False`` (manual exit-code
    handling), ``timeout=60`` (mandatory), and ``capture_output=True``
    per the CLAUDE.md subprocess-discipline rule.
    """
    if skip:
        return SecretsScanResult(findings=(), files_scanned=0)
    gitleaks_path = binaries.resolve_binary("gitleaks")
    if gitleaks_path is None:
        _warn(_MISSING_BINARY_MESSAGE)
        return SecretsScanResult(findings=(), files_scanned=0)
    file_count = sum(1 for p in tracked_root.rglob("*") if p.is_file())
    try:
        result = subprocess.run(
            [
                str(gitleaks_path),
                "detect",
                "--no-git",
                "--report-format=json",
                "--report-path=/dev/stdout",
                "--source",
                str(tracked_root),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GITLEAKS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        _warn(
            f"warning: gitleaks scan timed out after {_GITLEAKS_TIMEOUT_SECONDS}s; "
            "continuing without secrets check"
        )
        return SecretsScanResult(findings=(), files_scanned=0)
    except OSError as exc:
        # A which()-resolved gitleaks can still fail to exec (became
        # non-executable, broken shebang, removed in the TOCTOU window).
        # Degrade to the documented warn-and-continue contract rather
        # than crashing install with a raw traceback.
        _warn(
            f"warning: gitleaks scan could not be executed ({exc}); "
            "continuing without secrets check"
        )
        return SecretsScanResult(findings=(), files_scanned=0)
    if result.returncode == 0:
        return SecretsScanResult(findings=(), files_scanned=file_count)
    if result.returncode == 1:
        findings = _parse_gitleaks_json(result.stdout)
        filtered = _filter_allowlist(findings, allowlist_path)
        return SecretsScanResult(findings=filtered, files_scanned=file_count)
    _warn(
        f"warning: gitleaks scan failed (exit {result.returncode}): "
        f"{result.stderr.strip()}; continuing without secrets check"
    )
    return SecretsScanResult(findings=(), files_scanned=0)


def append_to_allowlist(*, snippet_hash: str, allowlist_path: Path) -> None:
    """Append ``snippet_hash`` to ``allowlist_path`` with an ISO-8601 comment.

    Creates parent directories and the file (with a header) on first
    use. Idempotent at the hash level: re-appending an already-present
    hash leaves the file structurally unchanged but adds a fresh
    timestamped comment line — callers that want strict idempotency
    should pre-check with :func:`_load_allowlist`.
    """
    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="seconds")
    header = (
        "# setforge secrets-allowlist — sha256(snippet) per line\n"
        "# Comment lines start with '#'; one hash per non-comment line.\n"
    )
    needs_header = not allowlist_path.exists() or not allowlist_path.read_text(
        encoding="utf-8"
    )
    with allowlist_path.open("a", encoding="utf-8") as fh:
        if needs_header:
            fh.write(header)
        fh.write(f"# Added {timestamp}\n{snippet_hash}\n")
