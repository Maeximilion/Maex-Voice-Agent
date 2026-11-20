// The fake backend of the public tablet demo (#892, B2). Loaded first on every page
// of /tablet-demo: it replaces the browser's XMLHttpRequest (htmx sends through it),
// fetch and EventSource, so every request the real pages make for the operations view
// ("/gui/...") is answered here, in the visitor's browser. Nothing leaves it: there
// is no server behind the demo, and each visitor has a restaurant of their own that a
// reload resets (docs/06 §11).
//
// The pages and scripts are the real board's. Which request is answered how is
// decided once in routes.json (scripts/demo/tablet_demo_routes.json); this file
// answers the "state" routes of the board from the visitor's state (demo-board.ts)
// and serves everything the build rendered (captures.json) as it is. A route that is
// not built yet answers with a line on the board instead of an error.
//
// Source: api/gui/ts/demo-backend.ts. The file of the same name in api/gui/static/ is
// its compiled output (`npm run build`, docs/22); edit the source, never the output.
import { demoLinks, Restaurant } from "./demo-board.js";
import { dayLabelOf, filterCalls, filterMenu, Sheets } from "./demo-sheets.js";
export {};
// Where the build wrote the demo (api/site/tablet-demo/), as the site serves it.
const BASE = "/tablet-demo/";
const HTML = "text/html; charset=utf-8";
const JSON_TYPE = "application/json";
// How long the print bridge takes to report a slip at the register.
const SLIP_MS = 1800;
const LOCKED = "In der echten Version mit Inhaber-PIN.";
const LATER = "Dieser Teil der Demo folgt in einer späteren Version.";
const realFetch = window.fetch.bind(window);
const started = Date.now();
const html = (body, status = 200, headers = {}) => ({
    status,
    headers: { "Content-Type": HTML, ...headers },
    body,
});
const notice = (line) => ({
    // No swap, only the board's line for a few seconds (HX-Trigger "order-notice").
    status: 200,
    headers: { "Content-Type": HTML, "HX-Reswap": "none", "HX-Trigger": JSON.stringify({ "order-notice": line }) },
    body: "",
});
const missing = () => ({
    status: 404,
    headers: { "Content-Type": JSON_TYPE },
    body: JSON.stringify({ detail: "Not Found" }),
});
// --- Server-sent events: what the stream of the real server tells the board --------
const streams = new Set();
class FakeEventSource extends EventTarget {
    constructor(url) {
        super();
        this.CONNECTING = 0;
        this.OPEN = 1;
        this.CLOSED = 2;
        this.readyState = 0;
        this.onopen = null;
        this.onerror = null;
        this.onmessage = null;
        this.withCredentials = false;
        this.url = String(url);
        streams.add(this);
        setTimeout(() => {
            if (this.readyState === 2)
                return;
            this.readyState = 1;
            if (this.onopen)
                this.onopen(new Event("open"));
        }, 0);
    }
    close() {
        this.readyState = 2;
        streams.delete(this);
    }
}
FakeEventSource.CONNECTING = 0;
FakeEventSource.OPEN = 1;
FakeEventSource.CLOSED = 2;
function emit(...names) {
    for (const name of names)
        for (const s of streams)
            s.dispatchEvent(new MessageEvent(name, { data: "" }));
}
async function json(file) {
    const res = await realFetch(BASE + file);
    if (!res.ok)
        throw new Error(`${file}: ${res.status}`);
    return (await res.json());
}
// Loaded once, by the first request; a failure is a lost connection for the board.
let world = null;
function load() {
    world ?? (world = Promise.all([
        json("routes.json"),
        json("captures.json"),
        json("state.json"),
    ]).then(([routes, captures, state]) => {
        const restaurant = new Restaurant(state);
        return { routes: routes.routes, captures, restaurant, sheets: new Sheets(state, restaurant) };
    }));
    return world;
}
const WAIT_LABEL = /^\/gui\/kopfzeile\/wartezeit\/([^/]+)$/;
// The bridge reports a slip a moment after it was sent; the board follows on its own.
function printSoon(r) {
    if (r.pendingSlips().length === 0)
        return;
    setTimeout(() => {
        let changed = false;
        for (const id of r.pendingSlips())
            changed = r.slipPrinted(id) || changed;
        if (changed)
            emit("orders");
    }, SLIP_MS);
}
// What a sheet answers, as a reply of the backend: the trigger header the board listens
// for, and the events the real server's stream would send afterwards.
function sheet(reply) {
    const headers = { "Content-Type": HTML };
    if (reply.trigger)
        headers["HX-Trigger"] = JSON.stringify(reply.trigger);
    if (reply.events?.length)
        setTimeout(() => emit(...(reply.events ?? [])), 0);
    return { status: reply.status, headers, body: reply.html };
}
// A rendered page of the build, as it opens (the pages that take a filter start from it).
async function captured(w, url) {
    const c = w.captures.find((x) => x.method === "GET" && x.url === url);
    if (!c)
        return null;
    const res = await realFetch(BASE + c.file);
    return res.ok ? res.text() : null;
}
function handlers(w) {
    const r = w.restaurant;
    const s = w.sheets;
    const g = (reply) => (reply ? sheet(reply) : null);
    const header = (tap) => tap ? html(r.kopfzeile(), tap.status) : null;
    const column = (tap) => {
        const reply = html(r.bestellungen(), tap.status);
        if (tap.notice)
            reply.headers["HX-Trigger"] = JSON.stringify({ "order-notice": tap.notice });
        return reply;
    };
    return [
        // --- B3: the sheets and the filters of the pages ---
        ["GET", /^\/gui\/gericht-aus$/, () => html(s.dishSheet())],
        ["GET", /^\/gui\/fragments\/gericht-aus$/, (_m, f) => html(s.dishSearch(f.get("q") ?? ""))],
        ["POST", /^\/gui\/gericht-aus\/([^/]+)\/([^/]+)$/, (m) => g(s.dishSwitch(m[1] ?? "", m[2] ?? "")) ?? missing()],
        ["GET", /^\/gui\/bestellungen\/([^/]+)\/korrigieren$/, (m) => g(s.correctionOpen(m[1] ?? ""))],
        ["POST", /^\/gui\/bestellungen\/([^/]+)\/korrigieren\/vorschau$/, (m, f) => g(s.correctionTap(m[1] ?? "", f))],
        ["POST", /^\/gui\/bestellungen\/([^/]+)\/korrigieren$/, (m, f) => {
                const reply = g(s.correctionSave(m[1] ?? "", f));
                // A slip that was sent is printed again; the board follows on its own.
                printSoon(r);
                return reply;
            }],
        ["GET", /^\/gui\/reservierung$/, () => g(s.entryOpen())],
        ["POST", /^\/gui\/reservierung$/, (_m, f) => g(s.entryCheck(f))],
        ["POST", /^\/gui\/reservierung\/([^/]+)\/eintragen$/, (m, f) => g(s.entryConfirm(m[1] ?? "", f)) ?? missing()],
        ["GET", /^\/gui\/teamnummer$/, () => g(s.teamOpen())],
        ["POST", /^\/gui\/teamnummer$/, (_m, f) => g(s.teamChange(f))],
        ["GET", /^\/gui\/tageszeiten$/, (_m, f) => g(s.hoursOpen(f.get("datum") ?? ""))],
        ["POST", /^\/gui\/tageszeiten$/, (_m, f) => g(s.hoursChange(f))],
        ["GET", /^\/gui\/fragments\/pruefen$/, () => html(s.checkCards())],
        ["GET", /^\/gui\/anrufe$/, async (_m, f) => {
                const day = f.get("tag") ?? "";
                const page = (await captured(w, day ? `/gui/anrufe?tag=${day}` : "/gui/anrufe")) ?? (await captured(w, "/gui/anrufe"));
                if (page === null)
                    return null;
                return html(filterCalls(page, f.get("ergebnis") ?? "", f.get("anliegen") ?? "", dayLabelOf(page, day || (/<option value="([^"]*)" selected>/.exec(page)?.[1] ?? ""))));
            }],
        ["GET", /^\/gui\/menue$/, async (_m, f) => {
                const page = await captured(w, "/gui/menue");
                // The page is the answer; htmx must not push this address (it is no file of the demo).
                return page === null ? null : html(filterMenu(page, f.get("q") ?? "", f.get("hinweis") ?? ""), 200, { "HX-Push-Url": "false" });
            }],
        ["GET", /^\/gui\/fragments\/kopfzeile$/, () => html(r.kopfzeile())],
        ["GET", /^\/gui\/fragments\/bestellungen$/, () => html(r.bestellungen())],
        ["GET", /^\/gui\/fragments\/rueckrufe$/, () => html(r.rueckrufe())],
        ["GET", /^\/gui\/fragments\/heute$/, () => html(r.heute())],
        ["GET", /^\/gui\/rueckrufe\/verlauf$/, () => html(r.verlauf())],
        ["GET", /^\/gui\/wartezeit\/([^/]+)$/, (m) => {
                const sheet = r.waitSheet(m[1] ?? "");
                return sheet === null ? missing() : html(sheet);
            }],
        ["POST", /^\/gui\/kopfzeile\/ki-pausieren$/, () => header(r.pause())],
        ["POST", /^\/gui\/kopfzeile\/ki-einschalten$/, () => header(r.resume())],
        ["POST", /^\/gui\/kopfzeile\/modus\/([^/]+)$/, (m) => header(r.setMode(m[1] ?? "")) ?? missing()],
        ["POST", /^\/gui\/kopfzeile\/wartezeit\/([^/]+)\/(-?[0-9]+)$/, (m) => header(r.stepWait(m[1] ?? "", Number(m[2]))) ?? missing()],
        ["POST", /^\/gui\/kopfzeile\/wartezeit\/([^/]+)$/, (m, f) => header(r.setWait(m[1] ?? "", f.get("minuten") ?? "")) ?? missing()],
        ["POST", /^\/gui\/kopfzeile\/standardzeit\/([^/]+)\/zurueck$/, (m) => header(r.resetWait(m[1] ?? "")) ?? missing()],
        ["POST", /^\/gui\/kopfzeile\/standardzeit\/([^/]+)$/, (m, f) => header(r.setStandard(m[1] ?? "", f.get("minuten") ?? "")) ?? missing()],
        // After the wait routes: this pattern would take their paths too (as in the real router).
        ["POST", /^\/gui\/kopfzeile\/([^/]+)\/([^/]+)$/, (m) => header(r.setService(m[1] ?? "", m[2] ?? "")) ?? missing()],
        ["POST", /^\/gui\/bestellungen\/([^/]+)\/passt$/, (m) => {
                const reply = column(r.approve(m[1] ?? ""));
                printSoon(r);
                return reply;
            }],
        ["POST", /^\/gui\/bestellungen\/([^/]+)\/nochmal-senden$/, (m) => {
                const reply = column(r.resend(m[1] ?? ""));
                printSoon(r);
                return reply;
            }],
        ["POST", /^\/gui\/bestellungen\/([^/]+)\/in-kasse\/([^/]+)$/, (m) => column(r.markEntered(m[1] ?? "", m[2] ?? ""))],
        ["POST", /^\/gui\/bestellungen\/([^/]+)\/storniert\/([^/]+)$/, (m) => column(r.cancel(m[1] ?? "", m[2] ?? ""))],
        ["POST", /^\/gui\/rueckrufe\/([^/]+)\/erledigt$/, (m) => {
                r.callbackDone(m[1] ?? "");
                return html(r.rueckrufe());
            }],
        ["POST", /^\/gui\/rueckrufe\/([^/]+)\/wieder-offen$/, (m) => {
                r.callbackReopen(m[1] ?? "");
                // The column follows through the event stream, as on the real board.
                setTimeout(() => emit("callbacks"), 0);
                return html(r.verlauf());
            }],
    ];
}
function pattern(path) {
    return new RegExp("^" + path.replace(/\{[^}]+\}/g, "[^/]+").replace(/\/$/, "/?") + "$");
}
async function answer(method, address, body) {
    const w = await load();
    w.restaurant.tick((Date.now() - started) / 60000);
    const url = new URL(address, location.href);
    const path = url.pathname;
    for (const [verb, re, handler] of handlers(w)) {
        if (verb !== method)
            continue;
        const m = re.exec(path);
        if (!m)
            continue;
        const reply = await handler(m, method === "GET" ? url.searchParams : body);
        if (reply) {
            // Links to rendered pages lead to their files, as in the pages the build wrote.
            const pages = w.routes.filter((r) => r.answer === "page").map((r) => r.path);
            reply.body = demoLinks(reply.body, pages, BASE.replace(/\/$/, ""));
            return reply;
        }
    }
    // What the build rendered: the sheets and pages as they open, the same for everyone.
    const route = w.routes.find((r) => r.method === method && pattern(r.path).test(path));
    if (route?.answer === "locked")
        return notice(LOCKED);
    const asked = path + url.search;
    const capture = w.captures.find((c) => c.method === method &&
        (c.url === asked || c.url === path) &&
        (c.body === null || c.body["anruf"] === body.get("anruf")));
    if (capture) {
        const res = await realFetch(BASE + capture.file);
        if (res.ok) {
            const type = capture.file.endsWith(".json") ? JSON_TYPE : HTML;
            return { status: 200, headers: { "Content-Type": type }, body: await res.text() };
        }
    }
    if (!route || route.answer === "absent")
        return missing();
    return notice(LATER);
}
// --- XMLHttpRequest, as far as htmx uses it ----------------------------------------
function formOf(body) {
    if (typeof body === "string")
        return new URLSearchParams(body);
    const form = new URLSearchParams();
    if (body instanceof FormData) {
        body.forEach((value, key) => {
            if (typeof value === "string")
                form.append(key, value);
        });
    }
    return form;
}
class FakeXhr extends EventTarget {
    constructor() {
        super(...arguments);
        this.readyState = 0;
        this.status = 0;
        this.statusText = "";
        this.responseText = "";
        this.response = "";
        this.responseURL = "";
        this.responseType = "";
        this.timeout = 0;
        this.withCredentials = false;
        this.upload = new EventTarget();
        this.onload = null;
        this.onerror = null;
        this.onabort = null;
        this.ontimeout = null;
        this.onreadystatechange = null;
        this.method = "GET";
        this.address = "";
        this.reply = null;
        this.aborted = false;
    }
    open(method, url) {
        this.method = method.toUpperCase();
        this.address = String(url);
        this.readyState = 1;
    }
    setRequestHeader() { }
    overrideMimeType() { }
    send(body) {
        answer(this.method, this.address, formOf(body)).then((reply) => {
            if (this.aborted)
                return;
            this.reply = reply;
            this.status = reply.status;
            this.statusText = reply.status === 200 ? "OK" : "";
            this.responseText = reply.body;
            this.response = reply.body;
            this.responseURL = new URL(this.address, location.href).href;
            this.readyState = 4;
            const done = new Event("load");
            if (this.onload)
                this.onload.call(this, done);
            this.dispatchEvent(done);
        }, () => {
            // The board's yellow bar: no connection.
            if (this.aborted)
                return;
            const lost = new Event("error");
            if (this.onerror)
                this.onerror.call(this, lost);
            this.dispatchEvent(lost);
        });
    }
    abort() {
        this.aborted = true;
        if (this.onabort)
            this.onabort.call(this, new Event("abort"));
    }
    getResponseHeader(name) {
        if (!this.reply)
            return null;
        const key = Object.keys(this.reply.headers).find((k) => k.toLowerCase() === name.toLowerCase());
        return key === undefined ? null : (this.reply.headers[key] ?? null);
    }
    getAllResponseHeaders() {
        if (!this.reply)
            return "";
        return Object.entries(this.reply.headers)
            .map(([k, v]) => `${k.toLowerCase()}: ${v}\r\n`)
            .join("");
    }
}
FakeXhr.UNSENT = 0;
FakeXhr.OPENED = 1;
FakeXhr.HEADERS_RECEIVED = 2;
FakeXhr.LOADING = 3;
FakeXhr.DONE = 4;
window.XMLHttpRequest = FakeXhr;
window.EventSource = FakeEventSource;
// fetch: the board's own requests for "/gui/" (the lost-connection probe, the floor
// plan); anything else is the browser's.
window.fetch = async (input, init) => {
    const raw = input instanceof Request ? input.url : String(input);
    const url = new URL(raw, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith("/gui/"))
        return realFetch(input, init);
    const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
    const reply = await answer(method, url.pathname + url.search, formOf(init?.body));
    return new Response(reply.body, { status: reply.status, headers: reply.headers });
};
// --- A line for the pages without the board's script -------------------------------
// The board shows "order-notice" itself (app.ts); the sub pages (Lieferung, Zeiten,
// Daten, Geräte, ...) load no board script, so a locked or not yet built control would
// do nothing visible there. They get the same line, in the flow of the page.
function notes() {
    if (document.getElementById("ton-an"))
        return;
    let timer;
    document.body.addEventListener("order-notice", (e) => {
        const value = e instanceof CustomEvent ? e.detail?.value : null;
        if (typeof value !== "string")
            return;
        let line = document.getElementById("demo-note");
        if (!line) {
            line = document.createElement("p");
            line.id = "demo-note";
            line.className = "demo-note";
            line.setAttribute("role", "status");
            const notice = document.querySelector(".demo-notice");
            if (notice)
                notice.after(line);
            else
                document.body.prepend(line);
        }
        line.textContent = value;
        window.clearTimeout(timer);
        timer = window.setTimeout(() => line?.remove(), 6000);
    });
}
// --- The demo bar: what the real board does not have ------------------------------
function bar() {
    const head = document.getElementById("kopfzeile");
    if (!head || document.getElementById("demo-bar"))
        return;
    const box = document.createElement("div");
    box.id = "demo-bar";
    box.className = "demo-bar";
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", "Demo");
    const text = document.createElement("p");
    text.className = "demo-bar-text";
    text.textContent = "Demo: ein Beispiel-Restaurant, alles lässt sich ausprobieren. Ein Neuladen setzt es zurück.";
    const call = document.createElement("button");
    call.type = "button";
    call.className = "primary";
    call.textContent = "Beispielanruf";
    const status = document.createElement("p");
    status.className = "demo-bar-status";
    status.setAttribute("role", "status");
    call.addEventListener("click", () => {
        call.disabled = true;
        load().then(async (w) => {
            const out = w.restaurant.exampleCall();
            const done = [];
            if (out.order)
                done.push("eine Bestellung");
            if (out.reservation)
                done.push("eine Reservierung");
            if (out.callback)
                done.push("einen Rückruf");
            status.textContent = `Beispielanruf angenommen: ${done.join(" und ")} auf dem Brett.`;
            emit(...(out.order ? ["orders"] : []), ...(out.reservation ? ["today"] : []), ...(out.callback ? ["callbacks"] : []));
            call.disabled = false;
        }, () => {
            status.textContent = "Die Demo konnte nicht geladen werden.";
            call.disabled = false;
        });
    });
    box.append(text, call, status);
    head.after(box);
}
function ready() {
    bar();
    notes();
}
if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", ready);
else
    ready();
