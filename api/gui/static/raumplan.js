// The floor plan editor (T-3.11, docs/06 §3 "Raumplan"), after the approved
// design gui/mockups/floor-plan-editor.html. A drag stays in the browser; each
// finished step (a drop, a field, a copy, an undo) sends the whole room to
// /gui/raumplan/bereiche/<id>, where domain/reservations/floor_plan.py checks
// and stores it. The server's answer is the truth: a refused step is undone
// here, and a room another tablet changed meanwhile is loaded as it stands.
// The page is drawn through the DOM, never through style attributes in markup:
// the proxy's Content-Security-Policy refuses those (docs/13 §3).
//
// Source: api/gui/ts/raumplan.ts. The file of the same name in api/gui/static/
// is its compiled output (`npm run build`, docs/22); edit the source, never the
// output. Loaded as a module (raumplan.html): the geometry is compiled from
// api/gui/ts/raumplan-geometry.ts. The page's `?v=2` is a new address for the
// two files that turned from classic scripts into modules: a browser still
// holding the old one would run it as a module and show an empty editor. Static
// files are asked for again on every use since then (api/gui/__init__.py), so a
// later change needs no new number.
import * as G from "./raumplan-geometry.js";
const TITLES = { table: "Tisch", counter: "Theke", door: "Eingang", wall: "Wand", label: "Beschriftung" };
const LABEL_TURN_TEXT = { "0": "Waagerecht", "-90": "Senkrecht, von unten zu lesen", "90": "Senkrecht, von oben zu lesen" };
const NOT_SAVED = "Nicht gespeichert: keine Verbindung zum Server. Die Änderung bleibt auf diesem Gerät.";
const SIDES = ["top", "right", "bottom", "left"];
const LABEL_SIZES = ["small", "normal", "large", "xlarge"];
// The elements of the page are looked up once; a missing one is a fault of the
// template, so it throws instead of letting a null travel on.
function must(scope, selector, kind) {
    const found = scope.querySelector(selector);
    if (!(found instanceof kind))
        throw new Error("floor plan page lacks " + selector);
    return found;
}
function record(value) {
    return typeof value === "object" && value !== null ? value : {};
}
function oneOf(list, value) {
    return list.find((x) => x === value);
}
function fmt(n, d) {
    return new Intl.NumberFormat("de-DE", { maximumFractionDigits: d === undefined ? 2 : d }).format(n);
}
const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ESCAPES[c] ?? c); }
function plural(n) { return n + " " + (n === 1 ? "Platz" : "Plätze"); }
function clone(v) { return JSON.parse(JSON.stringify(v)); }
function label(o) { return o.type === "label" ? o.text : (o.type === "table" ? "Tisch " : "") + o.name; }
function content(a) {
    return JSON.stringify({ room: a.room, objects: a.objects, links: a.links });
}
function newId() {
    if (window.crypto && window.crypto.randomUUID)
        return window.crypto.randomUUID();
    // Outside a secure context randomUUID is missing; the same version 4 by hand.
    const b = window.crypto.getRandomValues(new Uint8Array(16));
    b[6] = ((b[6] ?? 0) & 15) | 64;
    b[8] = ((b[8] ?? 0) & 63) | 128;
    const h = Array.from(b, (x) => (x + 256).toString(16).slice(1)).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
}
function node(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls)
        e.className = cls;
    if (text !== undefined)
        e.textContent = text;
    return e;
}
// ---- talking to the server -------------------------------------------------
function send(method, url, body) {
    const m = document.cookie.match(/(?:^|; )csrf_token=([^;]*)/);
    const init = {
        method,
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "HX-Request": "true", "X-CSRF-Token": m?.[1] ?? "" }
    };
    if (body !== undefined)
        init.body = JSON.stringify(body);
    return fetch(url, init).then((res) => {
        // Signed out, or locked after 15 minutes: the sign-in or the PIN first (#570).
        const away = res.status === 401 ? res.headers.get("HX-Redirect") : null;
        if (away) {
            location.href = away;
            return new Promise(() => { });
        }
        return res.json().catch(() => ({})).then((data) => ({ status: res.status, data }));
    });
}
(() => {
    const root = document.getElementById("raumplan");
    if (!root)
        return;
    const floor = must(root, ".re-floor", HTMLElement);
    const stage = must(root, ".re-stage", HTMLElement);
    const props = must(root, ".re-properties", HTMLElement);
    const form = must(root, ".re-room-form", HTMLFormElement);
    const L = G.LIMITS;
    let areas = [];
    let area = null;
    let selectedId = "room";
    let mode = "select";
    let drag = null;
    let drawStart = null;
    let preview = null;
    let ignoreClickUntil = 0;
    let scale = 1;
    const ui = { grid: true, advanced: false };
    function snap(n) { return ui.grid ? Math.round(n / 10) * 10 : Math.round(n); }
    // The open room; a handler that needs one runs only while one is open.
    function cur() {
        if (!area)
            throw new Error("no room is open");
        return area;
    }
    function find(id) { return area?.objects.find((o) => o.id === id); }
    function selected() { return find(selectedId); }
    function allTableNames() {
        const names = [];
        areas.forEach((a) => a.objects.forEach((o) => { if (o.type === "table")
            names.push(o.name); }));
        return names;
    }
    function formField(name) {
        const f = form.elements.namedItem(name);
        if (!(f instanceof HTMLInputElement || f instanceof HTMLSelectElement))
            throw new Error("room form lacks " + name);
        return f;
    }
    function fromDoc(doc) {
        // toClient reads the server's document field by field and refuses what it cannot read.
        const s = G.toClient(doc);
        return { room: s.room, objects: s.objects, links: s.links, revision: s.revision, saved: doc,
            history: [], saving: false, again: false, unsaved: false, unacked: null, pending: null };
    }
    function adopt(target, doc) {
        const fresh = fromDoc(doc);
        target.room = fresh.room;
        target.objects = fresh.objects;
        target.links = fresh.links;
        target.revision = fresh.revision;
        target.saved = fresh.saved;
        target.pending = fresh.pending;
    }
    // The other rooms as the server holds them now. A room with a step still
    // on its way keeps it; a room another tablet created is added.
    function refreshOthers(keep) {
        fetch("/gui/raumplan/plan", { credentials: "same-origin", headers: { Accept: "application/json" } })
            .then((res) => (res.status === 200 ? res.json() : null))
            .then((data) => {
            const docs = data ? record(data).areas : null;
            if (!Array.isArray(docs))
                return;
            let added = false;
            docs.forEach((doc) => {
                const fresh = fromDoc(doc);
                const mine = areas.find((a) => a.room.id === fresh.room.id);
                if (!mine) {
                    areas.push(fresh);
                    added = true;
                }
                else if (mine !== keep && mine !== area && !mine.saving && !mine.unsaved && !mine.unacked && !mine.pending)
                    adopt(mine, doc);
            });
            if (added)
                render();
        })
            .catch((err) => { if (window.console)
            window.console.error(err); });
    }
    // A save whose answer never came may have been stored: it goes out again,
    // exactly as it was, before anything newer. The server takes the same room
    // twice as no conflict and answers with its revision (Codex on #535).
    function save(target) {
        if (target.saving) {
            target.again = true;
            return;
        }
        target.saving = true;
        // Only finished steps go out: `pending` is taken at a drop, a field, an
        // undo, never from the state a drag in progress is moving (Codex on #535).
        const body = target.unacked || { revision: target.revision, area: target.pending || G.toServer(target) };
        let replaced = false;
        const lost = () => {
            target.unacked = body;
            target.unsaved = true;
            status(NOT_SAVED, true, true);
        };
        send("PUT", "/gui/raumplan/bereiche/" + target.room.id, body)
            .then((res) => {
            const data = record(res.data);
            const say = typeof data.say === "string" ? data.say : "";
            if (res.status === 200) {
                const stored = G.toClient(data.area);
                target.unacked = null;
                target.revision = stored.revision;
                target.saved = data.area;
                target.unsaved = false;
                // Steps made meanwhile, or held back behind a resent save, follow now.
                if (target.pending && JSON.stringify(target.pending) !== JSON.stringify(body.area))
                    target.again = true;
                // What was sent is stored: no step waits, the room may be read again (Codex PR #724).
                else
                    target.pending = null;
                if (!target.again && target === area)
                    status("Gespeichert.");
            }
            else if (res.status === 409 && data.area) {
                adopt(target, data.area);
                target.unacked = null;
                target.history = [];
                target.again = false;
                replaced = true;
                status(say || "Ein anderes Gerät hat diesen Raum geändert.", true);
            }
            else if (res.status >= 400 && res.status < 500) {
                // Refused: back to what the server holds, the step is not kept.
                adopt(target, target.saved);
                target.unacked = null;
                const top = target.history[target.history.length - 1];
                if (top && top === content(target))
                    target.history.pop();
                target.again = false;
                replaced = true;
                status(say || "Das hat nicht geklappt, nichts gespeichert.", true);
                // The refusal may come from a name another tablet took in another
                // room: those rooms are read again, or every retry picks it (#680).
                refreshOthers(target);
            }
            else {
                lost();
            }
            // A drag that began on the room now replaced would bring the old room
            // back on Escape: it ends here, with nothing to restore (Codex on #535).
            if (replaced && target === area && drag) {
                drag = null;
                drawStart = null;
                preview = null;
                mode = "select";
            }
            // Only a replaced room is drawn again: a redraw on every answer would
            // throw away what the team is typing into a field (Codex on #535).
            if (target === area) {
                if (replaced) {
                    if (!find(selectedId))
                        selectedId = "room";
                    render();
                }
                else
                    controls();
            }
        }, lost)
            .catch((err) => {
            // A fault in this page, not on the line: keep the step and say so.
            lost();
            if (window.console)
                window.console.error(err);
        })
            .then(() => {
            target.saving = false;
            if (target.again && !target.unsaved) {
                target.again = false;
                save(target);
            }
        });
    }
    function status(text, error, retry) {
        const el = must(root, ".re-status", HTMLElement);
        el.textContent = text;
        el.classList.toggle("re-error", !!error);
        if (retry) {
            const b = node("button", "re-btn", "Nochmal speichern");
            b.type = "button";
            b.dataset.action = "retry";
            el.append(b);
        }
    }
    window.addEventListener("beforeunload", (e) => {
        if (areas.some((a) => a.saving || a.unsaved))
            e.preventDefault();
    });
    // ---- editing ---------------------------------------------------------------
    // One finished step: remember the state before it for undo, then save.
    function commit(message, before) {
        const a = cur();
        a.history.push(before);
        if (a.history.length > 30)
            a.history.shift();
        render();
        status(message + " · speichert ...");
        a.pending = G.toServer(a);
        save(a);
    }
    // A cancelled drag puts back only the piece it moved, in the room it began
    // in: a snapshot of the whole room would undo whatever replaced the room
    // meanwhile, a conflict, an undo or another room (Codex on #535).
    function cancelDrag(d) {
        if (d && d.original && d.area) {
            const i = d.area.objects.findIndex((x) => x.id === d.id);
            if (i >= 0)
                d.area.objects[i] = clone(d.original);
        }
        drag = null;
        drawStart = null;
        preview = null;
    }
    function activate(target) {
        cancelDrag(drag);
        area = target;
        selectedId = "room";
        mode = "select";
        drawStart = null;
        preview = null;
        render();
    }
    function add(type) {
        const a = cur();
        const before = content(a), r = a.room, id = newId();
        const x = r.w * 0.5 + ((a.objects.length % 3) - 1) * 60, y = r.h * 0.5;
        let o;
        if (type === "table")
            o = { id, type, name: G.nextTableName(allTableNames()), x, y, w: 120, h: 80, angle: 0, shape: "rect", seats: 4, endSeat: "left" };
        else if (type === "counter")
            o = { id, type, name: "Theke", x, y, w: 80, h: 220, angle: 0 };
        else if (type === "door")
            o = { id, type, name: "Eingang", side: "bottom", offset: r.w * 0.7, width: 100 };
        else
            o = { id, type, text: "Beschriftung", x, y, size: "normal", angle: 0 };
        if (!G.available(o, r)) {
            status("Für dieses Objekt ist der Raum zu klein.", true);
            return;
        }
        G.fit(o, r);
        a.objects.push(o);
        selectedId = id;
        mode = "select";
        drawStart = null;
        preview = null;
        commit(TITLES[type] + " hinzugefügt", before);
        if (type === "label") {
            const input = props.querySelector('[data-field="text"]');
            if (input instanceof HTMLInputElement) {
                input.focus();
                input.select();
            }
        }
    }
    function finishWall(from, to) {
        if (Math.hypot(to.x - from.x, to.y - from.y) < L.wallLengthMin) {
            status("Die Wand muss mindestens 20 cm lang sein.", true);
            return;
        }
        const a = cur();
        const before = content(a);
        const o = { id: newId(), type: "wall", name: "Wand", thickness: 20, x: 0, y: 0, length: 0, angle: 0 };
        G.wallFromPoints(o, from, to);
        G.fit(o, a.room);
        a.objects.push(o);
        selectedId = o.id;
        drawStart = null;
        preview = null;
        mode = "select";
        commit("Wand gezeichnet", before);
    }
    function cloneTable() {
        const source = selected();
        if (!source || source.type !== "table")
            return;
        const a = cur();
        const before = content(a), copy = clone(source);
        copy.id = newId();
        copy.name = G.nextTableName(allTableNames());
        G.placeCopy(copy, source, a.objects, a.room);
        a.objects.push(copy);
        selectedId = copy.id;
        commit("Tisch " + source.name + " als " + copy.name + " kopiert", before);
    }
    function remove(o) {
        const a = cur();
        const what = o.type === "label" ? "Beschriftung „" + o.text + "“" : TITLES[o.type] + " „" + o.name + "“";
        if (!window.confirm(what + " entfernen?"))
            return;
        const before = content(a);
        a.objects = a.objects.filter((x) => x.id !== o.id);
        a.links = a.links.filter((p) => p.indexOf(o.id) < 0);
        selectedId = "room";
        commit(what + " entfernt", before);
    }
    function undo() {
        if (!area || !area.history.length)
            return;
        // A drag in progress would bring the state before the undo back on
        // Escape, unsaved (Codex on #535): the undo ends it.
        drag = null;
        const s = JSON.parse(area.history.pop() ?? "{}");
        area.room = s.room;
        area.objects = s.objects;
        area.links = s.links;
        mode = "select";
        drawStart = null;
        preview = null;
        if (!find(selectedId))
            selectedId = "room";
        render();
        status("Letzte Änderung rückgängig gemacht · speichert ...");
        area.pending = G.toServer(area);
        save(area);
    }
    function createRoom() {
        if (!form.reportValidity())
            return;
        const nameField = formField("areaName");
        const name = nameField.value.trim();
        if (!name) {
            nameField.focus();
            return;
        }
        const taken = areas.some((a) => a.room.name.toLocaleLowerCase("de-DE") === name.toLocaleLowerCase("de-DE"));
        if (taken) {
            status("Dieser Raumname ist schon vergeben. Bitte einen anderen Namen wählen.", true);
            return;
        }
        const body = { name, kind: formField("areaKind").value,
            width_cm: Math.round(Number(formField("areaWidth").value) * 100),
            depth_cm: Math.round(Number(formField("areaHeight").value) * 100) };
        status("Wird erstellt ...");
        send("POST", "/gui/raumplan/bereiche", body).then((res) => {
            const data = record(res.data);
            if (res.status !== 200) {
                status((typeof data.say === "string" && data.say) || "Das hat nicht geklappt.", true);
                return;
            }
            const created = fromDoc(data.area);
            areas.push(created);
            form.hidden = true;
            activate(created);
            status(name + " erstellt.");
        }, () => { status("Nicht erstellt: keine Verbindung zum Server.", true); });
    }
    // ---- drawing ---------------------------------------------------------------
    function chairs(wrap, o) {
        const cw = G.clamp(24 * scale, 6, 17), ch = G.clamp(10 * scale, 3, 7), w = o.w * scale, h = o.h * scale;
        const chair = (x, y, a) => {
            const c = node("span", "re-chair");
            c.setAttribute("aria-hidden", "true");
            c.style.left = x + "px";
            c.style.top = y + "px";
            c.style.width = cw + "px";
            c.style.height = ch + "px";
            c.style.setProperty("--chair-angle", (a || 0) + "deg");
            wrap.append(c);
        };
        const s = G.seatSides(o.seats, o.shape, o.endSeat);
        if ("around" in s) {
            for (let i = 0; i < s.around; i++) {
                const a = 2 * Math.PI * i / s.around - Math.PI / 2;
                chair(w / 2 + (w / 2 + ch + 2) * Math.cos(a), h / 2 + (h / 2 + ch + 2) * Math.sin(a), a * 180 / Math.PI + 90);
            }
            return;
        }
        for (let i = 0; i < s.top; i++)
            chair(w * (i + 1) / (s.top + 1), -ch - 2);
        for (let i = 0; i < s.bottom; i++)
            chair(w * (i + 1) / (s.bottom + 1), h + ch + 2);
        if (s.left)
            chair(-ch - 2, h / 2, 90);
        if (s.right)
            chair(w + ch + 2, h / 2, 90);
    }
    function place(wrap, x, y) { wrap.style.left = x * scale + "px"; wrap.style.top = y * scale + "px"; }
    // Keep a button that sticks out of its point inside the plan.
    function keepInside(button, x, y) {
        const room = cur().room;
        const w = button.offsetWidth, h = button.offsetHeight;
        const bx = x + (parseFloat(button.style.left) || 0), by = y + (parseFloat(button.style.top) || 0);
        button.style.left = G.clamp(bx, w / 2 + 3, room.w * scale - w / 2 - 3) - x + "px";
        button.style.top = G.clamp(by, h / 2 + 3, room.h * scale - h / 2 - 3) - y + "px";
    }
    function drawObject(o) {
        const room = cur().room;
        const active = o.id === selectedId, wrap = node("div", "re-object");
        wrap.dataset.id = o.id;
        if (active)
            wrap.classList.add("re-selected");
        if (o.type === "table" || o.type === "counter") {
            wrap.classList.add("re-furniture");
            if (o.type === "counter")
                wrap.classList.add("re-counter");
            if (o.type === "table" && o.shape === "round")
                wrap.classList.add("re-round");
            place(wrap, o.x, o.y);
            wrap.style.width = o.w * scale + "px";
            wrap.style.height = o.h * scale + "px";
            wrap.style.setProperty("--re-angle", o.angle + "deg");
            if (o.type === "table")
                chairs(wrap, o);
            const b = node("button", "re-piece");
            b.type = "button";
            b.setAttribute("aria-label", label(o) + (o.type === "table" ? ", " + plural(o.seats) : ""));
            b.setAttribute("aria-pressed", String(active));
            const turn = G.labelTurn(o), text = node("span", "re-piece-text");
            text.style.setProperty("--re-text-turn", turn.turn + "deg");
            if (turn.vertical)
                text.classList.add("re-vertical");
            const nameEl = node("span", "re-piece-name", o.name);
            text.append(nameEl);
            const seats = o.type === "table" ? node("span", "re-piece-seats", plural(o.seats)) : null;
            if (seats)
                text.append(seats);
            b.append(text);
            wrap.append(b);
            floor.append(wrap);
            if (seats) {
                // 18 px text does not fit every small table: the name stays, the count is in the label.
                const outer = b.getBoundingClientRect(), nr = nameEl.getBoundingClientRect(), sr = seats.getBoundingClientRect();
                if (Math.max(nr.width, sr.width) > outer.width - 8 || nr.height + sr.height > outer.height - 4)
                    seats.hidden = true;
            }
        }
        else if (o.type === "wall") {
            wrap.classList.add("re-wall-object");
            place(wrap, o.x, o.y);
            wrap.style.width = o.length * scale + "px";
            wrap.style.height = Math.max(60, o.thickness * scale) + "px";
            wrap.style.setProperty("--re-angle", o.angle + "deg");
            wrap.style.setProperty("--re-thickness", Math.max(3, o.thickness * scale) + "px");
            const b = node("button", "re-wall-hit");
            b.type = "button";
            b.setAttribute("aria-label", o.name + ", " + fmt(o.length / 100) + " Meter");
            b.setAttribute("aria-pressed", String(active));
            b.append(node("span", "re-wall-bar"));
            wrap.append(b);
            if (active)
                wrap.append(node("span", "re-wall-measure", fmt(o.length / 100) + " m · " + fmt(o.thickness) + " cm"));
            floor.append(wrap);
        }
        else if (o.type === "door") {
            const p = G.doorPoint(o, room), side = o.side === "left" || o.side === "right";
            const t = Math.max(4, room.thickness * scale) + 6;
            wrap.classList.add("re-door");
            if (side)
                wrap.classList.add("re-side");
            place(wrap, p.x, p.y);
            const gap = node("span", "re-door-gap");
            gap.style.width = (side ? t : o.width * scale) + "px";
            gap.style.height = (side ? o.width * scale : t) + "px";
            const arrow = { left: "→", right: "←", top: "↓", bottom: "↑" }[o.side];
            const b = node("button", "re-door-button", arrow + " " + o.name);
            b.type = "button";
            b.style.left = (o.side === "left" ? 64 : o.side === "right" ? -64 : 0) + "px";
            b.style.top = (o.side === "top" ? 30 : o.side === "bottom" ? -30 : 0) + "px";
            b.setAttribute("aria-label", o.name + ", " + fmt(o.width) + " Zentimeter breit");
            b.setAttribute("aria-pressed", String(active));
            wrap.append(gap, b);
            floor.append(wrap);
            keepInside(b, p.x * scale, p.y * scale);
        }
        else {
            wrap.classList.add("re-label");
            const b = node("button", "re-label-button", o.text);
            b.type = "button";
            const labelTurn = G.readableTurn(o.angle);
            b.style.fontSize = (G.LABEL_PX[o.size] || 20) + "px";
            b.style.setProperty("--re-label-turn", labelTurn + "deg");
            b.setAttribute("aria-label", "Beschriftung: " + o.text);
            b.setAttribute("aria-pressed", String(active));
            wrap.append(b);
            floor.append(wrap);
            // A label turned upright takes its width as height on the plan.
            const upright = labelTurn !== 0;
            const lw = upright ? b.offsetHeight : b.offsetWidth, lh = upright ? b.offsetWidth : b.offsetHeight;
            wrap.style.left = G.clamp(o.x * scale, lw / 2 + 3, room.w * scale - lw / 2 - 3) + "px";
            wrap.style.top = G.clamp(o.y * scale, lh / 2 + 3, room.h * scale - lh / 2 - 3) + "px";
        }
        if (active && o.type === "wall") {
            G.endpoints(o).forEach((pt, index) => {
                const e = node("button", "re-endpoint");
                e.type = "button";
                e.dataset.endpoint = String(index);
                e.dataset.id = o.id;
                e.setAttribute("aria-label", "Wand-Endpunkt " + (index + 1));
                e.style.left = pt.x * scale + "px";
                e.style.top = pt.y * scale + "px";
                floor.append(e);
            });
        }
    }
    function renderScene() {
        const focused = document.activeElement;
        const focusHolder = focused ? focused.closest(".re-object") : null;
        const focusId = focusHolder instanceof HTMLElement ? focusHolder.dataset.id : undefined;
        const focusEndpoint = focused instanceof HTMLElement ? focused.dataset.endpoint : undefined;
        const hadFocus = floor.contains(focused);
        floor.querySelectorAll(".re-object,.re-endpoint,.re-preview,.re-room-empty").forEach((e) => { e.remove(); });
        if (!area)
            return;
        const r = area.room;
        floor.classList.toggle("re-grid", ui.grid);
        floor.classList.toggle("re-drawing", mode === "wall");
        floor.classList.toggle("re-terrace", r.kind === "terrace");
        floor.style.backgroundSize = 10 * scale + "px " + 10 * scale + "px";
        const t = Math.max(2, r.thickness * scale), w = r.w * scale, h = r.h * scale;
        SIDES.forEach((s) => {
            const e = must(floor, '[data-side="' + s + '"]', HTMLElement), horizontal = s === "top" || s === "bottom";
            e.style.left = (s === "right" ? w - t / 2 : -t / 2) + "px";
            e.style.top = (s === "bottom" ? h - t / 2 : -t / 2) + "px";
            e.style.width = (horizontal ? w + t : t) + "px";
            e.style.height = (horizontal ? t : h + t) + "px";
        });
        must(floor, ".re-dimension-x", HTMLElement).textContent = fmt(r.w / 100) + " m";
        must(floor, ".re-dimension-y", HTMLElement).textContent = fmt(r.h / 100) + " m";
        const svg = must(floor, ".re-connections", SVGSVGElement);
        svg.setAttribute("viewBox", "0 0 " + w + " " + h);
        svg.replaceChildren();
        area.links.forEach((pair) => {
            const a = find(pair[0]), b = find(pair[1]);
            // Only tables are linked, and every piece but a door has a centre.
            if (!a || !b || !("x" in a) || !("x" in b))
                return;
            const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
            line.setAttribute("x1", String(a.x * scale));
            line.setAttribute("y1", String(a.y * scale));
            line.setAttribute("x2", String(b.x * scale));
            line.setAttribute("y2", String(b.y * scale));
            svg.append(line);
        });
        ["wall", "counter", "table", "label", "door"].forEach((type) => {
            area?.objects.filter((o) => o.type === type).forEach(drawObject);
        });
        if (!area.objects.length)
            floor.append(node("div", "re-room-empty", r.name + " · noch leer"));
        if (preview) {
            const pv = node("div", "re-preview re-wall-object"), o = { x: 0, y: 0, length: 0, angle: 0 };
            G.wallFromPoints(o, preview.a, preview.b);
            pv.style.left = o.x * scale + "px";
            pv.style.top = o.y * scale + "px";
            pv.style.width = o.length * scale + "px";
            pv.style.height = Math.max(3, 20 * scale) + "px";
            pv.style.setProperty("--re-angle", o.angle + "deg");
            floor.append(pv);
        }
        if (hadFocus) {
            const target = focusEndpoint !== undefined ? floor.querySelector('[data-endpoint="' + focusEndpoint + '"]')
                : focusId ? floor.querySelector('[data-id="' + focusId + '"] button') : null;
            if (target instanceof HTMLElement)
                target.focus({ preventScroll: true });
        }
    }
    function layout() {
        if (!area)
            return;
        const r = area.room;
        const height = Math.min(560, Math.max(280, (stage.clientWidth - 90) * r.h / r.w + 100));
        stage.style.height = height + "px";
        const box = stage.getBoundingClientRect();
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
            entries.map((e) => '<option value="' + e[0] + '"' + (String(e[0]) === String(value) ? " selected" : "") + ">" + e[1] + "</option>").join("") + "</select></label>";
    }
    function inspector() {
        const focusedField = document.activeElement instanceof HTMLElement && props.contains(document.activeElement) ? document.activeElement.dataset.field : undefined;
        const combos = props.querySelector(".re-combos");
        const comboOpen = combos instanceof HTMLDetailsElement && combos.open;
        const selector = must(root, '[data-field="selection"]', HTMLSelectElement);
        if (!area) {
            selector.replaceChildren();
            props.replaceChildren();
            return;
        }
        const current = area;
        selector.innerHTML = '<option value="room">' + esc(current.room.name) + "</option>" + current.objects.map((o) => '<option value="' + o.id + '">' + esc(o.type === "label" ? "Text: " + o.text : label(o)) + "</option>").join("");
        selector.value = selectedId;
        let html = "", extra = "";
        const r = current.room, o = selected();
        let title = r.kind === "terrace" ? "Terrasse" : "Raum";
        if (!o) {
            html = textField("roomName", "Raumname", r.name, 30) +
                selectField("roomKind", "Bereich", r.kind, [["inside", "Innenraum"], ["terrace", "Terrasse"]]) +
                selectField("roomShape", "Grundform", r.shape, [["rectangle", "Rechteck"], ["square", "Quadrat"]]) +
                numberField("roomW", "Breite (m)", r.w / 100, L.roomMin / 100, L.roomMax / 100, 0.1) +
                numberField("roomH", "Tiefe (m)", r.h / 100, L.roomMin / 100, L.roomMax / 100, 0.1) +
                (r.kind === "inside" ? numberField("roomThickness", "Außenwand (cm)", r.thickness, L.wallMin, L.wallMax, 1, true) : "");
            extra = '<p class="re-meta">' + fmt(r.w * r.h / 10000) + " m² Grundfläche" + (r.shape === "square" ? " · Seitenlängen gekoppelt" : "") + "</p>";
        }
        else {
            title = TITLES[o.type];
            html = o.type === "label" ? textField("text", "Text", o.text, 60) : textField("name", "Bezeichnung", o.name, 25);
            if (o.type === "table") {
                html += '<div class="re-field re-full"><span>Sitzplätze · Einerschritte</span><div class="re-stepper">' +
                    '<button type="button" class="re-btn" data-action="minus" aria-label="Ein Sitzplatz weniger"' + (o.seats <= L.seatsMin ? " disabled" : "") + ">−</button>" +
                    '<input type="number" data-field="seats" value="' + o.seats + '" min="' + L.seatsMin + '" max="' + L.seatsMax + '" step="1" aria-label="Sitzplätze">' +
                    '<button type="button" class="re-btn" data-action="plus" aria-label="Ein Sitzplatz mehr"' + (o.seats >= L.seatsMax ? " disabled" : "") + ">+</button></div></div>";
                html += selectField("shape", "Tischform", o.shape, [["rect", "Rechteckig"], ["round", "Rund"]]);
                const presets = G.PRESETS[o.shape];
                const match = presets.some((p) => p[0] === o.w && p[1] === o.h);
                const choices = presets.map((p) => [p[0] + "x" + p[1], o.shape === "round" ? "Ø " + p[0] + " cm" : p[0] + " × " + p[1] + " cm"]);
                if (!match)
                    choices.unshift(["custom", "Individuell: " + (o.shape === "round" ? "Ø " + o.w : o.w + " × " + o.h) + " cm"]);
                html += selectField("preset", "Tischgröße", match ? o.w + "x" + o.h : "custom", choices);
                if (G.hasEndChoice(o))
                    html += selectField("endSeat", "Zusatzplatz an der Stirnseite", o.endSeat || "left", [["left", "Links"], ["right", "Rechts"]]);
                html += '<label class="re-advanced re-full"><span>Erweiterter Modus<small>Individuelle Tischmaße</small></span><input type="checkbox" role="switch" data-field="advanced"' + (ui.advanced ? " checked" : "") + "></label>";
                if (!ui.advanced && !match)
                    html += '<p class="re-custom-size re-full">Individuelle Maße bleiben erhalten und sind gesperrt.</p>';
            }
            if (o.type === "table" || o.type === "counter") {
                const round = o.type === "table" && o.shape === "round";
                const min = o.type === "table" ? L.tableMin : L.counterMin, max = o.type === "table" ? L.tableMax : L.counterMax;
                if (o.type === "counter" || ui.advanced) {
                    html += numberField("w", round ? "Durchmesser (cm)" : "Breite (cm)", o.w, min, max, 1, round);
                    if (!round)
                        html += numberField("h", "Tiefe (cm)", o.h, min, max, 1);
                }
                if (o.type === "table")
                    html += '<div class="re-table-actions re-full">' + (!round ? '<button type="button" class="re-btn" data-action="rotate">90° drehen</button>' : "") + '<button type="button" class="re-btn" data-action="clone">Tisch kopieren</button></div>';
                else
                    html += '<button type="button" class="re-btn re-full" data-action="rotate">Um 90° drehen</button>';
            }
            if (o.type === "table") {
                const partners = current.objects.filter((x) => x.type === "table" && x.id !== o.id);
                extra = '<details class="re-combos"' + (comboOpen ? " open" : "") + '><summary>Tische kombinieren</summary><div class="re-combo-buttons">' +
                    (partners.length ? partners.map((x) => {
                        const on = current.links.some((p) => p.indexOf(x.id) >= 0 && p.indexOf(o.id) >= 0);
                        return '<button class="re-btn" type="button" data-partner="' + x.id + '" aria-pressed="' + on + '">' + esc(x.name) + "</button>";
                    }).join("") : '<p class="re-meta">Noch kein anderer Tisch in diesem Raum.</p>') + "</div></details>";
            }
            if (o.type === "door") {
                const horizontal = o.side === "top" || o.side === "bottom";
                html += selectField("side", "Außenwand", o.side, [["top", "Oben"], ["right", "Rechts"], ["bottom", "Unten"], ["left", "Links"]]) +
                    numberField("doorWidth", "Breite (cm)", o.width, L.doorMin, L.doorMax, 5, true) +
                    numberField("offset", horizontal ? "Mitte ab linker Ecke (cm)" : "Mitte ab oberer Ecke (cm)", Math.round(o.offset), 0, horizontal ? r.w : r.h, 1, true);
            }
            if (o.type === "wall") {
                html += numberField("length", "Länge (m)", Number((o.length / 100).toFixed(2)), L.wallLengthMin / 100, Math.ceil(Math.hypot(r.w, r.h)) / 100, 0.01) +
                    numberField("thickness", "Dicke (cm)", o.thickness, L.wallMin, L.wallMax, 1) +
                    numberField("angle", "Winkel (°)", Number(o.angle.toFixed(1)), -180, 180, 0.1, true);
            }
            if (o.type === "label") {
                html += selectField("size", "Schriftgröße", o.size, [["small", "Klein"], ["normal", "Normal"], ["large", "Groß"], ["xlarge", "Sehr groß"]]);
                html += '<button type="button" class="re-btn re-full" data-action="turnLabel">Drehen</button><p class="re-meta re-full">' + (LABEL_TURN_TEXT[String(G.readableTurn(o.angle))] ?? "") + "</p>";
            }
            extra += '<button class="re-btn re-delete" type="button" data-action="delete">' + title + " entfernen</button>";
        }
        props.innerHTML = "<h3>" + title + '</h3><div class="re-field-grid">' + html + "</div>" + extra;
        if (focusedField) {
            const f = props.querySelector('[data-field="' + focusedField + '"]');
            if (f instanceof HTMLElement)
                f.focus({ preventScroll: true });
        }
    }
    function controls() {
        const nav = must(root, ".re-room-tabs", HTMLElement);
        const focusedRoom = document.activeElement instanceof HTMLElement && nav.contains(document.activeElement) ? document.activeElement.dataset.roomId : undefined;
        nav.innerHTML = areas.map((a) => {
            const tag = a.room.kind === "terrace" && !/terrasse/i.test(a.room.name) ? '<span class="re-area-tag">Terrasse</span>' : "";
            return '<button class="re-btn re-room-tab" type="button" data-room-id="' + a.room.id + '"' + (a === area ? ' aria-current="page"' : "") + ">" + esc(a.room.name) + tag + "</button>";
        }).join("");
        if (focusedRoom) {
            const f = nav.querySelector('[data-room-id="' + focusedRoom + '"]');
            if (f instanceof HTMLElement)
                f.focus({ preventScroll: true });
        }
        root.querySelectorAll("[data-tool]").forEach((b) => { b.setAttribute("aria-pressed", String(b.dataset.tool === mode)); b.disabled = !area; });
        root.querySelectorAll("[data-add]").forEach((b) => { b.disabled = !area; });
        must(root, '[data-field="grid"]', HTMLInputElement).checked = ui.grid;
        must(root, '[data-action="undo"]', HTMLButtonElement).disabled = !area || !area.history.length;
        const summary = must(root, ".re-room-value", HTMLElement), total = must(root, ".re-total", HTMLElement);
        if (!area) {
            summary.textContent = "";
            total.textContent = "";
            return;
        }
        const r = area.room, tables = area.objects.filter((o) => o.type === "table");
        const seats = tables.reduce((n, o) => n + (o.type === "table" ? o.seats : 0), 0);
        summary.textContent = fmt(r.w / 100) + " × " + fmt(r.h / 100) + " m · " + (r.kind === "terrace" ? "Terrasse" : "Raum") + " anpassen";
        total.textContent = r.name + " · " + tables.length + " Tische · " + plural(seats) + " · " + fmt(r.w * r.h / 10000) + " m²";
        must(root, ".re-hint", HTMLElement).textContent = mode === "wall" ? "Zwei Punkte setzen oder ziehen · Esc bricht ab" : "Objekte ziehen · Wände an den Endpunkten ändern · Pfeiltasten verschieben";
    }
    function render() { controls(); inspector(); layout(); }
    // ---- events ----------------------------------------------------------------
    function openForm() {
        form.hidden = false;
        const nameField = formField("areaName");
        if (areas.length)
            nameField.value = formField("areaKind").value === "terrace" ? "Terrasse" : "Nebenraum";
        nameField.focus();
        if (nameField instanceof HTMLInputElement)
            nameField.select();
    }
    root.addEventListener("click", (event) => {
        if (!(event.target instanceof Element))
            return;
        if (Date.now() < ignoreClickUntil && floor.contains(event.target))
            return;
        const b = event.target.closest("button");
        if (!b || b.disabled)
            return;
        if (b.dataset.roomId) {
            const target = areas.find((a) => a.room.id === b.dataset.roomId);
            if (target) {
                form.hidden = true;
                activate(target);
                status(target.room.name + " geöffnet.");
            }
            return;
        }
        if (b.dataset.tool === "select" || b.dataset.tool === "wall") {
            mode = b.dataset.tool;
            drawStart = null;
            preview = null;
            controls();
            renderScene();
            return;
        }
        const add_ = b.dataset.add;
        if (add_ === "table" || add_ === "counter" || add_ === "door" || add_ === "label") {
            add(add_);
            return;
        }
        const action = b.dataset.action, o = selected();
        if (b.dataset.partner && o && area) {
            const before = content(area), id = b.dataset.partner;
            const index = area.links.findIndex((p) => p.indexOf(o.id) >= 0 && p.indexOf(id) >= 0);
            if (index < 0)
                area.links.push([o.id, id]);
            else
                area.links.splice(index, 1);
            commit("Kombination angepasst", before);
            return;
        }
        if (action === "newRoom") {
            openForm();
            return;
        }
        if (action === "cancelRoom") {
            form.hidden = true;
            return;
        }
        if (action === "retry") {
            areas.forEach((a) => { if (a.unsaved)
                save(a); });
            status("Speichert ...");
            return;
        }
        if (action === "undo") {
            undo();
            return;
        }
        if (action === "room") {
            selectedId = "room";
            mode = "select";
            render();
            return;
        }
        if (action === "clone") {
            cloneTable();
            return;
        }
        if (action === "delete" && o) {
            remove(o);
            return;
        }
        if (action === "turnLabel" && o && o.type === "label" && area) {
            const turnedFrom = content(area);
            o.angle = G.nextLabelTurn(o.angle);
            G.fit(o, area.room);
            commit("Text gedreht: " + (LABEL_TURN_TEXT[String(G.readableTurn(o.angle))] ?? ""), turnedFrom);
            return;
        }
        if (action && o && area) {
            const prior = content(area);
            // Seats and a quarter turn belong to a table or a counter, as the buttons do.
            if (action === "plus" && o.type === "table")
                o.seats = Math.min(L.seatsMax, o.seats + 1);
            if (action === "minus" && o.type === "table")
                o.seats = Math.max(L.seatsMin, o.seats - 1);
            if (action === "rotate" && (o.type === "table" || o.type === "counter")) {
                o.angle = (o.angle + 90) % 360;
                if (!G.available(o, area.room)) {
                    o.angle = (o.angle + 270) % 360;
                    status("Für diese Ausrichtung ist der Raum zu klein.", true);
                    return;
                }
                G.fit(o, area.room);
            }
            commit(action === "rotate" ? TITLES[o.type] + " gedreht" : "Sitzplätze: " + (o.type === "table" ? o.seats : ""), prior);
            return;
        }
        if (floor.contains(b) && mode === "select") {
            const holder = b.closest("[data-id]");
            if (holder?.dataset.id) {
                selectedId = holder.dataset.id;
                render();
            }
        }
    });
    form.addEventListener("submit", (event) => { event.preventDefault(); createRoom(); });
    formField("areaKind").addEventListener("change", (event) => {
        const field = formField("areaName");
        if (["Gastraum", "Nebenraum", "Terrasse"].indexOf(field.value) >= 0 && event.target instanceof HTMLSelectElement) {
            field.value = event.target.value === "terrace" ? "Terrasse" : areas.length ? "Nebenraum" : "Gastraum";
        }
    });
    root.addEventListener("change", (event) => {
        const input = event.target;
        if (!(input instanceof HTMLInputElement || input instanceof HTMLSelectElement))
            return;
        const field = input.dataset.field;
        if (!field || !area && field !== "grid")
            return;
        if (field === "selection") {
            selectedId = input.value;
            mode = "select";
            render();
            return;
        }
        if (field === "grid" && input instanceof HTMLInputElement) {
            ui.grid = input.checked;
            renderScene();
            return;
        }
        if (field === "advanced" && input instanceof HTMLInputElement) {
            ui.advanced = input.checked;
            inspector();
            status(ui.advanced ? "Individuelle Tischmaße freigeschaltet." : "Individuelle Tischmaße gesperrt.");
            return;
        }
        const a = cur();
        const before = content(a), o = selected(), r = a.room, raw = input.value, n = Number(raw);
        if (input.type === "number" && (raw.trim() === "" || !Number.isFinite(n) || !input.checkValidity())) {
            render();
            status("Bitte einen gültigen Wert innerhalb des angezeigten Bereichs eingeben.", true);
            return;
        }
        if (field === "roomName") {
            const name = raw.trim().slice(0, 30);
            const taken = areas.some((x) => x !== a && x.room.name.toLocaleLowerCase("de-DE") === name.toLocaleLowerCase("de-DE"));
            if (!name || taken) {
                render();
                status("Bitte einen freien Raumnamen eingeben.", true);
                return;
            }
            r.name = name;
        }
        else if (field === "roomKind")
            r.kind = raw;
        else if (field === "roomShape") {
            r.shape = raw;
            if (raw === "square")
                r.h = r.w;
        }
        else if (field === "roomW") {
            r.w = Math.round(n * 100);
            if (r.shape === "square")
                r.h = r.w;
        }
        else if (field === "roomH") {
            r.h = Math.round(n * 100);
            if (r.shape === "square")
                r.w = r.h;
        }
        else if (field === "roomThickness")
            r.thickness = n;
        else if (o) {
            if (field === "name" && o.type !== "label") {
                const newName = raw.trim().slice(0, 25);
                const clash = o.type === "table" && allTableNames().some((x) => x !== o.name && x.toLocaleLowerCase("de-DE") === newName.toLocaleLowerCase("de-DE"));
                if (!newName || clash) {
                    render();
                    status(clash ? "Den Tischnamen " + newName + " gibt es schon. Bitte einen anderen wählen." : "Bitte einen Namen eingeben.", true);
                    return;
                }
                o.name = newName;
            }
            if (field === "text" && o.type === "label")
                o.text = raw.trim().slice(0, 60) || "Beschriftung";
            if (field === "seats" && o.type === "table")
                o.seats = G.clamp(Math.round(n), L.seatsMin, L.seatsMax);
            if (field === "size" && o.type === "label")
                o.size = oneOf(LABEL_SIZES, raw) ?? o.size;
            if (field === "shape" && o.type === "table") {
                o.shape = raw === "round" ? "round" : "rect";
                o.w = 120;
                o.h = o.shape === "round" ? 120 : 80;
                o.angle = 0;
            }
            if (field === "preset" && o.type === "table") {
                const size = G.PRESETS[o.shape].find((p) => p[0] + "x" + p[1] === raw);
                if (size) {
                    o.w = size[0];
                    o.h = size[1];
                }
            }
            if (field === "endSeat" && o.type === "table")
                o.endSeat = raw === "right" ? "right" : "left";
            if (field === "w" && (o.type === "counter" || (o.type === "table" && ui.advanced))) {
                o.w = Math.round(n);
                if (o.type === "table" && o.shape === "round")
                    o.h = o.w;
            }
            if (field === "h" && (o.type === "counter" || (o.type === "table" && ui.advanced)))
                o.h = Math.round(n);
            if (field === "length" && o.type === "wall")
                o.length = n * 100;
            if (field === "thickness" && o.type === "wall")
                o.thickness = n;
            if (field === "angle" && o.type === "wall")
                o.angle = n;
            if (field === "side" && o.type === "door")
                o.side = oneOf(SIDES, raw) ?? o.side;
            if (field === "doorWidth" && o.type === "door")
                o.width = n;
            if (field === "offset" && o.type === "door")
                o.offset = n;
        }
        const tooLarge = a.objects.find((x) => !G.available(x, a.room));
        if (tooLarge) {
            const back = JSON.parse(before);
            a.room = back.room;
            a.objects = back.objects;
            a.links = back.links;
            render();
            status("Raum oder Möbelmaß passt nicht: " + label(tooLarge) + " ist dafür zu groß.", true);
            return;
        }
        let moved = 0;
        a.objects.forEach((x) => { const old = JSON.stringify(x); G.fit(x, a.room); if (JSON.stringify(x) !== old)
            moved++; });
        commit(field.indexOf("room") === 0 ? "Raum angepasst" + (moved ? " · " + moved + " Objekte an den Raum angepasst" : "") : "Eigenschaften angepasst", before);
    });
    function point(event) {
        const b = floor.getBoundingClientRect(), room = cur().room;
        return { x: G.clamp(snap((event.clientX - b.left) / scale), 0, room.w), y: G.clamp(snap((event.clientY - b.top) / scale), 0, room.h) };
    }
    floor.addEventListener("pointerdown", (event) => {
        if (!area || event.button !== 0 || !event.isPrimary)
            return;
        // The plan keeps the focus where it is (preventDefault below), so a field
        // being typed into would be redrawn before its change counts: commit it first.
        const typing = document.activeElement;
        if (typing instanceof HTMLElement && props.contains(typing))
            typing.blur();
        const p = point(event);
        if (mode === "wall") {
            event.preventDefault();
            drag = { type: "draw", endpoint: null, pointerId: event.pointerId, start: drawStart || p, wasStarted: !!drawStart,
                before: "", area: null, original: null, id: "", moved: false, startScreen: { x: event.clientX, y: event.clientY } };
            drawStart = drag.start;
            preview = { a: drawStart, b: p };
            floor.setPointerCapture(event.pointerId);
            renderScene();
            return;
        }
        const holder = event.target instanceof Element ? event.target.closest("[data-id]") : null;
        if (!holder) {
            if (selectedId !== "room") {
                selectedId = "room";
                inspector();
                renderScene();
            }
            return;
        }
        const o = holder.dataset.id ? find(holder.dataset.id) : undefined;
        if (!o)
            return;
        event.preventDefault();
        selectedId = o.id;
        const endpoint = event.target instanceof Element ? event.target.closest("[data-endpoint]") : null;
        const endpointIndex = endpoint ? Number(endpoint.dataset.endpoint) : null;
        drag = { type: endpoint ? "endpoint" : "move", endpoint: endpointIndex, pointerId: event.pointerId, wasStarted: false,
            before: content(area), area, original: clone(o), id: o.id, start: p, moved: false, startScreen: { x: event.clientX, y: event.clientY } };
        floor.setPointerCapture(event.pointerId);
        inspector();
        renderScene();
        const focus = endpoint ? floor.querySelector('[data-endpoint="' + endpointIndex + '"]') : floor.querySelector('[data-id="' + o.id + '"] button');
        if (focus instanceof HTMLElement)
            focus.focus({ preventScroll: true });
    });
    floor.addEventListener("pointermove", (event) => {
        if (!area)
            return;
        const p = point(event);
        if (!drag) {
            if (mode === "wall" && drawStart) {
                preview = { a: drawStart, b: p };
                renderScene();
            }
            return;
        }
        if (event.pointerId !== drag.pointerId)
            return;
        if (drag.type === "draw") {
            preview = { a: drag.start, b: p };
            renderScene();
            return;
        }
        if (!drag.moved && Math.hypot(event.clientX - drag.startScreen.x, event.clientY - drag.startScreen.y) < 4)
            return;
        drag.moved = true;
        const o = find(drag.id), original = drag.original, dx = p.x - drag.start.x, dy = p.y - drag.start.y;
        if (!o || !original)
            return;
        if (drag.type === "endpoint") {
            if (original.type !== "wall" || o.type !== "wall" || drag.endpoint === null)
                return;
            const ends = G.endpoints(original);
            ends[drag.endpoint === 0 ? 0 : 1] = p;
            G.wallFromPoints(o, ends[0], ends[1]);
            G.fit(o, area.room);
        }
        else if (o.type === "door" && original.type === "door") {
            const start = G.doorPoint(original, area.room);
            G.moveDoor(o, { x: start.x + dx, y: start.y + dy }, area.room, snap);
        }
        else if (o.type !== "door" && original.type !== "door") {
            o.x = original.x + dx;
            o.y = original.y + dy;
            G.fit(o, area.room);
        }
        renderScene();
    });
    function endPointer(event) {
        if (!drag || event.pointerId !== drag.pointerId)
            return;
        const d = drag;
        drag = null;
        if (floor.hasPointerCapture(event.pointerId))
            floor.releasePointerCapture(event.pointerId);
        ignoreClickUntil = Date.now() + 180;
        if (event.type === "pointercancel") {
            cancelDrag(d);
            render();
            status("Bewegung abgebrochen.");
            return;
        }
        if (d.type === "draw") {
            const p = point(event), moved = Math.hypot(event.clientX - d.startScreen.x, event.clientY - d.startScreen.y) > 4;
            if (moved || d.wasStarted)
                finishWall(d.start, p);
            else {
                preview = { a: d.start, b: d.start };
                renderScene();
                status("Jetzt den zweiten Wandpunkt setzen.");
            }
            return;
        }
        const shown = find(d.id) || d.original;
        if (d.moved && shown)
            commit(label(shown) + " verschoben", d.before);
        else
            render();
    }
    floor.addEventListener("pointerup", endPointer);
    floor.addEventListener("pointercancel", endPointer);
    root.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            cancelDrag(drag);
            mode = "select";
            render();
            status("Zeichnen oder Verschieben abgebrochen.");
            return;
        }
        const target = event.target;
        if (!(target instanceof HTMLElement) || target.matches("input,select,textarea"))
            return;
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
            event.preventDefault();
            undo();
            return;
        }
        const o = selected();
        if (event.key === "Delete" && o) {
            event.preventDefault();
            remove(o);
            return;
        }
        const deltas = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
        const delta = deltas[event.key];
        if (!delta || !floor.contains(target) || !o || !area)
            return;
        event.preventDefault();
        const before = content(area), unit = event.shiftKey ? 1 : ui.grid ? 10 : 5, endpoint = target.dataset.endpoint;
        if (o.type === "wall" && endpoint !== undefined) {
            const ends = G.endpoints(o), pt = ends[Number(endpoint) === 0 ? 0 : 1];
            pt.x = G.clamp(pt.x + delta[0] * unit, 0, area.room.w);
            pt.y = G.clamp(pt.y + delta[1] * unit, 0, area.room.h);
            G.wallFromPoints(o, ends[0], ends[1]);
        }
        else if (o.type === "door") {
            const dp = G.doorPoint(o, area.room);
            G.moveDoor(o, { x: dp.x + delta[0] * unit, y: dp.y + delta[1] * unit }, area.room, snap);
        }
        else {
            o.x += delta[0] * unit;
            o.y += delta[1] * unit;
        }
        G.fit(o, area.room);
        commit(label(o) + " verschoben", before);
    });
    // ---- start -----------------------------------------------------------------
    function load() {
        fetch("/gui/raumplan/plan", { credentials: "same-origin", headers: { Accept: "application/json" } })
            .then((res) => res.json().then((data) => ({ status: res.status, data })))
            .then((res) => {
            root.dataset.busy = "false";
            const data = record(res.data);
            if (res.status !== 200) {
                status((typeof data.say === "string" && data.say) || "Der Raumplan konnte nicht geladen werden.", true);
                render();
                return;
            }
            areas = Array.isArray(data.areas) ? data.areas.map((doc) => fromDoc(doc)) : [];
            const first = areas[0];
            if (!first) {
                render();
                openForm();
                status("Noch kein Raum angelegt. Bitte zuerst einen Raum oder eine Terrasse erstellen.");
                return;
            }
            activate(first);
            status("Raumgröße, Eingänge und Möbel sind frei anpassbar. Jede Änderung wird sofort gespeichert.");
        })
            .catch(() => {
            root.dataset.busy = "false";
            status("Der Raumplan konnte nicht geladen werden: keine Verbindung zum Server.", true);
        });
    }
    new ResizeObserver(() => { layout(); }).observe(stage);
    load();
})();
