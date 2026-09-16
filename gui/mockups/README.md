# gui/mockups

Abgenommene Entwürfe. Sie sind die Vorlage für die Jinja2-Templates: Aufbau, Farben, Abstände und Wortlaut sind entschieden, nur die Daten sind erfunden.

| Datei | Ansicht | Abgenommen |
|---|---|---|
| `admin-desktop.html` | Adminansicht am PC, alle sieben Bereiche, klickbar | 16.09.2026 |

Die Betriebsansicht fürs Tablet ist als Skizze in `docs/06_GUI.md` §3 beschrieben und abgenommen; ein HTML-Entwurf entsteht mit T-3.1.

**Für Claude Code:** Die CSS-Variablen in `:root` (inkl. Dunkelmodus) und die Klassen `.list`, `.row`, `.detail`, `.badge`, `.card`, `.tbl` werden 1:1 in `gui/static/app.css` übernommen. Der Reiterwechsel und das Aufklappen werden mit HTMX gelöst, nicht mit dem Skript aus dem Mockup.
