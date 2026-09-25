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

Optional `"caller_id": "+497215551234"`: die Nummer aus der Rufnummernerkennung. Ohne das Feld ist sie unterdrückt, und der Agent fragt nach der Rufnummer.

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

**Gebaut (T-5.1, 25.09.2026):**
- Jeder Lauf bekommt eine eigene, frisch migrierte Datenbank (`evals/scratch_db.py`) mit dem Mandanten "Evalbetrieb" und der Evalkarte aus `evals/menu/`. Entwicklungs- und Betriebsdaten berührt er nie. Gespielt wird über `sim/replay.py`, also derselbe Gesprächskern wie im Text-Telefon.
- Zeitpunkt: Dienstag, 15.09.2026, 18:00 Europe/Berlin. Ein Fall kann ihn mit `"now": "2026-09-15T23:30:00+02:00"` selbst setzen (Schließzeit, Tageswechsel).
- `expected` kennt `intent`, `confirmed`, `escalated`, `items` (`number`, `quantity`, optional `options`), `customer_name`, `party_size`. Verglichen wird nur, was im Fall steht. Ein unbekannter Schlüssel, ein fehlendes Feld oder eine doppelte `id` bricht den Lauf mit Exit 2 ab, statt still grün zu sein.
- Die harten Metriken misst ein Beobachter zwischen Gesprächskern und Modell (`evals/recorder.py`): eine `menu_item_id` in `draft_order`, die keine Suche im selben Anruf geliefert hat, gilt als geraten. Ein `confirm`, vor dem der letzte Kundensatz kein Ja war, gilt als unbestätigt, ebenso ein bestätigter Vorgang ohne `confirm` des Modells. Der Beobachter arbeitet für jedes Modell gleich, auch für das echte aus T-2.4.
- Urteil: Exit 1 bei einem einzigen harten Verstoß oder wenn die Genauigkeit unter den letzten Lauf mit demselben Modell und denselben Tags fällt. Ein einzelner roter Fall allein lässt den Lauf nicht durchfallen: so steht ein neuer Fall aus `/bug` rot in der Suite, bis der Fix da ist.
- Report als JSON und Markdown in `evals/reports/` (nicht im Repo). Tokens und Kosten je Fall bleiben leer, bis T-2.4 ein echtes Modell anschließt; `--model` nimmt bis dahin nur `scripted` und lehnt alles andere ab, statt still auf das Skript zurückzufallen.
- Die ganze Suite aus `evals/cases/` läuft auch in CI (`api/tests/test_evals_runner.py`), damit Regel 4 aus CLAUDE.md §2 bei jedem Pull Request greift.
- Noch offen: den Lauf in `eval_runs` speichern (Tabelle nach docs/03 §Migrationsreihenfolge, Nummer nach PR #139).

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
| Nachgestellt aus dem Anrufprotokoll (`docs/17`) | laufend | sofort; `source: handcrafted`, eigene Worte, nie der Wortlaut echter Anrufe |
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

---

## 7. Der Nummern-Eval-Satz

Neben der Gespraechs-Suite steht ein zweiter, viel kleinerer Satz:
`evals/cases/nummern.jsonl` mit `evals/number_eval.py`. Er prueft nicht, was ein
Modell entscheidet, sondern was der Code entscheidet - `sole_item_number` aus
`domain/menu/numberwords.py` -, und er laeuft deshalb ohne Datenbank, ohne HTTP
und ohne Modell in Millisekunden.

```bash
make eval-nummern            # Tabelle der Abweichungen, Exit 1 bei rot
python -m evals.number_eval  # dasselbe ohne Docker
```

Ein Fall ist eine Zeile:

```json
{"say": "Nummer 23 oder 24", "expect": "?", "why": "zwei genannte Nummern"}
```

Vier Erwartungswerte, mehr gibt es nicht:

| Wert | Bedeutung |
|---|---|
| `"23"` | genau diese Kartennummer, die Suche darf sie direkt nehmen |
| `"!23g"` | als Nummer genannt, aber keine gueltige Kartenform → `not_found`; nie Ausweichen auf aehnliche Namen (CLAUDE.md §2 Regel 2) |
| `"?"` | nicht eindeutig → `ambiguous` mit der Frage nach der einen Nummer |
| `"name"` | kein Nummernsatz → Alias- und Trigram-Suche entscheiden |

**Warum eigenstaendig.** Die Regeln fuer Marker, Kartenendung, Menge und
Verbindungswort greifen ineinander. Auf PR #117 haben acht Korrekturrunden
nacheinander je eine Form repariert, und zwei davon haben eine frueher richtige
Form wieder kaputt gemacht - jede Korrektur stimmte fuer sich. Einzelne
Testfunktionen zeigen das nicht, eine Tabelle aller bekannten Saetze sofort:
gegen die Staende vor den letzten Korrekturen faellt der Satz mit 3 bis 11
Abweichungen durch.

Derselbe Satz laeuft in CI ueber `api/tests/test_evals_nummern.py`, damit ein
Fall an genau einer Stelle gepflegt wird. Fuer neue Faelle gilt die Regel aus
Abschnitt 5: jeder Satz vom Telefon, der falsch aufgeloest wurde, kommt mit der
richtigen Erwartung hinein - der Satz ist dann rot, bis der Code stimmt.
