# 01 – Projektstatus

> **Dieses Dokument wird bei jeder Session aktualisiert.** Es ist die einzige Stelle, an der steht, wo das Projekt gerade wirklich steht.
> Stand: 17.09.2026 · Stufe 0 (Fundament) · Nächstes Gate: **G0 Go/No-Go** · Status-Version: 1.6.2

---

## Kurzfassung

CI-Ergänzung (17.09.2026): Der Job `docker-smoke` baut das API-Image ohne lokales CA-Zertifikat und startet es ohne Bind-Mount. Er prüft `/health`, die Ablehnung fehlender/falscher Tokens und einen authentifizierten Ping. Damit werden die früheren Dockerfile-Fehler bei Zertifikat und Paketpfad automatisch erkannt. Der Test prüft den Containerstart, keine DB-Integration; diese bleibt in der pytest-Suite. Lokale Docker-Ausführung war in dieser Session nicht verfügbar; Ausführung erfolgt über GitHub Actions.

Der Plan steht (`docs/00_PCF.md`, v1.0, freigegeben). Das Repo ist angelegt und enthält ein **lauffähiges Minimalgerüst**: FastAPI mit `/health`, Token-Auth, Antwort-Hülle, JSON-Logging, DB-Session und den zehn Stufe-1-Tabellen per Alembic-Migration 001 und einem idempotenten Seed mit Testkonfiguration; die Tools im heißen Pfad (`get_service_status`, `check_slot`, `create_reservation`, `confirm`) antworten in rund 8 bis 15 ms (p95); 183 Tests laufen grün. Die Reservierung ist damit vollständig: `create_reservation` legt den Entwurf mit `readback` an, `confirm` macht ihn gültig, schreibt `audit_log` und legt das Ereignis `reservation.confirmed` in die Outbox. Der Dispatcher (`api/events/`) leert die Outbox inzwischen: eigener Prozess, ein POST je Ereignis nach n8n mit der Ereignis-id als Idempotenz-Schlüssel, Backoff 5 s / 30 s / 2 min / 10 min, danach `failed` mit Alarm im Log. Rückrufe stehen ebenfalls: `create_callback` legt die Aufgabe für das Team an, protokolliert sie und meldet sie über die Outbox. Was noch fehlt: die Übergabe an einen Menschen, das Anruf-Log und die GUI.

Vor dem ersten echten Anruf fehlen zwei Dinge, die Maxi im Chat liefert: die Ist-Aufnahme des Betriebs (C1) und die Wahl der Voice-Plattform (C2). Claude Code kann trotzdem sofort weiterbauen: alles, was die Voice-Plattform nicht berührt, ist spezifiziert.

**Geprüft (16.09.2026):** `make up` baut das API-Image und startet Postgres und API, `/health` antwortet `{"status":"ok"}`, `make lint` und `make test` laufen im Container (T-0.1 fertig). Ein externes Code-Review (Codex) hat vier Findings am Docker-Setup geliefert, alle behoben: CA-Zertifikat im Build optional, Paketpfad `/app/api` erhalten, `scripts/` und `evals/` in den Container gemountet, README-Schnellstart auf das reduziert, was heute läuft.

Kompletter Fahrplan von hier bis zum Zielzustand: Abschnitt „Fahrplan" unten. Volle Details je Stufe: `docs/00_PCF.md` §5.

---

## Fahrplan: wo wir stehen, wo wir hinwollen

| Stufe | Ziel | Gate | Stand |
|---|---|---|---|
| P Plan | Plan freigegeben | – | bestanden 11.09.2026 |
| **0 Fundament** | Fakten, Recht, Budget, Anbieter klären | G0 Go/No-Go | läuft – Block „Gerüst" in `docs/07_ARBEITSPAKETE.md` |
| 1 Durchstich Reservierung | ganze Kette einmal echt (Testnummer → KI → Tool → DB → GUI) | G1 | offen |
| 2 Abholung | Menü sicher verstanden, Bestellung korrekt in der Küche | G2 | offen |
| 3 Lieferung | Adresse und Zone ohne Fehler | G3 | offen |
| 4 Einlernen & Schattenmessung | echte Fehlerquote messen, ohne Kundenrisiko | G4 | offen |
| 5 Überlauf-Betrieb | KI nimmt an, nur wenn das Team nicht abnimmt | G5 | offen |
| 6 Hauptannahme & Betrieb | KI nimmt zuerst an, Team bleibt Rückfallebene, Monats-Review läuft | Monats-Review | offen, Zielzustand |

