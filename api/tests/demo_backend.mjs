// Checks the fake backend of the public tablet demo (#892, B2) in node, with the
// browser's globals stubbed: a request the real page makes through XMLHttpRequest
// or fetch is answered from the visitor's state, never sent anywhere.
// Usage: node api/tests/demo_backend.mjs api/gui/static/demo-backend.js <built demo folder>
// (test_tablet_demo_build.py runs it). Prints "ok" or throws.
import assert from 'node:assert'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const dir = path.resolve(process.argv[3])
const sent = []

// What the browser has: the built folder behind /tablet-demo/, and nothing else.
globalThis.window = globalThis
globalThis.location = { href: 'http://demo.test/tablet-demo/', origin: 'http://demo.test' }
// A page without the board's script: no "#ton-an", a notice at the top, a body that
// takes listeners. What the backend puts into the page is kept in `page`.
const page = { listeners: {}, line: null }
globalThis.document = {
  readyState: 'complete',
  getElementById: (id) => (id === 'demo-note' ? page.line : null),
  querySelector: (sel) => (sel.startsWith('form') ? null : { after: (el) => (page.line = el) }),
  createElement: () => ({ setAttribute() {}, remove() { page.line = null } }),
  addEventListener() {},
  body: { addEventListener: (type, fn) => (page.listeners[type] = fn), prepend() {} },
}
globalThis.fetch = async (input) => {
  const url = new URL(String(input), location.href)
  sent.push(url.pathname)
  if (url.origin !== location.origin) throw new Error('offsite')
  const file = path.join(dir, url.pathname.replace(/^\/tablet-demo\//, ''))
  if (!url.pathname.startsWith('/tablet-demo/') || !fs.existsSync(file)) return new Response('', { status: 404 })
  return new Response(fs.readFileSync(file), { status: 200 })
}
globalThis.XMLHttpRequest = class {}
globalThis.EventSource = class {}

await import(pathToFileURL(path.resolve(process.argv[2])).href)
assert.notEqual(XMLHttpRequest.name, '', 'the backend replaced XMLHttpRequest')

// An XMLHttpRequest the way htmx drives it.
function xhr(method, url, body) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest()
    x.open(method, url, true)
    x.setRequestHeader('HX-Request', 'true')
    x.onload = () => resolve(x)
    x.onerror = () => reject(new Error('lost'))
    x.send(body ?? null)
  })
}
const routes = JSON.parse(fs.readFileSync(path.join(dir, 'routes.json'), 'utf8')).routes
const text = (html) => html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ')
const form = (o) => new URLSearchParams(o).toString()

// --- Header taps answer with the new header, in HTML ---
{
  const r = await xhr('POST', '/gui/kopfzeile/ki-pausieren')
  assert.equal(r.status, 200)
  assert.match(r.getResponseHeader('Content-Type'), /^text\/html/)
  assert.match(r.responseText, /<header id="kopfzeile" class="aus">/)
  assert.match(r.responseText, /href="\/tablet-demo\/anrufe\/"/, 'links lead to the demo pages')
  const back = await xhr('POST', '/gui/kopfzeile/ki-einschalten')
  assert.doesNotMatch(back.responseText, /class="aus"/)
  const wait = await xhr('POST', '/gui/kopfzeile/wartezeit/abholung/15')
  assert.match(text(wait.responseText), /Wartezeit Abholung 35 Min Standard 20 Min|Wartezeit Abholung 30 Min Standard 20 Min/)
  const typed = await xhr('POST', '/gui/kopfzeile/wartezeit/abholung', form({ minuten: '12' }))
  assert.match(text(typed.responseText), /Wartezeit Abholung 12 Min/)
  const refused = await xhr('POST', '/gui/kopfzeile/wartezeit/abholung', form({ minuten: '999' }))
  assert.equal(refused.status, 409, 'a refused value is a 409 with the sentence in the header')
  assert.match(refused.getResponseHeader('content-type'), /text\/html/)
  assert.match(refused.responseText, /Die Wartezeit geht von 10 bis 180 Minuten/)
}

