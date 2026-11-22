// The inquiry page (#892): checks the form in the browser before it is sent, and
// puts each refusal beside its field. A file, not inline code (the site's CSP).
//
// The checks themselves are in site-logic.ts. They save a round trip only; the
// server checks again when it takes the form.
//
// Source: api/gui/ts/anfrage.ts. The file of the same name in api/gui/static/ is
// its compiled output (`npm run build`, docs/22); edit the source, never the output.

import { check } from "./site-logic.js";
import type { Field, InquiryValues } from "./site-logic.js";

const FIELDS: Field[] = ["kind", "business", "name", "mail", "phone", "text"];

type Control = HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;

function control(form: HTMLFormElement, field: Field): Control | null {
  const element = form.elements.namedItem(field);
  if (
    element instanceof HTMLInputElement ||
    element instanceof HTMLSelectElement ||
    element instanceof HTMLTextAreaElement
  )
    return element;
  return null;
}

function values(form: HTMLFormElement): InquiryValues {
  const read = (field: Field): string => control(form, field)?.value ?? "";
  return {
    kind: read("kind"),
    business: read("business"),
    name: read("name"),
    mail: read("mail"),
    phone: read("phone"),
    text: read("text"),
  };
}

// Shows the sentence beside the field and marks the field for assistive tech.
function show(form: HTMLFormElement, field: Field, message: string | undefined): void {
  const element = control(form, field);
  const holder = form.querySelector<HTMLElement>(`[data-error="${field}"]`);
  if (!element || !holder) return;
  holder.textContent = message ?? "";
  holder.hidden = !message;
  if (message) element.setAttribute("aria-invalid", "true");
  else element.removeAttribute("aria-invalid");
}

function validate(form: HTMLFormElement): boolean {
  const errors = check(values(form));
  FIELDS.forEach((field) => show(form, field, errors[field]));
  const first = FIELDS.find((field) => errors[field]);
  if (first) control(form, first)?.focus();
  return !first;
}

const form = document.querySelector<HTMLFormElement>("form[data-inquiry]");
if (form) {
  form.addEventListener("submit", (event) => {
    if (!validate(form)) event.preventDefault();
  });
  // A refusal goes away when the visitor has put it right.
  form.addEventListener("input", (event) => {
    const target = event.target;
    if (!(target instanceof Element) || !target.hasAttribute("aria-invalid")) return;
    const field = FIELDS.find((name) => control(form, name) === target);
    if (field && !check(values(form))[field]) show(form, field, undefined);
  });
}
