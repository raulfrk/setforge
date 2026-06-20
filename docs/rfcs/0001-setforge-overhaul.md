# RFC 0001 — SetForge Overhaul

**Status:** DRAFT — all design settled: config-reconciliation, package-provisioning, migration, UX (wizard + theme), testing strategy, project Claude tooling, and the self-improvement loop. **This document is ordered in build sequence** (framing → Wave-0 foundations → feature pillars → migration → reference). Ready for decomposition into implementation beads. A **v1.1 roadmap** (project profiles) is in §14.
**Output target:** this RFC → an implementation plan (epics + subtasks).
**Method:** brain-dump → per-component elicitation → mockups iterated in revdiff + web-mockup (see ledger for state).

---

## 1. Certainty ledger

Legend: `SETTLED` = sure · `LEANING` = tentative · `OPEN` = needs figuring out · `DROPPED` = ruled out.

| Item | State |
|---|---|
| Full ground-up redesign (not bug point-fixes); F1/F2 superseded by-construction | SETTLED |
| One RFC overhauls all of SetForge (config-reconcile **+** package provisioning) | SETTLED |
| North-star mental model: **works like git merge conflicts** (detect → surface → you resolve) | SETTLED |
| Rip out current deploy model, rebuild, re-init, **with a migration path** (existing configs unaffected) | SETTLED |
| Flow: pull base → edit locally → edits recorded → choose **share** vs **keep-local** | SETTLED |
| A command **marks a change as mergeable** | SETTLED |
| Same model for structured configs (JSON/YAML), key-aware | SETTLED |
| Wizard fires **only on true conflict** (both sides touched same region); clean upstream change just installs | SETTLED |
| Merge engine = **plain line-level 3-way (git-style)** + `claude-merge` escape hatch; setforge never rewrites a non-conflict line | SETTLED |
| `claude-merge` = one resumable `claude -p` session **per conflict**; `re-prompt` resumes it (keeps prior attempt); default prompt + your instruction | SETTLED |
| Share-vs-keep UX = **hunk-level staging** (`status` / `share` / `keep`, git-`add -p` style); sync-time wizard fires only on unclassified edits | SETTLED |
| Store = **extend the existing intent/state split**: `local.yaml` = intent; `~/.local/state/setforge/` = base + recorded edits + classification | SETTLED (impl details deferred) |
| Base = **explicit, inspectable** file under state dir; first-install-on-divergence asks "seed from live vs upstream" (kills old F1/F2 silent overwrite) | SETTLED |
| Packages: provisioner per ecosystem (`cargo`/`python`/`go`/`github_release`/`plugin`/`extension`), name + version, install-if-absent, pin | SETTLED |
| Two declaration forms: **bare items** (single tools) **and bundles** (grouped, ordered) | SETTLED |
| Bundle = components + ordering (`depends_on` ⇒ pipeline, else parallel); component **references or inlines** a def | SETTLED |
| `github_release` provisioner: repo + tag + asset + checksum + extract + binary + install + rename + chmod (Linux-only first) | SETTLED |
| User-level overrides = a `file` component deployed into a plugin's data dir, via pillar-1 host-local/shared staging | SETTLED |
| Scope: user-level default; **system (apt) gated** by `allow_system:` flag + root/sudo capability, else soft-skip | SETTLED |
| Removal **only** on explicit cleanup; wizard asks *delete* vs *mark-orphan*; binaries never auto-pruned | SETTLED |
| Plugin keep-existing: `ADDITIVE` (default) already leaves unmanaged plugins; no `enabledPlugins` fight (uses `claude plugin` CLI) | SETTLED |
| Versions: pin-in-config baseline; **lockfile** is wanted but its own later epic | SETTLED |
| Migration: **one-shot, reversible**, "ask me" default, auto-fix+**confirm** on breakage risk, interrupt-resumes | SETTLED |
| UX: all wizards use a **navigable button bar** (no letter menus; letters survive as hidden accelerators), via prompt_toolkit | SETTLED |
| Color theme: **Tokyo Night** across all CLI + wizards; truecolor + 256 fallback (Termius); dark-only; shipped (no override yet) | SETTLED |
| Testing: **trophy shape** (80/15/5), Hypothesis **invariant catalog INV-1..10**, 85% branch (unit), mutation >80% on core, audit-and-prune each test | SETTLED |
| Project `.claude/` tooling: **enforce** (3-tier; deterministic+human block, LLM advisory) **+ develop** (scaffolds); `docs/RULES.md` rule index | SETTLED |
| Operational-tail skills: **docs-sync** (RULES/CLAUDE/RFC/CHANGELOG drift) + **security/dependency-triage** (wrap gitleaks + dep-audit); flaky-triage optional | SETTLED |
| Every tool carries a **self-improvement loop**: grounded proposal → backlog → revdiff → approve/decline; never auto-apply; gates ratchet up only | SETTLED |
| Session-flow integration: tooling overlays the 7 phases, **tightly integrated** (no standalone); bridge = **CLAUDE.md manifest** (P5); gates **always-on pre-commit+CI**; self-improve at **P6+session-end** | SETTLED |
| **Build order spine-first**: a tooling/test spine (gates, review agents, rule index, self-improvement, Hypothesis harness) is built FIRST and hard-blocks even the theme/widget and the rest of testing; then the under-spine layer (D + coverage/mutation policy), then the feature epics; scaffolds codified with their first instance | SETTLED |
| claude-merge UX: instruction optional (Enter=default, prompt shown first); per-conflict session **discarded** on accept/cancel | SETTLED |
| Staging: marking `share` **auto-drafts** shared wording (+ follow-up prompts); **local ⇄ shared via re-stage** (bidirectional, no extra verb) | SETTLED |
| Store encoding: file-id = `tracked_files` key; index hunks matched by **content-hash + 3-way position** (label cosmetic); class local/shared/pending | SETTLED |
| Lockfile (B10): one `setforge.lock`, per-ecosystem exact id+checksum (plugin = best-effort, flagged); `lock` / `--locked` / `lock --update` | SETTLED |
| **v1.1 roadmap**: project profiles — inject a `.claude/` tree into a path, per-file git-visibility toggle (hidden default), reconcile, `extends` (§14) | SETTLED (v1.1) |
| Sub-word merge / sub-word staging; word-level/unwrap merge engine; auto re-wrap | DROPPED (YAGNI; line-level + claude-merge cover it) |

