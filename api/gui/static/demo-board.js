// The board of the public tablet demo (#892, B2): the state a visitor changes and
// the markup of the four columns and the header, without a browser. The fake
// backend (demo-backend.ts) answers the requests of the real pages with these
// functions.
//
// The markup is the real templates' (api/gui/templates/fragments/kopfzeile.html,
// bestellungen.html, rueckrufe.html, heute.html, rueckruf_verlauf.html,
// wartezeit.html); the decisions are the real server's (api/gui/router.py,
// api/gui/orders_view.py, api/domain/ordering/board.py, api/domain/status/config.py),
// rebuilt here as far as the demo reaches. The build writes the data the real view
// rendered to state.json, and api/tests/test_tablet_demo_board.py renders it again
// with this file and compares it with the real fragments: a change of a template
// that is not made here fails that test.
//
// Source: api/gui/ts/demo-board.ts. The file of the same name in api/gui/static/ is
// its compiled output (`npm run build`, docs/22); edit the source, never the output.
// --- Escaping and small helpers ----------------------------------------------------
// Jinja's autoescape: the five characters, the same entities.
export function esc(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&#34;")
        .replace(/'/g, "&#39;");
}
export function euro(cents) {
    const sign = cents < 0 ? "-" : "";
    const abs = Math.abs(cents);
    return `${sign}${Math.floor(abs / 100)},${String(abs % 100).padStart(2, "0")} €`;
}
function two(n) {
    return String(n).padStart(2, "0");
}
// "12:00" plus minutes, wrapping at midnight.
export function clockPlus(start, minutes) {
    const [h, m] = start.split(":").map(Number);
    const total = ((h ?? 0) * 60 + (m ?? 0) + Math.floor(minutes)) % 1440;
    return `${two(Math.floor(total / 60))}:${two(total % 60)}`;
}
// --- Markup -------------------------------------------------------------------------
// The build turns a link to a rendered page into a link to its file
// (scripts/demo/tablet_demo_build.py `rewrite`); markup made here gets the same.
export function demoLinks(html, pages, base) {
    const known = new Set(pages.map((p) => p.replace(/\/$/, "")));
    return html.replace(/href="(\/gui(?:\/[^"?#]*)?)"/g, (whole, target) => {
        const path = target.replace(/\/$/, "") || "/gui";
        if (!known.has(path))
            return whole;
        const rest = path.slice("/gui".length).replace(/^\/+|\/+$/g, "");
        return `href="${base}/${rest ? rest + "/" : ""}"`;
    });
}
export function renderHeader(h) {
    const cfg = h.config;
    const paused = cfg !== null && cfg.call_mode === "paused";
    let out = `<header id="kopfzeile" class="${paused ? "aus" : ""}">`;
    if (h.header_problem)
        out += `<div class="banner danger">${esc(h.header_problem)}</div>`;
    if (h.bridge_problem)
        out += `<div class="banner danger">${esc(h.bridge_problem)}</div>`;
    out += `<div class="top"><div class="mode status tone-${esc(h.mode_tone)}">`;
    out += `<span class="dot ${esc(h.mode_tone)}"></span><b>${esc(h.mode_label)}</b>`;
    if (h.mode_until)
        out += `<span>nach Plan bis ${esc(h.mode_until)}</span>`;
    out += `</div>`;
    if (cfg) {
        if (h.mode_choices.length > 0 && cfg.call_mode !== "paused") {
            out += `<div class="seg" role="group" aria-label="Modus der KI">`;
            for (const [mode, label, pressed, up] of h.mode_choices) {
                out +=
                    `<button hx-post="/gui/kopfzeile/modus/${esc(mode)}" hx-target="#kopfzeile" hx-swap="outerHTML" ` +
                        `class="${pressed ? "primary" : ""}" aria-pressed="${pressed ? "true" : "false"}" ` +
                        (up ? `hx-confirm="Auf „${esc(label)}“ umschalten? Danach nimmt die KI mehr Anrufe an."` : "") +
                        `>${esc(label)}</button>`;
            }
            out += `</div>`;
        }
        out += `<div class="seg services" role="group" aria-label="Was wir gerade annehmen">`;
        for (const [key, label, on] of h.services) {
            out +=
                `<button hx-post="/gui/kopfzeile/${esc(key)}/${on ? "aus" : "an"}" hx-target="#kopfzeile" hx-swap="outerHTML" ` +
                    `class="${on ? "on" : "off"}" aria-pressed="${on ? "true" : "false"}">${esc(label)} ${on ? "an" : "aus"}</button>`;
        }
        out += `</div>`;
        if (cfg.call_mode === "paused") {
            out +=
                `<button class="primary push" hx-post="/gui/kopfzeile/ki-einschalten" hx-target="#kopfzeile" hx-swap="outerHTML" ` +
                    `hx-confirm="KI wieder einschalten? Danach nimmt sie wieder Anrufe an.">KI einschalten</button>`;
        }
        else {
            out += `<button class="stop push" hx-post="/gui/kopfzeile/ki-pausieren" hx-target="#kopfzeile" hx-swap="outerHTML">KI pausieren</button>`;
        }
    }
    out += `</div>`;
    if (cfg) {
        out += `<div class="top actions">`;
        for (const [key, label, minutes, standard] of h.waits) {
            out += `<div class="step"><button class="step-value" hx-get="/gui/wartezeit/${esc(key)}" hx-target="#wartezeit">`;
            out += `<b>Wartezeit ${esc(label)} ${minutes} Min</b>`;
            if (minutes !== standard)
                out += `<span class="step-std">Standard ${standard} Min</span>`;
            out += `</button>`;
            out +=
                `<button hx-post="/gui/kopfzeile/wartezeit/${esc(key)}/-${h.wait_step}" hx-target="#kopfzeile" hx-swap="outerHTML" ` +
                    `aria-label="Wartezeit ${esc(label)} ${h.wait_step} Minuten weniger" ${minutes <= h.wait_min ? "disabled" : ""}>-${h.wait_step}</button>`;
            out +=
                `<button hx-post="/gui/kopfzeile/wartezeit/${esc(key)}/${h.wait_step}" hx-target="#kopfzeile" hx-swap="outerHTML" ` +
                    `aria-label="Wartezeit ${esc(label)} ${h.wait_step} Minuten mehr" ${minutes >= h.wait_max ? "disabled" : ""}>+${h.wait_step}</button></div>`;
        }
        out += `<button class="push" hx-get="/gui/gericht-aus" hx-target="#gericht-aus">Gericht aus`;
        if (h.sold_out_count)
            out += ` <span class="badge warn">${h.sold_out_count} heute aus</span>`;
        out += `</button>`;
        out += `<a class="button" href="/gui/anrufe">Anrufe</a>`;
        out += `<a class="button" href="/gui/kennzahlen">Kennzahlen</a>`;
        out += `<a class="button" href="/gui/raumplan">Raumplan</a>`;
        out += `<button hx-get="/gui/einstellungen" hx-target="#einstellungen">Einstellungen</button></div>`;
    }
    return out + `</header>`;
}
export function renderWaitSheet(key, label, minutes, standard, min, max) {
    const k = esc(key);
    let out = `<section class="sheet wartezeit" role="dialog" aria-labelledby="wartezeit-titel"><div class="sheet-head">` +
        `<h2 id="wartezeit-titel" tabindex="-1">Wartezeit ${esc(label)}</h2>` +
        `<button type="button" class="schliessen" data-schliessen>Schließen</button></div><div class="sheet-body wait-rows">`;
    out +=
        `<form class="wait-row" novalidate hx-post="/gui/kopfzeile/wartezeit/${k}" hx-target="#kopfzeile" hx-swap="outerHTML">` +
            `<label for="wait-heute"><b>Heute</b><span class="muted">bis Betriebsschluss</span></label>` +
            `<input type="number" id="wait-heute" name="minuten" inputmode="numeric" value="${minutes}" min="${min}" max="${max}" required>` +
            `<span>Min</span><button type="submit" class="primary">Übernehmen</button></form>`;
    out +=
        `<form class="wait-row" novalidate hx-post="/gui/kopfzeile/standardzeit/${k}" hx-target="#kopfzeile" hx-swap="outerHTML">` +
            `<label for="wait-standard"><b>Standard</b><span class="muted">für alle Tage</span></label>` +
            `<input type="number" id="wait-standard" name="minuten" inputmode="numeric" value="${standard}" min="${min}" max="${max}" required>` +
            `<span>Min</span><button type="submit">Speichern</button></form>`;
    if (minutes !== standard) {
        out +=
            `<button type="button" hx-post="/gui/kopfzeile/standardzeit/${k}/zurueck" hx-target="#kopfzeile" hx-swap="outerHTML">` +
                `Zurück auf Standard (${standard} Min)</button>`;
    }
    return out + `</div></section>`;
}
function codeSpan(c) {
    return c ? `<span class="code" title="Abholcode">${esc(c)}</span>` : "";
}
function renderOrder(o) {
    const cls = "card order" +
        (o.compact ? " compact" : "") +
        " " +
        (o.failed ? "failed" : "") +
        ((o.overdue || o.register_late) && !o.failed ? "overdue" : "") +
        (o.awaiting && !o.failed && !o.overdue && !o.register_late ? " awaiting" : "");
    let out = `<li class="${cls}" data-id="${esc(o.id)}"${o.overdue && !o.failed ? ' data-overdue="1"' : ""}>`;
    const mark = `<button class="primary" hx-post="/gui/bestellungen/${esc(o.id)}/in-kasse/${esc(o.version)}" ` +
        `hx-target="#bestellungen-liste" hx-swap="innerHTML">In Kasse eingegeben</button>`;
    if (o.compact) {
        out += `<details><summary>${codeSpan(o.pickup_code)}<span class="name">${esc(o.name)}</span><b class="time">${esc(o.time)}</b>${mark}</summary>`;
    }
    out += `<div class="res-head"><b class="time">${esc(o.time)}</b><span class="badge accent">${esc(o.type)}</span>${codeSpan(o.pickup_code)}</div>`;
    out += `<div class="name">${esc(o.name)} <span class="muted">&middot; ${esc(o.phone)}</span></div>`;
    if (o.delivery) {
        out += `<div class="address">`;
        if (o.address) {
            out += esc(o.address);
            if (o.floor_note)
                out += ` <span class="muted">&middot; ${esc(o.floor_note)}</span>`;
        }
        else {
            out += `<span class="badge danger">Adresse fehlt</span>`;
        }
        out += `</div>`;
    }
    if (o.note)
        out += `<div class="note">Für die ganze Bestellung: ${esc(o.note)}</div>`;
    out += `<ul class="lines">`;
    for (const l of o.lines) {
        out += `<li>${l.quantity}&times; <span class="nr">${esc(l.number)}</span> ${esc(l.name)} `;
        for (const opt of l.options)
            out += `<div class="opt">&boxur; ${esc(opt)}</div>`;
        if (l.note)
            out += `<div class="note">${esc(l.note)}</div>`;
        out += `</li>`;
    }
    out += `</ul><div class="order-foot"><b>${esc(o.total)}</b>`;
    if (o.fee)
        out += `<span>Lieferpauschale ${esc(o.fee)}</span>`;
    if (o.ready)
        out += `<span>fertig ${esc(o.ready)}</span>`;
    out += `</div><div class="state"><span class="badge ${esc(o.tone)}">${esc(o.state)}</span>`;
    if (o.cancelled)
        out += `<span class="badge accent">Storniert</span>`;
    if (o.corrected)
        out += `<span class="badge accent">Korrigiert: ${esc(o.corrected)}</span>`;
    if (o.register)
        out += `<span class="badge ${esc(o.register[0])}">${esc(o.register[1])}</span>`;
    out += `</div><div class="cb-actions">`;
    if (o.failed) {
        out +=
            `<button class="stop" hx-post="/gui/bestellungen/${esc(o.id)}/nochmal-senden" hx-target="#bestellungen-liste" hx-swap="innerHTML">Nochmal senden</button>`;
    }
    else if (o.can_approve) {
        out +=
            `<button class="primary" hx-post="/gui/bestellungen/${esc(o.id)}/passt" hx-target="#bestellungen-liste" hx-swap="innerHTML">` +
                `${o.awaiting ? "Passt, Zettel an die Kasse" : "Passt"}</button>`;
    }
    if (o.can_mark)
        out += mark;
    if (o.can_change) {
        out += `<button hx-get="/gui/bestellungen/${esc(o.id)}/korrigieren" hx-target="#korrektur" hx-swap="innerHTML">Korrigieren</button>`;
        out +=
            `<button class="quiet" hx-post="/gui/bestellungen/${esc(o.id)}/storniert/${esc(o.version)}" ` +
                `hx-confirm="Bestellung${o.pickup_code ? " " + esc(o.pickup_code) : ""} stornieren? Steht sie schon in der Kasse, dort auch stornieren." ` +
                `hx-target="#bestellungen-liste" hx-swap="innerHTML">Bestellung stornieren</button>`;
    }
    out += `</div>`;
    // The real template never closes its <details>; the parser does at the end of the card.
    return out + `</li>`;
}
export function renderOrders(orders, problem) {
    let out = `<h2>Neue Bestellungen${orders.length ? ` <span class="count">${orders.length}</span>` : ""}</h2>`;
    if (!orders.length) {
        if (problem)
            out += `<p class="banner danger">${esc(problem)}</p>`;
        return out + `<p class="empty">Noch keine Bestellungen heute.</p>`;
    }
    if (problem)
        out += `<p class="banner danger">${esc(problem)}</p>`;
    const red = orders.filter((o) => o.failed);
    if (red.length) {
        out += `<ul class="red-bars">`;
        for (const o of red) {
            out += `<li class="red-bar" data-bar-for="${esc(o.id)}">${codeSpan(o.pickup_code)}<b>${esc(o.state)}</b>`;
            out += `<button class="stop" hx-post="/gui/bestellungen/${esc(o.id)}/nochmal-senden" hx-target="#bestellungen-liste" hx-swap="innerHTML">Nochmal senden</button></li>`;
        }
        out += `</ul>`;
    }
    return out + `<ul class="cards">${orders.map(renderOrder).join("")}</ul>`;
}
function callbackBody(c) {
    let out = `<div class="res-head"><span class="when"><span class="muted">Annahme</span> <b class="time">${esc(c.time)}</b></span>`;
    out += `<span class="badge ${esc(c.tone)}">${esc(c.label)}</span>`;
    return out;
}
export function renderCallbacks(callbacks) {
    let out = `<h2>R&uuml;ckrufe${callbacks.length ? ` <span class="count">${callbacks.length}</span>` : ""}` +
        ` <button type="button" class="h2-action" hx-get="/gui/rueckrufe/verlauf" hx-target="#rueckruf-verlauf">Verlauf</button></h2>`;
    if (!callbacks.length)
        return out + `<p class="empty">Keine R&uuml;ckrufe offen.</p>`;
    out += `<ul class="cards">`;
    for (const c of callbacks) {
        out += `<li class="card cb${c.reason === "complaint" ? " complaint" : ""}" data-id="${esc(c.id)}" data-reason="${esc(c.reason)}">`;
        out += callbackBody(c) + `</div>`;
        out += callbackPhone(c, true);
        if (c.summary)
            out += `<p class="summary">&bdquo;${esc(c.summary)}&ldquo;</p>`;
        out += `<div class="cb-actions">`;
        if (c.phone && !c.costly)
            out += `<a class="button" href="tel:${esc(c.phone)}">Anrufen</a>`;
        out +=
            `<button class="primary" hx-post="/gui/rueckrufe/${esc(c.id)}/erledigt" hx-target="#rueckrufe-liste" hx-swap="innerHTML">Erledigt</button>`;
        out += `</div></li>`;
    }
    return out + `</ul>`;
}
// Number, the warning for a costly one and the note on where it came from. The
// open column and the sheet of done callbacks print the same lines; the sheet
// keeps the summary inside the number's branch.
function callbackPhone(c, column) {
    if (!c.phone)
        return `<div class="muted">Nummer nach der L&ouml;schfrist entfernt</div>`;
    let out = `<div class="phone">${esc(c.phone)}</div>`;
    if (c.costly)
        out += `<div class="cb-note"><span class="badge danger">Teure Sondernummer &ndash; vor dem Anruf pr&uuml;fen</span></div>`;
    if (c.number_note) {
        out += `<div class="cb-note"><span class="badge warn">${esc(c.number_note)}</span>`;
        if (c.caller_id)
            out += `<span class="muted">Anruf kam von ${esc(c.caller_id)}</span>`;
        out += `</div>`;
    }
    if (!column && c.summary)
        out += `<p class="summary">&bdquo;${esc(c.summary)}&ldquo;</p>`;
    return out;
}
export function renderHistory(callbacks, days) {
    let out = `<section class="sheet rueckruf-verlauf" role="dialog" aria-labelledby="verlauf-titel"><div class="sheet-head">` +
        `<h2 id="verlauf-titel" tabindex="-1">Erledigte R&uuml;ckrufe</h2>` +
        `<button type="button" class="schliessen" data-schliessen>Schlie&szlig;en</button></div><div class="sheet-body">`;
    if (!callbacks.length) {
        out += `<p class="empty">Keine erledigten R&uuml;ckrufe in den letzten ${days} Tagen.</p>`;
    }
    else {
        out += `<ul class="cards">`;
        for (const c of callbacks) {
            out += `<li class="card cb" data-id="${esc(c.id)}">`;
            out += callbackBody(c) + `<span class="muted">${esc(c.done ?? "")}</span></div>`;
            out += callbackPhone(c, false);
            out += `<div class="cb-actions">`;
            if (c.phone && !c.costly)
                out += `<a class="button" href="tel:${esc(c.phone)}">Anrufen</a>`;
            out +=
                `<button type="button" class="primary" hx-post="/gui/rueckrufe/${esc(c.id)}/wieder-offen" hx-target="#rueckruf-verlauf" hx-swap="innerHTML">Wieder &ouml;ffnen</button>`;
            out += `</div></li>`;
        }
        out += `</ul>`;
    }
    return out + `</div></section>`;
}
export function renderToday(today) {
    let out = `<h2>Heute${today.length ? ` <span class="count">${today.length}</span>` : ""}` +
        ` <button type="button" class="h2-action" hx-get="/gui/vorlauf" hx-target="#vorlauf">Reservierungs-Vorlauf</button></h2>` +
        `<button type="button" class="entry-open" hx-get="/gui/reservierung" hx-target="#reservierung">Reservierung eintragen</button>`;
    if (!today.length)
        return out + `<p class="empty">Noch keine Reservierungen f&uuml;r heute.</p>`;
    out += `<ul class="cards">`;
    for (const r of today) {
        out += `<li class="card res" data-id="${esc(r.id)}"><div class="res-head"><b class="time">${esc(r.time)}</b>`;
        out += `<span class="badge accent">${r.party_size} ${r.party_size === 1 ? "Person" : "Personen"}</span>`;
        if (r.is_test)
            out += `<span class="badge warn">TEST</span>`;
        out += `</div>`;
        if (r.is_test)
            out += `<div class="note">Test der Einrichtung, kein Gast. Die Karte verschwindet von selbst.</div>`;
        out += `<div class="name">${esc(r.guest_name)}</div>`;
        if (r.tables.length)
            out += `<div class="tables">Tisch ${r.tables.map(esc).join(" + ")}</div>`;
        if (r.phone)
            out += `<div class="muted">${esc(r.phone)}</div>`;
        if (r.note)
            out += `<div class="note">${esc(r.note)}</div>`;
        if (r.off_hours)
            out += `<div class="note">Bewusst außerhalb der Öffnungszeiten eingetragen.</div>`;
        out += `</li>`;
    }
    return out + `</ul>`;
}
// --- The visitor's restaurant -------------------------------------------------------
const ORDER_FAILED = "Das hat nicht geklappt. Bitte nochmal tippen.";
const STALE = "Die Bestellung wurde inzwischen geändert. Bitte nochmal prüfen.";
const SWITCH_FAILED = "Umschalten hat nicht geklappt. Bitte nochmal tippen.";
const WAIT_RANGE = (min, max) => `Die Wartezeit geht von ${min} bis ${max} Minuten.`;
function fromCard(card) {
    const waiting = /seit (\d\d:\d\d)/.exec(card.state);
    return {
        card,
        // A red card that cannot be marked is one whose slip failed before "Passt".
        status: card.cancelled
            ? "cancelled"
            : card.can_mark
                ? "approved"
                : card.can_approve || card.failed
                    ? "confirmed"
                    : "approved",
        handover: card.failed
            ? "failed"
            : card.awaiting
                ? null
                : card.state === "Zettel wird gedruckt"
                    ? "pending"
                    : "sent",
        registered: false,
        waitingSince: card.overdue && waiting ? (waiting[1] ?? null) : null,
        overdue: card.overdue,
        registerLate: card.register_late,
        lateText: card.register_late && card.register ? card.register[1] : null,
    };
}
// orders_view.card, for the parts a tap changes.
export function deriveCard(o) {
    const cancelled = o.status === "cancelled";
    let tone;
    let state;
    if (o.handover === "failed") {
        tone = "danger";
        state = `${cancelled ? "Storno-Zettel" : "Zettel"} nicht gedruckt`;
    }
    else if (o.overdue && o.waitingSince !== null) {
        tone = "danger";
        state = `Wartet seit ${o.waitingSince} auf Freigabe`;
    }
    else if (o.handover === null) {
        tone = "warn";
        state = "Noch nicht an der Kasse";
    }
    else if (o.handover === "pending") {
        tone = "ok";
        state = "Zettel wird gedruckt";
    }
    else {
        tone = "ok";
        state = "Zettel liegt an der Kasse";
    }
    let register = null;
    let canMark = false;
    if (!o.registered && !cancelled) {
        register = o.registerLate
            ? ["danger", o.lateText ?? "Nicht als in der Kasse markiert"]
            : ["warn", "Noch nicht in der Kasse eingegeben"];
        canMark = o.status === "approved";
    }
    const compact = canMark && !o.registerLate && o.handover === "sent";
    return {
        ...o.card,
        tone,
        state,
        failed: o.handover === "failed",
        awaiting: o.handover === null,
        overdue: o.overdue,
        can_approve: o.status === "confirmed" && o.handover !== "failed",
        can_change: !cancelled,
        cancelled,
        register,
        can_mark: canMark,
        register_late: o.registerLate,
        compact,
    };
}
// Board order of list_new: red cards first, then the overdue, then the late, then
// the rest; oldest first within each (a stable sort of the creation order).
function rank(o) {
    return o.handover === "failed" ? 0 : o.overdue ? 1 : o.registerLate ? 2 : 3;
}
export class Restaurant {
    constructor(data) {
        this.serial = 0;
        this.minutes = 0;
        // Today's waits differ from the standard until the business day ends; the
        // standard is the third field of a wait.
        this.orderProblem = null;
        this.data = data;
        const marked = data.menu.filter((d) => d.sold_out).map((d) => d.number);
        this.off = new Set(marked.length ? marked : [data.sold_out]);
        this.header = structuredClone(data.header);
        this.orders = data.orders.orders.map((c) => fromCard(structuredClone(c)));
        this.callbacks = structuredClone(data.callbacks.callbacks);
        this.done = structuredClone(data.history.callbacks);
        this.today = structuredClone(data.today.today);
        this.modeBeforePause = this.currentMode();
        if (this.modeBeforePause === "paused")
            this.modeBeforePause = this.modes()[this.modes().length - 1] ?? "primary";
    }
    // The demo clock: the board's noon, then the minutes the visitor has spent here.
    tick(minutes) {
        this.minutes = minutes;
    }
    now() {
        return clockPlus(this.data.clock, this.minutes);
    }
    modes() {
        return this.data.header.mode_choices.map((c) => c[0]);
    }
    currentMode() {
        return this.header.config?.call_mode ?? "paused";
    }
    // --- Markup of the five answers ---
    kopfzeile() {
        return renderHeader(this.header);
    }
    bestellungen() {
        const problem = this.orderProblem;
        this.orderProblem = null;
        const sorted = this.orders
            .map((o, i) => ({ o, i }))
            .sort((a, b) => rank(a.o) - rank(b.o) || a.i - b.i)
            .map((x) => deriveCard(x.o));
        return renderOrders(sorted, problem);
    }
    rueckrufe() {
        return renderCallbacks(this.callbacks);
    }
    heute() {
        return renderToday([...this.today].sort((a, b) => a.time.localeCompare(b.time)));
    }
    verlauf() {
        return renderHistory(this.done, this.data.history.days);
    }
    waitSheet(service) {
        const wait = this.header.waits.find((w) => w[0] === service);
        if (!wait)
            return null;
        return renderWaitSheet(wait[0], wait[1], wait[2], wait[3], this.header.wait_min, this.header.wait_max);
    }
    // --- The header ---
    refreshMode() {
        const mode = this.currentMode();
        const choices = this.data.header.mode_choices;
        const rank = choices.findIndex((c) => c[0] === mode);
        const labels = this.data.mode_labels;
        const [toneBase, label] = labels[mode] ?? ["danger", "KI ist aus"];
        let tone = toneBase;
        if (tone === "ok" && !this.header.services.some((s) => s[2]))
            tone = "warn";
        this.header.mode_tone = tone;
        this.header.mode_label = label;
        this.header.mode_choices = choices.map((c, i) => [c[0], c[1], c[0] === mode, i > rank]);
    }
    mark(problem) {
        this.header.header_problem = problem;
        return { status: problem ? 409 : 200, problem };
    }
    pause() {
        if (this.currentMode() !== "paused") {
            this.modeBeforePause = this.currentMode();
            this.header.config = { call_mode: "paused" };
            this.refreshMode();
        }
        return this.mark(null);
    }
    resume() {
        if (this.currentMode() === "paused") {
            this.header.config = { call_mode: this.modeBeforePause };
            this.refreshMode();
        }
        return this.mark(null);
    }
    // The mode switch: never into the pause, never out of it (domain/status/config.py).
    setMode(mode) {
        if (!this.modes().includes(mode))
            return null;
        if (this.currentMode() === "paused")
            return this.mark(SWITCH_FAILED);
        this.header.config = { call_mode: mode };
        this.refreshMode();
        return this.mark(null);
    }
    setService(key, state) {
        const service = this.header.services.find((s) => s[0] === key);
        if (!service || (state !== "an" && state !== "aus"))
            return null;
        service[2] = state === "an";
        this.refreshMode();
        return this.mark(null);
    }
    // A step goes to the next multiple of the step, held to the limits, and a value
    // beyond a limit is never pulled back (config._next_wait).
    stepWait(key, step) {
        const wait = this.header.waits.find((w) => w[0] === key);
        const size = this.header.wait_step;
        if (!wait || (step !== size && step !== -size))
            return null;
        const before = wait[2];
        if (step < 0) {
            const floor = Math.min(this.header.wait_min, before);
            wait[2] = before <= floor ? before : Math.max(Math.floor((before - 1) / size) * size, floor);
        }
        else {
            const ceiling = Math.max(this.header.wait_max, before);
            wait[2] = before >= ceiling ? before : Math.min((Math.floor(before / size) + 1) * size, ceiling);
        }
        return this.mark(null);
    }
    typed(text) {
        const t = text.trim();
        return /^[0-9]{1,4}$/.test(t) ? Number(t) : -1;
    }
    inRange(minutes) {
        return minutes >= this.header.wait_min && minutes <= this.header.wait_max;
    }
    setWait(key, text) {
        const wait = this.header.waits.find((w) => w[0] === key);
        if (!wait)
            return null;
        const minutes = this.typed(text);
        if (!this.inRange(minutes))
            return this.mark(WAIT_RANGE(this.header.wait_min, this.header.wait_max));
        wait[2] = minutes;
        return this.mark(null);
    }
    setStandard(key, text) {
        const wait = this.header.waits.find((w) => w[0] === key);
        if (!wait)
            return null;
        const minutes = this.typed(text);
        if (!this.inRange(minutes))
            return this.mark(WAIT_RANGE(this.header.wait_min, this.header.wait_max));
        wait[3] = minutes;
        return this.mark(null);
    }
    resetWait(key) {
        const wait = this.header.waits.find((w) => w[0] === key);
        if (!wait)
            return null;
        wait[2] = wait[3];
        return this.mark(null);
    }
    isOff(number) {
        return this.off.has(number);
    }
    // One tap on "Gericht aus" or "Wieder da": the tile follows with the count.
    setOff(number, off) {
        if (off)
            this.off.add(number);
        else
            this.off.delete(number);
        this.header.sold_out_count = this.off.size;
    }
    // --- The sheets of B3 reach into the board through these ---
    // The card as the board shows it, for the correction sheet.
    card(id) {
        const o = this.find(id);
        return o ? deriveCard(o) : null;
    }
    // A saved correction (api/domain/ordering/correction.py apply_correction): the
    // lines and the sum change, the order is no longer marked as in the register, and
    // a slip already sent is printed again.
    correct(id, lines, cents, reason) {
        const o = this.find(id);
        if (!o)
            return false;
        o.card.lines = lines;
        o.card.total = euro(cents);
        o.card.corrected = reason;
        o.card.version = `${o.card.version}c`;
        o.registered = false;
        o.registerLate = false;
        o.lateText = null;
        if (o.handover !== null)
            o.handover = "pending";
        return true;
    }
    // A table the team entered by hand is on the board's "Heute".
    enter(reservation) {
        if (!this.today.some((r) => r.id === reservation.id))
            this.today.push(reservation);
    }
    // What is booked on the day, for the window's load of the reservation sheet.
    booked() {
        return this.today;
    }
    // --- Orders (api/domain/ordering/board.py) ---
    find(id) {
        return this.orders.find((o) => o.card.id === id);
    }
    refuse(problem) {
        this.orderProblem = problem;
        return { status: 200, problem };
    }
    // The card's own tap: an order that is gone is no error for the team, the
    // fresh column is the answer.
    approve(id) {
        const o = this.find(id);
        if (!o || o.status === "approved")
            return { status: 200 };
        if (o.status !== "confirmed")
            return this.refuse(STALE);
        if (o.handover === "failed")
            return this.refuse(ORDER_FAILED);
        const released = o.handover === null;
        o.status = "approved";
        o.overdue = false;
        o.waitingSince = null;
        if (released)
            o.handover = "pending";
        return { status: 200 };
    }
    resend(id) {
        const o = this.find(id);
        if (o && o.handover === "failed")
            o.handover = "pending";
        return { status: 200 };
    }
    // The print bridge reports a slip lying at the register; the demo has no bridge,
    // the backend calls this a moment after "Passt". True when a card changed.
    slipPrinted(id) {
        const o = this.find(id);
        if (!o || o.handover !== "pending")
            return false;
        o.handover = "sent";
        return true;
    }
    pendingSlips() {
        return this.orders.filter((o) => o.handover === "pending").map((o) => o.card.id);
    }
    markEntered(id, version) {
        const o = this.find(id);
        if (!o)
            return { status: 200 };
        if (o.status !== "approved")
            return this.refuse(STALE);
        if (o.card.version !== version)
            return this.refuse(STALE);
        this.orders = this.orders.filter((x) => x !== o);
        return { status: 200 };
    }
    cancel(id, version) {
        const o = this.find(id);
        if (!o || o.status === "cancelled")
            return { status: 200 };
        if (o.card.version !== version)
            return this.refuse(STALE);
        const name = o.card.pickup_code ?? o.card.name;
        // A cancelled order leaves the column; its note for the register is the bridge's.
        this.orders = this.orders.filter((x) => x !== o);
        return { status: 200, notice: `Bestellung ${name}: Storniert` };
    }
    // --- Callbacks (api/domain/callbacks/board.py) ---
    callbackDone(id) {
        const i = this.callbacks.findIndex((c) => c.id === id);
        if (i < 0)
            return;
        const [row] = this.callbacks.splice(i, 1);
        if (row)
            this.done.unshift({ ...row, done: `Erledigt ${this.now()}` });
    }
    callbackReopen(id) {
        const i = this.done.findIndex((c) => c.id === id);
        if (i < 0)
            return;
        const [row] = this.done.splice(i, 1);
        if (row) {
            const { done: _done, ...open } = row;
            this.callbacks.push(open);
            this.callbacks.sort((a, b) => a.time.localeCompare(b.time));
        }
    }
    // --- "Beispielanruf" ---
    // What a call does depends on the board: with the agent paused, or only
    // listening ("KI hört mit"), nobody answers it but the team, so the call
    // becomes a callback; otherwise the agent takes the order when pickup is on
    // and the table when reservations are on; with both off the guest is passed
    // on as a callback too. The demo's scripted call is one guest wanting both.
    exampleCall() {
        const mode = this.currentMode();
        const on = (key) => this.header.services.find((s) => s[0] === key)?.[2] ?? false;
        const answers = mode !== "paused" && mode !== "shadow";
        const order = answers && on("abholung");
        const reservation = answers && on("reservierung");
        const callback = !order && !reservation;
        const n = this.serial++;
        const time = this.now();
        const phone = this.data.phones[n % this.data.phones.length] ?? null;
        if (order)
            this.addOrder(n, time, phone ?? "");
        if (reservation)
            this.addReservation(n, time, phone);
        if (callback)
            this.addCallback(n, time, phone);
        return { order, reservation, callback };
    }
    dishes() {
        // Two dishes of different categories, never the one marked off.
        const open = this.data.menu.filter((d) => !this.off.has(d.number));
        const first = open[(this.serial * 7) % open.length];
        const second = open.find((d) => d.category !== first?.category && d.number !== first?.number);
        return [first, second].filter((d) => d !== undefined);
    }
    addOrder(n, time, phone) {
        const dishes = this.dishes();
        const lines = dishes.map((d, i) => ({
            quantity: i === 0 ? 2 : 1,
            number: d.number,
            name: d.name,
            options: [],
            note: null,
        }));
        const cents = dishes.reduce((sum, d, i) => sum + d.cents * (i === 0 ? 2 : 1), 0);
        const id = `demo-order-${n}`;
        const code = `A${n + 5}`;
        const card = {
            id,
            time,
            type: "Abholung",
            pickup_code: code,
            name: "Beispielgast",
            phone,
            note: null,
            delivery: false,
            address: null,
            floor_note: null,
            fee: null,
            lines,
            total: euro(cents),
            ready: clockPlus(time, this.header.waits.find((w) => w[0] === "abholung")?.[2] ?? 20),
            tone: "warn",
            state: "Noch nicht an der Kasse",
            failed: false,
            awaiting: true,
            overdue: false,
            can_approve: true,
            can_change: true,
            cancelled: false,
            version: `demo${n}`,
            corrected: null,
            compact: false,
            register: ["warn", "Noch nicht in der Kasse eingegeben"],
            can_mark: false,
            register_late: false,
        };
        this.orders.push({
            card,
            status: "confirmed",
            handover: null,
            registered: false,
            waitingSince: null,
            overdue: false,
            registerLate: false,
            lateText: null,
        });
    }
    addReservation(n, time, phone) {
        // An hour on from the call, on the half hour.
        const [h, m] = clockPlus(time, 60).split(":").map(Number);
        const at = `${two(h ?? 0)}:${(m ?? 0) < 30 ? "00" : "30"}`;
        this.today.push({
            id: `demo-res-${n}`,
            time: at,
            party_size: 4,
            guest_name: "Beispielgast",
            phone,
            note: null,
            tables: [this.data.table],
            is_test: false,
            off_hours: false,
        });
    }
    addCallback(n, time, phone) {
        const reason = this.currentMode() === "paused" || this.currentMode() === "shadow" ? "human_requested" : "out_of_scope";
        const [tone, label] = this.data.callback_labels[reason] ?? ["accent", "Rückruf"];
        this.callbacks.push({
            id: `demo-cb-${n}`,
            time,
            phone,
            costly: false,
            number_note: null,
            caller_id: null,
            reason,
            summary: reason === "human_requested"
                ? "Möchte etwas bestellen und einen Tisch für vier Personen reservieren. Die KI war aus."
                : "Möchte etwas bestellen und einen Tisch reservieren. Abholung und Reservierung sind aus.",
            tone,
            label,
        });
    }
}
