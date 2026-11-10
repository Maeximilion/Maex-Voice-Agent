// Floor plan geometry (T-3.11, docs/06 §3 "Raumplan"). Pure functions without
// the page, so node can test them (api/tests/test_gui_floor_plan_geometry.py).
// Lengths are centimetres, from the room's top left corner; angles are degrees.
// The rules are those of the approved design, docs/design/floor-plan/README.md.
//
// Source: api/gui/ts/raumplan-geometry.ts. The file of the same name in
// api/gui/static/ is its compiled output (`npm run build`, docs/22); edit the
// source, never the output.
// Editor presets, not rules about bookable capacity.
export const PRESETS = {
    rect: [[80, 80], [120, 80], [140, 80], [160, 80], [180, 90], [200, 100]],
    round: [[80, 80], [100, 100], [110, 110], [120, 120], [140, 140]]
};
// Pixel sizes of a free label; docs/06 §1 asks for 18 px at least.
export const LABEL_PX = { small: 18, normal: 20, large: 26, xlarge: 32 };
export const LIMITS = {
    roomMin: 200, roomMax: 5000, wallMin: 5, wallMax: 60,
    tableMin: 30, tableMax: 600, counterMin: 30, counterMax: 1000,
    seatsMin: 1, seatsMax: 40, doorMin: 60, doorMax: 300, wallLengthMin: 20
};
const SIDES = ["top", "right", "bottom", "left"];
const END_SEATS = ["left", "right"];
const TABLE_SHAPES = ["rect", "round"];
const LABEL_SIZES = ["small", "normal", "large", "xlarge"];
export function clamp(n, min, max) { return Math.max(min, Math.min(max, n)); }
function rad(a) { return a * Math.PI / 180; }
// How many chairs stand on which side of a table, unrotated. Up to five
// seats an odd one takes one end; from six on both ends stay occupied and
// further seats alternate between the long sides (2+2+1+1, 3+2+1+1, ...).
export function seatSides(seats, shape, endSeat) {
    if (shape === "round")
        return { around: seats };
    const ends = seats >= 6 ? 2 : seats % 2;
    const long = seats - ends;
    return {
        top: Math.ceil(long / 2),
        bottom: Math.floor(long / 2),
        left: ends === 2 || (ends === 1 && endSeat !== "right") ? 1 : 0,
        right: ends === 2 || (ends === 1 && endSeat === "right") ? 1 : 0
    };
}
// The end seat is a choice only while exactly one end is occupied.
export function hasEndChoice(table) {
    return table.shape === "rect" && table.seats < 6 && table.seats % 2 === 1;
}
// Width and depth as drawn.
export function footprint(o) {
    // Quarter turns swap exactly: cos(270 degrees) is not quite 0 in floating point.
    const q = ((Math.round(o.angle || 0) % 360) + 360) % 360;
    if (q === 0 || q === 180)
        return [o.w, o.h];
    if (q === 90 || q === 270)
        return [o.h, o.w];
    const a = rad(o.angle || 0);
    return [
        Math.abs(Math.cos(a)) * o.w + Math.abs(Math.sin(a)) * o.h,
        Math.abs(Math.sin(a)) * o.w + Math.abs(Math.cos(a)) * o.h
    ];
}
// The text on a table or counter turns back by the piece's angle, so it
// reads upright at every quarter turn; a counter drawn tall writes top to
// bottom instead. The earlier label stood on its head after a turn.
export function labelTurn(o) {
    const size = footprint(o);
    return { turn: -(o.angle || 0), vertical: o.type === "counter" && size[1] > size[0] * 1.5 };
}
// A free label turns with its quarter turn but never stands on its head:
// 180 reads as 0, 270 as -90 (from below). The result lies in (-90, 90].
export function readableTurn(angle) {
    let a = ((Math.round(angle || 0) % 360) + 360) % 360;
    if (a > 90 && a < 270)
        a -= 180;
    else if (a >= 270)
        a -= 360;
    return a;
}
// "Drehen" on a label: across, then from below (along a left wall), then from above.
const LABEL_TURNS = [0, 270, 90];
export function nextLabelTurn(angle) {
    const i = LABEL_TURNS.indexOf((readableTurn(angle) + 360) % 360);
    // An angle outside the cycle starts it again (-1 + 1). The index is always
    // inside the list; `?? 0` only tells the compiler so.
    return LABEL_TURNS[(i + 1) % LABEL_TURNS.length] ?? 0;
}
export function endpoints(o) {
    const dx = Math.cos(rad(o.angle)) * o.length / 2;
    const dy = Math.sin(rad(o.angle)) * o.length / 2;
    return [{ x: o.x - dx, y: o.y - dy }, { x: o.x + dx, y: o.y + dy }];
}
export function wallFromPoints(o, a, b) {
    o.x = (a.x + b.x) / 2;
    o.y = (a.y + b.y) / 2;
    o.length = Math.hypot(b.x - a.x, b.y - a.y);
    o.angle = Math.atan2(b.y - a.y, b.x - a.x) * 180 / Math.PI;
}
// Keep a piece inside its room; a table keeps 20 cm off the wall.
export function fit(o, r) {
    if (o.type === "door") {
        const extent = o.side === "top" || o.side === "bottom" ? r.w : r.h;
        o.width = Math.min(o.width, extent - 40);
        o.offset = clamp(o.offset, o.width / 2 + 10, extent - o.width / 2 - 10);
        return;
    }
    if (o.type === "wall") {
        const margin = o.thickness / 2 + 1;
        o.x = clamp(o.x, margin + 10, r.w - margin - 10);
        o.y = clamp(o.y, margin + 10, r.h - margin - 10);
        const c = Math.abs(Math.cos(rad(o.angle))), s = Math.abs(Math.sin(rad(o.angle)));
        const max = Math.min(c > 0.0001 ? (r.w - 2 * margin) / c : Infinity, s > 0.0001 ? (r.h - 2 * margin) / s : Infinity);
        o.length = clamp(o.length, LIMITS.wallLengthMin, max);
        const hw = c * o.length / 2 + margin, hh = s * o.length / 2 + margin;
        o.x = clamp(o.x, hw, r.w - hw);
        o.y = clamp(o.y, hh, r.h - hh);
        return;
    }
    const size = o.type === "label" ? [40, 30] : footprint(o);
    const pad = o.type === "table" ? 20 : 5;
    o.x = clamp(o.x, size[0] / 2 + pad, r.w - size[0] / 2 - pad);
    o.y = clamp(o.y, size[1] / 2 + pad, r.h - size[1] / 2 - pad);
    if (o.type === "table") {
        o.x = Math.round(o.x);
        o.y = Math.round(o.y);
    }
}
// Whether a table or counter fits into the room at all in its orientation.
export function available(o, r) {
    if (o.type !== "table" && o.type !== "counter")
        return true;
    const size = footprint(o), pad = o.type === "table" ? 40 : 10;
    return size[0] + pad <= r.w && size[1] + pad <= r.h;
}
export function doorPoint(o, r) {
    return {
        x: o.side === "left" ? 0 : o.side === "right" ? r.w : o.offset,
        y: o.side === "top" ? 0 : o.side === "bottom" ? r.h : o.offset
    };
}
// An entrance follows the pointer along the nearest outer wall; of two walls
// equally near, the earlier of left, right, top, bottom wins.
export function moveDoor(o, p, r, snap) {
    let side = "left", nearest = Math.abs(p.x);
    const others = [["right", Math.abs(p.x - r.w)],
        ["top", Math.abs(p.y)], ["bottom", Math.abs(p.y - r.h)]];
    for (const [name, distance] of others) {
        if (distance < nearest) {
            side = name;
            nearest = distance;
        }
    }
    o.side = side;
    o.offset = snap(o.side === "top" || o.side === "bottom" ? p.x : p.y);
    fit(o, r);
}
// The first free "T<n>" over every room: a copy gets a new number.
export function nextTableName(names) {
    const taken = new Set(names.map((name) => String(name).toLocaleLowerCase("de-DE")));
    let n = 1;
    while (taken.has("t" + n))
        n++;
    return "T" + n;
}
// Beside the source if there is room, else the first free spot on a grid.
// A convenience only: nothing here checks clearance between pieces.
export function placeCopy(o, source, objects, r) {
    const [w, h] = footprint(o);
    const candidates = [[source.x + w + 45, source.y], [source.x, source.y + h + 45],
        [source.x - w - 45, source.y], [source.x, source.y - h - 45]];
    for (let y = h / 2 + 30; y < r.h - h / 2 - 20; y += h + 45) {
        for (let x = w / 2 + 30; x < r.w - w / 2 - 20; x += w + 45)
            candidates.push([x, y]);
    }
    for (const [cx, cy] of candidates) {
        if (cx < w / 2 + 20 || cx > r.w - w / 2 - 20 || cy < h / 2 + 20 || cy > r.h - h / 2 - 20)
            continue;
        const free = objects.every((a) => {
            if (a.type !== "table" && a.type !== "counter")
                return true;
            const s = footprint(a);
            return Math.abs(a.x - cx) > (s[0] + w) / 2 + 25 || Math.abs(a.y - cy) > (s[1] + h) / 2 + 25;
        });
        if (free) {
            o.x = cx;
            o.y = cy;
            fit(o, r);
            return;
        }
    }
    o.x = source.x + 40;
    o.y = source.y + 40;
    fit(o, r);
}
function refuse(what) { throw new Error("floor plan document: " + what); }
function fields(value, what) {
    if (typeof value !== "object" || value === null || Array.isArray(value))
        refuse(what + " is not an object");
    return value;
}
function num(o, key) {
    const v = o[key];
    return typeof v === "number" && Number.isFinite(v) ? v : refuse(key + " is not a number");
}
function str(o, key) {
    const v = o[key];
    return typeof v === "string" ? v : refuse(key + " is not a string");
}
function oneOf(o, key, allowed) {
    const found = allowed.find((a) => a === o[key]);
    return found ?? refuse(key + " is none of " + allowed.join(", "));
}
function list(o, key) {
    const v = o[key];
    return Array.isArray(v) ? v : refuse(key + " is not a list");
}
function tableFrom(value) {
    const t = fields(value, "a table");
    return { id: str(t, "id"), type: "table", name: str(t, "name"), shape: oneOf(t, "shape", TABLE_SHAPES),
        seats: num(t, "seats"), w: num(t, "width_cm"), h: num(t, "depth_cm"), x: num(t, "x_cm"), y: num(t, "y_cm"),
        angle: num(t, "rotation"), endSeat: oneOf(t, "end_seat", END_SEATS) };
}
function fixtureFrom(value) {
    const f = fields(value, "a fixture");
    const kind = f["kind"];
    if (kind === "counter")
        return { id: str(f, "id"), type: "counter", name: str(f, "name"),
            x: num(f, "x_cm"), y: num(f, "y_cm"), w: num(f, "width_cm"), h: num(f, "depth_cm"), angle: num(f, "rotation") };
    if (kind === "door")
        return { id: str(f, "id"), type: "door", name: str(f, "name"),
            side: oneOf(f, "side", SIDES), offset: num(f, "offset_cm"), width: num(f, "width_cm") };
    if (kind === "wall")
        return { id: str(f, "id"), type: "wall", name: str(f, "name"),
            x: num(f, "x_cm"), y: num(f, "y_cm"), length: num(f, "length_cm"), angle: num(f, "angle"),
            thickness: num(f, "thickness_cm") };
    if (kind === "label")
        return { id: str(f, "id"), type: "label", text: str(f, "text"),
            x: num(f, "x_cm"), y: num(f, "y_cm"), size: oneOf(f, "size", LABEL_SIZES),
            // A label without a rotation lies across, as before the check existed.
            angle: f["rotation"] === undefined ? 0 : num(f, "rotation") };
    return refuse("fixture kind " + String(kind) + " is unknown");
}
function linkFrom(value) {
    if (!Array.isArray(value) || typeof value[0] !== "string" || typeof value[1] !== "string") {
        return refuse("a combination is not a pair of table ids");
    }
    return [value[0], value[1]];
}
// The server's room document (domain/reservations/floor_plan.py) to the
// editor's state, and back.
export function toClient(doc) {
    const d = fields(doc, "the room");
    return {
        room: { id: str(d, "id"), name: str(d, "name"), kind: str(d, "kind"), shape: str(d, "shape"),
            w: num(d, "width_cm"), h: num(d, "depth_cm"), thickness: num(d, "wall_cm") },
        objects: [...list(d, "tables").map(tableFrom), ...list(d, "fixtures").map(fixtureFrom)],
        links: list(d, "combinations").map(linkFrom),
        revision: num(d, "revision")
    };
}
function round1(n) { return Math.round(n * 10) / 10; }
function quarterTurn(angle) { return ((Math.round(angle / 90) * 90) % 360 + 360) % 360; }
export function toServer(state) {
    const r = state.room, tables = [], fixtures = [];
    state.objects.forEach((o) => {
        if (o.type === "table")
            tables.push({ id: o.id, name: o.name, shape: o.shape, seats: o.seats,
                width_cm: Math.round(o.w), depth_cm: Math.round(o.h), x_cm: Math.round(o.x),
                y_cm: Math.round(o.y), rotation: quarterTurn(o.angle),
                end_seat: o.endSeat === "right" ? "right" : "left" });
        else if (o.type === "counter")
            fixtures.push({ kind: "counter", id: o.id, name: o.name,
                x_cm: round1(o.x), y_cm: round1(o.y), width_cm: Math.round(o.w), depth_cm: Math.round(o.h),
                rotation: quarterTurn(o.angle) });
        else if (o.type === "door")
            fixtures.push({ kind: "door", id: o.id, name: o.name, side: o.side,
                offset_cm: round1(o.offset), width_cm: Math.round(o.width) });
        else if (o.type === "wall")
            fixtures.push({ kind: "wall", id: o.id, name: o.name,
                x_cm: round1(o.x), y_cm: round1(o.y), length_cm: round1(o.length), angle: round1(o.angle),
                thickness_cm: Math.round(o.thickness) });
        else if (o.type === "label")
            fixtures.push({ kind: "label", id: o.id, text: o.text,
                x_cm: round1(o.x), y_cm: round1(o.y), size: o.size, rotation: quarterTurn(o.angle || 0) });
    });
    return {
        name: r.name, kind: r.kind, shape: r.shape, width_cm: Math.round(r.w),
        depth_cm: Math.round(r.h), wall_cm: Math.round(r.thickness),
        tables: tables, fixtures: fixtures,
        combinations: state.links.map((p) => [p[0], p[1]])
    };
}
