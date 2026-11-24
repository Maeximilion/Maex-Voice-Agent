// Live-Aktualisierung der Betriebsansicht (docs/06 §1 Regel 5).
// Der Server schickt nur ein Signal, die Seite holt das Fragment - so bleibt die
// Darstellung im Jinja2-Template und es gibt keine zweite Wahrheit im Browser.
//
// Source: api/gui/ts/app.ts. The file of the same name in api/gui/static/ is its
// compiled output (`npm run build`, docs/22); edit the source, never the output.
// Loaded as a module by betrieb/index.html, after htmx.

// Marks the file as a module, so the declaration below can extend `Window`.
export {};

declare global {
  interface Window {
    // htmx is a classic script of its own (htmx.min.js); only what is called here.
    htmx: { ajax(verb: string, path: string, context: { target: string; swap?: string }): unknown };
    // Safari before 14.1 knows the audio context under this name only.
    webkitAudioContext?: typeof AudioContext;
  }
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

// htmx hands its data over in the event's detail, and the server's HX-Trigger
// values arrive the same way. Nothing types that object for us: it is the
// object htmx reads back (shouldSwap), and each field is checked where it is read.
function detail(event: Event): Record<string, unknown> {
  return record(event instanceof CustomEvent ? event.detail : null);
}

(() => {
  const banner = document.getElementById("verbindung");
  const todayList = document.getElementById("heute-liste");

  const outageBar = document.getElementById("stoerung");

  // Ein Tap, der am Server scheitert, liefert trotzdem die Kopfzeile mit der
  // roten Leiste. htmx tauscht bei 4xx/5xx sonst nichts aus, und der Knopf
  // saehe aus, als haette er nichts getan. Getauscht wird aber nur HTML: andere
  // Fehler (HTTPException, unbehandelte Ausnahmen) kommen aus main.py als
  // JSON-Huelle, die an die Stelle der Knoepfe oder der Liste zu setzen waere
  // schlimmer als der Fehler selbst. Dann bleibt die Ansicht stehen und die
  // rote Leiste oben meldet es (docs/06 §5).
  document.body.addEventListener("htmx:beforeSwap", (e) => {
    const d = detail(e);
    const xhr = d.xhr;
    if (!(xhr instanceof XMLHttpRequest)) return;
    if (xhr.status < 400) {
      if (outageBar) outageBar.hidden = true;
      return;
    }
    const typ = xhr.getResponseHeader("Content-Type") || "";
    if (typ.indexOf("text/html") === 0 && xhr.responseText) {
      d.shouldSwap = true;
      d.isError = false;
    } else {
      d.shouldSwap = false;
      if (outageBar) outageBar.hidden = false;
    }
  });
  // Server nicht erreichbar: gelbe Leiste statt stiller Knopf (docs/06 §5).
  document.body.addEventListener("htmx:sendError", () => {
    offline(true);
  });

  // The holiday strip is replaced with its list after every answer; keep it open
  // while the team works through the suggestions, or each tap would fold it.
  let holidaysOpen = false;
  document.addEventListener("toggle", (e) => {
    const t = e.target;
    if (t instanceof HTMLDetailsElement && t.id === "feiertage-fold") holidaysOpen = t.open;
  }, true);
  document.body.addEventListener("htmx:afterSwap", () => {
    const fold = document.getElementById("feiertage-fold");
    if (fold instanceof HTMLDetailsElement && holidaysOpen) fold.open = true;
  });

  // The "Mehr" row of the header (#517) holds the wait steppers and "Gericht aus"
  // below 1180 px. The header is replaced after every tap, so the team's choice
  // is kept here; from 1180 px the row is always open (an older browser hides
  // the content of a closed <details> whatever the stylesheet says).
  let moreOpen = false;
  const wide = window.matchMedia("(min-width: 1180px)");
  function syncMore(): void {
    const more = document.getElementById("mehr");
    if (more instanceof HTMLDetailsElement) more.open = wide.matches || moreOpen;
  }
  // Recorded on the team's own tap or key press on the summary, not on `toggle`:
  // that event also fires, late, for the open that syncMore sets from 1180 px
  // and would pass for a choice of the team (#938). The click runs before the
  // browser flips `open`, so the new state is the opposite of the current one.
  document.addEventListener("click", (e) => {
    const t = e.target;
    const summary = t instanceof Element ? t.closest("summary") : null;
    const more = summary?.parentElement;
    if (more instanceof HTMLDetailsElement && more.id === "mehr") moreOpen = !more.open;
  }, true);
  document.body.addEventListener("htmx:afterSwap", syncMore);
  wide.addEventListener("change", syncMore);
  syncMore();

  if (!todayList || !("EventSource" in window)) return;

  function offline(yes: boolean): void {
    if (banner) banner.hidden = !yes;
  }

  function reloadAll(): void {
    // htmx ersetzt den Inhalt des Ziels; faellt der Aufruf aus, bleibt die
    // zuletzt gezeigte Liste stehen statt zu leeren.
    window.htmx.ajax("GET", "/gui/fragments/heute", { target: "#heute-liste" });
  }

  function reloadHeader(): void {
    window.htmx.ajax("GET", "/gui/fragments/kopfzeile", {
      target: "#kopfzeile",
      swap: "outerHTML",
    });
  }

  function reloadCallbacks(): void {
    window.htmx.ajax("GET", "/gui/fragments/rueckrufe", {
      target: "#rueckrufe-liste",
    });
  }

  function reloadHolidays(): void {
    window.htmx.ajax("GET", "/gui/fragments/feiertage", {
      target: "#feiertage-liste",
    });
  }

  function reloadChecks(): void {
    window.htmx.ajax("GET", "/gui/fragments/pruefen", {
      target: "#pruefen-liste",
    });
  }

  function reloadPrices(): void {
    window.htmx.ajax("GET", "/gui/fragments/preise", {
      target: "#preise-liste",
    });
  }

  // Ton bei neuen Rueckrufen (docs/06 §1 Regel 5), Beschwerden mit eigenem Ton
  // (§3). Erzeugt statt Audiodatei: nichts nachzuladen, laeuft ohne Internet.
  // Browser spielen erst nach dem ersten Tap auf die Seite Ton ab; am Tablet
  // ist das nach dem ersten Handgriff der Fall.
  let audio: AudioContext | null = null;
  const Ctx: typeof AudioContext | undefined = window.AudioContext || window.webkitAudioContext;
  // Safari auf dem Tablet schaltet Web Audio nur innerhalb einer Geste frei:
  // der Kontext entsteht deshalb im Tap, nicht erst beim ersten Rueckruf, und
  // ein stiller Puffer spielt einmal an. Ohne das bliebe das Tablet stumm.
  function unlockSound(): void {
    if (!Ctx) return;
    if (!audio) {
      audio = new Ctx();
      const still = audio.createBufferSource();
      still.buffer = audio.createBuffer(1, 1, 22050);
      still.connect(audio.destination);
      still.start(0);
    }
    if (audio.state === "suspended") audio.resume();
    if (soundButton) soundButton.hidden = true;
  }
  // Until the first tap the browser keeps the tone off (docs/06 §8): say so
  // and let one tap on the button end it (T-3.16).
  const soundButton = document.getElementById("ton-an");
  if (soundButton && Ctx) soundButton.hidden = false;
  document.addEventListener("touchstart", unlockSound, { passive: true });
  document.addEventListener("click", unlockSound);

  function playTone(notes: number[]): void {
    // Noch kein Tap auf die Seite: kein Kontext, kein Ton (Browserregel).
    const ctx = audio;
    if (!ctx) return;
    const t = ctx.currentTime;
    notes.forEach((hz, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = hz;
      gain.gain.setValueAtTime(0.3, t + i * 0.25);
      gain.gain.exponentialRampToValueAtTime(0.001, t + i * 0.25 + 0.22);
      osc.connect(gain).connect(ctx.destination);
      osc.start(t + i * 0.25);
      osc.stop(t + i * 0.25 + 0.23);
    });
  }
  // The cards of a list by their id, with their `attr`. A plain object on
  // purpose: its keys come back in the order the old script announced new
  // cards in.
  function cardAttrs(listId: string, attr = "data-reason"): Record<string, string | null> {
    const ids: Record<string, string | null> = {};
    document.querySelectorAll("#" + listId + " [data-id]").forEach((el) => {
      const id = el.getAttribute("data-id");
      if (id !== null) ids[id] = el.getAttribute(attr);
    });
    return ids;
  }
  // Orders that wait for "Passt" longer than the restaurant allows (data-overdue).
  function overdueOrders(): Record<string, string | null> {
    const ids: Record<string, string | null> = {};
    document
      .querySelectorAll("#bestellungen-liste [data-overdue]")
      .forEach((el) => {
        const id = el.getAttribute("data-id");
        if (id !== null) ids[id] = "overdue";
      });
    return ids;
  }
  // Was beim Laden schon da war, klingelt nicht.
  let knownReasons = cardAttrs("rueckrufe-liste");
  // The text of each callback card, as a version: an open order that joins a
  // card changes it (#993), and the team must notice that as a new card (#998).
  let knownVersions = cardAttrs("rueckrufe-liste", "data-version");
  let knownOrders = cardAttrs("bestellungen-liste");
  let knownOverdue = overdueOrders();
  function newIds(before: Record<string, string | null>, nowCards: Record<string, string | null>): string[] {
    return Object.keys(nowCards).filter((id) => !(id in before));
  }
  document.body.addEventListener("htmx:afterSwap", (e) => {
    const target = detail(e).target;
    const targetId = target instanceof Element ? target.id : null;
    if (targetId === "bestellungen-liste") {
      const currentOrders = cardAttrs("bestellungen-liste");
      const newOrders = newIds(knownOrders, currentOrders);
      knownOrders = currentOrders;
      // A card that turned red since the last look: nobody has tapped "Passt",
      // so nobody is looking. The alarm of a complaint, once per card.
      const currentOverdue = overdueOrders();
      const turnedRed = newIds(knownOverdue, currentOverdue);
      knownOverdue = currentOverdue;
      if (turnedRed.length) {
        playTone([660, 440, 660, 440]);
        const red = document.querySelector(
          '#bestellungen-liste [data-id="' + turnedRed[0] + '"]'
        );
        if (red) red.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
      if (!newOrders.length) return;
      // A sheet covers the columns: say so on the sheet, a tap shows the card.
      notifySheet(
        "Neue Bestellung · ansehen",
        '#bestellungen-liste [data-id="' + newOrders[0] + '"]'
      );
      // Eigener Ton fuer Bestellungen, dazu kurzes Blinken (docs/06 §3).
      // Die erste neue Karte rollt ins Bild: steht sie unten, saehe sonst
      // niemand das Blinken. A card that turned red in the same redraw keeps
      // the sound and the view: the new cards still blink.
      if (!turnedRed.length) playTone([523, 659, 784]);
      newOrders.forEach((id, i) => {
        const el = document.querySelector('#bestellungen-liste [data-id="' + id + '"]');
        if (!el) return;
        el.classList.add("neu");
        if (i === 0 && !turnedRed.length) {
          el.scrollIntoView({ block: "nearest", behavior: "smooth" });
        }
      });
      return;
    }
    if (targetId !== "rueckrufe-liste") return;
    const nowCards = cardAttrs("rueckrufe-liste");
    const versions = cardAttrs("rueckrufe-liste", "data-version");
    const added = newIds(knownReasons, nowCards);
    // A card already seen whose text changed. An emptied one (the deletion
    // job, an erasure) has no version and nothing new to read.
    const changed = Object.keys(versions).filter(
      (id) => id in knownVersions && knownVersions[id] !== versions[id] && versions[id] !== ""
    );
    // A card that became a complaint (one joined it, #998) alarms like a new one.
    const complaints = added
      .concat(changed)
      .filter((id) => nowCards[id] === "complaint" && knownReasons[id] !== "complaint");
    knownReasons = nowCards;
    knownVersions = versions;
    // A changed card blinks like a new order: nobody has read its new text.
    changed.forEach((id) => {
      const el = document.querySelector('#rueckrufe-liste [data-id="' + id + '"]');
      if (el) el.classList.add("neu");
    });
    if (!added.length && !changed.length) return;
    const isComplaint = complaints.length > 0;
    if (isComplaint) {
      notifySheet(
        "Neue Beschwerde · ansehen",
        '#rueckrufe-liste [data-id="' + complaints[0] + '"]'
      );
    } else if (changed.length) {
      notifySheet(
        "Rückruf ergänzt · ansehen",
        '#rueckrufe-liste [data-id="' + changed[0] + '"]'
      );
    }
    playTone(isComplaint ? [660, 440, 660, 440] : [880, 1175]);
  });

  function reloadOrders(): void {
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
  const correctionBox = document.getElementById("korrektur");
  const dishBox = document.getElementById("gericht-aus");
  const noticeBox = document.getElementById("vorlauf");
  const waitBox = document.getElementById("wartezeit");
  const entryBox = document.getElementById("reservierung");
  const historyBox = document.getElementById("rueckruf-verlauf");
  const settingsBox = document.getElementById("einstellungen");
  const sheets = [correctionBox, dishBox, noticeBox, waitBox, entryBox, historyBox, settingsBox].filter((b): b is HTMLElement => b !== null);
  const board = document.querySelector<HTMLElement>(".board");
  let opener: HTMLElement | null = null;
  let idleTimer: number | null = null;
  const IDLE_MS = 90000;
  function sheetOpen(): boolean {
    return sheets.some((b) => b.firstElementChild);
  }
  // A new order or complaint while a sheet covers the columns: a strip above
  // the sheet, a tap closes the sheets and brings the card into view. It reads
  // `sheets`, so every sheet added to the array gets it.
  let alertBtn: HTMLButtonElement | null = null;
  let alertTarget: string | null = null;
  function hideAlert(): void {
    if (alertBtn) alertBtn.remove();
    alertBtn = null;
    alertTarget = null;
    const main = document.querySelector(".tafel main");
    if (main) main.classList.remove("alert-on");
  }
  function notifySheet(text: string, selector: string): void {
    const main = document.querySelector(".tafel main");
    if (!main || !sheetOpen()) return;
    if (!alertBtn) {
      alertBtn = document.createElement("button");
      alertBtn.type = "button";
      alertBtn.className = "sheet-alert";
      alertBtn.addEventListener("click", () => {
        const target = alertTarget ? document.querySelector(alertTarget) : null;
        // A correction with changes (the foot shows no "nothing changed" state)
        // is dropped: say so.
        const dirty =
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
  let boardNote: HTMLElement | null = null;
  let boardNoteTimer: number | null = null;
  function dropBoardNote(): void {
    if (boardNote) boardNote.remove();
    boardNote = null;
    if (boardNoteTimer) window.clearTimeout(boardNoteTimer);
    boardNoteTimer = null;
  }
  function showBoardNote(text: string, tone: string, ms?: number): void {
    const main = document.querySelector(".tafel main");
    if (!main || !board) return;
    dropBoardNote();
    const note = document.createElement("div");
    note.className = "banner " + tone + " board-note";
    note.setAttribute("role", "status");
    note.textContent = text;
    main.insertBefore(note, board);
    boardNote = note;
    if (ms) {
      boardNoteTimer = window.setTimeout(dropBoardNote, ms);
    } else {
      document.addEventListener("click", () => { if (boardNote === note) dropBoardNote(); }, { capture: true, once: true });
    }
  }
  const ABANDONED = "Korrektur abgebrochen, nichts gespeichert.";
  const RELOADED = "Die Liste hat sich geändert, nichts passiert. Bitte nochmal tippen.";
  // The text a cancel tap sends in its HX-Trigger: the refresh that follows
  // would remove a line inside the column. A value that is no text is not shown.
  document.body.addEventListener("order-notice", (e) => {
    const value = detail(e).value;
    if (typeof value === "string") showBoardNote(value, "ok", 6000);
  });
  // An "Erledigt" on a card whose text changed since it was drawn closed
  // nothing (#998): the board says why, until the next tap.
  document.body.addEventListener("callback-notice", (e) => {
    const value = detail(e).value;
    if (typeof value === "string") showBoardNote(value, "warn");
  });
  // A table the team entered (T-3.12): the sheet closes, the board says so; the
  // column "Heute" follows on its own through the event stream.
  document.body.addEventListener("reservierung-eingetragen", (e) => {
    closeSheet(entryBox);
    const value = detail(e).value;
    if (typeof value === "string") showBoardNote(value, "ok", 6000);
  });
  function syncBoard(): void {
    const open = sheetOpen();
    if (board) board.inert = open;
    if (!open) hideAlert();
  }
  // Empties a sheet and, if it is the correction, stops its idle timer: a timer
  // left running would close the next correction early.
  function dropSheet(box: HTMLElement): void {
    box.innerHTML = "";
    if (box === dishBox) openCats = {};
    if (box === correctionBox && idleTimer) {
      window.clearTimeout(idleTimer);
      idleTimer = null;
    }
  }
  function closeSheet(box: HTMLElement | null): void {
    if (!box || !box.firstElementChild) return;
    dropSheet(box);
    syncBoard();
    if (opener && document.contains(opener)) opener.focus();
    opener = null;
  }
  // A wait that was taken closes its sheet; a refused one stays, with the
  // sentence in the header (T-3.9).
  document.body.addEventListener("htmx:afterRequest", (e) => {
    const d = detail(e);
    const xhr = d.xhr;
    const elt = d.elt;
    if (xhr instanceof XMLHttpRequest && xhr.status < 300 && waitBox && elt instanceof Node &&
        waitBox.contains(elt) && record(d.requestConfig).verb === "post") {
      closeSheet(waitBox);
    }
  });
  // In-page question instead of the browser's confirm() (T-3.16, docs/06 §1
  // rule 6): the dialog is small and its buttons are 60 px targets. htmx asks
  // through htmx:confirm; the request goes out only on "Ja".
  function closeQuestion(): boolean {
    const open = document.querySelector(".frage-grund");
    if (open) open.remove();
    // Only what the question made inert: a sheet keeps its own inert board.
    madeInert.forEach((el) => {
      el.inert = false;
    });
    madeInert = [];
    return !!open;
  }
  let madeInert: HTMLElement[] = [];
  document.body.addEventListener("htmx:confirm", (e) => {
    const d = detail(e);
    const question = d.question;
    if (typeof question !== "string" || !question) return;
    e.preventDefault();
    const issue = d.issueRequest;
    const elt = d.elt;
    // Without the request to release, a "Ja" could do nothing: the red bar says
    // so, and nothing is sent.
    if (typeof issue !== "function" || !(elt instanceof Element)) {
      if (outageBar) outageBar.hidden = false;
      return;
    }
    const back = document.createElement("div");
    back.className = "frage-grund";
    const box = document.createElement("div");
    box.className = "frage";
    box.setAttribute("role", "alertdialog");
    box.setAttribute("aria-modal", "true");
    const text = document.createElement("p");
    text.textContent = question;
    const no = document.createElement("button");
    no.type = "button";
    no.textContent = "Nein";
    const yes = document.createElement("button");
    yes.type = "button";
    yes.className = "primary";
    yes.textContent = "Ja";
    // The board stays reachable by keyboard behind an overlay: make everything
    // else inert while the question stands, and keep Tab between its buttons.
    Array.from(document.body.children).forEach((el) => {
      if (!(el instanceof HTMLElement) || el.tagName === "SCRIPT" || el.inert) return;
      el.inert = true;
      madeInert.push(el);
    });
    box.addEventListener("keydown", (k) => {
      if (k.key !== "Tab") return;
      k.preventDefault();
      (document.activeElement === no ? yes : no).focus();
    });
    no.addEventListener("click", closeQuestion);
    yes.addEventListener("click", () => {
      closeQuestion();
      // A live update may have replaced the card while the question stood
      // (confirm() used to freeze the page): htmx drops a request from a
      // detached button, so say so instead of looking as if it had worked.
      if (!elt.isConnected) {
        showBoardNote(RELOADED, "warn");
        return;
      }
      issue(true);
    });
    box.append(text, no, yes);
    back.append(box);
    document.body.append(back);
    no.focus();
  });
  function closeCorrection(): void {
    closeSheet(correctionBox);
  }
  function armIdleTimer(): void {
    if (idleTimer) window.clearTimeout(idleTimer);
    idleTimer = window.setTimeout(() => {
      const open = correctionBox && correctionBox.firstElementChild;
      closeCorrection();
      if (open) showBoardNote(ABANDONED, "warn");
    }, IDLE_MS);
  }
  sheets.forEach((box) => {
    box.addEventListener("click", (e) => {
      if (e.target instanceof Element && e.target.closest("[data-schliessen]")) {
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
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!closeQuestion()) sheets.forEach(closeSheet);
  });
  // Only opening takes the focus: after a tap inside the sheet it stays put.
  let wasOpen = false;
  document.body.addEventListener("htmx:beforeRequest", (e) => {
    const d = detail(e);
    const t = d.target;
    const elt = d.elt;
    if (t instanceof HTMLElement && sheets.indexOf(t) >= 0 && !t.firstElementChild) {
      opener = elt instanceof HTMLElement ? elt : null;
    }
  });
  // A tap inside the sheet redraws it whole: keep the scroll position, or a
  // tap on a line far down would throw the view back to the top. A search
  // (GET) starts at the top on purpose.
  let scrollWas = 0;
  function scroller(t: Element): Element | null {
    return t.classList.contains("sheet-body") ? t : t.querySelector(".sheet-body");
  }
  document.body.addEventListener("htmx:beforeSwap", (e) => {
    const d = detail(e);
    const t = d.target;
    if (!(t instanceof Element)) return;
    wasOpen = !!t.firstElementChild;
    const sc = scroller(t);
    scrollWas = wasOpen && record(d.requestConfig).verb === "post" && sc ? sc.scrollTop : 0;
  });
  document.body.addEventListener("htmx:afterSwap", (e) => {
    const t = detail(e).target;
    if (!(t instanceof HTMLElement)) return;
    // A dish row was replaced in place: its button is new, keep the focus on it.
    if (t.tagName === "LI" && t.id.indexOf("dish-") === 0) {
      const row = document.getElementById(t.id);
      const btn = row && row.querySelector("button");
      if (btn && document.activeElement === document.body) btn.focus({ preventScroll: true });
      return;
    }
    const inList = t.id === "gericht-liste";
    if (!inList && sheets.indexOf(t) < 0) return;
    if (inList) applyOpenCats(t);
    const sc = scroller(t);
    if (sc) sc.scrollTop = scrollWas;
    // Only if the swap removed the focused element (focus fell back to body): focus elsewhere, e.g. the wait field in the header, stays.
    const box = inList ? t.closest("#gericht-aus") : t;
    if (wasOpen && box && document.activeElement === document.body) {
      const title = box.querySelector<HTMLElement>(".sheet-head h2");
      if (title) title.focus({ preventScroll: true });
    }
  });
  document.body.addEventListener("htmx:afterSwap", (e) => {
    const box = detail(e).target;
    if (!(box instanceof HTMLElement) || sheets.indexOf(box) < 0 || !box.firstElementChild) return;
    sheets.forEach((other) => {
      if (other !== box) dropSheet(other);
    });
    syncBoard();
    if (box === correctionBox) armIdleTimer();
    const refused = box.querySelector("#number-error");
    const body = box.querySelector(".sheet-body");
    if (refused && body) {
      // The sticky foot covers the bottom of the list: the sentence must stand above it.
      const foot = box.querySelector(".sheet-foot");
      const gap = foot ? body.getBoundingClientRect().bottom - foot.getBoundingClientRect().top : 0;
      const over = refused.getBoundingClientRect().bottom - (body.getBoundingClientRect().bottom - gap) + 12;
      if (over > 0) body.scrollTop += over;
    }
    const title = box.querySelector<HTMLElement>(".sheet-head h2");
    if (!wasOpen && title) title.focus();
  });
  document.body.addEventListener("htmx:afterSwap", (e) => {
    if (!waitBox || detail(e).target !== waitBox) return;
    const field = waitBox.querySelector("input");
    if (field) field.select();
  });
  // Which categories of "Gericht aus" the team opened, by name, so a search
  // that is cleared brings them back. Reset when the sheet
  // closes. A search shows its hits open and does not count as the team's choice.
  let openCats: Record<string, true> = {};
  function searching(): boolean {
    const s = document.getElementById("gericht-suche");
    return s instanceof HTMLInputElement && !!s.value.trim();
  }
  // toggle does not bubble: listen in the capture phase.
  document.addEventListener(
    "toggle",
    (e) => {
      const d = e.target;
      if (!(d instanceof HTMLDetailsElement) || !d.matches("#gericht-liste details[data-cat]") || searching()) return;
      const cat = d.getAttribute("data-cat");
      if (cat === null) return;
      // Opened by the team (not restored by a redraw): heading to the top, or
      // the dishes of a group at the bottom would hide below the fold.
      const fresh = d.open && !openCats[cat];
      if (d.open) openCats[cat] = true;
      else delete openCats[cat];
      if (fresh) {
        const sc = d.closest(".sheet-body");
        if (sc) sc.scrollTop += d.getBoundingClientRect().top - sc.getBoundingClientRect().top - 12;
      }
    },
    true
  );
  function applyOpenCats(list: Element): void {
    if (searching()) return;
    list.querySelectorAll<HTMLDetailsElement>("details[data-cat]").forEach((d) => {
      const cat = d.getAttribute("data-cat");
      if (cat !== null && openCats[cat]) d.open = true;
    });
  }
  document.body.addEventListener("gerichte-geaendert", () => {
    reloadHeader();
  });

  document.body.addEventListener("bestellungen-geaendert", () => {
    closeCorrection();
    reloadOrders();
  });

  const stream = new EventSource("/gui/events");
  // Laedt auch, waehrend jemand korrigiert: der Kasten liegt ausserhalb der
  // Liste und bleibt stehen (Design-Review T-4.7).
  stream.addEventListener("orders", () => {
    offline(false);
    reloadOrders();
  });
  stream.addEventListener("callbacks", () => {
    offline(false);
    reloadCallbacks();
  });
  // Umgeschaltet an einem anderen Tablet oder direkt in der Datenbank: jedes
  // Geraet zeigt denselben Stand.
  stream.addEventListener("header", () => {
    offline(false);
    reloadHeader();
  });
  // Another tablet switched a dish, or a switch ran out in the morning: the
  // tile shows it. An open list is not redrawn (a dish would slide away under
  // the finger); every button sets a state, so a stale row does no harm, and
  // the list is fresh the next time the sheet opens.
  stream.addEventListener("dishes", () => {
    offline(false);
    reloadHeader();
  });
  // The job wrote a suggestion, or another tablet answered one (#207).
  stream.addEventListener("holidays", () => {
    offline(false);
    reloadHolidays();
  });
  // The price check found a difference, or another tablet answered one (T-4.9).
  stream.addEventListener("prices", () => {
    offline(false);
    reloadPrices();
  });
  // The staff changed a team number or a date's hours, or the owner decided
  // a card on another tablet (#570 b).
  stream.addEventListener("checks", () => {
    offline(false);
    reloadChecks();
  });
  stream.addEventListener("today", () => {
    offline(false);
    reloadAll();
  });
  stream.addEventListener("problem", () => {
    offline(true);
  });
  stream.onopen = () => {
    offline(false);
  };
  // The browser reconnects by itself after a drop (retry in the stream).
  // Not after an answer that is no stream: a 401 once the device is signed out
  // (#570) or an error page of the proxy. Then a small request asks every 15
  // seconds: signed out goes to the sign-in, a server that answers again
  // reloads the page and its stream, anything else asks again.
  function probe(): void {
    fetch("/gui/fragments/heute", { headers: { "HX-Request": "true" }, credentials: "same-origin" }).then((res) => {
      const away = res.status === 401 && res.headers.get("HX-Redirect");
      if (away) location.href = away;
      else if (res.ok) location.reload();
      else setTimeout(probe, 15000);
    }, () => { setTimeout(probe, 15000); });
  }
  stream.onerror = () => {
    offline(true);
    if (stream.readyState === EventSource.CLOSED) setTimeout(probe, 15000);
  };
})();
