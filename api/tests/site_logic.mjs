// Checks the logic of the public site (#892) without a browser: how far an example
// call has been typed and the checks of the inquiry form.
// Usage: node api/tests/site_logic.mjs api/gui/static/site-logic.js
// (test_site_logic.py runs it). Prints "ok" or throws.
import assert from 'node:assert'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const L = await import(pathToFileURL(path.resolve(process.argv[2])).href)

// Four sentences, guest first. Lengths 10, 20, 5, 8.
const lengths = [10, 20, 5, 8]
const state = (n) => L.callState(lengths, n)

// Before the first character nobody speaks and the call is not confirmed.
assert.deepEqual(state(0).shown, [0, 0, 0, 0])
assert.equal(state(0).active, -1)
assert.equal(state(0).loud, false)
assert.equal(state(0).status, 'listening')

// The guest is typing: the waves swing, the agent is still silent.
assert.equal(state(5).active, 0)
assert.equal(state(5).loud, true)
assert.equal(state(5).status, 'listening')

// The pause after a sentence is quiet: the waves rest.
assert.equal(state(10).active, -1)
assert.equal(state(10).loud, false)

// The agent is typing (second sentence starts at 10 + PAUSE).
const agent = 10 + L.PAUSE + 7
assert.equal(state(agent).active, 1)
assert.equal(state(agent).status, 'speaking')
assert.equal(state(agent).shown[1], 7)

// Confirmed only once the guest's yes (the third sentence) is complete, not before.
const yesStart = 10 + L.PAUSE + 20 + L.PAUSE
assert.notEqual(state(yesStart + 4).status, 'confirmed')
assert.equal(state(yesStart + 5).status, 'confirmed')
assert.equal(state(L.total(lengths) + 5).status, 'confirmed')

// Never more than a sentence has, never less than nothing.
for (let n = -5; n < L.total(lengths) + 80; n++) {
  state(n).shown.forEach((count, i) => assert.ok(count >= 0 && count <= lengths[i], `n ${n}`))
}
// A finished call shows every sentence whole and nobody speaks.
assert.deepEqual(state(L.total(lengths)).shown, lengths)
assert.equal(state(L.total(lengths)).loud, false)

// A call plays once: finished exactly when everything is typed, never before.
assert.equal(L.finished(lengths, L.total(lengths) - 1), false)
assert.equal(L.finished(lengths, L.total(lengths)), true)
assert.equal(L.finished(lengths, 0), false)

// The wave swing is a matrix in SVG units around the middle line (y 120): full
// swing leaves it as it is, a flat swing never reaches 0 (no inverse, no stroke).
assert.equal(L.swingMatrix(1), 'matrix(1,0,0,1.000,0,0.00)')
assert.equal(L.swingMatrix(0.5), 'matrix(1,0,0,0.500,0,60.00)')
assert.equal(L.swingMatrix(0), 'matrix(1,0,0,0.020,0,117.60)')
assert.equal(L.swingMatrix(7), 'matrix(1,0,0,1.000,0,0.00)')

// The inquiry checks.
const good = { kind: 'Restaurant', business: 'Zum Test', name: 'Erika Muster', mail: 'erika@example.com', phone: '', text: '' }
assert.deepEqual(L.check(good), {})
assert.deepEqual(L.check({ ...good, phone: '+49 721 555-123' }), {})
assert.deepEqual(Object.keys(L.check({ ...good, business: '  ', name: '', mail: '' })).sort(), ['business', 'mail', 'name'])
assert.ok(L.check({ ...good, mail: 'erika@example' }).mail)
assert.ok(L.check({ ...good, mail: 'erika example.com' }).mail)
assert.ok(L.check({ ...good, kind: 'Bank' }).kind)
assert.ok(L.check({ ...good, phone: 'abc' }).phone)
assert.ok(L.check({ ...good, phone: '12345' }).phone)
assert.ok(L.check({ ...good, text: 'x'.repeat(L.LIMITS.text + 1) }).text)
assert.deepEqual(L.check({ ...good, text: 'x'.repeat(L.LIMITS.text) }), {})
assert.ok(L.check({ ...good, business: 'x'.repeat(L.LIMITS.business + 1) }).business)

console.log('ok')

// The glow eases toward the pointer without overshooting.
assert.equal(L.ease(0, 100, 0.25), 25)
assert.equal(L.ease(100, 100, 0.5), 100)

// The voice swings 0..1 for any time and layer, has quiet moments between phrases
// and loud ones inside them, and the three layers are out of step.
let low = 0, high = 0
for (let t = 0; t < 30; t += 0.02) {
  for (const phase of [0, 0.23, 0.51]) {
    const v = L.voice(t, phase)
    assert.ok(v >= 0 && v <= 1, `voice ${v} at ${t}`)
    if (v < 0.2) low++
    if (v > 0.8) high++
  }
}
assert.ok(low > 100 && high > 100, `speech-like rhythm: low ${low}, high ${high}`)
assert.notEqual(L.voice(3.1, 0), L.voice(3.1, 0.23))
