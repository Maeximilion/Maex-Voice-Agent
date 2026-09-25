# 02 – Architektur

---

## 1. Überblick

```text
Anrufer
  │
  ▼
Festnetz <Pilotbetrieb> (bestehender Anbieter)
  │  Rufumleitung oder SIP
  │  Modus: Schatten · Überlauf · Primär
  ▼
Voice-Plattform (EU, extern)
  │  Spracherkennung → Modell → Stimme
  │  Tastentöne · Unterbrechen · Weiterleitung
  │
  │  HTTPS-Tool-Aufrufe   ← heißer Pfad, Kunde wartet
  ▼
Agent-API (FastAPI) ────────► Agent-DB (PostgreSQL, EU)
  │                                ▲
  │  Ereignis nach `confirm`       │
  │  ← kalter Pfad                 │
  ▼                                │
n8n ── Kasse, SMS, Logs, Alarme   │
Druckbrücke im Lokal ── Küchenbon  │  (holt ab, T-4.6)
                                   │
GUI (FastAPI + HTMX) ──────────────┘
  Betrieb-Tablet · Admin-PC

Team-Durchwahl ◄── transfer_to_team, Rückrufe
Transkription ──  auf dem EU-Server, nachts, Stufe 4 (nichts läuft bei Maxi)
```

---

## 2. Heißer und kalter Pfad

Der wichtigste Schnitt im System.

| | Heißer Pfad | Kalter Pfad |
|---|---|---|
| **Wann** | Kunde wartet am Telefon | nach dem „Ja" des Kunden |
| **Was** | Menüsuche, Kundenerkennung, Zonen-Check, Slot-Check, Bestellprüfung | Bon oder Kasse, SMS, Logs, Statistik, Alarme |
| **Zeitbudget** | < 300 ms je Tool, hart | Sekunden sind in Ordnung |
| **Technik** | Agent-API direkt, synchron | Outbox-Tabelle → Dispatcher → n8n, asynchron; der Küchenbon wird von der Druckbrücke abgeholt (§2a) |
| **Bei Fehler** | sofort sauber antworten, nie hängen | Wiederholung mit Backoff, dann Alarm |

**Regel:** n8n kommt nur dann in den heißen Pfad, wenn ein Messwert belegt, dass die Latenz hält. Erst messen, dann entscheiden.

### 2a. Küchenbon (T-4.6)

Der Server steht im Rechenzentrum, der Bondrucker im Lokal. Weder der Server noch n8n erreichen den Drucker, ohne einen Port im Router des Restaurants zu öffnen. Deshalb **holt** eine kleine Druckbrücke (`printbridge/`, nur Python-Standardbibliothek) im Lokal die Bons ab, statt dass jemand sie hineinschiebt:

```text
confirm / Passt / Korrektur ─► Outbox order.confirmed (revision)
                                   │  Dispatcher lässt ihn aus
Druckbrücke ── POST /v1/kitchen/claim ─► leiht Bon für 60 s aus
    │ druckt (TCP 9100 oder Windows-Warteschlange)
    └── POST /v1/kitchen/ack ─► sent / Fehlversuch, handover_state folgt
Wächter (Dispatcher-Prozess) ─► 60 s unabgeholt: Karte rot, Alarm, order.handover_failed an n8n
```

- `handover_state` folgt nur dem Bon mit der neuesten Revision; ein überholter Bon wird nicht mehr ausgeliefert (docs/04 §confirm, Vertrag b und c).
- Rot wird die Karte sofort, wenn die Küche den Bon nicht hat (Druckfehler oder 60 s unabgeholt), nicht erst nach dem letzten Backoff. Der Bon bleibt fällig und wird gedruckt, sobald Drucker oder Brücke zurück sind; die Karte wird dann wieder normal.
- Die Brücke führt ein Druckprotokoll (nur Bestell-ids und Revisionen) und druckt einen Bon nie zweimal, auch wenn die Rückmeldung verloren ging.
- Die Kasse (Variante C, D2) bekommt später einen eigenen Weg über n8n; der Küchenbon hängt nicht daran.

---

## 3. Anrufablauf (Beispiel Abholung)

```text
1. Anruf trifft ein
   → Plattform startet Session, übergibt caller_id an unsere Tools
2. Begrüßung mit KI-Hinweis (AI Act Art. 50)
3. get_service_status          → offen? Lieferung an? Wartezeit? ausverkauft?
4. find_customer(caller_id)    → Name bekannt? gespeicherte Adressen?
5. Kunde nennt Wünsche
   → search_menu je Position   → Treffer mit menu_item_id, Preis, Optionen
   → bei Unsicherheit: Verständnis-Leiter, nie raten
6. draft_order                 → Summe, Mindestbestellwert, Vorlesetext
7. Agent liest vor, Kunde bestätigt
8. confirm(idempotency_key)    → Status draft → confirmed
   → Outbox                    → Druckbrücke holt den Bon, Eintrag in der GUI
9. Verabschiedung, Anruf endet
10. Anruf-Log wird geschrieben (Dauer, Tools, Kosten, Ergebnis)
```

**Reservierung** ist derselbe Ablauf mit `check_slot` und `create_reservation` statt Menü und Bestellung.
**Lieferung** ergänzt `check_delivery` vor `draft_order`.

---

## 4. Betriebsmodi

Der Modus steht in `service_config.call_mode` und ist in der GUI umschaltbar.

