# RFC 0002: Safe adoption authority and provenance

**Status:** Accepted

SetForge must distinguish discovering a resource from owning it. Matching bytes,
a shared declaration, an ignore rule, provenance evidence, or a legacy receipt
does not grant permission to update or remove something that already exists.

This RFC defines the contract used by later file, region, generated-resource,
application, tree, release-asset, and package work.

## Independent dimensions

| Dimension | Values | Meaning |
| --- | --- | --- |
| Current authority | `none`, `manage` | Whether SetForge may currently manage the concrete resource. |
| Claim lifecycle | no claim, `claimed`, `released` | Origin-neutral durable claim state. Discovery is ephemeral; a released claim is a tombstone with no current grant. |
| Declaration residence | `shared`, `host-local` | Where desired intent is stored. |
| Resolution binding | `portable`, `host-bound`, `instance-bound` | How inputs and concrete targets are resolved. |
| Provenance evidence | structured evidence record | Composable origin, acquisition, generator, resolver, artifact, platform, and history evidence; it never grants authority. |

The dimensions compose rather than compete. A shared declaration may resolve to
an instance-bound project target. Generated output may have shared intent and
host-bound inputs. An adopted external package may be managed on one host and
merely observed on another.

`claimed` covers every active ownership claim, whether SetForge installed the
resource or adopted it metadata-only. Installation versus external adoption is
recorded in acquisition provenance and history, not overloaded into lifecycle.
Consequently reversing an adoption removes its metadata claim, while reversing
an installation follows that installation operation's separately journaled
resource effects.

Skip is a one-run decision. Ignore is a durable host-local refusal of management
or cleanup. Neither creates a claim. Resolving an ignore in order to manage a
resource is an explicit transition in the normal flow.

## Adoption experience

Adoption uses normal SetForge workflows. It requires neither an entry in
`local.yaml` nor a separate command:

- A declared but pre-existing whole resource is reported as present and unowned.
  The normal install flow may offer an inline `manage this existing resource`
  confirmation. Acceptance records ownership without reinstalling or replacing
  the resource.
- For a mixed text or structured file, the existing stage flow is where an
  ownership claim is offered and unit authority is reviewed. Stable line and
  key units are classified as SHARED, LOCAL,
  SHARED_DRAFTED, or PENDING. Confirmed SHARED and SHARED_DRAFTED units express
  portable publication eligibility only under a current container claim;
  classification alone never creates or transfers that claim.
- Editing live bytes expresses current local state. It does not grant deletion
  authority. Changed SHARED units still require reconfirmation; LOCAL edits stay
  local.

`local.yaml` remains an optional surface for explicit host overrides and ignore
rules. It is not an ownership ledger or adoption requirement.

Ownership adoption must not be confused with the existing stage choice named
`Adopt locally`: ownership adoption is metadata-only, while `Adopt locally`
rewrites live content from a selected draft and retains its existing mutation
confirmation. Stage requires or obtains the container ownership claim before
publishing unit decisions. If a single interaction combines the claim and a
content adoption, it journals and confirms the two effects separately and
publishes neither from a partially validated plan.

## Durable identity and ownership records

The internal versioned ledger lives under SetForge's existing host state root,
with one atomically replaceable file per resource identity. Released claims remain
as tombstones and history records. Discovery observations remain in the frozen
in-memory plan unless a confirmed management decision records provenance.

A resource identity contains:

- resource kind and provider;
- a canonical coordinate; and
- target scope, such as the user host, a target project root, or an application
  instance.

Display names and profile membership are metadata. Package claims are user-global
and profiles are referrers. Project-file identity includes a verified target-root
identity and relative path. Mixed-file unit claims additionally use the existing
stable typed unit reference.

The owner is a host-local config-checkout UUID stored in the configuration
repository's Git common directory. It survives moving that checkout and is not
copied by a normal clone, so separate clones are separate owners. UUID creation
is atomic create-or-read under a lock keyed by the verified Git common-directory
identity; every claim CAS rereads it. Simultaneous first use from multiple
worktrees converges on one UUID, while malformed or conflicting state fails
closed. Human-readable paths, remotes, and profiles remain provenance only and
are never CAS keys. A non-Git configuration repository cannot mint ownership
until a separate stable identity contract exists.

Each claim records resource identity, owner UUID, declaration reference, current
authority, lifecycle, a structured provenance evidence record, concrete locator
and fingerprint, schema version, generation, and transfer history. Provenance is
not one exclusive enum: an adopted external generated artifact can retain its
external origin, adoption event, generator inputs, resolver facts, selected
artifact/checksum/platform, and later transfer events without precedence loss.

