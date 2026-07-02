"""``sync --auto=use-live`` host-local OVERLAY capture test — removed in the cutover.

The single test that lived here
(``test_sync_uselive_overlay_keep_localyaml_write_is_revertible``) pinned the
capture-time OVERLAY body flow: an install migrated a legacy
``host_local_sections`` block into ``spans`` overlay entries and injected the
body into the live file, a hand-edit was captured back into ``local.yaml`` via
``write_edited_body_to_local``, and ``revert`` restored the pre-sync
``local.yaml``. That machinery (the ``spans`` overlay representation, the
overlay-body capture wizard, and host-local section injection into live) was
retired with the ``Disposition``/``spans``/overlay cutover (schema 3.0), so the
test was deleted rather than rewritten — its subject no longer exists.
"""

from __future__ import annotations
