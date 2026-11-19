// The public site (#892): the example call of the hero card and the pause of the
// animations that are off screen. A file, not inline code: the site's
// Content-Security-Policy allows scripts from this origin only.
//
// Written for a smooth scroll: the card changes the text of its own text nodes
// and a class on the waves when the speaker changes, nothing else, and it types
// only while it is on screen. Nothing follows the pointer, and nothing redraws
// the page on a timer.
//
// Source: api/gui/ts/site.ts. The file of the same name in api/gui/static/ is its
// compiled output (`npm run build`, docs/22); edit the source, never the output.

import { callState, total, REST } from "./site-logic.js";
import type { Status } from "./site-logic.js";

const TICK_MS = 42;
const STATUS: Record<Status, string> = {
  listening: "hört zu",
  speaking: "spricht",
  confirmed: "✓ bestätigt",
};

const still = window.matchMedia("(prefers-reduced-motion: reduce)");

// A sentence is two text nodes: what has been said, and the rest, which is laid
// out but invisible. The card therefore keeps the height of the finished call
// from the first character on, and typing moves nothing around it.
interface Sentence {
  item: HTMLElement;
  said: Text;
  rest: Text;
  full: string;
}

function sentences(call: HTMLElement): Sentence[] {
  const found: Sentence[] = [];
  call.querySelectorAll<HTMLElement>("li").forEach((item) => {
    const said = item.querySelector(".typed")?.firstChild;
    const holder = item.querySelector(".rest");
    if (said instanceof Text && holder) {
      const rest = holder.appendChild(document.createTextNode(""));
      found.push({ item, said, rest, full: said.data });
    }
  });
  return found;
}

function initCard(card: HTMLElement): void {
  const chip = card.querySelector<HTMLElement>("[data-chip]");
  const choices = card.querySelector<HTMLElement>("[data-choices]");
  const waves = document.querySelector<HTMLElement>("[data-waves]");
  const calls = new Map<string, HTMLElement>();
  card.querySelectorAll<HTMLElement>("[data-call]").forEach((call) => {
    if (call.dataset.call) calls.set(call.dataset.call, call);
  });
  const first = card.querySelector<HTMLElement>("[data-call].is-shown") ?? calls.values().next().value;
  if (!chip || !choices || !first) return;

  let current: HTMLElement = first;
  let lines = sentences(current);
  let lengths = lines.map((line) => line.full.length);
  let n = 0;
  let timer = 0;
  let onScreen = true;
  let lastShown: number[] = [];
  let lastStatus: Status | null = null;
  let lastLoud: boolean | null = null;

  function paint(): void {
    const state = callState(lengths, n);
    state.shown.forEach((count, i) => {
      const line = lines[i];
      if (!line || lastShown[i] === count) return;
      lastShown[i] = count;
      line.said.data = line.full.slice(0, count);
      line.rest.data = line.full.slice(count);
      line.item.classList.toggle("is-pending", count === 0);
    });
    // The caret stands at the sentence being typed only.
    lines.forEach((line, i) => line.item.classList.toggle("is-active", i === state.active));
    if (state.status !== lastStatus) {
      lastStatus = state.status;
      chip!.textContent = STATUS[state.status];
      chip!.classList.toggle("is-ok", state.status === "confirmed");
    }
    if (waves && state.loud !== lastLoud) {
      lastLoud = state.loud;
      waves.classList.toggle("is-loud", state.loud);
    }
  }

  function tick(): void {
    timer = 0;
    if (!onScreen || document.hidden) return;
    n = n > total(lengths) + REST ? 0 : n + 1;
    paint();
    timer = window.setTimeout(tick, TICK_MS);
  }

  function resume(): void {
    if (!timer && onScreen && !document.hidden && !still.matches)
      timer = window.setTimeout(tick, TICK_MS);
  }

  function play(): void {
    window.clearTimeout(timer);
    timer = 0;
    lastShown = [];
    lastStatus = null;
    // A reader who wants no motion gets the whole call at once.
    n = still.matches ? total(lengths) : 0;
    paint();
    resume();
  }

  function pick(id: string): void {
    const next = calls.get(id);
    if (!next) return;
    // The call that was playing stands complete again.
    lines.forEach((line) => {
      line.said.data = line.full;
      line.rest.data = "";
    });
    current.classList.remove("is-shown");
    next.classList.add("is-shown");
    current = next;
    lines = sentences(current);
    lengths = lines.map((line) => line.full.length);
    choices!.querySelectorAll<HTMLButtonElement>("[data-pick]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.pick === id));
    });
    card.querySelectorAll<HTMLElement>("[data-tag]").forEach((tag) => {
      const text = tag.dataset.tag === "a" ? next.dataset.tagA : next.dataset.tagB;
      if (text !== undefined) tag.textContent = text;
    });
    play();
  }

  // The choices need this script: without it the first call stands complete.
  choices.hidden = false;
  choices.addEventListener("click", (event) => {
    const button = (event.target as Element).closest<HTMLButtonElement>("[data-pick]");
    if (button?.dataset.pick) pick(button.dataset.pick);
  });

  // Type only while the card is on screen and the tab is in front.
  new IntersectionObserver((entries) => {
    onScreen = entries.some((entry) => entry.isIntersecting);
    resume();
  }).observe(card);
  document.addEventListener("visibilitychange", resume);

  play();
}

// Looping demos and the waves stand still while they are off screen. Without this
// script they simply keep running.
function pauseOffScreen(): void {
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => entry.target.classList.toggle("off-screen", !entry.isIntersecting));
  });
  document.querySelectorAll("[data-live]").forEach((element) => observer.observe(element));
}

const card = document.querySelector<HTMLElement>("[data-callcard]");
if (card) initCard(card);
pauseOffScreen();
