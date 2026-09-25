# 06 – GUI

> Zielbild: Ein neues Teammitglied bedient die Betriebsansicht nach 5 Minuten ohne Erklärung, mit fettigen Fingern, auf einem Tablet, während das Telefon klingelt.

---

## 1. Bedienregeln

1. **Jede Aktion in höchstens 2 Taps.** Kein Menü, kein Untermenü, keine Suche für den Normalfall.
2. **Keine Fachbegriffe.** Nicht „Order", sondern „Bestellung". Nicht „Status", sondern was zu tun ist.
3. **Große Ziele.** Knöpfe mindestens 60 × 60 px, Schrift mindestens 18 px.
4. **Farbe ist nie die einzige Information.** Zusätzlich Symbol und Text (Küchenbeleuchtung, Sehschwächen).
5. **Live, ohne Nachladen.** Neue Bestellungen erscheinen von selbst, mit Ton.
6. **Gefährliche Aktionen fragen nach.** Löschen und Stornieren brauchen eine Bestätigung, alles andere nicht.
7. **Immer sichtbar:** Ist die KI an? Klemmt etwas? Ein Blick genügt.

---

## 2. Technik

**Entschieden (D6, 17.09.2026):** FastAPI + Jinja2 + HTMX + Server-Sent-Events. Statt Pico.css traegt `api/gui/static/app.css` die Variablen und Klassen des abgenommenen Mockups (§7) - eine Quelle fuer die Gestaltung statt zwei, und eine Abhaengigkeit weniger. HTMX liegt lokal in `api/gui/static/`, nichts kommt aus einem CDN.

| Warum | |
|---|---|
| Ein Container | keine zweite Laufzeit, kein Node-Build, kein CORS |
| SSE für Live-Updates | der Server schickt neue Bestellungen, kein Polling |
| Serverseitige Templates | Logik bleibt an einer Stelle |
| Offline-tauglicher Kern | keine externen CDN-Abhängigkeiten |

**Verworfen:** React + Vite. Bleibt der Weg, falls spaeter komplexe Interaktionen dazukommen (Drag-and-Drop im Tourenplan, Kartenansicht fuer Zonen); fuer Listen und grosse Knoepfe auf einem Tablet kostet es einen zweiten Container ohne Gegenwert.

**Endgeräte:** Tablet im Querformat (Betrieb), PC im Büro (Admin). Kein Handy-Layout in Stufe 1.

---

## 3. Betriebsansicht (Tablet)

### Kopfzeile, immer sichtbar
```text
┌──────────────────────────────────────────────────────────────────┐
│  ● KI nimmt an      Lieferung an      Wartezeit 25 / 50 Min      │
│  [ KI pausieren ]  [ Lieferung aus ]  [ Wartezeit +15 ] [ +30 ]  │
└──────────────────────────────────────────────────────────────────┘
```
- **Der Punkt hat drei Zustände:** grün „KI nimmt an" (Modus `primary`) · gelb „KI springt ein" (Modus `overflow`, nimmt nur ab, wenn niemand abnimmt) · rot „KI ist aus" (`paused`). Der Text steht immer daneben, Farbe allein reicht nicht.
- **KI pausieren** ist der Not-Aus. Ein Tap, sofortige Wirkung, alle Anrufe gehen ans Team. Wieder einschalten braucht eine Bestätigung.
- **Wartezeit +15 / +30** erhöhen die Wartezeit. Immer mit dem Wort „Wartezeit", sonst ist unklar, was der Knopf erhöht. Die neue Zeit gilt für jeden folgenden Anruf.

### Drei Spalten

