# 14 – Menü-Importformat

> Die Karte wird im Chat aus PDF oder Fotos in diese CSV-Dateien gebracht. Claude Code importiert sie mit `scripts/import_menu.py`. Das Format ist der Vertrag zwischen beiden Seiten.
> Ablage: `imports/` (im `.gitignore`). UTF-8, Semikolon als Trenner, Dezimalkomma bei Preisen.
> Zweite Quelle seit 26.09.2026: die Artikeldateien der Kasse (`.dbf`), Zuordnung im letzten Abschnitt. Ein Umwandler (T-4.11) macht daraus diese CSV-Dateien, der Import bleibt einer.

---

## `menu_items.csv`
| Spalte | Typ | Beispiel | Regel |
|---|---|---|---|
| number | Text | `23` | Pflicht, eindeutig. Text, weil „23a" vorkommen kann. **Nur Zahlen bis 999 (führende Nullen erlaubt), optional ein Buchstabe a bis f**: `23`, `23a`, `007`. Der Buchstabe wird klein gespeichert, `23A` und `23a` sind dieselbe Nummer. Alles andere (`23g`, `A12`, `12-3`) lehnt der Import ab - die Suche könnte es nicht eindeutig auflösen und fände sonst still ein anderes Gericht. Braucht eine Karte ein anderes Format, wird es bewusst erweitert. |
| name | Text | `Frühlingsrollen (4 Stück)` | Pflicht |
| category | Text | `Vorspeisen` | Pflicht, freie Gruppierung |
| price_eur | Dezimal | `6,90` | Pflicht, wird zu `690` Cent. Nur Ziffern und Komma. |
| description | Text | `mit Gemüsefüllung, dazu süßsaure Sauce` | optional |
| active | `ja`/`nein` | `ja` | Default `ja` |
| pos_code | Text | `35B` | optional, Spalte darf fehlen (T-4.11). Artikelnummer genau wie in der Kasse, für Eingabezettel und Kassenübergabe; doppelt ist ein Fehler. Fehlt die Spalte, bleibt der gespeicherte Wert |

## `item_options.csv`
| Spalte | Beispiel | Regel |
|---|---|---|
| number | `47` | muss in `menu_items.csv` existieren |
| group_name | `Fleisch` | |
| option_name | `Huhn` | |
| price_delta_eur | `0,00` / `2,50` / `-1,00` | negativ erlaubt |
| is_default | `ja`/`nein` | genau ein Default je Pflichtgruppe |
| required | `ja`/`nein` | gilt für die ganze Gruppe, muss je Gruppe gleich sein |
| price_reason | `Nudeln brauchen eine zweite Station in der Küche` | optional, Spalte darf fehlen. Warum die Option mehr kostet: der Agent nennt nur diesen Satz, wenn der Gast fragt, nie eine eigene Begründung. Leer heißt, der Preis steht so in der Karte (T-4.10) |

## `item_allergens.csv`
| Spalte | Beispiel | Regel |
|---|---|---|
| number | `23` | |
| allergen_codes | `A,F` | LMIV-Buchstaben, kommagetrennt. Leere Zeile = **keine Auskunft**, nicht „keine Allergene". |
| confirmed_by | `Maxi` | wer es geprüft hat |

## `item_aliases.csv`
| Spalte | Beispiel | Regel |
|---|---|---|
| number | `23` | |
| alias | `die knusprigen Rollen` | Kleinschreibung egal, wird normalisiert. Mehrere Zeilen je Gericht. |

---

## Prüfregeln des Importers (`--dry-run` zeigt sie)

- Doppelte `number` → Fehler
- Preis nicht parsebar → Fehler
- Option zu unbekannter Nummer → Fehler
- Pflichtgruppe ohne Default → Fehler
- Gericht ohne Alias → Warnung, Liste für den Chat
- Alias, der zu zwei Gerichten führt → Warnung, im Bericht sichtbar. Verglichen
  wird wie in der Suche, also ohne Füllwörter: „Ente“ und „die Ente“ an zwei
  Gerichten sind eine Kollision, auch wenn die Zeilen verschieden aussehen
