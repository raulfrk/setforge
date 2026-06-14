# Compatibility policy

This document is the standing **compatibility contract** that setforge's
config and migration layer commits to **as of v0.3.0 (in progress)**. It
states the guarantees the schema/migration work must deliver — how the
`setforge.yaml` schema is allowed to evolve, what the release process must
guarantee, and what users can rely on across engine versions. It is a forward
specification, not a description of what any one shipped engine already does;
the v0.3.0 schema/migration work implements against it.

Under this contract, every `setforge.yaml` carries a `schema_version`, and the
engine guarantees that a config written for an older schema upgrades to the
current schema with no manual edits and no data loss (see *Upgrade* below for
the mechanism's bound).

## Principles

- **Additive-first.** New schema fields are added, never repurposed. An
  existing field's name, type, and meaning are fixed once shipped; a new
  capability gets a new field rather than overloading an old one.
- **Breaking changes go expand → contract.** A field is never removed in a
  single step. During the *expand* window the old field is retained and stays
  readable alongside its replacement; the *contract* step removes the old field
  only after that window closes. There is no hard removal.
- **Every `schema_version` bump ships migrations both ways.** A version bump
  must not be considered done until it registers a forward (up) Migration *and*
  its reverse (down) migration. The reverse is what makes a cross-major
  downgrade a single command rather than a manual rewrite.
- **Forward-tolerant reading.** An older engine reading config written by a
  newer engine must ignore fields it does not recognize instead of crashing.
  Newer config stays loadable on an older engine, minus the features that older
  engine never had.
- **No removal without a deprecation window.** A field marked for removal must
  be announced as deprecated, kept functional through the expand window, and
  only dropped at the contract step in a later release. Users always get a
  release in which both the old and new shapes work.

## Guarantee scope

The principles above resolve to four concrete guarantees the engine must
honor. Each is stated with its exact bound — what holds always, and what holds
only within a window.

### Backward compatibility — full and permanent

A newer engine must fully understand config written for any older
`schema_version`. Old configs keep working, with full functionality, with no
edits required from the user. This guarantee does not expire.

### Forward compatibility — forward-safe within a major, refuse across a major

*Within a major version*, an older engine must never crash on newer config: it
reads what it understands and ignores unknown fields, emitting a warning naming
each ignored key. That *forward-safe* behavior is permanent for the major, and
is safe precisely because same-major changes are additive-only (see
*Principles*) — an unrecognized field never changes the meaning of a field the
engine already knows. *Full* forward functionality — the older engine acting on
everything the config expresses — holds only within the expand-contract window,
while the fields it knows are still present. Once a field has passed through
contract, an older engine simply will not see it.

*Across a major boundary* the guarantee changes. A major bump is where the
schema may restructure or retire fields, so an older engine cannot safely act
on a newer-major config. Rather than best-effort read it, the engine **refuses
cleanly** — a one-line `upgrade setforge to >= N.0` message and a non-zero exit,
mutating nothing. A clean refusal is **distinct from a crash**: the user gets an
actionable instruction, never a Python traceback. To run an older engine against
a newer-major config, first down-convert it on the newer engine with
`setforge migrate --to=<older>`.

### Upgrade — always zero-touch

Moving to a newer engine must never require the user to edit config by hand.
The engine guarantees that the registered forward migrations bring an older
config up to the current schema with zero data loss, across any version
distance. (The mechanism is an explicit, confirm-gated migration step — diff
preview plus backups — not a silent rewrite; the guarantee is the zero-touch,
zero-loss outcome, not that it happens invisibly on read.)

### Downgrade — zero-touch within the window, one command across a major

Downgrading to an older engine is zero-touch *within a major version* and
*within an open expand-contract window*: forward-tolerant reading covers it.
Across a *major* boundary, downgrade is a single command — the reverse
migrations registered at each bump rewrite the config back down to the target
schema.

### Stated limit

These guarantees cover schema shape, not deleted data. An OLD engine that has
already shipped cannot reconstruct data that a NEWER engine deleted: it has no
knowledge of fields introduced after it was built, and a reverse migration runs
on the engine that *defined* it, not on the older engine reading the result.
Downgrade restores the older *schema*; it cannot restore values the newer
engine chose to drop.

## Auto-on-install file migration — a separate class

The guarantees above govern **`setforge.yaml` schema migrations**: explicit,
`schema_version`-gated, confirm-gated transformations of the config document,
driven by `setforge migrate`. There is a **second, distinct migration class**
that this contract calls out separately so it is not confused with the schema
mechanism: the **auto-on-install file migration** that runs against a *deployed
live file* (not the config) the first time it installs under a stored-base
`disposition`.

This class is **not** a `schema_version` bump and does **not** go through
`setforge migrate`. It runs automatically inside `setforge install`, once per
file, when a `disposition`-bearing tracked file's first install finds **no
stored base yet** — and, for markdown files, a live file still carrying legacy
shared-section markers. On that first install the engine:

- seeds a **per-host base** from the current live file (the merge ancestor the
  stored-base three-way model needs), and
- strips legacy **shared-section** markers from the live file in place (markdown
  only; host-local markers are left untouched, and structured files have none),
  leaving every body byte intact.

It honors the same **additive-first / expand → contract** framing as the schema
class: the stored-base model is the *expand* shape introduced alongside the
legacy marker model, and the auto-migration is the one-time *contract* step that
retires the legacy markers for a given file. No live body content is dropped:
the seeded base equals the stripped-live file, so the first three-way merge has
zero spurious delta. It differs from the schema class on two axes:

- **Backup-not-prompt, no interactive gate.** Unlike `setforge migrate`'s
  diff-preview confirm, the auto-on-install migration runs without prompting. It
  is safe to do so because it is fully **reversible** (below) and emits a
  **one-time, per-file warning** naming what changed and how to undo it.
- **Reversible via `setforge revert`, not a down-migration.** There is no
  registered reverse *schema* migration for it. Instead, the seeded base and the
  in-place live rewrite are both captured in the install transition, so a single
  `setforge revert --profile=<profile>` restores the pre-migration live file
  **and** removes the seeded base in lockstep — returning the file to exactly its
  pre-install state with no stranded base for the next install to mis-merge
  against.

## `validate` orphan-overlay diagnostics

`local.yaml` may carry `tracked_files.<id>` overlay entries (per-host `mode` /
`dst` / `symlink_target` / `disposition` / `spans` knobs). `setforge install`,
`sync`, and `override` **silently skip** an overlay entry whose `id` is not in
the resolved profile — their exit codes and output are unaffected by a stale or
typo'd entry. The two read-only diagnosis verbs surface those skipped entries
instead:

- `setforge validate --profile=X` **exits 1** when an overlay `id` appears
  nowhere in `setforge.yaml`'s `tracked_files` (a typo or stale entry), with a
  "Did you mean '<close-match>'" suggestion over the known ids. An `id` that
  **is** declared in `setforge.yaml` but not used by the validated profile(s) is
  an off-profile entry (legitimate on a multi-profile host): `validate` prints a
  non-fatal note to stderr and **exit stays 0**.
- `setforge compare --profile=X` lists every skipped entry under a `Skipped
  overlay entries (N):` block (human) and an additive top-level
  `orphan_overlay_entries: [{ "id", "class" }]` array (`--format=json`, where
  `class` is `unknown` or `off_profile`). The existing `--json` keys are
  untouched.

The unknown-id `validate` failure is a tightening of `validate`'s **diagnostic
strictness only** — it does not change any deploy/capture behavior or any
`schema_version`. A `local.yaml` that previously passed `validate` with a typo'd
overlay `id` now fails it; fix the `id` or remove the entry.