---

## 2. Motivation (short)

- Today's config management is **unintuitive and does the wrong thing**.
- Four overlapping mechanisms (disposition / sections / spans / overlays) reuse
  the same four words at different levels → nobody can tell which to reach for.
- Two verified silent-data-loss bugs (F1/F2) live in the current merge path
  (`disposition_merge.py:283` first-run overwrite; `compare.py:181` false
  "expected").
- The fix is not patches — it's a **new model** with a git-like resolution UX,
  plus a second pillar that turns SetForge into a full-circle provisioner.

## 3. The two pillars

1. **Config reconciliation** — fetch a shared config, edit locally, reconcile
   upstream drift like a git merge. **(SETTLED — §9.)**
2. **Package provisioning** — declare tools (cargo / python / go / GitHub-release
   binaries), Claude plugins, and VSCode extensions that should exist on a host, as
   bare items or ordered **bundles**; SetForge makes them present, can bundle their
   config (incl. user-level overrides), and removes them only via an explicit cleanup
   wizard. **(SETTLED — §10.)**

---

## 4. Build order (read the rest of this doc in this sequence)

This is the **whole-project build plan**, not just the tooling. **Spine-first:** a tooling/test **spine**
— the deterministic gates, review agents, rule index, self-improvement loop, and the Hypothesis harness —
is built FIRST and **hard-blocks everything below it**, including the theme/widget (D) and the rest of
testing. Only once the spine is in does the rest proceed: every feature epic is then built *with* it —
scaffolds generate the boilerplate, invariants guard from day 1, gates enforce from the first commit. One
nuance: a **scaffold that codifies a pattern is built alongside that pattern's first instance** (e.g.
`scaffold-provisioner` lands with B1, then generates B2–B5).

```
 WAVE 0a  TOOLING SPINE (built FIRST — hard-blocks everything below;        → §5–§7
          gate = F2a + F5 + F7 + E1)
   F1  rule index + testing rubric
   F2a deterministic policy/AST lint gates (+ pre-commit/CI wiring)
   F3 + F4 review agents  →  F5 enforce-tests
   F7  self-improvement loop (captures proposals emitted by F2a / F3 / F4)
   E1  Hypothesis harness + state machine + fixtures
        │   nothing below may start until the spine is in
 WAVE 0b  UNDER THE SPINE (parallel)                                        → §6, §8
   D1 theme · D2 widget · D3 apply
   E3 coverage policy · E4 mutation  →  F2b (wire coverage/mutation into CI)
        │
 WAVE 1  PATTERN-DEFINING FIRST INSTANCES (+ codify the scaffold)           → §9, §10
   A0 fetch · A1 store · A2 merge · A3 conflict wizard · A7 inspect viewer
   B1 provisioner protocol  ⊕  scaffold-provisioner (F6)
        │
 WAVE 2  REPLICATE VIA SCAFFOLDS                                            → §9, §10
   A4 claude-merge · A5 staging · A6 seed-prompt
   B2 cargo · B3 python · B4 go · B5 github_release · B6 bundle · B7 file/override · B8 plugin/ext · B9 cleanup
   E2 invariant stateful machine (assembles the per-component @invariants; needs A+B cores)
        │
 WAVE 3  CROSS-CUTTING (needs A+B)                                          → §11
   C1–C4 + C-guard migration
   B10 lockfile · C5 contract  (later releases)
```

The granular epic tree + dependencies + gap-closers live in **§13 (Decomposition)**.

---

## 5. Wave 0 — Project Claude tooling

A project-scoped `.claude/` toolset (version-controlled in the engine repo), in **three parts**:

- **A. Enforcement** — a 3-tier funnel. **Tier 1 deterministic** (coverage/mutation/AST-lint + policy
  lints: wizard-letter-ban, theme-hardcode-ban, `shell=True`-ban, legacy-API-ban — via pre-commit + CI +
  an optional `PreToolUse` commit hook) **BLOCKS**. **Tier 2 advisory LLM review fan** — a skill dispatches
  `test-quality-reviewer` + `design-invariant-reviewer` alongside the existing python-* + bd-leak agents,
  grounded in `docs/RULES.md`, two-pass, structured verdicts — **ADVISORY only**. **Tier 3 human** (revdiff
  + merge) **BLOCKS**. Rule: deterministic + human block, LLM advises (hard-blocking AI review breeds
  false-positive fatigue).
- **B. Development (generative)** — assistive, human-reviewed scaffolds: `scaffold-provisioner`,
  `scaffold-wizard`, `author-invariants`, `author-migration`, `author-config-entry` — each encodes the
  design so new parts are conformant by construction (and ship their invariant tests), shrinking what
  enforcement must catch.
- **C. Maintenance (operational tail)** — `docs-sync` (catch RULES.md ↔ CLAUDE.md ↔ RFC ↔ CHANGELOG
  drift) and `security/dependency-triage` (wrap the existing gitleaks + a dep-audit; triage findings,
  flag CVEs / risky major bumps). Both stay **advisory** (end in a deterministic check or human review).
  `flaky-test-triage` is an optional later add. (Research verdict: the QC core is covered; these are the
  cheap, high-ROI gaps small projects get bitten by silently.)

**Rule index** `docs/RULES.md` (distilled from this RFC + CLAUDE.md) is the single source of truth
grounding the agents, the humans, and the generators. It additionally carries the project's **testing
rubric** (the tier-decision guide + the invariant-ownership map + the coverage/mutation bars).

**Tier-1 gates land as two build steps.** The deterministic lints — **F2a** (wizard-letter-ban,
theme-hardcode-ban, `shell=True`-ban, legacy-API-ban + pre-commit/CI) — have **no dependency on the test
harness**, so they are part of the spine and ship first. The **coverage + mutation** gates wire in
**later (F2b)**, once the E3/E4 policy exists.

