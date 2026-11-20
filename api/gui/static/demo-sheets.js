// The sheets of the public tablet demo (#892, B3): "Gericht aus", the correction of an
// order, "Reservierung eintragen", the staff settings (team number, a date's hours)
// and the filters of the pages Anrufe and Menü. Like demo-board.ts it holds the
// visitor's state and makes the markup without a browser; the fake backend
// (demo-backend.ts) answers the requests of the real pages with these functions.
//
// The markup is the real templates' (api/gui/templates/fragments/gericht_aus*.html,
// korrektur.html, reservierung.html, teamnummer.html, tageszeiten.html, pruefen.html);
// the decisions are the real server's, rebuilt as far as the demo reaches (what is
// left out is listed in docs/06 §11). api/tests/demo_sheets.mjs renders the start state
// with this file and compares it, tag for tag, with the sheets the real view rendered.
//
// Source: api/gui/ts/demo-sheets.ts. The file of the same name in api/gui/static/ is
// its compiled output (`npm run build`, docs/22); edit the source, never the output.
import { esc, euro } from "./demo-board.js";
const ORDER_STALE = "Die Bestellung wurde inzwischen geändert. Bitte nochmal prüfen.";
const BAD_STATE = "Die Korrektur ist durcheinander geraten. Bitte neu öffnen.";
const NO_CHANGE = "Noch nichts geändert.";
const NO_POSITION = "Mindestens ein Gericht muss bleiben. Eine leere Bestellung ist eine Stornierung.";
const MAX_QUANTITY = 30;
// Pickup orders only: "falsche Adresse" belongs to deliveries, which the demo has none of.
const REASONS = { wrong_item: "falsches Gericht", wrong_quantity: "falsche Menge", other: "Sonstiges" };
const WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"];
const SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
// --- Text helpers --------------------------------------------------------------------
function two(n) {
    return String(n).padStart(2, "0");
}
// A typed text as the domain compares it: without what nobody sees, at most 60 characters.
export function searched(text) {
    let out = "";
    for (const ch of text) {
        if (/\s/u.test(ch))
            out += " ";
        else if (!/[\p{Cc}\p{Cf}]/u.test(ch))
            out += ch;
    }
    return out.trim().slice(0, 60).trimEnd();
}
function fold(text) {
    return text
        .normalize("NFD")
        .replace(/[̀-ͯ]/g, "")
        .toLowerCase()
        .replace(/ß/g, "ss");
}
// "007" is 7, "s2" stays "s2": the number as the card spells it, without leading zeros.
function canonical(number) {
    return number.toLowerCase().replace(/\s+/g, "").replace(/^([a-z]*)0+(?=\d)/, "$1");
}
// The search field of "Gericht aus" and "Menü" (domain/menu/sold_out.py `matches`): a
// number ("7" finds 07) or a part of the name; a number alone never matches every name.
export function matches(number, name, query) {
    const said = searched(query);
    if (!said)
        return true;
    if (/^[a-z]{0,2}\d/i.test(said) && canonical(number).startsWith(canonical(said)))
        return true;
    const part = fold(said).replace(/\b\d+[a-z]*\b/g, " ").replace(/\s+/g, " ").trim();
    return part !== "" && fold(name).replace(/\s+/g, " ").includes(part);
}
// Card order: 2 before 12, 23 before 23a, plain numbers before S2 (domain/menu/sold_out.py).
function numberKey(number) {
    const m = /^([a-z]*)(\d+)([a-z]*)$/.exec(number.toLowerCase());
    return m ? [m[1] ?? "", Number(m[2]), m[3] ?? ""] : ["￿", 1e9, number];
}
function byCard(a, b) {
    const [pa, na, sa] = numberKey(a.number);
    const [pb, nb, sb] = numberKey(b.number);
    return pa.localeCompare(pb) || na - nb || sa.localeCompare(sb);
}
// --- "Gericht aus" ---------------------------------------------------------------------
function dishRow(d, off) {
    const state = off ? "wieder-da" : "aus";
    return `<li class="dish${off ? " aus" : ""}" id="dish-${esc(d.id ?? d.number)}">
  <span class="dish-name"><span class="nr">${esc(d.number)}</span> ${esc(d.name)}${off ? ' <span class="badge warn">heute aus</span>' : ""}</span>
  <button type="button" class="${off ? "primary" : ""}"
          hx-post="/gui/gericht-aus/${esc(d.id ?? d.number)}/${state}"
          hx-target="closest li" hx-swap="outerHTML">${off ? "Wieder da" : "Heute aus"}</button>

</li>`;
}
function dishList(menu, isOff, query) {
    const shown = [...menu].sort(byCard).filter((d) => matches(d.number, d.name, query));
    const searching = searched(query) !== "";
    const groups = [];
    const out = searching ? [] : shown.filter((d) => isOff(d.number));
    if (out.length)
        groups.push({ category: "", dishes: out, off: true });
    for (const d of shown) {
        if (!searching && isOff(d.number))
            continue;
        let g = groups.find((x) => !x.off && x.category === d.category);
        if (!g)
            groups.push((g = { category: d.category, dishes: [], off: false }));
        g.dishes.push(d);
    }
    let html = `<div hx-sync="this:drop">`;
    for (const g of groups) {
        const rows = g.dishes.map((d) => dishRow(d, isOff(d.number))).join("");
        if (g.off) {
            html += `<section class="dish-group"><h3 class="dish-cat">Heute aus</h3><ul class="dishes">${rows}</ul></section>`;
            continue;
        }
        const count = g.dishes.filter((d) => isOff(d.number)).length;
        html += `<details class="dish-group" data-cat="${esc(g.category)}"${searching ? " open" : ""}>
    <summary class="dish-cat">
      <span class="cat-name">${esc(g.category)}</span>
      <span class="count">${g.dishes.length}</span>
      ${count ? `<span class="badge warn">${count} heute aus</span>` : ""}
    </summary><ul class="dishes">${rows}</ul></details>`;
    }
    if (!groups.length)
        html += `<p class="empty">Kein Gericht gefunden.</p>`;
    return html + "</div>";
}
function delta(dish, names) {
    let sum = 0;
    for (const g of dish?.groups ?? [])
        for (const o of g.options)
            if (names.includes(o.name))
                sum += o.delta;
    return sum;
}
function tap(order, op, label, cls = "", confirm = null) {
    return `<button type="button" class="${cls}"
        hx-post="/gui/bestellungen/${esc(order)}/korrigieren/vorschau"
        hx-vals='{"op": ${JSON.stringify(op)}}'
        hx-sync="closest form:drop" hx-disabled-elt="closest fieldset"
        ${confirm ? `hx-confirm="${esc(confirm)}"` : ""}
        >${esc(label)}</button>`;
}
function numberField(label, message) {
    return `<div class="nummer">
  <label for="number">${esc(label)}</label>
  <input type="text" id="number" name="number" inputmode="text" autocomplete="off">
  <button type="submit">Übernehmen</button>
</div>
${message ? `<p class="blocker" id="number-error" role="alert">${esc(message)}</p>` : ""}`;
}
function lineView(order, l, swapping, message) {
    const badge = l.kind === "removed"
        ? '<span class="badge danger">wird entfernt</span>'
        : l.kind === "swapped"
            ? '<span class="badge accent">getauscht</span>'
            : l.kind === "added"
                ? '<span class="badge accent">neu</span>'
                : l.kind === "changed"
                    ? '<span class="badge accent">Menge geändert</span>'
                    : "";
    const idx = l.index;
    return `<li class="edit-line ${l.kind}">
  <div class="edit-head">
    <span>${l.quantity}&times; <span class="nr">${esc(l.number)}</span> ${esc(l.name)}</span>
    ${badge}
    ${l.soldOut ? '<span class="badge warn">heute aus</span>' : ""}
    ${l.kind !== "removed" ? `<b class="price">${euro(l.cents)}</b>` : ""}
  </div>
  ${l.options.map((o) => `<div class="opt">&boxur; ${esc(o)}</div>`).join("")}
  ${l.note ? `<div class="note">Hinweis: ${esc(l.note)}</div>` : ""}
  ${l.groups
        .map((g, gi) => `<div class="choice">
    <span class="choice-name">${esc(g.group)}${g.required ? ' <span class="muted">(Pflicht)</span>' : ""}</span>
    ${g.options
        .map((o, oi) => {
        const picked = l.picked.some((p) => p[0] === g.group && p[1] === o.name);
        const text = (picked ? "Gewählt: " : "") + o.name + (o.delta ? " " + euro(o.delta) : "");
        return tap(order, `opt:${l.where}:${idx}:${gi}:${oi}`, text, picked ? "picked" : "");
    })
        .join("\n    ")}
  </div>`)
        .join("")}
  ${l.missing.length ? `<p class="blocker">Fehlt noch: ${esc(l.missing.join(", "))}</p>` : ""}
  <div class="edit-actions">
    ${l.kind === "removed"
        ? tap(order, `restore:${idx}`, "Zurück")
        : `${tap(order, `minus:${l.where}:${idx}`, "−", "qty")}
    ${tap(order, `plus:${l.where}:${idx}`, "+", "qty")}
    ${l.where === "r"
            ? `${l.note ? tap(order, `remove:${idx}`, "Entfernen", "", `Der Hinweis fällt mit weg: ${l.note} - Wirklich entfernen?`) : ""}
    ${swapping === idx ? tap(order, "unswap", "Nicht tauschen") : tap(order, `swap:${idx}`, "Tauschen")}
    ${l.kind === "swapped" ? tap(order, `restore:${idx}`, "Zurück") : ""}`
            : ""}`}
  </div>
  ${l.where === "r" && swapping === idx ? `<p class="hint">Menge und Hinweis bleiben.</p>\n  ${numberField("Tauschen gegen Nummer", message)}` : ""}
</li>`;
}
export function renderCorrection(order, plan, edit, message) {
    const foot = plan.blockers.find((b) => b !== NO_CHANGE) ?? plan.blockers[0] ?? null;
    const error = plan.blockers.some((b) => b !== NO_CHANGE);
    const swapField = edit.t === null || !plan.rows.some((r) => r.index === edit.t);
    const state = esc(JSON.stringify(edit));
    return `<section class="sheet korrektur" data-order="${esc(order)}" role="dialog" aria-labelledby="korrektur-titel">
  <div class="sheet-head">
    <h2 id="korrektur-titel" tabindex="-1">Bestellung korrigieren</h2>
    <button type="button" class="schliessen" data-schliessen>Abbrechen</button>
  </div>
  <div class="sheet-body">
  <form id="korrektur-form"
        hx-post="/gui/bestellungen/${esc(order)}/korrigieren/vorschau"
        hx-target="#korrektur" hx-swap="innerHTML"
        hx-vals='{"op": "number"}'
        hx-include="#korrektur-adresse"
        hx-sync="this:drop" hx-disabled-elt="find fieldset">
    <fieldset>
      <input type="hidden" id="korrektur-stand" name="state" value="${state}">
      <ul class="cards">
        ${plan.rows.map((l) => lineView(order, l, edit.t, message)).join("")}
        ${plan.added.map((l) => lineView(order, l, edit.t, message)).join("")}
      </ul>
      ${swapField ? numberField(edit.t !== null ? "Tauschen gegen Nummer" : "Gericht dazu, Nummer", message) : ""}
      <div class="sheet-foot">
      <div class="order-foot">
        <b>Summe ${euro(plan.total)}</b>
        <span class="muted">vorher ${euro(plan.before)}</span>
      </div>
      ${foot ? `<p class="${error ? "blocker" : "state-note"}">${esc(foot)}</p>` : ""}
      <p class="muted">Grund antippen, dann ist gespeichert:</p>
      <div class="reasons">
        ${Object.entries(REASONS)
        .map(([key, label]) => `<button type="button" class="primary"
                hx-post="/gui/bestellungen/${esc(order)}/korrigieren"
                hx-vals='{"reason": ${JSON.stringify(key)}}'
                hx-sync="closest form:drop" hx-disabled-elt="closest fieldset"
                ${plan.blockers.length ? "disabled" : ""}>${esc(label)}</button>`)
        .join("\n        ")}
      </div>
      </div>
    </fieldset>
  </form>
  </div>
</section>`;
}
function refusedCorrection(order, message) {
    return `<section class="sheet korrektur" data-order="${esc(order)}" role="dialog" aria-labelledby="korrektur-titel">
  <div class="sheet-head">
    <h2 id="korrektur-titel" tabindex="-1">Bestellung korrigieren</h2>
    <button type="button" class="schliessen" data-schliessen>Abbrechen</button>
  </div>
  <div class="sheet-body">
  <p class="banner danger">${esc(message)}</p>
  </div>
</section>`;
}
// --- Reservierung eintragen -------------------------------------------------------------
// "0721 / 555-1234" is "+497215551234" (domain/customers/phone.py normalize_phone), as far as
// the demo goes: German national spellings and numbers with a country code; null for what
// is no number. Trunk zeros after another country code and per-country lengths are not rebuilt.
export function normalizePhone(raw) {
    let digits = raw.trim().replace(/[\s\-/().]/g, "");
    if (digits.startsWith("00"))
        digits = "+" + digits.slice(2);
    else if (digits.startsWith("0"))
        digits = "+49" + digits.slice(1);
    else if (digits && !digits.startsWith("+"))
        digits = "+" + digits;
    // "+49 0171 ...": the trunk zero said after the country code is dropped.
    if (/^\+490[1-9]/.test(digits))
        digits = "+49" + digits.slice(4);
    return /^\+[1-9][0-9]{6,14}$/.test(digits) ? digits : null;
}
const ENTRY_INVALID = "Bitte Datum, Uhrzeit, Personen und Namen prüfen.";
const SAY_PAST = "Dieser Zeitpunkt ist schon vorbei.";
const SAY_NO_NAME = "Bitte einen Namen eintragen.";
const SAY_FULL = "Laut Reservierungen ist dafür kein Platz frei.";
const SAY_CLOSED = "Zu dieser Zeit ist laut Öffnungszeiten geschlossen.";
const SAY_INVALID_PHONE = "Die Rufnummer habe ich nicht verstanden. Können Sie sie noch einmal sagen?";
// Pass the typed form back as it was typed, with the refusal above the buttons.
function entryForm(values, choices, message) {
    const v = (k) => esc(values[k] ?? "");
    return `<section class="sheet reservierung" role="dialog" aria-labelledby="reservierung-titel">
  <div class="sheet-head">
    <h2 id="reservierung-titel" tabindex="-1">Reservierung eintragen</h2>
    <button type="button" class="schliessen" data-schliessen>Schließen</button>
  </div>
  <form class="sheet-body" novalidate hx-post="/gui/reservierung" hx-target="#reservierung" hx-swap="innerHTML">
    <input type="hidden" name="schluessel" value="${v("schluessel")}">
    <div class="entry-fields">
      <label for="res-datum">Datum</label>
      <input type="date" id="res-datum" name="datum" value="${v("datum")}" required>
      <label for="res-uhrzeit">Uhrzeit</label>
      <input type="time" id="res-uhrzeit" name="uhrzeit" value="${v("uhrzeit")}" step="900" required>
      <label for="res-personen">Personen</label>
      <input type="number" id="res-personen" name="personen" inputmode="numeric" min="1" value="${v("personen")}" required>
      <label for="res-name">Name</label>
      <input type="text" id="res-name" name="name" maxlength="200" value="${v("name")}" required>
      <label for="res-telefon">Telefon</label>
      <input type="tel" id="res-telefon" name="telefon" maxlength="40" value="${v("telefon")}" placeholder="freiwillig">
      <label for="res-hinweis">Hinweis</label>
      <input type="text" id="res-hinweis" name="hinweis" maxlength="500" value="${v("hinweis")}">
      <label for="res-tisch">Tisch</label>
      <select id="res-tisch" name="tisch">
        <option value="">Ohne Tisch</option>
        ${choices.map(([value, label]) => `<option value="${esc(value)}"${values["tisch"] === value ? " selected" : ""}>${esc(label)}</option>`).join("\n        ")}
      </select>
    </div>
    <div class="sheet-foot">
      ${message ? `<p class="badge danger">${esc(message)}</p>` : ""}
      <button type="submit" class="primary">Weiter</button>
    </div>
  </form>
</section>`;
}
function entrySummary(d, message, blocked) {
    const [y, m, dd] = d.day.split("-").map(Number);
    const date = new Date(Date.UTC(y ?? 1970, (m ?? 1) - 1, dd ?? 1));
    const when = `${WEEKDAYS[(date.getUTCDay() + 6) % 7]}, ${two(dd ?? 1)}.${two(m ?? 1)}.${y}, ${d.at} Uhr`;
    const needs = d.full || d.closed;
    const confirm = d.closed
        ? ' hx-confirm="Laut Öffnungszeiten ist zu dieser Zeit geschlossen. Trotzdem eintragen?"'
        : d.full
            ? ' hx-confirm="Laut Reservierungen ist dafür kein Platz frei. Trotzdem eintragen?"'
            : "";
    return `<section class="sheet reservierung" role="dialog" aria-labelledby="reservierung-titel">
  <div class="sheet-head">
    <h2 id="reservierung-titel" tabindex="-1">Reservierung eintragen</h2>
    <button type="button" class="schliessen" data-schliessen>Schließen</button>
  </div>
  <div class="sheet-body">
    <dl class="entry-summary">
      <dt>Wann</dt><dd>${esc(when)}</dd>
      <dt>Personen</dt><dd>${d.party} ${d.party === 1 ? "Person" : "Personen"}</dd>
      <dt>Name</dt><dd>${esc(d.name)}</dd>
      ${d.phone ? `<dt>Telefon</dt><dd>${esc(d.phone)}</dd>` : ""}
      ${d.note ? `<dt>Hinweis</dt><dd>${esc(d.note)}</dd>` : ""}
      <dt>Tisch</dt><dd>${d.tables.length ? "Tisch " + esc(d.tables.join(" + ")) : "Ohne Tisch"}</dd>
    </dl>
    <p class="badge ${d.full ? "warn" : "ok"}">${esc(d.load)}</p>
    ${d.closed ? `<p class="badge warn">${esc(SAY_CLOSED)}</p>` : ""}
    <div class="sheet-foot">
      ${message ? `<p class="badge danger">${esc(message)}</p>` : ""}
      ${blocked
        ? ""
        : `<form hx-post="/gui/reservierung/${esc(d.id)}/eintragen" hx-target="#reservierung" hx-swap="innerHTML"${confirm}>
        ${d.full ? '<input type="hidden" name="trotzdem" value="1">' : ""}
        ${d.closed ? '<input type="hidden" name="geschlossen" value="1">' : ""}
        <button type="submit" class="primary">${needs ? "Trotzdem eintragen" : "Eintragen"}</button>
      </form>`}
    </div>
  </div>
</section>`;
}
// --- Staff settings: team number and a date's hours ---------------------------------------
const PHONE_HELD = "Gilt bis Betriebsschluss. Der Inhaber sieht es auf der Übersicht.";
const PHONE_BACK = "Zurückgenommen, es gilt wieder die Nummer von vorher.";
const NO_NUMBER = "Das ist keine Telefonnummer.";
const NOT_LISTED = "Diese Nummer kann die Telefonanlage nicht anrufen. Weitere Nummern gibt der Betreiber frei.";
const HOURS_HELD = "Gilt sofort. Der Inhaber sieht es auf der Übersicht.";
const HOURS_WAIT = "Wartet auf den Inhaber: die Karte steht auf der Übersicht.";
const HOURS_BACK = "Zurückgenommen, es gelten wieder die Zeiten von vorher.";
const SAME = "Keine Änderung.";
const NO_DATE = "Bitte einen Tag wählen.";
const NO_TIME = "Bitte Beginn und Ende als Uhrzeit angeben.";
const NO_SPAN = "Bitte Beginn und Ende angeben, das Ende nicht gleich dem Beginn.";
const OVERNIGHT = "Das Ende liegt vor dem Beginn. Geht der Tag über Mitternacht, bitte „Ende nach Mitternacht“ ankreuzen.";
const AT_MIDNIGHT = "Ein Ende um 00:00 Uhr liegt schon am nächsten Tag. Bitte „Ende nach Mitternacht“ ankreuzen.";
const PAST_DAY = "Ein vergangener Tag lässt sich nicht mehr ändern.";
const WEEK_OPENS = 11 * 60;
const WEEK_CLOSES = 23 * 60;
function minutesOf(text) {
    const m = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(text.trim());
    return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}
