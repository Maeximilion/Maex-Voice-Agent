# 08 – Evals

> Die Eval-Suite ist das Gewissen des Projekts. Sie ist der Grund, warum „Fehler gegen null" eine Zahl ist und keine Hoffnung.

---

## 1. Was ein Fall ist

Ein Fall besteht aus dem, was der Kunde sagt, und dem, was dabei herauskommen muss.

`evals/cases/menu_0042_nummer_verwechselt.json`
```json
{
  "id": "menu_0042",
  "name": "Nummer 23 bei Störgeräusch",
  "tags": ["menu", "noise", "pickup"],
  "source": "roleplay",
  "transcript": [
    { "role": "customer", "text": "Ja guten Tag, ich hätte gern zweimal die dreiundzwanzig zum Abholen." },
    { "role": "customer", "text": "Müller, ja genau." },
    { "role": "customer", "text": "Ja passt so." }
  ],
  "expected": {
    "intent": "pickup",
    "items": [{ "number": "23", "quantity": 2 }],
    "customer_name": "Müller",
    "confirmed": true,
    "escalated": false
  }
}
```

**Grundsatz:** Erwartet wird das **Ergebnis**, nicht der Wortlaut. Wie der Agent formuliert, ist ihm überlassen. Was er bucht, nicht.

---

## 2. Metriken

| Metrik | Berechnung | Ziel |
|---|---|---|
| Genauigkeit | Fälle mit exakt passendem `expected` ÷ alle Fälle | ≥ Team-Baseline, Ziel ≥ 99 % |
| Geratene Positionen | Positionen ohne `menu_item_id` aus `search_menu` | **0, hart** |
| Unbestätigte Vorgänge | `confirm` ohne vorheriges Ja im Transkript | **0, hart** |
| Falsche Eskalation | eskaliert, obwohl der Fall lösbar war | ≤ 5 % |
| Verpasste Eskalation | nicht eskaliert, obwohl `expected.escalated` | **0, hart** |
| Tokens je Fall | Summe Ein- und Ausgabe | sinkend über die Versionen |
| Kosten je Fall | Tokens × Preis + Plattform-Minuten | ≤ Budget |

Die drei harten Metriken sind Abbruchkriterien. Ein einziger Verstoß lässt den Lauf durchfallen, egal wie gut die Genauigkeit ist.

---

## 3. Der Runner

`evals/runner.py`

```text
Für jeden Fall:
  1. Frische Session aufbauen (System-Prompt + Menü-Index)
  2. Kundensätze der Reihe nach einspielen
  3. Tool-Aufrufe gegen die echte API auf einer Testdatenbank laufen lassen
  4. Endzustand aus der DB lesen (nicht aus dem Modelltext)
  5. Mit expected vergleichen, Abweichung protokollieren
Danach:
  Report als JSON und als Markdown, Lauf in eval_runs speichern
```

**Wichtig:** Geprüft wird der **Datenbankzustand**, nicht was das Modell behauptet. Ein Agent, der „ist gebucht" sagt, ohne `confirm` aufzurufen, muss durchfallen.

Aufruf:
```bash
make eval                      # alle Fälle
make eval TAGS=menu,noise      # gefiltert
make eval MODEL=<name>         # Modellvergleich
```

---

## 4. Wann die Suite läuft

| Anlass | Umfang |
|---|---|
| Vor jedem Merge nach `main` | vollständig |
| Nach jeder Prompt-Änderung | vollständig, Ergebnis in den Commit |
| Nach Menü- oder Preisänderung | Tag `menu` |
| Nach Modellwechsel | vollständig plus Kostenvergleich |
| Wöchentlich automatisch | vollständig, Trend in die GUI |

**Regressionsregel:** Fällt die Genauigkeit gegenüber dem letzten Lauf, wird nicht gemerged. Kein „ist nur ein Fall".

---

## 5. Woher die Fälle kommen

| Quelle | Menge | Wann |
|---|---|---|
| Handgeschrieben | 20–30 | sofort, deckt die Regeln ab |
| Rollenspiele mit dem Team | 50–80 | vor G1 und G2, mit echtem Küchenlärm |
| Echte Anrufe (Schattenmodus) | laufend | ab Stufe 4, nach Rechtsfreigabe |
| **Jeder Produktionsfehler** | 1 je Fehler | dauerhaft, nicht verhandelbar |

Die letzte Zeile ist die wichtigste. Jeder Fehler, der einmal passiert ist, wird ein Testfall. Danach kann ihn keine spätere Änderung unbemerkt zurückbringen.

---

## 6. Pflichtabdeckung

Die Suite ist unvollständig, solange einer dieser Fälle fehlt:

- Bestellung über die Kartennummer · über den Namen · über einen umgangssprachlichen Alias
- Menge über eins, Mengenänderung mitten im Satz („doch drei")
- Pflicht-Optionsgruppe nicht genannt → muss nachfragen
- Ausverkauftes Gericht → Alternative statt Annahme
- Zwei ähnliche Gerichte → **muss** nachfragen, darf nicht wählen
- Allergiefrage mit gepflegtem Wert · ohne gepflegten Wert
- Adresse außerhalb der Zone · Mindestbestellwert unterschritten
- Kunde bricht mittendrin ab
- Beschwerde in Satz eins → sofortige Eskalation
- „Ich will mit jemandem sprechen" → sofortige Eskalation
- Starkes Rauschen → Verständnis-Leiter statt Raten
- Bestellung während der Schließzeit
- Doppelter `confirm` → nur ein Vorgang (Idempotenz)
