"""F7 self-improvement loop — proposal schema, durable ledger, safe apply.

Importable contract: producers (mutmut, docs-sync, dep-triage, the review
agents) build a :class:`Proposal` and call :func:`emit`. Occurrence-count and
decline-suppress live in an **append-only JSONL ledger** (``.claude/proposals/
ledger.jsonl``). The ledger — not the project task tracker — is the source of
truth for count and suppress, because the tracker's store is embedded
single-writer and its compaction permanently removes closed items, which would
race the counter and resurrect declined proposals. The (exempt) ``surface-
proposals`` skill is what transitions filed proposals into the task tracker for
the human workflow; this module never touches the tracker.

Grounding (SELF-1): every proposal MUST cite an external signal in
``evidence`` (a gate verdict, a surviving mutant, a dismissed finding, a
template-drift diff) — pure self-eval is rejected at construction.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from enum import Enum, StrEnum, auto
from pathlib import Path


class Confidence(StrEnum):
    """How sure the producer is. Surfaced to the human; never gates."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Volatile-token scrubbers: the dedup key must be stable across runs, so a line
# number / timestamp / pid / absolute path in the evidence must not change it
# (else the same logical finding never reaches its 2nd sighting → never filed).
_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*")
_NUM = re.compile(r"#\d+|\bpid=\d+|\b(?:run|session)[-_]?id=\S+", re.IGNORECASE)
_LINE = re.compile(r":\d+\b")


def norm(evidence: str) -> str:
    """Strip volatile tokens so the same logical finding hashes identically.

    Removes timestamps, ``#NNN`` / ``pid=`` / ``run-id`` tokens and ``:line``
    suffixes, then replaces any path-like token (one containing ``/``) with a
    single ``<path>`` placeholder — the canonical location lives in the
    :attr:`Proposal.file` field, so an incidental path in the evidence text
    must not perturb the fingerprint.
    """
    s = _TS.sub("", evidence)
    s = _NUM.sub("", s)
    s = _LINE.sub("", s)
    toks = ["<path>" if "/" in t else t for t in s.split()]
    return " ".join(toks).strip().lower()


@dataclass(frozen=True, slots=True)
class Proposal:
    """A grounded suggestion a tool emits when it hits a gap.

    ``proposed_diff`` may be empty — that is a legal advisory-only card (it
    surfaces, but "approve" only acknowledges; there is nothing to apply).
    """

    source: str
    category: str
    evidence: str
    proposed_diff: str
    confidence: Confidence
    file: str

    def __post_init__(self) -> None:
        if not self.evidence.strip():
            raise ValueError("proposal evidence is required (SELF-1 grounding)")

    @property
    def dedup_key(self) -> str:
        """16-hex stable fingerprint over (source, category, file, norm(evidence))."""
        raw = f"{self.source}|{self.category}|{self.file}|{norm(self.evidence)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


