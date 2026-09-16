# 01 – Projektstatus

> **Dieses Dokument wird bei jeder Session aktualisiert.** Es ist die einzige Stelle, an der steht, wo das Projekt gerade wirklich steht.
> Stand: 16.09.2026 · Stufe 0 (Fundament) · Nächstes Gate: **G0 Go/No-Go** · Bundle v1.1

---

## Kurzfassung

Der Plan steht (`docs/00_PCF.md`, v1.0, freigegeben). Das Repo ist angelegt und enthält ein **lauffähiges Minimalgerüst**: FastAPI mit `/health`, Token-Auth, Antwort-Hülle, JSON-Logging, DB-Session und den zehn Stufe-1-Tabellen per Alembic-Migration 001; 42 Tests laufen grün. Seed, Tools und GUI fehlen noch.

Vor dem ersten echten Anruf fehlen zwei Dinge, die Maxi im Chat liefert: die Ist-Aufnahme des Betriebs (C1) und die Wahl der Voice-Plattform (C2). Claude Code kann trotzdem sofort weiterbauen: alles, was die Voice-Plattform nicht berührt, ist spezifiziert.

**Geprüft (16.09.2026):** `make up` baut das API-Image und startet Postgres und API, `/health` antwortet `{"status":"ok"}`, `make lint` und `make test` laufen im Container (T-0.1 fertig). Ein externes Code-Review (Codex) hat vier Findings am Docker-Setup geliefert, alle behoben: CA-Zertifikat im Build optional, Paketpfad `/app/api` erhalten, `scripts/` und `evals/` in den Container gemountet, README-Schnellstart auf das reduziert, was heute läuft.

---

## Gates

| Gate | Inhalt | Status |
|---|---|---|
| P | Plan freigegeben | bestanden 11.09.2026 |
| G0 | Budget · Recht · Telefonie-Weg · Anbieter gewählt | offen |
| G1 | Durchstich Reservierung, 20 Testanrufe fehlerfrei | offen |
| G2 | Abholung, Evals im Ziel, 0 geratene Positionen | offen |
| G3 | Lieferung, Zonen-Check fehlerfrei | offen |
| G4 | Schattenmessung, KI ≥ Team-Baseline | offen |
| G5 | Überlauf-Betrieb, 2 Wochen im Ziel | offen |

---

## Was als Nächstes dran ist

### In Claude Code (sofort startbar, ohne Anbieter)
1. **T-1.2** Seed-Skript `scripts/seed.py`: Mandant, Öffnungszeiten, Kapazität, Testkonfiguration (hängt an T-1.1, jetzt startklar)
2. **T-0.7** Slash-Befehle einmal durchspielen, CI grün
3. **T-0.8** Zahlwörter `domain/menu/numberwords.py` (startklar, hängt nur an T-0.6)

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
| D2 | Übergabeweg in die Kasse. Kasse ist **<Kassensystem> (<Kassenanbieter>)**, der eigene Shop läuft bereits automatisch hinein. Vier Stufen: A Tablet manuell · B Küchenbon direkt · C über den bestehenden Bestell-Eingang der Kasse (Partner-Kanal wie Lieferando, keine öffentliche Doku) · D Kassen-API. Empfehlung: Start mit A+B, C hängt an der Antwort von <Kassenanbieter> (Anfrage per E-Mail vorbereitet, 16.09.2026) | Maxi, <Kassenanbieter> | A+B sofort, C vor T-4.6 |
| D3 | Hosting-Anbieter in der EU | C2 | vor erstem Deployment |
| D4 | Stimme: natürlich oder hörbar synthetisch | Maxi | Stufe 1, Dialogtest |
| D5 | Lieferzonen: PLZ-Liste oder Polygone | Maxi | vor T-6.x (Stufe 3) |
| D6 | GUI-Technik: HTMX (empfohlen) oder React | Maxi | vor T-3.1 |
| D7 | Läuft unser eigener Gesprächs-Kern (`agent/`) auch im Betrieb, oder fährt die Plattform ihren eigenen Loop? empfohlen: eigener Kern, wenn die Plattform es erlaubt | Maxi mit C2 | zusammen mit D1 |

---

## Getroffene Annahmen (kippbar)

- Python 3.12, FastAPI, PostgreSQL 16, Alembic, pytest, ruff
- GUI als FastAPI + Jinja2 + HTMX + SSE, ein Container, kein Node-Build
- Ein Betrieb (<Pilotbetrieb>), aber mandantenfähiges Schema: jede betriebsbezogene Tabelle trägt `tenant_id`
- Deutsch als einzige Sprache in Stufe 1 bis 6
- Bezahlt wird bei Abholung oder Lieferung, keine Zahlung am Telefon
- **Betriebstag beginnt um 05:00 Ortszeit** (`api/core/time.py`, `DAY_STARTS_AT`): eine Bestellung um 00:30 zählt zum Vortag. In keinem Dokument definiert, Annahme vom 16.09.2026, kippbar
- Fehler der Fachlogik antworten mit HTTP 200 in der Hülle, damit die Voice-Plattform sie dem Agenten vorlegt; nur fehlende Auth ist 401
- **E9 (gesetzt, 16.09.2026):** Alles läuft auf EU-Servern oder bei EU-Anbietern, auch Transkription und Auswertung. Maxis PC ist nur Werkbank zum Entwickeln.

---

## Blocker

