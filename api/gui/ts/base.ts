// Loaded by base.html on every page. A file, not inline code: the proxy's
// Content-Security-Policy allows scripts from this origin only (docs/13 §3).
//
// Source: api/gui/ts/base.ts. The file of the same name in api/gui/static/ is its
// compiled output (`npm run build`, docs/22); edit the source, never the output.

// Marks the file as a module, so its helper stays out of the page's global scope.
export {};

// htmx hands the request headers over in the event's detail. Nothing types that
// object for us, so it is checked before a header is written to it.
function requestHeaders(event: Event): Record<string, string> | null {
  if (!(event instanceof CustomEvent)) return null;
  const detail: unknown = event.detail;
  if (typeof detail !== "object" || detail === null) return null;
  const headers: unknown = (detail as { headers?: unknown }).headers;
  if (typeof headers !== "object" || headers === null) return null;
  return headers as Record<string, string>;
}

// Every write sends the CSRF cookie as a header (docs/06 section 9).
document.addEventListener("htmx:configRequest", (event) => {
  const token = document.cookie.match(/(?:^|; )csrf_token=([^;]*)/);
  const headers = requestHeaders(event);
  if (token && token[1] !== undefined && headers) headers["X-CSRF-Token"] = token[1];
});

// "Nochmal versuchen" on the red bar reloads the page.
document.addEventListener("click", (event) => {
  if (event.target instanceof Element && event.target.closest("[data-neu-laden]")) {
    location.reload();
  }
});

// The setup link carries its code after "#", which no request sends (#570): copy
// it into the form, then drop it from the address bar and the history.
(() => {
  const field = document.querySelector("[data-setup-code]");
  if (!(field instanceof HTMLInputElement) || location.hash.length < 2) return;
  try {
    field.value = decodeURIComponent(location.hash.slice(1));
  } catch {
    // A code that is not valid percent-encoding is left in the address bar.
    return;
  }
  history.replaceState(null, "", location.pathname);
})();
