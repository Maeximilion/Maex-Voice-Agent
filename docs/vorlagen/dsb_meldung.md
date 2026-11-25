# Meldung des Datenschutzbeauftragten und Pflichtenliste (Entwurf)

> Draft, not legal advice (limits of D20). Serves D9 (`docs/09_OPERATIONS_LEGAL.md` section 1): Maxi is his own data protection officer (decided 20.10.2026, against the recommendation; recorded risk: conflict of interest, CJEU C-453/21, Art. 38(6)). Part 1 says what to report and what to publish (Art. 37(7)); part 2 is the one-page checklist of every duty from docs/09 with the step it blocks. Maxi fills in, reports and publishes. Competent is the data protection authority of the federal state where the controller has its seat; the state authorities take the report through an online form on their website, and the law names no fee and no deadline (check of 11.11.2026): <offen: Name der Aufsichtsbehörde und Adresse ihres Meldeformulars>.

> **Check of 11.11.2026 (docs/09 section 1, D9): more than a risk.** An officer is mandatory here (§ 38(1) sentence 2 BDSG, because the DSFA is due). The authorities see a conflict of interest as the rule when the managing director or owner is named (Hessian authority citing C-453/21; DSK short paper 12), so the authority can reject this report, and the DSFA then lacks the officer's advice (L10). The route without that conflict is an external officer (Art. 37(6) GDPR); part 1 works for either, only the name changes. **Maxi decided again on 11.11.2026, knowing this: he stays the officer.** If the authority objects to the report, an external officer is named.

## Teil 1: Meldung nach Art. 37 Abs. 7 DSGVO

*Entwurf, keine Rechtsberatung. Stand: <Datum>.*

### An die Aufsichtsbehörde melden

- Verantwortlicher: <Firmenname>, <Adresse>, <Telefonnummer>, <E-Mail-Adresse>.
- Datenschutzbeauftragter: <Name>, <Adresse>, <Telefonnummer>, <E-Mail-Adresse des Beauftragten>. Der Beauftragte ist zugleich Geschäftsinhaber <offen: Maxi bestätigt; die Entscheidung vom 20.10.2026 und das Risiko eines Interessenkonflikts sind im Repo vermerkt (D9)>.
- Bestellung zum Beauftragten schriftlich festhalten: Datum <Datum>, Unterschrift <Name>.

### Veröffentlichen

Die Kontaktdaten des Beauftragten (nicht notwendigerweise der Name) stehen im Datenschutzhinweis (`datenschutzhinweis.md`, Abschnitt 1) auf der Website. Für das Team stehen sie in `team_information.md`.

## Teil 2: Pflichtenliste

Eine Pflicht, die als "erster echter Anruf" markiert ist, blockiert jeden echten Kundenanruf in jedem Modus. Jede offene Pflicht blockiert die T-7.x-Aufgaben und den Wechsel in den Modus `overflow` (docs/09). Gebaut ist nichts davon durch diese Entwürfe; veröffentlichen, unterschreiben und melden bleibt bei Maxi.

