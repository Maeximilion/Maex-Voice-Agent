// Checks the sheets of the public tablet demo (#892, B3) without a browser: the markup
// demo-sheets.js renders from the data the build recorded is the markup the real view
// rendered, and the visitor's taps do what the real server does.
// Usage: node api/tests/demo_sheets.mjs api/gui/static/demo-board.js <built demo folder>
// (test_tablet_demo_build.py runs it; demo-sheets.js sits beside demo-board.js).
// Prints "ok" or throws.
import assert from 'node:assert'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const board = path.resolve(process.argv[2])
const B = await import(pathToFileURL(board).href)
const S = await import(pathToFileURL(path.join(path.dirname(board), 'demo-sheets.js')).href)
const dir = path.resolve(process.argv[3])
const read = (f) => fs.readFileSync(path.join(dir, f), 'utf8')
const state = () => JSON.parse(read('state.json'))

// Whitespace between tags carries no meaning in HTML; nothing else is loosened.
const flat = (s) =>
  s
    .replace(/\s+/g, ' ')
    .replace(/> </g, '><')
    .replace(/ >/g, '>')
    .replace(/(<li[^>]*>) /g, '$1')
    .replace(/ <\/li>/g, '</li>')
    .replace(/(<section[^>]*>) /g, '$1')
    .replace(/ <\/section>/g, '</section>')
    .trim()