**Wir sind hier:** Stufe 0, Block „Gerüst" – Gerüst starten, `core/`, Alembic, CI (siehe „Was als Nächstes dran ist"). **Wo wir hinwollen:** Stufe 6, laufender Betrieb mit KI als primärer Annahme und Team als Rückfallebene.

---

## Gates im Detail

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
1. **T-1.8** `transfer_to_team` inklusive Schleifenschutz und Erreichbarkeitsprüfung
2. **T-1.9** Anruf-Log `POST /v1/calls/start` und `/end`: bis dahin muss die `calls`-Zeile von Hand oder im Test angelegt werden (siehe Annahmen)
3. Ein n8n-Workflow, der die Ereignisse des Dispatchers entgegennimmt (Export nach `n8n/`); bis dahin läuft der kalte Pfad ins Leere
4. Jederzeit parallel: **T-0.7** Slash-Befehle und CI, **T-0.8** Zahlwörter

Reihenfolge der ersten sieben Sessions: `docs/07_ARBEITSPAKETE.md` §Empfohlene Reihenfolge. Jederzeit parallel möglich: **T-0.8** Zahlwörter (reine Funktion).

Details und vollständige Liste: `docs/07_ARBEITSPAKETE.md`. Auf GitHub gespiegelt als Issues: neun Sammel-Issues je Block (#5 bis #13), 68 Arbeitspakete als Sub-Issues darunter, Labels je Stufe. Gepflegt wird der Status hier in der Doku, der Spiegel folgt.

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
- **Testkonfiguration im Seed ist Platzhalter** (`scripts/seed.py`): Montag Ruhetag, 11:30–14:00 und 17:00–22:00 für alle Services, Kapazität 30 (mittags) / 40 (abends) Gäste im 30-Minuten-Raster, Wartezeit 20/45 min. Echte Werte kommen mit C1 und werden dann im Seed ersetzt
- Seed überschreibt `service_config` nie: der Live-Schalter (Modus, Lieferung, Wartezeit) gehört dem Team
- **Kapazität ohne Verweildauer** (`domain/reservations/capacity.py`): `capacity.max_guests` ist die Summe aller Gäste, deren Reservierung im Fenster beginnt; die Fenster bilden die Sitz-Turns ab (z. B. 18–20 und 20–22 Uhr). Entwürfe zählen mit, Stornierte und weich Gelöschte nicht. `check_slot` verlangt zusätzlich ein offenes `dinein`-Fenster (Sondertage greifen). Alternativen nur am selben Tag, im Raster `slot_minutes`, die zwei nächsten am Wunsch, nie in der Vergangenheit. In keinem Dokument definiert, Annahme vom 16.09.2026, kippbar
- Offene Entwürfe blockieren Kapazität, bis sie bestätigt oder storniert werden; ein Verfallsjob für liegengebliebene Entwürfe fehlt noch (Kandidat für `jobs/`)
- **`create_reservation` verlangt einen bekannten Anruf** (`domain/reservations/create.py`): die `calls`-Zeile muss vor dem ersten Schreibvorgang existieren (kommt mit T-1.9 über `POST /v1/calls/start`), sonst `not_found` mit Störungs-`say`. Kein stilles Anlegen, damit falsche IDs der Plattform sofort auffallen. Annahme vom 16.09.2026, kippbar
- **Entwurf prüft den Slot erneut** nach denselben Regeln wie `check_slot`; belegt → `conflict` mit den Alternativen im `say`. Idempotenz-Replay prüft nicht erneut und liefert die gespeicherte Antwort; derselbe Schlüssel unter einem anderen Mandanten → `conflict`
- **Prüfen und Anlegen sind gesperrt** (`_lock_business_day`): eine Advisory-Sperre je Mandant und Tag (`pg_advisory_xact_lock`) hält bis zum Ende der Transaktion, sonst lesen zwei gleichzeitige Anrufe dieselbe freie Kapazität und überbuchen das Fenster. Grob genug für Telefonlast, fein genug, dass verschiedene Tage sich nicht behindern. Mit dem gleichen Muster arbeitet später `confirm`
- **Readback-Format** (`domain/reservations/spoken.py`): „Ein Tisch für vier Personen heute / morgen / am Dienstag, den 22. September um halb sieben, auf den Namen Müller[, mit dem Hinweis: …]. Passt das so?" Personenzahl bis zwölf als Wort, darüber Ziffer. Bezugspunkt für „heute" und „morgen" ist `created_at` des Entwurfs, nicht die aktuelle Uhrzeit, damit ein Replay nach Mitternacht denselben Satz liefert. Wortlaut ist Vorschlag, wird im Dialogtest (D4) geschärft
- **Rufnummern** werden in `domain/customers/phone.py` nach E.164 normalisiert, Default-Land `+49`; `0049…`, `0…`, Leerzeichen, Schrägstriche und Klammern werden aufgelöst. Unterdrückte Nummer ist noch nicht modelliert (kommt mit `find_customer`)
- Jeder Schreibvorgang schreibt `audit_log` (`actor: agent`, `action: reservation.draft_created` bzw. `reservation.confirmed`), inline im Domain-Code. Zwei Stellen rechtfertigen noch keinen gemeinsamen Helfer; ab der dritten wieder prüfen
- **`confirm` ist über den Zustand idempotent, nicht über den Schlüssel** (`domain/confirm.py`): ein zweiter Aufruf liest `confirmed` und antwortet gleich, auch mit anderem `idempotency_key`; ein zweites Outbox-Ereignis entsteht nie. Der Schlüssel wandert nur ins `audit_log`. Das spart eine eigene Schlüsseltabelle; wenn später ein Vorgang wieder aus `confirmed` herausgehen kann, kippt diese Annahme. Annahme vom 17.09.2026, kippbar
- **`confirm` verlangt denselben Anruf wie der Entwurf** (`domain/confirm.py`): `reservation.call_id` muss zur `call_id` des Aufrufs passen, sonst `not_found`. Sonst bestätigt ein Anruf den Tisch eines anderen Gastes, und das „Ja" aus Regel 3 stammt von der falschen Person. Heute verliert das keinen Ablauf, weil die Entwurfs-UUID nur aus `create_reservation` desselben Anrufs kommt. Ein späteres „Gast ruft zurück und bestätigt" wäre ein eigener Weg mit eigener Prüfung. Codex-Review PR #90 (P1), 17.09.2026
- **Gleichzeitige `confirm`-Aufrufe werden per Zeilensperre serialisiert** (`SELECT … FOR UPDATE` auf die Reservierung), sonst schreiben zwei Aufrufe zwei Ereignisse in die Outbox und die Küche bekommt den Vorgang doppelt
- **`entity: "order"` steht im Vertrag, antwortet aber bis Stufe 2 mit `not_found`**: die Verzweigung in `domain/confirm.py` ist der Platz, an dem T-4.x die Bestellung ergänzt. `pickup_code` bleibt bei Reservierungen `null`
- **`approved` (Überlauf-Betrieb) fehlt noch bewusst:** `RESERVATION_STATUSES` kennt nur `draft`, `confirmed`, `cancelled`. Der Freigabe-Fluss kommt mit T-8.2
- **Backoff-Reihe und Versuchszahl** (`api/events/dispatcher.py`): 5 s, 30 s, 2 min, 10 min nach docs/03, danach `failed`. Das sind fünf Zustellversuche insgesamt; `attempts` zählt jeden Versuch, auch den erfolgreichen. Annahme vom 17.09.2026, kippbar
- **Alarm ist vorerst eine ERROR-Zeile im Log** (`dispatcher._on_failure`): ein Kanal zum Team (Telefon, Chat, GUI-Banner) existiert noch nicht. Sobald die GUI steht, gehört der Alarm zusätzlich dorthin. Annahme vom 17.09.2026, kippbar
- **Ein Ereignis je Transaktion, geholt mit `FOR UPDATE SKIP LOCKED`**: kein Ereignis geht doppelt raus, auch wenn zwei Dispatcher laufen, und ein langsamer HTTP-Aufruf hält keine fremden Zeilen fest. Die Reihenfolge richtet sich nach `next_attempt_at`, nicht nach dem Eingang
- **n8n wertet die Ereignis-id als Idempotenz-Schlüssel aus** (Kopfzeile `X-Idempotency-Key`, docs/03 §outbox): ein wiederholter Zustellversuch nach Timeout darf in der Küche keinen zweiten Bon erzeugen. Der Workflow, der das einlöst, fehlt noch
- **Migrationen schalten die App-Logger nicht mehr stumm** (`db/migrations/env.py`): `fileConfig(..., disable_existing_loggers=False)`. Vorher verstummte nach der ersten Migration im selben Prozess jeder bereits importierte Logger, auch im Betrieb nach `alembic upgrade` aus demselben Prozess
- **Lint-Regeln stehen in `pyproject.toml`, nicht im ruff-Default** (17.09.2026): ohne Konfiguration bestimmt die ruff-Version die Regelmenge, und ein Dependabot-Update fällt rot aus, ohne dass sich Code geändert hat (PR #85, ruff 0.7 auf 0.16: 14 Verstöße). Gesetzt sind `E, W, F, I, B, BLE, C4, UP, SIM, DTZ, RUF` ohne `E501` (die Zeilenlänge bestimmt der Formatter), dazu `extend-immutable-calls` für `Depends` und Geschwister, weil FastAPI den Aufruf im Default-Argument verlangt und B008 dort kein Fehler ist. Geprüft mit ruff 0.7.4 und 0.16.8, beide grün
- **Python bleibt auf 3.12, Dependabot-Sprünge auf das Image sind stillgelegt** (17.09.2026): die Version steht an sieben Stellen (Dockerfile, CI, `pyproject.toml`, README-Badge und -Text, `CLAUDE.md`, dieses Dokument, `CONTRIBUTING.md`); ein PR, der nur das Dockerfile anhebt (PR #82, 3.12 auf 3.14), bewegt eine davon und wird von der CI nicht geprüft, weil sie das Image nicht baut. 3.12 bekommt Sicherheits-Updates bis Oktober 2028. Der Sprung kommt als eigenes Arbeitspaket, wenn eine Abhängigkeit ihn verlangt oder das Support-Ende näher rückt, und bewegt dann alle Stellen zusammen plus einen `docker build`-Schritt in der CI
- **`create_callback` ist über den Zustand idempotent** (`domain/callbacks/create.py`): je Anruf gibt es höchstens einen offenen Rückruf, ein zweiter Aufruf liefert ihn zurück. Kein `idempotency_key` im Vertrag und keine zusätzliche Spalte; ein Zeitüberlauf der Plattform darf dem Team keine zwei Zettel für denselben Gast bringen. Ein erledigter Rückruf (`done`) blockiert einen neuen nicht. **Prüfen und Anlegen sind je Anruf gesperrt** (`pg_advisory_xact_lock`, wie beim Entwurf einer Reservierung): eine Sperre auf die Rückruf-Zeile greift beim ersten Rückruf ins Leere, weil es sie noch nicht gibt. Zum Vertrag gehört: jeder künftige Schreibpfad auf `callbacks` nimmt dieselbe Sperre, und die Datenbank fährt `READ COMMITTED` (seit 17.09.2026 in `api/db.py` festgelegt statt vom Serverdefault übernommen) — unter `REPEATABLE READ` sieht der Wartende den fremden Commit nicht und legt trotzdem ein zweites Mal an. Ein zweiter Aufruf aktualisiert den bestehenden Rückruf **nicht**: abweichende `summary`, `phone` oder `reason` werden verworfen. Codex-Review PR #95 (P1), roter Test zuerst; dazu ein kontrollierter Überlappungstest (A hält die Sperre, B wartet und liest danach A's Rückruf) und ein Rollback-Test. In docs/04 war keine Idempotenz-Regel für dieses Tool festgelegt, Annahme vom 17.09.2026, kippbar
- **Vorlesesatz nach dem Rückruf** (`SAY_NOTED`): „Ich habe Ihre Nummer notiert. Das Restaurant ruft Sie so bald wie möglich zurück." Wortlaut ist Vorschlag, wird im Dialogtest (D4) geschärft
- **Bugfix in `normalize_phone` (17.09.2026):** eine geklammerte `(0)` hinter der Landesvorwahl wurde bisher als Ziffer übernommen, `+49 (0)7221 5551234` ergab `+4907221 5551234` — eine Nummer, die es nicht gibt. Jetzt entfällt sie bei internationaler Schreibweise und bleibt als führende Null bei nationaler. Gefunden beim Bau von T-1.7, roter Test zuerst (`api/tests/test_domain_phone.py`)
- **E9 (gesetzt, 16.09.2026):** Alles läuft auf EU-Servern oder bei EU-Anbietern, auch Transkription und Auswertung. Maxis PC ist nur Werkbank zum Entwickeln.

---

## Offene Punkte aus Reviews

| Punkt | Warum noch offen | Wann fällig |
|---|---|---|
| Partieller Unique-Index auf `callbacks (tenant_id, call_id) WHERE status = 'open' AND deleted_at IS NULL`, plus Behandlung des Eindeutigkeitskonflikts als Replay | Heute schreibt genau ein Pfad auf `callbacks`, und der hält die Advisory-Sperre; der Index wäre der härtere Riegel, kostet aber eine Migration | spätestens bevor ein zweiter Schreibpfad auf `callbacks` entsteht (GUI-Freigabe, Import, Jobs) |
| Belastbare Latenzaussage unter Sperr-Konkurrenz | Die gemessenen rund 11 ms stammen aus Wiederholungen desselben Requests ohne Nebenläufigkeit; sie sagen nichts über Wartezeiten an der Sperre | mit dem ersten Lasttest, spätestens vor Gate G1 |

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
| 16.09.2026 | README-Status auf den tatsächlichen Stand synchronisiert |
| 16.09.2026 | Platzhalter statt Namen: `<Pilotbetrieb>`, `<Firmenname>`, `<Ort>`, `<Kassensystem>`, `<Kassenanbieter>`, `example.com` in Code, Doku, Mockup und Caddyfile; Regel in `CLAUDE.md` §1 |
| 16.09.2026 | **T-0.1 fertig:** `make up` gegen echtes Docker, Postgres + API healthy, n8n erreichbar (nach Fix `N8N_LISTEN_ADDRESS=0.0.0.0`), `/health` und `/v1/tools/ping` geprüft, `make lint` + `make test` im Container grün. Codex-Review (4 Findings) eingearbeitet. `ruff format` erstmals gelaufen (T-0.4: nur `make migrate` offen, wartet auf T-1.1) |
| 16.09.2026 | **T-0.2 fertig:** `api/db.py` mit Engine (`pool_pre_ping`), `SessionLocal`, `get_db` (Rollback bei Fehler, Close immer). Tests: Session arbeitet, Rollback bei Exception, DB nicht erreichbar liefert `service_unavailable`-Hülle statt Stacktrace. 7 Tests grün, lokal und im Container |
| 16.09.2026 | **T-0.6 fertig:** `api/core/` mit `envelope` (Hülle), `errors` (8 Codes aus 04 §1, `AppError` wird zentral übersetzt), `auth` (aus `main.py` gezogen), `logging` (JSON-Zeilen mit `request_id`/`call_id`, Middleware misst Dauer, `X-Request-ID` wird übernommen oder erzeugt), `time` (UTC/Ortszeit, Betriebstag, Sommerzeit-sichere Tagesgrenzen). `main.py` nutzt nur noch `core`. 34 Tests grün |
| 16.09.2026 | **T-1.1 fertig, T-0.4 fertig:** Alembic unter `db/` (`alembic -c db/alembic.ini`, URL aus `settings`), Migration 001 mit den zehn Stufe-1-Tabellen inkl. `outbox` und `audit_log`, Enums als CHECK-Constraints, `idempotency_key` unique, `deleted_at` auf Reservierungen und Rückrufen. Modelle unter `api/models/` (Base, Mixins für UUID-PK, `tenant_id`, Zeitstempel). Tests gegen eine Wegwerf-DB: up, Modelle ohne Diff zum Schema, down/up, doppelter Schlüssel, fehlende `call_id`, ungültiger `outbox.status`. `make migrate` läuft, Dev-DB auf 001. 42 Tests grün |
| 16.09.2026 | **T-1.2 fertig:** `scripts/seed.py` mit `seed(session, tenant_name, timezone)` und CLI (`make seed`, JSON-Ausgabe). Idempotent: Mandant und `service_config` nur bei Fehlen, Öffnungszeiten und Kapazität deterministisch ersetzt. Gemeinsame Wegwerf-DB-Fixtures in `api/tests/conftest.py`. Tests: Zähler, zweimal = gleich, Live-Schalter bleibt, zweiter Mandant, CLI. 47 Tests grün |
| 16.09.2026 | **T-1.3 fertig, T-0.5 fertig:** `domain/status/hours.py` (Fenster je Tag und Service, Sondertag schlägt Wochentag, Fenster über Mitternacht, nächste Öffnung) und `service.py` (`get_service_status`), Schemas `ToolRequest` und `ServiceStatus`, Tool-Router `tools/router.py` mit `tools/service_status.py`. 16 Tests: offen, Ruhetag mit `say`, zwischen den Fenstern, 00:30, Fenster 18–01 Uhr, Sondertag geschlossen, Sonderzeiten am Ruhetag, Sommerzeit Beginn und Ende, Lieferung pausiert, unbekannter Mandant, Hülle, 401, `invalid_input`, `not_found`. **Latenz:** p95 11,3 ms lokal, 12,3 ms im Container (Helfer `p95_ms`, Budget 300 ms), live per curl max 54,9 ms. Uvicorn-Access-Log abgeschaltet, die Middleware-Zeile hat Dauer und `request_id`. 63 Tests grün |
| 16.09.2026 | **T-1.4 fertig:** `domain/reservations/capacity.py` (Fenster je Wochentag, belegte Gäste aller Fenster in einer Abfrage), `slots.py` (`check_slot`: Öffnungszeit `dinein` und Kapazitätsfenster, bis zu zwei Alternativen im Raster, nächste zuerst), `spoken.py` (gesprochene Uhrzeit „halb sieben"). Schema `CheckSlotRequest` (zeitzonenbewusst, `party_size ≥ 1`), Tool `tools/check_slot.py`. 23 Tests: frei, voll mit Alternativen, kleine Gruppe passt noch, Entwurf zählt / Storno nicht, Gruppe größer als jedes Fenster, Ruhetag, Fensterende exklusiv, keine Alternativen in der Vergangenheit, Vergangenheit → `invalid_input`, naive Zeit → `invalid_input`, gesprochene Zeiten. **Latenz:** p95 13,0 ms lokal. 86 Tests grün |
| 16.09.2026 | **Übergabe Session 1 bis 3:** PR #2 gemerged (`main` = `df7c071`), Übergabeblock in `docs/00_PCF.md` §13. Nächster Schritt T-1.5. Stolpersteine für die Sandbox stehen dort (Docker-Daemon von Hand starten, lokale `.env`, `DATABASE_URL` auf localhost) |
| 16.09.2026 | **T-1.5 fertig:** `domain/reservations/create.py` (`create_reservation`: Mandant und Anruf prüfen, Rufnummer normalisieren, Slot erneut prüfen, Entwurf + `audit_log`, Idempotenz-Replay auch bei gleichzeitigem Doppelaufruf über den Unique-Index), `spoken.py` um Datum („heute", „morgen", Wochentag) und Personenzahl ergänzt, `core/ids.py` (`new_id`, deterministischer `idempotency_key`), `domain/customers/phone.py` (E.164), Schemas `CreateReservationRequest`/`ReservationDraft`, Tool `tools/create_reservation.py`. 40 Tests: Entwurf mit Audit, Replay ohne zweiten Vorgang, fremder Mandant, voller Slot mit Alternativen, Ruhetag, Vergangenheit, unbekannter Anruf, fremder Anruf, unbekannter Mandant, Rufnummer normalisiert/ungültig, leerer Name, drei Readback-Varianten, Entwurf zählt gegen Kapazität, HTTP-Hülle, Idempotenz über HTTP, fehlender Schlüssel, 401, 18 Rufnummern-Fälle, Schlüssel-Helfer. **Latenz:** p95 15,2 ms lokal, live per curl 3 bis 4 ms warm. 129 Tests grün |
| 16.09.2026 | **Codex-Review PR #2 (2 Findings) behoben, roter Test zuerst:** `check_slot` berücksichtigt Fenster des Vortags über Mitternacht (Wunsch 00:30 in einem Fenster 18–01 Uhr war fälschlich belegt); `get_service_status` zählt pausierte Lieferung nicht mehr als offen und verspricht Abholung nur bei offenem Abholfenster. 89 Tests grün |
| 17.09.2026 | **T-1.6 fertig:** `domain/confirm.py` (generisch, `draft → confirmed`, Zeilensperre `FOR UPDATE`, `audit_log`, Outbox-Ereignis `reservation.confirmed` mit vollständigem Vorgang im `payload`), Schemas `api/schemas/confirm.py`, Tool `tools/confirm.py`. 17 Tests: Bestätigung mit Audit und Ereignis, zweiter Aufruf ohne zweites Ereignis, storniert, weich gelöscht, unbekannt, fremder Mandant, unbekannter Anruf, unbekannter Mandant, `entity: order`, acht parallele Aufrufe legen genau ein Ereignis an, HTTP-Hülle, Idempotenz über HTTP, ungültige `entity`, fehlender Schlüssel, 401. **Latenz:** p95 9,9 ms (Schreibpfad), live per curl 14 ms kalt / 5,6 ms Replay. 148 Tests grün |
| 16.09.2026 | Auto-Update für diese Datei: `scripts/status_bump.py` (Semver, Datum, Changelog-Zeile automatisch), eingebunden in `/done`, `/task`, `/handover`; CI-Schritt „Status-Sync prüfen" schlägt an, wenn `docs/07_ARBEITSPAKETE.md` sich ändert, diese Datei aber nicht; `ruff format`-Altlast in `api/main.py` behoben; `CLAUDE.md`: kein Claude-Code-Attribution-Badge in PRs/Repo |

---

## Versionierung dieser Datei

Eigene, semantische Version `MAJOR.MINOR.PATCH`, unabhängig von der CLAUDE.md-Bundle-Version und von der Gate-Version der README (`docs/15_README_STRATEGY.md`) — die README versioniert das Produkt nach außen, diese Datei sich selbst nach innen:

| Bump | Auslöser | Beispiel |
|---|---|---|
| **MAJOR** | Gate bestanden / Stufenwechsel / Architektur-Entscheidung (E-Nr.) gekippt | G0 bestanden → Stufe 1 beginnt |
| **MINOR** | Entscheidung getroffen (D-Nr. beantwortet), neues Arbeitspaket-Ergebnis ändert „Was als Nächstes dran ist" | D1 Anbieter gewählt |
| **PATCH** | reine Status-Pflege: Task-Haken, neue Annahme, neuer/gelöster Blocker | T-0.1 auf fertig |

**Auto-Update:** `python scripts/status_bump.py <patch|minor|major> "<eine Zeile Änderung>"` setzt Datum, Version und Changelog-Zeile automatisch. Wird von `/done`, `/task` und `/handover` aufgerufen (siehe `.claude/commands/`) – von Hand nur bei Bedarf. Zusätzliches Sicherheitsnetz: CI (`.github/workflows/ci.yml`) schlägt fehl, wenn sich `docs/07_ARBEITSPAKETE.md` ändert, `docs/01_STATUS.md` im selben Diff aber unangetastet bleibt.

---

## Changelog

- **v1.6.2 · 17.09.2026:** Sperr-Vertrag fuer callbacks dokumentiert, READ COMMITTED in api/db.py festgelegt, Ueberlappungs- und Rollback-Test ergaenzt (Codex-Review PR #95)
- **v1.6.1 · 17.09.2026:** create_callback: Advisory-Sperre je Anruf gegen doppelte Rueckrufe bei gleichzeitigen Erstaufrufen (Codex-Review PR #95, P1)
- **v1.6.0 · 17.09.2026:** T-1.7 fertig: create_callback mit Zustands-Idempotenz, audit_log und Outbox-Ereignis; Bugfix in normalize_phone (geklammerte Null); naechster Schritt T-1.8
- **v1.5.2 · 17.09.2026:** Dependabot hebt die Python-Version des Containers nicht mehr allein an (PR #82 geschlossen); 3.12 bleibt gesetzt bis zum bewussten Upgrade
- **v1.5.1 · 17.09.2026:** Lint-Regeln in pyproject.toml festgeschrieben (select-Liste, extend-immutable-calls fuer FastAPI-Depends); Findings aus ruff 0.16 behoben, damit PR #85 gruen mergen kann
- **v1.5.0 · 17.09.2026:** T-1.12 fertig: events/ mit Outbox-Schreiber und Dispatcher nach n8n, Backoff und Alarm, eigener Dienst; Migrationen schalten App-Logger nicht mehr stumm; nächste Schritte T-1.7 bis T-1.9
- **v1.4.1 · 17.09.2026:** Codex-Review PR #90 (P1) behoben: confirm verlangt denselben Anruf wie der Entwurf, roter Test zuerst
- **v1.4.0 · 17.09.2026:** T-1.6 fertig: confirm generisch, draft nach confirmed mit Zeilensperre, audit_log und Outbox-Ereignis reservation.confirmed; nächster Schritt T-1.12 Dispatcher
- **v1.3.3 · 16.09.2026:** Repo-Standards: CONTRIBUTING, SECURITY, PR- und Issue-Vorlagen, Dependabot, README-Badges; Label-Taxonomie auf allen 77 Issues
- **v1.3.2 · 16.09.2026:** Roadmap auf GitHub gespiegelt: neun Block-Issues, 68 Arbeitspakete als Sub-Issues, Issue-Nummern in docs/07 eingetragen
- **v1.3.1 · 16.09.2026:** Codex-Review PR #4 (P1, P2) behoben: Sperre gegen Überbuchung bei parallelen Anrufen, readback stabil über Mitternacht
- **v1.3.0 · 16.09.2026:** T-1.5 fertig: create_reservation als draft mit readback, Idempotenz und audit_log; core/ids.py und E.164-Normalisierung neu; nächste Schritte T-1.6, T-1.12, T-1.7 bis T-1.9
- **v1.2.0 · 16.09.2026:** Merge PR #2: T-1.1 bis T-1.4 fertig (core/, Alembic, Seed, get_service_status, check_slot), Rebrand Maex Voice-Agent mit Platzhaltern, README-Strategie + CHANGELOG.md + /gate eingeführt, Codex-Review-Fixes
- **v1.1.1 · 16.09.2026:** Fahrplan-Abschnitt, Versionierungsschema und Auto-Update (Skript + CI-Sync-Check) eingeführt
- **v1.1.0 · 16.09.2026:** Bundle v1.1 – Lupe über den Plan: Module, Playbooks, Deployment, Importformat, Outbox, Agent-Kern, D7.
- **v1.0.0 · 15.09.2026:** Erstfassung beim Export nach Claude Code.

Ältere, nicht semver-versionierte Einträge (T-0.1 bis T-1.4, Rebrand, README-Strategie) stehen chronologisch in der Erledigt-Tabelle oben.