| Modus | Verhalten | Stufe |
|---|---|---|
| `shadow` | KI nimmt keine Anrufe an. Team telefoniert wie bisher, Aufnahmen laufen für das Einlernen. | 1–4 |
| `overflow` | Team klingelt zuerst. Nimmt nach X Sekunden niemand ab, übernimmt die KI. Jede KI-Bestellung braucht Freigabe in der GUI. | 5 |
| `primary` | KI nimmt zuerst an, Team bleibt über die Durchwahl erreichbar. | 6 |
| `paused` | KI ist aus, alle Anrufe gehen direkt ans Team. Der Not-Aus-Knopf der GUI. | jederzeit |

---

## 5. Ausfallverhalten

| Was fällt aus | Was passiert | Wer merkt es |
|---|---|---|
| Agent-API nicht erreichbar | Plattform bekommt Fehler → Agent sagt einen Satz und leitet ans Team weiter | Alarm an Maxi |
| DB nicht erreichbar | API antwortet mit `service_unavailable`, Agent leitet weiter | Alarm |
| Voice-Plattform down | Rufumleitung greift nicht → Telefon klingelt normal beim Team | Alarm über Heartbeat |
| n8n down | `confirm` gelingt trotzdem, Ereignis landet in der Warteschlange und wird nachgeliefert; GUI zeigt die Bestellung sofort | Alarm |
| Internet weg | Telefon klingelt beim Team (Rufumleitung des Anbieters greift bei Nichterreichbarkeit) | offline sichtbar |
| Bondrucker aus, Papier leer | Brücke meldet den Fehler, Karte sofort rot, Bon wird mit Backoff wiederholt, „Nochmal senden" | Alarm, Team am Tablet |
| Druckbrücke oder Internet im Lokal weg | Bon wartet in der Outbox; nach 60 s ohne Abholung Karte rot und `order.handover_failed` an n8n | Alarm, Team am Tablet |
| Kassenanbindung defekt | Bestellung bleibt `confirmed`, Übergabe wird wiederholt, GUI markiert sie rot | Team am Tablet |

**Grundsatz:** Jeder Ausfall endet beim Menschen, nie beim Kunden im Nichts.

---

## 6. Datenflüsse und Hoheiten

| Datum | Master | Kopie |
|---|---|---|
| Umsätze und Bons | <Kassensystem> (TSE) | – |
| Menü und Preise | Kasse, falls exportierbar; sonst Agent-DB | Agent-DB mit wöchentlichem Abgleich-Check |
| Öffnungszeiten, Wartezeit, Zonen | Agent-DB | GUI zeigt an |
| Kundenstamm für die Telefonerkennung | Agent-DB | – |
| Anrufaufnahmen und Transkripte | EU-Server, verschlüsselter Speicher, mit Löschfrist | nie im Repo, nie auf Maxis PC |

---

## 7. Sicherheit

- Agent-API nur über HTTPS, Authentifizierung per Bearer-Token (`AGENT_API_TOKEN`)
- Rate-Limit je Session, damit ein hängender Agent keine Kosten produziert
- Maximale Anrufdauer, danach automatische Weiterleitung ans Team
- Schleifenschutz: `transfer_to_team` leitet nur auf die Durchwahl, nie auf die Hauptnummer
- Kein Schreibvorgang ohne `call_id`, jeder Schreibvorgang landet im `audit_log`
- Personenbezogene Daten mit Löschfrist, Löschjob läuft täglich

---

## 8. Warum Hybrid

Echtzeit-Audio mit unter einer Sekunde Latenz ist ein eigenes Fachgebiet: Sprachaktivitätserkennung, Unterbrechen, Jitter-Puffer, Telefonie-Protokolle. Das kaufen wir ein. Die fehlerkritische Logik, an der ein falscher Preis oder eine falsche Adresse entsteht, bleibt bei uns und ist in Tests messbar. Dadurch ist die Voice-Plattform austauschbar, ohne dass die Geschäftslogik angefasst wird.

**Konsequenz für den Code:** Die Anbindung an die Plattform steckt in genau einem Modul (`api/telephony/`). Kein Plattform-spezifisches Detail kriecht in `api/domain/`.

---

## 9. Der eigene Gesprächs-Kern (`api/agent/`)

Unabhängig davon, ob die Voice-Plattform ihren eigenen Modell-Loop fährt, bauen wir einen eigenen: Text rein, Tool-Aufrufe, Text raus. Er ist die Grundlage für drei Dinge, die ohne ihn nicht gehen:

| Nutzer des Kerns | Wozu |
|---|---|
| `sim/` | Gespräche im Terminal, Durchstich ohne Telefon |
| `evals/` | jeder Testfall läuft durch denselben Kern |
| Schattenmessung (Stufe 4) | Transkript → Vorgang, gleiche Logik wie live |

**Entscheidung D7** (`docs/01_STATUS.md`): Läuft dieser Kern auch im Betrieb, indem die Plattform ihn als „eigenes Modell" anspricht? Dann sind Test und Betrieb identisch. Fährt die Plattform ihren eigenen Loop, bleibt eine Restabweichung, die die Rollenspiele auffangen. Wird zusammen mit D1 in C2 entschieden.

## 10. Module

Schichten, Abhängigkeitsrichtung und Bauplan je Modul: `docs/11_MODULE.md`.
