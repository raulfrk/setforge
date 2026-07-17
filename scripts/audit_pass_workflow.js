// Release-candidate audit — one pass (rc1/rc2/rc3), reused verbatim per bead.
// Approved design: Atelier `rc-audit-design` (2026-07-16).
//   Decompose (subsystem × lens) → fan read-only auditors → 2-skeptic verify
//   (majority-refute; split → judgment call) → auto-fix MECHANICAL confirmed
//   (grouped by file, no-conflict) → return structured report.
// The GATE (full suites) + TAG (v1.0.0-rcN) run in the ORCHESTRATOR after this
// returns — a workflow can't block on the 42-min docker e2e or a human notify.
//
// Models (tiered): opus deep-lenses + skeptics; sonnet rubric/dynamic/fixes.
// args: { rc: "rc1"|"rc2"|"rc3", perCellCap?: number }

export const meta = {
  name: 'release-audit-pass',
  description: 'Whole-codebase release audit: fan auditors (subsystem×lens) → 2-skeptic verify → auto-fix mechanical + collect judgment calls → structured report (gate+tag done by orchestrator)',
  phases: [
    { title: 'Audit', detail: 'fan read-only auditors across subsystem×lens + whole-codebase lenses' },
    { title: 'Verify', detail: '2 refute-biased skeptics per unique finding' },
    { title: 'Fix', detail: 'auto-apply mechanical confirmed fixes, grouped by file' },
  ],
}

const RC = (args && args.rc) || 'rc1'
const CAP = (args && args.perCellCap) || 6 // top-N findings per cell, by severity, to bound the verify fan
const ROOT = (args && args.root) || '.' // worktree abs path — ALL agents cd here so reads+fixes hit the isolated tree, never main
const CD = `First: \`cd ${ROOT}\` (work ONLY in this tree). `

// --- decomposition -----------------------------------------------------------
const SUBSYSTEMS = [
  { key: 'config',       paths: 'setforge/config.py setforge/source.py setforge/deploy.py setforge/transitions.py' },
  { key: 'reconcile',    paths: 'setforge/reconcile/ setforge/reconcile_adapter.py setforge/structural_merge.py setforge/yaml_merge.py' },
  { key: 'provision',    paths: 'setforge/provision/ setforge/binaries.py' },
  { key: 'cli',          paths: 'setforge/cli/' },
  { key: 'ui',           paths: 'setforge/ui/ setforge/wizard.py' },
  { key: 'plugins-ext',  paths: 'setforge/claude_plugins.py setforge/vscode_extensions.py setforge/claude_marketplace_cache.py' },
  { key: 'migration',    paths: 'setforge/migrations/ setforge/errors.py setforge/user_section_markers.py' },
]

// Per-subsystem lenses (run over each subsystem's files).
const SUBSYS_LENSES = [
  { key: 'correctness',  agentType: 'python-substance-reviewer',  model: 'opus',
    ask: 'Hunt real correctness bugs, edge cases, and error-model defects. Trace the logic; find where inputs break it.' },
  { key: 'security',     agentType: 'security-reviewer',          model: 'opus',
    ask: 'Reason about exploitability: injection, path/confinement escape, unsafe deserialization, secret handling, TOCTOU, trust-boundary validation.' },
  { key: 'concurrency',  agentType: 'concurrency-reviewer',       model: 'opus',
    ask: 'Lock discipline, shared-state safety, guard/try-finally balance, cancellation, signal-safety, resource/fd/proc leaks, clock-base consistency.' },
  { key: 'invariants',   agentType: 'design-invariant-reviewer',  model: 'opus',
    ask: 'Check the docs/RULES.md invariants (INV-*/UX-*/SAFE-*/PROV-*) this subsystem owns; report violations.' },
  { key: 'test-quality', agentType: 'test-quality-reviewer',      model: 'sonnet',
    ask: 'Per docs/RULES.md Part 2: change-detector / over-mocked / tautological / assertion-free tests, and coverage-is-not-assertion gaps for THIS subsystem.' },
  { key: 'conventions',  agentType: 'python-specifics-reviewer',  model: 'sonnet',
    ask: 'Project conventions (StrEnum/dataclass/pathlib/PEP 604/695), type-hint completeness, API-surface hygiene.' },
  { key: 'docs',         agentType: 'python-prose-reviewer',      model: 'sonnet',
    ask: 'Docstring/comment accuracy vs the actual code; stale or misleading prose; drift.' },
  { key: 'deadcode',     agentType: 'complexity-adversary',       model: 'sonnet',
    ask: 'Argue for LESS: dead code, unused symbols, premature abstraction, over-engineered/gold-plated constructs. Verify unused via a reference audit before claiming dead.' },
]

