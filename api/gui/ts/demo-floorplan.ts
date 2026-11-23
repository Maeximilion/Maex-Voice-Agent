// The floor plan of the public tablet demo (#892, B4): what the server does for
// GET /gui/raumplan/plan, POST /gui/raumplan/bereiche and PUT /gui/raumplan/bereiche/{id},
// in the visitor's browser. The rules are copied from the server, not invented:
// the limits of a request are api/schemas/floor_plan.py, the rules between fields and
// the revision are api/domain/reservations/floor_plan.py, the answers' shape is
// api/gui/router.py. The texts are the server's, so the tablet says what it would say
// in operation. Not rebuilt: the audit log, and the refusal to remove a table a
// confirmed reservation sits on (the demo's reservations hold no table).
//
// Source: api/gui/ts/demo-floorplan.ts. The file of the same name in api/gui/static/ is
// its compiled output (`npm run build`, docs/22); edit the source, never the output.

export type Reply = { status: number; body: string };
type Obj = Record<string, unknown>;
export type Doc = {
  id: string;
  revision: number;
  name: string;
  kind: string;
  shape: string;
  width_cm: number;
  depth_cm: number;
  wall_cm: number;
  tables: Obj[];
  fixtures: Obj[];
  combinations: [string, string][];
};

// Bounds against a runaway request (api/schemas/floor_plan.py, domain/.../floor_plan.py).
const MAX_AREAS = 20;
const MAX_TABLES = 200;
const MAX_FIXTURES = 300;
const MAX_PAIRS = 1000;
// Furniture may touch its room's edge by rounding, never cross it.
const TOLERANCE_CM = 1;
const FIXTURE_ID = /^[A-Za-z0-9-]{1,40}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const PLAN_INVALID = "Der Plan enthält einen ungültigen Wert und ist nicht gespeichert.";
const sayNameTaken = (n: string): string => `Den Raumnamen ${n} gibt es schon. Bitte einen anderen wählen.`;
const sayTableTaken = (n: string): string => `Den Tischnamen ${n} gibt es schon. Bitte einen anderen wählen.`;

const fail = (status: number, code: string, message: string, say: string, area?: Doc): Reply => ({
  status,
  body: JSON.stringify({ ok: false, error: { code, message }, say, ...(area ? { area } : {}) }),
});
const refuse = (message: string, say: string): Reply => fail(422, "invalid_input", message, say);
const invalid = (): Reply => fail(422, "invalid_input", "plan", PLAN_INVALID);

// --- The limits of one request (pydantic, extra="forbid", strings stripped) -------------

class Bad extends Error {}
const bad = (): never => {
  throw new Bad();
};

// A pydantic field in JSON mode: a whole number may arrive as 120.0, a bool is no number.
function num(v: unknown, lo: number, hi: number, whole = true): number {
  if (typeof v !== "number" || !Number.isFinite(v) || v < lo || v > hi) return bad();
  return whole && !Number.isInteger(v) ? bad() : v;
}
function str(v: unknown, min: number, max: number): string {
  if (typeof v !== "string") return bad();
  const s = v.trim();
  return s.length < min || s.length > max ? bad() : s;
}
function pick<T extends string | number>(v: unknown, allowed: readonly T[], fallback?: T): T {
  if (v === undefined && fallback !== undefined) return fallback;
  return allowed.includes(v as T) ? (v as T) : bad();
}
function obj(v: unknown, keys: string[], optional: string[] = []): Obj {
  if (typeof v !== "object" || v === null || Array.isArray(v)) return bad();
  const o = v as Obj;
  if (Object.keys(o).some((k) => !keys.includes(k) && !optional.includes(k))) return bad();
  if (keys.some((k) => !(k in o))) return bad();
  return o;
}
function list(v: unknown, max: number): unknown[] {
  return Array.isArray(v) && v.length <= max ? v : bad();
}
function uuid(v: unknown): string {
  return typeof v === "string" && UUID.test(v) ? v.toLowerCase() : bad();
}

const ROTATIONS = [0, 90, 180, 270] as const;
const rotation = (o: Obj): number => pick(o["rotation"], ROTATIONS, 0);
const coordinate = (v: unknown): number => num(v, -10, 5010, false);