// --- Edge: unknown service, unknown mode, bad step: the real router's 404 ---
for (const [m, u] of [
  ['POST', '/gui/kopfzeile/kueche/an'],
  ['POST', '/gui/kopfzeile/abholung/vielleicht'],
  ['POST', '/gui/kopfzeile/modus/nope'],
  ['POST', '/gui/kopfzeile/wartezeit/abholung/7'],
  ['GET', '/gui/wartezeit/kueche'],
  ['GET', '/gui/anmelden'],
]) {
  const r = await xhr(m, u)
  assert.equal(r.status, 404, u)
  assert.match(r.getResponseHeader('Content-Type'), /json/, `${u}: no HTML to swap into the board`)
}

// --- The wait sheet, the columns and the sheets the build rendered ---
{
  const sheet = await xhr('GET', '/gui/wartezeit/lieferung')
  assert.match(sheet.responseText, /Wartezeit Lieferung/)
  for (const u of ['/gui/fragments/bestellungen', '/gui/fragments/rueckrufe', '/gui/fragments/heute', '/gui/rueckrufe/verlauf']) {
    const r = await xhr('GET', u)
    assert.equal(r.status, 200, u)
    assert.match(r.responseText, /<h2|<section/, u)
  }
  const einstellungen = await xhr('GET', '/gui/einstellungen')
  assert.equal(einstellungen.status, 200)
  assert.match(einstellungen.responseText, /Einstellungen/)
  const call = JSON.parse(fs.readFileSync(path.join(dir, 'captures.json'), 'utf8')).find((c) => c.method === 'POST' && c.body)
  const detail = await xhr('POST', '/gui/anrufe/anruf', form({ anruf: call.body.anruf }))
  assert.equal(detail.status, 200)
  assert.equal(detail.responseText, fs.readFileSync(path.join(dir, call.file), 'utf8'), 'one detail per call')
}

// --- A red card resent: the slip is printed a moment later, with no further tap ---
{
  const col = await xhr('GET', '/gui/fragments/bestellungen')
  const red = /\/gui\/bestellungen\/([^/]+)\/nochmal-senden/.exec(col.responseText)[1]
  const again = await xhr('POST', `/gui/bestellungen/${red}/nochmal-senden`)
  assert.match(text(again.responseText), /Zettel wird gedruckt/)
  await new Promise((r) => setTimeout(r, 2200))
  const later = await xhr('GET', '/gui/fragments/bestellungen')
  assert.doesNotMatch(text(later.responseText), /Zettel wird gedruckt/)
  assert.match(text(later.responseText), /Zettel liegt an der Kasse/)
}

