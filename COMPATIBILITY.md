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
- **All-or-nothing, no per-step resume.** A failed *or* interrupted `setforge
  migrate --apply` rolls the whole chain back to its byte-exact origin (files,
  and the reconcile store for a store-cutover step) and exits. Re-running
  `--apply` restarts the entire chain from the origin schema — there is no
  resume from the failed step.

## User-section marker retirement — a one-way contract step

The `2.0 → 2.1` migration retires **user-section markers** — the HTML-comment
`<!-- setforge:user-section ... -->` pairs older markdown tracked files used to
delimit shared and host-local regions. It is the **contract** step of the
markerless overlay/span model introduced (as the *expand* shape) in the
preceding release: shared regions become ordinary tracked content and host-local
bodies move to `local.yaml` overlays, so the markers are no longer needed and
are stripped.

This step is the concrete instance of the *Stated limit* above: the retired
marker **syntax is deleted structure**, and a stateless reverse migration cannot
reconstruct it. The `2.0 → 2.1` bump therefore registers a reverse migration —
satisfying the *both ways* rule — but that reverse **refuses cleanly** rather
than emitting broken output. `setforge migrate --to=<2.0-or-lower>` across the
boundary exits non-zero with an explicit message, never a silent marker-less
config an older engine would misread — consistent with *clean refusal, never a
crash*.

Recovery is **transition-based, not schema-reverse-based.** The forward migration
records a revertible transition capturing the pre-migration bytes — the stripped
tracked + live sources and the seeded overlay / base store legs. While that
transition is retained, `setforge revert --profile=migrate` byte-restores the
original marker-bearing sources in lockstep — the recovery path a stateless
reverse migration cannot provide. This is the same `setforge revert` affordance
every install / migrate transition carries; there is no distinct irreversibility
cutover for user-section markers.

(The separate `migrate --finalize` command strips *vestigial host-local* markers
left by the earlier `1.1 → 1.2` conversion — floor-gated on
`markerless_conversion_schema_version` and likewise revertible via `setforge
revert --profile=migrate`. It is unrelated to the `2.0 → 2.1` user-section
retirement above.)

## Disposition / spans retirement — a one-way contract step

The `2.1 → 3.0` migration retires the legacy per-file reconciliation model — the
file-level `Disposition` (`shared` / `forked` / `pinned`), the sub-file `spans`
overlay, and the `scalar_base_store` — folding every deployed file into the
unified per-unit `SHARED` / `LOCAL` reconcile store introduced (as the *expand*
shape) across the preceding `4.15.x` releases. It is the **contract** step of
that model: `pinned` and host-local spans become ordinary `LOCAL` units,
`forked` files become all-`LOCAL`, and the legacy `Disposition`/`spans` config
fields and their stores are removed.

This is a **MAJOR** bump (`2 → 3`), so it is the concrete instance of the
*forward compatibility* limit above: an older (`2.x`) engine reading a `3.0`
config **refuses cleanly** (non-zero, no traceback) via the cross-major
`schema_version` guard, rather than silently misreading a config whose fields it
no longer understands.

The forward migration is **data-preserving**: it maps real deployed
`forked`/`pinned`/`spans`/`scalar-base` state onto the unified store, seeding
each unit's base from the tracked/upstream bytes (never a live or merge result).
But the collapse is **lossy in the reverse direction** — `pinned`/`forked`
distinctions dissolve into the binary `SHARED`/`LOCAL` classification — so, per
the *Stated limit*, the `3.0 → 2.1` schema reverse **refuses cleanly** rather
than emitting a config it cannot faithfully reconstruct. The bump still registers
that reverse (satisfying the *both ways* rule); it exits non-zero naming the
recovery path.

Recovery is **transition-based, not schema-reverse-based.** The forward migration
commits a revertible transition capturing the pre-migration bytes of every
mutated store (base / spans / scalar-base / reconcile legs) **before** any legacy
store is unlinked. While that transition is retained,
`setforge revert --profile=migrate` byte-restores the original legacy stores in
lockstep — the recovery a stateless reverse migration cannot provide, and the
**only** downgrade across this major boundary.

## Span-surface retirement — a one-way contract step

