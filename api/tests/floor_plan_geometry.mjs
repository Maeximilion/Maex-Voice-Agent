// Checks the floor plan geometry of the editor (T-3.11) without a browser.
// Usage: node api/tests/floor_plan_geometry.mjs api/gui/static/raumplan-geometry.js
// (test_gui_floor_plan_geometry.py runs it). Prints "ok" or throws.
import assert from 'node:assert'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const G = await import(pathToFileURL(path.resolve(process.argv[2])).href)

// The accepted seat rule (docs/design/floor-plan/README.md): six seats are
// 2 + 2 with both ends occupied, the second reported mistake.
const sides = (seats, endSeat) => G.seatSides(seats, 'rect', endSeat)
assert.deepEqual(sides(4), { top: 2, bottom: 2, left: 0, right: 0 })
assert.deepEqual(sides(5), { top: 2, bottom: 2, left: 1, right: 0 })
assert.deepEqual(sides(5, 'right'), { top: 2, bottom: 2, left: 0, right: 1 })
assert.deepEqual(sides(6), { top: 2, bottom: 2, left: 1, right: 1 })
assert.deepEqual(sides(6, 'right'), { top: 2, bottom: 2, left: 1, right: 1 })
assert.deepEqual(sides(7), { top: 3, bottom: 2, left: 1, right: 1 })
assert.deepEqual(sides(8), { top: 3, bottom: 3, left: 1, right: 1 })
assert.deepEqual(sides(1), { top: 0, bottom: 0, left: 1, right: 0 })
assert.deepEqual(sides(3), { top: 1, bottom: 1, left: 1, right: 0 })
for (let seats = 1; seats <= 40; seats++) {
  const s = sides(seats)
  assert.equal(s.top + s.bottom + s.left + s.right, seats, `seats ${seats}`)
  if (seats >= 6) assert.equal(s.left + s.right, 2, `both ends at ${seats}`)
}
assert.deepEqual(G.seatSides(5, 'round'), { around: 5 })
assert.equal(G.hasEndChoice({ shape: 'rect', seats: 5 }), true)
assert.equal(G.hasEndChoice({ shape: 'rect', seats: 6 }), false)
assert.equal(G.hasEndChoice({ shape: 'rect', seats: 7 }), false)
assert.equal(G.hasEndChoice({ shape: 'round', seats: 5 }), false)

// The first reported mistake: the counter's text stood on its head after a turn.
// The text turns back by the piece's angle at every quarter turn.
for (const angle of [0, 90, 180, 270]) {
  const counter = { type: 'counter', w: 80, h: 260, angle }
  const label = G.labelTurn(counter)
  assert.equal((angle + label.turn) % 360, 0, `counter upright at ${angle}`)
  // Tall as drawn at 0 and 180, wide at 90 and 270.
  assert.equal(label.vertical, angle % 180 === 0, `counter writing at ${angle}`)
  const table = G.labelTurn({ type: 'table', w: 120, h: 80, angle })
  assert.equal((angle + table.turn) % 360, 0)
  assert.equal(table.vertical, false)
}

// A free label turns but never stands on its head (Maxi, 03.11.2026).
assert.deepEqual([0, 90, 180, 270, -90, 360].map(G.readableTurn), [0, 90, 0, -90, -90, 0])
for (let a = -720; a <= 720; a += 90) {
  const r = G.readableTurn(a)
  assert.ok(r > -90 - 1e-9 && r <= 90, `readable at ${a}`)
  assert.equal((((a - r) % 180) + 180) % 180, 0, `same line at ${a}`)
}
// "Drehen" goes across, from below, from above, and across again.
assert.deepEqual([G.nextLabelTurn(0), G.nextLabelTurn(270), G.nextLabelTurn(90), G.nextLabelTurn(180)], [270, 90, 0, 270])

// A 160 x 80 table fits a 2 x 2 m room at every quarter turn (Codex on #535: 270 was refused).
for (const angle of [0, 90, 180, 270]) {
  assert.equal(G.available({ type: 'table', w: 160, h: 80, angle }, { w: 200, h: 200 }), true, `fits at ${angle}`)
}
assert.deepEqual(G.footprint({ w: 160, h: 80, angle: 270 }), [80, 160])

