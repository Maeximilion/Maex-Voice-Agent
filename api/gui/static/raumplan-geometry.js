// Floor plan geometry (T-3.11, docs/06 §3 "Raumplan"). Pure functions without
// the page, so node can test them (api/tests/test_gui_floor_plan_geometry.py).
// Lengths are centimetres, from the room's top left corner; angles are degrees.
// The rules are those of the approved design, docs/design/floor-plan/README.md.
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.FloorGeometry = factory();
})(this, function () {
  "use strict";

  // Editor presets, not rules about bookable capacity.
  var PRESETS = {
    rect: [[80, 80], [120, 80], [140, 80], [160, 80], [180, 90], [200, 100]],
    round: [[80, 80], [100, 100], [110, 110], [120, 120], [140, 140]]
  };
  // Pixel sizes of a free label; docs/06 §1 asks for 18 px at least.
  var LABEL_PX = { small: 18, normal: 20, large: 26, xlarge: 32 };
  var LIMITS = {
    roomMin: 200, roomMax: 5000, wallMin: 5, wallMax: 60,
    tableMin: 30, tableMax: 600, counterMin: 30, counterMax: 1000,
    seatsMin: 1, seatsMax: 40, doorMin: 60, doorMax: 300, wallLengthMin: 20
  };

  function clamp(n, min, max) { return Math.max(min, Math.min(max, n)); }
  function rad(a) { return a * Math.PI / 180; }

  // How many chairs stand on which side of a table, unrotated. Up to five
  // seats an odd one takes one end; from six on both ends stay occupied and
  // further seats alternate between the long sides (2+2+1+1, 3+2+1+1, ...).
  function seatSides(seats, shape, endSeat) {
    if (shape === "round") return { around: seats };
    var ends = seats >= 6 ? 2 : seats % 2;
    var long = seats - ends;
    return {
      top: Math.ceil(long / 2),
      bottom: Math.floor(long / 2),
      left: ends === 2 || (ends === 1 && endSeat !== "right") ? 1 : 0,
      right: ends === 2 || (ends === 1 && endSeat === "right") ? 1 : 0
    };
  }

  // The end seat is a choice only while exactly one end is occupied.
  function hasEndChoice(table) {
    return table.shape === "rect" && table.seats < 6 && table.seats % 2 === 1;
  }

  // Width and depth as drawn.
  function footprint(o) {
    var a = rad(o.angle || 0);
    return [
      Math.abs(Math.cos(a)) * o.w + Math.abs(Math.sin(a)) * o.h,
      Math.abs(Math.sin(a)) * o.w + Math.abs(Math.cos(a)) * o.h
    ];
  }

  // The text on a table or counter turns back by the piece's angle, so it
  // reads upright at every quarter turn; a counter drawn tall writes top to
  // bottom instead. The earlier label stood on its head after a turn.
  function labelTurn(o) {
    var size = footprint(o);
    return { turn: -(o.angle || 0), vertical: o.type === "counter" && size[1] > size[0] * 1.5 };
  }

  // A free label turns with its quarter turn but never stands on its head:
  // 180 reads as 0, 270 as -90 (from below). The result lies in (-90, 90].
  function readableTurn(angle) {
    var a = ((Math.round(angle || 0) % 360) + 360) % 360;
    if (a > 90 && a < 270) a -= 180;
    else if (a >= 270) a -= 360;
    return a;
  }

  // "Drehen" on a label: across, then from below (along a left wall), then from above.
  var LABEL_TURNS = [0, 270, 90];
  function nextLabelTurn(angle) {
    var i = LABEL_TURNS.indexOf((readableTurn(angle) + 360) % 360);
    return LABEL_TURNS[(i + 1) % LABEL_TURNS.length];
  }

  function endpoints(o) {
    var dx = Math.cos(rad(o.angle)) * o.length / 2;
    var dy = Math.sin(rad(o.angle)) * o.length / 2;
    return [{ x: o.x - dx, y: o.y - dy }, { x: o.x + dx, y: o.y + dy }];
  }

  function wallFromPoints(o, a, b) {
    o.x = (a.x + b.x) / 2;
    o.y = (a.y + b.y) / 2;
    o.length = Math.hypot(b.x - a.x, b.y - a.y);
    o.angle = Math.atan2(b.y - a.y, b.x - a.x) * 180 / Math.PI;
  }

  // Keep a piece inside its room; a table keeps 20 cm off the wall.
  function fit(o, r) {
    if (o.type === "door") {
      var extent = o.side === "top" || o.side === "bottom" ? r.w : r.h;
      o.width = Math.min(o.width, extent - 40);
      o.offset = clamp(o.offset, o.width / 2 + 10, extent - o.width / 2 - 10);
      return;
    }
    if (o.type === "wall") {
      var margin = o.thickness / 2 + 1;
      o.x = clamp(o.x, margin + 10, r.w - margin - 10);
      o.y = clamp(o.y, margin + 10, r.h - margin - 10);
      var c = Math.abs(Math.cos(rad(o.angle))), s = Math.abs(Math.sin(rad(o.angle)));
      var max = Math.min(c > 0.0001 ? (r.w - 2 * margin) / c : Infinity,
        s > 0.0001 ? (r.h - 2 * margin) / s : Infinity);
      o.length = clamp(o.length, LIMITS.wallLengthMin, max);
      var hw = c * o.length / 2 + margin, hh = s * o.length / 2 + margin;
      o.x = clamp(o.x, hw, r.w - hw);
      o.y = clamp(o.y, hh, r.h - hh);
      return;
    }
    var size = o.type === "label" ? [40, 30] : footprint(o);
    var pad = o.type === "table" ? 20 : 5;
    o.x = clamp(o.x, size[0] / 2 + pad, r.w - size[0] / 2 - pad);
    o.y = clamp(o.y, size[1] / 2 + pad, r.h - size[1] / 2 - pad);
    if (o.type === "table") { o.x = Math.round(o.x); o.y = Math.round(o.y); }
  }

  // Whether a table or counter fits into the room at all in its orientation.
  function available(o, r) {
    if (o.type !== "table" && o.type !== "counter") return true;
    var size = footprint(o), pad = o.type === "table" ? 40 : 10;
    return size[0] + pad <= r.w && size[1] + pad <= r.h;
  }

  function doorPoint(o, r) {
    return {
      x: o.side === "left" ? 0 : o.side === "right" ? r.w : o.offset,
      y: o.side === "top" ? 0 : o.side === "bottom" ? r.h : o.offset
    };
  }

  // An entrance follows the pointer along the nearest outer wall.
  function moveDoor(o, p, r, snap) {
    var sides = [["left", Math.abs(p.x)], ["right", Math.abs(p.x - r.w)],
      ["top", Math.abs(p.y)], ["bottom", Math.abs(p.y - r.h)]];
    sides.sort(function (a, b) { return a[1] - b[1]; });
    o.side = sides[0][0];
    o.offset = snap(o.side === "top" || o.side === "bottom" ? p.x : p.y);
    fit(o, r);
  }

  // The first free "T<n>" over every room: a copy gets a new number.
  function nextTableName(names) {
    var taken = {};
    names.forEach(function (n) { taken[String(n).toLocaleLowerCase("de-DE")] = true; });
    var n = 1;
    while (taken["t" + n]) n++;
    return "T" + n;
  }

  // Beside the source if there is room, else the first free spot on a grid.
  // A convenience only: nothing here checks clearance between pieces.
  function placeCopy(o, source, objects, r) {
    var size = footprint(o), w = size[0], h = size[1];
    var candidates = [[source.x + w + 45, source.y], [source.x, source.y + h + 45],
      [source.x - w - 45, source.y], [source.x, source.y - h - 45]];
    for (var y = h / 2 + 30; y < r.h - h / 2 - 20; y += h + 45) {
      for (var x = w / 2 + 30; x < r.w - w / 2 - 20; x += w + 45) candidates.push([x, y]);
    }
    for (var i = 0; i < candidates.length; i++) {
      var cx = candidates[i][0], cy = candidates[i][1];
      if (cx < w / 2 + 20 || cx > r.w - w / 2 - 20 || cy < h / 2 + 20 || cy > r.h - h / 2 - 20) continue;
      var free = objects.filter(function (a) { return a.type === "table" || a.type === "counter"; })
        .every(function (a) {
          var s = footprint(a);
          return Math.abs(a.x - cx) > (s[0] + w) / 2 + 25 || Math.abs(a.y - cy) > (s[1] + h) / 2 + 25;
        });
      if (free) { o.x = cx; o.y = cy; fit(o, r); return; }
    }
    o.x = source.x + 40;
    o.y = source.y + 40;
    fit(o, r);
  }

  // The server's room document (domain/reservations/floor_plan.py) to the
  // editor's state, and back.
  function toClient(doc) {
    var objects = doc.tables.map(function (t) {
      return { id: t.id, type: "table", name: t.name, shape: t.shape, seats: t.seats,
        w: t.width_cm, h: t.depth_cm, x: t.x_cm, y: t.y_cm, angle: t.rotation, endSeat: t.end_seat };
    });
    doc.fixtures.forEach(function (f) {
      if (f.kind === "counter") objects.push({ id: f.id, type: "counter", name: f.name,
        x: f.x_cm, y: f.y_cm, w: f.width_cm, h: f.depth_cm, angle: f.rotation });
      else if (f.kind === "door") objects.push({ id: f.id, type: "door", name: f.name,
        side: f.side, offset: f.offset_cm, width: f.width_cm });
      else if (f.kind === "wall") objects.push({ id: f.id, type: "wall", name: f.name,
        x: f.x_cm, y: f.y_cm, length: f.length_cm, angle: f.angle, thickness: f.thickness_cm });
      else if (f.kind === "label") objects.push({ id: f.id, type: "label", text: f.text,
        x: f.x_cm, y: f.y_cm, size: f.size, angle: f.rotation || 0 });
    });
    return {
      room: { id: doc.id, name: doc.name, kind: doc.kind, shape: doc.shape,
        w: doc.width_cm, h: doc.depth_cm, thickness: doc.wall_cm },
      objects: objects,
      links: doc.combinations.map(function (p) { return [p[0], p[1]]; }),
      revision: doc.revision
    };
  }

  function round1(n) { return Math.round(n * 10) / 10; }

  function toServer(state) {
    var r = state.room, tables = [], fixtures = [];
    state.objects.forEach(function (o) {
      if (o.type === "table") tables.push({ id: o.id, name: o.name, shape: o.shape, seats: o.seats,
        width_cm: Math.round(o.w), depth_cm: Math.round(o.h), x_cm: Math.round(o.x),
        y_cm: Math.round(o.y), rotation: ((Math.round(o.angle / 90) * 90) % 360 + 360) % 360,
        end_seat: o.endSeat === "right" ? "right" : "left" });
      else if (o.type === "counter") fixtures.push({ kind: "counter", id: o.id, name: o.name,
        x_cm: round1(o.x), y_cm: round1(o.y), width_cm: Math.round(o.w), depth_cm: Math.round(o.h),
        rotation: ((Math.round(o.angle / 90) * 90) % 360 + 360) % 360 });
      else if (o.type === "door") fixtures.push({ kind: "door", id: o.id, name: o.name, side: o.side,
        offset_cm: round1(o.offset), width_cm: Math.round(o.width) });
      else if (o.type === "wall") fixtures.push({ kind: "wall", id: o.id, name: o.name,
        x_cm: round1(o.x), y_cm: round1(o.y), length_cm: round1(o.length), angle: round1(o.angle),
        thickness_cm: Math.round(o.thickness) });
      else if (o.type === "label") fixtures.push({ kind: "label", id: o.id, text: o.text,
        x_cm: round1(o.x), y_cm: round1(o.y), size: o.size,
        rotation: ((Math.round((o.angle || 0) / 90) * 90) % 360 + 360) % 360 });
    });
    return {
      name: r.name, kind: r.kind, shape: r.shape, width_cm: Math.round(r.w),
      depth_cm: Math.round(r.h), wall_cm: Math.round(r.thickness),
      tables: tables, fixtures: fixtures,
      combinations: state.links.map(function (p) { return [p[0], p[1]]; })
    };
  }

  return {
    PRESETS: PRESETS, LABEL_PX: LABEL_PX, LIMITS: LIMITS, clamp: clamp,
    seatSides: seatSides, hasEndChoice: hasEndChoice, footprint: footprint,
    labelTurn: labelTurn, readableTurn: readableTurn, nextLabelTurn: nextLabelTurn, endpoints: endpoints, wallFromPoints: wallFromPoints,
    fit: fit, available: available, doorPoint: doorPoint, moveDoor: moveDoor,
    nextTableName: nextTableName, placeCopy: placeCopy, toClient: toClient,
    toServer: toServer
  };
});