| Blocker | Blockiert | Auflösung |
|---|---|---|
| Rechts-Check nicht abgeschlossen | jede Verarbeitung echter Anrufaufnahmen (C7 / T-7.x) | `docs/09_BETRIEB_RECHT.md` abarbeiten |
| Kein Anbieter gewählt | Anbindung der Voice-Plattform, echte Testanrufe | C2 im Chat |
| Antwort <Kassenanbieter> zur Bestell-Schnittstelle steht aus | Stufe C der Kassenanbindung (T-4.6 Variante C) | E-Mail abschicken, Lizenznummer bereithalten; bis dahin A+B bauen |
| Menüdaten liegen nicht strukturiert vor | Stufe 2 komplett | C1 klärt Format, dann T-4.1 Import |
| Docker Hub Rate-Limit beim ersten `make up` möglich (am 16.09.2026 einmal aufgetreten, nach Wartezeit durch) | Image-Build | `docker login` mit kostenlosem Konto, siehe README, Abschnitt Bekannte Probleme |

---

## Erledigt

| Datum | Was |
|---|---|
| 11.09.2026 | PCF v1.0 erstellt und freigegeben, Architektur Hybrid entschieden (E1) |
| 15.09.2026 | Repo-Gerüst und Specs für Claude Code exportiert |
| 15.09.2026 | API-Minimalgerüst: `/health`, Token-Auth, Antwort-Hülle, 3 Tests grün (T-0.3 fertig) |
| 16.09.2026 | README-Pflege eingeführt: `docs/15_README_STRATEGY.md`, `/gate`, `CHANGELOG.md`, Versionen je Gate; alle Emojis aus dem Bundle entfernt |
| 16.09.2026 | Umbenannt in Maex Voice-Agent (Produkt), der Pilotbetrieb wird als Platzhalter `<Pilotbetrieb>` geführt; Betriebsorte in `docs/13` §0 festgeschrieben: **nichts läuft auf Maxis PC, auch nicht die Transkription** (E9) |
| 16.09.2026 | Adminansicht als klickbares Desktop-Mockup in `gui/mockups/` aufgenommen, Vorlage für T-3.1 |
| 16.09.2026 | GUI-Runde abgeschlossen: Betriebs- und Adminansicht als Mockup abgenommen, Änderungen in `docs/06_GUI.md` §7 |
| 16.09.2026 | Bundle v1.1: Modul-Architektur (11), Playbooks + Slash-Befehle (12), Deployment (13), Menü-Importformat (14), Outbox, Agent-Kern + Simulator, CI, Prod-Compose; 18 neue Aufgaben |
| 16.09.2026 | Platzhalter statt Namen: `<Pilotbetrieb>`, `<Firmenname>`, `<Ort>`, `<Kassensystem>`, `<Kassenanbieter>`, `example.com` in Code, Doku, Mockup und Caddyfile; Regel in `CLAUDE.md` §1 |
| 16.09.2026 | **T-0.1 fertig:** `make up` gegen echtes Docker, Postgres + API healthy, n8n erreichbar (nach Fix `N8N_LISTEN_ADDRESS=0.0.0.0`), `/health` und `/v1/tools/ping` geprüft, `make lint` + `make test` im Container grün. Codex-Review (4 Findings) eingearbeitet. `ruff format` erstmals gelaufen (T-0.4: nur `make migrate` offen, wartet auf T-1.1) |
| 16.09.2026 | **T-0.2 fertig:** `api/db.py` mit Engine (`pool_pre_ping`), `SessionLocal`, `get_db` (Rollback bei Fehler, Close immer). Tests: Session arbeitet, Rollback bei Exception, DB nicht erreichbar liefert `service_unavailable`-Hülle statt Stacktrace. 7 Tests grün, lokal und im Container |
| 16.09.2026 | **T-0.6 fertig:** `api/core/` mit `envelope` (Hülle), `errors` (8 Codes aus 04 §1, `AppError` wird zentral übersetzt), `auth` (aus `main.py` gezogen), `logging` (JSON-Zeilen mit `request_id`/`call_id`, Middleware misst Dauer, `X-Request-ID` wird übernommen oder erzeugt), `time` (UTC/Ortszeit, Betriebstag, Sommerzeit-sichere Tagesgrenzen). `main.py` nutzt nur noch `core`. 34 Tests grün |
| 16.09.2026 | **T-1.1 fertig, T-0.4 fertig:** Alembic unter `db/` (`alembic -c db/alembic.ini`, URL aus `settings`), Migration 001 mit den zehn Stufe-1-Tabellen inkl. `outbox` und `audit_log`, Enums als CHECK-Constraints, `idempotency_key` unique, `deleted_at` auf Reservierungen und Rückrufen. Modelle unter `api/models/` (Base, Mixins für UUID-PK, `tenant_id`, Zeitstempel). Tests gegen eine Wegwerf-DB: up, Modelle ohne Diff zum Schema, down/up, doppelter Schlüssel, fehlende `call_id`, ungültiger `outbox.status`. `make migrate` läuft, Dev-DB auf 001. 42 Tests grün |

---

## Änderungsprotokoll dieser Datei

- **16.09.2026:** T-1.1 und T-0.4 abgeschlossen (Alembic, Migration 001, Modelle), T-1.2 startklar. Regel: Empfehlungen werden direkt abgenommen (`CLAUDE.md` §6).
- **16.09.2026:** T-0.6 abgeschlossen (`core/`), Annahme Betriebstag 05:00, T-0.8 startklar.
- **16.09.2026:** T-0.2 abgeschlossen (`db.py`), nächste Schritte neu nummeriert.
- **16.09.2026:** T-0.1 abgeschlossen, Codex-Review eingearbeitet, Platzhalter-Regel, nächste Schritte neu nummeriert.
- **16.09.2026:** v1.1 – Lupe über den Plan: Module, Playbooks, Deployment, Importformat, Outbox, Agent-Kern, D7.
- **15.09.2026:** Erstfassung beim Export nach Claude Code.
