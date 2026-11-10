// The simulator console (dev/console.html, ENV=dev only). This page does not
// load app.js: show the red bar of base.html when a tap fails for real (5xx, no
// network), or the form looks like it did nothing.
//
// Source: api/gui/ts/dev-console.ts. The file of the same name in api/gui/static/
// is its compiled output (`npm run build`, docs/22); edit the source, never the
// output.

// Marks the file as a module, so nothing here reaches the page's global scope.
export {};

function showFailure(failed: boolean): void {
  const bar = document.getElementById("stoerung");
  if (bar) bar.hidden = !failed;
}

["htmx:responseError", "htmx:sendError"].forEach((name) => {
  document.body.addEventListener(name, () => {
    showFailure(true);
  });
});
// A tap that works again clears the bar of the failed one before it. htmx says
// in the event's detail whether the request worked; nothing types that object
// for us, so it is checked before it is read.
document.body.addEventListener("htmx:afterRequest", (event) => {
  const detail: unknown = event instanceof CustomEvent ? event.detail : null;
  if (typeof detail !== "object" || detail === null) return;
  if ((detail as { successful?: unknown }).successful) showFailure(false);
});
