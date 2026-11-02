# Datenschutzhinweis (Entwurf)

> Draft, not legal advice (limits of D20: two AI models checked the questions, liability stays with the controller). Serves duty L4 (Art. 13 notice, `docs/09_OPERATIONS_LEGAL.md` section 1). Maxi fills in every `<...>` and `<offen: ...>`, publishes the page at `example.com/datenschutz` and then enters that address in `service_config.privacy_notice_address`. The greeting names the address only once it is set.

## Datenschutzhinweis für Anrufe bei <Pilotbetrieb>

*Entwurf, keine Rechtsberatung. Stand: <Datum>.*

### 1. Wer ist verantwortlich?

<Firmenname>, <Adresse>, E-Mail: <E-Mail-Adresse>, Telefon: <Telefonnummer>.

Datenschutzbeauftragter: <Name>, erreichbar unter <E-Mail-Adresse des Beauftragten> und <Adresse>. <offen: Maxi hat sich am 20.10.2026 selbst bestellt (D9); Kontakt veröffentlichen und der Aufsichtsbehörde melden, siehe dsb_meldung.md>

### 2. Mit wem sprechen Sie?

Wenn Sie bei uns anrufen, meldet sich möglicherweise ein KI-Assistent. Er nennt sich gleich zu Beginn so. Sie können jederzeit sagen, dass Sie mit einem Menschen sprechen möchten, dann geht der Anruf an unser Team. Auf die Frage, ob Sie mit einem Menschen sprechen, antwortet der Assistent immer wahrheitsgemäß.

Der Assistent nimmt Reservierungen, Abholbestellungen und Lieferbestellungen auf. Preise, Öffnungszeiten, Lieferzonen, Verfügbarkeit und Allergene kommen ausschließlich aus unserer Datenbank. Eine Bestellung oder Reservierung gilt erst, wenn Sie sie ausdrücklich bestätigt haben.

### 3. Welche Daten verarbeiten wir, wofür, auf welcher Grundlage?

| Zweck | Daten | Rechtsgrundlage |
|---|---|---|
| Bestellung (Abholung, Lieferung) und Reservierung annehmen und ausführen, Sprache dafür live verarbeiten | Name, Telefonnummer, Lieferadresse, Bestellung, Reservierungsdaten, Notizen, Gesprächsinhalt während des Anrufs | Art. 6 Abs. 1 lit. b DSGVO (Vertrag oder vorvertragliche Maßnahme) |
| Wiedererkennung Ihrer Rufnummer, damit wir z. B. fragen können, ob die Lieferung wieder an dieselbe Adresse geht | Rufnummer, Kundendatensatz | Art. 6 Abs. 1 lit. f DSGVO (berechtigtes Interesse an einem zügigen Ablauf); Sie können widersprechen, siehe Abschnitt 8 |
| Anrufstatistik, Anrufprotokoll, Kostenprotokolle, Betriebsprotokolle | Rufnummer, Uhrzeit, Dauer, Ergebnis, technische Daten | Art. 6 Abs. 1 lit. f DSGVO (Betrieb und Verbesserung des Dienstes, Fehlersuche) |
| Aufbewahrung steuerrelevanter Unterlagen | Bestellzeilen ohne Name, Telefonnummer und Notizen | Art. 6 Abs. 1 lit. c DSGVO (steuerliche Aufbewahrungspflichten) |
| Aufzeichnung von Gesprächen, Abschriften und deren Auswertung | Tonaufnahme, Abschrift | Art. 6 Abs. 1 lit. a DSGVO (Einwilligung), nur wenn Sie vorher ausdrücklich "Ja" sagen |
| Eigene Allergie als Hinweis für die Küche notieren | Angabe zu Ihrer Allergie (Gesundheitsdatum) | Art. 9 Abs. 2 lit. a DSGVO (ausdrückliche Einwilligung); der Assistent fragt vorher und protokolliert Ihre Antwort |

Der Assistent nennt gespeicherte Daten nie von sich aus laut, er fragt nur nach (z. B. ob die Lieferung an dieselbe Adresse gehen soll). Passt ein genannter Name nicht zur Nummer, legen wir einen neuen Kundendatensatz an.

### 4. Aufzeichnung nur mit Ihrer Einwilligung

Ein Gespräch wird nur aufgezeichnet, wenn Sie nach der Frage ausdrücklich "Ja" sagen. Bei "Nein" oder Schweigen läuft das Gespräch ohne Aufnahme weiter, Ihnen entsteht kein Nachteil. Die Aufnahme wird 30 Tage gespeichert. Sie können sie jederzeit mit "Aufnahme stoppen" beenden; dann wird die Aufnahme sofort gelöscht. Abschriften werden nach 90 Tagen gelöscht. <offen: Der Repo-Stand hat Aufzeichnung und Abschriften noch nicht gebaut (T-11.3, T-7.1). Diesen Abschnitt erst veröffentlichen, wenn sie in Betrieb gehen, sonst "Wir zeichnen keine Gespräche auf" schreiben.>

### 5. Anrufprotokoll

Das Team führt ein Anrufprotokoll ohne Tonaufnahme, um zu lernen, wie Gäste anrufen. Es enthält nur die volle Stunde des Anrufs (nicht die Minute), das Anliegen, das Ergebnis, Dauer und Notizen zu Wortlaut und Problemen; keine Namen und keine Telefonnummern. Löschung nach 90 Tagen.

### 6. Wer bekommt Ihre Daten (Auftragsverarbeiter nach Kategorie)?

Mit jedem Dienstleister, der Ihre Daten für uns verarbeitet, besteht ein Auftragsverarbeitungsvertrag nach Art. 28 DSGVO. Keiner darf mit Ihren Daten trainieren.