function hoursText(h) {
    if (h.kind === "closed")
        return "geschlossen";
    if (h.kind === "span")
        return `${h.opens} bis ${h.closes} Uhr`;
    return "normale Zeiten der Woche";
}
// The visitor's own date, not the build's: the pages were rendered some day, the demo is
// opened on another, and a reservation "for today" must not be in the past.
export function localDay() {
    const d = new Date();
    return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}`;
}
export class Sheets {
    constructor(data, r, today = localDay()) {
        this.data = data;
        this.r = r;
        this.today = today;
        this.checks = [];
        this.hours = new Map();
        this.drafts = new Map();
        this.serial = 0;
        // What the visitor entered for a day other than today (today's go to the board).
        this.later = [];
        this.menu = data.menu.filter((d) => d.id !== undefined);
        this.holding = data.team.holding;
        this.numbers = [data.team.standard, ...data.team.choices];
    }
    // --- Gericht aus ---
    dishSheet() {
        return this.dishSearch("", true);
    }
    dishSearch(query, whole = false) {
        const text = searched(query);
        const list = dishList(this.menu, (n) => this.r.isOff(n), text);
        if (!whole)
            return list;
        return `<section class="sheet gericht-aus" role="dialog" aria-labelledby="gericht-titel">
  <div class="sheet-head">
    <h2 id="gericht-titel" tabindex="-1">Gericht aus</h2>
    <button type="button" class="schliessen" data-schliessen>Fertig</button>
  </div>
  <div class="sheet-tools">
    <p class="muted">Antippen: heute aus bis Betriebsschluss. Die KI bietet es nicht mehr an. Nochmal antippen: wieder da.</p>
    <input type="search" id="gericht-suche" name="q" value="${esc(text)}"
           placeholder="Nummer oder Name" autocomplete="off" inputmode="search"
           aria-label="Nummer oder Name"
           hx-get="/gui/fragments/gericht-aus" hx-target="#gericht-liste"
           hx-trigger="input changed delay:250ms, search">
  </div>
  <div id="gericht-liste" class="sheet-body">${list}</div>
