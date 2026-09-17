# gui/mockups

Approved designs. They are the template for Jinja2 templates: layout, colors, spacing, and wording are decided; only the data are invented.

| File | View | Approved |
|---|---|---|
| `admin-desktop.html` | Admin view on desktop, all seven areas, clickable | 2026-09-16 |

The operations view for tablet is sketched and approved in `docs/06_GUI.md` §3; HTML design arrives with T-3.1.

**For Claude Code:** CSS variables in `:root` (including dark mode) and classes `.list`, `.row`, `.detail`, `.badge`, `.card`, `.tbl` transfer 1:1 to `gui/static/app.css`. Tab switching and expand/collapse are solved with HTMX, not the script from the mockup.