// --- Orders and callbacks through the requests the buttons send ---
{
  const col = await xhr('GET', '/gui/fragments/bestellungen')
  const id = /\/gui\/bestellungen\/([^/]+)\/passt/.exec(col.responseText)[1]
  const passt = await xhr('POST', `/gui/bestellungen/${id}/passt`)
  // The slip of this seed order may already be printed (the resend above printed every pending one).
  assert.match(text(passt.responseText), /Zettel (wird gedruckt|liegt an der Kasse)/)
  const gone = await xhr('POST', '/gui/bestellungen/not-there/passt')
  assert.equal(gone.status, 200, 'an order that is gone: the fresh column')
  const cancelId = /\/gui\/bestellungen\/([^/]+)\/storniert\/([^"]+)"/.exec(col.responseText)
  const cancelled = await xhr('POST', `/gui/bestellungen/${cancelId[1]}/storniert/${cancelId[2]}`)
  assert.match(JSON.parse(cancelled.getResponseHeader('HX-Trigger'))['order-notice'], /^Bestellung .+: Storniert$/)
  const cbs = await xhr('GET', '/gui/fragments/rueckrufe')
  const cb = /\/gui\/rueckrufe\/([^/]+)\/erledigt/.exec(cbs.responseText)[1]
  const done = await xhr('POST', `/gui/rueckrufe/${cb}/erledigt`)
  assert.ok(!done.responseText.includes(cb))
  const history = await xhr('GET', '/gui/rueckrufe/verlauf')
  assert.ok(history.responseText.includes(cb))
  const reopened = await xhr('POST', `/gui/rueckrufe/${cb}/wieder-offen`)
  assert.match(reopened.responseText, /Erledigte R&uuml;ckrufe/)
}

// --- A page without the board's script shows the line of a locked control itself ---
{
  assert.ok(page.listeners['order-notice'], 'the backend listens for the board line')
  const r = await xhr('POST', '/gui/lieferung/neu')
  page.listeners['order-notice'](new CustomEvent('order-notice', { detail: { value: JSON.parse(r.getResponseHeader('HX-Trigger'))['order-notice'] } }))
  assert.match(page.line.textContent, /Inhaber-PIN/)
}

// --- Owner's PIN parts: shown and locked, a line on the board, no swap ---
{
  const r = await xhr('POST', '/gui/lieferung/neu')
  assert.equal(r.status, 200)
  assert.equal(r.getResponseHeader('HX-Reswap'), 'none')
  assert.match(JSON.parse(r.getResponseHeader('HX-Trigger'))['order-notice'], /Inhaber-PIN/)
  const g = await xhr('GET', '/gui/zeiten/woche')
  assert.equal(g.getResponseHeader('HX-Reswap'), 'none')
  // Not built yet (the holiday answers, the floor plan's saves): a line, never an error page.
  const later = await xhr('POST', '/gui/feiertage/x/y')
  assert.equal(later.status, 200)
  assert.equal(later.getResponseHeader('HX-Reswap'), 'none')
}

// --- B3: the sheets answer from the visitor's restaurant, through the requests the buttons send ---
{
  const state = JSON.parse(fs.readFileSync(path.join(dir, 'state.json'), 'utf8'))
  const now = new Date()
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}` // the visitor's day, not the build's
  const dish = state.menu.find((d) => d.number === '302')
  const sheet = await xhr('GET', '/gui/gericht-aus')
  assert.match(sheet.responseText, /Gericht aus/)
  const off = await xhr('POST', `/gui/gericht-aus/${dish.id}/aus`)
  assert.equal(off.status, 200)
  assert.match(off.responseText, /class="dish aus"/)
  assert.equal(JSON.parse(off.getResponseHeader('HX-Trigger'))['gerichte-geaendert'], '')
  const head = await xhr('GET', '/gui/fragments/kopfzeile')
  assert.match(text(head.responseText), /2 heute aus/, 'the tile of the header counts the dishes off')
  const found = await xhr('GET', '/gui/fragments/gericht-aus?q=penne')
  assert.match(found.responseText, /Penne Arrabbiata/)
  assert.doesNotMatch(found.responseText, /Spaghetti/)
  assert.equal((await xhr('POST', `/gui/gericht-aus/${dish.id}/vielleicht`)).status, 404)
  const back = await xhr('POST', `/gui/gericht-aus/${dish.id}/wieder-da`)
  assert.doesNotMatch(back.responseText, /class="dish aus"/)

  const order = state.orders.orders[2].id
  const open = await xhr('GET', `/gui/bestellungen/${order}/korrigieren`)
  assert.match(open.responseText, /Bestellung korrigieren/)
  const stand = /name="state" value="([^"]*)"/.exec(open.responseText)[1].replace(/&#34;/g, '"')
  const tap = await xhr('POST', `/gui/bestellungen/${order}/korrigieren/vorschau`, form({ state: stand, op: 'plus:r:0' }))
  assert.equal(tap.status, 200)
  assert.match(text(tap.responseText), /Menge geändert/)
  const garbled = await xhr('POST', `/gui/bestellungen/${order}/korrigieren/vorschau`, form({ state: 'x', op: 'plus:r:0' }))
  assert.equal(garbled.status, 409)

  const team = await xhr('POST', '/gui/teamnummer', form({ nummer: state.team.choices[0] }))
  assert.match(text(team.responseText), /Gilt bis Betriebsschluss/)
  const checks = await xhr('GET', '/gui/fragments/pruefen')
  assert.match(text(checks.responseText), /Teamnummer heute/)
  const hours = await xhr('GET', `/gui/tageszeiten?datum=${today}`)
  assert.match(hours.responseText, /Öffnungszeit für einen Tag/)

  const res = await xhr('GET', '/gui/reservierung')
  const key = /name="schluessel" value="([^"]+)"/.exec(res.responseText)[1]
  const draft = await xhr('POST', '/gui/reservierung', form({ schluessel: key, datum: today, uhrzeit: '19:00', personen: '2', name: 'Test', tisch: '' }))
  const rid = /reservierung\/([^/]+)\/eintragen/.exec(draft.responseText)[1]
  const entered = await xhr('POST', `/gui/reservierung/${rid}/eintragen`)
  assert.match(JSON.parse(entered.getResponseHeader('HX-Trigger'))['reservierung-eingetragen'], /Test/)

  const day = /<option value="([^"]+)" selected>/.exec(fs.readFileSync(path.join(dir, 'anrufe/index.html'), 'utf8'))[1]
  const list = await xhr('GET', `/gui/anrufe?tag=${day}&ergebnis=completed&anliegen=`)
  assert.match(list.responseText, /<option value="completed" selected>/)
  const menu = await xhr('GET', '/gui/menue?q=pizza&hinweis=')
  assert.equal(menu.getResponseHeader('HX-Push-Url'), 'false', 'htmx must not push an address that is no file of the demo')
  assert.match(text(menu.responseText), /5 von 25 Gerichten/)
}

// --- fetch: "/gui/" is answered here, the rest is the browser's ---
{
  const before = sent.length
  const probe = await fetch('/gui/fragments/heute', { headers: { 'HX-Request': 'true' } })
  assert.equal(probe.status, 200)
  assert.match(await probe.text(), /<h2>Heute/)
  const plan = await fetch('/gui/raumplan/plan')
  assert.equal((await plan.json()).areas.length, 2)
  const own = await fetch('/static/app.js')
  assert.equal(own.status, 404, 'a file outside the demo folder is the stub fetch')
  assert.ok(sent.slice(before).includes('/static/app.js'))
  assert.ok(!sent.slice(before).some((p) => p.startsWith('/gui/')), 'nothing of the view leaves the browser')
}

// --- The event stream opens and can be closed ---
{
  const es = new EventSource('/gui/events')
  let opened = false
  es.onopen = () => {
    opened = true
  }
  const orders = []
  es.addEventListener('orders', () => orders.push(1))
  await new Promise((r) => setTimeout(r, 20))
  assert.ok(opened)
  assert.equal(es.readyState, 1)
  es.close()
  assert.equal(es.readyState, EventSource.CLOSED)
}

// --- Every route of the manifest is answered: nothing falls through to a missing page ---
for (const route of routes) {
  const url = route.path
    .replace('{service}', 'abholung')
    .replace('{state}', 'an')
    .replace('{step}', '15')
    .replace('{mode}', 'primary')
    .replace(/\{[^}]+\}/g, 'x')
  const r = await xhr(route.method, url)
  if (route.answer === 'absent') assert.equal(r.status, 404, `${route.method} ${url}`)
  else if (route.answer === 'locked') assert.equal(r.getResponseHeader('HX-Reswap'), 'none', `${route.method} ${url}`)
  else assert.ok(r.status < 500, `${route.method} ${url} -> ${r.status}`)
}
assert.ok(!sent.some((p) => p.startsWith('/gui/')), 'no request of the view was sent')

console.log('ok')
