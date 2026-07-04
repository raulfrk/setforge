# SetForge rules

The project's **rule index** and **testing rubric** — the single source of
truth that grounds the lint gates, the LLM review agents, and the code
scaffolds. Distilled from [RFC 0001](rfcs/0001-setforge-overhaul.md) and the
repo-root `CLAUDE.md`.

Two parts:

- **Part 1 — Rule index.** Every load-bearing rule, each tagged
  **DETERMINISTIC** (machine-checkable by a lint/gate) or **ADVISORY**
  (judgment, checked by an LLM review agent or a human), with its enforcing
  mechanism.
- **Part 2 — Testing rubric.** The tier-decision guide, the invariant→owner
  map, and the quality bars every implementation references.

Each rule has a **stable short ID** (`INV-*`, `UX-*`, `SAFE-*`, `PY-*`,
`PROV-*`). IDs never change once shipped — gates, agents, and commit messages
cite them.

---

## Part 1 — Rule index

### How to read a rule

| Field | Meaning |
|---|---|
| ID | Stable short identifier; cite it everywhere. |
| Statement | One line: what must hold. |
| Tag | **DETERMINISTIC** = a lint/test/gate proves it · **ADVISORY** = an LLM agent or human judges it. |
| Enforced by | The concrete mechanism: lint name, gate, agent, or human checkpoint. |

A DETERMINISTIC rule must have a mechanism that **blocks** (pre-commit + CI, a
gate, or a test). An ADVISORY rule surfaces in the LLM review fan or a human
gate and never hard-blocks (hard-blocking AI review breeds false-positive
fatigue — RFC §5).

---

### 1.1 Design invariants (RFC §6)

Each invariant is checked by the Hypothesis stateful machine after **every**
step of a random install/sync/revert/migrate sequence. All are DETERMINISTIC.
The owning component authors the `@invariant`; the harness assembles them (see
Part 2(b)).

| ID | Statement | Tag | Enforced by |
|---|---|---|---|
| INV-1 | No silent data loss: every user byte lands in `live ∪ store ∪ shown-in-wizard`. | DETERMINISTIC | `@invariant` (stateful machine); merge engine |
| INV-2 | Base round-trips: `base + recorded-local == live`. | DETERMINISTIC | `@invariant`; store + merge |
| INV-3 | Revert is byte-exact inverse of the prior op. | DETERMINISTIC | `@invariant`; revert tests |
| INV-4 | Reconcile is idempotent: `install ∘ install == install`. | DETERMINISTIC | `@invariant`; reconcile tests |
| INV-5 | Migrate is reversible: `revert ∘ migrate == identity`. | DETERMINISTIC | `@invariant`; migrate tests |
| INV-6 | Merge is non-destructive: every non-conflict region stays byte-identical. | DETERMINISTIC | `@invariant`; merge engine |
| INV-7 | Provision is idempotent: declared == installed ⇒ no-op. | DETERMINISTIC | `@invariant`; provisioner tests |
| INV-8 | Stage fidelity: install deploys exactly the `share`d hunks, nothing else. | DETERMINISTIC | `@invariant`; staging tests |
| INV-9 | Bundle DAG is acyclic and `depends_on` order is honored. | DETERMINISTIC | `@invariant` + a cycle/ref lint on the bundle model |
| INV-10 | Store index ↔ on-disk consistent: no orphan classification. | DETERMINISTIC | `@invariant`; store tests |

---

### 1.2 UI / UX (RFC §8)

| ID | Statement | Tag | Enforced by |
|---|---|---|---|
| UX-1 | Every choice is a navigable **button bar** (`←/→`/`Tab` move, `Enter` selects, `Esc` cancels). No memorize-a-letter menus. | DETERMINISTIC | `wizard-letter-ban` lint — [`scripts/check_policy_lints.py`](../scripts/check_policy_lints.py) (bans `read_one_choice` in `setforge/`) |
| UX-2 | Letter keys exist only as **hidden accelerators**, never as the documented surface. | ADVISORY | `design-invariant-reviewer` |
| UX-3 | No hardcoded colors outside the theme module — only semantic Tokyo Night roles (`accent`/`success`/`error`/`warning`/`heading`/`identifier`/`muted`/`text`). | DETERMINISTIC | `theme-hardcode-ban` lint — [`scripts/check_policy_lints.py`](../scripts/check_policy_lints.py) (no raw ANSI / whole-token hex in `setforge/`) |
| UX-4 | All wizards + non-wizard CLI output use the one shipped theme + button widget — truecolor with **256-color fallback**, dark-only. | DETERMINISTIC | `theme-256` lint — [`scripts/check_policy_lints.py`](../scripts/check_policy_lints.py) (every semantic role resolves to a valid curated 256-color index in `setforge/ui/theme.py`) |
| UX-5 | All wizards build on prompt_toolkit (`button_dialog`/`radiolist_dialog`/`input_dialog`) — no custom TUI. | ADVISORY | `design-invariant-reviewer` |
| UX-6 | Unicode box-framing (per the §8 mockups) is part of the theme, applied consistently across surfaces. | ADVISORY | `design-invariant-reviewer` + human (revdiff) |

