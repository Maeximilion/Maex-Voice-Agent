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

**Entschieden (Maxi, 24.09.2026):** Das Protokoll darf vor dem abgeschlossenen Rechts-Check geführt werden. Der DSFA-Entwurf vom 24.09.2026 führt es trotzdem als Verarbeitung personenbezogener Daten (D12), weil Uhrzeit und Wortlaut über die Anrufliste des Routers einem Anrufer zugeordnet werden können. Deshalb gelten die Regeln unten und eine Löschfrist von 90 Tagen für die CSV (D10). Keine Rechtsberatung.

---

## 2. Das Format

| Spalte | Pflicht | Beispiel | Regel |
|---|---|---|---|
| date | ja | `24.09.2026` | TT.MM.JJJJ |
| time | ja | `18:42` | HH:MM, Ortszeit, Beginn des Anrufs |
| duration_min | nein | `3` | Minuten, geschätzt reicht, Komma erlaubt |
| intent | ja | `abholung` | `reservierung` · `abholung` · `lieferung` · `beschwerde` · `frage` · `sonstiges` |
| outcome | ja | `erledigt` | `erledigt` · `rueckruf` · `abgelehnt` (ausgebucht, außerhalb Liefergebiet) · `abgebrochen` |
| phrases | nein | `zweimal die dreiundzwanzig \| ja passt so` | **wörtlich**, wie der Kunde es sagte, Sätze mit `\|` oder Zeilenumbruch in der Zelle trennen. Bleibt in der CSV, kommt nie ins Repo (§3) |
| items | nein | `2x 23, 1x Frühlingsrollen` | was am Ende bestellt wurde, **Komma**, nie Semikolon |
| problems | nein | `sagte erst 32, meinte 23` | Missverständnis, Rückfrage, Ärger |

Groß- und Kleinschreibung und Umlaute sind egal (`Rückruf` = `rueckruf`). Die Kürzel vom Druckbogen gehen auch: Anliegen `R` `A` `L` `B` `F` `S`, Ergebnis `E` `RR` `AL` `AB`. Vorlage mit zwei Beispielzeilen: `docs/vorlagen/anrufprotokoll.csv`. Papierbogen zum Ausdrucken: `docs/vorlagen/anrufprotokoll_druck.html`.

### Nie ins Protokoll
- **Namen** des Kunden. Im Satz „Auf Müller bitte" steht dann „Auf Mueller bitte" (der Platzhalter aus den Evals).
- **Telefonnummern, Adressen, E-Mail.** Bei Lieferung reicht „Lieferung in die Weststadt".
- Kürzel oder Namen aus dem Team.
- **Allergien oder Unverträglichkeiten einer Person** („ich habe eine Nussallergie“). Das ist ein Gesundheitsdatum (DSFA M8). Stattdessen produktbezogen: „sind in der 23 Nüsse?“

Das Script lehnt die ganze Datei ab, sobald ein Freitext nach Telefonnummer, E-Mail, Adresse oder Allergie einer Person aussieht:

- Telefonnummer: sechs Ziffern in Folge, auch mit Klammer, Strich, Leerzeichen oder Punkt zwischen Ziffern, und diktiert („null sieben zwei eins …“, „null sieben einundzwanzig …“)
- E-Mail: auch mit Leerzeichen oder diktiert („mueller at gmx punkt de“)
- Adresse: Straße mit Hausnummer („Kaiserstraße 12“); ein Stadtteil ist erlaubt
- Allergie: „Allergie“ (auch „Nussallergie“), „allergisch“, „Unverträglichkeit“, „Intoleranz“; die Frage nach „Allergenen“ oder „Allergien“ eines Gerichts ist erlaubt

Ein Datum mit Jahr schlägt deshalb auch an: `25.09.` statt `25.09.2026` schreiben, `am 25.09. 19 Uhr` geht. Mehrere Kartennummern mit Komma trennen (`die 12, 34 und 56`), sonst sehen sie wie eine Nummer aus. Namen erkennt das Script nicht, die bleiben Handarbeit.

---

## 3. Auswerten

```bash
python -m scripts.call_log imports/anrufprotokoll.csv
python -m scripts.call_log imports/anrufprotokoll.csv --cases imports/eval_entwuerfe/
python -m scripts.call_log imports/anrufprotokoll.csv --frist-tage 90            # nur anzeigen
python -m scripts.call_log imports/anrufprotokoll.csv --frist-tage 90 --loeschen # wirklich löschen
```

Ausgabe: Anzahl, Anliegen und Ergebnis mit Anteil, mittlere Dauer, Anrufe je Stunde, Anrufe je Wochentag als Mittel je Tag (ein Wochentag, der im Zeitraum öfter vorkommt, wirkt sonst stärker, `-` wenn er nicht vorkommt), alle notierten Probleme mit Zeilennummer der CSV. Exit-Code 0 ausgewertet, 1 Prüffehler oder Datei nicht UTF-8 (nichts ausgewertet), 2 Datei fehlt oder ist nicht lesbar, oder Zielordner liegt in `evals/cases/`.

