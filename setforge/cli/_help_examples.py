"""Module-level ``epilog=`` constants for every Typer leaf command.

One module-level constant per ``@app.command()`` / ``@<group>.command()``
registration. Each constant is the literal string passed as
the ``epilog`` keyword to the Typer decorator; it always ends with a
trailing newline so ``--help`` output renders cleanly under Click's
epilog renderer.

Newline preservation uses the Click ``\\b`` idiom (Click issue #56):
prefix any multi-line block with ``\\b`` on its own line so Click
does not collapse the embedded line breaks. The placeholder for the
profile name is ``<profile>`` (neutral, host-independent) — never a
real profile string from any host (would leak personal config into
engine ``--help`` output).

Each example references at least one flag from the command's
current surface (e.g. ``install`` mentions ``--dry-run`` and
``--auto=*``).
"""

INSTALL_EXAMPLES: str = """\
Examples:

\b
  # Most common: deploy tracked → live with auto-accept-tracked
  setforge install --profile=<profile> --auto=use-tracked --yes

\b
  # Preview without mutating the filesystem
  setforge install --profile=<profile> --dry-run

\b
  # Non-interactive (cron / CI), skip the secrets scan for speed
  setforge install --profile=<profile> --auto=use-tracked --yes --no-secrets-scan
"""

COMPARE_EXAMPLES: str = """\
Examples:

\b
  # Most common: read-only drift summary table
  setforge compare --profile=<profile>

\b
  # CI gate: exit non-zero on any drift (expected or unexpected)
  setforge compare --profile=<profile> --check --strict

\b
  # Show unified diff bodies below the summary
  setforge compare --profile=<profile> --full-diff
"""

CLEANUP_ORPHANS_EXAMPLES: str = """\
Examples:

\b
  # Most common: dry-run (no --apply prints what would be deleted)
  setforge cleanup-orphans --profile=<profile>

\b
  # Actually delete orphans with the wizard
  setforge cleanup-orphans --profile=<profile> --apply

\b
  # Non-interactive (CI / cron)
  setforge cleanup-orphans --profile=<profile> --apply --yes
"""

CAPTURE_EXAMPLES: str = """\
Examples:

\b
  # Most common: capture live edits back into tracked
  setforge capture --profile=<profile>

\b
  # Non-interactive: absorb every drift
  setforge capture --profile=<profile> --auto=use-live

\b
  # Non-interactive: reject every drift (no tracked mutations)
  setforge capture --profile=<profile> --auto=keep-tracked
"""

SYNC_EXAMPLES: str = """\
Examples:

\b
  # Most common: sync live → tracked (interactive on drift)
  setforge sync --profile=<profile>

\b
  # Non-interactive: absorb every drift (today's silent-absorb)
  setforge sync --profile=<profile> --auto=use-live --yes

\b
  # Non-interactive: reject every drift
  setforge sync --profile=<profile> --auto=keep-tracked --yes
"""

REVERT_EXAMPLES: str = """\
Examples:

\b
  # Most common: revert the latest install/sync transition
  setforge revert --profile=<profile>

\b
  # Multi-step: revert the named transition AND every newer one
  setforge revert --profile=<profile> --to-before=20260519T120000Z-abcd

\b
  # Non-interactive (CI / cron) — skip the confirm-explain-redo prompt
  setforge revert --profile=<profile> --yes
"""

TRANSITIONS_LIST_EXAMPLES: str = """\
Examples:

\b
  # Most common: list every transition (newest-first)
  setforge transitions list

\b
  # Filter to one profile
  setforge transitions list --profile=<profile>

\b
  # Reverse to chronological order
  setforge transitions list --oldest-first
"""

TRANSITIONS_SHOW_EXAMPLES: str = """\
Examples:

\b
  # Most common: show full audit panel by dirname prefix
  setforge transitions show 20260519T120000Z
"""

EXT_LIST_EXAMPLES: str = """\
Examples:

\b
  # Most common: show declared vs installed extensions
  setforge ext list --profile=<profile>
"""

EXT_ADD_EXAMPLES: str = """\
Examples:

\b
  # Most common: add an extension and install it via 'code'
  setforge ext add ms-python.python --profile=<profile>

\b
  # YAML-only edit (skip the 'code --install-extension' call)
  setforge ext add ms-python.python --profile=<profile> --no-install
"""

EXT_REMOVE_EXAMPLES: str = """\
Examples:

\b
  # Most common: remove from include only
  setforge ext remove ms-python.python --profile=<profile>

\b
  # Also add to exclude so reconcile actively uninstalls
  setforge ext remove ms-python.python --profile=<profile> --exclude
"""

EXT_RECONCILE_EXAMPLES: str = """\
Examples:

\b
  # Most common: apply reconcile (install/uninstall to match YAML)
  setforge ext reconcile --profile=<profile>

\b
  # Read-only: compute actions without invoking 'code'
  setforge ext reconcile --profile=<profile> --dry-run
"""

PLUGIN_LIST_EXAMPLES: str = """\
Examples:

\b
  # Most common: show declared vs installed Claude plugins
  setforge plugin list --profile=<profile>
"""

PLUGIN_ADD_EXAMPLES: str = """\
Examples:

\b
  # Most common: add a plugin from a GitHub marketplace
  setforge plugin add my-plugin --from=github:owner/repo --profile=<profile>

\b
  # YAML-only edit (skip the 'claude plugin install' call)
  setforge plugin add my-plugin --from=github:owner/repo \\
      --profile=<profile> --no-install
"""