---

### 1.3 Safety

| ID | Statement | Tag | Enforced by |
|---|---|---|---|
| SAFE-1 | No `shell=True` in subprocess calls; pass argv lists. | DETERMINISTIC | `shell=True`-ban lint (AST) — [`scripts/check_policy_lints.py`](../scripts/check_policy_lints.py) (repo-wide) |
| SAFE-2 | No legacy/deprecated internal APIs — the four old mechanisms (disposition / sections / spans / overlays) are not reached for in new code. | DETERMINISTIC | `legacy-API-ban` lint — [`scripts/check_policy_lints.py`](../scripts/check_policy_lints.py) (namespace-scoped: new-engine packages must not import the legacy subsystem) |
| SAFE-3 | Config schema evolves **additive-first**: a shipped field's name/type/meaning is fixed; new capability ⇒ new field. | ADVISORY | `python-spec-reviewer` + human gate ([`COMPATIBILITY.md`](../COMPATIBILITY.md)) |
| SAFE-4 | Breaking schema changes go **expand → contract** — old field stays readable through the expand window; removed only at contract. | ADVISORY | human gate (COMPATIBILITY.md); schema review |
| SAFE-5 | Every `schema_version` bump ships **both** an up and a down migration (cross-major downgrade = one command). | DETERMINISTIC | migration-pair check (registry has up + down per bump) |
| SAFE-6 | Forward-tolerant reading: an older engine ignores unknown fields within a major (warns, never crashes); refuses cleanly across a major. | DETERMINISTIC | reader tests + e2e |
| SAFE-7 | First install on divergence with no recorded base **never silently overwrites** — it fires the seed-base prompt (the F1/F2 kill). | DETERMINISTIC | e2e + `@invariant` (INV-1) |
| SAFE-8 | `github_release` downloads are checksum-verified; a bad checksum **aborts** (no install). | DETERMINISTIC | bad-checksum-abort test (the `github_release` provisioner) |
| SAFE-9 | Binaries are **never auto-pruned**; removal only via the explicit `cleanup` wizard. | ADVISORY | `design-invariant-reviewer` + e2e |
| SAFE-10 | A tool may **abort/flag** rather than force a green result; gates **ratchet up only** (never loosen). | ADVISORY | human gate (gate-config review) |

---

### 1.4 Python conventions (CLAUDE.md)

| ID | Statement | Tag | Enforced by |
|---|---|---|---|
| PY-1 | Enum classes that are string-valued use `StrEnum`. | ADVISORY | `python-specifics-reviewer` (until a StrEnum AST lint lands) |
| PY-2 | Plain record types use `@dataclass` (not ad-hoc dicts / tuples). | ADVISORY | `python-specifics-reviewer` |
| PY-3 | Filesystem paths use `pathlib`, not `os.path`. | DETERMINISTIC | ruff (`PTH`) |
| PY-4 | Unions use PEP 604 syntax (`X | None`), not `Optional[X]` / `Union[...]`. | DETERMINISTIC | ruff (`UP007`/`UP045`) |
| PY-5 | Generics use PEP 695 type params (`def f[T](...)`, `class C[T]`, `type X = ...`). | DETERMINISTIC | ruff (`UP046`/`UP047`) |
| PY-6 | Public functions/methods carry complete type hints. | DETERMINISTIC | mypy (strict) |

> Tag note: ruff/mypy already ship the `PTH`, `UP*`, and strict-hint rules, so
> PY-3/4/5/6 are deterministic today. PY-1 needs a small AST lint to catch a
> string-valued plain `Enum` (ruff has no built-in for it).

---

### 1.5 Provisioner protocol (RFC §10)

