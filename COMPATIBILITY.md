# Compatibility policy

setforge config is schema-versioned. As of v0.3.0 every `setforge.yaml`
carries a `schema_version`, and the engine migrates older configs forward on
read. This document is the standing contract for how that schema evolves, what
the release process must guarantee, and what users can rely on across engine
versions.

## Principles

- **Additive-first.** New schema fields are added, never repurposed. An
  existing field's name, type, and meaning are fixed once shipped; a new
  capability gets a new field rather than overloading an old one.
- **Breaking changes go expand → contract.** A field is never removed in a
  single step. During the *expand* window the old field is retained and stays
  readable alongside its replacement; the *contract* step removes the old field
  only after that window closes. There is no hard removal.
- **Every `schema_version` bump ships migrations both ways.** A version bump is
  not done until it registers a forward (up) Migration *and* its reverse (down)
  migration. The reverse is what makes a cross-major downgrade a single
  command rather than a manual rewrite.
- **Forward-tolerant reading.** An older engine reading config written by a
  newer engine ignores fields it does not recognize instead of crashing. Newer
  config stays loadable on an older engine, minus the features that older
  engine never had.
- **No removal without a deprecation window.** A field marked for removal is
  announced as deprecated, kept functional through the expand window, and only
  dropped at the contract step in a later release. Users always get a release
  in which both the old and new shapes work.

## Guarantee scope

The principles above resolve to four concrete guarantees. Each is stated with
its exact bound — what holds always, and what holds only within a window.

### Backward compatibility — full and permanent

A newer engine fully understands config written for any older
`schema_version`. Old configs keep working, with full functionality, with no
edits required from the user. This guarantee does not expire.

### Forward compatibility — forward-safe permanent, full within the window

An older engine never crashes on newer config: it reads what it understands
and ignores unknown fields. That *forward-safe* behavior is permanent. *Full*
forward functionality — the older engine acting on everything the config
expresses — holds only within the expand-contract window, while the fields it
knows are still present. Once a field has passed through contract, an older
engine simply will not see it.

### Upgrade — always zero-touch

Moving to a newer engine never requires the user to edit config by hand. The
engine applies the registered forward migrations on read. Upgrade is zero-touch
across any version distance.

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
