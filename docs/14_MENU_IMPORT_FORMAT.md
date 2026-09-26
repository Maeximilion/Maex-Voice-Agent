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

**Import ist idempotent.** Zweimal einspielen ändert nichts.

---

## Was im Chat entsteht

Aus der Karte werden die vier Dateien plus je Gericht 2–3 Aliase, wie Kunden es am Telefon sagen. Hinweise für die Aliase:
- Kurzform: „Frühlingsrollen" statt „Frühlingsrollen (4 Stück)"
- Umgangssprache: „die knusprige Ente"
- Aussprache vietnamesischer Namen, wie sie ankommt: „Fo", „Bun Bo", „Bao"
- Häufige Verwechslungen aus dem Betrieb (aus C1)

---

## Quelle Kasse: `.dbf`-Dateien von <Kassensystem> (Stand 26.09.2026, Fragen aus 01 D11 geklärt)

Die Kasse speichert ihre Artikel als dBase-Tabellen. Sie ist Master für Menü und Preise (docs/02 §6), also kommt die Karte von dort statt aus dem Chat. Die Spaltennamen unten stammen aus einem Blick von Maxi in die Dateien; Beispielwerte sind verfremdet.

**Beschaffen, ohne etwas kaputt zu machen**
- Nur **Kopien**, nach Kassenschluss, nach `imports/kasse/` (liegt im `.gitignore`, kommt nie ins Repo).
- Gehört zu einer Tabelle eine gleichnamige `.fpt`- oder `.dbt`-Datei, kommt sie mit: darin stehen die langen Textfelder, ohne sie sind sie leer.
- **Die sechs Dateien** (Schreibweise wie in der Kasse, der Umwandler liest Groß- und Kleinschreibung gleich): `artikel.DBF` + `artikel.DBT`, `zutaten.DBF` + `zutaten.DBT`, `warengrp.dbf`, `zutgrp.DBF`. Der Demo-Ordner der Kasse (Vorlage für China-Restaurants) wird nicht gebraucht: Er zeigt nicht unsere Karte, und fremde Vorlagedaten kommen nicht ins Repo.
- Nie Kundentabellen der Kasse kopieren, nur Artikel, Warengruppen, Zutaten.
- `.dbf` statt CSV-Export: Spaltentypen und Zeichensatz stehen in der Datei, Umlaute bleiben heil, der Preisabgleich (T-4.9) kann die Kopie später ohne Handarbeit lesen.

### `artikel.DBF` → `menu_items.csv`