| ID | Statement | Tag | Enforced by |
|---|---|---|---|
| PROV-1 | A failed component **exit-gates** its pipeline: dependents do not run. | DETERMINISTIC | provisioner-protocol tests + `@invariant` (INV-9) |
| PROV-2 | The revert delta records **only successful** installs (no half-applied rollback). | DETERMINISTIC | provisioner-protocol tests + `@invariant` (INV-3) |
| PROV-3 | `REPORT` / dry-run performs **no writes** (pure diff). | DETERMINISTIC | provisioner-protocol tests (no-write assertion) |
| PROV-4 | Idempotent skip: a component whose key already matches installed state is a **no-op**. | DETERMINISTIC | provisioner-protocol tests + `@invariant` (INV-7) |
| PROV-5 | User-scope by default; system (apt) needs `allow_system: true` **and** runtime root/sudo capability, else **soft-fail** (warn + skip, never hang). | DETERMINISTIC | e2e (soft-fail path) + `design-invariant-reviewer` |
| PROV-6 | `plugin` provisioning never writes `enabledPlugins` directly — it uses the `claude plugin` CLI. | DETERMINISTIC | `legacy-API-ban` lint + e2e |

---

### 1.6 Self-improvement loop (RFC §7)

| ID | Statement | Tag | Enforced by |
|---|---|---|---|
| SELF-1 | A proposal **must cite an external signal** (gate verdict / surviving mutant / dismissed finding / template-drift). Pure self-eval is rejected. | ADVISORY | `scripts/proposals.py` (evidence required at construction); `surface-proposals` skill (human gate) |
| SELF-2 | Proposals are **never auto-applied** — they land only on explicit approval. | ADVISORY | `scripts/proposals.py` (no auto-apply path; `approve()` requires an explicit call); `surface-proposals` skill (human gate) |
| SELF-3 | Proposals are **untrusted data**: they describe a change, never inject behavior/commands/URLs. | ADVISORY | `surface-proposals` skill (evidence fenced as untrusted; review against the rule's `git log -p` origin) |
| SELF-4 | Capture on the **2nd occurrence** (one-offs are noise); declined proposals are never re-raised. | ADVISORY | `scripts/proposals.py` ledger (2nd-occurrence dedup + durable decline-suppress); `surface-proposals` skill |

---

### Rule-count summary

| Tag | Count |
|---|---|
| DETERMINISTIC | 29 |
| ADVISORY | 13 |
| **Total** | **42** |

---

### 1.7 Design decisions (structured reconcile)

Durable **decision records** — the cuts a rule table can't hold (rejected
alternatives + rationale). Not tagged/counted as rules; cite by ID.

#### DEC-1 — Structured key-unit identity is **path-only**

**Decision.** A structured key-unit is identified by its **path alone**.
Composite identity is a non-goal for v1. Two hardenings from the structured
key-unit identity investigation were **rejected**:

- **Composite rename-survival identity** (path + value/sibling fingerprint, so
  an overlay survives an upstream key rename). Rejected — no observed demand:
  the author's config repo has **zero** key renames across its whole git
  history. A rename re-mints the unit, which is acceptable for config files.
- **Per-element list identity** (so a list reorder isn't whole-list
  divergence). Rejected — the sole tracked structured file's lists
  (`permissions.allow`, the `hooks.*` arrays) are **order-significant**, where
  whole-list divergence is the *correct* behavior; per-element identity would be
  wrong.

