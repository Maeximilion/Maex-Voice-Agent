# PCF – Maex Voice-Agent

> **KI-Telefonannahme für Lieferung, Abholung und Reservierung**
> Version 1.0 · 11.09.2026 · Status: freigegeben (Gate P bestanden)
> Diese Datei ist die Single Source of Truth für alle Chats des Projekts. Jeder Chat liest sie zu Beginn und endet mit einem Übergabeblock (Abschnitt 12).

---

## 1. Ziel

Ein KI-Agent nimmt Anrufe auf der Festnetznummer von <Pilotbetrieb> an. Er erledigt Reservierung, Abholung und Lieferung korrekt, übergibt an Küche, Kasse und Team und leitet Beschwerden an Menschen weiter. Die Oberfläche ist so einfach, dass das Team sie im Stress ohne Erklärung bedient.

### Leitregeln (First Principles)

Jeder Anruf durchläuft 5 Glieder: **Hören → Verstehen → Prüfen → Bestätigen → Übergeben.** Ein Fehler in einem Glied pflanzt sich bis in die Küche fort. Daraus folgen 6 Regeln:

1. **KI versteht, Code entscheidet.** Preise, Zonen, Zeiten, Verfügbarkeit und Allergene kommen nur aus der Datenbank.
2. **Nie raten.** Ohne eindeutige Menü-ID kommt keine Position in die Bestellung. Bei Unsicherheit greift die Verständnis-Leiter, am Ende der Mensch.
3. **Nichts ohne Bestätigung.** Vorlesen, „Ja" einholen, erst dann abschicken.
4. **Fehler werden gemessen.** Weiter geht es nur über Gates mit Zahlen.
5. **Jeder Ausfall endet beim Team.** Kein Anruf geht verloren.
6. **Token-sparsam by design.** Kleinster Kontext, kleinstes Modell, das die Evals besteht.

### KPIs

> Hinweis: Alle Zahlen in dieser PCF sind Richtwerte. Sie werden in G0 mit der Team-Baseline kalibriert.

| KPI | Definition | Richtwert |
|---|---|---|
| Genauigkeit | Vorgänge ohne nachträgliche Korrektur | ≥ Team-Baseline, Ziel ≥ 99 % |
| Unbestätigte Vorgänge | an Küche oder Reservierungsbuch ohne „Ja" | 0 |
| Geratene Positionen | Position ohne eindeutige Menü-ID | 0 |
| Eskalationsquote | Anrufe, die beim Team landen | sinkt von Stufe zu Stufe |
| Abbruchquote | Kunde legt vor dem Abschluss auf | ≤ Baseline |
| Antwortlatenz | Stille nach dem Kundensatz, bis die KI spricht | < 1,5 s |
| Kosten | € pro Anruf und pro Bestellung | ≤ Budget aus G0 |
| Verpasste Anrufe | Anrufe ohne Annahme | → 0 |

---

## 2. Scope

**Drin**
- Reservierung, Abholung, Lieferung (Zone, Pauschale, Mindestbestellwert)
- Auskünfte aus der Konfiguration: Öffnungszeiten, Liefergebiet, Wartezeit, Allergene
- Beschwerde oder Wunsch nach einem Menschen → Weiterleitung oder Rückruf-Aufgabe
- GUI für Betrieb (Tablet) und Admin
- Einlernphase (Offline-Schattenmodus) und Eval-Suite

**Bewusst draußen (später prüfbar)**
- Zahlung am Telefon: Bezahlt wird bei Abholung oder Lieferung
- Änderung oder Storno laufender Bestellungen → Team
- Weitere Sprachen: Deutsch zuerst
- Weitere Betriebe: Die Architektur hält es offen
- Anrufe, die die KI selbst startet

---

## 3. Entscheidungen

| # | Entscheidung | Grund | Status |
|---|---|---|---|
| E1 | **Hybrid:** Telefonie und Sprachverarbeitung über einen EU-gehosteten Spezialdienst; Logik, DB und GUI in eigener Hand | Schwerste Technik (Echtzeit-Audio, Latenz) wird eingekauft, die fehlerkritische Logik selbst gebaut, Bausteine bleiben austauschbar | gesetzt 11.09.2026 |
| E2 | Stufen: Reservierung → Abholung → Lieferung | Jede Stufe bringt genau eine neue Schwierigkeit | Annahme, in G0 per Anruf-Mix bestätigen |
| E3 | Rollout: Schatten → Überlauf → Hauptannahme | Das Risiko wächst nur mit Belegen | Annahme, gilt bis Veto |
| E4 | Kasse bleibt Buchungs-Master (TSE); Agent-DB nur für Agent-Daten | Rechtssicherheit, eine einzige Wahrheit für Umsätze | Annahme, gilt bis Veto |
| E5 | Pilot <Pilotbetrieb>, Nummer bleibt; Menü, Zeiten, Zonen aus der DB | Später auf andere Betriebe übertragbar | Annahme, gilt bis Veto |
| E6 | Einlernen = Wissensbasis + Eval-Suite + Offline-Schattenmodus, kein Modelltraining | Billiger, messbar, rechtlich schlanker | Annahme, gilt bis Veto |
| E7 | KI gibt sich zu Gesprächsbeginn als KI zu erkennen | AI Act Art. 50, gilt seit 02.08.2026 | Pflicht |
| E8 | Beschwerde, Mensch-Wunsch, Storno → sofort Team, sonst Rückruf-Aufgabe | Vertrauen, Fehlerbegrenzung | Annahme, gilt bis Veto |

