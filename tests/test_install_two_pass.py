"""Two-pass install ``--strict-spans`` tests — removed in the spans cutover.

The ``--strict-spans`` pinned-orphan refuse-before-write gate and the
disposition-base auto-migration these tests pinned were retired together with
the ``Disposition`` enum and the ``spans`` tracked-file field (schema 3.0).
Every tracked file now reconciles through the unified 3-way engine, which has
no spans-orphan refusal trigger, so the three tests that lived here
(``test_strict_refusal_on_later_file_leaves_all_files_unwritten``,
``test_pass1_defers_base_migration_write``,
``test_non_strict_orphan_still_warns_and_proceeds``) were deleted rather than
rewritten — their subject no longer exists.
"""

from __future__ import annotations