function table(v: unknown): Obj {
  const o = obj(v, ["id", "name", "shape", "seats", "width_cm", "depth_cm", "x_cm", "y_cm"], ["rotation", "end_seat"]);
  return {
    id: uuid(o["id"]),
    name: str(o["name"], 1, 25),
    shape: pick(o["shape"], ["rect", "round"]),
    seats: num(o["seats"], 1, 40),
    width_cm: num(o["width_cm"], 30, 600),
    depth_cm: num(o["depth_cm"], 30, 600),
    x_cm: num(o["x_cm"], 0, 5000),
    y_cm: num(o["y_cm"], 0, 5000),
    rotation: rotation(o),
    end_seat: pick(o["end_seat"], ["left", "right"], "left"),
  };
}

function fixture(v: unknown): Obj {
  const kind = typeof v === "object" && v !== null ? (v as Obj)["kind"] : undefined;
  const id = (o: Obj): string => (typeof o["id"] === "string" && FIXTURE_ID.test(o["id"]) ? o["id"] : bad());
  if (kind === "counter") {
    const o = obj(v, ["kind", "id", "name", "x_cm", "y_cm", "width_cm", "depth_cm"], ["rotation"]);
    return {
      kind, id: id(o), name: str(o["name"], 1, 25), x_cm: coordinate(o["x_cm"]), y_cm: coordinate(o["y_cm"]),
      width_cm: num(o["width_cm"], 30, 1000), depth_cm: num(o["depth_cm"], 30, 1000), rotation: rotation(o),
    };
  }
  if (kind === "door") {
    const o = obj(v, ["kind", "id", "name", "side", "offset_cm", "width_cm"]);
    return {
      kind, id: id(o), name: str(o["name"], 1, 25), side: pick(o["side"], ["top", "right", "bottom", "left"]),
      offset_cm: coordinate(o["offset_cm"]), width_cm: num(o["width_cm"], 50, 500),
    };
  }
  if (kind === "wall") {
    const o = obj(v, ["kind", "id", "name", "x_cm", "y_cm", "length_cm", "angle", "thickness_cm"]);
    return {
      kind, id: id(o), name: str(o["name"], 1, 25), x_cm: coordinate(o["x_cm"]), y_cm: coordinate(o["y_cm"]),
      length_cm: num(o["length_cm"], 20, 7100, false), angle: num(o["angle"], -360, 360, false),
      thickness_cm: num(o["thickness_cm"], 5, 60),
    };
  }
  if (kind === "label") {
    const o = obj(v, ["kind", "id", "text", "x_cm", "y_cm"], ["size", "rotation"]);
    return {
      kind, id: id(o), text: str(o["text"], 1, 60), x_cm: coordinate(o["x_cm"]), y_cm: coordinate(o["y_cm"]),
      size: pick(o["size"], ["small", "normal", "large", "xlarge"], "normal"), rotation: rotation(o),
    };
  }
  return bad();
}

type Wanted = Omit<Doc, "id" | "revision">;

function area(v: unknown): Wanted {
  const o = obj(v, ["name", "kind", "width_cm", "depth_cm", "shape", "wall_cm", "tables", "fixtures", "combinations"]);
  return {
    name: str(o["name"], 1, 30),
    kind: pick(o["kind"], ["inside", "terrace"]),
    width_cm: num(o["width_cm"], 200, 5000),
    depth_cm: num(o["depth_cm"], 200, 5000),
    shape: pick(o["shape"], ["rectangle", "square"]),
    wall_cm: num(o["wall_cm"], 5, 60),
    tables: list(o["tables"], MAX_TABLES).map(table),
    fixtures: list(o["fixtures"], MAX_FIXTURES).map(fixture),
    combinations: list(o["combinations"], MAX_PAIRS).map((p) => {
      const pair = list(p, 2);
      return pair.length === 2 ? ([uuid(pair[0]), uuid(pair[1])] as [string, string]) : bad();
    }),
  };
}

function parse<T>(raw: string, read: (v: unknown) => T): T | null {
  try {
    return read(JSON.parse(raw));
  } catch {
    // Not JSON, or a value that breaks a limit: the server's ValidationError.
    return null;
  }
}

