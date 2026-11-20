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

import { demoLinks, Restaurant, type Recorded } from "./demo-board.js";
import { dayLabelOf, filterCalls, filterMenu, Sheets, type Reply as SheetReply } from "./demo-sheets.js";

export {};

// Where the build wrote the demo (api/site/tablet-demo/), as the site serves it.
const BASE = "/tablet-demo/";
const HTML = "text/html; charset=utf-8";
const JSON_TYPE = "application/json";
// How long the print bridge takes to report a slip at the register.
const SLIP_MS = 1800;
const LOCKED = "In der echten Version mit Inhaber-PIN.";
const LATER = "Dieser Teil der Demo folgt in einer späteren Version.";

type Reply = { status: number; headers: Record<string, string>; body: string };
type Route = { method: string; path: string; answer: string };
type Capture = { method: string; path: string; url: string; body: Record<string, string> | null; file: string };
type Handler = (m: string[], form: URLSearchParams) => Reply | null | Promise<Reply | null>;

const realFetch = window.fetch.bind(window);
const started = Date.now();

const html = (body: string, status = 200, headers: Record<string, string> = {}): Reply => ({
  status,
  headers: { "Content-Type": HTML, ...headers },
  body,
});
const notice = (line: string): Reply => ({
  // No swap, only the board's line for a few seconds (HX-Trigger "order-notice").
  status: 200,
  headers: { "Content-Type": HTML, "HX-Reswap": "none", "HX-Trigger": JSON.stringify({ "order-notice": line }) },
  body: "",
});
const missing = (): Reply => ({
  status: 404,
  headers: { "Content-Type": JSON_TYPE },
  body: JSON.stringify({ detail: "Not Found" }),
});

// --- Server-sent events: what the stream of the real server tells the board --------

const streams = new Set<FakeEventSource>();

class FakeEventSource extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSED = 2;
  readyState = 0;
  onopen: ((e: Event) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  readonly url: string;
  readonly withCredentials = false;

  constructor(url: string | URL) {
    super();
    this.url = String(url);
    streams.add(this);
    setTimeout(() => {
      if (this.readyState === 2) return;
      this.readyState = 1;
      if (this.onopen) this.onopen(new Event("open"));
    }, 0);
  }

  close(): void {
    this.readyState = 2;
    streams.delete(this);
  }
}

function emit(...names: string[]): void {
  for (const name of names) for (const s of streams) s.dispatchEvent(new MessageEvent(name, { data: "" }));
}

// --- The visitor's restaurant and the answers --------------------------------------

type World = { routes: Route[]; captures: Capture[]; restaurant: Restaurant; sheets: Sheets };

async function json<T>(file: string): Promise<T> {
  const res = await realFetch(BASE + file);
  if (!res.ok) throw new Error(`${file}: ${res.status}`);
  return (await res.json()) as T;
}

// Loaded once, by the first request; a failure is a lost connection for the board.
let world: Promise<World> | null = null;
function load(): Promise<World> {
  world ??= Promise.all([
    json<{ routes: Route[] }>("routes.json"),
    json<Capture[]>("captures.json"),
    json<Recorded>("state.json"),
  ]).then(([routes, captures, state]) => {
    const restaurant = new Restaurant(state);
    return { routes: routes.routes, captures, restaurant, sheets: new Sheets(state, restaurant) };
  });
  return world;
}

const WAIT_LABEL = /^\/gui\/kopfzeile\/wartezeit\/([^/]+)$/;

// The bridge reports a slip a moment after it was sent; the board follows on its own.
function printSoon(r: Restaurant): void {
  if (r.pendingSlips().length === 0) return;
  setTimeout(() => {
    let changed = false;
    for (const id of r.pendingSlips()) changed = r.slipPrinted(id) || changed;
    if (changed) emit("orders");
  }, SLIP_MS);
}

// What a sheet answers, as a reply of the backend: the trigger header the board listens
// for, and the events the real server's stream would send afterwards.
function sheet(reply: SheetReply): Reply {
  const headers: Record<string, string> = { "Content-Type": HTML };
  if (reply.trigger) headers["HX-Trigger"] = JSON.stringify(reply.trigger);
  if (reply.events?.length) setTimeout(() => emit(...(reply.events ?? [])), 0);
  return { status: reply.status, headers, body: reply.html };
}

// A rendered page of the build, as it opens (the pages that take a filter start from it).
async function captured(w: World, url: string): Promise<string | null> {
  const c = w.captures.find((x) => x.method === "GET" && x.url === url);
  if (!c) return null;
  const res = await realFetch(BASE + c.file);
  return res.ok ? res.text() : null;
}