// Whole-codebase lenses (run once, not per subsystem).
const GLOBAL_LENSES = [
  { key: 'coverage',  agentType: 'test-quality-reviewer', model: 'sonnet', paths: 'tests/ setforge/',
    ask: 'Produce a COVERAGE-GAP report per tier — UNIT (tests/test_*.py), INTEGRATION (cross-module seams: sync↔install↔reconcile), E2E (tests/docker). Name critical paths with no test at each tier. Numeric gap assessment, not pass/fail. Run `uv run pytest --cov --co -q` context if useful.' },
  { key: 'cli',       agentType: 'general-purpose',       model: 'sonnet', paths: 'setforge/cli/', dynamic: true,
    ask: 'CLI CORRECTNESS — audit every command for real. For each subcommand: arg parsing, exit codes, --help text, error messages, --profile handling. Prefer DRIVING the real binary (`uv run setforge <cmd> --help`, invalid-arg cases, `--profile=` missing) and checking output/exit code; supplement with reading cli/. Report wrong exit codes, misleading errors, broken help.' },
  { key: 'visual',    agentType: 'general-purpose',       model: 'sonnet', paths: 'setforge/ui/ setforge/wizard.py', dynamic: true,
    ask: 'UI / VISUAL audit (dedicated visual-auditor agent is not yet registered this session — you are the fallback). Assess the terminal UI for visual bugs: alignment, wrapping/overflow (narrow AND wide), broken ANSI, color under 256 vs truecolor, malformed wizard/button-bar/dialog/diff-view panels. Use the tests/docker pyte harness (pyte_pty_session → .display grid) to SEE the rendered output if the e2e image is available; else read setforge/ui/ + theme logic and label findings UNCONFIRMED (static). A visual finding needs a captured grid excerpt.' },
  { key: 'migration', agentType: 'general-purpose',       model: 'sonnet', paths: 'setforge/migrations/ COMPATIBILITY.md',
    ask: 'MIGRATION / SCHEMA-COMPAT audit vs COMPATIBILITY.md guarantees: additive-first, expand→contract, an up AND down migration per schema_version bump, lockstep upgrade. Check every registered migration is reversible and floor-gated; flag any schema bump missing a down migration or a compat guarantee.' },
]

const FINDING_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' },
          line: { type: 'integer' },
          severity: { type: 'string', enum: ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'] },
          summary: { type: 'string', description: 'one-sentence defect statement' },
          evidence: { type: 'string', description: 'the concrete signal: repro / failing assertion / static hit / grid excerpt' },
          fix_class: { type: 'string', enum: ['mechanical', 'judgment'], description: 'mechanical = safe deterministic edit; judgment = needs a human/design call' },
          fix_hint: { type: 'string' },
        },
        required: ['file', 'severity', 'summary', 'evidence', 'fix_class'],
      },
    },
  },
  required: ['findings'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    refuted: { type: 'boolean', description: 'true if you can refute the finding (not real / not exploitable / already handled)' },
    reason: { type: 'string' },
  },
  required: ['refuted', 'reason'],
}

function severityRank(s) { return { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 }[s] ?? 4 }
function findingKey(f) { return `${f.file}:${f.line || 0}:${(f.summary || '').slice(0, 60)}` }

// --- Phase 1: AUDIT ----------------------------------------------------------
phase('Audit')
const cells = []
for (const sub of SUBSYSTEMS) for (const lens of SUBSYS_LENSES) cells.push({ sub, lens, paths: sub.paths })
for (const lens of GLOBAL_LENSES) cells.push({ sub: { key: 'all' }, lens, paths: lens.paths })
log(`${RC}: ${cells.length} audit cells (${SUBSYSTEMS.length} subsystems × ${SUBSYS_LENSES.length} lenses + ${GLOBAL_LENSES.length} global)`)

const auditResults = await parallel(cells.map((c) => () =>
  agent(
    CD +
    `Release audit (${RC}), lens="${c.lens.key}", scope="${c.sub.key}" over: ${c.paths}\n\n` +
    `${c.lens.ask}\n\n` +
    `This is a WHOLE-CODEBASE release audit (not a diff review) — read the target files fully. ` +
    `Report ONLY defects with a concrete empirical signal (a reproduction, a failing/absent test, a static hit, or — for visual — a captured grid). ` +
    `No signal ⇒ don't report it. Return at most ${CAP} findings, ranked by severity. ` +
    `Classify each fix_class: "mechanical" (a safe deterministic edit — a wrong constant, a missing guard, a stale docstring) or "judgment" (needs a design/human call). ` +
    `Do NOT edit any files; you are read-only here.`,
    { label: `audit:${c.sub.key}/${c.lens.key}`, phase: 'Audit', schema: FINDING_SCHEMA, model: c.lens.model, agentType: c.lens.agentType }
  ).then((r) => ({ cell: c, findings: (r && r.findings) || [] })).catch(() => ({ cell: c, findings: [] }))
))