---

## 4. Architektur (Hybrid)

```text
Anrufer
  │
  ▼
Festnetz <Pilotbetrieb> (bestehender Anbieter)
  │  Umleitung / SIP
  │  Modus: Schatten · Überlauf · Primär
  ▼
Voice-Plattform (EU)
  │  Spracherkennung → Modell → Stimme
  │  Tastentöne · Unterbrechen · Weiterleitung
  │
  │  Tools über HTTPS (heißer Pfad)
  ▼
Agent-API ─────────► Agent-DB (EU)
  │                      ▲
  │  Ereignisse          │
  │  (kalter Pfad)       │
  ▼                      │
n8n ── Küche / Kasse     │
    ── SMS, Logs, Alarme │
                         │
GUI (Browser) ───────────┘
  Betrieb-Tablet · Admin

Team-Durchwahl ◄── Weiterleitung, Rückrufe
EU-Server ────────  Transkription der Einlern-Aufnahmen (nachts)
```

### Heißer und kalter Pfad

- **Heiß** heißt: Der Kunde wartet am Telefon. Dazu gehören Menüsuche, Kunde per Nummer, Zonen-Check, Slot-Check und Bestellprüfung. Das muss schnell sein (Richtwert < 300 ms je Tool) und darf nie hängen → direkte Abfragen über eine kleine Agent-API.
- **Kalt** heißt: Alles nach dem „Ja". Dazu gehören Bon oder Kasse, SMS, Logs und Statistik. Das darf Sekunden dauern, braucht aber Wiederholung bei Fehlern → n8n.
- n8n-Webhooks kommen nur dann in den heißen Pfad, wenn der PoC die Latenz belegt. Erst messen, dann entscheiden.

### Tools des Agenten (Entwurf)

| Tool | Zweck | Deterministische Prüfung |
|---|---|---|
| `get_service_status` | offen/zu, Lieferung an/aus, Wartezeit, ausverkaufte Gerichte | Konfiguration |
| `find_customer` | Name und gespeicherte Adressen zur Anrufernummer | nur bei übermittelter Nummer |
| `search_menu` | Top-3-Treffer mit ID, Preis, Optionen | Nummer exakt, sonst Alias-Tabelle |
| `get_item_details` | Optionen, Extras, Allergene | nur DB-Werte |
| `check_delivery` | Zone, Pauschale, Mindestbestellwert, Lieferzeit | PLZ oder Polygon |
| `check_slot` | Tisch frei, sonst Alternativen | Kapazität je Zeitfenster |
| `create_reservation` | Reservierung als Entwurf | Pflichtfelder vollständig |
| `draft_order` | Bestellung prüfen, Summe rechnen, Vorlesetext liefern | Preise, Zone, Mindestbestellwert, Öffnungszeit |
| `confirm` | nach dem „Ja" final → kalter Pfad | Idempotenz: genau einmal |
| `create_callback` | Rückruf-Aufgabe mit Zusammenfassung | – |
| `transfer_to_team` | Weiterleitung an die Team-Durchwahl | Schleifenschutz |

---

## 5. Stufen & Gates

Stufen sind die Zeitachse, Chats die Arbeitspakete (Abschnitt 10). Jede Stufe liefert einen echten, testbaren Durchstich.

| Stufe | Ziel | Gate | Größe |
|---|---|---|---|
| 0 Fundament | Fakten, Recht, Budget, Anbieter | G0 Go/No-Go | M |
| 1 Durchstich Reservierung | ganze Kette einmal echt | G1 | M |
| 2 Abholung | Menü sicher bis in die Küche | G2 | L |
| 3 Lieferung | Adresse und Zone ohne Fehler | G3 | L |
| 4 Einlernen & Schattenmessung | echte Fehlerquote ohne Kundenrisiko | G4 | M |
| 5 Überlauf-Betrieb | KI nur, wenn niemand abnimmt | G5 | M |
| 6 Hauptannahme & Betrieb | KI zuerst, laufende Pflege | Monats-Review | laufend |