- Alias nur aus Füll- oder Zahlwörtern („bitte“, „die“, „x“, „23“) → Fehler: die
  Suche fiele darauf nie zurück, der Alias wäre gespeichert und unerreichbar.
  Eine Ziffer im Wort ist erlaubt, solange etwas übrig bleibt („7up“)
- Bestehendes Gericht mit anderem Preis → im Bericht als „Preisänderung", erst mit `--apply-price-changes` übernommen
- Gericht in der Datenbank, aber nicht in der Datei → im Bericht; mit `--deactivate-missing` wird es `active = nein`, nie gelöscht (Kasse als Quelle, T-4.11). Eine Datei ohne Gerichte oder mehr als die Hälfte der aktiven Karte weg → verweigert, außer mit `--allow-large-deactivation`: ein kaputter Export schaltet nie die Karte ab

**Import ist idempotent.** Zweimal einspielen ändert nichts.

---

## Was im Chat entsteht

Aus der Karte werden die vier Dateien plus je Gericht 2–3 Aliase, wie Kunden es am Telefon sagen. Hinweise für die Aliase:
- Kurzform: „Frühlingsrollen" statt „Frühlingsrollen (4 Stück)"
- Umgangssprache: „die knusprige Ente"
- Aussprache vietnamesischer Namen, wie sie ankommt: „Fo", „Bun Bo", „Bao"
- Häufige Verwechslungen aus dem Betrieb (aus C1)

---

## Quelle Kasse: `.dbf`-Dateien von <Kassensystem> (Stand 26.09.2026, an den echten Dateien geprüft in T-4.11)

Die Kasse speichert ihre Artikel als dBase-Tabellen. Sie ist Master für Menü und Preise (docs/02 §6), also kommt die Karte von dort statt aus dem Chat. Spalten und Deutungen unten sind an den echten Dateien geprüft (T-4.11); Beispielwerte sind verfremdet.

**Format:** dBase IV mit Memo-Dateien (`.DBT`, 1024-Byte-Blöcke), Zeichensatz laut Kopf German OEM (cp437). Gelesen mit eigenem Leser `api/domain/menu/pos_dbf.py` (nur Standardbibliothek, nur lesend); ein unbekannter Zeichensatz ist ein Fehler statt einer geratenen Codepage.

**Umwandler:** `python -m scripts.kasse_to_csv imports/kasse --out imports [--allergens-confirmed-by <Name>]` schreibt `menu_items.csv` (mit `pos_code`), `item_options.csv` und `item_allergens.csv`; `item_aliases.csv` aus dem Chat bleibt; Zeilen zu Nummern, die die Kasse in diesem Lauf nicht liefert (Getränk, gesperrt, Nummer unlesbar, Fehler im Bericht), legt er in `item_aliases.verworfen.csv`, weil der Import sonst ganz abbricht. Beide Dateien werden bei jedem Lauf neu aufgeteilt: kommt ein Gericht zurück, kommen seine Aliase von selbst zurück. Ohne ein einziges übernommenes Gericht schreibt der Umwandler nichts (Exit 2). Eine leere Preisstufe in `zutgrp` ist ein Fehler, kein Gratis-Extra; eine kaputte Datei (abgeschnitten, unlesbarer Memo-Zeiger, fehlende Spalte einer anderen Kassenversion) endet mit Exit 2 und nennt die Stelle, nicht mit einem Traceback. Regeln in `api/domain/menu/pos_convert.py`. Exit 0 geschrieben, 1 geschrieben mit Fehlern im Bericht (diese Artikel fehlen in der CSV), 2 nicht lesbar oder Ziel gesperrt (etwa in Excel offen): die drei Import-Dateien sind nicht getauscht, nicht importieren; 3 Import-Dateien geschrieben, Aliase nicht abgeglichen. Hat der Chat die Aliase eines fehlenden Gerichts neu geschrieben, gelten nur die neuen.

