# 17 – Anrufprotokoll ohne Tonaufnahme

> Echte Anrufe lernen, bevor aufgezeichnet werden darf. Das Team notiert nach jedem Anruf eine Zeile, `scripts/call_log.py` macht daraus die Baseline für C1 und Entwürfe für Eval-Fälle.
> Ablage der ausgefüllten Datei: `imports/anrufprotokoll.csv` (im `.gitignore`). UTF-8, Semikolon als Trenner.

---

## 1. Warum ohne Ton

Aufzeichnen braucht die Einwilligung von Kunde und Team (§201 StGB) und den vollständigen Rechts-Check (`docs/09_OPERATIONS_LEGAL.md`). Der ist offen, T-7.x sind blockiert. Ein handschriftliches Protokoll ohne Namen und Nummern braucht beides nicht und liefert schon das Wichtigste:

| Frage | Woher | Wofür |
|---|---|---|
| Wie viele Anrufe, wann? | `date`, `time` | C1 Baseline, Stoßzeiten, Personalplanung für `overflow` |
| Was wollen die Leute? | `intent` | Anrufmix, Reihenfolge der Stufen bestätigen |
| Wie gehen Anrufe aus? | `outcome` | Team-Baseline für Gate G4 |
| Wie sagen Kunden es? | `phrases` | Aliase, Eval-Fälle (`docs/08` §5) |
| Was ging schief? | `problems` | Fehler-Taxonomie, Leiter (`docs/05`) |

Keine Rechtsberatung: dass Protokolle ohne personenbezogene Daten unkritisch sind, gehört trotzdem in den Rechts-Check.

---

## 2. Das Format

| Spalte | Pflicht | Beispiel | Regel |
|---|---|---|---|
| date | ja | `24.09.2026` | TT.MM.JJJJ |
| time | ja | `18:42` | HH:MM, Ortszeit, Beginn des Anrufs |
| duration_min | nein | `3` | Minuten, geschätzt reicht, Komma erlaubt |
| intent | ja | `abholung` | `reservierung` · `abholung` · `lieferung` · `beschwerde` · `frage` · `sonstiges` |
| outcome | ja | `erledigt` | `erledigt` · `rueckruf` · `abgelehnt` (ausgebucht, außerhalb Liefergebiet) · `abgebrochen` |
| phrases | nein | `zweimal die dreiundzwanzig \| ja passt so` | **wörtlich**, wie der Kunde es sagte, Sätze mit `\|` trennen |
| items | nein | `2x 23, 1x Frühlingsrollen` | was am Ende bestellt wurde, **Komma**, nie Semikolon |
| problems | nein | `sagte erst 32, meinte 23` | Missverständnis, Rückfrage, Ärger |

Groß- und Kleinschreibung und Umlaute sind egal (`Rückruf` = `rueckruf`). Die Kürzel vom Druckbogen gehen auch: Anliegen `R` `A` `L` `B` `F` `S`, Ergebnis `E` `RR` `AL` `AB`. Vorlage mit zwei Beispielzeilen: `docs/vorlagen/anrufprotokoll.csv`. Papierbogen zum Ausdrucken: `docs/vorlagen/anrufprotokoll_druck.html`.

### Nie ins Protokoll
- **Namen** des Kunden. Im Satz „Auf Müller bitte" steht dann „Auf Mueller bitte" (der Platzhalter aus den Evals).
- **Telefonnummern, Adressen, E-Mail.** Bei Lieferung reicht „Lieferung in die Weststadt".
- Kürzel oder Namen aus dem Team.

Das Script lehnt die ganze Datei ab, sobald ein Freitext nach Telefonnummer (sechs Ziffern in Folge, auch mit Punkt, Klammer, Strich oder Leerzeichen dazwischen) oder E-Mail aussieht. Ein Datum mit Jahr im Freitext schlägt deshalb auch an: dort `25.09.` statt `25.09.2026` schreiben. Namen erkennt es nicht, die bleiben Handarbeit.

---

## 3. Auswerten

```bash
python -m scripts.call_log imports/anrufprotokoll.csv
python -m scripts.call_log imports/anrufprotokoll.csv --cases imports/eval_entwuerfe/
```

Ausgabe: Anzahl, Anliegen und Ergebnis mit Anteil, mittlere Dauer, Anrufe je Stunde und Wochentag, alle notierten Probleme. Exit-Code 0 ausgewertet, 1 Prüffehler (nichts ausgewertet), 2 Datei fehlt oder Zielordner ist `evals/cases/`.

### Eval-Entwürfe
Mit `--cases` wird jede Zeile mit Kundensätzen zu einem Fall im Format von `docs/08` §1, `source: "call_log"`, Dateiname `protokoll_<id>_<anliegen>.json`. Die ID kommt aus Datum, Stunde und Kundensätzen, nicht aus der Zeilennummer: Fälle aus verschiedenen Wochen überschreiben sich in `evals/cases/` nicht, derselbe Anruf behält seine ID. `--cases evals/cases/` lehnt das Script ab (Exit 2). Jeder Lauf schreibt den Ordner neu: alte `protokoll_*.json` werden vorher gelöscht, damit nach einer Korrektur kein überholter Entwurf liegen bleibt. Durchgesehene Fälle deshalb sofort nach `evals/cases/` verschieben, nicht im Entwurfsordner bearbeiten.

| Protokoll | `expected` |
|---|---|
| reservierung / abholung / lieferung | `intent` = `reservation` / `pickup` / `delivery` |
| outcome `erledigt` | `confirmed: true`, `escalated: false` |
| outcome `abgelehnt` oder `abgebrochen` | `confirmed: false`, `escalated: false` |
| outcome `rueckruf` oder intent `beschwerde` | `escalated: true` |
| `frage`, `sonstiges` ohne Eskalation, oder keine Kundensätze | kein Fall |

Ein Entwurf ist **kein** fertiger Fall. Das Feld `review` sagt, was fehlt: Positionen mit Kartennummer nach `expected.items`, Namen prüfen, dann `review` löschen und die Datei nach `evals/cases/` verschieben.

---

## 4. Ablauf im Betrieb

1. Bogen ausdrucken, neben das Telefon legen. Eine Zeile je Anruf, direkt nach dem Auflegen, 20 Sekunden.
2. Abends oder am Wochenende in die CSV abtippen (Tabellenkalkulation, als CSV UTF-8 mit Semikolon speichern). Bögen danach vernichten.
3. Einmal pro Woche auswerten, Zahlen in die C1-Bestandsaufnahme.
4. Zwei Wochen reichen für eine erste Baseline.

Wenn der Rechts-Check durch ist und die Voice-Plattform steht, übernimmt der Modus `shadow` mit Aufnahme und Transkription (T-7.1 bis T-7.5). Das Protokoll bleibt als Vergleich bis Gate G4 sinnvoll.
