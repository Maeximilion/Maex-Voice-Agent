// Runs the saved workflows against stubbed agents: a partly failed agent must end incomplete.
// Usage: node api/tests/workflow_stubs.mjs .claude/workflows (test_workflows.py runs it).
import fs from 'node:fs'
import assert from 'node:assert'
const dir = process.argv[2]
const load = name => fs.readFileSync(`${dir}/${name}.js`, 'utf8').replace('export const meta', 'const meta')
const run = (name, args, agent) => new Function('args', 'agent', 'parallel', 'phase',
  `return (async () => { ${load(name)} })()`)(args, agent, fns => Promise.all(fns.map(f => f())), () => {})

// gate-review: skeptic answers only id 0 (with a reworded title)
let verdictInput
const lensResult = { findings: [
  { severity: 'P1', title: 'a', where: 'x', scenario: 's' },
  { severity: 'P0', title: 'b', where: 'y', scenario: 's' },
  { severity: 'P2', title: 'c', where: 'z', scenario: 's' }], covered: '', complete: true }
const gate = skeptic => run('gate-review', 'G0', async (p, o) => {
  if (o.label === 'criteria') return 'crit'
  if (o.label.startsWith('lens:')) return o.label === 'lens:hard-rules' ? lensResult : { findings: [], covered: '', complete: true }
  if (o.label.startsWith('verify:')) return skeptic
  verdictInput = p; return 'report'
})
await gate({ verdicts: [{ id: 0, title: 'A reworded', status: 'refuted', reason: 'r' }] })
let v = JSON.parse(verdictInput.split('LENSES:\n')[1])[0].verdicts
assert.deepEqual(v.map(x => [x.id, x.status]), [[0, 'refuted'], [1, 'unverified']])
assert.match(v[1].reason, /no verdict/)
await gate(null)
v = JSON.parse(verdictInput.split('LENSES:\n')[1])[0].verdicts
assert.deepEqual(v.map(x => x.status), ['unverified', 'unverified'])
assert.match(v[0].reason, /returned no result/)

// review-load-audit
const rows = complete => ({ prs: [{ number: 400 }], aggregates: '', finding_quality: '', p2_outside_scope: '', complete, missing: complete ? '' : '#401: 502' })
const audit = (m, recount) => run('review-load-audit', {}, async (p, o) =>
  o.label === 'measure' ? m : o.label === 'recount' ? recount : 'compared')
assert.match(await audit(rows(false), 'ok'), /incomplete.*#401: 502/)
assert.match(await audit(rows(true), null), /Recount failed.*incomplete/)
assert.equal(await audit(rows(true), 'ok'), 'compared')
assert.match(await audit(null, 'ok'), /Measurement failed/)
console.log('ok')
