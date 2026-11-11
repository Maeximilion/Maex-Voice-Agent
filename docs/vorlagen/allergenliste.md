# Allergenliste, Seitenrahmen (Entwurf)

> Draft, not legal advice (limits of D20). Serves L17 (`docs/09_OPERATIONS_LEGAL.md` section 1): the allergen list must be on the website before the first real order (LMIV). **No allergen value is written in this file** (hard rule 1): the values come from the database as maintained (`item_allergens`) and are filled into the table from there, or exported from it. Maxi publishes the page. Where a value is not maintained for a dish, the page says so and does not guess. **A printed copy goes with every delivery** (check of 11.11.2026): for food sold at a distance the information is due before the order and again at the moment of delivery (Art. 14(1) and (2) of Regulation (EU) No 1169/2011). Before the order the website alone is as a rule not enough for a guest who orders by phone; the assistant's spoken answer from the maintained values and the team cover that.

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

Kennzeichnungspflichtig nach § 5 LMZDV, auch bei telefonischer Bestellung (vor der Bestellung und bei der Lieferung, § 5 Abs. 2 Nr. 3 LMZDV): "mit Farbstoff", "mit Konservierungsstoff" oder "konserviert", "mit Antioxidationsmittel", "mit Geschmacksverstärker", "geschwärzt", "gewachst", "mit Phosphat", "mit Süßungsmittel(n)", "enthält eine Phenylalaninquelle", "kann bei übermäßigem Verzehr abführend wirken"; bei Pökelware "mit Nitritpökelsalz" oder "mit Nitrat".

| Nr. | Speise | Zusatzstoffe |
|---|---|---|
| <Artikelnummer> | <Name> | <gepflegte Angaben aus der Kasse> |

<offen: Die Datenbank führt keine Zusatzstoffe, und das Feld der Kasse ist nicht gepflegt (docs/14). Maxi pflegt sie in der Kasse und trägt sie hier ein; bis dahin sagt der Telefonassistent zu Zusatzstoffen nichts und gibt die Frage ans Team (#638). Eine leere Zelle bedeutet nie "ohne Zusatzstoffe".>

### Stand und Pflege

Die Liste wird bei jeder Änderung der Speisekarte neu aus der Datenbank erzeugt. Stand der letzten Pflege: <Datum>.
