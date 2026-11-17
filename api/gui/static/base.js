// Loaded by base.html on every page. A file, not inline code: the proxy's
// Content-Security-Policy allows scripts from this origin only (docs/13 §3).
//
// Source: api/gui/ts/base.ts. The file of the same name in api/gui/static/ is its
// compiled output (`npm run build`, docs/22); edit the source, never the output.
// Marks the file as a module, so its helper stays out of the page's global scope.
export {};
// htmx hands the request headers over in the event's detail. Nothing types that
// object for us, so it is checked before a header is written to it.
function requestHeaders(event) {
    if (!(event instanceof CustomEvent))
        return null;
    const detail = event.detail;
    if (typeof detail !== "object" || detail === null)
        return null;
    const headers = detail.headers;
    if (typeof headers !== "object" || headers === null)
        return null;
    return headers;
}
// Every write sends the CSRF cookie as a header (docs/06 section 9).
document.addEventListener("htmx:configRequest", (event) => {
    const token = document.cookie.match(/(?:^|; )csrf_token=([^;]*)/);
    const headers = requestHeaders(event);
    if (token && token[1] !== undefined && headers)
        headers["X-CSRF-Token"] = token[1];
});
// A tap the server refused without a page to show (a 403, a 500), or one that
// never arrived: the red bar says so (docs/06 section 5), on every page.
// Without it a button of a sub page looks as if it had done nothing. The next
// tap that works takes the bar away again, so the page never says both. The
// board swaps the HTML of a refused tap itself (app.ts) and so raises no error
// for those, and it reports a lost connection in its own yellow bar: its
// listener sits on the body and has run before this one.
function failed(show) {
    const bar = document.getElementById("stoerung");
    if (bar)
        bar.hidden = !show;
}
document.addEventListener("htmx:responseError", () => failed(true));
document.addEventListener("htmx:sendError", () => {
    const offline = document.getElementById("verbindung");
    if (offline && !offline.hidden)
        return;
    failed(true);
});
document.addEventListener("htmx:afterRequest", (event) => {
    if (!(event instanceof CustomEvent))
        return;
    const detail = event.detail;
    if (typeof detail !== "object" || detail === null)
        return;
    if (detail.successful === true)
        failed(false);
});
// After a swap the answer's mark `data-fokus` (its message or list of faults,
// else its heading) takes the keyboard focus and comes into view (#821,
// #829-2): the tapped button is gone with the old HTML, and the message of a
// long form would sit above the screen. Only the owner's forms carry the mark;
// the board's live updates never move the focus.
document.addEventListener("htmx:afterSwap", (event) => {
    if (!(event.target instanceof Element))
        return;
    const mark = event.target.querySelector("[data-fokus]");
    if (!(mark instanceof HTMLElement))
        return;
    mark.focus({ preventScroll: true });
    mark.scrollIntoView({ block: "nearest" });
});
// "Nochmal versuchen" on the red bar reloads the page.
document.addEventListener("click", (event) => {
    if (event.target instanceof Element && event.target.closest("[data-neu-laden]")) {
        location.reload();
    }
});
// "Anzeigen" beside a password field shows what was typed and hides it again
// (#571): `data-zeigen` names the field's id. The operator dashboard, which is
// English, names its two labels in `data-zeigen-an` and `data-zeigen-aus`.
document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element))
        return;
    const button = event.target.closest("[data-zeigen]");
    if (!(button instanceof HTMLButtonElement))
        return;
    const field = document.getElementById(button.dataset["zeigen"] ?? "");
    if (!(field instanceof HTMLInputElement))
        return;
    const show = field.type === "password";
    field.type = show ? "text" : "password";
    button.setAttribute("aria-pressed", String(show));
    button.textContent = show
        ? (button.dataset["zeigenAn"] ?? "Verbergen")
        : (button.dataset["zeigenAus"] ?? "Anzeigen");
});
// A shown password is hidden again before its form leaves: a browser may keep
// what a text field sent in its form history, on a tablet the whole team uses.
document.addEventListener("submit", (event) => {
    if (!(event.target instanceof HTMLFormElement))
        return;
    for (const button of event.target.querySelectorAll('[data-zeigen][aria-pressed="true"]')) {
        if (button instanceof HTMLButtonElement)
            button.click();
    }
});
// The setup link carries its code after "#", which no request sends (#570): copy
// it into the form, then drop it from the address bar and the history.
(() => {
    const field = document.querySelector("[data-setup-code]");
    if (!(field instanceof HTMLInputElement) || location.hash.length < 2)
        return;
    try {
        field.value = decodeURIComponent(location.hash.slice(1));
    }
    catch {
        // A code that is not valid percent-encoding is left in the address bar.
        return;
    }
    history.replaceState(null, "", location.pathname);
})();