The `3.0 → 4.0` migration retires the **host-local span-declaration surface** —
the `local.yaml` `host_local_sections` blocks and the per-tracked-file
overlay-`spans` fields — folding whatever host-local intent survives into the
unified per-unit `LOCAL` reconcile store and stripping the retired keys from
`local.yaml`. It is the **contract** step of the store-backed host-local model:
each residual host-local section becomes an ordinary `LOCAL` unit keyed by a
`reloc_anchor` minted from the section body's markdown heading, and the legacy
`host_local_sections` / overlay-`spans` config fields are removed.

This is a **MAJOR** bump (`3 → 4`), so it is the concrete instance of the
*forward compatibility* limit above: an older (`3.x`) engine reading a `4.0`
config **refuses cleanly** (non-zero, no traceback) via the cross-major
`schema_version` guard, rather than silently misreading a config whose retired
fields it no longer understands.

The forward migration is **data-preserving**: it merges each residual section
into its unit's `LOCAL` store, preserving the recorded `local` bytes and the
existing hunk classifications. But the collapse is **lossy in the reverse
direction** — the folded sections dissolve into per-unit `SHARED`/`LOCAL` hunks
with no way to regenerate the original `host_local_sections`/`spans` declaration —
so, per the *Stated limit*, the `4.0 → 3.0` schema reverse **refuses cleanly**
rather than emitting a config it cannot faithfully reconstruct. The bump still
registers that reverse (satisfying the *both ways* rule); it exits non-zero
naming the recovery path.

Recovery is **transition-based, not schema-reverse-based.** The forward
migration captures pre-state snapshots of every mutated reconcile leg plus the
`local.yaml` text patch and commits ONE durable transition **before** any strip.
While that transition is retained, `setforge revert --profile=migrate`
byte-restores the pre-cutover state in lockstep — the recovery a stateless
reverse migration cannot provide, and the **only** downgrade across this major
boundary.

### Headingless host-local sections — a hard pre-flight refuse

The `4.0` store identity is heading-based: a unit's `reloc_anchor` is minted
from the section body's own leading markdown heading. A host-local section body
with **no markdown heading** therefore cannot mint a stable anchor, and the
migration has no neighbour heading to borrow from (the fold lands the body into
an empty base). Rather than silently mint a wrong anchor, the migration
**pre-flight-aborts before any mutation** if ANY host-local section body lacks a
heading, naming each offending `(tracked_file, section)`. The operator must give
each such body a leading markdown heading (e.g. `## My Notes`) or remove it, then
re-run the migration. This is a hard refuse, not a warning — a headingless
section is never folded.

### Migrate-before-operate ordering

A `4.0` engine on a pre-`4.0` config **requires migration first**. The capture
(`sync`) path no longer reads `local.yaml` `host_local_sections` — it relies on
the per-unit `LOCAL` store being populated, which only the `3.0 → 4.0` migration
does. Operating a `4.0` engine against an un-migrated config would therefore drop
host-local content silently; run `setforge migrate` to fold the surface into the
store before any `install` / `sync` / `compare` on a `4.0` engine.

## Span-types retirement — the reversible major bump

The `4.0 → 5.0` migration retires the dead `span_types` type definitions — the
now-unreferenced data model that survived the `3.0 → 4.0` fold only as schema
cruft. Unlike the three prior cutovers (`2.0 → 2.1`, `2.1 → 3.0`, `3.0 → 4.0`),
each a **lossy one-way** contract step whose down-migration **refuses**, this
major bump is the **EXCEPTION**: a fully-reversible **symmetric restamp**.

Nothing is lost that a schema restamp cannot restore. The host-local span
surface was already stripped at `3.0 → 4.0`; by `4.0` no deployed config carries
span data, so `4.0 → 5.0` only deletes dead type definitions and advances the
stamp. The forward `apply` defensively confirms no stray `spans` block lingers
in `setforge.yaml`, then overwrites `schema_version` in place.

So the `5.0 → 4.0` down-migration is a **real restamp, not a refuse**: both
endpoints carry `schema_version`, so the reverse down-stamps the value to `4.0`
in place (it never strips the key, which would let schema detection misread the
result as an older baseline). An `up → down → up` cycle is byte-identical against
the post-`ruamel`-normalization document. This honors the *both ways* rule
without the *Stated limit* escape the lossy majors invoke.

It remains a **MAJOR** bump (`4 → 5`): an older (`4.x`) engine reading a `5.0`
config still **refuses cleanly** via the cross-major `schema_version` guard.

As the **chain-terminal** step, a `migrate` reaching `5.0` from an older schema
in one command records this cutover's transition threading the whole chain's
pre-migration origin image, so a single `setforge revert --profile=migrate`
byte-restores the entire chain's origin — config **and** every reconcile-store
leg an earlier cutover seeded — not merely the intermediate `4.0` state.