// A turned table swaps width and depth as drawn.
assert.deepEqual(G.footprint({ w: 200, h: 80, angle: 90 }).map(Math.round), [80, 200])

// Copies take the next free number over every room, whatever its case.
assert.equal(G.nextTableName(['T1', 't2', 'T4']), 'T3')
assert.equal(G.nextTableName([]), 'T1')

// A copy lands inside the room and clear of the other tables.
const room = { w: 1000, h: 700 }
const source = { type: 'table', x: 170, y: 180, w: 120, h: 80, angle: 0 }
const copy = { ...source }
G.placeCopy(copy, source, [source], room)
assert.ok(copy.x - 60 >= 0 && copy.x + 60 <= 1000 && copy.y - 40 >= 0 && copy.y + 40 <= 700)
assert.ok(Math.abs(copy.x - source.x) > 120 || Math.abs(copy.y - source.y) > 80)

// fit keeps a table 20 cm off the wall and at whole centimetres.
const loose = { type: 'table', x: 995, y: -5, w: 121, h: 80, angle: 0 }
G.fit(loose, room)
assert.deepEqual([loose.x, loose.y], [Math.round(1000 - 60.5 - 20), 60])

// A wall drawn by two points and its end points agree.
const wall = { type: 'wall', thickness: 20 }
G.wallFromPoints(wall, { x: 100, y: 100 }, { x: 100, y: 400 })
assert.equal(Math.round(wall.length), 300)
assert.deepEqual(G.endpoints(wall).map(p => [Math.round(p.x), Math.round(p.y)]), [[100, 100], [100, 400]])

// The server document survives the way into the editor and back.
const doc = {
  id: 'a1', revision: 3, name: 'Gastraum', kind: 'inside', shape: 'rectangle',
  width_cm: 1000, depth_cm: 700, wall_cm: 20,
  tables: [{ id: 't1', name: 'T1', shape: 'rect', seats: 5, width_cm: 120, depth_cm: 80,
    x_cm: 200, y_cm: 200, rotation: 90, end_seat: 'right' }],
  fixtures: [
    { kind: 'counter', id: 'f1', name: 'Theke', x_cm: 880, y_cm: 450, width_cm: 80, depth_cm: 260, rotation: 0 },
    { kind: 'door', id: 'f2', name: 'Eingang', side: 'bottom', offset_cm: 150, width_cm: 110 },
    { kind: 'wall', id: 'f3', name: 'Wand', x_cm: 800, y_cm: 170, length_cm: 200, angle: 90, thickness_cm: 20 },
    { kind: 'label', id: 'f4', text: 'Fenster', x_cm: 200, y_cm: 55, size: 'large', rotation: 270 }
  ],
  combinations: [['t1', 't2']]
}
const state = G.toClient(doc)
assert.equal(state.revision, 3)
const back = G.toServer(state)
const { id, revision, ...content } = doc
assert.deepEqual(back, content)

// What the server sends is checked, not trusted (docs/22): a broken document is
// refused whole instead of drawing a room from undefined values.
assert.throws(() => G.toClient({ ...doc, tables: 'T1' }), /tables is not a list/)
assert.throws(() => G.toClient({ ...doc, width_cm: '1000' }), /width_cm is not a number/)
assert.throws(() => G.toClient({ ...doc, tables: [{ ...doc.tables[0], end_seat: 'middle' }] }), /end_seat/)
assert.throws(() => G.toClient(null), /the room is not an object/)
// An unknown fixture is refused as well: dropped, the next save would delete it.
assert.throws(() => G.toClient({ ...doc, fixtures: [{ kind: 'plant', id: 'f9' }] }), /fixture kind plant is unknown/)
// A label without a rotation lies across.
const flat = G.toClient({ ...doc, fixtures: [{ kind: 'label', id: 'f4', text: 'Fenster', x_cm: 200, y_cm: 55, size: 'large' }] })
assert.equal(flat.objects[1].angle, 0)

console.log('ok')