// What differs by design: keys and edit ids the server draws at random, the states of
// the correction that carry the order's lines.
const plain = (s) =>
  flat(s)
    .replace(/name="schluessel" value="[^"]*"/g, 'name="schluessel" value="K"')
    .replace(/(id="korrektur-stand" name="state" value=")[^"]*"/g, '$1S"')
    .replace(/(data-order|korrigieren)\/?"?[0-9a-f-]{36}/g, '$1')
    .replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g, 'ID')
const same = (made, file, what) => assert.equal(plain(made), plain(read(file)), `${what} differs from ${file}`)
const text = (html) => html.replace(/<[^>]*>/g, ' ').replace(/&times;/g, '×').replace(/&boxur;/g, '└').replace(/\s+/g, ' ')
const form = (o) => new URLSearchParams(o)

const fresh = () => {
  const data = state()
  const r = new B.Restaurant(data)
  return { r, s: new S.Sheets(data, r, data.hours.today), data }
}
const captures = JSON.parse(read('captures.json'))

// --- The start state renders to the real sheets ---
{
  const { r, s } = fresh()
  same(s.dishSheet(), 'gericht-aus/index.html', 'dish sheet')
  same(s.dishSearch(''), 'fragments/gericht-aus/index.html', 'dish list')
  same(s.teamOpen().html, 'teamnummer/index.html', 'team number sheet')
  same(s.hoursOpen('').html, 'tageszeiten/index.html', 'hours sheet')
  same(s.entryOpen().html, 'reservierung/index.html', 'reservation form')
  same(s.checkCards(), 'fragments/pruefen/index.html', 'check cards')
  for (const c of captures.filter((x) => x.method === 'GET' && /\/korrigieren$/.test(x.url))) {
    const id = /bestellungen\/([^/]+)\/korrigieren/.exec(c.url)[1]
    same(s.correctionOpen(id).html, c.file, 'correction sheet')
  }
}

// --- Gericht aus: switch, search, the count on the tile ---
{
  const { r, s, data } = fresh()
  const dish = data.menu.find((d) => d.number === '301')
  assert.equal(r.header.sold_out_count, 1)
  const off = s.dishSwitch(dish.id, 'aus')
  assert.equal(off.status, 200)
  assert.deepEqual(Object.keys(off.trigger), ['gerichte-geaendert'])
  assert.match(off.html, /class="dish aus"/)
  assert.match(off.html, /Wieder da/)
  assert.equal(r.header.sold_out_count, 2)
  assert.match(text(r.kopfzeile()), /2 heute aus/)
  // The list now has a group of its own for what is off, first.
  assert.match(s.dishSearch(''), /<h3 class="dish-cat">Heute aus<\/h3>/)
  const back = s.dishSwitch(dish.id, 'wieder-da')
  assert.doesNotMatch(back.html, /class="dish aus"/)
  assert.equal(r.header.sold_out_count, 1)
  // Edge: a state that is none, a dish that is gone.
  assert.equal(s.dishSwitch(dish.id, 'vielleicht'), null)
  const gone = s.dishSwitch('not-a-dish', 'aus')
  assert.equal(gone.status, 200)
  assert.equal(gone.html, '', 'a dish that is gone: its row goes')
  assert.equal(r.header.sold_out_count, 1)
  // Search: a number finds the dish ("7" would find 07), a name part finds it, case and umlauts fold.
  assert.match(s.dishSearch('301'), /Spaghetti Tomate/)
  assert.doesNotMatch(s.dishSearch('301'), /Penne/)
  assert.match(s.dishSearch('penne'), /Penne Arrabbiata/)
  assert.match(s.dishSearch('KAESE'.replace('AE', 'ä')), /Vier Käse/)
  assert.match(s.dishSearch('zzz'), /Kein Gericht gefunden/)
  assert.match(s.dishSearch('  ​ '), /<details/, 'a search of nothing visible is no search')
  assert.match(s.dishSearch('301'), /<details[^>]* open/, 'a search opens the groups')
  // A dish that is off is never taken into the example call.
  for (const d of data.menu) r.setOff(d.number, d.number !== '301' && d.number !== '401')
  r.setMode('primary')
  for (let i = 0; i < 6; i++) r.exampleCall()
  const made = [...r.bestellungen().matchAll(/<span class="nr">(\d+)<\/span>/g)].map((m) => m[1])
  assert.ok(made.includes('301') || made.includes('401'))
}

// --- Korrigieren: quantity, remove, swap, add, the sum, the save ---
{
  const { r, s } = fresh()
  const cards = state().orders.orders
  const two = cards.find((c) => c.lines.some((l) => l.number === '201'))
  const note = cards.find((c) => c.lines.some((l) => l.note))
  const tap = (id, html, op, extra = {}) =>
    s.correctionTap(id, form({ state: /name="state" value="([^"]*)"/.exec(html)[1].replace(/&#34;/g, '"'), op, ...extra }))
  let html = s.correctionOpen(two.id).html
  assert.match(text(html), /Noch nichts geändert/)
  assert.match(html, /<button type="button" class="primary"[^>]*>\s*falsche Menge[^<]*<\/button>/s)
  assert.match(html, /disabled>falsche Menge/, 'nothing changed: no reason can be saved')
  // One less: the price follows, money is the cent figure of the card.
  let t = tap(two.id, html, 'minus:r:0')
  assert.equal(t.status, 200)
  assert.match(text(t.html), /1× 201 Pizza Margherita.* 11,40 €/)
  assert.match(text(t.html), /Summe 17,30 € vorher 28,70 €/)
  assert.match(text(t.html), /Menge geändert/)
  assert.doesNotMatch(t.html, /disabled>falsche Menge/)
  // Swap: the field of the line, a number that is none, a dish that is.
  t = tap(two.id, t.html, 'swap:1')
  assert.match(t.html, /Tauschen gegen Nummer/)
  assert.match(t.html, /Menge und Hinweis bleiben/)
  const unknown = tap(two.id, t.html, 'number', { number: '999' })
  assert.match(text(unknown.html), /Nummer 999 gibt es nicht auf der Karte/)
  t = tap(two.id, t.html, 'number', { number: '502' })
  assert.match(text(t.html), /getauscht/)
  assert.match(text(t.html), /1× 502 Panna Cotta.* 5,50 €/)
  // A dish with a required choice is not ready until one is picked.
  t = tap(two.id, t.html, 'number', { number: '201' })
  assert.match(text(t.html), /Bei 201 Pizza Margherita fehlt noch: Größe\./)
  assert.match(t.html, /disabled>falsche Menge/)
  t = tap(two.id, t.html, 'opt:a:0:0:1')
  assert.match(text(t.html), /Gewählt: Groß/)
  assert.match(text(t.html), /Summe 28,30 €/)
  // Edge: a line with a note cannot go to zero by "−", only by "Entfernen", which asks first.
  let n = s.correctionOpen(note.id).html
  const nl = note.lines.findIndex((l) => l.note)
  const down = tap(note.id, n, `minus:r:${nl}`)
  assert.match(text(down.html), new RegExp(`${note.lines[nl].quantity}× ${note.lines[nl].number}`))
  assert.match(n, /hx-confirm="Der Hinweis fällt mit weg: ohne Zitrone/)
  // Edge: everything removed is a cancellation, not a correction.
  let all = n
  note.lines.forEach((l, i) => {
    if (l.note) all = tap(note.id, all, `remove:${i}`).html
    else for (let k = 0; k < l.quantity; k++) all = tap(note.id, all, `minus:r:${i}`).html
  })
  assert.match(text(all), /Mindestens ein Gericht muss bleiben/)
  assert.match(all, /disabled>falsche Menge/)
  // Save: a reason is needed, then the order carries the change and is not in the register.
  const saved = s.correctionSave(two.id, form({ state: /name="state" value="([^"]*)"/.exec(t.html)[1].replace(/&#34;/g, '"'), reason: 'wrong_item' }))
  assert.equal(saved.status, 200)
  assert.equal(saved.html, '')
  assert.deepEqual(Object.keys(saved.trigger), ['bestellungen-geaendert'])
  const column = text(r.bestellungen())
  assert.match(column, /Korrigiert: falsches Gericht/)
  assert.match(column, /Panna Cotta/)
  assert.match(column, /Noch nicht in der Kasse eingegeben/)
  // Edge: the sheet that was open is now stale and says so, nothing is saved twice.
  const stale = s.correctionSave(two.id, form({ state: /name="state" value="([^"]*)"/.exec(t.html)[1].replace(/&#34;/g, '"'), reason: 'other' }))
  assert.equal(stale.status, 409)
  assert.match(stale.html, /Die Bestellung wurde inzwischen geändert/)
  assert.equal(s.correctionOpen('gone').status, 409)
  const broken = s.correctionTap(note.id, form({ state: '{nonsense', op: 'swap:0' }))
  assert.equal(broken.status, 409)
  assert.match(broken.html, /durcheinander geraten/)
}

// --- Reservierung eintragen: the form, the summary, the entry (the booking rules are the real tablet's) ---
{
  const { r, s, data } = fresh()
  const open = s.entryOpen().html
  const key = /name="schluessel" value="([^"]+)"/.exec(open)[1]
  const today = data.hours.today
  const sent = (o) => form({ schluessel: key, datum: today, uhrzeit: '19:00', personen: '4', name: 'Familie Beispiel', telefon: '', hinweis: 'Kinderstuhl', tisch: '', ...o })
  // Refusals keep what was typed and say why in the real sentences.
  assert.match(text(s.entryCheck(sent({ name: ' ' })).html), /Bitte einen Namen eintragen/)
  assert.match(text(s.entryCheck(sent({ personen: '0' })).html), /Bitte Datum, Uhrzeit, Personen und Namen prüfen/)
  assert.match(text(s.entryCheck(sent({ uhrzeit: '' })).html), /Bitte Datum, Uhrzeit, Personen und Namen prüfen/)
  assert.match(text(s.entryCheck(sent({ datum: '2020-01-01' })).html), /Dieser Zeitpunkt ist schon vorbei/)
  const kept = s.entryCheck(sent({ name: '' })).html
  assert.match(kept, /value="Kinderstuhl"/)
  assert.equal(s.entryCheck(sent({ name: '' })).status, 409)
  // The summary, then the confirming tap puts the table on "Heute".
  const sum = s.entryCheck(sent({}))
  assert.equal(sum.status, 200)
  assert.match(text(sum.html), /Familie Beispiel/)
  assert.match(text(sum.html), /4 Personen/)
  assert.match(text(sum.html), /Beispiel: Auslastung, Tische und Öffnungszeiten prüft die echte Version/)
  assert.doesNotMatch(sum.html, /Trotzdem/, 'the demo asks for no override')
  const id = /reservierung\/([^/]+)\/eintragen/.exec(sum.html)[1]
  const done = s.entryConfirm(id)
  assert.equal(done.trigger['reservierung-eingetragen'], 'Reservierung für Familie Beispiel eingetragen.')
  assert.match(text(r.heute()), /19:00/)
  assert.match(text(r.heute()), /Familie Beispiel/)
  // Edge: the same confirm twice books once.
  s.entryConfirm(id)
  assert.equal((r.heute().match(/Familie Beispiel/g) ?? []).length, 1)
  assert.equal(s.entryConfirm('nope'), null)
  // The table the visitor picked is named in the summary; which tables are free is the real tablet's.
  const table = data.entry.choices[2]
  const named = s.entryCheck(sent({ tisch: table[0], uhrzeit: '19:30', name: 'Zweite', schluessel: 'k2' }))
  assert.equal(named.status, 200)
  assert.match(text(named.html), /Tisch T3/)
}

// --- Team number and a date's hours, with the owner's check cards ---
{
  const { r, s, data } = fresh()
  const other = data.team.choices[0]
  assert.match(text(s.teamOpen().html), new RegExp(`Heute: ${other.replace('+', '\\+')}`))
  const bad = s.teamChange(form({ nummer: 'abc' }))
  assert.equal(bad.status, 409)
  assert.match(text(bad.html), /Das ist keine Telefonnummer/)
  assert.match(text(s.teamChange(form({ nummer: '+4930999999' })).html), /kann die Telefonanlage nicht anrufen/)
  assert.match(text(s.teamChange(form({ nummer: data.team.standard })).html), /Keine Änderung/)
  const set = s.teamChange(form({ nummer: other }))
  assert.match(text(set.html), /Gilt jetzt: \+49/)
  assert.match(text(set.html), /Gilt bis Betriebsschluss/)
  assert.deepEqual(set.events, ['checks'])
  assert.match(text(s.checkCards()), /Teamnummer heute/)
  assert.match(s.checkCards(), /hx-post="\/gui\/pruefen\/team\/uebernehmen"/, 'the owner\'s answer is a route behind the PIN')
  // Back to the first number: the card closes as undone.
  const back = s.teamChange(form({ nummer: data.team.standard }))
  assert.match(text(back.html), /Zurückgenommen/)
  assert.doesNotMatch(s.checkCards(), /Teamnummer heute/)

  const today = data.hours.today
  const later = new Date(Date.parse(today) + 3 * 86400000).toISOString().slice(0, 10)
  assert.match(text(s.hoursChange(form({ datum: today, art: 'geschlossen' })).html), /Gilt sofort/)
  assert.match(text(s.hoursOpen(today).html), /Gilt jetzt geschlossen/)
  assert.match(text(s.checkCards()), /Öffnungszeit .{2}, \d\d\.\d\d\./)
  // Longer hours on a later day wait for the owner and say so; the day itself still holds the weekly hours.
  const wait = s.hoursChange(form({ datum: later, art: 'zeiten', von: '10:00', bis: '23:30' }))
  assert.match(text(wait.html), /Wartet auf den Inhaber/)
  assert.match(text(s.hoursOpen(later).html), /Gilt jetzt normale Zeiten der Woche/)
  assert.match(text(s.checkCards()), /Wartet auf den Inhaber/)
  // Shorter ones hold at once.
  assert.match(text(s.hoursChange(form({ datum: later, art: 'zeiten', von: '12:00', bis: '22:00' })).html), /Gilt sofort/)
  // Edge cases of the real form.
  assert.match(text(s.hoursChange(form({ datum: today, art: 'zeiten', von: '12:00', bis: '12:00' })).html), /nicht gleich dem Beginn/)
  assert.match(text(s.hoursChange(form({ datum: today, art: 'zeiten', von: '18:00', bis: '02:00' })).html), /Ende nach Mitternacht/)
  assert.match(text(s.hoursChange(form({ datum: today, art: 'zeiten', von: '18:00', bis: '00:00' })).html), /00:00 Uhr liegt schon am nächsten Tag/)
  assert.match(text(s.hoursChange(form({ datum: today, art: 'zeiten', von: 'abends', bis: '22:00' })).html), /Beginn und Ende als Uhrzeit/)
  assert.match(text(s.hoursChange(form({ datum: '2020-01-01', art: 'normal' })).html), /vergangener Tag/)
  assert.equal(s.hoursChange(form({ datum: '', art: 'normal' })).status, 422)
  assert.equal(s.hoursChange(form({ datum: today, art: 'nie' })).status, 422)
  assert.match(text(s.hoursChange(form({ datum: later, art: 'normal' })).html), /Zurückgenommen/)
  void r
}

// --- The pages Anrufe and Menü: their filters ---
{
  const calls = read('anrufe/index.html')
  const rows = (html) => (html.match(/<tr>\s*<th scope="row">/g) ?? []).length
  const all = rows(calls)
  assert.ok(all > 3)
  const done = S.filterCalls(calls, 'completed', '', 'heute')
  assert.ok(rows(done) > 0 && rows(done) < all, 'a result cuts the rows')
  assert.match(done, /<option value="completed" selected>/)
  assert.match(text(done), new RegExp(`${rows(done)} Anrufe? heute`))
  const both = S.filterCalls(calls, 'completed', 'info', 'heute')
  assert.ok(rows(both) <= rows(done))
  assert.match(S.filterCalls(calls, 'error', '', 'heute'), /Keine Anrufe für diese Auswahl/)
  assert.equal(rows(S.filterCalls(calls, '', '', 'heute')), all, 'no filter keeps every row')
  assert.equal(S.dayLabelOf(calls, '2026-01-01'), '')
  assert.equal(S.dayLabelOf(calls, /<option value="([^"]+)" selected>Heute/.exec(calls)[1]), 'heute')

  const menu = read('menue/index.html')
  const items = (html) => (html.match(/<bdi class="nr">/g) ?? []).length
  assert.equal(items(menu), 25)
  assert.equal(items(S.filterMenu(menu, '', '')), 25)
  const some = S.filterMenu(menu, 'pizza', '')
  assert.equal(items(some), 5)
  assert.match(text(some), /5 von 25 Gerichten/)
  assert.match(some, /value="pizza"/)
  assert.equal(items(S.filterMenu(menu, '30', '')), 5, 'a number prefix')
  assert.match(S.filterMenu(menu, 'xyz', ''), /Kein Gericht gefunden/)
  // Every dish lacks an allergen, so that filter keeps them all and no dish carries a price difference.
  assert.equal(items(S.filterMenu(menu, '', 'allergene')), 25)
  assert.match(S.filterMenu(menu, '', 'preis'), /Kein Gericht gefunden/)
  assert.match(S.filterMenu(menu, '', 'preis'), /<option value="preis" selected>/)
}

// --- Round 1 of the review of #899: the cases of the sheets ---
{
  const { data } = fresh()
  const today = data.hours.today

  // The time is checked again when the team confirms an unconfirmed draft; a confirmed one replays.
  const c = fresh()
  const soon = c.s.entryCheck(form({ datum: today, uhrzeit: '12:01', personen: '2', name: 'Knapp', telefon: '', hinweis: '', tisch: '', schluessel: 'late' }))
  assert.equal(soon.status, 200, 'it lies ahead at the demo clock 12:00')
  const sid = /reservierung\/([^/]+)\/eintragen/.exec(soon.html)[1]
  c.r.tick(2)
  const refused = c.s.entryConfirm(sid)
  assert.equal(refused.status, 409)
  assert.match(text(refused.html), /Dieser Zeitpunkt ist schon vorbei/)
  assert.doesNotMatch(refused.html, /Eintragen<\/button>/, 'a draft in the past has no confirming button')
  assert.doesNotMatch(text(c.r.heute()), /Knapp/)
  const d = fresh()
  const kept = d.s.entryCheck(form({ datum: today, uhrzeit: '12:01', personen: '2', name: 'Treu', telefon: '', hinweis: '', tisch: '', schluessel: 'ok' }))
  const kid = /reservierung\/([^/]+)\/eintragen/.exec(kept.html)[1]
  assert.equal(d.s.entryConfirm(kid).status, 200)
  d.r.tick(5)
  assert.equal(d.s.entryConfirm(kid).status, 200, 'a confirmed draft replays')
  assert.equal((d.r.heute().match(/Treu/g) ?? []).length, 1)

  // Every dish off: an example call takes no order and does not claim one.
  const e = fresh()
  for (const dish of e.data.menu) e.r.setOff(dish.number, true)
  const before = (e.r.bestellungen().match(/class="card order/g) ?? []).length
  const out = e.r.exampleCall()
  assert.equal(out.order, false)
  assert.equal((e.r.bestellungen().match(/class="card order/g) ?? []).length, before, 'no empty order card')
  assert.equal(out.reservation, true, 'the table is still taken')

  // Swapped to its own number: the choices stay editable and can be saved.
  const g = fresh()
  const margherita = state().orders.orders.find((o) => o.lines.some((l) => l.number === '201'))
  const tapg = (html, op, extra = {}) => g.s.correctionTap(margherita.id, form({ state: /name="state" value="([^"]*)"/.exec(html)[1].replace(/&#34;/g, '"'), op, ...extra }))
  let sheet = g.s.correctionOpen(margherita.id).html
  const idx = margherita.lines.findIndex((l) => l.number === '201')
  let step = tapg(sheet, `swap:${idx}`)
  step = tapg(step.html, 'number', { number: '201' })
  assert.match(text(step.html), /Bei 201 Pizza Margherita fehlt noch: Größe/, 'its own number opens the choices')
  step = tapg(step.html, `opt:r:${idx}:0:1`)
  assert.match(text(step.html), /Gewählt: Groß/)
  assert.doesNotMatch(text(step.html), /fehlt noch/)
  assert.match(text(step.html), /Noch nichts geändert/, 'the same choice as before is no change')
  step = tapg(step.html, `opt:r:${idx}:0:0`)
  assert.match(text(step.html), /Gewählt: Normal/)
  assert.doesNotMatch(text(step.html), /Noch nichts geändert/)
  const price = (html) => text(html).match(/Summe ([\d,]+) €/)[1]
  assert.ok(Number(price(step.html).replace(',', '.')) < 28.7, 'Normal costs less than Groß')
  const kept2 = g.s.correctionSave(margherita.id, form({ state: /name="state" value="([^"]*)"/.exec(step.html)[1].replace(/&#34;/g, '"'), reason: 'wrong_item' }))
  assert.equal(kept2.status, 200)
  assert.match(text(g.r.bestellungen()), /Korrigiert: falsches Gericht/)
  // A dish without choices swapped to its own number is no change at all.
  const plain = state().orders.orders.find((o) => o.lines.some((l) => l.number === '203'))
  const pidx = plain.lines.findIndex((l) => l.number === '203')
  const ps = g.s.correctionOpen(plain.id).html
  const pa = g.s.correctionTap(plain.id, form({ state: /name="state" value="([^"]*)"/.exec(ps)[1].replace(/&#34;/g, '"'), op: `swap:${pidx}` }))
  const pb = g.s.correctionTap(plain.id, form({ state: /name="state" value="([^"]*)"/.exec(pa.html)[1].replace(/&#34;/g, '"'), op: 'number', number: '203' }))
  assert.match(text(pb.html), /Noch nichts geändert/)
  assert.doesNotMatch(pb.html, /class="edit-line swapped"/)
}

// --- Round 3 of the review of #899 ---
{
  const { s, data } = fresh()
  const today = data.hours.today
  const other = new Date(Date.parse(today) + 86400000).toISOString().slice(0, 10)
  const put = (o) => form({ datum: other, uhrzeit: '19:00', personen: '2', name: 'Gast', telefon: '', hinweis: '', tisch: '', schluessel: 'r3' + Math.random(), ...o })

  // The phone number: refused when it is none, kept as E.164 otherwise, optional.
  assert.match(text(s.entryCheck(put({ telefon: 'abc' })).html), /Rufnummer habe ich nicht verstanden/)
  assert.equal(s.entryCheck(put({ telefon: 'abc' })).status, 409)
  const national = s.entryCheck(put({ telefon: '030 23125 000', name: 'National' }))
  assert.match(text(national.html), /Telefon \+493023125000/)
  assert.match(text(s.entryCheck(put({ telefon: '0049 30 / 23125-000', name: 'Alt' })).html), /Telefon \+493023125000/)
  assert.match(text(s.entryCheck(put({ telefon: '+49 030 23125000', name: 'Trunk' })).html), /Telefon \+493023125000/)
  assert.doesNotMatch(text(s.entryCheck(put({ telefon: '', name: 'Ohne' })).html), /Telefon/)
  assert.equal(S.normalizePhone('12'), null)
  assert.equal(S.normalizePhone('+0123456789'), null)

  // A request that waits is withdrawn by asking for the hours that hold now.
  const h = fresh()
  const later = new Date(Date.parse(today) + 3 * 86400000).toISOString().slice(0, 10)
  h.s.hoursChange(form({ datum: later, art: 'zeiten', von: '10:00', bis: '23:30' }))
  assert.match(h.s.checkCards(), /Wartet auf den Inhaber/)
  const back = h.s.hoursChange(form({ datum: later, art: 'normal' }))
  assert.match(text(back.html), /Zurückgenommen/)
  assert.doesNotMatch(h.s.checkCards(), /Wartet auf den Inhaber/)
  assert.deepEqual(back.events, ['checks'])
  assert.match(text(h.s.hoursChange(form({ datum: later, art: 'normal' })).html), /Keine Änderung/, 'nothing waits: no change')
  // A change in force, asked again, keeps its card and its hours.
  h.s.hoursChange(form({ datum: today, art: 'geschlossen' }))
  assert.match(h.s.checkCards(), /Öffnungszeit/)
  const again = h.s.hoursChange(form({ datum: today, art: 'geschlossen' }))
  assert.match(text(again.html), /Keine Änderung/)
  assert.match(text(again.html), /Gilt jetzt geschlossen/)
  assert.match(h.s.checkCards(), /Öffnungszeit/, 'the card of the owner stays')
}

console.log('ok')
