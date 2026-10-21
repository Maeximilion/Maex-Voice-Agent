// Loaded by base.html on every page. A file, not inline code: the proxy's
// Content-Security-Policy allows scripts from this origin only (docs/13 §3).

// Every write sends the CSRF cookie as a header (docs/06 section 9).
document.addEventListener("htmx:configRequest", function (e) {
  var m = document.cookie.match(/(?:^|; )csrf_token=([^;]*)/);
  if (m) e.detail.headers["X-CSRF-Token"] = m[1];
});

// "Nochmal versuchen" on the red bar reloads the page.
document.addEventListener("click", function (e) {
  if (e.target.closest("[data-neu-laden]")) location.reload();
});
