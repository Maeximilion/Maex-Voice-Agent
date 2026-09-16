Bearbeite Aufgabe $ARGUMENTS aus `docs/07_ARBEITSPAKETE.md`.

1. Prüfe, ob alle Abhängigkeiten ✅ sind. Falls nicht: nenne die fehlende und stoppe.
2. Lies die Spec aus der Spalte „Spec" und prüfe in `docs/11_MODULE.md`, in welche Module die Dateien gehören.
3. Lege den Branch `task/<id-mit-bindestrichen>-<kurzname>` an.
4. Zeige einen Plan mit maximal 5 Zeilen: Dateien, Tests, Migrationsbedarf. Warte auf ein Ja, wenn mehr als drei Dateien betroffen sind.
5. Schreibe zuerst die Tests (Normalfall + mindestens zwei Grenzfälle), dann den Code, bis sie grün sind. Führe sie wirklich aus.
6. Für Tools im heißen Pfad: miss die Antwortzeit und nenne den Wert.
7. Setze in `docs/07_ARBEITSPAKETE.md` den Status auf 🟡 zu Beginn und auf ✅ am Ende. Folgt direkt `/done`, übernimmt das den Status-Bump – sonst am Ende selbst `python scripts/status_bump.py patch "<Task-ID> ✅"`.
8. Nutze bis zu drei Folgeschritte für offensichtliche Lücken, dann melde: was läuft, was gemessen wurde, was offen ist.
Halte dich an die harten Regeln aus `CLAUDE.md` §2. Frage geschlossen mit ⭐, eine Frage pro Unterbrechung.
