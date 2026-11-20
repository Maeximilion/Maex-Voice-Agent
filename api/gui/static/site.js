// The public site (#892): the example call of the hero card and the pause of the
// animations that are off screen, the glow that follows the pointer and the
// swing of the sound waves. A file, not inline code: the site's
// Content-Security-Policy allows scripts from this origin only.
//
// Written for a smooth scroll: the card changes the text of its own text nodes
// and a class on the waves when the speaker changes, it types only while it is on
// screen, and glow and waves move through transforms only (custom properties),
// never layout, and stop off screen.
//
// Source: api/gui/ts/site.ts. The file of the same name in api/gui/static/ is its
// compiled output (`npm run build`, docs/22); edit the source, never the output.
import { callState, ease, voice, total, REST } from "./site-logic.js";
const TICK_MS = 42;
const STATUS = {
    listening: "hört zu",
    speaking: "spricht",
    confirmed: "✓ bestätigt",
};
const still = window.matchMedia("(prefers-reduced-motion: reduce)");
function sentences(call) {
    const found = [];
    call.querySelectorAll("li").forEach((item) => {
        const said = item.querySelector(".typed")?.firstChild;
        const holder = item.querySelector(".rest");
        if (said instanceof Text && holder) {
            const rest = holder.appendChild(document.createTextNode(""));
            found.push({ item, said, rest, full: said.data });
        }
    });
    return found;
}
function initCard(card) {
    const chip = card.querySelector("[data-chip]");
    const choices = card.querySelector("[data-choices]");
    const waves = document.querySelector("[data-waves]");
    const calls = new Map();
    card.querySelectorAll("[data-call]").forEach((call) => {
        if (call.dataset.call)
            calls.set(call.dataset.call, call);
    });
    const first = card.querySelector("[data-call].is-shown") ?? calls.values().next().value;
    if (!chip || !choices || !first)
        return;
    let current = first;
    let lines = sentences(current);
    let lengths = lines.map((line) => line.full.length);
    let n = 0;
    let timer = 0;
    let onScreen = true;
    let lastShown = [];
    let lastStatus = null;
    let lastLoud = null;
    function paint() {
        const state = callState(lengths, n);
        state.shown.forEach((count, i) => {
            const line = lines[i];
            if (!line || lastShown[i] === count)
                return;
            lastShown[i] = count;
            line.said.data = line.full.slice(0, count);
            line.rest.data = line.full.slice(count);
            line.item.classList.toggle("is-pending", count === 0);
        });
        // The caret stands at the sentence being typed only.
        lines.forEach((line, i) => line.item.classList.toggle("is-active", i === state.active));
        if (state.status !== lastStatus) {
            lastStatus = state.status;
            chip.textContent = STATUS[state.status];
            chip.classList.toggle("is-ok", state.status === "confirmed");
        }
        if (waves && state.loud !== lastLoud) {
            lastLoud = state.loud;
            waves.classList.toggle("is-loud", state.loud);
        }
    }
    function tick() {
        timer = 0;
        if (!onScreen || document.hidden)
            return;
        n = n > total(lengths) + REST ? 0 : n + 1;
        paint();
        timer = window.setTimeout(tick, TICK_MS);
    }
    function resume() {
        if (!timer && onScreen && !document.hidden && !still.matches)
            timer = window.setTimeout(tick, TICK_MS);
    }
    function play() {
        window.clearTimeout(timer);
        timer = 0;
        lastShown = [];
        lastStatus = null;
        // A reader who wants no motion gets the whole call at once.
        n = still.matches ? total(lengths) : 0;
        paint();
        resume();
    }
    function pick(id) {
        const next = calls.get(id);
        if (!next)
            return;
        // The call that was playing stands complete again, with no caret and no
        // pending line left behind (a caret of a hidden call would still show).
        lines.forEach((line) => {
            line.said.data = line.full;
            line.rest.data = "";
            line.item.classList.remove("is-active", "is-pending");
        });
        current.classList.remove("is-shown");
        next.classList.add("is-shown");
        current = next;
        lines = sentences(current);
        lengths = lines.map((line) => line.full.length);
        choices.querySelectorAll("[data-pick]").forEach((button) => {
            button.setAttribute("aria-pressed", String(button.dataset.pick === id));
        });
        card.querySelectorAll("[data-tag]").forEach((tag) => {
            const text = tag.dataset.tag === "a" ? next.dataset.tagA : next.dataset.tagB;
            if (text !== undefined)
                tag.textContent = text;
        });
        play();
    }
    // The choices need this script: without it the first call stands complete.
    choices.hidden = false;
    choices.addEventListener("click", (event) => {
        const button = event.target.closest("[data-pick]");
        if (button?.dataset.pick)
            pick(button.dataset.pick);
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
function pauseOffScreen() {
    const observer = new IntersectionObserver((entries) => {
        entries.forEach((entry) => entry.target.classList.toggle("off-screen", !entry.isIntersecting));
    });
    document.querySelectorAll("[data-live]").forEach((element) => observer.observe(element));
}
// The three waves stand in place and swing up and down like a voice: one loop sets
// the height of each layer (a transform, so the compositor does the drawing) while
// someone speaks, and it stops while the waves are off screen or the tab is hidden.
function initWaves(waves) {
    const layers = Array.from(waves.querySelectorAll(".wave"));
    const phases = [0, 0.23, 0.51];
    let level = 0;
    let frame = 0;
    let onScreen = true;
    let started = 0;
    let drawn = 0;
    function rest() {
        window.cancelAnimationFrame(frame);
        frame = 0;
        layers.forEach((layer) => layer.style.removeProperty("transform"));
    }
    function draw(now) {
        frame = 0;
        if (still.matches)
            return rest();
        if (!onScreen || document.hidden)
            return;
        frame = window.requestAnimationFrame(draw);
        // Thirty pictures a second are plenty for a swing and halve the work.
        if (now - drawn < 30)
            return;
        drawn = now;
        started || (started = now);
        level = ease(level, waves.classList.contains("is-loud") ? 1 : 0, 0.12);
        layers.forEach((layer, i) => {
            layer.style.transform = `scaleY(${(level * voice((now - started) / 1000, phases[i] ?? 0)).toFixed(3)})`;
        });
    }
    function resume() {
        if (!frame && onScreen && !document.hidden && !still.matches)
            frame = window.requestAnimationFrame(draw);
    }
    new IntersectionObserver((entries) => {
        onScreen = entries.some((entry) => entry.isIntersecting);
        resume();
    }).observe(waves);
    document.addEventListener("visibilitychange", resume);
    // A reader who asks for no motion while the page is open gets the quiet curve of
    // the stylesheet at once: the loop ends and the inline height goes.
    still.addEventListener("change", () => {
        if (still.matches)
            rest();
        else
            resume();
    });
    resume();
}
// The purple glow of the hero follows a fine pointer, eased frame by frame (a
// transform through two custom properties, no layout). Touch and a reader who
// wants no motion keep the glow at its resting place in the top left corner.
function initGlow(hero) {
    if (!window.matchMedia("(hover:hover) and (pointer:fine)").matches)
        return;
    let x = 0;
    let y = 0;
    let toX = 0;
    let toY = 0;
    let frame = 0;
    let seen = false;
    function move() {
        frame = 0;
        x = ease(x, toX, 0.12);
        y = ease(y, toY, 0.12);
        hero.style.setProperty("--gx", `${x.toFixed(1)}px`);
        hero.style.setProperty("--gy", `${y.toFixed(1)}px`);
        if (Math.abs(toX - x) + Math.abs(toY - y) > 0.5)
            frame = window.requestAnimationFrame(move);
    }
    function rest() {
        window.cancelAnimationFrame(frame);
        frame = 0;
        seen = false;
        hero.style.removeProperty("--gx");
        hero.style.removeProperty("--gy");
    }
    still.addEventListener("change", () => {
        if (still.matches)
            rest();
    });
    hero.addEventListener("pointermove", (event) => {
        if (still.matches)
            return rest();
        const box = hero.getBoundingClientRect();
        toX = event.clientX - box.left;
        toY = event.clientY - box.top;
        // The first move starts where the glow already stands, never a jump from 0.
        if (!seen) {
            seen = true;
            x = 6 * 16;
            y = 6 * 16;
        }
        if (!frame)
            frame = window.requestAnimationFrame(move);
    });
}
const card = document.querySelector("[data-callcard]");
const waves = document.querySelector("[data-waves]");
const hero = document.querySelector(".hero");
if (waves)
    initWaves(waves);
if (hero)
    initGlow(hero);
if (card)
    initCard(card);
pauseOffScreen();
