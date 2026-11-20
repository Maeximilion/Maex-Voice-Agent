// Checks the board of the public tablet demo (#892, B2) without a browser: the
// markup demo-board.js renders from the data the build recorded is the markup the
// real view rendered, and the visitor's taps do what the real server does.
// Usage: node api/tests/demo_board.mjs api/gui/static/demo-board.js <built demo folder>
// (test_tablet_demo_build.py runs it). Prints "ok" or throws.
import assert from 'node:assert'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const B = await import(pathToFileURL(path.resolve(process.argv[2])).href)
const dir = path.resolve(process.argv[3])
const read = (f) => fs.readFileSync(path.join(dir, f), 'utf8')
const state = () => JSON.parse(read('state.json'))

// Whitespace between tags and runs of it carry no meaning in HTML; nothing else is
// loosened: a different class, text or attribute fails.
const flat = (s) =>
  s
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\s+/g, ' ')
    .replace(/> </g, '><')
    .replace(/ >/g, '>')
    .replace(/(<li[^>]*>) /g, '$1')
    .replace(/ <\/li>/g, '</li>')
    .trim()
const same = (made, file, what) => assert.equal(flat(made), flat(read(file)), `${what} differs from ${file}`)

const rest = () => new B.Restaurant(state())
// The pages the build turned into files: links to them lead there.
const pages = JSON.parse(read('routes.json')).routes.filter((r) => r.answer === 'page').map((r) => r.path)
const linked = (html) => B.demoLinks(html, pages, '/tablet-demo')

// --- The start state renders to the real fragments ---
{
  const r = rest()
  same(linked(r.kopfzeile()), 'fragments/kopfzeile/index.html', 'header')
  same(r.bestellungen(), 'fragments/bestellungen/index.html', 'orders')
  same(r.rueckrufe(), 'fragments/rueckrufe/index.html', 'callbacks')
  same(r.heute(), 'fragments/heute/index.html', 'today')
  same(r.verlauf(), 'rueckrufe/verlauf/index.html', 'history')
  same(r.waitSheet('abholung'), 'wartezeit/abholung/index.html', 'wait sheet')
  same(r.waitSheet('lieferung'), 'wartezeit/lieferung/index.html', 'wait sheet')
  assert.equal(r.waitSheet('kueche'), null)
}

const text = (html) => html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ')
const ids = (html) => [...html.matchAll(/data-id="([^"]+)"/g)].map((m) => m[1])
const wait = (r, key) => r.header.waits.find((w) => w[0] === key)

