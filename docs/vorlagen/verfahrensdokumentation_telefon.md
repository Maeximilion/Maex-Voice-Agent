# Verfahrensdokumentation Telefonkanal (Entwurf)

> Draft, not legal advice (limits of D20). Serves L18 and D2 (`docs/09_OPERATIONS_LEGAL.md` section 1; `docs/02_ARCHITECTURE.md` section 2a): GoBD procedure documentation for the phone channel. It is a reading of the general rule, no source names an order system without payment (review of 02.11.2026); the tax adviser may correct it. Maxi fills in the placeholders and keeps the signed copy with the register's own procedure documentation, outside the repository.

## Verfahrensdokumentation Telefonkanal <Pilotbetrieb>

*Entwurf, keine Rechtsberatung. Stand: <Datum>. Verantwortlich: <Name>.*

### 1. Zweck und Abgrenzung

Am Telefon des Restaurants nimmt ein KI-Assistent Reservierungen, Abholbestellungen und Lieferbestellungen auf. Dieses Dokument beschreibt, wie eine solche Bestellung bis zur Buchung in der Kasse gelangt.

### 2. Der Assistent nimmt keine Zahlung an

Der Assistent nimmt kein Geld an, kennt keine Zahlungsart, erzeugt keine Quittung und hat keine Kassenfunktion. Er braucht deshalb keine eigene technische Sicherheitseinrichtung (TSE) (AEAO zu § 146a Nr. 1.2, als allgemeine Regel gelesen). Die Zahlung geschieht beim Abholen oder bei der Lieferung gegenüber dem Team. <offen: Zahlungsweise bei Lieferung eintragen>

### 3. Die Kasse ist das einzige Buchungssystem

Die Kasse (<Kassensystem> von <Kassenanbieter>) mit TSE bleibt das einzige System, das Umsätze bucht, Quittungen ausgibt und Küchenbons druckt (D2). Der Kassenanbieter bietet keine Schnittstelle (02.11.2026); es gibt keine automatische Übergabe. Jede Bestellung des Assistenten wird vom Team von Hand in die Kasse eingegeben.

<offen: Name und Version der Kasse, TSE-Seriennummer und Verfahrensdokumentation der Kasse selbst; Maxi trägt sie ein und verweist hierher>

### 4. Ablauf einer Bestellung

1. **Annahme.** Der Anrufer bestätigt die vorgelesene Bestellung mit Gesamtpreis und Liefergebühr ausdrücklich. Erst dann wird sie `confirmed`. Preise, Zonen und Verfügbarkeit kommen aus der Datenbank. Jede Bestellung trägt die Kennung des Anrufs (`call_id`) und eine Abholnummer.
2. **Eingabezettel.** Die Druckbrücke druckt jede bestätigte Bestellung auf den Haupt-Bondrucker an der Kasse mit der Kopfzeile "NICHT IN KASSE – bitte eingeben". Der Zettel ist weder Küchenbon noch Quittung.
3. **Eingabe.** Das Team tippt die Bestellung in die Kasse. Die Kasse bucht mit TSE und druckt Quittung und Küchenbon auf ihre Drucker.
4. **Markierung.** Danach tippt das Team auf dem Tablet "In Kasse eingegeben". Das Tablet speichert den Zeitpunkt (`orders.register_entered_at`).
5. **Korrektur.** Wird eine Bestellung nach der Bestätigung geändert, druckt die Druckbrücke einen neuen Zettel mit höherer Revision; nur der Zettel der neuesten Revision zählt.

### 5. Tagesbericht der nicht eingegebenen Bestellungen

Eine Bestellung, die vom Vortag noch nicht als "In Kasse eingegeben" markiert ist, erscheint ab dem nächsten Geschäftstag als rote Karte auf dem Tablet, und es geht eine Mitteilung pro Tag. Das Team prüft die rote Karte, gibt nach oder klärt, warum die Bestellung nicht gebucht wurde (z. B. nicht abgeholt), und markiert sie. So bleibt jede Bestellung des Assistenten nachweisbar gebucht oder erklärt.

### 6. Aufbewahrung

- **Bestellzeilen ohne persönliche Angaben** (Positionen, Preise, Zeiten, `call_id`) werden so lange aufbewahrt, wie die Steuerberatung es verlangt (L18). <offen: Dauer; Maxi fragt nach. Gelöscht wird erst nach deren Wort.>
- **Name, Telefonnummer, Adresse und Notizen** an der Bestellung werden nach 90 Tagen entfernt, die Bestellzeile bleibt (docs/03 Löschkonzept; Art. 17 Abs. 3 lit. b DSGVO für die steuerlichen Zeilen).
- **Papierzettel** (Eingabezettel): <offen: Wann sie vernichtet werden, legt Maxi fest; das Repo nennt keine Frist. Aufbewahrungspflichtig ist der Beleg der Kasse.>
- Sicherungskopien der Datenbank laufen rollierend 30 Tage (docs/03). Die Kasse führt ihre eigene Aufbewahrung.

### 7. Verantwortlichkeiten

| Aufgabe | Wer |
|---|---|
| Eingabe in die Kasse, Markierung auf dem Tablet | <Rolle im Team> |
| Prüfung der roten Karten und des Tagesberichts | <Rolle im Team> |
| Pflege dieses Dokuments, Änderungsvermerk | <Name> |

### 8. Änderungen

| Datum | Änderung | Wer |
|---|---|---|
| <Datum> | Erstfassung | <Name> |
