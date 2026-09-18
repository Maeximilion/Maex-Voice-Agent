// Live-Aktualisierung der Betriebsansicht (docs/06 §1 Regel 5).
// Der Server schickt nur ein Signal, die Seite holt das Fragment - so bleibt die
// Darstellung im Jinja2-Template und es gibt keine zweite Wahrheit im Browser.
(function () {
  "use strict";
  var banner = document.getElementById("verbindung");
  var liste = document.getElementById("heute-liste");

  var stoerung = document.getElementById("stoerung");

  // Ein Tap, der am Server scheitert, liefert trotzdem die Kopfzeile mit der
  // roten Leiste. htmx tauscht bei 4xx/5xx sonst nichts aus, und der Knopf
  // saehe aus, als haette er nichts getan. Getauscht wird aber nur HTML: andere
  // Fehler (HTTPException, unbehandelte Ausnahmen) kommen aus main.py als
  // JSON-Huelle, die an die Stelle der Knoepfe oder der Liste zu setzen waere
  // schlimmer als der Fehler selbst. Dann bleibt die Ansicht stehen und die
  // rote Leiste oben meldet es (docs/06 §5).
  document.body.addEventListener("htmx:beforeSwap", function (e) {
    var xhr = e.detail.xhr;
    if (xhr.status < 400) {
      if (stoerung) stoerung.hidden = true;
      return;
    }
    var typ = xhr.getResponseHeader("Content-Type") || "";
    if (typ.indexOf("text/html") === 0 && xhr.responseText) {
      e.detail.shouldSwap = true;
      e.detail.isError = false;
    } else {
      e.detail.shouldSwap = false;
      if (stoerung) stoerung.hidden = false;
    }
  });
  // Server nicht erreichbar: gelbe Leiste statt stiller Knopf (docs/06 §5).
  document.body.addEventListener("htmx:sendError", function () {
    offline(true);
  });

  if (!liste || !window.EventSource) return;

  function offline(yes) {
    if (banner) banner.hidden = !yes;
  }

  function nachladen() {
    // htmx ersetzt den Inhalt des Ziels; faellt der Aufruf aus, bleibt die
    // zuletzt gezeigte Liste stehen statt zu leeren.
    window.htmx.ajax("GET", "/gui/fragments/heute", { target: "#heute-liste" });
  }

  function kopfzeile() {
    window.htmx.ajax("GET", "/gui/fragments/kopfzeile", {
      target: "#kopfzeile",
      swap: "outerHTML",
    });
  }

  var strom = new EventSource("/gui/events");
  // Umgeschaltet an einem anderen Tablet oder direkt in der Datenbank: jedes
  // Geraet zeigt denselben Stand.
  strom.addEventListener("header", function () {
    offline(false);
    kopfzeile();
  });
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
