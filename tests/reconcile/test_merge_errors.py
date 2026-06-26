"""Merge error nodes under the reconcile error subtree."""

from __future__ import annotations

from setforge import errors


def test_merge_error_hierarchy() -> None:
    assert issubclass(errors.MergeError, errors.ReconcileStoreError)
    assert issubclass(errors.MergeInvariantError, errors.MergeError)
