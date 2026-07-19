// Release-candidate audit — one pass (rc1/rc2/rc3), reused verbatim per bead.
// Redesigned 2026-07-19 (approved spec): 25 cells (7 subsystems × {deep,hygiene,docs}
// + 4 global), tiered models, file-batched tiered verification, post-fix Recheck.
//   Decompose → fan auditors → evidence-checked tiered verify (C/H: 2 skeptics
//   majority-refute; M/L: 1 skeptic) → auto-fix MECHANICAL confirmed (by file)
//   → Recheck fix-touched cells → structured report.
// The GATE (full suites) + TAG (v1.0.0-rcN) run in the ORCHESTRATOR after this
// returns — a workflow can't block on the 42-min docker e2e or a human notify.
// args: { rc: "rc1"|"rc2"|"rc3", root: string (required) }

export const meta = {
  name: 'release-audit-pass',
  description: 'Whole-codebase release audit: 25 tiered cells → evidence-checked tiered verify → auto-fix mechanical + collect judgment calls → recheck touched cells → structured report (gate+tag done by orchestrator)',
  phases: [
    { title: 'Audit', detail: 'fan tiered auditor cells (deep/hygiene/docs per subsystem + global)' },
    { title: 'Verify', detail: 'evidence check + file-batched skeptics (C/H 2-vote, M/L 1-vote)' },
    { title: 'Fix', detail: 'auto-apply mechanical confirmed fixes, grouped by file' },
    { title: 'Recheck', detail: 'post-fix re-audit of fix-touched cells' },
  ],
}

const A = typeof args === 'string' ? JSON.parse(args) : (args || {})
const RC = A.rc || 'rc1'
if (!A.root) throw new Error('args.root is required — refusing to default to "." (an args-delivery failure would audit and FIX the wrong tree)')
const ROOT = A.root // worktree abs path — ALL agents cd here
const CD = `First: \`cd ${ROOT}\` (work ONLY in this tree). `

// --- decomposition -----------------------------------------------------------
const SUBSYSTEMS = [
  { key: 'config',       paths: 'setforge/config.py setforge/source.py setforge/deploy.py setforge/transitions.py' },
  { key: 'reconcile',    paths: 'setforge/reconcile/ setforge/reconcile_adapter.py setforge/structural_merge.py setforge/yaml_merge.py' },
  { key: 'provision',    paths: 'setforge/provision/ setforge/binaries.py' },
  { key: 'cli',          paths: 'setforge/cli/' },
  { key: 'ui',           paths: 'setforge/ui/' },
  { key: 'plugins-ext',  paths: 'setforge/claude_plugins.py setforge/vscode_extensions.py setforge/claude_marketplace_cache.py' },
  { key: 'migration',    paths: 'setforge/migrations/ setforge/errors.py setforge/user_section_markers.py' },
]

// Per-lens asks.
const LENS_ASKS = {
  correctness:  'Hunt real correctness bugs, edge cases, and error-model defects. Trace the logic; find where inputs break it.',
  security:     'Reason about exploitability: injection, path/confinement escape, unsafe deserialization, secret handling, TOCTOU, trust-boundary validation.',
  concurrency:  'Lock discipline, shared-state safety, guard/try-finally balance, cancellation, signal-safety, resource/fd/proc leaks, clock-base consistency.',
  invariants:   'Check the docs/RULES.md invariants (INV-*/UX-*/SAFE-*/PROV-*) this subsystem owns; report violations.',
  'test-quality': 'Per docs/RULES.md Part 2: change-detector / over-mocked / tautological / assertion-free tests, and coverage-is-not-assertion gaps for THIS subsystem.',
  conventions:  'Project conventions (StrEnum/dataclass/pathlib/PEP 604/695), type-hint completeness, API-surface hygiene.',
  docs:         'Docstring/comment accuracy vs the actual code; stale or misleading prose; drift.',
  deadcode:     'Argue for LESS: dead code, unused symbols, premature abstraction, over-engineered/gold-plated constructs. Verify unused via a reference audit before claiming dead.',
}