Legacy receipts are dual-read as `legacy-unverified`. They preserve existing
no-op behavior but authorize no upgrade, uninstall, or cleanup until inline
confirmation upgrades them to a current claim. Ambiguous bare-key migration
fails closed.

Existing tracked-file reconcile stores are also dual-read. Base, local, index,
draft, and transition evidence is preserved as `legacy-unverified` container and
unit history: it neither loses established LOCAL/SHARED_DRAFTED intent nor
silently grants a new container claim. The next normal stage or install flow
shows that preserved classification and requires inline container confirmation
before further managed publication. Matching declarations or bytes alone do not
bootstrap authority. Ambiguous, corrupt, or cross-owner state remains readable
for preservation but blocks mutation and destructive cleanup until explicitly
resolved. Claim publication and any canonical unit-key migration are one
journaled, index-last transaction.

## Adoption, transfer, release, and deletion

Adoption is a metadata-only compare-and-swap operation:

1. Discover and freeze the concrete resource, declaration, owner, and ledger
   generation.
2. Show the relevant inline confirmation outside mutation locks.
3. Reacquire locks and reproduce the exact observation.
4. Refuse drift, ambiguity, or a competing claim.
5. Atomically publish the claim without installing, replacing, or deleting the
   resource.

A same-owner exact replay is idempotent. Transfer names the expected old owner
UUID, new owner UUID, and generation; it preserves provenance history and refuses
stale or competing state. No caller silently overwrites a claim.

Release sets current authority to `none`, records a released tombstone, and
leaves the resource intact. Upgrade, replacement, deletion, and uninstall are
separate operations. They require a current `manage` grant, a fresh target
identity and fingerprint, and their own confirmation or declared policy. Drift
causes refusal and review, never destructive convergence.

## Reverse operations

Reverse operations are asymmetric because some reversals grant authority:

- Reverting adoption may remove authority after verifying the exact
  post-adoption claim.
- Reverting release or transfer would grant authority. It repeats the full locked
  CAS against the expected post-owner and generation, current declaration,
  resource identity and fingerprint, and competing claims.
- Drift or collision preserves the current claim and retains the revert/recovery
  record for retry or manual resolution. Restoring a ledger snapshot alone never
  grants authority.

## Mixed-file journey

Consider a tracked `CLAUDE.md` containing team instructions, machine-local paths,
and a sanitized shareable form:

1. Stage compares canonical source, base, and live bytes and presents stable line
   or structured units.
2. The user marks team instructions SHARED, machine paths LOCAL, and the sanitized
   form SHARED_DRAFTED. PENDING units remain inert.
3. A container claim permits reconciliation of the file, while unit records
   define which bytes may propagate. The container claim never turns LOCAL bytes
   into portable managed content.
4. Sync promotes only confirmed SHARED or SHARED_DRAFTED units and preserves
   LOCAL and PENDING units.
5. Whole-file replacement or deletion refuses while LOCAL or PENDING content
   remains unless a later separately reviewed action resolves or preserves it.

## Package journey

Cargo reports `ripgrep` installed before SetForge has a claim. Normal install
reports `present, external, unowned` and offers inline management confirmation.
Acceptance records ecosystem, version, source, target, and fingerprint evidence
without invoking Cargo. A later upgrade requires the matching claim and current
observation. Release leaves the package installed. Cleanup cannot uninstall the
released package; removal requires management again and a separate uninstall
decision.

## Collision and transfer journey

Config A owns a resource that Config B also declares. Config B sees the existing
owner and cannot silently manage it, even when bytes match. Inline transfer review
names both checkout UUIDs. Under shared locks SetForge verifies the old generation,
current declaration, and resource fingerprint, then publishes B atomically. A
stale or concurrent transfer changes nothing.

## Locking and recovery

Adoption follows the existing lock order and adds target-root locks after sorted
resource and config locks and before sorted profile locks. Multiple target locks
are canonicalized, deduplicated, and sorted.

Every target uses a stable ancestor-coordinate lock: a descriptor-verified
existing ancestor identity plus the normalized relative target path. That name
does not change when a missing root is created. When the target exists, an
additional filesystem-object lock keyed authoritatively by verified device/inode
identity unifies symlink and bind aliases; realpath is locator and guard evidence,
not equality. Operations acquire both applicable keys in sorted order and retain
the stable coordinate lock across absent-to-present publication. Publication
rechecks ancestor and object identities so aliases, creation, and replacement
cannot bypass serialization.

