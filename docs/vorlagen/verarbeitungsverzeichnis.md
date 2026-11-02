# Verzeichnis von Verarbeitungstätigkeiten (Entwurf)

> Draft, not legal advice (limits of D20). Serves L9 (Art. 30 record, `docs/09_OPERATIONS_LEGAL.md` section 1): mandatory, the Art. 30(5) exemption does not apply. One block per processing activity of L3. **The filled copy stays outside the repository** (L9, like the DSFA draft): copy this file out, fill in every `<...>` and `<offen: ...>` there, never commit the filled version. Maxi fills in and signs.

## Verzeichnis von Verarbeitungstätigkeiten nach Art. 30 Abs. 1 DSGVO

*Entwurf, keine Rechtsberatung. Stand: <Datum>.*

**Verantwortlicher:** <Firmenname>, <Adresse>, E-Mail <E-Mail-Adresse>, Telefon <Telefonnummer>.
**Datenschutzbeauftragter:** <Name>, <E-Mail-Adresse des Beauftragten> (D9: Maxi selbst; Risiko eines Interessenkonflikts vermerkt, EuGH C-453/21).
**Gemeinsam Verantwortliche:** <offen: keine bekannt; Maxi trägt ein oder streicht>

Gemeinsame technische und organisatorische Maßnahmen (Art. 32), für alle Blöcke: Zugriff auf die Agent-API nur mit Token; Datenbankrolle der Anwendung ist nicht der Eigentümer; Sicherungskopien clientseitig verschlüsselt, Speicher in der EU; getrennte Mandanten; Server und Container-Logs 14 Tage; Not-Aus "KI pausieren". <offen: Maxi ergänzt die Maßnahmen für Räume, Geräte (Tablet, Kasse) und Zugangskonten des Teams>

### V1 Bestellannahme und Reservierung per Telefon

- **Zweck:** Bestellungen (Abholung, Lieferung) und Reservierungen am Telefon aufnehmen und ausführen; Sprache dafür live verarbeiten.
- **Rechtsgrundlage:** Art. 6 Abs. 1 lit. b DSGVO.
- **Betroffene:** Anrufer, Gäste, Besteller.
- **Datenkategorien:** Name, Telefonnummer, Lieferadresse, Bestellung, Reservierungsdaten, Notizen, Gesprächsinhalt während des Anrufs.
- **Empfänger:** Team (Tablet, Kassenarbeitsplatz); Auftragsverarbeiter: Hosting, Spracherkennung, Sprachausgabe, Textmodell <offen: Anbieter je Dienst, L5, U8>.
- **Drittland:** <offen: je Anbieter, L7>.
- **Löschfristen:** Name, Telefonnummer, Adresse, Notizen 90 Tage nach Bestell- oder Reservierungsdatum; Bestellzeilen ohne persönliche Angaben bleiben, solange die Steuerberatung es verlangt (docs/03 Löschkonzept).

### V2 Allergie-Hinweis für die Küche

- **Zweck:** eigene Allergie des Anrufers als Hinweis für die Küche notieren.
- **Rechtsgrundlage:** Art. 9 Abs. 2 lit. a DSGVO (ausdrückliche Einwilligung, L13); bei einer anderen erwachsenen Person wird nur "ohne <Zutat>" ohne Grund gespeichert.
- **Betroffene:** Anrufer und deren Kinder.
- **Datenkategorien:** Gesundheitsdatum (Allergie), Protokoll der Antwort (`consent_log`, ohne Nummer).
- **Empfänger:** Team, Küche.
- **Löschfristen:** Notiztext bis zum Ende des Geschäftstags der Bestellung, bei Reservierung die Nacht nach dem Termin; `consent_log` bis zum Ende des dritten Kalenderjahres nach dem Jahr der Antwort.

### V3 Wiedererkennung der Rufnummer

- **Zweck:** Kundendatensatz finden, um nachzufragen (z. B. gleiche Adresse wie beim letzten Mal).
- **Rechtsgrundlage:** Art. 6 Abs. 1 lit. f DSGVO; Abwägung als eine Seite in der DSFA (L12). <offen: Abwägung ist Maxis Text in der DSFA>
- **Betroffene:** wiederkehrende Anrufer.
- **Datenkategorien:** Rufnummer, Name, Adressen, letzter Kontakt; Widersprüche (`recognition_objections`).
- **Löschfristen:** Kunden und Adressen 24 Monate nach dem letzten Kontakt; Widerspruch bis zur Rücknahme.

### V4 Anrufstatistik, Anrufprotokoll, Kostenprotokolle