// Three tiered cells per subsystem.
const CELL_KINDS = [
  { kind: 'deep',    lenses: ['correctness', 'security', 'concurrency', 'invariants'], model: 'opus',                  perLens: 4, cap: 12 },
  { kind: 'hygiene', lenses: ['test-quality', 'conventions', 'deadcode'],              model: 'sonnet', effort: 'low', perLens: 4, cap: 9 },
  { kind: 'docs',    lenses: ['docs'],                                                 model: 'haiku',  effort: 'low', perLens: 4, cap: 4 },
]

// Whole-codebase lenses (run once, not per subsystem).
const GLOBAL_LENSES = [
  { key: 'coverage',  agentType: 'test-quality-reviewer', model: 'sonnet', effort: 'low', paths: 'tests/ setforge/',
    ask: 'Produce a COVERAGE-GAP report per tier — UNIT (tests/test_*.py), INTEGRATION (cross-module seams: sync↔install↔reconcile), E2E (tests/docker). Name critical paths with no test at each tier. Numeric gap assessment, not pass/fail. Run `uv run pytest --cov --co -q` context if useful.' },
  { key: 'cli',       agentType: 'general-purpose',       model: 'sonnet', paths: 'setforge/cli/',
    ask: 'CLI CORRECTNESS — audit every command for real. For each subcommand: arg parsing, exit codes, --help text, error messages, --profile handling. Prefer DRIVING the real binary (`uv run setforge <cmd> --help`, invalid-arg cases, `--profile=` missing) and checking output/exit code; supplement with reading cli/. Report wrong exit codes, misleading errors, broken help.' },
  { key: 'visual',    agentType: 'visual-auditor',        model: 'sonnet', paths: 'setforge/ui/',
    ask: 'UI / VISUAL audit. Assess the terminal UI for visual bugs: alignment, wrapping/overflow (narrow AND wide), broken ANSI, color under 256 vs truecolor, malformed wizard/button-bar/dialog/diff-view panels. Use the tests/docker pyte harness (pyte_pty_session → .display grid) to SEE the rendered output if the e2e image is available; else read setforge/ui/ + theme logic and label findings UNCONFIRMED (static). A visual finding needs a captured grid excerpt.' },
  { key: 'migration', agentType: 'general-purpose',       model: 'sonnet', effort: 'low', paths: 'setforge/migrations/ COMPATIBILITY.md',
    ask: 'MIGRATION / SCHEMA-COMPAT audit vs COMPATIBILITY.md guarantees: additive-first, expand→contract, an up AND down migration per schema_version bump, lockstep upgrade. Check every registered migration is reversible and floor-gated; flag any schema bump missing a down migration or a compat guarantee.' },
]

// Fold the 4 global asks into LENS_ASKS (keyed by GLOBAL_LENSES[i].key) so
// cellPrompt() can build its lens list uniformly for all 25 cells.
for (const g of GLOBAL_LENSES) LENS_ASKS[g.key] = g.ask