PLUGIN_REMOVE_EXAMPLES: str = """\
Examples:

\b
  # Most common: remove from YAML
  setforge plugin remove my-plugin --profile=<profile>

\b
  # Also run 'claude plugin disable' after the YAML edit
  setforge plugin remove my-plugin --profile=<profile> --disable
"""

PLUGIN_RECONCILE_EXAMPLES: str = """\
Examples:

\b
  # Most common: apply reconcile (install/disable to match YAML)
  setforge plugin reconcile --profile=<profile>

\b
  # Read-only: compute actions without invoking 'claude'
  setforge plugin reconcile --profile=<profile> --dry-run
"""

PLUGIN_SYNC_CACHE_EXAMPLES: str = """\
Examples:

\b
  # Most common: refresh marketplace caches (local-clone install mode)
  setforge plugin sync-cache --profile=<profile>
"""

MARKETPLACE_ADD_EXAMPLES: str = """\
Examples:

\b
  # Most common: register a GitHub-backed marketplace
  setforge marketplace add my-marketplace --from=github:owner/repo

\b
  # Register a local-path marketplace (for development)
  setforge marketplace add my-marketplace --from=path:/srv/marketplaces/local
"""

MARKETPLACE_REMOVE_EXAMPLES: str = """\
Examples:

\b
  # Most common: remove a marketplace from YAML
  setforge marketplace remove my-marketplace
"""

MARKETPLACE_UPDATE_EXAMPLES: str = """\
Examples:

\b
  # Most common: refresh one marketplace's cache + claude registration
  setforge marketplace update my-marketplace
"""

VALIDATE_EXAMPLES: str = """\
Examples:

\b
  # Most common: validate one profile's config shape (no FS comparison)
  setforge validate --profile=<profile>

\b
  # CI gate: validate every profile in setforge.yaml
  setforge validate --all
"""

FETCH_EXAMPLES: str = """\
Examples:

\b
  # Most common: clone/fetch the configured git source
  setforge fetch
"""

SECTION_EMIT_EXAMPLES: str = """\
Examples:

\b
  # Most common: print a paste-ready shared marker pair
  setforge section emit shared my-section

\b
  # Host-local marker pair (always preserved-live)
  setforge section emit host-local my-section
"""

SECTION_ADD_EXAMPLES: str = """\
Examples:

\b
  # Most common: interactively insert a marker pair via the wizard
  setforge section add --profile=<profile>

\b
  # Non-interactive: every field pre-supplied via flags
  setforge section add --profile=<profile> --tracked-file=claude_md \\
      --semantics=shared --name=my-section --anchor-line=42 \\
      --body-source=empty --yes
"""

INIT_EXAMPLES: str = """\
Examples:

\b
  # Most common: bootstrap config dirs + local.yaml interactively
  setforge init

\b
  # Read-only health check (no mutations)
  setforge init --check

\b
  # Non-interactive: pre-select a path source for the local.yaml
  setforge init --no-prompt --path-source=/srv/setforge-config
"""

UPGRADE_EXAMPLES: str = """\
Examples:

\b
  # Most common: interactive upgrade (PyPI check + release notes)
  setforge upgrade

\b
  # Read-only: report current vs latest with release notes
  setforge upgrade --check

\b
  # Non-interactive (cron / CI): pick the recommended choice
  setforge upgrade --no-prompt
"""

MIGRATE_EXAMPLES: str = """\
Examples:

\b
  # Most common: read-only inventory of migrations needed
  setforge migrate --check

\b
  # Apply the migration chain after multi-file confirm
  setforge migrate --apply

\b
  # Non-interactive: apply without the confirm prompt
  setforge migrate --apply --yes
"""

STATUS_EXAMPLES: str = """\
Examples:

\b
  # Most common: one-screen summary for a profile
  setforge status --profile=<profile>
"""

PROFILE_LIST_EXAMPLES: str = """\
Examples:

\b
  # Most common: list every profile with its extends: chain
  setforge profile list
"""

PROFILE_SHOW_EXAMPLES: str = """\
Examples:

\b
  # Most common: render the fully-resolved profile with provenance tags
  setforge profile show <profile>
"""

SNAPSHOT_CREATE_EXAMPLES: str = """\
Examples:

\b
  # Most common: capture a snapshot before an experiment
  setforge snapshot create before-experiment --profile=<profile>

\b
  # Override retention (default keeps 10)
  setforge snapshot create before-experiment --profile=<profile> --keep=3
"""

SNAPSHOT_LIST_EXAMPLES: str = """\
Examples:

\b
  # Most common: list every snapshot (newest-first)
  setforge snapshot list
"""

SNAPSHOT_RESTORE_EXAMPLES: str = """\
Examples:

\b
  # Most common: restore by label (interactive arrow-key confirm)
  setforge snapshot restore before-experiment --profile=<profile>

\b
  # Non-interactive (CI / cron): skip the confirm and pre-restore snapshot
  setforge snapshot restore before-experiment --profile=<profile> \\
      --yes --non-interactive
"""

COMPLETION_INSTALL_EXAMPLES: str = """\
Examples:

\b
  # Most common: install zsh completion + wire the rc file
  setforge completion install zsh

\b
  # Non-interactive: write the script but do not edit the rc file
  setforge completion install bash --non-interactive --no-wire
"""