**Beschaffen, ohne etwas kaputt zu machen**
- Nur **Kopien**, nach Kassenschluss, nach `imports/kasse/` (liegt im `.gitignore`, kommt nie ins Repo).
- Gehört zu einer Tabelle eine gleichnamige `.fpt`- oder `.dbt`-Datei, kommt sie mit: darin stehen die langen Textfelder, ohne sie sind sie leer.
- **Die sechs Dateien** (Schreibweise wie in der Kasse, der Umwandler liest Groß- und Kleinschreibung gleich): `artikel.DBF` + `artikel.DBT`, `zutaten.DBF` + `zutaten.DBT`, `warengrp.dbf`, `zutgrp.DBF`. Der Demo-Ordner der Kasse (Vorlage für China-Restaurants) wird nicht gebraucht: Er zeigt nicht unsere Karte, und fremde Vorlagedaten kommen nicht ins Repo.
- Nie Kundentabellen der Kasse kopieren, nur Artikel, Warengruppen, Zutaten.
- `.dbf` statt CSV-Export: Spaltentypen und Zeichensatz stehen in der Datei, Umlaute bleiben heil, der Preisabgleich (T-4.9) kann die Kopie später ohne Handarbeit lesen.

### `artikel.DBF` → `menu_items.csv`

| Spalte Kasse | Beispiel | Ziel | Regel |
|---|---|---|---|
| `ARTNR` | `35B` | `number` und `pos_code` | Schlüssel für Import, Preisabgleich und Kasseneingabe. `number` wird für die Suche klein gespeichert (`35b`, `importer.py`), die Kasse braucht aber ihre Schreibweise: Feld `menu_items.pos_code` (Migration 004) mit der Nummer genau wie in der Kasse; Eingabezettel und Kassenübergabe drucken `pos_code`, nie `number`. Nummern, die die Suche nicht versteht (`25G`, Sushi `S1` … `S53`, `SM1` … `SM6`), werden nicht übernommen, Fehler im Bericht; das Nummernformat zu erweitern ist eine eigene Aufgabe |
| `ARTNR2` | `  35B` | – | dieselbe Nummer, rechtsbündig mit Leerzeichen; nur zur Kontrolle |
| `WRG` | `008` | `category` über `warengrp.dbf` | |
| `K_BEZEICH` | `Geb. Nudeln Huhn` | – | Kurzname für den Bon, Kandidat für Aliase |
| `BEZEICH` | `Gebr. Nudeln mit Hühnerbrust` | `name` | **höchstens 40 Zeichen**, längere Namen sind abgeschnitten („…Rindfleisc"). Der Agent liest den Namen vor: abgeschnittene Namen in der Kasse korrigieren oder als Warnung im Bericht |
| `VK1_PREIS` | `13,5` | `price_eur` | **regulärer Preis, gilt am Telefon** (D11, Maxi 26.09.2026) |
| `VK2_PREIS`, `VK3_PREIS` | `13,5` | – | Abholer- und Restaurantpreis, optional in der Kasse, heute überall gleich `VK1_PREIS`. Weicht `VK2_PREIS` ab, ist das ein **Fehler** im Bericht, kein stiller Import: eine telefonische Bestellung ist eine Abholung, die Kasse könnte dann einen anderen Preis nehmen als der Agent nennt (Regel 1) |
| `A_PREIS1` … `A_PREIS6` | `7,90` | – | Aktionspreise, der Betrieb nutzt keine (D11). Ein Wert ungleich 0 wird im Bericht als Warnung gezeigt |
| `GROESSE`, `GRPREIS1` … `GRPREIS6` | `+-23`, `4.50` | `item_options.csv`, Gruppe „Größe" | **Größe der Speise** (D11). `GROESSE` ist eine Zeichenfolge: `+` Extras erlaubt, `-` Weglassen erlaubt, jede Ziffer eine Größe, in der der Artikel verkauft wird. Größe 1 kostet `VK1_PREIS`, Größe n ab 2 kostet `VK1_PREIS + GRPREIS(n-1)` (Aufschlag, kein fester Preis). Belegt an der Maske (Suppe `+-23`: klein 6,50 = VK1, groß 11,00 = VK1 + `GRPREIS2` 4,50) und am Wein (6,50 + 23,50 = 30,00, der Preis der Flasche). Die Namen stehen nicht in den Dateien, sondern in der Maske der Kasse („Größen Bezeichnung ändern"): **1 normal, 2 klein, 3 groß, 4 party**, 5 bis 7 ohne Namen (Maxi 26.09.2026). Mehr als eine Größe mit Namen → Pflichtgruppe „Größe", Default die kleinste Nummer, `price_eur` ist ihr Preis. Größe ohne Namen → nicht übernommen, Warnung (heute Größe 6 bei neun Hauptgerichten, Salaten und Pho); keine Größe mit Namen → Gericht nicht übernommen, Fehler. `GRPREIS`-Spalten von Größen, die nicht in `GROESSE` stehen, sind Altwerte und zählen nicht |
| `DETAILS` | `Gebratene Nudeln mit Ente` | – | lange Menübeschreibung, vom Betrieb nicht genutzt (D11); wird nicht übernommen |
| `ZUTATEN` | `Gebratene_Nudeln, Ei, Sojasoße` | – (vorerst) | Rezeptur, **vom Betrieb vollständig gepflegt**, Unterstrich statt Leerzeichen. **Nie Quelle für Allergene** (Regel 1). Nutzen später: Prüfen, ob „ohne Zwiebeln" überhaupt drin ist |
| `ALLERGENE` | `ACFG` | `item_allergens.csv` | Kassenbuchstaben ohne Trenner, **nur über die Umsetztabelle unten**. Heute in der Kasse nicht gepflegt (Ausnahme: Testeinträge bei vier Gerichten), also überall **keine Auskunft**. Der Umwandler schreibt je Gericht eine Zeile: leer = keine Auskunft; Buchstaben nur mit `--allergens-confirmed-by`, sonst Fehler und leere Zeile; unbekannter Buchstabe → Fehler und leere Zeile. Der Bericht nennt jede Umsetzung („13 k -> N") zum Prüfen |
| `ZUSATZ` | `.4.` | – | Zusatzstoffe 1 bis 11 (Legende unten), Nummern zwischen Punkten. Nicht gepflegt, im Datenmodell kein Feld; nicht Teil von T-4.11 |
| `DRUCKER` | leer | – | Druckerzuordnung der Kasse; wir drucken nicht nach Artikel |
| Rest (`BONUS`, `PUNKTE`, `BON_*`, `ERSTELLT`, `LT_*`, `ANZ_ORDER`, `EK_PREIS`, `LAGER`, `L_*`, `SPMNU`, `POCKETVS` …) | | – | nicht gebraucht |

Sonderfälle: Die Zeile mit `ARTNR` `000` ohne Name und Preis ist ein Platzhalter und fällt heraus. Varianten sind eigene Artikel (`35A` bis `35E`), keine Optionen; so bleiben sie auch bei uns, jede Position hat genau eine Artikelnummer der Kasse.

**Gesperrte Artikel:** `artikel.DBF` hat **keine Sperr-Spalte** (`BEARB`, `SPMNU` überall `F`; `L_AKT`, `LAGER` sind Lagerbestand). Die Kasse sperrt, indem sie die Zeile als gelöscht markiert (dBase-Markierung `*`, heute 376 von 667 Zeilen). Gelöschte Zeilen übernimmt der Umwandler nie; die Kasse vergibt Nummern neu (84 Nummern stehen gelöscht und aktiv zugleich), nur die aktive Zeile gilt.

**Warengruppen ohne Telefonbestellung** (Maxi 26.09.2026, Standard in `scripts/kasse_to_csv.py`, mit `--skip-groups` überschreibbar): `015` bis `025` (Getränke), `100` bis `102` (Menüs), `EXS`, `FRE`, `GAH`, `GEH`, `GET`, `OHN`, `PFA`, `RTN`, `SON`. Übernommen werden die Speisen aus `001` bis `013`.

**Stand 26.09.2026:** Allergene und Zusatzstoffe sind in der Kasse nicht gepflegt, die Zutaten schon. Einmal gepflegt, ist die Kasse auch hier die einzige Quelle (docs/02 §6): Maxi hakt die Allergene je Artikel in der Kasse an, der nächste Export bringt sie mit. `confirmed_by` setzt der Umwandler aus einem Aufrufparameter (wer die Liste geprüft hat), nie von selbst. Der Umwandler warnt, wenn `ZUTATEN` einen typischen Allergenträger nennt (Ei, Soja, Weizen …) und `ALLERGENE` leer ist; die Warnung ist nur für Menschen, der Agent sagt weiter „keine Auskunft".

**Umsetztabelle Allergene: Kasse → Datenbank.** Die Kasse zählt die 14 Hauptallergene der EU (LMIV Anhang II) von a bis n **ohne Lücke** durch. Die übliche Kennzeichnung und unsere Datenbank (docs/03 `item_allergens`) überspringen I, J, K und Q. Ab dem neunten Allergen bedeuten dieselben Buchstaben also etwas anderes. Eine 1:1-Übernahme würde aus Sulfiten Sellerie machen (`l`), aus Lupinen Senf (`m`) und aus Weichtieren Sesam (`n`), ohne dass die Datenbank widerspricht. Deshalb nur über diese Tabelle, und jeder unbekannte Buchstabe ist ein Fehler:

| Kasse | Allergen | DB |
|---|---|---|
| a | Glutenhaltiges Getreide | A |
| b | Krebstiere | B |
| c | Eier | C |
| d | Fische | D |
| e | Erdnüsse | E |
| f | Sojabohnen | F |
| g | Milch inkl. Laktose | G |
| h | Schalenfrüchte | H |
| **i** | Sellerie | **L** |
| **j** | Senf | **M** |
| **k** | Sesamsamen | **N** |
| **l** | Schwefeldioxid und Sulphite | **O** |
| **m** | Lupinen | **P** |
| **n** | Weichtiere | **R** |

Gespeichert sind die Buchstaben groß (`AG`), die Maske der Kasse zeigt sie klein. Zusatzstoffe laut Maske: 1 Farbstoff, 2 Konservierungsstoff, 3 Antioxidationsmittel, 4 Geschmacksverstärker, 5 Schwefeldioxid, 6 Schwärzungsmittel, 7 Phosphat, 8 Milcheiweiß, 9 koffeinhaltig, 10 chininhaltig, 11 Süßungsmittel.

**Kasse als Master heißt auch: was dort fehlt, ist bei uns aus.** Der heutige Import lässt ein Gericht, das nicht in der Datei steht, unverändert aktiv und nennt es nur im Bericht („In der Datenbank, aber nicht in der Datei"). Für die Kasse als Quelle reicht das nicht, sonst bietet der Agent einen gestrichenen Artikel weiter an. T-4.11 baut deshalb: ein Artikel, der im Export fehlt oder in der Kasse gesperrt (gelöscht markiert) ist, wird bei uns `active = nein`. Wie Preisänderungen erst nach Sicht: `--dry-run --deactivate-missing` listet die Kandidaten („würden deaktiviert"), ohne `--dry-run` übernimmt der Schalter sie. Nie gelöscht, `order_items` verweisen auf das Gericht. Steht der Artikel wieder in der Kasse, macht ihn der nächste Import wieder aktiv.

**Ablauf eines Kassenimports:** Kopien nach `imports/kasse/` → `python -m scripts.kasse_to_csv imports/kasse --out imports` → Bericht des Umwandlers lesen → `python -m scripts.import_menu imports/ --dry-run --deactivate-missing` → Bericht lesen (Preisänderungen, fehlende Artikel, Warnungen) → erneut ohne `--dry-run` mit `--apply-price-changes --deactivate-missing`. Ohne `--apply-price-changes` bleibt bei schon vorhandenen Gerichten der alte Preis stehen, und der Agent nennt einen anderen Preis als die Kasse.

### `warengrp.dbf` → `category`

| Spalte | Beispiel | Regel |
|---|---|---|
| `W_WRG` | `001` | Schlüssel, passt zu `Artikel.WRG` |
| `W_BEZEICH` | `Suppe` | **wird `category`**: die feinere Gruppe. Ist ein Gericht aus, schlägt der Agent Gerichte derselben Kategorie vor (T-4.8), bei Suppe also Suppe |
| `W_LBEZEICH` | `Vorspeisen` | gröbere Gruppe, nicht gebraucht |
| `W_MWST`, `W_MWST2` | `A` | Steuer rechnet die Kasse, nicht gebraucht |

### `zutaten.DBF` und `zutgrp.DBF` → `item_options.csv`

In der Kasse sind „Zutaten" **Extras**: Nach der Artikelnummer tippt das Team auf „+" und wählt, was der Gast dazu- oder abbestellt, mit Aufschlag oder Abzug. Gedacht war das für Pizzerien. Eine Zutatengruppe legt fest, bei welchen Gerichten eine Zutat wählbar ist. Beispiele von Maxi:
- „Mango-Curry-Soße" +2,90 € bei gebratenen Nudeln und gebratenem Reis, nicht bei Suppen
- „Nudeln statt Reis" +3,50 € bei Reisgerichten

Bei uns wird daraus je Gericht eine Gruppe in `item_options` mit `required` = `nein` und ohne Default: Gruppe „Extras", Option = Zutat, `price_delta_eur` = Aufschlag. Das deckt sich mit D8: der Agent bietet nur an, was hier steht.

**Geklärt an den echten Dateien (T-4.11):**
- `zutgrp.DBF` ist keine Gruppe, sondern eine Tabelle von **Preisstufen**: `ZGRP`/`ZGRP3` → `ZPREIS` (`1` = 0,10 … `A` = 1,00, `U` = 3,50, `Z` = 9,40).
- Eine Zutat hängt über **Warengruppen** am Artikel, nicht über `GROESSE`: wählbar, wenn `WRGSHOWALL` = `T` ist oder `artikel.WRG` in der Liste `WRGSHOW` steht (Memo, eine Warengruppe je Zeile). Zusätzlich muss `GROESSE` des Artikels ein `+` enthalten (Annahme: `+` = Extras erlaubt; ohne `+` bietet der Agent weniger an, nie mehr).
- Preis = Preisstufe `ZPREIGRP3` plus `ZGRPREIS(n-1)` für Größe n ab 2, gleiche Zählung wie beim Artikel. Belegt: alle aktiven Zutaten passen zu `ZPREIGRP3` („Nudeln statt Reis" 3,50 wie im Beispiel oben), bei Größe 6 heben sich Stufe und Abschlag auf 0 auf; `Extra_Ente` hat gar kein `ZPREIGRP`. Mango-Curry kostet heute 3,50, nicht 2,90 wie im Beispiel oben.
- Gelöschte Zutaten (33 von 44) fallen heraus.

Die Gruppe heißt je Gericht **„Extras"** (benannte Zutatengruppen gibt es nicht). Kostet eine Zutat je Größe des Gerichts verschieden, wird sie bei diesem Gericht nicht übernommen (unsere Option hat einen Preis je Gericht), Warnung. Präfix „A." … „G." in `ZBEZEICH` ist eine Sortierung und fällt weg, ebenso der Unterstrich (`B.Mango_Curry` → „Mango Curry"). `ZBEZEICH` hat nur 16 Zeichen: „Panierte_Hühnerb", „Nudeln_statt_Rei" sind abgeschnitten, Warnung im Bericht.

