# Mitarbeiten

Kurzfassung für alle, die an diesem Repository arbeiten. Die fachlichen Regeln stehen in `CLAUDE.md`, der Projektstand in `docs/01_STATUS.md`, die Aufgabenliste in `docs/07_ARBEITSPAKETE.md`.

## Entwicklungsumgebung

```bash
git clone https://github.com/Maeximilion/Maex-Voice-Agent.git
cd Maex-Voice-Agent
cp .env.example .env        # Zugangsdaten eintragen, Datei bleibt lokal
make up                     # Postgres, API und n8n starten
make migrate && make seed   # Schema anlegen, Testkonfiguration einspielen
make test                   # Suite gegen die echte Datenbank
```

Voraussetzungen: Docker mit Compose, Python 3.12 für Läufe außerhalb der Container.

## Ablauf einer Änderung

1. Aufgabe in `docs/07_ARBEITSPAKETE.md` wählen, deren Abhängigkeiten erledigt sind. Jede Aufgabe hat ein Issue (Spalte „Issue").
2. Branch vom aktuellen `main` abzweigen, Issue-Nummer im Namen: `feature/27-confirm-tool`, `fix/31-readback-datum`, `docs/44-bedientest`.
3. Bauen, dabei die Definition of Done in `CLAUDE.md` §7 einhalten. Fehler bekommen zuerst einen roten Test, dann den Fix.
4. `make lint` und `make test` lokal grün bekommen, bevor gepusht wird.
5. Pull Request öffnen, Issue verknüpfen (`Closes #27`), CI abwarten.
6. Review einarbeiten, dann Squash-Merge.

## Commits

Conventional Commits, deutsche Beschreibung, ein Commit je abgeschlossener Aufgabe:

```
feat(tools): confirm mit Outbox-Eintrag und audit_log
fix(reservations): Ueberbuchung bei parallelen Anrufen
docs(status): T-1.6 abgeschlossen
test(evals): Adressfaelle fuer Zonen ausserhalb des Gebiets
```

Verwendete Typen: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`. Der Geltungsbereich in Klammern ist das betroffene Modul (`tools`, `domain`, `gui`, `db`, `events`).

## Pull Requests

- Ein Pull Request behandelt ein Thema. Kein Sammel-PR über mehrere Aufgaben.
- Titel im Conventional-Commits-Format, denn beim Squash-Merge wird er zur Commit-Nachricht auf `main`.
- Beschreibung nach der Vorlage in `.github/PULL_REQUEST_TEMPLATE.md`: was geändert wurde, warum, wie geprüft, welches Issue geschlossen wird.
- `main` bleibt immer lauffähig. Direkt auf `main` wird nicht gepusht.
- Vor dem Merge laufen `ruff check`, `ruff format --check` und die Testsuite in der CI. Bei Änderungen an Dialogverhalten zusätzlich die Evals (`docs/08_EVALS.md`).

## Labels

| Kategorie | Labels |
|---|---|
| Typ | `feature`, `enhancement`, `bug`, `docs`, `refactor`, `chore`, `test` |
| Priorität | `priority: high`, `priority: medium`, `priority: low` |
| Status | `status: blocked`, `status: in-progress`, `status: needs-review` |
| Einordnung | `block` (Sammel-Issue), `stufe-0` bis `stufe-5`, `deployment` |

Jedes Issue trägt genau ein Typ-Label. Priorität und Status nur, wenn sie den Zustand wirklich ändern.

## Code-Stil

- `ruff` entscheidet über Format und Linting, Konfiguration im Repository.
- Englische Bezeichner, deutsche Kommentare und Fehlermeldungen.
- Kommentare erklären das Warum, nicht das Was. Keine Emojis in Code, Dokumentation, Commits oder Oberfläche.
- Geldbeträge immer als Integer in Cent, Telefonnummern in E.164, Zeiten in UTC gespeichert und in Ortszeit angezeigt.
- Fachlogik gehört nach `api/domain/`, niemals in `api/tools/` oder `api/gui/`. Die Abhängigkeitsrichtung steht in `docs/11_MODULE.md`.

## Was nie ins Repository gehört

`.env`, Zugangsdaten, echte Anrufaufnahmen, Transkripte und Kundendaten. Betriebs-, Orts- und Anbieternamen stehen als Platzhalter in spitzen Klammern; die echten Werte kommen aus der Umgebung und der Datenbank.

Sicherheitslücken bitte nicht als Issue melden, sondern nach `SECURITY.md`.
