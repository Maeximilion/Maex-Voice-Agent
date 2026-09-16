# 02 – Architektur

---

## 1. Überblick

```text
Anrufer
  │
  ▼
Festnetz Yoki (bestehender Anbieter)
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
n8n ── Küche / Kasse               │
    ── SMS, Logs, Alarme           │
                                   │
GUI (FastAPI + HTMX) ──────────────┘
  Betrieb-Tablet · Admin-PC

Team-Durchwahl ◄── transfer_to_team, Rückrufe
RTX 4080 lokal ──  Transkription der Einlern-Aufnahmen (offline, Stufe 4)
```

---

## 2. Heißer und kalter Pfad

Der wichtigste Schnitt im System.

| | Heißer Pfad | Kalter Pfad |
|---|---|---|
| **Wann** | Kunde wartet am Telefon | nach dem „Ja" des Kunden |
| **Was** | Menüsuche, Kundenerkennung, Zonen-Check, Slot-Check, Bestellprüfung | Bon oder Kasse, SMS, Logs, Statistik, Alarme |
| **Zeitbudget** | < 300 ms je Tool, hart | Sekunden sind in Ordnung |
| **Technik** | Agent-API direkt, synchron | Outbox-Tabelle → Dispatcher → n8n, asynchron |
| **Bei Fehler** | sofort sauber antworten, nie hängen | Wiederholung mit Backoff, dann Alarm |

**Regel:** n8n kommt nur dann in den heißen Pfad, wenn ein Messwert belegt, dass die Latenz hält. Erst messen, dann entscheiden.

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
   → Ereignis an n8n           → Bon in der Küche, Eintrag in der GUI
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
| Kassenanbindung defekt | Bestellung bleibt `confirmed`, Übergabe wird wiederholt, GUI markiert sie rot | Team am Tablet |

**Grundsatz:** Jeder Ausfall endet beim Menschen, nie beim Kunden im Nichts.

---

## 6. Datenflüsse und Hoheiten

| Datum | Master | Kopie |
|---|---|---|
| Umsätze und Bons | Kassensystem order smart (TSE) | – |
| Menü und Preise | Kasse, falls exportierbar; sonst Agent-DB | Agent-DB mit wöchentlichem Abgleich-Check |
| Öffnungszeiten, Wartezeit, Zonen | Agent-DB | GUI zeigt an |
| Kundenstamm für die Telefonerkennung | Agent-DB | – |
| Anrufaufnahmen und Transkripte | lokal auf der RTX 4080, mit Löschfrist | nie im Repo, nie in der Cloud |

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