</section>`;
    }
    // One tap. A dish that is gone is no error for the team: its row goes (an empty answer).
    dishSwitch(id, state) {
        if (state !== "aus" && state !== "wieder-da")
            return null;
        const dish = this.menu.find((d) => d.id === id);
        if (!dish)
            return { status: 200, html: "", trigger: { "gerichte-geaendert": "" } };
        this.r.setOff(dish.number, state === "aus");
        return {
            status: 200,
            html: dishRow(dish, this.r.isOff(dish.number)),
            trigger: { "gerichte-geaendert": "" },
            events: ["header"],
        };
    }
    // --- The correction of an order ---
    base(card) {
        return card.lines.map((l) => {
            const dish = this.menu.find((d) => d.number === l.number);
            return { dish, unit: (dish?.cents ?? 0) + delta(dish, l.options) };
        });
    }
    fresh(card) {
        this.serial += 1;
        return {
            v: card.version,
            e: `demo-edit-${this.serial}`,
            r: card.lines.map((_, i) => ({ id: `${card.id}:${i}`, q: card.lines[i]?.quantity ?? 1 })),
            a: [],
            t: null,
            ad: null,
        };
    }
    plan(card, edit) {
        const base = this.base(card);
        const rows = card.lines.map((line, i) => {
            const e = edit.r[i];
            const stored = {
                where: "r",
                index: i,
                kind: (e?.q ?? line.quantity) !== line.quantity ? "changed" : "kept",
                number: line.number,
                name: line.name,
                quantity: e?.q ?? line.quantity,
                options: line.options,
                note: line.note,
                cents: (base[i]?.unit ?? 0) * (e?.q ?? line.quantity),
                missing: [],
                soldOut: false,
                groups: [],
                picked: [],
            };
            if (!e || e.q === 0)
                return { ...stored, kind: "removed", quantity: line.quantity };
            const dish = e.s ? this.menu.find((d) => d.id === e.s) : undefined;
            if (!dish)
                return stored;
            const picks = e.o ?? [];
            // Swapped to its own number: only the choices change (domain/ordering/correction.py);
            // the same choices as before leave the stored line, with its choices offered.
            const keys = (xs) => [...xs].sort().join("|");
            if (dish.number === line.number && keys(picks.map((p) => p[1])) === keys(line.options)) {
                return { ...stored, groups: dish.groups ?? [], picked: picks };
            }
            return this.fromDish("r", i, "swapped", dish, e.q, picks, line.note);
        });
        const added = edit.a.map((a, j) => {
            const dish = this.menu.find((d) => d.id === a.m);
            return this.fromDish("a", j, "added", dish, a.q, a.o, null);
        });
        const remaining = [...rows.filter((l) => l.kind !== "removed"), ...added];
        const total = remaining.reduce((sum, l) => sum + l.cents, 0);
        const blockers = [];
        if (!(added.length > 0 || rows.some((l) => l.kind !== "kept")))
            blockers.push(NO_CHANGE);
        if (!remaining.length)
            blockers.push(NO_POSITION);
        for (const l of remaining) {
            if (l.missing.length)
                blockers.push(`Bei ${l.number} ${l.name} fehlt noch: ${l.missing.join(", ")}.`);
        }
        return { rows, added, total, before: this.cents(card), blockers };
    }
    cents(card) {
        const base = this.base(card);
        return card.lines.reduce((sum, l, i) => sum + (base[i]?.unit ?? 0) * l.quantity, 0);
    }
    fromDish(where, index, kind, dish, q, picks, note) {
        const groups = dish.groups ?? [];
        const names = picks.map((p) => p[1]);
        return {
            where,
            index,
            kind,
            number: dish.number,
            name: dish.name,
            quantity: q,
            options: names,
            note,
            cents: (dish.cents + delta(dish, names)) * q,
            missing: groups.filter((g) => g.required && !picks.some((p) => p[0] === g.group)).map((g) => g.group),
            soldOut: this.r.isOff(dish.number),
            groups,
            picked: picks,
        };
    }
    // The state the sheet sent back, in its shape; whether it fits the order comes after the
    // version check, as on the server (an order that changed is stale before it is garbled).
    parse(text) {
        try {
            const e = JSON.parse(text);
            const ok = typeof e.v === "string" &&
                Array.isArray(e.r) &&
                e.r.every((r) => typeof r.id === "string" && Number.isInteger(r.q)) &&
                Array.isArray(e.a) &&
                (e.t === null || (Number.isInteger(e.t) && e.t >= 0 && e.t < e.r.length));
            return ok ? e : null;
        }
        catch {
            return null;
        }
    }
    fits(card, e) {
        return e.r.length === card.lines.length && e.r.every((r, i) => r.id === `${card.id}:${i}`) && e.a.every((a) => this.menu.some((d) => d.id === a.m));
    }
    // The edit of a request, or the sheet's refusal (stale order, garbled state).
    edited(id, text) {
        const card = this.r.card(id);
        if (!card || !card.can_change)
            return { status: 409, html: refusedCorrection(id, ORDER_STALE) };
        const edit = this.parse(text);
        if (!edit)
            return { status: 409, html: refusedCorrection(id, BAD_STATE) };
        if (edit.v !== card.version)
            return { status: 409, html: refusedCorrection(id, ORDER_STALE) };
        if (!this.fits(card, edit))
            return { status: 409, html: refusedCorrection(id, BAD_STATE) };
        return { card, edit };
    }
    correctionOpen(id) {
        const card = this.r.card(id);
        if (!card || !card.can_change)
            return { status: 409, html: refusedCorrection(id, ORDER_STALE) };
        const edit = this.fresh(card);
        return { status: 200, html: renderCorrection(id, this.plan(card, edit), edit, null) };
    }
    correctionTap(id, form) {
        const got = this.edited(id, form.get("state") ?? "");
        if ("status" in got)
            return got;
        const { card, edit } = got;
        const before = JSON.stringify(edit);
        const message = this.apply(edit, form.get("op") ?? "", form.get("number") ?? "", card);
        if (message === BAD_STATE) {
            const kept = JSON.parse(before);
            return { status: 422, html: renderCorrection(id, this.plan(card, kept), kept, message) };
        }
        return { status: 200, html: renderCorrection(id, this.plan(card, edit), edit, message) };
    }
    // One tap on the sheet (gui/orders_view.py apply_op); a message for the team or null.
    apply(edit, op, number, card) {
        const parts = op.split(":");
        const kind = parts[0];
        const target = (where, index) => where === "r" ? edit.r[index] : edit.a[index];
        const i = Number(parts[1]);
        if (kind === "number") {
            const wanted = canonical(number);
            const dish = wanted ? this.menu.find((d) => canonical(d.number) === wanted) : undefined;
            if (!dish?.id)
                return `Nummer ${number.trim() || "?"} gibt es nicht auf der Karte.`;
            if (edit.t !== null) {
                const row = edit.r[edit.t];
                if (row) {
                    row.s = dish.id;
                    row.o = [];
                }
                edit.t = null;
            }
            else {
                edit.a.push({ m: dish.id, q: 1, o: [] });
            }
            return null;
        }
        if (kind === "plus" || kind === "minus") {
            const t = target(parts[1], Number(parts[2]));
            if (!t)
                return BAD_STATE;
            t.q = Math.max(0, Math.min(MAX_QUANTITY, t.q + (kind === "plus" ? 1 : -1)));
            const index = Number(parts[2]);
            if (parts[1] === "a" && t.q === 0)
                edit.a.splice(index, 1);
            // A line with a note goes only through "Entfernen", which asks first.
            else if (parts[1] === "r" && t.q === 0 && card.lines[index]?.note)
                t.q = 1;
            return null;
        }
        if (kind === "remove" || kind === "restore" || kind === "swap") {
            const row = edit.r[i];
            if (!row)
                return BAD_STATE;
            if (kind === "remove") {
                row.q = 0;
                row.d = true;
            }
            else if (kind === "restore") {
                Object.assign(row, { q: card.lines[i]?.quantity ?? 1, d: false, s: null, o: [] });
            }
            else
                edit.t = i;
            return null;
        }
        if (kind === "unswap") {
            edit.t = null;
            return null;
        }
        if (kind === "opt") {
            const t = target(parts[1], Number(parts[2]));
            const where = parts[1];
            const dishId = where === "r" ? t?.s : t?.m;
            const dish = this.menu.find((d) => d.id === dishId);
            const group = dish?.groups?.[Number(parts[3])];
            const option = group?.options[Number(parts[4])];
            if (!t || !group || !option)
                return BAD_STATE;
            const picked = [group.group, option.name];
            const current = t.o ?? [];
            if (group.required)
                t.o = [...current.filter((c) => c[0] !== group.group), picked];
            else if (current.some((c) => c[0] === picked[0] && c[1] === picked[1]))
                t.o = current.filter((c) => !(c[0] === picked[0] && c[1] === picked[1]));
            else
                t.o = [...current, picked];
            return null;
        }
        return BAD_STATE;
    }
    correctionSave(id, form) {
        const got = this.edited(id, form.get("state") ?? "");
        if ("status" in got)
            return got;
        const { card, edit } = got;
        const plan = this.plan(card, edit);
        const reason = REASONS[form.get("reason") ?? ""];
        if (plan.blockers.length || reason === undefined) {
            return { status: 422, html: renderCorrection(id, plan, edit, plan.blockers[0] ?? "Das hat nicht geklappt. Bitte nochmal tippen.") };
        }
        const lines = [...plan.rows.filter((l) => l.kind !== "removed"), ...plan.added].map((l) => ({
            quantity: l.quantity,
            number: l.number,
            name: l.name,
            options: l.options,
            note: l.note,
        }));
        this.r.correct(id, lines, plan.total, reason);
        return { status: 200, html: "", trigger: { "bestellungen-geaendert": "" }, events: ["orders"] };
    }
    // --- Team number and hours ---
    teamSheet(message, tone) {
        const choices = this.numbers.filter((n) => n !== this.holding);
        return `<section class="sheet teamnummer" role="dialog" aria-labelledby="teamnummer-titel">
  <div class="sheet-head">
    <h2 id="teamnummer-titel" tabindex="-1">Teamnummer</h2>
    <button type="button" class="schliessen" data-schliessen>Schließen</button>
  </div>
  <div class="sheet-tools">
    <p>Hierhin gibt die KI Anrufe weiter. Gilt jetzt: <b>${esc(this.holding)}</b></p>
    <p class="muted">Standard: ${esc(this.data.team.standard)}. Eine Änderung gilt bis Betriebsschluss, dann wieder der Standard, außer der Inhaber übernimmt sie.</p>
  </div>
  <div class="sheet-body">
    ${choices.length
            ? choices
                .map((n) => `<button class="primary" hx-post="/gui/teamnummer" hx-vals='{"nummer": "${esc(n)}"}'
            hx-target="#einstellungen" hx-swap="innerHTML">Heute: ${esc(n)}</button>`)
                .join("\n    ")
            : "<p>Keine andere Nummer möglich. Weitere Nummern gibt der Betreiber frei.</p>"}
    <div class="sheet-foot">
      ${message ? `<span class="badge ${tone}" role="status">${esc(message)}</span>` : ""}
    </div>
  </div>
