"""The reconcile-store error subtree exists and is correctly rooted."""

from __future__ import annotations

from setforge import errors


def test_reconcile_error_hierarchy() -> None:
    assert issubclass(errors.ReconcileStoreError, errors.SetforgeError)
    for cls in (
        errors.CorruptIndexError,
        errors.IndexVersionError,
        errors.InvariantViolation,
        errors.UnsafeFileId,
    ):
        assert issubclass(cls, errors.ReconcileStoreError)
