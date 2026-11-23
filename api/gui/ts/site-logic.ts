// The testable part of the public site (#892): how far an example call has been
// typed, and the checks of the inquiry form. No DOM in here, so node runs it
// (api/tests/site_logic.mjs).
//
// Source: api/gui/ts/site-logic.ts. The file of the same name in api/gui/static/
// is its compiled output (`npm run build`, docs/22); edit the source.

// Characters of pause between two sentences of a call, counted like typed ones.
export const PAUSE = 14;
// Middle line of the wave drawing (viewBox 0 0 1440 240, api/site/waves.py MID).
const MID = 120;
// A swing never reaches 0: a matrix without an inverse draws nothing.
const MIN_SWING = 0.02;
// A call is confirmed once the guest's yes (the third sentence) is complete.
const YES = 2;

export type Status = "listening" | "speaking" | "confirmed";

export interface CallState {
  // Characters of each sentence shown at step `n`.
  shown: number[];
  // The sentence being typed, or -1 in a pause.
  active: number;
  status: Status;
  // Someone is speaking: the sound waves swing.
  loud: boolean;
}

// Total steps of a call: every sentence and its pause.
export function total(lengths: number[]): number {
  return lengths.reduce((sum, length) => sum + length + PAUSE, 0);
}

// A call has played to its end once every sentence and pause is typed.
export function finished(lengths: number[], n: number): boolean {
  return n >= total(lengths);
}

// CSS matrix that squeezes a wave path to `swing` (0..1) of its height around the
// middle line, in SVG units: the stroke (non-scaling) keeps its width on screen.
export function swingMatrix(swing: number): string {
  const s = Math.min(1, Math.max(MIN_SWING, swing));
  return `matrix(1,0,0,${s.toFixed(3)},0,${(MID * (1 - s)).toFixed(2)})`;
}

// The state of a call of sentences with these lengths after `n` typed characters.
// Sentences alternate guest, agent, guest, agent.
export function callState(lengths: number[], n: number): CallState {
  let start = 0;
  let active = -1;
  let said = 0;
  const shown = lengths.map((length, i) => {
    const part = Math.min(Math.max(n - start, 0), length);
    if (part > 0 && part < length) active = i;
    if (part === length && length > 0) said = i + 1;
    start += length + PAUSE;
    return part;
  });
  const status: Status =
    said > YES ? "confirmed" : active >= 0 && active % 2 === 1 ? "speaking" : "listening";
  return { shown, active, status, loud: active >= 0 };
}

// One step of an exponential approach: `from` moves the share `k` (0..1) toward `to`.
export function ease(from: number, to: number, k: number): number {
  return from + (to - from) * k;
}

// Swing of a sound wave at `seconds`, 0..1: syllable bursts of about 4 per second
// inside phrases that breathe, with a short pause between them. `phase` (seconds)
// offsets a layer so the layers do not move in step.
export function voice(seconds: number, phase: number): number {
  const t = seconds + phase;
  const syllable = Math.abs(Math.sin(Math.PI * 4.2 * t)) ** 0.8;
  const phrase = Math.sin(Math.PI * 0.9 * t) * 0.5 + 0.5;
  const gate = Math.min(1, Math.max(0, (phrase - 0.1) / 0.3));
  return Math.min(1, 0.08 + 1.1 * syllable * gate * (0.7 + 0.3 * Math.sin(2.1 * t)));
}

export interface InquiryValues {
  kind: string;
  business: string;
  name: string;
  mail: string;
  phone: string;
  text: string;
}

export type Field = keyof InquiryValues;

export const KINDS = [
  "Restaurant",
  "Imbiss",
  "Café oder Bistro",
  "Lieferdienst",
  "Systemgastronomie",
  "Etwas anderes",
];

export const LIMITS = { business: 120, name: 120, mail: 254, phone: 25, text: 2000 };

const MAIL = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
const PHONE = /^[0-9 +()/.-]{6,25}$/;

// One sentence per refused field, German as the page is. The server checks again
// when it takes the form (#892); this only saves the visitor the round trip.
export function check(values: InquiryValues): Partial<Record<Field, string>> {
  const errors: Partial<Record<Field, string>> = {};
  const business = values.business.trim();
  const name = values.name.trim();
  const mail = values.mail.trim();
  const phone = values.phone.trim();
  if (!KINDS.includes(values.kind)) errors.kind = "Bitte wählen Sie die Art des Betriebs.";
  if (!business) errors.business = "Bitte nennen Sie den Namen Ihres Betriebs.";
  else if (business.length > LIMITS.business)
    errors.business = `Bitte höchstens ${LIMITS.business} Zeichen.`;
  if (!name) errors.name = "Bitte nennen Sie Ihren Namen.";
  else if (name.length > LIMITS.name) errors.name = `Bitte höchstens ${LIMITS.name} Zeichen.`;
  if (!mail) errors.mail = "Bitte geben Sie Ihre E-Mail-Adresse an.";
  else if (mail.length > LIMITS.mail || !MAIL.test(mail))
    errors.mail = "Diese E-Mail-Adresse sieht nicht vollständig aus.";
  if (phone && !PHONE.test(phone))
    errors.phone = "Bitte nur Ziffern, Leerzeichen und + - ( ) / angeben, 6 bis 25 Zeichen.";
  if (values.text.length > LIMITS.text)
    errors.text = `Bitte höchstens ${LIMITS.text} Zeichen.`;
  return errors;
}