| Spalte Kasse | Beispiel | Ziel | Regel |
|---|---|---|---|
| `ARTNR` | `35B` | `number` und neues Feld `pos_code` | Schlüssel für Import, Preisabgleich und Kasseneingabe. `number` wird für die Suche klein gespeichert (`35b`, `importer.py`), die Kasse braucht aber ihre Schreibweise. Deshalb bringt T-4.11 ein eigenes Feld `menu_items.pos_code` (Migration, docs/03 dann nachziehen) mit der Nummer genau wie in der Kasse; Eingabezettel und Kassenübergabe drucken `pos_code`, nie `number` |
| `ARTNR2` | `  35B` | – | dieselbe Nummer, rechtsbündig mit Leerzeichen; nur zur Kontrolle |
| `WRG` | `008` | `category` über `warengrp.dbf` | |
| `K_BEZEICH` | `Geb. Nudeln Huhn` | – | Kurzname für den Bon, Kandidat für Aliase |
| `BEZEICH` | `Gebr. Nudeln mit Hühnerbrust` | `name` | **höchstens 40 Zeichen**, längere Namen sind abgeschnitten („…Rindfleisc"). Der Agent liest den Namen vor: abgeschnittene Namen in der Kasse korrigieren oder als Warnung im Bericht |
| `VK1_PREIS` | `13,5` | `price_eur` | **regulärer Preis, gilt am Telefon** (D11, Maxi 26.09.2026) |
| `VK2_PREIS`, `VK3_PREIS` | `13,5` | – | Abholer- und Restaurantpreis, optional in der Kasse, heute überall gleich `VK1_PREIS`. Weicht `VK2_PREIS` ab, ist das ein **Fehler** im Bericht, kein stiller Import: eine telefonische Bestellung ist eine Abholung, die Kasse könnte dann einen anderen Preis nehmen als der Agent nennt (Regel 1) |
| `A_PREIS1` … `A_PREIS6` | `7,90` | – | Aktionspreise, der Betrieb nutzt keine (D11). Ein Wert ungleich 0 wird im Bericht als Warnung gezeigt |
| `GROESSE`, `GRPREIS1` … `GRPREIS6` | `+-16`, `-9` | `item_options.csv`, Gruppe „Größe" | **Größe der Speise** (D11): Suppen gibt es klein und groß zu verschiedenen Preisen. Wird eine Pflichtgruppe „Größe" mit Default. Wie `+-16` und `GRPREIS1` … `6` die Größen und ihre Preise kodieren und wo die Namen „klein"/„groß" stehen, klärt T-4.11 an den echten Suppenzeilen; ohne eindeutige Deutung wird das Gericht nicht importiert (Fehler im Bericht), nie geraten |
| `DETAILS` | `Gebratene Nudeln mit Ente` | – | lange Menübeschreibung, vom Betrieb nicht genutzt (D11); wird nicht übernommen |
| `ZUTATEN` | `Gebratene_Nudeln, Ei, Sojasoße` | – (vorerst) | Rezeptur, **vom Betrieb vollständig gepflegt**, Unterstrich statt Leerzeichen. **Nie Quelle für Allergene** (Regel 1). Nutzen später: Prüfen, ob „ohne Zwiebeln" überhaupt drin ist |
| `ALLERGENE` | `ACFG` | `item_allergens.csv` | Kassenbuchstaben ohne Trenner, **nur über die Umsetztabelle unten**. Heute in der Kasse nicht gepflegt (Ausnahme: ein Testeintrag), also überall **keine Auskunft** |
| `ZUSATZ` | `.4.` | – | Zusatzstoffe 1 bis 11 (Legende unten), Nummern zwischen Punkten. Nicht gepflegt, im Datenmodell kein Feld; nicht Teil von T-4.11 |
| `DRUCKER` | leer | – | Druckerzuordnung der Kasse; wir drucken nicht nach Artikel |
| Rest (`BONUS`, `PUNKTE`, `BON_*`, `ERSTELLT`, `LT_*`, `ANZ_ORDER`, `EK_PREIS`, `LAGER`, `L_*`, `SPMNU`, `POCKETVS` …) | | – | nicht gebraucht |

Sonderfälle: Die Zeile mit `ARTNR` `000` ohne Name und Preis ist ein Platzhalter und fällt heraus. Varianten sind eigene Artikel (`35A` bis `35E`), keine Optionen; so bleiben sie auch bei uns, jede Position hat genau eine Artikelnummer der Kasse.

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

**Kasse als Master heißt auch: was dort fehlt, ist bei uns aus.** Der heutige Import lässt ein Gericht, das nicht in der Datei steht, unverändert aktiv und nennt es nur im Bericht („In der Datenbank, aber nicht in der Datei"). Für die Kasse als Quelle reicht das nicht, sonst bietet der Agent einen gestrichenen Artikel weiter an. T-4.11 baut deshalb: ein Artikel, der im Export fehlt oder in der Kasse gesperrt ist (ob `artikel.DBF` dafür eine Spalte hat, klärt T-4.11 an der echten Datei), wird bei uns `active = nein`. Wie Preisänderungen erst nach Sicht: `--dry-run` listet die Kandidaten, ein eigener Schalter übernimmt sie. Nie gelöscht, `order_items` verweisen auf das Gericht.

**Ablauf eines Kassenimports:** Kopien ziehen → Umwandler → `python -m scripts.import_menu imports/ --dry-run` → Bericht lesen (Preisänderungen, fehlende Artikel, Warnungen) → erneut mit `--apply-price-changes` und dem Schalter für fehlende Artikel. Ohne `--apply-price-changes` bleibt bei schon vorhandenen Gerichten der alte Preis stehen, und der Agent nennt einen anderen Preis als die Kasse.

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

Bei uns wird daraus je Gericht eine Gruppe in `item_options` mit `required` = `nein` und ohne Default: Gruppe = Zutatengruppe, Option = Zutat, `price_delta_eur` = Aufschlag oder Abzug. Das deckt sich mit D8: der Agent bietet nur an, was hier steht.

**Offen, klärt T-4.11 an den echten Dateien:** wie eine Zutat zu ihrer Gruppe und ihrem Preis kommt (die gezeigten Spalten `ZBEZEICH`, `PUBLIC`, `ZGRP`, `ZPREIS`, `ZGRP3` verbinden das nicht; `ZPREIS` 0,10 / 0,20 / 0,30 sieht eher nach Preisstufen aus) und wie ein Artikel seine erlaubten Gruppen findet (Vermutung `GROESSE`). Präfix „A.", „B." in `ZBEZEICH` ist vermutlich eine Sortierung und wird beim Vorlesen entfernt, ebenso der Unterstrich.