</section>`;
    }
    teamOpen() {
        return { status: 200, html: this.teamSheet(null, "danger") };
    }
    teamChange(form) {
        const number = (form.get("nummer") ?? "").trim();
        if (!/^\+?[0-9 ]{5,20}$/.test(number))
            return { status: 409, html: this.teamSheet(NO_NUMBER, "danger") };
        if (!this.numbers.includes(number))
            return { status: 409, html: this.teamSheet(NOT_LISTED, "danger") };
        if (number === this.holding)
            return { status: 200, html: this.teamSheet(SAME, "ok") };
        const before = this.holding;
        this.holding = number;
        // Back to the number that held first: the card closes as undone (#861).
        const open = this.checks.find((c) => c.id === "team");
        if (open && open.before === number) {
            this.checks = this.checks.filter((c) => c !== open);
            return { status: 200, html: this.teamSheet(PHONE_BACK, "ok"), events: ["checks"] };
        }
        if (!open)
            this.checks.push({ id: "team", title: "Teamnummer heute", before, after: number, now: number, waiting: false, keep: "Als Standard übernehmen", seen: "demo" });
        else
            open.after = open.now = number;
        return { status: 200, html: this.teamSheet(PHONE_HELD, "ok"), events: ["checks"] };
    }
    // Whether the restaurant is open at a time of a day, by the hours the visitor set (a span
    // "after midnight" runs into the next day) or else by the weekly hours.
    openAt(day, at) {
        const h = this.hours.get(day);
        if (h?.kind === "closed")
            return false;
        if (h?.kind === "span") {
            const from = minutesOf(h.opens ?? "") ?? 0;
            const to = (minutesOf(h.closes ?? "") ?? 0) + (h.nachts ? 1440 : 0);
            return at >= from && at < to;
        }
        return at >= WEEK_OPENS && at < WEEK_CLOSES;
    }
    dayOf(text) {
        return /^\d{4}-\d\d-\d\d$/.test(text) && !Number.isNaN(Date.parse(text)) ? text : null;
    }
    hoursSheet(day, message, tone, typed) {
        const today = this.today;
        const shown = day !== null && day >= today ? day : today;
        const holding = this.hours.get(shown) ?? { kind: "normal", text: "" };
        const opens = typed?.von ?? (holding.kind === "span" ? (holding.opens ?? "") : "");
        const closes = typed?.bis ?? (holding.kind === "span" ? (holding.closes ?? "") : "");
        return `<section class="sheet tageszeiten" role="dialog" aria-labelledby="tageszeiten-titel">
  <div class="sheet-head">
    <h2 id="tageszeiten-titel" tabindex="-1">Öffnungszeit für einen Tag</h2>
    <button type="button" class="schliessen" data-schliessen>Schließen</button>
  </div>
  <form class="sheet-body" novalidate hx-post="/gui/tageszeiten" hx-target="#einstellungen" hx-swap="innerHTML">
    <div class="entry-fields">
      <label for="datum">Tag</label>
      <input type="date" id="datum" name="datum" value="${esc(shown)}" min="${esc(today)}" required
             hx-get="/gui/tageszeiten" hx-trigger="change" hx-include="this" hx-target="#einstellungen" hx-swap="innerHTML">
      <span>Gilt jetzt</span>
      <b>${esc(hoursText(holding))}</b>
      <label for="von">Von</label>
      <input type="time" id="von" name="von" value="${esc(opens)}" step="900">
      <label for="bis">Bis</label>
      <input type="time" id="bis" name="bis" value="${esc(closes)}" step="900">
      <span></span>
      <label><input type="checkbox" name="nachts" value="1"${typed?.nachts ? " checked" : ""}> Ende nach Mitternacht</label>
    </div>
    <p class="muted">Heute und kürzere Zeiten gelten sofort. Längere oder zusätzliche Zeiten an einem späteren Tag und eigene Zeiten an einem Tag „wie Sonntag“ (oder einem anderen Wochentag) gelten erst, wenn der Inhaber sie übernimmt.</p>
    <div class="sheet-foot">
      <button type="submit" name="art" value="zeiten" class="primary">Diese Zeiten</button>
      <button type="submit" name="art" value="geschlossen">Geschlossen</button>
      <button type="submit" name="art" value="normal">Normale Zeiten</button>
      ${message ? `<span class="badge ${tone}" role="status">${esc(message)}</span>` : ""}
    </div>
  </form>