### 5.1 Session-flow integration

The tooling **overlays the global `session-flow` 7-phase workflow** — it is **tightly integrated, not
standalone** (no ad-hoc mode):

- **P1 brainstorm** — `RULES.md` grounds the design.
- **P2 spec** — `design-invariant-reviewer` pre-checks the spec against RULES.md / invariants.
- **P4 implement** — scaffolds draft new parts; **Tier-1 gates run always-on (pre-commit + CI)** on every
  commit.
- **P5 review fan** — **the bridge is a CLAUDE.md manifest (Option A):** the project CLAUDE.md declares the
  extra reviewers (`test-quality-reviewer`, `design-invariant-reviewer`) that session-flow dispatches
  alongside the global python-* + bd-leak fan.
- **P6 merge** — Tier-1 gates block.
- **P7 post-merge** — the fan re-runs on merged HEAD; nightly full mutation.
- **Self-improvement** proposals surface at the **P6 and session-end** checkpoints → revdiff →
  approve/decline.

---

## 6. Wave 0 — Testing strategy

Today's suite is an **ice-cream cone** (300+ Docker e2e, ~42 min) over a thin unit base; F1/F2 slipped
because **coverage ≠ assertion** (lines executed, results unasserted). The rebuild targets test
*effectiveness*, not quantity.

**Shape — testing trophy** (setforge is integration-heavy; mocking both fs + subprocess tests the mock):
static (ruff+mypy) → **unit** (pure logic: merge/diff/reconcile/parse — no disk, no mocks) →
**integration** (the bulk: each subcommand on a real `tmp_path`, subprocess mocked via pytest-subprocess)
→ **thin e2e** (one golden path per verb, real binaries, <5 min smoke). Target mix ≈ **80/15/5**; push
every test as far down as it goes.

**Invariant catalog** — a Hypothesis stateful machine over install/sync/revert/migrate; `@invariant`
checked after every step (the F1/F2 killers):

| ID | Invariant | Catches |
|---|---|---|
| INV-1 | no silent data loss — every user byte ∈ {live ∪ store ∪ shown-in-wizard} | F1 |
| INV-2 | base round-trips: `base + recorded-local == live` | F2 |
| INV-3 | revert is inverse (byte-exact) | revert corruption |
| INV-4 | reconcile idempotent (`install∘install == install`) | first-vs-Nth divergence |
| INV-5 | migrate reversible: `revert∘migrate == identity` | migration loss |
| INV-6 | merge non-destructive: non-conflict region byte-identical | merge mangling |
| INV-7 | provision idempotent: declared==installed ⇒ no-op | re-install churn |
| INV-8 | stage fidelity: install deploys exactly the `share`d hunks | staging leak |
| INV-9 | bundle DAG acyclic + `depends_on` order honored | bundle ordering |
| INV-10 | store index ↔ on-disk consistent (no orphan classification) | store drift |

The catalog is **authored per component** — each component ships its own `@invariant` alongside its code.
**E2 is the stateful machine that assembles and runs them together**, so E2 depends on the pillar cores
and lands **after A+B**, not early.

**Coverage + mutation:** 85% **branch** coverage gate on the **unit** suite only (e2e stays `--no-cov`
per the known pytest-cov+xdist crash); exclude non-logic; higher bars on the core. **Mutation score >80%**
on the merge/reconcile/store core (`mutmut`, diff-mode on PR, full nightly) is the real anti-change-detector
measure. **Audit-and-prune:** review **each** existing test → keep/change/delete by signal (change-detector
/ over-mocked / tautological / assertion-free / redundant / blindly-blessed snapshot); safe-delete only
after confirming another test or a surviving mutant still covers the behavior; output a verdict manifest.
**CI staging:** unit per-commit (<1 min) · unit+integration+coverage-gate+e2e-smoke+mutmut-diff per-PR
(<10 min) · full e2e + full mutation + deeper Hypothesis nightly.

---

## 7. Wave 0 — Self-improvement loop

Every tool (gate, agent, scaffold) carries a uniform self-improvement component, mirroring the project's
existing capture → propose → approve protocol:

- **Loop:** tool hits a gap → emits a **proposal** (SARIF-shaped card: source / category / evidence /
  proposed-diff / dedup-key / confidence) → a **backlog** (the bd task system; dedup + supersede +
  vote-evict) → surfaced **batched at the P6 and session-end checkpoints** → **revdiff** review → **approve**
  (lands as a diff) or **decline** (suppressed, never re-raised).
- **Grounding rule (reliability):** a proposal MUST cite an external signal — gate verdict, surviving
  mutant, dismissed finding, template-drift diff. Pure self-eval is insufficient (LLMs can't reliably
  self-correct reasoning). Capture on the **2nd occurrence** (one-offs are noise).
- **Guardrails:** **never auto-apply** (agents demonstrably game/weaken their own gates); gates **ratchet
  up only**; proposals are **untrusted data** (describe a change, never inject behavior); review against the
  rule's **origin** (`git log -p`), not just current text (cumulative-drift defense); a tool may
  **abort/flag** rather than force a green result.

---

## 8. Wave 0 — UX: wizard interaction model + color theme

A single consistent interaction model + visual theme across **all** of SetForge — every
wizard (conflict / staging / migration / seed / cleanup / claude-merge) and all non-wizard
CLI output (`status` / `install` / `compare` / `inspect`). Built on **prompt_toolkit**
(`button_dialog` / `radiolist_dialog` / `input_dialog`) — no custom TUI.

### 8.1 Interaction — switchable button bars (no letter menus)

- Every choice is a **navigable button bar** (Option A): `←/→` or `Tab` move the focus,
  `Enter` selects, `Esc` cancels. The focused button is shown reversed (`«Button»`).
- **No memorize-a-letter menus.** Letter keys survive only as **hidden accelerators** for
  power users (`o` jumps to Ours); buttons are the documented surface.