| Pflicht | Entwurf | Maxi tut | Blockiert |
|---|---|---|---|
| L4 Datenschutzhinweis online, Adresse in `service_config.privacy_notice_address` | `datenschutzhinweis.md` | ausfüllen, veröffentlichen, Adresse eintragen | erster echter Anruf |
| L5 Auftragsverarbeitungsvertrag je Dienst (Server, Sicherung, Spracherkennung, Sprachausgabe, Textmodell, Push-Dienst falls extern) | Übersicht in `verarbeitungsverzeichnis.md` | je Dienst unterschreiben, kein Training, EU-Region (U8) | der erste Schlüssel eines Dienstes mit echten Daten |
| L6 Dienste ohne Speicherung, Audio-Logging beim Anbieter aus | Zeile im Datenschutzhinweis | im Vertrag festhalten | erster echter Anruf an einem Sprachdienst |
| L7 Drittlandprüfung je Anbieter | Abschnitt 7 des Hinweises | je Anbieter prüfen, Grundlage eintragen | erster echter Anruf, D15 vor G1 |
| L8 Löschkonzept | Fristen im Hinweis | Löschjob und Tests sind gebaut oder offen: T-11.2, T-11.7, T-6.7, T-9.3 | erster echter Anruf |
| L9 Verzeichnis von Verarbeitungstätigkeiten | `verarbeitungsverzeichnis.md` | ausfüllen, Kopie außerhalb des Repos ablegen | erster echter Anruf |
| L10 DSFA fertigstellen, mit Rat des Beauftragten | Entwurf v0.1 liegt außerhalb des Repos | fertigstellen (Abwägung L12 als eine Seite) | erster echter Anruf in jedem Modus |
| L11 Informationsblatt, Einwilligungen des Teams | `team_information.md` | austeilen, unterschreiben lassen, Kopien außerhalb des Repos | Informationsblatt: erster echter Anruf; Einwilligungen: Modus `shadow` mit Aufzeichnung |
| L2 Einwilligungsfrage, Aufbewahrung, Stopp der Aufnahme | Abschnitt 4 des Hinweises | gebaut am 02.11.2026 (T-11.3), aus; einschalten erst nach den Einwilligungen des Teams (L11) | Modus `shadow` mit Aufzeichnung |
| L1 KI-Hinweis, Eval "Sind Sie ein Mensch?" | Abschnitt 2 des Hinweises | nichts; gebaut am 26.10.2026 (T-11.5); offen ist ein Eval für Anrufer, die den Assistenten für einen Menschen halten (#638) | erster echter Anruf (erledigt im Code) |
| N1 Kennzeichnung der synthetischen Stimme (Art. 50 Abs. 2 KI-Verordnung, Leitlinien vom 05.11.2026) | keiner | nichts; Messung und Notiz sowie die Kennzeichnung gespeicherter Aufnahmen sind eine Aufgabe im Code (#637) | Notiz: erster echter Anruf; Kennzeichnung: Aufzeichnung |
| N3 KI-Kompetenz des Teams (Art. 4 KI-Verordnung) | `team_einweisung.md` | Einweisung halten, Blatt unterschrieben außerhalb des Repos ablegen | erster echter Anruf |
| L15 Hinweis auf das Anrufprotokoll | Abschnitt 5 des Hinweises | mit dem Hinweis veröffentlichen | Hinweis online |
| L16 Piper-Lizenz | keiner | vor jeder Weitergabe einer Kopie entscheiden (E16) | Weitergabe einer Kopie |
| L17 Allergenliste mit Zusatzstoffen auf der Website und gedruckt bei jeder Lieferung | `allergenliste.md` | Werte aus der Datenbank erzeugen, Zusatzstoffe in der Kasse pflegen und eintragen, veröffentlichen, drucken | erste echte Bestellung |
| L18 Verfahrensdokumentation, Tagesbericht, Steuerberater | `verfahrensdokumentation_telefon.md` | ausfüllen, die Frage aus Abschnitt 6 der Steuerberatung stellen | erste echte Bestellung |
| N2 Lieferbedingungen, kein Widerrufsrecht, Bestätigung auf Papier (§ 312f Abs. 2 BGB) | `lieferbedingungen.md` | ausfüllen, veröffentlichen, gedruckt mit dem Kassenbeleg jeder Lieferung und jeder telefonisch bestellten Abholung beilegen | erste echte Bestellung |
| D9 Beauftragter melden, Kontakt veröffentlichen | Teil 1 oben | melden (Art. 37 Abs. 7), Kontakt in den Hinweis | erster echter Anruf |
| U2 Anfragen betroffener Personen | Ablauf in docs/09 Abschnitt 2 | Anfrageprotokoll als Datei außerhalb des Repos führen | erster echter Anruf |
| U7 Sicherungen verschlüsselt, Hinweis zu Backups | Abschnitt 8 des Hinweises | Speicheranbieter mit Vertrag wählen | echte Daten auf dem Server |
| Sicherheitsaudit | docs/09 Abschnitt 5 | entscheiden, ob und wann es läuft | erster Anruf von außerhalb des Arbeitsplatzes, jedes Tor ab G1 |

<offen: Welche Pflicht den Wechsel in `overflow` zusätzlich zu den oben genannten blockiert, steht nur pauschal in docs/09 ("every open duty"); die Zeilen oben nennen deshalb nur den Schritt, an dem die Pflicht zuerst blockiert.>