</section>`;
    }
    hoursOpen(day) {
        return { status: 200, html: this.hoursSheet(this.dayOf(day), null, "danger") };
    }
    hoursChange(form) {
        const day = this.dayOf(form.get("datum") ?? "");
        const choice = form.get("art") ?? "";
        const typed = { von: form.get("von") ?? "", bis: form.get("bis") ?? "", nachts: Boolean(form.get("nachts")) };
        if (day === null || !["geschlossen", "zeiten", "normal"].includes(choice)) {
            return { status: 422, html: this.hoursSheet(day, NO_DATE, "danger") };
        }
        if (day < this.today)
            return { status: 409, html: this.hoursSheet(day, PAST_DAY, "danger", typed) };
        let next = { kind: "normal", text: "" };
        if (choice === "geschlossen")
            next = { kind: "closed", text: "" };
        if (choice === "zeiten") {
            const from = minutesOf(typed.von);
            const to = minutesOf(typed.bis);
            if (from === null || to === null)
                return { status: 422, html: this.hoursSheet(day, NO_TIME, "danger") };
            if (from === to)
                return { status: 409, html: this.hoursSheet(day, NO_SPAN, "danger", typed) };
            if (to < from && !typed.nachts)
                return { status: 409, html: this.hoursSheet(day, to === 0 ? AT_MIDNIGHT : OVERNIGHT, "danger", typed) };
            next = { kind: "span", text: "", opens: typed.von, closes: typed.bis, nachts: typed.nachts };
        }
        const before = this.hours.get(day) ?? { kind: "normal", text: "" };
        const id = `hours-${day}`;
        const open = this.checks.find((c) => c.id === id);
        if (hoursText(before) === hoursText(next)) {
            // The hours that hold now, asked while a request waits: the request is withdrawn.
            if (!open)
                return { status: 200, html: this.hoursSheet(day, SAME, "ok") };
            this.checks = this.checks.filter((c) => c !== open);
            return { status: 200, html: this.hoursSheet(day, HOURS_BACK, "ok"), events: ["checks"] };
        }
        const first = open ? open.before : hoursText(before);
        if (hoursText(next) === first) {
            this.hours.delete(day);
            this.checks = this.checks.filter((c) => c !== open);
            return { status: 200, html: this.hoursSheet(day, HOURS_BACK, "ok"), events: ["checks"] };
        }
        // Held at once: today, or hours that stay inside the weekly ones; later or longer waits for the owner.
        let held = true;
        if (next.kind === "span") {
            const from = minutesOf(next.opens ?? "") ?? 0;
            const to = (minutesOf(next.closes ?? "") ?? 0) + (typed.nachts ? 1440 : 0);
            held = day === this.today || (from >= WEEK_OPENS && to <= WEEK_CLOSES);
        }
        if (held)
            this.hours.set(day, next);
        const [y, m, d] = day.split("-").map(Number);
        const weekday = SHORT[(new Date(Date.UTC(y ?? 1970, (m ?? 1) - 1, d ?? 1)).getUTCDay() + 6) % 7];
        const title = `Öffnungszeit ${weekday}, ${two(d ?? 1)}.${two(m ?? 1)}.`;
        const card = { id, title, before: first, after: hoursText(next), now: hoursText(held ? next : before), waiting: !held, keep: "Übernehmen", seen: "demo" };
        if (open)
            Object.assign(open, card);
        else
            this.checks.push(card);
        return { status: 200, html: this.hoursSheet(day, held ? HOURS_HELD : HOURS_WAIT, "ok"), events: ["checks"] };
    }
    // The check cards for the owner (fragments/pruefen.html); both buttons ask the PIN.
    checkCards() {
        const cards = this.checks
            .map((c) => `<li class="card check" data-id="${esc(c.id)}">
    <div class="res-head">
      <b>${esc(c.title)}</b>
      ${c.waiting ? '<span class="badge warn">Wartet auf den Inhaber</span>' : '<span class="badge">Vom Team geändert</span>'}
    </div>
    <p>Vorher: ${esc(c.before)}</p>
    <p>${c.waiting ? "Beantragt" : "Jetzt"}: <b>${esc(c.after)}</b></p>
    ${c.waiting && c.now !== c.before ? `<p class="muted">Gilt bis dahin: ${esc(c.now)}</p>` : ""}
    <div class="cb-actions">
      <button class="primary" hx-post="/gui/pruefen/${esc(c.id)}/uebernehmen" hx-vals='{"gesehen": "${c.seen}"}'
              hx-target="#pruefen-liste" hx-swap="innerHTML">${esc(c.keep)}</button>
      <button hx-post="/gui/pruefen/${esc(c.id)}/zuruecknehmen" hx-vals='{"gesehen": "${c.seen}"}'
              hx-target="#pruefen-liste" hx-swap="innerHTML">Zurücknehmen</button>
    </div>
  </li>`)
            .join("");
        return `${cards ? `<ul class="cards">${cards}</ul>` : ""}