**Spalte 1 – Neue Bestellungen**
```text
┌──────────────────────────────┐
│ + 18:42   Abholung   A17    │
│ Müller · +49 176 …           │
│ 2× 23 Frühlingsrollen        │
│    └ Erdnusssauce            │
│ 1× 47 Ente knusprig          │
│ ────────────────────────     │
│ 24,80 €      fertig 19:10    │
│ [ Passt ]  [ Korrigieren ]│
└──────────────────────────────┘
```
- Neue Karte → Ton plus kurzes Blinken
- **Abholcode** („A17") groß und farbig hinterlegt: Der Kunde sagt ihn am Tresen, das Team muss ihn aus zwei Metern lesen.
- Positionen zeigen die Kartennummer grau vor dem Namen. Die Küche arbeitet mit Nummern.
- **Passt** → ab in die Küche (in Stufe 5 ist das die Freigabe)
- **Korrigieren** → Positionen ändern, Grund wählen (falsches Gericht · falsche Menge · falsche Adresse · Sonstiges). Jede Korrektur wird gezählt und ist der Rohstoff für die Genauigkeits-KPI.
- **Rote Karte = Übergabe fehlgeschlagen.** Eigener Zustand einer Bestellkarte (`handover_state = failed`), kein Fehlerbanner: Die Bestellung ist gebucht und bestätigt, nur der Bon fehlt. Roter Rahmen, Zeile „Küche nicht erreicht", ein Knopf „Nochmal senden". Bleibt oben in Spalte 1, bis die Übergabe gelingt. Rot wird sie, sobald die Küche den Bon nicht hat: Druckfehler oder 60 s ohne Abholung durch die Druckbrücke (T-4.6); druckt die Brücke den Bon doch noch, wird sie von selbst wieder normal.

**Gebaut (T-4.7, 25.09.2026)** - `domain/ordering/board.py`, `correction.py`, `ticket.py`, `gui/orders_view.py`, `fragments/bestellungen.html`, `fragments/korrektur.html`:
- In der Spalte steht, was das Team noch tun muss: bestätigte Bestellungen des Betriebstags, die noch niemand abgehakt hat; jede, die noch auf die Freigabe wartet, und jede rote Karte - beide auch über 05:00 hinaus, die rote auch nach „Passt". Rote Karten oben, danach die älteste. Zähler in der Überschrift, neue Karte mit eigenem Ton, Blinken und Rollen ins Bild.
- **Passt je Modus:** Außerhalb von `primary` wartet die Bestellung ohne Bon („Noch nicht in der Küche", gelber Rand, Knopf „Passt, ab in die Küche"); Passt setzt `approved` und schickt den Bon. In `primary` hat die Küche den Bon schon („Küche hat den Bon"); Passt hakt nur ab. Eine rote Karte hat kein Passt, sondern „Nochmal senden".
- **Korrigieren** öffnet einen eigenen Kasten über den Spalten, damit die Spalte live bleibt und nichts überschreibt, was das Team gerade tippt. Menge −/+, Tauschen (Gericht per Kartennummer ersetzen, Menge und Hinweis bleiben), Gericht per Nummer dazu, Auswahl je Gruppe als große Knöpfe (Pflicht wird nie vorbelegt). Jeder Tap rechnet am Server neu; gespeichert wird mit einem Tap auf den Grund. Die Knöpfe sind gesperrt, solange nichts geändert ist, eine Pflichtauswahl fehlt oder kein Gericht bliebe - der Grund steht als Text darüber. „Falsche Adresse" nur bei Lieferungen. Nach 90 Sekunden ohne Tap schließt der Kasten.
- **Hinweise gehen nie still verloren:** Eine Position mit Hinweis (z. B. „WICHTIG: Keine Erdnüsse. Grund: Allergie") fällt mit Minus nicht weg; „Entfernen" fragt nach und nennt den Hinweis. Für „falsches Gericht" ist Tauschen der Weg.
- Ein Gericht, das heute aus ist, darf das Team nehmen; es steht „heute aus" daneben.
- Jede Korrektur steht als `order.corrected` im `audit_log` mit Grund und vorher/nachher (ohne Name, Telefon, Hinweistext) - Rohstoff für die Genauigkeit (T-8.4).

**Spalte 2 – Heute**
- Reservierungen des Tages nach Uhrzeit, mit Personenzahl und Notiz
- Laufende Bestellungen mit Zeitbalken
- Überfällige Positionen wandern nach oben und werden orange

**Spalte 3 – Rückrufe**
```text
┌──────────────────────────────┐
│ Annahme: 18:39  Beschwerde          │
│ +49 176 …                    │
│ „Letzte Lieferung war kalt"  │
│ [ Anrufen ]  [ Erledigt ]│
└──────────────────────────────┘
```
Beschwerden stehen immer oben und haben einen eigenen Ton.

### Der Knopf „Gericht aus"
Eigene Kachel, führt zu einer Liste mit Suchfeld und großen Schaltern. Ein Tap = ausverkauft bis Betriebsschluss. Der Agent bietet das Gericht ab sofort nicht mehr an und nennt eine Alternative.

---

## 4. Adminansicht (PC)

**Immer sichtbar, in jedem Bereich:** vier Kennzahlen der letzten 7 Tage — Genauigkeit, Kosten je Bestellung, Anteil an das Team übergeben, verpasste Anrufe. Der Bereich „Kennzahlen" zeigt dann den Verlauf. Bereiche als Reiter, keine Seitenleiste.

| Bereich | Inhalt |
|---|---|
| **Menü** | Liste mit Suche nach Name oder Nummer, kein Kachel-Raster. Zeile = Nummer grau, Name, Preis, Warnbadges. Aufklappen zeigt Optionen, Allergene und **Aliase mit Trefferzahl**. Darunter **Vorschläge aus Anrufen**: Alias, Anzahl „nicht erkannt", Knopf „Annehmen" (aus der Schattenmessung, Stufe 4). Warnbadges in der Zeile: „Allergene fehlen" (gelb, KI gibt keine Auskunft) · „Kasse: 13,50 €" (rot, Preisabgleich hat eine Abweichung gefunden, Klick übernimmt oder verwirft). |
| **Zeiten** | Öffnungszeiten je Wochentag und Service, Sondertage, Urlaub |
| **Lieferung** | Zonen mit Pauschale, Mindestbestellwert, Lieferzeit. PLZ-Liste oder Karte. |
| **Anrufe** | Liste mit Filter: Datum, Ergebnis, Absicht. Je Anruf: Dauer, Tool-Aufrufe, Kosten, Ergebnis, Transkript (falls vorhanden). |
| **Kennzahlen** | Genauigkeit, Eskalationsquote, Abbrüche, verpasste Anrufe, Kosten je Anruf und je Bestellung. Tag, Woche, Monat. |
| **Evals** | letzter Lauf, Verlauf der Genauigkeit über die Prompt-Versionen, fehlgeschlagene Fälle im Detail |
| **Daten** | Löschfristen, manuelles Löschen auf Kundenwunsch, Exporte |

---

## 5. Zustände und Rückmeldungen

| Zustand | Darstellung |
|---|---|
| Lädt | Skelett-Karten, kein Spinner über dem ganzen Bild |
| Leer | „Noch keine Bestellungen heute." Kein leerer Kasten. |
| Fehler | rote Leiste oben mit Klartext und Knopf „Nochmal versuchen" |
| Verbindung weg | gelbe Leiste „Keine Verbindung — Anrufe gehen ans Telefon", automatischer Wiederverbindungsversuch |
| KI pausiert | Kopfzeile wird grau, Punkt wird rot, Text „KI ist aus" |
| KI im Überlauf | Punkt gelb, Text „KI springt ein" |
| Übergabe fehlgeschlagen | Bestellkarte mit rotem Rahmen und „Nochmal senden", siehe Spalte 1 |

---

## 6. Was die GUI **nicht** macht

- Keine Zahlungsabwicklung (bezahlt wird vor Ort oder an der Tür)
- Keine Tourenplanung für die Fahrer in Stufe 1 bis 6
- Keine Nutzerverwaltung mit Rollen. Ein gemeinsamer Zugang je Gerät, PIN für den Adminbereich. Erweiterbar, wenn nötig.
- Kein Chat mit dem Agenten, keine Live-Mithörfunktion

---

## 7. Mockup-Abnahme

- **16.09.2026 – Adminansicht als klickbare Desktop-Seite:** `gui/mockups/admin-desktop.html`, alle sieben Bereiche mit Beispieldaten, abgenommen. Ergänzungen: Zähler an Reitern, Filter „Mit Hinweis"/„Ohne Alias", Feiertags-Vorschläge mit Ansagetext je Sondertag, „Abgelehnte Adressen" unter Lieferung, Kunden-Löschwunsch unter Daten. Mockup ist Vorlage für die Templates.
- **16.09.2026 – Adminansicht (Menü):** als Bild abgenommen. Änderungen: Kennzahlen-Leiste in jedem Bereich, Preisabweichung als Badge am Gericht, Alias-Vorschläge mit Zähler „nicht erkannt", Reiter statt Seitenleiste, Liste statt Kacheln.
- **17.09.2026 – Betriebsansicht gebaut (T-3.1, T-3.3):** `api/gui/templates/betrieb/index.html` mit Kopfzeile und drei Spalten, `fragments/heute.html` als HTMX-Fragment der Spalte "Heute", `api/gui/sse.py` als Ereignisstrom. Noch offen: Knoepfe der Kopfzeile (T-3.2), Spalte "Rueckrufe" (T-3.4), Spalte "Neue Bestellungen" (T-4.7).
- **25.09.2026 – Spalte "Neue Bestellungen" gebaut (T-4.7):** Karten, Passt, Nochmal senden, Korrektur-Kasten; im Browser durchgeklickt (Tauschen mit Hinweis, Speichern, Passt). Kopfzeile (T-3.2) und Rueckrufe (T-3.4) sind seit 18.09.2026 fertig.
- **16.09.2026 – Betriebsansicht:** als Bild im Chat abgenommen. Änderungen gegenüber der ersten Spec: dritter Kopfzeilen-Zustand (Überlauf), Fehlerkarte als eigener Kartenzustand, Abholcode groß, „Wartezeit" als Wort auf den Knöpfen, Kartennummer vor jedem Gerichtnamen.