- Hosting: Server in der EU <offen: Anbieter, Land, Vertrag (L5, U8)>
- Speicher für Sicherungskopien, verschlüsselt auf unserer Seite, in der EU <offen: Anbieter (L5, U7)>
- Spracherkennung (Sprache zu Text) <offen: Anbieter und Region (L5, U8)>
- Sprachausgabe (Text zu Sprache) <offen: Anbieter und Region (L5, U8)>
- Textmodell des Assistenten <offen: Anbieter und Region (L5, U8)>
- Push-Dienst für Benachrichtigungen an das Team, falls extern betrieben <offen: ob extern und welcher Anbieter (L5)>
- Automatisierung (n8n): auf eigenem Server betrieben, kein externer Empfänger

Der Telefonanbieter, über dessen Leitung Ihr Anruf läuft, ist für die Übertragung selbst verantwortlich und kein Auftragsverarbeiter von uns. <offen: Name des Anbieters; ob er für uns aufzeichnet oder speichert (dann Auftragsverarbeiter, L5)>

### 7. Übermittlung in Drittländer

Wir wählen Dienstleister, die Daten in der EU verarbeiten. Wo ein Dienstleister oder dessen Unterauftragnehmer Daten außerhalb der EU verarbeitet oder darauf zugreifen kann (auch durch Support einer Muttergesellschaft), stützen wir die Übermittlung auf eine Zertifizierung nach dem EU-US Data Privacy Framework oder auf Standardvertragsklauseln mit einer Transfer-Folgenabschätzung (L7). <offen: je Dienstleister die Angabe, ob und welche Grundlage zutrifft; leer lassen, wenn keiner Daten außerhalb der EU verarbeitet>

### 8. Wie lange speichern wir?

| Daten | Dauer |
|---|---|
| Tonaufnahmen (nur mit Einwilligung) | 30 Tage, bei Widerruf sofort |
| Abschriften | 90 Tage |
| Anrufprotokoll ohne Ton | 90 Tage |
| Rufnummer im Anrufdatensatz | 90 Tage nach dem Anruf (die Statistik bleibt ohne Nummer) |
| Name, Telefonnummer, Adresse und Notizen an Bestellungen und Reservierungen | 90 Tage nach Bestell- oder Reservierungsdatum |
| Allergie-Hinweis | bis zum Ende des Geschäftstags der Bestellung, bei Reservierungen bis zur Nacht nach dem Termin |
| Protokoll Ihrer Antwort auf die Allergiefrage | bis zum Ende des dritten Kalenderjahres nach dem Jahr der Antwort |
| Rückrufwunsch (Nummer und Anlass) | 90 Tage nach Erledigung, spätestens 90 Tage nach Anlage |
| Kundendatensatz und Adressen | 24 Monate nach dem letzten Kontakt |
| Widerspruch gegen die Wiedererkennung | bis Sie ihn zurücknehmen, sonst würde die Wiedererkennung wieder eingeschaltet |
| Protokolle der Server und Container | 14 Tage |
| Ausführungen der Automatisierung (n8n) | 7 Tage |
| Zugriffsprotokoll des Teams (ohne Namen, Nummern, Notizen) | 12 Monate |
| Bestellzeilen ohne persönliche Angaben (steuerlich) | solange die Steuerberatung es verlangt <offen: Dauer; Maxi trägt sie ein, sobald bekannt (L18)> |
| Anfragen betroffener Personen | Prüfangaben 3 Monate nach der Antwort, Rest bis zum Ende des dritten Kalenderjahres nach dem Jahr der Antwort <offen: Annahme vom 21.10.2026, Maxi bestätigt oder ändert> |

Gelöschte Daten bleiben noch in Sicherungskopien, bis diese ablaufen (rollierend 30 Tage). Wird eine Sicherung zurückgespielt, löschen wir bereits Gelöschtes danach erneut, bevor der Assistent wieder Anrufe annimmt.

### 9. Ihre Rechte

Sie haben das Recht auf Auskunft (Art. 15), Berichtigung (Art. 16), Löschung (Art. 17), Einschränkung der Verarbeitung (Art. 18), Datenübertragbarkeit (Art. 20) und Widerspruch gegen Verarbeitung auf Grundlage berechtigter Interessen (Art. 21), insbesondere gegen die Wiedererkennung Ihrer Rufnummer. Eine erteilte Einwilligung können Sie jederzeit mit Wirkung für die Zukunft widerrufen (Art. 7 Abs. 3). Wenden Sie sich per Telefon, E-Mail oder persönlich an uns (Kontakt oben). Wir antworten innerhalb eines Monats und prüfen vorher, dass Sie die betroffene Person sind; die Rufnummer allein genügt dafür nicht. Steuerlich aufzubewahrende Bestellzeilen löschen wir nicht, sondern entfernen Name, Nummer und Notizen.

### 10. Beschwerde

Sie können sich bei einer Datenschutz-Aufsichtsbehörde beschweren: <Name und Adresse der zuständigen Aufsichtsbehörde>.

### 11. Pflicht zur Angabe, automatisierte Entscheidung

Ohne Name und Telefonnummer können wir keine Bestellung oder Reservierung aufnehmen; ohne Adresse keine Lieferung. Der KI-Assistent versteht Ihr Anliegen, aber Preise, Verfügbarkeit und Bestätigung entscheidet unsere Software nach festen Regeln aus der Datenbank. Wir treffen keine Entscheidung, die Sie rechtlich erheblich beeinträchtigt, allein automatisiert, ohne dass Sie zuvor bestätigt haben oder das Team übernehmen kann. <offen: Maxi prüft diesen Satz (Art. 22), die Repo-Dokumente enthalten dazu keine eigene Prüfung>
