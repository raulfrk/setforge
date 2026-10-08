"""Regression test for audit finding ``transitions_patch_outcomes``.

An install whose only change is a zero-byte file creation still records a
transition, so revert can remove the file.
"""

from pathlib import Path

from setforge.cli._install_helpers import DeployOutcome, _install_recorded_nothing
from tests.shared_helpers import text_images


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