// --- schemas -----------------------------------------------------------------
function findingSchema(lensKeys, cap) {
  return {
    type: 'object',
    properties: {
      findings: {
        type: 'array',
        maxItems: cap,
        items: {
          type: 'object',
          properties: {
            lens: { type: 'string', enum: lensKeys, description: 'which lens produced this finding' },
            file: { type: 'string' },
            line: { type: 'integer' },
            severity: { type: 'string', enum: ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'] },
            summary: { type: 'string', description: 'one-sentence defect statement' },
            evidence: { type: 'string', description: 'the concrete signal: repro / failing assertion / static hit / grid excerpt' },
            fix_class: { type: 'string', enum: ['mechanical', 'judgment'], description: 'mechanical = safe deterministic edit; judgment = needs a human/design call' },
            fix_hint: { type: 'string' },
          },
          required: ['lens', 'file', 'severity', 'summary', 'evidence', 'fix_class'],
        },
      },
    },
    required: ['findings'],
  }
}

function verdictSchema(indices) {
  return {
    type: 'object',
    properties: {
      verdicts: {
        type: 'array',
        minItems: indices.length,
        maxItems: indices.length,
        items: {
          type: 'object',
          properties: {
            idx: { type: 'integer', enum: indices, description: 'the finding index you are judging — copy exactly' },
            evidence_check: { type: 'string', enum: ['found', 'relocated', 'absent'], description: 'mechanical evidence-string search result' },
            refuted: { type: 'boolean', description: 'true if you can refute the finding (not real / not exploitable / already handled / evidence absent)' },
            reason: { type: 'string' },
          },
          required: ['idx', 'evidence_check', 'refuted', 'reason'],
        },
      },
    },
    required: ['verdicts'],
  }
}

function severityRank(s) { return { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 }[s] ?? 4 }
function findingKey(f) { return `${f.file}:${f.line || 0}:${(f.summary || '').slice(0, 60)}` }
function rotate(arr, n) { const k = n % arr.length; return arr.slice(k).concat(arr.slice(0, k)) }
// Subsystem-path membership: dir entries (trailing /) prefix-match, file entries exact-match.
function subsystemOwns(sub, file) {
  return sub.paths.split(/\s+/).filter(Boolean).some((p) => (p.endsWith('/') ? file.startsWith(p) : file === p))
}

// --- instrumentation accumulators -------------------------------------------
const perLens = {}            // lens → raw finding count (pre-dedupe, all phases)
const capHits = []            // cell labels whose findings hit maxItems
const cellErrors = []         // cell labels whose agent call crashed
const evidenceChecks = { found: 0, relocated: 0, absent: 0 } // per skeptic verdict (C/H findings counted up to twice)
const refuteByBatchSize = {}  // batchSize → { calls, refutes, judged }

// --- shared audit-cell runner ------------------------------------------------
function cellPrompt(c, scopeNote) {
  const lensList = c.lenses.map((l, j) => `${j + 1}. [${l}] ${LENS_ASKS[l]}`).join('\n')
  return (
    CD +
    `Release audit (${RC}), cell="${c.kindKey}", scope="${c.subKey}" over: ${c.paths}\n` +
    (scopeNote || '') + '\n' +
    `Apply EACH of these ${c.lenses.length} lens(es) explicitly and independently:\n${lensList}\n\n` +
    `This is a WHOLE-CODEBASE release audit (not a diff review) — read the target files fully. ` +
    `For EVERY lens above, either report findings or explicitly conclude it has none — do not skip a lens. ` +
    `Report ONLY defects with a concrete empirical signal (a reproduction, a failing/absent test, a static hit, or — for visual — a captured grid). ` +
    `No signal ⇒ don't report it. Tag each finding with its \`lens\`. ` +
    `At most ${c.perLens} findings per lens and ${c.cap} total, ranked by severity. ` +
    `Classify each fix_class: "mechanical" (a safe deterministic edit — a wrong constant, a missing guard, a stale docstring) or "judgment" (needs a design/human call). ` +
    `Do NOT edit any files; you are read-only here.`
  )
}

async function runCells(cells, phaseName) {
  const results = await parallel(cells.map((c) => () =>
    agent(cellPrompt(c, c.scopeNote), {
      label: c.label, phase: phaseName, schema: findingSchema(c.lenses, c.cap),
      model: c.model, ...(c.effort ? { effort: c.effort } : {}), agentType: c.agentType || 'general-purpose',
    })
      .then((r) => ({ cell: c, findings: (r && r.findings) || [] }))
      .catch((e) => ({ cell: c, findings: [], error: String(e) }))
  ))
  const kept = results.filter(Boolean)
  for (const r of kept) {
    if (r.error) cellErrors.push(r.cell.label)
    if (r.findings.length >= r.cell.cap) capHits.push(r.cell.label)
    for (const f of r.findings) perLens[f.lens] = (perLens[f.lens] || 0) + 1
  }
  return kept
}

function dedupe(cellResults) {
  const seen = new Map()
  for (const { cell, findings } of cellResults) {
    for (const f of findings) {
      const k = findingKey(f)
      if (!seen.has(k)) seen.set(k, { ...f, lenses: [f.lens], subsystem: cell.subKey })
      else if (!seen.get(k).lenses.includes(f.lens)) seen.get(k).lenses.push(f.lens)
    }
  }
  return [...seen.values()].sort((a, b) => severityRank(a.severity) - severityRank(b.severity))
}

// --- shared tiered verify ----------------------------------------------------
const SKEPTIC_ANGLES = [
  { agentType: 'complexity-adversary', angle: 'Is this over-stated / already-defended / a non-issue? Refute if the code actually handles it.' },
  { agentType: 'general-purpose',      angle: 'Try hard to REFUTE: is the evidence real and reproducible? Default to refuted=true if you cannot confirm the signal.' },
]

function skepticPrompt(group, angle) {
  const rows = group.map((f) =>
    `idx ${f.idx}: [${f.severity}] ${f.file}:${f.line || '?'}\n  claim: ${f.summary}\n  evidence claimed: ${f.evidence}`
  ).join('\n')
  return (
    CD +
    `Adversarially verify these ${group.length} release-audit finding(s). Judge EACH finding INDEPENDENTLY — ` +
    `never issue a blanket verdict for the batch; each verdict must stand on its own reasoning.\n\n` +
    `STEP 1 (mechanical, per finding): check the claimed evidence. Normalize whitespace and search for the evidence string ` +
    `ANYWHERE in the cited file (e.g. grep -F on a distinctive fragment). ` +
    `Evidence absent from the WHOLE file ⇒ evidence_check "absent" and refuted=true (reason "evidence-absent"). ` +
    `Found at a different line than cited ⇒ evidence_check "relocated" — NOT a refutation by itself. Found as cited ⇒ "found".\n` +
    `STEP 2 (judgment, per finding): ${angle}\n` +
    `Read the actual code before deciding. A finding counts as REAL only with a concrete, reproducible signal.\n\n` +
    `Findings:\n${rows}\n\n` +
    `Return one verdict per finding, keyed by its idx exactly as listed.`
  )
}

async function verifyFindings(findings, phaseName) {
  const withIdx = findings.map((f, idx) => ({ ...f, idx }))
  const byFile = new Map()
  for (const f of withIdx) {
    const k = f.file && String(f.file).trim() ? f.file : 'GLOBAL'
    if (!byFile.has(k)) byFile.set(k, [])
    byFile.get(k).push(f)
  }
  const calls = []
  for (const [fileKey, group] of byFile.entries()) {
    const ch = group.filter((f) => f.severity === 'CRITICAL' || f.severity === 'HIGH')
    const ml = group.filter((f) => f.severity !== 'CRITICAL' && f.severity !== 'HIGH')
    if (ch.length) for (const s of SKEPTIC_ANGLES) calls.push({ fileKey, group: ch, skeptic: s, tier: 'ch' })
    if (ml.length) calls.push({ fileKey, group: ml, skeptic: SKEPTIC_ANGLES[1], tier: 'ml' })
  }
  const callResults = await parallel(calls.map((c) => () =>
    agent(skepticPrompt(c.group, c.skeptic.angle), {
      label: `verify:${c.fileKey}:${c.tier}:${c.skeptic.agentType}`, phase: phaseName,
      schema: verdictSchema(c.group.map((f) => f.idx)), model: 'sonnet', agentType: c.skeptic.agentType,
    })
      .then((r) => ({ call: c, verdicts: (r && r.verdicts) || null }))
      .catch(() => ({ call: c, verdicts: null }))
  ))
  // Tally votes per finding idx. A crashed/null call counts as a refute for
  // every finding it covered (fail-closed).
  const votes = new Map() // idx → { refutes, judged, checks: [] }
  for (const f of withIdx) votes.set(f.idx, { refutes: 0, judged: 0, checks: [] })
  for (const { call, verdicts } of callResults.filter(Boolean)) {
    const size = call.group.length
    const bucket = (refuteByBatchSize[size] = refuteByBatchSize[size] || { calls: 0, refutes: 0, judged: 0 })
    bucket.calls += 1
    const byIdx = new Map((verdicts || []).map((v) => [v.idx, v]))
    for (const f of call.group) {
      const v = byIdx.get(f.idx)
      const rec = votes.get(f.idx)
      rec.judged += 1
      bucket.judged += 1
      if (!v || v.refuted) { rec.refutes += 1; bucket.refutes += 1 }
      if (v && v.evidence_check) rec.checks.push(v.evidence_check)
    }
  }
  return withIdx.map((f) => {
    const rec = votes.get(f.idx)
    for (const c of rec.checks) evidenceChecks[c] = (evidenceChecks[c] || 0) + 1
    const isCH = f.severity === 'CRITICAL' || f.severity === 'HIGH'
    const status = isCH
      ? (rec.refutes === 0 ? 'confirmed' : rec.refutes === 1 ? 'split' : 'dropped')
      : (rec.refutes === 0 ? 'confirmed' : 'dropped')
    return { ...f, status, refutes: rec.refutes, judgedBy: rec.judged }
  })
}

// --- Phase 1: AUDIT ----------------------------------------------------------
phase('Audit')
const auditCells = []
SUBSYSTEMS.forEach((sub, i) => {
  for (const ck of CELL_KINDS) {
    auditCells.push({
      subKey: sub.key, kindKey: ck.kind, paths: sub.paths,
      lenses: rotate(ck.lenses, i), // deterministic rotation: no lens is permanently last
      model: ck.model, effort: ck.effort, perLens: ck.perLens, cap: ck.cap,
      label: `audit:${sub.key}/${ck.kind}`,
    })
  }
})
for (const g of GLOBAL_LENSES) {
  auditCells.push({
    subKey: 'all', kindKey: g.key, paths: g.paths, lenses: [g.key],
    model: g.model, effort: g.effort, perLens: 6, cap: 6,
    label: `audit:all/${g.key}`, agentType: g.agentType,
  })
}
log(`${RC}: ${auditCells.length} audit cells (7 subsystems × 3 cells + ${GLOBAL_LENSES.length} global); projected agents ≈ cells + verify batches + recheck`)

const auditResults = await runCells(auditCells, 'Audit')
const unique = dedupe(auditResults)
log(`${RC}: ${unique.length} unique findings after dedupe (from ${auditResults.reduce((n, r) => n + r.findings.length, 0)} raw)`)

// --- Phase 2: VERIFY ---------------------------------------------------------
phase('Verify')
const verified = await verifyFindings(unique, 'Verify')
const confirmed = verified.filter((f) => f.status === 'confirmed')
const split = verified.filter((f) => f.status === 'split')
const dropped = verified.filter((f) => f.status === 'dropped')
log(`${RC}: verify → ${confirmed.length} confirmed, ${split.length} split (→ notify user), ${dropped.length} dropped`)

// --- Phase 3: FIX ------------------------------------------------------------
phase('Fix')
const mechanical = confirmed.filter((f) => f.fix_class === 'mechanical')
const judgment = confirmed.filter((f) => f.fix_class === 'judgment')
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
)).then((rs) => rs.filter(Boolean))