## Profile-fields contraction — the translating major bump

The `5.0 → 6.0` migration retires four legacy per-profile fields —
`cargo_binaries`, `claude_plugins`, `plugins_reconcile`, and `extensions` — by
folding each into the general `packages` registry plus the `reconcile` block:
every `cargo_binaries` / `claude_plugins` entry and every `extensions.include`
id becomes a top-level `packages` entry (kind `cargo` / `plugin` / `extension`)
referenced from the profile, `plugins_reconcile` and
`extensions.{exclude,reconcile}` move under `reconcile.{plugins,extensions}`,
and the four legacy keys are dropped. The `packages` / `reconcile` surface
already existed at `5.0`, so this is an **expand → contract** step: the new home
shipped additively first, and `6.0` removes the old one only after.

Unlike the lossy one-way contract steps (`2.0 → 2.1`, `2.1 → 3.0`,
`3.0 → 4.0`), whose down-migrations **refuse**, this reverse is a **real
translating down-migration**, not a refuse: a single `migrate --to=5.0`
unfolds the minted `packages` entries back into `cargo_binaries` /
`claude_plugins` / `extensions.include` and rebuilds the legacy
`plugins_reconcile` / `extensions` shape. It honors the *both ways* rule; the
round-trip restores schema **shape and values**, not byte identity (`ruamel`
normalizes on the first load).

The reverse has no round-trip context, so it recognizes a minted entry purely
by shape: a `packages` ref is unfolded **only** when its top-level entry is the
exact identity body the forward emits — `{type: cargo, crate: <key>}` /
`{type: plugin, plugin: <key>}` / `{type: extension, extension: <key>}` with the
inner value equal to the key. Any other native ref (a `python` / `go` /
`github_release` / `local` package, or a `cargo` / `plugin` / `extension` whose
body carries extra fields or a non-identity value) is **preserved in place** with
its top-level entry intact. A natively-authored `6.0` package that happens to
match the identity shape is therefore translated to the equivalent legacy field
rather than kept as a `packages` ref — a values-preserving rewrite into the `5.0`
spelling of the same intent (the two forms are semantically identical), not a
loss.

The forward drop is **irreversible on un-upgraded hosts**, so it is gated
behind an operator-declared `minimum_version >= 6.0` floor — the attestation
that every host reading this config already speaks the contracted shape. Below
the floor, or with no floor declared, the migration **refuses cleanly** and
mutates nothing.

It remains a **MAJOR** bump (`5 → 6`) in **lockstep**: an older (`5.x`) engine
reading a `6.0` config still **refuses cleanly** via the cross-major
`schema_version` guard, and this cutover is the new **chain-terminal** step —
threading the whole chain's origin image so one `setforge revert
--profile=migrate` byte-restores the pre-migration origin.

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

## Span-disposition diagnostics retirement — a one-way contract step

While the `Disposition` / `spans` surface was live (schema `2.x`), a
`pinned`/`forked` span was consumed only on the disposition merge path. On a
tracked_file with **no `disposition`**, the verbatim deploy processed only
`overlay` spans, so a `pinned`/`forked` span was silently ignored on deploy and
not excluded on capture — host-local content could leak into tracked. To turn
that silent no-op into a fast failure, `setforge validate` **exited 1** and
`setforge install` **refused at pre-flight** when a `pinned`/`forked` span was
declared on a tracked_file whose (local.yaml-folded) `disposition` was `None`,
naming the tracked_file, the span anchor, and the fix (set a `disposition`, or
switch to `kind: overlay`).

That whole `Disposition` / `spans` surface has since been **retired** — folded
into the per-unit `SHARED` / `LOCAL` reconcile store across the retirement chain
documented above and stripped from configs at schema **5.0**. The diagnostic no
longer fires on any live path: `setforge validate` now **strips** the retired
`spans` / `disposition` keys in memory before its strict models validate, so a
pre-retirement `local.yaml` still carrying them **validates clean** rather than
tripping the old refusal; the pre-flight refusal survives only inside the retire
migrations, where it is unreachable by ordinary `validate` / `install` runs.

No supported config reaches this diagnostic today. The entry is kept as the
historical record of the retired behavior, per this doc's *no removal without a
deprecation window* contract — the same reason the retirement entries above are
preserved rather than deleted.