function handlers(w: World): [string, RegExp, Handler][] {
  const r = w.restaurant;
  const s = w.sheets;
  const g = (reply: SheetReply | null): Reply | null => (reply ? sheet(reply) : null);
  const header = (tap: { status: number } | null): Reply | null =>
    tap ? html(r.kopfzeile(), tap.status) : null;
  const column = (tap: { status: number; notice?: string }): Reply => {
    const reply = html(r.bestellungen(), tap.status);
    if (tap.notice) reply.headers["HX-Trigger"] = JSON.stringify({ "order-notice": tap.notice });
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
    ["POST", /^\/gui\/reservierung\/([^/]+)\/eintragen$/, (m) => g(s.entryConfirm(m[1] ?? "")) ?? missing()],
    ["GET", /^\/gui\/teamnummer$/, () => g(s.teamOpen())],
    ["POST", /^\/gui\/teamnummer$/, (_m, f) => g(s.teamChange(f))],
    ["GET", /^\/gui\/tageszeiten$/, (_m, f) => g(s.hoursOpen(f.get("datum") ?? ""))],
    ["POST", /^\/gui\/tageszeiten$/, (_m, f) => g(s.hoursChange(f))],
    ["GET", /^\/gui\/fragments\/pruefen$/, () => html(s.checkCards())],
    ["GET", /^\/gui\/anrufe$/, async (_m, f) => {
      const day = f.get("tag") ?? "";
      const page = (await captured(w, day ? `/gui/anrufe?tag=${day}` : "/gui/anrufe")) ?? (await captured(w, "/gui/anrufe"));
      if (page === null) return null;
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
    ["POST", /^\/gui\/kopfzeile\/wartezeit\/([^/]+)\/(-?[0-9]+)$/, (m) =>
      header(r.stepWait(m[1] ?? "", Number(m[2]))) ?? missing()],
    ["POST", /^\/gui\/kopfzeile\/wartezeit\/([^/]+)$/, (m, f) =>
      header(r.setWait(m[1] ?? "", f.get("minuten") ?? "")) ?? missing()],
    ["POST", /^\/gui\/kopfzeile\/standardzeit\/([^/]+)\/zurueck$/, (m) => header(r.resetWait(m[1] ?? "")) ?? missing()],
    ["POST", /^\/gui\/kopfzeile\/standardzeit\/([^/]+)$/, (m, f) =>
      header(r.setStandard(m[1] ?? "", f.get("minuten") ?? "")) ?? missing()],
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

function pattern(path: string): RegExp {
  return new RegExp("^" + path.replace(/\{[^}]+\}/g, "[^/]+").replace(/\/$/, "/?") + "$");
}

async function answer(method: string, address: string, body: URLSearchParams): Promise<Reply> {
  const w = await load();
  w.restaurant.tick((Date.now() - started) / 60000);
  const url = new URL(address, location.href);
  const path = url.pathname;
  for (const [verb, re, handler] of handlers(w)) {
    if (verb !== method) continue;
    const m = re.exec(path);
    if (!m) continue;
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
  if (route?.answer === "locked") return notice(LOCKED);
  const asked = path + url.search;
  const capture = w.captures.find(
    (c) =>
      c.method === method &&
      (c.url === asked || c.url === path) &&
      (c.body === null || c.body["anruf"] === body.get("anruf")),
  );
  if (capture) {
    const res = await realFetch(BASE + capture.file);
    if (res.ok) {
      const type = capture.file.endsWith(".json") ? JSON_TYPE : HTML;
      return { status: 200, headers: { "Content-Type": type }, body: await res.text() };
    }
  }
  if (!route || route.answer === "absent") return missing();
  return notice(LATER);
}

// --- XMLHttpRequest, as far as htmx uses it ----------------------------------------

function formOf(body: unknown): URLSearchParams {
  if (typeof body === "string") return new URLSearchParams(body);
  const form = new URLSearchParams();
  if (body instanceof FormData) {
    body.forEach((value, key) => {
      if (typeof value === "string") form.append(key, value);
    });
  }
  return form;
}

class FakeXhr extends EventTarget {
  static readonly UNSENT = 0;
  static readonly OPENED = 1;
  static readonly HEADERS_RECEIVED = 2;
  static readonly LOADING = 3;
  static readonly DONE = 4;
  readyState = 0;
  status = 0;
  statusText = "";
  responseText = "";
  response = "";
  responseURL = "";
  responseType = "";
  timeout = 0;
  withCredentials = false;
  readonly upload = new EventTarget();
  onload: ((e: Event) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  onabort: ((e: Event) => void) | null = null;
  ontimeout: ((e: Event) => void) | null = null;
  onreadystatechange: ((e: Event) => void) | null = null;
  private method = "GET";
  private address = "";
  private reply: Reply | null = null;
  private aborted = false;

  open(method: string, url: string | URL): void {
    this.method = method.toUpperCase();
    this.address = String(url);
    this.readyState = 1;
  }
  setRequestHeader(): void {}
  overrideMimeType(): void {}

  send(body?: unknown): void {
    answer(this.method, this.address, formOf(body)).then(
      (reply) => {
        if (this.aborted) return;
        this.reply = reply;
        this.status = reply.status;
        this.statusText = reply.status === 200 ? "OK" : "";
        this.responseText = reply.body;
        this.response = reply.body;
        this.responseURL = new URL(this.address, location.href).href;
        this.readyState = 4;
        const done = new Event("load");
        if (this.onload) this.onload.call(this, done);
        this.dispatchEvent(done);
      },
      () => {
        // The board's yellow bar: no connection.
        if (this.aborted) return;
        const lost = new Event("error");
        if (this.onerror) this.onerror.call(this, lost);
        this.dispatchEvent(lost);
      },
    );
  }

  abort(): void {
    this.aborted = true;
    if (this.onabort) this.onabort.call(this, new Event("abort"));
  }

  getResponseHeader(name: string): string | null {
    if (!this.reply) return null;
    const key = Object.keys(this.reply.headers).find((k) => k.toLowerCase() === name.toLowerCase());
    return key === undefined ? null : (this.reply.headers[key] ?? null);
  }

  getAllResponseHeaders(): string {
    if (!this.reply) return "";
    return Object.entries(this.reply.headers)
      .map(([k, v]) => `${k.toLowerCase()}: ${v}\r\n`)
      .join("");
  }
}

window.XMLHttpRequest = FakeXhr as unknown as typeof XMLHttpRequest;
window.EventSource = FakeEventSource as unknown as typeof EventSource;

// fetch: the board's own requests for "/gui/" (the lost-connection probe, the floor
// plan); anything else is the browser's.
window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const raw = input instanceof Request ? input.url : String(input);
  const url = new URL(raw, location.href);
  if (url.origin !== location.origin || !url.pathname.startsWith("/gui/")) return realFetch(input, init);
  const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
  const reply = await answer(method, url.pathname + url.search, formOf(init?.body));
  return new Response(reply.body, { status: reply.status, headers: reply.headers });
};

// --- A line for the pages without the board's script -------------------------------

// The board shows "order-notice" itself (app.ts); the sub pages (Lieferung, Zeiten,
// Daten, Geräte, ...) load no board script, so a locked or not yet built control would
// do nothing visible there. They get the same line, in the flow of the page.
function notes(): void {
  if (document.getElementById("ton-an")) return;
  let timer: number | undefined;
  document.body.addEventListener("order-notice", (e) => {
    const value: unknown = e instanceof CustomEvent ? (e.detail as { value?: unknown } | null)?.value : null;
    if (typeof value !== "string") return;
    let line = document.getElementById("demo-note");
    if (!line) {
      line = document.createElement("p");
      line.id = "demo-note";
      line.className = "demo-note";
      line.setAttribute("role", "status");
      const notice = document.querySelector(".demo-notice");
      if (notice) notice.after(line);
      else document.body.prepend(line);
    }
    line.textContent = value;
    window.clearTimeout(timer);
    timer = window.setTimeout(() => line?.remove(), 6000);
  });
}

// --- The demo bar: what the real board does not have ------------------------------

function bar(): void {
  const head = document.getElementById("kopfzeile");
  if (!head || document.getElementById("demo-bar")) return;
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
    load().then(
      async (w) => {
        const out = w.restaurant.exampleCall();
        const done: string[] = [];
        if (out.order) done.push("eine Bestellung");
        if (out.reservation) done.push("eine Reservierung");
        if (out.callback) done.push("einen Rückruf");
        status.textContent = `Beispielanruf angenommen: ${done.join(" und ")} auf dem Brett.`;
        emit(...(out.order ? ["orders"] : []), ...(out.reservation ? ["today"] : []), ...(out.callback ? ["callbacks"] : []));
        call.disabled = false;
      },
      () => {
        status.textContent = "Die Demo konnte nicht geladen werden.";
        call.disabled = false;
      },
    );
  });
  box.append(text, call, status);
  head.after(box);
}

// --- The filter of the call list ----------------------------------------------------------

// Its form is a plain GET, which the browser would send to the file server, and that
// ignores the query. The demo asks its own backend and swaps the list in, as htmx would.
function filters(): void {
  const form = document.querySelector<HTMLFormElement>('form.call-filter[action="/gui/anrufe"]');
  if (!form || form.dataset["demo"]) return;
  form.dataset["demo"] = "1";
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const query = new URLSearchParams();
    new FormData(form).forEach((value, key) => {
      if (typeof value === "string") query.append(key, value);
    });
    window
      .fetch(`/gui/anrufe?${query.toString()}`)
      .then((res) => res.text())
      .then((text) => {
        const next = new DOMParser().parseFromString(text, "text/html").querySelector("section.calls");
        const now = document.querySelector("section.calls");
        if (!next || !now) return;
        now.replaceWith(next);
        (window as unknown as { htmx?: { process(el: Element): void } }).htmx?.process(next);
        filters();
      })
      .catch(() => undefined);
  });
}

function ready(): void {
  bar();
  notes();
  filters();
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready);
else ready();
