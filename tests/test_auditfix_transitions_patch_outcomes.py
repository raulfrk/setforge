"""Regression tests for audit finding ``transitions_patch_outcomes``.

1. An install whose only change is a zero-byte file creation still records a
   transition, so revert can remove the file.

2. :func:`load_reconcile_outcomes` read ``reconcile_outcomes.json`` with an
   unguarded ``json.loads``, so a truncated / hand-corrupted file raised a
   bare :class:`json.JSONDecodeError` (not a :class:`SetforgeError`),
   escaping the top-level CLI handler as an opaque traceback.
"""

import json
from pathlib import Path

import pytest

from setforge.cli._install_helpers import DeployOutcome, _install_recorded_nothing
from setforge.errors import InvalidTransitionRecord
from setforge.transitions import TransitionDir, load_reconcile_outcomes
from tests.shared_helpers import text_images

# ---------------------------------------------------------------------------
# Finding 1 — a zero-byte creation is a recorded change
# ---------------------------------------------------------------------------


def test_empty_file_creation_is_not_classified_as_no_transition(
    tmp_path: Path,
) -> None:
    target = tmp_path / "empty.conf"

    assert not _install_recorded_nothing(
        file_pre=text_images({target: None}),
        file_post=text_images({target: ""}),
        deploy_outcome=DeployOutcome(),
        ext_delta=None,
        plugin_delta=None,
        mcp_delta=None,
        reconcile_outcomes=(),
        seeded=False,
    )


# ---------------------------------------------------------------------------
# Finding 2 — load_reconcile_outcomes failure branches
# ---------------------------------------------------------------------------


def test_load_reconcile_outcomes_corrupt_json_raises(tmp_path: Path) -> None:
    """A truncated / hand-corrupted ``reconcile_outcomes.json`` raises
    :class:`InvalidTransitionRecord` (a :class:`SetforgeError` subclass),
    NOT a bare :class:`json.JSONDecodeError` that would escape the CLI
    handler as a traceback."""
    (tmp_path / "reconcile_outcomes.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(InvalidTransitionRecord):
        load_reconcile_outcomes(TransitionDir(tmp_path))


def test_load_reconcile_outcomes_corrupt_json_is_not_jsondecodeerror(
    tmp_path: Path,
) -> None:
    """Pin the exception type precisely: the raw decode error must be
    wrapped, not leaked."""
    (tmp_path / "reconcile_outcomes.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(InvalidTransitionRecord):
        load_reconcile_outcomes(TransitionDir(tmp_path))
    # And it must NOT surface as the bare decoder error.
    try:
        load_reconcile_outcomes(TransitionDir(tmp_path))
    except json.JSONDecodeError:  # pragma: no cover - regression guard
        pytest.fail("bare json.JSONDecodeError escaped load_reconcile_outcomes")
    except InvalidTransitionRecord:
        pass


def test_load_reconcile_outcomes_non_dict_raises(tmp_path: Path) -> None:
    """A shape-valid-JSON-but-wrong-type top level (a list, not a dict)
    raises :class:`InvalidTransitionRecord` with the documented message."""
    (tmp_path / "reconcile_outcomes.json").write_text("[]", encoding="utf-8")
    with pytest.raises(InvalidTransitionRecord, match="top-level must be a dict"):
        load_reconcile_outcomes(TransitionDir(tmp_path))
