export const meta = {
  name: 'gate-review',
  description: 'Read-only gate review: criteria with evidence, four lenses, each P0/P1 finding checked by a skeptic',
  whenToUse: 'Before /gate, when Maxi asks for a gate review. Pass the gate, for example "G0". Changes nothing.',
  phases: [
    { title: 'Review', detail: 'gate criteria and four lenses in parallel' },
    { title: 'Verify', detail: 'one skeptic per lens tries to refute its P0 and P1 findings' },
    { title: 'Verdict', detail: 'go or no-go per criterion, blockers' },
  ],
}

// args: "G0" or { gate: "G0" }.
const GATE = (typeof args === 'string' ? args : args && args.gate) || 'G0'
const READ_ONLY = 'Read-only: change no file, branch, issue or PR, start no container and no eval run that uses the GPU.'

const FINDINGS = {
  type: 'object',
  properties: {
    findings: { type: 'array', items: { type: 'object', properties: {
      severity: { type: 'string', enum: ['P0', 'P1', 'P2'] },
      title: { type: 'string' }, where: { type: 'string', description: 'file:line or doc section' },
      scenario: { type: 'string', description: 'the concrete failing input or sequence' },
    }, required: ['severity', 'title', 'where', 'scenario'] } },
    covered: { type: 'string', description: 'what was checked and what was not' },
  },
  required: ['findings', 'covered'],
}
const VERDICTS = {
  type: 'object',
  properties: { verdicts: { type: 'array', items: { type: 'object', properties: {
    title: { type: 'string' }, real: { type: 'boolean' }, reason: { type: 'string' },
  }, required: ['title', 'real', 'reason'] } } },
  required: ['verdicts'],
}

// Severities follow AGENTS.md "Code Review Rules"; a finding needs a concrete failing scenario.
const LENSES = [
  { key: 'hard-rules', prompt: `Check the code on main against hard rules 1, 2, 3 and 5 of CLAUDE.md section 2: prices, zones, hours, availability and allergens only from the database; no order item without a definitive menu_item_id; draft leaves only through confirm after an explicit yes; every failure branch in the call path ends with the team or a callback. Look in api/domain, api/agent, api/tools, api/telephony and prompts/.` },
  { key: 'security', model: 'sonnet', prompt: `Check security as AGENTS.md "Security and secrets" and "Containers and deployment" describe it, and run bash scripts/audit_due.sh --gate to see whether the audit of docs/09_OPERATIONS_LEGAL.md section 5 is due. That audit is a gate requirement only from G1 on (.claude/commands/gate.md step 1b): for G0 report its verdict as information, never as a finding. Also list open code-scanning and Dependabot alerts with python3 scripts/alert_fixes.py. A real pilot name, number or credential anywhere is P0.` },
  { key: 'evals', model: 'sonnet', prompt: `Check hard rule 4 for this gate: which eval and measurement numbers exist (docs/metrics/, docs/08_EVALS.md, docs/20_VOICE_LAYER.md section 11), whether they were taken on the current main, and whether they meet the thresholds the gate names. A gate criterion that rests on a number without a recorded run is P1.` },
  { key: 'docs', model: 'sonnet', prompt: `Check that docs/01_STATUS.md, docs/07_WORKPACKAGES.md, README.md and CHANGELOG.md agree on what is done, open and blocked for this gate (docs/15_README_STRATEGY.md "Gate workflow"). A task marked done whose code or test is missing on main is P1.` },
]

phase('Review')
const criteria = () => agent(`List the criteria of gate ${GATE} from docs/00_PCF.md section 5 and the open decisions and blockers in docs/01_STATUS.md for it. For each criterion give the evidence on main (file, report, decision ID) or say that it is missing. ${READ_ONLY}`, { label: 'criteria', phase: 'Review', model: 'sonnet' })

const lens = l => agent(`Gate ${GATE} review, lens "${l.key}". ${l.prompt} Report only findings with a concrete scenario. ${READ_ONLY}`, { label: `lens:${l.key}`, phase: 'Review', schema: FINDINGS, ...(l.model ? { model: l.model } : {}) })
  .then(r => {
    if (!r) return null
    const serious = r.findings.filter(f => f.severity !== 'P2')
    if (!serious.length) return { lens: l.key, ...r, verdicts: [] }
    return agent(`Try to refute each of these gate ${GATE} findings against the code and docs on main. A finding stays real only if its scenario actually happens; default to real=false when you cannot reproduce it from the code. ${READ_ONLY}

FINDINGS:
${JSON.stringify(serious)}`, { label: `verify:${l.key}`, phase: 'Verify', schema: VERDICTS })
      // Without a skeptic's answer a serious finding stays a blocker, marked unverified.
      .then(v => ({ lens: l.key, ...r, verdicts: v ? v.verdicts
        : serious.map(f => ({ title: f.title, real: true, reason: 'unverified: the skeptic returned no result' })) }))
  })

const [crit, ...lenses] = await parallel([criteria, ...LENSES.map(l => () => lens(l))])
const missing = LENSES.filter((l, i) => !lenses[i]).map(l => l.key)
if (missing.length) log(`Lenses without a result: ${missing.join(', ')}`)

phase('Verdict')
return await agent(`Write the gate ${GATE} review in German for Maxi.
1. Per criterion: go, no-go or open, with its evidence.
2. Blockers: every P0 and P1 a skeptic kept as real or left unverified (say which), with where and scenario. Findings a skeptic refuted are listed in one line each with the reason, not as blockers.
3. P2 findings in one short list.
4. What no lens covered${missing.length ? ` (lenses without a result: ${missing.join(', ')})` : ''}.
5. Recommendation: run /gate ${GATE} now, or what has to happen first. The decision is Maxi's.

CRITERIA:
${crit || 'not available'}

LENSES:
${JSON.stringify(lenses.filter(Boolean))}`, { label: 'verdict', phase: 'Verdict' })