### Löschfrist
`--frist-tage N` zeigt, wie viele Einträge älter als N Tage sind; mit `--loeschen` entfernt das Script sie aus der CSV (die übrigen Zeilen bleiben unverändert). Ohne `--frist-tage` gilt `CALL_LOG_RETENTION_DAYS` aus der Umgebung. `--loeschen` ohne Frist bricht ab (Exit 2), eine Datei mit Prüffehlern wird nie verändert (Exit 1). Die Frist ist **90 Tage** (D10, entschieden 24.09.2026), in `.env` als `CALL_LOG_RETENTION_DAYS=90`. Ein Entwurf, der kein gültiges JSON mehr ist, bleibt liegen und wird gemeldet.

### Eval-Entwürfe
Mit `--cases` wird jede Zeile mit Kundensätzen zum **Gerüst** eines Falls im Format von `docs/08` §1, `source: "handcrafted"`, Dateiname `protokoll_<id>_<anliegen>.json`. **Der Entwurf enthält keinen echten Kundensatz**, `transcript` ist leer (DSFA M15, CLAUDE.md §8: echte Kundensätze nie ins Git). Das Team stellt die Sätze mit eigenen Worten nach: gleiches Anliegen, gleiches Problem, anderer Wortlaut. Der echte Wortlaut bleibt in der CSV in `imports/`. Die ID kommt aus Datum, Uhrzeit und Kundensätzen, nicht aus der Zeilennummer: Fälle aus verschiedenen Wochen überschreiben sich in `evals/cases/` nicht, derselbe Anruf behält seine ID. Einen Zielordner in `evals/cases/` lehnt das Script ab, auch in anderer Schreibweise oder als Unterordner (Exit 2). Jeder Lauf räumt den Ordner auf: Entwürfe, die noch das Feld `review` tragen, werden vorher gelöscht, damit nach einer Korrektur kein überholter Entwurf liegen bleibt. Ein Fall ohne `review` (schon durchgesehen) bleibt stehen und wird nicht überschrieben. Ein Anruf, dessen ID schon in `evals/cases/` liegt, wird nicht neu entworfen.

| Protokoll | `expected` |
|---|---|
| reservierung / abholung / lieferung | `intent` = `reservation` / `pickup` / `delivery` |
| outcome `erledigt` | `confirmed: true`, `escalated: false` |
| outcome `abgelehnt` oder `abgebrochen` | `confirmed: false`, `escalated: false` |
| outcome `rueckruf` oder intent `beschwerde` | `escalated: true` |
| `frage`, `sonstiges` ohne Eskalation, oder keine Kundensätze | kein Fall |

Name und Rufnummer stehen nie im Protokoll, der Agent braucht beide vor `confirm`. Ein Fall mit `confirmed: true` bekommt deshalb die erfundene `caller_id` `+497215551234` (Beispielnummer aus `docs/08` §1), und `review` erinnert daran, beim Nachstellen die Zeile „Auf den Namen Mueller.“ vor den letzten Kundensatz zu setzen. Die ID enthält die Minute; eine doppelt abgetippte Zeile ergibt einen Fall, nicht zwei.

Ein Entwurf ist **kein** fertiger Fall. Das Feld `review` sagt, was fehlt: Kundensätze nachstellen (Anzahl steht dabei), das notierte Problem nachstellen, Positionen mit Kartennummer nach `expected.items`, dann `review` löschen und die Datei nach `evals/cases/` verschieben.

---

## 4. Ablauf im Betrieb

1. Bogen ausdrucken, neben das Telefon legen. Eine Zeile je Anruf, direkt nach dem Auflegen, 20 Sekunden.
2. Abends oder am Wochenende in die CSV abtippen (Tabellenkalkulation, als „CSV UTF-8 (durch Trennzeichen getrennt)“ speichern; das normale CSV aus Excel ist kein UTF-8 und wird abgelehnt). Bögen danach vernichten.
3. Einmal pro Woche auswerten, Zahlen in die C1-Bestandsaufnahme, danach mit `--frist-tage 90 --loeschen` alles außerhalb der Frist aus der CSV entfernen.
4. Zwei Wochen reichen für eine erste Baseline.

Wenn der Rechts-Check durch ist und die Voice-Plattform steht, übernimmt der Modus `shadow` mit Aufnahme und Transkription (T-7.1 bis T-7.5). **Tonaufnahmen und ihre weitere Verarbeitung brauchen die Einwilligung des Kunden** (Maxi, 24.09.2026; §201 StGB, DSFA M1 bis M3). Das Protokoll bleibt als Vergleich bis Gate G4 sinnvoll.
