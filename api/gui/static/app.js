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

  function rueckrufe() {
    window.htmx.ajax("GET", "/gui/fragments/rueckrufe", {
      target: "#rueckrufe-liste",
    });
  }

  // Ton bei neuen Rueckrufen (docs/06 §1 Regel 5), Beschwerden mit eigenem Ton
  // (§3). Erzeugt statt Audiodatei: nichts nachzuladen, laeuft ohne Internet.
  // Browser spielen erst nach dem ersten Tap auf die Seite Ton ab; am Tablet
  // ist das nach dem ersten Handgriff der Fall.
  var audio = null;
  var Ctx = window.AudioContext || window.webkitAudioContext;
  // Safari auf dem Tablet schaltet Web Audio nur innerhalb einer Geste frei:
  // der Kontext entsteht deshalb im Tap, nicht erst beim ersten Rueckruf, und
  // ein stiller Puffer spielt einmal an. Ohne das bliebe das Tablet stumm.
  function freischalten() {
    if (!Ctx) return;
    if (!audio) {
      audio = new Ctx();
      var still = audio.createBufferSource();
      still.buffer = audio.createBuffer(1, 1, 22050);
      still.connect(audio.destination);
      still.start(0);
    }
    if (audio.state === "suspended") audio.resume();
  }
  document.addEventListener("touchstart", freischalten, { passive: true });
  document.addEventListener("click", freischalten);

  function ton(folge) {
    // Noch kein Tap auf die Seite: kein Kontext, kein Ton (Browserregel).
    if (!audio) return;
    var t = audio.currentTime;
    folge.forEach(function (hz, i) {
      var osc = audio.createOscillator();
      var gain = audio.createGain();
      osc.frequency.value = hz;
      gain.gain.setValueAtTime(0.3, t + i * 0.25);
      gain.gain.exponentialRampToValueAtTime(0.001, t + i * 0.25 + 0.22);
      osc.connect(gain).connect(audio.destination);
      osc.start(t + i * 0.25);
      osc.stop(t + i * 0.25 + 0.23);
    });
  }
  function karten(liste) {
    var ids = {};
    document.querySelectorAll("#" + liste + " [data-id]").forEach(function (el) {
      ids[el.getAttribute("data-id")] = el.getAttribute("data-reason");
    });
    return ids;
  }
  // Was beim Laden schon da war, klingelt nicht.
  var bekannt = karten("rueckrufe-liste");
  var bekannteBestellungen = karten("bestellungen-liste");
  function neueIds(vorher, jetzt) {
    return Object.keys(jetzt).filter(function (id) {
      return !(id in vorher);
    });
  }
  document.body.addEventListener("htmx:afterSwap", function (e) {
    var ziel = e.detail.target && e.detail.target.id;
    if (ziel === "bestellungen-liste") {
      var jetztB = karten("bestellungen-liste");
      var neuB = neueIds(bekannteBestellungen, jetztB);
      bekannteBestellungen = jetztB;
      if (!neuB.length) return;
      // Eigener Ton fuer Bestellungen, dazu kurzes Blinken (docs/06 §3).
      // Die erste neue Karte rollt ins Bild: steht sie unten, saehe sonst
      // niemand das Blinken.
      ton([523, 659, 784]);
      neuB.forEach(function (id, i) {
        var el = document.querySelector('#bestellungen-liste [data-id="' + id + '"]');
        if (!el) return;
        el.classList.add("neu");
        if (i === 0) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
      });
      return;
    }
    if (ziel !== "rueckrufe-liste") return;
    var jetzt = karten("rueckrufe-liste");
    var neu = neueIds(bekannt, jetzt);
    bekannt = jetzt;
    if (!neu.length) return;
    var beschwerde = neu.some(function (id) {
      return jetzt[id] === "complaint";
    });
    ton(beschwerde ? [660, 440, 660, 440] : [880, 1175]);
  });

  function bestellungen() {
    window.htmx.ajax("GET", "/gui/fragments/bestellungen", {
      target: "#bestellungen-liste",
    });
  }

  // Korrektur: eigener Kasten ueber den Spalten. Er schliesst nach dem
  // Speichern (Ereignis vom Server), mit "Abbrechen" und nach 90 Sekunden ohne
  // Tap - wer zum klingelnden Telefon geht, laesst sonst eine halbe Korrektur
  // offen stehen.
  var korrektur = document.getElementById("korrektur");
  var ruhe = null;
  var RUHE_MS = 90000;
  function korrekturZu() {
    if (korrektur) korrektur.innerHTML = "";
    if (ruhe) window.clearTimeout(ruhe);
    ruhe = null;
  }
  function wecker() {
    if (ruhe) window.clearTimeout(ruhe);
    ruhe = window.setTimeout(korrekturZu, RUHE_MS);
  }
  if (korrektur) {
    korrektur.addEventListener("click", function (e) {
      if (e.target.closest("[data-schliessen]")) {
        korrekturZu();
        return;
      }
      wecker();
    });
    // Ins Bild rollen nur beim Oeffnen, nicht nach jedem Tap: rollt die Seite
    // unter dem Finger weg, trifft der naechste Tap einen anderen Knopf (im
    // Browsertest die Wartezeit in der Kopfzeile).
    var warOffen = false;
    document.body.addEventListener("htmx:beforeSwap", function (e) {
      if (e.detail.target === korrektur) warOffen = !!korrektur.firstElementChild;
    });
    document.body.addEventListener("htmx:afterSwap", function (e) {
      if (e.detail.target !== korrektur || !korrektur.firstElementChild) return;
      wecker();
      if (!warOffen) korrektur.scrollIntoView({ block: "start" });
    });
  }
  document.body.addEventListener("bestellungen-geaendert", function () {
    korrekturZu();
    bestellungen();
  });

  var strom = new EventSource("/gui/events");
  // Laedt auch, waehrend jemand korrigiert: der Kasten liegt ausserhalb der
  // Liste und bleibt stehen (Design-Review T-4.7).
  strom.addEventListener("orders", function () {
    offline(false);
    bestellungen();
  });
  strom.addEventListener("callbacks", function () {
    offline(false);
    rueckrufe();
  });
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