- The same bar renders yes/no prompts (`«Apply fix» [ Skip ] [ Show diff ]`), share-vs-keep
  (`«Share» [ Keep local ] [ Skip ]`), and the seed prompt
  (`«Seed from live» [ Seed from upstream ]`).

### 8.2 Color theme — Tokyo Night

One curated, non-basic, readable palette. **Truecolor where available; official 256-color
fallback** so it degrades cleanly on 256-only clients (e.g. Termius) — never to raw ANSI
basics. Dark background; a light variant is deferred (YAGNI). The theme is shipped (no
user override initially; a `local.yaml` color knob is a future add).

| Role | Tokyo Night | Used for |
|---|---|---|
| accent / primary | `#7aa2f7` | focused button, prompts, brand, `▸` markers |
| success | `#9ece6a` | ✓ installed / clean / applied |
| error | `#f7768e` | ✗ failures / conflicts |
| warning | `#e0af68` | ⚠ soft-fail / drift / needs-decision |
| heading | `#bb9af7` | section titles, wizard frame |
| identifier | `#7dcfff` | file paths, package names, IDs |
| muted | `#9aa5ce` | hints, key legends |
| text | `#c0caf5` | body |

Semantic colors **plus** the Unicode box-framing shown in the wizard mockups are both part
of the theme.

```
┌─ Conflict 1/1 · ~/.claude/CLAUDE.md ───────────────────────────────┐
│ OURS (this host)        - Run /goal only for complex tasks.         │
│ THEIRS (upstream)       - Always run /goal at the start of session… │
├─────────────────────────────────────────────────────────────────────┤
│   [ Ours ]  «Theirs»  [ Edit ]  [ Claude-merge ]  [ Skip ]          │
│   ← → move · Enter choose                                          │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 9. Wave 1–2 — Config-reconciliation pillar

### 9.0 Fetch upstream — where "theirs" comes from

`setforge install` **first fetches upstream** when the source is a git repo: it
`git pull`s the config repo, and the newly-pulled file content becomes **THEIRS** in the
3-way (base = last-installed snapshot, ours = live, theirs = new upstream). `--no-fetch`
skips the pull for offline / air-gapped runs. No new upstream + no local edits ⇒ nothing
to reconcile; the common case is a fast no-op.

### 9.1 Merge engine — plain line-level 3-way + `claude-merge`

- **Engine = git's behavior, line-level 3-way.** Region you didn't touch but
  upstream changed → applied silently, no prompt. Both touched the same region →
  conflict → wizard.
- **setforge never rewrites a non-conflict line.** Clean lines pass through
  byte-for-byte. No reformatting, no unwrap/rewrap engine. (Your "don't mangle my
  file" concern → there's no engine to mangle it.)
- **Reflow / line-split pain is handled by `claude-merge`, not a clever diff.**
  We deliberately DROPPED word/char-level merging, unwrap-normalize, and auto
  re-wrap — too many edge cases. When a reflow makes an ugly conflict, you press
  `c`.

Conflict wizard:

```
setforge install — conflict in ~/.claude/CLAUDE.md (1 region)

<<<<<<< OURS (this host)
- Run `/goal` only for complex tasks.
=======
- Always run `/goal` at the start of every session, then end the turn.
>>>>>>> THEIRS (upstream)

  [ Ours ]  «Theirs»  [ Edit ]  [ Claude-merge ]  [ Skip ]
  ← → move · Enter choose                  (« » = focused button)
```

Selection is a **navigable button bar** (`←/→`/`Tab` move, `Enter` choose), never a
letter menu — the project's standing UX (see §8). Letter accelerators still work as a
hidden power-user shortcut; buttons + the Tokyo Night theme are the primary surface.

`claude-merge` (the **Claude-merge** button) — **one resumable `claude -p` session per conflict**:

```
> c
claude-merge — CLAUDE.md region
your instruction ⟩ keep "only for complex tasks", take upstream's "between turns"

