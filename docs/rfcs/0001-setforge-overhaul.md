# RFC 0001 — SetForge Overhaul

**Status:** DRAFT — config-reconciliation pillar settled; package pillar + migration still OPEN.
**Output target:** this RFC → an implementation plan (epics + subtasks).
**Method:** brain-dump → per-component elicitation → mockups iterated in revdiff (see ledger for state).

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
| Packages: thin wrapper per ecosystem (cargo/python/go), per-profile, name + version, install-if-absent, pin/bump | SETTLED (direction) |
| Package can **bundle the config it needs** (install tool + deploy its dotfiles as one unit) | SETTLED (direction) |
| Removal **only** on explicit cleanup; wizard asks *delete* vs *mark-orphan* (orphan = drop from config, keep installed) | SETTLED |
| Plugin install ↔ Claude `settings.json` (`enabledPlugins`) integration | OPEN |
| Full package vision; version pin vs lockfile; user-scope-only boundary | OPEN |
| Migration path shape | OPEN |
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
   upstream drift like a git merge. **(SETTLED — §4.)**
2. **Package provisioning** — declare cargo/python/go packages (and Claude
   plugins) that should exist on a host; SetForge makes them present, can bundle
   their config, and removes them only via an explicit cleanup wizard.
   **(Direction settled; details OPEN — §5.)**

---

## 4. Settled design — Config-reconciliation pillar

### 4.1 Merge engine — plain line-level 3-way + `claude-merge`

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

[o] ours   [t] theirs   [e] edit   [c] claude-merge   [s] skip
```

`claude-merge` (the `c` key) — **one resumable `claude -p` session per conflict**:

```
> c
claude-merge — CLAUDE.md region
your instruction ⟩ keep "only for complex tasks", take upstream's "between turns"

⟳ claude session #a1b2c3 (new, for this conflict)
draft:
  - Run `/goal` only for complex tasks to keep the evaluator engaged between turns.

[a] accept   [r] re-prompt (resumes #a1b2c3)   [e] edit   [x] cancel
> r
re-prompt #a1b2c3 ⟩ drop "to keep the evaluator engaged", too wordy
⟳ resuming #a1b2c3 …
draft:
  - Run `/goal` only for complex tasks, between turns.
[a] accept …
> a
✓ saved as your local version
```

Rules:
- One session per conflict; `re-prompt` resumes that session (`claude -p --resume <id>`) so refinements build on the prior attempt — not a cold start.
- A built-in default merge prompt always frames it; your typed note is appended; Enter = default-only.
- Sessions are per-conflict, independent.
- OPEN (small): default-instruction-vs-always-type; keep session after accept or discard.

### 4.2 Share vs keep-local — hunk-level staging (git `add -p` style)

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

- OPEN (small): should `claude -p` also draft the *shared* wording at stage time (classify + write in one step)?

### 4.3 Store — extend the existing intent/state split

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
- **Implementation details deferred** (per-file-id encoding, index schema, exact dir names) — revisit when we spec this pillar. `local.yaml` scaling concern resolved: bulky content lives in the state dir, not YAML.

---

## 5. OPEN — Package-provisioning pillar + migration (next brain-dump)

Direction settled (see ledger); details to elicit next session:

1. **Full package vision** — beyond install-if-absent / pin / bundle-config; the user noted the vision isn't fully formed.
2. **Plugins ↔ Claude `settings.json`** — `enabledPlugins` is managed by the `claude` CLI and drifts benignly today; how does package-provisioning own plugin install without fighting Claude's own settings writes?
3. **Version pin vs lockfile** for packages.
4. **User-scope-only boundary** (system installs need confirmation per env rules).
5. **Migration path shape** — one-shot up-migration disposition/spans/sections → new model, reversible? (cross-cutting; do last, honoring COMPATIBILITY.md.)

## 6. Decomposition (filled once design settles)

_TBD — becomes the epic tree of implementation beads (epics + subtasks). Likely:
one epic for config-reconciliation (engine / staging / store / claude-merge),
one for package-provisioning, one for migration._