### Stufe 0 – Fundament
**Ziel:** Fakten, Rechtsrahmen, Budget und Anbieter klären, bevor Code entsteht.
- **C1** Ist-Aufnahme: Kasse und Schnittstelle, Bondrucker, Telefonanbieter und Router, Anrufvolumen und Stoßzeiten, verpasste Anrufe, Anruf-Mix, Menüformat, Reservierungsablauf, Team-Abläufe
- **C1** Baseline messen: Fehler und Reklamationen je 100 Telefonbestellungen (2 Wochen Strichliste), verpasste Anrufe
- **C1** Rechts-Check (Abschnitt 8) vorbereiten, Ansagetexte entwerfen
- **C2** Anbieter-Shortlist nach Abschnitt 6, Kostenmodell, Telefonie-Weg skizzieren
- **Hypothese Business Case:** Der größte Hebel sind verpasste Anrufe in Stoßzeiten. Das wird mit der Anrufliste belegt.

**Gate G0 – Go/No-Go:** Budget freigegeben · Rechtsrahmen geklärt · Telefonie-Weg machbar · Anbieter für den PoC gewählt · Stufenreihenfolge per Anruf-Mix bestätigt

### Stufe 1 – Durchstich Reservierung
**Ziel:** Die Kette Testnummer → KI → Tool → DB → GUI läuft einmal komplett echt. Die Reservierung ist dafür das Vehikel, weil sie wenige Daten braucht und kein Geld berührt.
- **C2** PoC auf Testnummer: Latenz, Anrufer-ID bei Umleitung, Tastentöne, Weiterleitung, Unterbrechen
- **C3** Minimal-Schema: Konfiguration, Öffnungszeiten, Kapazität, Reservierungen, Anrufe, Rückrufe
- **C4** Dialog Reservierung, KI-Hinweis, Mensch-Wunsch, Rückruf, System-Prompt v1
- **C5** Tools `get_service_status`, `check_slot`, `create_reservation`, `create_callback`; Anruf-Log; Fehler-Workflow mit Benachrichtigung
- **C6** GUI Betrieb v0: Reservierungen heute, Rückrufe, Schalter „KI pausieren"
- **C7 parallel** (nach Rechtsfreigabe und Test): Aufzeichnung echter Team-Anrufe mit Einwilligung starten. Ab jetzt sammeln sich Daten im Hintergrund.

**Gate G1:** 20 Rollenspiel-Anrufe (Lärm, Dialekt, Unterbrechen, Mensch-Wunsch) → 100 % korrekt gebucht oder sauber eskaliert · Latenz im Richtwert · Kosten je Anruf gemessen · Ausfalltest: Plattform weg → Anruf landet beim Team

### Stufe 2 – Abholung
**Ziel:** Das Menü wird sicher verstanden, die Bestellung kommt korrekt in der Küche an.
- **C3** Menü-Import (Nummern, Varianten, Extras, Preise, Allergene), Alias-Tabelle, Suche
- **C4** Bestelldialog, Verständnis-Leiter, Allergie-Regel, Vorlesen + „Ja"
- **C4** Eval-Suite v1 (≥ 100 Fälle aus Aufnahmen und Rollenspielen), Modellwahl per Eval
- **C5** `draft_order`, `confirm`, Übergabe an Küche/Kasse (Schnittstelle oder Netzwerk-Bon), Idempotenz, Wiederholung bei Fehlern
- **C6** GUI: neue Bestellungen mit „OK/Korrigieren", „Gericht aus", Wartezeit-Regler

**Gate G2:** Eval v1 ≥ Zielgenauigkeit · 0 geratene Positionen · 0 unbestätigte Bestellungen · Preise identisch mit der Kasse (Abgleich-Test) · 30 Rollenspiel-Bestellungen fehlerfrei in der Küche

### Stufe 3 – Lieferung
**Ziel:** Adresse und Zone ohne Fehler.
- **C3** Kunden, Adressen, Zonen (PLZ oder Polygone aus der neuen Liefergebiets-Kalkulation), Pauschalen, Mindestbestellwert
- **C4** Adressdialog: „Wieder an …?" per Anrufer-ID, Straßennamen buchstabieren, Hausnummer per Tastatur, außerhalb der Zone → Abholung anbieten
- **C5** `check_delivery`, `find_customer`; SMS-Zusammenfassung optional (eigenes Gate wegen Kosten und Außenwirkung)
- **C6** GUI: Lieferaufträge, Schalter „Lieferung pausieren"
- **C4** Eval-Suite v2 mit Adressfällen

**Gate G3:** Eval v2 ≥ Ziel · Zonen-Check an Grenzfällen 100 % korrekt · 20 Rollenspiel-Lieferungen korrekt · Löschkonzept für die Kunden-DB umgesetzt

### Stufe 4 – Einlernen & Schattenmessung
**Ziel:** Die echte Fehlerquote unter Realbedingungen messen, ohne dass ein Kunde mit der KI spricht.
- **C7** Aufnahmen (seit Stufe 1) auf dem EU-Server transkribieren (Whisper-Container oder EU-Dienst mit AVV) → KI extrahiert den Vorgang als JSON → Abgleich mit Kasse/Bon
- **C7** Fehler-Taxonomie: Hören · Verstehen · Menü · Adresse · Regel · Dialog → Fixes in C3/C4
- **C4** Jeder echte Fehler wird ein neuer Eval-Fall; Aliase und FAQ aus echten Anrufen ergänzen

