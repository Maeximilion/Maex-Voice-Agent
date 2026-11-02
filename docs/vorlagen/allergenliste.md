# Allergenliste, Seitenrahmen (Entwurf)

> Draft, not legal advice (limits of D20). Serves L17 (`docs/09_OPERATIONS_LEGAL.md` section 1): the allergen list must be on the website before the first real order (LMIV). **No allergen value is written in this file** (hard rule 1): the values come from the database as maintained (`item_allergens`) and are filled into the table from there, or exported from it. Maxi publishes the page. Where a value is not maintained for a dish, the page says so and does not guess.

## Allergene in unseren Speisen

*Entwurf, keine Rechtsberatung. Stand: <Datum>.*

Diese Liste zeigt die Allergene der Zutaten unserer Speisen, wie wir sie gepflegt haben. Sie gilt für Bestellungen am Telefon und im Lokal. Kreuzkontakt oder Spuren in der Küche können wir nicht ausschließen. Haben Sie eine Allergie oder eine Frage, die über diese Liste hinausgeht, sagen Sie es bitte vor der Bestellung: Unser Team beantwortet sie oder ruft zurück, bevor Ihre Bestellung bestätigt wird. Der Telefonassistent gibt nur diese gepflegten Angaben weiter.

Kontakt bei Fragen: <Telefonnummer>, <E-Mail-Adresse>.

### Die 14 Allergengruppen (LMIV)

| Kürzel | Gruppe |
|---|---|
| <Kürzel aus der Datenbank> | glutenhaltiges Getreide (Weizen, Roggen, Gerste, Hafer, Dinkel, Kamut, Hybridstämme) |
| <Kürzel aus der Datenbank> | Krebstiere |
| <Kürzel aus der Datenbank> | Eier |
| <Kürzel aus der Datenbank> | Fisch |
| <Kürzel aus der Datenbank> | Erdnüsse |
| <Kürzel aus der Datenbank> | Sojabohnen |
| <Kürzel aus der Datenbank> | Milch und Milcherzeugnisse (einschließlich Laktose) |
| <Kürzel aus der Datenbank> | Schalenfrüchte (Mandeln, Haselnüsse, Walnüsse, Cashewnüsse, Pecannüsse, Paranüsse, Pistazien, Macadamianüsse) |
| <Kürzel aus der Datenbank> | Sellerie |
| <Kürzel aus der Datenbank> | Senf |
| <Kürzel aus der Datenbank> | Sesamsamen |
| <Kürzel aus der Datenbank> | Schwefeldioxid und Sulfite (mehr als 10 mg/kg oder 10 mg/l) |
| <Kürzel aus der Datenbank> | Lupinen |
| <Kürzel aus der Datenbank> | Weichtiere |

<offen: Die Kürzel sind die der Datenbank (docs/03, `item_allergens`). Beim Ausfüllen aus der Datenbank übernehmen, nicht erfinden.>

### Tabelle der Speisen

Erzeugt aus der Datenbank; jede Zeile enthält nur gepflegte Werte.

| Nr. | Speise | Allergene (Kürzel) | Hinweis |
|---|---|---|---|
| <Artikelnummer aus der Datenbank> | <Name aus der Datenbank> | <gepflegte Kürzel> | <Pflegestatus> |

Bei einer Speise ohne gepflegte Angabe steht: "Allergene nicht gepflegt, bitte beim Team nachfragen." Eine leere Zelle bedeutet nie "allergenfrei".

### Zusatzstoffe

<offen: Das Repo führt keine Zusatzstoffe (Farbstoff, Konservierungsstoff usw.); Maxi prüft selbst, ob die Speisekarte sie kennzeichnen muss.>

### Stand und Pflege

Die Liste wird bei jeder Änderung der Speisekarte neu aus der Datenbank erzeugt. Stand der letzten Pflege: <Datum>.