// --- Phase 4: RECHECK (post-fix re-audit of touched cells) -------------------
phase('Recheck')
const touched = fixResults.filter((r) => r.applied > 0).map((r) => r.file)
let recheckConfirmed = []
if (touched.length) {
  const recheckCells = []
  SUBSYSTEMS.forEach((sub, i) => {
    const mine = touched.filter((f) => subsystemOwns(sub, f))
    if (!mine.length) return
    for (const ck of CELL_KINDS) {
      recheckCells.push({
        subKey: sub.key, kindKey: ck.kind, paths: mine.join(' '),
        lenses: rotate(ck.lenses, i), model: ck.model, effort: ck.effort,
        perLens: 4, cap: 4, label: `recheck:${sub.key}/${ck.kind}`,
        scopeNote: 'POST-FIX RE-AUDIT: these files were JUST edited by verified mechanical fixes; audit ONLY them for regressions the fixes may have introduced.',
      })
    }
  })
  log(`${RC}: recheck → ${touched.length} touched files, ${recheckCells.length} cells`)
  const recheckResults = await runCells(recheckCells, 'Recheck')
  const recheckUnique = dedupe(recheckResults)
  const recheckVerified = recheckUnique.length ? await verifyFindings(recheckUnique, 'Recheck') : []
  recheckConfirmed = recheckVerified.filter((f) => f.status === 'confirmed').map((f) => ({ ...f, recheck: true }))
  log(`${RC}: recheck → ${recheckConfirmed.length} new confirmed finding(s)`)
} else {
  log(`${RC}: recheck skipped — no applied fixes`)
}

return {
  rc: RC,
  totals: {
    cells: auditCells.length, unique: unique.length, confirmed: confirmed.length,
    split: split.length, dropped: dropped.length,
    mechanicalFixed: mechanical.length, judgmentToNotify: judgment.length,
    recheckConfirmed: recheckConfirmed.length,
  },
  confirmed,
  needsHuman: [...split, ...judgment, ...recheckConfirmed],
  fixes: fixResults,
  coverageFindings: unique.filter((f) => (f.lenses || []).includes('coverage')),
  instrumentation: { perLens, capHits, cellErrors, evidenceChecks, refuteByBatchSize },
}