Ledger and coupled state changes are journaled before mutation and checkpointed
before each effect. Recovery restores metadata in safe publication order. An
adapter that cannot compensate retains manual recovery state rather than claiming
rollback.

## Roadmap boundaries

- Project profiles own target-root injection, Git visibility, project
  reconcile/inheritance, and restoration of project-local Git state. Hidden or
  tracked is publication state, not ownership. They consume generic container
  and unit claims.
- `local.yaml` owns optional host overrides and ignore policy, not adoption.
- tmux configuration is a tracked or project-profile file concern.
- An existing TPM checkout may only be observed or metadata-adopted when clean
  and uniquely identified. Preserve leaves it unowned. Replacement, update, and
  removal are blocked until directory inventory, journal/compensation,
  collision-safe reverse, and manual-remediation designs exist.
- Directory trees cannot claim reversibility through current leaf-only deltas.
- Operating-system and architecture selection belong to resolution and artifact
  provenance, not ownership.

## Implementation and verification matrix

| Lane | Depends on | Focused evidence | Integration boundary | Docker boundary |
| --- | --- | --- | --- | --- |
| Existing files and owned regions | Identity and ledger | Container versus unit authority, stable refs, mixed classifications, bytes/modes/symlinks, overlap, removal refusal | Real filesystem and stage/install flows, locked drift, restart, revert, unrelated-byte preservation | Extend an existing interactive conflict or stage journey for the real TTY handoff |
| Generated resources and host-resolved inputs | Target vocabulary and provenance | Composable residence/binding, deterministic spec, typed inputs, host-value normalization, missing/changed input refusal | Config to frozen resolution to apply/replan while shared data stays host-free | Extend template installation only when a real environment/platform boundary exists |
| Multi-target application capability graphs | Identity and typed targets | Node/edge discrimination, cycle/dangling/type validation, stable ordering | Immutable cross-target plan, partial failure, compensation, group recovery | Extend one existing cross-ecosystem bundle/application journey |
| Durable identity and collision management | This RFC | Canonicalization/injectivity, checkout UUID move/clone/corruption, collision, schema, receipt and reconcile-store migration, CAS, released tombstones, reverse grants | Two fresh processes, simultaneous worktree UUID creation, missing-root creation and alias locking, atomic create/transfer/release, stale generation, crash recovery | Not required unless identity depends on an installed-tool path |
| Directory trees | File adoption and identity | Containment, modes, symlink/mount policy, excludes, nested boundaries, deterministic inventory, no destructive TPM path | Nested adopt/update/remove/restart with unrelated entries preserved and declared recovery limits | Extend the existing directory-copy journey for true POSIX behavior |
| Platform release assets | Target vocabulary and provenance | OS/architecture aliases, precedence, unsupported/ambiguous refusal, checksum binding | Fixture-backed metadata/download and frozen platform revalidation | Extend an existing toolchain or locked-install node; live API stays a network canary |
| Package provenance lifecycle | Identity and typed targets | Present-unowned, inline adopt, upgrade/remove authority, source collision, drift refusal, receipt migration | Real CLI plan/apply/claim/second run, cleanup, restart across provisioners | Extend representative existing ecosystem and locked-install journeys |

Every lane adds meaningful focused unit or property coverage, a real store/CLI or
filesystem integration boundary, changed-line and branch evidence, the relevant
broad non-Docker suite, Ruff formatting and lint, and mypy. High-example or
stateful properties may use the slow lane but retain a bounded fast regression.
Docker nodes are extended only for installed-tool, TTY, Git, platform, or POSIX
behavior; the closed node budget is not expanded without a distinct observable
boundary.

## Sequencing

1. Land this authority and provenance contract.
2. Implement resource identity, config-checkout identity, and the ownership
   ledger.
3. Freeze the typed target and capability vocabulary.
4. Build package provenance and mixed file/region adoption on the frozen
   foundation.
5. Add generated and host-resolved resources.
6. Add directory trees and platform asset selection. They may proceed in
   parallel only after shared configuration interfaces and path ownership are
   frozen.
7. Run integrated compatibility, recovery, and lifecycle verification.

The foundation is serial because every implementation lane depends on it. Later
parallel implementation requires concrete disjoint path ownership and frozen
shared interfaces.
