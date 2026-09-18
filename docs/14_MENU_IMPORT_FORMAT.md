# 14 – Menü-Importformat

> Die Karte wird im Chat aus PDF oder Fotos in diese CSV-Dateien gebracht. Claude Code importiert sie mit `scripts/import_menu.py`. Das Format ist der Vertrag zwischen beiden Seiten.
> Ablage: `imports/` (im `.gitignore`). UTF-8, Semikolon als Trenner, Dezimalkomma bei Preisen.

---

## `menu_items.csv`
| Spalte | Typ | Beispiel | Regel |
|---|---|---|---|
| number | Text | `23` | Pflicht, eindeutig. Text, weil „23a" vorkommen kann. **Nur Ziffern (führende Nullen erlaubt), optional ein Buchstabe a bis f**: `23`, `23a`, `007`. Alles andere (`23g`, `A12`, `12-3`) lehnt der Import ab - die Suche könnte es nicht eindeutig auflösen und fände sonst still ein anderes Gericht. Braucht eine Karte ein anderes Format, wird es bewusst erweitert. |
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
- Alias, der zu zwei Gerichten führt → Warnung, im Bericht sichtbar
- Bestehendes Gericht mit anderem Preis → im Bericht als „Preisänderung", erst mit `--apply-price-changes` übernommen

**Import ist idempotent.** Zweimal einspielen ändert nichts.

---

## Was im Chat entsteht

Aus der Karte werden die vier Dateien plus je Gericht 2–3 Aliase, wie Kunden es am Telefon sagen. Hinweise für die Aliase:
- Kurzform: „Frühlingsrollen" statt „Frühlingsrollen (4 Stück)"
- Umgangssprache: „die knusprige Ente"
- Aussprache vietnamesischer Namen, wie sie ankommt: „Fo", „Bun Bo", „Bao"
- Häufige Verwechslungen aus dem Betrieb (aus C1)
