# 02 – Architektur

---

## 1. Überblick

> Decided 07.10.2026: the voice layer is our own, not a hosted platform. It is planned, not built and not measured. Spec: `docs/20_VOICE_LAYER.md`.

```text
Anrufer
  │
  ▼
Festnetz <Pilotbetrieb> (bestehender Anbieter)
  │  pilot's router (test phase) or SIP trunk (operation)
  │  Modus: Schatten · Überlauf · Primär
  ▼
Own voice layer (Asterisk + service voice, own EU server; docs/20, planned)
  │  ear (speech-to-text) → our core → mouth (text-to-speech)
  │  Tastentöne · Unterbrechen · Weiterleitung
  │  (ear and mouth: speech engines as services behind ports)
  │
  │  calls into the core and domain   ← heißer Pfad, Kunde wartet
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

### 2a. Bon über die Druckbrücke (T-4.6)

Der Server steht im Rechenzentrum, der Bondrucker im Lokal. Weder der Server noch n8n erreichen den Drucker, ohne einen Port im Router des Restaurants zu öffnen. Deshalb **holt** eine kleine Druckbrücke (`printbridge/`, nur Python-Standardbibliothek) im Lokal die Bons ab, statt dass jemand sie hineinschiebt:

```text
confirm / Passt / Korrektur ─► Outbox order.confirmed (revision)
                                   │  Dispatcher lässt ihn aus
Druckbrücke ── POST /v1/kitchen/claim ─► leiht Bon für 60 s aus
    │ druckt (TCP 9100 oder Windows-Warteschlange)
    └── POST /v1/kitchen/ack ─► sent / Fehlversuch, handover_state folgt
Wächter (eigener Faden)       ─► 60 s unabgeholt: Karte rot, Alarm, order.handover_failed an n8n
```

- `handover_state` folgt nur dem Bon mit der neuesten Revision; ein überholter Bon wird nicht mehr ausgeliefert (docs/04 §confirm, Vertrag b und c).
- Rot wird die Karte sofort, wenn die Küche den Bon nicht hat (Druckfehler oder 60 s unabgeholt), nicht erst nach dem letzten Backoff. Der Bon bleibt fällig und wird gedruckt, sobald Drucker oder Brücke zurück sind; die Karte wird dann wieder normal.
- Die Brücke führt ein Druckprotokoll (nur Bestell-ids und Revisionen) und druckt einen Bon nie zweimal, auch wenn die Rückmeldung verloren ging.
- Die Kasse (Variante C, D2) bekommt später einen eigenen Weg über n8n; die Brücke hängt nicht daran.

**Entscheidung 26.09.2026 (D2): Bons druckt die Kasse.** Eine Bestellung wird in die Kasse eingespielt, die Kasse bucht sie mit TSE und verteilt Quittung und Küchenbon selbst auf ihre Drucker. Die Druckbrücke bleibt als **unabhängiger Weg** bestehen und druckt auf den **Haupt-Bondrucker an der Kasse** (80 mm, 48 Zeichen), nicht in die Küche:

| Phase | Kasse | Druckbrücke |
|---|---|---|
| Bis zur Kassenschnittstelle (Annahme: <Kassenanbieter> arbeitet nicht mit) | Team tippt die Bestellung ein, die Kasse druckt Quittung und Küchenbon | druckt **jede** bestätigte Bestellung mit dem Kopf „NICHT IN KASSE – bitte eingeben" |
| Mit Kassenschnittstelle (Variante C) | Bestellung kommt automatisch, Kasse druckt | druckt nur, wenn die Kasse die Bestellung nicht rechtzeitig annimmt; Kopf wie oben, Team bucht nach (Regel 5) |

Ein Bon der Brücke ist ein Eingabezettel für das Team, kein Küchenbon und kein Beleg. `handover_state` heißt bis zur Schnittstelle „Zettel liegt an der Kasse", danach „Kasse hat angenommen".

---

## 3. Anrufablauf (Beispiel Abholung)

```text
1. Anruf trifft ein
   → the voice layer starts the session and hands caller_id to the core
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