**What was fixed instead — the dotted-key collision.**
`reconcile/structured_units.py::_walk_leaves` built a unit's path by joining
segments with a bare `.`, so a literal flat key `"a.b"` collided with a nested
`a: {b: …}` (both → path `"a.b"`) and `extract_structured_units` silently
collapsed the two distinct leaves into one unit. Fixed with an **injective path
encoding** (escape a literal `.`/`\` within a segment — see
`structural_merge.encode_key_segment` / `split_key_path`). The encoding is
byte-identical for keys containing neither `.` nor `\`, so existing persisted
index rows are **not** re-minted.

**Scope — JSONC is not walked at key level yet.** `_load_model` returns a
non-`Mapping` `JSONText` for JSONC, so `_walk_leaves` never recurses into it: a
JSONC file (e.g. VSCode `settings.json`) is staged as a **single whole-document
unit**, not per-key. VSCode's flat dotted keys
(`claudeCode.allowDangerouslySkipPermissions`, `editor.fontSize`) therefore do
**not** route through the KeyUnit engine today. The dotted-key fix is
**forward-insurance** for when JSONC key-level walking + structured host-local
keys land — the **STAGE B unification**, which is the follow-up that will
exercise this path.

---

## Part 2 — Testing rubric

Every implementation references this section. The goal is test
**effectiveness**, not quantity — the prior suite was an ice-cream cone (300+
slow e2e over a thin unit base) and the silent-data-loss bugs slipped because
**coverage ≠ assertion** (RFC §6).

### (a) Tier-decision guide — the testing trophy

Pick the **lowest** tier a test can live in. Rule: **push every test as far
down as it goes.**

| Test that exercises… | Tier | How |
|---|---|---|
| Pure logic — merge / diff / reconcile / parse | **UNIT** | No filesystem, **no mocks**. Feed inputs, assert outputs. |
| One subcommand's behavior end to end | **INTEGRATION** | Real `tmp_path` as `$HOME`; subprocess mocked via **pytest-subprocess**. The bulk of the suite. |
| One golden path per verb | **THIN E2E** | Real binaries (`claude`/`code`/`gitleaks`), one happy path, **< 5 min** smoke. |

**Target mix ≈ 80 / 15 / 5** (unit / integration / e2e).

Guidance:

- If a test needs a mock to test pure logic, it belongs one tier up (or the
  logic should be extracted to be unit-testable).
- Mocking **both** fs and subprocess means you're testing the mock — that's an
  e2e smoke instead.
- Edge cases get **pushed down**: an edge first found at e2e should be re-homed
  as an integration or unit case once the logic is isolated.
- e2e exists to catch integration-emergent regressions (install / sync /
  revert / plugin / extension) that lower tiers cannot exercise — one golden
  path per verb, not exhaustive coverage.

### (b) Invariant → owning-component map

Each component authors its **own** `@invariant` methods; the Hypothesis
stateful machine assembles them into one model over `install`/`sync`/`revert`/
`migrate`. A component is responsible for the invariants listed against it.

| Component | Owns invariants |
|---|---|
| store-layout (`base/` + `local/` + `index/`) | INV-2, INV-10 |
| 3-way-merge (line-level engine + conflict detection) | INV-1, INV-2, INV-6 |
| hunk-staging (`status` / `share` / `keep`) | INV-8 |
| provisioner-protocol (reconcile/error model) | INV-7 |
| bundle-model (components, `depends_on`) | INV-9 |
| migrate (config + package reshape, snapshot/revert) | INV-3, INV-5 |
| (whole-machine — see note below) | INV-4 |

INV-4 (reconcile idempotence) is a whole-machine property over repeated
`install` steps and is asserted by the harness across the reconcile path, not
owned by a single component.

### (c) Quality bars

| Bar | Target | Scope | Notes |
|---|---|---|---|
| **Branch coverage** | ≥ 85% | **UNIT suite only** | `branch=true`, `fail_under=85`; exclude non-logic. e2e stays **`--no-cov`** (the known pytest-cov + xdist crash — see `CLAUDE.md` "Final checks"). |
| **Mutation score** | > 80% | merge / reconcile / store **core** | `mutmut`; **diff-mode on PR**, full run **nightly**. The real anti-change-detector measure. |

**Coverage ≠ done.** Lines executed without assertions is exactly how the
silent-loss bugs hid. Mutation score on the core is the bar that proves the
assertions bite.

**Audit-and-prune.** Review **each** test by signal — change-detector /
over-mocked / tautological / assertion-free / redundant / blindly-blessed
snapshot. Safe-delete a test only after confirming another test or a surviving
mutant still covers the behavior. Output a verdict manifest.

### CI staging

| Stage | Runs | Budget |
|---|---|---|
| per-commit | unit | < 1 min |
| per-PR | unit + integration + coverage-gate + e2e-smoke + `mutmut` diff | < 10 min |
| nightly | full e2e + full mutation + deeper Hypothesis | — |

---

## Maintaining this file

`docs/RULES.md` is the grounding source for the gates, the review agents, and
the scaffolds — keep it in sync with the RFC, `CLAUDE.md`, and
[`COMPATIBILITY.md`](../COMPATIBILITY.md). When a rule's mechanism ships,
update its **Enforced by** column from the planned lint/agent name to the real
one. Never recycle an ID for a different rule.
