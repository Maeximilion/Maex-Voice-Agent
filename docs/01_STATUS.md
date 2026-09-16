# 01 – Projektstatus

> **Dieses Dokument wird bei jeder Session aktualisiert.** Es ist die einzige Stelle, an der steht, wo das Projekt gerade wirklich steht.
> Stand: 16.09.2026 · Stufe 0 (Fundament) · Nächstes Gate: **G0 Go/No-Go** · Status-Version: 1.1.1

---

## Kurzfassung

Der Plan steht (`docs/00_PCF.md`, v1.0, freigegeben). Das Repo ist angelegt und enthält ein **lauffähiges Minimalgerüst**: FastAPI mit `/health`, Token-Auth und der einheitlichen Antwort-Hülle, drei Tests laufen grün. Datenbank, Tools und GUI fehlen noch komplett.

Vor dem ersten echten Anruf fehlen zwei Dinge, die Maxi im Chat liefert: die Ist-Aufnahme des Betriebs (C1) und die Wahl der Voice-Plattform (C2). Claude Code kann trotzdem sofort weiterbauen: alles, was die Voice-Plattform nicht berührt, ist spezifiziert.

**Ungeprüft:** `docker-compose.yml` wurde geschrieben, aber noch nie gegen echtes Docker gestartet. Das ist T-0.1 und der erste Schritt in Claude Code.

Kompletter Fahrplan von hier bis zum Zielzustand: Abschnitt „Fahrplan" unten. Volle Details je Stufe: `docs/00_PCF.md` §5.

---

## Fahrplan: wo wir stehen, wo wir hinwollen

| Stufe | Ziel | Gate | Stand |
|---|---|---|---|
| P Plan | Plan freigegeben | – | ✅ 11.09.2026 |
| **0 Fundament** | Fakten, Recht, Budget, Anbieter klären | G0 Go/No-Go | 🟡 läuft – Block „Gerüst" in `docs/07_ARBEITSPAKETE.md` |
| 1 Durchstich Reservierung | ganze Kette einmal echt (Testnummer → KI → Tool → DB → GUI) | G1 | ⬜ |
| 2 Abholung | Menü sicher verstanden, Bestellung korrekt in der Küche | G2 | ⬜ |
| 3 Lieferung | Adresse und Zone ohne Fehler | G3 | ⬜ |
| 4 Einlernen & Schattenmessung | echte Fehlerquote messen, ohne Kundenrisiko | G4 | ⬜ |
| 5 Überlauf-Betrieb | KI nimmt an, nur wenn das Team nicht abnimmt | G5 | ⬜ |
| 6 Hauptannahme & Betrieb | KI nimmt zuerst an, Team bleibt Rückfallebene, Monats-Review läuft | Monats-Review | ⬜ Zielzustand |

**Wir sind hier:** Stufe 0, Block „Gerüst" – Gerüst starten, `core/`, Alembic, CI (siehe „Was als Nächstes dran ist"). **Wo wir hinwollen:** Stufe 6, laufender Betrieb mit KI als primärer Annahme und Team als Rückfallebene.

---

## Gates im Detail

| Gate | Inhalt | Status |
|---|---|---|
| P | Plan freigegeben | ✅ 11.09.2026 |
| G0 | Budget · Recht · Telefonie-Weg · Anbieter gewählt | 🔴 offen |
| G1 | Durchstich Reservierung, 20 Testanrufe fehlerfrei | ⬜ |
| G2 | Abholung, Evals im Ziel, 0 geratene Positionen | ⬜ |
| G3 | Lieferung, Zonen-Check fehlerfrei | ⬜ |
| G4 | Schattenmessung, KI ≥ Team-Baseline | ⬜ |
| G5 | Überlauf-Betrieb, 2 Wochen im Ziel | ⬜ |

---

## Was als Nächstes dran ist

### In Claude Code (sofort startbar, ohne Anbieter)
1. **T-0.1** `docker compose up` einmal wirklich starten, `/health` im Browser prüfen, Fehler ausräumen
2. **T-0.2 Rest** `db.py` mit Engine und Session ergänzen
3. **T-0.7** Slash-Befehle einmal durchspielen, CI grün
4. **T-0.6** `core/` – Hülle, Fehlerklassen, JSON-Logging
5. **T-1.1** Alembic einrichten, Migration 001 (jetzt inkl. `outbox`)

Reihenfolge der ersten sieben Sessions: `docs/07_ARBEITSPAKETE.md` §Empfohlene Reihenfolge. Jederzeit parallel möglich: **T-0.8** Zahlwörter (reine Funktion).

Details und vollständige Liste: `docs/07_ARBEITSPAKETE.md`

### Im Chat (Maxi)
- **C1** Ist-Aufnahme: Kasse, Telefonanlage, Anrufvolumen, Menüformat, Baseline-Messung, Rechts-Check
- **C2** Voice-Plattform recherchieren, bewerten, PoC auf Testnummer

---

## Offene Entscheidungen