A call that reaches the agent in `shadow` or `paused` anyway is transferred to the team extension at once, without a word from the AI (`telephony/handler.py`, T-1.13). The mode is read at the start of every call, so the emergency stop holds from the next call on, whatever the routing in front of the voice layer says.

---

## 5. Ausfallverhalten

| Was fällt aus | Was passiert | Wer merkt es |
|---|---|---|
| Agent-API nicht erreichbar | the voice layer gets the error, says one sentence and transfers to the team (docs/20 §5, target, not yet measured) | Alarm an Maxi |
| DB nicht erreichbar | API antwortet mit `service_unavailable`, Agent leitet weiter | Alarm |
| Failure during a call on our side (database, model, adapter) | `telephony/handler.py` says the outage sentence (docs/05 §6) and transfers to the team extension, the call log gets `error`; without a database the number from `TEAM_PHONE` in `.env`; if the transfer fails, the dialplan dials the team number (docs/20 §5, target, not yet measured) | error log |
| Voice layer down | `voice` down: the Asterisk dialplan dials the team number. Asterisk or the whole machine down: no dialplan runs, the router rings the team telephones (test phase) or the trunk's forwarding rules apply (operation); docs/20 §5, target, not yet measured | Alarm über Heartbeat |
| n8n down | `confirm` gelingt trotzdem, Ereignis landet in der Warteschlange und wird nachgeliefert; GUI zeigt die Bestellung sofort | Alarm |
| Internet weg | Telefon klingelt beim Team (test phase: the router; operation: the trunk's forwarding rules; docs/20 §5, target, not yet measured) | offline sichtbar |
| Bondrucker aus, Papier leer | Brücke meldet den Fehler, Karte sofort rot, Bon wird mit Backoff wiederholt, „Nochmal senden" | Alarm, Team am Tablet |
| Druckbrücke oder Internet im Lokal weg | Bon wartet in der Outbox; nach 60 s ohne Abholung Karte rot und `order.handover_failed` an n8n | Alarm, Team am Tablet |
| Kassenanbindung defekt | Bestellung bleibt `confirmed`, Übergabe wird wiederholt, GUI markiert sie rot; die Druckbrücke druckt den Eingabezettel auf den Haupt-Bondrucker (§2a) | Team am Tablet und an der Kasse |

**Grundsatz:** Jeder Ausfall endet beim Menschen, nie beim Kunden im Nichts.

---

## 6. Datenflüsse und Hoheiten

| Datum | Master | Kopie |
|---|---|---|
| Umsätze und Bons | <Kassensystem> (TSE) | – |
| Menü und Preise | Kasse (`.dbf`-Kopie, docs/14 §Quelle Kasse) | Agent-DB mit wöchentlichem Abgleich-Check, Schlüssel ist die Artikelnummer der Kasse |
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

**Decided 07.10.2026: we build the voice layer ourselves** (E1 flipped, E15, E16; `docs/20_VOICE_LAYER.md`). Reasons: no fixed platform fee, our code decides every sentence (read-back, transfer, keypad, outage path), and the product does not depend on a restaurant's router or carrier. Price: an estimated 5 to 8 weeks of work, and uptime is ours. A hosted platform stays the last resort (docs/20 §13). History: until 07.10.2026 the plan bought real-time audio from a hosted platform and kept only the error-critical logic.

**Consequence for the code:** everything that knows telephony or a speech engine sits in exactly one module (`api/telephony/`, adapters per engine and access). No such detail creeps into `api/domain/`.

## 9. Der eigene Gesprächs-Kern (`api/agent/`)

We build our own loop: text in, tool calls, text out. It runs in operation too (D7) and is the basis for three more things that do not work without it:

| Nutzer des Kerns | Wozu |
|---|---|
| `sim/` | Gespräche im Terminal, Durchstich ohne Telefon |
| `evals/` | jeder Testfall läuft durch denselben Kern |
| Schattenmessung (Stufe 4) | Transkript → Vorgang, gleiche Logik wie live |

**Decision D7 (decided 07.10.2026):** this core runs in operation, called by the voice layer (docs/20 §2). Test and operation are identical; there is no platform loop with a remaining deviation.

## 10. Module

Schichten, Abhängigkeitsrichtung und Bauplan je Modul: `docs/11_MODULE.md`.