- **Zweck:** Betrieb, Fehlersuche, Verbesserung, Kosten im Blick halten; Lernen aus echten Anrufen ohne Ton.
- **Rechtsgrundlage:** Art. 6 Abs. 1 lit. f DSGVO.
- **Betroffene:** Anrufer.
- **Datenkategorien:** Rufnummer (`calls.caller_id`), Uhrzeit, Dauer, Ergebnis, Kosten; Anrufprotokoll (nur volle Stunde, Anliegen, Ergebnis, Wortlaut, Probleme, ohne Namen und Nummern).
- **Löschfristen:** `caller_id` 90 Tage nach dem Anruf; Anrufprotokoll 90 Tage; Container- und Caddy-Logs 14 Tage; n8n-Ausführungen 7 Tage; Outbox-Inhalte 7 Tage nach Versand, spätestens 30 Tage, Zeile nach 90 Tagen. Asterisk führt keine Gesprächsdatensätze.

### V5 Steuerliche Aufbewahrung

- **Zweck:** Erfüllung steuerlicher Aufzeichnungs- und Aufbewahrungspflichten; die Kasse bleibt das einzige Buchungssystem (L18, D2).
- **Rechtsgrundlage:** Art. 6 Abs. 1 lit. c DSGVO.
- **Datenkategorien:** Bestellzeilen ohne Name, Nummer und Notizen.
- **Löschfristen:** solange die Steuerberatung es verlangt. <offen: Dauer>

### V6 Aufzeichnung, Abschriften, Auswertung (nur mit Einwilligung)

- **Zweck:** Verbesserung des Assistenten; im Modus `shadow` Aufzeichnung der Team-Anrufe (L11).
- **Rechtsgrundlage:** Art. 6 Abs. 1 lit. a DSGVO; bei Team-Anrufen zusätzlich die schriftliche Einwilligung des Teammitglieds.
- **Betroffene:** Anrufer, Teammitglieder.
- **Datenkategorien:** Tonaufnahme, Abschrift.
- **Löschfristen:** Aufnahme 30 Tage, bei Widerruf sofort; Abschrift 90 Tage.
- **Hinweis:** <offen: noch nicht gebaut (T-11.3, T-7.x); Block erst aktivieren, wenn die Aufzeichnung läuft>

### V7 Zugangskonten und Protokoll der Team-Aktionen

- **Zweck:** Bedienung der Tablet-Oberfläche, Nachvollziehbarkeit der Aktionen des Teams.
- **Rechtsgrundlage:** Art. 6 Abs. 1 lit. b, c, f DSGVO (U5). § 26 Abs. 1 BDSG wird nicht herangezogen (EuGH C-34/21).
- **Betroffene:** Teammitglieder.
- **Datenkategorien:** Konten, Protokoll mit Kennungen, Gründen und Bezeichnungen, ohne Namen, Nummern und Notizen von Gästen. Das Protokoll bewertet kein Personal.
- **Löschfristen:** 12 Monate.

### V8 Anfragen betroffener Personen

- **Zweck:** Auskunft, Löschung, Widerspruch fristgerecht bearbeiten und nachweisen (Art. 12 Abs. 3, Art. 5 Abs. 2).
- **Rechtsgrundlage:** Art. 6 Abs. 1 lit. c DSGVO.
- **Datenkategorien:** Anfrage-Kennung, Eingangs- und Antwortdatum, Art, Prüfangaben, Ergebnis.
- **Löschfristen:** Prüfangaben 3 Monate nach der Antwort, Rest bis zum Ende des dritten Kalenderjahres nach dem Jahr der Antwort. <offen: Annahme vom 21.10.2026, Maxi bestätigt oder ändert>

### V9 Sicherungskopien

- **Zweck:** Wiederherstellung nach einem Ausfall.
- **Rechtsgrundlage:** Art. 6 Abs. 1 lit. f DSGVO.
- **Empfänger:** Speicheranbieter in der EU <offen: Anbieter, L5>.
- **Löschfristen:** rollierend 30 Tage; Löschungsprotokoll außerhalb der Datenbank (ohne personenbezogene Daten) wird nach jeder Wiederherstellung neu angewandt (U7, T-9.3).

### Auftragsverarbeiter und Übermittlungen (Übersicht)

| Kategorie | Anbieter | Vertrag Art. 28 | Region | Drittland-Grundlage |
|---|---|---|---|---|
| Hosting (EU-Server) | <offen> | <offen> | <offen> | <offen> |
| Sicherungsspeicher | <offen> | <offen> | <offen> | <offen> |
| Spracherkennung | <offen> | <offen> | <offen> | <offen> |
| Sprachausgabe | <offen> | <offen> | <offen> | <offen> |
| Textmodell | <offen> | <offen> | <offen> | <offen> |
| Push-Dienst (falls extern) | <offen> | <offen> | <offen> | <offen> |

Jeder Vertrag verbietet das Training mit unseren Daten und regelt Unterauftragnehmer und Löschung bei Vertragsende (U8).