| # | Frage | Wer | Wann gebraucht |
|---|---|---|---|
| D1 | Voice-Plattform | Maxi nach C2-Recherche | vor T-2.x (Anbindung) |
| D2 | Übergabeweg in die Kasse. Kasse ist **order smart (app smart GmbH / OrderYOYO)**, der eigene Shop läuft bereits automatisch hinein. Vier Stufen: A Tablet manuell · B Küchenbon direkt · C über den bestehenden Bestell-Eingang der Kasse (Partner-Kanal wie Lieferando, keine öffentliche Doku) · D Kassen-API. ⭐ Start mit A+B, C hängt an der Antwort von app smart (Anfrage per E-Mail vorbereitet, 16.09.2026) | Maxi, app smart | A+B sofort, C vor T-4.6 |
| D3 | Hosting-Anbieter in der EU | C2 | vor erstem Deployment |
| D4 | Stimme: natürlich oder hörbar synthetisch | Maxi | Stufe 1, Dialogtest |
| D5 | Lieferzonen: PLZ-Liste oder Polygone | Maxi | vor T-6.x (Stufe 3) |
| D6 | GUI-Technik: HTMX ⭐ oder React | Maxi | vor T-3.1 |
| D7 | Läuft unser eigener Gesprächs-Kern (`agent/`) auch im Betrieb, oder fährt die Plattform ihren eigenen Loop? ⭐ eigener Kern, wenn die Plattform es erlaubt | Maxi mit C2 | zusammen mit D1 |

---

## Getroffene Annahmen (⚠️ kippbar)

- Python 3.12, FastAPI, PostgreSQL 16, Alembic, pytest, ruff
- GUI als FastAPI + Jinja2 + HTMX + SSE, ein Container, kein Node-Build
- Ein Betrieb (Yoki Yoki), aber mandantenfähiges Schema: jede betriebsbezogene Tabelle trägt `tenant_id`
- Deutsch als einzige Sprache in Stufe 1 bis 6
- Bezahlt wird bei Abholung oder Lieferung, keine Zahlung am Telefon

---

## Blocker

| Blocker | Blockiert | Auflösung |
|---|---|---|
| Rechts-Check nicht abgeschlossen | jede Verarbeitung echter Anrufaufnahmen (C7 / T-7.x) | `docs/09_BETRIEB_RECHT.md` abarbeiten |
| Kein Anbieter gewählt | Anbindung der Voice-Plattform, echte Testanrufe | C2 im Chat |
| Antwort app smart zur Bestell-Schnittstelle steht aus | Stufe C der Kassenanbindung (T-4.6 Variante C) | E-Mail abschicken, Lizenznummer bereithalten; bis dahin A+B bauen |
| Menüdaten liegen nicht strukturiert vor | Stufe 2 komplett | C1 klärt Format, dann T-4.1 Import |

---

## Erledigt

| Datum | Was |
|---|---|
| 11.09.2026 | PCF v1.0 erstellt und freigegeben, Architektur Hybrid entschieden (E1) |
| 15.09.2026 | Repo-Gerüst und Specs für Claude Code exportiert |
| 15.09.2026 | API-Minimalgerüst: `/health`, Token-Auth, Antwort-Hülle, 3 Tests grün (T-0.3 ✅) |
| 16.09.2026 | Adminansicht als klickbares Desktop-Mockup in `gui/mockups/` aufgenommen, Vorlage für T-3.1 |
| 16.09.2026 | GUI-Runde abgeschlossen: Betriebs- und Adminansicht als Mockup abgenommen, Änderungen in `docs/06_GUI.md` §7 |
| 16.09.2026 | Bundle v1.1: Modul-Architektur (11), Playbooks + Slash-Befehle (12), Deployment (13), Menü-Importformat (14), Outbox, Agent-Kern + Simulator, CI, Prod-Compose; 18 neue Aufgaben |
| 16.09.2026 | README-Status auf den tatsächlichen Stand synchronisiert |

---

## Versionierung dieser Datei

Eigene, semantische Version `MAJOR.MINOR.PATCH`, unabhängig von der CLAUDE.md-Bundle-Version:

| Bump | Auslöser | Beispiel |
|---|---|---|
| **MAJOR** | Gate bestanden / Stufenwechsel / Architektur-Entscheidung (E-Nr.) gekippt | G0 bestanden → Stufe 1 beginnt |
| **MINOR** | Entscheidung getroffen (D-Nr. beantwortet), neues Arbeitspaket-Ergebnis ändert „Was als Nächstes dran ist" | D1 Anbieter gewählt |
| **PATCH** | reine Status-Pflege: Task-Haken, neue Annahme ⚠️, neuer/gelöster Blocker | T-0.1 auf ✅ |

**Auto-Update:** `python scripts/status_bump.py <patch|minor|major> "<eine Zeile Änderung>"` setzt Datum, Version und Changelog-Zeile automatisch. Wird von `/done`, `/task` und `/handover` aufgerufen (siehe `.claude/commands/`) – von Hand nur bei Bedarf. Zusätzliches Sicherheitsnetz: CI (`.github/workflows/ci.yml`) schlägt fehl, wenn sich `docs/07_ARBEITSPAKETE.md` ändert, `docs/01_STATUS.md` im selben Diff aber unangetastet bleibt.

---

## Changelog

- **v1.1.1 · 16.09.2026:** Fahrplan-Abschnitt, Versionierungsschema und Auto-Update (Skript + CI-Sync-Check) eingeführt
- **v1.1.0 · 16.09.2026:** Bundle v1.1 – Lupe über den Plan: Module, Playbooks, Deployment, Importformat, Outbox, Agent-Kern, D7.
- **v1.0.0 · 15.09.2026:** Erstfassung beim Export nach Claude Code.
