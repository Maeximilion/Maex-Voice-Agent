// Live-Aktualisierung der Betriebsansicht (docs/06 §1 Regel 5).
// Der Server schickt nur ein Signal, die Seite holt das Fragment - so bleibt die
// Darstellung im Jinja2-Template und es gibt keine zweite Wahrheit im Browser.
(function () {
  "use strict";
  var banner = document.getElementById("verbindung");
  var liste = document.getElementById("heute-liste");
  if (!liste || !window.EventSource) return;

  function offline(yes) {
    if (banner) banner.hidden = !yes;
  }

  function nachladen() {
    // htmx ersetzt den Inhalt des Ziels; faellt der Aufruf aus, bleibt die
    // zuletzt gezeigte Liste stehen statt zu leeren.
    window.htmx.ajax("GET", "/gui/fragments/heute", { target: "#heute-liste" });
  }

  var strom = new EventSource("/gui/events");
  strom.addEventListener("today", function () {
    offline(false);
    nachladen();
  });
  strom.addEventListener("problem", function () {
    offline(true);
  });
  strom.onopen = function () {
    offline(false);
  };
  // Der Browser verbindet nach dem Abbruch von selbst neu (retry im Strom).
  strom.onerror = function () {
    offline(true);
  };
})();
