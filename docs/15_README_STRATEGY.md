# 15 – README-Pflege

Die README ist das Schaufenster des Repos. Sie beschreibt den Ist-Zustand für jemanden, der das Projekt zum ersten Mal sieht. Sie ist kein Änderungsprotokoll (das ist `docs/01_STATUS.md`) und keine Spezifikation (das sind `docs/02` bis `docs/14`).

## Wann die README aktualisiert wird

| Auslöser | Was sich ändert | Wer |
|---|---|---|
| Gate bestanden (G0 bis G5) | Status, Version, „Was funktioniert", nächste Schritte | Claude Code über `/gate` |
| Neue Abhängigkeit oder neuer Dienst (Docker-Image, Anbieter, Bibliothek) | Voraussetzungen, Schnellstart | Claude Code in derselben Aufgabe |
| Änderung am Schnellstart (neuer Befehl, neue Umgebungsvariable) | Schnellstart, Konfiguration | Claude Code in derselben Aufgabe |
| Bekanntes Problem, das Nutzer treffen wird | Bekannte Probleme | Claude Code über `/bug` oder `/done` |
| Neue Doku-Datei unter `docs/` | Dokumentation | Claude Code in derselben Aufgabe |

Nicht bei jedem Commit. Eine README, die sich täglich ändert, liest niemand mehr.

## Versionierung

Die Version in der README folgt den Gates. Semantic Versioning, Tag im Repo.

| Gate | Version | Bedeutung |
|---|---|---|
| Repo angelegt | 0.0.x | Gerüst, nichts Nutzbares |
| G0 | 0.1.0 | Fundament: Anbieter, Recht, Budget geklärt |
| G1 | 0.2.0 | Reservierung läuft auf Testnummer |
| G2 | 0.3.0 | Abholung läuft, Evals im Ziel |
| G3 | 0.5.0 | Lieferung läuft, funktional vollständig |
| G4 | 0.8.0 | Schattenmessung bestanden, bereit für echte Anrufe |
| G5 | 1.0.0 | Überlauf-Betrieb mit echten Kunden bestanden |
| G6 laufend | 1.x | Hauptannahme, Betrieb |

Alternative aus der Vorplanung: 1.0.0 bereits nach G3. Dagegen spricht: Vor G5 hat kein echter Kunde mit dem System gesprochen. Eine 1.0, die noch nie produktiv lief, ist keine.

Zwischen Gates: Patch-Versionen (0.2.1, 0.2.2) für Fixes, die nach `main` gehen.

## Pflichtabschnitte

Die README enthält immer, in dieser Reihenfolge:

1. Titel und ein Absatz: was das Projekt tut und für wen
2. Status: aktuelle Version, aktuelle Stufe, nächstes Gate, Datum
3. Was funktioniert / was nicht (je eine Liste, ehrlich)
4. Voraussetzungen
5. Schnellstart (kopierbar, getestet)
6. Konfiguration (die wichtigsten Umgebungsvariablen, Verweis auf `.env.example`)
7. Projektstruktur (Kurzform, Verweis auf `docs/11_MODULE.md`)
8. Entwicklung (Tests, Lint, Evals, Slash-Befehle)
9. Dokumentation (Tabelle der `docs/`-Dateien)
10. Bekannte Probleme
11. Lizenz und Kontakt

Was nicht hineingehört: Marketingtext, Emojis, Feature-Versprechen, Screenshots von Mockups, Änderungsverlauf.

## Ablauf bei einem Gate

```text
/gate G1
1. docs/01_STATUS.md: Gate-Tabelle auf „bestanden" mit Datum, Belege verlinken (Eval-Report, Protokoll)
2. README.md: Status, Version, „Was funktioniert", „Nächste Schritte" nach dieser Datei aktualisieren
3. Schnellstart einmal auf einem sauberen Checkout ausführen. Was nicht klappt, wird korrigiert, nicht kommentiert.
4. CHANGELOG.md: Abschnitt für die neue Version aus den Commits seit dem letzten Tag
5. Commit: docs: README und Status für <Gate>, Version <x.y.z>
6. Tag: v<x.y.z>
```

## Stil

- Deutsch, Sätze statt Stichwortsalat, kein Ausrufezeichen
- Präsens und Ist-Zustand: „Die API antwortet auf /health", nicht „wird antworten"
- Befehle in Codeblöcken, genau so, wie sie eingegeben werden
- Wenn etwas nicht funktioniert, steht es unter „Bekannte Probleme", nicht in einem Nebensatz
- Keine Emojis, keine Badges, die nichts messen