⟳ claude session #a1b2c3 (new, for this conflict)
draft:
  - Run `/goal` only for complex tasks to keep the evaluator engaged between turns.

  «Accept»  [ Re-prompt ]  [ Edit ]  [ Cancel ]      (Re-prompt resumes #a1b2c3)
> Re-prompt
re-prompt #a1b2c3 ⟩ drop "to keep the evaluator engaged", too wordy
⟳ resuming #a1b2c3 …
draft:
  - Run `/goal` only for complex tasks, between turns.
  «Accept» …
> Accept
✓ saved as your local version
```

Rules:
- One session per conflict; `re-prompt` resumes that session (`claude -p --resume <id>`) so refinements build on the prior attempt — not a cold start.
- **Built-in default prompt** always frames the session; your typed note is *appended* (never
  replaces it); Enter = default-only. The default is roughly:

  ```
  Resolve ONE merge conflict in <file>. OURS is the host-local version, THEIRS is
  upstream; both edited the same region. Produce a SINGLE merged version of just
  this region that honors both intents. Output ONLY the merged text — no conflict
  markers, no commentary.
  ⟨your instruction, appended verbatim if you typed one⟩
  ```
- Sessions are per-conflict, independent, and **discarded on Accept/Cancel** (re-doing a region starts
  fresh — clean, no lingering sessions).
- **Instruction is optional**: Enter = the default prompt alone; when you type nothing the wizard
  **shows the default prompt** first, so you see exactly what Claude is asked to do.

### 9.2 Share vs keep-local — hunk-level staging (git `add -p` style)

- Spine = `status` / `share` / `keep` (your "works like git" north-star).
- Unit is **per hunk**, so one file can have some hunks shared, some kept local.
- A sync-time wizard fires **only** when you install/sync with still-unclassified edits (the 2A fallback).
- Sub-word staging was considered and **DROPPED** (too much machinery to store "half a line shared").

```
$ setforge status --profile=debian-vm
Local changes (host-local until shared):
  modified  CLAUDE.md         (local)
  modified  settings.json     (local)

$ setforge stage CLAUDE.md          # per-hunk: [s]hare [l]ocal [n]ext
Hunk 1/2 "## Session rules"  + Run `/goal` only for complex tasks.   > l
Hunk 2/2 "## Tool preferences"  + Always use `rg`, not `grep`.        > s
Staged: 1 shared, 1 kept-local.
```

- **Marking `share` auto-drafts the shared wording**: `claude -p` rewrites host-specific phrasing into
  shareable wording (you accept/edit), and you can **follow up with more prompts** to refine — the same
  resumable feel as `claude-merge`.
- **Local ⇄ shared is bidirectional**: re-running `stage` re-classifies a hunk (`keep` ⇄ `share`). On
  `sync`, newly-`share`d hunks are captured into `tracked/` (the shared config); commit + push → other
  hosts pick them up on install. Re-stage is the single mechanism (no separate `promote`/`demote` verb).

### 9.3 Store — extend the existing intent/state split

setforge already splits intent (`local.yaml`) from machine state
(`~/.local/state/setforge/`, which already holds `transitions/`, `meta.json`,
spans manifests, and a byte-faithful base). Keep that split:

```
~/.config/setforge/local.yaml          ← intent only (small, you edit)

~/.local/state/setforge/               ← machine state (setforge owns, gitignored)
  transitions/                         ← (exists) history + meta.json for revert
  base/<profile>/<file-id>             ← last-installed upstream snapshot (3-way base), verbatim
  local/<profile>/<file-id>            ← your recorded keep-local content, verbatim
  index/<profile>.json                 ← classification: which hunks shared vs local, pending
```

- **Base is explicit + inspectable** (a real file you can `cat`/`diff`), not
  hidden/derived. `compare` shows a real 3-way (no "expected/unexpected"
  classifier). First install where the file already exists & differs and no base
  is recorded → setforge does NOT silently overwrite; it prompts:

```
~/.claude/CLAUDE.md already exists and differs from upstream, no base recorded.
Seed the merge base from:  [l] live (keep your edits)   [u] upstream (replace)
```

- This is the structural kill of old F1/F2.
- **Built-in diff viewer.** `setforge inspect <file>` renders the store in the Tokyo Night
  theme — a **3-way view** (base | live | merge-preview) plus an `index` summary of which
  hunks are shared vs kept-local. Self-contained (no external viewer dependency); same
  button-bar + theme as the wizards.
- **Store encoding (settled).** The **file-id is the `tracked_files` key** (e.g.
  `base/debian-vm/claude_md`) — stable, human-readable, `cat`/`diff`-able. The **index**
  (`index/<profile>.json`) lists each file's hunks as `{ class: local|shared|pending, label, hash, ctx }`.
  A hunk is matched by **content-hash + 3-way position against the base**, *not* by the label — so it
  works for any line (heading or not), survives line-number drift, and a hunk changed on both sides
  becomes `pending` → wizard. `local.yaml` stays small; bulky content lives in the state dir.

---

## 10. Wave 1–2 — Package-provisioning pillar

**North-star:** simple overall, no over-engineering — but **flexible**. Provisioning
reconciles a declared set of host capabilities (tools, plugins, extensions, config
overrides) into existence, the same "declare intent → reconcile reality" model as
pillar 1. A fresh host is just maximal drift from empty.

This pillar is **not greenfield** — three same-shaped reconcilers already exist
(`claude_plugins`, VSCode `extensions`, `cargo_binaries`, each an item-list + an
`ADDITIVE`/`PRUNE`/`REPORT` policy). The pillar generalizes them into one model and
adds the missing ecosystems.

### 10.1 Two declaration forms — bare items and bundles

**Not everything is a bundle.** Single tools are declared as bare items; a `bundle`
is used only when several components must be grouped and ordered.

- **Bare item** — one provisioned thing in a top-level `packages:` registry, referenced
  from a profile (same define-up-top / list-in-profile pattern as `tracked_files` and
  `claude_plugins`).
- **Bundle** — a named group of components with an ordering spec: a component with no
  `depends_on` runs in **parallel**; `depends_on` makes a **pipeline** edge. A bundle
  component may **reference** a bare item by name **or inline** its full definition.
- **Any provisioner can be a component.** A bundle mixes *any* types — `cargo` / `python` /
  `go` / `github_release` / `plugin` / `extension`, a `file`, or a reference to a bare item —
  with `depends_on` between any of them. (The revdiff bundle just happens to use
  release → plugin → file.)

### 10.2 Provisioners (item types)

| Type | Installer | Notes |
|---|---|---|
| `cargo` | `cargo install` | clean `--list` probe; slow (compiles); `cargo uninstall` exists |
| `python` | `uv tool install` | clean `uv tool list` probe; fast; clean uninstall |
| `go` | `go install pkg@ver` | **no native installed-list** → idempotency needs a binary scan |
| `github_release` | download a GH release asset | the new surface — see §10.3 |
| `plugin` | `claude plugin install/enable/disable` | existing engine; never writes `enabledPlugins` directly |
| `extension` | `code --install-extension` | existing engine |

All four ecosystems are **user-scope by default** (`~/.cargo/bin`, `~/.local`, `$GOBIN`,
`~/.local/bin`). System-scope (apt) is gated — see §10.5.

### 10.3 `github_release` provisioner

Pulls a prebuilt binary from a GitHub release. Fields:

```yaml
packages:
  revdiff-bin:
    type: github_release
    repo: raulfrk/revdiff
    tag: v0.8.9                  # version pin
    asset: revdiff_linux_x86_64.tar.gz   # user-specified (Linux-only for now)
    checksum: sha256:…           # verify the download
    extract: true                # asset is an archive
    binary: revdiff              # which file inside the archive to install
    install: ~/.local/bin        # dir
    rename: revdiff              # final name
    chmod: "+x"
```

- **Archive-aware:** most GH releases ship `.tar.gz`/`.zip`; download → verify checksum
  → extract → pick `binary:` → install + rename + `chmod`.
- **Linux-only** initially; `{os}`/`{arch}` asset tokens are a trivial future add if a
  second platform is ever provisioned.

### 10.4 Bundles + user-level overrides (the revdiff case)

A capability can span ecosystems with dependencies. revdiff = a binary (GH release) +
a Claude plugin (requires the binary) + override launcher scripts (deploy into the
plugin's data dir). The override scripts are the **user-level override** mechanism:
revdiff's launcher resolver checks a user layer (`$CLAUDE_PLUGIN_DATA/scripts/<name>`)
before its bundled default, so a deployed file silently wins.

```yaml
bundles:
  revdiff:
    components:
      - id: bin
        package: revdiff-bin                 # reference the bare item
      - id: plugin
        plugin: revdiff@revdiff
        depends_on: [bin]                    # pipeline edge
      - id: launcher-diff
        file: launch-revdiff.sh              # a tracked file → plugin data dir
        into: ~/.claude/plugins/data/revdiff-revdiff/scripts/
        chmod: "+x"
        depends_on: [plugin]
        share: host-local                    # default; opt into `shared`
      - id: launcher-plan
        file: launch-plan-review.sh
        into: ~/.claude/plugins/data/revdiff-planning-revdiff/scripts/
        chmod: "+x"
        depends_on: [plugin]
        share: host-local
```

- A `file` component is "a package brings its own config" — it reuses pillar-1's
  tracked-file + **host-local-vs-shared staging** (default host-local; opt into shared).
  No separate override mechanism.

### 10.5 Scope, removal, versions, plugin-merge

- **User vs system scope.** User-scope by default. System (apt) requires a profile flag
  `allow_system: true` (intent) **and** runtime capability (`geteuid()==0` or `sudo -n`
  succeeds). The sudo call is itself the confirmation. No capability ⇒ **soft-fail**
  (warn + skip, never hang on a password prompt).
- **Removal.** Binaries are **never auto-pruned** (uninstall is unsafe/irreversible).
  Removal happens only via an explicit `setforge cleanup` wizard: *delete* vs
  *mark-orphan* (drop from config, leave installed).
- **Versions + lockfile (B10).** Pin-in-config is the baseline (`tag:` / `version:`). A **lockfile**
  layers on: a single committed **`setforge.lock`** (config-repo root, all ecosystems + profiles)
  pinning the **exact resolved identity + checksum** per package, each in its ecosystem's natural form —
  `cargo` version + crates.io `.crate` sha256 · `python`(uv) version + wheel hash · `go` module version
  + module sum (`h1:`) · `github_release` tag + asset sha256 · `extension` `publisher.ext@version` ·
  `plugin` best-available version/commit (**weaker upstream pin — flagged, best-effort**). Commands:
  `setforge lock` (resolve → write), `install` (uses the lock if present), `lock --update <pkg>`
  (bump + re-lock), `--locked` (CI fails on drift). Opt-in: no lock ⇒ resolve from specs.
- **Plugin keep-existing.** The default `plugins_reconcile: ADDITIVE` already keeps
  pre-existing unmanaged plugins untouched (installs/enables declared, disables nothing).
  `PRUNE` disables non-declared; `REPORT` shows what `PRUNE` would do. (No
  prune-whitelist for now — `PRUNE` stays all-or-nothing.)

---

## 11. Wave 3 — Migration path

A **one-shot, reversible** cutover honoring COMPATIBILITY.md (additive-first,
**expand → contract**, up + down migration per `schema_version` bump). The `migrate`
verb + `MIGRATE` transition already exist, so revert works.

- **Reversible by snapshot.** `migrate` first snapshots the full pre-migration state
  (files, `schema_version`, package keys) into the transitions store; `setforge revert`
  rolls the entire cutover back to the 1.0 state.
- **"Ask me" default.** Most changes are confirmed interactively, **one at a time with
  enough context** to understand each.
- **Auto-fix + confirm on breakage risk** (mode *b*). When migration detects a change
  that would silently break a host (e.g. an overlay whose anchor bytes no longer match
  live), it computes the fix (re-map current → desired) and asks a single
  confirm before applying — eyes-on exactly where it matters.
- **Unambiguous mappings auto-apply** and are reported; **ambiguous files ask.** The
  canonical ambiguous case is a `disposition: shared` file whose live differs from
  tracked: migration fires the §9.3 "seed base from **live vs upstream**" prompt (the
  F1/F2 kill) to establish the 3-way base.
- **Mapping table** (config-reconcile): `disposition:none` → deploy-as-is;
  `disposition:shared` → base + staged hunks (ambiguous ⇒ seed prompt);
  `disposition:forked|pinned` + host-local user-sections / host-local spans / overlays
  → host-local staged hunks; shared user-sections → shared hunks.
- **Package keys reshape:** `cargo_binaries` → `packages` (`type: cargo`),
  `claude_plugins` → `packages`/`bundles` (`type: plugin`), `extensions` → `packages`
  (`type: extension`). Migration **proactively detects** a binary + plugin + override
  files for the same tool and **offers to fold them into a `bundle`**.
- **Interrupt-safe:** a half-finished `migrate` (Ctrl-C) **resumes** from where it
  stopped on re-run — decisions already made are not re-asked.
- **Bidirectional.** `migrate` ↔ `revert` ↔ **redo** (re-running revert redoes the migration), each
  snapshotted — change your mind in either direction safely.
- **Version guard.** setforge stamps the config's format version; if a binary can't operate the live
  format (e.g. config rolled back but not the binary), it **stops with clear guidance** ("run `migrate`,
  or roll the binary back") — never a silent mismatch. Works both directions, any time.
- **Contract deferred (C5).** v1.0.0 **keeps the old-format reader** so in-tool rollback to the 1.0 model
  works for a release; the hard contract (deleting the dead keys + the reader) ships in **v1.1+**, once
  2.0 is proven. (The accepted resolution: keep the reader, guard the version, reverse freely.)

---

## 12. Decision guide — "I want X → use Y"

The four old mechanisms (disposition / sections / spans / overlays) collapse into the
git-like staging + provisioning model:

| I want… | …use |
|---|---|
| this file's content host-specific | edit live → `stage` the hunk **keep-local** |
| my edit to flow back to the shared config | `stage` the hunk **share** |
| to resolve an upstream change to a file I also edited | conflict wizard on `install` (button bar; `c` = claude-merge) |
| a CLI tool present (Rust / Python / Go) | bare item in `packages:` (`type: cargo`/`python`/`go`) |
| a prebuilt binary from a GitHub release | `packages:` item, `type: github_release` |
| a Claude plugin / VSCode extension present | `packages:` `type: plugin`/`extension` |
| a tool + its plugin + its override scripts as ONE unit | a `bundle` (components + `depends_on` ordering) |
| a user-level override on a plugin (e.g. revdiff new-window) | a `file` component → into the plugin's data dir (host-local by default) |
| to keep my pre-existing unmanaged plugins | nothing — `ADDITIVE` (default) leaves them |
| to remove a managed tool | `setforge cleanup` → delete vs mark-orphan |
| a system-level (apt) install | set `allow_system: true`; setforge uses root/sudo, else soft-skips |
| to inspect a file's base vs live vs merge-preview | `setforge inspect <file>` (themed 3-way viewer) |
| to move an existing host onto the new model | `setforge migrate` (one-shot, reversible) |

---

## 13. Decomposition — epics + dependencies

**Bead hierarchy = version → sub-epic → task.** Each release is a **top-level epic**; the lettered
epics are **sub-epics** under it; the numbered items are **tasks** under each sub-epic (beads `--parent`
chain: `v1.0.0` → `A` → `A1`). Two release epics:

- **Epic `v1.0.0`** — sub-epics **A–F** below (build order in §4; ~40 tasks).
- **Epic `v1.1`** — sub-epic **G** (project profiles — §14).

Each task carries its own `--design`/`--acceptance` + dep links when the tree is created; the detail
in this section and §14 is what those fields are populated from.

Sub-epics + their tasks (the build-order waves are in §4):

**Epic A — Config-reconciliation engine**
- A0. Fetch-upstream step (`git pull` source; `--no-fetch`)
- A1. Store layout: `base/` + `local/` + `index/` under the state dir (schema) (+INV-10)
- A2. Line-level 3-way merge + conflict detection (+INV-1/2/6)
- A3. Conflict wizard UX (button bar)
- A4. `claude-merge` — resumable per-conflict `claude -p` sessions
- A5. Hunk-level staging (`status` / `share` / `keep`) (+INV-8)
- A6. First-install-on-divergence "seed base from live vs upstream" prompt (F1/F2 kill)
- A7. Built-in themed diff viewer (`setforge inspect`)

**Epic B — Package-provisioning**
- B1. Provisioner protocol + uniform reconcile/error model (exit-gating, success-only
  revert delta, REPORT short-circuit, idempotent skip-with-matching-key) (+INV-7)
- B2. `cargo` provisioner (refactor existing `cargo_binaries` into the protocol)
- B3. `python` provisioner (`uv tool`)
- B4. `go` provisioner (incl. binary-scan idempotency probe)
- B5. `github_release` provisioner (download / checksum / extract / install / chmod)
- B6. Bundle model — components, parallel vs `depends_on` pipeline, reference-or-inline (+INV-9)
- B7. `file` components + user-override deploy into plugin data dirs (host-local/shared)
- B8. Fold existing `plugin` + `extension` reconcilers into the model (vocabulary-align)
- B9. `setforge cleanup` wizard — delete vs mark-orphan
- B10. Lockfile (own sub-epic; resolved exact versions; layered on later)

**Epic C — Migration**
- C1. Expand: `schema_version` 2.0, dual-read (old keys still honored)
- C2. `migrate` config-reconcile mapping (disposition/sections/spans/overlays → staging)
  with seed-base prompt + auto-fix-confirm (+INV-3/5)
- C3. `migrate` package-key reshape + bundle-detection offer
- C4. Snapshot + down-migration / revert **+ redo (bidirectional)**; interrupt-resume (+INV-3/5)
- C-guard. **Version guard** — stamp the config format version; detect binary↔format mismatch → clear
  "migrate / roll back the binary" guidance (never silent). Keeps the old-format reader in v1.0.0.
- C5. Contract: drop the dead old keys + reader — **DEFERRED to v1.1+** (keep the reader in v1.0.0 so
  rollback works; ship contract once 2.0 is proven)

**Epic D — UX framework** (Wave 0; consumed by all wizards)
- D1. Tokyo Night color theme — semantic role palette, truecolor + 256 fallback, dark-only
- D2. Button-bar wizard widget (prompt_toolkit) — navigable selection, hidden letter
  accelerators, box framing; one widget reused by every wizard + the inspect viewer
- D3. Apply theme + button widget across all surfaces — conflict, claude-merge, staging,
  seed prompt, migration prompts, cleanup, and non-wizard CLI output

**Epic E — Testing harness** (Wave 0)
- E1. Harness: Hypothesis strategies + invariant helpers + `RuleBasedStateMachine` scaffold
- E2. Invariant stateful machine — a `RuleBasedStateMachine` that ASSEMBLES the per-component
  `@invariant`s (each authored with its own component) and checks them after every generated step. E2 does
  not re-implement them centrally; it depends on the pillar cores and lands once A+B exist.
- E3. Coverage policy: `branch=true`, `fail_under=85` unit-only, exclude config, CI gate
- E4. Mutation: `mutmut` scoped to core, diff-on-PR + nightly, >80% score
- E5. e2e audit-and-prune: per-test verdict manifest; thin to critical journeys + smoke
- E6. Build the integration tier (`tmp_path` + pytest-subprocess) absorbing pushed-down edge cases

**Epic F — Project Claude tooling** (Wave 0)
- F1. `docs/RULES.md` rule index (distil RFC invariants + design rules + conventions); also carries the
  **testing rubric** (tier-decision guide + the invariant-ownership map + coverage/mutation bars)
- F2a. Deterministic policy/AST lint gates (wizard-letter-ban, theme-hardcode-ban, `shell=True`-ban,
  legacy-API-ban) + pre-commit/CI + optional commit hook
- F2b. Wire the coverage + mutation gates into pre-commit/CI (once the E3/E4 policy exists)
- F3. `test-quality-reviewer` agent
- F4. `design-invariant-reviewer` agent (grounded in RULES.md)
- F5. `enforce-tests` skill + wire the fan into session-flow Phase 5/7
- F6. Generative scaffolds (`scaffold-provisioner` / `-wizard` / `author-*`); CI staging
- F7. Self-improvement loop (proposal schema + bd backlog + revdiff approval)
- F8. **Maintenance skills:** `docs-sync` + `security/dependency-triage` (operational tail; `flaky-test-triage` optional)

**Deps:** the tooling/test SPINE — F1 → F2a; F1 → F3/F4 → F5; F1 → (F2a/F3/F4) → F7; and E1 — is built
FIRST and hard-blocks D, the rest of E, and A/B/C. The downstream gate is **F2a + F5 + F7 + E1**. Under
the spine (parallel): D, and E3/E4 → F2b. Then Wave 1 (A0–A3/A7 + B1 ⊕ F6), Wave 2 (A4–A6 + B2–B9), E2
once A+B cores exist, and C → A+B (Wave 3).

**Gap-closers** (from the RFC×tooling traceability): INV-9 + deterministic cycle/ref lint (B6); INV-10
(A1); github_release bad-checksum-abort test (B5); theme 256-fallback-completeness lint (D1); claude-merge
faked-binary integration test (A4).

**Pitfall checklist** (subprocess injection, partial-failure/idempotent reconcile, process/resource/network)
is recorded on the design bead and seeds each B-epic's "bugs to avoid" spec section.

---

## 14. v1.1 roadmap — Project profiles

A **post-1.0** feature: inject a reusable, named set of project-scoped files (a `.claude/` tree —
CLAUDE.md, agents, skills, hooks) into any target path, with a per-file git-visibility toggle.

### 14.1 Concept + CLI

A **project profile** is a named source→dest mapping (like `tracked_files`, but rooted at a target
**project path** instead of `~`), defined in the config repo.

```
setforge project inject <profile> <path> [--git-hidden | --git-tracked]
setforge project list                       # what's injected where
setforge project sync   <path>              # pull profile updates into an injected repo
setforge project remove <profile> <path>
setforge project visibility <path> <file> --hidden | --tracked
```

### 14.2 Git-visibility toggle (settled)

- **Default = hidden**, toggleable **any time**, **per file**.
- **hidden** = added to the target repo's `.git/info/exclude` (per-clone, never committed) — your private
  host-local layer; the team never sees it (the stealth trick beads use).
- **tracked** = normal committed repo content, shared with the team.
- **Toggling**: hidden→tracked un-excludes it; tracked→hidden does `git rm --cached` (keeps the file on
  disk) + re-excludes.
- **Already-tracked `.claude/`**: a *new* injected file is hidden/tracked per the toggle; for a file the
  project already tracks (e.g. the team's `CLAUDE.md`), your additions ride **pillar-1 hunk-staging
  (keep-local)** so the file stays committed but your edits stay private. Mixed visibility in one dir is fine.
- **Non-git target path**: inject still works; the visibility toggle is a no-op (nothing to hide from) —
  noted, not errored.

### 14.3 Reconcile + inheritance (settled)

- **Reconcile**: `project sync` reuses the **pillar-1 base/staging 3-way per project**, so local project
  edits survive a profile update; sync **preserves each file's visibility** choice.
- **Inheritance**: `extends:` — a profile builds on a base; a child file with the same `dst` overrides
  the parent's.
- **Sources** live in the config repo under `project/<profile>/`, declared in a `project_profiles:` block.

### 14.4 Decomposition — Epic `v1.1` ▸ sub-epic G (project profiles)

Each task carries the settled decisions it must implement (→ its `--design` / `--acceptance`):

- **G1. Schema + sources + inheritance.** A `project_profiles:` block (name → source→dest mappings,
  `default_visibility: hidden|tracked` [default **hidden**], optional `extends:`). Sources live in the
  config repo at `project/<profile>/`. Resolve `extends` (child file with same `dst` overrides parent).
  *Accept:* a profile with `extends` resolves to the merged file set; validation rejects unknown refs.
- **G2. `project inject` / `remove`.** Materialise the resolved set into a target **path** (rooted at the
  path, not `~`); `--git-hidden` (default) / `--git-tracked` per inject; `remove` undoes an injection.
  *Accept:* inject into a temp repo lands all files at the right dests; remove restores pre-inject state.
- **G3. Git-visibility engine.** Hidden = add to the target repo's `.git/info/exclude` (**not**
  `.gitignore`); tracked = normal commit. Toggle **any time, per file**: hidden→tracked un-excludes;
  tracked→hidden does `git rm --cached` (keeps the file) + re-excludes. *Accept:* a hidden file never
  shows in `git status`; toggling round-trips; nothing about the hide is itself committed.
- **G4. `project sync`.** Pull profile updates into an injected repo via the **pillar-1 base/staging
  3-way per project** (local project edits survive); **preserves each file's visibility**. *Accept:* a
  profile change syncs cleanly; a locally-edited injected file conflicts via the wizard (no clobber); a
  hidden file stays hidden after sync.
- **G5. Already-tracked files + non-git paths.** A *new* injected file follows the toggle; for a file the
  project **already tracks**, your additions ride **pillar-1 keep-local hunk-staging** (file stays
  committed, edits stay private; mixed visibility per dir is fine). A **non-git target path**: inject
  works, visibility toggle is a **no-op + note**. *Accept:* a hunk injected into a tracked `CLAUDE.md`
  stays out of `git diff`; inject into a plain dir warns + proceeds.
- **G6. `project list` / `project visibility`.** `list` shows what's injected where + each file's
  visibility; `visibility <path> <file> --hidden|--tracked` flips one. *Accept:* `list` reflects reality;
  `visibility` matches G3 behaviour.

Builds on v1.0.0: the tracked-file engine (G2), the pillar-1 merge store (G4/G5), and Epic F's `.claude/`
contents become reusable profiles. **Net-new:** target-path rooting + the git-visibility toggle.
**Dep:** the whole `v1.1` epic depends on `v1.0.0` (Epics A + F shipped).
