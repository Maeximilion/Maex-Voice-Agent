// The testable part of the public site (#892): how far an example call has been
// typed, and the checks of the inquiry form. No DOM in here, so node runs it
// (api/tests/site_logic.mjs).
//
// Source: api/gui/ts/site-logic.ts. The file of the same name in api/gui/static/
// is its compiled output (`npm run build`, docs/22); edit the source.
// Characters of pause between two sentences of a call, counted like typed ones.
export const PAUSE = 14;
// The pause after the last sentence before the call plays again.
export const REST = 80;
// A call is confirmed once the guest's yes (the third sentence) is complete.
const YES = 2;
// Total steps of a call: every sentence and its pause.
export function total(lengths) {
    return lengths.reduce((sum, length) => sum + length + PAUSE, 0);
}
// The state of a call of sentences with these lengths after `n` typed characters.
// Sentences alternate guest, agent, guest, agent.
export function callState(lengths, n) {
    let start = 0;
    let active = -1;
    let said = 0;
    const shown = lengths.map((length, i) => {
        const part = Math.min(Math.max(n - start, 0), length);
        if (part > 0 && part < length)
            active = i;
        if (part === length && length > 0)
            said = i + 1;
        start += length + PAUSE;
        return part;
    });
    const status = said > YES ? "confirmed" : active >= 0 && active % 2 === 1 ? "speaking" : "listening";
    return { shown, active, status, loud: active >= 0 };
}
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
export function check(values) {
    const errors = {};
    const business = values.business.trim();
    const name = values.name.trim();
    const mail = values.mail.trim();
    const phone = values.phone.trim();
    if (!KINDS.includes(values.kind))
        errors.kind = "Bitte wählen Sie die Art des Betriebs.";
    if (!business)
        errors.business = "Bitte nennen Sie den Namen Ihres Betriebs.";
    else if (business.length > LIMITS.business)
        errors.business = `Bitte höchstens ${LIMITS.business} Zeichen.`;
    if (!name)
        errors.name = "Bitte nennen Sie Ihren Namen.";
    else if (name.length > LIMITS.name)
        errors.name = `Bitte höchstens ${LIMITS.name} Zeichen.`;
    if (!mail)
        errors.mail = "Bitte geben Sie Ihre E-Mail-Adresse an.";
    else if (mail.length > LIMITS.mail || !MAIL.test(mail))
        errors.mail = "Diese E-Mail-Adresse sieht nicht vollständig aus.";
    if (phone && !PHONE.test(phone))
        errors.phone = "Bitte nur Ziffern, Leerzeichen und + - ( ) / angeben, 6 bis 25 Zeichen.";
    if (values.text.length > LIMITS.text)
        errors.text = `Bitte höchstens ${LIMITS.text} Zeichen.`;
    return errors;
}
