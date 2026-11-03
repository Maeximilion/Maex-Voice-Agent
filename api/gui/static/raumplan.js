// The floor plan editor (T-3.11, docs/06 §3 "Raumplan"), after the approved
// design gui/mockups/floor-plan-editor.html. A drag stays in the browser; each
// finished step (a drop, a field, a copy, an undo) sends the whole room to
// /gui/raumplan/bereiche/<id>, where domain/reservations/floor_plan.py checks
// and stores it. The server's answer is the truth: a refused step is undone
// here, and a room another tablet changed meanwhile is loaded as it stands.
// The page is drawn through the DOM, never through style attributes in markup:
// the proxy's Content-Security-Policy refuses those (docs/13 §3).
(function () {
  "use strict";
  var G = window.FloorGeometry;
  var root = document.getElementById("raumplan");
  if (!root || !G) return;
  var floor = root.querySelector(".re-floor");
  var stage = root.querySelector(".re-stage");
  var props = root.querySelector(".re-properties");
  var form = root.querySelector(".re-room-form");
  var L = G.LIMITS;

  var areas = [];
  var area = null;
  var selectedId = "room";
  var mode = "select";
  var drag = null, drawStart = null, preview = null, ignoreClickUntil = 0, scale = 1;
  var ui = { grid: true, advanced: false };

  var TITLES = { table: "Tisch", counter: "Theke", door: "Eingang", wall: "Wand", label: "Beschriftung" };
  var NOT_SAVED = "Nicht gespeichert: keine Verbindung zum Server. Die Änderung bleibt auf diesem Gerät.";

  function fmt(n, d) { return new Intl.NumberFormat("de-DE", { maximumFractionDigits: d === undefined ? 2 : d }).format(n); }
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function snap(n) { return ui.grid ? Math.round(n / 10) * 10 : Math.round(n); }
  function selected() { return area && area.objects.find(function (o) { return o.id === selectedId; }); }
  function label(o) { return o.type === "label" ? o.text : (o.type === "table" ? "Tisch " : "") + o.name; }
  function plural(n) { return n + " " + (n === 1 ? "Platz" : "Plätze"); }
  function clone(v) { return JSON.parse(JSON.stringify(v)); }
  function content(a) { return JSON.stringify({ room: a.room, objects: a.objects, links: a.links }); }

  function newId() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    // Outside a secure context randomUUID is missing; the same version 4 by hand.
    var b = window.crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    var h = Array.prototype.map.call(b, function (x) { return (x + 256).toString(16).slice(1); }).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  }

  function node(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // ---- talking to the server -------------------------------------------------

  function send(method, url, body) {
    var m = document.cookie.match(/(?:^|; )csrf_token=([^;]*)/);
    return fetch(url, {
      method: method,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "HX-Request": "true", "X-CSRF-Token": m ? m[1] : "" },
      body: body === undefined ? undefined : JSON.stringify(body)
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) { return { status: res.status, data: data }; });
    });
  }

  function fromDoc(doc) {
    var s = G.toClient(doc);
    return { room: s.room, objects: s.objects, links: s.links, revision: s.revision, saved: doc,
      history: [], saving: false, again: false, unsaved: false };
  }

  function adopt(target, doc) {
    var fresh = fromDoc(doc);
    ["room", "objects", "links", "revision", "saved"].forEach(function (k) { target[k] = fresh[k]; });
  }

  function save(target) {
    if (target.saving) { target.again = true; return; }
    target.saving = true;
    var sentContent = content(target);
    send("PUT", "/gui/raumplan/bereiche/" + target.room.id, { revision: target.revision, area: G.toServer(target) })
      .then(function (res) {
        var say = res.data && res.data.say;
        if (res.status === 200) {
          target.revision = res.data.area.revision;
          target.saved = res.data.area;
          target.unsaved = false;
          if (!target.again && target === area) status("Gespeichert.");
        } else if (res.status === 409 && res.data.area) {
          adopt(target, res.data.area);
          target.history = [];
          target.again = false;
          status(say || "Ein anderes Gerät hat diesen Raum geändert.", true);
        } else if (res.status >= 400 && res.status < 500) {
          // Refused: back to what the server holds, the step is not kept.
          adopt(target, target.saved);
          var top = target.history[target.history.length - 1];
          if (top && top === content(target)) target.history.pop();
          target.again = false;
          status(say || "Das hat nicht geklappt, nichts gespeichert.", true);
        } else {
          target.unsaved = true;
          status(NOT_SAVED, true, true);
        }
        if (target === area) { if (!find(selectedId)) selectedId = "room"; render(); }
      }, function () {
        target.unsaved = true;
        if (content(target) !== sentContent) target.again = true;
        status(NOT_SAVED, true, true);
      })
      .catch(function (err) {
        // A fault in this page, not on the line: keep the step and say so.
        target.unsaved = true;
        status(NOT_SAVED, true, true);
        if (window.console) window.console.error(err);
      })
      .then(function () {
        target.saving = false;
        if (target.again) { target.again = false; save(target); }
      });
  }

  function find(id) { return area && area.objects.find(function (o) { return o.id === id; }); }

  function status(text, error, retry) {
    var el = root.querySelector(".re-status");
    el.textContent = text;
    el.classList.toggle("re-error", !!error);
    if (retry) {
      var b = node("button", "re-btn", "Nochmal speichern");
      b.type = "button";
      b.dataset.action = "retry";
      el.append(b);
    }
  }

  window.addEventListener("beforeunload", function (e) {
    if (areas.some(function (a) { return a.saving || a.unsaved; })) e.preventDefault();
  });

  // ---- editing ---------------------------------------------------------------

  function allTableNames() {
    var names = [];
    areas.forEach(function (a) { a.objects.forEach(function (o) { if (o.type === "table") names.push(o.name); }); });
    return names;
  }

  // One finished step: remember the state before it for undo, then save.
  function commit(message, before) {
    if (before !== undefined) {
      area.history.push(before);
      if (area.history.length > 30) area.history.shift();
    }
    render();
    status(message + " · speichert ...");
    save(area);
  }

  function activate(target) {
    area = target;
    selectedId = "room";
    mode = "select";
    drawStart = null;
    preview = null;
    render();
  }

  function add(type) {
    var before = content(area), r = area.room, id = newId();
    var x = r.w * 0.5 + ((area.objects.length % 3) - 1) * 60, y = r.h * 0.5, o;
    if (type === "table") o = { id: id, type: type, name: G.nextTableName(allTableNames()), x: x, y: y, w: 120, h: 80, angle: 0, shape: "rect", seats: 4, endSeat: "left" };
    if (type === "counter") o = { id: id, type: type, name: "Theke", x: x, y: y, w: 80, h: 220, angle: 0 };
    if (type === "door") o = { id: id, type: type, name: "Eingang", side: "bottom", offset: r.w * 0.7, width: 100 };
    if (type === "label") o = { id: id, type: type, text: "Beschriftung", x: x, y: y, size: "normal" };
    if (!G.available(o, r)) { status("Für dieses Objekt ist der Raum zu klein.", true); return; }
    G.fit(o, r);
    area.objects.push(o);
    selectedId = id;
    mode = "select";
    drawStart = null;
    preview = null;
    commit(TITLES[type] + " hinzugefügt", before);
    if (type === "label") { var input = props.querySelector('[data-field="text"]'); if (input) { input.focus(); input.select(); } }
  }

  function finishWall(a, b) {
    if (Math.hypot(b.x - a.x, b.y - a.y) < L.wallLengthMin) { status("Die Wand muss mindestens 20 cm lang sein.", true); return; }
    var before = content(area), o = { id: newId(), type: "wall", name: "Wand", thickness: 20 };
    G.wallFromPoints(o, a, b);
    G.fit(o, area.room);
    area.objects.push(o);
    selectedId = o.id;
    drawStart = null;
    preview = null;
    mode = "select";
    commit("Wand gezeichnet", before);
  }

  function cloneTable() {
    var source = selected();
    if (!source || source.type !== "table") return;
    var before = content(area), copy = clone(source);
    copy.id = newId();
    copy.name = G.nextTableName(allTableNames());
    G.placeCopy(copy, source, area.objects, area.room);
    area.objects.push(copy);
    selectedId = copy.id;
    commit("Tisch " + source.name + " als " + copy.name + " kopiert", before);
  }

  function remove(o) {
    var what = o.type === "label" ? "Beschriftung „" + o.text + "“" : TITLES[o.type] + " „" + o.name + "“";
    if (!window.confirm(what + " entfernen?")) return;
    var before = content(area);
    area.objects = area.objects.filter(function (x) { return x.id !== o.id; });
    area.links = area.links.filter(function (p) { return p.indexOf(o.id) < 0; });
    selectedId = "room";
    commit(what + " entfernt", before);
  }

  function undo() {
    if (!area || !area.history.length) return;
    var s = JSON.parse(area.history.pop());
    area.room = s.room; area.objects = s.objects; area.links = s.links;
    mode = "select"; drawStart = null; preview = null;
    if (!find(selectedId)) selectedId = "room";
    render();
    status("Letzte Änderung rückgängig gemacht · speichert ...");
    save(area);
  }

  function createRoom() {
    if (!form.reportValidity()) return;
    var name = form.elements.areaName.value.trim();
    if (!name) { form.elements.areaName.focus(); return; }
    var taken = areas.some(function (a) { return a.room.name.toLocaleLowerCase("de-DE") === name.toLocaleLowerCase("de-DE"); });
    if (taken) { status("Dieser Raumname ist schon vergeben. Bitte einen anderen Namen wählen.", true); return; }
    var body = { name: name, kind: form.elements.areaKind.value,
      width_cm: Math.round(Number(form.elements.areaWidth.value) * 100),
      depth_cm: Math.round(Number(form.elements.areaHeight.value) * 100) };
    status("Wird erstellt ...");
    send("POST", "/gui/raumplan/bereiche", body).then(function (res) {
      if (res.status !== 200) { status((res.data && res.data.say) || "Das hat nicht geklappt.", true); return; }
      var created = fromDoc(res.data.area);
      areas.push(created);
      form.hidden = true;
      activate(created);
      status(name + " erstellt.");
    }, function () { status("Nicht erstellt: keine Verbindung zum Server.", true); });
  }

  // ---- drawing ---------------------------------------------------------------

  function chairs(wrap, o) {
    var cw = G.clamp(24 * scale, 6, 17), ch = G.clamp(10 * scale, 3, 7), w = o.w * scale, h = o.h * scale;
    function chair(x, y, a) {
      var c = node("span", "re-chair");
      c.setAttribute("aria-hidden", "true");
      c.style.left = x + "px"; c.style.top = y + "px"; c.style.width = cw + "px"; c.style.height = ch + "px";
      c.style.setProperty("--chair-angle", (a || 0) + "deg");
      wrap.append(c);
    }
    var s = G.seatSides(o.seats, o.shape, o.endSeat), i;
    if (s.around !== undefined) {
      for (i = 0; i < s.around; i++) {
        var a = 2 * Math.PI * i / s.around - Math.PI / 2;
        chair(w / 2 + (w / 2 + ch + 2) * Math.cos(a), h / 2 + (h / 2 + ch + 2) * Math.sin(a), a * 180 / Math.PI + 90);
      }
      return;
    }
    for (i = 0; i < s.top; i++) chair(w * (i + 1) / (s.top + 1), -ch - 2);
    for (i = 0; i < s.bottom; i++) chair(w * (i + 1) / (s.bottom + 1), h + ch + 2);
    if (s.left) chair(-ch - 2, h / 2, 90);
    if (s.right) chair(w + ch + 2, h / 2, 90);
  }

  function place(wrap, x, y) { wrap.style.left = x * scale + "px"; wrap.style.top = y * scale + "px"; }

  // Keep a button that sticks out of its point inside the plan.
  function keepInside(button, x, y) {
    var w = button.offsetWidth, h = button.offsetHeight;
    var bx = x + (parseFloat(button.style.left) || 0), by = y + (parseFloat(button.style.top) || 0);
    button.style.left = G.clamp(bx, w / 2 + 3, area.room.w * scale - w / 2 - 3) - x + "px";
    button.style.top = G.clamp(by, h / 2 + 3, area.room.h * scale - h / 2 - 3) - y + "px";
  }

  function drawObject(o) {
    var active = o.id === selectedId, wrap = node("div", "re-object"), b;
    wrap.dataset.id = o.id;
    if (active) wrap.classList.add("re-selected");
    if (o.type === "table" || o.type === "counter") {
      wrap.classList.add("re-furniture");
      if (o.type === "counter") wrap.classList.add("re-counter");
      if (o.shape === "round") wrap.classList.add("re-round");
      place(wrap, o.x, o.y);
      wrap.style.width = o.w * scale + "px";
      wrap.style.height = o.h * scale + "px";
      wrap.style.setProperty("--re-angle", o.angle + "deg");
      if (o.type === "table") chairs(wrap, o);
      b = node("button", "re-piece");
      b.type = "button";
      b.setAttribute("aria-label", label(o) + (o.type === "table" ? ", " + plural(o.seats) : ""));
      b.setAttribute("aria-pressed", String(active));
      var turn = G.labelTurn(o), text = node("span", "re-piece-text");
      text.style.setProperty("--re-text-turn", turn.turn + "deg");
      if (turn.vertical) text.classList.add("re-vertical");
      text.append(node("span", "re-piece-name", o.name));
      var seats = o.type === "table" ? node("span", "re-piece-seats", plural(o.seats)) : null;
      if (seats) text.append(seats);
      b.append(text);
      wrap.append(b);
      floor.append(wrap);
      if (seats) {
        // 18 px text does not fit every small table: the name stays, the count is in the label.
        var outer = b.getBoundingClientRect(), nr = text.firstChild.getBoundingClientRect(), sr = seats.getBoundingClientRect();
        if (Math.max(nr.width, sr.width) > outer.width - 8 || nr.height + sr.height > outer.height - 4) seats.hidden = true;
      }
    } else if (o.type === "wall") {
      wrap.classList.add("re-wall-object");
      place(wrap, o.x, o.y);
      wrap.style.width = o.length * scale + "px";
      wrap.style.height = Math.max(60, o.thickness * scale) + "px";
      wrap.style.setProperty("--re-angle", o.angle + "deg");
      wrap.style.setProperty("--re-thickness", Math.max(3, o.thickness * scale) + "px");
      b = node("button", "re-wall-hit");
      b.type = "button";
      b.setAttribute("aria-label", o.name + ", " + fmt(o.length / 100) + " Meter");
      b.setAttribute("aria-pressed", String(active));
      b.append(node("span", "re-wall-bar"));
      wrap.append(b);
      if (active) wrap.append(node("span", "re-wall-measure", fmt(o.length / 100) + " m · " + fmt(o.thickness) + " cm"));
      floor.append(wrap);
    } else if (o.type === "door") {
      var p = G.doorPoint(o, area.room), side = o.side === "left" || o.side === "right";
      var t = Math.max(4, area.room.thickness * scale) + 6;
      wrap.classList.add("re-door");
      if (side) wrap.classList.add("re-side");
      place(wrap, p.x, p.y);
      var gap = node("span", "re-door-gap");
      gap.style.width = (side ? t : o.width * scale) + "px";
      gap.style.height = (side ? o.width * scale : t) + "px";
      var arrow = { left: "→", right: "←", top: "↓", bottom: "↑" }[o.side];
      b = node("button", "re-door-button", arrow + " " + o.name);
      b.type = "button";
      b.style.left = (o.side === "left" ? 64 : o.side === "right" ? -64 : 0) + "px";
      b.style.top = (o.side === "top" ? 30 : o.side === "bottom" ? -30 : 0) + "px";
      b.setAttribute("aria-label", o.name + ", " + fmt(o.width) + " Zentimeter breit");
      b.setAttribute("aria-pressed", String(active));
      wrap.append(gap, b);
      floor.append(wrap);
      keepInside(b, p.x * scale, p.y * scale);
    } else {
      wrap.classList.add("re-label");
      b = node("button", "re-label-button", o.text);
      b.type = "button";
      b.style.fontSize = (G.LABEL_PX[o.size] || 20) + "px";
      b.setAttribute("aria-label", "Beschriftung: " + o.text);
      b.setAttribute("aria-pressed", String(active));
      wrap.append(b);
      floor.append(wrap);
      var lw = b.offsetWidth, lh = b.offsetHeight;
      wrap.style.left = G.clamp(o.x * scale, lw / 2 + 3, area.room.w * scale - lw / 2 - 3) + "px";
      wrap.style.top = G.clamp(o.y * scale, lh / 2 + 3, area.room.h * scale - lh / 2 - 3) + "px";
    }
    if (active && o.type === "wall") {
      G.endpoints(o).forEach(function (pt, index) {
        var e = node("button", "re-endpoint");
        e.type = "button";
        e.dataset.endpoint = index;
        e.dataset.id = o.id;
        e.setAttribute("aria-label", "Wand-Endpunkt " + (index + 1));
        e.style.left = pt.x * scale + "px";
        e.style.top = pt.y * scale + "px";
        floor.append(e);
      });
    }
  }

  function renderScene() {
    var focused = document.activeElement;
    var focusId = focused && focused.closest && focused.closest(".re-object") ? focused.closest(".re-object").dataset.id : null;
    var focusEndpoint = focused && focused.dataset ? focused.dataset.endpoint : undefined;
    var hadFocus = floor.contains(focused);
    floor.querySelectorAll(".re-object,.re-endpoint,.re-preview,.re-room-empty").forEach(function (e) { e.remove(); });
    if (!area) return;
    var r = area.room;
    floor.classList.toggle("re-grid", ui.grid);
    floor.classList.toggle("re-drawing", mode === "wall");
    floor.classList.toggle("re-terrace", r.kind === "terrace");
    floor.style.backgroundSize = 10 * scale + "px " + 10 * scale + "px";
    var t = Math.max(2, r.thickness * scale), w = r.w * scale, h = r.h * scale;
    ["top", "bottom", "left", "right"].forEach(function (s) {
      var e = floor.querySelector('[data-side="' + s + '"]'), horizontal = s === "top" || s === "bottom";
      e.style.left = (s === "right" ? w - t / 2 : -t / 2) + "px";
      e.style.top = (s === "bottom" ? h - t / 2 : -t / 2) + "px";
      e.style.width = (horizontal ? w + t : t) + "px";
      e.style.height = (horizontal ? t : h + t) + "px";
    });
    floor.querySelector(".re-dimension-x").textContent = fmt(r.w / 100) + " m";
    floor.querySelector(".re-dimension-y").textContent = fmt(r.h / 100) + " m";
    var svg = floor.querySelector(".re-connections");
    svg.setAttribute("viewBox", "0 0 " + w + " " + h);
    svg.replaceChildren();
    area.links.forEach(function (pair) {
      var a = find(pair[0]), b = find(pair[1]);
      if (!a || !b) return;
      var line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", a.x * scale); line.setAttribute("y1", a.y * scale);
      line.setAttribute("x2", b.x * scale); line.setAttribute("y2", b.y * scale);
      svg.append(line);
    });
    ["wall", "counter", "table", "label", "door"].forEach(function (type) {
      area.objects.filter(function (o) { return o.type === type; }).forEach(drawObject);
    });
    if (!area.objects.length) floor.append(node("div", "re-room-empty", r.name + " · noch leer"));
    if (preview) {
      var pv = node("div", "re-preview re-wall-object"), o = {};
      G.wallFromPoints(o, preview.a, preview.b);
      pv.style.left = o.x * scale + "px"; pv.style.top = o.y * scale + "px";
      pv.style.width = o.length * scale + "px"; pv.style.height = Math.max(3, 20 * scale) + "px";
      pv.style.setProperty("--re-angle", o.angle + "deg");
      floor.append(pv);
    }
    if (hadFocus) {
      var target = focusEndpoint !== undefined ? floor.querySelector('[data-endpoint="' + focusEndpoint + '"]')
        : focusId ? floor.querySelector('[data-id="' + focusId + '"] button') : null;
      if (target) target.focus({ preventScroll: true });
    }
  }

  function layout() {
    if (!area) return;
    var r = area.room;
    var height = Math.min(560, Math.max(280, (stage.clientWidth - 90) * r.h / r.w + 100));
    stage.style.height = height + "px";
    var box = stage.getBoundingClientRect();
    scale = Math.max(0.01, Math.min((box.width - 90) / r.w, (box.height - 100) / r.h));
    floor.style.width = r.w * scale + "px";
    floor.style.height = r.h * scale + "px";
    renderScene();
  }

  function numberField(name, title, value, min, max, step, full) {
    return '<label class="re-field ' + (full ? "re-full" : "") + '"><span>' + title + '</span><input class="re-input" type="number" data-field="' + name + '" value="' + value + '" min="' + min + '" max="' + max + '" step="' + (step || 1) + '"></label>';
  }
  function textField(name, title, value, max) {
    return '<label class="re-field re-full"><span>' + title + '</span><input class="re-input" type="text" data-field="' + name + '" value="' + esc(value) + '" maxlength="' + max + '"></label>';
  }
  function selectField(name, title, value, entries, half) {
    return '<label class="re-field ' + (half ? "" : "re-full") + '"><span>' + title + '</span><select class="re-input" data-field="' + name + '">' +
      entries.map(function (e) { return '<option value="' + e[0] + '"' + (String(e[0]) === String(value) ? " selected" : "") + ">" + e[1] + "</option>"; }).join("") + "</select></label>";
  }

  function inspector() {
    var oldFocus = props.contains(document.activeElement) ? document.activeElement.dataset.field : null;
    var comboOpen = props.querySelector(".re-combos") && props.querySelector(".re-combos").open;
    var selector = root.querySelector('[data-field="selection"]');
    if (!area) { selector.replaceChildren(); props.replaceChildren(); return; }
    selector.innerHTML = '<option value="room">' + esc(area.room.name) + "</option>" + area.objects.map(function (o) {
      return '<option value="' + o.id + '">' + esc(o.type === "label" ? "Text: " + o.text : label(o)) + "</option>";
    }).join("");
    selector.value = selectedId;
    var html = "", extra = "", r = area.room, o = selected(), title = r.kind === "terrace" ? "Terrasse" : "Raum";
    if (!o) {
      html = textField("roomName", "Raumname", r.name, 30) +
        selectField("roomKind", "Bereich", r.kind, [["inside", "Innenraum"], ["terrace", "Terrasse"]]) +
        selectField("roomShape", "Grundform", r.shape, [["rectangle", "Rechteck"], ["square", "Quadrat"]]) +
        numberField("roomW", "Breite (m)", r.w / 100, L.roomMin / 100, L.roomMax / 100, 0.1) +
        numberField("roomH", "Tiefe (m)", r.h / 100, L.roomMin / 100, L.roomMax / 100, 0.1) +
        (r.kind === "inside" ? numberField("roomThickness", "Außenwand (cm)", r.thickness, L.wallMin, L.wallMax, 1, true) : "");
      extra = '<p class="re-meta">' + fmt(r.w * r.h / 10000) + " m² Grundfläche" + (r.shape === "square" ? " · Seitenlängen gekoppelt" : "") + "</p>";
    } else {
      title = TITLES[o.type];
      html = o.type === "label" ? textField("text", "Text", o.text, 60) : textField("name", "Bezeichnung", o.name, 25);
      if (o.type === "table") {
        html += '<div class="re-field re-full"><span>Sitzplätze · Einerschritte</span><div class="re-stepper">' +
          '<button type="button" class="re-btn" data-action="minus" aria-label="Ein Sitzplatz weniger"' + (o.seats <= L.seatsMin ? " disabled" : "") + ">−</button>" +
          '<input type="number" data-field="seats" value="' + o.seats + '" min="' + L.seatsMin + '" max="' + L.seatsMax + '" step="1" aria-label="Sitzplätze">' +
          '<button type="button" class="re-btn" data-action="plus" aria-label="Ein Sitzplatz mehr"' + (o.seats >= L.seatsMax ? " disabled" : "") + ">+</button></div></div>";
        html += selectField("shape", "Tischform", o.shape, [["rect", "Rechteckig"], ["round", "Rund"]]);
        var presets = G.PRESETS[o.shape];
        var match = presets.some(function (p) { return p[0] === o.w && p[1] === o.h; });
        var choices = presets.map(function (p) { return [p[0] + "x" + p[1], o.shape === "round" ? "Ø " + p[0] + " cm" : p[0] + " × " + p[1] + " cm"]; });
        if (!match) choices.unshift(["custom", "Individuell: " + (o.shape === "round" ? "Ø " + o.w : o.w + " × " + o.h) + " cm"]);
        html += selectField("preset", "Tischgröße", match ? o.w + "x" + o.h : "custom", choices);
        if (G.hasEndChoice(o)) html += selectField("endSeat", "Zusatzplatz an der Stirnseite", o.endSeat || "left", [["left", "Links"], ["right", "Rechts"]]);
        html += '<label class="re-advanced re-full"><span>Erweiterter Modus<small>Individuelle Tischmaße</small></span><input type="checkbox" role="switch" data-field="advanced"' + (ui.advanced ? " checked" : "") + "></label>";
        if (!ui.advanced && !match) html += '<p class="re-custom-size re-full">Individuelle Maße bleiben erhalten und sind gesperrt.</p>';
      }
      if (o.type === "table" || o.type === "counter") {
        var min = o.type === "table" ? L.tableMin : L.counterMin, max = o.type === "table" ? L.tableMax : L.counterMax;
        if (o.type === "counter" || ui.advanced) {
          html += numberField("w", o.shape === "round" ? "Durchmesser (cm)" : "Breite (cm)", o.w, min, max, 1, o.shape === "round");
          if (o.shape !== "round") html += numberField("h", "Tiefe (cm)", o.h, min, max, 1);
        }
        if (o.type === "table") html += '<div class="re-table-actions re-full">' + (o.shape !== "round" ? '<button type="button" class="re-btn" data-action="rotate">90° drehen</button>' : "") + '<button type="button" class="re-btn" data-action="clone">Tisch kopieren</button></div>';
        else html += '<button type="button" class="re-btn re-full" data-action="rotate">Um 90° drehen</button>';
      }
      if (o.type === "table") {
        var partners = area.objects.filter(function (x) { return x.type === "table" && x.id !== o.id; });
        extra = '<details class="re-combos"' + (comboOpen ? " open" : "") + '><summary>Tische kombinieren</summary><div class="re-combo-buttons">' +
          (partners.length ? partners.map(function (x) {
            var on = area.links.some(function (p) { return p.indexOf(x.id) >= 0 && p.indexOf(o.id) >= 0; });
            return '<button class="re-btn" type="button" data-partner="' + x.id + '" aria-pressed="' + on + '">' + esc(x.name) + "</button>";
          }).join("") : '<p class="re-meta">Noch kein anderer Tisch in diesem Raum.</p>') + "</div></details>";
      }
      if (o.type === "door") {
        var horizontal = o.side === "top" || o.side === "bottom";
        html += selectField("side", "Außenwand", o.side, [["top", "Oben"], ["right", "Rechts"], ["bottom", "Unten"], ["left", "Links"]]) +
          numberField("doorWidth", "Breite (cm)", o.width, L.doorMin, L.doorMax, 5, true) +
          numberField("offset", horizontal ? "Mitte ab linker Ecke (cm)" : "Mitte ab oberer Ecke (cm)", Math.round(o.offset), 0, horizontal ? r.w : r.h, 1, true);
      }
      if (o.type === "wall") {
        html += numberField("length", "Länge (m)", Number((o.length / 100).toFixed(2)), L.wallLengthMin / 100, Math.ceil(Math.hypot(r.w, r.h)) / 100, 0.01) +
          numberField("thickness", "Dicke (cm)", o.thickness, L.wallMin, L.wallMax, 1) +
          numberField("angle", "Winkel (°)", Number(o.angle.toFixed(1)), -180, 180, 0.1, true);
      }
      if (o.type === "label") html += selectField("size", "Schriftgröße", o.size, [["small", "Klein"], ["normal", "Normal"], ["large", "Groß"], ["xlarge", "Sehr groß"]]);
      extra += '<button class="re-btn re-delete" type="button" data-action="delete">' + title + " entfernen</button>";
    }
    props.innerHTML = "<h3>" + title + '</h3><div class="re-field-grid">' + html + "</div>" + extra;
    if (oldFocus) { var f = props.querySelector('[data-field="' + oldFocus + '"]'); if (f) f.focus({ preventScroll: true }); }
  }

  function controls() {
    var nav = root.querySelector(".re-room-tabs");
    var focusedRoom = nav.contains(document.activeElement) ? document.activeElement.dataset.roomId : null;
    nav.innerHTML = areas.map(function (a) {
      var tag = a.room.kind === "terrace" && !/terrasse/i.test(a.room.name) ? '<span class="re-area-tag">Terrasse</span>' : "";
      return '<button class="re-btn re-room-tab" type="button" data-room-id="' + a.room.id + '"' + (a === area ? ' aria-current="page"' : "") + ">" + esc(a.room.name) + tag + "</button>";
    }).join("");
    if (focusedRoom) { var f = nav.querySelector('[data-room-id="' + focusedRoom + '"]'); if (f) f.focus({ preventScroll: true }); }
    root.querySelectorAll("[data-tool]").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.tool === mode)); b.disabled = !area; });
    root.querySelectorAll("[data-add]").forEach(function (b) { b.disabled = !area; });
    root.querySelector('[data-field="grid"]').checked = ui.grid;
    root.querySelector('[data-action="undo"]').disabled = !area || !area.history.length;
    var summary = root.querySelector(".re-room-value"), total = root.querySelector(".re-total");
    if (!area) { summary.textContent = ""; total.textContent = ""; return; }
    var r = area.room, tables = area.objects.filter(function (o) { return o.type === "table"; });
    summary.textContent = fmt(r.w / 100) + " × " + fmt(r.h / 100) + " m · " + (r.kind === "terrace" ? "Terrasse" : "Raum") + " anpassen";
    total.textContent = r.name + " · " + tables.length + " Tische · " + plural(tables.reduce(function (n, o) { return n + o.seats; }, 0)) + " · " + fmt(r.w * r.h / 10000) + " m²";
    root.querySelector(".re-hint").textContent = mode === "wall" ? "Zwei Punkte setzen oder ziehen · Esc bricht ab" : "Objekte ziehen · Wände an den Endpunkten ändern · Pfeiltasten verschieben";
  }

  function render() { controls(); inspector(); layout(); }

  // ---- events ----------------------------------------------------------------

  function openForm() {
    form.hidden = false;
    if (areas.length) {
      form.elements.areaName.value = form.elements.areaKind.value === "terrace" ? "Terrasse" : "Nebenraum";
    }
    form.elements.areaName.focus();
    form.elements.areaName.select();
  }

  root.addEventListener("click", function (event) {
    if (Date.now() < ignoreClickUntil && floor.contains(event.target)) return;
    var b = event.target.closest("button");
    if (!b || b.disabled) return;
    if (b.dataset.roomId) {
      var target = areas.find(function (a) { return a.room.id === b.dataset.roomId; });
      if (target) { form.hidden = true; activate(target); status(target.room.name + " geöffnet."); }
      return;
    }
    if (b.dataset.tool) { mode = b.dataset.tool; drawStart = null; preview = null; controls(); renderScene(); return; }
    if (b.dataset.add) { add(b.dataset.add); return; }
    var action = b.dataset.action, o = selected();
    if (b.dataset.partner && o) {
      var before = content(area), id = b.dataset.partner;
      var index = area.links.findIndex(function (p) { return p.indexOf(o.id) >= 0 && p.indexOf(id) >= 0; });
      if (index < 0) area.links.push([o.id, id]); else area.links.splice(index, 1);
      commit("Kombination angepasst", before);
      return;
    }
    if (action === "newRoom") { openForm(); return; }
    if (action === "cancelRoom") { form.hidden = true; return; }
    if (action === "retry") { areas.forEach(function (a) { if (a.unsaved) save(a); }); status("Speichert ..."); return; }
    if (action === "undo") { undo(); return; }
    if (action === "room") { selectedId = "room"; mode = "select"; render(); return; }
    if (action === "clone") { cloneTable(); return; }
    if (action === "delete" && o) { remove(o); return; }
    if (action && o) {
      var prior = content(area);
      if (action === "plus") o.seats = Math.min(L.seatsMax, o.seats + 1);
      if (action === "minus") o.seats = Math.max(L.seatsMin, o.seats - 1);
      if (action === "rotate") {
        o.angle = (o.angle + 90) % 360;
        if (!G.available(o, area.room)) { o.angle = (o.angle + 270) % 360; status("Für diese Ausrichtung ist der Raum zu klein.", true); return; }
        G.fit(o, area.room);
      }
      commit(action === "rotate" ? TITLES[o.type] + " gedreht" : "Sitzplätze: " + o.seats, prior);
      return;
    }
    if (floor.contains(b) && mode === "select") {
      var holder = b.closest("[data-id]");
      if (holder) { selectedId = holder.dataset.id; render(); }
    }
  });

  form.addEventListener("submit", function (event) { event.preventDefault(); createRoom(); });
  form.elements.areaKind.addEventListener("change", function (event) {
    var field = form.elements.areaName;
    if (["Gastraum", "Nebenraum", "Terrasse"].indexOf(field.value) >= 0) {
      field.value = event.target.value === "terrace" ? "Terrasse" : areas.length ? "Nebenraum" : "Gastraum";
    }
  });

  root.addEventListener("change", function (event) {
    var field = event.target.dataset.field;
    if (!field || !area && field !== "grid") return;
    if (field === "selection") { selectedId = event.target.value; mode = "select"; render(); return; }
    if (field === "grid") { ui.grid = event.target.checked; renderScene(); return; }
    if (field === "advanced") {
      ui.advanced = event.target.checked;
      inspector();
      status(ui.advanced ? "Individuelle Tischmaße freigeschaltet." : "Individuelle Tischmaße gesperrt.");
      return;
    }
    var before = content(area), o = selected(), r = area.room, raw = event.target.value, n = Number(raw);
    if (event.target.type === "number" && (raw.trim() === "" || !Number.isFinite(n) || !event.target.checkValidity())) {
      render();
      status("Bitte einen gültigen Wert innerhalb des angezeigten Bereichs eingeben.", true);
      return;
    }
    if (field === "roomName") {
      var name = raw.trim().slice(0, 30);
      var taken = areas.some(function (a) { return a !== area && a.room.name.toLocaleLowerCase("de-DE") === name.toLocaleLowerCase("de-DE"); });
      if (!name || taken) { render(); status("Bitte einen freien Raumnamen eingeben.", true); return; }
      r.name = name;
    } else if (field === "roomKind") r.kind = raw;
    else if (field === "roomShape") { r.shape = raw; if (raw === "square") r.h = r.w; }
    else if (field === "roomW") { r.w = Math.round(n * 100); if (r.shape === "square") r.h = r.w; }
    else if (field === "roomH") { r.h = Math.round(n * 100); if (r.shape === "square") r.w = r.h; }
    else if (field === "roomThickness") r.thickness = n;
    else if (o) {
      if (field === "name") {
        var newName = raw.trim().slice(0, 25);
        var clash = o.type === "table" && allTableNames().some(function (x) { return x !== o.name && x.toLocaleLowerCase("de-DE") === newName.toLocaleLowerCase("de-DE"); });
        if (!newName || clash) { render(); status(clash ? "Den Tischnamen " + newName + " gibt es schon. Bitte einen anderen wählen." : "Bitte einen Namen eingeben.", true); return; }
        o.name = newName;
      }
      if (field === "text") o.text = raw.trim().slice(0, 60) || "Beschriftung";
      if (field === "seats") o.seats = G.clamp(Math.round(n), L.seatsMin, L.seatsMax);
      if (field === "size") o.size = raw;
      if (field === "shape") { o.shape = raw; o.w = 120; o.h = raw === "round" ? 120 : 80; o.angle = 0; }
      if (field === "preset") { var size = G.PRESETS[o.shape].find(function (p) { return p[0] + "x" + p[1] === raw; }); if (size) { o.w = size[0]; o.h = size[1]; } }
      if (field === "endSeat") o.endSeat = raw;
      if (field === "w" && (o.type !== "table" || ui.advanced)) { o.w = Math.round(n); if (o.shape === "round") o.h = o.w; }
      if (field === "h" && (o.type !== "table" || ui.advanced)) o.h = Math.round(n);
      if (field === "length") o.length = n * 100;
      if (field === "thickness") o.thickness = n;
      if (field === "angle") o.angle = n;
      if (field === "side") o.side = raw;
      if (field === "doorWidth") o.width = n;
      if (field === "offset") o.offset = n;
    }
    var tooLarge = area.objects.find(function (x) { return !G.available(x, area.room); });
    if (tooLarge) {
      var back = JSON.parse(before);
      area.room = back.room; area.objects = back.objects; area.links = back.links;
      render();
      status("Raum oder Möbelmaß passt nicht: " + label(tooLarge) + " ist dafür zu groß.", true);
      return;
    }
    var moved = 0;
    area.objects.forEach(function (x) { var old = JSON.stringify(x); G.fit(x, area.room); if (JSON.stringify(x) !== old) moved++; });
    commit(field.indexOf("room") === 0 ? "Raum angepasst" + (moved ? " · " + moved + " Objekte an den Raum angepasst" : "") : "Eigenschaften angepasst", before);
  });

  function point(event) {
    var b = floor.getBoundingClientRect();
    return { x: G.clamp(snap((event.clientX - b.left) / scale), 0, area.room.w), y: G.clamp(snap((event.clientY - b.top) / scale), 0, area.room.h) };
  }

  floor.addEventListener("pointerdown", function (event) {
    if (!area || event.button !== 0 || !event.isPrimary) return;
    var p = point(event);
    if (mode === "wall") {
      event.preventDefault();
      drag = { type: "draw", pointerId: event.pointerId, start: drawStart || p, wasStarted: !!drawStart, startScreen: { x: event.clientX, y: event.clientY } };
      drawStart = drag.start;
      preview = { a: drawStart, b: p };
      floor.setPointerCapture(event.pointerId);
      renderScene();
      return;
    }
    var holder = event.target.closest("[data-id]");
    if (!holder) { if (selectedId !== "room") { selectedId = "room"; inspector(); renderScene(); } return; }
    var o = find(holder.dataset.id);
    if (!o) return;
    event.preventDefault();
    selectedId = o.id;
    var endpoint = event.target.closest("[data-endpoint]");
    drag = { type: endpoint ? "endpoint" : "move", endpoint: endpoint ? Number(endpoint.dataset.endpoint) : null, pointerId: event.pointerId,
      before: content(area), original: clone(o), id: o.id, start: p, moved: false, startScreen: { x: event.clientX, y: event.clientY } };
    floor.setPointerCapture(event.pointerId);
    inspector();
    renderScene();
    var focus = endpoint ? floor.querySelector('[data-endpoint="' + drag.endpoint + '"]') : floor.querySelector('[data-id="' + o.id + '"] button');
    if (focus) focus.focus({ preventScroll: true });
  });

  floor.addEventListener("pointermove", function (event) {
    if (!area) return;
    var p = point(event);
    if (!drag) { if (mode === "wall" && drawStart) { preview = { a: drawStart, b: p }; renderScene(); } return; }
    if (event.pointerId !== drag.pointerId) return;
    if (drag.type === "draw") { preview = { a: drag.start, b: p }; renderScene(); return; }
    if (!drag.moved && Math.hypot(event.clientX - drag.startScreen.x, event.clientY - drag.startScreen.y) < 4) return;
    drag.moved = true;
    var o = find(drag.id), original = drag.original, dx = p.x - drag.start.x, dy = p.y - drag.start.y;
    if (!o) return;
    if (drag.type === "endpoint") {
      var ends = G.endpoints(original);
      ends[drag.endpoint] = p;
      G.wallFromPoints(o, ends[0], ends[1]);
      G.fit(o, area.room);
    } else if (o.type === "door") {
      var start = G.doorPoint(original, area.room);
      G.moveDoor(o, { x: start.x + dx, y: start.y + dy }, area.room, snap);
    } else {
      o.x = original.x + dx;
      o.y = original.y + dy;
      G.fit(o, area.room);
    }
    renderScene();
  });

  function endPointer(event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    var d = drag;
    drag = null;
    if (floor.hasPointerCapture(event.pointerId)) floor.releasePointerCapture(event.pointerId);
    ignoreClickUntil = Date.now() + 180;
    if (event.type === "pointercancel") {
      if (d.before) { var s = JSON.parse(d.before); area.room = s.room; area.objects = s.objects; area.links = s.links; }
      drawStart = null;
      preview = null;
      render();
      status("Bewegung abgebrochen.");
      return;
    }
    if (d.type === "draw") {
      var p = point(event), moved = Math.hypot(event.clientX - d.startScreen.x, event.clientY - d.startScreen.y) > 4;
      if (moved || d.wasStarted) finishWall(d.start, p);
      else { preview = { a: d.start, b: d.start }; renderScene(); status("Jetzt den zweiten Wandpunkt setzen."); }
      return;
    }
    if (d.moved) commit(label(find(d.id) || d.original) + " verschoben", d.before);
    else render();
  }
  floor.addEventListener("pointerup", endPointer);
  floor.addEventListener("pointercancel", endPointer);

  root.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      if (drag && drag.before) { var s = JSON.parse(drag.before); area.room = s.room; area.objects = s.objects; area.links = s.links; }
      drag = null; drawStart = null; preview = null; mode = "select";
      render();
      status("Zeichnen oder Verschieben abgebrochen.");
      return;
    }
    if (event.target.matches("input,select,textarea")) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") { event.preventDefault(); undo(); return; }
    var o = selected();
    if (event.key === "Delete" && o) { event.preventDefault(); remove(o); return; }
    var delta = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
    if (!delta || !floor.contains(event.target) || !o) return;
    event.preventDefault();
    var before = content(area), unit = event.shiftKey ? 1 : ui.grid ? 10 : 5, endpoint = event.target.dataset.endpoint;
    if (o.type === "wall" && endpoint !== undefined) {
      var ends = G.endpoints(o), pt = ends[Number(endpoint)];
      pt.x = G.clamp(pt.x + delta[0] * unit, 0, area.room.w);
      pt.y = G.clamp(pt.y + delta[1] * unit, 0, area.room.h);
      G.wallFromPoints(o, ends[0], ends[1]);
    } else if (o.type === "door") {
      var dp = G.doorPoint(o, area.room);
      G.moveDoor(o, { x: dp.x + delta[0] * unit, y: dp.y + delta[1] * unit }, area.room, snap);
    } else { o.x += delta[0] * unit; o.y += delta[1] * unit; }
    G.fit(o, area.room);
    commit(label(o) + " verschoben", before);
  });

  // ---- start -----------------------------------------------------------------

  function load() {
    fetch("/gui/raumplan/plan", { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (res) { return res.json().then(function (data) { return { status: res.status, data: data }; }); })
      .then(function (res) {
        root.dataset.busy = "false";
        if (res.status !== 200) { status((res.data && res.data.say) || "Der Raumplan konnte nicht geladen werden.", true); render(); return; }
        areas = res.data.areas.map(fromDoc);
        if (!areas.length) {
          render();
          openForm();
          status("Noch kein Raum angelegt. Bitte zuerst einen Raum oder eine Terrasse erstellen.");
          return;
        }
        activate(areas[0]);
        status("Raumgröße, Eingänge und Möbel sind frei anpassbar. Jede Änderung wird sofort gespeichert.");
      })
      .catch(function () {
        root.dataset.busy = "false";
        status("Der Raumplan konnte nicht geladen werden: keine Verbindung zum Server.", true);
      });
  }

  new ResizeObserver(function () { layout(); }).observe(stage);
  load();
})();