**Gate G4:** ≥ 200 echte Anrufe ausgewertet · KI-Extraktion ≥ Team-Baseline · keine offene kritische Fehlerklasse (Allergene, Adresse, Preis) · Rechts-Check bestätigt

> Der Offline-Schattenmodus misst das **Verstehen** mit einem Bruchteil der Technik, die Live-Mithören bräuchte. Die **Gesprächsführung** messen die Rollenspiele (G1–G3) und der Überlauf-Betrieb (G5).

### Stufe 5 – Überlauf-Betrieb
**Ziel:** Die KI nimmt echte Anrufe an, aber nur, wenn das Team nicht abnimmt. Das Team gibt jede KI-Bestellung frei.
- **C8** Telefonie-Modus Überlauf (nach X Sekunden Klingeln oder zu Stoßzeiten), Team-Schulung (15 Minuten, 1 Seite), Notfall-Runbook
- **C6** Freigabe-Knopf je Bestellung, Ton-Alarm bei Rückrufen
- **C5** Kosten-Alarm, Tagesbericht

**Gate G5:** ≥ 2 Wochen und ≥ 100 KI-Bestellungen · Genauigkeit ≥ Ziel · Beschwerden ≤ Baseline · Kosten ≤ Budget · Team-Feedback positiv → der Freigabe-Knopf darf weg

### Stufe 6 – Hauptannahme & Betrieb
- Die KI nimmt zuerst an, das Team bleibt über die Durchwahl erreichbar
- Vor jeder Änderung an Prompt, Menü-Logik oder Modell läuft die Eval-Suite als Regressionstest
- Monats-Review: KPIs, Kosten, neue Fehlerklassen, Anbieterpreise

---

## 6. Anbieter-Kriterien (C2)

**Muss**
- Datenverarbeitung in der EU, AVV verfügbar
- Gute deutsche Spracherkennung und Stimmen
- Tool-Aufrufe per HTTPS an eigene Logik
- Deutsche Nummer oder SIP-Anbindung · Weiterleitung · Tastentöne · Anrufernummer an die Tools
- Kunde kann die KI unterbrechen, Latenz im Richtwert
- Aufzeichnung steuerbar (nur mit Einwilligung)

**Soll**
- Eigenes Vokabular für Gerichtnamen (Keyword-Boost)
- Modell frei wählbar, Prompt-Caching
- Transparente Kosten pro Minute, geringe Grundgebühr
- Genug gleichzeitige Anrufe für die Sonntagsspitze
- Export von Transkripten und Anruf-Metadaten per API
- Test- oder Simulationsmodus

**Methode:** Punkte-Matrix → Top 2 → gleicher PoC mit denselben 20 Testanrufen → Entscheidung per Messung.

---

## 7. Datenmodell (Entwurf für C3)

| Bereich | Tabellen |
|---|---|
| Betrieb | `service_config`, `opening_hours`, `special_days`, `capacity` |
| Menü | `menu_items`, `item_options`, `item_allergens`, `item_aliases` |
| Kunden | `customers`, `addresses` |
| Lieferung | `delivery_zones` |
| Vorgänge | `reservations`, `orders`, `order_items`, `callbacks` |
| Qualität | `calls`, `eval_cases`, `eval_runs` |
| Nachvollziehbarkeit | `audit_log` |

**Regeln**
- Preise in Cent als Integer, Telefonnummern im Format E.164
- Jeder Vorgang trägt eine `call_id`
- Aufnahmen und Transkripte bekommen ein Löschdatum
- Menü-Master ist die Kasse, falls exportierbar; sonst die Agent-DB plus wöchentlicher Abgleich-Check
- Empfehlung DB-Technik: PostgreSQL in der EU (Entscheidung in C3)

---

## 8. Rechts-Check (C1, vor dem ersten echten Anruf)

> Hinweis: Keine Rechtsberatung. Vor Go-live durch Anwalt oder Datenschutzberater prüfen lassen.

