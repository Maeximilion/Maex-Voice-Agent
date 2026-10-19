# gui/mockups

Approved designs. They are the template for Jinja2 templates: layout, colors, spacing, and wording are decided; only the data are invented.

| File | View | Approved |
|---|---|---|
| `admin-desktop.html` | Admin view on desktop, all seven areas, clickable | 2026-09-16 |

A mockup is a drawing, not the product. In `admin-desktop.html` only the tabs, expanding a row, search, the filter chips, "Annehmen" on an alias suggestion and the light/dark switch react; every other button does nothing. What runs today and what a restaurant needs: `README.md` and `docs/13_DEPLOYMENT.md` §0a.

The operations view for tablet is sketched and approved in `docs/06_GUI.md` §3; HTML design arrives with T-3.1.

**For Claude Code:** CSS variables in `:root` (including dark mode) and classes `.list`, `.row`, `.detail`, `.badge`, `.card`, `.tbl` transfer 1:1 to `gui/static/app.css`. Tab switching and expand/collapse are solved with HTMX, not the script from the mockup.