// dedupe across cells (same file+line+summary from two lenses = one finding)
const seen = new Map()
for (const { cell, findings } of auditResults.filter(Boolean)) {
  for (const f of findings.slice(0, CAP)) {
    const k = findingKey(f)
    if (!seen.has(k)) seen.set(k, { ...f, lenses: [cell.lens.key], subsystem: cell.sub.key })
    else seen.get(k).lenses.push(cell.lens.key)
  }
}
const unique = [...seen.values()].sort((a, b) => severityRank(a.severity) - severityRank(b.severity))
log(`${RC}: ${unique.length} unique findings after dedupe (from ${auditResults.reduce((n, r) => n + (r ? r.findings.length : 0), 0)} raw)`)

// --- Phase 2: VERIFY (2 skeptics, majority-refute) ---------------------------
phase('Verify')
const SKEPTICS = [
  { agentType: 'complexity-adversary', model: 'sonnet', angle: 'Is this over-stated / already-defended / a non-issue? Refute if the code actually handles it.' },
  { agentType: 'general-purpose',      model: 'sonnet', angle: 'Try hard to REFUTE: is the evidence real and reproducible? Default to refuted=true if you cannot confirm the signal.' },
]
const verified = await parallel(unique.map((f) => () =>
  parallel(SKEPTICS.map((s) => () =>
    agent(
      CD +
      `Adversarially verify this release-audit finding. REFUTE it if you can.\n\n` +
      `File: ${f.file}:${f.line || '?'}  Severity: ${f.severity}\n` +
      `Claim: ${f.summary}\nEvidence claimed: ${f.evidence}\n\n${s.angle}\n` +
      `Read the actual code at that location before deciding. A finding counts as REAL only with a concrete, reproducible signal.`,
      { label: `verify:${f.file}`, phase: 'Verify', schema: VERDICT_SCHEMA, model: s.model, agentType: s.agentType }
    ).then((v) => v || { refuted: true, reason: 'verifier error → refuted' }).catch(() => ({ refuted: true, reason: 'verifier crash → refuted' }))
  )).then((votes) => {
    const refutes = votes.filter((v) => v && v.refuted).length
    // 2 skeptics: 0 refutes → CONFIRMED; 1 → SPLIT (judgment/notify); 2 → DROPPED
    const status = refutes === 0 ? 'confirmed' : refutes === 1 ? 'split' : 'dropped'
    return { ...f, status, votes }
  })
))

const confirmed = verified.filter((f) => f && f.status === 'confirmed')
const split = verified.filter((f) => f && f.status === 'split')
const dropped = verified.filter((f) => f && f.status === 'dropped')
log(`${RC}: verify → ${confirmed.length} confirmed, ${split.length} split (→ notify user), ${dropped.length} dropped`)

// --- Phase 3: FIX (auto-apply MECHANICAL confirmed, grouped by file) ----------
phase('Fix')
const mechanical = confirmed.filter((f) => f.fix_class === 'mechanical')
const judgment = confirmed.filter((f) => f.fix_class === 'judgment') // confirmed-but-not-safe-to-auto-fix → notify

// group by file so no two fixers touch the same file concurrently
const byFile = new Map()
for (const f of mechanical) { if (!byFile.has(f.file)) byFile.set(f.file, []); byFile.get(f.file).push(f) }
const fixResults = await parallel([...byFile.entries()].map(([file, fs]) => () =>
  agent(
    CD +
    `Apply these ${fs.length} verified MECHANICAL fix(es) to ${file}. Make the minimal correct edit for each; do NOT refactor beyond the fix. ` +
    `After editing, run the file's tests if obvious, and \`uv run ruff check ${file}\` + \`uv run ruff format ${file}\`. ` +
    `If a fix turns out to be non-trivial or you're unsure it's safe, DO NOT force it — report it as skipped with a reason.\n\n` +
    fs.map((f, i) => `${i + 1}. [${f.severity}] ${f.file}:${f.line || '?'} — ${f.summary}\n   hint: ${f.fix_hint || '(none)'}`).join('\n'),
    { label: `fix:${file}`, phase: 'Fix', model: 'sonnet', agentType: 'general-purpose' }
  ).then((r) => ({ file, applied: fs.length, report: r })).catch((e) => ({ file, applied: 0, error: String(e) }))
))

return {
  rc: RC,
  totals: { cells: cells.length, unique: unique.length, confirmed: confirmed.length, split: split.length, dropped: dropped.length, mechanicalFixed: mechanical.length, judgmentToNotify: judgment.length },
  confirmed,
  needsHuman: [...split, ...judgment], // split-vote + confirmed-judgment-class → surface to user (Q5 notify)
  fixes: fixResults,
  coverageFindings: unique.filter((f) => (f.lenses || []).includes('coverage')),
}