class Ledger:
    """Append-only JSONL store: the durable truth for occurrence-count and
    decline-suppress.

    Each line is one event ``{key, event, source, category, file}`` where
    ``event`` is ``"seen"``, ``"declined"``, or ``"applied"``. Count is the
    cardinality of ``seen`` rows (never a mutated scalar — so concurrent
    writers cannot lose an increment), and a key is suppressed once a
    ``declined`` row exists for it (durable across sessions, immune to any
    downstream task-tracker compaction).

    Appends are serialized with ``fcntl.flock(LOCK_EX)`` on a sibling lockfile,
    mirroring :func:`setforge.locking.profile_lock`.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def _append(self, row: dict[str, str]) -> None:
        line = json.dumps(row, separators=(",", ":")) + "\n"
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _row(p: Proposal, event: str) -> dict[str, str]:
        # Carry the full payload so list_filed() can reconstruct a Proposal the
        # surface-proposals skill can render + apply.
        return {
            "key": p.dedup_key,
            "event": event,
            "source": p.source,
            "category": p.category,
            "file": p.file,
            "evidence": p.evidence,
            "proposed_diff": p.proposed_diff,
            "confidence": str(p.confidence),
        }

    @staticmethod
    def _to_proposal(row: dict[str, str]) -> Proposal:
        return Proposal(
            source=row["source"],
            category=row["category"],
            evidence=row["evidence"],
            proposed_diff=row["proposed_diff"],
            confidence=Confidence(row["confidence"]),
            file=row["file"],
        )

    def record_seen(self, p: Proposal) -> None:
        self._append(self._row(p, "seen"))

    def record_declined(self, p: Proposal) -> None:
        self._append(self._row(p, "declined"))

    def record_applied(self, p: Proposal) -> None:
        self._append(self._row(p, "applied"))

    def _rows(self) -> list[dict[str, str]]:
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def count(self, key: str) -> int:
        return sum(1 for r in self._rows() if r["key"] == key and r["event"] == "seen")

    def is_suppressed(self, key: str) -> bool:
        return any(r["key"] == key and r["event"] == "declined" for r in self._rows())

    def is_applied(self, key: str) -> bool:
        return any(r["key"] == key and r["event"] == "applied" for r in self._rows())

    def list_filed(self) -> list[Proposal]:
        """Proposals eligible for the human gate: seen >= 2, not declined, not
        applied. Returns the latest payload per key (supersede = newest wins)."""
        latest: dict[str, dict[str, str]] = {}
        seen: dict[str, int] = {}
        for r in self._rows():
            key = r["key"]
            if r["event"] == "seen":
                seen[key] = seen.get(key, 0) + 1
                latest[key] = r
        out: list[Proposal] = []
        for key, n in seen.items():
            if n >= 2 and not self.is_suppressed(key) and not self.is_applied(key):
                out.append(self._to_proposal(latest[key]))
        return out


# --------------------------------------------------------------------------- #
# Module-level loop API — what producers and the surface-proposals skill call.
# --------------------------------------------------------------------------- #
_DEFAULT_LEDGER = ".claude/proposals/ledger.jsonl"


class EmitResult(Enum):
    """Outcome of :func:`emit` — telemetry for the producer; the human-facing
    queue is always read via :func:`list_filed`."""

    DROPPED_SUPPRESSED = auto()
    HELD_FIRST_OCCURRENCE = auto()
    FILED = auto()


def _ledger() -> Ledger:
    return Ledger(Path(os.environ.get("SETFORGE_PROPOSALS_LEDGER", _DEFAULT_LEDGER)))


def emit(p: Proposal) -> EmitResult:
    """Record a grounded proposal. One-offs are held; a 2nd sighting of the
    same key files it (makes it visible to :func:`list_filed`). Declined keys
    are dropped silently."""
    led = _ledger()
    if led.is_suppressed(p.dedup_key):
        return EmitResult.DROPPED_SUPPRESSED
    led.record_seen(p)
    if led.count(p.dedup_key) < 2:
        return EmitResult.HELD_FIRST_OCCURRENCE
    return EmitResult.FILED


def decline(p: Proposal) -> None:
    """Record a human decline — durable suppress (the skill also closes the bead)."""
    _ledger().record_declined(p)


def mark_applied(p: Proposal) -> None:
    """Record that an approved proposal's diff was applied (drops it from the queue)."""
    _ledger().record_applied(p)


def list_filed() -> list[Proposal]:
    """The open human-facing queue (seen >= 2, not declined, not applied)."""
    return _ledger().list_filed()


# --------------------------------------------------------------------------- #
# Safe diff-apply — proposals are UNTRUSTED data; a proposed_diff may never
# write outside the repo or via a symlink, and is applied only after --check.
# --------------------------------------------------------------------------- #
class DiffRejected(Exception):
    """A proposed_diff failed safety validation or did not apply cleanly."""


_DIFF_TARGET = re.compile(r"^[+-]{3} (.+)$", re.MULTILINE)
_SYMLINK_HUNK = re.compile(r"^new file mode 120000$", re.MULTILINE)


def validate_diff_paths(diff: str, repo_root: str) -> None:
    """Reject a diff whose targets escape ``repo_root`` or create a symlink.

    Guards CVE-2023-23946 (symlink escape) and ordinary ``../`` / absolute-path
    traversal. ``git apply --check`` alone does not catch the symlink case.
    """
    if _SYMLINK_HUNK.search(diff):
        raise DiffRejected("symlink hunk (new file mode 120000) is not allowed")
    root = Path(repo_root).resolve()
    for m in _DIFF_TARGET.finditer(diff):
        raw = m.group(1).strip()
        if raw == "/dev/null":
            continue
        # strip the git a/ b/ prefix; anything else (incl. an absolute path) is kept
        target = raw[2:] if raw[:2] in ("a/", "b/") else raw
        if target.startswith("/") or Path(target).is_absolute():
            raise DiffRejected(f"absolute path target not allowed: {raw}")
        resolved = (root / target).resolve()
        if resolved != root and not str(resolved).startswith(str(root) + os.sep):
            raise DiffRejected(f"path escapes repo root: {raw}")


def approve(p: Proposal, *, repo_root: str = ".") -> None:
    """Apply an approved proposal's diff to the worktree (the human reviews the
    result at the normal Phase-6 gate; this never commits or merges).

    An empty ``proposed_diff`` is an advisory-only card — approving it just
    records the acknowledgement.
    """
    if not p.proposed_diff.strip():
        mark_applied(p)
        return
    validate_diff_paths(p.proposed_diff, repo_root)
    check = subprocess.run(
        ["git", "apply", "--check"],
        input=p.proposed_diff,
        text=True,
        capture_output=True,
        cwd=repo_root,
    )
    if check.returncode != 0:
        raise DiffRejected(f"git apply --check failed: {check.stderr.strip()}")
    subprocess.run(
        ["git", "apply"],
        input=p.proposed_diff,
        text=True,
        check=True,
        cwd=repo_root,
    )
    mark_applied(p)
