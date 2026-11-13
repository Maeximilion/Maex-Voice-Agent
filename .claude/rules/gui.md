---
paths:
  - "api/gui/**"
  - "api/dashboard/**"
---

# Browser code and tablet text

Conventions of `CLAUDE.md` section 8 for the operations view and the dashboard (moved here by #695 Loop D PR 4). They load when a file under `api/gui/` or `api/dashboard/` is read or edited.

- Browser code is TypeScript, and the languages stay where they are (Maxi, 2026-11-10, #553): logic that runs in the browser (controls, events, drawing, checks on the client), in the operations view, the dashboard or a later interface, is written in `api/gui/ts/` and compiled (`docs/22_LANGUAGE_AND_PERFORMANCE.md` "Browser code in TypeScript"). A task or issue that needs or uses controls builds them this way. No new handwritten `.js` file and no new inline script; what is TypeScript stays TypeScript. The handwritten scripts of the operations view (`base.js`, `raumplan.js`, `app.js`) are migrated since 10.11.2026 (#626); the files of those names in `api/gui/static/` are compiled output and are never edited by hand. Pages are still rendered by Jinja2 with HTMX, and the backend stays Python: nothing moves back or across to another language without Maxi's word (the gate for the backend is #551). `api/tests/test_browser_code_language.py` fails on a handwritten script; its list of JavaScript files not yet migrated is empty and stays empty.
- German is product content in what the team reads on the tablet: labels, messages and banners of `api/gui/`, with the assertions on them in tests (Maxi, 30.10.2026: the team works in German, identifiers and comments around that text stay English).