// --- Header: modes ---
{
  const r = rest()
  assert.equal(r.setMode('nope'), null)
  assert.equal(r.setMode('shadow').status, 200)
  assert.match(r.kopfzeile(), /tone-warn/)
  assert.match(text(r.kopfzeile()), /KI hört mit/)
  // Stepping back is one tap, stepping up asks first (hx-confirm on the "up" buttons).
  assert.equal((r.kopfzeile().match(/hx-confirm="Auf/g) ?? []).length, 2)
  r.setMode('primary')
  assert.equal((r.kopfzeile().match(/hx-confirm="Auf/g) ?? []).length, 0)
}

// --- Header: emergency stop and back, the mode before the pause returns ---
{
  const r = rest()
  r.setMode('overflow')
  r.pause()
  const paused = r.kopfzeile()
  assert.match(paused, /class="aus"/)
  assert.match(text(paused), /KI ist aus/)
  assert.match(paused, /KI einschalten/)
  assert.doesNotMatch(paused, /Modus der KI/)
  // Never switched while paused: a refusal with the sentence in the header.
  assert.equal(r.setMode('primary').status, 409)
  assert.match(r.kopfzeile(), /Umschalten hat nicht geklappt/)
  r.resume()
  assert.match(text(r.kopfzeile()), /KI springt ein/)
  assert.doesNotMatch(r.kopfzeile(), /Umschalten hat nicht geklappt/)
}

// --- Header: channels; all off turns the green tick yellow ---
{
  const r = rest()
  assert.equal(r.setService('kueche', 'an'), null)
  assert.equal(r.setService('abholung', 'vielleicht'), null)
  r.setService('abholung', 'aus')
  assert.match(r.kopfzeile(), /Abholung aus/)
  r.setService('lieferung', 'aus')
  r.setService('reservierung', 'aus')
  assert.match(r.kopfzeile(), /status tone-warn/)
  r.setService('reservierung', 'an')
  assert.match(r.kopfzeile(), /status tone-ok/)
}

// --- Header: wait times step on the grid and stop at the limits ---
{
  const r = rest()
  assert.equal(wait(r, 'abholung')[2], 20)
  r.stepWait('abholung', 15)
  assert.equal(wait(r, 'abholung')[2], 30)
  assert.match(text(r.kopfzeile()), /Wartezeit Abholung 30 Min Standard 20 Min/)
  r.stepWait('abholung', -15)
  r.stepWait('abholung', -15)
  assert.equal(wait(r, 'abholung')[2], 10, 'the lower limit cuts the step short')
  assert.match(r.kopfzeile(), /Minuten weniger" disabled/)
  r.stepWait('abholung', 15)
  assert.equal(wait(r, 'abholung')[2], 15, 'back on the grid, not 25')
  r.setWait('abholung', '175')
  r.stepWait('abholung', 15)
  assert.equal(wait(r, 'abholung')[2], 180)
  assert.match(r.kopfzeile(), /Minuten mehr" disabled/)
  r.stepWait('abholung', 15)
  assert.equal(wait(r, 'abholung')[2], 180)
  assert.equal(r.stepWait('abholung', 10), null, 'only the two steps of the buttons')
  assert.equal(r.stepWait('kueche', 15), null)
  assert.equal(wait(r, 'lieferung')[2], 45, 'the other service stays')
}

// --- Header: typed wait, standard, reset, refusals ---
{
  const r = rest()
  r.setWait('lieferung', ' 50 ')
  assert.equal(wait(r, 'lieferung')[2], 50)
  for (const bad of ['9', '181', 'abc', '', '12345', '-5', '1e2']) {
    assert.equal(r.setWait('lieferung', bad).status, 409, bad)
    assert.match(r.kopfzeile(), /Die Wartezeit geht von 10 bis 180 Minuten/)
    assert.equal(wait(r, 'lieferung')[2], 50, `${bad} changes nothing`)
  }
  r.setStandard('lieferung', '60')
  assert.equal(wait(r, 'lieferung')[3], 60)
  assert.equal(wait(r, 'lieferung')[2], 50, "today's wait stays until the day ends")
  assert.match(r.waitSheet('lieferung'), /Zurück auf Standard \(60 Min\)/)
  r.resetWait('lieferung')
  assert.equal(wait(r, 'lieferung')[2], 60)
  assert.doesNotMatch(r.waitSheet('lieferung'), /Zurück auf Standard/)
}

// --- Orders: Passt, the slip, the register mark ---
{
  const r = rest()
  assert.equal(ids(r.bestellungen()).length, 3)
  const waiting = [...r.bestellungen().matchAll(/hx-post="\/gui\/bestellungen\/([^/]+)\/passt"/g)].map((m) => m[1])
  assert.ok(waiting.length >= 1, 'the start state has an order waiting for Passt')
  const id = waiting[0]
  assert.equal(r.approve(id).status, 200)
  assert.match(text(r.bestellungen()), /Zettel wird gedruckt/)
  assert.equal(r.approve(id).status, 200, 'twice is the same as once')
  assert.deepEqual(r.pendingSlips(), [id])
  assert.equal(r.slipPrinted(id), true)
  assert.equal(r.slipPrinted(id), false)
  const html = r.bestellungen()
  assert.match(html, new RegExp(`data-id="${id}"[^>]*><details>`), 'only the register mark is left: the compact card')
  const mark = new RegExp(`/gui/bestellungen/${id}/in-kasse/([^"]+)"`).exec(html)
  // A card older than a change cannot mark: the refusal sits above the cards, once.
  r.markEntered(id, 'old')
  assert.match(r.bestellungen(), /Die Bestellung wurde inzwischen geändert/)
  assert.doesNotMatch(r.bestellungen(), /Die Bestellung wurde inzwischen geändert/, 'shown once')
  assert.ok(ids(r.bestellungen()).includes(id))
  r.markEntered(id, mark[1])
  assert.ok(!ids(r.bestellungen()).includes(id), 'marked: the card leaves the column')
  assert.equal(r.markEntered('gone', 'x').status, 200, 'an order that is gone is no error')
  assert.equal(r.approve('gone').status, 200)
}

// --- Orders: cancel names the order, resend only for red cards ---
{
  const r = rest()
  const first = ids(r.bestellungen())[0]
  const v = new RegExp(`/gui/bestellungen/${first}/storniert/([^"]+)"`).exec(r.bestellungen())
  assert.equal(r.cancel(first, 'old').status, 200)
  assert.ok(ids(r.bestellungen()).includes(first), 'a stale cancel changes nothing')
  const tap = r.cancel(first, v[1])
  assert.match(tap.notice, /^Bestellung .+: Storniert$/)
  assert.ok(!ids(r.bestellungen()).includes(first))
  const before = r.bestellungen()
  r.resend(ids(before)[0])
  assert.equal(r.bestellungen(), before, 'nothing to resend on a card that is not red')
}

// --- Callbacks: done, history, reopen ---
{
  const r = rest()
  const open = ids(r.rueckrufe())
  assert.equal(open.length, 2)
  r.callbackDone(open[0])
  assert.deepEqual(ids(r.rueckrufe()), [open[1]])
  assert.match(text(r.verlauf()), /Erledigt 12:00/)
  assert.equal(ids(r.verlauf()).length, 2)
  r.callbackDone('gone')
  assert.equal(ids(r.rueckrufe()).length, 1)
  r.callbackReopen(open[0])
  assert.equal(ids(r.rueckrufe()).length, 2)
  r.callbackDone(open[0])
  r.callbackDone(open[1])
  assert.match(text(r.rueckrufe()), /Keine R&uuml;ckrufe offen/)
  r.callbackReopen('gone')
}

// --- Example call: what it does depends on the board ---
{
  const r = rest()
  const orders = ids(r.bestellungen()).length
  const today = ids(r.heute()).length
  assert.deepEqual(r.exampleCall(), { order: true, reservation: true, callback: false })
  assert.equal(ids(r.bestellungen()).length, orders + 1)
  assert.equal(ids(r.heute()).length, today + 1)
  const card = text(r.bestellungen())
  assert.match(card, /Passt, Zettel an die Kasse/)
  assert.match(card, /\d+,\d\d €/)
  assert.ok(!card.includes('Pizza Vier Käse'), 'never the dish marked off')
  // The new order takes the whole way through Passt.
  r.approve(ids(r.bestellungen()).find((x) => x.startsWith('demo-order')))
  assert.match(text(r.bestellungen()), /Zettel wird gedruckt/)
}
{
  const r = rest()
  r.pause()
  const orders = ids(r.bestellungen()).length
  assert.deepEqual(r.exampleCall(), { order: false, reservation: false, callback: true })
  assert.equal(ids(r.bestellungen()).length, orders, 'a paused agent takes no order')
  assert.match(text(r.rueckrufe()), /Will Mitarbeiter sprechen/)
  assert.match(text(r.rueckrufe()), /Die KI war aus/)
}
{
  const r = rest()
  r.setMode('shadow')
  assert.equal(r.exampleCall().callback, true, 'a listening agent answers nothing')
}
{
  const r = rest()
  r.setService('abholung', 'aus')
  assert.deepEqual(r.exampleCall(), { order: false, reservation: true, callback: false })
  r.setService('reservierung', 'aus')
  assert.equal(r.exampleCall().callback, true)
  assert.match(text(r.rueckrufe()), /Abholung und Reservierung sind aus/)
}
{
  // Many calls: ids stay distinct.
  const r = rest()
  for (let i = 0; i < 12; i++) r.exampleCall()
  const all = ids(r.bestellungen())
  assert.equal(new Set(all).size, all.length)
}

// --- A visitor's restaurant is their own: two instances do not share state ---
{
  const a = rest()
  const b = rest()
  a.pause()
  a.callbackDone(ids(a.rueckrufe())[0])
  same(linked(b.kopfzeile()), 'fragments/kopfzeile/index.html', 'the second restaurant')
  same(b.rueckrufe(), 'fragments/rueckrufe/index.html', 'the second restaurant')
}

// --- Markup helpers ---
assert.equal(B.esc(`<a href="x">&'</a>`), '&lt;a href=&#34;x&#34;&gt;&amp;&#39;&lt;/a&gt;')
assert.equal(B.euro(1380), '13,80 €')
assert.equal(B.euro(5), '0,05 €')
assert.equal(B.clockPlus('23:50', 20), '00:10')
assert.equal(linked('<a href="/gui/anrufe">x</a><a href="/gui/vorlauf">y</a><a href="/gui">z</a>'),
  '<a href="/tablet-demo/anrufe/">x</a><a href="/gui/vorlauf">y</a><a href="/tablet-demo/">z</a>')

console.log('ok')
