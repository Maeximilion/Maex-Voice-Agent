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
- `expected` kennt `intent`, `confirmed`, `escalated`, `items` (`number`, `quantity`, optional `options` und `note` im festen Wortlaut, etwa der Allergiehinweis aus E14), `customer_name`, `party_size` und `tools` (Tools, die das Modell **erfolgreich** aufgerufen haben muss, als Name oder `{"tool": "get_item_details", "number": "23"}` für genau dieses Gericht: eine Allergiefrage gilt nur mit dem Nachschlagen des gefragten Gerichts als beantwortet, T-5.2). Verglichen wird nur, was im Fall steht. Ein unbekannter Schlüssel, ein fehlendes Feld oder eine doppelte `id` bricht den Lauf mit Exit 2 ab, statt still grün zu sein.
- Die harten Metriken misst ein Beobachter zwischen Gesprächskern und Modell (`evals/recorder.py`): eine `menu_item_id` in `draft_order`, die keine Suche im selben Anruf geliefert hat, gilt als geraten. Ein `confirm` gilt als unbestätigt, wenn nach dem letzten Entwurf (`draft_order`, `create_reservation`) kein Kundensatz kam oder dieser kein Ja war (ein beiläufiges "Ja, guten Tag" vor dem Vorlesen zählt nicht), ebenso ein bestätigter Vorgang ohne `confirm` des Modells. Der Beobachter arbeitet für jedes Modell gleich, auch für das echte aus T-2.4.
- Jeder Fall bekommt einen eigenen Mandanten mit Evalkarte: Kapazität, Abholcodes und offene Rückrufe eines Falls beeinflussen keinen anderen, das Ergebnis hängt nicht an Reihenfolge oder Tag-Filter.
- Urteil: Exit 1 bei einem abgestürzten Fall, bei einem einzigen harten Verstoß oder wenn ein Fall, der im letzten **bestandenen** Lauf mit demselben Modell und denselben Tags grün war, jetzt rot ist. Verglichen wird je Fall, nicht über die Genauigkeit: ein neuer roter Fall aus `/bug` ist keine Regression und darf rot stehen, bis der Fix da ist; neue grüne Fälle verdecken keinen kaputten. Ein durchgefallener Lauf ist nie Maßstab, sonst verschwände eine Regression beim zweiten Aufruf.
- Report als JSON und Markdown in `evals/reports/` (nicht im Repo). Tokens und Kosten je Fall bleiben leer, bis T-2.4 ein echtes Modell anschließt; `--model` nimmt bis dahin nur `scripted` und lehnt alles andere ab, statt still auf das Skript zurückzufallen.
- Die ganze Suite aus `evals/cases/` läuft auch in CI (`api/tests/test_evals_runner.py`), damit Regel 4 aus CLAUDE.md §2 bei jedem Pull Request greift.

**Suite v1 (T-5.2, 26.09.2026):** 107 Fälle in `evals/cases/` (Abholung, Reservierung, Eskalation, Allergie, Lieferung), jede Zeile aus §6 hat mindestens einen. Ein Lauf dauert etwa 12 s. Stand mit dem Skript-Modell: 99 von 107 grün, harte Metriken 0, falsche Eskalation 4,5 %.
- `"sold_out": ["48"]` setzt „heute aus" nur für diesen Fall (die Evalkarte im Importformat kennt keinen Tagesstand). Eine Nummer, die nicht auf der Evalkarte steht, lässt den Fall abstürzen.
- `"pending": "T-6.5: …"` markiert eine **bekannte Lücke**: der Fall beschreibt das Ziel, das der heutige Stand noch nicht kann, mit der Aufgabe, die ihn grün macht. Er zählt in der Genauigkeit mit und steht im Report unter „Bekannte Lücken". CI verlangt, dass jeder Fall ohne `pending` grün und jeder mit `pending` rot ist, wie ein striktes xfail: wird eine Lücke grün, fällt CI auf, und das Feld kommt weg. Höchstens jeder zehnte Fall darf eine Lücke sein.
- `"repeat_confirm": true` schickt den letzten `confirm` des Modells nach dem Gespräch ein zweites Mal, wie eine Plattform nach einem Timeout. Ein zweites Ja im Transkript käme nie an: das Replay endet mit der Bestätigung. Unabhängig davon zählt jeder Abgleich, ob ein Vorgang mehr als einmal bestätigt wurde (`audit_log` und Outbox); das ist immer rot.
- Die Evalkarte trägt Allergene für 23 und 24; die 13 hat bewusst keine (Allergiefrage ohne gepflegten Wert).

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

**Regressionsregel:** Ist ein Fall rot, der im letzten bestandenen Lauf mit demselben Modell und denselben Tags grün war, wird nicht gemerged. Kein „ist nur ein Fall". Verglichen wird je Fall, nicht über die Genauigkeit: ein neuer, noch roter Fall aus `/bug` ist keine Regression, und neue grüne Fälle verdecken keinen kaputten (§3, T-5.1).

---

## 5. Woher die Fälle kommen

| Quelle | Menge | Wann |
|---|---|---|
| Handgeschrieben | 20–30, gebaut: 102 (T-5.2) | sofort, deckt die Regeln ab |
| Rollenspiele mit dem Team | 50–80 | vor G1 und G2, mit echtem Küchenlärm |
| Nachgestellt aus dem Anrufprotokoll (`docs/17`) | laufend | sofort; `source: handcrafted`, eigene Worte, nie der Wortlaut echter Anrufe |
| Echte Anrufe (Schattenmodus) | laufend | ab Stufe 4, nach Rechtsfreigabe |
| **Jeder Produktionsfehler** | 1 je Fehler | dauerhaft, nicht verhandelbar |

Die letzte Zeile ist die wichtigste. Jeder Fehler, der einmal passiert ist, wird ein Testfall. Danach kann ihn keine spätere Änderung unbemerkt zurückbringen.

---

## 6. Pflichtabdeckung

Die Suite ist unvollständig, solange einer dieser Fälle fehlt. Die Spalte Tag ist verbindlich: `api/tests/test_evals_suite.py` liest diese Tabelle und verlangt zu jedem Tag mindestens einen Fall in `evals/cases/`.

| Pflichtfall | Tag |
|---|---|
| Bestellung über die Kartennummer | `nummer` |
| Bestellung über den Namen | `name` |
| Bestellung über einen umgangssprachlichen Alias | `alias` |
| Menge über eins | `menge` |
| Mengenänderung mitten im Satz („doch drei") | `mengenaenderung` |
| Pflicht-Optionsgruppe nicht genannt → muss nachfragen | `pflichtoption` |
| Ausverkauftes Gericht → Alternative statt Annahme | `ausverkauft` |
| Zwei ähnliche Gerichte → **muss** nachfragen, darf nicht wählen | `mehrdeutig` |
| Allergiefrage mit gepflegtem Wert | `allergie_gepflegt` |
| Allergiefrage ohne gepflegten Wert | `allergie_ohne_wert` |
| Adresse außerhalb der Zone | `zone` |
| Mindestbestellwert unterschritten | `mindestbestellwert` |
| Kunde bricht mittendrin ab | `abbruch` |
| Beschwerde in Satz eins → sofortige Eskalation | `beschwerde` |
| „Ich will mit jemandem sprechen" → sofortige Eskalation | `mensch` |
| Starkes Rauschen → Verständnis-Leiter statt Raten | `rauschen` |
| Bestellung während der Schließzeit | `schliesszeit` |
| Doppelter `confirm` → nur ein Vorgang (Idempotenz) | `doppeltes_confirm` |

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