// --- The rules between fields (domain/reservations/floor_plan.py) ---------------------

const lower = (s: string): string => s.toLowerCase(); // shortcut: the server casefolds; the same for names a restaurant uses
const asNum = (v: unknown): number => v as number;

function turned(p: Obj): [number, number] {
  return p["rotation"] === 90 || p["rotation"] === 270 ? [asNum(p["depth_cm"]), asNum(p["width_cm"])] : [asNum(p["width_cm"]), asNum(p["depth_cm"])];
}

// The refusal for the first rule a room breaks, in the server's order, or null.
function check(others: Doc[], w: Wanted): Reply | null {
  const inside = (name: string, x: number, y: number, wd: number, d: number): Reply | null =>
    wd / 2 - TOLERANCE_CM <= x && x <= w.width_cm - wd / 2 + TOLERANCE_CM &&
    d / 2 - TOLERANCE_CM <= y && y <= w.depth_cm - d / 2 + TOLERANCE_CM
      ? null
      : refuse("piece outside its room", `${name} liegt außerhalb des Raums.`);

  if (w.shape === "square" && w.width_cm !== w.depth_cm) {
    return refuse("square with two sides", "Ein Quadrat hat zwei gleich lange Seiten.");
  }
  if (others.some((a) => lower(a.name) === lower(w.name))) {
    return refuse("floor area name taken", sayNameTaken(w.name));
  }
  const ids = w.tables.map((t) => t["id"] as string);
  if (new Set(ids).size !== ids.length) return refuse("table twice in one room", "Ein Tisch steht doppelt im Plan.");
  if (others.some((a) => a.tables.some((t) => ids.includes(t["id"] as string)))) {
    return refuse("table of another area", "Ein Tisch gehört zu einem anderen Raum. Bitte die Seite neu laden.");
  }
  const elsewhere = new Set(others.flatMap((a) => a.tables.map((t) => lower(t["name"] as string))));
  const seen = new Set<string>();
  for (const t of w.tables) {
    const name = t["name"] as string;
    if (seen.has(lower(name)) || elsewhere.has(lower(name))) return refuse("table name taken", sayTableTaken(name));
    seen.add(lower(name));
    if (t["shape"] === "round" && t["width_cm"] !== t["depth_cm"]) {
      return refuse("round table not round", `Tisch ${name} ist rund: ein Maß.`);
    }
    const out = inside(name, asNum(t["x_cm"]), asNum(t["y_cm"]), ...turned(t));
    if (out) return out;
  }
  for (const [a, b] of w.combinations) {
    if (a === b || !ids.includes(a) || !ids.includes(b)) {
      return refuse("combination outside the room", "Kombinieren geht nur mit Tischen dieses Raums.");
    }
  }
  const fids = w.fixtures.map((f) => f["id"] as string);
  if (new Set(fids).size !== fids.length) return refuse("fixture twice", "Ein Objekt steht doppelt im Plan.");
  for (const f of w.fixtures) {
    const name = String(f["name"]);
    let out: Reply | null = null;
    if (f["kind"] === "counter") out = inside(name, asNum(f["x_cm"]), asNum(f["y_cm"]), ...turned(f));
    else if (f["kind"] === "label") out = inside(String(f["text"]), asNum(f["x_cm"]), asNum(f["y_cm"]), 0, 0);
    else if (f["kind"] === "wall") {
      const rad = (asNum(f["angle"]) * Math.PI) / 180;
      const dx = (Math.cos(rad) * asNum(f["length_cm"])) / 2;
      const dy = (Math.sin(rad) * asNum(f["length_cm"])) / 2;
      const x = asNum(f["x_cm"]);
      const y = asNum(f["y_cm"]);
      out = inside(name, x - dx, y - dy, 0, 0) ?? inside(name, x + dx, y + dy, 0, 0);
    } else {
      const extent = f["side"] === "top" || f["side"] === "bottom" ? w.width_cm : w.depth_cm;
      const half = asNum(f["width_cm"]) / 2;
      const at = asNum(f["offset_cm"]);
      if (!(half - TOLERANCE_CM <= at && at <= extent - half + TOLERANCE_CM)) out = refuse("door off its wall", `${name} liegt nicht an der Wand.`);
    }
    if (out) return out;
  }
  return null;
}