- [ ] **AI Act Art. 50:** KI-Hinweis zu Gesprächsbeginn, gilt seit 02.08.2026 ([Quelle](https://www.ai-ops-engine.com/blog/eu-ai-act-digital-omnibus-fristen))
- [ ] **Aufzeichnung:** Einwilligung von Kunde und Team (§201 StGB); Ansage und Weg zum Widersprechen
- [ ] **DSGVO:** Rechtsgrundlage je Zweck (Bestellung · Aufnahme · Auswertung) · Informationspflicht (kurze Ansage + Datenschutzerklärung auf example.com) · AVV mit allen Dienstleistern · Drittlandtransfer prüfen · Löschkonzept · Verzeichnis der Verarbeitungstätigkeiten · Datenschutz-Folgenabschätzung prüfen
- [ ] **Team:** informieren, Einwilligung oder Vereinbarung zu Aufnahmen
- [ ] **Allergene (LMIV):** Auskunft nur aus gepflegten DB-Werten, sonst Rückruf durch das Team
- [ ] **Kasse (TSE):** KI-Bestellungen werden ordnungsgemäß in der Kasse gebucht

---

## 9. Risiken (Pre-Mortem)

| # | Risiko | Gegenmaßnahme | Stufe |
|---|---|---|---|
| R1 | Latenz oder unnatürliche Stimme → Kunden legen auf | Latenz im PoC messen, Anbieter danach wählen | 1 |
| R2 | Gerichtnamen oder Dialekt falsch verstanden | Vokabular, Nummern, Tastatur, Evals mit echten Aufnahmen | 2 |
| R3 | Internet oder Plattform fällt zur Stoßzeit aus | Fallback aufs Team, Alarm, „KI pausieren" | 1 |
| R4 | Aufnahme oder KI-Hinweis rechtlich angreifbar | Rechts-Check vor G0, geprüfte Ansagetexte | 0 |
| R5 | Menü oder Preise weichen von der Kasse ab | ein Master, Abgleich-Test, „Gericht aus" | 2 |
| R6 | Falsche Allergen-Auskunft | nur DB-Werte, sonst Rückruf durch das Team | 2 |
| R7 | Kosten laufen weg (Spam, Schleifen, lange Anrufe) | Maximaldauer, Schleifenschutz, Kosten-Alarm | 1 |
| R8 | Anrufer-ID geht bei der Umleitung verloren | im PoC testen; Fallback: Nummer abfragen | 1 |
| R9 | Eine Änderung verschlechtert unbemerkt die Qualität | Regression-Evals, Versionierung | 2 |
| R10 | Team nutzt die GUI nicht | 2-Tap-Regel, Team in Rollenspiele einbinden | 1 |
| R11 | Anbieter-Lock-in oder Preiserhöhung | Logik, Prompts und Evals bei dir, Tools per Webhook | 0 |
| R12 | Doppelbestellung durch Wiederholung oder Netzfehler | Idempotenz-Schlüssel je Bestellung | 2 |

---

## 10. Chats & Start-Prompts

| Chat | Arbeitspaket | Stufen | Liefert |
|---|---|---|---|
| C0 | Steuerung | alle | Plan, Gates, Entscheidungen, PCF-Updates |
| C1 | Ist-Aufnahme & Recht | 0 | Fakten, Baseline, Rechts-To-dos, Ansagetexte |
| C2 | Architektur, Telefonie & Anbieter | 0–1, 5 | Anbieterwahl, Kostenmodell, Routing, PoC |
| C3 | Datenmodell & Agent-API | 1–3 | Schema, Import, Tools im heißen Pfad |
| C4 | Dialog, Prompts & Evals | 1–4 | Gesprächsflüsse, System-Prompt, Eval-Suite |
| C5 | Integration mit n8n | 1–5 | Küche/Kasse, Rückrufe, Logs, Alarme |
| C6 | GUI | 1–5 | Betrieb-Tablet, Admin |
| C7 | Einlernen & Qualität | 1–4 | Aufnahmen, lokale Transkription, Schattenmessung |
| C8 | Rollout & Betrieb | 5–6 | Runbook, Schulung, Monitoring, Review |

**Reihenfolge:** C1 + C2 parallel → G0 → je Stufe C3 → C4 → C5 → C6 · C7 läuft ab Stufe 1 im Hintergrund · C8 ab Stufe 5

### So arbeitest du mit den Chats
1. Neuer Chat in diesem Claude-Projekt → Start-Prompt des Pakets einfügen
2. Der Chat liest die aktuelle PCF aus Google Drive, oder du hängst sie an
3. Am Ende liefert der Chat einen Übergabeblock → in Abschnitt 13 einfügen, Version +0.1
4. Gates und neue Entscheidungen meldest du in C0 → C0 aktualisiert die Abschnitte 3 und 5
5. Wird ein Chat lang: Übergabeblock erzeugen und im frischen Chat weitermachen

### C1 – Ist-Aufnahme & Recht
```text
Projekt Maex Voice-Agent · Chat C1 – Ist-Aufnahme & Recht (Stufe 0)
Lies zuerst die aktuelle „PCF – Maex Voice-Agent" (Google Drive oder Anhang). E1 und E7 sind gesetzt, alle anderen Entscheidungen gelten bis Veto.
Arbeitsweise: code-autopilot · Rückfragen geschlossen mit markierter Empfehlung, eine pro Unterbrechung · Recherchierbares selbst recherchieren.

Ziel: alle Fakten und rechtlichen Grundlagen für Gate G0.
1. Kasse: Hersteller, Schnittstelle oder Export, Bondrucker im Netzwerk?
2. Telefonie: Anbieter, Router oder Anlage, Leitungen, Rufumleitung möglich?
3. Anrufe: pro Tag, Stoßzeiten, verpasste Anrufe, Mix aus Bestellung, Reservierung, Frage, Beschwerde
4. Menü: Quelle und Format, Nummerierung, Varianten, Allergene gepflegt?
5. Reservierung: heutiger Ablauf, Kapazität
6. Team: Wer nimmt ab? Durchwahl oder Handy für Weiterleitungen?
7. Baseline: Messplan für Fehler je 100 Telefonbestellungen (2 Wochen)
8. Recht: PCF-Abschnitt 8 abarbeiten, Ansagetexte entwerfen (KI-Hinweis, Aufnahme-Einwilligung), offene Punkte für Anwalt oder Datenschutzberater sammeln
9. Budget: Obergrenze laufend pro Monat und einmalig

Output: Faktenliste · Baseline-Messplan · Ansagetexte · Rechts-To-dos
Ende: Übergabeblock nach PCF-Abschnitt 12.
```

### C2 – Architektur, Telefonie & Anbieter
```text
Projekt Maex Voice-Agent · Chat C2 – Architektur, Telefonie & Anbieter (Stufe 0–1)
Lies zuerst die aktuelle „PCF – Maex Voice-Agent" (Google Drive oder Anhang). E1 Hybrid ist gesetzt.
Arbeitsweise: code-autopilot · Rückfragen geschlossen mit markierter Empfehlung · Verträge, Kosten und API-Schlüssel nur nach Freigabe.

Ziel: Voice-Plattform wählen und den ersten Testanruf durchstechen.
1. Aktuelle Anbieter recherchieren und nach PCF-Abschnitt 6 bewerten → Top 2
2. Kostenmodell: Anrufe pro Tag × Ø Minuten × € pro Minute + Fixkosten (Zahlen aus C1)
3. Telefonie-Routing: Modi Schatten/Überlauf/Primär, Team-Durchwahl, Schleifenschutz, Fallback bei Ausfall, Aufzeichnungsweg
4. Hosting in der EU für Agent-API, DB und n8n
5. PoC auf Testnummer: Latenz, Anrufer-ID bei Umleitung, Tastentöne, Weiterleitung, Unterbrechen

Output: Bewertungsmatrix · Kostenmodell · Routing-Skizze · PoC-Protokoll
Gate: Anbieter und Hosting freigegeben (Teil von G0)
Ende: Übergabeblock nach PCF-Abschnitt 12.
```

### C3 – Datenmodell & Agent-API
```text
Projekt Maex Voice-Agent · Chat C3 – Datenmodell & Agent-API (Stufe 1–3)
Lies zuerst die aktuelle „PCF – Maex Voice-Agent" (Google Drive oder Anhang), besonders Abschnitt 4 und 7.
Arbeitsweise: code-autopilot Build-Loop · Umsetzung idealerweise in Claude Code, damit alles echt ausgeführt wird.

Ziel: Agent-DB und schnelle Tools für den heißen Pfad, Stufe für Stufe.
Stufe 1: Konfiguration, Öffnungszeiten, Kapazität, Reservierungen, Anrufe, Rückrufe · Entscheidung DB-Technik
Stufe 2: Menü, Optionen, Allergene, Aliase · Import aus Kasse oder Menü · Suche
Stufe 3: Kunden, Adressen, Lieferzonen · Zonen-Check (PLZ oder Polygon)
Regeln: Preise in Cent · E.164 · audit_log · Löschdaten · versionierte Migrationen · Backup vor jeder Änderung

Gate je Stufe: Tool-Antwort < 300 ms · Tests für Grenzfälle grün
Ende: Übergabeblock nach PCF-Abschnitt 12.
```

### C4 – Dialog, Prompts & Evals
```text
Projekt Maex Voice-Agent · Chat C4 – Dialog, Prompts & Evals (Stufe 1–4)
Lies zuerst die aktuelle „PCF – Maex Voice-Agent" (Google Drive oder Anhang). Die Leitregeln in Abschnitt 1 sind bindend.
Arbeitsweise: code-autopilot Prompt-Werkstatt · eine Variable pro Iteration · jede Prompt-Version mit Eval-Lauf.

Ziel: Gesprächsflüsse, System-Prompt, Tool-Beschreibungen und Eval-Suite, die G1 bis G3 bestehen.
Dialog: Begrüßung mit KI-Hinweis · Absicht erkennen · Pflichtangaben je Vorgang · Verständnis-Leiter (nachfragen → buchstabieren → Tastatur → SMS → Rückruf) · Vorlesen + „Ja" · Allergene nur aus der DB · Beschwerde, Mensch-Wunsch, Storno → Team · Maximaldauer
Token: kurzer System-Prompt · Menü-Index statt ganzem Menü · Details per Tool · kompakter Bestellstatus statt Verlauf · kleinstes Modell, das die Evals besteht
Evals: Fall = Transkript → erwartetes JSON · Metriken aus PCF-Abschnitt 1 · Regression vor jeder Änderung
Entscheidung in Stufe 1: Stimme natürlich oder synthetisch

Ende: Übergabeblock nach PCF-Abschnitt 12.
```

### C5 – Integration mit n8n
```text
Projekt Maex Voice-Agent · Chat C5 – Integration mit n8n (Stufe 1–5)
Lies zuerst die aktuelle „PCF – Maex Voice-Agent" (Google Drive oder Anhang), besonders Abschnitt 4.
Arbeitsweise: code-autopilot · n8n self-hosted per Docker · Python nur, wo n8n nicht reicht.

Ziel: Der kalte Pfad läuft zuverlässig. Bestätigte Vorgänge landen in Küche und Kasse, Rückrufe beim Team.
Inhalte: Webhooks der Plattform · Küche/Kasse (Schnittstelle oder Netzwerk-Bon) · Idempotenz · Retry und Fehler-Workflow mit Benachrichtigung · Kosten- und Ausfall-Alarm · Tagesbericht · SMS optional (eigenes Gate)

Gate je Stufe: Ende-zu-Ende-Test · Ausfalltest (Plattform, DB oder n8n weg)
Ende: Übergabeblock nach PCF-Abschnitt 12.
```

### C6 – GUI
```text
Projekt Maex Voice-Agent · Chat C6 – GUI (Stufe 1–5)
Lies zuerst die aktuelle „PCF – Maex Voice-Agent" (Google Drive oder Anhang).
Arbeitsweise: code-autopilot · Umsetzung idealerweise in Claude Code.

Ziel: eine Oberfläche, die das Team im Stress ohne Erklärung bedient.
Betrieb (Tablet): neue Bestellungen mit OK/Korrigieren · Reservierungen heute · Rückrufe mit Ton · Schalter: KI pausieren, Lieferung pausieren, Wartezeit +15/+30, Gericht aus
Admin (PC): Menü und Aliase · Zeiten und Sondertage · Zonen · Anruf-Log · KPIs und Kosten · Eval-Ergebnisse · Daten löschen
Regeln: jede Aktion ≤ 2 Taps · große Schrift · keine Fachbegriffe · Live-Aktualisierung

Gate: Technik-Entscheidung in Stufe 1 · ein Teammitglied bedient die Betrieb-Ansicht 5 Minuten ohne Erklärung
Ende: Übergabeblock nach PCF-Abschnitt 12.
```

### C7 – Einlernen & Qualität
```text
Projekt Maex Voice-Agent · Chat C7 – Einlernen & Qualität (ab Stufe 1, Messung in Stufe 4)
Lies zuerst die aktuelle „PCF – Maex Voice-Agent" (Google Drive oder Anhang). Start erst nach der Rechtsfreigabe (Abschnitt 8).
Arbeitsweise: code-autopilot · Verarbeitung ausschließlich auf dem EU-Server, nichts auf Maxis PC.

Ziel: aus echten Anrufen lernen und die echte Fehlerquote messen, ohne Kundenrisiko.
Pipeline: Aufnahme mit Einwilligung → Transkription auf dem EU-Server → KI extrahiert den Vorgang als JSON → Abgleich mit Kasse/Bon → Fehler-Taxonomie → neue Aliase, FAQ und Eval-Fälle → Löschen nach Frist

Gate G4: ≥ 200 Anrufe · KI ≥ Team-Baseline · keine kritische Fehlerklasse offen
Ende: Übergabeblock nach PCF-Abschnitt 12.
```

### C8 – Rollout & Betrieb
```text
Projekt Maex Voice-Agent · Chat C8 – Rollout & Betrieb (Stufe 5–6)
Lies zuerst die aktuelle „PCF – Maex Voice-Agent" (Google Drive oder Anhang). Voraussetzung: G1 bis G4 bestanden.
Arbeitsweise: code-autopilot · jede Umschaltung mit Rückweg.

Ziel: sicherer Wechsel vom Überlauf zur Hauptannahme und stabiler Betrieb.
Inhalte: Modus-Umschaltung und Zeitfenster · Team-Schulung (15 Minuten, 1 Seite) · Notfall-Runbook (Plattform, Internet, DB weg) · Monitoring und KPIs · Freigabe-Knopf nach G5 abschalten · Monats-Review

Gate: G5, danach monatlicher Review
Ende: Übergabeblock nach PCF-Abschnitt 12.
```

---

## 11. Repo & Versionierung

```text
maex-voice-agent/
├── api/             Tools im heißen Pfad
├── db/migrations/   versionierte Schema-Änderungen
├── n8n/             Workflow-Exporte mit Datum und Version
├── prompts/         System-Prompt je Version
├── evals/           Testfälle und Auswertung
├── gui/
├── docker-compose.yml
├── .env.example
└── README.md
```

- Conventional Commits, `main` bleibt lauffähig
- `.env`, echte Aufnahmen und Kundendaten kommen nie ins Repo
- Jede Prompt-Version bekommt einen Eval-Lauf, das Ergebnis steht im Commit

---

## 12. Übergabeblock (Vorlage für jedes Chat-Ende)

```text
## Übergabe <Datum> – C<Nr> <Thema>
Stand: <was läuft, was nicht>
Artefakte: <Dateien, Links>
Entscheidungen: <E-Nr · Entscheidung · Grund>
Gate: <bestanden | offen + was fehlt>
Offen: <nächster konkreter Schritt zuerst>
Stolpersteine: <Lernpunkte>
```

---

## 13. Übergaben (neueste oben)

```text
## Übergabe 16.09.2026 – T-0.1 bis T-0.6, T-1.1 bis T-1.4
Stand: Stack läuft (Postgres, API, n8n), Schema 001 mit zehn Stufe-1-Tabellen, Seed idempotent,
       Tools get_service_status und check_slot mit p95 10 bis 14 ms; 89 Tests grün, CI grün.
       Schreibende Tools (create_reservation, confirm, create_callback, transfer_to_team), Anruf-Log,
       Prompt, Agent-Kern und GUI fehlen.
Artefakte: PR #1 und #2 auf main gemerged (main = df7c071). Branch claude/new-session-c3waic = main.
       api/core, api/db.py, api/models, api/domain/{status,reservations}, api/tools, db/, scripts/seed.py.
Entscheidungen: Platzhalter statt Namen (CLAUDE.md §1) · Empfehlungen werden direkt abgenommen (§6) ·
       Betriebstag ab 05:00 · Fachfehler HTTP 200 in der Hülle, nur Auth 401 · Kapazität ohne Verweildauer,
       Fenster = Sitz-Turns, Entwürfe zählen · Seed-Werte Platzhalter bis C1 · Seed überschreibt Live-Schalter nie.
Gate: G0 offen. Fehlt: Anbieter (C2), Rechts-Check, Budget, Ist-Aufnahme (C1).
Offen: T-1.5 create_reservation (Entwurf, readback, Idempotenz, core/ids.py) → T-1.6 confirm mit audit_log
       und Outbox → T-1.7 create_callback → T-1.8 transfer_to_team → T-1.9 Anruf-Log. Parallel: T-0.7, T-0.8.
Stolpersteine: Claude-Code-Web-Sandbox: Docker-Daemon läuft nicht automatisch und stirbt mit der Shell;
       Start mit setsid nohup dockerd. Lokale .env ist nicht im Repo, Zugangsdaten maex/maex/maex_agent.
       Lokale Tests brauchen DATABASE_URL auf localhost. Docker-Hub-Rate-Limit möglich (docker login).
       str(URL) in SQLAlchemy maskiert das Passwort. Codex reviewt jeden PR automatisch, Findings sind gut.
```

---

## 14. Offene Fragen (werden gestellt, wenn sie gebraucht werden)

| Frage | Wann | Chat |
|---|---|---|
| Kassensystem und Schnittstelle | Stufe 0 | C1 |
| Telefonanbieter, Router, Rufumleitung | Stufe 0 | C1 |
| Budget laufend und einmalig | vor G0 | C1 |
| Stufenreihenfolge final (Anruf-Mix) | G0 | C0 |
| Anbieter und Hosting | G0 | C2 |
| DB-Technik | Stufe 1 | C3 |
| GUI-Technik | Stufe 1 | C6 |
| Stimme: natürlich oder synthetisch | Stufe 1 | C4 |
| SMS-Zusammenfassung ja/nein | Stufe 3 | C5 |

---

## 15. Glossar

- **Durchstich:** kleinste Version, die einmal durch alle Schichten läuft, vom Telefon bis in die GUI
- **Gate:** Prüfpunkt mit messbaren Kriterien; ohne Bestehen keine nächste Stufe
- **Eval-Suite:** Testfälle mit erwartetem Ergebnis, die die Genauigkeit automatisch messen
- **Regressionstest:** Evals nach jeder Änderung erneut laufen lassen, damit nichts unbemerkt schlechter wird
- **Heißer / kalter Pfad:** Arbeit, während der Kunde wartet, gegenüber Arbeit nach dem Gespräch
- **Tastentöne (DTMF):** Zahleneingabe über die Telefontastatur, robust bei schlechtem Empfang
- **Unterbrechen (Barge-in):** Der Kunde kann in die Ansage der KI hineinsprechen
- **Idempotenz:** doppelt gesendet, einmal ausgeführt; verhindert Doppelbestellungen
- **AVV:** Auftragsverarbeitungsvertrag nach DSGVO mit jedem Dienstleister, der Kundendaten verarbeitet
- **PoC:** Proof of Concept, technischer Machbarkeitstest

---

## 16. Changelog

- **v1.0 · 11.09.2026:** Erstfassung aus C0 (Loops 1–8). Enthält Ziel, Leitregeln, KPIs, Architektur Hybrid, Stufen 0–6 mit Gates, Anbieter-Kriterien, Datenmodell, Rechts-Check, Risiken und 8 Arbeitspakete mit Start-Prompts.
