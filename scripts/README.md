# scripts

| Skript | Zweck | Aufgabe |
|---|---|---|
| `seed.py` | Mandant, Öffnungszeiten, Kapazität, Testkonfiguration | T-1.2 |
| `import_menu.py` | CSV nach `docs/14_MENU_IMPORTFORMAT.md` einspielen, `--dry-run` | T-4.2 |
| `menu_diff.py` | Preise Kasse gegen Agent-DB | T-4.9 |
| `backup.sh` / `restore.sh` | `pg_dump` verschlüsselt, Wiederherstellung | T-8.6 |
| `seed_zones.py` | Lieferzonen aus der Zonenliste (aus dem Lieferservice-Projekt) | T-6.3 |
| `status_bump.py` | Version, Datum und Changelog-Zeile in `docs/01_STATUS.md` automatisch fortschreiben | – (Meta/Tooling) |