// What a room holds, without its id and revision, in an order that does not matter:
// the same room sent twice (a retry) is no conflict and no change.
function content(d: Wanted): string {
  const key = (o: Obj): string => JSON.stringify(Object.keys(o).sort().map((k) => [k, o[k]]));
  return JSON.stringify([
    d.name, d.kind, d.shape, d.width_cm, d.depth_cm, d.wall_cm,
    d.tables.map(key).sort(), d.fixtures.map(key).sort(), pairs(d.combinations),
  ]);
}
const pairs = (c: [string, string][]): string[] => [...new Set(c.map(([a, b]) => (a < b ? `${a} ${b}` : `${b} ${a}`)))].sort();
const asPairs = (c: [string, string][]): [string, string][] => pairs(c).map((p) => p.split(" ") as [string, string]);

// --- The visitor's rooms ---------------------------------------------------------------

export class FloorPlan {
  private areas: Doc[];

  constructor(plan: { areas: Doc[] }) {
    this.areas = structuredClone(plan.areas);
  }

  plan(): Reply {
    return { status: 200, body: JSON.stringify({ ok: true, areas: this.areas }) };
  }

  create(raw: string): Reply {
    const n = parse(raw, (v) => {
      const o = obj(v, ["name", "kind", "width_cm", "depth_cm"]);
      return {
        name: str(o["name"], 1, 30), kind: pick(o["kind"], ["inside", "terrace"]),
        width_cm: num(o["width_cm"], 200, 5000), depth_cm: num(o["depth_cm"], 200, 5000),
      };
    });
    if (!n) return invalid();
    if (this.areas.length >= MAX_AREAS) {
      return refuse("too many floor areas", `Es gehen höchstens ${MAX_AREAS} Räume und Terrassen.`);
    }
    if (this.areas.some((a) => lower(a.name) === lower(n.name))) {
      return refuse("floor area name taken", sayNameTaken(n.name));
    }
    const made: Doc = {
      id: crypto.randomUUID(), revision: 0, ...n, shape: n.width_cm === n.depth_cm ? "square" : "rectangle",
      wall_cm: 20, tables: [], fixtures: [], combinations: [],
    };
    this.areas.push(made);
    return { status: 200, body: JSON.stringify({ ok: true, area: made }) };
  }

  save(id: string, raw: string): Reply {
    const req = parse(raw, (v) => {
      const o = obj(v, ["revision", "area"]);
      return { revision: num(o["revision"], 0, Number.MAX_SAFE_INTEGER), area: area(o["area"]) };
    });
    if (!req) return invalid();
    const at = this.areas.findIndex((a) => a.id === id.toLowerCase());
    const current = this.areas[at];
    if (!current) return fail(404, "not_found", "floor area not found", "Diesen Raum gibt es nicht mehr.");
    const wanted = req.area;
    if (req.revision !== current.revision) {
      // The same room twice (a retry after a lost answer) is no conflict.
      if (content(current) === content(wanted)) return { status: 200, body: JSON.stringify({ ok: true, area: current }) };
      return fail(
        409, "conflict", "floor area changed since its revision",
        "Ein anderes Gerät hat diesen Raum inzwischen geändert. Der aktuelle Stand ist geladen, die letzte Änderung hier ist nicht gespeichert.",
        current,
      );
    }
    const refused = check(this.areas.filter((a) => a !== current), wanted);
    if (refused) return refused;
    if (content(current) === content(wanted)) return { status: 200, body: JSON.stringify({ ok: true, area: current }) };
    // A table that stays keeps its place in the list; new ones follow by name.
    const old = current.tables.map((t) => t["id"] as string);
    const kept = old.flatMap((i) => wanted.tables.filter((t) => t["id"] === i));
    const added = wanted.tables.filter((t) => !old.includes(t["id"] as string)).sort((a, b) => (a["name"] as string).localeCompare(b["name"] as string));
    const next: Doc = { ...current, ...wanted, tables: [...kept, ...added], combinations: asPairs(wanted.combinations), revision: current.revision + 1 };
    this.areas[at] = next;
    return { status: 200, body: JSON.stringify({ ok: true, area: next }) };
  }
}