<section id="zeiten-gaeste">

</section>`;
    }
    // --- Reservierung eintragen ---
    entryOpen() {
        this.serial += 1;
        const values = { schluessel: `demo-key-${this.serial}`, datum: this.today };
        return { status: 200, html: entryForm(values, this.data.entry.choices, null) };
    }
    entryCheck(form) {
        const values = {};
        for (const k of ["schluessel", "datum", "uhrzeit", "personen", "name", "telefon", "hinweis", "tisch"])
            values[k] = form.get(k) ?? "";
        const refuse = (say) => ({ status: 409, html: entryForm(values, this.data.entry.choices, say) });
        const day = this.dayOf(values["datum"] ?? "");
        const at = minutesOf(values["uhrzeit"] ?? "");
        const party = /^[0-9]{1,3}$/.test((values["personen"] ?? "").trim()) ? Number(values["personen"]) : 0;
        if (day === null || at === null || party < 1)
            return refuse(ENTRY_INVALID);
        const name = (values["name"] ?? "").trim();
        if (!name)
            return refuse(SAY_NO_NAME);
        const raw = (values["telefon"] ?? "").trim();
        const phone = raw ? normalizePhone(raw) : "";
        if (phone === null)
            return refuse(SAY_INVALID_PHONE);
        const clock = this.r.now();
        if (day < this.today || (day === this.today && at < (minutesOf(clock) ?? 0)))
            return refuse(SAY_PAST);
        const picked = this.data.entry.choices.find((c) => c[0] === values["tisch"]);
        const table = picked ? (picked[1].split(" (")[0] ?? "") : null;
        const time = `${two(Math.floor(at / 60))}:${two(at % 60)}`;
        const same = [
            ...(day === this.today ? this.r.booked().map((x) => ({ time: x.time, party: x.party_size, tables: x.tables })) : []),
            ...this.later.filter((x) => x.day === day),
        ];
        // A time inside the capacity window holds its table for the whole window; outside every
        // window only the same start time collides (domain/reservations/manual.py taken_tables).
        const within = (hhmm) => {
            const m = minutesOf(hhmm) ?? 0;
            return m >= (minutesOf(this.data.window[0]) ?? 0) && m < (minutesOf(this.data.window[1]) ?? 0);
        };
        const inWindow = within(time);
        // A booking outside every window belongs to no window: it holds no table of this one and
        // counts no guest (the window's own predicates in `taken_tables` and `booked_guests`).
        if (table && same.some((x) => x.tables.includes(table) && (inWindow ? within(x.time) : x.time === time))) {
            return refuse(`Tisch ${table} ist um ${time} Uhr schon vergeben.`);
        }
        // The seeded capacity is one window for the whole opening time (slot_minutes is only
        // the grid of the times), so every booking of the day counts (api/domain/reservations/capacity.py).
        const guests = same.filter((x) => within(x.time)).reduce((s, x) => s + x.party, 0) + party;
        const closed = !this.openAt(day, at);
        const draft = {
            id: `demo-draft-${values["schluessel"] || this.serial}`,
            at: time,
            day,
            party,
            name,
            phone: phone || null,
            note: (values["hinweis"] ?? "").trim() || null,
            tables: table ? [table] : [],
            // No window at that time is as full as a full one: only "Trotzdem" books it (manual.py `fits`).
            full: !inWindow || guests > this.data.max_guests,
            closed,
            load: !inWindow
                ? "Zu dieser Zeit gibt es kein Reservierungsfenster."
                : `Fenster ${this.data.window[0]} bis ${this.data.window[1]} Uhr: ${guests} von ${this.data.max_guests} Plätzen belegt.`,
            entered: false,
        };
        // The same sheet key is the same draft: a second "Weiter" does not book twice.
        const known = this.drafts.get(draft.id);
        if (!known?.entered)
            this.drafts.set(draft.id, draft);
        return { status: 200, html: entrySummary(known?.entered ? known : draft, null, false) };
    }
    entryConfirm(id, form) {
        const d = this.drafts.get(id);
        if (!d)
            return null;
        // A draft that was not confirmed yet must still lie ahead when the team confirms it.
        if (!d.entered && (d.day < this.today || (d.day === this.today && (minutesOf(d.at) ?? 0) < (minutesOf(this.r.now()) ?? 0)))) {
            return { status: 409, html: entrySummary(d, SAY_PAST, true) };
        }
        if (d.full && form.get("trotzdem") !== "1")
            return { status: 409, html: entrySummary(d, SAY_FULL, false) };
        if (d.closed && form.get("geschlossen") !== "1")
            return { status: 409, html: entrySummary(d, SAY_CLOSED, true) };
        if (!d.entered) {
            d.entered = true;
            const entry = {
                id: d.id,
                time: d.at,
                party_size: d.party,
                guest_name: d.name,
                phone: d.phone,
                note: d.note,
                tables: d.tables,
                is_test: false,
                off_hours: d.closed,
            };
            if (d.day === this.today)
                this.r.enter(entry);
            else
                this.later.push({ day: d.day, time: d.at, party: d.party, tables: d.tables });
        }
        return { status: 200, html: "", trigger: { "reservierung-eingetragen": `Reservierung für ${d.name} eingetragen.` }, events: ["today"] };
    }
}
// --- The pages Anrufe and Menü: the filters of their forms --------------------------------
function selectBlock(select) {
    return new RegExp(`(<select[^>]*name="${select}"[^>]*>)([\\s\\S]*?)(</select>)`);
}
function optionLabel(html, select, value) {
    const block = selectBlock(select).exec(html)?.[2] ?? "";
    for (const m of block.matchAll(/<option value="([^"]*)"[^>]*>([^<]*)<\/option>/g)) {
        if (m[1] === value)
            return m[2] ?? null;
    }
    return null;
}
// Marks the chosen option of a select, as the real page does after a filter.
function choose(html, select, value) {
    return html.replace(selectBlock(select), (_all, a, body, c) => {
        const options = body.replace(/ selected/g, "").replace(/<option value="([^"]*)"/g, (whole, v) => (v === value ? `${whole} selected` : whole));
        return a + options + c;
    });
}
// The call list of one day (the day picks the captured page); the two other filters cut
// its rows. The count line says how many remain.
export function filterCalls(html, outcome, intent, dayLabel) {
    const outcomeLabel = outcome ? optionLabel(html, "ergebnis", outcome) : null;
    const intentLabel = intent ? optionLabel(html, "anliegen", intent) : null;
    let shown = 0;
    let out = html.replace(/<tr>\s*<th scope="row">[\s\S]*?<\/tr>/g, (row) => {
        const cell = (label) => (new RegExp(`<td data-label="${label}"[^>]*>([\\s\\S]*?)</td>`).exec(row)?.[1] ?? "").trim();
        // The result cell starts with its label and may add a badge after it; the reason cell is the label.
        const keep = (!outcomeLabel || cell("Ergebnis").startsWith(outcomeLabel)) && (!intentLabel || cell("Anliegen") === intentLabel);
        if (keep)
            shown += 1;
        return keep ? row : "";
    });
    out = out.replace(/<span class="muted">\d+ Anrufe? [^<]*<\/span>/, `<span class="muted">${shown} ${shown === 1 ? "Anruf" : "Anrufe"} ${esc(dayLabel)}</span>`);
    out = choose(choose(out, "ergebnis", outcome), "anliegen", intent);
    if (shown === 0)
        out = out.replace(/<table class="tbl">[\s\S]*?<\/table>/, '<p class="note">Keine Anrufe für diese Auswahl.</p>');
    return out;
}
export function dayLabelOf(html, day) {
    const label = optionLabel(html, "tag", day) ?? "";
    return label === "Heute" ? "heute" : label;
}
// The menu page for a search and a filter: the whole page, as the real route answers it.
// A note every dish carries is said once above the list and no badge sits on the rows
// (the page's all_missing, all_differ), so the filter then keeps every dish.
export function filterMenu(html, query, hint) {
    const said = searched(query);
    const rows = [...html.matchAll(/<li>\s*<bdi class="nr">([^<]*)<\/bdi>[\s\S]*?<\/li>/g)];
    const all = {
        allergene: /Bei keinem Gericht ist ein Allergen/.test(html),
        preis: /Bei jedem Gericht nennt die Kasse einen anderen Preis/.test(html),
    };
    const badge = { allergene: "Allergene fehlen", preis: "Kasse:" };
    const kept = rows.filter((m) => {
        const name = (/<bdi class="menu-name">([^<]*)<\/bdi>/.exec(m[0])?.[1] ?? "").replace(/&#39;/g, "'").replace(/&amp;/g, "&");
        const flagged = !hint || (all[hint] ?? false) || m[0].includes(badge[hint] ?? "");
        return flagged && matches(m[1] ?? "", name, said);
    });
    const narrowed = said !== "" || hint !== "";
    const list = /<div id="menue-liste">[\s\S]*?<\/ul>/.exec(html)?.[0] ?? '<div id="menue-liste">';
    const body = kept.length
        ? `<ul class="menu-list">\n${kept.map((m) => m[0]).join("\n")}\n  </ul>`
        : `<p class="empty">Kein Gericht gefunden.</p>`;
    const total = rows.length;
    const head = narrowed
        ? `<p><a class="button" href="/gui/menue">Alle zeigen</a> <span class="muted" role="status">${kept.length} von ${total} ${total === 1 ? "Gericht" : "Gerichten"}</span></p>\n  `
        : "";
    const out = html.replace(list, () => `<div id="menue-liste">\n  ${head}${body}`);
    return choose(out.replace(/(<input type="search" id="menue-suche" name="q" value=")[^"]*(")/, `$1${esc(said)}$2`), "hinweis", hint);
}
