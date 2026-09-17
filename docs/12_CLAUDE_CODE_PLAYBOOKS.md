# 12 – Claude-Code-Playbooks

> Nicht jede Session ist gleich. Neun wiederkehrende Situationen, jede mit festem Ablauf.
> Die Slash-Befehle in `.claude/commands/` automatisieren die Rituale: `/start`, `/task`, `/done`, `/bug`, `/eval`, `/gate`, `/handover`.

---

## Grundregeln für jede Session

1. **Erst lesen, dann tippen.** `/start` liest `CLAUDE.md` und `docs/01_STATUS.md` und schlägt die nächste Aufgabe vor.
2. **Eine Aufgabe, ein Branch.** `task/T-1-3-service-status`. Merge nach `main` nur mit grünen Tests.
3. **Plan vor Code**, sobald mehr als drei Dateien betroffen sind. Plan zeigen, kurz bestätigen lassen, dann bauen.
4. **Klein committen.** Ein Commit pro abgeschlossenem Schritt, nicht pro Session.
5. **Ausführen, nicht behaupten.** Tests laufen wirklich. Latenz wird wirklich gemessen.
6. **Weiterdenk-Budget: drei Schritte.** Nach der Aufgabe bis zu drei naheliegende Folgeschritte selbst machen (Test ergänzen, Doku nachziehen, offensichtliche Lücke), dann melden.
7. **Am Ende `/done` und `/handover`.** Status ist aktuell, bevor die Session schließt.

---

## S1 – Session-Start
```text
/start
→ liest CLAUDE.md, docs/01_STATUS.md, docs/07_ARBEITSPAKETE.md
→ nennt Stufe, Gate, Blocker
→ schlägt 1–3 startklare Aufgaben vor, mit Empfehlung
→ wartet auf Wahl
```

## S2 – Feature-Aufgabe (der Normalfall)
```text
/task T-1.3
1. Spec lesen (Spalte „Spec" in 07), betroffene Module in 11 prüfen
2. Branch anlegen
3. Plan: Dateien, Tests, Migrationsbedarf – max. 5 Zeilen
4. Tests zuerst für Normalfall + 2 Grenzfälle (rot)
5. Implementieren, bis grün
6. Latenz messen (Tools im heißen Pfad)
7. ruff, Doku, Status → /done
```

## S3 – Fehler aus dem Betrieb
Der wichtigste Ablauf, weil er die Qualität langfristig trägt.
```text
/bug "Kunde sagte 'zwei Ente' und bekam Nummer 2"
1. Fall als Eval-Fall in evals/cases/ anlegen – zuerst, vor jeder Analyse
2. make eval → der neue Fall muss ROT sein (sonst ist der Fall falsch beschrieben)
3. Ursache finden: welches Modul? numberwords? search? ladder?
4. Fix minimal, Unit-Test im Modul
5. make eval → alles grün, Genauigkeit nicht gesunken
6. Commit: fix(menu): Mengenwort vor Gerichtname nicht als Nummer lesen (#eval menu_0042)
```
**Regel:** Kein Fix ohne vorherigen roten Testfall. Sonst kommt der Fehler zurück.

## S4 – Schema-Änderung
```text
1. docs/03_DATA_MODEL.md zuerst ändern (die Spec ist der Vertrag)
2. alembic revision --autogenerate -m "…", Migration durchlesen und korrigieren
3. alembic upgrade head → downgrade -1 → upgrade head, alle drei müssen laufen
4. Modelle, Schemas, betroffene domain-Funktionen
5. Seed anpassen, Tests
6. Auf echten Daten nur mit vorherigem Backup (scripts/backup.sh)
```

## S5 – Prompt-Iteration
```text
1. prompts/system_vN+1.md anlegen, EINE Variable ändern
2. make eval → Vergleich zu vN in evals/reports/
3. besser oder gleich → übernehmen, Ergebnis in die Commit-Nachricht
4. schlechter → Erkenntnis in docs/01_STATUS.md, Datei behalten als Beleg
```
**Regel:** Nie zwei Dinge gleichzeitig ändern. Sonst weißt du nicht, was gewirkt hat.

## S6 – Menü-Import
```text
1. CSV-Dateien nach docs/14_MENU_IMPORT_FORMAT.md liegen in imports/ (nicht im Repo)
2. python -m scripts.import_menu --dry-run → Bericht: neu, geändert, Fehler
3. Fehler in der CSV beheben (nicht im Code umgehen)
4. python -m scripts.import_menu → einspielen
5. make eval TAGS=menu → nichts darf schlechter werden
6. Neue Gerichte ohne Alias → Liste an Maxi für den Chat
```

## S7 – Anbieter-Adapter (nach D1)
```text
1. telephony/port.py lesen – das Interface ist gesetzt
2. Webhook-Beispiele des Anbieters in telephony/fixtures/ ablegen
3. adapters/<anbieter>.py: Webhook → Port-Aufruf, Port → Anbieter-API
4. Tests gegen die Fixtures, ohne Netz
5. Signaturprüfung der Webhooks
6. Erster echter Testanruf: nur mit Tunnel (docs/13_DEPLOYMENT.md §Entwicklung)
7. Latenz, Anrufer-ID, DTMF, Weiterleitung, Unterbrechen → Protokoll in docs/01_STATUS.md
```

## S8 – Deployment oder Update
```text
1. make test && make eval lokal grün
2. git tag vX.Y
3. Auf dem Server: backup → git pull → docker compose -f … up -d --build → alembic upgrade
4. /health prüfen, ein Testanruf oder sim/replay gegen Produktion mit Test-Mandant
5. Rückweg bereit: vorheriger Tag, alembic downgrade, Backup
```

## S8b – Gate bestanden
```text
/gate G1
→ Belege je Kriterium prüfen, Status und README nach docs/15 nachziehen,
  Schnellstart auf sauberem Checkout testen, CHANGELOG, Tag v0.2.0
```

## S9 – Session-Ende
```text
/done      → Tests, Lint, Status in 07 setzen, 01_STATUS aktualisieren, Commit vorschlagen
/handover  → Übergabeblock nach PCF Abschnitt 12, für den Chat oder die nächste Session
```

---

## Wann Claude Code fragt

Geschlossen, mit Empfehlung, eine Frage pro Unterbrechung. Fragen ist Pflicht bei:
- Änderung an einer Regel aus `CLAUDE.md` §2
- Löschen oder Umbenennen von Tabellen mit Daten
- Allem, was einen externen Dienst kostet oder einen Vertrag berührt
- Zwei gleichwertigen Architekturwegen, die später schwer umkehrbar sind

Nicht fragen bei: Dateinamen, Testfällen, Reihenfolge innerhalb einer Aufgabe, Formulierung von Kommentaren. Da wird entschieden und als Annahme notiert.

---

## Parallel arbeiten

Zwei Sessions gleichzeitig sind sinnvoll, wenn die Module sich nicht berühren (Regel aus 11 §1):
- `domain/menu/` und `gui/` gleichzeitig: ja
- `domain/ordering/` und `tools/draft_order`: nein, gleicher Vertrag
- Zwei Sessions auf derselben Migration: nie

Vor einer parallelen Session: in `docs/01_STATUS.md` eintragen, wer was hält.
