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

  // The holiday strip is replaced with its list after every answer; keep it open
  // while the team works through the suggestions, or each tap would fold it.
  var feiertageOffen = false;
  document.addEventListener("toggle", function (e) {
    if (e.target.id === "feiertage-fold") feiertageOffen = e.target.open;
  }, true);
  document.body.addEventListener("htmx:afterSwap", function () {
    var fold = document.getElementById("feiertage-fold");
    if (fold && feiertageOffen) fold.open = true;
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

  function feiertage() {
    window.htmx.ajax("GET", "/gui/fragments/feiertage", {
      target: "#feiertage-liste",
    });
  }

  function reloadPrices() {
    window.htmx.ajax("GET", "/gui/fragments/preise", {
      target: "#preise-liste",
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
  // Orders that wait for "Passt" longer than the restaurant allows (data-overdue).
  function overdueOrders() {
    var ids = {};
    document
      .querySelectorAll("#bestellungen-liste [data-overdue]")
      .forEach(function (el) {
        ids[el.getAttribute("data-id")] = true;
      });
    return ids;
  }
  // Was beim Laden schon da war, klingelt nicht.
  var bekannt = karten("rueckrufe-liste");
  var knownOrders = karten("bestellungen-liste");
  var knownOverdue = overdueOrders();
  function newIds(vorher, jetzt) {
    return Object.keys(jetzt).filter(function (id) {
      return !(id in vorher);
    });
  }
  document.body.addEventListener("htmx:afterSwap", function (e) {
    var ziel = e.detail.target && e.detail.target.id;
    if (ziel === "bestellungen-liste") {
      var currentOrders = karten("bestellungen-liste");
      var newOrders = newIds(knownOrders, currentOrders);
      knownOrders = currentOrders;
      // A card that turned red since the last look: nobody has tapped "Passt",
      // so nobody is looking. The alarm of a complaint, once per card.
      var currentOverdue = overdueOrders();
      var turnedRed = newIds(knownOverdue, currentOverdue);
      knownOverdue = currentOverdue;
      if (turnedRed.length) {
        ton([660, 440, 660, 440]);
        var red = document.querySelector(
          '#bestellungen-liste [data-id="' + turnedRed[0] + '"]'
        );
        if (red) red.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
      if (!newOrders.length) return;
      // A sheet covers the columns: say so on the sheet, a tap shows the card.
      notifySheet(
        "Neue Bestellung \u00b7 ansehen",
        '#bestellungen-liste [data-id="' + newOrders[0] + '"]'
      );
      // Eigener Ton fuer Bestellungen, dazu kurzes Blinken (docs/06 §3).
      // Die erste neue Karte rollt ins Bild: steht sie unten, saehe sonst
      // niemand das Blinken. A card that turned red in the same redraw keeps
      // the sound and the view: the new cards still blink.
      if (!turnedRed.length) ton([523, 659, 784]);
      newOrders.forEach(function (id, i) {
        var el = document.querySelector('#bestellungen-liste [data-id="' + id + '"]');
        if (!el) return;
        el.classList.add("neu");
        if (i === 0 && !turnedRed.length) {
          el.scrollIntoView({ block: "nearest", behavior: "smooth" });
        }
      });
      return;
    }
    if (ziel !== "rueckrufe-liste") return;
    var jetzt = karten("rueckrufe-liste");
    var neu = newIds(bekannt, jetzt);
    bekannt = jetzt;
    if (!neu.length) return;
    var beschwerde = neu.some(function (id) {
      return jetzt[id] === "complaint";
    });
    if (beschwerde) {
      notifySheet(
        "Neue Beschwerde \u00b7 ansehen",
        '#rueckrufe-liste [data-id="' +
          neu.filter(function (id) {
            return jetzt[id] === "complaint";
          })[0] +
          '"]'
      );
    }
    ton(beschwerde ? [660, 440, 660, 440] : [880, 1175]);
  });

  function reloadOrders() {
    window.htmx.ajax("GET", "/gui/fragments/bestellungen", {
      target: "#bestellungen-liste",
    });
  }

  // Sheet (T-3.8): "Korrigieren" and "Gericht aus" open the same way, as a
  // sheet over the columns, which stay live underneath. The sheet takes the
  // focus, the columns below are inert meanwhile, and on close the focus goes
  // back to the button that opened it. Escape closes. The correction also
  // closes after saving (event from the server) and after 90 seconds without a
  // tap: whoever walks off to the ringing phone would leave a half-done
  // correction open.
  var correctionBox = document.getElementById("korrektur");
  var dishBox = document.getElementById("gericht-aus");
  var noticeBox = document.getElementById("vorlauf");
  var waitBox = document.getElementById("wartezeit");
  var sheets = [correctionBox, dishBox, noticeBox, waitBox].filter(Boolean);
  var board = document.querySelector(".board");
  var opener = null;
  var idleTimer = null;
  var IDLE_MS = 90000;
  function sheetOpen() {
    return sheets.some(function (b) { return b.firstElementChild; });
  }
  // A new order or complaint while a sheet covers the columns: a strip above
  // the sheet, a tap closes the sheets and brings the card into view. It reads
  // `sheets`, so every sheet added to the array gets it.
  var alertBtn = null;
  var alertTarget = null;
  function hideAlert() {
    if (alertBtn) alertBtn.remove();
    alertBtn = null;
    alertTarget = null;
    var main = document.querySelector(".tafel main");
    if (main) main.classList.remove("alert-on");
  }
  function notifySheet(text, selector) {
    var main = document.querySelector(".tafel main");
    if (!main || !sheetOpen()) return;
    if (!alertBtn) {
      alertBtn = document.createElement("button");
      alertBtn.type = "button";
      alertBtn.className = "sheet-alert";
      alertBtn.addEventListener("click", function () {
        var target = alertTarget ? document.querySelector(alertTarget) : null;
        // A correction with changes (the foot shows no "nothing changed" state)
        // is dropped: say so.
        var dirty =
          correctionBox &&
          correctionBox.querySelector(".sheet-foot") &&
          !correctionBox.querySelector(".sheet-foot .state-note");
        sheets.forEach(closeSheet);
        if (dirty) showBoardNote(ABANDONED, "warn");
        hideAlert();
        if (target) target.scrollIntoView({ block: "nearest" });
      });
      main.appendChild(alertBtn);
    }
    alertBtn.textContent = text;
    alertTarget = selector;
    main.classList.add("alert-on");
  }
  // One line above the columns, in view whatever the scroll position: it goes
  // with the next tap, or after `ms` when a time is given. A new one replaces
  // the old.
  var boardNote = null;
  var boardNoteTimer = null;
  function dropBoardNote() {
    if (boardNote) boardNote.remove();
    boardNote = null;
    if (boardNoteTimer) window.clearTimeout(boardNoteTimer);
    boardNoteTimer = null;
  }
  function showBoardNote(text, tone, ms) {
    var main = document.querySelector(".tafel main");
    if (!main || !board) return;
    dropBoardNote();
    var note = document.createElement("div");
    note.className = "banner " + tone + " board-note";
    note.setAttribute("role", "status");
    note.textContent = text;
    main.insertBefore(note, board);
    boardNote = note;
    if (ms) {
      boardNoteTimer = window.setTimeout(dropBoardNote, ms);
    } else {
      document.addEventListener("click", function () { if (boardNote === note) dropBoardNote(); }, { capture: true, once: true });
    }
  }
  var ABANDONED = "Korrektur abgebrochen, nichts gespeichert.";
  // The text a cancel tap sends in its HX-Trigger: the refresh that follows
  // would remove a line inside the column.
  document.body.addEventListener("order-notice", function (e) {
    showBoardNote(e.detail.value, "ok", 6000);
  });
  function syncBoard() {
    var open = sheetOpen();
    if (board) board.inert = open;
    if (!open) hideAlert();
  }
  // Empties a sheet and, if it is the correction, stops its idle timer: a timer
  // left running would close the next correction early.
  function dropSheet(box) {
    box.innerHTML = "";
    if (box === dishBox) openCats = {};
    if (box === correctionBox && idleTimer) {
      window.clearTimeout(idleTimer);
      idleTimer = null;
    }
  }
  function closeSheet(box) {
    if (!box || !box.firstElementChild) return;
    dropSheet(box);
    syncBoard();
    if (opener && document.contains(opener)) opener.focus();
    opener = null;
  }
  // A wait that was taken closes its sheet; a refused one stays, with the
  // sentence in the header (T-3.9).
  document.body.addEventListener("htmx:afterRequest", function (e) {
    if (e.detail.xhr.status < 300 && waitBox && waitBox.contains(e.detail.elt) &&
        e.detail.requestConfig.verb === "post") {
      closeSheet(waitBox);
    }
  });
  function closeCorrection() {
    closeSheet(correctionBox);
  }
  function armIdleTimer() {
    if (idleTimer) window.clearTimeout(idleTimer);
    idleTimer = window.setTimeout(function () {
      var open = correctionBox && correctionBox.firstElementChild;
      closeCorrection();
      if (open) showBoardNote(ABANDONED, "warn");
    }, IDLE_MS);
  }
  sheets.forEach(function (box) {
    box.addEventListener("click", function (e) {
      if (e.target.closest("[data-schliessen]")) {
        closeSheet(box);
        return;
      }
      if (box === correctionBox) armIdleTimer();
    });
  });
  if (correctionBox) {
    // Tippen im Nummernfeld zaehlt auch als Taetigkeit, nicht nur Tippen auf Knoepfe.
    correctionBox.addEventListener("input", armIdleTimer);
  }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") sheets.forEach(closeSheet);
  });
  // Only opening takes the focus: after a tap inside the sheet it stays put.
  var wasOpen = false;
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    var t = e.detail.target;
    if (sheets.indexOf(t) >= 0 && !t.firstElementChild) opener = e.detail.elt;
  });
  // A tap inside the sheet redraws it whole: keep the scroll position, or a
  // tap on a line far down would throw the view back to the top. A search
  // (GET) starts at the top on purpose.
  var scrollWas = 0;
  function scroller(t) {
    return t.classList.contains("sheet-body") ? t : t.querySelector(".sheet-body");
  }
  document.body.addEventListener("htmx:beforeSwap", function (e) {
    var t = e.detail.target;
    wasOpen = !!t.firstElementChild;
    var sc = scroller(t);
    var cfg = e.detail.requestConfig || {};
    scrollWas = wasOpen && cfg.verb === "post" && sc ? sc.scrollTop : 0;
  });
  document.body.addEventListener("htmx:afterSwap", function (e) {
    var t = e.detail.target;
    // A dish row was replaced in place: its button is new, keep the focus on it.
    if (t.tagName === "LI" && t.id.indexOf("dish-") === 0) {
      var row = document.getElementById(t.id);
      var btn = row && row.querySelector("button");
      if (btn && document.activeElement === document.body) btn.focus({ preventScroll: true });
      return;
    }
    var inList = t.id === "gericht-liste";
    if (!inList && sheets.indexOf(t) < 0) return;
    if (inList) applyOpenCats(t);
    var sc = scroller(t);
    if (sc) sc.scrollTop = scrollWas;
    // Only if the swap removed the focused element (focus fell back to body): focus elsewhere, e.g. the wait field in the header, stays.
    var box = inList ? t.closest("#gericht-aus") : t;
    if (wasOpen && box && document.activeElement === document.body) {
      var title = box.querySelector(".sheet-head h2");
      if (title) title.focus({ preventScroll: true });
    }
  });
  document.body.addEventListener("htmx:afterSwap", function (e) {
    var box = e.detail.target;
    if (sheets.indexOf(box) < 0 || !box.firstElementChild) return;
    sheets.forEach(function (other) {
      if (other !== box) dropSheet(other);
    });
    syncBoard();
    if (box === correctionBox) armIdleTimer();
    var refused = box.querySelector("#number-error");
    if (refused) {
      // The sticky foot covers the bottom of the list: the sentence must stand above it.
      var body = box.querySelector(".sheet-body");
      var foot = box.querySelector(".sheet-foot");
      var gap = foot ? body.getBoundingClientRect().bottom - foot.getBoundingClientRect().top : 0;
      var over = refused.getBoundingClientRect().bottom - (body.getBoundingClientRect().bottom - gap) + 12;
      if (over > 0) body.scrollTop += over;
    }
    var title = box.querySelector(".sheet-head h2");
    if (!wasOpen && title) title.focus();
  });
  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.detail.target !== waitBox) return;
    var field = waitBox.querySelector("input");
    if (field) field.select();
  });
  // Which categories of "Gericht aus" the team opened, by name, so a search
  // that is cleared brings them back. Reset when the sheet
  // closes. A search shows its hits open and does not count as the team's choice.
  var openCats = {};
  function searching() {
    var s = document.getElementById("gericht-suche");
    return !!(s && s.value.trim());
  }
  // toggle does not bubble: listen in the capture phase.
  document.addEventListener(
    "toggle",
    function (e) {
      var d = e.target;
      if (!d.matches || !d.matches("#gericht-liste details[data-cat]") || searching()) return;
      var cat = d.getAttribute("data-cat");
      // Opened by the team (not restored by a redraw): heading to the top, or
      // the dishes of a group at the bottom would hide below the fold.
      var fresh = d.open && !openCats[cat];
      if (d.open) openCats[cat] = true;
      else delete openCats[cat];
      if (fresh) {
        var sc = d.closest(".sheet-body");
        if (sc) sc.scrollTop += d.getBoundingClientRect().top - sc.getBoundingClientRect().top - 12;
      }
    },
    true
  );
  function applyOpenCats(list) {
    if (searching()) return;
    list.querySelectorAll("details[data-cat]").forEach(function (d) {
      if (openCats[d.getAttribute("data-cat")]) d.open = true;
    });
  }
  document.body.addEventListener("gerichte-geaendert", function () {
    kopfzeile();
  });

  document.body.addEventListener("bestellungen-geaendert", function () {
    closeCorrection();
    reloadOrders();
  });

  var strom = new EventSource("/gui/events");
  // Laedt auch, waehrend jemand korrigiert: der Kasten liegt ausserhalb der
  // Liste und bleibt stehen (Design-Review T-4.7).
  strom.addEventListener("orders", function () {
    offline(false);
    reloadOrders();
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
  // Another tablet switched a dish, or a switch ran out in the morning: the
  // tile shows it. An open list is not redrawn (a dish would slide away under
  // the finger); every button sets a state, so a stale row does no harm, and
  // the list is fresh the next time the sheet opens.
  strom.addEventListener("dishes", function () {
    offline(false);
    kopfzeile();
  });
  // The job wrote a suggestion, or another tablet answered one (#207).
  strom.addEventListener("holidays", function () {
    offline(false);
    feiertage();
  });
  // The price check found a difference, or another tablet answered one (T-4.9).
  strom.addEventListener("prices", function () {
    offline(false);
    reloadPrices();
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
